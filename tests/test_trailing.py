"""保本和移动止损（持仓管家的开关，默认关）：风控算 hummingbot trailing_stop 的启动线和回撤距离。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from jiaoyi.core.profiles import HOLD
from jiaoyi.risk.guard import RiskGuardImpl
from tests.fastloop_helpers import FAST, RISK, Lots, account, card, jev, signal
from tests.test_execution import Rig, intent

FEE = RISK.taker_fee_bps / 100.0


def _intent(now: datetime, **over: object):  # type: ignore[no-untyped-def]
    g = RiskGuardImpl(RISK, FAST.model_copy(update=over), Lots())
    v = g.review(signal(now), jev(), card(now), account(now), now, market=signal(now).features)
    assert v.approved, v.reasons
    assert v.intent is not None
    return v.intent


def test_trailing_on_by_default_breakeven_off(now: datetime) -> None:
    it = _intent(now)
    assert it.trailing_activation_pct is not None and it.trailing_stop_pct is not None
    off = _intent(now, trailing_on=False)
    assert off.trailing_activation_pct is None and off.trailing_stop_pct is None


def test_trailing_only_wide_and_starts_late(now: datetime) -> None:
    it = _intent(now, trailing_on=True, trail_start_r=1.0, trail_distance_r=1.2)
    r = it.stop_loss_pct
    assert it.trailing_stop_pct is not None and it.trailing_activation_pct is not None
    assert abs(it.trailing_stop_pct - 1.2 * r) < 1e-6
    # 开始线不低于「移动距离 + 平仓手续费」：启动那一刻止损线不低于保本价
    assert abs(it.trailing_activation_pct - max(1.0 * r, 1.2 * r + FEE)) < 1e-6
    late = _intent(now, trailing_on=True, trail_start_r=3.0, trail_distance_r=1.0)
    assert abs(late.trailing_activation_pct - 3.0 * r) < 1e-6  # type: ignore[operator]


def test_breakeven_only_floor_at_fee(now: datetime) -> None:
    it = _intent(now, breakeven_on=True, breakeven_trigger_r=0.8, trailing_on=False)
    r = it.stop_loss_pct
    act, delta = it.trailing_activation_pct, it.trailing_stop_pct
    assert act is not None and delta is not None
    assert abs(act - 0.8 * r) < 1e-6
    assert abs((act - delta) - FEE) < 1e-6  # 启动时止损线 = 平仓手续费 → 扣完不亏


def test_both_on_trails_from_breakeven(now: datetime) -> None:
    it = _intent(
        now, breakeven_on=True, breakeven_trigger_r=0.8, trailing_on=True, trail_distance_r=0.3
    )
    r = it.stop_loss_pct
    assert abs(it.trailing_activation_pct - 0.8 * r) < 1e-6  # type: ignore[operator]
    assert abs(it.trailing_stop_pct - 0.3 * r) < 1e-6  # type: ignore[operator]


def test_tier_defaults_wide() -> None:
    assert HOLD["30m"]["fast_loop"]["trail_distance_r"] == 0.7
    assert HOLD["2h"]["fast_loop"]["trail_distance_r"] == 0.7
    assert HOLD["4h"]["fast_loop"]["trail_distance_r"] == 1.0
    assert HOLD["24h"]["fast_loop"]["trail_distance_r"] == 1.0
    assert all(HOLD[t]["fast_loop"]["trail_start_r"] == 0.7 for t in HOLD)
    assert FAST.trailing_on is True and FAST.breakeven_on is False


async def test_executor_passes_trailing_to_hummingbot(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    await rig.adapter.open(intent(trailing_activation_pct=1.5, trailing_stop_pct=1.2))
    ts = rig.api.created[-1]["triple_barrier_config"]["trailing_stop"]
    assert ts == {"activation_price": "0.015", "trailing_delta": "0.012"}
