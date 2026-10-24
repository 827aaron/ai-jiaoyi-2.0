"""和作战卡相反的信号：哨兵只报，主循环按账本持仓分流（管家急查 / 临时会 / 只记录）。"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from jiaoyi import contracts as c
from jiaoyi.core.against import KEEP, MEETING_TRIGGER, AgainstSignalDesk
from jiaoyi.core.recorder import RoleRecorder
from jiaoyi.core.store import JsonlDecisionLog
from jiaoyi.gate.jev import MockJevTransport
from jiaoyi.loop import Runner
from jiaoyi.sentinel.rules import RuleTrigger
from tests import fastloop_helpers as h
from tests.test_keeper_patrol import close_p, keeper
from tests.test_keeper_patrol import position as kpos
from tests.test_loop import FakeExecutor, FakeKeeper, FakePatrol, _app

FAST = h.FAST
SURGE_LONG: dict[str, float] = {"vol_ratio_1m": 5.0, "taker_buy_ratio_1m": 0.8}


def short_card(now: datetime) -> c.BattleCard:
    return h.card(
        now,
        bias="short",
        allowed_signals=["volume_surge", "volume_breakout", "liquidation_rebound"],
    )


def sig(now: datetime, **over: Any) -> c.AgainstSignalEvent:
    data: dict[str, Any] = {
        "symbol": "BTCUSDT",
        "ts": now,
        "kind": c.SignalKind.VOLUME_SURGE,
        "side": c.Side.SHORT,  # 持仓是多，这个信号对持仓是反向
        "desc": "1 分钟量 5.0 倍，主动卖 80%",
        "price": 68000.0,
        "vol_ratio": 5.0,
        "buy_ratio": 0.2,
    }
    data.update(over)
    return c.AgainstSignalEvent.model_validate(data)


def pos(now: datetime, **over: Any) -> c.PositionRecord:
    return kpos(now, **over)  # 多单，开仓 5 分钟前，入场 68000，止损 67880


# ------------------------------------------------------------------ 哨兵


def test_sentinel_emits_against_event_not_a_signal(now: datetime) -> None:
    t = RuleTrigger(FAST)
    cd = short_card(now)
    assert t.check(h.features(now, **SURGE_LONG), cd, now) == []
    [ev] = t.pop_against()
    assert (ev.symbol, ev.kind, ev.side) == ("BTCUSDT", c.SignalKind.VOLUME_SURGE, c.Side.LONG)
    assert ev.vol_ratio == 5.0 and ev.buy_ratio == 0.8 and ev.price == 68000.0
    assert not ev.strong and ev.actions == [] and "主动买 80%" in ev.desc
    assert t.pop_against() == []  # 取走就没了


def test_sentinel_dedups_same_against_signal(now: datetime) -> None:
    t = RuleTrigger(FAST)
    cd = short_card(now)
    t.check(h.features(now, **SURGE_LONG), cd, now)
    later = now + timedelta(seconds=FAST.against_dedup_s - 1)
    t.check(h.features(later, **SURGE_LONG), cd, later)
    assert len(t.pop_against()) == 1  # 5 分钟内同一种只算 1 次
    again = now + timedelta(seconds=FAST.against_dedup_s)
    t.check(h.features(again, **SURGE_LONG), cd, again)
    assert len(t.pop_against()) == 1


def test_strong_kinds(now: datetime) -> None:
    cd = short_card(now)
    liq = h.features(
        now,
        liq_long_usdt_1m=500_000.0,
        liq_ratio_1m=8.0,
        ret_5m=-0.015,
        cvd_30s_usdt=20_000.0,
        vol_ratio_1m=1.0,
    )
    t = RuleTrigger(FAST)
    t.check(liq, cd, now)
    assert [(e.kind, e.strong) for e in t.pop_against()] == [
        (c.SignalKind.LIQUIDATION_REBOUND, True)
    ]
    # 放量突破：量 ≥ against_strong_breakout_vol_ratio 才算强
    cfg = FAST.model_copy(
        update={"breakout_vol_ratio": 2.0, "against_strong_breakout_vol_ratio": 3.0}
    )
    for vol, strong in ((2.5, False), (3.5, True)):
        t = RuleTrigger(cfg)
        t.check(h.features(now, price=67990.0, vol_ratio_1m=1.0), cd, now)
        t.check(h.features(now, price=68010.0, vol_ratio_1m=vol, taker_buy_ratio_1m=0.5), cd, now)
        [ev] = t.pop_against()
        assert ev.kind is c.SignalKind.VOLUME_BREAKOUT and ev.strong is strong


def test_result_shows_after_against_text(now: datetime) -> None:
    t = RuleTrigger(FAST)
    cd = short_card(now)
    t.check(h.features(now, **SURGE_LONG), cd, now)
    [ev] = t.pop_against()
    t.note_against_result(ev, "已叫管家复查")
    line = next(x for x in t.detail_lines(now) if x.startswith("BTCUSDT"))
    assert "量能突增（多）：和卡上方向相反，已叫管家复查" in line
    later = now + timedelta(seconds=10)  # 去重期内再来：结果还在
    t.check(h.features(later, **SURGE_LONG), cd, later)
    line = next(x for x in t.detail_lines(later) if x.startswith("BTCUSDT"))
    assert "和卡上方向相反，已叫管家复查" in line


# ------------------------------------------------------------------ 分流


def desk(cfg: c.FastLoopConfig = FAST, **kw: Any) -> AgainstSignalDesk:
    return AgainstSignalDesk(cfg, **kw)


def test_no_position_only_records(tmp_path: Path, now: datetime) -> None:
    log = JsonlDecisionLog(tmp_path)
    d = desk(recorder=RoleRecorder(c.Role.SENTINEL, log))
    ev, rechecks = d.route(sig(now), [], set(), now)
    assert rechecks == [] and ev.actions == [] and ev.result == "只记录"
    [rec] = log.read_day(now.date().isoformat())
    assert rec.outcome is c.Outcome.INFO and "反向信号" in rec.summary and "只记录" in rec.summary
    assert rec.output["against_signal"]["result"] == "只记录"


def test_opposite_position_gets_urgent_recheck(now: datetime) -> None:
    ev, rechecks = desk().route(sig(now), [pos(now)], set(), now)
    assert [(p.position_id, one) for p, one in rechecks] == [("pos-1", False)]
    assert ev.actions == ["keeper_recheck"] and ev.result == "已叫管家复查"
    assert ev.position_id == "pos-1"


def test_other_symbol_position_ignored(now: datetime) -> None:
    ev, rechecks = desk().route(sig(now), [pos(now, symbol="ETHUSDT")], set(), now)
    assert rechecks == [] and ev.result == "只记录"


def test_strong_signal_closes_on_one_verdict(now: datetime) -> None:
    s = sig(now, kind=c.SignalKind.LIQUIDATION_REBOUND, strong=True)
    ev, rechecks = desk().route(s, [pos(now)], set(), now)
    assert [one for _, one in rechecks] == [True] and "强信号" in ev.result


def test_loss_past_half_stop_closes_on_one_verdict(now: datetime) -> None:
    # 多单入场 68000、止损 67880（距离 120）：价格 67930 亏 70 ≥ 0.5×120
    ev, rechecks = desk().route(sig(now, price=67930.0), [pos(now)], set(), now)
    assert [one for _, one in rechecks] == [True] and "0.5 倍止损" in ev.result
    _, rechecks = desk().route(sig(now, price=67950.0), [pos(now)], set(), now)
    assert [one for _, one in rechecks] == [False]  # 亏 50 < 60
    off = FAST.model_copy(update={"against_loss_stop_frac": 0.0})
    _, rechecks = desk(off).route(sig(now, price=67000.0), [pos(now)], set(), now)
    assert [one for _, one in rechecks] == [False]
    _, rechecks = desk().route(
        sig(now, price=67000.0), [pos(now, stop_loss_price=None)], set(), now
    )
    assert [one for _, one in rechecks] == [False]  # 没止损价不算


def test_short_position_loss_direction(now: datetime) -> None:
    short = pos(now, side="short", entry_price=68000.0, stop_loss_price=68120.0)
    s = sig(now, side=c.Side.LONG, price=68070.0)  # 空单涨 70 ≥ 60
    _, rechecks = desk().route(s, [short], set(), now)
    assert [one for _, one in rechecks] == [True]


def test_grace_after_open(now: datetime) -> None:
    fresh = pos(now, opened_at=(now - timedelta(seconds=FAST.against_grace_s - 1)).isoformat())
    ev, rechecks = desk().route(sig(now), [fresh], set(), now)
    assert rechecks == [] and ev.result == "只记录（开仓不到 3 分钟，不算）"


def test_recheck_cooldown_per_position(now: datetime) -> None:
    d = desk()
    assert d.route(sig(now), [pos(now)], set(), now)[1]
    soon = now + timedelta(seconds=FAST.against_recheck_cooldown_s - 1)
    ev, rechecks = d.route(sig(soon), [pos(now)], set(), soon)
    assert rechecks == [] and "5 分钟内已急查过" in ev.result
    later = now + timedelta(seconds=FAST.against_recheck_cooldown_s)
    assert d.route(sig(later), [pos(now)], set(), later)[1]
    d.forget("pos-1")  # 平仓后清掉冷却
    assert d.route(sig(later), [pos(now)], set(), later)[1]


def test_closing_or_opening_positions_not_rechecked(now: datetime) -> None:
    for p, closing, why in (
        (pos(now, status="closing"), set(), "持仓正在平仓"),
        (pos(now), {"pos-1"}, "持仓正在平仓"),
        (pos(now, status="opening"), set(), "持仓还在开"),
    ):
        ev, rechecks = desk().route(sig(now), [p], closing, now)
        assert rechecks == [] and ev.result == f"只记录（{why}）"
    ev, rechecks = desk().route(sig(now), [pos(now, status="closed")], set(), now)
    assert rechecks == [] and ev.result == "只记录"


def test_same_side_as_position_not_rechecked(now: datetime) -> None:
    # 卡偏空、手上还有旧多单，来了多头信号：和卡反、和持仓同向，不用复查
    ev, rechecks = desk().route(sig(now, side=c.Side.LONG), [pos(now)], set(), now)
    assert rechecks == [] and "同向" in ev.result


def test_repeated_against_requests_one_meeting(now: datetime) -> None:
    asked: list[tuple[str, str]] = []
    d = desk(request_event=lambda s, t: asked.append((s, t)), meeting_ok=lambda: True)
    ev1, _ = d.route(sig(now), [], set(), now)
    assert asked == [] and ev1.result == "只记录"
    t2 = now + timedelta(minutes=10)
    ev2, _ = d.route(sig(t2, kind=c.SignalKind.VOLUME_BREAKOUT), [], set(), t2)
    assert asked == [("BTCUSDT", MEETING_TRIGGER)]
    assert ev2.actions == ["meeting"] and ev2.result == "已申请临时会"
    # 60 分钟内再反复：不再申请
    t3, t4 = now + timedelta(minutes=20), now + timedelta(minutes=25)
    d.route(sig(t3), [], set(), t3)
    ev4, _ = d.route(sig(t4), [], set(), t4)
    assert len(asked) == 1 and ev4.result == "只记录（60 分钟内已申请过临时会）"
    # 过了 60 分钟、30 分钟内又 2 次：再申请
    t5, t6 = now + timedelta(minutes=75), now + timedelta(minutes=80)
    d.route(sig(t5), [], set(), t5)
    d.route(sig(t6), [], set(), t6)
    assert len(asked) == 2


def test_meeting_needs_hits_within_window(now: datetime) -> None:
    asked: list[tuple[str, str]] = []
    d = desk(request_event=lambda s, t: asked.append((s, t)))
    d.route(sig(now), [], set(), now)
    t2 = now + timedelta(seconds=FAST.against_meeting_window_s + 1)
    d.route(sig(t2), [], set(), t2)
    assert asked == []  # 两次隔了 30 分钟以上
    d.route(sig(now, symbol="ETHUSDT"), [], set(), t2)
    assert asked == []  # 别的币不算


def test_meeting_not_enabled_says_so(now: datetime) -> None:
    asked: list[tuple[str, str]] = []
    d = desk(request_event=lambda s, t: asked.append((s, t)), meeting_ok=lambda: False)
    d.route(sig(now), [], set(), now)
    ev, _ = d.route(sig(now), [], set(), now)
    assert asked == [] and "这档没开这类临时会" in ev.result
    d = desk()  # 没有临时会入口（桩军师团）
    d.route(sig(now), [], set(), now)
    ev, _ = d.route(sig(now), [], set(), now)
    assert "这档没开这类临时会" in ev.result


def test_keeper_and_meeting_together(now: datetime) -> None:
    asked: list[tuple[str, str]] = []
    cfg = FAST.model_copy(update={"against_meeting_count": 1})
    ev, rechecks = desk(cfg, request_event=lambda s, t: asked.append((s, t))).route(
        sig(now), [pos(now)], set(), now
    )
    assert rechecks and asked and ev.actions == ["keeper_recheck", "meeting"]
    assert ev.result == "已叫管家复查；已申请临时会"


def test_switch_off_only_records(now: datetime) -> None:
    asked: list[tuple[str, str]] = []
    off = FAST.model_copy(update={"against_signal_on": False, "against_meeting_count": 1})
    ev, rechecks = desk(off, request_event=lambda s, t: asked.append((s, t))).route(
        sig(now), [pos(now)], set(), now
    )
    assert rechecks == [] and asked == [] and ev.result == "只记录（反向信号处理已关）"


def test_settle_and_recent_window(now: datetime) -> None:
    d = desk()
    d.route(sig(now), [pos(now)], set(), now)
    d.settle("pos-1", "close")
    d.settle("pos-other", "hold")  # 没有对应的不动
    [ev] = d.recent(now)
    assert ev.keeper_verdict == "close"
    assert d.recent(now + KEEP + timedelta(seconds=1)) == []


# ------------------------------------------------------------------ 管家


async def test_keeper_one_shot_closes_first_time(now: datetime) -> None:
    t = MockJevTransport(answer=close_p(0.7))
    k = keeper(t, now)
    k.note_against("pos-1", sig(now, strong=True), True)
    v = await k.review(pos(now), now)
    assert v.action == "close" and v.consecutive == 1 and "过线即平" in v.reason
    assert '"against"' in t.calls[0] and '"strong":true' in t.calls[0].replace(" ", "")


async def test_keeper_normal_against_still_needs_two(now: datetime) -> None:
    t = MockJevTransport(answer=close_p(0.7))
    k = keeper(t, now)
    k.note_against("pos-1", sig(now), False)
    v1 = await k.review(pos(now), now)
    assert v1.action == "hold" and "反向信号急查" in v1.reason
    v2 = await k.review(pos(now), now)  # 下一轮不再带反向信号，照常第 2 次确认
    assert v2.action == "close" and '"against"' not in t.calls[1]


async def test_keeper_one_shot_still_needs_threshold(now: datetime) -> None:
    k = keeper(MockJevTransport(answer=close_p(0.3)), now)
    k.note_against("pos-1", sig(now, strong=True), True)
    assert (await k.review(pos(now), now)).action == "hold"


async def test_keeper_forget_drops_pending_against(now: datetime) -> None:
    t = MockJevTransport(answer=close_p(0.7))
    k = keeper(t, now)
    k.note_against("pos-1", sig(now, strong=True), True)
    k.forget("pos-1")
    assert (await k.review(pos(now), now)).action == "hold"


# ------------------------------------------------------------------ 主循环


class LedgerExecutor(FakeExecutor):
    """账本里此刻的持仓（比上一轮刷新的 ``Runner.positions`` 新）。"""

    def __init__(self, live: list[c.PositionRecord]) -> None:
        super().__init__()
        self.live = live

    def live_positions(self) -> list[c.PositionRecord]:
        return self.live


class AgainstTrigger:
    def __init__(self, events: list[c.AgainstSignalEvent]) -> None:
        self.events = events
        self.results: list[str] = []

    def check(self, *args: object) -> list[c.CandidateSignal]:
        return []

    def pop_against(self) -> list[c.AgainstSignalEvent]:
        out, self.events = self.events, []
        return out

    def note_against_result(self, sig: c.AgainstSignalEvent, result: str) -> None:
        self.results.append(result)


class Feats:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def update(self, event: object) -> None:
        return None

    def snapshot(self, symbol: str) -> c.FeatureSnapshot:
        return h.features(self.now)


class NotingKeeper(FakeKeeper):
    def __init__(self, action: str = "hold") -> None:
        super().__init__(action)
        self.noted: list[tuple[str, bool]] = []

    def note_against(self, position_id: str, sig: c.AgainstSignalEvent, one_shot: bool) -> None:
        self.noted.append((position_id, one_shot))


def _runner(
    tmp_path: Path, now: datetime, live: list[c.PositionRecord], action: str = "hold"
) -> tuple[Runner, AgainstTrigger, NotingKeeper, FakePatrol]:
    app = _app(tmp_path, now)
    trig = AgainstTrigger([sig(now)])
    k, patrol = NotingKeeper(action), FakePatrol()
    app.m = dataclasses.replace(
        app.m,
        executor=LedgerExecutor(live),
        trigger=trig,
        features=Feats(now),
        keeper=k,
        patrol=patrol,
        cards=_Cards(),
    )
    return Runner(app), trig, k, patrol


class _Cards:
    def current(self, now: datetime) -> None:
        return None


def _trade(now: datetime) -> c.Trade:
    return c.Trade(symbol="BTCUSDT", ts=now, price=68000.0, qty=1.0, taker_side=c.Side.SHORT)


async def test_runner_routes_with_ledger_positions(tmp_path: Path, now: datetime) -> None:
    p = _position_now(now)
    r, trig, k, patrol = _runner(tmp_path, now, [p], action="close")
    assert r.positions == []  # 上一轮刷新还没看到这笔，账本里已经有了
    r.positions = [p]
    await r.on_event(_trade(now))
    assert patrol.urgent == {"pos-1"} and k.noted == [("pos-1", False)]
    assert trig.results == ["已叫管家复查"]
    await r.urgent_tick()
    assert k.reviewed == ["pos-1"]
    [ev] = r.app.against_desk.recent(now)
    assert ev.keeper_verdict == "close" and ev.actions == ["keeper_recheck"]


async def test_runner_skips_position_being_closed(tmp_path: Path, now: datetime) -> None:
    p = _position_now(now)
    r, trig, k, patrol = _runner(tmp_path, now, [p])
    r._closing.add("pos-1")  # 主循环正在平这笔
    await r.on_event(_trade(now))
    assert patrol.urgent == set() and k.noted == []
    assert trig.results == ["只记录（持仓正在平仓）"]
    r2, trig2, _, patrol2 = _runner(tmp_path, now, [_position_now(now, status="closing")])
    await r2.on_event(_trade(now))
    assert patrol2.urgent == set() and trig2.results == ["只记录（持仓正在平仓）"]


async def test_runner_heartbeat_lists_recent_against(tmp_path: Path, now: datetime) -> None:
    app = _app(tmp_path, now)
    app.against_desk.route(sig(now), [], set(), now)
    app.heartbeat_all()
    st = app.log.read_status()[c.Role.SENTINEL.value]
    assert [e.result for e in st.against_signals] == ["只记录"]
    assert st.against_signals[0].kind is c.SignalKind.VOLUME_SURGE


def _position_now(now: datetime, **over: Any) -> c.PositionRecord:
    return pos(now, **over)


# ------------------------------------------------------------------ 过滤顺序（逻辑审查 #199）


def test_against_detected_before_allowed_kinds(now: datetime) -> None:
    # 卡偏空、只允许量能突增：瀑布反弹（多）不在名单里，也要发反向事件
    cd = h.card(now, bias="short", allowed_signals=["volume_surge"])
    liq = h.features(
        now,
        liq_long_usdt_1m=500_000.0,
        liq_ratio_1m=8.0,
        ret_5m=-0.015,
        cvd_30s_usdt=20_000.0,
        vol_ratio_1m=1.0,
    )
    t = RuleTrigger(FAST)
    assert t.check(liq, cd, now) == []
    [ev] = t.pop_against()
    assert ev.kind is c.SignalKind.LIQUIDATION_REBOUND and ev.strong


def test_against_detected_when_live_switch_hides_surge(now: datetime) -> None:
    off = FAST.model_copy(update={"volume_surge_live": False})
    t = RuleTrigger(off, live=True)
    assert t.check(h.features(now, **SURGE_LONG), short_card(now), now) == []
    assert len(t.pop_against()) == 1


def test_cleared_signals_still_report_against(now: datetime) -> None:
    """卡过期变只盯不开（信号清空、方向还在）的持仓币：瀑布反弹反向照样叫管家急查。"""
    cd = h.card(now, bias="short", allowed_signals=[])
    liq = h.features(
        now,
        liq_long_usdt_1m=500_000.0,
        liq_ratio_1m=8.0,
        ret_5m=-0.015,
        cvd_30s_usdt=20_000.0,
        vol_ratio_1m=1.0,
    )
    t = RuleTrigger(FAST)
    assert t.check(liq, cd, now) == []
    [ev] = t.pop_against()
    assert ev.kind is c.SignalKind.LIQUIDATION_REBOUND and ev.strong
    held = pos(now, side="short", entry_price=68000.0, stop_loss_price=68120.0)
    ev2, rechecks = desk().route(ev, [held], set(), now)
    assert [(p.position_id, one) for p, one in rechecks] == [("pos-1", True)]
    assert ev2.actions == ["keeper_recheck"]
    # 顺卡方向的信号仍然不开仓，面板写只看不做
    t2 = RuleTrigger(FAST)
    surge_short = h.features(now, vol_ratio_1m=5.0, taker_buy_ratio_1m=0.2)
    assert t2.check(surge_short, cd, now) == [] and t2.pop_against() == []
    assert "卡上是只看不做" in next(x for x in t2.detail_lines(now) if x.startswith("BTCUSDT"))


def test_no_trade_card_skipped(now: datetime) -> None:
    t = RuleTrigger(FAST)
    assert t.check(h.features(now, **SURGE_LONG), h.card(now, bias="no_trade"), now) == []
    assert t.pop_against() == []
