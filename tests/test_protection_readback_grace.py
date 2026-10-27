"""回执后短暂查无（Binance Demo 读延迟）的回读宽限；只有合成订单，不连接任何服务。"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from jiaoyi.contracts import Side
from jiaoyi.contracts.position import CloseReason, PositionStatus
from jiaoyi.execution.brackets import BracketManager, ProtectionIncomplete
from jiaoyi.execution.exchange import ExchangeBracket
from tests.test_execution import START
from tests.test_server_protection_regression import AdapterHarness

UNREADABLE = "ProtectionIncomplete: 已提交条件单暂时不可查，不能再次创建或当作已撤"


class GraceHarness(AdapterHarness):
    """时钟随假 sleep 前进；hide 指定前缀的腿在 ACK 后查无，visible_after 次重读后恢复可查。"""

    def __init__(
        self, root: Path, side: Side, grace: float, prefix: str = "lb-", visible_after: int = 2
    ) -> None:
        self.grace, self.now, self.slept = grace, START, []
        super().__init__(root, side)
        self.prefix, self.visible_after, self.reads = prefix, visible_after, []
        self.exchange.on_place = self.hide
        original_get = self.exchange._get

        def counting_get(algo_id, client_order_id):
            self.reads.append((algo_id, client_order_id))
            return original_get(algo_id, client_order_id)

        self.exchange._get = counting_get

    def build(self) -> BracketManager:
        self.exchange.registry = self.registry
        return BracketManager(
            self.exchange,
            self.registry,
            account_name="synthetic-account",
            connector_name="synthetic-demo",
            clock=lambda: self.now,
            readback_grace_s=self.grace,
            sleep=self.sleep,
        )

    async def hide(self, order: ExchangeBracket) -> None:
        if order.client_order_id.startswith(self.prefix):
            self.exchange.hidden.add(order.client_order_id)

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += timedelta(seconds=seconds)
        if len(self.slept) >= self.visible_after:
            self.exchange.hidden.clear()

    def closing(self) -> bool:
        """已按对账原因进入直连平仓（保命单仍查无时直连会等核验，不一定已发单）。"""
        rec = self.registry.get("pos-1")
        return rec.record.status is PositionStatus.CLOSING and (
            rec.requested_close is CloseReason.RECONCILER
        )


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
@pytest.mark.parametrize("prefix", ["lb-", "br-sl-", "br-tp-"])
async def test_leg_briefly_unreadable_after_ack_arms_all_three_legs(
    tmp_path: Path, side: Side, prefix: str
):
    h = GraceHarness(tmp_path, side, grace=3.0, prefix=prefix)
    await h.adapter.protection_tick()
    assert sorted(c[:3] for c in h.creates()) == ["br-", "br-", "lb-"]  # 每腿只提交一次。
    assert not h.cancels() and not h.closing() and not h.exchange.direct
    state = h.state()
    assert state.stage == "armed" and state.ever_armed and state.last_error is None
    assert all(leg.state == "confirmed" and leg.acked_at is None for leg in state.legs)
    assert h.registry.get("pos-1").record.status is PositionStatus.OPEN
    assert not [a for a in h.alerter.sent if a.title == "普通托管保护未完整确认"]
    assert sum(h.slept) <= 3.0
    hidden = next(leg for leg in state.legs if leg.client_order_id.startswith(prefix))
    retries = [r for r in h.reads if r[0] is not None]
    assert retries and all(algo == hidden.algo_id for algo, _ in retries)  # 重读按 algoId。


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_zero_grace_fails_exactly_as_before(tmp_path: Path, side: Side):
    h = GraceHarness(tmp_path, side, grace=0.0)
    await h.adapter.protection_tick()
    assert [c[:3] for c in h.creates()] == ["lb-"]
    assert not h.slept
    assert h.state().last_error == UNREADABLE
    assert h.closing()
    assert [a.body for a in h.alerter.sent if a.title == "普通托管保护未完整确认"] == [
        f"BTCUSDT pos-1：{UNREADABLE}；保留已确认保护，停止入场并核验退出"
    ]


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_leg_unreadable_past_grace_still_raises_without_resubmit(tmp_path: Path, side: Side):
    h = GraceHarness(tmp_path, side, grace=2.0, visible_after=10**6)
    with pytest.raises(ProtectionIncomplete, match="暂时不可查"):
        await h.manager.sync("pos-1")
    assert sum(h.slept) == pytest.approx(2.0)
    assert [c[:3] for c in h.creates()] == ["lb-"] and not h.cancels()
    (leg,) = [leg for leg in h.state().legs if leg.client_order_id.startswith("lb-")]
    assert leg.state == "confirmed" and leg.acked_at == START  # 不当已撤，不清回执时刻。
    assert h.state().generation == 1
    # 重启后宽限仍从原回执时刻算，不重新计时、不再等待。
    h.restart()
    h.slept.clear()
    with pytest.raises(ProtectionIncomplete, match="暂时不可查"):
        await h.manager.sync("pos-1")
    assert not h.slept and [c[:3] for c in h.creates()] == ["lb-"]
    assert h.state().generation == 1


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_restart_inside_grace_only_waits_the_remaining_window(tmp_path: Path, side: Side):
    h = GraceHarness(tmp_path, side, grace=0.0)
    with pytest.raises(ProtectionIncomplete):
        await h.manager.sync("pos-1")
    h.grace, h.visible_after = 2.0, 10**6
    h.restart()
    h.now = START + timedelta(seconds=1.5)
    with pytest.raises(ProtectionIncomplete, match="暂时不可查"):
        await h.manager.sync("pos-1")
    assert sum(h.slept) == pytest.approx(0.5)
    assert [c[:3] for c in h.creates()] == ["lb-"]


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_leg_that_was_read_back_once_gets_no_grace_when_it_disappears(
    tmp_path: Path, side: Side
):
    h = GraceHarness(tmp_path, side, grace=3.0, prefix="none-")
    assert await h.manager.sync("pos-1") == "armed"
    lb = next(cid for cid in h.exchange.orders if cid.startswith("lb-"))
    h.exchange.hidden.add(lb)
    h.visible_after = 10**6
    with pytest.raises(ProtectionIncomplete, match="暂时不可查"):
        await h.manager.sync("pos-1")
    assert not h.slept and len(h.creates()) == 3
