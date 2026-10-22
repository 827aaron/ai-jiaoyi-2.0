"""保镖：四层决定权、数量计算、故障即关闭、日亏熔断按曼谷 0 点清零。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from jiaoyi import contracts as c
from jiaoyi import interfaces as i
from jiaoyi.core.recorder import RoleRecorder
from jiaoyi.core.store import JsonlDecisionLog
from jiaoyi.risk.guard import LotRules, RiskGuardImpl
from tests.fastloop_helpers import FAST, RISK, Lots, account, card, features, jev, signal


def guard(lots: Lots | None = None, **fast: object) -> RiskGuardImpl:
    cfg = FAST.model_copy(update=fast) if fast else FAST
    return RiskGuardImpl(RISK, cfg, lots or Lots())


def test_satisfies_interface() -> None:
    assert isinstance(guard(), i.RiskGuard)


def test_happy_path_sizes_by_risk_and_caps(now: datetime) -> None:
    v = guard().review(signal(now), jev(), card(now), account(now), now)
    assert v.approved, v.reasons
    it = v.intent
    assert it is not None and it.approved_by == "risk_guard"
    # 风险 100U × M 档 0.75 = 75U；止损 1.5 ATR = 120 → 0.625 BTC ≈ 42500U，被作战卡 2000U 封顶
    assert it.quantity == pytest.approx(0.029)
    assert it.notional_usdt == pytest.approx(0.029 * 68000)
    assert it.notional_usdt <= 2000
    assert it.leverage == 3  # 作战卡 2–3 倍，硬上限 5
    assert it.stop_loss_pct == pytest.approx(120 / 68000 * 100)
    assert it.take_profit_pct == pytest.approx(it.stop_loss_pct * 1.5)
    assert it.lifeboat_stop_pct == pytest.approx(it.stop_loss_pct * 1.5)
    assert it.risk_usdt == pytest.approx(0.029 * 120)
    assert it.category == c.Category.MAJOR
    assert it.time_limit_s == FAST.time_limit_s


def test_risk_amount_binds_when_caps_are_loose(now: datetime) -> None:
    cd = card(now, notional_usdt={"min": 10, "max": 100_000})
    v = guard().review(
        signal(now, f=features(now, atr=4000.0)),
        jev(size={"S": 0.1, "M": 0.1, "L": 0.8}),
        cd,
        account(now, equity=10_000.0),
        now,
    )
    assert v.approved, v.reasons
    it = v.intent
    assert it is not None
    # L 档满额 100U，止损 1.5×4000=6000 → 0.01667 BTC ≈ 1133U（低于各上限），按 0.001 向下取整
    assert it.quantity == pytest.approx(0.016)
    assert it.risk_usdt <= 100.0


def test_jev_failure_rejects(now: datetime) -> None:
    v = guard().review(signal(now), jev(ok=False), card(now), account(now), now)
    assert not v.approved and v.intent is None
    assert any("拍板人不可用" in r for r in v.reasons)


@pytest.mark.parametrize(
    "over",
    [
        {"genuine": {"real": 0.4, "fake": 0.6}},
        {"take": {"take": 0.5, "skip": 0.5}},
        {"toxicity": {"low": 0.3, "high": 0.7}},
        {"direction": {"up": 0.3, "down": 0.5, "flat": 0.2}},
    ],
)
def test_below_thresholds_rejects(now: datetime, over: dict[str, dict[str, float]]) -> None:
    v = guard().review(signal(now), jev(**over), card(now), account(now), now)
    assert not v.approved


def test_missing_or_wrong_answers_reject(now: datetime) -> None:
    good = jev()
    missing = good.model_copy(update={"answers": [a for a in good.answers if a.id != "size"]})
    v = guard().review(signal(now), missing, card(now), account(now), now)
    assert not v.approved and any("size" in r for r in v.reasons)

    wrong = jev(size={"tiny": 0.5, "huge": 0.5})
    v = guard().review(signal(now), wrong, card(now), account(now), now)
    assert not v.approved and any("选项" in r for r in v.reasons)


def test_no_card_or_expired_card_rejects(now: datetime) -> None:
    assert not guard().review(signal(now), jev(), None, account(now), now).approved
    later = now + timedelta(hours=2)
    v = guard().review(signal(later), jev(), card(now), account(later), later)
    assert not v.approved and any("作战卡" in r for r in v.reasons)


def test_card_direction_and_signal_kind(now: datetime) -> None:
    short = signal(now, side=c.Side.SHORT)
    probs = {"direction": {"up": 0.2, "down": 0.6, "flat": 0.2}}
    v = guard().review(short, jev(**probs), card(now), account(now), now)
    assert not v.approved and any("相反" in r for r in v.reasons)

    surge = signal(now, kind=c.SignalKind.VOLUME_SURGE)
    v = guard().review(surge, jev(), card(now), account(now), now)
    assert not v.approved and any("不允许" in r for r in v.reasons)

    v = guard().review(signal(now), jev(), card(now, bias="no_trade"), account(now), now)
    assert not v.approved


def test_card_ranges_are_rechecked(now: datetime) -> None:
    v = guard().review(
        signal(now), jev(), card(now, stop_atr={"min": 1, "max": 9}), account(now), now
    )
    assert not v.approved and any("止损" in r for r in v.reasons)
    v = guard().review(
        signal(now), jev(), card(now, take_profit_r={"min": 0.6, "max": 3}), account(now), now
    )
    assert not v.approved and any("止盈" in r for r in v.reasons)


def test_card_leverage_above_hard_cap_rejects(now: datetime) -> None:
    v = guard().review(
        signal(now), jev(), card(now, leverage={"min": 6, "max": 8}), account(now), now
    )
    assert not v.approved and any("杠杆" in r for r in v.reasons)


def test_size_tier_capped_by_card(now: datetime) -> None:
    cd = card(now, size_tier_max="S", notional_usdt={"min": 10, "max": 100_000})
    v = guard().review(
        signal(now, f=features(now, atr=2000.0)),
        jev(size={"S": 0.1, "M": 0.1, "L": 0.8}),
        cd,
        account(now),
        now,
    )
    assert v.intent is not None
    assert v.intent.risk_usdt <= 100 * FAST.size_fraction_s + 1e-9


def test_account_level_rejections(now: datetime) -> None:
    g = guard()
    assert not g.review(signal(now), jev(), card(now), account(now, paused=True), now).approved
    full = account(now, open_positions=RISK.max_open_positions)
    assert not g.review(signal(now), jev(), card(now), full, now).approved


def test_exposure_caps_use_card_category(now: datetime) -> None:
    # major 组上限 2 倍权益 = 20000U，已经占满
    acct = account(now, open_notional_usdt=20_000.0, open_notional_by_category={"major": 20_000.0})
    v = guard().review(signal(now), jev(), card(now), acct, now)
    assert not v.approved and any("相关组" in r for r in v.reasons)
    # 同样的敞口算在 alt 组上就不挡 major
    acct = account(now, open_notional_usdt=5_000.0, open_notional_by_category={"alt": 5_000.0})
    assert guard().review(signal(now), jev(), card(now), acct, now).approved


def test_total_exposure_cap(now: datetime) -> None:
    acct = account(now, open_notional_usdt=29_900.0)
    v = guard().review(signal(now), jev(), card(now), acct, now)
    assert not v.approved  # 只剩 100U，低于作战卡下限 500U


def test_market_quality_rejections(now: datetime) -> None:
    wide = features(now)
    wide = wide.model_copy(update={"book": wide.book.model_copy(update={"spread_bps": 20.0})})  # type: ignore[union-attr]
    assert not guard().review(signal(now, f=wide), jev(), card(now), account(now), now).approved

    nobook = features(now).model_copy(update={"book": None})
    v = guard().review(signal(now, f=nobook), jev(), card(now), account(now), now)
    assert not v.approved and any("盘口" in r for r in v.reasons)

    late = now + timedelta(seconds=10)
    v = guard().review(signal(now), jev(), card(now), account(late), late)
    assert not v.approved and any("作废" in r for r in v.reasons)

    moved = signal(now).model_copy(update={"trigger_price": 67_900.0})
    v = guard().review(moved, jev(), card(now), account(now), now)
    assert not v.approved and any("滑点" in r for r in v.reasons)


def test_fee_floor(now: datetime) -> None:
    tiny_atr = features(now, atr=5.0)  # 止损 7.5 → 止盈约 1.7bp，不够付手续费
    v = guard().review(signal(now, f=tiny_atr), jev(), card(now), account(now), now)
    assert not v.approved and any("手续费" in r for r in v.reasons)


def test_lot_rules_missing_or_too_small(now: datetime) -> None:
    v = guard(Lots(None)).review(signal(now), jev(), card(now), account(now), now)
    assert not v.approved and any("精度" in r for r in v.reasons)
    v = guard(Lots(LotRules(1.0, 1.0, 5.0))).review(
        signal(now), jev(), card(now), account(now), now
    )
    assert not v.approved


def test_stop_moves_beyond_nearby_key_level(now: datetime) -> None:
    # 多单入场 68000，止损 1.5 ATR=120 → 67880；支撑 67860 在外侧 20 处 → 挪到 67860-8
    levels = [{"price": 67860.0, "kind": "support", "note": ""}]
    v = guard().review(signal(now), jev(), card(now, key_levels=levels), account(now), now)
    assert v.intent is not None
    assert v.intent.stop_loss_pct == pytest.approx((68000 - 67852) / 68000 * 100)


def test_daily_halt_latches_and_resets_at_bangkok_midnight() -> None:
    g = guard()
    # 曼谷 10-22 20:00 = UTC 13:00
    t = datetime(2026, 10, 22, 13, 0, tzinfo=UTC)
    down = account(t, equity=9_650.0, day_start_equity_usdt=10_000.0)  # -3.5%
    v = g.review(signal(t), jev(), card(t), down, t)
    assert not v.approved and any("熔断" in r for r in v.reasons)

    # 同一天权益回来了也不解锁
    t2 = t + timedelta(hours=2)
    back = account(t2, equity=10_000.0, day_start_equity_usdt=10_000.0)
    assert not g.review(signal(t2), jev(), card(t2), back, t2).approved

    # 曼谷 10-23 00:00 = UTC 10-22 17:00，跨天清零
    t3 = datetime(2026, 10, 22, 17, 0, 1, tzinfo=UTC)
    fresh = account(t3, equity=9_650.0, day_start_equity_usdt=9_650.0)
    v = g.review(signal(t3), jev(), card(t3), fresh, t3)
    assert v.approved, v.reasons


def test_daily_halt_uses_worse_of_two_day_starts() -> None:
    g = guard()
    t = datetime(2026, 10, 22, 3, 0, tzinfo=UTC)
    assert g.review(signal(t), jev(), card(t), account(t), t).approved
    # 账户这边的日初权益被错误重置，但保镖自己记着当天开始是 10000
    t2 = t + timedelta(hours=1)
    bad = account(t2, equity=9_600.0, day_start_equity_usdt=9_600.0)
    v = g.review(signal(t2), jev(), card(t2), bad, t2)
    assert not v.approved and any("熔断" in r for r in v.reasons)


def test_writes_decision_log(tmp_path: object, now: datetime) -> None:
    from pathlib import Path

    log = JsonlDecisionLog(Path(str(tmp_path)))
    g = RiskGuardImpl(RISK, FAST, Lots(), RoleRecorder(c.Role.RISK_GUARD, log))
    g.review(signal(now), jev(), card(now), account(now), now)
    g.review(signal(now), jev(ok=False), card(now), account(now), now)
    recs = log.read_day(now.date().isoformat())
    assert [r.outcome for r in recs] == [c.Outcome.PASS, c.Outcome.REJECT]
    assert recs[0].output["intent"]["approved_by"] == "risk_guard"
    assert all(r.role == c.Role.RISK_GUARD for r in recs)
