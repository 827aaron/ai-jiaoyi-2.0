"""情绪分析师（加密币）的数据：币安的持仓人群数据 + 这个币最近 24 小时的新闻标题。

接口都是币安公开的 /futures/data/*（几乎每个 USDT 永续都有，只保留最近 30 天）：
全市场账户多空比、大户持仓多空比、主动买卖量比、持仓量历史。
每一项独立失败，只在报告数据里写「不可用」；被限流（418/429）往外抛，整轮暂停。
"""

from __future__ import annotations

import statistics
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from jiaoyi.slow import binance_gate

_BASE = "https://fapi.binance.com/futures/data"
PERIOD_MIN = {"5m": 5, "15m": 15, "30m": 30, "1h": 60, "2h": 120, "4h": 240, "6h": 360, "12h": 720}
WINDOW_H = 24  # 覆盖最近 24 小时（6–24 小时都在里面）


@dataclass(frozen=True)
class Series:
    title: str
    path: str
    field: str  # 取哪个数
    note: str  # 怎么读
    fmt: str = "{:.3f}"


SERIES = (
    Series(
        "Global long/short account ratio (all accounts)",
        "globalLongShortAccountRatio",
        "longShortRatio",
        ">1 = more accounts are long; crowd positioning of ordinary traders",
    ),
    Series(
        "Top traders long/short position ratio (by position size)",
        "topLongShortPositionRatio",
        "longShortRatio",
        ">1 = large accounts hold more long than short",
    ),
    Series(
        "Taker buy/sell volume ratio",
        "takerlongshortRatio",
        "buySellRatio",
        ">1 = aggressive buying dominates in the period",
    ),
    Series(
        "Open interest (USDT)",
        "openInterestHist",
        "sumOpenInterestValue",
        "rising with price = new money following the move; falling = positions closing",
        "{:,.0f}",
    ),
)


def summarize(rows: Sequence[dict[str, Any]], s: Series, period: str) -> str:
    """一条序列 → 几行文字：最新值、约 6 小时前、约 24 小时前、24 小时高低、最近几个值。纯函数。"""
    vals = [float(r[s.field]) for r in rows if s.field in r]
    if not vals:
        return f"### {s.title}\n(no data returned)"
    f = s.fmt.format

    def ago(hours: int) -> str:
        n = int(hours * 60 / PERIOD_MIN[period])
        return f(vals[-1 - n]) if 0 < n < len(vals) else "n/a"

    stamp = datetime.fromtimestamp(int(rows[-1]["timestamp"]) / 1000, tz=UTC)
    step = max(1, 360 // PERIOD_MIN[period] // 6)  # 最近 6 小时左右均匀取 6 个点
    tail = ", ".join(f(v) for v in vals[::-1][::step][:6][::-1])
    return (
        f"### {s.title} ({period}, latest bar {stamp:%Y-%m-%d %H:%M} UTC)\n"
        f"- Read: {s.note}\n"
        f"- Latest {f(vals[-1])}; about 6h ago {ago(6)}; about 24h ago {f(vals[0])}; "
        f"24h low {f(min(vals))}, high {f(max(vals))}\n"
        f"- Recent values (old to new): {tail}"
    )


# ---------------------------------------------------------------- 相对值（crowd_ratio_mode）
ACCOUNT_RATIO = SERIES[0]
MEDIAN_DAYS = 30  # 币安这几个接口只保留最近 30 天
MEDIAN_PERIOD, MEDIAN_LIMIT = "4h", MEDIAN_DAYS * 6
MEDIAN_TTL_S = 6 * 3600.0  # 30 天中位数变得很慢：每个币 6 小时最多拉一次
_MEDIANS: dict[str, tuple[float, float]] = {}  # symbol → (拉取时刻 monotonic, 中位数)
_MEDIANS_LOCK = threading.Lock()

STRUCTURAL_NOTE = (
    "most coins' all-account ratio sits above 1 structurally, so a ratio above 1 alone does "
    "not show an unusually long crowd"
)


def account_ratio_median(
    symbol: str,
    get: Callable[..., Any] = binance_gate.get,
    clock: Callable[[], float] = time.monotonic,
) -> float | None:
    """全市场账户多空比最近 30 天（4h × 180）的中位数，按币缓存 6 小时。
    拿不到返回 None（不缓存，下一场会再试）。"""
    now = clock()
    with _MEDIANS_LOCK:
        hit = _MEDIANS.get(symbol)
    if hit is not None and now - hit[0] < MEDIAN_TTL_S:
        return hit[1]
    try:
        r = get(
            f"{_BASE}/{ACCOUNT_RATIO.path}",
            {"symbol": symbol, "period": MEDIAN_PERIOD, "limit": MEDIAN_LIMIT},
        )
        r.raise_for_status()
        vals = [float(x[ACCOUNT_RATIO.field]) for x in r.json() if ACCOUNT_RATIO.field in x]
    except binance_gate.RateLimited:
        raise
    except Exception:
        return None
    if not vals:
        return None
    med = statistics.median(vals)
    with _MEDIANS_LOCK:
        _MEDIANS[symbol] = (now, med)
    return med


def relative_line(latest: float | None, median: float | None) -> str:
    """账户多空比相对这个币自己 30 天中位数的偏离；没有中位数就说明只有原值。纯函数。"""
    if latest is None or median is None or median <= 0:
        return (
            f"- Relative view: the {MEDIAN_DAYS}-day median is unavailable this round, so only "
            f"the raw ratio is shown; {STRUCTURAL_NOTE}."
        )
    dev = (latest / median - 1) * 100
    return (
        f"- Relative to this coin's own {MEDIAN_DAYS}-day median {median:.3f}: latest "
        f"{latest:.3f} is {dev:+.1f}% versus that median; {STRUCTURAL_NOTE}, so judge crowding "
        "by the deviation from this coin's own median, not by the ratio being above 1."
    )


def fetch_blocks(
    symbol: str,
    period: str,
    get: Callable[..., Any] = binance_gate.get,
    *,
    ratio_mode: str = "raw",
) -> tuple[list[str], int]:
    """(各序列的文字块, 成功的条数)。某一条失败只写「不可用」。

    ``ratio_mode="relative"``：账户多空比那一块再加一行相对 30 天中位数的偏离
    （多一次 GET，有缓存）。"""
    limit = min(500, max(6, WINDOW_H * 60 // PERIOD_MIN[period]))
    blocks: list[str] = []
    ok = 0
    for s in SERIES:
        try:
            r = get(f"{_BASE}/{s.path}", {"symbol": symbol, "period": period, "limit": limit})
            r.raise_for_status()
            rows = r.json()
            block = summarize(rows, s, period)
            if ratio_mode == "relative" and s is ACCOUNT_RATIO and rows:
                vals = [float(x[s.field]) for x in rows if s.field in x]
                latest = vals[-1] if vals else None
                block += "\n" + relative_line(latest, account_ratio_median(symbol, get))
            blocks.append(block)
            ok += 1 if rows else 0
        except binance_gate.RateLimited:
            raise
        except Exception as exc:
            blocks.append(f"### {s.title}\n(unavailable: {type(exc).__name__})")
    return blocks, ok
