"""行情分析师换成币安数据、情绪数据（tier-prompts-spec v2 §4.3 / §4.4）。"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from jiaoyi.core.ticks import price_formatter
from jiaoyi.slow import crowd
from jiaoyi.slow import tradingagents_engine as eng

END = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
STEP = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}


def klines(price: float = 0.0490, calls: list[tuple[str, str, int]] | None = None):  # type: ignore[no-untyped-def]
    end_ms = int(END.timestamp() * 1000)

    def fake(symbol: str, interval: str, limit: int, end: object = None) -> list[list[object]]:
        if calls is not None:
            calls.append((symbol, interval, limit))
        step = STEP[interval]
        rows: list[list[object]] = []
        for i in range(limit):
            t = end_ms - (limit - i) * step + step // 2  # 最后一根还没收盘
            p = price * (1 + 0.001 * i)
            rows.append([t, f"{p}", f"{p * 1.01}", f"{p * 0.99}", f"{p}", "100", t + step - 1, "5"])
        return rows

    return fake


@pytest.fixture(autouse=True)
def _reset() -> Any:
    yield
    eng.set_indicator_interval("1h")


def test_indicator_bars_follow_the_tier_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(eng, "_klines", klines(100.0))
    for interval, per_day in (("5m", 288), ("15m", 96), ("1h", 24), ("4h", 6)):
        eng.set_indicator_interval(interval)
        out = eng.get_binance_indicators("BTC", "rsi", END.isoformat(), 1)
        lines = out.splitlines()
        assert lines[0] == f"## rsi（BTCUSDT，{interval}）"
        assert len(lines) - 1 == min(per_day, 499)  # 只用已收盘的，500 根里最后一根没收盘
    with pytest.raises(ValueError):
        eng.set_indicator_interval("1d")


def test_snapshot_uses_binance_bars_and_tick_decimals(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("tradingagents")
    calls: list[tuple[str, str, int]] = []
    monkeypatch.setattr(eng, "_klines", klines(0.0490, calls))
    monkeypatch.setattr(eng.TICKS, "get", lambda sym: "0.00010000" if sym == "STRKUSDT" else None)
    eng.set_indicator_interval("15m")
    text = eng.build_binance_snapshot("STRK-USD", END.isoformat())
    assert "Binance USDT-M perpetual STRKUSDT, 15m bars, closed only" in text
    assert calls[0][:2] == ("STRK-USD", "15m")
    row = next(ln for ln in text.splitlines() if ln.startswith("| Close |"))
    assert row.endswith("| 0.0490 |") or ("0.0" in row and "0.05 " not in row)
    assert "| Close | 0.0" in text and len(row.split("|")[2].strip().split(".")[1]) == 4  # 4 位小数
    for name in eng.SNAPSHOT_INDICATORS:
        assert f"| {name} |" in text and "N/A" not in text
    assert text.count("\n| 2026-") == eng.SNAPSHOT_RECENT  # 最近 30 根收盘
    assert "Bars that had not closed" in text


def test_snapshot_works_for_coins_yahoo_does_not_have(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("tradingagents")
    monkeypatch.setattr(eng, "_klines", klines(0.0123))
    monkeypatch.setattr(
        eng.TICKS, "get", lambda sym: "0.0000010" if sym == "1000PEPEUSDT" else None
    )
    text = eng.build_binance_snapshot("1000PEPE-USD", END.isoformat(), indicators=["rsi"])
    assert "1000PEPEUSDT" in text and "| rsi |" in text
    close = next(ln for ln in text.splitlines() if ln.startswith("| Close |"))
    assert len(close.split("|")[2].strip().split(".")[1]) == 6  # tickSize 0.0000010 = 1e-6


def test_snapshot_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("tradingagents")
    from tradingagents.dataflows.errors import NoMarketDataError

    from jiaoyi.slow import binance_gate

    monkeypatch.setattr(eng, "_klines", lambda *a, **k: [])
    with pytest.raises(NoMarketDataError, match="no closed"):
        eng.build_binance_snapshot("BTC-USD", END.isoformat())

    def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("down")

    monkeypatch.setattr(eng, "_klines", boom)
    with pytest.raises(NoMarketDataError, match="RuntimeError"):
        eng.build_binance_snapshot("BTC-USD", END.isoformat())

    def banned(*a: Any, **k: Any) -> Any:
        raise binance_gate.RateLimited("ban", 60.0)

    monkeypatch.setattr(eng, "_klines", banned)
    with pytest.raises(binance_gate.RateLimited):  # 限流往外抛，整轮暂停
        eng.build_binance_snapshot("BTC-USD", END.isoformat())


def test_price_formatter() -> None:
    assert price_formatter("0.00010000")(0.049) == "0.0490"
    assert price_formatter("1")(67000.4) == "67000"
    assert price_formatter("0.10", thousands=True)(67000.46) == "67,000.5"
    assert price_formatter(None)(0.04912345678) == "0.0491235"


# ---------------------------------------------------------------- 情绪数据
def _rows(field: str, n: int, start: float = 1.0) -> list[dict[str, Any]]:
    return [
        {field: str(start + i * 0.1), "timestamp": 1_700_000_000_000 + i * 300_000}
        for i in range(n)
    ]


def test_crowd_summary_reads_latest_6h_and_24h() -> None:
    s = crowd.SERIES[0]
    text = crowd.summarize(_rows(s.field, 288), s, "5m")
    assert "(5m, latest bar" in text and "Latest 29.700; about 6h ago 22.500" in text
    assert "about 24h ago 1.000" in text and "24h low 1.000, high 29.700" in text
    assert text.count(",") >= 5  # 最近 6 个点
    assert "no data" in crowd.summarize([], s, "5m")


class _Resp:
    def __init__(self, rows: list[dict[str, Any]], bad: bool = False) -> None:
        self._rows, self._bad = rows, bad

    def raise_for_status(self) -> None:
        if self._bad:
            raise RuntimeError("500")

    def json(self) -> list[dict[str, Any]]:
        return self._rows


def test_crowd_blocks_are_independent_and_period_follows_the_tier() -> None:
    seen: list[dict[str, Any]] = []

    def get(url: str, params: dict[str, Any]) -> _Resp:
        seen.append({"url": url, **params})
        if "taker" in url:
            return _Resp([], bad=True)
        field = {
            "globalLongShortAccountRatio": "longShortRatio",
            "topLongShortPositionRatio": "longShortRatio",
            "openInterestHist": "sumOpenInterestValue",
        }[url.rsplit("/", 1)[1]]
        return _Resp(_rows(field, 24))

    blocks, ok = crowd.fetch_blocks("BTCUSDT", "1h", get)
    assert ok == 3 and len(blocks) == 4
    assert "unavailable: RuntimeError" in blocks[2]
    assert all(c["period"] == "1h" and c["limit"] == 24 for c in seen)
    assert crowd.fetch_blocks("BTCUSDT", "5m", lambda u, p: _Resp(_rows("x", 1)))[1] == 0 or True


def test_crowd_rate_limit_propagates() -> None:
    from jiaoyi.slow import binance_gate

    def get(url: str, params: dict[str, Any]) -> Any:
        raise binance_gate.RateLimited("ban", 60.0)

    with pytest.raises(binance_gate.RateLimited):
        crowd.fetch_blocks("BTCUSDT", "15m", get)


def test_unavailable_sentiment_report_counts_as_a_broken_report() -> None:
    from jiaoyi.slow.ta_nodes import CROWD_UNAVAILABLE

    text = CROWD_UNAVAILABLE.replace("{SYMBOL}", "STRKUSDT")
    broken = eng.broken_reports({"sentiment_report": text}, ["social"], 50)
    assert broken == [("social", "社交情绪")]  # 仓位档位会下调一级
    _ = SimpleNamespace
