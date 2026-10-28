"""回放用的模拟件：延迟模型、纸面账户、按成交价模拟的执行器。

执行器的行为尽量贴近 hummingbot 开仓执行器：止损 / 止盈 / 到时平仓每秒检查一次，
触发后市价全平，所以退出价是“检查那一刻看到的价格”而不是理想的止损价。
这是故意偏保守的：回放结果应该比理想情况差，不能比实盘好看。
"""

from __future__ import annotations

import random
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from jiaoyi.contracts import AccountState, Category, MarketEvent, OrderIntent, Side
from jiaoyi.contracts.market import Kline


def event_ts(ev: MarketEvent) -> datetime:
    """事件“已知”的时刻：K 线是收盘时间，其余是自带时间。"""
    return ev.close_time if isinstance(ev, Kline) else ev.ts


@dataclass(frozen=True)
class LatencyModel:
    """曼谷 → 币安的往返延迟 + 决策模型（Jev）延迟，单位毫秒。

    ``jev_ms`` 为 None 时用决策模型自己回复里的 ``latency_ms``（录制答复带真实延迟）。
    ``jitter`` 是相对抖动（0.3 = ±30%），固定随机种子保证结果可复现。
    """

    order_ms: float = 250.0
    jev_ms: float | None = None
    jitter: float = 0.3
    seed: int = 7
    _rng: random.Random = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_rng", random.Random(self.seed))

    def _j(self, base: float) -> float:
        return max(0.0, base * (1.0 + self._rng.uniform(-self.jitter, self.jitter)))

    def jev_delay(self, reported_ms: float) -> timedelta:
        return timedelta(milliseconds=self._j(reported_ms if self.jev_ms is None else self.jev_ms))

    def order_delay(self) -> timedelta:
        return timedelta(milliseconds=self._j(self.order_ms))


@dataclass
class OpenPosition:
    position_id: str
    intent: OrderIntent
    entry_price: float
    entry_ts: datetime
    entry_fee: float
    stop_price: float
    take_price: float
    last_check: datetime
    best_price: float


@dataclass(frozen=True)
class ClosedTrade:
    position_id: str
    signal_id: str
    symbol: str
    category: Category
    side: Side
    quantity: float
    entry_price: float
    exit_price: float
    entry_ts: datetime
    exit_ts: datetime
    reason: str
    gross_pnl: float
    fees: float
    risk_usdt: float

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.fees


def _sign(side: Side) -> int:
    return 1 if side is Side.LONG else -1


