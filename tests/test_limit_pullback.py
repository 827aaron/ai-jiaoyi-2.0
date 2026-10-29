"""进场挂单等回踩（默认关 = 市价进）：风控员算挂单价，执行层只挂单、到时撤 / 追、按成交卡持仓。"""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

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


async def test_unfilled_limit_hands_chase_to_main_loop(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    asked: list[object] = []
    rig.adapter.set_chase_request(asked.append)
    it = _limit(limit_chase=True)
    rec = await rig.adapter.open(it)
    rig.t = START + timedelta(minutes=61)
    await rig.adapter.positions()
    assert rig.registry.get(rec.position_id).record.status is PositionStatus.FAILED  # type: ignore[union-attr]
    assert asked == [it] and len(rig.api.created) == 1  # 执行层自己不追，交回主循环
    chased = await rig.adapter.open_chase(it)  # 主循环复核过才调
    assert chased.intent_id == f"{it.intent_id}-chase"
    assert rig.api.created[-1]["triple_barrier_config"]["open_order_type"] == 1  # 市价
    assert "entry_price" not in rig.api.created[-1]


async def test_chase_without_hook_does_not_chase(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    await rig.adapter.open(_limit(limit_chase=True))
    rig.t = START + timedelta(minutes=61)
    await rig.adapter.positions()
    assert len(rig.api.created) == 1


def test_guard_recheck_chase(now: datetime) -> None:
    it = _intent(now, ON.model_copy(update={"entry_limit_chase": True}))
    g = RiskGuardImpl(RISK, FAST, Lots(TICK_LOT))
    assert g.recheck_chase(it, card(now), account(now), now) == []
    why = g.recheck_chase(it, card(now), account(now).model_copy(update={"paused": True}), now)
    assert "面板已暂停开新仓" in why
    flipped = card(now, bias="short")
    assert any("方向已改成 short" in r for r in g.recheck_chase(it, flipped, account(now), now))
    assert any("过期" in r for r in g.recheck_chase(it, None, account(now), now))
    held = account(now).model_copy(update={"open_symbols": [it.symbol], "open_positions": 1})
    assert any("已有在管持仓" in r for r in g.recheck_chase(it, card(now), held, now))


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


async def test_hold_limit_close_goes_through_main_loop(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    asked: list[tuple[str, CloseReason]] = []
    rig.adapter.set_close_request(lambda pid, reason: asked.append((pid, reason)))
    it = _limit()
    rec = await rig.adapter.open(it)
    eid = rig.registry.get(rec.position_id).record.executor_id  # type: ignore[union-attr]
    assert eid is not None
    rig.t = START + timedelta(minutes=30)
    rig.api.fill(eid, 67900.0)
    rig.ex.set_position(rec.symbol, rec.side, rec.quantity)
    await rig.adapter.positions()
    rig.t = START + timedelta(minutes=30, seconds=it.time_limit_s + 1)
    await rig.adapter.positions()
    assert asked == [(rec.position_id, CloseReason.TIME_LIMIT)]
    assert rig.api.stopped == []  # 执行层自己不平


async def test_runner_dispatches_executor_close_request(tmp_path: Path, now: datetime) -> None:
    from jiaoyi.app import App
    from jiaoyi.core.config import load_app_config, load_risk_config
    from jiaoyi.loop import Runner
    from tests.test_loop import FakeExecutor, _position

    class _Hooked(FakeExecutor):
        request: Any = None

        def set_close_request(self, request: Any) -> None:
            self.request = request

    root = Path(__file__).resolve().parents[1]
    cfg = load_app_config(root / "config" / "app.example.toml").model_copy(
        update={"data_dir": str(tmp_path)}
    )
    app = App(
        load_risk_config(root / "config" / "risk.example.toml"),
        cfg,
        clock=lambda: now,
        offline=True,
    )
    ex = _Hooked()
    ex.held = [_position()]
    app.m = dataclasses.replace(app.m, executor=ex)
    r = Runner(app)
    r.positions = list(ex.held)
    assert ex.request is not None
    ex.request("pos-0001", CloseReason.TIME_LIMIT)
    ex.request("pos-nope", CloseReason.TIME_LIMIT)  # 不在持仓里：不平
    await asyncio.gather(*r._tasks)
    assert ex.closed == [("pos-0001", CloseReason.TIME_LIMIT)]


class _Cards:
    def __init__(self, c: object) -> None:
        self.c = c

    def current(self, now: datetime) -> object:
        return self.c


class _ChaseExec:
    def __init__(self, inner: Any, now: datetime) -> None:
        self.inner, self.now = inner, now
        self.chased: list[object] = []
        self.request: Any = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    async def account(self) -> Any:
        return account(self.now)

    def set_chase_request(self, request: Any) -> None:
        self.request = request

    async def open_chase(self, intent: Any) -> Any:
        self.chased.append(intent)
        return SimpleNamespace(symbol=intent.symbol)


async def test_runner_chases_under_open_lock_after_recheck(tmp_path: Path, now: datetime) -> None:
    from jiaoyi.app import App
    from jiaoyi.core.config import load_app_config, load_risk_config
    from jiaoyi.loop import Runner

    root = Path(__file__).resolve().parents[1]
    cfg = load_app_config(root / "config" / "app.example.toml").model_copy(
        update={"data_dir": str(tmp_path)}
    )
    app = App(
        load_risk_config(root / "config" / "risk.example.toml"),
        cfg,
        clock=lambda: now,
        offline=True,
    )
    ex = _ChaseExec(app.m.executor, now)
    app.m = dataclasses.replace(
        app.m, executor=ex, cards=_Cards(card(now)), risk=RiskGuardImpl(RISK, FAST, Lots(TICK_LOT))
    )
    r = Runner(app)
    assert ex.request is not None  # 主循环接上了
    it = _intent(now, ON.model_copy(update={"entry_limit_chase": True}))
    app.paused = {"by": "test"}
    await r._chase(it)
    assert ex.chased == []  # 暂停了不追
    app.paused = None
    async with r._open_lock:  # 和开新仓抢同一把锁
        task = asyncio.ensure_future(r._chase(it))
        await asyncio.sleep(0.01)
        assert ex.chased == []
    await task
    assert ex.chased == [it]
