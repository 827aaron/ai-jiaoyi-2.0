"""对账员的无主仓 / 反手仓直连平仓：先落盘 CID，结果未知只查原 CID，按交易所复查判完成。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from jiaoyi.contracts import Side
from jiaoyi.contracts.position import PositionStatus, ReconcileIssueKind
from jiaoyi.core.store import JsonlDecisionLog
from jiaoyi.execution.exchange import CloseFill, ExchangeError
from jiaoyi.execution.exit_resolution import (
    prepare_resolution,
    read_result,
    resolutions_dir,
    write_resolution,
)
from jiaoyi.execution.fills import TradeFill
from jiaoyi.execution.hb_client import HbApiError
from jiaoyi.execution.hb_executor import SymbolAlreadyHeld
from jiaoyi.execution.reconciler import ThreeWayReconciler
from jiaoyi.execution.registry import ForeignExecutorStop, PositionRegistry
from tests.test_execution import Rig, explain_reverse, intent, open_filled, with_close_unknown


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


async def _reversed_after_close(rig: Rig, side: Side, *, evidence: bool = True) -> float:
    """我们的仓刚结束，币安上出现反向等量仓（反手仓触发条件）。"""
    pid = await open_filled(rig, side=side.value)
    if evidence:
        explain_reverse(rig, pid)
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
    explain_reverse(rig, pid)
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
    explain_reverse(rig, pid)
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


# ---------------------------------------------------------------- 反手仓要有归属证据


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_reverse_without_our_fills_is_left_alone(tmp_path: Path, side: Side) -> None:
    """窗口内出现反向等量仓，但成交明细里没有能解释它的我方订单：可能是手动开的，不动。"""
    rig = Rig(tmp_path)
    await _reversed_after_close(rig, side, evidence=False)
    rig.t += timedelta(minutes=3)
    for _ in range(2):
        (issue,) = (await rig.rec.reconcile(rig.t)).issues
        assert issue.kind is ReconcileIssueKind.SIDE_MISMATCH and not issue.fixed
        assert "不能证明" in issue.detail
    assert rig.ex.close_submissions == {} and rig.ex.market_closed == []
    assert rig.registry.data.emergency_exits == {}


async def test_manual_fill_mixed_in_is_not_ownership_evidence(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    await _reversed_after_close(rig, Side.LONG)
    # 有人手动买了又卖（净量为零），但这两笔不是我方订单：整段成交就不能当证据
    for side in ("buy", "sell"):
        rig.ex.trades.append(TradeFill(rig.t, "999", side, 68000.0, 0.001, None, 0.0))
    rig.t += timedelta(minutes=3)
    (issue,) = (await rig.rec.reconcile(rig.t)).issues
    assert not issue.fixed and "不是认得出的我方订单" in issue.detail
    assert rig.ex.close_submissions == {}


async def test_our_fills_that_do_not_add_up_are_not_evidence(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    await _reversed_after_close(rig, Side.LONG)
    rig.ex.trades.pop()  # 执行器多成交的那一笔不在：我方成交解释不了反手仓
    rig.t += timedelta(minutes=3)
    (issue,) = (await rig.rec.reconcile(rig.t)).issues
    assert not issue.fixed and "解释不了" in issue.detail
    assert rig.ex.close_submissions == {}


async def test_evidence_lookup_failure_is_not_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(tmp_path)
    await _reversed_after_close(rig, Side.LONG)

    async def down(symbol: str, since: Any) -> Any:
        raise ExchangeError("合成：成交明细不可用")

    monkeypatch.setattr(rig.ex, "user_trades", down)
    rig.t += timedelta(minutes=3)
    (issue,) = (await rig.rec.reconcile(rig.t)).issues
    assert not issue.fixed and rig.ex.close_submissions == {}


async def test_executor_order_list_missing_is_not_evidence(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    await _reversed_after_close(rig, Side.LONG)
    rig.api.executors["ex1"]["custom_info"].pop("order_ids")
    rig.t += timedelta(minutes=3)
    (issue,) = (await rig.rec.reconcile(rig.t)).issues
    assert not issue.fixed and rig.ex.close_submissions == {}
    assert "身份查不到" in issue.detail


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_opposite_side_open_ledger_without_evidence_is_left_alone(
    tmp_path: Path, side: Side
) -> None:
    """账本开着多单，币安却是等量空单，但我方成交解释不了（例如人手动平了又反开）：不动。"""
    rig = Rig(tmp_path)
    pid = await open_filled(rig, side=side.value)
    qty = rig.registry.get(pid).record.quantity  # type: ignore[union-attr]
    rig.ex.set_position("BTCUSDT", Side.SHORT if side is Side.LONG else Side.LONG, qty)
    report = await rig.rec.reconcile(rig.t)
    (issue,) = [i for i in report.issues if i.kind is ReconcileIssueKind.SIDE_MISMATCH]
    assert not issue.fixed and "不能证明" in issue.detail
    assert rig.ex.close_submissions == {} and rig.ex.market_closed == []


# ---------------------------------------------------------------- 人工结束意图（resolve-exit）


async def _stuck_intent(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> str:
    """无主仓平仓单超时、原 CID 一直查不到：意图卡住。返回它的 CID。"""
    rig.rec = with_close_unknown(rig)
    rig.ex.set_position("BTCUSDT", Side.LONG, 0.015)
    _ack_only(rig, monkeypatch, timeout=True)
    await rig.rec.reconcile(rig.t)
    cid = rig.registry.data.emergency_exits["BTCUSDT"].client_order_id
    assert cid is not None
    return cid


async def _request(rig: Rig, cid: str, *, force: bool = False) -> tuple[Any, str]:
    return await prepare_resolution(
        rig.registry.path,
        rig.ex.positions_evidence,
        "BTCUSDT",
        cid,
        force=force,
        reason="已在币安核实",
        now=rig.t,
    )


async def test_resolve_exit_command_side_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(tmp_path)
    cid = await _stuck_intent(rig, monkeypatch)
    req, text = await _request(rig, cid + "x")
    assert req is None and "CID 对不上" in text
    req, text = await _request(rig, cid)  # 币安仍有仓
    assert req is None and "--force" in text
    rig.ex.down = True
    req, text = await _request(rig, cid)  # 读不到严格持仓
    assert req is None and "--force" in text
    req, _ = await _request(rig, cid, force=True)
    assert req is not None and req.force and req.observed_quantity is None
    rig.ex.down = False
    rig.ex.set_position("BTCUSDT", Side.LONG, 0)
    req, _ = await _request(rig, cid)
    assert req is not None and not req.force and req.observed_quantity == 0


async def test_reconciler_applies_resolution_once_and_rechecks_under_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(tmp_path)
    cid = await _stuck_intent(rig, monkeypatch)
    root = resolutions_dir(rig.registry.path)
    rig.ex.set_position("BTCUSDT", Side.LONG, 0)
    req, _ = await _request(rig, cid)
    assert req is not None
    rig.ex.set_position("BTCUSDT", Side.LONG, 0.015)  # 命令之后仓位又出现：主循环复核时拒绝
    write_resolution(root, req)
    await rig.rec.reconcile(rig.t)
    result = read_result(root, req.id)
    assert result is not None and not result.applied and "仍有净仓" in result.detail
    assert "BTCUSDT" in rig.registry.data.emergency_exits

    rig.ex.set_position("BTCUSDT", Side.LONG, 0)
    req, _ = await _request(rig, cid)
    assert req is not None
    write_resolution(root, req)
    await rig.rec.reconcile(rig.t)
    result = read_result(root, req.id)
    assert result is not None and result.applied and result.removed is not None
    assert result.removed["client_order_id"] == cid
    assert rig.registry.data.emergency_exits == {}
    assert len(rig.ex.close_submissions) == 1
    assert any("人工结束" in a.title for a in rig.alerts.sent)
    # 同一个 id 再来一次不执行（防重放）
    write_resolution(root, req)
    await rig.rec.reconcile(rig.t)
    assert not list(root.glob("*.json"))
    kept = read_result(root, req.id)
    assert kept is not None and kept.applied  # 原结果不被覆盖


async def test_resolution_with_stale_cid_is_refused_by_reconciler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(tmp_path)
    cid = await _stuck_intent(rig, monkeypatch)
    req, _ = await _request(rig, cid, force=True)
    assert req is not None
    plan = rig.registry.data.emergency_exits["BTCUSDT"]
    rig.registry.data.emergency_exits["BTCUSDT"] = plan.model_copy(
        update={"client_order_id": cid[:-1] + "2", "generation": 2}
    )  # 命令写下之后意图已换了一代
    root = resolutions_dir(rig.registry.path)
    write_resolution(root, req)
    await rig.rec.reconcile(rig.t)
    result = read_result(root, req.id)
    assert result is not None and not result.applied and "CID 对不上" in result.detail
    assert "BTCUSDT" in rig.registry.data.emergency_exits


async def test_resolve_exit_cli_writes_request_and_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import jiaoyi.cli as cli
    from tests.test_app import APP

    rig = Rig(tmp_path)
    cid = await _stuck_intent(rig, monkeypatch)
    app_toml = tmp_path / "app.toml"
    app_toml.write_text(
        APP.read_text(encoding="utf-8").replace('data_dir = "data"', f'data_dir = "{tmp_path}"'),
        encoding="utf-8",
    )

    class _NoClose:
        positions_evidence = rig.ex.positions_evidence

        async def close(self) -> None:
            return None

    monkeypatch.setattr(cli, "_strict_exchange", lambda _cfg: _NoClose())
    args = ["resolve-exit", "--app", str(app_toml), "--symbol", "BTCUSDT", "--wait", "0"]
    # 仍有仓、没 --force：拒绝，不写请求
    assert await asyncio.to_thread(cli.main, [*args, "--cid", cid]) == 2
    assert "--force" in capsys.readouterr().err
    root = resolutions_dir(rig.registry.path)
    assert not list(root.glob("*.json"))
    assert await asyncio.to_thread(cli.main, [*args, "--cid", cid, "--force"]) == 0
    out = capsys.readouterr().out
    assert "已提交" in out and cid in out
    (path,) = list(root.glob("*.json"))
    days = {rig.t.date().isoformat(), datetime.now(UTC).date().isoformat()}
    records = [r for d in days for r in JsonlDecisionLog(tmp_path).read_day(d)]
    assert any("人工请求结束平仓意图" in r.summary and r.symbol == "BTCUSDT" for r in records)
    # 主循环下一轮执行（--force：不再看净仓）。开关关掉，免得余仓又被当无主仓平
    rig.rec = _rec_with(rig)
    await rig.rec.reconcile(rig.t)
    assert rig.registry.data.emergency_exits == {} and not path.exists()
    result = read_result(root, path.stem)
    assert result is not None and result.applied and "--force" in result.detail


# -------------------------------------------- 停执行器：结果不明后的重试 / 已结束仓的执行器


def _rec_with(rig: Rig, **kw: Any) -> ThreeWayReconciler:
    return ThreeWayReconciler(
        rig.adapter,
        rig.api,
        rig.ex,
        rig.lifeboat,
        rig.registry,
        rig.alerts,
        account_name="acct",
        **kw,
    )


def _flaky_stop(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, bool]]:
    """第一次停止请求超时（结果不明），之后受理，但执行器一直不结束。"""
    calls: list[tuple[str, bool]] = []

    async def stop(executor_id: str, *, keep_position: bool) -> None:
        calls.append((executor_id, keep_position))
        if len(calls) == 1:
            raise HbApiError("合成：停止超时", transient=True)

    monkeypatch.setattr(rig.api, "stop_executor", stop)
    return calls


@pytest.mark.parametrize(("rounds", "expected"), [(0, 1), (2, 2)])
async def test_unclear_executor_stop_retry_follows_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rounds: int, expected: int
) -> None:
    rig = Rig(tmp_path)
    await rig.api.create_executor({"trading_pair": "SOL-USDT"}, "acct", "someone-else")
    calls = _flaky_stop(rig, monkeypatch)
    rec = _rec_with(rig, close_unknown_positions=True, executor_stop_retry_rounds=rounds)
    for _ in range(6):
        await rec.reconcile(rig.t)
    assert len(calls) == expected  # 0 = 永不重发；2 = 不明后满 2 轮再发一次，受理后不再发
    assert all(keep for _, keep in calls)


async def test_retry_counter_survives_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(tmp_path)
    await rig.api.create_executor({"trading_pair": "SOL-USDT"}, "acct", "someone-else")
    calls = _flaky_stop(rig, monkeypatch)
    for _ in range(2):
        await _rec_with(rig, close_unknown_positions=True, executor_stop_retry_rounds=3).reconcile(
            rig.t
        )
    _restart(rig, tmp_path, close_unknown=True)
    rec = _rec_with(rig, close_unknown_positions=True, executor_stop_retry_rounds=3)
    await rec.reconcile(rig.t)
    assert len(calls) == 1
    await rec.reconcile(rig.t)
    assert len(calls) == 2 and calls[-1][1] is True


async def _closed_with_running_executor(rig: Rig) -> str:
    """账本已结束，但它的执行器还在运行列表里（比如 SHUTTING_DOWN 慢）。"""
    pid = await open_filled(rig)
    tracked = rig.registry.get(pid)
    assert tracked is not None
    rig.registry.put(
        tracked.model_copy(
            update={
                "record": tracked.record.model_copy(
                    update={"status": PositionStatus.CLOSED, "closed_at": rig.t}
                )
            }
        )
    )
    rig.api.auto_terminate_on_stop = False
    return pid


async def test_own_closed_executor_is_isolated_even_with_switch_off(tmp_path: Path) -> None:
    rig = Rig(tmp_path)  # 开关默认关
    pid = await _closed_with_running_executor(rig)
    rig.ex.set_position("BTCUSDT", Side.LONG, 0)
    first = await rig.rec.reconcile(rig.t)
    await rig.rec.reconcile(rig.t)
    assert rig.api.stopped == [("ex1", True)]  # 只停一次，不平
    assert rig.registry.data.foreign_executor_stops["ex1"].position_id == pid
    assert any(i.position_id == pid and "还在运行列表里" in i.detail for i in first.issues)
    rig.api.terminate("ex1", "EARLY_STOP", leaves_position=True)
    await rig.rec.reconcile(rig.t)
    assert rig.registry.data.foreign_executor_stops == {}
    assert rig.ex.market_closed == []


async def test_reverse_close_waits_until_own_closed_executor_is_isolated(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path)
    pid = await _closed_with_running_executor(rig)
    explain_reverse(rig, pid)
    rig.ex.set_position("BTCUSDT", Side.SHORT, 0.015)
    rig.t += timedelta(minutes=1)
    first = await rig.rec.reconcile(rig.t)
    assert rig.ex.close_submissions == {}  # 还有执行器在跑：先隔离，不并行直连
    assert any("先隔离" in i.detail for i in first.issues)
    assert rig.api.stopped == [("ex1", True)]
    rig.api.terminate("ex1", "EARLY_STOP", leaves_position=True)
    second = await rig.rec.reconcile(rig.t)
    assert len(rig.ex.close_submissions) == 1
    assert any(i.kind is ReconcileIssueKind.SIDE_MISMATCH and i.fixed for i in second.issues)


# ---------------------------------------------------------------- 旧账本


async def test_old_positions_json_without_any_new_field_still_loads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """托管仓（腿的 working_type / acked_at、prearm_failing_since）加上对账员意图与执行器停止记录
    全写进账本；去掉这些后来才加的字段，旧文件照样能读，默认值正确；全字段来回一致。"""
    import json

    from tests.test_server_protection_regression import Harness

    h = Harness(tmp_path, Side.LONG)
    assert await h.build().sync("pos-1") == "armed"
    rig = Rig(tmp_path / "rig")
    await _stuck_intent(rig, monkeypatch)
    await rig.api.create_executor({"trading_pair": "SOL-USDT"}, "acct", "someone-else")
    calls = _flaky_stop(rig, monkeypatch)
    await _rec_with(rig, close_unknown_positions=True).reconcile(rig.t)
    assert calls and rig.registry.data.foreign_executor_stops
    data = json.loads(h.path.read_text(encoding="utf-8"))
    extra = json.loads(rig.registry.path.read_text(encoding="utf-8"))
    data["emergency_exits"] = extra["emergency_exits"]
    data["foreign_executor_stops"] = extra["foreign_executor_stops"]
    full = tmp_path / "full" / "positions.json"
    full.parent.mkdir()
    full.write_text(json.dumps(data), encoding="utf-8")
    loaded = PositionRegistry(full)
    loaded.save()
    assert json.loads(full.read_text(encoding="utf-8")) == json.loads(loaded.data.model_dump_json())
    assert loaded.data.emergency_exits.keys() == {"BTCUSDT"}

    del data["emergency_exits"], data["foreign_executor_stops"]
    sp = data["positions"]["pos-1"]["server_protection"]
    sp.pop("prearm_failing_since", None)
    for leg in sp["legs"]:
        leg.pop("working_type", None)
        leg.pop("acked_at", None)
    old = tmp_path / "old" / "positions.json"
    old.parent.mkdir()
    old.write_text(json.dumps(data), encoding="utf-8")
    legacy = PositionRegistry(old)
    assert legacy.data.emergency_exits == {} and legacy.data.foreign_executor_stops == {}
    state = legacy.get("pos-1").server_protection  # type: ignore[union-attr]
    assert state is not None and state.prearm_failing_since is None
    assert {leg.purpose: leg.working_type for leg in state.legs} == {
        "lifeboat": "MARK_PRICE",
        "fixed": "CONTRACT_PRICE",
    }
    stop = extra["foreign_executor_stops"]["ex1"]
    del stop["position_id"], stop["unresolved_rounds"]
    legacy_stop = ForeignExecutorStop.model_validate(stop)
    assert legacy_stop.position_id is None and legacy_stop.unresolved_rounds == 0