class PaperAccount:
    """纸面账户：权益 = 初始资金 + 已实现净盈亏 + 浮动盈亏；按时区 0 点清零当日起点。"""

    def __init__(
        self,
        initial_equity: float,
        fee_bps: float,
        slippage_bps: float,
        day_reset_tz: str = "Asia/Bangkok",
        leverage_cap: int = 5,
    ) -> None:
        self.initial_equity = initial_equity
        self.fee_bps = fee_bps
        self.slippage_bps = slippage_bps
        self._tz = ZoneInfo(day_reset_tz)
        self._leverage_cap = leverage_cap
        self.realized = 0.0
        self.positions: dict[str, OpenPosition] = {}
        self.closed: list[ClosedTrade] = []
        self.last_price: dict[str, float] = {}
        self._day: object = None
        self._day_start_equity = initial_equity
        self._day_start_realized = 0.0
        self.paused = False

    # ---- 权益与日清零
    def unrealized(self) -> float:
        total = 0.0
        for p in self.positions.values():
            px = self.last_price.get(p.intent.symbol, p.entry_price)
            total += (px - p.entry_price) * p.intent.quantity * _sign(p.intent.side)
        return total

    def equity(self) -> float:
        # 开仓费已经从已实现里扣掉（见 open_position），这里不再重复扣
        return self.initial_equity + self.realized + self.unrealized()

    def day_key(self, ts: datetime) -> object:
        return ts.astimezone(self._tz).date()

    def roll_day(self, ts: datetime) -> bool:
        """跨过时区 0 点就把当日起点权益重置为当前权益；返回是否发生了清零。"""
        key = self.day_key(ts)
        if self._day is None:
            self._day = key
            self._day_start_equity = self.equity()
            self._day_start_realized = self.realized
            return False
        if key != self._day:
            self._day = key
            self._day_start_equity = self.equity()
            self._day_start_realized = self.realized
            return True
        return False

    def state(self, ts: datetime) -> AccountState:
        self.roll_day(ts)
        by_cat: dict[Category, float] = {}
        total = 0.0
        for p in self.positions.values():
            px = self.last_price.get(p.intent.symbol, p.entry_price)
            notional = px * p.intent.quantity
            by_cat[p.intent.category] = by_cat.get(p.intent.category, 0.0) + notional
            total += notional
        eq = max(self.equity(), 0.0)
        margin = total / self._leverage_cap
        return AccountState(
            ts=ts,
            equity_usdt=eq,
            available_usdt=max(eq - margin, 0.0),
            day_start_equity_usdt=max(self._day_start_equity, 0.01),
            open_notional_usdt=total,
            open_notional_by_category=by_cat,
            open_positions=len(self.positions),
            open_symbols=sorted({p.intent.symbol for p in self.positions.values()}),
            open_risk_usdt=sum(
                p.entry_price * p.intent.quantity * p.intent.lifeboat_stop_pct / 100.0
                for p in self.positions.values()
            ),
            day_realized_pnl_usdt=self.realized - self._day_start_realized,
            paused=self.paused,
            open_by_kind=dict(
                Counter(
                    p.intent.signal_kind
                    for p in self.positions.values()
                    if p.intent.signal_kind is not None
                )
            ),
        )

    # ---- 成交
    def _fee(self, notional: float) -> float:
        return notional * self.fee_bps / 10_000.0

    def open_position(self, intent: OrderIntent, ref_price: float, ts: datetime) -> OpenPosition:
        slip = ref_price * self.slippage_bps / 10_000.0
        entry = ref_price + slip * _sign(intent.side)
        fee = self._fee(entry * intent.quantity)
        sl = entry * (1 - _sign(intent.side) * intent.stop_loss_pct / 100.0)
        tp = entry * (1 + _sign(intent.side) * intent.take_profit_pct / 100.0)
        pos = OpenPosition(
            position_id=f"pos-{intent.intent_id}",
            intent=intent,
            entry_price=entry,
            entry_ts=ts,
            entry_fee=fee,
            stop_price=sl,
            take_price=tp,
            last_check=ts,
            best_price=entry,
        )
        self.realized -= fee
        self.positions[pos.position_id] = pos
        return pos

    def close_position(
        self, position_id: str, ref_price: float, ts: datetime, reason: str
    ) -> ClosedTrade:
        pos = self.positions.pop(position_id)
        side = pos.intent.side
        slip = ref_price * self.slippage_bps / 10_000.0
        exit_px = ref_price - slip * _sign(side)
        qty = pos.intent.quantity
        gross = (exit_px - pos.entry_price) * qty * _sign(side)
        exit_fee = self._fee(exit_px * qty)
        self.realized += gross - exit_fee
        trade = ClosedTrade(
            position_id=pos.position_id,
            signal_id=pos.intent.signal_id,
            symbol=pos.intent.symbol,
            category=pos.intent.category,
            side=side,
            quantity=qty,
            entry_price=pos.entry_price,
            exit_price=exit_px,
            entry_ts=pos.entry_ts,
            exit_ts=ts,
            reason=reason,
            gross_pnl=gross,
            fees=pos.entry_fee + exit_fee,
            risk_usdt=pos.intent.risk_usdt,
        )
        self.closed.append(trade)
        return trade

    def check_exits(
        self, symbol: str, price: float, ts: datetime, interval_s: float
    ) -> list[ClosedTrade]:
        """每个持仓至少隔 ``interval_s`` 秒检查一次止损、止盈、时间到。"""
        out: list[ClosedTrade] = []
        self.last_price[symbol] = price
        for pid, pos in list(self.positions.items()):
            if pos.intent.symbol != symbol:
                continue
            if (ts - pos.last_check).total_seconds() < interval_s:
                continue
            pos.last_check = ts
            long = pos.intent.side is Side.LONG
            reason: str | None = None
            if (price <= pos.stop_price) if long else (price >= pos.stop_price):
                reason = "stop_loss"
            elif (price >= pos.take_price) if long else (price <= pos.take_price):
                reason = "take_profit"
            elif (ts - pos.entry_ts).total_seconds() >= pos.intent.time_limit_s:
                reason = "time_limit"
            if reason:
                out.append(self.close_position(pid, price, ts, reason))
        return out
