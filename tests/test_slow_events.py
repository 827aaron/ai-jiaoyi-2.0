"""临时会、开仓前卡龄检查、每天固定时间开会、日花费 80% 放慢。"""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from jiaoyi.contracts import BattleCard, Bias, Side, SymbolPool
from jiaoyi.slow.commander import EventRules
from jiaoyi.slow.settings import slow_defaults
from tests import fastloop_helpers as h
from tests.conftest import load_example
from tests.test_loop import FakeCards, _app
from tests.test_slow_commander import FakeEngine, entry, make, pool

ALL = frozenset(
    {
        "key_level_break",
        "move_since_card",
        "new_head_symbol",
        "predictor_against_card",
        "news_high_impact",
        "stale_card_on_entry",
    }
)


def rules(**kw: object) -> EventRules:
    base: dict[str, object] = {
        "enabled": True,
        "triggers": ALL,
        "symbol_gap": timedelta(minutes=60),
    }
    return EventRules(**{**base, **kw})  # type: ignore[arg-type]


async def first_card(c, now: datetime) -> SymbolPool:  # type: ignore[no-untyped-def]
    p = pool(now, entry("AAUSDT"), entry("BBUSDT"))
    await c.refresh(p, "scheduled")
    return p


async def test_level_break_opens_single_symbol_meeting_without_new_round(
    tmp_path: Path, now: datetime
) -> None:
    eng = FakeEngine(["AAUSDT", "BBUSDT"])
    c, _, clock = make(tmp_path, eng, now, events=rules())
    p = await first_card(c, now)
    before = c.current(now)
    assert before is not None and eng.analyzed == ["AAUSDT", "BBUSDT"]

    clock[0] = now + timedelta(minutes=70)  # 过了同币间隔
    t = clock[0]
    # 关键位在 100，价格 101 → 突破；开了临时会就不重开整轮
    assert c.refresh_reason(t, p, {"AAUSDT": 101.0, "BBUSDT": 90.0}) is None
    assert c.pop_event(t, p) == ("AAUSDT", "key_level_break")
    card = await c.refresh_symbol(p, "AAUSDT", "key_level_break")
    assert card is not None and eng.analyzed == ["AAUSDT", "BBUSDT", "AAUSDT"]
    assert card.version == before.version + 1
    assert card.expires_at == before.expires_at  # 有效期不延长
    assert card.refresh_reason == "event:key_level_break"
    assert [s.symbol for s in card.symbols] == ["AAUSDT", "BBUSDT"]
    assert c.pop_event(t, p) is None  # 同币间隔内不再开


async def test_trigger_not_in_list_never_opens_and_disabled_keeps_full_round(
    tmp_path: Path, now: datetime
) -> None:
    c, _, clock = make(
        tmp_path, FakeEngine(["AAUSDT"]), now, events=rules(triggers=frozenset({"move_since_card"}))
    )
    p = pool(now, entry("AAUSDT"))
    await c.refresh(p, "scheduled")
    clock[0] = now + timedelta(minutes=70)
    assert c.refresh_reason(clock[0], p, {"AAUSDT": 101.0}) is None
    assert c.pop_event(clock[0], p) is None

    off, _, clock2 = make(tmp_path / "off", FakeEngine(["AAUSDT"]), now)
    await off.refresh(p, "scheduled")
    clock2[0] = now + timedelta(minutes=70)
    assert off.refresh_reason(clock2[0], p, {"AAUSDT": 101.0}) == "key_level_break"


async def test_event_daily_cap_and_symbol_gap(tmp_path: Path, now: datetime) -> None:
    c, _, clock = make(tmp_path, FakeEngine(["AAUSDT", "BBUSDT"]), now, events=rules(max_per_day=1))
    p = await first_card(c, now)
    clock[0] = t = now + timedelta(minutes=70)
    c.request_event("AAUSDT", "predictor_against_card")
    c.request_event("BBUSDT", "predictor_against_card")
    sym, trig = c.pop_event(t, p) or ("", "")
    assert trig == "predictor_against_card"
    await c.refresh_symbol(p, sym, trig)
    assert c.pop_event(t, p) is None  # 每天最多 1 场


async def test_event_daily_cap_zero_means_unlimited_but_still_counts(
    tmp_path: Path, now: datetime
) -> None:
    syms = ["AAUSDT", "BBUSDT", "CCUSDT"]
    c, _, clock = make(tmp_path, FakeEngine(syms), now, events=rules(max_per_day=0))
    p = pool(now, *(entry(x) for x in syms))
    await c.refresh(p, "scheduled")
    clock[0] = t = now + timedelta(minutes=70)
    for sym in syms:
        c.request_event(sym, "predictor_against_card")
    for _ in syms:
        got = c.pop_event(t, p)
        assert got is not None  # 不被当天次数挡住
        await c.refresh_symbol(p, got[0], got[1])
    assert c._event_count == 3  # 照常计数


