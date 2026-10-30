"""后备止盈单（``protection_fallback_take_profit``）：没有交易所移动止损在保护、程序的移动止损
也没激活时，止损和保命单确认之后补挂 ``br-tp-``；移动止损激活或交易所移动止损确认后撤掉。

仓位：开仓 100、2 张，账本止盈价多 102 / 空 98；启动线 0.3%、回撤 0.5%（见 test_trailing_native）。
只有合成订单和假时钟，不连接任何服务。
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from jiaoyi.contracts import Side
from jiaoyi.contracts.position import CloseReason, PositionStatus
from jiaoyi.execution.brackets import BracketManager, ProtectionIncomplete
from jiaoyi.execution.registry import ProtectiveLeg
from tests.test_execution import START
from tests.test_trailing_native import NativeHarness, native_armed, stream_events


class TpHarness(NativeHarness):
    def __init__(
        self,
        root: Path,
        side: Side,
        *,
        native: bool = True,
        fallback: bool = True,
        order="lifeboat_first",
    ) -> None:
        self.fallback, self.order = fallback, order
        super().__init__(root, side, native=native)

    def build(self) -> BracketManager:
        self.exchange.registry = self.registry
        return BracketManager(
            self.exchange,
            self.registry,
            account_name="synthetic-account",
            connector_name="synthetic-demo",
            clock=lambda: self.now,
            sleep=self.sleep,
            native_trailing=lambda: self.native,
            take_profit_working_type="MARK_PRICE",
            fallback_take_profit=self.fallback,
            leg_order=self.order,
        )

    def tp_legs(self) -> list[tuple[str, str, float]]:
        return [
            (leg.client_order_id, leg.state, leg.trigger_price)
            for leg in self.state().legs
            if leg.kind == "take_profit"
        ]


def tp_price(side: Side) -> float:
    return 102.0 if side is Side.LONG else 98.0


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_native_confirmed_places_no_take_profit_leg(tmp_path: Path, side: Side) -> None:
    h = TpHarness(tmp_path, side)
    await native_armed(h)
    await h.adapter.protection_tick()
    assert h.manager.native_mode("pos-1") == "confirmed"
    assert h.creates() == ["br-sl-pos-1-1", "br-ts-pos-1-1"]
    assert h.tp_legs() == []


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_native_fallback_places_take_profit_after_stop_and_lifeboat(
    tmp_path: Path, side: Side
) -> None:
    h = TpHarness(tmp_path, side)
    h.native_fail = "reject"
    await native_armed(h)
    assert h.manager.native_mode("pos-1") == "off"
    assert h.creates() == ["br-sl-pos-1-1", "br-ts-pos-1-1", "lb-pos-1-2", "br-tp-pos-1-3"]
    assert h.live_cids() == ["br-sl-pos-1-1", "br-tp-pos-1-3", "lb-pos-1-2"]
    order = h.exchange.orders["br-tp-pos-1-3"]
    assert (order.kind, order.trigger_price, order.quantity, order.working_type) == (
        "take_profit",
        tp_price(side),
        2.0,
        "MARK_PRICE",
    )
    assert order.close_side == ("sell" if side is Side.LONG else "buy") and order.reduce_only
    assert h.state().stage == "armed" and not h.closed()
    await h.manager.sync("pos-1")
    assert len(h.creates()) == 4 and not h.cancels()


@pytest.mark.parametrize(
    ("order", "creates"),
    [
        ("lifeboat_first", ["lb-pos-1-1", "br-sl-pos-1-1", "br-tp-pos-1-2"]),
        ("stop_first", ["br-sl-pos-1-1", "lb-pos-1-1", "br-tp-pos-1-2"]),
    ],
)
async def test_trailing_native_off_places_take_profit_last(
    tmp_path: Path, order: str, creates: list[str]
) -> None:
    h = TpHarness(tmp_path, Side.LONG, native=False, order=order)
    await h.armed()
    assert h.creates() == creates
    assert h.tp_legs() == [("br-tp-pos-1-2", "confirmed", 102.0)]


async def test_switch_off_places_nothing_and_cancels_a_live_take_profit(tmp_path: Path) -> None:
    h = TpHarness(tmp_path, Side.LONG, native=False)
    await h.armed()
    h.fallback = False
    h.restart()
    await h.manager.sync("pos-1")
    assert h.cancels() == ["br-tp-pos-1-2"]
    assert h.live_cids() == ["br-sl-pos-1-1", "lb-pos-1-1"]
    assert h.tp_legs() == [("br-tp-pos-1-2", "cancelled", 102.0)]


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_trailing_activation_cancels_the_take_profit(tmp_path: Path, side: Side) -> None:
    h = TpHarness(tmp_path, side)
    h.native_fail = "reject"
    await native_armed(h)
    assert "br-tp-pos-1-3" in h.live_cids()
    h.bid, h.ask = (101.4, 101.5) if side is Side.LONG else (98.5, 98.6)
    h.mark_value = 101.45 if side is Side.LONG else 98.55
    await h.adapter.protection_tick()
    assert h.state().trailing_activated
    assert h.cancels() == ["br-sl-pos-1-1", "br-tp-pos-1-3"]
    assert "br-tp-pos-1-3" not in h.live_cids() and not h.closed()
    await h.adapter.protection_tick()
    assert not [c for c in h.creates() if c.startswith("br-tp-") and c != "br-tp-pos-1-3"]


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_native_confirmed_later_cancels_the_take_profit(tmp_path: Path, side: Side) -> None:
    h = TpHarness(tmp_path, side, native=False)
    await h.armed()
    assert h.live_cids() == ["br-sl-pos-1-1", "br-tp-pos-1-2", "lb-pos-1-1"]
    h.native = True
    await h.manager.sync("pos-1")  # pending：不新挂、在挂的留着
    assert not h.cancels()
    await h.adapter.protection_tick()
    assert h.manager.native_mode("pos-1") == "confirmed"
    assert sorted(h.cancels()) == ["br-tp-pos-1-2", "lb-pos-1-1"]
    assert h.live_cids() == ["br-sl-pos-1-1", "br-ts-pos-1-1"]


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_take_profit_fill_from_the_stream_closes_as_take_profit(
    tmp_path: Path, side: Side
) -> None:
    h = TpHarness(tmp_path, side, native=False)
    await h.armed()
    h.exchange.held = 0.0
    at = int((h.now + timedelta(seconds=5)).timestamp() * 1000)
    for msg in stream_events("br-tp-pos-1-2", "sell" if side is Side.LONG else "buy", at):
        h.adapter.on_stream_event(msg)
    await asyncio.gather(*list(h.adapter._stream_tasks))
    rec = h.registry.get("pos-1").record
    assert rec.status is PositionStatus.CLOSED and rec.close_reason is CloseReason.TAKE_PROFIT
    assert h.state().exit_reason is CloseReason.TAKE_PROFIT
    assert sorted(h.cancels()) == ["br-sl-pos-1-1", "lb-pos-1-1"]


async def test_restart_keeps_the_take_profit_leg_without_resubmitting(tmp_path: Path) -> None:
    h = TpHarness(tmp_path, Side.SHORT, native=False)
    await h.armed()
    h.restart()
    assert await h.manager.sync("pos-1") == "armed"
    assert h.creates() == ["lb-pos-1-1", "br-sl-pos-1-1", "br-tp-pos-1-2"] and not h.cancels()
    assert h.tp_legs() == [("br-tp-pos-1-2", "confirmed", 98.0)]


async def test_rejected_take_profit_keeps_the_stops_and_is_not_retried(tmp_path: Path) -> None:
    h = TpHarness(tmp_path, Side.LONG, native=False)
    h.exchange.reject_kind, h.exchange.reject_immediate = "take_profit", True
    assert await h.manager.sync("pos-1") == "armed"
    assert h.tp_legs() == [("br-tp-pos-1-2", "rejected", 102.0)]
    assert h.state().stage == "armed" and h.state().exit_reason is None
    assert await h.manager.sync("pos-1") == "armed"
    assert h.creates() == ["lb-pos-1-1", "br-sl-pos-1-1", "br-tp-pos-1-2"]
    assert h.live_cids() == ["br-sl-pos-1-1", "lb-pos-1-1"]


async def test_unknown_take_profit_only_reads_its_original_cid(tmp_path: Path) -> None:
    h = TpHarness(tmp_path, Side.LONG, native=False)
    await h.armed()
    h.registry.put(
        h.registry.get("pos-1").model_copy(
            update={
                "record": h.registry.get("pos-1").record.model_copy(
                    update={"take_profit_price": 103.0}
                )
            }
        )
    )
    h.exchange.unknown_submit = True
    assert await h.manager.sync("pos-1") == "armed"
    assert h.tp_legs()[-1] == ("br-tp-pos-1-3", "submit_pending", 103.0)
    assert not h.cancels()  # 新的没确认：旧的先留着
    h.exchange.unknown_submit = False
    assert await h.manager.sync("pos-1") == "armed"
    assert h.creates() == ["lb-pos-1-1", "br-sl-pos-1-1", "br-tp-pos-1-2", "br-tp-pos-1-3"]
    h.exchange.hidden.clear()
    assert await h.manager.sync("pos-1") == "armed"
    assert h.cancels() == ["br-tp-pos-1-2"]
    assert h.live_cids() == ["br-sl-pos-1-1", "br-tp-pos-1-3", "lb-pos-1-1"]


async def test_partial_entry_waits_for_the_final_quantity_before_the_take_profit(
    tmp_path: Path,
) -> None:
    h = TpHarness(tmp_path, Side.LONG, native=False)
    h.exchange.entry = replace(h.exchange.entry, filled_quantity=1, status="PARTIALLY_FILLED")
    h.exchange.held = 1
    assert await h.manager.sync("pos-1") == "provisional"
    assert h.creates() == ["lb-pos-1-1", "br-sl-pos-1-1"]
    h.exchange.entry = replace(h.exchange.entry, filled_quantity=2, status="FILLED")
    h.exchange.held = 2
    assert await h.manager.sync("pos-1") == "armed"
    assert h.creates()[-1] == "br-tp-pos-1-3"
    assert h.exchange.orders["br-tp-pos-1-3"].quantity == 2.0


def set_protection(h: TpHarness, **changes: object) -> None:
    tracked = h.registry.get("pos-1")
    state = tracked.server_protection.model_copy(update=changes)
    h.registry.put(tracked.model_copy(update={"server_protection": state}))


async def test_unknown_cancel_is_retried_while_the_exchange_still_shows_it_new(
    tmp_path: Path,
) -> None:
    h = TpHarness(tmp_path, Side.LONG, native=False)
    await h.armed()
    set_protection(h, trailing_activated=True)
    h.exchange.cancel_unknown = True
    assert await h.manager.sync("pos-1") == "armed"
    assert h.tp_legs() == [("br-tp-pos-1-2", "cancel_pending", 102.0)]
    h.exchange.cancel_unknown = False
    assert await h.manager.sync("pos-1") == "armed"
    assert h.cancels() == ["br-tp-pos-1-2", "br-tp-pos-1-2"]
    assert h.tp_legs() == [("br-tp-pos-1-2", "cancelled", 102.0)]
    assert h.live_cids() == ["br-sl-pos-1-1", "lb-pos-1-1"]


async def test_native_estimate_already_activated_places_no_take_profit(tmp_path: Path) -> None:
    h = TpHarness(tmp_path, Side.SHORT, native=False)
    set_protection(h, native_activated=True, native_fallback="交易所拒绝交易所移动止损")
    assert await h.manager.sync("pos-1") == "armed"
    assert h.creates() == ["lb-pos-1-1", "br-sl-pos-1-1"]


async def test_stop_readback_failing_during_the_take_profit_step_fails_the_sync(
    tmp_path: Path,
) -> None:
    h = TpHarness(tmp_path, Side.LONG, native=False)

    async def hide_stop(order) -> None:
        if order.kind == "take_profit":
            h.exchange.hidden.add("br-sl-pos-1-1")

    h.exchange.on_place = hide_stop
    with pytest.raises(ProtectionIncomplete, match="已提交条件单暂时不可查"):
        await h.manager.sync("pos-1")
    assert h.state().last_error is not None and "已提交条件单暂时不可查" in h.state().last_error


def test_take_profit_rejected_after_trigger_is_not_an_exit_at_target(tmp_path: Path) -> None:
    h = TpHarness(tmp_path, Side.LONG, native=False)
    leg = ProtectiveLeg(
        kind="take_profit",
        generation=2,
        client_order_id="br-tp-pos-1-2",
        quantity=2.0,
        trigger_price=102.0,
        state="cancelled",
        algo_id="3",
        algo_status="REJECTED",
        triggered_at=START,
        child_state="absent",
    )
    set_protection(h, legs=[leg])
    assert h.adapter.take_profit_exiting("pos-1") is False
    triggered = leg.model_copy(update={"algo_status": "TRIGGERED", "state": "confirmed"})
    set_protection(h, legs=[triggered])
    assert h.adapter.take_profit_exiting("pos-1") is True
