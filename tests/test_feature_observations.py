from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from jiaoyi.contracts import BookTop, Interval, Kline, MarkPrice, Side, Trade
from jiaoyi.market.features import RollingFeatureEngine

T0 = datetime(2026, 10, 27, tzinfo=UTC)
SYMBOL = "BTCUSDT"


def trade(seconds: float, price: float) -> Trade:
    return Trade(
        symbol=SYMBOL,
        ts=T0 + timedelta(seconds=seconds),
        price=price,
        qty=1,
        taker_side=Side.LONG,
    )


def mark(seconds: float) -> MarkPrice:
    return MarkPrice(
        symbol=SYMBOL,
        ts=T0 + timedelta(seconds=seconds),
        mark_price=101,
        funding_rate=0,
        next_funding_at=T0 + timedelta(hours=8),
    )


def warm() -> RollingFeatureEngine:
    engine = RollingFeatureEngine(atr_period_min=1, observation_wall=lambda: T0)
    for i in range(15):
        start = T0 + timedelta(minutes=i - 15)
        engine.update(
            Kline(
                symbol=SYMBOL,
                interval=Interval.M1,
                open_time=start,
                close_time=start + timedelta(seconds=59.999),
                open=100,
                high=101,
                low=99,
                close=100,
                volume=10,
                quote_volume=1000,
            )
        )
    return engine


def test_mixed_events_do_not_renew_trade_source() -> None:
    engine = RollingFeatureEngine(observation_wall=lambda: T0 + timedelta(seconds=5))
    engine.update(trade(0.125, 100))
    engine.update(
        BookTop(symbol=SYMBOL, ts=T0 + timedelta(seconds=2), bids=[(99, 3)], asks=[(101, 4)])
    )
    engine.update(mark(3))
    captured = engine.diagnostic_observation(SYMBOL, T0 + timedelta(seconds=6))
    assert captured["feature_ts"] == (T0 + timedelta(seconds=3)).isoformat()
    assert captured["price"]["source_ts"] == (T0 + timedelta(seconds=0.125)).isoformat()
    assert captured["price"]["observed_at"] == (T0 + timedelta(seconds=5)).isoformat()
    assert captured["price"]["age_s"] == pytest.approx(5.875)
    assert captured["mark"]["age_s"] == 3
    assert captured["book"]["age_s"] == 4
    assert captured["book"]["bid_qty"] == 3
    engine.update(trade(7, 102))
    assert captured["price"]["value"] == 100
    assert captured["price"]["source"] == "trade"


def test_bootstrap_and_unknown_source_are_not_fabricated_as_trades() -> None:
    engine = warm()
    captured = engine.diagnostic_observation(SYMBOL, T0)
    assert captured["price"]["source"] == "kline_close_bootstrap"
    assert captured["price"]["source_ts"] == (T0 - timedelta(milliseconds=1)).isoformat()
    assert captured["returns"]["ret5m"]["coverage_status"] == "no_trade_price_history"
    before = len(engine._s)
    missing = engine.diagnostic_observation("ETHUSDT", T0)
    assert missing["status"] == "unknown_symbol" and missing["price"] is None
    assert len(engine._s) == before
    engine._s[SYMBOL].last_price_source = None
    engine._s[SYMBOL].last_price_source_ts = None
    assert engine.diagnostic_observation(SYMBOL, T0)["price"]["source_ts"] is None


def test_short_warmup_keeps_original_return_and_exposes_fallback() -> None:
    engine = warm()
    engine.update(trade(1, 100))
    engine.update(trade(11, 101))
    before = engine.snapshot(SYMBOL)
    assert before is not None
    captured = engine.diagnostic_observation(SYMBOL, T0 + timedelta(seconds=12))
    assert before.values["ret_5m"] == pytest.approx(0.01)
    for field in ("ret1m", "ret5m"):
        row = captured["returns"][field]
        assert row["actual_lookback_s"] == 10
        assert row["warmup_complete"] is False
        assert row["used_first_available"] is True
        assert row["source_bucket_precision_s"] == 1
    assert captured["returns"]["ret5m"]["anchor_gap_s"] == -290
    assert engine.snapshot(SYMBOL) == before


def test_window_anchor_gap_and_missing_history_are_explicit() -> None:
    engine = RollingFeatureEngine(observation_wall=lambda: T0)
    for seconds, price in ((0, 100), (239, 101), (300, 102)):
        engine.update(trade(seconds, price))
    captured = engine.diagnostic_observation(SYMBOL, T0 + timedelta(seconds=301))
    one = captured["returns"]["ret1m"]
    assert one["actual_lookback_s"] == 61 and one["anchor_gap_s"] == 1
    assert one["max_bucket_gap_s"] == 61
    five = captured["returns"]["ret5m"]
    assert five["actual_lookback_s"] == 300
    assert five["warmup_complete"] is True
    assert five["coverage_status"] == "target_reached_not_continuity_proof"
    assert five["max_bucket_gap_s"] == 239
    engine.update(mark(602))
    stale = engine.diagnostic_observation(SYMBOL, T0 + timedelta(seconds=603))
    assert stale["price"]["age_s"] == 303
    assert stale["returns"]["ret1m"]["anchor_price"] is None
    assert stale["returns"]["ret5m"]["used_first_available"] is None


def test_clock_errors_future_source_and_nonfinite_values_remain_diagnostic() -> None:
    def broken_clock() -> datetime:
        raise RuntimeError("clock unavailable")

    engine = RollingFeatureEngine(observation_wall=broken_clock)
    engine.update(trade(10, 100))
    captured = engine.diagnostic_observation(SYMBOL, T0)
    assert captured["price"]["observed_at"] is None
    assert captured["price"]["age_s"] == -10
    assert captured["price"]["source_after_capture"] is True
    engine.update(trade(9, 99))
    captured = engine.diagnostic_observation(SYMBOL, T0 + timedelta(seconds=11))
    assert captured["price"]["source_ts"] == (T0 + timedelta(seconds=9)).isoformat()
    assert captured["returns"]["ret1m"]["coverage_status"] == "unordered_history"
    engine._s[SYMBOL].last_price = float("nan")
    engine._s[SYMBOL].prices.append((float("nan"), 1))
    invalid = engine.diagnostic_observation(SYMBOL, T0)
    assert invalid["price"]["value"] is None
    assert invalid["returns"]["ret1m"]["coverage_status"] == "nonfinite_history"
    json.dumps(invalid, allow_nan=False)


def test_diagnostics_never_run_extra_context_or_change_feature_values() -> None:
    engine = warm()
    engine.update(trade(0, 100))
    before = engine.snapshot(SYMBOL)

    def forbidden_extra(symbol: str, now: datetime) -> dict[str, float]:
        pytest.fail("diagnostic observation invoked external feature callback")

    engine.set_extra(forbidden_extra)
    engine.diagnostic_observation(SYMBOL, T0)
    engine.set_extra(None)
    assert engine.snapshot(SYMBOL) == before
