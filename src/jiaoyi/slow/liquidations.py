"""强平数据的接口（选币打分 + 会议上下文用）。

数据本身由快环的行情层产出：它连一路全市场强平流 ``!forceOrder@arr``，按币滚动统计 1 小时名义额
（``market/liquidations.py``）。慢环不另开连接、不重复计数，只依赖下面这个窄接口，接线时把快环的
统计对象传进来即可，没有新契约。

线程安全：快环的统计对象由事件循环改写，所以**只能在事件循环里读**。会议在工作线程里跑，
因此引擎在进线程前先取好快照（``{symbol: 总额}``），线程里只读这份快照，不碰统计对象。
没传时强平分项为 0（选币少一个维度，会议上下文不带强平）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol


class LiquidationSource(Protocol):
    def liquidations_1h_usdt(self, symbol: str, now: datetime) -> float:
        """该币近 1 小时被强平的名义额，USDT（多空合计）。"""
        ...


def describe_total(total: float) -> str:
    if total <= 0:
        return (
            "- No liquidations recorded in the last hour (or the liquidation stream has just "
            "connected or dropped)"
        )
    return (
        f"- About {total:,.0f} USDT liquidated in the last hour (clustered liquidations are "
        "often followed by a reversal after a cascade or squeeze)"
    )
