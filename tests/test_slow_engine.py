"""TradingAgents 适配层里不依赖真库、真 key 的部分。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from jiaoyi.contracts import Bias, Category, HardFilters, SignalKind, SizeTier, SymbolPoolEntry
from jiaoyi.slow.binance_stats import BinanceStatsSource
from jiaoyi.slow.tradingagents_engine import (
    TradingAgentsSettings,
    _CardSymbolDraft,
    _indicator,
    binance_symbol,
    build_config,
    draft_to_card_symbol,
    format_derivatives,
    get_binance_indicators,
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
    assert cfg["quick_think_llm"] == "deepseek-flash" and cfg["deep_think_llm"] == "deepseek-flash"
    assert cfg["output_language"].startswith("Chinese.")
    assert "PERPETUAL" in cfg["output_language"]  # 永续合约语境，不是股票
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
    v = [1.0] * 60
    assert _indicator("close_10_sma", o, h, low, c, v)[-1] == pytest.approx(55.5)
    assert _indicator("rsi", o, h, low, c, v)[-1] == 100.0
    assert len(_indicator("macdh", o, h, low, c, v)) == 60
    assert _indicator("atr", o, h, low, c, v)[-1] == pytest.approx(1.0, abs=1e-2)
    with pytest.raises(ValueError):
        _indicator("nope", o, h, low, c, v)


def test_boll_vwma_mfi() -> None:
    c = [10.0] * 30
    v = [1.0] * 30
    assert _indicator("boll", c, c, c, c, v)[-1] == pytest.approx(10.0)
    assert _indicator("boll_ub", c, c, c, c, v)[-1] == pytest.approx(10.0)  # 价格不动→带宽为 0
    rising = [float(x) for x in range(1, 41)]
    mid = _indicator("boll", rising, rising, rising, rising, [1.0] * 40)[-1]
    ub = _indicator("boll_ub", rising, rising, rising, rising, [1.0] * 40)[-1]
    lb = _indicator("boll_lb", rising, rising, rising, rising, [1.0] * 40)[-1]
    assert lb < mid < ub
    assert mid - lb == pytest.approx(ub - mid)
    # 成交量全压在最新一根：VWMA 就等于它的收盘价
    heavy = [0.0] * 39 + [5.0]
    assert _indicator("vwma", rising, rising, rising, rising, heavy)[-1] == pytest.approx(40.0)
    assert _indicator("mfi", rising, rising, rising, rising, [1.0] * 40)[-1] == 100.0
    assert len(_indicator("mfi", rising, rising, rising, rising, [1.0] * 40)) == 40


def test_unknown_indicator_does_not_fail_the_meeting(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [[i * 3_600_000, "1", "2", "0.5", "1.5", "10", 0, "15"] for i in range(30)]
    monkeypatch.setattr("jiaoyi.slow.tradingagents_engine._klines", lambda *a, **k: rows)
    out = get_binance_indicators("BTC", "nope", "2026-10-23", 2)
    assert "boll_ub" in out and "nope" in out


def test_draft_conversion_normalizes_and_blocks_no_trade_signals() -> None:
    d = _CardSymbolDraft(
        bias=Bias.NO_TRADE,
        key_levels=[],
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


def test_directional_card_with_empty_signals_gets_default_set() -> None:
    d = _CardSymbolDraft(
        bias=Bias.SHORT,
        key_levels=[],
        allowed_signals=[],
        size_tier_max=SizeTier.S,
        notional_min=100,
        notional_max=500,
        leverage_min=1,
        leverage_max=2,
        stop_atr_min=1,
        stop_atr_max=2,
        take_profit_r_min=1.5,
        take_profit_r_max=3,
        reason="偏空",
    )
    cs = draft_to_card_symbol(d, entry("XRPUSDT", Category.ALT))
    assert set(cs.allowed_signals) == set(SignalKind)  # 方向卡信号不许为空
    assert "按默认全部信号" in cs.reason


VOLS = (("BTCUSDT", "9e9"), ("ETHUSDT", "5e9"), ("XUSDT", "1e9"), ("TSLAUSDT", "2e9"))


def _binance_handler(req: httpx.Request) -> httpx.Response:
    p = req.url.path
    now_ms = int(datetime(2026, 10, 22, tzinfo=UTC).timestamp() * 1000)
    if p == "/fapi/v1/exchangeInfo":
        syms = [
            {"symbol": s, "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT",
             "onboardDate": now_ms - 30 * 86_400_000}
            for s in ("BTCUSDT", "ETHUSDT")
        ]  # fmt: skip
        syms.append({"symbol": "TSLAUSDT", "status": "TRADING", "contractType": "TRADIFI_PERPETUAL",
                     "quoteAsset": "USDT", "underlyingType": "EQUITY",
                     "onboardDate": now_ms - 30 * 86_400_000})  # fmt: skip
        return httpx.Response(200, json={"symbols": syms})
    if p == "/fapi/v1/ticker/24hr":
        return httpx.Response(200, json=[
            {"symbol": s, "lastPrice": "100", "highPrice": "105", "lowPrice": "100",
             "priceChangePercent": "-1.5", "quoteVolume": v} for s, v in VOLS
        ])  # fmt: skip
    if p == "/fapi/v1/ticker/bookTicker":
        return httpx.Response(200, json=[
            {"symbol": s, "bidPrice": "99.99", "askPrice": "100.01"}
            for s in ("BTCUSDT", "ETHUSDT", "TSLAUSDT")
        ])  # fmt: skip
    if p == "/fapi/v1/premiumIndex":
        return httpx.Response(200, json=[
            {"symbol": s, "lastFundingRate": "0.0005"} for s in ("BTCUSDT", "ETHUSDT", "TSLAUSDT")
        ])  # fmt: skip
    if p == "/fapi/v1/depth":
        return httpx.Response(200, json={"bids": [["99.9", "1000"]], "asks": [["100.1", "500"]]})
    if p == "/fapi/v1/klines":
        # 整点后 30 分钟（conftest.now = 06:30 UTC）：最后一根 06:00 开的半截小时
        limit = int(req.url.params["limit"])
        hour = 3_600_000
        start = int(datetime(2026, 10, 22, 6, tzinfo=UTC).timestamp() * 1000)
        ks = [
            [start - (limit - 1 - i) * hour, "100", "101", "99", "100", "1",
             start - (limit - 1 - i) * hour + hour - 1,
             "400" if i == limit - 1 else "100", 10, "0.5",
             "240" if i == limit - 1 else "60", "0"]
            for i in range(limit)
        ]  # fmt: skip
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
    assert set(by) == {"BTCUSDT", "ETHUSDT", "TSLAUSDT"}  # XUSDT 不是在交易的永续，被排除
    assert "tradifi_perpetual" in by["TSLAUSDT"].kind and "equity" in by["TSLAUSDT"].kind
    b = by["BTCUSDT"]
    assert b.spread_bps == pytest.approx(2.0, rel=0.01)
    assert b.depth_usdt == pytest.approx(100.1 * 500)  # 取较小的一边
    assert b.volume_surge == pytest.approx(8.0)  # 本小时走了 30 分钟：400 ÷ 0.5 小时 ÷ 100
    assert b.volume_surge_old == pytest.approx(4.0)  # 旧算法：半截小时直接比
    assert b.range_hold_pct == pytest.approx(2.0) and b.hold_hours == 2.0
    assert b.metrics["taker_buy_1h"] == pytest.approx(60.0)
    assert b.metrics["chg_24h"] == -1.5 and b.metrics["pos_24h"] == 0.0
    assert b.oi_change_pct == pytest.approx(10.0)
    assert b.range_24h_pct == pytest.approx(5.0)
    assert b.listed_days == 30
    assert b.funding_rate == 0.0005
    await client.aclose()


async def test_market_stats_refreshes_original_symbols_outside_current_top_k(now: datetime) -> None:
    async with httpx.AsyncClient(
        base_url="https://fapi.binance.com", transport=httpx.MockTransport(_binance_handler)
    ) as client:
        source = BinanceStatsSource(client, top_k=1)
        assert {s.symbol for s in await source.fetch(now)} == {"BTCUSDT"}
        snapshot = await source.fetch_symbols(["TSLAUSDT"], now)
        assert {s.symbol for s in snapshot} == {"BTCUSDT", "TSLAUSDT"}
        assert next(s for s in snapshot if s.symbol == "TSLAUSDT").metrics["chg_24h"] == -1.5


def test_format_derivatives() -> None:
    funding = [{"fundingRate": "0.0001"}, {"fundingRate": "-0.0002"}, {"fundingRate": "0.0005"}]
    oi = [{"sumOpenInterestValue": "100"}, {"sumOpenInterestValue": "112"}]
    text = format_derivatives(funding, oi)
    assert "+0.0500%" in text and "crowded long" in text
    assert "+12.00%" in text
    assert "no data" in format_derivatives([], [])


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


def test_wilder_atr_matches_fast_loop_formula() -> None:
    from jiaoyi.slow.tradingagents_engine import wilder_atr

    # 每根 [open_time, o, h, l, c]：区间恒为 2、收盘不跳空 → 每根 TR=2，ATR=2
    rows = [[i, 100, 101, 99, 100] for i in range(20)]
    assert wilder_atr(rows) == pytest.approx(2.0)
    assert wilder_atr(rows[:10]) is None  # 根数不够
    # 最后一根放大：Wilder 平滑 (2*13 + 15)/14
    rows.append([20, 100, 108, 93, 100])
    assert wilder_atr(rows) == pytest.approx((2.0 * 13 + 15) / 14)


def test_convert_prompt_states_configured_atr_interval_and_value() -> None:
    from jiaoyi.contracts import Category
    from jiaoyi.core.config import load_risk_config
    from jiaoyi.slow.commander import StopScale
    from jiaoyi.slow.tradingagents_engine import atr_interval, convert_prompt, describe_atr
    from tests.conftest import ROOT
    from tests.test_slow_commander import entry

    risk = load_risk_config(ROOT / "config" / "risk.example.toml")
    st = StopScale()
    m, lo, hi = st.period_min, st.min_stop_atr, st.max_stop_atr
    note = describe_atr(1.5, 100.0, st)
    p = convert_prompt(entry("AAUSDT", cat=Category.ALT), risk, "震荡", "", "结论正文", note)
    assert f"stop_atr = {atr_interval(m)} ATR" in p  # 周期跟风控配置，不写死
    assert "stop_atr = 1.5" in p and "1.50%" in p
    assert f"between {lo:g} and {hi:g}" in p  # 告诉军师风控员认的倍数区间
    assert f"between {st.min_tp_r:g} and {st.max_tp_r:g}" in p  # 止盈倍数区间也写明
    assert "unavailable" in describe_atr(None, None, st)
    assert [atr_interval(x) for x in (5, 15, 30, 60)] == ["5m", "15m", "30m", "1h"]
    assert "stop_atr = 30m ATR" in describe_atr(1.0, 10.0, StopScale(period_min=30))


def test_cost_meter_prefers_reported_cost_and_estimates_the_rest() -> None:
    from types import SimpleNamespace as NS

    from jiaoyi.slow.tradingagents_engine import CostMeter

    prices = {"flash": (0.30, 1.20), "pro": (0.50, 2.00)}
    m = CostMeter(prices)

    def resp(cost: float | None, tin: int, tout: int, where: str = "llm_output") -> NS:
        usage = {} if cost is None else {"cost": cost}
        msg = NS(
            usage_metadata={"input_tokens": tin, "output_tokens": tout},
            response_metadata={
                "model_name": "deepseek/deepseek-v4.1-flash",
                **({"token_usage": usage} if where == "message" else {}),
            },
        )
        return NS(
            llm_output={"token_usage": usage} if where == "llm_output" else {},
            generations=[[NS(message=msg)]],
        )

    m.record_response(resp(0.01, 5000, 500))  # 通道报了：用真实值
    m.record_response(resp(0.002, 100, 10, where="message"))  # 在消息的 response_metadata 里
    assert m.cost == pytest.approx(0.012)
    m.record_response(resp(None, 1_000_000, 1_000_000))  # 没报：按单价估这一次 = 0.30 + 1.20
    assert m.cost == pytest.approx(1.512)
    assert m.calls == 3
    m.abort()
    with pytest.raises(RuntimeError, match="取消"):
        m.check()


def test_engine_reports_partial_cost_and_aborts() -> None:
    import threading

    from jiaoyi.slow.tradingagents_engine import TradingAgentsEngine, TradingAgentsSettings

    eng = TradingAgentsEngine.__new__(
        TradingAgentsEngine
    )  # 构造函数要 tradingagents 包，这里不需要
    eng._s = TradingAgentsSettings()
    eng._inflight = {}
    eng._live = {}
    eng._inflight_lock = threading.Lock()
    meter = eng._meter("SOLUSDT")
    meter.add("deepseek/deepseek-v4.1-flash", 0, 0, 0.04)
    assert eng.partial_cost("SOLUSDT") == pytest.approx(0.04)
    eng.abort("SOLUSDT")
    with pytest.raises(RuntimeError):
        meter.check()
    eng._done("SOLUSDT", meter)
    assert eng.partial_cost("SOLUSDT") is None


def test_engine_late_finishing_timed_out_meeting_keeps_the_next_meetings_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json
    import threading

    import jiaoyi.slow.tradingagents_engine as te

    monkeypatch.setattr(te, "cost_handler", lambda meter: None)  # CI 不装 LangChain
    eng = te.TradingAgentsEngine.__new__(te.TradingAgentsEngine)
    eng._s = TradingAgentsSettings(live_dir=str(tmp_path))
    eng._inflight, eng._live = {}, {}
    eng._inflight_lock = threading.Lock()
    release = {1: threading.Event(), 2: threading.Event()}
    started = {1: threading.Event(), 2: threading.Event()}
    turn = iter([1, 2])

    def run(*args: Any) -> Any:
        n = next(turn)
        started[n].set()
        assert release[n].wait(timeout=5)
        raise RuntimeError(f"第 {n} 场的线程结束")

    eng._analyze_run = run  # type: ignore[method-assign]

    def meeting() -> None:
        with pytest.raises(te.MeetingError):
            eng._analyze_sync(entry("SOLUSDT", Category.MAJOR), "震荡")

    first = threading.Thread(target=meeting)
    first.start()
    assert started[1].wait(timeout=5)
    eng.abort("SOLUSDT")  # 第一场超时取消，后台线程还在跑
    second = threading.Thread(target=meeting)
    second.start()
    assert started[2].wait(timeout=5)
    release[1].set()
    first.join(timeout=5)
    eng.abort("SOLUSDT")  # 第二场也超时：实况要标成失败，不能停在「正在开」
    doc = json.loads((tmp_path / "SOLUSDT.json").read_text(encoding="utf-8"))
    assert doc["status"] == "failed" and doc["error"] == "超时取消，会议没有结论"
    release[2].set()
    second.join(timeout=5)
    assert not first.is_alive() and not second.is_alive()
    assert eng._live == {}


def test_empty_speeches_detects_label_only_debate() -> None:
    from jiaoyi.slow.tradingagents_engine import empty_speeches

    shell = {
        "investment_debate_state": {
            "bull_history": "\nBull Analyst: ",
            "bear_history": "\nBear Analyst: 有内容",
        },
        "risk_debate_state": {
            "aggressive_history": "\nAggressive Analyst: 激进",
            "conservative_history": "\nConservative Analyst: 保守",
            "neutral_history": "",
        },
    }
    assert empty_speeches(shell, 1, 1) == ["多头", "中立派"]
    assert empty_speeches(shell, 0, 0) == []  # 轮数为 0：本来就没人发言
    assert empty_speeches({}, 1, 1) == ["多头", "空头", "激进派", "保守派", "中立派"]


def test_cost_meter_counts_truncated_answers() -> None:
    from types import SimpleNamespace as NS

    from jiaoyi.slow.tradingagents_engine import CostMeter

    m = CostMeter({"flash": (0.3, 1.2), "pro": (0.5, 2.0)})

    def resp(reason: str, where: str) -> NS:
        msg = NS(
            usage_metadata={},
            response_metadata={"finish_reason": reason} if where == "meta" else {},
        )
        info = {"finish_reason": reason} if where == "info" else {}
        return NS(llm_output={}, generations=[[NS(message=msg, generation_info=info)]])

    m.record_response(resp("stop", "info"))
    m.record_response(resp("length", "info"))
    m.record_response(resp("length", "meta"))
    assert m.truncated == 2


def test_openrouter_request_body_carries_usage_and_reasoning() -> None:
    from types import SimpleNamespace

    from jiaoyi.slow.tradingagents_engine import apply_openrouter_options, openrouter_body

    assert openrouter_body("low") == {
        "usage": {"include": True},
        "reasoning": {"effort": "low"},
    }
    assert openrouter_body("none")["reasoning"] == {"enabled": False}
    llm = SimpleNamespace(extra_body={"keep": 1})
    apply_openrouter_options(llm, "medium")
    assert llm.extra_body == {
        "keep": 1,
        "usage": {"include": True},
        "reasoning": {"effort": "medium"},
    }
    bare = SimpleNamespace()
    apply_openrouter_options(bare, "high")  # 原来没有 extra_body 也行
    assert bare.extra_body["reasoning"] == {"effort": "high"}


class _Msg:
    def __init__(self, content: str, finish: str = "stop", tool_calls: list[object] | None = None):
        self.content, self.response_metadata = content, {"finish_reason": finish}
        self.tool_calls = tool_calls or []


class _FakeLlm:
    def __init__(self, replies: list[_Msg]) -> None:
        self.replies, self.calls = replies, []  # type: ignore[var-annotated]
        self.extra_body = {"usage": {"include": True}, "reasoning": {"effort": "low"}}

    def invoke(self, inp: object, config: object = None, **kw: object) -> _Msg:
        self.calls.append((inp, kw))
        return self.replies.pop(0)


def test_reply_problem_kinds() -> None:
    from jiaoyi.slow.tradingagents_engine import reply_problem

    assert reply_problem(_Msg("很长的回答" * 10), 20) is None
    assert reply_problem(_Msg("被截断了", finish="length"), 20) == "length"
    assert reply_problem(_Msg("  "), 20) == "empty"
    assert reply_problem(_Msg("太短"), 20) == "short"
    assert reply_problem(_Msg("", tool_calls=[object()]), 20) is None  # 调工具的回答正文可以空


def test_bad_step_is_retried_once_without_reasoning() -> None:
    from jiaoyi.slow.tradingagents_engine import RETRY_HINT, CostMeter, add_retry

    meter = CostMeter({"flash": (0.3, 1.2), "pro": (1.0, 4.0)})
    meter.truncated = 1  # 回调已经把这次截断记上了
    seen: list[str] = []
    meter.on_retry = seen.append
    llm = _FakeLlm([_Msg("半句", finish="length"), _Msg("这是重试后给出的完整结论" * 3)])
    add_retry(llm, meter, reasoning_off=True, min_chars=20)
    out = llm.invoke("请分析", {"callbacks": []}, max_tokens=100)
    assert out.content.startswith("这是重试")
    (first_in, first_kw), (second_in, second_kw) = llm.calls
    assert first_in == "请分析" and "extra_body" not in first_kw
    assert second_in == f"请分析\n\n{RETRY_HINT}"
    assert second_kw["extra_body"]["reasoning"] == {"enabled": False}  # type: ignore[index]
    assert second_kw["extra_body"]["usage"] == {"include": True}  # type: ignore[index]
    assert second_kw["max_tokens"] == 100
    assert (meter.retries, meter.truncated, seen) == (1, 0, ["length"])  # 重试成功，不再算截断
    # 好回答不重试；重试仍坏就原样交回、截断计数保留
    ok = _FakeLlm([_Msg("没问题的完整回答" * 5)])
    add_retry(ok, meter, reasoning_off=False, min_chars=20)
    ok.invoke([("human", "x")])
    assert len(ok.calls) == 1
    meter.truncated = 1
    bad = _FakeLlm([_Msg("", finish="length"), _Msg("", finish="length")])
    add_retry(bad, meter, reasoning_off=False, min_chars=20)
    bad.invoke("q")
    assert "extra_body" not in bad.calls[1][1] and meter.truncated == 1


def test_model_max_tokens_from_openrouter_info_or_fallback() -> None:
    from jiaoyi.slow.tradingagents_engine import FALLBACK_MAX_TOKENS, model_max_tokens

    data = [
        {"id": "a/x", "top_provider": {"max_completion_tokens": 900_000}},
        {"id": "a/y", "top_provider": {"max_completion_tokens": 131_072}},
        {"id": "a/none", "top_provider": {"max_completion_tokens": None}},
    ]
    assert model_max_tokens("openrouter", ["a/x"], lambda: data) == 900_000
    assert model_max_tokens("openrouter", ["a/x", "a/y"], lambda: data) == 131_072  # 取小
    assert (
        model_max_tokens("openrouter", ["a/none"], lambda: data)
        == FALLBACK_MAX_TOKENS["openrouter"]
    )
    assert model_max_tokens("openrouter", ["a/missing"], lambda: data) == 384_000

    def boom() -> list[dict[str, object]]:
        raise OSError("断网")

    assert model_max_tokens("openrouter", ["a/x"], boom) == 384_000
    assert model_max_tokens("deepseek", ["deepseek-flash"], boom) == 8192


def test_record_response_reports_reasoning_tokens() -> None:
    from types import SimpleNamespace

    from jiaoyi.slow.tradingagents_engine import CostMeter

    meter = CostMeter({"flash": (0.3, 1.2), "pro": (1.0, 4.0)})
    calls: list[tuple[str | None, int, int]] = []
    meter.on_call = lambda r, t, rt, _retry: calls.append((r, t, rt))
    msg = SimpleNamespace(
        response_metadata={"finish_reason": "stop"},
        usage_metadata={
            "input_tokens": 10,
            "output_tokens": 500,
            "output_token_details": {"reasoning": 300},
        },
    )
    gen = SimpleNamespace(message=msg, generation_info={"finish_reason": "stop"})
    meter.record_response(SimpleNamespace(llm_output={}, generations=[[gen]]))
    assert calls == [("stop", 500, 300)]
    raw = SimpleNamespace(
        response_metadata={"token_usage": {"completion_tokens_details": {"reasoning_tokens": 7}}},
        usage_metadata={"output_tokens": 9},
    )
    meter.record_response(
        SimpleNamespace(
            llm_output={}, generations=[[SimpleNamespace(message=raw, generation_info={})]]
        )
    )
    assert calls[1][2] == 7


def test_settings_max_tokens_none_means_model_max() -> None:
    from jiaoyi.slow.settings import SlowConfig

    s = SlowConfig().engine_settings()
    assert s.max_tokens is None and s.json_max_tokens is None and s.retry_bad_steps
    s2 = SlowConfig.model_validate({"meeting": {"max_tokens": 4096}}).engine_settings()
    assert (s2.max_tokens, s2.json_max_tokens) == (4096, 8192)


def test_quality_gate_short_speech_and_broken_reports() -> None:
    from jiaoyi.slow.tradingagents_engine import broken_reports, empty_speeches

    state = {
        "investment_debate_state": {
            "bull_history": "Bull Analyst: " + "多" * 60,
            "bear_history": "Bear Analyst: 太短",
        },
        "risk_debate_state": {
            k: f"{lab}: " + "字" * 60
            for k, lab in (
                ("aggressive_history", "Aggressive Analyst"),
                ("conservative_history", "Conservative Analyst"),
                ("neutral_history", "Neutral Analyst"),
            )
        },
    }
    assert empty_speeches(state, 1, 1, min_chars=50) == ["空头"]
    assert empty_speeches(state, 1, 1) == []  # 不设下限只查空
    assert empty_speeches(state, 2, 1, min_chars=50) == ["多头", "空头"]  # 两轮 = 至少 100 字
    reports = {
        "market_report": "技术面" * 30,
        "sentiment_report": "",
        "news_report": "Error calling tool: rate limit exceeded",
        "fundamentals_report": "基本面良好" * 20,
    }
    assert broken_reports(reports, ("market", "social", "news"), 50) == [
        ("social", "社交情绪"),
        ("news", "新闻"),
    ]
    assert broken_reports(reports, ("fundamentals",), 50) == []  # 没请的分析师不查
    long_ok = {"news_report": "The error rate of the order book is low. " * 20}
    assert broken_reports(long_ok, ("news",), 50) == []  # 长报告里提到 error 不算报错


def test_direction_conflict_and_size_degrade() -> None:
    from jiaoyi.contracts import Bias, SizeTier
    from jiaoyi.slow.tradingagents_engine import degrade_size, direction_conflict
    from tests.test_slow_commander import card_symbol

    plan = "...\nFINAL TRANSACTION PROPOSAL: **SELL**"
    assert "做多" in (direction_conflict(Bias.LONG, "Buy", plan) or "")  # 交易员说卖
    # 没有 FINAL TRANSACTION PROPOSAL 行（模型自由文本）时看 **Action** 行
    assert "做多" in (
        direction_conflict(Bias.LONG, "Buy", "**Action**: Sell\n\n**Reasoning**: x") or ""
    )
    assert direction_conflict(Bias.LONG, "Buy", "**Action**: Buy") is None
    assert direction_conflict(Bias.LONG, "Underweight", "") is not None
    assert direction_conflict(Bias.SHORT, "Overweight", "") is not None
    assert direction_conflict(Bias.SHORT, "Sell", plan) is None
    assert direction_conflict(Bias.LONG, "Hold", "FINAL TRANSACTION PROPOSAL: **HOLD**") is None
    assert direction_conflict(Bias.LONG, None, "") is None  # 解析不出不算矛盾
    assert direction_conflict(Bias.NO_TRADE, "Sell", plan) is None
    assert direction_conflict(Bias.BOTH, "Buy", plan) is None  # 双向：不算矛盾
    cs = degrade_size(card_symbol("AAUSDT", size_tier_max=SizeTier.L), ["新闻", "社交情绪"])
    assert cs.size_tier_max is SizeTier.M and "新闻、社交情绪报告缺失" in cs.reason
    assert (
        degrade_size(card_symbol("AAUSDT", size_tier_max=SizeTier.S), ["新闻"]).size_tier_max
        is SizeTier.S
    )


def test_abort_estimates_inflight_calls_and_late_result_corrects() -> None:
    from types import SimpleNamespace

    from jiaoyi.slow.tradingagents_engine import CostMeter

    prices = {"flash": (0.30, 1.20), "pro": (1.0, 4.0)}
    meter = CostMeter(prices)
    corrections: list[float] = []
    meter.on_late = corrections.append
    # 一次已完成的回答（真实花费 0.001、输出 3000 token），一次还在途（输入 20000 字）
    done = SimpleNamespace(
        llm_output={"token_usage": {"cost": 0.001}, "model_name": "m"},
        generations=[
            [
                SimpleNamespace(
                    message=SimpleNamespace(
                        response_metadata={},
                        usage_metadata={"input_tokens": 10, "output_tokens": 3000},
                    ),
                    generation_info={},
                )
            ]
        ],
    )
    meter.start_call("done", 100, "deepseek/flash")
    meter.record_response(done, "done")
    meter.start_call("fly", 20_000, "deepseek/flash")
    assert meter.cost == 0.001 and not meter.estimated
    meter.abort()
    est = round((10_000 * 0.30 + 3000 * 1.20) / 1e6, 6)  # 估的输入 1 万 token + 平均输出 3000
    assert meter.estimated and meter.cost == round(0.001 + est, 6)
    # 真实结果晚到：估算换成真实值，差额回调；不会重复计
    late = SimpleNamespace(
        llm_output={"token_usage": {"cost": 0.02}, "model_name": "m"}, generations=[]
    )
    meter.record_response(late, "fly")
    assert not meter.estimated and meter.cost == 0.021
    assert corrections == [round(0.02 - est, 6)]
    # 在途请求报错结束：不再算在途
    m2 = CostMeter(prices)
    m2.start_call("x", 4000, "")
    m2.drop_call("x")
    m2.abort()
    assert m2.cost == 0.0 and not m2.estimated


def test_retry_call_is_marked_in_on_call() -> None:
    from types import SimpleNamespace

    from jiaoyi.slow.tradingagents_engine import CostMeter, add_retry

    meter = CostMeter({"flash": (0.3, 1.2), "pro": (1.0, 4.0)})
    seen: list[tuple[str | None, int]] = []
    meter.on_call = lambda r, _t, _rt, retry: seen.append((r, retry))

    def respond(finish: str) -> None:  # 模拟回调：每次请求结束时记一次
        gen = SimpleNamespace(message=None, generation_info={"finish_reason": finish})
        meter.record_response(SimpleNamespace(llm_output={}, generations=[[gen]]))

    class Llm(_FakeLlm):
        def invoke(self, inp: object, config: object = None, **kw: object) -> _Msg:
            msg = super().invoke(inp, config, **kw)
            respond(msg.response_metadata["finish_reason"])
            return msg

    llm = Llm([_Msg("半句", finish="length"), _Msg("重试后的完整回答" * 5)])
    add_retry(llm, meter, reasoning_off=False, min_chars=20)
    llm.invoke("q")
    assert seen == [("length", 0), ("stop", 1)]  # 只有第二次是重试


def test_meter_reports_the_model_of_each_call() -> None:
    from types import SimpleNamespace

    from jiaoyi.slow.tradingagents_engine import CostMeter

    meter = CostMeter({"flash": (0.3, 1.2)})
    seen: list[str] = []
    meter.on_model = seen.append
    gen = SimpleNamespace(message=None, generation_info={"finish_reason": "stop"})
    meter.record_response(SimpleNamespace(llm_output={"model_name": "flash"}, generations=[[gen]]))
    assert seen == ["flash"]


def test_convert_instructions_describe_new_signal_kinds() -> None:
    from jiaoyi.core.config import load_risk_config
    from jiaoyi.slow import ta_prompts
    from jiaoyi.slow.tradingagents_engine import convert_instructions
    from tests.conftest import ROOT

    risk = load_risk_config(ROOT / "config" / "risk.example.toml")
    short = convert_instructions(risk, ta_prompts.PromptParams.for_hold("1h"))
    long_ = convert_instructions(risk, ta_prompts.PromptParams.for_hold("24h"))
    for kind in ("oi_follow", "top_vs_crowd", "btc_lead"):
        assert kind in short and kind in long_
    for text in (short, long_):
        assert "Choose allowed_signals from the eight signals below" in text
        assert "chase-style" not in text and "pure breakout chase" not in text
        assert "older" not in text and "newer" not in text
        for kind in (
            "volume_breakout", "volume_surge", "liquidation_rebound", "crowded_reversal",
            "session_event",
        ):  # fmt: skip
            assert f"- {kind}:" in text
        assert "a conclusion that waits for a pullback zone does not exclude it" in text


def _pm_logs() -> dict[str, dict[str, str]]:
    import json
    from pathlib import Path

    return json.loads(
        (Path(__file__).parent / "fixtures" / "pm_empty_lines.json").read_text("utf-8")
    )


def test_empty_trade_lines_are_filled_from_the_portfolio_summary() -> None:
    from jiaoyi.slow.tradingagents_engine import fill_trade_lines

    expect = {
        "1625_SOLUSDT": ("98.60–98.80", "98.00", "100.30", "S"),
        "1656_1000PEPEUSDT": ("0.0042530–0.0042600", "0.0042280", "0.0043150", "S"),
        "1656_SOLUSDT": ("100.60–100.75", "101.55", "99.30", "S"),
    }
    for name, (zone, stop, target, size) in expect.items():
        log = _pm_logs()[name]
        text, filled = fill_trade_lines(log["decision"], log["trader"])
        assert filled == ["Entry Zone", "Invalidation", "Take Profit", "Size Tier"], name
        assert f"**Entry Zone**: {zone}" in text, name
        assert f"**Invalidation**: {stop}" in text, name
        assert f"**Take Profit**: {target}" in text, name
        assert f"**Size Tier**: {size}" in text, name


def test_trade_lines_fall_back_to_the_trader_then_stay_empty() -> None:
    from jiaoyi.slow.tradingagents_engine import fill_trade_lines

    log = _pm_logs()["1625_SOLUSDT"]
    bare = (
        log["decision"]
        .replace("98.60–98.80", "回踩带")
        .replace("失效 98.00", "")
        .replace("目标 100.30", "")
    )
    text, filled = fill_trade_lines(bare, log["trader"])
    assert "**Entry Zone**: 98.60–99.00" in text and "Entry Zone" in filled  # 交易员那行
    none_text, none_filled = fill_trade_lines(bare, "")
    assert none_filled == ["Size Tier"] and "**Entry Zone**: none" in none_text  # 两处都没有 → 照旧


def test_hold_rating_keeps_none_lines() -> None:
    from jiaoyi.slow.tradingagents_engine import fill_trade_lines

    log = _pm_logs()["1625_SOLUSDT"]
    hold = log["decision"].replace("**Rating**: Overweight", "**Rating**: Hold")
    assert fill_trade_lines(hold, log["trader"]) == (hold, [])
