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
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import pairwise
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
    "openrouter": ("deepseek/deepseek-v4.1-flash", "deepseek/deepseek-v4.1-flash"),
}


@dataclass(frozen=True)
class TradingAgentsSettings:
    provider: str = "deepseek"
    quick_model: str = "deepseek-flash"
    deep_model: str = "deepseek-flash"
    output_language: str = "Chinese"
    debate_rounds: int = 1
    risk_rounds: int = 1
    max_tokens: int = 8192
    max_retries: int = 6
    # 每类标的请哪几位分析师（越少越省钱）；默认与 TradingAgents 的完整阵容一致。
    analysts_crypto: tuple[str, ...] = ("market", "social", "news")
    analysts_stock: tuple[str, ...] = ("market", "social", "news", "fundamentals")
    # 挑币会议、整理作战指令这两步用哪档模型："quick"=flash，"deep"=深度档模型。
    pick_tier: str = "quick"
    convert_tier: str = "quick"
    # 每百万 token 的美元价（输入, 输出）。⚠ 占位值，M2 拿到 key 真机验证时按 DeepSeek 官网核对，
    # 并把核对日期写在这里。只影响资金页的花费统计，不影响下单和风控。
    price_per_mtok: Mapping[str, tuple[float, float]] = field(
        default_factory=lambda: {"flash": (0.15, 0.60), "pro": (0.50, 2.00)}
    )
    results_dir: str = "data/tradingagents"
    # Reddit 匿名 RSS 限流很严：guarded=加闸（缓存 + 间隔 + 冷却），off=不抓。见 social_guard.py。
    reddit: str = "guarded"
    # 每笔单最多持有多久（分钟，档位的时间止损）：告诉军师团，止损 / 止盈 / 关键位按这个时长的波动给
    hold_minutes: int = 120
    # 关键位范围 = level_span_sigma × ATR × √(持仓时长 / ATR 周期)，见 levels.py
    level_span_sigma: float = SPAN_SIGMA


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
        "that horizon for this specific asset, never multi-day swing targets. Apply all of this "
        "to every sentence you write"
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


