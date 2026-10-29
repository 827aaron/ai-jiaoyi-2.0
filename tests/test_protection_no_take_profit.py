"""托管保护不挂止盈单：新仓只有保命单 + 止损，盈利出场只靠移动止损换代止损腿。

旧版本挂下的 br-tp- 腿（INJUSDT 真实账本）：仓位还在时下一轮同步撤掉、不平仓；零仓清理照样撤。
只有合成订单，不连接任何服务。
"""

from __future__ import annotations

from typing import Any

import pytest

from jiaoyi.contracts import Side
from jiaoyi.contracts.position import PositionStatus
from tests.test_rejected_trigger_settle import (
    EXECUTOR,
    PID,
    TP_CID,
    IncidentExchange,
    build,
    live_algo,
    open_ledger,
    tp_algo,
)
from tests.test_server_protection_regression import Harness, reads, trailing_inputs


class CancellingExchange(IncidentExchange):
    """撤单照实执行：把条件单回包改成 CANCELED。"""

    def _cancel(self, algo_id: str, cid: str) -> None:
        self.events.append(("cancel", cid))
        raw = self.algo[cid]
        assert raw["algoId"] == algo_id
        raw.update(algoStatus="CANCELED", triggerTime=0)

    async def cancel_bracket(self, symbol, algo_id, *, expected_client_order_id) -> None:
        self._cancel(algo_id, expected_client_order_id)

    async def cancel_lifeboat(self, symbol, algo_id, *, expected_client_order_id) -> None:
        self._cancel(algo_id, expected_client_order_id)


def legacy_position(held: float) -> tuple[dict[str, Any], CancellingExchange]:
    """仓位还在、已武装：保命单、止损、旧版本挂的止盈单都在交易所上 NEW。"""
    ledger = open_ledger()
    legs = ledger["server_protection"]["legs"]
    legs[2].update(algo_status="NEW", triggered_at=None, child_state="absent")
    algo = {leg["client_order_id"]: live_algo(leg) for leg in legs[:2]}
    algo[TP_CID] = tp_algo(algoStatus="NEW", triggerTime=0)
    return ledger, CancellingExchange(held=held, algo=algo)


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_brackets_sync_places_only_lifeboat_and_stop(tmp_path, side: Side) -> None:
    h = Harness(tmp_path, side)
    assert await h.manager.sync("pos-1") == "armed"
    assert h.creates() == ["lb-pos-1-1", "br-sl-pos-1-1"]
    assert {o.kind for o in h.exchange.orders.values()} == {"stop_loss"}
    state = h.state()
    assert [(leg.purpose, leg.state) for leg in state.legs] == [
        ("lifeboat", "confirmed"),
        ("fixed", "confirmed"),
    ]
    assert state.stage == "armed" and state.ever_armed


async def test_legacy_take_profit_leg_is_cancelled_and_the_position_stays_open(tmp_path) -> None:
    ledger, exchange = legacy_position(held=1194.7)
    registry, _, adapter, alerter = build(tmp_path, ledger, exchange)
    adapter._api.executors[EXECUTOR].update(status="RUNNING", is_active=True, is_trading=True)
    await adapter.protection_tick()
    final = registry.get(PID)
    assert final.record.status is PositionStatus.OPEN and final.requested_close is None
    assert exchange.actions("cancel") == [TP_CID]
    assert not exchange.creates() and not exchange.actions("direct")
    state = final.server_protection
    assert state.stage == "armed" and state.exit_reason is None
    assert [(leg.kind, leg.state, leg.algo_status) for leg in state.legs] == [
        ("stop_loss", "confirmed", "NEW"),
        ("stop_loss", "confirmed", "NEW"),
        ("take_profit", "cancelled", "CANCELED"),
    ]
    assert not [a for a in alerter.sent if a.title == "普通托管保护未完整确认"]
    exchange.events.clear()
    await adapter.protection_tick()
    assert not exchange.actions("cancel") and not exchange.creates()


async def test_legacy_take_profit_leg_is_cancelled_by_flat_cleanup(tmp_path) -> None:
    ledger, exchange = legacy_position(held=0.0)
    registry, manager, _, _ = build(tmp_path, ledger, exchange)
    assert await manager.flat_cleanup(PID) is True
    assert sorted(exchange.actions("cancel")) == sorted(
        ["lb-pos-42600c953a4c-1", "br-sl-pos-42600c953a4c-1", TP_CID]
    )
    assert registry.get(PID).server_protection.stage == "done"


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_trailing_replaces_the_stop_leg_and_leaves_lifeboat_alone(tmp_path, side) -> None:
    h = Harness(tmp_path, side)
    bid, ask = (102.0, 103.0) if side is Side.LONG else (97.0, 98.0)
    rec, _, basis, book = trailing_inputs(tmp_path / "math", side, bid, ask)
    h.registry.put(h.registry.get("pos-1").model_copy(update={"record": rec}))
    assert await h.manager.sync("pos-1") == "armed"
    assert await h.manager.observe_trailing("pos-1", basis, reads(book))
    assert await h.manager.sync("pos-1") == "armed"
    assert h.creates() == ["lb-pos-1-1", "br-sl-pos-1-1", "br-sl-pos-1-2"]
    assert h.cancels() == ["br-sl-pos-1-1"]
    live = {cid for cid, o in h.exchange.orders.items() if o.algo_status == "NEW"}
    assert live == {"lb-pos-1-1", "br-sl-pos-1-2"}
    assert h.exchange.orders["br-sl-pos-1-2"].trigger_price == pytest.approx(
        101.5 if side is Side.LONG else 98.5
    )
