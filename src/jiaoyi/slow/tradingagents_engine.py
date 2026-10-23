"""TradingAgents + DeepSeek 的会议引擎。

TradingAgents：Apache-2.0，v0.7.0，提交 e8f5503，钉在 pyproject 的 slow 依赖里；
不复制它的代码，升级版本要单独开 PR 并重跑真机验证。

只在这个文件里导入 ``tradingagents`` / ``langchain_core``，且都是用到时才导入，
所以没装 ``jiaoyi[slow]`` 或没有 key 时其余代码和测试照常跑。

我们对 TradingAgents 做的三处接入，不改它的源码：
1. 配置：deepseek provider、中文、1 轮辩论 + 1 轮风控、最大输出 8192、6 次重试
2. 数据：往它的 ``VENDOR_METHODS`` 里登记一个 ``binance`` 数据源（K 线、指标），
   美股永续的财报 / 新闻仍走它自带的工具
3. 输出：它的结论是一段文字，再用一次便宜的结构化调用转成 ``CardSymbol``，校验失败重试一次

⚠ 本文件对着 TradingAgents 0.7.0 的公开接口写成，还没用真 key 跑过；M2 拿到 DeepSeek key 后
要做一次真机验证（见 PR 说明）。
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, Field, ValidationError

from jiaoyi.contracts import (
    BattleCard,
    Bias,
    CardSymbol,
    Category,
    KeyLevel,
    LevelKind,
    Range,
    RiskConfig,
    SignalKind,
    SizeTier,
    SymbolPoolEntry,
)
from jiaoyi.interfaces import SecretStore
from jiaoyi.slow.brief import MarketBriefService
from jiaoyi.slow.commander import (
    DEFAULT_STOP_SCALE,
    Analysis,
    MeetingError,
    Pick,
    PickResult,
    StopScale,
)
from jiaoyi.slow.levels import (
    SPAN_SIGMA,
    LevelContext,
    build_candidates,
    describe_candidates,
    holding_span,
    select_levels,
)
from jiaoyi.slow.liquidations import LiquidationSource, describe_total
from jiaoyi.slow.meeting_live import MeetingLive, tap_graph
from jiaoyi.slow.news import NewsHub, format_items
from jiaoyi.slow.social_guard import RedditGuard, register_reddit_guard

_STOCK_LIKE = frozenset({Category.US_TECH, Category.CRYPTO_STOCK})


# 军师团可选的大模型来源：直连 DeepSeek（默认），或经 OpenRouter 用同一批 DeepSeek 模型。
# 模型 id 核对于 2026-10-23（OpenRouter /api/v1/models）：deepseek-flash 对应 V4.1 Flash。
# Aaron 2026-10-23 决定：深度档也用 4.1（flash），不用 v4-pro；想换回去在 slow.toml 写 deep_model。
PROVIDER_KEY_ENV: Mapping[str, str] = {
    "deepseek": "DEEPSEEK_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}
PROVIDER_MODELS: Mapping[str, tuple[str, str]] = {  # (quick, deep)
    "deepseek": ("deepseek-flash", "deepseek-flash"),
    "openrouter": ("deepseek/deepseek-v4.1-flash-1017", "deepseek/deepseek-v4.1-flash-1017"),
}


@dataclass(frozen=True)
class TradingAgentsSettings:
    provider: str = "deepseek"
    quick_model: str = "deepseek-flash"
    deep_model: str = "deepseek-flash"
    output_language: str = "Chinese"
    debate_rounds: int = 1
    risk_rounds: int = 1
    # None = 用模型自己的最大输出（引擎启动时读 OpenRouter 模型信息，读不到用兜底值）。
    # 思考模型的思考也算在里面；4096 实测会把辩论回答挤成空的
    max_tokens: int | None = 8192
    # 整理作战指令 / 挑币要输出结构化 JSON，单独给足长度，免得被截断；None = max(8192, max_tokens)
    json_max_tokens: int | None = 8192
    # 某一步回答被截断（length）/ 空 / 过短：自动重试一次，重试关掉推理并提示直接给结论
    retry_bad_steps: bool = True
    retry_min_chars: int = 20
    # 会议质量门槛：每个辩手每轮发言至少多少字；分析师报告至少多少字（低于就算缺失）
    speech_min_chars: int = 50
    report_min_chars: int = 50
    max_retries: int = 6
    # 每类标的请哪几位分析师（越少越省钱）；默认与 TradingAgents 的完整阵容一致。
    analysts_crypto: tuple[str, ...] = ("market", "social", "news")
    analysts_stock: tuple[str, ...] = ("market", "social", "news", "fundamentals")
    # 挑币会议、整理作战指令这两步用哪档模型："quick"=flash，"deep"=深度档模型。
    pick_tier: str = "quick"
    convert_tier: str = "quick"
    # 每百万 token 的美元价（输入, 输出），只在拿不到通道报的真实花费时估算用（见 CostMeter）。
    # flash：OpenRouter deepseek/deepseek-v4.1-flash 标价 0.30 / 1.20（2026-10-23 Aaron 核对；
    # 缓存命中输入 0.006 不单算，估算会偏高）。pro 仍是占位值，用到前按官网核对。
    price_per_mtok: Mapping[str, tuple[float, float]] = field(
        default_factory=lambda: {"flash": (0.30, 1.20), "pro": (0.50, 2.00)}
    )
    results_dir: str = "data/tradingagents"
    # Reddit 匿名 RSS 限流很严：guarded=加闸（缓存 + 间隔 + 冷却），off=不抓。见 social_guard.py。
    reddit: str = "guarded"
    # 每笔单最多持有多久（分钟，档位的时间止损）：告诉军师团，止损 / 止盈 / 关键位按这个时长的波动给
    hold_minutes: int = 120
    # 分析师看的 K 线，每段 "周期 x 根数"（从细到粗）；None = 15m x 96、1h x 72、4h x 60。
    # 档位默认按最长持仓走（core/profiles.py），app.py 填进来
    analyst_klines: tuple[str, ...] | None = None
    # 开会间隔（分钟）：比最细那段 K 线周期还短时，最前面加一段 15m x 48，保证每次开会有新 K 线
    meeting_min: int = 120
    # 关键位的摆动 / 成交密集区用哪个周期的 K 线找；None = 跟 ATR 周期
    level_candidate_interval: str | None = None
    # 关键位范围 = level_span_sigma × ATR × √(持仓时长 / ATR 周期)，见 levels.py
    level_span_sigma: float = SPAN_SIGMA
    # 有止损倍数建议（StopScale.stop_hint）时，关键位范围 = level_span_stops × 倍数中值 × stop_atr
    level_span_stops: float = 2.5
    # 推理长度（OpenRouter reasoning.effort）：none/low/medium/high；直连 DeepSeek 忽略
    reasoning_effort: str = "low"
    # 开会过程实时落盘的目录（面板读）；空 = 不落盘。见 meeting_live.py
    live_dir: str = ""


# TradingAgents 的提示词是写死的股票语境（Buy/Overweight/Hold/Underweight/Sell，“减持”）。
# 它会把 output_language 原样拼进每个角色的提示词（"Write your entire response in {lang}, except
# the labelled lines…"），所以把永续合约的语境放在这里，不用改它的源码。
_PERP_BODY = (
    "Domain context for everything you write: this is Binance USDT-margined PERPETUAL FUTURES "
    "trading (go long, go short, or stay flat; leverage, funding, liquidations), not equities and "
    "there is no existing position to trim. Read the rating scale as: Buy = open or add a long, "
    "Overweight = lean long, Hold = stay flat / no new entry, Underweight = lean short or stay "
    "flat, Sell = open a short or close longs. In prose say long / short / flat and name concrete "
    "price levels from the data; avoid stock wording such as 'overweight', 'underweight', 'trim "
    "or reduce exposure' and 'earnings'."
)


def perp_context(hold_minutes: float) -> str:
    """永续合约语境 + 持仓时长：目标价、止损、关键位要按这段时间内这个币的正常波动来给。"""
    return (
        f"{_PERP_BODY} Every position here is held at most about {hold_minutes:g} minutes (forced "
        "exit at the time limit): size price targets, stops and levels to moves plausible within "
        "that horizon for this specific asset, never multi-day swing targets. Overbought/oversold "
        "readings and Bollinger band breaches tend to keep going in this market: never treat them "
        "alone as reversal signals. Apply all of this to every sentence you write"
    )


PERP_CONTEXT = perp_context(120)


def build_config(
    base: Mapping[str, Any], s: TradingAgentsSettings, *, crypto_news: bool = False
) -> dict[str, Any]:
    """在 TradingAgents 的 ``DEFAULT_CONFIG`` 上叠加我们的设置，返回新字典。"""
    cfg = dict(base)
    cfg.update(
        llm_provider=s.provider,
        quick_think_llm=s.quick_model,
        deep_think_llm=s.deep_model,
        output_language=f"{s.output_language}. {perp_context(s.hold_minutes)}",
        max_debate_rounds=s.debate_rounds,
        max_risk_discuss_rounds=s.risk_rounds,
        max_tokens=s.max_tokens,
        llm_max_retries=s.max_retries,
        results_dir=os.path.join(s.results_dir, "logs"),
        data_cache_dir=os.path.join(s.results_dir, "cache"),
        memory_log_path=os.path.join(s.results_dir, "memory.md"),
    )
    vendors = dict(cfg.get("data_vendors", {}))
    vendors.update(core_stock_apis="binance", technical_indicators="binance")
    if crypto_news:
        vendors["news_data"] = "crypto_news"
    cfg["data_vendors"] = vendors
    return cfg


def ta_ticker(entry: SymbolPoolEntry) -> tuple[str, str]:
    """币安代码 → (TradingAgents 的 ticker, asset_type)。美股永续用股票代码走财报 / 新闻。"""
    base = entry.symbol.removesuffix("USDT")
    if entry.category in _STOCK_LIKE:
        return base, "stock"
    return f"{base}-USD", "crypto"


def binance_symbol(ticker: str) -> str:
    """TradingAgents 传来的 ticker（BTC / BTC-USD / BTCUSDT）→ 币安永续代码。"""
    t = ticker.upper().removesuffix("-USD").removesuffix("USDT")
    return f"{t}USDT"


# ---------------------------------------------------------------- 币安数据源
_KL = "https://fapi.binance.com/fapi/v1/klines"


def _klines(symbol: str, interval: str, limit: int, end: datetime | None = None) -> list[Any]:
    params: dict[str, Any] = {
        "symbol": binance_symbol(symbol),
        "interval": interval,
        "limit": limit,
    }
    if end is not None:
        params["endTime"] = int(end.timestamp() * 1000)
    r = httpx.get(_KL, params=params, timeout=10.0)
    r.raise_for_status()
    data: list[Any] = r.json()
    return data


DEFAULT_ANALYST_KLINES = ("15m x 96", "1h x 72", "4h x 60")
FINEST_EXTRA = ("15m", 48)
_INTERVAL_MIN = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}
_ANALYST_KLINES: tuple[tuple[str, int], ...] = ()


def parse_kline_spec(spec: str) -> tuple[str, int]:
    """``"15m x 96"`` → ``("15m", 96)``。"""
    m = re.fullmatch(r"\s*(\d+[mhd])\s*[xX×]\s*(\d+)\s*", spec)
    if m is None or m[1] not in _INTERVAL_MIN or not 1 <= int(m[2]) <= 1000:
        raise ValueError(
            f"K 线写法应为「周期 x 根数」，如 15m x 96（周期 1m/5m/15m/30m/1h/4h/1d）：{spec!r}"
        )
    return m[1], int(m[2])


def set_analyst_klines(spec: Sequence[str] | None, meeting_min: int = 120) -> None:
    """设置分析师看的 K 线（模块级）。开会间隔比最细一段还短，最前面补 15m x 48。"""
    parsed = [parse_kline_spec(s) for s in (spec or DEFAULT_ANALYST_KLINES)]
    parsed.sort(key=lambda p: _INTERVAL_MIN[p[0]])
    if meeting_min < _INTERVAL_MIN[parsed[0][0]] and parsed[0][0] != FINEST_EXTRA[0]:
        parsed.insert(0, FINEST_EXTRA)
    global _ANALYST_KLINES
    _ANALYST_KLINES = tuple(parsed)


def as_of(date_str: str, now: datetime | None = None) -> datetime:
    """TradingAgents 只传日期：今天（或更晚）取现在，过去的日子取当天 0 点。

    不这样的话「今天」被当成 0 点，分析师看到的最新一根 K 线最多是 24 小时前的价格。
    """
    now = now or datetime.now(UTC)
    d = datetime.fromisoformat(date_str)
    if d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    return now if d.date() >= now.date() else d


def get_binance_stock_data(symbol: str, start_date: str, end_date: str) -> str:
    """登记为 TradingAgents 的 ``get_stock_data``：每周期一段 CSV，从细到粗，已收盘的。"""
    end = as_of(end_date)
    end_ms = int(end.timestamp() * 1000)
    parts: list[str] = []
    for interval, n in _ANALYST_KLINES or tuple(
        parse_kline_spec(s) for s in DEFAULT_ANALYST_KLINES
    ):
        rows = [k for k in _klines(symbol, interval, n + 1, end) if k[6] <= end_ms][-n:]
        if not rows:
            continue
        fmt = "%Y-%m-%d" if interval == "1d" else "%Y-%m-%d %H:%M"
        t0, t1 = (datetime.fromtimestamp(rows[i][0] / 1000, tz=UTC).strftime(fmt) for i in (0, -1))
        lines = ["Date,Open,High,Low,Close,Volume,QuoteVolume"]
        for k in rows:
            ts = datetime.fromtimestamp(k[0] / 1000, tz=UTC).strftime(fmt)
            lines.append(f"{ts},{k[1]},{k[2]},{k[3]},{k[4]},{k[5]},{k[7]}")
        parts.append(
            f"# {binance_symbol(symbol)} 币安永续 {interval} K 线，已收盘 {len(rows)} 根"
            f"（{t0} → {t1}，UTC）\n" + "\n".join(lines)
        )
    return "\n\n".join(parts) + "\n\n" + derivatives_context(symbol, end)


_FAPI = "https://fapi.binance.com"
logger = logging.getLogger(__name__)

_LIQ_TOTALS: Mapping[str, float] = {}


def set_liquidation_totals(totals: Mapping[str, float]) -> None:
    """登记各币近 1 小时强平总额的**快照**（由事件循环取好再传进来）。

    整体替换而不是原地修改，工作线程读到的永远是一份完整的旧字典或新字典。
    """
    global _LIQ_TOTALS
    _LIQ_TOTALS = totals


def format_derivatives(
    funding: Sequence[Mapping[str, Any]], oi: Sequence[Mapping[str, Any]]
) -> str:
    """资金费率历史 + 持仓量 24 小时变化 → 给分析师看的一段文字。纯函数，便于测试。"""
    lines = ["## 衍生品数据（币安 U 本位永续）"]
    if funding:
        rates = [float(f["fundingRate"]) * 100 for f in funding]
        hist = "，".join(f"{r:+.4f}%" for r in rates[-6:])
        lines.append(f"- 资金费率（%，最近 {min(len(rates), 6)} 次，由旧到新）：{hist}")
        lines.append(f"- 最新一次资金费率 {rates[-1]:+.4f}%（正：多头付费，拥挤做多；负相反）")
    if len(oi) >= 2:
        first, last = float(oi[0]["sumOpenInterestValue"]), float(oi[-1]["sumOpenInterestValue"])
        if first > 0:
            chg = (last - first) / first * 100
            lines.append(
                f"- 持仓量（USDT 计）{first:,.0f} → {last:,.0f}，{len(oi) - 1} 小时变化 {chg:+.2f}%"
            )
    if len(lines) == 1:
        lines.append("- （暂无数据）")
    return "\n".join(lines)


def derivatives_context(symbol: str, end: datetime) -> str:
    """资金费率和持仓量变化。失败只说明不可用，不让 K 线数据跟着失败。"""
    sym = binance_symbol(symbol)
    try:
        end_ms = int(end.timestamp() * 1000)
        f = httpx.get(
            f"{_FAPI}/fapi/v1/fundingRate",
            params={"symbol": sym, "limit": 9, "endTime": end_ms},
            timeout=10.0,
        )
        f.raise_for_status()
        o = httpx.get(
            f"{_FAPI}/futures/data/openInterestHist",
            params={"symbol": sym, "period": "1h", "limit": 25, "endTime": end_ms},
            timeout=10.0,
        )
        o.raise_for_status()
        text = format_derivatives(f.json(), o.json())
        if sym in _LIQ_TOTALS:
            text += "\n" + describe_total(_LIQ_TOTALS[sym])
        return text
    except Exception as exc:  # 衍生品数据只是补充，拿不到不影响会议
        return f"## 衍生品数据\n- （暂不可用：{type(exc).__name__}）"


ATR_PERIOD = 14


def wilder_atr(rows: Sequence[Sequence[Any]], period: int = ATR_PERIOD) -> float | None:
    """币安 K 线行 → Wilder ATR（前 ``period`` 根 TR 的均值起步，再逐根平滑）。

    算法与快环特征层 / 保镖用的 ATR 一致，这样“止损 1–2 倍 ATR”两边说的是同一把尺子。
    根数不够返回 None。
    """
    trs = [
        max(
            float(r[2]) - float(r[3]),
            abs(float(r[2]) - float(p[4])),
            abs(float(r[3]) - float(p[4])),
        )
        for p, r in pairwise(rows)
    ]
    if len(trs) < period:
        return None
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def atr_interval(minutes: int) -> str:
    """``risk.atr_period_min`` → 币安 K 线周期名。"""
    return "1h" if minutes == 60 else f"{minutes}m"


TP_HINT = (1.2, 2.0)
"""出卡提示词里建议的止盈倍数范围（研究 v3）；硬范围仍是保镖的 min_tp_r–max_tp_r。"""


def stop_atr_name(stop: StopScale) -> str:
    """提示词里 stop_atr 的叫法：只有短周期 = ``15m ATR``；带长周期 = ``√(5m ATR × 1h ATR)``。"""
    short = f"{atr_interval(stop.period_min)} ATR"
    if not stop.long_period_min:
        return short
    return f"√({short} × {atr_interval(stop.long_period_min)} ATR)"


def combine_atr(short: float | None, long: float | None, stop: StopScale) -> float | None:
    """stop_atr = √(短 × 长)；没有长周期用短周期；长周期拿不到先只用短周期（同保镖）。"""
    if short is None or not stop.long_period_min or long is None:
        return short
    return math.sqrt(short * long)


def _closed_atr(symbol: str, minutes: int, end: datetime) -> tuple[float | None, float | None]:
    """(已收盘 K 线算的 ATR, 最新收盘价)。"""
    rows = _klines(symbol, atr_interval(minutes), 60, end)
    closed = [r for r in rows if r[6] <= int(end.timestamp() * 1000)]  # 去掉还没收盘的那根
    return wilder_atr(closed), (float(closed[-1][4]) if closed else None)


def describe_atr(atr: float | None, price: float | None, stop: StopScale) -> str:
    """给会议提示词用的 ATR 说明；拿不到数值时明说，让模型别瞎猜价格尺度。"""
    name = stop_atr_name(stop)
    sl_hint = (
        f"（建议 {stop.stop_hint[0]:g}–{stop.stop_hint[1]:g}，"
        "窄 / 中 / 宽取这个范围的下沿 / 中 / 上沿）"
        if stop.stop_hint
        else ""
    )
    tp_lo, tp_hi = max(TP_HINT[0], stop.min_tp_r), min(TP_HINT[1], stop.max_tp_r)
    tp_hint = f"（建议 {tp_lo:g}–{tp_hi:g}，别贪）" if tp_lo < tp_hi else ""
    head = (
        f"止损倍数按 stop_atr = {name}（各 ATR 都是 Wilder {ATR_PERIOD} 根，周期由风控配置决定，"
        "不是 1h K 线上看到的 ATR）计，"
        f"每个标的的止损倍数必须在 {stop.min_stop_atr:g}–{stop.max_stop_atr:g} 之间{sl_hint}，"
        f"止盈倍数（止损距离的倍数）必须在 {stop.min_tp_r:g}–{stop.max_tp_r:g} 之间{tp_hint}，"
        "超出整张卡这个标的会被保镖拒绝"
    )
    if atr is None or price is None or price <= 0:
        return head + "；该币当前 ATR 数值暂不可用"
    pct = atr / price * 100
    return f"{head}。该币当前 stop_atr = {atr:.6g}，约占价格 {pct:.2f}%"


def atr_context(symbol: str, end: datetime, stop: StopScale) -> str:
    """取与保镖同周期的 K 线算 stop_atr（不乘时段系数）。失败只说明不可用，不影响开会。"""
    try:
        short, price = _closed_atr(symbol, stop.period_min, end)
        long = _closed_atr(symbol, stop.long_period_min, end)[0] if stop.long_period_min else None
        return describe_atr(combine_atr(short, long, stop), price, stop)
    except Exception:
        return describe_atr(None, None, stop)


def level_context(
    symbol: str,
    end: datetime,
    stop: StopScale,
    hold_min: float,
    sigma: float = SPAN_SIGMA,
    candidate_interval: str | None = None,
    span_stops: float = 2.5,
) -> LevelContext:
    """取档位周期的 K 线 + 日线算关键位候选。范围：有止损倍数建议（``stop.stop_hint``）时 =
    ``span_stops`` × 倍数中值 × stop_atr（持仓期内价格 94% 的时候不会走到 2.5 个止损以外）；
    否则 = σ × 该币 ATR × √(持仓时长 / ATR 周期)。同一条公式适用所有币所有档位。
    失败返回空背景（不出关键位，不瞎编）。"""
    try:
        end_ms = int(end.timestamp() * 1000)
        rows = _klines(symbol, atr_interval(stop.period_min), 300, end)
        daily = [r for r in _klines(symbol, "1d", 12, end) if r[6] <= end_ms]
        closed = [r for r in rows if r[6] <= end_ms]
        short = wilder_atr(closed)
        long = _closed_atr(symbol, stop.long_period_min, end)[0] if stop.long_period_min else None
        atr = combine_atr(short, long, stop)
        if not atr or not rows:
            return LevelContext()
        price = float(rows[-1][4])
        name = atr_interval(stop.period_min)
        if stop.stop_hint:
            mid = sum(stop.stop_hint) / 2
            span = span_stops * mid * atr
            basis = (
                f"= {span_stops:g}×止损倍数中值 {mid:g}×该币 stop_atr（{stop_atr_name(stop)}），"
                "即持仓期内价格几乎不会走到的范围"
            )
        else:
            span = holding_span(atr, stop.eff_period_min, hold_min, sigma)
            basis = (
                f"= {sigma:g}×该币 {name} ATR×√({hold_min:g}/{stop.period_min})，"
                "即持仓期内的典型波动"
            )
        bars = closed
        if candidate_interval and candidate_interval != name:  # 摆动 / 成交密集区用另一个周期找
            raw = _klines(symbol, candidate_interval, 300, end)
            bars = [r for r in raw if r[6] <= end_ms] or closed
        cands = build_candidates(price, atr, bars, daily, span)
        return LevelContext(price, atr, span, cands, basis)
    except Exception:
        return LevelContext()


def describe_hold(hold_min: float, atr: float | None, price: float | None, stop: StopScale) -> str:
    """告诉会议每笔单最多拿多久、持仓期内该币的正常波动有多大，止损 / 止盈 / 关键位按这个来。"""
    head = (
        f"持仓时长：本系统每笔单最多持有约 {hold_min:g} 分钟（到点强制平仓）。止损、止盈和关键位"
        "都要按这段时间内的正常波动来给，不要给要几天才可能到的目标"
    )
    if atr is None or price is None or price <= 0:
        return head
    period = stop.eff_period_min
    move = holding_span(atr, period, hold_min, 1.0) / price * 100
    return (
        f"{head}。该币 stop_atr 约占价格 {atr / price * 100:.2f}%，"
        f"持仓期内一个标准差的价格移动约 ±{move:.2f}%（= stop_atr×√({hold_min:g}/{period:g})）"
    )


def _ema(xs: Sequence[float], n: int) -> list[float]:
    k = 2 / (n + 1)
    out: list[float] = []
    for x in xs:
        out.append(x if not out else x * k + out[-1] * (1 - k))
    return out


SUPPORTED_INDICATORS = (
    "close_N_sma",
    "close_N_ema",
    "rsi",
    "atr",
    "macd",
    "macds",
    "macdh",
    "boll",
    "boll_ub",
    "boll_lb",
    "vwma",
    "mfi",
)
BOLL_N = 20
VWMA_N = 20
MFI_N = 14


def _roll(xs: Sequence[float], n: int, i: int) -> Sequence[float]:
    return xs[max(0, i - n + 1) : i + 1]


def _boll(c: Sequence[float]) -> tuple[list[float], list[float], list[float]]:
    """布林带（20 根，±2 倍总体标准差）。前面不足 20 根时用已有的根数。"""
    mid, up, lo = [], [], []
    for i in range(len(c)):
        w = _roll(c, BOLL_N, i)
        m = sum(w) / len(w)
        sd = (sum((x - m) ** 2 for x in w) / len(w)) ** 0.5
        mid.append(m)
        up.append(m + 2 * sd)
        lo.append(m - 2 * sd)
    return mid, up, lo


def _vwma(c: Sequence[float], v: Sequence[float]) -> list[float]:
    out = []
    for i in range(len(c)):
        cw, vw = _roll(c, VWMA_N, i), _roll(v, VWMA_N, i)
        vol = sum(vw)
        out.append(sum(x * y for x, y in zip(cw, vw, strict=True)) / vol if vol else cw[-1])
    return out


def _mfi(
    h: Sequence[float], low: Sequence[float], c: Sequence[float], v: Sequence[float]
) -> list[float]:
    tp = [(a + b + d) / 3 for a, b, d in zip(h, low, c, strict=True)]
    flow = [t * x for t, x in zip(tp, v, strict=True)]
    out = [50.0]
    for i in range(1, len(c)):
        pos = neg = 0.0
        for j in range(max(1, i - MFI_N + 1), i + 1):
            if tp[j] > tp[j - 1]:
                pos += flow[j]
            elif tp[j] < tp[j - 1]:
                neg += flow[j]
        if neg == 0:
            out.append(100.0 if pos > 0 else 50.0)
        else:
            out.append(100 - 100 / (1 + pos / neg))
    return out


def _indicator(
    name: str,
    o: list[float],
    h: list[float],
    low: list[float],
    c: list[float],
    v: list[float],
) -> list[float]:
    if name in ("boll", "boll_ub", "boll_lb"):
        mid, up, lo = _boll(c)
        return {"boll": mid, "boll_ub": up, "boll_lb": lo}[name]
    if name == "vwma":
        return _vwma(c, v)
    if name == "mfi":
        return _mfi(h, low, c, v)
    if name.endswith("_sma"):
        n = int(name.split("_")[1])
        return [sum(c[max(0, i - n + 1) : i + 1]) / min(i + 1, n) for i in range(len(c))]
    if name.endswith("_ema"):
        return _ema(c, int(name.split("_")[1]))
    if name == "rsi":
        gains = [max(c[i] - c[i - 1], 0) for i in range(1, len(c))]
        losses = [max(c[i - 1] - c[i], 0) for i in range(1, len(c))]
        ag, al = _ema(gains, 14), _ema(losses, 14)
        return [50.0] + [
            100.0 if x == 0 else 100 - 100 / (1 + g / x) for g, x in zip(ag, al, strict=True)
        ]
    if name == "atr":
        tr = [h[0] - low[0]] + [
            max(h[i] - low[i], abs(h[i] - c[i - 1]), abs(low[i] - c[i - 1]))
            for i in range(1, len(c))
        ]
        return _ema(tr, 14)
    if name in ("macd", "macds", "macdh"):
        macd = [a - b for a, b in zip(_ema(c, 12), _ema(c, 26), strict=True)]
        sig = _ema(macd, 9)
        return {
            "macd": macd,
            "macds": sig,
            "macdh": [m - s for m, s in zip(macd, sig, strict=True)],
        }[name]
    raise ValueError(f"币安数据源暂不支持指标 {name}")


def get_binance_indicators(
    symbol: str, indicator: str, as_of_date: str, look_back_days: int
) -> str:
    """登记为 TradingAgents 的 ``get_indicators``：对小时线算 TradingAgents 常用的几个指标。"""
    end = as_of(as_of_date)
    end_ms = int(end.timestamp() * 1000)
    rows = [k for k in _klines(symbol, "1h", 500, end) if k[6] <= end_ms]  # 只用已收盘的
    o, h, lo, c, v = ([float(k[i]) for k in rows] for i in (1, 2, 3, 4, 5))
    try:
        vals = _indicator(indicator, o, h, lo, c, v)
    except ValueError as e:  # 不认识的指标别让整场会议失败，告诉分析师换一个
        return f"## {indicator}\n{e}。可用：{', '.join(SUPPORTED_INDICATORS)}"
    n = min(look_back_days * 24, len(vals))
    out = [
        f"{datetime.fromtimestamp(k[0] / 1000, tz=UTC):%Y-%m-%d %H:%M}: {v:.6g}"
        for k, v in zip(rows[-n:], vals[-n:], strict=True)
    ]
    return f"## {indicator}（{binance_symbol(symbol)}，1h）\n" + "\n".join(reversed(out))


def register_binance_vendor() -> None:
    """把 ``binance`` 加进 TradingAgents 的数据路由表。重复调用无害。"""
    from tradingagents.dataflows import router

    router.VENDOR_METHODS["get_stock_data"]["binance"] = get_binance_stock_data
    router.VENDOR_METHODS["get_indicators"]["binance"] = get_binance_indicators


def make_news_vendors(
    hub: NewsHub,
    fallback_news: Callable[..., str] | None = None,
    fallback_global: Callable[..., str] | None = None,
) -> tuple[Callable[..., str], Callable[..., str]]:
    """（get_news, get_global_news）两个数据源函数：加密币读 NewsHub 缓存，美股永续仍走原来的。

    只读内存缓存、不碰网络，所以在 TradingAgents 的工作线程里调用是安全的；
    新闻源不可用时返回“暂无”并说明，会议照常进行。
    """

    def _soft(fn: Callable[..., str] | None, *a: Any) -> str:
        if fn is None:
            return ""
        try:
            return fn(*a)
        except Exception as exc:  # 原来的 yfinance 新闻挂了也不能拖垮会议
            return f"（原新闻源不可用：{exc}）"

    def get_news(ticker: str, start_date: str, end_date: str) -> str:
        if not ticker.upper().endswith("-USD"):
            return _soft(fallback_news, ticker, start_date, end_date)
        items = hub.for_symbol(binance_symbol(ticker), hours=48)
        return f"## {ticker} 近 48 小时公告与新闻\n" + format_items(items)

    def get_global_news(
        as_of_date: str, look_back_days: int | None = None, limit: int | None = None
    ) -> str:
        items = hub.market(hours=(look_back_days or 1) * 24, limit=limit or 10)
        text = "## 加密市场近期公告与新闻\n" + format_items(items)
        if not items:
            text += "\n" + _soft(fallback_global, as_of_date, look_back_days, limit)
        return text

    return get_news, get_global_news


def register_crypto_news_vendor(hub: NewsHub) -> None:
    """把 ``crypto_news`` 加进 TradingAgents 的 news 路由表，替换默认的 yfinance 新闻（加密币）。"""
    from tradingagents.dataflows import router

    methods = router.VENDOR_METHODS
    get_news, get_global = make_news_vendors(
        hub,
        methods["get_news"].get("yfinance"),
        methods["get_global_news"].get("yfinance"),
    )
    methods["get_news"]["crypto_news"] = get_news
    methods["get_global_news"]["crypto_news"] = get_global


# ---------------------------------------------------------------- LLM 的结构化输出
class _LevelDraft(BaseModel):
    price: float = Field(gt=0)
    kind: LevelKind
    note: str = ""


class _CardSymbolDraft(BaseModel):
    bias: Bias
    key_levels: list[_LevelDraft] = Field(default_factory=list)
    allowed_signals: list[SignalKind] = Field(default_factory=list)
    size_tier_max: SizeTier
    notional_min: float = Field(ge=0)
    notional_max: float = Field(ge=0)
    leverage_min: float = Field(ge=0)
    leverage_max: float = Field(ge=0)
    stop_atr_min: float = Field(ge=0)
    stop_atr_max: float = Field(ge=0)
    take_profit_r_min: float = Field(ge=0)
    take_profit_r_max: float = Field(ge=0)
    reason: str = Field(min_length=1, description="中文，一两句话")


class _PickDraft(BaseModel):
    class _P(BaseModel):
        symbol: str
        reason: str

    regime: str = Field(min_length=1, description="对大盘的一句话判断，中文")
    picks: list[_P]
    rejections: list[_P] = Field(
        default_factory=list, description="没挑中的候选币，每个一句话理由（为什么今天不值得开会）"
    )


def draft_to_card_symbol(
    d: _CardSymbolDraft,
    entry: SymbolPoolEntry,
    levels: LevelContext | None = None,
) -> CardSymbol:
    """会议结论 → 作战指令。给了 ``levels`` 就只认候选关键位（对不上的丢），不给才照单全收。"""

    def rng(lo: float, hi: float) -> Range:
        return Range(min=min(lo, hi), max=max(lo, hi))

    return CardSymbol(
        symbol=entry.symbol,
        category=entry.category,
        bias=d.bias,
        key_levels=(
            [KeyLevel(price=x.price, kind=x.kind, note=x.note) for x in d.key_levels]
            if levels is None
            else select_levels([(x.price, x.note) for x in d.key_levels], levels)
        ),
        allowed_signals=d.allowed_signals if d.bias is not Bias.NO_TRADE else [],
        size_tier_max=d.size_tier_max,
        notional_usdt=rng(d.notional_min, d.notional_max),
        leverage=rng(d.leverage_min, d.leverage_max),
        stop_atr=rng(d.stop_atr_min, d.stop_atr_max),
        take_profit_r=rng(d.take_profit_r_min, d.take_profit_r_max),
        reason=d.reason,
    )


# ---------------------------------------------------------------- 引擎
def convert_prompt(
    entry: SymbolPoolEntry,
    r: RiskConfig,
    regime: str,
    brief: str,
    decision: str,
    atr_note: str,
    level_note: str = "",
    hold_note: str = "",
) -> str:
    """把会议结论整理成作战指令的提示词（单独成函数，便于快照测试）。"""
    top = r.max_notional_per_trade_usdt
    cap = f"账户权益 × {r.max_notional_per_trade_x:g} 倍" + (f"且 ≤ {top:g} USDT" if top else "")
    return (
        f"把下面的交易结论整理成作战指令。所有数值必须落在硬上限内：单笔名义价值 ≤ {cap}，"
        f"杠杆 ≤ {r.max_leverage} 倍。结论偏弱、"
        "矛盾或重大事件（如议息会议）前夜之类就选 no_trade。这是币安永续合约，只有做多 / 做空 / "
        "观望：结论里的 Buy / Overweight 对应偏多（long），Sell 对应偏空（short），Underweight 对应"
        "偏空或观望，Hold 对应观望（no_trade）；reason 里用多 / 空 / 观望，不要用“减持”“增持”"
        "这类股票持仓词。止损用 ATR 倍数区间（如 1–2），止盈用止损距离的倍数区间（如 1.5–3）。"
        "reason 用中文。\n"
        f"{atr_note}\n{hold_note}\n{level_note}\n"
        f"标的：{entry.symbol}（{entry.category.value}）\n大盘：{regime}{brief}\n结论：\n{decision}"
    )


def _reported_cost(usage: Any) -> float | None:
    """通道在 usage 里直接报的花费（OpenRouter 的 ``usage.cost``，美元）；没有返回 None。"""
    if isinstance(usage, Mapping):
        cost = usage.get("cost")
        if isinstance(cost, int | float) and not isinstance(cost, bool) and cost >= 0:
            return float(cost)
    return None


_SPEAKERS = (
    ("investment_debate_state", "bull_history", "Bull Analyst:", "多头", True),
    ("investment_debate_state", "bear_history", "Bear Analyst:", "空头", True),
    ("risk_debate_state", "aggressive_history", "Aggressive Analyst:", "激进派", False),
    ("risk_debate_state", "conservative_history", "Conservative Analyst:", "保守派", False),
    ("risk_debate_state", "neutral_history", "Neutral Analyst:", "中立派", False),
)


def empty_speeches(
    state: Mapping[str, Any], debate_rounds: int, risk_rounds: int, min_chars: int = 0
) -> list[str]:
    """该开口却只剩「Bull Analyst:」这种空壳（或短于 ``min_chars`` 字）的辩手。TradingAgents 把模型
    空回答照样记进历史，研究经理看着空记录也会给结论，所以这里直接查出来，会议按失败处理。
    多轮辩论时历史是各轮拼在一起的，每轮至少 ``min_chars`` 字。"""
    out: list[str] = []
    for outer, key, label, name, is_debate in _SPEAKERS:
        rounds = debate_rounds if is_debate else risk_rounds
        if rounds <= 0:
            continue
        inner = state.get(outer)
        history = inner.get(key, "") if isinstance(inner, Mapping) else ""
        text = str(history).replace(label, "").strip()
        if not text or len(text) < min_chars * rounds:
            out.append(name)
    return out


_REPORTS = (
    ("market", "market_report", "行情技术"),
    ("social", "sentiment_report", "社交情绪"),
    ("news", "news_report", "新闻"),
    ("fundamentals", "fundamentals_report", "基本面"),
)
_ERROR_MARK = re.compile(
    r"traceback|exception|error (?:code|calling|fetching)|failed to|unable to (?:fetch|retrieve)|"
    r"rate.?limit|tool (?:call )?(?:failed|error)|无法获取|获取失败|调用失败|接口(?:报错|错误)",
    re.IGNORECASE,
)


def broken_reports(
    state: Mapping[str, Any], analysts: Sequence[str], min_chars: int
) -> list[tuple[str, str]]:
    """请来的分析师里，报告为空 / 短于 ``min_chars`` 字 / 短且像工具报错的：[(分析师, 中文名)]。"""
    out: list[tuple[str, str]] = []
    for analyst, key, zh in _REPORTS:
        if analyst not in analysts:
            continue
        text = str(state.get(key) or "").strip()
        if not text or len(text) < min_chars or (len(text) < 300 and _ERROR_MARK.search(text)):
            out.append((analyst, zh))
    return out


_BUY = {"buy", "overweight"}
_SELL = {"sell", "underweight"}
_PROPOSAL = re.compile(r"FINAL TRANSACTION PROPOSAL:\s*\**\s*(BUY|HOLD|SELL)", re.IGNORECASE)


def direction_conflict(bias: Bias, rating: str | None, trader_plan: str) -> str | None:
    """卡上的方向和会议自己的结论（组合经理评级、交易员建议）是否相反；相反返回说明。
    只比相反的方向（做多 vs 卖出 / 做空 vs 买入）；观望 / 持有 / 解析不出不算矛盾。"""
    if bias in (Bias.NO_TRADE, Bias.BOTH):  # 观望 / 双向都没有"相反"可言
        return None
    proposal = _PROPOSAL.findall(trader_plan or "")
    signals = {("评级", (rating or "").lower())}
    if proposal:
        signals.add(("交易员建议", proposal[-1].lower()))
    against = _SELL if bias is Bias.LONG else _BUY
    for who, value in sorted(signals):
        if value in against:
            way = "做多" if bias is Bias.LONG else "做空"
            return f"{who}是 {value}，和作战指令的方向（{way}）相反"
    return None


def degrade_size(cs: CardSymbol, missing: Sequence[str]) -> CardSymbol:
    """新闻 / 社交 / 基本面报告缺失：仓位档位下调一级（L→M→S），理由里注明。"""
    lower = {SizeTier.L: SizeTier.M, SizeTier.M: SizeTier.S, SizeTier.S: SizeTier.S}
    note = f"（{'、'.join(missing)}报告缺失，仓位档位下调一级）"
    return cs.model_copy(
        update={"size_tier_max": lower[cs.size_tier_max], "reason": (cs.reason + note)[:500]}
    )


FALLBACK_MAX_TOKENS = {"openrouter": 384_000, "deepseek": 8192}
"""读不到模型信息时的输出上限：OpenRouter 上 deepseek-v4 系列文档标的最大输出是 384000；
直连 DeepSeek 按它官方接口的 8192 保守处理。"""
MODELS_URL = "https://openrouter.ai/api/v1/models"
RETRY_HINT = "直接给结论，不要思考过程。"


def _fetch_models() -> list[dict[str, Any]]:
    import httpx

    r = httpx.get(MODELS_URL, timeout=10)
    r.raise_for_status()
    return list(r.json()["data"])


def model_max_tokens(
    provider: str,
    models: Sequence[str],
    fetch: Callable[[], list[dict[str, Any]]] = _fetch_models,
) -> int:
    """模型的最大输出 token 数：OpenRouter 模型信息里的 ``top_provider.max_completion_tokens``
    （几个模型取最小）；读不到、没有这个模型 = ``FALLBACK_MAX_TOKENS``。"""
    fallback = FALLBACK_MAX_TOKENS.get(provider, 8192)
    if provider != "openrouter":
        return fallback
    try:
        by_id = {str(m.get("id")): m for m in fetch()}
        found = []
        for name in models:
            top = (by_id.get(name) or {}).get("top_provider") or {}
            v = top.get("max_completion_tokens")
            if isinstance(v, int) and v > 0:
                found.append(v)
        if found and len(found) == len(models):
            return min(found)
    except Exception:
        logger.warning("读 OpenRouter 模型信息失败，max_tokens 用 %d", fallback, exc_info=True)
        return fallback
    logger.warning("OpenRouter 模型信息里没有 %s，max_tokens 用 %d", list(models), fallback)
    return fallback


def reply_problem(res: Any, min_chars: int) -> str | None:
    """一次模型回答有没有问题：被 max_tokens 截断 → ``length``；正文空 → ``empty``；
    过短 → ``short``；没问题 None。带工具调用的回答正文可以是空的。"""
    meta = getattr(res, "response_metadata", None) or {}
    if meta.get("finish_reason") == "length":
        return "length"
    if getattr(res, "tool_calls", None):
        return None
    content = getattr(res, "content", "")
    if isinstance(content, list):
        content = "".join(
            str(p.get("text", "")) if isinstance(p, dict) else str(p) for p in content
        )
    text = str(content).strip()
    if not text:
        return "empty"
    return "short" if len(text) < min_chars else None


def _with_hint(inp: Any) -> Any:
    if isinstance(inp, str):
        return f"{inp}\n\n{RETRY_HINT}"
    to_messages = getattr(inp, "to_messages", None)
    msgs = list(to_messages()) if callable(to_messages) else list(inp)
    return [*msgs, ("human", RETRY_HINT)]


def add_retry(llm: Any, meter: CostMeter | None, *, reasoning_off: bool, min_chars: int) -> None:
    """让这个模型对象的每次 ``invoke`` 在回答有问题时自动重试一次：提示「直接给结论」，
    OpenRouter 下同时关掉推理。重试仍有问题就原样交回去，由会议末尾的检查让整场失败。"""
    original = llm.invoke

    def invoke(inp: Any, config: Any = None, **kwargs: Any) -> Any:
        res = original(inp, config, **kwargs)
        why = reply_problem(res, min_chars)
        if why is None:
            return res
        if meter is not None:
            meter.note_retry(why)
        retry_kwargs = dict(kwargs)
        if reasoning_off:
            body = getattr(llm, "extra_body", None) or {}
            retry_kwargs["extra_body"] = {**body, "reasoning": {"enabled": False}}
        if meter is not None:
            meter.mark_retry_call()
        res = original(_with_hint(inp), config, **retry_kwargs)
        if meter is not None and reply_problem(res, min_chars) is None:
            meter.note_recovered(why)
        return res

    object.__setattr__(llm, "invoke", invoke)


def openrouter_body(reasoning_effort: str) -> dict[str, Any]:
    """OpenRouter 请求里额外带的参数：``usage.include`` 让响应带真实花费（usage.cost）；
    ``reasoning`` 限制推理长度——推理模型的思考也算在 max_tokens 里，不限的话个别币的思考
    会把额度用满、回答被截断。``none`` = 关掉推理，其余是 OpenRouter 的 effort 档。"""
    reasoning: dict[str, Any] = (
        {"enabled": False} if reasoning_effort == "none" else {"effort": reasoning_effort}
    )
    return {"usage": {"include": True}, "reasoning": reasoning}


def apply_openrouter_options(llm: Any, reasoning_effort: str) -> None:
    """把 ``openrouter_body`` 合进模型对象的 ``extra_body``（ChatOpenAI 会原样放进请求体）。"""
    body = getattr(llm, "extra_body", None) or {}
    llm.extra_body = {**body, **openrouter_body(reasoning_effort)}


class CostMeter:
    """一场会议（或总盘会）的花费累计。线程安全：模型请求发生在后台线程里。

    费用一律用通道报的真实值（OpenRouter 的 ``usage.cost``）；某次请求没报（直连 DeepSeek、
    通道没带这个字段）才按 token 和 ``price_per_mtok`` 估这一次，估的部分会偏高（缓存命中价没算）。
    ``abort`` 之后新的模型请求直接失败，超时取消的会议不会在后台继续烧钱。
    """

    def __init__(self, prices: Mapping[str, tuple[float, float]]) -> None:
        self._prices = prices
        self._lock = threading.Lock()
        self._reported = 0.0
        self._unreported: dict[str, list[int]] = {}
        self._aborted = False
        self.calls = 0
        self.on_call: Callable[[str | None, int, int, int], None] | None = None
        self.on_usage: Callable[[int, int], None] | None = None  # (输入 tokens, 命中缓存的输入)
        """每次响应后回调 (finish_reason, 输出 token 数, 其中推理 token 数, 是否重试的那次 0/1)。"""
        self.truncated = 0
        """因为 max_tokens 到顶而被截断的回答数（finish_reason == "length"）；重试成功的不算。"""
        self.retries = 0
        """自动重试的次数（回答被截断 / 空 / 过短）。"""
        self._flying: dict[str, tuple[int, str]] = {}
        """在途的模型请求：run_id → (估的输入 token 数, 价格档)。"""
        self._outs: list[int] = []
        self.tokens_in = self.tokens_out = self.tokens_reasoning = self.tokens_cached = 0
        self._est_runs: dict[str, float] = {}
        """超时取消时还在途的请求，及各自记入的估算花费；真实结果回来后改成真实值。"""
        self._est_total = 0.0
        self.on_late: Callable[[float], None] | None = None
        """在途请求的真实花费晚到时回调（真实 - 估算），让日花费补正。"""
        self.on_retry: Callable[[str], None] | None = None
        self._local = threading.local()
        """同一线程里同步 invoke：重试那次请求发出前打标，它的回调读到并清掉。"""

    def mark_retry_call(self) -> None:
        self._local.retry = 1

    def note_retry(self, why: str) -> None:
        with self._lock:
            self.retries += 1
        if self.on_retry is not None:
            try:
                self.on_retry(why)
            except Exception:
                logger.exception("记录重试失败")

    def note_recovered(self, why: str) -> None:
        """重试拿到了好回答：之前那次被截断的不再让整场失败。"""
        if why == "length":
            with self._lock:
                self.truncated = max(self.truncated - 1, 0)

    @property
    def estimated(self) -> bool:
        """花费里是否含超时取消时在途请求的估算。"""
        with self._lock:
            return bool(self._est_runs)

    def start_call(self, run_id: str, chars: int, model: str) -> None:
        """一次模型请求发出：记下估的输入 token（字数 / 2，偏高一点，偏保守）。"""
        tier = "pro" if "pro" in model else "flash"
        with self._lock:
            self._flying[run_id] = (max(chars // 2, 1), tier)

    def drop_call(self, run_id: str) -> None:
        """请求没拿到结果就结束了（报错）：不再算在途。"""
        with self._lock:
            self._flying.pop(run_id, None)
            self._est_total -= self._est_runs.pop(run_id, 0.0)

    def add(self, model: str, input_tokens: int, output_tokens: int, cost: float | None) -> None:
        with self._lock:
            self.calls += 1
            if cost is not None:
                self._reported += cost
                return
            tokens = self._unreported.setdefault(model, [0, 0])
            tokens[0] += input_tokens
            tokens[1] += output_tokens

    def record_response(self, response: Any, run_id: str | None = None) -> None:
        """喂一次 LangChain 的 ``LLMResult``：取真实花费，没有就记 token 留着估。
        超时取消时按估算记过的在途请求，真实结果回来后换成真实值，差额通过 ``on_late`` 补正。"""
        before = self.cost
        est: float | None = None
        if run_id is not None:
            with self._lock:
                self._flying.pop(run_id, None)
                est = self._est_runs.pop(run_id, None)
                if est is not None:
                    self._est_total -= est
        self._record(response)
        if est is not None and self.on_late is not None:
            try:
                self.on_late(round(self.cost - before, 6))
            except Exception:
                logger.exception("补正在途请求的花费失败")

    def _record(self, response: Any) -> None:
        usage = (getattr(response, "llm_output", None) or {}).get("token_usage")
        cost = _reported_cost(usage)
        model = str((getattr(response, "llm_output", None) or {}).get("model_name") or "")
        tin = tout = reasoning = 0
        reason: str | None = None
        for gens in getattr(response, "generations", None) or []:
            for gen in gens:
                info = getattr(gen, "generation_info", None) or {}
                reason = info.get("finish_reason") or reason
                if info.get("finish_reason") == "length":
                    with self._lock:
                        self.truncated += 1
                msg = getattr(gen, "message", None)
                if msg is None:
                    continue
                meta = getattr(msg, "response_metadata", None) or {}
                reason = meta.get("finish_reason") or reason
                if meta.get("finish_reason") == "length" and info.get("finish_reason") != "length":
                    with self._lock:
                        self.truncated += 1
                um = getattr(msg, "usage_metadata", None) or {}
                tin += int(um.get("input_tokens", 0) or 0)
                tout += int(um.get("output_tokens", 0) or 0)
                reasoning += _reasoning_tokens(um, meta)
                model = str(meta.get("model_name") or model)
                if cost is None:
                    cost = _reported_cost(meta.get("token_usage"))
        with self._lock:
            if tout:
                self._outs.append(tout)
            self.tokens_in += tin
            self.tokens_out += tout
            self.tokens_reasoning += reasoning
            self.tokens_cached += _cached_tokens(usage)
        self.add(model, tin, tout, cost)
        if self.on_call is not None:
            try:
                self.on_call(
                    str(reason) if reason else None,
                    tout,
                    reasoning,
                    int(getattr(self._local, "retry", 0)),
                )
                self._local.retry = 0
            except Exception:
                logger.exception("记录模型调用信息失败")
        if self.on_usage is not None:
            try:
                self.on_usage(tin, _cached_tokens(usage))
            except Exception:
                logger.exception("记录模型输入用量失败")

    @property
    def cost(self) -> float:
        with self._lock:
            total = self._reported
            for model, (tin, tout) in self._unreported.items():
                pin, pout = self._prices["pro" if "pro" in model else "flash"]
                total += (tin * pin + tout * pout) / 1e6
            total += self._est_total
        return round(total, 6)

    def abort(self) -> None:
        """叫停：新请求直接失败；此刻还在途的请求按估算记一笔（估的输入 token × 输入价 +
        本场已完成回答的平均输出 × 输出价），真实花费晚到时再补正，见 ``record_response``。"""
        with self._lock:
            self._aborted = True
            avg_out = sum(self._outs) / len(self._outs) if self._outs else 2000.0
            for run_id, (tin, tier) in self._flying.items():
                pin, pout = self._prices[tier]
                est = round((tin * pin + avg_out * pout) / 1e6, 6)
                self._est_runs[run_id] = est
                self._est_total += est
            self._flying.clear()

    def check(self) -> None:
        if self._aborted:
            raise RuntimeError("会议已被取消（超时），不再发新的模型请求")


def _cached_tokens(usage: Any) -> int:
    """OpenRouter 原始 usage 的 ``prompt_tokens_details.cached_tokens``（命中缓存的输入）。"""
    if not isinstance(usage, Mapping):
        return 0
    v = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
    return int(v) if isinstance(v, int | float) else 0


def _reasoning_tokens(usage_metadata: Mapping[str, Any], meta: Mapping[str, Any]) -> int:
    """一次回答里推理（思考）用了多少 token：langchain 的 ``output_token_details.reasoning``，
    或 OpenRouter 原始 usage 的 ``completion_tokens_details.reasoning_tokens``。"""
    v = (usage_metadata.get("output_token_details") or {}).get("reasoning")
    if v is None:
        raw = meta.get("token_usage") or {}
        v = (raw.get("completion_tokens_details") or {}).get("reasoning_tokens")
    return int(v or 0)


def cost_handler(meter: CostMeter) -> Any:
    """把 ``CostMeter`` 接到 LangChain 的回调上（LangChain 只在真跑时才导入）。"""
    from langchain_core.callbacks import BaseCallbackHandler

    class _CostHandler(BaseCallbackHandler):  # type: ignore[misc]
        raise_error = True  # 被取消的会议要让下一次请求真的失败，不能被回调管理器吞掉

        def on_chat_model_start(self, serialized: Any, messages: Any, **kwargs: Any) -> None:
            meter.check()
            chars = sum(len(str(getattr(m, "content", m))) for batch in messages for m in batch)
            self._begin(chars, kwargs)

        def on_llm_start(self, serialized: Any, prompts: Any, **kwargs: Any) -> None:
            meter.check()
            self._begin(sum(len(str(p)) for p in prompts), kwargs)

        @staticmethod
        def _begin(chars: int, kwargs: Mapping[str, Any]) -> None:
            run_id = kwargs.get("run_id")
            model = str((kwargs.get("invocation_params") or {}).get("model") or "")
            if run_id is not None:
                meter.start_call(str(run_id), chars, model)

        def on_llm_end(self, response: Any, **kwargs: Any) -> None:
            run_id = kwargs.get("run_id")
            meter.record_response(response, str(run_id) if run_id is not None else None)

        def on_llm_error(self, error: BaseException, **kwargs: Any) -> None:
            run_id = kwargs.get("run_id")
            if run_id is not None:
                meter.drop_call(str(run_id))

    handler = _CostHandler()
    handler.meter = meter  # 引擎给模型对象加自动重试时要用
    return handler


class TradingAgentsEngine:
    """满足 ``commander.MeetingEngine``。每次开会新建一个 TradingAgents 图（它不是并发安全的）。"""

    def __init__(
        self,
        risk: RiskConfig,
        secrets: SecretStore,
        settings: TradingAgentsSettings | None = None,
        today: Callable[[], datetime] = lambda: datetime.now(UTC),
        news: NewsHub | None = None,
        liquidations: LiquidationSource | None = None,
        brief: MarketBriefService | None = None,
        stop_scale: StopScale = DEFAULT_STOP_SCALE,
    ) -> None:
        settings = settings or TradingAgentsSettings()
        key_env = PROVIDER_KEY_ENV.get(settings.provider)
        if key_env is None:
            raise RuntimeError(f"不支持的 provider：{settings.provider}")
        key = secrets.get(key_env)
        if not key:
            raise RuntimeError(f"缺少 {key_env}：先跑一次 setup-keys")
        # TradingAgents 只认环境变量。注意子进程会继承：从本进程拉起别的程序时 env= 里要去掉它；
        # 日志和 DecisionRecord 不得写入请求头或配置字典。
        os.environ.setdefault(key_env, key)
        if settings.max_tokens is None:  # 没写 = 用模型自己的最大输出
            mt = model_max_tokens(settings.provider, [settings.quick_model, settings.deep_model])
            logger.info("max_tokens 用模型最大输出：%d", mt)
            settings = replace(settings, max_tokens=mt)
        if settings.json_max_tokens is None:
            settings = replace(settings, json_max_tokens=max(8192, settings.max_tokens or 0))
        self._risk = risk
        self._stop = stop_scale
        self._s = settings
        self._today = today
        self._news = news
        self._brief = brief
        self._liq = liquidations
        self._late_cost: Callable[[str, float], None] | None = None
        self._inflight: dict[str, CostMeter] = {}
        self._live: dict[str, MeetingLive] = {}
        self._inflight_lock = threading.Lock()
        register_binance_vendor()
        set_analyst_klines(settings.analyst_klines, settings.meeting_min)
        self._guard_reddit(settings)
        if news is not None:
            register_crypto_news_vendor(news)

    @staticmethod
    def _guard_reddit(settings: TradingAgentsSettings) -> None:
        try:
            from tradingagents.agents.analysts import sentiment_analyst
        except ImportError:  # 慢环依赖没装时，引擎本来也跑不起来；不在这里报错
            return
        original = getattr(sentiment_analyst.fetch_reddit_posts, "_fetch", None)
        fetch = original or sentiment_analyst.fetch_reddit_posts  # 重复构造引擎不叠加包装
        register_reddit_guard(RedditGuard(fetch, mode=settings.reddit))

    def _meter(self, key: str) -> CostMeter:
        meter = CostMeter(self._s.price_per_mtok)
        late = getattr(self, "_late_cost", None)
        if late is not None:
            meter.on_late = lambda delta: late(key, delta)
        with self._inflight_lock:
            self._inflight[key] = meter
        return meter

    def _done(self, key: str, meter: CostMeter) -> None:
        with self._inflight_lock:
            if self._inflight.get(key) is meter:
                del self._inflight[key]

    def bind_late_cost(self, callback: Callable[[str, float], None]) -> None:
        """超时取消的会议，在途请求的真实花费晚到时 ``callback(key, 差额)``（在工作线程里调）。"""
        self._late_cost = callback

    def cost_estimated(self, key: str) -> bool:
        """这场会议的花费里是否含超时取消时在途请求的估算。"""
        with self._inflight_lock:
            meter = self._inflight.get(key)
        return bool(meter and meter.estimated)

    def partial_cost(self, key: str) -> float | None:
        """进行中（或刚超时取消）的会议已经花了多少；没有这场会议返回 None。"""
        with self._inflight_lock:
            meter = self._inflight.get(key)
        return meter.cost if meter else None

    def abort(self, key: str) -> None:
        """叫停一场会议：它的后台线程里不再发新的模型请求（已发出的那一个照常结束）。"""
        with self._inflight_lock:
            meter = self._inflight.get(key)
        if meter:
            meter.abort()
        live = self._live.get(key)
        if live:
            live.fail(
                "超时取消，会议没有结论",
                meter.cost if meter else 0.0,
                estimated=bool(meter and meter.estimated),
            )

    @staticmethod
    def _llm(graph: Any, tier: str) -> Any:
        return graph.deep_thinking_llm if tier == "deep" else graph.quick_thinking_llm

    def _graph(self, analysts: tuple[str, ...], handler: Any) -> Any:
        from tradingagents.default_config import build_default_config
        from tradingagents.graph.trading_graph import (
            TradingAgentsGraph,
        )

        cfg = build_config(build_default_config(), self._s, crypto_news=self._news is not None)
        graph = TradingAgentsGraph(selected_analysts=analysts, config=cfg, callbacks=[handler])
        if self._s.provider == "openrouter":
            # TradingAgents 不透传这些请求参数，所以直接设到它建好的模型对象上
            for llm in {
                id(x): x for x in (graph.deep_thinking_llm, graph.quick_thinking_llm)
            }.values():
                apply_openrouter_options(llm, self._s.reasoning_effort)
        if self._s.retry_bad_steps:
            meter = getattr(handler, "meter", None)
            for llm in {
                id(x): x for x in (graph.deep_thinking_llm, graph.quick_thinking_llm)
            }.values():
                add_retry(
                    llm,
                    meter,
                    reasoning_off=self._s.provider == "openrouter",
                    min_chars=self._s.retry_min_chars,
                )
        return graph

    def _snapshot_liquidations(self, symbols: Sequence[str]) -> None:
        """在事件循环里取强平快照，再让工作线程去读（统计对象不是线程安全的）。"""
        if self._liq is None:
            return
        now = self._today()
        fresh = {s: self._liq.liquidations_1h_usdt(s, now) for s in symbols}
        set_liquidation_totals({**_LIQ_TOTALS, **fresh})

    async def pick(
        self, candidates: Sequence[SymbolPoolEntry], max_n: int, previous: BattleCard | None
    ) -> PickResult:
        if max_n <= 0 or not candidates:
            return PickResult(regime=previous.regime if previous else "无新增标的", picks=[])
        self._snapshot_liquidations([e.symbol for e in candidates])
        return await asyncio.to_thread(self._pick_sync, list(candidates), max_n, previous)

    def _pick_sync(
        self, candidates: list[SymbolPoolEntry], max_n: int, previous: BattleCard | None
    ) -> PickResult:
        meter = self._meter("pick")
        handler = cost_handler(meter)
        try:
            return self._pick_run(candidates, max_n, previous, handler, meter)
        except Exception as exc:  # 失败时把已烧掉的花费带出去，日花费上限才不漏算
            raise MeetingError(str(exc), meter.cost) from exc
        finally:
            self._done("pick", meter)

    def _pick_run(
        self,
        candidates: list[SymbolPoolEntry],
        max_n: int,
        previous: BattleCard | None,
        handler: Any,
        meter: CostMeter,
    ) -> PickResult:
        graph = self._graph(("market",), handler)
        llm = (
            self._llm(graph, self._s.pick_tier)
            .with_structured_output(_PickDraft)
            .bind(max_tokens=self._s.json_max_tokens)
        )
        table = "\n".join(
            f"{e.symbol} 类别={e.category.value} 分数={e.score:.2f} 理由={'、'.join(e.reasons)}"
            + self._liq_note(e.symbol)
            + self._headlines(e.symbol)
            for e in candidates
        )
        market = self._brief_text() + self._market_news()
        prev = f"上一张卡的大盘判断：{previous.regime}\n" if previous else ""
        draft: _PickDraft = llm.invoke(
            f"你是加密永续合约的总盘军师。现在是 {self._today():%Y-%m-%d %H:%M} UTC。\n{prev}"
            f"下面是代码筛出的候选池。请先用一句中文判断大盘，再挑最多 {max_n} 个今天最值得"
            f"开会研究的标的（只能从表里挑），每个给一句理由；"
            f"没挑中的候选币也各给一句不上会的理由（rejections）。{market}\n{table}",
            config={"callbacks": [handler]},
        )
        valid = {e.symbol for e in candidates}
        picks = [Pick(p.symbol, p.reason) for p in draft.picks if p.symbol in valid][:max_n]
        rejections = [
            Pick(p.symbol, p.reason)
            for p in draft.rejections
            if p.symbol in valid and p.symbol not in {x.symbol for x in picks}
        ]
        return PickResult(draft.regime, picks, meter.cost, rejections)

    def _liq_note(self, symbol: str) -> str:
        total = _LIQ_TOTALS.get(symbol, 0.0)
        return f" 近1h强平={total:,.0f}USDT" if total > 0 else ""

    def _headlines(self, symbol: str) -> str:
        if self._news is None:
            return ""
        items = self._news.for_symbol(symbol, hours=48, limit=2)
        return "".join(f"\n    · {i.title}" for i in items)

    def _brief_text(self) -> str:
        return f"\n{self._brief.text}\n" if self._brief is not None and self._brief.text else ""

    def _market_news(self) -> str:
        if self._news is None:
            return ""
        now = self._today()
        return "\n近期公告与新闻（供判断大盘，可能不完整）：\n" + format_items(
            self._news.market(hours=24, limit=8), now
        )

    async def analyze(self, entry: SymbolPoolEntry, regime: str) -> Analysis:
        self._snapshot_liquidations([entry.symbol])
        return await asyncio.to_thread(self._analyze_sync, entry, regime)

    def _analyze_sync(self, entry: SymbolPoolEntry, regime: str) -> Analysis:
        meter = self._meter(entry.symbol)
        handler = cost_handler(meter)
        live = MeetingLive(Path(self._s.live_dir), entry.symbol) if self._s.live_dir else None
        if live:
            self._live[entry.symbol] = live
            meter.on_call = live.note_call
            meter.on_usage = live.note_usage
            meter.on_retry = live.note_retry
        try:
            res = self._analyze_run(entry, regime, handler, meter, live)
        except Exception as exc:  # 同上：失败也带上真实花费
            if live:
                live.fail(str(exc) or type(exc).__name__, meter.cost)
            raise MeetingError(str(exc), meter.cost) from exc
        else:
            if live:
                cs = res.card_symbol
                live.finish(cs.bias.value, cs.reason, cs.reason, res.cost_usd)
            return res
        finally:
            self._done(entry.symbol, meter)
            self._live.pop(entry.symbol, None)

    def _analyze_run(
        self,
        entry: SymbolPoolEntry,
        regime: str,
        handler: Any,
        meter: CostMeter,
        live: MeetingLive | None = None,
    ) -> Analysis:
        ticker, asset_type = ta_ticker(entry)
        analysts = self._s.analysts_stock if asset_type == "stock" else self._s.analysts_crypto
        graph = self._graph(analysts, handler)
        if live:
            tap_graph(graph, live)
        state, rating = graph.propagate(ticker, f"{self._today():%Y-%m-%d}", asset_type=asset_type)
        decision = str(state.get("final_trade_decision", ""))
        if not decision:
            raise RuntimeError(f"{entry.symbol}：TradingAgents 没有给出结论")
        if meter.truncated:
            raise RuntimeError(
                f"{entry.symbol}：{meter.truncated} 次模型回答被 max_tokens 截断"
                f"（当前 {self._s.max_tokens}），辩论 / 决定不完整，这场会不能用；调大 max_tokens"
            )
        if empty := empty_speeches(
            state, self._s.debate_rounds, self._s.risk_rounds, self._s.speech_min_chars
        ):
            # 辩论没产出还给结论，等于没开会（实测 max_tokens 太小时研究经理看着空记录给 Hold）
            raise RuntimeError(
                f"{entry.symbol}：{'、'.join(empty)}的发言是空的或太短（少于 "
                f"{self._s.speech_min_chars} 字；模型回答为空，多半被 max_tokens 挤掉了），"
                "这场会不能用"
            )
        broken = broken_reports(state, analysts, self._s.report_min_chars)
        if any(a == "market" for a, _ in broken):
            raise RuntimeError(
                f"{entry.symbol}：行情技术分析师的报告是空的或工具报错，这场会不能用"
            )
        missing = [zh for _, zh in broken]
        if live and missing:
            live.step("quality", "gate", f"{'、'.join(missing)}报告缺失（空 / 太短 / 工具报错）")
        if live:
            live.converting()

        levels = level_context(
            entry.symbol,
            self._today(),
            self._stop,
            self._s.hold_minutes,
            self._s.level_span_sigma,
            self._s.level_candidate_interval,
            self._s.level_span_stops,
        )
        prompt = convert_prompt(
            entry,
            self._risk,
            regime,
            self._brief_text(),
            decision,
            atr_context(entry.symbol, self._today(), self._stop),
            describe_candidates(levels),
            describe_hold(self._s.hold_minutes, levels.atr, levels.price, self._stop),
        )
        llm = (
            self._llm(graph, self._s.convert_tier)
            .with_structured_output(_CardSymbolDraft)
            .bind(max_tokens=self._s.json_max_tokens)
        )
        last: Exception | None = None
        for _ in range(2):
            try:
                draft = llm.invoke(prompt, config={"callbacks": [handler]})
                if not isinstance(draft, _CardSymbolDraft):
                    # 思考模型偶尔把结构化输出退回成自由文本：当作没有结论，不去猜
                    raise ValueError("模型没有返回结构化结果")
                cs = draft_to_card_symbol(draft, entry, levels)
                if why := direction_conflict(
                    cs.bias, rating, str(state.get("trader_investment_plan", ""))
                ):
                    raise RuntimeError(f"{entry.symbol}：会议结论自相矛盾：{why}，这场会不能用")
                if missing:
                    cs = degrade_size(cs, missing)
                return Analysis(
                    cs,
                    meter.cost,
                    {
                        "input_tokens": meter.tokens_in,
                        "output_tokens": meter.tokens_out,
                        "reasoning_tokens": meter.tokens_reasoning,
                        "cached_tokens": meter.tokens_cached,  # 命中缓存的输入 token
                    },
                )
            except (ValidationError, ValueError) as exc:
                last = exc
        raise RuntimeError(f"{entry.symbol}：作战指令格式不合格：{last}")
