"""进场挂单等回踩（默认关 = 市价进）：保镖算挂单价，执行层只挂单、到时撤 / 追、按成交卡持仓。"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta
from pathlib import Path

from jiaoyi.contracts import Outcome
from jiaoyi.contracts.position import CloseReason, PositionStatus
from jiaoyi.risk.guard import RiskGuardImpl
from tests.fastloop_helpers import BTC_LOT, FAST, RISK, Lots, account, card, jev, signal
from tests.test_execution import START, Rig, intent

ON = FAST.model_copy(update={"entry_limit_pullback": True, "entry_limit_offset_stop": 0.25})
TICK_LOT = dataclasses.replace(BTC_LOT, tick_size=0.1)


def _intent(now: datetime, fast=FAST, *, live: bool = False):  # type: ignore[no-untyped-def]
    g = RiskGuardImpl(RISK, fast, Lots(TICK_LOT), live=live)
    v = g.review(signal(now), jev(), card(now), account(now), now, market=signal(now).features)
    assert v.approved, v.reasons
    assert v.intent is not None
    return v.intent


def test_off_by_default_is_market(now: datetime) -> None:
    it = _intent(now)
    assert it.order_type == "market" and it.limit_price is None and it.limit_wait_s is None


def test_limit_price_offset_and_tick_rounding(now: datetime) -> None:
    it = _intent(now, ON)
    assert it.order_type == "limit" and it.limit_price is not None
    entry = signal(now).features.last_price
    dist = entry * it.stop_loss_pct / 100.0
    assert it.limit_price <= entry - 0.25 * dist + 1e-9  # 多单往下让、往下取整
    assert entry - 0.25 * dist - it.limit_price < 0.1 + 1e-9
    assert round(it.limit_price / 0.1, 6) == round(it.limit_price / 0.1)
    assert it.limit_wait_s == 3600 and not it.limit_chase
    chase = _intent(now, ON.model_copy(update={"entry_limit_chase": True}))
    assert chase.limit_chase


def test_live_needs_its_own_switch(now: datetime) -> None:
    assert _intent(now, ON, live=True).order_type == "market"
    both = ON.model_copy(update={"entry_limit_pullback_live": True})
    assert _intent(now, both, live=True).order_type == "limit"


# ---------------------------------------------------------------- 执行层


def _limit(**over: object):  # type: ignore[no-untyped-def]
    return intent(order_type="limit", limit_price=67900.0, limit_wait_s=3600, **over)


async def test_executor_posts_maker_order_with_wait_added(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    it = _limit()
    rec = await rig.adapter.open(it)
    assert rec.status is PositionStatus.OPENING and rig.ex.placed == []  # 没成交不挂保命止损
    barrier = rig.api.created[-1]["triple_barrier_config"]
    assert barrier["open_order_type"] == 3  # 只挂单
    assert barrier["time_limit"] == it.time_limit_s + 3600


async def test_unfilled_limit_cancelled_quietly(tmp_path: Path) -> None:
    rig = Rig(tmp_path, notices=True)
    records: list[tuple[Outcome, str]] = []
    real = rig.adapter._record

    def spy(outcome: Outcome, summary: str, rec: object, **kw: object) -> None:
        records.append((outcome, summary))
        real(outcome, summary, rec, **kw)  # type: ignore[arg-type]

    rig.adapter._record = spy  # type: ignore[method-assign]
    rec = await rig.adapter.open(_limit())
    rig.t = START + timedelta(minutes=59)
    await rig.adapter.positions()
    assert rig.registry.get(rec.position_id).record.status is PositionStatus.OPENING  # type: ignore[union-attr]
    rig.t = START + timedelta(minutes=61)
    await rig.adapter.positions()
    after = rig.registry.get(rec.position_id).record  # type: ignore[union-attr]
    assert after.status is PositionStatus.FAILED
    assert any("挂单没成交已撤" in s for _, s in records)
    assert not any("开仓没成交" in a.title for a in rig.alerts.sent)  # 不当故障报警
    assert len(rig.api.created) == 1  # 不追


async def test_unfilled_limit_chases_with_market(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rec = await rig.adapter.open(_limit(limit_chase=True))
    rig.t = START + timedelta(minutes=61)
    await rig.adapter.positions()
    assert rig.registry.get(rec.position_id).record.status is PositionStatus.FAILED  # type: ignore[union-attr]
    assert len(rig.api.created) == 2
    assert rig.api.created[-1]["triple_barrier_config"]["open_order_type"] == 1  # 市价
    assert "entry_price" not in rig.api.created[-1]


async def test_hold_counted_from_fill(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    it = _limit()
    rec = await rig.adapter.open(it)
    eid = rig.registry.get(rec.position_id).record.executor_id  # type: ignore[union-attr]
    assert eid is not None
    rig.t = START + timedelta(minutes=30)
    rig.api.fill(eid, 67900.0)
    rig.ex.set_position(rec.symbol, rec.side, rec.quantity)
    await rig.adapter.positions()
    t = rig.registry.get(rec.position_id)
    assert t is not None and t.record.status is PositionStatus.OPEN and t.filled_at == rig.t
    assert rig.ex.placed  # 成交后才挂保命止损
    rig.t = START + timedelta(minutes=30, seconds=it.time_limit_s - 1)
    await rig.adapter.positions()
    assert rig.api.stopped == []
    rig.t = START + timedelta(minutes=30, seconds=it.time_limit_s + 1)
    await rig.adapter.positions()
    assert rig.api.stopped and rig.api.stopped[-1] == (eid, False)
    closed = rig.registry.get(rec.position_id).record  # type: ignore[union-attr]
    assert closed.close_reason is CloseReason.TIME_LIMIT
