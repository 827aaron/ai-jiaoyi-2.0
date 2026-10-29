"""托管保护后台轮的告警分级、程序自己平仓时记下的具体原因；不连接任何服务。"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from jiaoyi.acceptance import read_records
from jiaoyi.contracts import Outcome, Role, Side
from jiaoyi.contracts.module_status import AlertLevel
from jiaoyi.contracts.position import CloseReason, PositionStatus
from jiaoyi.core.recorder import RoleRecorder
from jiaoyi.core.store import JsonlDecisionLog
from jiaoyi.execution import shared_reads
from jiaoyi.execution.hb_client import HbApiError
from jiaoyi.execution.trailing import TrailingBookUnavailable
from tests.test_server_protection_regression import AdapterHarness


def tick_alerts(h: AdapterHarness) -> list:
    return [a for a in h.alerter.sent if a.title == "托管保护本轮未确认"]


def broken_hb(h: AdapterHarness, monkeypatch: pytest.MonkeyPatch, broken: list[bool]) -> None:
    original = h.api.get_executor

    async def get_executor(executor_id):
        if broken[0]:
            try:
                raise ValueError("synthetic connection reset")
            except ValueError:
                raise HbApiError("")  # noqa: B904  没文字、也没 from：只有 __context__
        return await original(executor_id)

    monkeypatch.setattr(h.api, "get_executor", get_executor)


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_default_alerts_on_every_failed_tick_with_the_underlying_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, side: Side
):
    h = AdapterHarness(tmp_path, side)
    await h.protect()
    broken_hb(h, monkeypatch, [True])
    await h.adapter.protection_tick()
    [alert] = tick_alerts(h)
    assert alert.level is AlertLevel.CRITICAL
    assert "HbApiError <- ValueError: synthetic connection reset" in alert.body


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_alert_waits_for_consecutive_failures_and_success_resets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, side: Side
):
    h = AdapterHarness(tmp_path, side)
    h.adapter._tick_alert_after = 3
    await h.protect()

    async def no_trailing(pid):
        return None  # 合成交易所没有移动止损要的成交明细；这里只看同步本身

    monkeypatch.setattr(h.adapter, "_update_server_trailing", no_trailing)
    broken = [True]
    broken_hb(h, monkeypatch, broken)
    for _ in range(2):
        await h.adapter.protection_tick()
    assert not tick_alerts(h)
    broken[0] = False
    await h.adapter.protection_tick()
    broken[0] = True
    for _ in range(2):
        await h.adapter.protection_tick()
    assert not tick_alerts(h)  # 中间成功过一轮，重新计数
    await h.adapter.protection_tick()
    [alert] = tick_alerts(h)
    assert alert.level is AlertLevel.CRITICAL and "连续 3 轮" in alert.body


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_non_retryable_protection_failure_close_records_the_cause(tmp_path: Path, side: Side):
    h = AdapterHarness(tmp_path, side)
    h.exchange.reject_kind = "stop_loss"  # 保护腿被明确拒绝：不属于未托管宽限
    for _ in range(2):
        await h.adapter.protection_tick()
    done = h.registry.get("pos-1")
    assert done.record.status is PositionStatus.CLOSED
    assert done.record.close_reason is CloseReason.RECONCILER
    assert done.close_detail is not None
    assert done.close_detail.startswith(
        "托管保护未完整确认，不在未托管宽限内：ProtectionIncomplete"
    )
    [notice] = [a for a in h.alerter.sent if a.dedup_key == "closed:pos-1"]
    assert f"原因：{done.close_detail}" in notice.body
    assert "close_detail" not in h.path.read_text()  # 结束后账本与旧版一致


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_book_unavailable_round_counts_as_synced_and_resets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, side: Side
):
    h = AdapterHarness(tmp_path, side)
    h.adapter._tick_alert_after = 3
    await h.protect()

    async def no_book(pid):
        raise TrailingBookUnavailable("synthetic stale book")

    monkeypatch.setattr(h.adapter, "_update_server_trailing", no_book)
    broken = [True]
    broken_hb(h, monkeypatch, broken)
    for _ in range(2):
        await h.adapter.protection_tick()
    broken[0] = False
    await h.adapter.protection_tick()  # 同步成功，只是盘口不可用
    broken[0] = True
    for _ in range(2):
        await h.adapter.protection_tick()
    assert not tick_alerts(h)


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_failure_counter_is_dropped_once_the_position_ends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, side: Side
):
    h = AdapterHarness(tmp_path, side)
    h.adapter._tick_alert_after = 3
    await h.protect()
    broken_hb(h, monkeypatch, [True])
    await h.adapter.protection_tick()
    assert h.adapter._tick_failures == {("pos-1", "sync"): 1}
    tracked = h.registry.get("pos-1")
    h.registry.put(
        tracked.model_copy(
            update={"record": tracked.record.model_copy(update={"status": PositionStatus.FAILED})}
        )
    )
    await h.adapter.protection_tick()
    assert h.adapter._tick_failures == {}


def recorded(h: AdapterHarness, tmp_path: Path) -> JsonlDecisionLog:
    log = JsonlDecisionLog(tmp_path / "data")
    h.adapter._rec = RoleRecorder(Role.EXECUTOR, log)
    return log


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_close_record_carries_the_cause_in_summary_and_output(tmp_path: Path, side: Side):
    h = AdapterHarness(tmp_path, side)
    recorded(h, tmp_path)
    h.exchange.reject_kind = "stop_loss"
    for _ in range(2):
        await h.adapter.protection_tick()
    detail = h.registry.get("pos-1").close_detail
    [close] = [r for r in read_records(tmp_path / "data") if r.outcome is Outcome.CLOSE]
    assert close.output["detail"] == detail
    assert close.summary.startswith(f"BTCUSDT 直连平仓（reconciler：{detail}），净盈亏")


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_only_the_first_cause_is_kept(tmp_path: Path, side: Side):
    h = AdapterHarness(tmp_path, side)
    tracked = h.registry.get("pos-1")
    h.registry.put(tracked.model_copy(update={"close_detail": "synthetic first cause"}))
    h.exchange.reject_kind = "stop_loss"
    for _ in range(2):
        await h.adapter.protection_tick()
    done = h.registry.get("pos-1")
    assert done.record.status is PositionStatus.CLOSED
    assert done.close_detail == "synthetic first cause"


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_cause_is_persisted_while_closing_and_survives_restart(tmp_path: Path, side: Side):
    h = AdapterHarness(tmp_path, side)
    h.api.auto_terminate_on_stop = False  # HB 还没结束：平仓停在半路
    h.exchange.reject_kind = "stop_loss"
    for _ in range(2):
        await h.adapter.protection_tick()
    closing = h.registry.get("pos-1")
    assert closing.record.status is PositionStatus.CLOSING
    assert closing.close_detail is not None and "close_detail" in h.path.read_text()
    h.restart_adapter()
    assert h.registry.get("pos-1").close_detail == closing.close_detail
    h.api.terminate("owned-executor", "POSITION_HOLD", leaves_position=True)
    await h.adapter.protection_tick()
    done = h.registry.get("pos-1")
    assert done.record.status is PositionStatus.CLOSED
    [notice] = [a for a in h.alerter.sent if a.dedup_key == "closed:pos-1"]
    assert f"原因：{closing.close_detail}" in notice.body


def three_positions(
    h: AdapterHarness, monkeypatch: pytest.MonkeyPatch, slow: str | None = None
) -> tuple[list[int], list[str]]:
    """pos-1..3 三个币；同步换成「读持仓、读三条腿、再读持仓」，``slow`` 那笔在锁里卡 30 秒。"""
    base = h.registry.get("pos-1")
    assert base is not None
    for n, symbol in ((2, "ETHUSDT"), (3, "SOLUSDT")):
        pid, eid = f"pos-{n}", f"ex-{n}"
        rec = base.record.model_copy(
            update={"position_id": pid, "symbol": symbol, "executor_id": eid}
        )
        h.registry.put(base.model_copy(update={"record": rec, "entry_client_order_id": f"e-{n}"}))
        h.api.executors[eid] = {
            **h.api.executors["owned-executor"],
            "executor_id": eid,
            "controller_id": f"jy-{pid}",
            "custom_info": {"current_position_average_price": 100, "order_ids": [f"e-{n}"]},
        }
    reads = [0]
    synced: list[str] = []

    async def positions_evidence():
        reads[0] += 1
        await asyncio.sleep(0.05)
        return []

    async def sync_server(tracked, detail):
        pid = tracked.record.position_id
        read = shared_reads.current() or positions_evidence
        await read()
        await asyncio.sleep(30 if pid == slow else 0.09)
        await read()
        synced.append(pid)
        return h.registry.get(pid) or tracked

    async def no_trailing(pid):
        return None

    monkeypatch.setattr(h.exchange, "positions_evidence", positions_evidence)
    monkeypatch.setattr(h.adapter, "_sync_server", sync_server)
    monkeypatch.setattr(h.adapter, "_update_server_trailing", no_trailing)
    return reads, synced


async def test_one_round_shares_the_positions_read_across_positions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    h = AdapterHarness(tmp_path, Side.LONG)
    reads, synced = three_positions(h, monkeypatch)
    await asyncio.wait_for(h.adapter.protection_tick(), 5)
    assert sorted(synced) == ["pos-1", "pos-2", "pos-3"]
    assert reads[0] == 2  # 同步前一次、改单后核对一次，三笔仓共用


async def test_a_slow_position_does_not_starve_the_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    h = AdapterHarness(tmp_path, Side.LONG)
    h.adapter._round_budget_s = 2.0
    _, synced = three_positions(h, monkeypatch, slow="pos-1")
    try:
        await asyncio.wait_for(h.adapter.protection_tick(), 4)
        assert sorted(synced) == ["pos-2", "pos-3"]
        assert h.adapter._exit_busy("pos-1")
        started = time.perf_counter()
        await asyncio.wait_for(h.adapter.protection_tick(), 1)
        assert time.perf_counter() - started < 0.5
        assert sorted(synced) == ["pos-2", "pos-2", "pos-3", "pos-3"]
        started = time.perf_counter()
        await asyncio.wait_for(h.adapter.positions(), 2)
        assert time.perf_counter() - started < 1.0
        assert "pos-1" not in synced
    finally:
        for task in list(h.adapter._tick_tasks.values()):
            task.cancel()


async def test_more_positions_space_the_rounds_further_apart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    h = AdapterHarness(tmp_path, Side.LONG)
    h.adapter._gap_per_position_s = 0.2
    _, synced = three_positions(h, monkeypatch)
    await h.adapter.protection_tick()
    await h.adapter.protection_tick()  # 三笔仓：本轮结束后歇 0.6 秒
    assert len(synced) == 3
    await asyncio.sleep(0.7)
    await h.adapter.protection_tick()
    assert len(synced) == 6
