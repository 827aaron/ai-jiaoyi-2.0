"""直连平仓的独立故障回归：ACK不是零仓，未知提交不能撤保护或重复下单。"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from jiaoyi.contracts import Side
from jiaoyi.contracts.position import CloseReason, PositionStatus, ReconcileIssueKind
from jiaoyi.execution.exchange import CloseFill, ExchangeError
from jiaoyi.execution.hb_client import HbApiError
from jiaoyi.execution.hb_executor import SymbolAlreadyHeld
from jiaoyi.execution.registry import PositionRegistry
from tests.test_execution import Rig, intent, open_filled


async def unavailable_hb(*args: Any, **kwargs: Any) -> None:
    raise HbApiError("synthetic Hummingbot explicitly rejected", definite_rejection=True)


async def request_close(rig: Rig, pid: str) -> None:
    # 接口可以报告待重试；账本、保护和是否重复提交才是本组验收对象。
    with suppress(HbApiError, ExchangeError):
        await rig.adapter.close(pid, CloseReason.KEEPER)


@pytest.mark.parametrize("side", ["long", "short"])
async def test_late_legacy_entry_lookup_cannot_erase_pending_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    side: str,
) -> None:
    """升级前账本补 CID 的迟到查询，不得覆盖查询期间保存的退出意图。"""
    rig = Rig(tmp_path)
    pid = await open_filled(rig, side=side)
    stale = rig.registry.get(pid).model_copy(
        update={"entry_client_order_id": None, "entry_original_quantity": None}
    )
    rig.registry.put(stale)
    stops = dict(rig.ex.stops)
    detail = await rig.api.get_executor(stale.record.executor_id)
    lookup_started, lookup_release = asyncio.Event(), asyncio.Event()
    submissions = 0

    async def slow_lookup(executor_id: str) -> Any:
        lookup_started.set()
        await lookup_release.wait()
        return detail

    async def accepted_then_timeout(*args: Any, **kwargs: Any) -> CloseFill:
        nonlocal submissions
        submissions += 1
        raise ExchangeError("synthetic accepted then timeout", transient=True)

    monkeypatch.setattr(rig.api, "get_executor", slow_lookup)
    monkeypatch.setattr(rig.api, "stop_executor", unavailable_hb)
    monkeypatch.setattr(rig.ex, "close_position_market", accepted_then_timeout)
    syncing = asyncio.create_task(rig.adapter._sync_one(stale))
    await lookup_started.wait()
    closing = asyncio.create_task(request_close(rig, pid))
    try:
        # 兼容两种安全实现：锁串行化，或异步查询后只合并最新账本的入场证据。
        with suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(closing), timeout=0.05)
    finally:
        lookup_release.set()
        await asyncio.gather(syncing, closing)

    state = rig.registry.get(pid)
    assert state.record.status is PositionStatus.CLOSING
    assert state.requested_close is CloseReason.KEEPER
    assert state.direct_close_pending is True
    assert state.direct_close_client_order_id is not None
    assert state.direct_close_generation == 1
    assert submissions == 1
    assert_still_protected(rig, pid, stops)
    restored = PositionRegistry(tmp_path / "positions.json").get(pid)
    assert restored.direct_close_client_order_id == state.direct_close_client_order_id
    assert restored.direct_close_pending is True


def assert_still_protected(rig: Rig, pid: str, stops: dict[str, Any]) -> None:
    tracked = rig.registry.get(pid)
    assert tracked is not None
    assert tracked.record.status is not PositionStatus.CLOSED
    assert tracked.record.closed_at is None
    assert rig.ex.stops == stops and rig.ex.cancelled == []


@pytest.mark.parametrize("side", ["long", "short"])
@pytest.mark.parametrize("remaining", [0.015, 0.006])
async def test_adapter_ack_or_partial_fill_keeps_remaining_position_and_protection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    side: str,
    remaining: float,
) -> None:
    rig = Rig(tmp_path)
    pid = await open_filled(rig, side=side)
    stops = dict(rig.ex.stops)
    calls: list[tuple[str, str, float]] = []

    async def accepted(symbol: str, close_side: str, quantity: float, **kwargs: Any) -> CloseFill:
        calls.append((symbol, close_side, quantity))
        rig.ex.set_position(symbol, Side(side), remaining)
        return CloseFill(None if remaining == 0.015 else 68000.0, None)

    monkeypatch.setattr(rig.api, "stop_executor", unavailable_hb)
    monkeypatch.setattr(rig.ex, "close_position_market", accepted)
    await request_close(rig, pid)
    assert len(calls) == 1
    assert_still_protected(rig, pid, stops)
    assert rig.registry.get(pid).record.status is PositionStatus.CLOSING


@pytest.mark.parametrize("side", ["long", "short"])
async def test_adapter_unknown_venue_position_cannot_confirm_flat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    side: str,
) -> None:
    rig = Rig(tmp_path)
    pid = await open_filled(rig, side=side)
    stops = dict(rig.ex.stops)

    async def accepted(*args: Any, **kwargs: Any) -> CloseFill:
        return CloseFill(68000.0, 0.1)

    async def unknown() -> Any:
        raise ExchangeError("synthetic position query timeout", transient=True)

    monkeypatch.setattr(rig.api, "stop_executor", unavailable_hb)
    monkeypatch.setattr(rig.ex, "close_position_market", accepted)
    monkeypatch.setattr(rig.ex, "positions", unknown)
    await request_close(rig, pid)
    assert_still_protected(rig, pid, stops)


@pytest.mark.parametrize("side", ["long", "short"])
async def test_adapter_uncertain_submit_is_not_rolled_back_or_submitted_again(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    side: str,
) -> None:
    rig = Rig(tmp_path)
    pid = await open_filled(rig, side=side)
    stops = dict(rig.ex.stops)
    calls = 0

    async def uncertain(*args: Any, **kwargs: Any) -> CloseFill:
        nonlocal calls
        calls += 1
        raise ExchangeError("synthetic accepted-then-timeout", transient=True)

    monkeypatch.setattr(rig.api, "stop_executor", unavailable_hb)
    monkeypatch.setattr(rig.ex, "close_position_market", uncertain)
    await request_close(rig, pid)
    first = rig.registry.get(pid)
    await request_close(rig, pid)
    assert calls == 1  # 结果不明，不能只因还有仓就发送第二张不同的市价单。
    assert first.record.status is PositionStatus.CLOSING
    assert first.requested_close is CloseReason.KEEPER
    assert_still_protected(rig, pid, stops)


@pytest.mark.parametrize("side", ["long", "short"])
async def test_pending_close_only_settles_after_fresh_zero_position(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    side: str,
) -> None:
    rig = Rig(tmp_path)
    pid = await open_filled(rig, side=side)
    stops = dict(rig.ex.stops)
    calls = 0

    async def accepted(*args: Any, **kwargs: Any) -> CloseFill:
        nonlocal calls
        calls += 1
        return CloseFill()

    monkeypatch.setattr(rig.api, "stop_executor", unavailable_hb)
    monkeypatch.setattr(rig.ex, "close_position_market", accepted)
    await request_close(rig, pid)
    assert_still_protected(rig, pid, stops)
    rig.ex.set_position("BTCUSDT", Side(side), 0)
    await request_close(rig, pid)
    assert calls == 1
    assert_still_protected(rig, pid, stops)  # 零仓但原单仍 NEW，不能撤保护后容许新代仓。
    cid = rig.registry.get(pid).direct_close_client_order_id
    order = rig.ex.close_orders[cid]
    rig.ex.close_orders[cid] = replace(
        order,
        status="FILLED",
        filled_quantity=order.quantity,
        remaining_quantity=0.0,
        is_terminal=True,
    )
    rig.api.terminate("ex1", "SYSTEM_CLEANUP", leaves_position=True)
    await request_close(rig, pid)
    assert calls == 1
    assert rig.registry.get(pid).record.status is PositionStatus.CLOSED
    assert rig.ex.stops == {}


@pytest.mark.parametrize("side", ["long", "short"])
async def test_reconciler_return_from_slow_adapter_does_not_mean_flat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    side: str,
) -> None:
    rig = Rig(tmp_path)
    pid = await open_filled(rig, side=side)
    stops = dict(rig.ex.stops)

    async def pending(position_id: str, reason: CloseReason) -> None:
        tracked = rig.registry.get(position_id)
        rig.registry.put(
            tracked.model_copy(
                update={
                    "record": tracked.record.model_copy(update={"status": PositionStatus.CLOSING}),
                    "requested_close": reason,
                }
            )
        )

    monkeypatch.setattr(rig.adapter, "close", pending)
    issue, flattened = await rig.rec._flatten(
        pid, "synthetic protective close", kind=ReconcileIssueKind.EXECUTOR_FAILED
    )
    assert not issue.fixed and not flattened
    assert_still_protected(rig, pid, stops)


@pytest.mark.parametrize("side", ["long", "short"])
@pytest.mark.parametrize("remaining", [0.015, 0.006])
async def test_reconciler_fallback_ack_or_partial_cannot_settle_and_cancel_protection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    side: str,
    remaining: float,
) -> None:
    rig = Rig(tmp_path)
    pid = await open_filled(rig, side=side)
    stops = dict(rig.ex.stops)

    async def accepted(symbol: str, close_side: str, quantity: float, **kwargs: Any) -> CloseFill:
        rig.ex.set_position(symbol, Side(side), remaining)
        return CloseFill(68000.0, 0.1)

    monkeypatch.setattr(rig.adapter, "close", unavailable_hb)
    monkeypatch.setattr(rig.ex, "close_position_market", accepted)
    issue, flattened = await rig.rec._flatten(pid, "synthetic protective close")
    assert not issue.fixed and not flattened
    assert_still_protected(rig, pid, stops)


@pytest.mark.parametrize("side", ["long", "short"])
async def test_reconciler_fallback_keeps_protection_when_position_query_is_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    side: str,
) -> None:
    rig = Rig(tmp_path)
    pid = await open_filled(rig, side=side)
    stops = dict(rig.ex.stops)

    async def accepted(*args: Any, **kwargs: Any) -> CloseFill:
        return CloseFill(68000.0, 0.1)

    async def unknown() -> Any:
        raise ExchangeError("synthetic position query unavailable", transient=True)

    monkeypatch.setattr(rig.adapter, "close", unavailable_hb)
    monkeypatch.setattr(rig.ex, "close_position_market", accepted)
    monkeypatch.setattr(rig.ex, "positions", unknown)
    issue, flattened = await rig.rec._flatten(pid, "synthetic protective close")
    assert not issue.fixed and not flattened
    assert_still_protected(rig, pid, stops)


@pytest.mark.parametrize("side", ["long", "short"])
async def test_two_fallback_layers_do_not_duplicate_an_uncertain_market_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    side: str,
) -> None:
    rig = Rig(tmp_path)
    pid = await open_filled(rig, side=side)
    stops = dict(rig.ex.stops)
    calls = 0

    async def uncertain(*args: Any, **kwargs: Any) -> CloseFill:
        nonlocal calls
        calls += 1
        raise ExchangeError("synthetic accepted-then-timeout", transient=True)

    monkeypatch.setattr(rig.api, "stop_executor", unavailable_hb)
    monkeypatch.setattr(rig.ex, "close_position_market", uncertain)
    issue, flattened = await rig.rec._flatten(pid, "synthetic protective close")
    assert not issue.fixed and not flattened
    assert calls == 1
    assert_still_protected(rig, pid, stops)


@pytest.mark.parametrize("side", ["long", "short"])
async def test_hb_accepted_then_timeout_never_switches_to_direct_or_repeats_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    side: str,
) -> None:
    rig = Rig(tmp_path)
    pid = await open_filled(rig, side=side)
    stops = dict(rig.ex.stops)
    hb_calls = 0

    async def accepted_then_timeout(*args: Any, **kwargs: Any) -> None:
        nonlocal hb_calls
        hb_calls += 1
        raise HbApiError("synthetic accepted stop response timeout", transient=True)

    monkeypatch.setattr(rig.api, "stop_executor", accepted_then_timeout)
    await request_close(rig, pid)
    await request_close(rig, pid)
    rig.registry = PositionRegistry(tmp_path / "positions.json")
    rig.adapter = rig._adapter()  # 新对象从磁盘恢复，不靠原进程内去重集合。
    await request_close(rig, pid)
    assert hb_calls == 1
    assert rig.ex.market_closed == [] and rig.ex.close_submissions == {}
    assert rig.registry.get(pid).record.status is PositionStatus.CLOSING
    assert_still_protected(rig, pid, stops)


@pytest.mark.parametrize("side", ["long", "short"])
@pytest.mark.parametrize("method", ["entry_order", "positions_evidence"])
@pytest.mark.parametrize("fault", ["missing", "raises", "empty_fields"])
async def test_missing_query_capability_or_bad_evidence_never_settles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    side: str,
    method: str,
    fault: str,
) -> None:
    rig = Rig(tmp_path)
    pid = await open_filled(rig, side=side)
    stops = dict(rig.ex.stops)
    rig.ex.set_position("BTCUSDT", Side(side), 0)

    async def broken(*args: Any, **kwargs: Any) -> Any:
        if fault == "raises":
            raise ExchangeError("synthetic evidence unavailable")
        return {}  # 不是完整列表，也不是带订单身份与终态的订单对象。

    monkeypatch.setattr(rig.ex, method, None if fault == "missing" else broken)
    monkeypatch.setattr(rig.api, "stop_executor", unavailable_hb)
    await request_close(rig, pid)
    assert_still_protected(rig, pid, stops)
    assert rig.ex.market_closed == []


@pytest.mark.parametrize("side", ["long", "short"])
async def test_confirmed_zero_position_can_still_settle_and_release_protection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    side: str,
) -> None:
    rig = Rig(tmp_path)
    pid = await open_filled(rig, side=side)
    rig.api.terminate("ex1", "SYSTEM_CLEANUP", leaves_position=True)
    monkeypatch.setattr(rig.api, "stop_executor", unavailable_hb)
    await request_close(rig, pid)  # 原FakeExchange确实把净仓清零，作为正对照。
    assert rig.registry.get(pid).record.status is PositionStatus.CLOSED
    assert rig.ex.stops == {}


@pytest.mark.parametrize("side", ["long", "short"])
async def test_concurrent_direct_close_rechecks_pending_after_await(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    side: str,
) -> None:
    rig = Rig(tmp_path)
    pid = await open_filled(rig, side=side)
    stops = dict(rig.ex.stops)
    original_positions = rig.ex.positions
    entered = asyncio.Event()
    release = asyncio.Event()
    query_n = 0
    calls = 0

    async def slow_positions() -> Any:
        nonlocal query_n
        query_n += 1
        entered.set()
        await release.wait()
        return await original_positions()

    async def accepted(*args: Any, **kwargs: Any) -> CloseFill:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return CloseFill()  # ACK 后余仓仍在，另一入口不能据此再发一次。

    monkeypatch.setattr(rig.ex, "positions", slow_positions)
    monkeypatch.setattr(rig.ex, "close_position_market", accepted)
    first = asyncio.create_task(rig.adapter.request_direct_close(pid, CloseReason.KEEPER))
    await entered.wait()
    second = asyncio.create_task(rig.adapter.request_direct_close(pid, CloseReason.RECONCILER))
    await asyncio.sleep(0)  # 无锁实现进入相同 await；有锁实现会在锁外等待。
    release.set()
    await asyncio.gather(first, second)
    assert query_n >= 1
    assert calls == 1
    assert_still_protected(rig, pid, stops)


@pytest.mark.parametrize("side", ["long", "short"])
@pytest.mark.parametrize("cleanup_fault", ["hb_unknown", "lifeboat_cancel"])
async def test_unfinished_exit_cleanup_blocks_new_generation_across_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    side: str,
    cleanup_fault: str,
) -> None:
    rig = Rig(tmp_path)
    pid = await open_filled(rig, side=side)
    stop_actions: list[bool] = []

    async def stop(*args: Any, keep_position: bool, **kwargs: Any) -> None:
        stop_actions.append(keep_position)
        if keep_position:
            raise HbApiError("synthetic cleanup accepted-then-timeout", transient=True)
        raise HbApiError("synthetic close rejected", definite_rejection=True)

    monkeypatch.setattr(rig.api, "stop_executor", stop)
    if cleanup_fault == "lifeboat_cancel":
        rig.api.terminate("ex1", "SYSTEM_CLEANUP", leaves_position=True)
        rig.ex.fail_cancel = True
    await request_close(rig, pid)
    tracked = rig.registry.get(pid)
    assert tracked.record.status is PositionStatus.CLOSED and tracked.exit_cleanup_pending
    assert rig.ex.stops  # 仓已平不代表旧执行器/残保护单已清理。
    if cleanup_fault == "hb_unknown":
        rig.rec._close_unknown = True
        before_close_requests = stop_actions.count(False)
        await rig.rec.reconcile(rig.t)
        assert rig.ex.stops  # 对账的孤儿单清理也不能绕过尚未结束的退出清理。
        assert stop_actions.count(False) == before_close_requests
    calls_before = (len(rig.api.created), len(rig.ex.leverage_set))
    for restored in (False, True):
        if restored:
            rig.registry = PositionRegistry(tmp_path / "positions.json")
            rig.adapter = rig._adapter()
        with pytest.raises(SymbolAlreadyHeld):
            await rig.adapter.open(intent(intent_id="int-next", side=side))
        assert (len(rig.api.created), len(rig.ex.leverage_set)) == calls_before
    rig.ex.fail_cancel = False
    rig.api.terminate("ex1", "SYSTEM_CLEANUP", leaves_position=True)
    await rig.adapter.positions()
    assert not rig.registry.get(pid).exit_cleanup_pending and rig.ex.stops == {}
    new = await rig.adapter.open(intent(intent_id="int-next", side=side))
    assert new.position_id != pid and new.status is PositionStatus.OPEN
