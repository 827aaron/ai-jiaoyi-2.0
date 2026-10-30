"""大盘台转向：立刻暂停卡上旧方向的币开旧方向的新仓，回来就恢复；有信号才给那个币加开临时会。"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from jiaoyi import contracts as c
from jiaoyi.core.desk_turn import DESK_TURN_FILE, TRIGGER, DeskTurnPause
from jiaoyi.core.hotconfig import HOT_FAST, patch_inplace
from jiaoyi.core.recorder import RoleRecorder
from jiaoyi.loop import Runner
from jiaoyi.slow.commander import EventRules
from jiaoyi.slow.market_desk import DeskRun
from tests import fastloop_helpers as h
from tests.conftest import load_example
from tests.test_loop import _app
from tests.test_slow_commander import FakeEngine, make

T = datetime(2026, 10, 30, 0, 48, tzinfo=UTC)  # 曼谷 07:48
ISSUED = T - timedelta(minutes=40)
ON = h.FAST.model_copy(update={"signal_desk_turn_pause": True})
SHORT, LONG = c.Side.SHORT, c.Side.LONG


def card(**bias: str) -> c.BattleCard:
    base = h.card(T)
    tmpl = base.symbols[0]
    syms = [
        tmpl.model_copy(update={"symbol": s, "bias": c.Bias(b), "issued_at": ISSUED})
        for s, b in bias.items()
    ]
    return base.model_copy(update={"symbols": syms})


def met(cd: c.BattleCard, symbol: str, at: datetime, bias: str) -> c.BattleCard:
    syms = [
        s.model_copy(update={"issued_at": at, "bias": c.Bias(bias)}) if s.symbol == symbol else s
        for s in cd.symbols
    ]
    return cd.model_copy(update={"symbols": syms})


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


def pause(
    tmp_path: Path,
    asked: Asked,
    *,
    busy: set[str] | None = None,
    active: list[bool] | None = None,
    initial: str | None = "看跌",
    **cfg: Any,
) -> tuple[DeskTurnPause, c.FastLoopConfig, Log]:
    fast = ON.model_copy(update=cfg)
    log = Log()
    queued = busy if busy is not None else set()
    p = DeskTurnPause(
        fast,
        tmp_path / DESK_TURN_FILE,
        request_event=asked,
        meeting_ok=lambda t: asked.ok,
        pending=lambda s: s in queued,
        active=(lambda: active[0]) if active is not None else None,
        recorder=RoleRecorder(c.Role.RISK_GUARD, log),
        initial_direction=initial,
    )
    return p, fast, log


def summaries(log: Log) -> list[str]:
    return [r.summary for r in log.records]


def test_turn_away_pauses_only_old_side_on_old_direction_cards(tmp_path: Path) -> None:
    asked = Asked()
    p, _, log = pause(tmp_path, asked)
    cd = card(AAUSDT="short", BBUSDT="long")
    p.on_desk_run("中性", T)
    assert asked.calls == []  # 转向时不开会
    assert summaries(log) == [
        "大盘台 07:48 看跌→中性，卡上做空的币暂停开新空单，有信号时再给那个币加开临时会"
    ]
    later = T + timedelta(minutes=14)
    assert (
        p.blocked("AAUSDT", SHORT, cd, later)
        == "大盘台 07:48 看跌→中性，暂停新空单，已给 AAUSDT 加开临时会"
    )
    assert asked.calls == [("AAUSDT", TRIGGER)]
    assert p.blocked("AAUSDT", LONG, cd, later) is None
    assert p.blocked("BBUSDT", SHORT, cd, later) is None  # 卡上做多的币
    assert p.blocked("CCUSDT", SHORT, cd, later) is None  # 卡上没有


def test_bullish_to_bearish_pauses_new_longs(tmp_path: Path) -> None:
    asked = Asked()
    p, _, _ = pause(tmp_path, asked, initial="看涨")
    cd = card(AAUSDT="short", BBUSDT="long")
    p.on_desk_run("看跌", T)
    assert (
        p.blocked("BBUSDT", LONG, cd, T)
        == "大盘台 07:48 看涨→看跌，暂停新多单，已给 BBUSDT 加开临时会"
    )
    assert p.blocked("AAUSDT", SHORT, cd, T) is None


def test_flapping_toggles_the_pause_instantly_without_meetings(tmp_path: Path) -> None:
    asked = Asked()
    p, _, log = pause(tmp_path, asked)
    cd = card(AAUSDT="short")
    p.on_desk_run("中性", T)
    back = T + timedelta(minutes=74)  # 02:02Z，曼谷 09:02
    p.on_desk_run("看跌", back)
    assert p.blocked("AAUSDT", SHORT, cd, back) is None
    again = T + timedelta(minutes=90)  # 02:18Z
    p.on_desk_run("中性", again)
    assert (
        p.blocked("AAUSDT", SHORT, cd, again)
        == "大盘台 09:18 看跌→中性，暂停新空单，已给 AAUSDT 加开临时会"
    )
    assert summaries(log)[1] == "大盘台 09:02 回到看跌，恢复新空单（07:48 看跌→中性 起暂停）"
    assert asked.calls == [("AAUSDT", TRIGGER)]


def test_no_pause_while_neutral_or_turning_into_a_direction(tmp_path: Path) -> None:
    asked = Asked()
    p, _, log = pause(tmp_path, asked, initial="中性")
    cd = card(AAUSDT="short", BBUSDT="long")
    p.on_desk_run("中性", T)
    p.on_desk_run("看跌", T + timedelta(minutes=15))
    assert p.blocked("AAUSDT", SHORT, cd, T) is None
    assert p.blocked("BBUSDT", LONG, cd, T) is None
    assert log.records == []


def test_meeting_once_per_symbol_while_it_is_queued_or_running(tmp_path: Path) -> None:
    asked = Asked()
    busy: set[str] = set()
    p, _, _ = pause(tmp_path, asked, busy=busy)
    cd = card(AAUSDT="short")
    p.on_desk_run("中性", T)
    p.blocked("AAUSDT", SHORT, cd, T + timedelta(minutes=1))
    busy.add("AAUSDT")  # 在排 / 已派出 / 在开
    assert p.blocked("AAUSDT", SHORT, cd, T + timedelta(minutes=2)) == (
        "大盘台 07:48 看跌→中性，暂停新空单，AAUSDT 的临时会已在排 / 在开"
    )
    assert asked.calls == [("AAUSDT", TRIGGER)]
    busy.clear()  # 开完了却没放出来（判不做被忽略 / 开失败）：下一个信号再申请
    assert p.blocked("AAUSDT", SHORT, cd, T + timedelta(minutes=70)) is not None
    assert asked.calls == [("AAUSDT", TRIGGER)] * 2


def test_no_pause_when_the_desk_is_not_the_market_publisher(tmp_path: Path) -> None:
    asked = Asked()
    active = [False]
    p, _, _ = pause(tmp_path, asked, active=active)
    cd = card(AAUSDT="short")
    p.on_desk_run("中性", T)
    assert p.blocked("AAUSDT", SHORT, cd, T) is None
    active[0] = True
    assert p.holds("AAUSDT", SHORT, cd, T) == "大盘台 07:48 看跌→中性，暂停新空单"
    assert asked.calls == []  # holds 只看不申请


def test_reconfirming_meeting_releases_the_symbol_until_the_next_turn(tmp_path: Path) -> None:
    asked = Asked()
    p, _, log = pause(tmp_path, asked)
    cd = card(AAUSDT="short", CCUSDT="short")
    p.on_desk_run("中性", T)
    early = met(cd, "AAUSDT", T - timedelta(seconds=1), "short")  # 转向前开始的会不算
    assert p.blocked("AAUSDT", SHORT, early, T) is not None
    fresh = met(cd, "AAUSDT", T, "short")
    assert p.blocked("AAUSDT", SHORT, fresh, T + timedelta(minutes=8)) is None
    assert summaries(log)[-1] == "大盘台 07:48 看跌→中性 后 AAUSDT 开过会，还是做空，恢复开新空单"
    count = len(log.records)
    assert p.blocked("AAUSDT", SHORT, fresh, T + timedelta(minutes=9)) is None
    assert len(log.records) == count  # 放出来只记一次
    assert p.blocked("CCUSDT", SHORT, fresh, T + timedelta(minutes=8)) is not None
    p.on_desk_run("看涨", T + timedelta(minutes=30))  # 换方向：放出来的作废，空单仍暂停
    assert p.blocked("AAUSDT", SHORT, fresh, T + timedelta(minutes=31)) is not None


def test_flipped_meeting_leaves_it_to_the_card(tmp_path: Path) -> None:
    asked = Asked()
    p, _, log = pause(tmp_path, asked)
    cd = card(AAUSDT="short")
    p.on_desk_run("中性", T)
    flipped = met(cd, "AAUSDT", T + timedelta(minutes=3), "long")
    assert p.blocked("AAUSDT", SHORT, flipped, T + timedelta(minutes=9)) is None
    assert p.blocked("AAUSDT", LONG, flipped, T + timedelta(minutes=9)) is None
    assert len(log.records) == 1


def test_held_coin_meeting_counts_as_the_reconfirmation(tmp_path: Path) -> None:
    asked = Asked()
    p, _, log = pause(tmp_path, asked)
    cd = card(AAUSDT="short")
    p.on_desk_run("中性", T + timedelta(seconds=40), at=T)  # 发布时刻 T，持仓翻向的会 T 开始
    held = met(cd, "AAUSDT", T, "short")
    p.sweep(held, T + timedelta(minutes=6))
    assert summaries(log)[-1] == "大盘台 07:48 看跌→中性 后 AAUSDT 开过会，还是做空，恢复开新空单"
    assert p.blocked("AAUSDT", SHORT, held, T + timedelta(minutes=7)) is None
    assert asked.calls == []


def test_pause_and_releases_survive_restart(tmp_path: Path) -> None:
    asked = Asked()
    p, _, _ = pause(tmp_path, asked)
    cd = card(AAUSDT="short", CCUSDT="short")
    p.on_desk_run("中性", T)
    fresh = met(cd, "AAUSDT", T, "short")
    p.sweep(fresh, T + timedelta(minutes=6))
    restarted, _, rlog = pause(tmp_path, asked, initial="看涨")
    assert restarted.blocked("CCUSDT", SHORT, fresh, T + timedelta(minutes=7)) is not None
    assert restarted.blocked("AAUSDT", SHORT, fresh, T + timedelta(minutes=7)) is None
    assert rlog.records == []  # 重启前已放出来、记过
    restarted.on_desk_run("中性", T + timedelta(minutes=15))  # 存档里上一轮是中性：不算换方向
    assert restarted.released("看跌") == {"AAUSDT"}
    restarted.on_desk_run("看跌", T + timedelta(minutes=30))
    third, _, _ = pause(tmp_path, asked, initial=None)
    assert third.paused_sides() == {}


def test_initial_direction_used_without_a_file(tmp_path: Path) -> None:
    asked = Asked()
    p, _, _ = pause(tmp_path, asked, initial=None)
    p.on_desk_run("中性", T)
    assert p.paused_sides() == {}
    q, _, _ = pause(tmp_path / "b", asked, initial="看跌")
    q.on_desk_run("中性", T)
    assert q.paused_sides() == {"看跌": "看跌→中性"}


def test_off_switch_and_meeting_closed(tmp_path: Path) -> None:
    asked = Asked()
    p, fast, log = pause(tmp_path, asked, signal_desk_turn_pause=False)
    cd = card(AAUSDT="short")
    p.on_desk_run("中性", T)
    assert p.blocked("AAUSDT", SHORT, cd, T) is None and log.records == []
    patch_inplace(fast, ON, HOT_FAST)  # 热生效打开：转向是关着时记下的，照样拦
    assert p.blocked("AAUSDT", SHORT, cd, T) is not None
    assert h.FAST.signal_desk_turn_pause is False

    closed = Asked(ok=False)
    q, _, _ = pause(tmp_path / "c", closed)
    q.on_desk_run("中性", T)
    assert (
        q.blocked("AAUSDT", SHORT, cd, T)
        == "大盘台 07:48 看跌→中性，暂停新空单；临时会没开，等 AAUSDT 下一轮开会"
    )
    assert closed.calls == []


async def test_commander_takes_desk_turn_without_listing_it(tmp_path: Path) -> None:
    cmd, _, _ = make(
        tmp_path,
        FakeEngine(["AAUSDT"]),
        T,
        events=EventRules(enabled=True, triggers=frozenset({"key_level_break"})),
    )
    assert cmd.accepts_event(TRIGGER)


async def test_runner_pauses_on_desk_run_and_rejects_the_signal(tmp_path: Path) -> None:
    app = _app(tmp_path, T)
    patch_inplace(app.app_cfg.fast_loop, ON, HOT_FAST)
    asked = Asked()
    log = Log()
    app.desk_turn = DeskTurnPause(
        app.app_cfg.fast_loop,
        tmp_path / DESK_TURN_FILE,
        request_event=asked,
        initial_direction="看跌",
    )
    app.recorders[c.Role.RISK_GUARD] = RoleRecorder(c.Role.RISK_GUARD, log)
    cd = card(BTCUSDT="short")

    class Cards:
        def current(self, now: datetime) -> c.BattleCard:
            return cd

    class Desk:
        async def run(self, pool: object, reason: str) -> DeskRun:
            return DeskRun("2026-10-30T00:47Z", "ok", "中性")

    app.m = dataclasses.replace(app.m, cards=Cards())
    r = Runner(app)
    await r._desk_run(Desk(), "scheduled")
    assert r._desk_turn_blocked(h.signal(T, side=SHORT), cd, T)
    assert not r._desk_turn_blocked(h.signal(T, side=LONG), cd, T)
    assert asked.calls == [("BTCUSDT", TRIGGER)]
    rej = log.records[-1]
    assert rej.outcome is c.Outcome.REJECT
    assert rej.summary == "BTCUSDT 大盘台 07:48 看跌→中性，暂停新空单，已给 BTCUSDT 加开临时会"

    class Gate:
        def __init__(self) -> None:
            self.asked: list[str] = []

        async def evaluate(self, signal: c.CandidateSignal, cd: object, now: datetime) -> None:
            self.asked.append(signal.symbol)
            raise AssertionError("暂停的信号不该问 Jev")

    gate = Gate()
    r.m = dataclasses.replace(r.m, gate=gate)
    await r.handle_signal(h.signal(T, side=SHORT))
    assert gate.asked == [] and log.records[-1].summary == rej.summary


async def test_runner_ignores_backfill_runs_and_rechecks_chases(tmp_path: Path) -> None:
    app = _app(tmp_path, T)
    patch_inplace(app.app_cfg.fast_loop, ON, HOT_FAST)
    app.desk_turn = DeskTurnPause(
        app.app_cfg.fast_loop, tmp_path / DESK_TURN_FILE, initial_direction="看跌"
    )
    cd = card(BTCUSDT="short")

    class Cards:
        def current(self, now: datetime) -> c.BattleCard:
            return cd

    class Desk:
        def __init__(self, reason: str) -> None:
            self.reason = reason

        async def run(self, pool: object, reason: str) -> DeskRun:
            return DeskRun("2026-10-30T00:47Z", "ok", "中性", reason=self.reason)

    app.m = dataclasses.replace(app.m, cards=Cards())
    r = Runner(app)
    await r._desk_run(Desk("prior_missing"), "scheduled")  # 重启后回填期的方向不算
    assert app.desk_turn.paused_sides() == {}
    await r._desk_run(Desk(""), "scheduled")
    intent = c.OrderIntent.model_validate(
        {**load_example("order_intent"), "symbol": "BTCUSDT", "side": "short"}
    )
    why = await r._may_chase(intent)
    assert "大盘台 07:48 看跌→中性，暂停新空单" in why
