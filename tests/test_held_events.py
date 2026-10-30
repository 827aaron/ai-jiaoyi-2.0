"""持仓币加开临时会：反向信号且已亏 0.5R、大盘台翻向且这笔仓在亏。"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from jiaoyi import contracts as c
from jiaoyi.core.held_events import (
    DESK_TRIGGER,
    HELD_EVENTS_FILE,
    REVERSE_TRIGGER,
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


def sig(now: datetime, side: str = "short", symbol: str = "AAUSDT") -> c.AgainstSignalEvent:
    return c.AgainstSignalEvent.model_validate(
        {
            "symbol": symbol,
            "ts": now,
            "kind": "volume_surge",
            "side": side,
            "desc": "1 分钟量 5.0 倍",
            "price": 100.0,
            "vol_ratio": 5.0,
            "buy_ratio": 0.2,
        }
    )


def view(now: datetime, direction: str, source: str = "market_desk") -> c.MarketView:
    return c.MarketView(
        market_view_id=f"mv-{direction}-{now:%H%M}",
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


class Met:
    """``met_since``：哪些币在什么时间开过会。"""

    def __init__(self) -> None:
        self.at: dict[str, datetime] = {}

    def __call__(self, symbol: str, since: datetime) -> bool:
        return symbol in self.at and self.at[symbol] >= since


ON = h.FAST.model_copy(
    update={"patrol_held_event_loss_r": 0.5, "patrol_held_event_on_desk_flip": True}
)


def desk(
    tmp_path: Path, asked: Asked, met: Met | None = None, **cfg: Any
) -> tuple[HeldEventDesk, c.FastLoopConfig, Log]:
    fast = ON.model_copy(update=cfg)
    log = Log()
    d = HeldEventDesk(
        fast,
        tmp_path / HELD_EVENTS_FILE,
        fee_bps=lambda: 5.0,
        request_event=asked,
        meeting_ok=lambda t: asked.ok,
        met_since=met,
        recorder=RoleRecorder(c.Role.PATROL, log),
    )
    return d, fast, log


def summaries(log: Log) -> list[str]:
    return [r.summary for r in log.records]


# ------------------------------------------------------------------ 反向信号且已亏


def test_reverse_signal_on_long_fires_at_exactly_half_r_net_of_fees(
    tmp_path: Path, now: datetime
) -> None:
    asked = Asked()
    d, _, log = desk(tmp_path, asked)
    p = pos(now)
    d.on_mark("AAUSDT", 99.12, [], now)  # 毛 −0.88%，扣费 −0.98% = 0.49R
    d.on_reverse(sig(now), [p], now)
    assert asked.calls == []
    d.on_mark("AAUSDT", 99.10000000000001, [], now)  # 扣费 −1.0% = 0.5R（浮点算出 0.49999…）
    d.on_reverse(sig(now), [p], now)
    assert asked.calls == [("AAUSDT", REVERSE_TRIGGER)]
    assert summaries(log) == ["AAUSDT 反向信号且持仓已亏 0.50R，加开临时会"]


def test_reverse_signal_on_short_fires_at_half_r(tmp_path: Path, now: datetime) -> None:
    asked = Asked()
    d, _, _ = desk(tmp_path, asked)
    p = pos(now, side="short", stop_loss_price=102.0)
    d.on_mark("AAUSDT", 100.88, [], now)
    d.on_reverse(sig(now, side="long"), [p], now)
    assert asked.calls == []
    d.on_mark("AAUSDT", 100.9, [], now)
    d.on_reverse(sig(now, side="long"), [p], now)
    assert asked.calls == [("AAUSDT", REVERSE_TRIGGER)]


def test_reverse_signal_ignores_winning_same_side_and_unknown_r(
    tmp_path: Path, now: datetime
) -> None:
    asked = Asked()
    d, _, _ = desk(tmp_path, asked)
    d.on_mark("AAUSDT", 101.0, [], now)
    d.on_reverse(sig(now), [pos(now)], now)  # 在赚
    d.on_mark("AAUSDT", 98.5, [], now)
    d.on_reverse(sig(now, side="long"), [pos(now)], now)  # 信号和持仓同向
    d.on_reverse(sig(now), [pos(now, stop_loss_pct=None)], now)  # 没有开仓时的 R
    d.on_reverse(sig(now), [pos(now, status="closing")], now)
    assert asked.calls == []
    d.on_reverse(sig(now), [pos(now)], now)
    assert asked.calls == [("AAUSDT", REVERSE_TRIGGER)]


def test_reverse_signal_uses_signal_price_without_a_mark(tmp_path: Path, now: datetime) -> None:
    asked = Asked()
    d, _, _ = desk(tmp_path, asked)
    s = sig(now).model_copy(update={"price": 99.1})
    d.on_reverse(s, [pos(now)], now)
    assert asked.calls == [("AAUSDT", REVERSE_TRIGGER)]


def test_reverse_signal_asks_again_on_each_later_signal(tmp_path: Path, now: datetime) -> None:
    asked = Asked()
    d, _, _ = desk(tmp_path, asked)
    d.on_mark("AAUSDT", 99.0, [], now)
    two = [pos(now), pos(now, position_id="pos-2")]
    d.on_reverse(sig(now), two, now)
    d.on_reverse(sig(now + timedelta(minutes=5)), two, now + timedelta(minutes=5))
    assert asked.calls == [("AAUSDT", REVERSE_TRIGGER)] * 2  # 一个信号一个币只申请一次


def test_defaults_are_off() -> None:
    assert h.FAST.patrol_held_event_loss_r == 0.0
    assert h.FAST.patrol_held_event_on_desk_flip is False


def test_reverse_loss_off_and_meeting_closed(tmp_path: Path, now: datetime) -> None:
    asked = Asked()
    d, fast, _ = desk(tmp_path, asked, patrol_held_event_loss_r=0.0)
    d.on_mark("AAUSDT", 90.0, [], now)
    d.on_reverse(sig(now), [pos(now)], now)
    assert asked.calls == []
    patch_inplace(fast, ON, HOT_FAST)  # 热生效：就地改的对象
    d.on_reverse(sig(now), [pos(now)], now)
    assert asked.calls == [("AAUSDT", REVERSE_TRIGGER)]

    closed = Asked(ok=False)
    d2, _, log = desk(tmp_path / "b", closed)
    d2.on_mark("AAUSDT", 99.0, [], now)
    d2.on_reverse(sig(now), [pos(now)], now)
    assert closed.calls == []
    [rec] = log.records
    assert rec.summary == "AAUSDT 反向信号且持仓已亏 0.55R；临时会没开，只记录"
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
    short = pos(now, side="short", stop_loss_price=102.0)
    d.on_market(view(now, "看涨"), [short], now, price=lambda s: 99.95)  # 毛 +0.05%，净 −0.05%
    assert asked.calls == [("AAUSDT", DESK_TRIGGER)]
    ok = Asked()
    d2, _, _ = desk(tmp_path / "b", ok)
    d2.on_market(view(now, "看涨"), [short], now, price=lambda s: 99.85)  # 净 +0.05%
    assert ok.calls == []


def test_desk_flip_keeps_asking_until_the_coin_meets(tmp_path: Path, now: datetime) -> None:
    asked, met = Asked(), Met()
    d, _, log = desk(tmp_path, asked, met)
    p = pos(now)
    d.on_market(view(now, "看跌"), [], now)
    d.on_mark("AAUSDT", 99.0, [p], now)
    d.on_mark("AAUSDT", 99.0, [p], now + timedelta(minutes=31))  # 排队过期了也再申请
    assert asked.calls == [("AAUSDT", DESK_TRIGGER)] * 2
    assert len(log.records) == 1  # 决策记录只写一条
    met.at["AAUSDT"] = now - timedelta(minutes=1)  # 翻向前开的会不算
    d.on_mark("AAUSDT", 99.0, [p], now)
    assert len(asked.calls) == 3
    met.at["AAUSDT"] = now + timedelta(minutes=40)
    d.on_mark("AAUSDT", 99.0, [p], now + timedelta(minutes=41))
    assert len(asked.calls) == 3


def test_desk_flip_turning_losing_later_still_asks(tmp_path: Path, now: datetime) -> None:
    asked = Asked()
    d, _, _ = desk(tmp_path, asked, Met())
    p = pos(now)
    d.on_mark("AAUSDT", 101.0, [p], now)
    d.on_market(view(now, "看跌"), [p], now)
    assert asked.calls == []
    d.on_mark("AAUSDT", 99.5, [p], now + timedelta(minutes=10))
    assert asked.calls == [("AAUSDT", DESK_TRIGGER)]


def test_desk_direction_survives_restart_and_rearms_on_next_flip(
    tmp_path: Path, now: datetime
) -> None:
    asked, met = Asked(), Met()
    d, _, _ = desk(tmp_path, asked, met)
    d.on_market(view(now, "看跌"), [], now)
    met.at["AAUSDT"] = now + timedelta(minutes=5)
    restarted, _, log = desk(tmp_path, asked, met)
    p = pos(now)
    restarted.on_mark("AAUSDT", 99.0, [p], now + timedelta(minutes=6))
    assert asked.calls == []  # 翻向（重启前）以后已开过会
    restarted.on_market(view(now + timedelta(minutes=7), "看跌"), [p], now)  # 同方向：不算翻向
    assert asked.calls == []
    later = now + timedelta(minutes=20)
    restarted.on_market(view(later - timedelta(minutes=5), "中性"), [p], later)
    restarted.on_market(view(later, "看跌"), [p], later)
    assert asked.calls == [("AAUSDT", DESK_TRIGGER)]
    assert len(log.records) == 1
    again = later + timedelta(minutes=30)
    restarted.on_market(view(again - timedelta(minutes=5), "中性"), [p], again)
    restarted.on_market(view(again, "看跌"), [p], again)
    assert len(log.records) == 2  # 新的一次翻向再写一条


def test_desk_flip_off_and_other_sources(tmp_path: Path, now: datetime) -> None:
    asked = Asked()
    d, fast, _ = desk(tmp_path, asked, patrol_held_event_on_desk_flip=False)
    p = pos(now)
    d.on_market(view(now, "看跌"), [p], now, price=lambda s: 99.0)
    assert asked.calls == []
    patch_inplace(fast, ON, HOT_FAST)
    other, _, _ = desk(tmp_path / "b", asked)
    other.on_market(view(now, "看跌", source="market_only"), [p], now, price=lambda s: 99.0)
    assert asked.calls == []
    d.on_mark("AAUSDT", 99.0, [p], now)  # 开关打开后，已记下的翻向照样认
    assert asked.calls == [("AAUSDT", DESK_TRIGGER)]


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
    assert cmd.accepts_event(REVERSE_TRIGGER) and cmd.accepts_event(DESK_TRIGGER)
    clock[0] = t = now + timedelta(minutes=30)
    cmd.request_event("AAUSDT", REVERSE_TRIGGER)
    assert cmd.pop_event(t, p) is None  # 同币 60 分钟间隔没到
    clock[0] = t = now + timedelta(minutes=60)
    assert cmd.pop_event(t, p) == ("AAUSDT", REVERSE_TRIGGER)

    off, _, _ = make(tmp_path / "off", FakeEngine(["AAUSDT"]), now, events=rules(enabled=False))
    assert not off.accepts_event(REVERSE_TRIGGER)


async def test_commander_one_meeting_covers_all_held_reasons(tmp_path: Path, now: datetime) -> None:
    cmd, _, clock = make(tmp_path, FakeEngine(["AAUSDT"]), now, events=rules())
    p = pool(now, entry("AAUSDT"))
    await cmd.refresh(p, "scheduled")
    clock[0] = t = now + timedelta(minutes=70)
    assert cmd.refresh_reason(t, p, {"AAUSDT": 101.0}) is None  # 穿关键位排了一场
    cmd.request_event("AAUSDT", REVERSE_TRIGGER)
    cmd.request_event("AAUSDT", DESK_TRIGGER)
    assert cmd.pop_event(t, p) == ("AAUSDT", "key_level_break")
    await cmd.refresh_symbol(p, "AAUSDT", "key_level_break")
    assert cmd.pop_event(t, p) is None  # 同一个币只开了一场


async def test_commander_met_since(tmp_path: Path, now: datetime) -> None:
    cmd, _, clock = make(tmp_path, FakeEngine(["AAUSDT"]), now, events=rules())
    p = pool(now, entry("AAUSDT"))
    await cmd.refresh(p, "scheduled")
    assert cmd.met_since("AAUSDT", now)
    assert not cmd.met_since("AAUSDT", now + timedelta(seconds=1))
    assert not cmd.met_since("BBUSDT", now - timedelta(days=1))
    clock[0] = t = now + timedelta(minutes=70)
    cmd.request_event("AAUSDT", DESK_TRIGGER)
    assert cmd.claim_event(t, p) == ("AAUSDT", DESK_TRIGGER)
    assert cmd.met_since("AAUSDT", t)  # 已派出，还没开始


# ------------------------------------------------------------------ 主循环


class _Trig:
    def __init__(self) -> None:
        self.results: list[str] = []

    def note_against_result(self, s: c.AgainstSignalEvent, result: str) -> None:
        self.results.append(result)


async def test_runner_feeds_marks_reverse_signals_and_desk_views(
    tmp_path: Path, now: datetime
) -> None:
    app = _app(tmp_path, now)
    patch_inplace(app.app_cfg.fast_loop, ON, HOT_FAST)
    asked = Asked()
    app.held_events = HeldEventDesk(
        app.app_cfg.fast_loop, tmp_path / HELD_EVENTS_FILE, fee_bps=lambda: 5.0, request_event=asked
    )
    r = Runner(app)
    r.positions = [pos(now), pos(now, position_id="pos-2", symbol="BBUSDT")]
    mark = c.MarkPrice(symbol="AAUSDT", ts=now, mark_price=99.0, funding_rate=0.0)
    await r.on_event(mark)
    await r.on_event(mark.model_copy(update={"symbol": "BBUSDT", "mark_price": 99.9}))
    r._route_against(_Trig(), sig(now), now)  # type: ignore[arg-type]
    assert asked.calls == [("AAUSDT", REVERSE_TRIGGER)]
    r._market_published(view(now, "看跌"))
    assert asked.calls[1:] == [("AAUSDT", DESK_TRIGGER), ("BBUSDT", DESK_TRIGGER)]
    await r.on_event(mark)
    assert asked.calls[3:] == [("AAUSDT", DESK_TRIGGER)]
