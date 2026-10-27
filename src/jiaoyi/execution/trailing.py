"""保留 HB 净收益百分点含义，逆算交给止损条件单的价格。

观察价仍是多仓 BestBid / 空仓 BestAsk；交易所触发源是该腿记下的合约成交价或标记价，
两者都不能和观察价宣称等价。
只记程序实际观察的峰值，断流期间没有观察到的最高/最低价不补造。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from jiaoyi.contracts import PositionRecord, Side
from jiaoyi.core.trailing_price import net_of, net_to_price
from jiaoyi.execution.exchange import EntryBasis, ExchangeError, ExecutionBook
from jiaoyi.execution.registry import ServerProtection

BOOK_MAX_AGE_S = 5.0


class TrailingBookUnavailable(ExchangeError):
    """本轮没有可用的盘口证据（读不到、过期、来自未来或源时钟回退）：只是不更新移动止损，
    已确认的止损 / 止盈 / 保命单不受影响；和真正的保护失败分开告警。"""


@dataclass(frozen=True)
class TrailingUpdate:
    net: float
    peak_net: float
    threshold_net: float | None
    stop_price: float | None
    activated: bool
    observation_gap: bool
    peak_net_exact: str
    threshold_net_exact: str | None


def trailing_update(
    rec: PositionRecord,
    state: ServerProtection,
    basis: EntryBasis,
    book: ExecutionBook,
    now: datetime,
) -> TrailingUpdate | None:
    """缺证据抛异常；重复源时刻不更新。这里不产生交易所已触发/已成交的结论。"""
    if (
        basis.symbol != rec.symbol
        or book.symbol != rec.symbol
        or basis.order_id != state.entry_order_id
        or basis.side != ("buy" if rec.side is Side.LONG else "sell")
        or basis.order_status != "FILLED"
        or not all(
            math.isfinite(v)
            for v in (
                basis.filled_quantity,
                basis.filled_quote,
                basis.fee_usdt,
                basis.entry_price,
                book.bid,
                book.ask,
            )
        )
        or min(basis.filled_quantity, basis.filled_quote, basis.entry_price, book.bid) <= 0
        or book.ask < book.bid
        or not math.isclose(
            basis.filled_quantity * basis.entry_price, basis.filled_quote, rel_tol=1e-10
        )
        or state.observed_quantity is None
        or not math.isclose(state.observed_quantity, basis.filled_quantity, rel_tol=1e-10)
        or not state.entry_terminal
        or state.entry_status != "FILLED"
        or rec.trailing_activation_pct is None
        or rec.trailing_stop_pct is None
    ):
        raise ExchangeError("移动止损的入场、费用或盘口证据不完整")
    if any(
        t.tzinfo is None or t.utcoffset() is None for t in (book.source_at, book.observed_at, now)
    ):
        raise TrailingBookUnavailable("移动止损盘口缺带时区的源时刻")
    if (
        not book.source_at <= book.observed_at <= now
        or (now - book.source_at).total_seconds() > BOOK_MAX_AGE_S
    ):
        raise TrailingBookUnavailable("移动止损盘口过期、来自未来或本地时钟回退")
    if state.trailing_source_at is not None:
        if book.source_at < state.trailing_source_at:
            raise TrailingBookUnavailable("移动止损盘口源时钟回退")
        if book.source_at == state.trailing_source_at:
            return None
    if state.trailing_entry_quote is not None and (
        state.trailing_entry_quote != basis.filled_quote
        or state.trailing_entry_quantity != basis.filled_quantity
        or state.trailing_fees_quote != basis.fee_usdt
    ):
        raise ExchangeError("净收益基准已变化，不能沿用旧基准峰值")
    long = rec.side is Side.LONG
    price = Decimal(str(book.bid if long else book.ask))
    quantity, quote, fee = (
        Decimal(str(v)) for v in (basis.filled_quantity, basis.filled_quote, basis.fee_usdt)
    )
    net = net_of(long, price, quantity, quote, fee)
    old_peak = (
        Decimal(state.trailing_peak_net_exact)
        if state.trailing_peak_net_exact is not None
        else (Decimal(str(state.trailing_peak_net)) if state.trailing_peak_net is not None else net)
    )
    if not old_peak.is_finite():
        raise ExchangeError("持久化净收益峰值无效")
    peak = max(old_peak, net)
    activated = state.trailing_activated or net > Decimal(str(rec.trailing_activation_pct)) / 100
    # 反向卡锚定（#314）可覆盖回撤距离；None = 开仓指令带的距离。
    distance = (
        state.trailing_distance_pct
        if state.trailing_distance_pct is not None
        else rec.trailing_stop_pct
    )
    threshold = peak - Decimal(str(distance)) / 100 if activated else None
    stop: Decimal | None = None
    if threshold is not None:
        stop = net_to_price(long, quote, quantity, fee, threshold)
        if not stop.is_finite() or stop <= 0:
            raise ExchangeError("移动止损逆算价格无效")
        if state.trailing_stop_price is not None:
            previous = Decimal(str(state.trailing_stop_price))
            stop = max(stop, previous) if rec.side is Side.LONG else min(stop, previous)
    gap = state.trailing_observation_gap or bool(
        state.trailing_source_at is not None
        and (book.source_at - state.trailing_source_at).total_seconds() > BOOK_MAX_AGE_S
    )
    return TrailingUpdate(
        float(net),
        float(peak),
        float(threshold) if threshold is not None else None,
        float(stop) if stop is not None else None,
        activated,
        gap,
        str(peak),
        str(threshold) if threshold is not None else None,
    )
