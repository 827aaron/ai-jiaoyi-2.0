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
import json
import logging
import math
import os
import re
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

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
from jiaoyi.core.ticks import price_formatter
from jiaoyi.interfaces import SecretStore
from jiaoyi.slow import binance_gate, market_table, ta_nodes, ta_prompts, ta_roles
from jiaoyi.slow.brief import MarketBriefService
from jiaoyi.slow.commander import (
    DEFAULT_STOP_SCALE,
    Analysis,
    Decision,
    MeetingError,
    Pick,
    PickResult,
    StopScale,
)
from jiaoyi.slow.lessons import Lesson, LessonBook
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
from jiaoyi.slow.ticks import TICKS

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
    # 最长持仓档（30m…24h）：提示词写法（short / mid / long）、指标周期、手续费占比都跟它走，
    # 见 core/profiles.py 的 7 档词表和 slow/ta_prompts.py；app.py 按 app.toml 的 hold 填进来
    hold: str = "2h"
    # 研究经理 / 组合经理只给「偏向」（Overweight / Underweight）时，卡的仓位档最多到哪一档
    lean_size_tier: SizeTier = SizeTier.M
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


def prompt_params(s: TradingAgentsSettings) -> ta_prompts.PromptParams:
    """这套设置对应的提示词参数（持仓时长、写法、指标周期、手续费占比）。"""
    return ta_prompts.PromptParams.for_hold(s.hold, s.hold_minutes)


def perp_context(p: ta_prompts.PromptParams, group: str = "manager") -> str:
    """永续合约语境 + 持仓时长 + 这一档的市场习性（所有角色共用，拼在 output_language 后）。"""
    return ta_prompts.perp_context(p, group)


PERP_CONTEXT = perp_context(ta_prompts.PromptParams())


