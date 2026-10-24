"""情绪分析师（加密币）的数据：币安的持仓人群数据 + 这个币最近 24 小时的新闻标题。

接口都是币安公开的 /futures/data/*（几乎每个 USDT 永续都有，只保留最近 30 天）：
全市场账户多空比、大户持仓多空比、主动买卖量比、持仓量历史。
每一项独立失败，只在报告数据里写「不可用」；被限流（418/429）往外抛，整轮暂停。
"""

from __future__ import annotations

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


def fetch_blocks(
    symbol: str, period: str, get: Callable[..., Any] = binance_gate.get
) -> tuple[list[str], int]:
    """(各序列的文字块, 成功的条数)。某一条失败只写「不可用」。"""
    limit = min(500, max(6, WINDOW_H * 60 // PERIOD_MIN[period]))
    blocks: list[str] = []
    ok = 0
    for s in SERIES:
        try:
            r = get(f"{_BASE}/{s.path}", {"symbol": symbol, "period": period, "limit": limit})
            r.raise_for_status()
            rows = r.json()
            blocks.append(summarize(rows, s, period))
            ok += 1 if rows else 0
        except binance_gate.RateLimited:
            raise
        except Exception as exc:
            blocks.append(f"### {s.title}\n(unavailable: {type(exc).__name__})")
    return blocks, ok
