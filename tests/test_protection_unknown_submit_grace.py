"""条件单提交结果未知时按原 CID 只读重查的宽限（INJUSDT 2026-10-28 复现）；只有合成订单。

时间线：保命单回执确认 → 固定止损 POST 结果未知（交易所其实已收下）。
原行为立即按对账原因平仓；打开 protection_unknown_submit_grace_s 后先按原 CID 只读重查。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from jiaoyi.contracts import Side
from jiaoyi.contracts.module_status import AlertLevel
from jiaoyi.contracts.position import CloseReason, PositionStatus
from jiaoyi.execution.brackets import BracketManager, ProtectionIncomplete
from jiaoyi.execution.exchange import BracketSubmissionUnknown
from tests.test_execution import START
from tests.test_protection_readback_grace import GraceHarness

TITLE = "普通托管保护未完整确认"
EXIT_TITLE = "托管保护已触发，按原因退出"
UNKNOWN = "ProtectionIncomplete: 普通保护提交结果未知，只查原CID"


class UnknownSubmitHarness(GraceHarness):
    """``unknown_prefix`` 的腿 POST 后交易所已收下但回执未知，visible_after 次 sleep 后才查得到。"""

    def __init__(
        self,
        root: Path,
        side: Side,
        *,
        unknown: float,
        prearm: float = 60.0,
        unknown_prefix: str = "br-sl-",
        visible_after: int = 2,
        observed: bool = False,
    ) -> None:
        self.unknown, self.prearm = unknown, prearm
        super().__init__(root, side, grace=3.0, prefix="none-", visible_after=visible_after)
        self.unknown_prefix, self.observed = unknown_prefix, observed
        original = self.exchange._place

        async def place(symbol, close_side, quantity, trigger_price, cid, kind, working_type):
            if not cid.startswith(self.unknown_prefix):
                return await original(
                    symbol, close_side, quantity, trigger_price, cid, kind, working_type
                )
            self.exchange.unknown_submit = True
            try:
                return await original(
                    symbol, close_side, quantity, trigger_price, cid, kind, working_type
                )
            except BracketSubmissionUnknown:
                if self.observed:  # 交易所层失败后当场按 CID 查到了。
                    self.exchange.hidden.discard(cid)
                    raise BracketSubmissionUnknown(cid, self.exchange.orders[cid]) from None
                raise
            finally:
                self.exchange.unknown_submit = False

        self.exchange._place = place

    def build(self) -> BracketManager:
        manager = super().build()
        manager.prearm_grace_s = self.prearm
        manager.unknown_submit_grace_s = self.unknown
        return manager

    def alerts(self) -> list[str]:
        return [a.body for a in self.alerter.sent if a.title == TITLE]

    def leg(self, prefix: str):
        return next(leg for leg in self.state().legs if leg.client_order_id.startswith(prefix))


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_zero_setting_closes_as_in_the_incident(tmp_path: Path, side: Side):
    h = UnknownSubmitHarness(tmp_path, side, unknown=0.0)
    await h.adapter.protection_tick()
    assert h.closing()
    assert [c[:6] for c in h.creates()] == ["lb-pos", "br-sl-"]
    (body,) = h.alerts()
    assert body.startswith(f"BTCUSDT pos-1：{UNKNOWN} <- BracketSubmissionUnknown")
    assert body.endswith("保留已确认保护，停止入场并核验退出")
    assert h.leg("lb-").state == "confirmed" and h.leg("lb-").algo_status == "NEW"
    assert h.leg("br-sl-").state == "submit_pending"
    assert not h.slept


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_stop_found_by_original_cid_arms_every_leg_in_the_same_tick(
    tmp_path: Path, side: Side
):
    h = UnknownSubmitHarness(tmp_path, side, unknown=3.0)
    await h.adapter.protection_tick()
    assert not h.closing() and not h.exchange.direct and not h.alerts()
    assert sorted(c[:6] for c in h.creates()) == ["br-sl-", "lb-pos"]  # 每腿一次。
    state = h.state()
    assert state.stage == "armed" and state.ever_armed and state.generation == 1
    assert all(leg.state == "confirmed" and leg.algo_status == "NEW" for leg in state.legs)
    assert h.slept == [0.5, 0.5]
    assert [cid[:6] for algo, cid in h.reads if algo is None and cid].count("br-sl-") >= 2
    assert h.registry.get("pos-1").record.status is PositionStatus.OPEN


async def test_order_seen_by_the_exchange_layer_is_accepted_without_waiting(tmp_path: Path):
    h = UnknownSubmitHarness(tmp_path, Side.SHORT, unknown=3.0, observed=True)
    await h.adapter.protection_tick()
    assert h.state().stage == "armed" and not h.closing()
    assert not h.slept and len(h.creates()) == 2


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_still_unknown_keeps_position_in_prearm_grace_and_only_rereads_the_cid(
    tmp_path: Path, side: Side
):
    h = UnknownSubmitHarness(tmp_path, side, unknown=2.0, visible_after=10**6)
    await h.adapter.protection_tick()
    assert sum(h.slept) == pytest.approx(2.0)
    assert not h.closing() and h.state().prearm_failing_since == START + timedelta(seconds=2)
    (body,) = h.alerts()
    assert body.startswith(f"BTCUSDT pos-1：{UNKNOWN}") and "宽限内" in body
    h.restart_adapter()
    h.now += timedelta(seconds=20)
    await h.adapter.protection_tick()  # 每轮只按原 CID 查，不重发、不建新代。
    assert not h.closing() and "已 20 / 60 秒" in h.alerts()[-1]
    assert [c[:6] for c in h.creates()] == ["lb-pos", "br-sl-"] and h.state().generation == 1
    h.exchange.hidden.clear()
    await h.adapter.protection_tick()
    assert sorted(c[:6] for c in h.creates()) == ["br-sl-", "lb-pos"]
    state = h.state()
    assert state.stage == "armed" and state.prearm_failing_since is None
    assert not h.closing()


async def test_still_unknown_past_prearm_grace_closes(tmp_path: Path):
    h = UnknownSubmitHarness(tmp_path, Side.SHORT, unknown=2.0, visible_after=10**6)
    await h.adapter.protection_tick()
    assert not h.closing()
    h.now += timedelta(seconds=60)
    await h.adapter.protection_tick()
    assert h.closing() and "宽限内" not in h.alerts()[-1]
    assert [c[:6] for c in h.creates()] == ["lb-pos", "br-sl-"]


async def test_unknown_lifeboat_without_any_confirmed_stop_still_closes(tmp_path: Path):
    h = UnknownSubmitHarness(
        tmp_path, Side.SHORT, unknown=2.0, unknown_prefix="lb-", visible_after=10**6
    )
    await h.adapter.protection_tick()
    assert h.closing() and [c[:3] for c in h.creates()] == ["lb-"]
    assert "宽限内" not in h.alerts()[-1]


async def test_inline_reread_without_prearm_grace_closes_after_the_wait(tmp_path: Path):
    h = UnknownSubmitHarness(tmp_path, Side.LONG, unknown=1.0, prearm=0.0, visible_after=10**6)
    with pytest.raises(ProtectionIncomplete, match="提交结果未知") as raised:
        await h.manager.sync("pos-1")
    assert raised.value.read_lag and sum(h.slept) == pytest.approx(1.0)
    await h.adapter.protection_tick()
    assert h.closing()


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_armed_tightened_generation_found_by_cid_replaces_the_old_stop_once(
    tmp_path: Path, side: Side
):
    h = UnknownSubmitHarness(tmp_path, side, unknown=3.0, unknown_prefix="br-sl-pos-1-2")
    assert await h.manager.sync("pos-1") == "armed" and not h.slept
    tightened = 99.5 if side is Side.LONG else 100.5
    assert h.manager.set_tightened_stop("pos-1", tightened, "synthetic opposite card")
    assert await h.manager.sync("pos-1") == "armed"
    assert sorted(h.creates()) == sorted(
        ["lb-pos-1-1", "br-sl-pos-1-1", "br-sl-pos-1-2"]
    )  # 每个 CID 只 POST 一次。
    assert h.cancels() == ["br-sl-pos-1-1"]
    assert h.slept == [0.5, 0.5]
    assert h.manager.live_stop_price("pos-1") == tightened
    assert h.leg("br-sl-pos-1-1").state == "cancelled"
    assert not h.closing()


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_stop_triggered_while_another_leg_is_unknown_exits_as_stop_loss(
    tmp_path: Path, side: Side
):
    unknown_prefix = "lb-"
    h = UnknownSubmitHarness(
        tmp_path, side, unknown=2.0, unknown_prefix=unknown_prefix, visible_after=10**6
    )
    h.manager.leg_order = "stop_first"
    await h.adapter.protection_tick()
    assert not h.closing() and h.leg(unknown_prefix).state == "submit_pending"
    stop = h.leg("br-sl-").client_order_id
    h.exchange.orders[stop] = replace(
        h.exchange.orders[stop],
        algo_status="FINISHED",
        child_state="identified",
        actual_order_id="201",
        actual_order_status="FILLED",
        filled_quantity=2.0,
        triggered_at=START,
    )
    h.exchange.held = 0
    before = len(h.alerts())
    await h.adapter.protection_tick()
    tracked = h.registry.get("pos-1")
    assert h.state().exit_reason is CloseReason.STOP_LOSS
    assert tracked.requested_close is CloseReason.STOP_LOSS
    assert tracked.record.status is PositionStatus.CLOSING  # 未知腿查清前不记已平。
    assert len(h.alerts()) == before
    (info,) = [a for a in h.alerter.sent if a.title == EXIT_TITLE]
    assert info.level is not AlertLevel.CRITICAL and "stop_loss" in info.body
    h.exchange.hidden.clear()
    await h.adapter.protection_tick()
    tracked = h.registry.get("pos-1")
    assert tracked.record.status is PositionStatus.CLOSED
    assert tracked.record.close_reason is CloseReason.STOP_LOSS
    assert not h.exchange.direct and len(h.alerts()) == before
    assert len([a for a in h.alerter.sent if a.title == EXIT_TITLE]) == 1
