"""预测员意见占几成（fast_loop.predictor_weight）：官方 Kronos 排名，和卡方向一致才加“做”概率。"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from jiaoyi import contracts as c
from jiaoyi.core.recorder import RoleRecorder
from jiaoyi.core.store import JsonlDecisionLog
from tests.fastloop_helpers import FAST, RISK, Lots, account, card, jev, signal
from tests.test_risk_guard import _Guard, guard

_PCT = {"top": 0.95, "bottom": 0.05, "mid": 0.5}


def _with_forecast(
    sig: c.CandidateSignal, view: str | None, *, stale: bool = False
) -> c.CandidateSignal:
    fc = c.Forecast(
        symbol=sig.symbol,
        ts=sig.ts - timedelta(minutes=5),
        horizon_min=120,
        price_low=60000.0,
        price_high=70000.0,
        p_up=1.0 if view == "top" else 0.0,
        score=0.004 if view == "top" else -0.004,
        rank_pct=_PCT[view] if view else None,
        rank_n=21 if view else None,
        rank_view=view,  # type: ignore[arg-type]
        model="Kronos-small",
        stale_after=sig.ts + (timedelta(minutes=-1) if stale else timedelta(minutes=10)),
    )
    return sig.model_copy(update={"forecast": fc})


def _take(p: float) -> c.JevResponse:
    return jev(take={"take": p, "skip": round(1 - p, 4)})


@pytest.mark.parametrize(
    ("side", "view", "take", "approved"),
    [
        (c.Side.LONG, "top", 0.55, True),  # 排前段 = 看好，做多同向：0.55 + 0.10 = 0.65 ≥ 0.6
        (c.Side.LONG, "bottom", 0.65, True),  # 反向不扣（Aaron 10-24）：0.65 ≥ 0.6
        (c.Side.LONG, "mid", 0.55, False),  # 中间不表态：不加，0.55 < 0.6
        (c.Side.SHORT, "bottom", 0.55, True),  # 排后段 = 看坏，做空同向
        (c.Side.SHORT, "mid", 0.55, False),
        (c.Side.SHORT, "top", 0.55, False),  # 反向不加也不扣
    ],
)
def test_nudge_moves_take_only(
    now: datetime, side: c.Side, view: str, take: float, approved: bool
) -> None:
    sig = _with_forecast(signal(now, side=side), view)
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
        (guard(min_p_take=0.6, predictor_weight=0.0), _with_forecast(signal(now), "top")),
        (guard(min_p_take=0.6), signal(now)),
        (guard(min_p_take=0.6), _with_forecast(signal(now), "top", stale=True)),
        (guard(min_p_take=0.6), _with_forecast(signal(now), None)),  # 币太少排不出名次
    ]
    for g, sig in cases:
        reasons = g.review(sig, _take(0.55), card(now), account(now), now).reasons
        assert any("“做”概率 0.55 低于门槛" in r for r in reasons), reasons


def test_weight_scales_and_clamps(now: datetime) -> None:
    sig = _with_forecast(signal(now), "top")
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
    v = g.review(_with_forecast(signal(now), "top"), _take(0.45), card(now), account(now), now)
    assert any("“做”概率 0.55（预测员 +0.10） 低于门槛 0.6" in r for r in v.reasons), v.reasons
    g.review(signal(now), _take(0.65), card(now), account(now), now)
    a, b = log.read_day(now.date().isoformat())
    d = a.output["detail"]
    assert d["predictor"] == "同向（看好，21 个币里排第 2）" and d["predictor_rank_pct"] == 0.95
    assert d["predictor_score_bps"] == 40.0
    assert d["predictor_nudge"] == pytest.approx(0.1)
    assert d["take_raw"] == 0.45 and d["take_adj"] == pytest.approx(0.55)
    assert b.output["detail"]["predictor"] == "没有预测" and "take_raw" in b.output["detail"]


@pytest.mark.parametrize(
    ("side", "view", "nudge", "said"),
    [
        (c.Side.LONG, "top", 0.1, "同向（看好"),
        (c.Side.LONG, "mid", 0.0, "不同向，不扣分（不表态"),
        (c.Side.LONG, "bottom", 0.0, "不同向，不扣分（看坏"),
        (c.Side.SHORT, "bottom", 0.1, "同向（看坏"),
        (c.Side.SHORT, "top", 0.0, "不同向，不扣分（看好"),
    ],
)
def test_only_ranking_counts(
    tmp_path: Path, now: datetime, side: c.Side, view: str, nudge: float, said: str
) -> None:
    """官方用法：只看名次（单个币的涨跌方向不看），和卡一致才加，相反不扣（Aaron 10-24）。"""
    log = JsonlDecisionLog(tmp_path)
    g = _Guard(RISK, FAST, Lots(), RoleRecorder(c.Role.RISK_GUARD, log))
    sig = _with_forecast(signal(now, side=side), view)
    g.review(sig, _take(0.65), card(now, bias="both"), account(now), now)
    [rec] = log.read_day(now.date().isoformat())
    d = rec.output["detail"]
    assert d["predictor_nudge"] == pytest.approx(nudge)
    assert d["predictor"].startswith(said)