def test_event_max_per_day_config_accepts_zero() -> None:
    from pydantic import ValidationError

    from jiaoyi.slow.settings import ScheduleConfig

    assert ScheduleConfig(event_max_per_day=0).event_max_per_day == 0
    assert ScheduleConfig().event_max_per_day == 12
    with pytest.raises(ValidationError):
        ScheduleConfig(event_max_per_day=-1)


async def test_held_symbol_goes_first_and_unlisted_request_ignored(
    tmp_path: Path, now: datetime
) -> None:
    c, _, clock = make(
        tmp_path,
        FakeEngine(["AAUSDT", "BBUSDT"]),
        now,
        events=rules(triggers=frozenset({"predictor_against_card"})),
    )
    p = await first_card(c, now)
    clock[0] = t = now + timedelta(minutes=70)
    c.request_event("AAUSDT", "predictor_against_card")
    c.request_event("BBUSDT", "predictor_against_card")
    c.request_event("AAUSDT", "news_high_impact")  # 不在名单里
    c.set_held({"BBUSDT": Side.LONG})
    assert c.pop_event(t, p) == ("BBUSDT", "predictor_against_card")


async def test_move_since_card_uses_atr_multiple(tmp_path: Path, now: datetime) -> None:
    c, _, clock = make(tmp_path, FakeEngine(["AAUSDT"]), now, events=rules())
    p = pool(now, entry("AAUSDT"))
    await c.refresh(p, "scheduled")
    clock[0] = t = now + timedelta(minutes=70)
    c.refresh_reason(t, p, {"AAUSDT": 50.0}, {"AAUSDT": 1.0})  # 记下参考价 50
    assert c.pop_event(t, p) is None
    c.refresh_reason(t, p, {"AAUSDT": 52.9}, {"AAUSDT": 1.0})  # 2.9 倍
    assert c.pop_event(t, p) is None
    c.refresh_reason(t, p, {"AAUSDT": 53.0}, {"AAUSDT": 1.0})  # 3 倍
    assert c.pop_event(t, p) == ("AAUSDT", "move_since_card")


async def test_new_head_symbol_only_for_head_tier(tmp_path: Path, now: datetime) -> None:
    c, _, clock = make(tmp_path, FakeEngine(["AAUSDT"]), now, events=rules())
    p1 = pool(now, entry("AAUSDT"))
    await c.refresh(p1, "scheduled")
    clock[0] = t = now + timedelta(minutes=70)
    p2 = pool(now, entry("AAUSDT"), entry("CCUSDT", tier="tail"), entry("DDUSDT", tier="head"))
    assert c.refresh_reason(t, p2, {}) is None
    assert c.pop_event(t, p2) == ("DDUSDT", "new_head_symbol")
    card = await c.refresh_symbol(p2, "DDUSDT", "new_head_symbol")
    assert card is not None and "DDUSDT" in [s.symbol for s in card.symbols]


async def test_stale_card_blocks_entry_signal_then_supplement_meeting_restores(
    tmp_path: Path, now: datetime
) -> None:
    ttl = timedelta(hours=24, minutes=20)
    c, _, clock = make(
        tmp_path,
        FakeEngine(["AAUSDT"]),
        now,
        ttl=ttl,
        events=rules(entry_max_age=timedelta(hours=6), symbol_gap=timedelta(minutes=30)),
    )
    p = pool(now, entry("AAUSDT"))
    await c.refresh(p, "scheduled")
    fresh = c.current(now + timedelta(hours=5))
    assert fresh is not None and fresh.symbols[0].allowed_signals

    clock[0] = t = now + timedelta(hours=7)
    stale = c.current(t)
    assert stale is not None and stale.symbols[0].allowed_signals == []
    assert stale.symbols[0].bias is Bias.LONG  # 方向不动，持仓照常盯
    assert c.pop_event(t, p) == ("AAUSDT", "stale_card_on_entry")
    await c.refresh_symbol(p, "AAUSDT", "stale_card_on_entry")
    back = c.current(t)
    assert back is not None and back.symbols[0].allowed_signals  # 新结论出来后的信号能开


