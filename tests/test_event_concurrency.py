"""临时会并发（[schedule] event_concurrency）：同时开几场、不同币不撞、每日上限 / 日花费
不被并发越过、结论并进卡版本连续不丢，整轮期间只给本轮不开的币开临时会。"""

from __future__ import annotations

import asyncio
import dataclasses
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from jiaoyi.app import App
from jiaoyi.contracts import SymbolPool, SymbolPoolEntry
from jiaoyi.core.config import load_app_config, load_risk_config
from jiaoyi.core.store import JsonlDecisionLog
from jiaoyi.loop import Runner
from jiaoyi.slow.commander import Analysis, BattleCardCommander
from jiaoyi.slow.settings import ScheduleConfig, slow_defaults
from tests.test_loop import APP
from tests.test_loop import RISK as RISK_PATH
from tests.test_slow_commander import RISK, FakeEngine, card_symbol, entry, pool
from tests.test_slow_events import rules

SYMS = ["AAUSDT", "BBUSDT", "CCUSDT", "DDUSDT"]
DELAY = 0.3


class SlowEngine(FakeEngine):
    """每场会睡 ``delay`` 秒（可被取消）；记每个币同时在开几场，结论里写第几次开会。"""

    def __init__(self, picks: list[str]) -> None:
        super().__init__(picks)
        self.delay = 0.0
        self.slow: dict[str, float] = {}
        self.per_symbol: dict[str, int] = {}
        self.symbol_peak: dict[str, int] = {}

    async def analyze(self, e: SymbolPoolEntry, regime: str, pick_reason: str = "") -> Analysis:
        self.running += 1
        self.peak = max(self.peak, self.running)
        n = self.per_symbol[e.symbol] = self.per_symbol.get(e.symbol, 0) + 1
        self.symbol_peak[e.symbol] = max(self.symbol_peak.get(e.symbol, 0), n)
        try:
            await asyncio.sleep(self.slow.get(e.symbol, self.delay))
        finally:
            self.running -= 1
            self.per_symbol[e.symbol] -= 1
        self.analyzed.append(e.symbol)
        return Analysis(card_symbol(e.symbol, reason=f"{e.symbol} #{len(self.analyzed)}"), 0.02)


async def setup(
    tmp_path: Path, now: datetime, *, event_concurrency: int, **kw: object
) -> tuple[BattleCardCommander, SlowEngine, SymbolPool, list[datetime]]:
    eng = SlowEngine(list(SYMS))
    clock = [now]
    c = BattleCardCommander(
        eng,
        RISK,
        JsonlDecisionLog(tmp_path),
        clock=lambda: clock[0],
        concurrency=8,
        events=rules(concurrency=event_concurrency, **kw),  # type: ignore[arg-type]
    )
    p = pool(now, *(entry(s) for s in SYMS))
    await c.refresh(p, "scheduled")
    eng.delay, eng.peak, eng.symbol_peak = DELAY, 0, {}
    clock[0] = now + timedelta(minutes=70)  # 过了同币间隔
    for s in SYMS:
        c.request_event(s, "predictor_against_card")
    return c, eng, p, clock


def runner(tmp_path: Path, c: BattleCardCommander, p: SymbolPool, clock: list[datetime]) -> Runner:
    cfg = load_app_config(APP).model_copy(update={"data_dir": str(tmp_path / "app")})
    app = App(load_risk_config(RISK_PATH), cfg, clock=lambda: clock[0], offline=True)
    app.pool = p
    app.m = dataclasses.replace(app.m, cards=c)
    r = Runner(app)
    r._last_screen = clock[0]
    return r


async def drain(r: Runner) -> None:
    while r._tasks:
        await asyncio.wait_for(asyncio.gather(*list(r._tasks)), timeout=10)


async def test_four_event_meetings_run_together_in_about_one_meeting_time(
    tmp_path: Path, now: datetime
) -> None:
    c, eng, p, clock = await setup(tmp_path, now, event_concurrency=4)
    r = runner(tmp_path, c, p, clock)
    before = c.current(clock[0])
    assert before is not None
    started = time.monotonic()
    await r.slow_tick()
    assert r._event_running == set(SYMS)
    await drain(r)
    elapsed = time.monotonic() - started
    assert elapsed < 2 * DELAY  # 一场的时间，不是四场
    assert eng.peak == 4 and max(eng.symbol_peak.values()) == 1
    card = c.current(clock[0])
    assert card is not None and card.version == before.version + 4
    assert all(s.reason.split(" #")[1] in {"5", "6", "7", "8"} for s in card.symbols)
    assert not r._event_running and c._event_claimed == set() and c._event_live == []


async def test_default_one_keeps_one_meeting_at_a_time(tmp_path: Path, now: datetime) -> None:
    c, eng, p, clock = await setup(tmp_path, now, event_concurrency=1)
    r = runner(tmp_path, c, p, clock)
    before = c.current(clock[0])
    assert before is not None
    await r.slow_tick()
    assert len(r._event_running) == 1
    await asyncio.sleep(0)
    await r.slow_tick()  # 在开着一场：不再派
    assert len(r._event_running) == 1
    for _ in SYMS:
        await drain(r)
        await r.slow_tick()
    await drain(r)
    assert eng.peak == 1 and sorted(eng.analyzed[4:]) == SYMS
    card = c.current(clock[0])
    assert card is not None and card.version == before.version + 4


async def test_same_symbol_never_meets_twice_at_once(tmp_path: Path, now: datetime) -> None:
    c, eng, p, clock = await setup(tmp_path, now, event_concurrency=4, symbol_gap=timedelta(0))
    r = runner(tmp_path, c, p, clock)
    await r.slow_tick()
    await asyncio.sleep(DELAY / 3)
    for s in SYMS:  # 开会期间同一个币又来事件
        c.request_event(s, "predictor_against_card")
    assert c.claim_event(clock[0], p) is None
    await r.slow_tick()
    await drain(r)
    assert max(eng.symbol_peak.values()) == 1


