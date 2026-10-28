"""哨兵：规则触发、作战卡约束、去重冷却、时段窗口。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from jiaoyi import contracts as c
from jiaoyi import interfaces as i
from jiaoyi.core.recorder import RoleRecorder
from jiaoyi.core.store import JsonlDecisionLog
from jiaoyi.predictor.stub import StubPredictor
from jiaoyi.sentinel.rules import RuleTrigger, session_event
from tests.fastloop_helpers import FAST, card, features


def trig(**kw: object) -> RuleTrigger:
    return RuleTrigger(FAST, dedup_s=30, **kw)  # type: ignore[arg-type]


def test_interface() -> None:
    assert isinstance(trig(), i.Trigger)


def breakout(t: RuleTrigger, now: datetime, cd: c.BattleCard | None) -> list[c.CandidateSignal]:
    t.check(features(now, price=67990.0), cd, now)
    return t.check(features(now, price=68010.0, vol_ratio_1m=3.5), cd, now)


def test_volume_breakout_long(now: datetime) -> None:
    out = breakout(trig(predictor=StubPredictor()), now, card(now))
    assert len(out) == 1
    s = out[0]
    assert s.kind == c.SignalKind.VOLUME_BREAKOUT and s.side_hint == c.Side.LONG
    assert s.card_id == "card-20261022-0600" and "68000" in s.trigger_desc
    assert s.dedup_key == "BTCUSDT:volume_breakout:long" and s.forecast is None


def test_no_card_no_signal(now: datetime) -> None:
    assert breakout(trig(), now, None) == []
    assert breakout(trig(), now, card(now - timedelta(hours=3))) == []
    assert breakout(trig(), now, card(now, bias="no_trade")) == []


def test_card_bias_blocks_opposite_side(now: datetime) -> None:
    t = trig()
    cd = card(now)  # bias long
    t.check(features(now, price=68010.0), cd, now)
    assert t.check(features(now, price=67990.0, vol_ratio_1m=3.5), cd, now) == []
    cd = card(now, bias="both")
    t = trig()
    t.check(features(now, price=68010.0), cd, now)
    out = t.check(features(now, price=67990.0, vol_ratio_1m=3.5), cd, now)
    assert [s.side_hint for s in out] == [c.Side.SHORT]


def test_kind_must_be_allowed(now: datetime) -> None:
    f = features(now, vol_ratio_1m=5.0, taker_buy_ratio_1m=0.8)
    assert trig().check(f, card(now), now) == []  # 卡只允许突破和爆仓反弹
    out = trig().check(f, card(now, allowed_signals=["volume_surge"]), now)
    assert [s.kind for s in out] == [c.SignalKind.VOLUME_SURGE]


def test_dedup_cooldown(now: datetime) -> None:
    cd = card(now, allowed_signals=["volume_surge"])
    t = trig()
    f = features(now, vol_ratio_1m=5.0, taker_buy_ratio_1m=0.8)
    assert len(t.check(f, cd, now)) == 1
    assert t.check(f, cd, now + timedelta(seconds=29)) == []
    assert len(t.check(f, cd, now + timedelta(seconds=31))) == 1


def test_liquidation_rebound(now: datetime) -> None:
    f = features(
        now,
        liq_long_usdt_1m=500_000.0,
        liq_ratio_1m=8.0,
        ret_5m=-0.015,
        cvd_30s_usdt=20_000.0,
        vol_ratio_1m=1.0,
    )
    out = trig().check(f, card(now), now)
    assert [(s.kind, s.side_hint) for s in out] == [(c.SignalKind.LIQUIDATION_REBOUND, c.Side.LONG)]
    # 主动成交还没掉头 → 不出
    f2 = f.model_copy(update={"values": {**f.values, "cvd_30s_usdt": -1.0}})
    assert trig().check(f2, card(now), now) == []
    # 连续行情不满 30 分钟：30 分钟均值只有这几分钟，比值虚高
    f3 = f.model_copy(update={"values": {**f.values, "live_s": 1799.0}})
    assert trig().check(f3, card(now), now) == []


def test_crowded_reversal(now: datetime) -> None:
    cd = card(now, bias="both", allowed_signals=["crowded_reversal"])
    f = features(now, funding_rate=0.001, ret_1m=-0.001, taker_buy_ratio_1m=0.4, vol_ratio_1m=1.0)
    out = trig().check(f, cd, now)
    assert [(s.kind, s.side_hint) for s in out] == [(c.SignalKind.CROWDED_REVERSAL, c.Side.SHORT)]
    cold = f.model_copy(update={"values": {**f.values, "live_s": 59.0}})
    assert trig().check(cold, cd, now) == []


def test_session_windows() -> None:
    assert session_event(datetime(2026, 10, 22, 8, 3, tzinfo=UTC), 5) == "资金费结算 08:00 UTC"
    assert session_event(datetime(2026, 10, 22, 23, 57, tzinfo=UTC), 5) == "资金费结算 00:00 UTC"
    # 纽约 10 月是夏令时：9:30 = 13:30 UTC；11 月冬令时：14:30 UTC
    assert session_event(datetime(2026, 10, 22, 13, 32, tzinfo=UTC), 5) == "纽约开盘"
    assert session_event(datetime(2026, 11, 12, 14, 28, tzinfo=UTC), 5) == "纽约开盘"
    assert session_event(datetime(2026, 11, 12, 13, 30, tzinfo=UTC), 5) is None
    assert session_event(datetime(2026, 10, 24, 13, 30, tzinfo=UTC), 5) is None  # 周六
    assert session_event(datetime(2026, 10, 22, 11, 0, tzinfo=UTC), 5) is None


def test_session_event_signal() -> None:
    now = datetime(2026, 10, 22, 13, 31, tzinfo=UTC)
    cd = card(now, allowed_signals=["session_event"])
    f = features(now, vol_ratio_1m=2.5, ret_5m=0.004, realized_vol_5m=0.0004)
    out = trig().check(f, cd, now)
    assert [(s.kind, s.side_hint) for s in out] == [(c.SignalKind.SESSION_EVENT, c.Side.LONG)]
    cold = f.model_copy(update={"values": {**f.values, "live_s": 299.0}})
    assert trig().check(cold, cd, now) == []


def test_logs_each_signal(tmp_path: Path, now: datetime) -> None:
    log = JsonlDecisionLog(tmp_path)
    t = trig(recorder=RoleRecorder(c.Role.SENTINEL, log))
    breakout(t, now, card(now))
    recs = log.read_day(now.date().isoformat())
    assert len(recs) == 1 and recs[0].role == c.Role.SENTINEL and "放量突破" in recs[0].summary


def test_volume_surge_live_switch(now: datetime) -> None:
    f = features(now, vol_ratio_1m=5.0, taker_buy_ratio_1m=0.8)
    cd = card(now, allowed_signals=["volume_surge"])
    assert c.FastLoopConfig.model_fields["volume_surge_live"].default is True  # 默认开
    on = RuleTrigger(FAST.model_copy(update={"volume_surge_live": True}), live=True)
    assert [s.kind for s in on.check(f, cd, now)] == [c.SignalKind.VOLUME_SURGE]
    off = RuleTrigger(FAST.model_copy(update={"volume_surge_live": False}), live=True)
    assert off.check(f, cd, now) == []
    paper = RuleTrigger(FAST.model_copy(update={"volume_surge_live": False}))
    assert [s.kind for s in paper.check(f, cd, now)] == [c.SignalKind.VOLUME_SURGE]


def test_detail_lines_say_why_no_signal(now: datetime) -> None:
    t = trig()
    cd = card(now, bias="short", allowed_signals=["volume_surge"])
    # 量 2.5 倍、主动买 60%：离量能突增（5 倍）还差 2.5 倍
    assert t.check(features(now, vol_ratio_1m=2.5, taker_buy_ratio_1m=0.6), cd, now) == []
    lines = t.detail_lines(now)
    btc = next(x for x in lines if x.startswith("BTCUSDT"))
    assert "量 2.5 倍" in btc and "主动买 60%" in btc and "还差 2.5 倍" in btc
    # 够量能突增但方向是多、卡上只做空 → 记跳过原因
    later = now + timedelta(seconds=5)
    t.check(features(later, vol_ratio_1m=5.0, taker_buy_ratio_1m=0.8), cd, later)
    btc = next(x for x in t.detail_lines(later) if x.startswith("BTCUSDT"))
    assert "量能突增（多）：和卡上方向相反" in btc and "1 小时内最高 5.0 倍" in btc
    assert "只做空 1" in t.brief(later)


def test_detail_lines_flag_card_symbols_without_trades(now: datetime) -> None:
    t = trig()
    cd = card(now)
    t.check(features(now), cd, now)
    others = [s.symbol for s in cd.symbols if s.symbol != "BTCUSDT"]
    lines = t.detail_lines(now)
    for sym in others:
        assert any(x.startswith(f"{sym} 没收到行情") for x in lines)
    stale = now + timedelta(minutes=3)  # BTC 也 3 分钟没来成交
    assert any(x.startswith("BTCUSDT 没收到行情（最后一笔") for x in t.detail_lines(stale))


def test_detail_lines_warmup_keeps_watch_only_reason(now: datetime) -> None:
    t = trig()
    t.check(features(now, live_s=10.0), card(now, allowed_signals=[]), now)
    btc = next(x for x in t.detail_lines(now) if x.startswith("BTCUSDT"))
    assert "卡上是只看不做" in btc and "行情预热中" not in btc
    t.check(features(now, live_s=10.0), card(now), now)
    btc = next(x for x in t.detail_lines(now) if x.startswith("BTCUSDT"))
    assert "行情预热中：实时行情才 10 秒" in btc


def test_detail_lines_cooldown_reason(now: datetime) -> None:
    t = trig()
    cd = card(now, allowed_signals=["volume_surge"])
    f = features(now, vol_ratio_1m=5.0, taker_buy_ratio_1m=0.8)
    assert len(t.check(f, cd, now)) == 1
    t.check(f, cd, now + timedelta(seconds=1))
    btc = next(x for x in t.detail_lines(now) if x.startswith("BTCUSDT"))
    assert "冷却中" in btc


def test_app_heartbeat_picks_up_detail() -> None:
    from jiaoyi.app import _SentinelDetail

    assert isinstance(trig(), _SentinelDetail)


def test_session_cme_sunday_open() -> None:
    # 芝加哥周日 17:00：夏令时 22:00 UTC，冬令时 23:00 UTC
    summer = datetime(2026, 10, 25, 22, 3, tzinfo=UTC)
    winter = datetime(2026, 11, 8, 22, 57, tzinfo=UTC)
    assert session_event(summer, 5) is None  # 没开就不看
    assert session_event(summer, 5, cme=True) == "CME 周日开盘"
    assert session_event(winter, 5, cme=True) == "CME 周日开盘"
    assert session_event(datetime(2026, 11, 8, 22, 3, tzinfo=UTC), 5, cme=True) is None
    assert session_event(datetime(2026, 10, 26, 22, 0, tzinfo=UTC), 5, cme=True) is None  # 周一
    # 原有时段不受影响
    assert session_event(datetime(2026, 10, 22, 8, 3, tzinfo=UTC), 5, cme=True) == (
        "资金费结算 08:00 UTC"
    )


def test_session_cme_switches() -> None:
    on = FAST.model_copy(update={"session_cme_open": True})
    assert c.FastLoopConfig.model_fields["session_cme_open_live"].default is False
    assert RuleTrigger(on)._cme
    assert not RuleTrigger(on, live=True)._cme  # 实盘 / 测试网默认不出
    assert RuleTrigger(on.model_copy(update={"session_cme_open_live": True}), live=True)._cme
    assert not RuleTrigger(FAST)._cme


def test_session_cme_tiers() -> None:
    from jiaoyi.core.profiles import HOLD

    on = {h for h, p in HOLD.items() if p["fast_loop"]["session_cme_open"]}
    assert on == {"30m", "1h", "2h"}


def test_session_cme_signal_paper_only() -> None:
    now = datetime(2026, 10, 25, 22, 2, tzinfo=UTC)
    cd = card(now, allowed_signals=["session_event"])
    f = features(now, vol_ratio_1m=2.5, ret_5m=0.004, realized_vol_5m=0.0004)
    on = FAST.model_copy(update={"session_cme_open": True})
    out = RuleTrigger(on).check(f, cd, now)
    assert [(s.kind, s.side_hint) for s in out] == [(c.SignalKind.SESSION_EVENT, c.Side.LONG)]
    assert "CME 周日开盘" in out[0].trigger_desc
    assert RuleTrigger(on, live=True).check(f, cd, now) == []
