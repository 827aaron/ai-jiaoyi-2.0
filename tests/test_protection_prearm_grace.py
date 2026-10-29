"""从未完整托管时同步失败的宽限：只宽限读延迟 / 网络一时失败，且须仍有已确认保命或止损。

只有合成订单，不连接任何服务。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from jiaoyi.contracts import Side
from jiaoyi.contracts.position import PositionStatus
from jiaoyi.execution.brackets import BracketManager, ProtectionIncomplete
from jiaoyi.execution.exchange import ExchangeError
from tests.test_execution import START
from tests.test_protection_readback_grace import UNREADABLE, GraceHarness

TITLE = "普通托管保护未完整确认"
CLOSE_BODY = f"BTCUSDT pos-1：{UNREADABLE}；保留已确认保护，停止入场并核验退出"
LIVE = {"state": "confirmed", "algo_id": "9", "algo_status": "NEW", "child_state": "absent"}


class PrearmHarness(GraceHarness):
    """回读宽限关掉（0），只测未托管宽限；lb 回执后一直查无，直到测试自己放开。"""

    def __init__(self, root: Path, side: Side, prearm: float) -> None:
        self.prearm = prearm
        super().__init__(root, side, grace=0.0, prefix="lb-", visible_after=10**6)

    def build(self) -> BracketManager:
        manager = super().build()
        manager.prearm_grace_s = self.prearm
        return manager

    def alerts(self) -> list[str]:
        return [a.body for a in self.alerter.sent if a.title == TITLE]


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_within_grace_keeps_position_and_next_tick_arms(tmp_path: Path, side: Side):
    h = PrearmHarness(tmp_path, side, prearm=60.0)
    await h.adapter.protection_tick()
    assert not h.closing() and not h.exchange.direct
    assert h.registry.get("pos-1").record.status is PositionStatus.OPEN
    assert h.state().prearm_failing_since == START
    (body,) = h.alerts()
    assert body.startswith(f"BTCUSDT pos-1：{UNREADABLE}；") and "宽限内" in body
    assert "下一轮重试" in body and "已 0 / 60 秒" in body
    h.now += timedelta(seconds=30)
    await h.adapter.protection_tick()
    assert len(h.alerts()) == 2 and "已 30 / 60 秒" in h.alerts()[1]
    h.exchange.hidden.clear()  # 交易所读延迟结束。
    h.now += timedelta(seconds=10)
    await h.adapter.protection_tick()
    assert sorted(c[:3] for c in h.creates()) == ["br-", "lb-"]  # 没有重复提交。
    state = h.state()
    assert state.stage == "armed" and state.prearm_failing_since is None
    assert not h.closing() and len(h.alerts()) == 2


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_exceeding_grace_closes_exactly_as_before_even_after_restart(
    tmp_path: Path, side: Side
):
    h = PrearmHarness(tmp_path, side, prearm=60.0)
    await h.adapter.protection_tick()
    assert not h.closing()
    h.restart_adapter()  # 计时已落盘，重启不重新开始。
    h.now += timedelta(seconds=60)
    await h.adapter.protection_tick()
    assert h.closing()
    assert h.alerts()[-1] == CLOSE_BODY
    assert [c[:3] for c in h.creates()] == ["lb-"]


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_zero_grace_closes_on_first_failure(tmp_path: Path, side: Side):
    h = PrearmHarness(tmp_path, side, prearm=0.0)
    await h.adapter.protection_tick()
    assert h.closing() and h.alerts() == [CLOSE_BODY]
    assert h.state().prearm_failing_since is None


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_no_confirmed_stop_closes_immediately(tmp_path: Path, side: Side):
    h = PrearmHarness(tmp_path, side, prearm=60.0)

    async def network_down():
        raise ExchangeError("synthetic timeout", transient=True)

    h.exchange.positions_evidence = network_down  # 还没挂任何保护就一时失败。
    await h.adapter.protection_tick()
    assert h.closing() and not h.creates()
    assert "宽限内" not in h.alerts()[-1]


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_transient_network_failure_with_confirmed_lifeboat_is_tolerated(
    tmp_path: Path, side: Side
):
    h = PrearmHarness(tmp_path, side, prearm=60.0)
    await h.adapter.protection_tick()  # lb 已回执确认，SL 尚未提交。
    h.exchange.hidden.clear()
    original = h.exchange.positions_evidence

    async def network_down():
        raise ExchangeError("synthetic timeout", transient=True)

    h.exchange.positions_evidence = network_down
    h.now += timedelta(seconds=5)
    await h.adapter.protection_tick()
    assert not h.closing() and "已 5 / 60 秒" in h.alerts()[-1]
    h.exchange.positions_evidence = original
    await h.adapter.protection_tick()
    assert h.state().stage == "armed" and not h.closing()


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_parameter_mismatch_is_never_tolerated(tmp_path: Path, side: Side):
    h = PrearmHarness(tmp_path, side, prearm=60.0)
    await h.adapter.protection_tick()
    lb = h.creates()[0]
    h.exchange.hidden.clear()
    h.exchange.orders[lb] = replace(h.exchange.orders[lb], trigger_price=1.0)
    await h.adapter.protection_tick()
    assert h.closing()
    assert "触发价" in h.alerts()[-1] and "宽限内" not in h.alerts()[-1]


# ---- 各条资格逐条核对（直接问状态机）


def _read_lag() -> ProtectionIncomplete:
    return ProtectionIncomplete("已提交条件单暂时不可查，不能再次创建或当作已撤", read_lag=True)


async def _lb_acked(tmp_path: Path, side: Side, prearm: float = 60.0) -> PrearmHarness:
    h = PrearmHarness(tmp_path, side, prearm=prearm)
    with pytest.raises(ProtectionIncomplete) as raised:
        await h.manager.sync("pos-1")
    assert raised.value.read_lag
    return h


def _edit_leg(h: PrearmHarness, prefix: str, **changes) -> None:
    tracked = h.registry.get("pos-1")
    state = tracked.server_protection
    legs = [
        leg.model_copy(update=changes) if leg.client_order_id.startswith(prefix) else leg
        for leg in state.legs
    ]
    h.registry.put(
        tracked.model_copy(update={"server_protection": state.model_copy(update={"legs": legs})})
    )


def _edit_state(h: PrearmHarness, **changes) -> None:
    tracked = h.registry.get("pos-1")
    state = tracked.server_protection.model_copy(update=changes)
    h.registry.put(tracked.model_copy(update={"server_protection": state}))


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
@pytest.mark.parametrize(
    "stop",
    [
        {**LIVE, "algo_id": "8"},
        {**LIVE, "algo_id": "8", "algo_status": "CANCELED", "state": "cancelled"},
    ],
)
async def test_eligible_read_lag_is_tolerated(tmp_path: Path, side: Side, stop: dict):
    h = await _lb_acked(tmp_path, side)
    _edit_leg(h, "br-sl-", **stop)  # 其他腿明确 NEW 或已明确撤销也可宽限
    assert h.manager.prearm_grace("pos-1", _read_lag()) == 0
    assert h.manager.prearm_grace("pos-1", ExchangeError("x", transient=True)) == 0


@pytest.mark.parametrize(
    "error",
    [
        ProtectionIncomplete("已提交条件单暂时不可查，不能再次创建或当作已撤"),  # 读到过又消失
        ProtectionIncomplete("实际仓量超过已确认入场成交，不能覆盖未知增仓"),
        ExchangeError("synthetic definite failure"),
        ValueError("synthetic bug"),
    ],
)
async def test_other_errors_are_never_tolerated(tmp_path: Path, error: Exception):
    h = await _lb_acked(tmp_path, Side.LONG)
    assert h.manager.prearm_grace("pos-1", error) is None


@pytest.mark.parametrize(
    ("target", "changes"),
    [
        # 保命单本身不再明确 NEW：没有已确认止损
        ("lb-", {"state": "submit_pending"}),
        ("lb-", {"child_state": "unknown"}),
        ("lb-", {"algo_status": "TRIGGERED"}),
        ("lb-", {"algo_status": "CANCELED", "state": "cancelled"}),
        # 保命单仍在，但另一腿提交/撤销未知、被拒、子单未知或已触发
        ("br-sl-", {"state": "submit_pending"}),
        ("br-sl-", {**LIVE, "state": "cancel_pending"}),
        ("br-sl-", {"state": "rejected"}),
        ("br-sl-", {**LIVE, "child_state": "unknown"}),
        ("br-sl-", {**LIVE, "algo_status": "TRIGGERED"}),
        ("br-sl-", {**LIVE, "filled_quantity": 0.5}),
        ("br-sl-", {**LIVE, "algo_status": "FINISHED", "state": "cancelled"}),
        # 已在退出或曾经完整托管（后者走原“保留最后确认止损”分支）
        ("state", {"stage": "exiting"}),
        ("state", {"ever_armed": True}),
    ],
)
async def test_unsafe_protection_state_is_never_tolerated(
    tmp_path: Path, target: str, changes: dict
):
    h = await _lb_acked(tmp_path, Side.LONG)
    if target == "state":
        _edit_state(h, **changes)
    else:
        _edit_leg(h, target, **changes)
    assert h.manager.prearm_grace("pos-1", _read_lag()) is None
    assert h.state().prearm_failing_since is None


async def test_zero_setting_is_never_tolerated(tmp_path: Path):
    h = await _lb_acked(tmp_path, Side.LONG, prearm=0.0)
    assert h.manager.prearm_grace("pos-1", _read_lag()) is None


async def test_tolerance_runs_out_at_the_configured_seconds(tmp_path: Path):
    h = await _lb_acked(tmp_path, Side.LONG)
    assert h.manager.prearm_grace("pos-1", _read_lag()) == 0
    h.now = START + timedelta(seconds=59)
    assert h.manager.prearm_grace("pos-1", _read_lag()) == 59
    h.now = START + timedelta(seconds=60)
    assert h.manager.prearm_grace("pos-1", _read_lag()) is None