async def test_untradeable_sync_on_a_stale_card_queues_no_meeting(
    tmp_path: Path, now: datetime
) -> None:
    c, _, clock = make(
        tmp_path / "c",
        FakeEngine(["AAUSDT", "BBUSDT"]),
        now,
        ttl=timedelta(hours=24, minutes=20),
        events=rules(entry_max_age=timedelta(hours=6)),
    )
    await c.refresh(pool(now, entry("AAUSDT"), entry("BBUSDT")), "scheduled")
    clock[0] = t = now + timedelta(hours=7)
    asked: list[set[str]] = []

    class Rules:
        def unavailable(self, symbols: set[str]) -> dict[str, str]:
            asked.append(set(symbols))
            return {"AAUSDT": "SETTLING"}

    app = _app(tmp_path / "app", t)
    app.m = dataclasses.replace(app.m, cards=c)
    app.lot_rules = Rules()  # type: ignore[assignment]
    app.sync_untradeable(t)
    assert asked == [{"AAUSDT", "BBUSDT"}]
    assert c._pending == {}
    assert c.pop_event(t, pool(t, entry("AAUSDT"), entry("BBUSDT"))) is None


async def test_untradeable_sync_uses_screener_judgement_on_partial_lot_rules(
    tmp_path: Path, now: datetime
) -> None:
    c, _, _ = make(tmp_path / "c", FakeEngine(["AAUSDT", "BBUSDT", "CCUSDT"]), now)
    p = pool(now, entry("AAUSDT"), entry("BBUSDT"), entry("CCUSDT"))
    await c.refresh(p, "scheduled")
    gone = {"AAUSDT": "SETTLING", "BBUSDT": "无下单精度", "CCUSDT": "无下单精度"}

    class Rules:
        def unavailable(self, symbols: list[str]) -> dict[str, str]:
            return {s: gone[s] for s in symbols if s in gone}

    app = _app(tmp_path / "app", now)
    app.m = dataclasses.replace(app.m, cards=c)
    app.lot_rules = Rules()  # type: ignore[assignment]
    app.sync_untradeable(now)
    card = c.current(now)
    assert card is not None
    watch = {s.symbol for s in card.symbols if not s.allowed_signals}
    assert watch == {"AAUSDT"}  # 2 / 3 lack lot rules: only the status-based one counts
    gone["CCUSDT"] = "SETTLING"
    app.sync_untradeable(now)
    card = c.current(now)
    assert card is not None
    assert {s.symbol for s in card.symbols if not s.allowed_signals} == {
        "AAUSDT",
        "BBUSDT",
        "CCUSDT",
    }


async def test_stale_check_off_by_default(tmp_path: Path, now: datetime) -> None:
    c, _, _ = make(tmp_path, FakeEngine(["AAUSDT"]), now, ttl=timedelta(hours=30), events=rules())
    await c.refresh(pool(now, entry("AAUSDT")), "scheduled")
    old = c.current(now + timedelta(hours=20))
    assert old is not None and old.symbols[0].allowed_signals


async def test_failed_event_meeting_keeps_card_and_respects_gap(
    tmp_path: Path, now: datetime
) -> None:
    eng = FakeEngine(["AAUSDT"])
    c, _, clock = make(tmp_path, eng, now, events=rules())
    p = pool(now, entry("AAUSDT"))
    card = await c.refresh(p, "scheduled")
    eng.fail = {"AAUSDT"}
    clock[0] = t = now + timedelta(minutes=70)
    c.request_event("AAUSDT", "predictor_against_card")
    assert await c.refresh_symbol(p, "AAUSDT", "predictor_against_card") is None
    assert c.current(t) == card
    c.request_event("AAUSDT", "predictor_against_card")
    assert c.pop_event(t, p) is None  # 失败也算一场，间隔内不重试


async def test_fixed_time_schedule_opens_once_per_day_at_0800_bangkok(
    tmp_path: Path,
) -> None:
    ttl = timedelta(hours=24, minutes=20)
    t0 = datetime(2026, 10, 22, 2, 0, tzinfo=UTC)  # 曼谷 09:00
    c, _, clock = make(tmp_path, FakeEngine(["AAUSDT"]), t0, ttl=ttl, schedule_at="08:00")
    p = pool(t0, entry("AAUSDT"))
    assert c.fixed_time
    assert c.refresh_reason(t0, p, {}) == "scheduled"  # 没卡先开
    await c.refresh(p, "scheduled")
    for hours in (1, 10, 22):  # 当天晚些时候、次日 00:00 曼谷之前：不开
        assert c.refresh_reason(t0 + timedelta(hours=hours), p, {}) is None
    clock[0] = datetime(2026, 10, 23, 0, 59, tzinfo=UTC)  # 曼谷 07:59
    assert c.refresh_reason(clock[0], p, {}) is None
    clock[0] = datetime(2026, 10, 23, 1, 1, tzinfo=UTC)  # 曼谷 08:01
    assert c.refresh_reason(clock[0], p, {}) == "scheduled"
    await c.refresh(p, "scheduled")
    assert c.refresh_reason(clock[0] + timedelta(minutes=30), p, {}) is None


