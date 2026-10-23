"""TradingAgents 适配层里不依赖真库、真 key 的部分。"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from jiaoyi.contracts import Bias, Category, HardFilters, SizeTier, SymbolPoolEntry
from jiaoyi.slow.binance_stats import BinanceStatsSource
from jiaoyi.slow.tradingagents_engine import (
    TradingAgentsSettings,
    _CardSymbolDraft,
    _indicator,
    binance_symbol,
    build_config,
    draft_to_card_symbol,
    format_derivatives,
    ta_ticker,
)


def entry(sym: str, cat: Category) -> SymbolPoolEntry:
    return SymbolPoolEntry(
        symbol=sym,
        category=cat,
        score=0.5,
        filters=HardFilters(
            quote_volume_24h_usdt=1e8, spread_bps=1, depth_usdt=1e6, listed_days=100, passed=True
        ),
        in_pool=True,
    )


def test_build_config_overrides_without_mutating_base() -> None:
    base = {"llm_provider": "openai", "data_vendors": {"core_stock_apis": "yfinance", "x": "y"}}
    cfg = build_config(base, TradingAgentsSettings())
    assert cfg["llm_provider"] == "deepseek"
    assert cfg["quick_think_llm"] == "deepseek-flash" and cfg["deep_think_llm"] == "deepseek-v4-pro"
    assert cfg["output_language"] == "Chinese"
    assert (cfg["max_debate_rounds"], cfg["max_risk_discuss_rounds"]) == (1, 1)
    assert (cfg["max_tokens"], cfg["llm_max_retries"]) == (8192, 6)
    assert cfg["data_vendors"]["core_stock_apis"] == "binance"
    assert cfg["data_vendors"]["x"] == "y"
    assert base["data_vendors"]["core_stock_apis"] == "yfinance"  # type: ignore[index]


def test_ticker_mapping() -> None:
    assert ta_ticker(entry("BTCUSDT", Category.MAJOR)) == ("BTC-USD", "crypto")
    assert ta_ticker(entry("TSLAUSDT", Category.US_TECH)) == ("TSLA", "stock")
    for t in ("BTC", "BTC-USD", "btcusdt"):
        assert binance_symbol(t) == "BTCUSDT"


def test_indicators() -> None:
    c = [float(x) for x in range(1, 61)]
    o = h = low = c
    assert _indicator("close_10_sma", o, h, low, c)[-1] == pytest.approx(55.5)
    assert _indicator("rsi", o, h, low, c)[-1] == 100.0
    assert len(_indicator("macdh", o, h, low, c)) == 60
    assert _indicator("atr", o, h, low, c)[-1] == pytest.approx(1.0, abs=1e-2)
    with pytest.raises(ValueError):
        _indicator("nope", o, h, low, c)


def test_draft_conversion_normalizes_and_blocks_no_trade_signals() -> None:
    d = _CardSymbolDraft(
        bias=Bias.NO_TRADE,
        allowed_signals=["volume_breakout"],  # type: ignore[list-item]
        size_tier_max=SizeTier.S,
        notional_min=500,
        notional_max=100,  # 上下限写反了
        leverage_min=1,
        leverage_max=2,
        stop_atr_min=1,
        stop_atr_max=2,
        take_profit_r_min=1.5,
        take_profit_r_max=3,
        reason="财报前夜",
    )
    cs = draft_to_card_symbol(d, entry("TSLAUSDT", Category.US_TECH))
    assert cs.allowed_signals == [] and cs.category is Category.US_TECH
    assert (cs.notional_usdt.min, cs.notional_usdt.max) == (100, 500)


VOLS = (("BTCUSDT", "9e9"), ("ETHUSDT", "5e9"), ("XUSDT", "1e9"))


def _binance_handler(req: httpx.Request) -> httpx.Response:
    p = req.url.path
    now_ms = int(datetime(2026, 10, 22, tzinfo=UTC).timestamp() * 1000)
    if p == "/fapi/v1/exchangeInfo":
        syms = [
            {"symbol": s, "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT",
             "onboardDate": now_ms - 30 * 86_400_000}
            for s in ("BTCUSDT", "ETHUSDT")
        ]  # fmt: skip
        return httpx.Response(200, json={"symbols": syms})
    if p == "/fapi/v1/ticker/24hr":
        return httpx.Response(200, json=[
            {"symbol": s, "lastPrice": "100", "highPrice": "105", "lowPrice": "100",
             "quoteVolume": v} for s, v in VOLS
        ])  # fmt: skip
    if p == "/fapi/v1/ticker/bookTicker":
        return httpx.Response(200, json=[
            {"symbol": s, "bidPrice": "99.99", "askPrice": "100.01"} for s in ("BTCUSDT", "ETHUSDT")
        ])  # fmt: skip
    if p == "/fapi/v1/premiumIndex":
        return httpx.Response(200, json=[
            {"symbol": s, "lastFundingRate": "0.0005"} for s in ("BTCUSDT", "ETHUSDT")
        ])  # fmt: skip
    if p == "/fapi/v1/depth":
        return httpx.Response(200, json={"bids": [["99.9", "1000"]], "asks": [["100.1", "500"]]})
    if p == "/fapi/v1/klines":
        ks = [[0, "0", "0", "0", "0", "0", 0, "100"] for _ in range(24)] + [
            [0, "0", "0", "0", "0", "0", 0, "400"]
        ]
        return httpx.Response(200, json=ks)
    if p == "/futures/data/openInterestHist":
        return httpx.Response(200, json=[{"sumOpenInterestValue": "100"},
                                         {"sumOpenInterestValue": "110"}])  # fmt: skip
    return httpx.Response(404)


async def test_binance_stats_source(now: datetime) -> None:
    client = httpx.AsyncClient(
        base_url="https://fapi.binance.com", transport=httpx.MockTransport(_binance_handler)
    )
    stats = await BinanceStatsSource(client).fetch(now)
    by = {s.symbol: s for s in stats}
    assert set(by) == {"BTCUSDT", "ETHUSDT"}  # XUSDT 不是在交易的永续，被排除
    b = by["BTCUSDT"]
    assert b.spread_bps == pytest.approx(2.0, rel=0.01)
    assert b.depth_usdt == pytest.approx(100.1 * 500)  # 取较小的一边
    assert b.volume_surge == pytest.approx(4.0)
    assert b.oi_change_pct == pytest.approx(10.0)
    assert b.range_24h_pct == pytest.approx(5.0)
    assert b.listed_days == 30
    assert b.funding_rate == 0.0005
    await client.aclose()


def test_format_derivatives() -> None:
    funding = [{"fundingRate": "0.0001"}, {"fundingRate": "-0.0002"}, {"fundingRate": "0.0005"}]
    oi = [{"sumOpenInterestValue": "100"}, {"sumOpenInterestValue": "112"}]
    text = format_derivatives(funding, oi)
    assert "+0.0500%" in text and "拥挤做多" in text
    assert "变化 +12.00%" in text
    assert "暂无数据" in format_derivatives([], [])


async def test_binance_stats_source_uses_liquidation_stats(now: datetime) -> None:
    class Liq:
        def liquidations_1h_usdt(self, symbol: str, at: datetime) -> float:
            return 123_456.0 if symbol == "BTCUSDT" else 0.0

    client = httpx.AsyncClient(
        base_url="https://fapi.binance.com", transport=httpx.MockTransport(_binance_handler)
    )
    stats = await BinanceStatsSource(client, liquidations=Liq()).fetch(now)
    by = {s.symbol: s for s in stats}
    assert (
        by["BTCUSDT"].liquidations_1h_usdt == 123_456.0 and by["ETHUSDT"].liquidations_1h_usdt == 0
    )
    await client.aclose()
