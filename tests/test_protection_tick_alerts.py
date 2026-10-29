"""托管保护后台轮的告警分级、程序自己平仓时记下的具体原因；不连接任何服务。"""

from __future__ import annotations

from pathlib import Path

import pytest

from jiaoyi.acceptance import read_records
from jiaoyi.contracts import Outcome, Role, Side
from jiaoyi.contracts.module_status import AlertLevel
from jiaoyi.contracts.position import CloseReason, PositionStatus
from jiaoyi.core.recorder import RoleRecorder
from jiaoyi.core.store import JsonlDecisionLog
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
