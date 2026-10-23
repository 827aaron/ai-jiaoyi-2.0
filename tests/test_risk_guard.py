"""保镖：四层决定权、数量计算、故障即关闭、日亏熔断按曼谷 0 点清零。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from jiaoyi import contracts as c
from jiaoyi import interfaces as i
from jiaoyi.core.recorder import RoleRecorder
from jiaoyi.core.store import JsonlDecisionLog
from jiaoyi.risk.guard import LotRules, RiskGuardImpl
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

    # 进程重启：新保镖读回状态，同一天权益回到线内也照样锁住
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
    v = guard().review(signal(now, f=quiet), jev(), card(now), account(now), now)
    assert v.approved and v.intent is not None, v.reasons
    floored = v.intent.stop_loss_pct
    v2 = guard(atr_floor_7d=0.0).review(signal(now, f=quiet), jev(), card(now), account(now), now)
    assert v2.intent is not None and v2.intent.stop_loss_pct < floored  # 关掉就按当前 ATR
    busy = features(now, atr=150.0).model_copy(update={"atr_median_7d": 100.0})
    v3 = guard().review(signal(now, f=busy), jev(), card(now), account(now), now)
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
    tight = guard(min_stop_fee_multiple=bps / (2 * taker) + 0.1)
    v = tight.review(signal(now), jev(), card(now), account(now), now)
    assert not v.approved and any("离手续费太近" in r for r in v.reasons)
    assert guard().review(signal(now), jev(), card(now), account(now), now).approved  # 默认 0 不查


def test_funding_against_us_rejects_only_the_paying_side(now: datetime) -> None:
    g = guard(funding_against_max_bp=5.0)
    paying = signal(now, f=features(now, funding_rate=0.0008))  # 多头要付 8bp
    v = g.review(paying, jev(), card(now), account(now), now)
    assert not v.approved and any("资金费" in r for r in v.reasons)
    getting = signal(now, f=features(now, funding_rate=-0.0008))  # 多头反而收钱
    assert g.review(getting, jev(), card(now), account(now), now).approved
    unknown = signal(now, f=features(now))
    assert g.review(unknown, jev(), card(now), account(now), now).approved  # 没费率不拦
