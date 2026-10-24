"""持仓页的移动止损：启动价、现在的移动止损价按 K 线估（hummingbot 不吐当前状态）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from jiaoyi import contracts as c
from jiaoyi.panel.hb import HbSnapshot
from jiaoyi.panel.overview import position_views
from jiaoyi.panel.trailing import best_since, estimate, fetch_best
from tests.conftest import load_example

OPEN = datetime(2026, 10, 24, 14, 0, tzinfo=UTC)
T0 = int(OPEN.timestamp())


def _bar(t: int, high: float, low: float) -> dict[str, float]:
    return {"time": t, "open": low, "high": high, "low": low, "close": low}


def test_short_not_started_then_started() -> None:
    # 空单 100 开，启动线 0.7%（扣开仓费 0.05% 后），回撤 0.7%
    idle = estimate("short", 100.0, 0.7, 0.7, 0.05, best=99.5, mark=99.6)
    assert idle.activation_price == pytest.approx(99.25)
    assert idle.active is False and idle.stop_price is None and idle.best_price == 99.5
    on = estimate("short", 100.0, 0.7, 0.7, 0.05, best=99.0, mark=99.4)
    assert on.active is True
    assert on.stop_price == pytest.approx(99.7)  # 最低 99 = 赚 1%，往回 0.7% → 赚 0.3% 的价


def test_long_uses_mark_when_it_is_the_best() -> None:
    est = estimate("long", 100.0, 0.5, 0.3, 0.0, best=100.4, mark=101.0)
    assert est.best_price == 101.0 and est.active is True
    assert est.stop_price == pytest.approx(100.7)


def test_unknown_without_prices() -> None:
    est = estimate("long", 100.0, 0.5, 0.3, 0.05, best=None, mark=None)
    assert est.active is None and est.stop_price is None
    assert est.activation_price == pytest.approx(100.55)


def test_best_skips_the_entry_minute_and_fills_with_5m() -> None:
    b1 = [_bar(T0 - 60, 200.0, 1.0), _bar(T0 + 600, 103.0, 99.0), _bar(T0 + 660, 102.0, 98.5)]
    assert best_since("long", OPEN + timedelta(seconds=10), b1, []) == 103.0
    assert best_since("short", OPEN + timedelta(seconds=10), b1, []) == 98.5
    # 1 分钟线从开仓后 10 分钟才有：中间那段用整根落在开仓后的 5 分钟线补
    b5 = [_bar(T0 - 300, 300.0, 0.5), _bar(T0, 105.0, 99.0), _bar(T0 + 300, 104.0, 99.0)]
    assert best_since("long", OPEN, b1[1:], b5) == 105.0
    assert best_since("long", OPEN, [], []) is None


async def test_fetch_best_only_pulls_5m_when_1m_is_short() -> None:
    asked: list[str] = []

    async def kl(tf: str) -> list[dict[str, float]]:
        asked.append(tf)
        return [_bar(T0 - 60, 500.0, 1.0), _bar(T0 + 60, 101.0, 99.0)] if tf == "1m" else []

    assert await fetch_best("long", OPEN, kl) == 101.0  # type: ignore[arg-type]
    assert asked == ["1m"]
    assert await fetch_best("long", OPEN - timedelta(hours=6), kl) == 500.0  # type: ignore[arg-type]
    assert asked == ["1m", "1m", "5m"]


def test_view_reads_ledger_then_falls_back_to_guard_record() -> None:
    raw = load_example("position_record")
    old = c.PositionRecord.model_validate(raw)
    assert old.trailing_stop_pct is None
    guard = c.DecisionRecord(
        record_id="g1",
        ts=OPEN,
        role=c.Role.RISK_GUARD,
        outcome=c.Outcome.PASS,
        summary="放行",
        output={
            "intent_id": old.intent_id,
            "trailing_activation_pct": 0.7,
            "trailing_stop_pct": 0.5,
        },
    )
    [v] = position_views([old], [guard], HbSnapshot(ok=False), taker_fee_bps=5.0)
    assert (v.trailing_activation_pct, v.trailing_stop_pct) == (0.7, 0.5)
    assert v.entry_fee_pct is not None and v.trail is None
    new = old.model_copy(update={"trailing_activation_pct": 1.0, "trailing_stop_pct": 0.4})
    [v] = position_views([new], [guard], HbSnapshot(ok=False))
    assert (v.trailing_activation_pct, v.trailing_stop_pct) == (1.0, 0.4)
    [v] = position_views([old], [], HbSnapshot(ok=False))
    assert v.trailing_stop_pct is None
