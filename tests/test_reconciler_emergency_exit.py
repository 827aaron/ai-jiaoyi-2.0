"""对账员的无主仓 / 反手仓直连平仓：先落盘 CID，结果未知只查原 CID，按交易所复查判完成。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from jiaoyi.contracts import Side
from jiaoyi.contracts.position import PositionStatus, ReconcileIssueKind
from jiaoyi.execution.exchange import CloseFill, ExchangeError
from jiaoyi.execution.hb_executor import SymbolAlreadyHeld
from jiaoyi.execution.reconciler import ThreeWayReconciler
from jiaoyi.execution.registry import PositionRegistry
from tests.test_execution import Rig, intent, open_filled, with_close_unknown


def _restart(rig: Rig, tmp_path: Path, *, close_unknown: bool) -> None:
    """模拟进程重启：账本从文件重读，适配器和对账员都是新的。"""
    rig.registry = PositionRegistry(tmp_path / "positions.json")
    rig.adapter = rig._adapter()
    rig.rec = ThreeWayReconciler(
        rig.adapter,
        rig.api,
        rig.ex,
        rig.lifeboat,
        rig.registry,
        rig.alerts,
        account_name="acct",
        close_unknown_positions=close_unknown,
    )


def _ack_only(rig: Rig, monkeypatch: pytest.MonkeyPatch, *, timeout: bool) -> list[float]:
    """币安受理了（或超时后结果不明），但仓位没动。返回每次真正发单的数量。"""
    calls: list[float] = []

    async def accepted(symbol: str, close_side: Any, quantity: float) -> CloseFill:
        calls.append(quantity)
        if timeout:
            raise ExchangeError("合成：已受理但响应超时", transient=True)
        return CloseFill()

    monkeypatch.setattr(rig.ex, "close_position_market", accepted)
    return calls


async def _reversed_after_close(rig: Rig, side: Side) -> float:
    """我们的仓刚结束，币安上出现反向等量仓（反手仓触发条件）。"""
    pid = await open_filled(rig, side=side.value)
    rig.api.terminate("ex1", "STOP_LOSS", pnl=-5.0)
    await rig.adapter.positions()
    rec = rig.registry.get(pid).record  # type: ignore[union-attr]
    assert rec.status is PositionStatus.CLOSED
    rig.ex.set_position(rec.symbol, Side.SHORT if side is Side.LONG else Side.LONG, rec.quantity)
    return rec.quantity


# ---------------------------------------------------------------- 回报丢失 / 重试


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
@pytest.mark.parametrize("timeout", [False, True])
async def test_unknown_lost_response_never_submits_twice_even_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, side: Side, timeout: bool
) -> None:
    rig = Rig(tmp_path)
    rig.rec = with_close_unknown(rig)
    rig.ex.set_position("BTCUSDT", side, 0.015)
    foreign = await rig.ex.place_stop(
        "BTCUSDT", "sell" if side is Side.LONG else "buy", 0.015, 60000, "foreign-protection"
    )
    calls = _ack_only(rig, monkeypatch, timeout=timeout)
    reports = [await rig.rec.reconcile(rig.t), await rig.rec.reconcile(rig.t)]
    _restart(rig, tmp_path, close_unknown=True)
    reports.append(await rig.rec.reconcile(rig.t))
    assert calls == [0.015]
    assert len(rig.ex.close_submissions) == 1
    assert all(not i.fixed for r in reports for i in r.issues)
    assert "不重发" in reports[-1].issues[0].detail
    assert rig.ex.stops[foreign.order_id] == foreign and rig.ex.cancelled == []
    assert "BTCUSDT" in rig.registry.data.emergency_exits  # 意图跨重启保留


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_restart_mid_close_confirms_by_original_cid_and_flat_position(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, side: Side
) -> None:
    rig = Rig(tmp_path)
    rig.rec = with_close_unknown(rig)
    rig.ex.set_position("BTCUSDT", side, 0.015)
    calls = _ack_only(rig, monkeypatch, timeout=True)
    first = await rig.rec.reconcile(rig.t)
    assert not first.issues[0].fixed
    cid = rig.registry.data.emergency_exits["BTCUSDT"].client_order_id
    assert cid is not None and cid.startswith("dc-em-")
    _restart(rig, tmp_path, close_unknown=True)
    # 重启期间那张单其实成交了：交易所按原 CID 查得到终态，仓位为零
    rig.ex.close_orders[cid] = await _filled_order(rig, cid, side, 0.015)
    rig.ex.set_position("BTCUSDT", side, 0)
    report = await rig.rec.reconcile(rig.t)
    (issue,) = report.issues
    assert issue.kind is ReconcileIssueKind.ORPHAN_POSITION and issue.fixed
    assert calls == [0.015] and len(rig.ex.close_submissions) == 1
    assert rig.registry.data.emergency_exits == {}


async def _filled_order(rig: Rig, cid: str, side: Side, qty: float) -> Any:
    from jiaoyi.execution.exchange import IdentifiedCloseOrder

    return IdentifiedCloseOrder(
        symbol="BTCUSDT",
        client_order_id=cid,
        order_id="77",
        close_side="sell" if side is Side.LONG else "buy",
        quantity=qty,
        filled_quantity=qty,
        remaining_quantity=0.0,
        average_price=68000.0,
        status="FILLED",
        position_side="BOTH",
        reduce_only=True,
        is_terminal=True,
    )


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_ack_is_not_flat_ack_then_position_gone_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, side: Side
) -> None:
    rig = Rig(tmp_path)
    rig.rec = with_close_unknown(rig)
    rig.ex.set_position("BTCUSDT", side, 0.015)
    calls = _ack_only(rig, monkeypatch, timeout=False)
    assert not (await rig.rec.reconcile(rig.t)).issues[0].fixed
    cid = rig.registry.data.emergency_exits["BTCUSDT"].client_order_id
    assert cid is not None
    rig.ex.set_position("BTCUSDT", side, 0)
    # 仓位没了，但原 CID 查到的订单号和受理时的不一致：身份矛盾不算证据
    rig.ex.close_orders[cid] = replace(
        await _filled_order(rig, cid, side, 0.015),
        order_id="77",
    )
    report = await rig.rec.reconcile(rig.t)
    assert [i.fixed for i in report.issues] == [False]  # 订单号和受理时的不一致 → 未知
    rig.ex.close_orders[cid] = replace(
        await _filled_order(rig, cid, side, 0.015),
        order_id=rig.registry.data.emergency_exits["BTCUSDT"].order_id or "",
    )
    report = await rig.rec.reconcile(rig.t)  # 仓位已不在 → 由兜底循环核对原意图
    assert [i.fixed for i in report.issues] == [True]
    assert calls == [0.015] and rig.registry.data.emergency_exits == {}


async def test_ack_then_flat_by_someone_else_keeps_intent_until_order_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """单子只受理（NEW），仓位却被别人平了：原单没终态前不算完成，同币继续禁止新开。"""
    rig = Rig(tmp_path)
    rig.rec = with_close_unknown(rig)
    rig.ex.set_position("BTCUSDT", Side.LONG, 0.015)
    _ack_only(rig, monkeypatch, timeout=False)
    place = rig.ex.place_close

    async def ack_then_manual_flat(*args: Any) -> Any:
        order = await place(*args)
        rig.ex.set_position("BTCUSDT", Side.LONG, 0)
        return order

    monkeypatch.setattr(rig.ex, "place_close", ack_then_manual_flat)
    (issue,) = (await rig.rec.reconcile(rig.t)).issues
    assert not issue.fixed and "BTCUSDT" in rig.registry.data.emergency_exits
    (issue,) = (await rig.rec.reconcile(rig.t)).issues
    assert not issue.fixed and "BTCUSDT" in rig.registry.data.emergency_exits


# ---------------------------------------------------------------- 并发


async def test_concurrent_checks_racing_on_position_read_submit_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """两次检查都卡在下单前的复查持仓上：同币锁让第二次看到第一次落盘的意图。"""
    rig = Rig(tmp_path)
    rig.rec = with_close_unknown(rig)
    rig.ex.set_position("BTCUSDT", Side.LONG, 0.015)
    observed = await rig.ex.positions_evidence()
    calls = _ack_only(rig, monkeypatch, timeout=False)
    read = rig.ex.positions_evidence
    gate = asyncio.Event()

    async def slow_read() -> Any:
        await gate.wait()
        return await read()

    monkeypatch.setattr(rig.ex, "positions_evidence", slow_read)
    first = asyncio.create_task(rig.rec._unknown_position("BTCUSDT", observed))
    second = asyncio.create_task(rig.rec._unknown_position("BTCUSDT", observed))
    await asyncio.sleep(0)
    gate.set()
    await asyncio.gather(first, second)
    assert calls == [0.015] and len(rig.ex.close_submissions) == 1


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_concurrent_unknown_checks_submit_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, side: Side
) -> None:
    rig = Rig(tmp_path)
    rig.rec = with_close_unknown(rig)
    rig.ex.set_position("BTCUSDT", side, 0.015)
    observed = await rig.ex.positions_evidence()
    started, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def accepted(*args: Any, **kwargs: Any) -> CloseFill:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return CloseFill()

    monkeypatch.setattr(rig.ex, "close_position_market", accepted)
    first = asyncio.create_task(rig.rec._unknown_position("BTCUSDT", observed))
    await started.wait()
    second = asyncio.create_task(rig.rec._unknown_position("BTCUSDT", observed))
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(first, second)
    assert calls == 1
    assert all(not issue.fixed for issue in results)


async def test_concurrent_reconcile_passes_submit_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(tmp_path)
    await _reversed_after_close(rig, Side.LONG)
    rig.t += timedelta(minutes=3)
    other = ThreeWayReconciler(
        rig.adapter,
        rig.api,
        rig.ex,
        rig.lifeboat,
        rig.registry,
        rig.alerts,
        account_name="acct",
    )
    started, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def accepted(*args: Any, **kwargs: Any) -> CloseFill:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return CloseFill()

    monkeypatch.setattr(rig.ex, "close_position_market", accepted)
    first = asyncio.create_task(rig.rec.reconcile(rig.t))
    await started.wait()
    second = asyncio.create_task(other.reconcile(rig.t))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first, second)
    assert calls == 1 and len(rig.ex.close_submissions) == 1


# ---------------------------------------------------------------- 手动仓 / 归属不明不动


async def test_switch_off_manual_position_is_never_touched(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.ex.set_position("ETHUSDT", Side.LONG, 2.0)
    for _ in range(3):
        report = await rig.rec.reconcile(rig.t)
        assert report.issues[0].kind is ReconcileIssueKind.ORPHAN_POSITION
    assert rig.ex.close_submissions == {} and rig.ex.market_closed == []
    assert rig.registry.data.emergency_exits == {}


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_stale_unknown_classification_cannot_close_a_now_owned_position(
    tmp_path: Path, side: Side
) -> None:
    rig = Rig(tmp_path)
    rig.rec = with_close_unknown(rig)
    await open_filled(rig, side=side.value)
    observed = await rig.ex.positions_evidence()
    result = await rig.rec._unknown_position("BTCUSDT", observed)
    assert not result.fixed and "归属未明" in result.detail
    assert rig.ex.market_closed == [] and rig.ex.close_submissions == {}
    assert rig.ex.cancelled == []


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_unknown_with_foreign_executor_isolates_first_and_never_runs_two_channels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, side: Side
) -> None:
    rig = Rig(tmp_path)
    rig.rec = with_close_unknown(rig)
    created = await rig.api.create_executor(
        {"trading_pair": "BTC-USDT", "side": 1 if side is Side.LONG else 2, "amount": 0.015},
        "acct",
        "foreign-executor",
    )
    rig.api.fill(created["executor_id"], 68000)
    rig.api.auto_terminate_on_stop = False
    calls = _ack_only(rig, monkeypatch, timeout=False)
    await rig.rec.reconcile(rig.t)
    await rig.rec.reconcile(rig.t)
    assert calls == []
    assert rig.api.stopped == [(created["executor_id"], True)]  # 只停一次，且保留仓位


async def test_quantity_growth_after_partial_is_not_treated_as_remainder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(tmp_path)
    rig.rec = with_close_unknown(rig)
    rig.ex.set_position("BTCUSDT", Side.LONG, 0.015)
    calls: list[float] = []

    async def partial(symbol: str, close_side: Any, quantity: float) -> CloseFill:
        calls.append(quantity)
        rig.ex.set_position(symbol, Side.LONG, quantity / 2)
        return CloseFill(68000, None)

    monkeypatch.setattr(rig.ex, "close_position_market", partial)
    await rig.rec.reconcile(rig.t)
    cid, order = next(iter(rig.ex.close_orders.items()))
    rig.ex.close_orders[cid] = replace(order, status="EXPIRED", is_terminal=True)
    rig.ex.set_position("BTCUSDT", Side.LONG, 0.02)  # 有人手动加了仓
    report = await rig.rec.reconcile(rig.t)
    assert calls == [0.015]
    assert not report.issues[0].fixed and "超过本意图剩余上限" in report.issues[0].detail
    rig.ex.set_position("BTCUSDT", Side.SHORT, 0.005)  # 方向也变了
    report = await rig.rec.reconcile(rig.t)
    assert calls == [0.015] and "方向" in report.issues[0].detail


async def test_pending_exit_blocks_new_open_on_same_symbol(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(tmp_path)
    rig.rec = with_close_unknown(rig)
    rig.ex.set_position("BTCUSDT", Side.LONG, 0.015)
    _ack_only(rig, monkeypatch, timeout=True)
    await rig.rec.reconcile(rig.t)
    with pytest.raises(SymbolAlreadyHeld):
        await rig.adapter.open(intent())
    assert rig.api.created == []


# ---------------------------------------------------------------- 部分成交 / 正对照


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_partial_waits_terminal_then_only_closes_actual_remainder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, side: Side
) -> None:
    rig = Rig(tmp_path)
    rig.rec = with_close_unknown(rig)
    rig.ex.set_position("BTCUSDT", side, 0.015)
    actual = rig.ex.close_position_market
    calls: list[float] = []

    async def partial(symbol: str, close_side: Any, quantity: float) -> CloseFill:
        calls.append(quantity)
        if len(calls) == 1:
            rig.ex.set_position(symbol, side, quantity / 2)
            return CloseFill(68000, None)
        return await actual(symbol, close_side, quantity)

    monkeypatch.setattr(rig.ex, "close_position_market", partial)
    first = await rig.rec.reconcile(rig.t)
    assert not any(issue.fixed for issue in first.issues)
    await rig.rec.reconcile(rig.t)
    assert calls == [0.015]
    cid, order = next(iter(rig.ex.close_orders.items()))
    rig.ex.close_orders[cid] = replace(order, status="EXPIRED", is_terminal=True)
    last = await rig.rec.reconcile(rig.t)
    assert calls == [0.015, 0.0075]
    assert not await rig.ex.positions_evidence()
    assert any(issue.fixed for issue in last.issues)
    assert rig.registry.data.emergency_exits == {}


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_true_unmanaged_flat_positive_control(tmp_path: Path, side: Side) -> None:
    rig = Rig(tmp_path)
    rig.rec = with_close_unknown(rig)
    rig.ex.set_position("BTCUSDT", side, 0.015)
    report = await rig.rec.reconcile(rig.t)
    assert not await rig.ex.positions_evidence()
    assert [i.fixed for i in report.issues] == [True]
    assert len(rig.ex.close_submissions) == 1


# ---------------------------------------------------------------- 反手仓 / 方向不对


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_reverse_position_lost_response_never_resubmits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, side: Side
) -> None:
    rig = Rig(tmp_path)
    qty = await _reversed_after_close(rig, side)
    rig.t += timedelta(minutes=3)
    calls = _ack_only(rig, monkeypatch, timeout=True)
    reports = [await rig.rec.reconcile(rig.t), await rig.rec.reconcile(rig.t)]
    _restart(rig, tmp_path, close_unknown=False)
    rig.t += timedelta(minutes=20)  # 过了反手窗口也要继续核对原意图，不能退回“只告警”后遗忘
    reports.append(await rig.rec.reconcile(rig.t))
    assert calls == [qty]
    sm = [i for r in reports for i in r.issues if i.kind is ReconcileIssueKind.SIDE_MISMATCH]
    assert len(sm) == 3 and not any(i.fixed for i in sm)


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_opposite_side_open_ledger_lost_response_never_resubmits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, side: Side
) -> None:
    rig = Rig(tmp_path)
    pid = await open_filled(rig, side=side.value)
    qty = rig.registry.get(pid).record.quantity  # type: ignore[union-attr]
    rig.ex.stops.clear()
    rig.ex.set_position("BTCUSDT", Side.SHORT if side is Side.LONG else Side.LONG, qty)
    calls = _ack_only(rig, monkeypatch, timeout=False)
    first = await rig.rec.reconcile(rig.t)
    second = await rig.rec.reconcile(rig.t)
    assert calls == [qty]
    for report in (first, second):
        (issue,) = [i for i in report.issues if i.kind is ReconcileIssueKind.SIDE_MISMATCH]
        assert not issue.fixed and issue.position_id == pid
    # 平仓单成交、仓位归零后才算处理完
    cid = rig.registry.data.emergency_exits["BTCUSDT"].client_order_id
    assert cid is not None
    order = rig.ex.close_orders[cid]
    rig.ex.close_orders[cid] = replace(
        order,
        status="FILLED",
        filled_quantity=qty,
        remaining_quantity=0.0,
        is_terminal=True,
    )
    rig.ex.set_position("BTCUSDT", Side.LONG, 0)
    third = await rig.rec.reconcile(rig.t)
    assert any(i.kind is ReconcileIssueKind.SIDE_MISMATCH and i.fixed for i in third.issues)
    assert calls == [qty] and rig.registry.data.emergency_exits == {}


async def test_opposite_side_close_waits_while_ledger_owner_changed(tmp_path: Path) -> None:
    """触发后账本那笔已经结束（不再是开着的反向仓）：不凭旧判断下单。"""
    rig = Rig(tmp_path)
    pid = await open_filled(rig)
    qty = rig.registry.get(pid).record.quantity  # type: ignore[union-attr]
    rig.ex.set_position("BTCUSDT", Side.SHORT, qty)
    tracked = rig.registry.get(pid)
    assert tracked is not None
    stale = tracked.record
    rig.registry.put(
        tracked.model_copy(
            update={"record": stale.model_copy(update={"status": PositionStatus.CLOSED})}
        )
    )
    issue = await rig.rec._side_mismatch(stale, await rig.ex.positions_evidence())
    assert not issue.fixed and rig.ex.close_submissions == {}