def get_binance_stock_data(symbol: str, start_date: str, end_date: str) -> str:
    """登记为 TradingAgents 的 ``get_stock_data``：小时线 CSV（最近 ~200 根）。"""
    end = datetime.fromisoformat(end_date).replace(tzinfo=UTC)
    rows = _klines(symbol, "1h", 200, end)
    lines = ["Date,Open,High,Low,Close,Volume,QuoteVolume"]
    for k in rows:
        ts = datetime.fromtimestamp(k[0] / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M")
        lines.append(f"{ts},{k[1]},{k[2]},{k[3]},{k[4]},{k[5]},{k[7]}")
    return (
        f"# {binance_symbol(symbol)} 币安永续 1h K 线（{start_date} → {end_date}）\n"
        + "\n".join(lines)
        + "\n\n"
        + derivatives_context(symbol, end)
    )


_FAPI = "https://fapi.binance.com"
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


def describe_atr(atr: float | None, price: float | None, stop: StopScale) -> str:
    """给会议提示词用的 ATR 说明；拿不到数值时明说，让模型别瞎猜价格尺度。"""
    name = atr_interval(stop.period_min)
    head = (
        f"止损倍数按 {name} ATR({ATR_PERIOD}) 计（周期由风控配置决定，不是 1h K 线上看到的 ATR），"
        f"每个标的的止损倍数必须在 {stop.min_stop_atr:g}–{stop.max_stop_atr:g} 之间，"
        f"止盈倍数（止损距离的倍数）必须在 {stop.min_tp_r:g}–{stop.max_tp_r:g} 之间，"
        "超出整张卡这个标的会被保镖拒绝"
    )
    if atr is None or price is None or price <= 0:
        return head + "；该币当前 ATR 数值暂不可用"
    pct = atr / price * 100
    return f"{head}。该币当前 {name} ATR({ATR_PERIOD}) = {atr:.6g}，约占价格 {pct:.2f}%"


def atr_context(symbol: str, end: datetime, stop: StopScale) -> str:
    """取与保镖同周期的 K 线算 ATR。失败只说明不可用，不影响开会。"""
    try:
        rows = _klines(symbol, atr_interval(stop.period_min), 60, end)
        closed = [r for r in rows if r[6] <= int(end.timestamp() * 1000)]  # 去掉还没收盘的那根
        price = float(closed[-1][4]) if closed else None
        return describe_atr(wilder_atr(closed), price, stop)
    except Exception:
        return describe_atr(None, None, stop)


def level_context(
    symbol: str, end: datetime, stop: StopScale, hold_min: float, sigma: float = SPAN_SIGMA
) -> LevelContext:
    """取档位周期的 K 线 + 日线算关键位候选。范围 = σ × 该币 ATR × √(持仓时长 / ATR 周期)，
    同一条公式适用所有币所有档位。失败返回空背景（不出关键位，不瞎编）。"""
    try:
        end_ms = int(end.timestamp() * 1000)
        rows = _klines(symbol, atr_interval(stop.period_min), 300, end)
        daily = [r for r in _klines(symbol, "1d", 12, end) if r[6] <= end_ms]
        closed = [r for r in rows if r[6] <= end_ms]
        atr = wilder_atr(closed)
        if not atr or not rows:
            return LevelContext()
        price = float(rows[-1][4])
        span = holding_span(atr, stop.period_min, hold_min, sigma)
        name = atr_interval(stop.period_min)
        basis = (
            f"= {sigma:g}×该币 {name} ATR×√({hold_min:g}/{stop.period_min})，即持仓期内的典型波动"
        )
        cands = build_candidates(price, atr, closed, daily, span)
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
    move = holding_span(atr, stop.period_min, hold_min, 1.0) / price * 100
    return (
        f"{head}。该币 {atr_interval(stop.period_min)} ATR 约占价格 {atr / price * 100:.2f}%，"
        f"持仓期内一个标准差的价格移动约 ±{move:.2f}%（= ATR×√({hold_min:g}/{stop.period_min})）"
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
    end = datetime.fromisoformat(as_of_date).replace(tzinfo=UTC)
    rows = _klines(symbol, "1h", 500, end)
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
        self._risk = risk
        self._stop = stop_scale
        self._s = settings
        self._today = today
        self._news = news
        self._brief = brief
        self._liq = liquidations
        register_binance_vendor()
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

    def _cost(self, usage: Mapping[str, Mapping[str, Any]]) -> float:
        total = 0.0
        for model, u in usage.items():
            tier = "pro" if "pro" in model else "flash"
            pin, pout = self._s.price_per_mtok[tier]
            total += (u.get("input_tokens", 0) * pin + u.get("output_tokens", 0) * pout) / 1e6
        return round(total, 6)

    @staticmethod
    def _llm(graph: Any, tier: str) -> Any:
        return graph.deep_thinking_llm if tier == "deep" else graph.quick_thinking_llm

    def _graph(self, analysts: tuple[str, ...], handler: Any) -> Any:
        from tradingagents.default_config import build_default_config
        from tradingagents.graph.trading_graph import (
            TradingAgentsGraph,
        )

        cfg = build_config(build_default_config(), self._s, crypto_news=self._news is not None)
        return TradingAgentsGraph(selected_analysts=analysts, config=cfg, callbacks=[handler])

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
        from langchain_core.callbacks import UsageMetadataCallbackHandler

        handler = UsageMetadataCallbackHandler()
        try:
            return self._pick_run(candidates, max_n, previous, handler)
        except Exception as exc:  # 失败时把已烧掉的 token 花费带出去，日花费上限才不漏算
            raise MeetingError(str(exc), self._cost(handler.usage_metadata)) from exc

    def _pick_run(
        self,
        candidates: list[SymbolPoolEntry],
        max_n: int,
        previous: BattleCard | None,
        handler: Any,
    ) -> PickResult:
        graph = self._graph(("market",), handler)
        llm = self._llm(graph, self._s.pick_tier).with_structured_output(_PickDraft)
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
            f"开会研究的标的（只能从表里挑），每个给一句理由。{market}\n{table}",
            config={"callbacks": [handler]},
        )
        valid = {e.symbol for e in candidates}
        picks = [Pick(p.symbol, p.reason) for p in draft.picks if p.symbol in valid][:max_n]
        return PickResult(draft.regime, picks, self._cost(handler.usage_metadata))

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
        from langchain_core.callbacks import UsageMetadataCallbackHandler

        handler = UsageMetadataCallbackHandler()
        try:
            return self._analyze_run(entry, regime, handler)
        except Exception as exc:  # 同上：失败也带上真实花费
            raise MeetingError(str(exc), self._cost(handler.usage_metadata)) from exc

    def _analyze_run(self, entry: SymbolPoolEntry, regime: str, handler: Any) -> Analysis:
        ticker, asset_type = ta_ticker(entry)
        analysts = self._s.analysts_stock if asset_type == "stock" else self._s.analysts_crypto
        graph = self._graph(analysts, handler)
        state, _rating = graph.propagate(ticker, f"{self._today():%Y-%m-%d}", asset_type=asset_type)
        decision = str(state.get("final_trade_decision", ""))
        if not decision:
            raise RuntimeError(f"{entry.symbol}：TradingAgents 没有给出结论")

        levels = level_context(
            entry.symbol, self._today(), self._stop, self._s.hold_minutes, self._s.level_span_sigma
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
        llm = self._llm(graph, self._s.convert_tier).with_structured_output(_CardSymbolDraft)
        last: Exception | None = None
        for _ in range(2):
            try:
                draft = llm.invoke(prompt, config={"callbacks": [handler]})
                if not isinstance(draft, _CardSymbolDraft):
                    # 思考模型偶尔把结构化输出退回成自由文本：当作没有结论，不去猜
                    raise ValueError("模型没有返回结构化结果")
                cs = draft_to_card_symbol(draft, entry, levels)
                return Analysis(cs, self._cost(handler.usage_metadata))
            except (ValidationError, ValueError) as exc:
                last = exc
        raise RuntimeError(f"{entry.symbol}：作战指令格式不合格：{last}")