def build_config(
    base: Mapping[str, Any], s: TradingAgentsSettings, *, crypto_news: bool = False
) -> dict[str, Any]:
    """在 TradingAgents 的 ``DEFAULT_CONFIG`` 上叠加我们的设置，返回新字典。"""
    cfg = dict(base)
    cfg.update(
        llm_provider=s.provider,
        quick_think_llm=s.quick_model,
        deep_think_llm=s.deep_model,
        output_language=f"{s.output_language}. {perp_context(prompt_params(s))}",
        **{
            ta_nodes.PROMPT_KEY: {
                "hold": prompt_params(s).hold,
                "hold_minutes": s.hold_minutes,
                "lean": s.lean_size_tier.value,
                "lang": s.output_language,
            }
        },
        max_debate_rounds=s.debate_rounds,
        max_risk_discuss_rounds=s.risk_rounds,
        max_tokens=s.max_tokens,
        llm_max_retries=s.max_retries,
        results_dir=os.path.join(s.results_dir, "logs"),
        data_cache_dir=os.path.join(s.results_dir, "cache"),
        # 停掉 TradingAgents 原版的记忆：它按「天」算收益、跟 SPY 比，是股票口径。教训改由
        # PerpGraph 交当前这一批（加密版，见 §4.7）；现有 memory.md 不删
        memory_log_path="",
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
    r = binance_gate.get(_KL, params)
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
            f"# {binance_symbol(symbol)} Binance perpetual {interval} klines, "
            f"{len(rows)} closed bars "
            f"({t0} -> {t1}, UTC)\n" + "\n".join(lines)
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
    lines = ["## Derivatives data (Binance USDT-M perpetual)"]
    if funding:
        rates = [float(f["fundingRate"]) * 100 for f in funding]
        hist = ", ".join(f"{r:+.4f}%" for r in rates[-6:])
        lines.append(
            f"- Funding rate (%, last {min(len(rates), 6)} settlements, oldest to newest): {hist}"
        )
        lines.append(
            f"- Latest funding rate {rates[-1]:+.4f}% "
            "(positive: longs pay, crowded long; negative: the opposite)"
        )
    if len(oi) >= 2:
        first, last = float(oi[0]["sumOpenInterestValue"]), float(oi[-1]["sumOpenInterestValue"])
        if first > 0:
            chg = (last - first) / first * 100
            lines.append(
                f"- Open interest (USDT) {first:,.0f} -> {last:,.0f}, "
                f"change over {len(oi) - 1}h {chg:+.2f}%"
            )
    if len(lines) == 1:
        lines.append("- (no data)")
    return "\n".join(lines)


def derivatives_context(symbol: str, end: datetime) -> str:
    """资金费率和持仓量变化。失败只说明不可用，不让 K 线数据跟着失败。"""
    sym = binance_symbol(symbol)
    try:
        end_ms = int(end.timestamp() * 1000)
        f = binance_gate.get(
            f"{_FAPI}/fapi/v1/fundingRate",
            {"symbol": sym, "limit": 9, "endTime": end_ms},
        )
        f.raise_for_status()
        o = binance_gate.get(
            f"{_FAPI}/futures/data/openInterestHist",
            {"symbol": sym, "period": "1h", "limit": 25, "endTime": end_ms},
        )
        o.raise_for_status()
        text = format_derivatives(f.json(), o.json())
        if sym in _LIQ_TOTALS:
            text += "\n" + describe_total(_LIQ_TOTALS[sym])
        return text
    except binance_gate.RateLimited:
        raise  # 被封了整轮暂停，不当成“暂不可用”吞掉
    except Exception as exc:  # 衍生品数据只是补充，拿不到不影响会议
        return f"## Derivatives data\n- (unavailable: {type(exc).__name__})"


ATR_PERIOD = 14


def wilder_atr(rows: Sequence[Sequence[Any]], period: int = ATR_PERIOD) -> float | None:
    """币安 K 线行 → Wilder ATR（前 ``period`` 根 TR 的均值起步，再逐根平滑）。

    算法与快环特征层 / 风控员用的 ATR 一致，这样“止损 1–2 倍 ATR”两边说的是同一把尺子。
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


_BINANCE_INTERVALS = {
    1: "1m", 3: "3m", 5: "5m", 15: "15m", 30: "30m", 60: "1h", 120: "2h", 240: "4h",
    360: "6h", 480: "8h", 720: "12h", 1440: "1d",
}  # fmt: skip


def atr_interval(minutes: int) -> str:
    """``risk.atr_period_min`` → 币安 K 线周期名（币安只认固定几个：240 是 ``4h``）。"""
    return _BINANCE_INTERVALS.get(minutes, f"{minutes}m")


TP_HINT = (1.2, 2.0)
"""出卡提示词里建议的止盈倍数范围（研究 v3）；硬范围仍是风控员的 min_tp_r–max_tp_r。"""


def stop_atr_name(stop: StopScale) -> str:
    """提示词里 stop_atr 的叫法：只有短周期 = ``15m ATR``；带长周期 = ``√(5m ATR × 1h ATR)``。"""
    short = f"{atr_interval(stop.period_min)} ATR"
    if not stop.long_period_min:
        return short
    return f"√({short} × {atr_interval(stop.long_period_min)} ATR)"


def combine_atr(short: float | None, long: float | None, stop: StopScale) -> float | None:
    """stop_atr = √(短 × 长)；没有长周期用短周期；长周期拿不到先只用短周期（同风控员）。"""
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
        f" (suggested {stop.stop_hint[0]:g}-{stop.stop_hint[1]:g}; tight / medium / wide take "
        "the lower end / middle / upper end of that range)"
        if stop.stop_hint
        else ""
    )
    tp_lo, tp_hi = max(TP_HINT[0], stop.min_tp_r), min(TP_HINT[1], stop.max_tp_r)
    tp_hint = f" (suggested {tp_lo:g}-{tp_hi:g}; do not be greedy)" if tp_lo < tp_hi else ""
    head = (
        f"Stop distance is measured in stop_atr = {name} (every ATR is a Wilder {ATR_PERIOD}-bar "
        "ATR on the period set by the risk config, not the ATR you see on 1h klines). "
        f"Each symbol's stop multiple must be between {stop.min_stop_atr:g} and "
        f"{stop.max_stop_atr:g}{sl_hint}; the take-profit multiple (a multiple of the stop "
        f"distance) must be between {stop.min_tp_r:g} and {stop.max_tp_r:g}{tp_hint}. Outside "
        "these ranges the risk guard rejects the symbol on the card"
    )
    if atr is None or price is None or price <= 0:
        return head + ". This coin's current ATR value is unavailable"
    pct = atr / price * 100
    return f"{head}. This coin's current stop_atr = {atr:.6g}, about {pct:.2f}% of price"


def atr_context(symbol: str, end: datetime, stop: StopScale) -> str:
    """取与风控员同周期的 K 线算 stop_atr（不乘时段系数）。失败只说明不可用，不影响开会。"""
    try:
        short, price = _closed_atr(symbol, stop.period_min, end)
        long = _closed_atr(symbol, stop.long_period_min, end)[0] if stop.long_period_min else None
        return describe_atr(combine_atr(short, long, stop), price, stop)
    except Exception:
        logger.warning("%s 算 ATR 失败，这场会拿不到 ATR 说明", symbol, exc_info=True)
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
                f"= {span_stops:g} x the median stop multiple {mid:g} x this coin's stop_atr "
                f"({stop_atr_name(stop)}), a range price almost never leaves inside the holding "
                "window"
            )
        else:
            span = holding_span(atr, stop.eff_period_min, hold_min, sigma)
            basis = (
                f"= {sigma:g} x this coin's {name} ATR x sqrt({hold_min:g}/{stop.period_min}), "
                "a typical move inside the holding window"
            )
        bars = closed
        if candidate_interval and candidate_interval != name:  # 摆动 / 成交密集区用另一个周期找
            raw = _klines(symbol, candidate_interval, 300, end)
            bars = [r for r in raw if r[6] <= end_ms] or closed
        cands = build_candidates(price, atr, bars, daily, span)
        return LevelContext(price, atr, span, cands, basis)
    except Exception:
        logger.warning("%s 算关键位候选失败，这场会没有候选关键位", symbol, exc_info=True)
        return LevelContext()


def describe_hold(hold_min: float, atr: float | None, price: float | None, stop: StopScale) -> str:
    """告诉会议每笔单最多拿多久、持仓期内该币的正常波动有多大，止损 / 止盈 / 关键位按这个来。"""
    head = (
        f"Holding time: every trade in this system is held at most about {hold_min:g} minutes "
        "(force-closed at the deadline). Set stops, targets and key levels from the normal "
        "move inside that time; do not give targets that would take days"
    )
    if atr is None or price is None or price <= 0:
        return head
    period = stop.eff_period_min
    move = holding_span(atr, period, hold_min, 1.0) / price * 100
    return (
        f"{head}. This coin's stop_atr is about {atr / price * 100:.2f}% of price; one standard "
        f"deviation of price movement inside the holding window is about ±{move:.2f}% "
        f"(= stop_atr x sqrt({hold_min:g}/{period:g}))"
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


_INDICATOR_INTERVAL = "1h"
"""行情分析师的指标 / 价格快照周期（按最长持仓档，见 ``core/profiles.py``）；引擎启动时设置。"""


def set_indicator_interval(interval: str) -> None:
    if interval not in _INTERVAL_MIN or interval == "1d":
        raise ValueError(f"指标周期应为 1m–4h 之一：{interval!r}")
    global _INDICATOR_INTERVAL
    _INDICATOR_INTERVAL = interval


def get_binance_indicators(
    symbol: str, indicator: str, as_of_date: str, look_back_days: int
) -> str:
    """登记为 TradingAgents 的 ``get_indicators``：对当前档位周期的已收盘 K 线算常用指标。"""
    end = as_of(as_of_date)
    end_ms = int(end.timestamp() * 1000)
    interval = _INDICATOR_INTERVAL
    rows = [k for k in _klines(symbol, interval, 500, end) if k[6] <= end_ms]  # 只用已收盘的
    o, h, lo, c, v = ([float(k[i]) for k in rows] for i in (1, 2, 3, 4, 5))
    try:
        vals = _indicator(indicator, o, h, lo, c, v)
    except ValueError as e:  # 不认识的指标别让整场会议失败，告诉分析师换一个
        return f"## {indicator}\n{e}. Available: {', '.join(SUPPORTED_INDICATORS)}"
    n = min(look_back_days * (1440 // _INTERVAL_MIN[interval]), len(vals))
    out = [
        f"{datetime.fromtimestamp(k[0] / 1000, tz=UTC):%Y-%m-%d %H:%M}: {v:.6g}"
        for k, v in zip(rows[-n:], vals[-n:], strict=True)
    ]
    return f"## {indicator}（{binance_symbol(symbol)}，{interval}）\n" + "\n".join(reversed(out))


SNAPSHOT_INDICATORS = (
    "close_10_ema",
    "close_50_sma",
    "close_200_sma",
    "rsi",
    "boll",
    "boll_ub",
    "boll_lb",
    "macd",
    "macds",
    "macdh",
    "atr",
)
SNAPSHOT_RECENT = 30


def _no_data(symbol: str, sym: str, detail: str) -> Exception:
    from tradingagents.dataflows.errors import NoMarketDataError

    return NoMarketDataError(symbol, sym, detail)  # type: ignore[no-any-return]


def _bar_time(k: Sequence[Any]) -> str:
    return f"{datetime.fromtimestamp(k[0] / 1000, tz=UTC):%Y-%m-%d %H:%M}"


def build_binance_snapshot(
    symbol: str,
    as_of_date: str,
    look_back_days: int = SNAPSHOT_RECENT,
    indicators: Sequence[str] | None = None,
) -> str:
    """登记为 TradingAgents 的 ``build_verified_market_snapshot``（雅虎日线的替身）：用币安永续
    已收盘的当前档位周期 K 线，格式和原版一样：最新一根 OHLCV、指标表、最近 30 根收盘。"""
    sym = binance_symbol(symbol)
    interval = _INDICATOR_INTERVAL
    end = as_of(as_of_date)
    end_ms = int(end.timestamp() * 1000)
    try:
        rows = [k for k in _klines(symbol, interval, 500, end) if k[6] <= end_ms]
        tick = TICKS.get(sym)
    except binance_gate.RateLimited:
        raise  # 被封了整轮暂停，不当成“没有数据”吞掉
    except Exception as exc:
        raise _no_data(symbol, sym, f"Binance request failed ({type(exc).__name__})") from exc
    if not rows:
        raise _no_data(symbol, sym, f"no closed {interval} bars")
    o, h, lo, c, v = ([float(k[i]) for k in rows] for i in (1, 2, 3, 4, 5))
    price = price_formatter(tick)
    values: dict[str, str] = {}
    for name in indicators or SNAPSHOT_INDICATORS:
        try:
            values[name] = f"{_indicator(name, o, h, lo, c, v)[-1]:.6g}"
        except Exception as exc:  # 一个指标算不出来不拖垮整份快照
            values[name] = f"N/A ({type(exc).__name__})"
    recent = rows[-max(1, min(int(look_back_days), SNAPSHOT_RECENT)) :]
    last = rows[-1]
    lines = [
        f"## Verified market data snapshot — Binance USDT-M perpetual {sym}, {interval} bars, "
        "closed only",
        "",
        f"- Requested analysis time: {end:%Y-%m-%d %H:%M} UTC",
        f"- Latest closed {interval} bar used: {_bar_time(last)} UTC",
        "- Bars that had not closed by the analysis time are excluded.",
        "",
        "### Latest verified OHLCV row",
        "",
        "| Field | Value |",
        "|---|---:|",
        f"| Open | {price(o[-1])} |",
        f"| High | {price(h[-1])} |",
        f"| Low | {price(lo[-1])} |",
        f"| Close | {price(c[-1])} |",
        f"| Volume | {v[-1]:,.2f} |",
        "",
        "### Verified technical indicators (latest row)",
        "",
        "| Indicator | Value |",
        "|---|---:|",
        *(f"| {k} | {val} |" for k, val in values.items()),
        "",
        f"### Recent verified closes (last {len(recent)} bars)",
        "",
        "| Time (UTC) | Close |",
        "|---|---:|",
        *(f"| {_bar_time(k)} | {price(float(k[4]))} |" for k in recent),
        "",
        "Use this snapshot as the source of truth for exact OHLCV, price-level, and "
        "indicator-value claims. If another tool output conflicts with it, flag the discrepancy "
        "rather than inventing a reconciled number. Do not claim historical validation, "
        "support/resistance bounces, or exact percentage moves unless directly supported by tool "
        "output with concrete times and prices.",
    ]
    return "\n".join(lines)


def register_binance_vendor() -> None:
    """把 ``binance`` 加进 TradingAgents 的数据路由表。重复调用无害。"""
    from tradingagents.dataflows import router

    router.VENDOR_METHODS["get_stock_data"]["binance"] = get_binance_stock_data
    router.VENDOR_METHODS["get_indicators"]["binance"] = get_binance_indicators
    from tradingagents.agents import tools

    # 行情分析师的「验证快照」原来读雅虎日线；换成币安当前档位周期的已收盘 K 线
    tools.build_verified_market_snapshot = build_binance_snapshot
    ta_nodes.install()  # 同时换掉写死股票口径的多空研究员、风控三人、新闻分析师


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
            return f"(original news source unavailable: {exc})"

    def get_news(ticker: str, start_date: str, end_date: str) -> str:
        if not ticker.upper().endswith("-USD"):
            return _soft(fallback_news, ticker, start_date, end_date)
        items = hub.for_symbol(binance_symbol(ticker), hours=48)
        return f"## {ticker} announcements and news, last 48 hours\n" + format_items(items)

    def get_global_news(
        as_of_date: str, look_back_days: int | None = None, limit: int | None = None
    ) -> str:
        items = hub.market(hours=(look_back_days or 1) * 24, limit=limit or 10)
        text = "## Recent crypto market announcements and news\n" + format_items(items)
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


_MUST_WRITE = ("key_levels", "allowed_signals")


def _require_in_schema(schema: dict[str, Any]) -> None:
    required = schema.setdefault("required", [])
    required.extend(f for f in _MUST_WRITE if f not in required)


def omitted_fields(draft: BaseModel, has_candidates: bool) -> list[str]:
    """模型在工具调用里整个没写的必填数组（没有候选关键位时 key_levels 不用写）。"""
    return [
        f
        for f in _MUST_WRITE
        if f not in draft.model_fields_set and (f != "key_levels" or has_candidates)
    ]


def reask_prompt(base: str, omitted: Sequence[str]) -> str:
    return (
        f"{base}\n\nYour previous answer left out {' and '.join(omitted)}. Both key_levels and "
        "allowed_signals are required fields: write them explicitly in the answer, using [] only "
        "when there is truly nothing to put in them."
    )


class _CardSymbolDraft(BaseModel):
    model_config = ConfigDict(json_schema_extra=_require_in_schema)

    bias: Bias
    # 两个数组在发给模型的 schema 里标成必填（可选字段在工具调用里会被模型整个跳过），但解析时
    # 放宽成默认空列表：模型仍漏填时转卡不能整个失败，由引擎用 model_fields_set 发现并重问一次
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
    entry_min: float | None = Field(
        default=None,
        ge=0,
        description="Lower edge of the entry zone (price); leave empty if the conclusion gave none",
    )
    entry_max: float | None = Field(
        default=None,
        ge=0,
        description="Upper edge of the entry zone (price); leave empty if the conclusion gave none",
    )
    reason: str = Field(min_length=1, description="Chinese, one or two sentences")


class _LessonDraft(BaseModel):
    class _L(BaseModel):
        text: str
        evidence: str = Field(
            description="Chinese: which kind of card, how many, the average result"
        )

    lessons: list[_L] = Field(default_factory=list, description="At most 5; empty if none stand up")


class _PickDraft(BaseModel):
    class _D(BaseModel):
        symbol: str
        pick: bool = Field(description="true = worth a meeting now; false = skip")
        reason: str = Field(
            description="Chinese, one sentence quoting at least two numbers from this coin's row"
        )

    regime: str = Field(
        min_length=1,
        description="One-sentence market view, in Chinese, citing at least two numbers from the "
        "brief or the pool breadth line",
    )
    decisions: list[_D] = Field(
        description="Exactly one entry for every candidate in the table: pick or skip, each "
        "with its reason"
    )


def pick_problems(draft: _PickDraft, valid: Sequence[str]) -> list[str]:
    """总盘会回答的校验：每个候选正好一条；每条理由和大盘判断至少 2 个数字。返回问题清单。"""
    seen: dict[str, int] = {}
    for d in draft.decisions:
        seen[d.symbol] = seen.get(d.symbol, 0) + 1
    out: list[str] = []
    if missing := [s for s in valid if s not in seen]:
        out.append(f"no decision for: {', '.join(missing)}")
    if dup := [s for s, n in seen.items() if n > 1 and s in valid]:
        out.append(f"more than one decision for: {', '.join(dup)}")
    first: set[str] = set()
    for d in draft.decisions:
        if d.symbol not in valid or d.symbol in first:
            continue
        first.add(d.symbol)
        if market_table.count_numbers(d.reason) < 2:
            out.append(f"{d.symbol}: the reason must quote at least two numbers from its row")
    if market_table.count_numbers(draft.regime) < 2:
        out.append("regime: quote at least two numbers from the brief or the pool breadth line")
    return out


def pick_result(
    draft: _PickDraft,
    valid: Sequence[str],
    max_n: int,
    problems: Sequence[str],
    retried: bool,
    cost: float,
    breadth: str,
    table: str,
) -> PickResult:
    """回答 → ``PickResult``。校验不去掉任何一个挑中的币（格式问题不能让会变少）：
    理由缺数字照样算；挑中数超过 ``max_n`` 才截断（那是名额，不是校验）。"""
    decisions: list[Decision] = []
    seen: set[str] = set()
    for d in draft.decisions:
        if d.symbol in valid and d.symbol not in seen:
            seen.add(d.symbol)
            decisions.append(Decision(d.symbol, d.pick, d.reason))
    picks = [Pick(d.symbol, d.reason) for d in decisions if d.pick][:max_n]
    kept = {p.symbol for p in picks}
    rejections = [Pick(d.symbol, d.reason) for d in decisions if d.symbol not in kept]
    return PickResult(
        draft.regime,
        picks,
        cost,
        rejections,
        decisions=decisions,
        retried=retried,
        errors=list(problems),
        breadth=breadth,
        table=table,
    )


def entry_zone_of(d: _CardSymbolDraft, levels: LevelContext | None) -> Range | None:
    """会议给的进场区间；上下沿必须落在「现价 ± span」里，超出就当没给。
    拿不到背景（现价 / span）时只查 0 < entry_min ≤ entry_max。"""
    lo, hi = d.entry_min, d.entry_max
    if lo is None or hi is None:
        return None
    lo, hi = min(lo, hi), max(lo, hi)  # 模型把上下沿写反很常见：先排序再检查
    if lo <= 0:
        return None
    if levels is not None and levels.price is not None and levels.span:
        near = levels.price - levels.span <= lo and hi <= levels.price + levels.span
        if not near:
            return None
    return Range(min=lo, max=hi)


def draft_to_card_symbol(
    d: _CardSymbolDraft,
    entry: SymbolPoolEntry,
    levels: LevelContext | None = None,
    zone_required: bool = False,
) -> CardSymbol:
    """会议结论 → 作战指令。给了 ``levels`` 就只认候选关键位（对不上的丢），不给才照单全收。
    ``zone_required``（30m–4h 档）：做多 / 做空没有能用的进场区间就出观望。"""

    def rng(lo: float, hi: float) -> Range:
        return Range(min=min(lo, hi), max=max(lo, hi))

    bias = d.bias
    signals = d.allowed_signals if bias is not Bias.NO_TRADE else []
    reason = d.reason
    zone = entry_zone_of(d, levels)
    if bias in (Bias.LONG, Bias.SHORT) and zone_required and zone is None:
        bias, signals, reason = Bias.NO_TRADE, [], f"{reason}（会议没给进场区间）"[:500]
    elif bias is not Bias.NO_TRADE and not signals:
        # 方向卡信号列表为空 = 这个币一个信号都出不来：按默认信号集（全部）填上，并在理由里记一笔
        signals = list(SignalKind)
        reason = f"{reason}（会议没选信号类型，按默认全部信号）"
    return CardSymbol(
        symbol=entry.symbol,
        category=entry.category,
        bias=bias,
        key_levels=(
            [KeyLevel(price=x.price, kind=x.kind, note=x.note) for x in d.key_levels]
            if levels is None
            else select_levels([(x.price, x.note) for x in d.key_levels], levels)
        ),
        allowed_signals=signals,
        size_tier_max=d.size_tier_max,
        notional_usdt=rng(d.notional_min, d.notional_max),
        leverage=rng(d.leverage_min, d.leverage_max),
        stop_atr=rng(d.stop_atr_min, d.stop_atr_max),
        take_profit_r=rng(d.take_profit_r_min, d.take_profit_r_max),
        entry_zone=zone if bias is not Bias.NO_TRADE else None,
        reason=reason,
    )


# ---------------------------------------------------------------- 引擎
def signal_kinds_note(hold_minutes: int) -> str:
    """allowed_signals 里三种新信号怎么选：默认放宽，有方向又没有排除理由就都允许。"""
    kinds = [
        "oi_follow (open interest building in the direction of the move; trend-following)"
        if hold_minutes >= 120
        else "",
        "top_vs_crowd (fade the crowd when top traders lean the other way; contrarian)"
        if hold_minutes >= 480
        else "",
        "btc_lead (BTC moved first and this alt follows; alt cards only)"
        if hold_minutes <= 120 and hold_minutes >= 30
        else "",
    ]
    fit = "; ".join(k for k in kinds if k)
    return (
        "Besides the older signals, three newer ones may be allowed: oi_follow, top_vs_crowd "
        "and btc_lead. Be generous: when the card has a direction and you have no reason to "
        "exclude them, allow them too. Best fit at this horizon: "
        f"{fit or 'none specially'}. "
        "Leave oi_follow out only if the conclusion fades the move, top_vs_crowd out only if "
        "it is a pure breakout chase, and btc_lead out for BTC itself.\n"
    )


def convert_instructions(
    r: RiskConfig,
    p: ta_prompts.PromptParams | None = None,
    lean_tier: SizeTier = SizeTier.M,
    rm: str = "",
    pm: str = "",
) -> str:
    """整理成作战指令的规则部分（不含 ATR / 关键位说明和标的、结论正文）。"""
    p = p or ta_prompts.PromptParams()
    w = ta_prompts.words(p)
    top = r.max_notional_per_trade_usdt
    cap = f"account equity x {r.max_notional_per_trade_x:g}" + (
        f" and at most {top:g} USDT" if top else ""
    )
    zone_rule = " and must have an entry zone" if p.zone_required else ""
    zone = (
        "A long or short must have entry_min / entry_max, which is the bounce-short zone or "
        "pullback-long zone written in the conclusion; if the conclusion gives no usable zone, "
        "choose no_trade."
        if p.zone_required
        else "Give entry_min / entry_max when you can; leave empty if the conclusion gave none."
    )
    return ta_prompts.fill(
        "Turn the trading conclusion below into an operations card. Every number must stay "
        f"inside the hard limits: notional per trade <= {cap}, leverage <= {r.max_leverage}x. "
        "Choose no_trade when the conclusion is weak, self-contradictory, or sits on the eve of a "
        "major event (such as a rate decision). This is Binance perpetual futures, so the only "
        "directions are long, short and no_trade; avoid stock-holding words such as 'trim' or "
        "'accumulate'. Give the stop as a range of ATR multiples (e.g. 1-2) and the take-profit as "
        "a range of multiples of the stop distance (e.g. 1.5-3).\n"
        "Every position here is held at most {H}; read the conclusion as a view of the next {H}.\n"
        f"{w.habit_line(windowed=False)}"
        "Rating mapping: Buy = long, Sell = short, Hold = no_trade. Overweight / Underweight are "
        "only leans: still output long / short, but size_tier_max is at most "
        f"{lean_tier.value}{zone_rule}.\n"
        f"The Research Manager's recommendation is {rm or '(unknown)'}; the Portfolio Manager's "
        f"rating is {pm or '(unknown)'}.\n"
        f"Entry zone: {zone}\n"
        "The decision gives its trade on labelled lines: entry_min and entry_max are the two "
        "prices on its **Entry Zone** line, and size_tier_max is its **Size Tier** line, capped "
        "as above. An Entry Zone of none gives no zone.\n"
        "key_levels: fill it from the candidate key levels listed below, the nearest up to 3 "
        "supports and up to 3 resistances, copying each candidate's price. Breakout signals only "
        "fire at card key levels, so an empty list disables them: leave it empty only when the "
        "candidates are none (then write []).\n"
        "Choose allowed_signals to match the entry method in the conclusion; do not pick only "
        "chase-style signals when the conclusion waits for a zone.\n"
        f"{signal_kinds_note(p.hold_minutes)}"
        "reason: in Chinese (at most 500 characters); state the direction, entry zone, "
        "invalidation price and why this is a view of the next {H}.",
        p,
    )


def convert_prompt(
    entry: SymbolPoolEntry,
    r: RiskConfig,
    regime: str,
    brief: str,
    decision: str,
    atr_note: str,
    level_note: str = "",
    hold_note: str = "",
    params: ta_prompts.PromptParams | None = None,
    lean_tier: SizeTier = SizeTier.M,
    rm: str = "",
    pm: str = "",
) -> str:
    """把会议结论整理成作战指令的提示词（单独成函数，便于快照测试）。"""
    return (
        f"{convert_instructions(r, params, lean_tier, rm, pm)}\n"
        f"{atr_note}\n{hold_note}\n{level_note}\n"
        f"Symbol: {entry.symbol} ({entry.category.value})\nMarket view: {regime}{brief}\n"
        f"Conclusion:\n{decision}"
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
    r"rate.?limit|unavailable this round|tool (?:call )?(?:failed|error)|"
    r"无法获取|获取失败|调用失败|接口(?:报错|错误)",
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
_ACTION = re.compile(r"\*\*Action\*\*\s*:\s*\**\s*(BUY|HOLD|SELL)", re.IGNORECASE)


def direction_conflict(bias: Bias, rating: str | None, trader_plan: str) -> str | None:
    """卡上的方向和会议自己的结论（组合经理评级、交易员建议）是否相反；相反返回说明。
    只比相反的方向（做多 vs 卖出 / 做空 vs 买入）；观望 / 持有 / 解析不出不算矛盾。"""
    if bias in (Bias.NO_TRADE, Bias.BOTH):  # 观望 / 双向都没有"相反"可言
        return None
    proposal = _PROPOSAL.findall(trader_plan or "") or _ACTION.findall(trader_plan or "")
    signals = {("评级", (rating or "").lower())}
    if proposal:
        signals.add(("交易员建议", proposal[-1].lower()))
    against = _SELL if bias is Bias.LONG else _BUY
    for who, value in sorted(signals):
        if value in against:
            way = "做多" if bias is Bias.LONG else "做空"
            return f"{who}是 {value}，和作战指令的方向（{way}）相反"
    return None


_NUM = r"\d[\d,]*(?:\.\d+)?"
_RANGE = rf"({_NUM})\s*[–—~\-到至]\s*({_NUM})"
_ZONE_IN_TEXT = re.compile(
    rf"(?:区间|承接带|入场|进入|挂单|挂限价|在|zone|entry)\D{{0,6}}?{_RANGE}", re.I
)
_STOP_IN_TEXT = re.compile(rf"(?:失效(?:价)?|止损|invalidation|stop)\D{{0,4}}?({_NUM})", re.I)
_TARGET_IN_TEXT = re.compile(rf"(?:目标|止盈|take[- ]profit|target)\D{{0,4}}?({_NUM})", re.I)
_SIZE_IN_TEXT = re.compile(r"(?:仓位|size)\D{0,3}?([SML])\b|\b([SML])\s*档", re.I)
_RATING_LINE = re.compile(r"\*\*Rating\*\*\s*:\s*\**\s*(\w+)", re.I)
_SUMMARY = re.compile(r"\*\*Executive Summary\*\*\s*:(.*?)(?=\n\s*\*\*|\Z)", re.I | re.S)


def _line_re(label: str) -> re.Pattern[str]:
    return re.compile(rf"(\*\*{label}\*\*\s*:)[ \t]*([^\n]*)", re.I)


def _is_empty(value: str) -> bool:
    return value.strip(" *").lower() in {"", "none", "n/a", "-", "无"}


def fill_trade_lines(decision: str, trader_plan: str) -> tuple[str, list[str]]:
    """组合经理评级不是 Hold，可它单独的区间 / 失效价 / 止盈 / 仓位档几行写了 none
    （等回踩的挂单它常这样留空）时，把这些行补上：先取它自己摘要里写的，取不到再用交易员那几行。
    不是 Hold 且两处都没有的行保持原样（转卡照旧按没有区间处理）。返回（新结论, 补了哪些行）。"""
    rated = _RATING_LINE.search(decision)
    rating = rated.group(1).lower() if rated else ""
    if not rating or rating == "hold":
        return decision, []
    summary_match = _SUMMARY.search(decision)
    summary = summary_match.group(1) if summary_match else ""
    from_summary: dict[str, str] = {}
    if m := _ZONE_IN_TEXT.search(summary):
        lo, hi = sorted((m.group(1), m.group(2)), key=lambda v: float(v.replace(",", "")))
        from_summary["Entry Zone"] = f"{lo}–{hi}"
    if m := _STOP_IN_TEXT.search(summary):
        from_summary["Invalidation"] = m.group(1)
    if m := _TARGET_IN_TEXT.search(summary):
        from_summary["Take Profit"] = m.group(1)
    if m := _SIZE_IN_TEXT.search(summary):
        from_summary["Size Tier"] = (m.group(1) or m.group(2)).upper()
    filled: list[str] = []
    for label in ("Entry Zone", "Invalidation", "Take Profit", "Size Tier"):
        pm_line = _line_re(label).search(decision)
        if not pm_line or not _is_empty(pm_line.group(2)):
            continue
        value = from_summary.get(label)
        if value is None:
            tl = _line_re(label).search(trader_plan or "")
            if tl and not _is_empty(tl.group(2)):
                value = tl.group(2).strip()
        if value is None:
            continue
        decision = (
            decision[: pm_line.start()] + f"{pm_line.group(1)} {value}" + decision[pm_line.end() :]
        )
        filled.append(label)
    return decision, filled


_RECOMMENDATION = re.compile(
    r"\*\*Recommendation\*\*\s*:\s*\**\s*(Buy|Overweight|Hold|Underweight|Sell)", re.IGNORECASE
)
_DIRECTION = {"buy": 1, "overweight": 1, "hold": 0, "underweight": -1, "sell": -1}
_STRENGTH = {"buy": 2, "sell": 2, "overweight": 1, "underweight": 1, "hold": 0}


def research_recommendation(plan: str | None) -> str | None:
    """研究经理方案里的 ``**Recommendation**: X``（小写）；解析不出返回 None。"""
    found = _RECOMMENDATION.findall(plan or "")
    return found[0].lower() if found else None  # 取第一处：正文里引用别的评级不算


def _no_trade(cs: CardSymbol, why: str) -> CardSymbol:
    return cs.model_copy(
        update={
            "bias": Bias.NO_TRADE,
            "allowed_signals": [],
            "reason": (f"{cs.reason}（{why}）")[:500],
        }
    )


_TIER_ORDER = {SizeTier.S: 0, SizeTier.M: 1, SizeTier.L: 2}


def rating_guard(
    cs: CardSymbol,
    rm: str | None,
    pm: str | None,
    zone_ok: bool | None = None,
    lean_tier: SizeTier = SizeTier.M,
) -> CardSymbol:
    """评级兜底（只会把卡变弱，不会变强）：卡不能比研究经理和组合经理里较弱的那个更强。

    - 两人方向相反 → 观望；任何一个是 Hold → 观望。
    - 同方向、较弱的是偏向（Overweight / Underweight）→ 仓位档压到 ``lean_tier``（可配置，
      默认 M；只压低不抬高）；必须有进场区间的档
      （``zone_ok`` 为 False，即没有可用区间）→ 观望。
    - 两人都是 Buy 或都是 Sell → 照转卡结果。转卡的方向和他们相反 → 观望。
    ``zone_ok``：None = 这一档不要求区间（或卡还没有区间字段），False = 要求但没有。
    """
    if cs.bias is Bias.NO_TRADE:
        return cs
    given = [r for r in (rm, pm) if r in _DIRECTION]
    if not given:
        return cs
    if "hold" in given:
        who = "研究经理" if rm == "hold" else "组合经理"
        return _no_trade(cs, f"{who}是观望，这一轮不做")
    dirs = {_DIRECTION[r] for r in given}
    if len(dirs) > 1:
        return _no_trade(cs, "研究经理和组合经理方向相反")
    agreed = dirs.pop()
    if cs.bias in (Bias.LONG, Bias.SHORT) and (cs.bias is Bias.LONG) != (agreed > 0):
        return _no_trade(cs, "转卡方向和会议评级相反")
    weakest = min(_STRENGTH[r] for r in given)
    if weakest >= 2:
        return cs
    if zone_ok is False:
        return _no_trade(cs, "只是偏向，要等进场区间，会议没给")
    lean = "偏多" if agreed > 0 else "偏空"
    who = "研究经理" if rm in _DIRECTION and _STRENGTH[rm] == 1 else "组合经理"
    note = f"（{who}只是{lean}，仓位最多 {lean_tier.value}、等区间）"
    reason = cs.reason if note in cs.reason else (cs.reason + note)[:500]
    tier = min(cs.size_tier_max, lean_tier, key=_TIER_ORDER.__getitem__)
    return cs.model_copy(update={"size_tier_max": tier, "reason": reason})


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
DEFAULT_MAX_TOKENS = 16384
"""没写 max_tokens 时的上限。OpenRouter 按每次请求的 max_tokens 预留额度：写成模型最大的
384000，8 场会议并发每次预留约 $0.1，余额一低就成批 402。多空辩论和三位经理的回答实测
都在几千 token 以内，16384 已有余量；要更长在 slow.toml 的 [meeting] max_tokens 里写。"""
MODELS_URL = "https://openrouter.ai/api/v1/models"
RETRY_HINT = "Give the conclusion directly; no thinking out loud."


def _fetch_models() -> list[dict[str, Any]]:

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


def default_max_tokens(
    provider: str,
    models: Sequence[str],
    fetch: Callable[[], list[dict[str, Any]]] = _fetch_models,
) -> int:
    """没写 max_tokens 时用的值：模型最大输出和 ``DEFAULT_MAX_TOKENS`` 里小的那个。"""
    return min(model_max_tokens(provider, models, fetch), DEFAULT_MAX_TOKENS)


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
        self.on_model: Callable[[str], None] | None = None  # 这次调用实际用的模型 id
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
        if self.on_model is not None and model:
            try:
                self.on_model(model)
            except Exception:
                logger.exception("记录模型 id 失败")
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
        if settings.max_tokens is None:  # 没写 = 模型最大输出和 DEFAULT_MAX_TOKENS 里小的那个
            mt = default_max_tokens(settings.provider, [settings.quick_model, settings.deep_model])
            logger.info("max_tokens 用 %d（模型最大输出和 %d 取小）", mt, DEFAULT_MAX_TOKENS)
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
        set_indicator_interval(prompt_params(settings).indicator)
        self._ticks = TICKS
        pp = prompt_params(settings)
        self._book: LessonBook | None = (
            LessonBook(
                Path(settings.results_dir).parent,
                tz=risk.day_reset_tz,
                hold_minutes=pp.hold_minutes,
                horizon=pp.horizon,
                fee_pct=risk.taker_fee_bps * 2 / 100,
            )
            if risk is not None
            else None
        )
        self._write_active_prompts(settings)
        self._guard_reddit(settings)
        if news is not None:
            register_crypto_news_vendor(news)
        ta_nodes.set_news_hub(news)

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

    def _write_active_prompts(self, s: TradingAgentsSettings) -> None:
        """把当前生效的提示词原文写到 ``data/prompts_active.json``，给面板显示。"""
        try:
            p = prompt_params(s)
            w = ta_prompts.words(p)
            out = {
                "hold": p.hold,
                "style": p.style,
                "shared": perp_context(p),
                "shared_reporter": perp_context(p, "reporter"),
                "shared_analyst": perp_context(p, "analyst"),
                "shared_risk": perp_context(p, "risk"),
                "placeholders": {
                    "{start}": "开会时填开会时间（UTC）",
                    "{end}": "开会时填开会时间加最长持仓（UTC）",
                    "{now}": "开会时填当前 UTC 时间",
                    "{max_n}": "开会时填这轮最多挑几个（卡容量减去固定 / 持仓币）",
                    "{prev}": "开会时填上一轮的大盘判断（没有就整行去掉）",
                    "{market}": "开会时填大盘简报和近期新闻",
                    "{table}": "开会时填候选池表",
                },
                "pick": ta_prompts.pick_prompt(
                    p, "{now}", "{max_n}", "{prev}", "{market}", "{table}"
                ),
                "convert": convert_instructions(
                    self._risk, p, s.lean_size_tier, "{research_manager}", "{portfolio_manager}"
                ),
                "meeting_note": ta_prompts.meeting_note(
                    p, "{ticker}", "{SYMBOL}", "{regime}", "{pick_reason}", None, "{start}", "{end}"
                ),
                "position_note": ta_prompts.position_note(p, "{SYMBOL}"),
                "roles": {
                    name: ta_roles.fill(text, p, s.lean_size_tier.value)
                    for name, text in (
                        ("market_analyst", ta_roles.MARKET),
                        ("research_manager", ta_roles.RM),
                        ("trader_system", ta_roles.TRADER_SYSTEM),
                        ("trader_user", ta_roles.TRADER_USER),
                        ("portfolio_manager", ta_roles.PM),
                        ("lessons_line", ta_roles.LESSONS_LINE),
                    )
                },
                "brief_fields": {"main": list(w.brief_main), "background": list(w.brief_bg)},
                "indicator_interval": p.indicator,
                "analyst_klines": list(s.analyst_klines or DEFAULT_ANALYST_KLINES),
            }
            path = Path(s.results_dir).parent / "prompts_active.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
        except Exception:
            logger.exception("写 prompts_active.json 失败（只影响面板显示）")

    def _graph(
        self,
        analysts: tuple[str, ...],
        handler: Any,
        meeting_note: str = "",
        position_note: str = "",
    ) -> Any:
        from tradingagents.default_config import build_default_config

        cfg = build_config(build_default_config(), self._s, crypto_news=self._news is not None)
        graph = ta_nodes.perp_graph_class()(
            selected_analysts=analysts,
            config=cfg,
            callbacks=[handler],
            meeting_note=meeting_note,
            position_note=position_note,
            lessons=self._lessons(),
        )
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

    def _lessons(self) -> str:
        """当前这一批教训（交给组合经理）。没有就是空。"""
        book = getattr(self, "_book", None)
        return book.text() if book is not None else ""

    def _refresh_lessons(self, handler: Any) -> None:
        """每 5 天（曼谷日切后）把打完分的卡汇总成一批教训，下一场会起生效。失败不影响开会。"""
        book = getattr(self, "_book", None)
        now = self._today()
        if book is None or not book.due(now):
            return

        def ask(prompt: str) -> list[Lesson]:
            llm = (
                self._llm(self._graph(("market",), handler), "quick")
                .with_structured_output(_LessonDraft)
                .bind(max_tokens=self._s.json_max_tokens)
            )
            d: _LessonDraft = llm.invoke(prompt, config={"callbacks": [handler]})
            return [Lesson(x.text, x.evidence) for x in d.lessons]

        try:
            book.refresh(now, ask)
        except Exception:
            logger.exception("教训汇总失败（下一场会照旧用上一批）")

    def _snapshot_liquidations(self, symbols: Sequence[str]) -> None:
        """在事件循环里取强平快照，再让工作线程去读（统计对象不是线程安全的）。"""
        if self._liq is None:
            return
        now = self._today()
        fresh = {s: self._liq.liquidations_1h_usdt(s, now) for s in symbols}
        set_liquidation_totals({**_LIQ_TOTALS, **fresh})

    async def pick(
        self,
        candidates: Sequence[SymbolPoolEntry],
        max_n: int,
        previous: BattleCard | None,
        pool: Sequence[SymbolPoolEntry] = (),
    ) -> PickResult:
        if max_n <= 0 or not candidates:
            return PickResult(regime=previous.regime if previous else "无新增标的", picks=[])
        self._snapshot_liquidations([e.symbol for e in candidates])
        return await asyncio.to_thread(
            self._pick_sync, list(candidates), max_n, previous, list(pool) or list(candidates)
        )

    def _pick_sync(
        self,
        candidates: list[SymbolPoolEntry],
        max_n: int,
        previous: BattleCard | None,
        pool: list[SymbolPoolEntry],
    ) -> PickResult:
        meter = self._meter("pick")
        handler = cost_handler(meter)
        self._refresh_lessons(handler)
        try:
            return self._pick_run(candidates, max_n, previous, handler, meter, pool)
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
        pool: Sequence[SymbolPoolEntry] = (),
    ) -> PickResult:
        graph = self._graph(("market",), handler)
        llm = (
            self._llm(graph, self._s.pick_tier)
            .with_structured_output(_PickDraft)
            .bind(max_tokens=self._s.json_max_tokens)
        )
        breadth = market_table.breadth_line(pool or candidates)
        rows = "\n".join(
            market_table.format_row(e, self._s.hold, self._headlines(e.symbol)) for e in candidates
        )
        table = f"{breadth}\n{rows}"
        market = self._brief_text() + self._market_news()
        prompt = ta_prompts.pick_prompt(
            prompt_params(self._s),
            f"{self._today():%Y-%m-%d %H:%M}",
            max_n,
            previous.regime if previous else "",
            market,
            table,
        )
        valid = [e.symbol for e in candidates]
        draft: _PickDraft = llm.invoke(prompt, config={"callbacks": [handler]})
        problems = pick_problems(draft, valid)
        retried = bool(problems)
        if problems:
            # 重问一次，把缺了哪些币、哪条不合格写进追问；重问失败就用第一次的回答
            try:
                again: _PickDraft = llm.invoke(
                    prompt
                    + "\n\nYour previous answer had these problems, answer again in full:\n- "
                    + "\n- ".join(problems),
                    config={"callbacks": [handler]},
                )
                draft, problems = again, pick_problems(again, valid)
            except Exception:
                logger.exception("总盘会重问失败，用第一次的回答")
        return pick_result(draft, valid, max_n, problems, retried, meter.cost, breadth, table)

    def _liq_note(self, symbol: str) -> str:
        total = _LIQ_TOTALS.get(symbol, 0.0)
        return f" liquidated_1h={total:,.0f}USDT" if total > 0 else ""

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
        return (
            "\nRecent announcements and news (for judging the market; may be incomplete):\n"
            + format_items(self._news.market(hours=24, limit=8), now)
        )

    async def analyze(self, entry: SymbolPoolEntry, regime: str, pick_reason: str = "") -> Analysis:
        self._snapshot_liquidations([entry.symbol])
        return await asyncio.to_thread(self._analyze_sync, entry, regime, pick_reason)

    def _analyze_sync(self, entry: SymbolPoolEntry, regime: str, pick_reason: str = "") -> Analysis:
        meter = self._meter(entry.symbol)
        handler = cost_handler(meter)
        live = MeetingLive(Path(self._s.live_dir), entry.symbol) if self._s.live_dir else None
        if live:
            self._live[entry.symbol] = live
            meter.on_call = live.note_call
            meter.on_usage = live.note_usage
            meter.on_model = live.note_model
            meter.on_retry = live.note_retry
        try:
            res = self._analyze_run(entry, regime, handler, meter, live, pick_reason)
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
        pick_reason: str = "",
    ) -> Analysis:
        ticker, asset_type = ta_ticker(entry)
        analysts = self._s.analysts_stock if asset_type == "stock" else self._s.analysts_crypto
        params = prompt_params(self._s)
        try:
            tick = self._ticks.get(entry.symbol)
        except binance_gate.RateLimited:
            raise
        except Exception:
            tick = None
        now = self._today()
        note = ta_prompts.meeting_note(
            params,
            ticker,
            entry.symbol,
            regime,
            pick_reason,
            tick,
            f"{now:%Y-%m-%d %H:%M}",
            f"{now + timedelta(minutes=params.hold_minutes):%Y-%m-%d %H:%M}",
        )
        graph = self._graph(analysts, handler, note, ta_prompts.position_note(params, entry.symbol))
        if live:
            live.step("input", "meeting_note", note)
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
        decision, filled = fill_trade_lines(decision, str(state.get("trader_investment_plan", "")))
        if live and filled:
            live.step(
                "quality",
                "gate",
                f"组合经理单独的{'、'.join(filled)}行是空的，已从摘要 / 交易员补上",
            )
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
        rm = research_recommendation(str(state.get("investment_plan", "")))
        prompt = convert_prompt(
            entry,
            self._risk,
            regime,
            self._brief_text(),
            decision,
            atr_context(entry.symbol, self._today(), self._stop),
            describe_candidates(levels),
            describe_hold(self._s.hold_minutes, levels.atr, levels.price, self._stop),
            params,
            self._s.lean_size_tier,
            rm or "",
            str(rating or ""),
        )
        llm = (
            self._llm(graph, self._s.convert_tier)
            .with_structured_output(_CardSymbolDraft)
            .bind(max_tokens=self._s.json_max_tokens)
        )
        last: Exception | None = None
        base_prompt = prompt
        for attempt in range(2):
            try:
                draft = llm.invoke(prompt, config={"callbacks": [handler]})
                if not isinstance(draft, _CardSymbolDraft):
                    # 思考模型偶尔把结构化输出退回成自由文本：当作没有结论，不去猜
                    raise ValueError("模型没有返回结构化结果")
                omitted = omitted_fields(draft, bool(levels.candidates))
                if omitted and attempt == 0:
                    # 模型把必填数组整个漏了：带更严的提示重问一次
                    prompt = reask_prompt(base_prompt, omitted)
                    last = ValueError(f"模型漏填 {'、'.join(omitted)}")
                    continue
                if omitted:  # 重问后仍漏：退回空列表（卡不丢），记一笔
                    logger.warning("%s 转卡模型漏填 %s，按空列表处理", entry.symbol, omitted)
                    if live:
                        live.step("quality", "gate", f"模型漏填 {'、'.join(omitted)}（重问后仍漏）")
                cs = draft_to_card_symbol(draft, entry, levels, params.zone_required)
                if live:
                    live.step(
                        "quality",
                        "gate",
                        f"关键位：候选 {len(levels.candidates)} 个，"
                        f"会议写了 {len(draft.key_levels)} 个，"
                        f"对上候选留下 {len(cs.key_levels)} 个；"
                        "会议原样返回 key_levels="
                        f"{[(x.price, x.kind.value) for x in draft.key_levels]}"
                        f" allowed_signals={[x.value for x in draft.allowed_signals]}",
                    )
                if why := direction_conflict(
                    cs.bias, rating, str(state.get("trader_investment_plan", ""))
                ):
                    raise RuntimeError(f"{entry.symbol}：会议结论自相矛盾：{why}，这场会不能用")
                cs = rating_guard(
                    cs,
                    rm,
                    str(rating or "").lower() or None,
                    zone_ok=(cs.entry_zone is not None) if params.zone_required else None,
                    lean_tier=self._s.lean_size_tier,
                )
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
