"""风控员：四层决定权、数量计算、故障即关闭、日亏熔断按曼谷 0 点清零。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from jiaoyi import contracts as c
from jiaoyi import interfaces as i
from jiaoyi.core.recorder import RoleRecorder
from jiaoyi.core.store import JsonlDecisionLog
from jiaoyi.risk.guard import LeverageBracket, LotRules, RiskGuardImpl
from tests.fastloop_helpers import FAST, RISK, Lots, account, card, features, jev, signal

_SAME = c.FeatureSnapshot.__new__(c.FeatureSnapshot)  # 哨兵值：没传 market 时用信号自带的快照


class _Guard(RiskGuardImpl):
    """测试方便：不传 ``market`` 时当作“拍板期间行情没动”。要测行情变化就显式传。"""

    def review(  # type: ignore[override]
        self,
        signal: c.CandidateSignal,
        jev: c.JevResponse,
        card: c.BattleCard | None,
        account: c.AccountState,
        now: datetime,
        *,
        market: c.FeatureSnapshot | None = _SAME,
    ) -> c.RiskVerdict:
        m = signal.features if market is _SAME else market
        return super().review(signal, jev, card, account, now, market=m)


def guard(lots: Lots | None = None, **fast: object) -> _Guard:
    cfg = FAST.model_copy(update=fast) if fast else FAST
    return _Guard(RISK, cfg, lots or Lots())


def test_satisfies_interface() -> None:
    assert isinstance(guard(), i.RiskGuard)


def test_happy_path_sizes_by_risk_and_caps(now: datetime) -> None:
    v = guard(card_caps_binding=True).review(signal(now), jev(), card(now), account(now), now)
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
    v = guard(gate_questions="six").review(signal(now), jev(**over), card(now), account(now), now)
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
    g = guard(card_range_clamp=False)
    v = g.review(signal(now), jev(), card(now, stop_atr={"min": 1, "max": 9}), account(now), now)
    assert not v.approved and any("止损" in r for r in v.reasons)
    v = g.review(
        signal(now), jev(), card(now, take_profit_r={"min": 0.6, "max": 3}), account(now), now
    )
    assert not v.approved and any("止盈" in r for r in v.reasons)


def test_card_ranges_are_clamped_to_tier(now: datetime) -> None:
    """默认取交集：越界的那截不要，剩下的照用；交集为空才不做。"""
    lo, hi = FAST.min_stop_atr, FAST.max_stop_atr
    cd = card(now, stop_atr={"min": lo, "max": hi + 5}, take_profit_r={"min": 0.6, "max": 3})
    v = guard().review(signal(now), jev(), cd, account(now), now)
    assert v.approved, v.reasons
    assert v.intent is not None
    assert v.intent.stop_loss_pct == pytest.approx((lo + hi) / 2 * 80 / 68000 * 100)  # mid 档
    assert v.intent.take_profit_pct == pytest.approx(v.intent.stop_loss_pct * FAST.min_tp_r)
    out = card(now, stop_atr={"min": hi + 1, "max": hi + 2})
    v = guard().review(signal(now), jev(), out, account(now), now)
    assert not v.approved and any("没有交集" in r for r in v.reasons)


def test_card_leverage_above_hard_cap_rejects(now: datetime) -> None:
    v = guard(card_caps_binding=True).review(
        signal(now), jev(), card(now, leverage={"min": 6, "max": 8}), account(now), now
    )
    assert not v.approved and any("杠杆" in r for r in v.reasons)


def test_card_caps_are_advice_by_default(now: datetime) -> None:
    """默认卡上的名义 / 杠杆只当建议：仓位按风险和硬上限算，杠杆用硬上限，建议写进 detail。"""
    cd = card(now, leverage={"min": 6, "max": 8}, notional_usdt={"min": 50_000, "max": 60_000})
    v = guard().review(signal(now), jev(), cd, account(now), now)
    assert v.approved, v.reasons
    assert v.intent is not None and v.intent.leverage == RISK.max_leverage
    cd = card(now)
    v = guard().review(signal(now), jev(), cd, account(now), now)
    assert v.intent is not None
    cs = cd.for_symbol(v.intent.symbol)
    assert cs is not None and v.intent.notional_usdt > cs.notional_usdt.max  # 不再被卡封顶


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


def test_symbol_already_held_is_rejected(now: datetime) -> None:
    sig = signal(now)
    held = account(now, open_positions=1, open_symbols=[sig.symbol])
    v = guard().review(sig, jev(), card(now), held, now)
    assert not v.approved and any("已有在管持仓" in r for r in v.reasons)
    other = account(now, open_positions=1, open_symbols=["NOTHELDUSDT"])
    assert guard().review(sig, jev(), card(now), other, now).approved


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
    v = guard(card_caps_binding=True).review(signal(now), jev(), card(now), acct, now)
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


def test_market_max_qty_caps_quantity(now: datetime) -> None:
    lot = LotRules(0.001, 0.001, 5.0, max_qty=0.0105)  # 不封顶时 0.029
    v = guard(Lots(lot)).review(signal(now), jev(), card(now), account(now), now)
    assert v.approved, v.reasons
    assert v.intent is not None and v.intent.quantity == pytest.approx(0.01)  # 向下取整到步长
    assert v.intent.risk_usdt == pytest.approx(0.01 * 120)  # 风险按封顶后的数量重算


def test_max_qty_cap_below_minimum_rejects(now: datetime) -> None:
    lot = LotRules(0.001, 0.005, 5.0, max_qty=0.002)
    v = guard(Lots(lot)).review(signal(now), jev(), card(now), account(now), now)
    assert not v.approved and any("最小下单量" in r for r in v.reasons)


def test_stop_moves_beyond_nearby_key_level(now: datetime) -> None:
    # 多单入场 68000，止损 1.5 ATR=120 → 67880；支撑 67860 在外侧 20 处 → 挪到 67860-8
    levels = [{"price": 67860.0, "kind": "support", "note": ""}]
    c = card(now, key_levels=levels)
    v = guard(stop_level_snap=True).review(signal(now), jev(), c, account(now), now)
    assert v.intent is not None
    assert v.intent.stop_loss_pct == pytest.approx((68000 - 67852) / 68000 * 100)
    # 默认不贴关键位：止损就是 1.5 ATR
    v = guard().review(signal(now), jev(), c, account(now), now)
    assert v.intent is not None
    assert v.intent.stop_loss_pct == pytest.approx(120 / 68000 * 100)


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
    # 账户这边的日初权益被错误重置，但风控员自己记着当天开始是 10000
    t2 = t + timedelta(hours=1)
    bad = account(t2, equity=9_600.0, day_start_equity_usdt=9_600.0)
    v = g.review(signal(t2), jev(), card(t2), bad, t2)
    assert not v.approved and any("熔断" in r for r in v.reasons)


def test_writes_decision_log(tmp_path: object, now: datetime) -> None:
    from pathlib import Path

    log = JsonlDecisionLog(Path(str(tmp_path)))
    g = _Guard(RISK, FAST, Lots(), RoleRecorder(c.Role.RISK_GUARD, log))
    g.review(signal(now), jev(), card(now), account(now), now)
    g.review(signal(now), jev(ok=False), card(now), account(now), now)
    recs = log.read_day(now.date().isoformat())
    assert [r.outcome for r in recs] == [c.Outcome.PASS, c.Outcome.REJECT]
    # 验收脚本约定：放行记录的 output 顶层就是 OrderIntent
    assert recs[0].output["approved_by"] == "risk_guard" and recs[0].output["intent_id"]
    assert recs[0].signal_id == "sig-test-1"
    assert recs[1].output["reasons"]
    assert all(r.role == c.Role.RISK_GUARD for r in recs)


def test_daily_halt_survives_restart(tmp_path: Path) -> None:
    t = datetime(2026, 10, 22, 13, 0, tzinfo=UTC)  # 曼谷 20:00
    g1 = _Guard(RISK, FAST, Lots(), state_dir=tmp_path)
    down = account(t, equity=9_650.0, day_start_equity_usdt=10_000.0)
    assert not g1.review(signal(t), jev(), card(t), down, t).approved

    # 进程重启：新风控员读回状态，同一天权益回到线内也照样锁住
    t2 = t + timedelta(hours=1)
    g2 = _Guard(RISK, FAST, Lots(), state_dir=tmp_path)
    back = account(t2, equity=10_000.0, day_start_equity_usdt=10_000.0)
    v = g2.review(signal(t2), jev(), card(t2), back, t2)
    assert not v.approved and any("熔断" in r for r in v.reasons)

    # 曼谷第二天 0 点后重启，解锁
    t3 = datetime(2026, 10, 22, 17, 0, 1, tzinfo=UTC)
    g3 = _Guard(RISK, FAST, Lots(), state_dir=tmp_path)
    assert g3.review(signal(t3), jev(), card(t3), account(t3), t3).approved


def test_corrupt_breaker_state_fails_closed(tmp_path: Path, now: datetime) -> None:
    (tmp_path / "risk_state.json").write_text("{not json", encoding="utf-8")
    g = _Guard(RISK, FAST, Lots(), state_dir=tmp_path)
    v = g.review(signal(now), jev(), card(now), account(now), now)
    assert not v.approved and any("熔断" in r for r in v.reasons)


# ---------------------------------------------------------------- 逻辑审查 I1 / I2
def test_slippage_and_spread_use_fresh_market(now: datetime) -> None:
    sig = signal(now)
    later = now + timedelta(milliseconds=800)
    # 拍板期间价格跑了 30bp（上限见 RISK.max_slippage_bps）
    moved = features(later, price=sig.trigger_price * (1 + (RISK.max_slippage_bps + 5) / 1e4))
    v = guard().review(sig, jev(), card(now), account(later), later, market=moved)
    assert not v.approved and any("滑点" in r for r in v.reasons)
    # 价差变宽
    fresh = features(later)
    wide = fresh.model_copy(
        update={"book": fresh.book.model_copy(update={"spread_bps": RISK.max_spread_bps + 1})}  # type: ignore[union-attr]
    )
    v = guard().review(sig, jev(), card(now), account(later), later, market=wide)
    assert not v.approved and any("价差" in r for r in v.reasons)
    # 没有最新行情、拿错币、行情太旧：都拒
    v = guard().review(sig, jev(), card(now), account(later), later, market=None)
    assert not v.approved and any("最新行情" in r for r in v.reasons)
    eth = fresh.model_copy(update={"symbol": "ETHUSDT"})
    assert not guard().review(sig, jev(), card(now), account(later), later, market=eth).approved
    stale = features(now - timedelta(seconds=FAST.signal_max_age_s + 1))
    assert not guard().review(sig, jev(), card(now), account(later), later, market=stale).approved
    # 行情没怎么动：放行，入场价按最新价
    ok = features(later, price=sig.trigger_price * 1.0001)
    v = guard().review(sig, jev(), card(now), account(later), later, market=ok)
    assert v.approved, v.reasons


def test_card_refreshed_during_jev_is_rejected(now: datetime) -> None:
    cd = card(now)
    newer = cd.model_copy(update={"version": cd.version + 1})
    v = guard().review(signal(now), jev(), newer, account(now), now)
    assert not v.approved and any("作战卡" in r and "已换成" in r for r in v.reasons)
    no_version = signal(now).model_copy(update={"card_version": None})
    assert not guard().review(no_version, jev(), cd, account(now), now).approved
    assert guard().review(signal(now), jev(), cd, account(now), now).approved


# ---------------------------------------------------------------- T1：止损按 15 分钟 ATR
def test_guard_uses_configured_atr_timeframe(now: datetime) -> None:
    f15 = features(now, atr=150.0)
    assert f15.atr_period_min == 15 == FAST.atr_period_min
    v = guard().review(signal(now, f=f15), jev(), card(now), account(now), now)
    assert v.approved, v.reasons
    assert v.intent is not None
    # 止损距离 = 作战卡倍数 × 15 分钟 ATR（不是 1 分钟 ATR）
    lo, hi = card(now).symbols[0].stop_atr.min, card(now).symbols[0].stop_atr.max
    dist = v.intent.stop_loss_pct / 100.0 * f15.last_price
    assert lo * 150.0 - 1e-6 <= dist <= hi * 150.0 + 80.0  # 关键位外侧最多再挪半个 ATR 左右
    # 快照是别的周期：拒绝（接线错了，止损会差好几倍）
    f1 = f15.model_copy(update={"atr_period_min": 1})
    v = guard().review(signal(now, f=f1), jev(), card(now), account(now), now)
    assert not v.approved and any("ATR 是 1 分钟口径" in r for r in v.reasons)


def test_stop_atr_floored_by_7d_median(now: datetime) -> None:
    """周末 ATR 只有平时一半：止损 ATR 至少按 7 天中位数 × atr_floor_7d 算。"""
    quiet = features(now, atr=80.0).model_copy(update={"atr_median_7d": 200.0})
    v = guard(atr_floor_7d=0.7).review(signal(now, f=quiet), jev(), card(now), account(now), now)
    assert v.approved and v.intent is not None, v.reasons
    floored = v.intent.stop_loss_pct
    v2 = guard(atr_floor_7d=0.0).review(signal(now, f=quiet), jev(), card(now), account(now), now)
    assert v2.intent is not None and v2.intent.stop_loss_pct < floored  # 关掉就按当前 ATR
    busy = features(now, atr=150.0).model_copy(update={"atr_median_7d": 100.0})
    v3 = guard(atr_floor_7d=0.7).review(signal(now, f=busy), jev(), card(now), account(now), now)
    v4 = guard(atr_floor_7d=0.0).review(signal(now, f=busy), jev(), card(now), account(now), now)
    assert v3.intent is not None and v4.intent is not None
    assert v3.intent.stop_loss_pct == v4.intent.stop_loss_pct  # 当前 ATR 更大时不动


def test_min_stop_atr_rejects_too_tight_card(now: datetime) -> None:
    tight = card(now, stop_atr={"min": 0.2, "max": 0.4})
    v = guard().review(signal(now), jev(), tight, account(now), now)
    assert not v.approved and any("止损" in r and "ATR 超出" in r for r in v.reasons)
    v = guard(min_stop_atr=0.1).review(signal(now), jev(), tight, account(now), now)
    assert not any("ATR 超出" in r for r in v.reasons)


def test_atr_config_validation() -> None:
    assert c.FastLoopConfig().atr_period_min == 15
    with pytest.raises(ValueError):
        c.FastLoopConfig(atr_period_min=7)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="min_stop_atr"):
        c.FastLoopConfig(min_stop_atr=4.0, max_stop_atr=4.0)


# ---------------------------------------------------------------- I3：总持仓风险上限
def _new_lifeboat_risk(now: datetime) -> float:
    v = guard().review(signal(now), jev(), card(now), account(now), now)
    assert v.intent is not None
    return v.intent.notional_usdt * v.intent.lifeboat_stop_pct / 100.0


def test_total_lifeboat_risk_within_day_budget(now: datetime) -> None:
    new = _new_lifeboat_risk(now)
    budget = RISK.daily_loss_halt_pct / 100.0 * 10_000.0
    # 刚好等于额度：放行
    ok = account(now, open_risk_usdt=budget - new)
    assert guard().review(signal(now), jev(), card(now), ok, now).approved
    # 多 1U：拒绝，原因写明三个数
    over = account(now, open_risk_usdt=budget - new + 1.0)
    v = guard().review(signal(now), jev(), card(now), over, now)
    assert not v.approved
    assert any("总风险" in r and "超过当日剩余额度" in r for r in v.reasons), v.reasons


def test_realized_loss_shrinks_budget_but_profit_does_not_grow_it(now: datetime) -> None:
    new = _new_lifeboat_risk(now)
    budget = RISK.daily_loss_halt_pct / 100.0 * 10_000.0
    acct = account(now, open_risk_usdt=budget - new - 50.0)
    assert guard().review(signal(now), jev(), card(now), acct, now).approved
    lost = acct.model_copy(update={"day_realized_pnl_usdt": -60.0})
    assert not guard().review(signal(now), jev(), card(now), lost, now).approved
    won = account(now, open_risk_usdt=budget - new + 1.0, day_realized_pnl_usdt=500.0)
    assert not guard().review(signal(now), jev(), card(now), won, now).approved


def test_stop_must_clear_fee_multiple(now: datetime) -> None:
    taker = RISK.taker_fee_bps
    bps = 120 / 68000 * 1e4  # 示例卡止损 1.5 ATR = 120
    ok = guard(min_stop_fee_multiple=bps / (2 * taker) - 0.1)
    assert ok.review(signal(now), jev(), card(now), account(now), now).approved
    tight = guard(min_stop_fee_multiple=bps / (2 * taker) + 0.1, stop_floor_widen=False)
    v = tight.review(signal(now), jev(), card(now), account(now), now)
    assert not v.approved and any("离手续费太近" in r for r in v.reasons)
    assert guard().review(signal(now), jev(), card(now), account(now), now).approved  # 默认 0 不查


def test_too_tight_stop_is_widened_and_resized(tmp_path: Path, now: datetime) -> None:
    """止损离手续费太近（默认开 stop_floor_widen）：拉宽到下限。

    风险金额不变、张数变少，止盈和救生艇按新止损算。"""
    taker = RISK.taker_fee_bps
    bps = 120 / 68000 * 1e4  # 示例卡止损 1.5 ATR = 120 ≈ 17.6bp
    mult = 4 * bps / (2 * taker)  # 下限 = 原止损的 4 倍（原仓位被敞口封顶，拉宽后不封顶）
    cd = card(now, notional_usdt={"min": 10, "max": 100_000})
    base = guard().review(signal(now), jev(), cd, account(now), now)
    log = JsonlDecisionLog(tmp_path)
    cfg = FAST.model_copy(update={"min_stop_fee_multiple": mult})
    g = _Guard(RISK, cfg, Lots(), RoleRecorder(c.Role.RISK_GUARD, log))
    v = g.review(signal(now), jev(), cd, account(now), now)
    assert base.approved and v.approved, v.reasons
    a, b = base.intent, v.intent
    assert a is not None and b is not None
    assert b.stop_loss_pct == pytest.approx(4 * a.stop_loss_pct)
    assert b.take_profit_pct == pytest.approx(b.stop_loss_pct * 1.5)
    assert b.lifeboat_stop_pct == pytest.approx(b.stop_loss_pct * 1.5)
    # 风险金额照旧：1% × M 档 0.75 = 75U；止损 4 × 120 = 480 → 0.156 张，向下取整
    assert b.quantity == pytest.approx(0.156)
    assert b.risk_usdt == pytest.approx(75, rel=0.01) and b.quantity < a.quantity
    [rec] = log.read_day(now.date().isoformat())
    assert "拉宽到" in rec.output["detail"]["stop_widened"]
    assert "仓位按新止损重算" in rec.output["detail"]["stop_widened"]


def test_funding_against_us_rejects_only_the_paying_side(now: datetime) -> None:
    g = guard(funding_against_max_bp=5.0)
    paying = signal(now, f=features(now, funding_rate=0.0008))  # 多头要付 8bp
    v = g.review(paying, jev(), card(now), account(now), now)
    assert not v.approved and any("资金费" in r for r in v.reasons)
    getting = signal(now, f=features(now, funding_rate=-0.0008))  # 多头反而收钱
    assert g.review(getting, jev(), card(now), account(now), now).approved
    unknown = signal(now, f=features(now))
    assert g.review(unknown, jev(), card(now), account(now), now).approved  # 没费率不拦


# ---------------------------------------------------------------- 止损 ATR = √(短 × 长) × 时段系数
def test_stop_atr_geometric_mean_and_session_factor() -> None:
    from jiaoyi.risk.guard import session_factor, stop_atr

    wd = [1.0] * 24
    wd[21] = 1.5  # 工作日曼谷 21 点
    we = [0.8] * 24
    fast = FAST.model_copy(
        update={
            "atr_period_min": 5,
            "atr_long_period_min": 60,
            "session_factor": True,
            "session_factor_weekday": wd,
            "session_factor_weekend": we,
        }
    )
    f = features(datetime(2026, 10, 22, 14, tzinfo=UTC), atr=40.0).model_copy(
        update={"atr_period_min": 5, "atr_long": 160.0, "atr_long_period_min": 60}
    )
    thu_21 = datetime(2026, 10, 22, 14, tzinfo=UTC)  # 周四 UTC 14 点 = 曼谷 21 点
    detail: dict[str, float | str] = {}
    assert stop_atr(f, fast, thu_21, detail) == pytest.approx(80.0 * 1.5)  # √(40×160)=80
    assert detail["atr_short"] == 40.0 and detail["atr_long"] == 160.0
    assert detail["session_factor"] == 1.5
    # 周五 UTC 18 点 = 曼谷周六 1 点：按曼谷日期算周末
    assert session_factor(fast, datetime(2026, 10, 23, 18, tzinfo=UTC)) == 0.8
    # 周日 UTC 17:59 = 曼谷周一 0:59：工作日
    assert session_factor(fast, datetime(2026, 10, 25, 17, 59, tzinfo=UTC)) == 1.0
    # 关掉时段系数 = 1；长周期没攒够 → 先只用短周期
    off = fast.model_copy(update={"session_factor": False})
    cold = f.model_copy(update={"atr_long": None})
    detail = {}
    assert stop_atr(cold, off, thu_21, detail) == pytest.approx(40.0)
    assert detail["session_factor"] == 1.0 and detail["atr_long"] == "未就绪，只用短周期"
    # 没配长周期：只用短周期
    assert stop_atr(f, FAST, thu_21, {}) == pytest.approx(40.0)


def test_guard_sizes_stop_with_combined_atr(now: datetime) -> None:
    f = features(now, atr=50.0).model_copy(update={"atr_long": 200.0, "atr_long_period_min": 60})
    v = guard(atr_long_period_min=60).review(signal(now, f=f), jev(), card(now), account(now), now)
    assert v.approved and v.intent is not None, v.reasons
    # 作战卡中档 1.5 倍 × √(50×200)=100 → 止损 150
    assert v.intent.stop_loss_pct == pytest.approx(150 / 68000 * 100)


def test_session_factor_table_validated() -> None:
    with pytest.raises(ValueError):
        FAST.model_validate({**FAST.model_dump(), "session_factor_weekday": [1.0] * 23})
    with pytest.raises(ValueError):
        FAST.model_validate({**FAST.model_dump(), "atr_long_period_min": 15})  # 不比短周期长


# ---------------------------------------------------------------- 拍板放弃 vs 风控员拒绝
def test_gate_only_rejection_tagged_as_gate_skip(tmp_path: Path, now: datetime) -> None:
    """只因为拍板人概率没过门槛：记成「拍板放弃」（detail.by = gate）。

    夹了风控员自己的规则就还是拒绝。
    """
    log = JsonlDecisionLog(tmp_path)
    six = FAST.model_copy(update={"gate_questions": "six"})
    g = _Guard(RISK, six, Lots(), RoleRecorder(c.Role.RISK_GUARD, log))
    skip = jev(take={"take": 0.3, "skip": 0.7}, toxicity={"low": 0.4, "high": 0.6})
    assert not g.review(signal(now), skip, card(now), account(now), now).approved
    busy = account(now, open_positions=RISK.max_open_positions)
    assert not g.review(signal(now), skip, card(now), busy, now).approved
    a, b = log.read_day(now.date().isoformat())
    assert a.output["detail"]["by"] == "gate" and "拍板放弃" in a.summary and "共 2 条" in a.summary
    assert "by" not in b.output["detail"] and "拒绝" in b.summary
    assert a.output["detail"]["since_trigger_ms"] >= 0


def test_gate_record_only_lets_threshold_misses_through(tmp_path: Path, now: datetime) -> None:
    """paper「拍板只记不拦」：门槛没过照样放行并记下；有毒 ≥ 0.8 还是拦；回答不全永远拦。"""
    log = JsonlDecisionLog(tmp_path)
    g = _Guard(RISK, FAST, Lots(), RoleRecorder(c.Role.RISK_GUARD, log), gate_record_only=True)
    skip = jev(take={"take": 0.3, "skip": 0.7}, toxicity={"low": 0.4, "high": 0.6})
    v = g.review(signal(now), skip, card(now), account(now), now)
    assert v.approved, v.reasons
    [rec] = log.read_day(now.date().isoformat())
    assert "做" in rec.output["detail"]["gate_would_block"] and "拍板本会拦" in rec.summary
    toxic = jev(toxicity={"low": 0.15, "high": 0.85})
    v = g.review(signal(now), toxic, card(now), account(now), now)
    assert not v.approved and any("0.8" in r for r in v.reasons)
    broken = jev()
    broken = broken.model_copy(update={"answers": broken.answers[:-1]})
    assert not g.review(signal(now), broken, card(now), account(now), now).approved


def test_slippage_only_blocks_adverse_direction(now: datetime) -> None:
    """做多：价格比触发时高才算滑点；跌了（更便宜）不拦。"""
    px = 68_000.0 * (1 + (RISK.max_slippage_bps + 5) / 1e4)
    worse = features(now, atr=80.0).model_copy(update={"last_price": px})
    v = guard().review(signal(now), jev(), card(now), account(now), now, market=worse)
    assert not v.approved and any("不利方向" in r for r in v.reasons)
    px = 68_000.0 * (1 - (RISK.max_slippage_bps + 5) / 1e4)
    better = features(now, atr=80.0).model_copy(update={"last_price": px})
    v = guard().review(signal(now), jev(), card(now), account(now), now, market=better)
    assert not any("滑点" in r for r in v.reasons), v.reasons


def test_slippage_and_spread_limits_scale_with_stop(now: datetime) -> None:
    """不写固定 bp 时只按止损比例：滑点 ≤ 1/4 止损，价差 ≤ 1/10 止损。"""
    risk = RISK.model_copy(update={"max_spread_bps": None, "max_slippage_bps": None})
    g = RiskGuardImpl(risk, FAST, Lots())
    stop_bps = 1.5 * 800 / 68000 * 1e4  # mid 1.5 ATR，ATR 800 → 约 176bp
    ok = features(now, atr=800.0, price=68000 * (1 + 0.2 * stop_bps / 1e4))
    sig = signal(now, f=features(now, atr=800.0))
    big = card(now, notional_usdt={"min": 10, "max": 1_000_000})
    v = g.review(sig, jev(), big, account(now), now, market=ok)
    assert not any("滑点" in r for r in v.reasons), v.reasons
    bad = features(now, atr=800.0, price=68000 * (1 + 0.3 * stop_bps / 1e4))
    v = g.review(sig, jev(), big, account(now), now, market=bad)
    assert any("滑点" in r and "止损" in r for r in v.reasons), v.reasons
    wide = features(now, atr=800.0)
    wide = wide.model_copy(
        update={"book": wide.book.model_copy(update={"spread_bps": 0.12 * stop_bps})}  # type: ignore[union-attr]
    )
    v = g.review(sig, jev(), big, account(now), now, market=wide)
    assert any("价差" in r for r in v.reasons), v.reasons


def test_book_depth_shrinks_size_without_rejecting(now: datetime) -> None:
    f = features(now, atr=4000.0)
    assert f.book is not None
    thin = f.book.model_copy(update={"ask_depth_usdt": 1000.0, "bid_depth_usdt": 900_000.0})
    f = f.model_copy(update={"book": thin})
    cd = card(now, notional_usdt={"min": 10, "max": 100_000})
    size = jev(size={"S": 0.1, "M": 0.1, "L": 0.8})
    acct = account(now, equity=10_000.0)
    v = guard().review(signal(now, f=f), size, cd, acct, now)
    assert v.approved, v.reasons
    assert v.intent is not None and v.intent.notional_usdt <= 500.0  # 做多吃卖盘 1000U × 0.5
    off = guard(max_notional_book_frac=0.0).review(signal(now, f=f), size, cd, acct, now)
    assert off.intent is not None and off.intent.notional_usdt > 1000.0  # 关掉不看盘口


def test_us_hours_gate_blocks_equity_entries_outside_window() -> None:
    from datetime import UTC, datetime

    def verdict(at: datetime, category: str, **fast: object) -> c.RiskVerdict:
        return guard(**fast).review(signal(at), jev(), card(at, category=category), account(at), at)

    ok = datetime(2026, 10, 23, 14, 0, tzinfo=UTC)  # 周五 10:00 美东
    assert verdict(ok, "us_tech").approved
    for at in (
        datetime(2026, 10, 23, 13, 40, tzinfo=UTC),  # 9:40，开盘后不满 15 分钟
        datetime(2026, 10, 23, 19, 50, tzinfo=UTC),  # 15:50，收盘前 15 分钟内
        datetime(2026, 10, 24, 14, 0, tzinfo=UTC),  # 周六
    ):
        v = verdict(at, "crypto_stock")
        assert not v.approved and any("美股休市" in r for r in v.reasons)
    closed = datetime(2026, 10, 24, 14, 0, tzinfo=UTC)
    assert verdict(closed, "precious_metal").approved  # 金属不受美股时段限制
    assert verdict(closed, "us_tech", us_hours_gate=False).approved  # 开关关掉
    assert verdict(
        datetime(2026, 10, 23, 13, 40, tzinfo=UTC), "us_tech", us_open_delay_min=0
    ).approved


class _BracketLots(Lots):
    def __init__(self, tiers: tuple[LeverageBracket, ...] | None) -> None:
        super().__init__()
        self.tiers = tiers

    def leverage_brackets(self, symbol: str) -> tuple[LeverageBracket, ...] | None:
        return self.tiers


_MAX_2X = (LeverageBracket(5_000.0, 2), LeverageBracket(50_000.0, 1))


def _bracket_review(
    tmp_path: Path, now: datetime, tiers: tuple[LeverageBracket, ...] | None, **fast: object
) -> tuple[c.RiskVerdict, dict[str, object]]:
    log = JsonlDecisionLog(tmp_path)
    cfg = FAST.model_copy(update=fast)
    g = _Guard(RISK, cfg, _BracketLots(tiers), RoleRecorder(c.Role.RISK_GUARD, log))
    # Little free margin, so the margin cap (which depends on leverage) binds the size.
    v = g.review(signal(now), jev(), card(now), account(now, available_usdt=500.0), now)
    [rec] = log.read_day(now.date().isoformat())
    return v, rec.output.get("detail", {})


def test_leverage_bracket_ignored_when_switch_is_off(tmp_path: Path, now: datetime) -> None:
    v, detail = _bracket_review(tmp_path, now, _MAX_2X)
    assert v.intent is not None and v.intent.leverage == 5
    assert v.intent.quantity == pytest.approx(0.034)  # 500 * 0.95 / (1/5 + 5bp) = 2369 U
    assert "bracket_max_leverage" not in detail and "leverage_clamped" not in detail


def test_leverage_clamped_to_bracket_resizes_by_margin(tmp_path: Path, now: datetime) -> None:
    v, detail = _bracket_review(tmp_path, now, _MAX_2X, leverage_clamp_to_bracket=True)
    assert v.intent is not None, v.reasons
    it = v.intent
    assert it.leverage == 2
    assert detail["bracket_max_leverage"] == 2
    assert detail["leverage_clamped"] == (
        "杠杆 5x 超过币安 BTCUSDT 能开这么大仓位的上限，按 2x 算（最高档 2x）"
    )
    # 500 * 0.95 / (1/2 + 5bp) = 949.05 U, floored to the 0.001 lot; risk shrinks, never grows.
    assert detail["binding"] == "可用保证金"
    assert it.quantity == pytest.approx(0.013)
    assert it.risk_usdt == pytest.approx(0.013 * 120)


def test_leverage_bracket_notional_cap_binds(tmp_path: Path, now: datetime) -> None:
    tiers = (LeverageBracket(500.0, 20), LeverageBracket(1_020.0, 5), LeverageBracket(9e9, 1))
    v, detail = _bracket_review(tmp_path, now, tiers, leverage_clamp_to_bracket=True)
    assert v.intent is not None and v.intent.leverage == 5
    assert "leverage_clamped" not in detail
    assert detail["binding"] == "币安杠杆分层名义上限"
    # 0.98 * 1020 = 999.6 U -> 0.014 (the full 1020 U would have allowed 0.015).
    assert v.intent.quantity == pytest.approx(0.014)


def test_bracket_choice_prefers_lower_leverage_with_larger_tier(now: datetime) -> None:
    """At 10x the 5k tier would cap the trade; 5x allows 25k, so the guard picks 5x."""
    tiers = (LeverageBracket(5_000.0, 10), LeverageBracket(25_000.0, 5))
    cfg = FAST.model_copy(update={"leverage_clamp_to_bracket": True})
    g = _Guard(RISK.model_copy(update={"max_leverage": 15}), cfg, _BracketLots(tiers))
    v = g.review(signal(now), jev(), card(now), account(now), now)
    assert v.intent is not None, v.reasons
    # Risk notional 75 U / 120 * 68000 = 42.5k, single-trade cap 20k binds at 5x.
    assert v.intent.leverage == 5
    assert v.intent.quantity == pytest.approx(0.294)
    assert v.intent.risk_usdt <= 75.0
    small = (LeverageBracket(50_000.0, 10), LeverageBracket(250_000.0, 5))
    v = _Guard(RISK.model_copy(update={"max_leverage": 15}), cfg, _BracketLots(small)).review(
        signal(now), jev(), card(now), account(now), now
    )
    assert v.intent is not None and v.intent.leverage == 10  # tie on size keeps more leverage


def test_unknown_bracket_keeps_leverage(tmp_path: Path, now: datetime) -> None:
    v, detail = _bracket_review(tmp_path, now, None, leverage_clamp_to_bracket=True)
    assert v.intent is not None and v.intent.leverage == 5
    assert v.intent.quantity == pytest.approx(0.034)
    assert detail["leverage_bracket"] == "没拿到币安杠杆分层，杠杆按原值"


def test_bracket_below_binding_card_leverage_rejects(now: datetime) -> None:
    cfg = FAST.model_copy(update={"leverage_clamp_to_bracket": True, "card_caps_binding": True})
    g = _Guard(RISK, cfg, _BracketLots(_MAX_2X))
    v = g.review(signal(now), jev(), card(now, leverage={"min": 3, "max": 3}), account(now), now)
    assert not v.approved
    assert "币安 BTCUSDT 杠杆上限 2x 低于作战卡杠杆下限 3.0" in v.reasons


def _kind_guard(by_kind: dict[str, float], log: JsonlDecisionLog | None = None) -> _Guard:
    cfg = c.FastLoopConfig.model_validate(
        {**FAST.model_dump(mode="json"), "min_p_take_by_kind": by_kind}
    )
    rec = RoleRecorder(c.Role.RISK_GUARD, log) if log is not None else None
    return _Guard(RISK, cfg, Lots(), rec)


def _kind_case(
    now: datetime, side: c.Side, kind: c.SignalKind, p: float
) -> tuple[c.CandidateSignal, c.JevResponse, c.BattleCard]:
    sig = signal(now, side=side, kind=kind)
    j = jev(take={"take": p, "skip": round(1 - p, 4)})
    return sig, j, card(now, bias="both", allowed_signals=[k.value for k in c.SignalKind])


_SIDES = pytest.mark.parametrize("side", [c.Side.LONG, c.Side.SHORT])


@_SIDES
def test_review_take_by_kind_default_uses_global_floor(now: datetime, side: c.Side) -> None:
    sig, j, cd = _kind_case(now, side, c.SignalKind.BTC_LEAD, 0.2)
    v = guard().review(sig, j, cd, account(now), now)
    assert not v.approved
    assert v.reasons == ["“做”概率 0.20 低于门槛 0.55"]


@_SIDES
def test_review_take_by_kind_zero_lets_low_p_through_and_records_it(
    tmp_path: Path, now: datetime, side: c.Side
) -> None:
    log = JsonlDecisionLog(tmp_path)
    sig, j, cd = _kind_case(now, side, c.SignalKind.BTC_LEAD, 0.2)
    v = _kind_guard({"btc_lead": 0.0}, log).review(sig, j, cd, account(now), now)
    assert v.approved, v.reasons
    assert v.intent is not None and v.intent.side == side
    (rec,) = log.read_day(now.date().isoformat())
    assert rec.input["kind"] == "btc_lead"
    assert rec.output["detail"]["take_floor_kind"] == "btc_lead"
    assert rec.output["detail"]["take_floor"] == 0.0


@_SIDES
def test_review_take_by_kind_keeps_other_guard_checks(now: datetime, side: c.Side) -> None:
    g = _kind_guard({"btc_lead": 0.0})
    sig, j, cd = _kind_case(now, side, c.SignalKind.BTC_LEAD, 0.2)
    held = account(now, open_positions=1, open_symbols=[sig.symbol])
    full = account(now, open_positions=RISK.max_open_positions)
    stale = sig.model_copy(update={"ts": now - timedelta(seconds=60)})
    no_kind = card(now, bias="both", allowed_signals=["volume_breakout"])
    assert g.review(sig, j, cd, held, now).reasons == ["BTCUSDT 已有在管持仓，不开第二笔（含反向）"]
    assert g.review(sig, j, cd, full, now).reasons == [
        f"持仓已有 {RISK.max_open_positions} 笔，达到上限"
    ]
    assert any("秒作废" in r for r in g.review(stale, j, cd, account(now), now).reasons)
    assert g.review(sig, j, no_kind, account(now), now).reasons == ["作战卡不允许 btc_lead 信号"]
    assert g.review(sig, jev(ok=False), cd, account(now), now).reasons == ["拍板人不可用：超时"]


@_SIDES
def test_review_take_by_kind_leaves_other_kinds_on_global_floor(
    now: datetime, side: c.Side
) -> None:
    sig, j, cd = _kind_case(now, side, c.SignalKind.VOLUME_BREAKOUT, 0.2)
    v = _kind_guard({"btc_lead": 0.0}).review(sig, j, cd, account(now), now)
    assert v.reasons == ["“做”概率 0.20 低于门槛 0.55"]


@_SIDES
def test_review_take_by_kind_can_be_stricter(now: datetime, side: c.Side) -> None:
    sig, j, cd = _kind_case(now, side, c.SignalKind.BTC_LEAD, 0.6)
    v = _kind_guard({"btc_lead": 0.7}).review(sig, j, cd, account(now), now)
    assert v.reasons == ["“做”概率 0.60 低于门槛 0.7（btc_lead 单独门槛）"]


@pytest.mark.parametrize(
    ("by_kind", "msg"), [({"not_a_kind": 0.0}, "not_a_kind"), ({"btc_lead": 1.5}, "less than")]
)
def test_fast_loop_take_by_kind_rejects_unknown_kind_and_bad_p(
    by_kind: dict[str, float], msg: str
) -> None:
    with pytest.raises(ValueError, match=msg):
        c.FastLoopConfig.model_validate(
            {**FAST.model_dump(mode="json"), "min_p_take_by_kind": by_kind}
        )


def _cap_guard(caps: dict[str, int]) -> _Guard:
    cfg = c.FastLoopConfig.model_validate(
        {**FAST.model_dump(mode="json"), "max_open_by_kind": caps}
    )
    return _Guard(RISK, cfg, Lots())


@_SIDES
def test_review_open_by_kind_caps_concurrent_positions_of_that_kind(
    now: datetime, side: c.Side
) -> None:
    g = _cap_guard({"btc_lead": 2})
    sig, j, cd = _kind_case(now, side, c.SignalKind.BTC_LEAD, 0.8)
    one = account(now, open_positions=1, open_by_kind={"btc_lead": 1})
    two = account(now, open_positions=2, open_by_kind={"btc_lead": 2, "oi_follow": 3})
    v = g.review(sig, j, cd, one, now)
    assert v.approved, v.reasons
    assert v.intent is not None and v.intent.signal_kind is c.SignalKind.BTC_LEAD
    assert g.review(sig, j, cd, two, now).reasons == [
        "btc_lead 信号开的仓已有 2 笔，达到这类上限 2 笔"
    ]
    other, j2, _ = _kind_case(now, side, c.SignalKind.VOLUME_BREAKOUT, 0.8)
    assert g.review(other, j2, cd, two, now).approved
    assert guard().review(sig, j, cd, two, now).approved  # default: no per-kind cap


@_SIDES
def test_review_open_by_kind_zero_blocks_kind(now: datetime, side: c.Side) -> None:
    sig, j, cd = _kind_case(now, side, c.SignalKind.BTC_LEAD, 0.8)
    v = _cap_guard({"btc_lead": 0}).review(sig, j, cd, account(now), now)
    assert v.reasons == ["btc_lead 信号开的仓已有 0 笔，达到这类上限 0 笔"]


@_SIDES
def test_recheck_chase_applies_open_by_kind(now: datetime, side: c.Side) -> None:
    g = _cap_guard({"btc_lead": 1})
    sig, j, cd = _kind_case(now, side, c.SignalKind.BTC_LEAD, 0.8)
    v = g.review(sig, j, cd, account(now), now)
    assert v.intent is not None
    held = account(now, open_positions=1, open_by_kind={"btc_lead": 1})
    assert g.recheck_chase(v.intent, cd, held, now) == [
        "btc_lead 信号开的仓已有 1 笔，达到这类上限 1 笔"
    ]
    assert g.recheck_chase(v.intent, cd, account(now), now) == []


def test_fast_loop_open_by_kind_rejects_unknown_kind_and_negative() -> None:
    for caps in ({"nope": 1}, {"btc_lead": -1}):
        with pytest.raises(ValueError):
            c.FastLoopConfig.model_validate(
                {**FAST.model_dump(mode="json"), "max_open_by_kind": caps}
            )


def _dust_review(
    tmp_path: Path, now: datetime, side: c.Side, release: bool | None = None, **fast: object
) -> tuple[c.RiskVerdict, dict[str, object]]:
    log = JsonlDecisionLog(tmp_path)
    cfg = FAST.model_copy(update=fast)
    g = _Guard(RISK, cfg, Lots(), RoleRecorder(c.Role.RISK_GUARD, log))
    sig, j, cd = _kind_case(now, side, c.SignalKind.BTC_LEAD, 0.8)
    # Margin nearly used up by other positions: 100 * 0.95 / (1/5 + 5bp) = 474 U, i.e. 0.006
    # BTC risking 0.72 U against a planned 10000 * 1% * 0.75 (M) = 75 U.
    acc = account(now, available_usdt=100.0)
    if release is None:
        v = g.review(sig, j, cd, acc, now)
    else:
        v = g.review_manual(sig, j, cd, acc, now, market=sig.features, release=release)
    [rec] = log.read_day(now.date().isoformat())
    return v, rec.output.get("detail", {})


@_SIDES
def test_review_min_size_ratio_rejects_margin_squeezed_dust(
    tmp_path: Path, now: datetime, side: c.Side
) -> None:
    v, detail = _dust_review(tmp_path, now, side, min_size_ratio=0.25)
    assert not v.approved
    assert v.reasons == ["仓位被可用保证金压到计划的 1.0%（风险 0.72U），低于下限 25%，不开"]
    assert detail["planned_risk_usdt"] == 75.0
    assert detail["size_ratio"] == 0.0096


@_SIDES
def test_review_min_size_ratio_zero_keeps_dust_trade(
    tmp_path: Path, now: datetime, side: c.Side
) -> None:
    v, detail = _dust_review(tmp_path, now, side)
    assert v.approved, v.reasons
    assert v.intent is not None and v.intent.quantity == pytest.approx(0.006)
    assert detail["binding"] == "可用保证金"
    assert "size_ratio" not in detail


@_SIDES
def test_review_manual_release_waives_min_size_ratio(
    tmp_path: Path, now: datetime, side: c.Side
) -> None:
    v, detail = _dust_review(tmp_path, now, side, release=True, min_size_ratio=0.25)
    assert v.approved, v.reasons
    assert detail["waived"] == "仓位被可用保证金压到计划的 1.0%（风险 0.72U），低于下限 25%，不开"


@_SIDES
def test_review_min_size_ratio_leaves_full_size_trade(
    tmp_path: Path, now: datetime, side: c.Side
) -> None:
    log = JsonlDecisionLog(tmp_path)
    cfg = FAST.model_copy(update={"min_size_ratio": 0.25})
    g = _Guard(RISK, cfg, Lots(), RoleRecorder(c.Role.RISK_GUARD, log))
    sig = signal(now, side=side, kind=c.SignalKind.BTC_LEAD, f=features(now, atr=4000.0))
    cd = card(now, bias="both", allowed_signals=[k.value for k in c.SignalKind])
    v = g.review(sig, jev(size={"S": 0.1, "M": 0.1, "L": 0.8}), cd, account(now), now)
    assert v.approved, v.reasons
    assert v.intent is not None and v.intent.quantity == pytest.approx(0.016)
    [rec] = log.read_day(now.date().isoformat())
    detail = rec.output.get("detail", {})
    assert detail["binding"] == "风险金额"
    assert (detail["planned_risk_usdt"], detail["size_ratio"]) == (100.0, 0.96)


@_SIDES
def test_review_respect_card_notional_min(tmp_path: Path, now: datetime, side: c.Side) -> None:
    v, _ = _dust_review(tmp_path / "on", now, side, respect_card_notional_min=True)
    assert v.reasons == ["按可用保证金只能开 408U，低于作战卡名义下限 500U"]
    v, _ = _dust_review(tmp_path / "off", now, side)
    assert v.approved, v.reasons