async def test_fixed_time_does_not_reopen_after_restart(tmp_path: Path) -> None:
    ttl = timedelta(hours=24, minutes=20)
    t0 = datetime(2026, 10, 22, 2, 0, tzinfo=UTC)
    kw: dict[str, object] = {
        "ttl": ttl,
        "schedule_at": "08:00",
        "card_path": tmp_path / "card.json",
    }
    c, _, _ = make(tmp_path, FakeEngine(["AAUSDT"]), t0, **kw)
    p = pool(t0, entry("AAUSDT"))
    await c.refresh(p, "scheduled")
    later = t0 + timedelta(hours=3)
    c2, _, _ = make(tmp_path / "again", FakeEngine(["AAUSDT"]), later, **kw)
    assert c2.current(later) is not None
    assert c2.refresh_reason(later, p, {}) is None  # 存档的卡是 08:00 之后开的，不补开


async def test_slowdown_at_80_percent_then_stop_at_100(tmp_path: Path, now: datetime) -> None:
    slow = (timedelta(minutes=80), timedelta(minutes=60))
    c, _, clock = make(
        tmp_path,
        FakeEngine(["AAUSDT"]),
        now,
        daily_cost_limit_usd=10.0,
        slowdown=slow,
        ttl=timedelta(minutes=50),
    )
    p = pool(now, entry("AAUSDT"))
    await c.refresh(p, "scheduled")
    base = timedelta(minutes=30)
    assert c.schedule_interval(base) == base
    c._charge(7.0)
    assert c.schedule_interval(base) == base  # 7.02 < 8
    c._charge(1.0)
    assert c.schedule_interval(base) == timedelta(minutes=60)
    notice = c.take_notice()
    assert notice is not None and "80%" in notice and "60" in notice
    assert c.take_notice() is None  # 一天只提示一次
    card = await c.refresh(p, "scheduled")
    assert card.expires_at - card.created_at == slow[0]  # 新卡按放慢后的有效期出
    c._charge(5.0)
    assert not c.may_refresh(clock[0])  # 到 100% 当天停会


def test_profile_defaults_for_events_slowdown_and_schedule() -> None:
    s15 = slow_defaults("15m").schedule
    assert not s15.event_meeting_enabled and s15.over_budget == "slow"
    assert s15.slowdown_interval_min == 30
    s2 = slow_defaults("2h").schedule
    assert s2.event_meeting_enabled and s2.slowdown_interval_min == 360
    assert s2.entry_max_card_age_min == 360
    assert s2.event_max_per_day == 50
    assert set(s2.event_triggers) == {
        "key_level_break",
        "move_since_card",
        "new_head_symbol",
        "signal_against_card",
    }
    s6 = slow_defaults("6h").schedule
    assert s6.over_budget == "stop" and s6.slowdown_interval_min is None
    assert "predictor_against_card" in s6.event_triggers and s6.entry_max_card_age_min == 1080
    s12 = slow_defaults("12h").schedule
    assert s12.entry_max_card_age_min == 2160 and "stale_card_on_entry" in s12.event_triggers
    s24 = slow_defaults("24h").schedule
    assert (s24.schedule_at, s24.entry_max_card_age_min, s24.event_max_per_day) == (
        "08:00",
        2160,
        24,
    )
    kw = slow_defaults("24h").commander_kwargs()
    assert kw["schedule_at"] == "08:00" and kw["slowdown"] is None
    assert kw["events"].entry_max_age == timedelta(minutes=2160)  # type: ignore[union-attr]
    assert slow_defaults("2h").commander_kwargs()["slowdown"] == (
        timedelta(minutes=380),
        timedelta(minutes=360),
    )


def test_bad_schedule_at_is_rejected() -> None:
    from pydantic import ValidationError

    from jiaoyi.slow.settings import ScheduleConfig

    with pytest.raises(ValidationError):
        ScheduleConfig(schedule_at="8am")