async def test_claim_reserves_symbol_before_the_meeting_starts(
    tmp_path: Path, now: datetime
) -> None:
    c, _, p, clock = await setup(tmp_path, now, event_concurrency=4)
    got = [c.claim_event(clock[0], p) for _ in range(5)]
    assert sorted(e[0] for e in got if e is not None) == SYMS and got[4] is None


async def test_daily_cap_holds_when_meetings_are_dispatched_together(
    tmp_path: Path, now: datetime
) -> None:
    c, eng, p, clock = await setup(tmp_path, now, event_concurrency=4, max_per_day=2)
    r = runner(tmp_path, c, p, clock)
    await r.slow_tick()
    assert len(r._event_running) == 2
    await drain(r)
    await r.slow_tick()
    await drain(r)
    assert len(eng.analyzed) == 4 + 2 and c._event_count == 2


async def test_daily_cost_limit_counts_meetings_not_yet_charged(
    tmp_path: Path, now: datetime
) -> None:
    # 首轮花了 4 × 0.02 + 选币 0.01 = 0.09；每场估 0.02：派两场后 0.13 ≥ 0.12，第三场不派
    c, _, p, clock = await setup(tmp_path, now, event_concurrency=4)
    c._cost_limit = 0.12
    r = runner(tmp_path, c, p, clock)
    await r.slow_tick()
    assert len(r._event_running) == 2
    await drain(r)


async def test_events_during_round_skip_round_symbols_and_land_on_its_card(
    tmp_path: Path, now: datetime
) -> None:
    c, eng, p, clock = await setup(tmp_path, now, event_concurrency=4)
    eng.picks = ["AAUSDT", "BBUSDT"]
    eng.slow = {"BBUSDT": 3 * DELAY}  # 整轮最后一场在临时会都开完之后才出卡
    base = c.current(clock[0])
    assert base is not None
    versions: list[int] = []
    c.set_on_card(lambda: versions.append(c._card.version if c._card else -1))
    round_task = asyncio.create_task(c.refresh(p, "manual"))
    await asyncio.sleep(DELAY / 3)
    assert c._round_meeting == {"AAUSDT", "BBUSDT"}
    claimed = [c.claim_event(clock[0], p) for _ in range(3)]
    assert sorted(e[0] for e in claimed if e is not None) == ["CCUSDT", "DDUSDT"]
    events = [c.refresh_symbol(p, s, t) for s, t in (e for e in claimed if e is not None)]
    await asyncio.wait_for(asyncio.gather(round_task, *events), timeout=10)
    assert max(eng.symbol_peak.values()) == 1
    card = c.current(clock[0])
    assert card is not None
    reasons = {s.symbol: int(s.reason.split(" #")[1]) for s in card.symbols}
    assert all(n > 4 for n in reasons.values())  # 四个币都是这一刻新开的结论，没有被整轮冲掉
    distinct = list(dict.fromkeys(versions))
    assert distinct == list(range(base.version + 1, base.version + 5))  # 版本连续、不重号
    assert card.version == base.version + 4


async def test_round_pick_phase_blocks_events(tmp_path: Path, now: datetime) -> None:
    c, _, p, clock = await setup(tmp_path, now, event_concurrency=4)
    c._round_on = True
    assert c.claim_event(clock[0], p) is None
    c._round_on = False
    assert c.claim_event(clock[0], p) is not None


async def test_heartbeat_names_every_event_meeting_in_progress(
    tmp_path: Path, now: datetime
) -> None:
    c, _, p, _ = await setup(tmp_path, now, event_concurrency=4)
    tasks = [
        asyncio.create_task(c.refresh_symbol(p, s, "predictor_against_card")) for s in SYMS[:2]
    ]
    await asyncio.sleep(DELAY / 3)
    assert c._progress_text() == "AAUSDT、BBUSDT 临时会"
    await asyncio.gather(*tasks)
    assert c._progress_text() == ""


def test_event_concurrency_setting_bounds() -> None:
    assert ScheduleConfig().event_concurrency == 1
    assert ScheduleConfig(concurrency=16, event_concurrency=16).concurrency == 16
    assert ScheduleConfig(concurrency=32, event_concurrency=0).event_concurrency == 0
    with pytest.raises(ValueError, match="event_concurrency"):
        ScheduleConfig(concurrency=4, event_concurrency=8)
    with pytest.raises(ValueError):
        ScheduleConfig(concurrency=33)


@pytest.mark.parametrize(("limit", "dispatched"), [(4, {"CCUSDT", "DDUSDT"}), (1, set())])
async def test_runner_during_round_dispatches_only_symbols_outside_the_round(
    tmp_path: Path, now: datetime, limit: int, dispatched: set[str]
) -> None:
    c, _, p, clock = await setup(tmp_path, now, event_concurrency=limit)
    r = runner(tmp_path, c, p, clock)
    c._round_on, c._round_meeting = True, {"AAUSDT", "BBUSDT"}
    r._refreshing = True
    await r.slow_tick()
    assert r._event_running == dispatched
    c._round_on, c._round_meeting = False, None
    await drain(r)


def test_event_concurrency_zero_follows_meeting_concurrency() -> None:
    base = slow_defaults()
    for given, used in ((0, 12), (3, 3)):
        sched = base.schedule.model_copy(update={"concurrency": 12, "event_concurrency": given})
        cfg = base.model_copy(update={"schedule": sched})
        assert cfg.commander_kwargs()["events"].concurrency == used
