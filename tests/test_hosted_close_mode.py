"""托管仓平仓顺序（hosted_close_mode）：合成交易所 + 合成时钟复现决定到成交的时间线。

不连接任何服务。

时延模型：每次币安 REST 0.3 秒（直连下单要查模式、查原 CID、下单三次，记 0.9 秒），本机 HB 一次
0.015 秒。HB 收到停止后不会当场终态（真实 HB 要等下一轮控制循环），由测试显式 terminate。
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import datetime, timedelta
from itertools import pairwise
from pathlib import Path

import pytest

from jiaoyi.contracts import Side
from jiaoyi.contracts.module_status import Alert, AlertLevel
from jiaoyi.contracts.position import CloseReason, PositionStatus
from jiaoyi.execution.brackets import BracketManager, CloseInProgress, ProtectionIncomplete
from jiaoyi.execution.exchange import (
    CloseFill,
    ExchangeEntryOrder,
    ExchangeError,
    IdentifiedCloseCancelledBeforeSend,
    IdentifiedCloseNotSent,
    IdentifiedCloseOrder,
    IdentifiedCloseRejected,
    IdentifiedCloseUnknown,
)
from jiaoyi.execution.exit_fills import ExitFills, read_exit_fills
from jiaoyi.execution.hb_client import HbApiError
from jiaoyi.execution.hb_executor import HbExecutorAdapter
from tests.test_execution import START
from tests.test_server_protection_regression import AdapterExchange, AdapterHarness

REST_S = 0.3
PLACE_S = 0.9
HB_S = 0.015
FILL_BUDGET_S = 3.0
_REST = (
    "positions_evidence",
    "entry_order",
    "get_bracket",
    "get_lifeboat",
    "cancel_bracket",
    "cancel_lifeboat",
    "close_order",
    "place_bracket",
    "place_lifeboat",
    "execution_mark",
    "assert_entry_clear",
)


class FakeClock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class RaceExchange(AdapterExchange):
    """只减仓语义：可成交量不超过实际余仓；余仓已为零时按币安 -2022 明确拒绝。"""

    def __init__(self, side: Side) -> None:
        super().__init__(side)
        self.before_place: Callable[[], Awaitable[None]] | None = None
        self.fill_cap: float | None = None
        self.unknown_fills = True
        self.hidden_direct: set[str] = set()
        self.legs_at_place: list[dict[str, str | None]] = []
        self.not_sent_once = False
        self.ack_only = False

    def fill_acked(self, client_order_id: str, at: datetime) -> None:
        order = self.direct_orders[client_order_id]
        self.held -= order.quantity
        self.direct_orders[client_order_id] = replace(
            order,
            filled_quantity=order.quantity,
            remaining_quantity=0.0,
            average_price=100,
            status="FILLED",
            is_terminal=True,
            update_time=at,
        )

    async def place_close(self, symbol, close_side, quantity, client_order_id):
        self.legs_at_place.append({c: o.algo_status for c, o in self.orders.items()})
        if self.not_sent_once:
            self.not_sent_once = False
            raise IdentifiedCloseNotSent("synthetic position-mode query timed out")
        if self.before_place is not None:
            await self.before_place()
        self.direct.append((client_order_id, quantity))
        if self.ack_only:
            order = IdentifiedCloseOrder(
                symbol=symbol,
                client_order_id=client_order_id,
                order_id=f"direct-{len(self.direct)}",
                close_side=close_side,
                quantity=quantity,
                filled_quantity=0.0,
                remaining_quantity=quantity,
                average_price=None,
                status="NEW",
                position_side="BOTH",
                reduce_only=True,
                is_terminal=False,
            )
            self.direct_orders[client_order_id] = order
            return order
        if self.direct_unknown and not self.unknown_fills:
            raise IdentifiedCloseUnknown(client_order_id)  # 请求没到交易所，查也查不到
        filled = min(quantity, self.held, math.inf if self.fill_cap is None else self.fill_cap)
        if filled <= 0:
            raise IdentifiedCloseRejected("synthetic -2022 ReduceOnly Order is rejected")
        self.held -= filled
        order = IdentifiedCloseOrder(
            symbol=symbol,
            client_order_id=client_order_id,
            order_id=f"direct-{len(self.direct)}",
            close_side=close_side,
            quantity=quantity,
            filled_quantity=filled,
            remaining_quantity=quantity - filled,
            average_price=100,
            status="FILLED" if filled == quantity else "EXPIRED",
            position_side="BOTH",
            reduce_only=True,
            is_terminal=True,
        )
        self.direct_orders[client_order_id] = order
        if self.direct_unknown:
            self.hidden_direct.add(client_order_id)
            raise IdentifiedCloseUnknown(client_order_id)
        return order

    async def close_order(self, symbol, client_order_id):
        if client_order_id in self.hidden_direct:
            return None
        return self.direct_orders.get(client_order_id)


class LatencyHarness(AdapterHarness):
    def __init__(self, root: Path, side: Side, mode: str = "direct") -> None:
        self.clock = FakeClock()
        self.mode = mode
        self.settled: list[tuple[CloseReason, CloseFill | None]] = []
        super().__init__(root, side)
        self.exchange = RaceExchange(side)
        self.manager = self.build()
        self.adapter = self.make_adapter()
        self.api.auto_terminate_on_stop = False
        self.calls: list[tuple[float, str]] = []
        self._wrap()

    def build(self) -> BracketManager:
        self.exchange.registry = self.registry
        return BracketManager(
            self.exchange,
            self.registry,
            account_name="synthetic-account",
            connector_name="synthetic-demo",
            clock=self.clock,
        )

    def make_adapter(self) -> HbExecutorAdapter:
        adapter = super().make_adapter()
        adapter._clock = self.clock
        adapter._hosted_close_mode = self.mode  # type: ignore[assignment]
        original = adapter.mark_closed_directly

        def spy(pid: str, reason: CloseReason, fill: CloseFill | None = None) -> None:
            self.settled.append((reason, fill))
            original(pid, reason, fill)

        adapter.mark_closed_directly = spy  # type: ignore[method-assign]
        return adapter

    def _wrap(self) -> None:
        def timed(target, name: str, cost: float):
            original = getattr(target, name)

            async def call(*args, **kwargs):
                self.clock.advance(cost)
                self.calls.append(((self.clock.now - START).total_seconds(), name))
                return await original(*args, **kwargs)

            setattr(target, name, call)

        for name in _REST:
            timed(self.exchange, name, REST_S)
        timed(self.exchange, "place_close", PLACE_S)
        timed(self.api, "get_executor", HB_S)
        timed(self.api, "stop_executor", HB_S)

    def at(self, name: str) -> list[float]:
        return [t for t, n in self.calls if n == name]

    def hb_terminates(self) -> None:
        self.api.terminate("owned-executor", "POSITION_HOLD", leaves_position=True)

    def live_stop(self) -> bool:
        return any(
            o.kind == "stop_loss" and o.algo_status == "NEW" and cid.startswith("br-sl-")
            for cid, o in self.exchange.orders.items()
        )

    def tracked(self):
        return self.registry.get("pos-1")

    def exit_alerts(self) -> list[Alert]:
        return [a for a in self.alerter.sent if a.dedup_key == "closed:pos-1"]

    def failure_alerts(self) -> list[Alert]:
        return [a for a in self.alerter.sent if a.dedup_key == "closed_directly:pos-1"]


async def armed(tmp_path: Path, side: Side, mode: str = "direct") -> LatencyHarness:
    h = LatencyHarness(tmp_path, side, mode)
    await h.protect()
    assert h.state().entry_terminal
    h.calls.clear()
    return h


def elapsed(h: LatencyHarness, start: datetime) -> float:
    return (h.clock.now - start).total_seconds()


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_direct_mode_fills_inside_budget_with_stop_still_live(tmp_path: Path, side: Side):
    h = await armed(tmp_path, side)
    decided = h.clock.now
    await h.adapter.close("pos-1", CloseReason.KEEPER)  # 不抛：已确认零仓，只差收尾
    placed = h.at("place_close")
    assert len(placed) == 1
    to_fill = placed[0] - (decided - START).total_seconds()
    assert to_fill < FILL_BUDGET_S
    assert [n for _, n in h.calls if n != "get_executor"][:2] == [
        "positions_evidence",
        "place_close",
    ]
    assert h.exchange.direct == [("dc-pos-1-1", 2.0)]
    assert all(status == "NEW" for status in h.exchange.legs_at_place[0].values())
    assert h.at("stop_executor")[0] > placed[0]  # HB 在单子上了交易所之后才只停不平
    assert h.api.stopped == [("owned-executor", True)]
    # HB 还没终态：保护腿一条不撤，账本保持平仓中。
    assert h.tracked().record.status is PositionStatus.CLOSING
    assert h.live_stop() and not h.cancels()
    assert '"direct_first":true' in h.path.read_text().replace(" ", "")
    h.hb_terminates()
    await h.adapter.protection_tick()
    assert "direct_first" not in h.path.read_text()  # 结束后账本与旧版一致，回退可读
    done = h.tracked()
    assert done.record.status is PositionStatus.CLOSED
    assert done.record.close_reason is CloseReason.KEEPER
    assert h.settled == [(CloseReason.KEEPER, CloseFill(100, None))]
    assert h.exchange.direct == [("dc-pos-1-1", 2.0)]
    assert not h.backups() and not h.live_stop() and len(h.cancels()) == 2
    [notice] = h.exit_alerts()
    assert (notice.level, notice.kind) == (AlertLevel.INFO, "close_done")
    assert notice.body.endswith("先直连、后停 HB（direct）；直连只减仓单 1 代，我方成交 2")
    assert not h.failure_alerts()


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_hb_first_mode_waits_for_hb_terminal_and_next_tick(tmp_path: Path, side: Side):
    h = await armed(tmp_path, side, "hb_first")
    decided = h.clock.now
    with pytest.raises(ProtectionIncomplete):
        await h.adapter.close("pos-1", CloseReason.KEEPER)
    assert not h.exchange.direct and h.api.stopped == [("owned-executor", True)]
    tick_wait = 12.4  # NEAR 2026-10-28 00:55:32.9 停止 → 00:55:45.3 串行保护轮回到这一仓
    h.clock.advance(tick_wait)
    h.hb_terminates()
    await h.adapter.protection_tick()
    placed = h.at("place_close")
    assert len(placed) == 1
    to_fill = placed[0] - (decided - START).total_seconds()
    assert to_fill > tick_wait > FILL_BUDGET_S
    before = [n for t, n in h.calls if t < placed[0] and n not in ("get_executor", "stop_executor")]
    assert len(before) >= 6  # 先核 HB 子单、入场、三条腿，才轮到下单
    assert h.tracked().record.status is PositionStatus.CLOSED


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
@pytest.mark.parametrize("actually_filled", [True, False])
async def test_direct_unknown_result_is_never_resent_across_ticks_and_restart(
    tmp_path: Path, side: Side, actually_filled: bool
):
    h = await armed(tmp_path, side)
    h.exchange.direct_unknown = True
    h.exchange.unknown_fills = actually_filled
    if actually_filled:
        await h.adapter.close("pos-1", CloseReason.KEEPER)  # 已零仓：不报平仓失败
    else:
        with pytest.raises(ProtectionIncomplete):
            await h.adapter.close("pos-1", CloseReason.KEEPER)
    h.hb_terminates()
    for _ in range(2):
        await h.adapter.protection_tick()
    h.restart_adapter()
    await h.adapter.protection_tick()
    if not actually_filled:
        with pytest.raises(ProtectionIncomplete):
            await h.adapter.close("pos-1", CloseReason.KEEPER)
    assert h.exchange.direct == [("dc-pos-1-1", 2.0)]
    assert h.tracked().direct_close_pending
    assert h.tracked().record.status is PositionStatus.CLOSING
    assert h.backups() and not h.cancels()  # 结果未明时一条保护都不撤
    if not actually_filled:
        return  # 原 CID 一直查不到就一直不重发，交给人工核实
    h.exchange.hidden_direct.clear()
    await h.adapter.protection_tick()
    assert h.tracked().record.status is PositionStatus.CLOSED
    assert h.exchange.direct == [("dc-pos-1-1", 2.0)]


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
@pytest.mark.parametrize("status", ["SHUTTING_DOWN", "RUNNING"])
async def test_hb_already_closing_on_its_own_falls_back_to_hb_first(
    tmp_path: Path, side: Side, status: str
):
    h = await armed(tmp_path, side)
    hb = h.api.executors["owned-executor"]
    hb.update(status=status, close_type="TIME_LIMIT")
    hb["custom_info"]["order_ids"] = ["entry-1", "hb-timer-close"]
    with pytest.raises(ProtectionIncomplete):
        await h.adapter.close("pos-1", CloseReason.KEEPER)
    assert not h.exchange.direct and not h.cancels()
    assert h.live_stop()


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_hb_selling_concurrently_leaves_one_reduce_only_rejection_and_no_retry(
    tmp_path: Path, side: Side
):
    h = await armed(tmp_path, side)
    hb = h.api.executors["owned-executor"]
    closer = "sell" if side is Side.LONG else "buy"

    async def hb_timer_fires_first() -> None:
        hb["custom_info"]["order_ids"] = ["entry-1", "hb-timer-close"]
        h.exchange.held = 0

    h.exchange.before_place = hb_timer_fires_first
    original_read = h.exchange.entry_order

    async def read_order(symbol, client_order_id):
        if client_order_id == "hb-timer-close":
            return ExchangeEntryOrder(
                symbol, client_order_id, "timer-1", closer, 2, 2, "FILLED", "BOTH", True
            )
        return await original_read(symbol, client_order_id)

    h.exchange.entry_order = read_order
    await h.adapter.close("pos-1", CloseReason.KEEPER)  # 被拒但已零仓：不报平仓失败
    assert h.exchange.direct == [("dc-pos-1-1", 2.0)]
    assert h.tracked().direct_close_order_status == "REJECTED"
    assert h.tracked().record.status is PositionStatus.CLOSING and h.live_stop()
    h.hb_terminates()
    await h.adapter.protection_tick()
    assert h.tracked().record.status is PositionStatus.CLOSED
    assert h.exchange.direct == [("dc-pos-1-1", 2.0)]
    assert not h.live_stop() and not h.backups()


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_partial_fill_keeps_stop_and_next_generation_closes_only_the_rest(
    tmp_path: Path, side: Side
):
    h = await armed(tmp_path, side)
    h.exchange.fill_cap = 1.5
    with pytest.raises(ProtectionIncomplete):
        await h.adapter.close("pos-1", CloseReason.KEEPER)
    assert h.exchange.held == 0.5 and h.live_stop() and not h.cancels()
    h.exchange.fill_cap = None
    h.hb_terminates()
    await h.adapter.protection_tick()
    assert h.exchange.direct == [("dc-pos-1-1", 2.0), ("dc-pos-1-2", 0.5)]
    done = h.tracked()
    assert done.record.status is PositionStatus.CLOSED
    assert done.record.realized_pnl_usdt is None  # 两代均价交成交明细回填


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_unknown_hb_stop_after_direct_fill_keeps_protection_until_hb_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, side: Side
):
    h = await armed(tmp_path, side)

    async def lost(executor_id, *, keep_position):
        raise HbApiError("synthetic response lost after accepted")

    monkeypatch.setattr(h.api, "stop_executor", lost)
    await h.adapter.close("pos-1", CloseReason.KEEPER)
    assert h.exchange.direct == [("dc-pos-1-1", 2.0)] and h.exchange.held == 0
    assert h.tracked().hb_close_unknown
    await h.adapter.protection_tick()
    assert h.tracked().record.status is PositionStatus.CLOSING
    assert h.live_stop() and not h.cancels()
    h.hb_terminates()
    await h.adapter.protection_tick()
    assert h.tracked().record.status is PositionStatus.CLOSED
    assert h.exchange.direct == [("dc-pos-1-1", 2.0)]


def trigger_stop(h: LatencyHarness, fills: float) -> None:
    """交易所一侧止损触发、子单成交；本地账本还没回读到。"""
    sl = next(cid for cid in h.exchange.orders if cid.startswith("br-sl-"))
    h.exchange.orders[sl] = replace(
        h.exchange.orders[sl],
        algo_status="FINISHED",
        child_state="identified",
        actual_order_id="child-sl",
        actual_order_status="FILLED",
        filled_quantity=fills,
        triggered_at=h.clock.now,
    )
    h.exchange.held -= fills


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
@pytest.mark.parametrize(
    "stop_fills,reason,cause",
    [
        (2.0, CloseReason.STOP_LOSS, "我方成交 0；交易所 stop_loss 腿成交 2"),
        (0.8, CloseReason.KEEPER, "我方成交 1.2；交易所 stop_loss 腿成交 0.8"),
    ],
)
async def test_exchange_stop_triggering_during_direct_close_settles_once(
    tmp_path: Path, side: Side, stop_fills: float, reason: CloseReason, cause: str
):
    h = await armed(tmp_path, side)

    async def stop_triggers() -> None:
        trigger_stop(h, stop_fills)

    h.exchange.before_place = stop_triggers
    await h.adapter.close("pos-1", CloseReason.KEEPER)
    assert h.exchange.held == 0
    assert len(h.exchange.direct) == 1
    h.hb_terminates()
    await h.adapter.protection_tick()
    assert h.tracked().record.status is PositionStatus.CLOSED
    # 止损全吃掉：原因归止损；各成交一部分：仍是请求的原因，均价交成交明细回填。
    assert h.settled == [(reason, CloseFill())]
    assert len(h.exchange.direct) == 1
    assert h.exit_alerts()[0].body.endswith(cause)
    assert not h.failure_alerts()


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_stop_fill_before_held_read_cannot_price_whole_position_from_remainder(
    tmp_path: Path, side: Side
):
    h = await armed(tmp_path, side)
    trigger_stop(h, 0.8)  # 我方读余仓之前已成交；只能下 1.2
    await h.adapter.close("pos-1", CloseReason.KEEPER)
    assert h.exchange.direct == [("dc-pos-1-1", 1.2)]
    h.hb_terminates()
    await h.adapter.protection_tick()
    assert h.tracked().record.status is PositionStatus.CLOSED
    assert h.settled == [(CloseReason.KEEPER, CloseFill())]


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
@pytest.mark.parametrize("mode", ["direct", "hb_first"])
async def test_submit_failing_before_request_retries_with_next_generation(
    tmp_path: Path, side: Side, mode: str
):
    h = await armed(tmp_path, side, mode)
    h.exchange.not_sent_once = True
    if mode == "hb_first":
        h.api.auto_terminate_on_stop = True
    with pytest.raises(ProtectionIncomplete) as failed:
        await h.adapter.close("pos-1", CloseReason.KEEPER)
    assert not isinstance(failed.value, CloseInProgress)  # a definite non-send stays loud
    current = h.tracked()
    assert not current.direct_close_pending
    assert current.direct_close_order_status == "NOT_SENT"
    assert h.live_stop() and h.exchange.held == 2
    h.hb_terminates()
    await h.adapter.protection_tick()
    assert h.exchange.direct == [("dc-pos-1-2", 2.0)]
    assert h.tracked().record.status is PositionStatus.CLOSED


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_crash_between_stop_intent_and_request_resends_keep_position_stop(
    tmp_path: Path, side: Side
):
    h = await armed(tmp_path, side)
    h.exchange.held = 0  # 直连已成交，落盘了停止意图但请求前崩溃
    tracked = h.tracked()
    h.registry.put(
        tracked.model_copy(
            update={
                "record": tracked.record.model_copy(update={"status": PositionStatus.CLOSING}),
                "requested_close": CloseReason.KEEPER,
                "hb_close_pending": True,
                "server_protection": h.state().model_copy(update={"stage": "exiting"}),
            }
        )
    )
    h.restart_adapter()
    h.api.auto_terminate_on_stop = False
    await h.adapter.protection_tick()
    assert h.api.stopped == [("owned-executor", True)]
    await h.adapter.protection_tick()
    assert h.api.stopped == [("owned-executor", True)]  # 本进程已发过：只等终态，不连发
    h.hb_terminates()
    await h.adapter.protection_tick()
    assert h.tracked().record.status is PositionStatus.CLOSED
    assert h.settled == [(CloseReason.KEEPER, CloseFill())]


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_exiting_stage_blocks_trailing_replacement_and_tighten(tmp_path: Path, side: Side):
    h = await armed(tmp_path, side)
    created = list(h.creates())
    h.manager.mark_exiting("pos-1")
    assert h.state().stage == "exiting"
    await h.adapter._update_server_trailing("pos-1")
    result = await h.adapter.tighten_stop("pos-1", reason="synthetic", buffer_bps=0)
    assert result is not None and result.get("why") == "protection_exiting"
    assert h.creates() == created and not h.cancels()


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_triggered_then_cancelled_leg_is_not_quiet(tmp_path: Path, side: Side):
    h = await armed(tmp_path, side)
    assert h.manager.legs_quiet("pos-1")
    state = h.state()
    legs = [
        leg.model_copy(
            update={
                "state": "cancelled",
                "algo_status": "CANCELED",
                "child_state": "identified",
                "actual_order_id": "child-lb",
                "actual_order_status": "CANCELED",
                "filled_quantity": 0.5,
                "triggered_at": START,
            }
        )
        if leg.purpose == "lifeboat"
        else leg
        for leg in state.legs
    ]
    tracked = h.tracked()
    h.registry.put(
        tracked.model_copy(update={"server_protection": state.model_copy(update={"legs": legs})})
    )
    assert not h.manager.legs_quiet("pos-1")
    with pytest.raises(ProtectionIncomplete):
        await h.adapter.close("pos-1", CloseReason.KEEPER)
    assert not h.exchange.direct


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_unclear_leg_falls_back_to_hb_first(tmp_path: Path, side: Side):
    h = await armed(tmp_path, side)
    state = h.state()
    legs = [
        leg.model_copy(update={"state": "submit_pending"}) if leg.purpose == "lifeboat" else leg
        for leg in state.legs
    ]
    tracked = h.tracked()
    h.registry.put(
        tracked.model_copy(update={"server_protection": state.model_copy(update={"legs": legs})})
    )
    with pytest.raises(ProtectionIncomplete):
        await h.adapter.close("pos-1", CloseReason.KEEPER)
    assert not h.exchange.direct and h.live_stop()


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_direct_order_acked_but_not_filled_is_in_progress_and_settles_once(
    tmp_path: Path, side: Side
):
    h = await armed(tmp_path, side)
    h.adapter._exit_fills = ExitFills(tmp_path / "exit_fills.json")
    h.exchange.ack_only = True
    with pytest.raises(CloseInProgress):
        await h.adapter.close("pos-1", CloseReason.KEEPER)
    assert h.tracked().direct_close_pending and h.live_stop()
    await h.adapter.protection_tick()
    assert h.exchange.direct == [("dc-pos-1-1", 2.0)]
    filled_at = h.clock.now
    h.exchange.fill_acked("dc-pos-1-1", filled_at)
    h.clock.advance(20)
    h.hb_terminates()
    await h.adapter.protection_tick()
    done = h.tracked().record
    assert done.status is PositionStatus.CLOSED
    assert h.exchange.direct == [("dc-pos-1-1", 2.0)]
    assert done.closed_at is not None and (done.closed_at - filled_at).total_seconds() >= 20
    assert read_exit_fills(tmp_path / "exit_fills.json") == {"pos-1": filled_at}
    assert not h.failure_alerts()


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
@pytest.mark.parametrize("when_exiting", [False, True])
async def test_triggered_leg_with_working_child_is_in_progress_without_direct_order(
    tmp_path: Path, side: Side, when_exiting: bool
):
    h = await armed(tmp_path, side)
    h.adapter._direct_when_exiting = when_exiting
    sl = next(cid for cid in h.exchange.orders if cid.startswith("br-sl-"))
    working = {
        "algo_status": "TRIGGERED",
        "child_state": "identified",
        "actual_order_id": "child-sl",
        "actual_order_status": "NEW",
        "filled_quantity": 0.0,
        "triggered_at": h.clock.now,
    }
    h.exchange.orders[sl] = replace(h.exchange.orders[sl], **working)
    state = h.state()
    legs = [
        leg.model_copy(update=working) if leg.client_order_id == sl else leg for leg in state.legs
    ]
    h.registry.put(
        h.tracked().model_copy(
            update={"server_protection": state.model_copy(update={"legs": legs})}
        )
    )
    with pytest.raises(CloseInProgress):
        await h.adapter.close("pos-1", CloseReason.KEEPER)
    assert not h.exchange.direct


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
@pytest.mark.parametrize("when_exiting", [False, True])
async def test_direct_when_exiting_places_the_order_before_stopping_hb(
    tmp_path: Path, side: Side, when_exiting: bool
):
    h = await armed(tmp_path, side)
    h.adapter._direct_when_exiting = when_exiting
    h.manager.mark_exiting("pos-1")
    if when_exiting:
        await h.adapter.close("pos-1", CloseReason.KEEPER)
        assert h.at("place_close")[0] < h.at("stop_executor")[0]
        assert h.tracked().direct_first
    else:
        with pytest.raises(CloseInProgress):
            await h.adapter.close("pos-1", CloseReason.KEEPER)
        assert not h.exchange.direct
    assert h.live_stop()
    h.hb_terminates()
    await h.adapter.protection_tick()
    assert h.tracked().record.status is PositionStatus.CLOSED
    assert h.exchange.direct == [("dc-pos-1-1", 2.0)]
    assert not h.failure_alerts()


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_hb_stop_that_never_terminates_warns_once_after_the_grace(tmp_path: Path, side: Side):
    h = await armed(tmp_path, side, "hb_first")
    with pytest.raises(CloseInProgress):
        await h.adapter.close("pos-1", CloseReason.KEEPER)

    def slow() -> list[Alert]:
        return [a for a in h.alerter.sent if a.dedup_key == "close_slow:pos-1"]

    h.clock.advance(119)
    await h.adapter.protection_tick()
    assert not slow()
    h.clock.advance(2)
    await h.adapter.protection_tick()
    await h.adapter.protection_tick()
    [alert] = slow()
    assert alert.level is AlertLevel.WARN and "pos-1" in alert.body
    h.hb_terminates()
    await h.adapter.protection_tick()
    assert h.tracked().record.status is PositionStatus.CLOSED
    assert "pos-1" not in h.adapter._exit_started


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_unknown_hb_stop_result_stays_a_loud_failure(tmp_path: Path, side: Side):
    h = await armed(tmp_path, side, "hb_first")
    h.api.stop_error = HbApiError("synthetic stop timeout")
    with pytest.raises(ProtectionIncomplete) as failed:
        await h.adapter.close("pos-1", CloseReason.KEEPER)
    assert h.tracked().hb_close_pending and h.tracked().hb_close_unknown
    assert not isinstance(failed.value, CloseInProgress)


def test_unknown_hosted_close_mode_is_refused(tmp_path: Path):
    h = LatencyHarness(tmp_path, Side.LONG)
    with pytest.raises(ValueError):
        HbExecutorAdapter(
            h.api,
            h.lifeboat,
            h.registry,
            h.alerter,
            account_name="synthetic-account",
            connector_name="synthetic-demo",
            hosted_close_mode="fast",  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
@pytest.mark.parametrize("retries", [0, 3])
async def test_unknown_hb_stop_is_resent_with_backoff_up_to_the_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, side: Side, retries: int
):
    h = await armed(tmp_path, side)
    h.adapter._hb_stop_unknown_retries = retries
    attempts: list[float] = []

    async def always_lost(executor_id, *, keep_position):
        assert keep_position is True
        attempts.append(elapsed(h, START))
        raise HbApiError("synthetic response lost after accepted")

    monkeypatch.setattr(h.api, "stop_executor", always_lost)
    await h.adapter.close("pos-1", CloseReason.KEEPER)
    assert h.exchange.held == 0 and len(attempts) == 1
    await h.adapter.protection_tick()
    assert len(attempts) == 1  # 退避未到不重发
    for _ in range(120):
        h.clock.advance(1)
        await h.adapter.protection_tick()
    assert len(attempts) == 1 + retries
    gaps = [b - a for a, b in pairwise(attempts)]
    assert all(gap >= floor for gap, floor in zip(gaps, [5, 10, 20], strict=False))
    assert h.tracked().record.status is PositionStatus.CLOSING and h.live_stop()


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_unknown_hb_stop_resend_success_finishes_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, side: Side
):
    h = await armed(tmp_path, side)
    h.adapter._hb_stop_unknown_retries = 3
    original = h.api.stop_executor
    lost = [True]

    async def lost_once(executor_id, *, keep_position):
        if lost:
            lost.pop()
            raise HbApiError("synthetic response lost after accepted")
        await original(executor_id, keep_position=keep_position)
        h.hb_terminates()

    monkeypatch.setattr(h.api, "stop_executor", lost_once)
    await h.adapter.close("pos-1", CloseReason.KEEPER)
    assert h.tracked().hb_close_unknown
    h.clock.advance(60)
    await h.adapter.protection_tick()
    await h.adapter.protection_tick()
    assert h.api.stopped == [("owned-executor", True)]
    assert h.tracked().record.status is PositionStatus.CLOSED
    assert not h.live_stop() and not h.backups()


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_cancel_before_request_is_recorded_not_sent_and_reraised(tmp_path: Path, side: Side):
    h = await armed(tmp_path, side)

    async def cancelled() -> None:
        raise IdentifiedCloseCancelledBeforeSend()

    h.exchange.before_place = cancelled
    with pytest.raises(asyncio.CancelledError):
        await h.adapter.close("pos-1", CloseReason.KEEPER)
    current = h.tracked()
    assert not current.direct_close_pending
    assert current.direct_close_order_status == "NOT_SENT"
    h.exchange.before_place = None
    h.hb_terminates()
    await h.adapter.protection_tick()
    assert h.exchange.direct == [("dc-pos-1-2", 2.0)]
    assert h.tracked().record.status is PositionStatus.CLOSED


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_leg_reason_survives_an_earlier_not_sent_generation(tmp_path: Path, side: Side):
    h = await armed(tmp_path, side)
    h.exchange.not_sent_once = True
    with pytest.raises(ProtectionIncomplete):
        await h.adapter.close("pos-1", CloseReason.KEEPER)

    async def stop_takes_everything() -> None:
        trigger_stop(h, 2.0)

    h.exchange.before_place = stop_takes_everything
    h.hb_terminates()
    await h.adapter.protection_tick()  # 第 2 代被只减仓拒绝：止损先平光了
    h.exchange.before_place = None
    await h.adapter.protection_tick()
    assert h.tracked().record.status is PositionStatus.CLOSED
    assert h.settled == [(CloseReason.STOP_LOSS, CloseFill())]
    assert h.exit_alerts()[0].body.endswith("我方成交 0；交易所 stop_loss 腿成交 2")


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_earlier_generation_fill_counts_as_ours(tmp_path: Path, side: Side):
    h = await armed(tmp_path, side)
    h.exchange.fill_cap = 1.5
    with pytest.raises(ProtectionIncomplete):
        await h.adapter.close("pos-1", CloseReason.KEEPER)
    h.exchange.fill_cap = None

    async def stop_takes_rest() -> None:
        trigger_stop(h, 0.5)

    h.exchange.before_place = stop_takes_rest
    h.hb_terminates()
    await h.adapter.protection_tick()
    h.exchange.before_place = None
    await h.adapter.protection_tick()
    assert h.tracked().record.status is PositionStatus.CLOSED
    # 第 1 代成交了 1.5：不是腿平掉的整仓，仍按请求的原因。
    assert h.settled == [(CloseReason.KEEPER, CloseFill())]
    assert "直连只减仓单 2 代，我方成交 1.5" in h.exit_alerts()[0].body


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
@pytest.mark.parametrize("status", ["SHUTTING_DOWN", "TERMINATED"])
async def test_unknown_hb_stop_is_not_resent_unless_hb_reports_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, side: Side, status: str
):
    h = await armed(tmp_path, side)
    h.adapter._hb_stop_unknown_retries = 3
    attempts: list[bool] = []

    async def lost(executor_id, *, keep_position):
        attempts.append(keep_position)
        raise HbApiError("synthetic response lost after accepted")

    monkeypatch.setattr(h.api, "stop_executor", lost)
    await h.adapter.close("pos-1", CloseReason.KEEPER)
    # 只改 HB 报的状态，不改 is_active：终态核验仍不过，只看补发与否。
    h.api.executors["owned-executor"]["status"] = status
    for _ in range(5):
        h.clock.advance(60)
        await h.adapter.protection_tick()
    assert attempts == [True]


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_successful_resend_clears_unknown_before_hb_terminates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, side: Side
):
    h = await armed(tmp_path, side)
    h.adapter._hb_stop_unknown_retries = 3
    original = h.api.stop_executor
    lost = [True]

    async def lost_once(executor_id, *, keep_position):
        if lost:
            lost.pop()
            raise HbApiError("synthetic response lost after accepted")
        await original(executor_id, keep_position=keep_position)

    monkeypatch.setattr(h.api, "stop_executor", lost_once)
    await h.adapter.close("pos-1", CloseReason.KEEPER)
    assert h.tracked().hb_close_unknown
    h.clock.advance(60)
    await h.adapter.protection_tick()
    assert h.api.stopped == [("owned-executor", True)]
    assert not h.tracked().hb_close_unknown and h.tracked().hb_close_pending
    assert "pos-1" not in h.adapter._hb_stop_retries
    h.clock.advance(60)
    await h.adapter.protection_tick()
    assert h.api.stopped == [("owned-executor", True)]  # 已发成功：只等终态，不再发


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_retry_bookkeeping_is_dropped_once_the_position_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, side: Side
):
    h = await armed(tmp_path, side)

    async def lost(executor_id, *, keep_position):
        raise HbApiError("synthetic response lost after accepted")

    monkeypatch.setattr(h.api, "stop_executor", lost)
    await h.adapter.close("pos-1", CloseReason.KEEPER)
    assert "pos-1" in h.adapter._hb_stop_retries
    h.hb_terminates()
    await h.adapter.protection_tick()
    assert h.tracked().record.status is PositionStatus.CLOSED
    assert "pos-1" not in h.adapter._hb_stop_retries


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_earlier_generation_that_reached_the_exchange_but_reads_absent_is_unknown(
    tmp_path: Path, side: Side
):
    h = await armed(tmp_path, side)
    h.exchange.fill_cap = 1.5
    with pytest.raises(ProtectionIncomplete):
        await h.adapter.close("pos-1", CloseReason.KEEPER)
    assert h.tracked().direct_close_outcomes == {"1": "EXPIRED"}
    h.exchange.fill_cap = None
    h.exchange.hidden_direct.add("dc-pos-1-1")  # 交易所查不到这代了，但它确实成交过

    async def stop_takes_rest() -> None:
        trigger_stop(h, 0.5)

    h.exchange.before_place = stop_takes_rest
    h.hb_terminates()
    await h.adapter.protection_tick()
    h.exchange.before_place = None
    await h.adapter.protection_tick()
    assert h.tracked().record.status is PositionStatus.CLOSED
    assert h.settled == [(CloseReason.KEEPER, CloseFill())]
    assert "直连只减仓单 2 代，成交量未知" in h.exit_alerts()[0].body


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_generation_outcomes_are_dropped_from_the_ledger_once_closed(
    tmp_path: Path, side: Side
):
    h = await armed(tmp_path, side)
    h.exchange.fill_cap = 1.5
    with pytest.raises(ProtectionIncomplete):
        await h.adapter.close("pos-1", CloseReason.KEEPER)
    assert "direct_close_outcomes" in h.path.read_text()
    h.exchange.fill_cap = None
    h.hb_terminates()
    await h.adapter.protection_tick()
    assert h.tracked().record.status is PositionStatus.CLOSED
    assert "direct_close_outcomes" not in h.path.read_text()  # 结束后账本与旧版一致


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_generation_without_a_recorded_outcome_counts_as_unknown(tmp_path: Path, side: Side):
    h = await armed(tmp_path, side)
    h.exchange.not_sent_once = True
    with pytest.raises(ProtectionIncomplete):
        await h.adapter.close("pos-1", CloseReason.KEEPER)
    # 升级时正在平仓的仓：前一代没有落盘结果。
    h.registry.put(h.tracked().model_copy(update={"direct_close_outcomes": {}}))

    async def stop_takes_everything() -> None:
        trigger_stop(h, 2.0)

    h.exchange.before_place = stop_takes_everything
    h.hb_terminates()
    await h.adapter.protection_tick()
    h.exchange.before_place = None
    await h.adapter.protection_tick()
    assert h.tracked().record.status is PositionStatus.CLOSED
    assert h.settled == [(CloseReason.KEEPER, CloseFill())]
    assert "直连只减仓单 2 代，成交量未知" in h.exit_alerts()[0].body


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
@pytest.mark.parametrize(
    ("mode", "exiting"), [("hb_first", False), ("hb_first", True), ("direct", True)]
)
async def test_manual_close_sends_the_order_before_any_leg_cancel_or_hb_stop(
    tmp_path: Path, side: Side, mode: str, exiting: bool
):
    h = await armed(tmp_path, side, mode)
    assert not h.adapter._direct_when_exiting
    if exiting:
        h.manager.mark_exiting("pos-1")
    h.api.auto_terminate_on_stop = True
    await h.adapter.close("pos-1", CloseReason.MANUAL)
    names = [n for _, n in h.calls]
    first_order = names.index("place_close")
    assert "stop_executor" not in names[:first_order]
    assert not any(n.startswith("cancel_") for n in names[:first_order])
    assert h.exchange.direct == [("dc-pos-1-1", 2.0)]
    assert all(status == "NEW" for status in h.exchange.legs_at_place[0].values())
    await h.adapter.close("pos-1", CloseReason.MANUAL)  # 再点一次不再下单
    await h.adapter.protection_tick()
    done = h.tracked()
    assert done.record.status is PositionStatus.CLOSED
    assert done.record.close_reason is CloseReason.MANUAL
    assert h.exchange.direct == [("dc-pos-1-1", 2.0)]
    assert not h.backups() and not h.live_stop() and len(h.cancels()) == 2
    assert not h.failure_alerts()


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_keeper_close_in_hb_first_mode_still_stops_hb_first(tmp_path: Path, side: Side):
    h = await armed(tmp_path, side, "hb_first")
    with pytest.raises(ProtectionIncomplete):
        await h.adapter.close("pos-1", CloseReason.KEEPER)
    assert not h.exchange.direct and h.api.stopped == [("owned-executor", True)]


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_close_order_is_sized_on_the_close_lane_read(tmp_path: Path, side: Side):
    h = await armed(tmp_path, side)
    lane: list[str] = []
    shared = type(h.exchange).positions_evidence.__get__(h.exchange)

    async def close_lane():
        lane.append("close_positions_evidence")
        return await shared()

    h.exchange.close_positions_evidence = close_lane  # type: ignore[attr-defined]
    await h.adapter.close("pos-1", CloseReason.MANUAL)
    names = [n for _, n in h.calls]
    assert "positions_evidence" not in names[: names.index("place_close")]
    assert lane and h.exchange.direct == [("dc-pos-1-1", 2.0)]


async def test_direct_close_leaves_the_exit_lock_once_flat_and_sync_skips_the_cleanup(
    tmp_path: Path,
):
    h = await armed(tmp_path, Side.LONG)
    h.api.auto_terminate_on_stop = True  # HB 当场终态：收尾马上就能做，看它在不在平仓调用里做

    def slow(name: str) -> None:
        original = getattr(h.exchange, name)

        async def call(*args, **kwargs):
            await asyncio.sleep(0.3)
            return await original(*args, **kwargs)

        setattr(h.exchange, name, call)

    slow("cancel_bracket")
    slow("cancel_lifeboat")
    started = time.perf_counter()
    await asyncio.wait_for(h.adapter.close("pos-1", CloseReason.KEEPER), 5)
    assert time.perf_counter() - started < 0.25
    assert h.exchange.direct == [("dc-pos-1-1", 2.0)] and not h.cancels()
    assert h.tracked().record.status is PositionStatus.CLOSING
    assert not h.adapter._exit_busy("pos-1")
    tick = asyncio.create_task(h.adapter.protection_tick())
    await asyncio.sleep(0.1)
    assert h.adapter._exit_busy("pos-1")  # 保护轮正在锁里撤腿
    started = time.perf_counter()
    await asyncio.wait_for(h.adapter.positions(), 0.2)
    assert time.perf_counter() - started < 0.2
    await asyncio.wait_for(tick, 10)
    assert h.tracked().record.status is PositionStatus.CLOSED
    assert len(h.cancels()) == 3 and not h.live_stop()


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_manual_close_whose_direct_first_fails_falls_back_to_hb_first(
    tmp_path: Path, side: Side
):
    h = await armed(tmp_path, side)
    h.api.auto_terminate_on_stop = True

    async def close_lane_down():
        raise ExchangeError("synthetic close-lane read failed")

    h.exchange.close_positions_evidence = close_lane_down  # type: ignore[attr-defined]
    with pytest.raises(ProtectionIncomplete):
        await h.adapter.close("pos-1", CloseReason.MANUAL)
    assert not h.exchange.direct and h.tracked().direct_first and h.live_stop()
    await h.adapter.protection_tick()
    names = [n for _, n in h.calls]
    assert names.index("stop_executor") < names.index("place_close")
    done = h.tracked()
    assert done.record.status is PositionStatus.CLOSED
    assert done.record.close_reason is CloseReason.MANUAL
    assert h.exchange.direct == [("dc-pos-1-1", 2.0)]


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_manual_close_beaten_by_a_stop_fill_is_booked_as_the_stop(tmp_path: Path, side: Side):
    h = await armed(tmp_path, side)

    async def stop_triggers() -> None:
        trigger_stop(h, 2.0)

    h.exchange.before_place = stop_triggers
    await h.adapter.close("pos-1", CloseReason.MANUAL)
    assert h.exchange.held == 0 and len(h.exchange.direct) == 1
    h.hb_terminates()
    await h.adapter.protection_tick()
    assert h.tracked().record.status is PositionStatus.CLOSED
    assert h.settled == [(CloseReason.STOP_LOSS, CloseFill())]
    assert h.exit_alerts()[0].body.endswith("我方成交 0；交易所 stop_loss 腿成交 2")
