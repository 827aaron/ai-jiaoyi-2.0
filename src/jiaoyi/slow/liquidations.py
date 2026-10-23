"""强平数据的接口（选币打分 + 会议上下文用）。

数据本身由快环的行情层产出：它连一路全市场强平流 ``!forceOrder@arr``，按币滚动统计 1 小时名义额
（``market/liquidations.py``）。慢环不另开连接、不重复计数，只依赖下面这个窄接口，接线时把快环的
统计对象传进来即可，没有新契约。没传时强平分项为 0（选币少一个维度，会议上下文不带强平）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol


class LiquidationStats(Protocol):
    def liquidations_1h_usdt(self, symbol: str, now: datetime) -> float:
        """该币近 1 小时被强平的名义额，USDT（多空合计）。"""
        ...


def describe_liquidations(stats: LiquidationStats, symbol: str, now: datetime) -> str:
    total = stats.liquidations_1h_usdt(symbol, now)
    if total <= 0:
        return "- 近 1 小时无强平记录（也可能是强平流刚连上或断线）"
    return f"- 近 1 小时被强平约 {total:,.0f} USDT（集中强平常伴随瀑布 / 逼空后的反向波动）"