class EventCards(FakeCards):
    """带临时会接口的假军师团。"""

    def __init__(self, card: BattleCard | None) -> None:
        super().__init__(card)
        self.events: list[tuple[str, str]] = [("BTCUSDT", "key_level_break")]
        self.met: list[tuple[str, str]] = []
        self.fixed = False
        self.every: timedelta | None = None

    def refresh_reason(self, now, pool, prices, atrs=None):  # type: ignore[no-untyped-def]
        return None

    def pop_event(self, now: datetime, pool: SymbolPool) -> tuple[str, str] | None:
        return self.events.pop(0) if self.events else None

    async def refresh_symbol(
        self, pool: SymbolPool, symbol: str, trigger: str
    ) -> BattleCard | None:
        self.met.append((symbol, trigger))
        return self.card

    def request_event(self, symbol: str, trigger: str) -> None: ...

    def schedule_interval(self, base: timedelta) -> timedelta:
        return self.every or base

    @property
    def fixed_time(self) -> bool:
        return self.fixed


async def test_slow_tick_runs_event_meeting_and_respects_fixed_time(
    tmp_path: Path, now: datetime
) -> None:
    from jiaoyi.loop import Runner

    app = _app(tmp_path, now)
    app.pool = SymbolPool.model_validate(load_example("symbol_pool"))
    cards = EventCards(h.card(now))
    app.m = dataclasses.replace(app.m, cards=cards)
    r = Runner(app)
    r._last_screen = now
    await r.slow_tick()
    await asyncio.sleep(0.01)
    assert cards.met == [("BTCUSDT", "key_level_break")] and cards.refreshed == []
    assert not r._refreshing

    # 每天固定时间的档：到了开会间隔也不兜底；放慢后的间隔更长，也不兜底
    interval = timedelta(minutes=app.app_cfg.slow_loop_interval_min)
    r._last_card_at = now - interval
    cards.fixed = True
    await r.slow_tick()
    await asyncio.sleep(0.01)
    assert cards.refreshed == []
    cards.fixed, cards.every = False, interval * 2
    await r.slow_tick()
    await asyncio.sleep(0.01)
    assert cards.refreshed == []
    cards.every = None
    await r.slow_tick()
    await asyncio.sleep(0.01)
    assert cards.refreshed == ["scheduled"]


# ---------------------------------------------------------------- 关键位：在外面 / 穿过

CARD_TTL = timedelta(hours=8)  # 卡有效期拉长，几场临时会都在同一张卡里


def _level_logs(tmp_path: Path, now: datetime) -> list[str]:
    from jiaoyi.core.store import JsonlDecisionLog

    recs = JsonlDecisionLog(tmp_path).read_day(now.date().isoformat())
    return [r.output["key_level"] for r in recs if "key_level" in (r.output or {})]


async def test_outside_mode_keeps_refiring_while_price_stays_beyond(
    tmp_path: Path, now: datetime
) -> None:
    """默认 outside = 现在的行为：新卡关键位还在价格这一侧之外，隔够间隔又开。"""
    c, _, clock = make(tmp_path, FakeEngine(["AAUSDT"]), now, events=rules(), ttl=CARD_TTL)
    p = pool(now, entry("AAUSDT"))
    await c.refresh(p, "scheduled")
    for k in (1, 2, 3):
        clock[0] = t = now + timedelta(minutes=70 * k)
        c.refresh_reason(t, p, {"AAUSDT": 101.0})
        assert c.pop_event(t, p) == ("AAUSDT", "key_level_break")
        assert await c.refresh_symbol(p, "AAUSDT", "key_level_break") is not None
    assert _level_logs(tmp_path, now) == []


async def test_cross_mode_fires_once_on_real_crossing(tmp_path: Path, now: datetime) -> None:
    c, _, clock = make(
        tmp_path, FakeEngine(["AAUSDT"]), now, events=rules(key_level_trigger="cross"), ttl=CARD_TTL
    )
    p = pool(now, entry("AAUSDT"))
    await c.refresh(p, "scheduled")
    clock[0] = t = now + timedelta(minutes=1)
    c.refresh_reason(t, p, {"AAUSDT": 99.0})  # 出卡后价格在阻力 100 下面：记下里侧
    assert c.pop_event(t, p) is None
    clock[0] = t = now + timedelta(minutes=70)
    c.refresh_reason(t, p, {"AAUSDT": 101.0})  # 穿过去
    assert c.pop_event(t, p) == ("AAUSDT", "key_level_break")
    assert await c.refresh_symbol(p, "AAUSDT", "key_level_break") is not None
    # 新卡关键位还是 100、价格还在外面：之后不再开
    for k in (2, 3, 4):
        clock[0] = t = now + timedelta(minutes=70 * k)
        c.refresh_reason(t, p, {"AAUSDT": 101.0})
        assert c.pop_event(t, p) is None
    assert _level_logs(tmp_path, now) == ["crossed", "suppressed_already_beyond"]


