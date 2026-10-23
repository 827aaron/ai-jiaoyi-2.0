"""账本按币安实际成交回填：入场均价、出场均价、手续费、已实现盈亏。

hummingbot 执行器报的入场价和平仓盈亏是它自己的口径（入场价可能是意向价，外部 / 手动平仓时
没有出场价），面板和日盈亏要和币安对得上，所以成交后用 ``/fapi/v1/userTrades`` 再算一遍：

- 入场：开仓方向（多 = 买、空 = 卖）的成交，从 ``opened_at`` 前 10 秒起按时间累加到持仓数量，
  算成交均价。原来的入场价留在 ``intended_price``。
- 出场：入场成交之后、``closed_at`` 后 60 秒之内的平仓方向成交，累加到持仓数量，算均价。
  手续费 = 开仓 + 平仓成交的手续费（只认 USDT 计价的）；已实现盈亏 = 币安平仓成交的
  ``realizedPnl`` 合计 − 手续费（净额，和执行器的 ``net_pnl_quote`` 同口径，不含资金费）。
- 凑不够数量就不回填，下一轮再试；只回填最近 ``LOOKBACK`` 内开 / 平的仓，旧账不动。

触发：用户数据流（``user_stream``）收到成交回报马上跑一次；没有数据流时主循环每
``FILLS_REFRESH_S`` 秒轮询兜底。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from jiaoyi.contracts import PositionRecord, Side

FILLS_REFRESH_S = 60.0
LOOKBACK = timedelta(hours=24)
OPEN_SLACK = timedelta(seconds=10)
CLOSE_SLACK = timedelta(seconds=60)
QTY_TOL = 1e-6


@dataclass(frozen=True)
class TradeFill:
    """币安 userTrades 的一条成交。"""

    time: datetime
    order_id: str
    side: str
    """``buy`` / ``sell``。"""
    price: float
    qty: float
    commission_usdt: float | None
    """USDT 计价的手续费（正数）；用 BNB 等抵扣时是 None。"""
    realized_pnl: float
    """币安算的这笔成交的已实现盈亏（不含手续费），开仓成交为 0。"""


UserTrades = Callable[[str, datetime], Awaitable[list[TradeFill]]]
"""(symbol, since) → 这之后的成交，按时间排好。"""


@dataclass(frozen=True)
class Fill:
    price: float
    """成交均价。"""
    commission_usdt: float | None
    """这些成交的手续费合计；有一笔不是 USDT 计价就是 None。"""
    realized_pnl: float
    last: datetime
    """凑够数量的最后一笔成交时间。"""


def _take(trades: Iterable[TradeFill], side: str, qty: float, after: datetime) -> Fill | None:
    """按时间累加 ``side`` 方向、``after`` 之后的成交，凑够 ``qty`` 就算均价；凑不够返回 None。"""
    got = cost = pnl = fee = 0.0
    fee_known = True
    for t in sorted(trades, key=lambda t: t.time):
        if t.side != side or t.time < after or t.qty <= 0:
            continue
        use = min(t.qty, qty - got)
        share = use / t.qty
        got += use
        cost += use * t.price
        pnl += t.realized_pnl * share
        if t.commission_usdt is None:
            fee_known = False
        else:
            fee += t.commission_usdt * share
        if got >= qty * (1 - QTY_TOL):
            return Fill(cost / got, fee if fee_known else None, pnl, t.time)
    return None


def open_side(side: Side) -> str:
    return "buy" if side is Side.LONG else "sell"


def close_side(side: Side) -> str:
    return "sell" if side is Side.LONG else "buy"


def entry_fill(rec: PositionRecord, trades: Iterable[TradeFill]) -> Fill | None:
    return _take(trades, open_side(rec.side), rec.quantity, rec.opened_at - OPEN_SLACK)


def exit_fill(rec: PositionRecord, trades: Iterable[TradeFill], entry: Fill | None) -> Fill | None:
    """平仓成交：入场成交之后、``closed_at`` + 60 秒之内。"""
    if rec.closed_at is None:
        return None
    start = entry.last if entry is not None else rec.opened_at
    end = rec.closed_at + CLOSE_SLACK
    window = [t for t in trades if t.time <= end]
    return _take(window, close_side(rec.side), rec.quantity, start)
