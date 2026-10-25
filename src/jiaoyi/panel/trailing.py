"""持仓页的移动止损线：启动线、现在的移动止损价，按 K 线估。

hummingbot 不把移动止损的当前状态吐出来，面板按同一口径估（风控员 ``_trailing`` 的说明）：
- 盈亏按扣过开仓手续费算：赚到「启动线」% 才开始跟，启动价 = 开仓价 ×（1 ± (启动线 + 开仓费率)）；
- 启动后止损线 = 最高盈利 − 回撤距离，开仓费两边抵掉，止损价 = 最有利价 ×（1 ∓ 回撤距离）附近，
  精确写法是开仓价 ×（1 ± (最有利涨跌 − 回撤距离)）。
「最有利价」用开仓以后的 1 分钟 K 线最高 / 最低（开仓那一分钟不算，免得算进开仓前的价）
加上现价；超过 1 分钟线覆盖的那段用 5 分钟线补。K 线是分钟级、执行器是逐笔，所以只是估算。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime

from pydantic import BaseModel

from jiaoyi.panel.market import Interval

Bars = Sequence[dict[str, float]]


class TrailEstimate(BaseModel):
    activation_price: float
    """价格到这里，移动止损开始跟（已含开仓手续费）。"""
    best_price: float | None
    """开仓以来最有利的价（多单最高、空单最低）；K 线拿不到时 None。"""
    active: bool | None
    """已经启动没有；K 线拿不到时 None（不知道）。"""
    stop_price: float | None
    """启动以后现在的移动止损价；没启动 / 不知道时 None。"""


def best_since(side: str, opened_at: datetime, bars_1m: Bars, bars_5m: Bars) -> float | None:
    """开仓以后最有利的价：开仓那一根之后的 1 分钟线，再用 5 分钟线补 1 分钟线够不着的那段。"""
    start = opened_at.timestamp()
    first_1m = bars_1m[0]["time"] if bars_1m else float("inf")
    picked = [b for b in bars_1m if b["time"] >= start]
    picked += [b for b in bars_5m if start <= b["time"] and b["time"] + 300 <= first_1m]
    if not picked:
        return None
    if side == "long":
        return max(b["high"] for b in picked)
    return min(b["low"] for b in picked)


def estimate(
    side: str,
    entry: float,
    activation_pct: float,
    delta_pct: float,
    fee_pct: float,
    best: float | None,
    mark: float | None,
) -> TrailEstimate:
    s = 1 if side == "long" else -1
    act_price = entry * (1 + s * (activation_pct + fee_pct) / 100)
    if mark is not None:
        best = mark if best is None else (max(best, mark) if s > 0 else min(best, mark))
    if best is None:
        return TrailEstimate(
            activation_price=act_price, best_price=None, active=None, stop_price=None
        )
    move = s * (best - entry) / entry * 100
    active = move - fee_pct > activation_pct
    stop = entry * (1 + s * (move - delta_pct) / 100) if active else None
    return TrailEstimate(
        activation_price=act_price, best_price=best, active=active, stop_price=stop
    )


async def fetch_best(
    side: str, opened_at: datetime, klines: Callable[[Interval], Awaitable[Bars]]
) -> float | None:
    """拉 K 线算开仓以后最有利的价；``klines(interval)`` 用持仓页 K 线同一个源（有缓存）。
    1 分钟线 300 根够 5 小时，开得更久才再拉 5 分钟线。"""
    b1 = await klines("1m")
    b5: Bars = []
    if b1 and opened_at.timestamp() < b1[0]["time"]:
        b5 = await klines("5m")
    return best_since(side, opened_at, b1, b5)
