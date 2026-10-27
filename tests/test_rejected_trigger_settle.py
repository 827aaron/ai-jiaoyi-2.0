"""条件单触发后子单被交易所拒绝（algoStatus REJECTED、没有 actualOrderId）的收尾。

账本取自 Demo 2026-10-27 INJUSDT 空仓 pos-42600c953a4c 的真实落盘：冻结峰值规则直连平仓
dc-pos-42600c953a4c-1 全部成交后，止盈条件单 1000000261008734 在 22:43:44.459Z 触发、
子单被拒，账本卡在 closing / exiting / child_state unknown。交易所读回走真实的
``CcxtBinanceFutures.get_bracket``，只把 ccxt 换成假件。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from unittest.mock import AsyncMock

import ccxt.async_support as ccxt
import pytest

from jiaoyi.alerting.stub import StubAlerter
from jiaoyi.contracts import Side
from jiaoyi.contracts.position import CloseReason, PositionStatus
from jiaoyi.execution.brackets import BracketManager
from jiaoyi.execution.exchange import (
    CcxtBinanceFutures,
    ExchangeBracket,
    ExchangeEntryOrder,
    ExchangePosition,
    IdentifiedCloseOrder,
    NormalizedBracketSpec,
)
from jiaoyi.execution.hb_executor import HbExecutorAdapter
from jiaoyi.execution.lifeboat import BinanceLifeboat
from jiaoyi.execution.registry import PositionRegistry, TrackedPosition
from tests.fakes import FakeExchange, FakeHbApi

LEDGER = Path(__file__).parent / "fixtures" / "inj_rejected_tp_ledger.json"
PID = "pos-42600c953a4c"
SYMBOL = "INJUSDT"
EXECUTOR = "8F3w2J7r8kks22ggzGWfNCajzSYAbBVeZaNSMHMdWByi"
ENTRY_CID = "x-nbQe1H39SIJUT65ed94bb1acfaca42"
TP_CID = "br-tp-pos-42600c953a4c-1"
NOW = datetime(2026, 10, 27, 22, 50, tzinfo=UTC)


def tp_algo(**changes: Any) -> dict[str, Any]:
    """Binance Query Algo Order 对这张止盈单的回包。"""
    item: dict[str, Any] = {
        "symbol": SYMBOL,
        "algoId": "1000000261008734",
        "clientAlgoId": TP_CID,
        "orderType": "TAKE_PROFIT_MARKET",
        "side": "BUY",
        "quantity": "1194.7",
        "triggerPrice": "7.923",
        "positionSide": "BOTH",
        "workingType": "CONTRACT_PRICE",
        "reduceOnly": True,
        "priceProtect": False,
        "closePosition": False,
        "algoStatus": "REJECTED",
        "createTime": 1793136031000,
        "updateTime": 1793141024459,
        "triggerTime": 1793141024459,
        "actualOrderId": "",
    }
    item.update(changes)
    return item


class IncidentExchange:
    """INJ 现场：开仓单已成交、直连平仓单已全成；条件单读回经真实解析与核验。"""

    def __init__(self, *, held: float, algo: dict[str, dict[str, Any]]) -> None:
        self.held = held
        self.algo = algo
        self.events: list[tuple[str, str]] = []
        self.child_lookup: Exception | dict[str, Any] = ccxt.OrderNotFound("Order does not exist")
        self.venue = CcxtBinanceFutures.__new__(CcxtBinanceFutures)
        self.venue._markets_loaded = True
        ex = AsyncMock()
        ex.fapiPrivateGetAlgoOrder.side_effect = self._algo
        ex.fapiPrivateGetOrder.side_effect = self._child
        self.venue._ex = ex
        self.ccxt = ex

    async def _algo(self, request: dict[str, Any]) -> dict[str, Any]:
        cid = request.get("clientAlgoId")
        self.events.append(("algo", str(cid or request.get("algoId"))))
        for raw in self.algo.values():
            if raw["clientAlgoId"] == cid or raw["algoId"] == request.get("algoId"):
                return raw
        raise ccxt.OrderNotFound("synthetic absent")

    async def _child(self, request: dict[str, Any]) -> dict[str, Any]:
        self.events.append(("child_lookup", str(request)))
        if isinstance(self.child_lookup, Exception):
            raise self.child_lookup
        return self.child_lookup

    async def position_mode(self) -> Literal["one_way", "hedge"]:
        return "one_way"

    async def positions_evidence(self) -> list[ExchangePosition]:
        return [ExchangePosition(SYMBOL, Side.SHORT, self.held)] if self.held else []

    async def assert_entry_clear(self, symbol: str) -> None:
        return None

    async def entry_order(self, symbol: str, client_order_id: str) -> ExchangeEntryOrder | None:
        if (symbol, client_order_id) != (SYMBOL, ENTRY_CID):
            return None
        return ExchangeEntryOrder(
            SYMBOL, ENTRY_CID, "323448030", "sell", 1194.7, 1194.7, "FILLED", "BOTH"
        )

    async def normalized_bracket_spec(
        self, symbol: str, quantity: float, trigger_price: float
    ) -> NormalizedBracketSpec:
        return NormalizedBracketSpec(quantity, round(trigger_price, 3))

    async def get_bracket(self, symbol, *, algo_id=None, client_order_id=None):
        return await self.venue.get_bracket(
            symbol, algo_id=algo_id, client_order_id=client_order_id
        )

    async def get_lifeboat(self, symbol, *, client_order_id, algo_id=None):
        return await self.venue.get_lifeboat(
            symbol, client_order_id=client_order_id, algo_id=algo_id
        )

    async def open_brackets(self, symbol=None) -> list[ExchangeBracket]:
        return []

    async def place_bracket(
        self, symbol, close_side, quantity, trigger_price, client_order_id, *, kind, working_type
    ) -> ExchangeBracket:
        self.events.append(("create", client_order_id))
        raw = tp_algo(
            algoId=str(1000000261009000 + len(self.algo)),
            clientAlgoId=client_order_id,
            orderType="TAKE_PROFIT_MARKET" if kind == "take_profit" else "STOP_MARKET",
            side=close_side.upper(),
            quantity=str(quantity),
            triggerPrice=str(trigger_price),
            workingType=working_type,
            algoStatus="NEW",
            triggerTime=0,
        )
        self.algo[client_order_id] = raw
        return await self.venue.get_bracket(symbol, client_order_id=client_order_id)

    async def place_lifeboat(self, *args: Any) -> ExchangeBracket:
        raise AssertionError("lifeboat is still live; it must not be recreated")

    async def cancel_bracket(self, symbol, algo_id, *, expected_client_order_id) -> None:
        self.events.append(("cancel", expected_client_order_id))
        raise AssertionError("no cancel expected")

    async def cancel_lifeboat(self, symbol, algo_id, *, expected_client_order_id) -> None:
        self.events.append(("cancel", expected_client_order_id))
        raise AssertionError("no cancel expected")

    async def place_close(self, symbol, close_side, quantity, client_order_id):
        self.events.append(("direct", client_order_id))
        raise AssertionError("the position is already flat; no new close order")

    async def close_order(self, symbol: str, client_order_id: str) -> IdentifiedCloseOrder | None:
        if client_order_id != "dc-pos-42600c953a4c-1":
            return None
        return IdentifiedCloseOrder(
            symbol=SYMBOL,
            client_order_id=client_order_id,
            order_id="323465572",
            close_side="buy",
            quantity=1194.7,
            filled_quantity=1194.7,
            remaining_quantity=0,
            average_price=7.952,
            status="FILLED",
            position_side="BOTH",
            reduce_only=True,
            is_terminal=True,
        )

    def creates(self) -> list[str]:
        return [cid for action, cid in self.events if action == "create"]

    def actions(self, kind: str) -> list[str]:
        return [cid for action, cid in self.events if action == kind]


def load_ledger() -> dict[str, Any]:
    return json.loads(LEDGER.read_text())


def build(tmp_path: Path, ledger: dict[str, Any], exchange: IncidentExchange):
    registry = PositionRegistry(tmp_path / "positions.json")
    registry.put(TrackedPosition.model_validate(ledger))
    manager = BracketManager(
        exchange,
        registry,
        account_name="master_account",
        connector_name="binance_perpetual_testnet",
        clock=lambda: NOW,
    )
    api = FakeHbApi()
    api.executors[EXECUTOR] = {
        "executor_id": EXECUTOR,
        "controller_id": f"jy-{PID}",
        "trading_pair": "INJ-USDT",
        "status": "TERMINATED",
        "is_active": False,
        "is_trading": False,
        "close_type": "EARLY_STOP",
        "net_pnl_quote": 0.0,
        "cum_fees_quote": 0.0,
        "filled_amount_quote": 9646.0078,
        "custom_info": {"current_position_average_price": 8.074, "order_ids": [ENTRY_CID]},
    }
    alerter = StubAlerter()
    adapter = HbExecutorAdapter(
        api,
        BinanceLifeboat(FakeExchange(), clock=lambda: NOW),
        registry,
        alerter,
        account_name="master_account",
        connector_name="binance_perpetual_testnet",
        exchange=exchange,
        protection=manager,
        clock=lambda: NOW,
    )
    return registry, manager, adapter, alerter


async def test_inj_ledger_fixture_is_the_stuck_live_state() -> None:
    ledger = load_ledger()
    tp = ledger["server_protection"]["legs"][2]
    assert ledger["record"]["status"] == "closing"
    assert ledger["requested_close"] == "keeper"
    assert ledger["direct_close_order_status"] == "FILLED"
    assert ledger["server_protection"]["stage"] == "exiting"
    assert (tp["client_order_id"], tp["algo_status"], tp["child_state"]) == (
        TP_CID,
        "REJECTED",
        "unknown",
    )
    assert tp["triggered_at"] == "2026-10-27T22:43:44.459000Z"
    assert tp["actual_order_id"] is None


def same_cid_order(**changes: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "symbol": SYMBOL,
        "orderId": "323465999",
        "clientOrderId": TP_CID,
        "side": "BUY",
        "type": "MARKET",
        "origType": "TAKE_PROFIT_MARKET",
        "positionSide": "BOTH",
        "reduceOnly": True,
        "origQty": "1194.7",
        "executedQty": "0",
        "status": "REJECTED",
    }
    item.update(changes)
    return item


@pytest.mark.parametrize(
    "algo_status,lookup",
    [
        ("REJECTED", ccxt.OrderNotFound("Order does not exist")),
        ("EXPIRED", ccxt.OrderNotFound("Order does not exist")),
        ("REJECTED", same_cid_order()),
        ("REJECTED", same_cid_order(status="EXPIRED")),
    ],
)
async def test_inj_rejected_tp_after_direct_close_finishes_as_keeper_close(
    tmp_path: Path, algo_status: str, lookup: Exception | dict[str, Any]
) -> None:
    exchange = IncidentExchange(held=0.0, algo={TP_CID: tp_algo(algoStatus=algo_status)})
    exchange.child_lookup = lookup
    registry, _, adapter, _ = build(tmp_path, load_ledger(), exchange)
    await adapter.protection_tick()
    final = registry.get(PID)
    assert final.record.status is PositionStatus.CLOSED
    assert final.record.close_reason is CloseReason.KEEPER
    assert final.server_protection.stage == "done"
    assert final.server_protection.last_error is None
    tp = final.server_protection.legs[2]
    assert (tp.algo_status, tp.child_state, tp.actual_order_id) == (algo_status, "absent", None)
    assert tp.filled_quantity is None
    assert not exchange.actions("cancel") and not exchange.creates()
    assert not exchange.actions("direct")
    # 子单身份按 clientAlgoId 复核过一次：查无才认作没有子单。
    exchange.ccxt.fapiPrivateGetOrder.assert_any_await(
        {"symbol": SYMBOL, "origClientOrderId": TP_CID}
    )


@pytest.mark.parametrize(
    "change",
    [
        {"actualOrderId": None},
        {"triggerTime": None},
        {"algoStatus": "CANCELED"},
        {"algoStatus": "TRIGGERED"},
        {"algoStatus": "FINISHED"},
    ],
)
async def test_inj_rejected_tp_without_positive_evidence_stays_closing(
    tmp_path: Path, change: dict[str, Any]
) -> None:
    raw = tp_algo(**change)
    if change.get("actualOrderId", "") is None:
        raw.pop("actualOrderId")
    exchange = IncidentExchange(held=0.0, algo={TP_CID: raw})
    registry, _, adapter, _ = build(tmp_path, load_ledger(), exchange)
    await adapter.protection_tick()
    final = registry.get(PID)
    assert final.record.status is PositionStatus.CLOSING
    assert final.server_protection.stage == "exiting"
    assert final.server_protection.legs[2].child_state == "unknown"
    assert not exchange.actions("cancel") and not exchange.actions("direct")


@pytest.mark.parametrize(
    "lookup",
    [
        ccxt.RequestTimeout("synthetic timeout"),
        same_cid_order(status="NEW"),
        same_cid_order(status="FILLED", executedQty="1194.7"),
        same_cid_order(status="CANCELED", executedQty="100"),
        same_cid_order(side="SELL"),
        same_cid_order(symbol="OTHERUSDT"),
    ],
)
async def test_inj_rejected_tp_with_unproven_child_stays_closing(
    tmp_path: Path, lookup: Exception | dict[str, Any]
) -> None:
    exchange = IncidentExchange(held=0.0, algo={TP_CID: tp_algo()})
    exchange.child_lookup = lookup
    registry, _, adapter, _ = build(tmp_path, load_ledger(), exchange)
    await adapter.protection_tick()
    final = registry.get(PID)
    assert final.record.status is PositionStatus.CLOSING
    assert final.server_protection.stage == "exiting"
    assert final.server_protection.legs[2].child_state == "unknown"


def open_ledger() -> dict[str, Any]:
    """同一张单，但仓位还在：保命单与止损仍 NEW，止盈触发后子单被拒。"""
    ledger = load_ledger()
    ledger["record"].update(status="open", lifeboat=None)
    ledger.update(
        requested_close=None,
        direct_close_client_order_id=None,
        direct_close_generation=0,
        direct_close_quantity=None,
        direct_close_order_status=None,
        direct_close_order_id=None,
        frozen_peak_exit=None,
    )
    state = ledger["server_protection"]
    state.update(stage="armed", exit_reason=None, last_error=None, observed_quantity=1194.7)
    for leg in state["legs"][:2]:
        leg.update(state="confirmed", algo_status="NEW")
    return ledger


def live_algo(leg: dict[str, Any]) -> dict[str, Any]:
    return tp_algo(
        algoId=leg["algo_id"],
        clientAlgoId=leg["client_order_id"],
        orderType="STOP_MARKET",
        triggerPrice=str(leg["trigger_price"]),
        workingType=leg["working_type"],
        algoStatus="NEW",
        triggerTime=0,
    )


async def test_rejected_tp_while_position_open_rearms_and_alerts(tmp_path: Path) -> None:
    ledger = open_ledger()
    legs = ledger["server_protection"]["legs"]
    algo = {leg["client_order_id"]: live_algo(leg) for leg in legs[:2]}
    algo[TP_CID] = tp_algo()
    exchange = IncidentExchange(held=1194.7, algo=algo)
    registry, manager, _, alerter = build(tmp_path, ledger, exchange)
    assert await manager.sync(PID) == "armed"
    final = registry.get(PID)
    assert final.record.status is PositionStatus.OPEN
    assert final.server_protection.stage == "armed"
    assert final.server_protection.exit_reason is None
    assert exchange.creates() == ["br-tp-pos-42600c953a4c-2"]
    assert not exchange.actions("cancel") and not exchange.actions("direct")
    new_tp = registry.get(PID).server_protection.legs[-1]
    assert (new_tp.kind, new_tp.trigger_price, new_tp.algo_status) == (
        "take_profit",
        7.923,
        "NEW",
    )
    assert [a.title for a in alerter.sent] == ["条件单触发后子单被交易所拒绝"]
    assert TP_CID in alerter.sent[0].body
    old_tp = final.server_protection.legs[2]
    assert (old_tp.state, old_tp.algo_status, old_tp.child_state) == (
        "cancelled",
        "REJECTED",
        "absent",
    )
    assert old_tp.triggered_at is not None
    exchange.events.clear()
    exchange.child_lookup = ccxt.RequestTimeout("synthetic timeout")
    del exchange.algo[TP_CID]
    assert await manager.sync(PID) == "armed"
    assert ("algo", TP_CID) not in exchange.events
    assert not exchange.actions("child_lookup") and not exchange.creates()
    assert len(alerter.sent) == 1


def open_case(failed: int, **leg_changes: Any) -> tuple[dict[str, Any], IncidentExchange]:
    """仓位还在，第 ``failed`` 条腿（0 保命单 / 1 止损 / 2 止盈）触发后子单被拒，其余仍 NEW。"""
    ledger = open_ledger()
    legs = ledger["server_protection"]["legs"]
    legs[2].update(algo_status="NEW", triggered_at=None, child_state="absent")
    legs[failed].update(leg_changes)
    algo = {leg["client_order_id"]: live_algo(leg) for leg in legs[:2]}
    algo[TP_CID] = tp_algo(algoStatus="NEW", triggerTime=0)
    algo[legs[failed]["client_order_id"]].update(algoStatus="REJECTED", triggerTime=1793141024459)
    return ledger, IncidentExchange(held=1194.7, algo=algo)


@pytest.mark.parametrize(
    "failed,leg_changes,reason",
    [
        (1, {}, CloseReason.STOP_LOSS),
        (1, {"purpose": "trailing"}, CloseReason.TRAILING_STOP),
        (0, {}, CloseReason.LIFEBOAT),
    ],
)
async def test_rejected_stop_while_position_open_exits_without_rearm(
    tmp_path: Path, failed: int, leg_changes: dict[str, Any], reason: CloseReason
) -> None:
    ledger, exchange = open_case(failed, **leg_changes)
    registry, manager, _, alerter = build(tmp_path, ledger, exchange)
    assert await manager.sync(PID) == "exit"
    state = registry.get(PID).server_protection
    assert (state.stage, state.exit_reason) == ("exiting", reason)
    assert state.legs[failed].state == "cancelled"
    assert not exchange.creates() and not exchange.actions("cancel")
    assert [a.title for a in alerter.sent] == ["条件单触发后子单被交易所拒绝"]
    assert "不再补挂" in alerter.sent[0].body


async def test_take_profit_rejected_twice_exits_instead_of_rearming_again(tmp_path: Path) -> None:
    ledger, exchange = open_case(2, algo_status="NEW")
    exchange.algo[TP_CID].update(algoStatus="REJECTED", triggerTime=1793141024459)
    registry, manager, _, alerter = build(tmp_path, ledger, exchange)
    assert await manager.sync(PID) == "armed"
    second = "br-tp-pos-42600c953a4c-2"
    assert exchange.creates() == [second]
    exchange.algo[second].update(algoStatus="REJECTED", triggerTime=1793141100000)
    assert await manager.sync(PID) == "exit"
    state = registry.get(PID).server_protection
    assert (state.stage, state.exit_reason) == ("exiting", CloseReason.TAKE_PROFIT)
    assert exchange.creates() == [second]
    assert [a.dedup_key for a in alerter.sent] == [
        f"server_trigger_rejected:{PID}:take_profit-1",
        f"server_trigger_rejected:{PID}:take_profit-2",
    ]
    assert "第二次" in alerter.sent[1].body


async def test_triggering_then_rejected_reports_the_exit_as_rejected_after_trigger(
    tmp_path: Path,
) -> None:
    ledger, exchange = open_case(2)
    exchange.algo[TP_CID].update(algoStatus="TRIGGERING")
    registry, manager, _, alerter = build(tmp_path, ledger, exchange)
    assert await manager.sync(PID) == "exit"
    assert registry.get(PID).server_protection.exit_reason is CloseReason.TAKE_PROFIT
    assert not alerter.sent
    exchange.algo[TP_CID].update(algoStatus="REJECTED")
    assert await manager.prepare_exit(PID)
    assert [a.title for a in alerter.sent] == ["条件单触发后子单被交易所拒绝"]
    assert "触发后被拒" in alerter.sent[0].body
    assert registry.get(PID).server_protection.legs[2].state == "cancelled"


async def test_rejected_tp_while_position_open_is_not_a_close_via_adapter_tick(
    tmp_path: Path,
) -> None:
    ledger = open_ledger()
    legs = ledger["server_protection"]["legs"]
    algo = {leg["client_order_id"]: live_algo(leg) for leg in legs[:2]}
    algo[TP_CID] = tp_algo()
    exchange = IncidentExchange(held=1194.7, algo=algo)
    registry, _, adapter, _ = build(tmp_path, ledger, exchange)
    api = adapter._api
    api.executors[EXECUTOR].update(status="RUNNING", is_active=True, is_trading=True)
    await adapter.protection_tick()
    final = registry.get(PID)
    assert final.record.status is PositionStatus.OPEN
    assert final.requested_close is None
    assert not exchange.actions("direct") and not api.stopped
