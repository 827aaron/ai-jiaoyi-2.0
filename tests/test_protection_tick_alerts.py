"""托管保护后台轮的告警分级、程序自己平仓时记下的具体原因；不连接任何服务。"""

from __future__ import annotations

from pathlib import Path

import pytest

from jiaoyi.contracts import Side
from jiaoyi.contracts.module_status import AlertLevel
from jiaoyi.contracts.position import CloseReason, PositionStatus
from jiaoyi.execution.hb_client import HbApiError
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
    h.exchange.reject_kind = "take_profit"  # 止盈被明确拒绝：不属于未托管宽限
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