async def test_cross_mode_level_beyond_at_card_time_waits_for_return_then_rearms(
    tmp_path: Path, now: datetime
) -> None:
    c, _, clock = make(
        tmp_path, FakeEngine(["AAUSDT"]), now, events=rules(key_level_trigger="cross"), ttl=CARD_TTL
    )
    p = pool(now, entry("AAUSDT"))
    await c.refresh(p, "scheduled")
    for k in (1, 2, 3):  # 出卡时就在外面
        clock[0] = t = now + timedelta(minutes=70 * k)
        c.refresh_reason(t, p, {"AAUSDT": 101.0})
        assert c.pop_event(t, p) is None
    clock[0] = t = now + timedelta(minutes=215)
    c.refresh_reason(t, p, {"AAUSDT": 99.0})  # 回到里侧：重新上膛
    assert c.pop_event(t, p) is None
    clock[0] = t = now + timedelta(minutes=216)
    c.refresh_reason(t, p, {"AAUSDT": 100.5})
    assert c.pop_event(t, p) == ("AAUSDT", "key_level_break")


async def test_cross_mode_side_survives_restart(tmp_path: Path, now: datetime) -> None:
    kw: dict[str, object] = {
        "events": rules(key_level_trigger="cross"),
        "card_path": tmp_path / "card.json",
        "ttl": CARD_TTL,
    }
    c, _, _ = make(tmp_path, FakeEngine(["AAUSDT", "BBUSDT"]), now, **kw)
    p = pool(now, entry("AAUSDT"), entry("BBUSDT"))
    await c.refresh(p, "scheduled")
    c.refresh_reason(now, p, {"AAUSDT": 99.0, "BBUSDT": 101.0})  # AA 在里侧，BB 已在外侧
    later = now + timedelta(minutes=70)
    c2, _, _ = make(tmp_path / "again", FakeEngine(["AAUSDT", "BBUSDT"]), later, **kw)
    c2.refresh_reason(later, p, {"AAUSDT": 101.0, "BBUSDT": 101.0})
    assert c2.pop_event(later, p) == ("AAUSDT", "key_level_break")  # 重启前在里侧，现在穿了
    assert "BBUSDT" not in c2._pending  # BB 重启前就在外面，重启不当新穿越


async def test_cross_mode_failed_meeting_does_not_loop(tmp_path: Path, now: datetime) -> None:
    eng = FakeEngine(["AAUSDT"])
    kw: dict[str, object] = {
        "events": rules(key_level_trigger="cross"),
        "card_path": tmp_path / "card.json",
        "ttl": CARD_TTL,
    }
    c, _, clock = make(tmp_path, eng, now, **kw)
    p = pool(now, entry("AAUSDT"))
    await c.refresh(p, "scheduled")
    c.refresh_reason(now, p, {"AAUSDT": 99.0})
    eng.fail = {"AAUSDT"}
    clock[0] = t = now + timedelta(minutes=70)
    c.refresh_reason(t, p, {"AAUSDT": 101.0})
    assert c.pop_event(t, p) == ("AAUSDT", "key_level_break")
    assert await c.refresh_symbol(p, "AAUSDT", "key_level_break") is None
    for k in (2, 3, 4):
        clock[0] = t = now + timedelta(minutes=70 * k)
        c.refresh_reason(t, p, {"AAUSDT": 101.0})
        assert c.pop_event(t, p) is None
    c2, _, _ = make(tmp_path / "again", eng, t, **kw)  # 重启也不重排
    c2.refresh_reason(t, p, {"AAUSDT": 101.0})
    assert c2.pop_event(t, p) is None


async def test_cross_mode_crossing_inside_symbol_gap_waits_for_gap(
    tmp_path: Path, now: datetime
) -> None:
    """刚开过会、间隔没到时穿过：等到间隔满再开一场，不丢也不连开。"""
    c, _, clock = make(
        tmp_path, FakeEngine(["AAUSDT"]), now, events=rules(key_level_trigger="cross"), ttl=CARD_TTL
    )
    p = pool(now, entry("AAUSDT"))
    await c.refresh(p, "scheduled")
    c.refresh_reason(now, p, {"AAUSDT": 99.0})
    clock[0] = t = now + timedelta(minutes=10)
    c.refresh_reason(t, p, {"AAUSDT": 101.0})
    assert c.pop_event(t, p) is None  # 同币间隔 60 分钟
    clock[0] = t = now + timedelta(minutes=61)
    c.refresh_reason(t, p, {"AAUSDT": 101.0})
    assert c.pop_event(t, p) == ("AAUSDT", "key_level_break")


