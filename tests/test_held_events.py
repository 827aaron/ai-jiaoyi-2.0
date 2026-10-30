"""持仓币加开临时会：逆向到 0.5R、大盘台翻向且这笔仓在亏。"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from jiaoyi import contracts as c
from jiaoyi.core.held_events import (
    ADVERSE_TRIGGER,
    DESK_TRIGGER,
    HELD_EVENTS_FILE,
    HeldEventDesk,
)
from jiaoyi.core.hotconfig import HOT_FAST, patch_inplace
from jiaoyi.core.recorder import RoleRecorder
from jiaoyi.loop import Runner
from jiaoyi.slow.commander import EventRules
from tests import fastloop_helpers as h
from tests.conftest import load_example
from tests.test_loop import _app
from tests.test_slow_commander import FakeEngine, entry, make, pool


def pos(now: datetime, **over: Any) -> c.PositionRecord:
    data = load_example("position_record")
    data.update(
        position_id="pos-1",
        symbol="AAUSDT",
        side="long",
        status="open",
        entry_price=100.0,
        stop_loss_price=98.0,
        take_profit_price=104.0,
        stop_loss_pct=2.0,
        opened_at=(now - timedelta(minutes=5)).isoformat(),
    )
    data.update(over)
    return c.PositionRecord.model_validate(data)


def view(now: datetime, direction: str, source: str = "market_desk") -> c.MarketView:
    return c.MarketView(
        market_view_id=f"mv-{direction}",
        version=2,
        observed_at=now,
        regime="大盘",
        regime_direction=direction,  # type: ignore[arg-type]
        source=source,
    )


class Asked:
    def __init__(self, ok: bool = True) -> None:
        self.calls: list[tuple[str, str]] = []
        self.ok = ok

    def __call__(self, symbol: str, trigger: str) -> None:
        self.calls.append((symbol, trigger))


class Log:
    def __init__(self) -> None:
        self.records: list[c.DecisionRecord] = []

    def write(self, record: c.DecisionRecord) -> None:
        self.records.append(record)

    def heartbeat(self, status: c.ModuleStatus) -> None:
        pass


def desk(tmp_path: Path, asked: Asked, **cfg: Any) -> tuple[HeldEventDesk, c.FastLoopConfig, Log]:
    fast = h.FAST.model_copy(update=cfg)
    log = Log()
    d = HeldEventDesk(
        fast,
        tmp_path / HELD_EVENTS_FILE,
        fee_bps=lambda: 5.0,
        request_event=asked,
        meeting_ok=lambda t: asked.ok,
        recorder=RoleRecorder(c.Role.PATROL, log),
    )
    return d, fast, log


def summaries(log: Log) -> list[str]:
    return [r.summary for r in log.records]


# ------------------------------------------------------------------ 逆向 R


def test_long_fires_at_exactly_half_r_once(tmp_path: Path, now: datetime) -> None:
    asked = Asked()
    d, _, log = desk(tmp_path, asked)
    p = pos(now)
    d.on_mark("AAUSDT", 99.02, [p], now)  # 0.49R
    assert asked.calls == []
    d.on_mark("AAUSDT", 99.0, [p], now)  # 0.5R
    d.on_mark("AAUSDT", 98.5, [p], now)
    assert asked.calls == [("AAUSDT", ADVERSE_TRIGGER)]
    assert summaries(log) == ["AAUSDT 持仓逆向 0.5R，加开临时会"]


def test_short_fires_at_exactly_half_r(tmp_path: Path, now: datetime) -> None:
    asked = Asked()
    d, _, _ = desk(tmp_path, asked)
    p = pos(now, side="short", stop_loss_price=102.0)
    d.on_mark("AAUSDT", 100.98, [p], now)
    assert asked.calls == []
    d.on_mark("AAUSDT", 101.0, [p], now)
    assert asked.calls == [("AAUSDT", ADVERSE_TRIGGER)]


def test_r_is_the_stop_distance_fixed_at_open(tmp_path: Path, now: datetime) -> None:
    asked = Asked()
    d, _, _ = desk(tmp_path, asked)
    p = pos(now, stop_loss_price=99.9)  # 移动止损收紧过，R 仍按开仓时的 2%
    d.on_mark("AAUSDT", 99.5, [p], now)
    assert asked.calls == []
    d.on_mark("AAUSDT", 99.0, [p], now)
    assert asked.calls == [("AAUSDT", ADVERSE_TRIGGER)]


def test_adverse_does_not_refire_after_restart(tmp_path: Path, now: datetime) -> None:
    asked = Asked()
    d, _, _ = desk(tmp_path, asked)
    p = pos(now)
    d.on_mark("AAUSDT", 99.0, [p], now)
    again, _, _ = desk(tmp_path, asked)
    again.on_mark("AAUSDT", 98.9, [p], now)
    assert asked.calls == [("AAUSDT", ADVERSE_TRIGGER)]
    other = pos(now, position_id="pos-2")
    again.on_mark("AAUSDT", 98.9, [p, other], now)
    assert asked.calls == [("AAUSDT", ADVERSE_TRIGGER), ("AAUSDT", ADVERSE_TRIGGER)]


def test_second_level_fires_once_more(tmp_path: Path, now: datetime) -> None:
    asked = Asked()
    d, _, log = desk(tmp_path, asked, patrol_held_event_adverse_r2=0.75)
    p = pos(now)
    d.on_mark("AAUSDT", 99.0, [p], now)
    d.on_mark("AAUSDT", 98.52, [p], now)  # 0.74R
    assert len(asked.calls) == 1
    d.on_mark("AAUSDT", 98.5, [p], now)
    d.on_mark("AAUSDT", 98.4, [p], now)
    assert len(asked.calls) == 2
    assert summaries(log)[-1] == "AAUSDT 持仓逆向 0.75R，加开临时会"


def test_adverse_off_and_skips_unopened(tmp_path: Path, now: datetime) -> None:
    asked = Asked()
    d, fast, _ = desk(tmp_path, asked, patrol_held_event_adverse_r=0.0)
    d.on_mark("AAUSDT", 90.0, [pos(now)], now)
    assert asked.calls == []
    patch_inplace(fast, h.FAST, HOT_FAST)  # 热生效：就地改的对象
    d.on_mark("AAUSDT", 90.0, [pos(now, status="opening"), pos(now, symbol="BBUSDT")], now)
    assert asked.calls == []
    d.on_mark("AAUSDT", 90.0, [pos(now)], now)
    assert asked.calls == [("AAUSDT", ADVERSE_TRIGGER)]


def test_meeting_closed_only_records(tmp_path: Path, now: datetime) -> None:
    asked = Asked(ok=False)
    d, _, log = desk(tmp_path, asked)
    d.on_mark("AAUSDT", 99.0, [pos(now)], now)
    assert asked.calls == []
    [rec] = log.records
    assert rec.summary == "AAUSDT 持仓逆向 0.5R；临时会没开，只记录"
    assert rec.outcome is c.Outcome.INFO


# ------------------------------------------------------------------ 大盘台翻向


def test_desk_flip_only_for_losing_disagreeing_coins(tmp_path: Path, now: datetime) -> None:
    asked = Asked()
    d, _, log = desk(tmp_path, asked)
    losing = pos(now, position_id="p-aa", symbol="AAUSDT")
    winning = pos(now, position_id="p-bb", symbol="BBUSDT")
    short = pos(now, position_id="p-cc", symbol="CCUSDT", side="short", stop_loss_price=102.0)
    d.on_mark("AAUSDT", 99.9, [], now)
    d.on_mark("BBUSDT", 101.0, [], now)
    d.on_mark("CCUSDT", 101.0, [], now)
    d.on_market(view(now, "看跌"), [losing, winning, short], now)
    assert asked.calls == [("AAUSDT", DESK_TRIGGER)]
    assert summaries(log) == ["AAUSDT 大盘台转跌，持仓多单加开临时会"]


def test_desk_flip_counts_round_trip_fees(tmp_path: Path, now: datetime) -> None:
    asked = Asked()
    d, _, _ = desk(tmp_path, asked)
    up_a_bit = pos(now, side="short", stop_loss_price=102.0)
    d.on_market(view(now, "看涨"), [up_a_bit], now, price=lambda s: 99.95)  # 毛 +0.05%，净 −0.05%
    assert asked.calls == [("AAUSDT", DESK_TRIGGER)]
    d2, _, _ = desk(tmp_path / "b", Asked())
    ok = Asked()
    d2._request_event = ok
    d2.on_market(view(now, "看涨"), [up_a_bit], now, price=lambda s: 99.85)  # 净 +0.05%
    assert ok.calls == []


def test_desk_flip_once_per_direction_and_rearms(tmp_path: Path, now: datetime) -> None:
    asked = Asked()
    d, _, _ = desk(tmp_path, asked)
    p = pos(now)
    px = lambda s: 99.0  # noqa: E731
    d.on_market(view(now, "看跌"), [p], now, price=px)
    restarted, _, _ = desk(tmp_path, asked)
    restarted.on_market(view(now, "看跌"), [p], now, price=px)
    assert len(asked.calls) == 1
    restarted.on_market(view(now, "中性"), [p], now, price=px)
    restarted.on_market(view(now, "看跌"), [p], now, price=px)
    assert len(asked.calls) == 2


def test_desk_flip_off_and_other_sources(tmp_path: Path, now: datetime) -> None:
    asked = Asked()
    d, fast, _ = desk(tmp_path, asked, patrol_held_event_on_desk_flip=False)
    p = pos(now)
    d.on_market(view(now, "看跌"), [p], now, price=lambda s: 99.0)
    assert asked.calls == []
    patch_inplace(fast, h.FAST, HOT_FAST)
    d.on_market(view(now, "看跌", source="market_only"), [p], now, price=lambda s: 99.0)
    assert asked.calls == []
    d.on_market(view(now, "看跌"), [p], now, price=lambda s: 99.0)
    assert asked.calls == [("AAUSDT", DESK_TRIGGER)]


def test_closed_positions_are_dropped_from_state(tmp_path: Path, now: datetime) -> None:
    asked = Asked()
    d, _, _ = desk(tmp_path, asked)
    p = pos(now)
    d.on_mark("AAUSDT", 99.0, [p], now)
    d.forget("pos-1")
    restarted, _, _ = desk(tmp_path, asked)
    restarted.on_mark("AAUSDT", 99.0, [p], now)
    assert len(asked.calls) == 2
    restarted.on_market(view(now, "看涨"), [pos(now, status="closed")], now)
    assert (tmp_path / HELD_EVENTS_FILE).read_text(encoding="utf-8") == (
        '{"adverse": {}, "desk": {}}'
    )


# ------------------------------------------------------------------ 军师团队列


def rules(**kw: object) -> EventRules:
    base: dict[str, object] = {
        "enabled": True,
        "triggers": frozenset({"key_level_break"}),
        "symbol_gap": timedelta(minutes=60),
    }
    return EventRules(**{**base, **kw})  # type: ignore[arg-type]


async def test_commander_takes_held_triggers_without_listing_them(
    tmp_path: Path, now: datetime
) -> None:
    cmd, _, clock = make(tmp_path, FakeEngine(["AAUSDT"]), now, events=rules())
    p = pool(now, entry("AAUSDT"))
    await cmd.refresh(p, "scheduled")
    assert cmd.accepts_event(ADVERSE_TRIGGER) and cmd.accepts_event(DESK_TRIGGER)
    clock[0] = t = now + timedelta(minutes=30)
    cmd.request_event("AAUSDT", ADVERSE_TRIGGER)
    assert cmd.pop_event(t, p) is None  # 同币 60 分钟间隔没到
    clock[0] = t = now + timedelta(minutes=60)
    assert cmd.pop_event(t, p) == ("AAUSDT", ADVERSE_TRIGGER)


async def test_commander_drops_held_request_that_waits_past_expiry(
    tmp_path: Path, now: datetime
) -> None:
    cmd, _, clock = make(tmp_path, FakeEngine(["AAUSDT"]), now, events=rules())
    p = pool(now, entry("AAUSDT"))
    await cmd.refresh(p, "scheduled")
    clock[0] = t = now + timedelta(minutes=20)
    cmd.request_event("AAUSDT", DESK_TRIGGER)
    assert cmd.pop_event(t, p) is None
    clock[0] = t = now + timedelta(minutes=60)
    assert cmd.pop_event(t, p) is None  # 排队 30 分钟没轮到就丢

    off, _, _ = make(tmp_path / "off", FakeEngine(["AAUSDT"]), now, events=rules(enabled=False))
    assert not off.accepts_event(ADVERSE_TRIGGER)


async def test_commander_dedupes_with_key_level_request(tmp_path: Path, now: datetime) -> None:
    cmd, _, clock = make(tmp_path, FakeEngine(["AAUSDT"]), now, events=rules())
    p = pool(now, entry("AAUSDT"))
    await cmd.refresh(p, "scheduled")
    clock[0] = t = now + timedelta(minutes=70)
    assert cmd.refresh_reason(t, p, {"AAUSDT": 101.0}) is None  # 穿关键位排了一场
    cmd.request_event("AAUSDT", DESK_TRIGGER)
    assert cmd.pop_event(t, p) == ("AAUSDT", "key_level_break")
    await cmd.refresh_symbol(p, "AAUSDT", "key_level_break")
    assert cmd.pop_event(t, p) is None  # 同一个币只开了一场


# ------------------------------------------------------------------ 主循环


async def test_runner_feeds_mark_prices_and_desk_views(tmp_path: Path, now: datetime) -> None:
    app = _app(tmp_path, now)
    asked = Asked()
    app.held_events = HeldEventDesk(
        app.app_cfg.fast_loop, tmp_path / HELD_EVENTS_FILE, fee_bps=lambda: 5.0, request_event=asked
    )
    r = Runner(app)
    long_aa = pos(now)
    long_bb = pos(now, position_id="pos-2", symbol="BBUSDT")
    r.positions = [long_aa, long_bb]
    mark = c.MarkPrice(symbol="AAUSDT", ts=now, mark_price=99.0, funding_rate=0.0)
    await r.on_event(mark)
    assert asked.calls == [("AAUSDT", ADVERSE_TRIGGER)]
    await r.on_event(mark.model_copy(update={"symbol": "BBUSDT", "mark_price": 99.9}))
    r._market_published(view(now, "看跌"))
    assert asked.calls[1:] == [("AAUSDT", DESK_TRIGGER), ("BBUSDT", DESK_TRIGGER)]
