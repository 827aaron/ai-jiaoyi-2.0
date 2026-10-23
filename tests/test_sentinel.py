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


def test_crowded_reversal(now: datetime) -> None:
    cd = card(now, bias="both", allowed_signals=["crowded_reversal"])
    f = features(now, funding_rate=0.001, ret_1m=-0.001, taker_buy_ratio_1m=0.4, vol_ratio_1m=1.0)
    out = trig().check(f, cd, now)
    assert [(s.kind, s.side_hint) for s in out] == [(c.SignalKind.CROWDED_REVERSAL, c.Side.SHORT)]


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


def test_logs_each_signal(tmp_path: Path, now: datetime) -> None:
    log = JsonlDecisionLog(tmp_path)
    t = trig(recorder=RoleRecorder(c.Role.SENTINEL, log))
    breakout(t, now, card(now))
    recs = log.read_day(now.date().isoformat())
    assert len(recs) == 1 and recs[0].role == c.Role.SENTINEL and "放量突破" in recs[0].summary


def test_volume_surge_off_outside_paper_by_default(now: datetime) -> None:
    f = features(now, vol_ratio_1m=5.0, taker_buy_ratio_1m=0.8)
    cd = card(now, allowed_signals=["volume_surge"])
    assert trig(live=True).check(f, cd, now) == []
    on = RuleTrigger(FAST.model_copy(update={"volume_surge_live": True}), live=True)
    assert [s.kind for s in on.check(f, cd, now)] == [c.SignalKind.VOLUME_SURGE]