def test_key_level_trigger_config_values() -> None:
    from pydantic import ValidationError

    from jiaoyi.slow.settings import ScheduleConfig

    assert ScheduleConfig().key_level_trigger == "outside"
    assert ScheduleConfig(key_level_trigger="cross").key_level_trigger == "cross"
    with pytest.raises(ValidationError):
        ScheduleConfig(key_level_trigger="touch")  # type: ignore[arg-type]
    s = slow_defaults()
    s = s.model_copy(
        update={"schedule": s.schedule.model_copy(update={"key_level_trigger": "cross"})}
    )
    assert s.commander_kwargs()["events"].key_level_trigger == "cross"


def _cross(tmp_path: Path, now: datetime, eng: FakeEngine, **kw: object):  # type: ignore[no-untyped-def]
    base: dict[str, object] = {"events": rules(key_level_trigger="cross"), "ttl": CARD_TTL}
    return make(tmp_path, eng, now, **{**base, **kw})


@pytest.mark.parametrize("picks", [["AAUSDT", "BBUSDT"], ["BBUSDT"]])
async def test_cross_mode_full_round_keeps_sides_of_coins_without_new_conclusion(
    tmp_path: Path, now: datetime, picks: list[str]
) -> None:
    """整轮会里 AA 没有新结论（同币间隔内沿用 / 没挑中原样留着）：两侧和没开上的穿越都不清。"""
    eng = FakeEngine(["AAUSDT", "BBUSDT"])
    c, _, clock = _cross(tmp_path, now, eng)
    p = pool(now, entry("AAUSDT"), entry("BBUSDT"))
    await c.refresh(p, "scheduled")
    c.refresh_reason(now, p, {"AAUSDT": 99.0, "CCUSDT": 99.0})  # AA 在里侧
    eng.picks = picks
    clock[0] = t = now + timedelta(minutes=10)
    await c.refresh(p, "scheduled")
    assert eng.analyzed.count("AAUSDT") == 1  # AA 这轮没重开
    clock[0] = t = now + timedelta(minutes=70)
    c.refresh_reason(t, p, {"AAUSDT": 101.0})
    assert c.pop_event(t, p) == ("AAUSDT", "key_level_break")


async def test_cross_mode_full_round_keeps_pending_crossing_of_reused_coin(
    tmp_path: Path, now: datetime
) -> None:
    eng = FakeEngine(["AAUSDT", "BBUSDT"])
    c, _, clock = _cross(tmp_path, now, eng)
    p = pool(now, entry("AAUSDT"), entry("BBUSDT"))
    await c.refresh(p, "scheduled")
    c.refresh_reason(now, p, {"AAUSDT": 99.0})
    clock[0] = t = now + timedelta(minutes=5)
    c.refresh_reason(t, p, {"AAUSDT": 101.0})  # 穿了，同币间隔没到
    eng.picks = ["BBUSDT"]
    clock[0] = t = now + timedelta(minutes=10)
    await c.refresh(p, "scheduled")
    clock[0] = t = now + timedelta(minutes=61)
    c.refresh_reason(t, p, {"AAUSDT": 101.0})
    assert c.pop_event(t, p) == ("AAUSDT", "key_level_break")


async def test_cross_mode_without_event_meetings_records_sides_inside_refresh_gap(
    tmp_path: Path, now: datetime
) -> None:
    """不开临时会时：开会间隔内也照常记两侧、认穿越，间隔只管什么时候开整轮。"""
    c, _, clock = make(
        tmp_path,
        FakeEngine(["AAUSDT"]),
        now,
        events=EventRules(key_level_trigger="cross"),
        min_refresh_gap=timedelta(minutes=120),
        ttl=CARD_TTL,
    )
    p = pool(now, entry("AAUSDT"))
    await c.refresh(p, "scheduled")
    clock[0] = t = now + timedelta(minutes=1)
    assert c.refresh_reason(t, p, {"AAUSDT": 99.0}) is None
    clock[0] = t = now + timedelta(minutes=30)
    assert c.refresh_reason(t, p, {"AAUSDT": 101.0}) is None  # 间隔没到
    clock[0] = t = now + timedelta(minutes=121)
    assert c.refresh_reason(t, p, {"AAUSDT": 101.0}) == "key_level_break"


