"""预测员意见占几成（fast_loop.predictor_weight）：保镖按看涨概率挪“做”概率。"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from jiaoyi import contracts as c
from jiaoyi.core.recorder import RoleRecorder
from jiaoyi.core.store import JsonlDecisionLog
from tests.fastloop_helpers import FAST, RISK, Lots, account, card, jev, signal
from tests.test_risk_guard import _Guard, guard


def _with_forecast(
    sig: c.CandidateSignal, p_up: float, *, stale: bool = False
) -> c.CandidateSignal:
    fc = c.Forecast(
        symbol=sig.symbol,
        ts=sig.ts - timedelta(minutes=5),
        horizon_min=120,
        price_low=60000.0,
        price_high=70000.0,
        p_up=p_up,
        model="Kronos-small",
        stale_after=sig.ts + (timedelta(minutes=-1) if stale else timedelta(minutes=10)),
    )
    return sig.model_copy(update={"forecast": fc})


def _take(p: float) -> c.JevResponse:
    return jev(take={"take": p, "skip": round(1 - p, 4)})


@pytest.mark.parametrize(
    ("side", "p_up", "take", "approved"),
    [
        (c.Side.LONG, 1.0, 0.55, True),  # 同向：0.55 + 0.10 = 0.65 ≥ 0.6
        (c.Side.LONG, 0.0, 0.65, False),  # 反向：0.65 − 0.10 = 0.55 < 0.6
        (c.Side.LONG, 0.5, 0.55, False),  # 中性不挪
        (c.Side.SHORT, 0.0, 0.55, True),  # 做空看跌 = 同向
        (c.Side.SHORT, 1.0, 0.65, False),
    ],
)
def test_nudge_moves_take_only(
    now: datetime, side: c.Side, p_up: float, take: float, approved: bool
) -> None:
    sig = _with_forecast(signal(now, side=side), p_up)
    j = _take(take)
    if side == c.Side.SHORT:
        j = jev(
            direction={"up": 0.1, "down": 0.8, "flat": 0.1},
            take={"take": take, "skip": round(1 - take, 4)},
        )
    cd = card(now, bias="both")
    reasons = guard(min_p_take=0.6).review(sig, j, cd, account(now), now).reasons
    gate = [r for r in reasons if "“做”" in r]
    assert (not gate) is approved, reasons


def test_zero_weight_or_no_valid_forecast_does_not_nudge(now: datetime) -> None:
    cases = [
        (guard(min_p_take=0.6, predictor_weight=0.0), _with_forecast(signal(now), 1.0)),
        (guard(min_p_take=0.6), signal(now)),
        (guard(min_p_take=0.6), _with_forecast(signal(now), 1.0, stale=True)),
    ]
    for g, sig in cases:
        reasons = g.review(sig, _take(0.55), card(now), account(now), now).reasons
        assert any("“做”概率 0.55 低于门槛" in r for r in reasons), reasons


def test_weight_scales_and_clamps(now: datetime) -> None:
    sig = _with_forecast(signal(now), 1.0)
    # 权重 1 → 最多 +0.5，夹在 1 以内
    v = guard(min_p_take=1.0, predictor_weight=1.0).review(
        sig, _take(0.9), card(now), account(now), now
    )
    assert not any("“做”" in r for r in v.reasons), v.reasons


def test_record_has_raw_and_adjusted(tmp_path: Path, now: datetime) -> None:
    log = JsonlDecisionLog(tmp_path)
    g = _Guard(
        RISK,
        FAST.model_copy(update={"min_p_take": 0.6}),
        Lots(),
        RoleRecorder(c.Role.RISK_GUARD, log),
    )
    v = g.review(_with_forecast(signal(now), 0.0), _take(0.65), card(now), account(now), now)
    assert any("“做”概率 0.55（预测员 -0.10） 低于门槛 0.6" in r for r in v.reasons), v.reasons
    g.review(signal(now), _take(0.65), card(now), account(now), now)
    a, b = log.read_day(now.date().isoformat())
    d = a.output["detail"]
    assert d["predictor"].startswith("反向") and d["predictor_p_up"] == 0.0
    assert d["predictor_nudge"] == pytest.approx(-0.1)
    assert d["take_raw"] == 0.65 and d["take_adj"] == pytest.approx(0.55)
    assert b.output["detail"]["predictor"] == "没有预测" and "take_raw" in b.output["detail"]
