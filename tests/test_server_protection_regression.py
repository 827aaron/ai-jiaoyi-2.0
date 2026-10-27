"""交易所保护编排的独立故障回归；只有合成订单，不连接任何服务。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Literal

import pytest

from jiaoyi.alerting.stub import StubAlerter
from jiaoyi.contracts import Side
from jiaoyi.contracts.position import CloseReason, PositionStatus
from jiaoyi.execution.brackets import BracketManager, ProtectionIncomplete
from jiaoyi.execution.exchange import (
    BracketSubmissionRejected,
    BracketSubmissionUnknown,
    EntryBasis,
    ExchangeBracket,
    ExchangeEntryOrder,
    ExchangeError,
    ExchangePosition,
    ExecutionBook,
    IdentifiedCloseOrder,
    IdentifiedCloseUnknown,
    NormalizedBracketSpec,
)
from jiaoyi.execution.hb_client import HbApiError
from jiaoyi.execution.hb_executor import HbExecutorAdapter
from jiaoyi.execution.lifeboat import BinanceLifeboat
from jiaoyi.execution.registry import PositionRegistry, TrackedPosition
from jiaoyi.execution.trailing import trailing_update
from tests.fakes import FakeExchange, FakeHbApi
from tests.test_execution import START, intent
from tests.test_keeper_patrol import position


class EvidenceExchange:
    """未知 CID 返回 None；不从净仓或请求数量伪造成交/子单状态。"""

    def __init__(self, side: Side) -> None:
        self.side, self.held = side, 2.0
        self.mode: Literal["one_way", "hedge"] = "one_way"
        self.entry = ExchangeEntryOrder(
            "BTCUSDT",
            "entry-1",
            "100",
            "buy" if side is Side.LONG else "sell",
            2.0,
            2.0,
            "FILLED",
            "BOTH",
        )
        self.orders: dict[str, ExchangeBracket] = {}
        self.events: list[tuple[str, str]] = []
        self.hidden: set[str] = set()
        self.unknown_submit = False
        self.reject_kind: str | None = None
        self.reject_immediate = False
        self.cancel_unknown = False
        self.on_place: Callable[[ExchangeBracket], Awaitable[None]] | None = None
        self.foreign_orders = False
        self.registry: PositionRegistry | None = None

    async def position_mode(self) -> Literal["one_way", "hedge"]:
        return self.mode

    async def positions_evidence(self) -> list[ExchangePosition]:
        return [ExchangePosition("BTCUSDT", self.side, self.held)] if self.held else []

    async def assert_entry_clear(self, symbol: str) -> None:
        if self.foreign_orders:
            raise ExchangeError("synthetic foreign order exists")

    async def entry_order(self, symbol: str, client_order_id: str) -> ExchangeEntryOrder | None:
        return self.entry if (symbol, client_order_id) == ("BTCUSDT", "entry-1") else None

    async def normalized_bracket_spec(
        self, symbol: str, quantity: float, trigger_price: float
    ) -> NormalizedBracketSpec:
        return NormalizedBracketSpec(quantity, round(trigger_price, 2))

    async def place_bracket(
        self,
        symbol: str,
        close_side: Literal["buy", "sell"],
        quantity: float,
        trigger_price: float,
        client_order_id: str,
        *,
        kind: Literal["stop_loss", "take_profit"],
    ) -> ExchangeBracket:
        assert client_order_id.startswith("br-")
        return await self._place(symbol, close_side, quantity, trigger_price, client_order_id, kind)

    async def place_lifeboat(self, symbol, close_side, quantity, trigger_price, client_order_id):
        assert client_order_id.startswith("lb-")
        return await self._place(
            symbol, close_side, quantity, trigger_price, client_order_id, "stop_loss"
        )

    async def _place(self, symbol, close_side, quantity, trigger_price, client_order_id, kind):
        assert self.registry is not None
        stored = self.registry.get("pos-1").server_protection
        leg = next(x for x in stored.legs if x.client_order_id == client_order_id)
        assert leg.state == "submit_pending"  # API调用之前已持久化提交意图。
        self.events.append(("create", client_order_id))
        if kind == self.reject_kind:
            raise BracketSubmissionRejected(
                "synthetic definite rejection", immediate_trigger=self.reject_immediate
            )
        assert client_order_id not in self.orders, "repeated submission of an existing CID"
        order = ExchangeBracket(
            symbol=symbol,
            algo_id=str(len(self.orders) + 1),
            client_order_id=client_order_id,
            kind=kind,
            close_side=close_side,
            quantity=quantity,
            trigger_price=trigger_price,
            position_side="BOTH",
            working_type="MARK_PRICE" if client_order_id.startswith("lb-") else "CONTRACT_PRICE",
            reduce_only=True,
            price_protect=False,
            close_position=False,
            algo_status="NEW",
            child_state="absent",
        )
        self.orders[client_order_id] = order
        if self.unknown_submit:
            self.hidden.add(client_order_id)
            raise BracketSubmissionUnknown(client_order_id)
        if self.on_place is not None:
            await self.on_place(order)
        return self.orders[client_order_id]

    async def get_bracket(
        self, symbol: str, *, algo_id: str | None = None, client_order_id: str | None = None
    ) -> ExchangeBracket | None:
        assert client_order_id is None or client_order_id.startswith("br-")
        return self._get(algo_id, client_order_id)

    async def get_lifeboat(self, symbol, *, algo_id=None, client_order_id=None):
        assert client_order_id is None or client_order_id.startswith("lb-")
        return self._get(algo_id, client_order_id)

    def _get(self, algo_id, client_order_id):
        if client_order_id is not None:
            order = self.orders.get(client_order_id)
        else:
            order = next((o for o in self.orders.values() if o.algo_id == algo_id), None)
        if order is None or order.client_order_id in self.hidden:
            return None  # 按 algoId 查也同样查无，模拟交易所读延迟。
        return order if algo_id is None or order.algo_id == algo_id else None

    async def open_brackets(self, symbol: str | None = None) -> list[ExchangeBracket]:
        return [
            o
            for o in self.orders.values()
            if o.algo_status == "NEW" and o.client_order_id.startswith("br-")
        ]

    async def cancel_bracket(
        self, symbol: str, algo_id: str, *, expected_client_order_id: str
    ) -> None:
        assert expected_client_order_id.startswith("br-")
        self._cancel(symbol, algo_id, expected_client_order_id)

    async def cancel_lifeboat(self, symbol, algo_id, *, expected_client_order_id):
        assert expected_client_order_id.startswith("lb-")
        self._cancel(symbol, algo_id, expected_client_order_id)

    def _cancel(self, symbol, algo_id, expected_client_order_id):
        order = self.orders[expected_client_order_id]
        assert (order.symbol, order.algo_id) == (symbol, algo_id)
        self.events.append(("cancel", expected_client_order_id))
        if self.cancel_unknown:
            raise ExchangeError("synthetic cancel accepted or not; unknown")
        self.orders[expected_client_order_id] = replace(order, algo_status="CANCELED")


class Harness:
    def __init__(self, root: Path, side: Side) -> None:
        self.path = root / "positions.json"
        self.registry = PositionRegistry(self.path)
        self.exchange = EvidenceExchange(side)
        self.manager = self.build()
        rec = position(
            START,
            side=side,
            quantity=2.0,
            entry_price=100.0,
            stop_loss_price=99.0 if side is Side.LONG else 101.0,
            take_profit_price=102.0 if side is Side.LONG else 98.0,
        )
        self.registry.put(
            TrackedPosition(
                record=rec,
                lifeboat_stop_pct=1.5,
                stop_loss_pct=1.0,
                take_profit_pct=2.0,
                entry_client_order_id="entry-1",
                entry_original_quantity=2.0,
                server_protection=self.manager.initial(2.0),
            )
        )

    def build(self) -> BracketManager:
        self.exchange.registry = self.registry
        return BracketManager(
            self.exchange,
            self.registry,
            account_name="synthetic-account",
            connector_name="synthetic-demo",
            clock=lambda: START,
        )

    def restart(self) -> None:
        self.registry = PositionRegistry(self.path)
        self.manager = self.build()

    def state(self):
        return self.registry.get("pos-1").server_protection

    def creates(self) -> list[str]:
        return [cid for action, cid in self.exchange.events if action == "create"]

    def cancels(self) -> list[str]:
        return [cid for action, cid in self.exchange.events if action == "cancel"]


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_fully_identified_three_legs_arm_once_and_survive_restart(tmp_path: Path, side: Side):
    h = Harness(tmp_path, side)
    assert await h.manager.sync("pos-1") == "armed"
    h.restart()
    assert await h.manager.sync("pos-1") == "armed"
    assert len(h.creates()) == 3 and not h.cancels()
    assert {leg.kind for leg in h.state().legs} == {"stop_loss", "take_profit"}
    assert {leg.purpose for leg in h.state().legs} == {"fixed", "lifeboat"}


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_partial_entry_is_only_provisional_and_new_coverage_precedes_cancel(
    tmp_path: Path,
    side: Side,
):
    h = Harness(tmp_path, side)
    h.exchange.entry = replace(h.exchange.entry, filled_quantity=1, status="PARTIALLY_FILLED")
    h.exchange.held = 1
    assert await h.manager.sync("pos-1") == "provisional"
    first = set(h.creates())
    assert not h.cancels()
    assert all(h.exchange.orders[c].quantity == 1 for c in first)
    h.exchange.entry = replace(h.exchange.entry, filled_quantity=2, status="FILLED")
    h.exchange.held = 2
    assert await h.manager.sync("pos-1") == "armed"
    assert set(h.cancels()) == first
    kinds = [kind for kind, _ in h.exchange.events]
    assert kinds.index("cancel") > max(i for i, kind in enumerate(kinds) if kind == "create")


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_unknown_submit_keeps_original_cid_across_retries_and_restart(
    tmp_path: Path, side: Side
):
    h = Harness(tmp_path, side)
    h.exchange.unknown_submit = True
    for attempt in range(3):
        if attempt == 2:
            h.restart()
        with pytest.raises(ProtectionIncomplete):
            await h.manager.sync("pos-1")
    assert len(h.creates()) == 1 and not h.cancels()
    assert h.state().stage != "armed"
    original_cid = h.creates()[0]
    h.exchange.hidden.clear()
    h.exchange.unknown_submit = False
    assert await h.manager.sync("pos-1") == "armed"
    assert len(h.creates()) == 3 and h.creates().count(original_cid) == 1


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_restart_rejects_wrong_trigger_price_without_rebuilding(tmp_path: Path, side: Side):
    h = Harness(tmp_path, side)
    await h.manager.sync("pos-1")
    cid = h.creates()[0]
    h.exchange.orders[cid] = replace(h.exchange.orders[cid], trigger_price=80.0)
    h.restart()
    with pytest.raises(ProtectionIncomplete):
        await h.manager.sync("pos-1")
    assert len(h.creates()) == 3 and not h.cancels()
    assert (
        h.state().last_error == "ProtectionIncomplete: 条件单身份、触发价、数量或只减仓参数不匹配"
    )


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_rejected_tp_keeps_confirmed_sl(tmp_path: Path, side: Side):
    h = Harness(tmp_path, side)
    h.exchange.reject_kind = "take_profit"
    with pytest.raises(ProtectionIncomplete):
        await h.manager.sync("pos-1")
    assert len(h.exchange.orders) == 2
    assert all(o.kind == "stop_loss" for o in h.exchange.orders.values())
    assert {o.working_type for o in h.exchange.orders.values()} == {"MARK_PRICE", "CONTRACT_PRICE"}
    assert not h.cancels() and h.state().stage != "armed"


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_entry_grows_during_create_without_false_armed(tmp_path: Path, side: Side):
    h = Harness(tmp_path, side)
    h.exchange.entry = replace(h.exchange.entry, filled_quantity=1, status="PARTIALLY_FILLED")
    h.exchange.held = 1

    async def fill_rest(order: ExchangeBracket) -> None:
        h.exchange.entry = replace(h.exchange.entry, filled_quantity=2, status="FILLED")
        h.exchange.held = 2
        h.exchange.on_place = None

    h.exchange.on_place = fill_rest
    assert await h.manager.sync("pos-1") == "provisional"
    assert len(h.creates()) == 1 and not h.cancels()
    assert await h.manager.sync("pos-1") == "armed"
    assert len(h.creates()) == 4


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_old_generation_trigger_unknown_blocks_new_generation_and_direct(
    tmp_path: Path, side: Side
):
    h = Harness(tmp_path, side)
    h.exchange.entry = replace(h.exchange.entry, filled_quantity=1, status="PARTIALLY_FILLED")
    h.exchange.held = 1
    await h.manager.sync("pos-1")
    old = h.creates()[0]
    h.exchange.entry = replace(h.exchange.entry, filled_quantity=2, status="FILLED")
    h.exchange.held = 2

    async def old_trigger(order: ExchangeBracket) -> None:
        h.exchange.orders[old] = replace(
            h.exchange.orders[old],
            algo_status="TRIGGERED",
            child_state="unknown",
            actual_order_id=None,
            filled_quantity=None,
        )

    h.exchange.on_place = old_trigger
    assert await h.manager.sync("pos-1") == "exit"
    assert h.state().stage == "exiting"
    assert len(h.creates()) == 4 and not h.cancels()
    assert await h.manager.prepare_exit("pos-1") is False
    assert not h.cancels()


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_cancel_unknown_does_not_resend_and_later_confirmation_recovers(
    tmp_path: Path, side: Side
):
    h = Harness(tmp_path, side)
    await h.manager.sync("pos-1")
    # 退出新口径：普通保护保留至真实零仓；撤单未知只在清理阶段核验。
    assert await h.manager.prepare_exit("pos-1") is True
    assert not h.cancels()
    h.exchange.held = 0
    h.exchange.cancel_unknown = True
    assert await h.manager.flat_cleanup("pos-1") is False
    h.restart()
    assert await h.manager.flat_cleanup("pos-1") is False
    assert len(h.cancels()) == 1
    cid = h.cancels()[0]
    h.exchange.orders[cid] = replace(h.exchange.orders[cid], algo_status="CANCELED")
    h.exchange.cancel_unknown = False
    assert await h.manager.flat_cleanup("pos-1") is True
    assert len(h.cancels()) == 3


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_flat_cleanup_rechecks_zero_before_removing_live_protection(
    tmp_path: Path, side: Side
):
    h = Harness(tmp_path, side)
    await h.manager.sync("pos-1")
    assert await h.manager.flat_cleanup("pos-1") is False
    assert not h.cancels()  # 仍有实际仓位时，不能把cleanup变成撤掉普通保护的授权。
    assert all(order.algo_status == "NEW" for order in h.exchange.orders.values())


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_zero_position_with_unknown_child_cannot_finish_cleanup(tmp_path: Path, side: Side):
    h = Harness(tmp_path, side)
    await h.manager.sync("pos-1")
    cid = h.creates()[0]
    h.exchange.orders[cid] = replace(
        h.exchange.orders[cid],
        algo_status="FINISHED",
        child_state="unknown",
        actual_order_id=None,
        filled_quantity=None,
    )
    h.exchange.held = 0
    assert await h.manager.flat_cleanup("pos-1") is False
    assert h.state().stage != "done" and not h.cancels()


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_concurrent_sync_uses_one_pair_and_reentrant_cleanup_does_not_deadlock(
    tmp_path: Path, side: Side
):
    h = Harness(tmp_path, side)
    result = await asyncio.wait_for(
        asyncio.gather(h.manager.sync("pos-1"), h.manager.sync("pos-1")), 1
    )
    assert result == ["armed", "armed"] and len(h.creates()) == 3
    h.exchange.held = 0
    assert await asyncio.wait_for(h.manager.flat_cleanup("pos-1"), 1) is True
    assert h.state().stage == "done"


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_old_leg_fills_while_replacement_cancel_cannot_revert_exit_to_provisional(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, side: Side
):
    h = Harness(tmp_path, side)
    await h.manager.sync("pos-1")
    old_sl = next(cid for cid, order in h.exchange.orders.items() if cid.startswith("br-sl-"))
    tracked = h.registry.get("pos-1")
    updated = tracked.record.model_copy(
        update={"stop_loss_price": 99.5 if side is Side.LONG else 100.5}
    )
    h.registry.put(tracked.model_copy(update={"record": updated}))

    async def triggered_instead_of_cancel(
        symbol: str, algo_id: str, *, expected_client_order_id: str
    ) -> None:
        assert expected_client_order_id == old_sl
        h.exchange.events.append(("cancel", old_sl))
        h.exchange.orders[old_sl] = replace(
            h.exchange.orders[old_sl],
            algo_status="FINISHED",
            child_state="identified",
            actual_order_id="201",
            actual_order_status="FILLED",
            filled_quantity=2.0,
            triggered_at=START,
        )
        h.exchange.held = 0

    monkeypatch.setattr(h.exchange, "cancel_bracket", triggered_instead_of_cancel)
    assert await h.manager.sync("pos-1") == "exit"
    assert h.state().stage == "exiting"
    assert len(h.creates()) == 4  # 旧 SL 已触发，不能继续创建又一代。


class AdapterExchange(EvidenceExchange):
    def __init__(self, side: Side) -> None:
        super().__init__(side)
        self.direct: list[tuple[str, float]] = []
        self.direct_orders: dict[str, IdentifiedCloseOrder] = {}
        self.direct_unknown = False

    async def place_close(self, symbol, close_side, quantity, client_order_id):
        self.direct.append((client_order_id, quantity))
        if self.direct_unknown:
            raise IdentifiedCloseUnknown(client_order_id)
        assert 0 < quantity <= self.held
        self.held -= quantity
        order = IdentifiedCloseOrder(
            symbol=symbol,
            client_order_id=client_order_id,
            order_id=f"direct-{len(self.direct)}",
            close_side=close_side,
            quantity=quantity,
            filled_quantity=quantity,
            remaining_quantity=0,
            average_price=100,
            status="FILLED",
            position_side="BOTH",
            reduce_only=True,
            is_terminal=True,
        )
        self.direct_orders[client_order_id] = order
        return order

    async def close_order(self, symbol, client_order_id):
        return self.direct_orders.get(client_order_id)


class AdapterHarness(Harness):
    def __init__(self, root: Path, side: Side) -> None:
        super().__init__(root, side)
        self.exchange = AdapterExchange(side)
        self.manager = self.build()
        tracked = self.registry.get("pos-1")
        self.registry.put(
            tracked.model_copy(
                update={
                    "record": tracked.record.model_copy(
                        update={"executor_id": "owned-executor", "lifeboat": None}
                    )
                }
            )
        )
        self.api = FakeHbApi()
        self.api.executors["owned-executor"] = {
            "executor_id": "owned-executor",
            "controller_id": "jy-pos-1",
            "trading_pair": "BTC-USDT",
            "status": "RUNNING",
            "is_active": True,
            "is_trading": True,
            "filled_amount_quote": 200,
            "custom_info": {"current_position_average_price": 100, "order_ids": ["entry-1"]},
        }
        self.backup_exchange = FakeExchange()
        self.lifeboat = BinanceLifeboat(self.backup_exchange, clock=lambda: START)
        self.adapter = self.make_adapter()

    def make_adapter(self):
        self.alerter = StubAlerter()
        return HbExecutorAdapter(
            self.api,
            self.lifeboat,
            self.registry,
            self.alerter,
            account_name="synthetic-account",
            connector_name="synthetic-demo",
            exchange=self.exchange,
            protection=self.manager,
            clock=lambda: START,
        )

    async def protect(self, *, backup=True):
        assert await self.manager.sync("pos-1") == "armed"
        if not backup:
            # 明确外部取消了本仓保命单；普通SL仍真实存在。
            for cid, order in self.exchange.orders.items():
                if cid.startswith("lb-"):
                    self.exchange.orders[cid] = replace(order, algo_status="CANCELED")

    def backups(self):
        return [
            o
            for o in self.exchange.orders.values()
            if o.client_order_id.startswith("lb-") and o.algo_status == "NEW"
        ]

    async def request_close(self):
        with suppress(HbApiError, ProtectionIncomplete):
            await self.adapter.close("pos-1", CloseReason.KEEPER)

    def restart_adapter(self):
        self.restart()
        self.adapter = self.make_adapter()


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_lifeboat_unreadable_after_ack_reports_which_check_failed(tmp_path: Path, side: Side):
    h = AdapterHarness(tmp_path, side)

    async def unreadable(order: ExchangeBracket) -> None:
        if order.client_order_id.startswith("lb-"):
            h.exchange.hidden.add(order.client_order_id)

    h.exchange.on_place = unreadable
    await h.adapter.protection_tick()
    detail = "ProtectionIncomplete: 已提交条件单暂时不可查，不能再次创建或当作已撤"
    assert [c[:3] for c in h.creates()] == ["lb-"]
    assert h.state().last_error == detail
    assert [a.body for a in h.alerter.sent if a.title == "普通托管保护未完整确认"] == [
        f"BTCUSDT pos-1：{detail}；保留已确认保护，停止入场并核验退出"
    ]


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_missing_backup_cannot_remove_last_confirmed_sl_before_uncertain_direct(
    tmp_path: Path, side: Side
):
    h = AdapterHarness(tmp_path, side)
    await h.protect(backup=False)
    h.exchange.direct_unknown = True
    await h.request_close()
    assert h.registry.get("pos-1").record.status is PositionStatus.CLOSING
    assert h.exchange.held == 2
    assert any(
        order.kind == "stop_loss" and order.algo_status == "NEW"
        for order in h.exchange.orders.values()
    ) or bool(h.backups())  # 最后一张真实保护不能先消失。


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_unknown_hb_stop_keeps_all_protection_and_never_sends_direct(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, side: Side
):
    h = AdapterHarness(tmp_path, side)
    await h.protect()
    requests = []

    async def accepted_then_timeout(executor_id, *, keep_position):
        requests.append((executor_id, keep_position))
        raise HbApiError("synthetic response lost after accepted")

    monkeypatch.setattr(h.api, "stop_executor", accepted_then_timeout)
    await h.request_close()
    h.restart_adapter()
    await h.request_close()
    assert requests == [("owned-executor", True)]
    assert not h.exchange.direct and not h.cancels()
    assert h.backups() and not h.backup_exchange.cancelled
    assert h.registry.get("pos-1").record.status is PositionStatus.CLOSING


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
@pytest.mark.parametrize("prefix", ["br-sl-", "lb-"])
async def test_native_hb_timer_and_condition_child_must_both_finish_before_residual_direct(
    tmp_path: Path, side: Side, prefix: str
):
    h = AdapterHarness(tmp_path, side)
    await h.protect()
    sl = next(cid for cid in h.exchange.orders if cid.startswith(prefix))
    h.exchange.orders[sl] = replace(
        h.exchange.orders[sl],
        algo_status="FINISHED",
        child_state="identified",
        actual_order_id="child-sl",
        actual_order_status="PARTIALLY_FILLED",
        filled_quantity=0.5,
        triggered_at=START,
    )
    h.exchange.held = 1.5
    hb = h.api.executors["owned-executor"]
    hb.update(status="SHUTTING_DOWN", close_type="TIME_LIMIT")
    hb["custom_info"]["order_ids"] = ["entry-1", "hb-timer-close"]
    original_read = h.exchange.entry_order
    hb_close_terminal = False

    async def read_order(symbol, client_order_id):
        if client_order_id == "hb-timer-close":
            return ExchangeEntryOrder(
                symbol,
                client_order_id,
                "timer-1",
                "sell" if side is Side.LONG else "buy",
                2,
                0.5,
                "CANCELED" if hb_close_terminal else "PARTIALLY_FILLED",
                "BOTH",
                True,
            )
        return await original_read(symbol, client_order_id)

    h.exchange.entry_order = read_order
    await h.adapter.positions()
    assert not h.exchange.direct and not h.cancels()
    assert any(o.kind == "stop_loss" and o.algo_status == "NEW" for o in h.exchange.orders.values())
    # HB 终态也不能忽略仍在部分成交的交易所子单。
    hb.update(status="TERMINATED", is_active=False)
    hb_close_terminal = True
    await h.adapter.positions()
    assert not h.exchange.direct and not h.cancels()
    assert any(o.kind == "stop_loss" and o.algo_status == "NEW" for o in h.exchange.orders.values())
    # 两个原通道均终态后，按新鲜实际余仓仅补一笔，并在真实零仓后清理。
    h.exchange.orders[sl] = replace(
        h.exchange.orders[sl], actual_order_status="CANCELED", filled_quantity=0.5
    )
    h.exchange.held = 1.0
    await h.adapter.positions()
    assert h.exchange.direct == [("dc-pos-1-1", 1.0)]
    assert h.registry.get("pos-1").record.status is PositionStatus.CLOSED
    assert h.exchange.held == 0 and not h.backups()
    assert h.api.stopped == [("owned-executor", True)]


def test_only_new_server_configuration_disables_hb_price_barriers_but_keeps_timer(tmp_path):
    h = AdapterHarness(tmp_path, Side.LONG)
    requested = intent(time_limit_s=14400, trailing_activation_pct=0.3, trailing_stop_pct=0.1)
    server = h.adapter._executor_config(requested)["triple_barrier_config"]
    assert server["time_limit"] == 14400
    assert [server[k] for k in ("stop_loss", "take_profit", "trailing_stop")] == [None] * 3
    h.adapter._protection = None
    legacy = h.adapter._executor_config(requested)["triple_barrier_config"]
    assert legacy["time_limit"] == 14400
    assert all(legacy[k] is not None for k in ("stop_loss", "take_profit", "trailing_stop"))


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_restart_without_manager_never_falls_back_to_legacy_close(tmp_path, side):
    h = AdapterHarness(tmp_path, side)
    await h.protect()
    h.restart_adapter()
    h.adapter._protection = None
    await h.request_close()
    assert not h.api.stopped and not h.exchange.direct and not h.cancels()
    assert h.backups() and h.registry.get("pos-1").record.status is PositionStatus.CLOSING


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
@pytest.mark.parametrize("prefix", ["br-sl-", "lb-"])
async def test_zero_during_unknown_triggered_child_does_not_close_or_remove_backup(
    tmp_path, side, prefix
):
    h = AdapterHarness(tmp_path, side)
    await h.protect()
    sl = next(cid for cid in h.exchange.orders if cid.startswith(prefix))
    h.exchange.orders[sl] = replace(
        h.exchange.orders[sl],
        algo_status="FINISHED",
        child_state="unknown",
        actual_order_id=None,
        actual_order_status=None,
        filled_quantity=None,
    )
    h.exchange.held = 0
    h.api.terminate("owned-executor", "TIME_LIMIT", leaves_position=True)
    await h.adapter.positions()
    assert h.registry.get("pos-1").record.status is PositionStatus.CLOSING
    assert not h.exchange.direct and not h.cancels()
    assert not h.backup_exchange.cancelled
    assert any(o.algo_status == "NEW" for o in h.exchange.orders.values())


def trailing_inputs(tmp_path, side, bid, ask):
    h = Harness(tmp_path, side)
    rec = h.registry.get("pos-1").record.model_copy(
        update={
            "trailing_activation_pct": 0.3,
            "trailing_stop_pct": 0.5,
        }
    )
    state = h.state().model_copy(
        update={
            "entry_order_id": "100",
            "entry_status": "FILLED",
            "entry_terminal": True,
            "observed_quantity": 2.0,
        }
    )
    basis = EntryBasis(
        "BTCUSDT",
        "entry-1",
        "100",
        "buy" if side is Side.LONG else "sell",
        2.0,
        200.0,
        0.2,
        100.0,
        "FILLED",
    )
    return rec, state, basis, ExecutionBook("BTCUSDT", bid, ask, START, START)


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
def test_trailing_inverse_preserves_net_fee_distance_and_correct_book_side(tmp_path, side):
    bid, ask = (102.0, 103.0) if side is Side.LONG else (97.0, 98.0)
    rec, state, basis, book = trailing_inputs(tmp_path, side, bid, ask)
    result = trailing_update(rec, state, basis, book, START)
    assert result.activated is True
    assert result.net == pytest.approx(0.019)
    assert result.threshold_net == pytest.approx(0.014)
    assert result.stop_price == pytest.approx(101.5 if side is Side.LONG else 98.5)


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
def test_exact_decimal_activation_equality_must_not_activate_from_float_noise(tmp_path, side):
    # HB真实Decimal：毛盈0.8U−费0.2U，净0.6/200正好0.3%，严格>才启动。
    bid, ask = (100.4, 100.5) if side is Side.LONG else (99.5, 99.6)
    rec, state, basis, book = trailing_inputs(tmp_path, side, bid, ask)
    result = trailing_update(rec, state, basis, book, START)
    assert result.activated is False
    assert result.threshold_net is None and result.stop_price is None


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
def test_active_zero_net_threshold_is_not_treated_as_inactive(tmp_path, side):
    bid, ask = (100.2, 100.3) if side is Side.LONG else (99.7, 99.8)
    rec, state, basis, book = trailing_inputs(tmp_path, side, bid, ask)
    state = state.model_copy(update={"trailing_activated": True, "trailing_peak_net": 0.005})
    result = trailing_update(rec, state, basis, book, START)
    assert result.activated and result.threshold_net == pytest.approx(0)
    assert result.stop_price == pytest.approx(100.1 if side is Side.LONG else 99.9)


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
@pytest.mark.parametrize("source_shift", [-6, 1])
def test_trailing_rejects_stale_or_future_book_without_inventing_peak(tmp_path, side, source_shift):
    rec, state, basis, book = trailing_inputs(tmp_path, side, 102, 103)
    book = replace(book, source_at=START + timedelta(seconds=source_shift))
    with pytest.raises(ExchangeError):
        trailing_update(rec, state, basis, book, START)
    assert state.trailing_peak_net is None and state.trailing_stop_price is None


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_trailing_replacement_unknown_preserves_old_sl_and_recovers_same_cid(tmp_path, side):
    h = Harness(tmp_path, side)
    bid, ask = (102.0, 103.0) if side is Side.LONG else (97.0, 98.0)
    rec, _, basis, book = trailing_inputs(tmp_path / "math", side, bid, ask)
    tracked = h.registry.get("pos-1")
    h.registry.put(tracked.model_copy(update={"record": rec}))
    await h.manager.sync("pos-1")
    old_sl = next(cid for cid, order in h.exchange.orders.items() if cid.startswith("br-sl-"))
    assert await h.manager.observe_trailing("pos-1", basis, book)
    h.exchange.unknown_submit = True
    with pytest.raises(ProtectionIncomplete):
        await h.manager.sync("pos-1")
    new_sl = h.creates()[-1]
    assert new_sl != old_sl and h.exchange.orders[old_sl].algo_status == "NEW"
    assert not h.cancels()
    exact_peak = h.state().trailing_peak_net_exact
    h.restart()
    with pytest.raises(ProtectionIncomplete):
        await h.manager.sync("pos-1")
    assert len(h.creates()) == 4 and h.state().trailing_peak_net_exact == exact_peak
    h.exchange.hidden.clear()
    h.exchange.unknown_submit = False
    assert await h.manager.sync("pos-1") == "armed"
    assert h.creates().count(new_sl) == 1 and h.cancels() == [old_sl]
    assert h.exchange.events.index(("create", new_sl)) < h.exchange.events.index(("cancel", old_sl))
    assert h.exchange.orders[new_sl].trigger_price == pytest.approx(
        101.5 if side is Side.LONG else 98.5
    )


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_restart_lower_quote_preserves_peak_stop_and_marks_observation_gap(tmp_path, side):
    h = Harness(tmp_path, side)
    bid, ask = (102.0, 103.0) if side is Side.LONG else (97.0, 98.0)
    rec, _, basis, book = trailing_inputs(tmp_path / "math", side, bid, ask)
    tracked = h.registry.get("pos-1")
    h.registry.put(tracked.model_copy(update={"record": rec}))
    await h.manager.sync("pos-1")
    await h.manager.observe_trailing("pos-1", basis, book)
    await h.manager.sync("pos-1")
    peak, stop = h.state().trailing_peak_net_exact, h.state().trailing_stop_price
    h.restart()
    now = START + timedelta(seconds=10)
    h.manager.clock = lambda: now
    later = ExecutionBook(
        "BTCUSDT",
        101.0 if side is Side.LONG else 98.0,
        102.0 if side is Side.LONG else 99.0,
        now,
        now,
    )
    assert await h.manager.observe_trailing("pos-1", basis, later) is False
    assert h.state().trailing_peak_net_exact == peak
    assert h.state().trailing_stop_price == stop and h.state().trailing_observation_gap
    assert await h.manager.sync("pos-1") == "armed"
    assert len(h.creates()) == 4


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
@pytest.mark.parametrize("fee", [float("nan"), 0.3])
def test_unknown_or_changed_fee_cannot_reuse_a_different_net_peak(tmp_path, side, fee):
    bid, ask = (102.0, 103.0) if side is Side.LONG else (97.0, 98.0)
    rec, state, basis, book = trailing_inputs(tmp_path, side, bid, ask)
    state = state.model_copy(
        update={
            "trailing_entry_quote": 200.0,
            "trailing_entry_quantity": 2.0,
            "trailing_fees_quote": 0.2,
            "trailing_peak_net_exact": "0.019",
            "trailing_activated": True,
            "trailing_stop_price": 101.5 if side is Side.LONG else 98.5,
        }
    )
    with pytest.raises(ExchangeError):
        trailing_update(rec, state, replace(basis, fee_usdt=fee), book, START)
    assert state.trailing_peak_net_exact == "0.019"


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_limit_entry_cancel_unknown_keeps_new_hb_pending_intent(tmp_path, monkeypatch, side):
    h = AdapterHarness(tmp_path, side)
    h.exchange.held = 0
    h.exchange.entry = replace(h.exchange.entry, status="NEW", filled_quantity=0)
    tracked = h.registry.get("pos-1")
    tracked = tracked.model_copy(
        update={
            "record": tracked.record.model_copy(
                update={
                    "status": PositionStatus.OPENING,
                    "entry_price": None,
                    "stop_loss_price": None,
                    "take_profit_price": None,
                }
            ),
            "limit_wait_s": 60,
        }
    )
    h.registry.put(tracked)
    hb = h.api.executors["owned-executor"]
    hb.update(is_trading=False, filled_amount_quote=0)
    hb["custom_info"]["current_position_average_price"] = None
    requests = []

    async def unknown_stop(executor_id, *, keep_position):
        requests.append((executor_id, keep_position))
        raise HbApiError("synthetic cancellation accepted but response lost")

    monkeypatch.setattr(h.api, "stop_executor", unknown_stop)
    await h.adapter._cancel_unfilled_limit(tracked)
    assert h.registry.get("pos-1").record.status is PositionStatus.CLOSING
    assert h.registry.get("pos-1").hb_close_pending is True
    h.restart_adapter()
    await h.adapter.protection_tick()
    assert requests == [("owned-executor", True)]
    assert not h.exchange.direct


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_verified_actual_entry_cost_cannot_oscillate_with_stale_hb_average(tmp_path, side):
    h = AdapterHarness(tmp_path, side)
    tracked = h.registry.get("pos-1")
    h.registry.put(
        tracked.model_copy(
            update={
                "record": tracked.record.model_copy(
                    update={
                        "trailing_activation_pct": None,
                        "trailing_stop_pct": None,
                    }
                )
            }
        )
    )
    await h.protect()

    async def actual_basis(symbol, client_order_id):
        return EntryBasis(
            "BTCUSDT",
            "entry-1",
            "100",
            "buy" if side is Side.LONG else "sell",
            2.0,
            202.0,
            0.2,
            101.0,
            "FILLED",
        )

    h.exchange.entry_basis = actual_basis
    # HB旧查询仍报首笔100，实际完整成交账已证明加权均价101。
    await h.adapter.protection_tick()
    assert h.registry.get("pos-1").record.entry_price == 101.0
    after_confirmed = list(h.exchange.events)
    await h.adapter.protection_tick()
    assert h.registry.get("pos-1").record.entry_price == 101.0
    assert h.exchange.events == after_confirmed  # 不每轮撤改单、短暂恢复过期的止损价。


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_trailing_price_crossed_rejection_enters_exit_without_dropping_old_sl(tmp_path, side):
    h = Harness(tmp_path, side)
    bid, ask = (102.0, 103.0) if side is Side.LONG else (97.0, 98.0)
    rec, _, basis, book = trailing_inputs(tmp_path / "math", side, bid, ask)
    tracked = h.registry.get("pos-1")
    h.registry.put(tracked.model_copy(update={"record": rec}))
    await h.manager.sync("pos-1")
    old_sl = next(cid for cid in h.exchange.orders if cid.startswith("br-sl-"))
    await h.manager.observe_trailing("pos-1", basis, book)
    h.exchange.reject_kind = "stop_loss"
    h.exchange.reject_immediate = True
    assert await h.manager.sync("pos-1") == "exit"
    assert h.exchange.orders[old_sl].algo_status == "NEW" and not h.cancels()
    assert h.state().stage == "exiting"
    assert h.state().exit_reason is CloseReason.TRAILING_STOP


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_restart_before_hb_stop_intent_plus_404_cannot_bypass_server_cleanup(tmp_path, side):
    h = AdapterHarness(tmp_path, side)
    await h.protect()
    tracked = h.registry.get("pos-1")
    # 模拟已持久CLOSING，但在写HB停止意图前崩溃；重启HB查询404仍是unknown。
    h.registry.put(
        tracked.model_copy(
            update={
                "record": tracked.record.model_copy(update={"status": PositionStatus.CLOSING}),
                "requested_close": CloseReason.KEEPER,
                "hb_close_pending": False,
            }
        )
    )
    lb = next(cid for cid in h.exchange.orders if cid.startswith("lb-"))
    h.exchange.orders[lb] = replace(
        h.exchange.orders[lb],
        algo_status="FINISHED",
        child_state="unknown",
        actual_order_id=None,
        actual_order_status=None,
        filled_quantity=None,
    )
    h.exchange.held = 0
    h.api.executors.clear()
    h.restart_adapter()
    for _ in range(3):
        await h.adapter.positions()
    current = h.registry.get("pos-1")
    assert current.record.status is PositionStatus.CLOSING
    assert current.record.closed_at is None and current.record.realized_pnl_usdt is None
    assert not h.cancels() and not h.exchange.direct