async def test_cross_mode_new_card_sides_use_price_at_meeting_start(
    tmp_path: Path, now: datetime
) -> None:
    """开会开始时价格在里侧、开完已到外侧：这是穿越，不当成“出卡时就在外侧”。"""
    c, _, clock = _cross(tmp_path, now, FakeEngine(["AAUSDT"]))
    p = pool(now, entry("AAUSDT"))
    await c.refresh(p, "scheduled")
    clock[0] = t = now + timedelta(minutes=70)
    c.refresh_reason(t, p, {"AAUSDT": 99.0})
    c.request_event("AAUSDT", "predictor_against_card")
    assert c.pop_event(t, p) == ("AAUSDT", "predictor_against_card")
    assert await c.refresh_symbol(p, "AAUSDT", "predictor_against_card") is not None
    clock[0] = t = now + timedelta(minutes=131)
    c.refresh_reason(t, p, {"AAUSDT": 101.0})
    assert c.pop_event(t, p) == ("AAUSDT", "key_level_break")


async def test_cross_mode_crossing_kept_when_meeting_never_starts(
    tmp_path: Path, now: datetime
) -> None:
    c, _, clock = _cross(tmp_path, now, FakeEngine(["AAUSDT", "BBUSDT"]))
    p = pool(now, entry("AAUSDT"), entry("BBUSDT"))
    await c.refresh(p, "scheduled")
    c.refresh_reason(now, p, {"AAUSDT": 99.0})
    clock[0] = t = now + timedelta(minutes=70)
    c.refresh_reason(t, p, {"AAUSDT": 101.0})
    assert c.pop_event(t, p) == ("AAUSDT", "key_level_break")
    # 刚好这时 AA 不在池里：会没开成，穿越不算用掉
    assert await c.refresh_symbol(pool(t, entry("BBUSDT")), "AAUSDT", "key_level_break") is None
    clock[0] = t = now + timedelta(minutes=71)
    c.refresh_reason(t, p, {"AAUSDT": 101.0})
    assert c.pop_event(t, p) == ("AAUSDT", "key_level_break")


async def test_cross_mode_requeue_only_own_reason_and_in_pool(
    tmp_path: Path, now: datetime
) -> None:
    c, _, clock = _cross(tmp_path, now, FakeEngine(["AAUSDT", "BBUSDT"]))
    p = pool(now, entry("AAUSDT"), entry("BBUSDT"))
    await c.refresh(p, "scheduled")
    c.refresh_reason(now, p, {"AAUSDT": 99.0, "BBUSDT": 99.0})
    clock[0] = t0 = now + timedelta(minutes=5)
    c.request_event("AAUSDT", "predictor_against_card")  # AA 先排了别的原因
    c.refresh_reason(t0, p, {"AAUSDT": 101.0, "BBUSDT": 101.0})
    assert c._pending["BBUSDT"] == ("key_level_break", t0)
    clock[0] = t = now + timedelta(minutes=40)  # 两个排队都过期了
    c.refresh_reason(t, pool(t, entry("AAUSDT")), {"AAUSDT": 101.0, "BBUSDT": 101.0})
    assert c._pending["AAUSDT"] == ("predictor_against_card", t0)  # 别的原因不替它重排
    assert c._pending["BBUSDT"] == ("key_level_break", t0)  # 不在池里不重排


async def test_cross_mode_pending_crossing_expires(tmp_path: Path, now: datetime) -> None:
    """穿越一直开不上会（每日上限）：等过开会间隔和同币间隔就作废，不隔夜补开。"""
    c, _, clock = _cross(
        tmp_path,
        now,
        FakeEngine(["AAUSDT", "BBUSDT"]),
        events=rules(key_level_trigger="cross", max_per_day=1),
    )
    p = pool(now, entry("AAUSDT"), entry("BBUSDT"))
    await c.refresh(p, "scheduled")
    clock[0] = t = now + timedelta(minutes=70)
    c.request_event("BBUSDT", "predictor_against_card")
    await c.refresh_symbol(p, "BBUSDT", "predictor_against_card")  # 当天名额用完
    c.refresh_reason(t, p, {"AAUSDT": 99.0})
    clock[0] = t = now + timedelta(minutes=71)
    c.refresh_reason(t, p, {"AAUSDT": 101.0})
    assert c.pop_event(t, p) is None
    clock[0] = t = now + timedelta(minutes=135)
    c.refresh_reason(t, p, {"AAUSDT": 101.0})
    assert "AAUSDT" not in c._level_cross and "AAUSDT" not in c._pending
    assert "expired" in _level_logs(tmp_path, now)
