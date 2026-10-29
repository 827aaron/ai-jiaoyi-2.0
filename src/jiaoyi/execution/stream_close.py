"""用户数据流里的成交证据：托管保护腿的子单一成交就认出平仓，不等 hummingbot-api / REST 轮询。

币安在成交后约 100 毫秒推三类事件，这里只收、只算，不发网络请求：

- ``ORDER_TRADE_UPDATE``：一张订单的状态和每笔成交（``l`` 本笔量、``z`` 累计量、``ap`` 均价、
  ``n``/``N`` 手续费和计价币、``rp`` 本笔已实现盈亏、``t`` 成交号）。按订单号聚合，同一成交号
  重复推送只算一次。
- ``ALGO_UPDATE``：条件单（``caid`` = 我们的 clientAlgoId）的状态和触发后下出的子单号 ``ai``。
  子单的 clientOrderId 由交易所生成，不能拿来对应条件单，只认 ``ai``（或 REST 回读的
  ``actualOrderId``）。
- ``ACCOUNT_UPDATE``：持仓量变化（单向持仓 ``ps = BOTH`` 的 ``pa``）。

``evaluate`` 判定一笔托管仓此刻能否按推送收尾：某条腿（任一代）或第一代直连平仓单的子单 FILLED，
累计成交 = 腿的数量 = 入场终态成交量，且之后的 ``ACCOUNT_UPDATE`` 报这个币净仓为 0。缺一项就不收：
部分成交只记腿已触发，对不上号的只减仓成交交给调用方按 REST 补对应，没收到零仓的交给调用方按 REST
查一次持仓。轮询收尾照旧在后面兜底。
"""

from __future__ import annotations

import math
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from jiaoyi.contracts import Side
from jiaoyi.contracts.position import CloseReason, PositionStatus
from jiaoyi.execution.registry import ProtectiveLeg, TrackedPosition

KEEP_ORDERS = 512
"""最多记多少张订单 / 条件单（按最近更新淘汰）；一天的成交远少于这个数。"""
_QTY_REL = 1e-9
_NEVER_SUBMITTED = frozenset({"planned", "superseded", "rejected"})


def _num(value: object) -> float | None:
    try:
        x = float(str(value))
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _ms(value: object) -> int | None:
    x = _num(value)
    return int(x) if x is not None and x > 0 else None


def _id(value: object) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text if text and text != "0" else None


def ms_time(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, UTC)


def _same(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=_QTY_REL, abs_tol=0.0)


@dataclass
class _Trade:
    qty: float
    price: float
    fee_usdt: float | None
    realized: float | None


@dataclass
class OrderFills:
    """一张订单在推送里看到的状态和成交。"""

    symbol: str
    order_id: str
    client_order_id: str
    side: Literal["buy", "sell"]
    reduce_only: bool
    position_side: str
    quantity: float
    status: str = "NEW"
    filled: float = 0.0
    avg_price: float | None = None
    last_fill_ms: int | None = None
    first_fill_ms: int | None = None
    trades: dict[str, _Trade] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        """收到的逐笔成交加起来就是累计成交量（中间没漏推送）。"""
        return self.filled > 0 and _same(sum(t.qty for t in self.trades.values()), self.filled)

    @property
    def price(self) -> float | None:
        if self.avg_price is not None and self.avg_price > 0:
            return self.avg_price
        if not self.complete:
            return None
        return sum(t.qty * t.price for t in self.trades.values()) / self.filled

    @property
    def fee_usdt(self) -> float | None:
        """全部成交的手续费（只认 USDT 计价）；漏了成交或有一笔不是 USDT 就是 None。"""
        if not self.complete or any(t.fee_usdt is None for t in self.trades.values()):
            return None
        return sum(t.fee_usdt or 0.0 for t in self.trades.values())

    @property
    def realized(self) -> float | None:
        if not self.complete or any(t.realized is None for t in self.trades.values()):
            return None
        return sum(t.realized or 0.0 for t in self.trades.values())


@dataclass(frozen=True)
class AlgoState:
    client_algo_id: str
    symbol: str
    status: str
    actual_order_id: str | None
    event_ms: int


@dataclass(frozen=True)
class AccountPosition:
    quantity: float
    """``pa`` 的绝对值。"""
    event_ms: int


class StreamBook:
    """推送证据的内存账：只增不改历史，查询不发请求。"""

    def __init__(self) -> None:
        self._orders: OrderedDict[str, OrderFills] = OrderedDict()
        self._algos: OrderedDict[str, AlgoState] = OrderedDict()
        self._links: dict[str, str] = {}
        """clientAlgoId → 子单号，来自 REST 回读（推送缺 ALGO_UPDATE 时补对应）。"""
        self._accounts: dict[str, AccountPosition] = {}

    def ingest(self, msg: dict[str, Any]) -> str | None:
        """收一条推送；返回涉及的币（没用的事件返回 None）。格式不对的整条丢掉。"""
        e = msg.get("e")
        if e == "ORDER_TRADE_UPDATE":
            return self._order(msg.get("o"))
        if e == "ALGO_UPDATE":
            return self._algo(msg.get("o"), _ms(msg.get("E")) or _ms(msg.get("T")))
        if e == "ACCOUNT_UPDATE":
            return self._account(msg.get("a"), _ms(msg.get("E")) or _ms(msg.get("T")))
        return None

    def _order(self, o: object) -> str | None:
        if not isinstance(o, dict):
            return None
        symbol, oid, side = o.get("s"), _id(o.get("i")), o.get("S")
        qty, filled = _num(o.get("q")), _num(o.get("z"))
        if (
            not isinstance(symbol, str)
            or oid is None
            or side not in ("BUY", "SELL")
            or qty is None
            or qty <= 0
            or filled is None
            or not 0 <= filled <= qty * (1 + _QTY_REL)
        ):
            return None
        agg = self._orders.get(oid)
        if agg is None:
            agg = OrderFills(
                symbol=symbol,
                order_id=oid,
                client_order_id=str(o.get("c") or ""),
                side="buy" if side == "BUY" else "sell",
                reduce_only=o.get("R") is True,
                position_side=str(o.get("ps") or ""),
                quantity=qty,
            )
        elif agg.symbol != symbol or agg.quantity != qty:
            return None
        self._orders[oid] = agg
        self._orders.move_to_end(oid)
        while len(self._orders) > KEEP_ORDERS:
            self._orders.popitem(last=False)
        if o.get("x") == "TRADE":
            tid, last_qty, last_px = _id(o.get("t")), _num(o.get("l")), _num(o.get("L"))
            if tid is not None and last_qty and last_qty > 0 and last_px and last_px > 0:
                fee = _num(o.get("n"))
                agg.trades.setdefault(
                    tid,
                    _Trade(
                        qty=last_qty,
                        price=last_px,
                        fee_usdt=abs(fee) if fee is not None and o.get("N") == "USDT" else None,
                        realized=_num(o.get("rp")),
                    ),
                )
                at = _ms(o.get("T"))
                if at is not None:
                    agg.last_fill_ms = max(agg.last_fill_ms or 0, at)
                    agg.first_fill_ms = min(agg.first_fill_ms or at, at)
        # 推送按连接有序；累计量只增，FILLED 之后不回退（重复或迟到的旧推送不改结果）
        if filled >= agg.filled and agg.status != "FILLED":
            agg.filled = filled
            agg.status = str(o.get("X") or agg.status)
            avg = _num(o.get("ap"))
            if avg is not None and avg > 0:
                agg.avg_price = avg
        return symbol

    def _algo(self, o: object, event_ms: int | None) -> str | None:
        if not isinstance(o, dict) or event_ms is None:
            return None
        cid, symbol, status = o.get("caid"), o.get("s"), o.get("X")
        if not isinstance(cid, str) or not cid or not isinstance(symbol, str):
            return None
        if not isinstance(status, str) or not status:
            return None
        old = self._algos.get(cid)
        if old is not None and old.event_ms > event_ms:
            return symbol
        actual = _id(o.get("ai")) or (old.actual_order_id if old is not None else None)
        self._algos[cid] = AlgoState(cid, symbol, status, actual, event_ms)
        self._algos.move_to_end(cid)
        while len(self._algos) > KEEP_ORDERS:
            self._algos.popitem(last=False)
        return symbol

    def _account(self, a: object, event_ms: int | None) -> str | None:
        if not isinstance(a, dict) or event_ms is None:
            return None
        rows = a.get("P")
        if not isinstance(rows, list):
            return None
        touched: str | None = None
        for p in rows:
            if not isinstance(p, dict) or p.get("ps") not in (None, "BOTH"):
                continue
            symbol, amount = p.get("s"), _num(p.get("pa"))
            if not isinstance(symbol, str) or amount is None:
                continue
            old = self._accounts.get(symbol)
            if old is None or old.event_ms <= event_ms:
                self._accounts[symbol] = AccountPosition(abs(amount), event_ms)
            touched = symbol
        return touched

    def link(self, client_algo_id: str, order_id: str) -> None:
        """REST 回读到的条件单子单号。"""
        self._links[client_algo_id] = order_id

    def algo(self, client_algo_id: str) -> AlgoState | None:
        return self._algos.get(client_algo_id)

    def order(self, order_id: str) -> OrderFills | None:
        return self._orders.get(order_id)

    def by_client_id(self, client_order_id: str) -> OrderFills | None:
        for agg in reversed(self._orders.values()):
            if agg.client_order_id == client_order_id:
                return agg
        return None

    def account(self, symbol: str) -> AccountPosition | None:
        return self._accounts.get(symbol)

    def child_of(self, leg: ProtectiveLeg) -> OrderFills | None:
        oid = leg.actual_order_id or self._links.get(leg.client_order_id)
        if oid is None:
            algo = self._algos.get(leg.client_order_id)
            oid = algo.actual_order_id if algo is not None else None
        return self._orders.get(oid) if oid is not None else None

    def close_fills(self, symbol: str, close_side: str, since_ms: int) -> list[OrderFills]:
        """这个币 ``since_ms`` 之后有成交的只减仓平仓方向订单。"""
        return [
            o
            for o in self._orders.values()
            if o.symbol == symbol
            and o.side == close_side
            and o.reduce_only
            and o.filled > 0
            and (o.last_fill_ms or 0) >= since_ms
        ]


@dataclass(frozen=True)
class Verdict:
    """``evaluate`` 的结论。

    ``close``：可以按推送收尾；``partial``：有腿已触发、子单部分成交，只记腿；``need_flat``：
    成交全了但还没看到净仓为 0 的推送（调用方按 REST 查一次持仓）；``need_link``：有对不上号的
    只减仓成交（调用方按 REST 读条件单补对应）。"""

    kind: Literal["close", "partial", "need_flat", "need_link"]
    fills: OrderFills | None = None
    leg: ProtectiveLeg | None = None
    reason: CloseReason | None = None
    unknown: tuple[str, ...] = ()
    """``need_link`` 时对不上号的订单号。"""


def _leg_reason(leg: ProtectiveLeg) -> CloseReason:
    if leg.purpose == "lifeboat":
        return CloseReason.LIFEBOAT
    if leg.purpose == "trailing":
        return CloseReason.TRAILING_STOP
    if leg.purpose == "tightened":
        return CloseReason.TIGHTENED_STOP
    return CloseReason.STOP_LOSS if leg.kind == "stop_loss" else CloseReason.TAKE_PROFIT


def _ours(fills: OrderFills, tracked: TrackedPosition) -> bool:
    rec = tracked.record
    close_side = "sell" if rec.side is Side.LONG else "buy"
    return (
        fills.symbol == rec.symbol
        and fills.side == close_side
        and fills.reduce_only
        and fills.position_side == "BOTH"
    )


def evaluate(tracked: TrackedPosition, book: StreamBook) -> Verdict | None:
    rec, state = tracked.record, tracked.server_protection
    if state is None or rec.status not in (PositionStatus.OPEN, PositionStatus.CLOSING):
        return None
    held = state.entry_filled_quantity
    if not state.entry_terminal or held is None or held <= 0 or rec.entry_price is None:
        return None
    close_side = "sell" if rec.side is Side.LONG else "buy"
    known: set[str] = set()
    full: list[tuple[OrderFills, ProtectiveLeg | None, CloseReason]] = []
    partial: tuple[OrderFills, ProtectiveLeg] | None = None
    for leg in state.legs:
        if leg.state in _NEVER_SUBMITTED:
            continue
        fills = book.child_of(leg)
        if fills is None or not _ours(fills, tracked) or not _same(fills.quantity, leg.quantity):
            continue
        known.add(fills.order_id)
        if fills.status == "FILLED" and _same(fills.filled, leg.quantity):
            full.append((fills, leg, _leg_reason(leg)))
        elif fills.filled > 0 and partial is None:
            partial = (fills, leg)
    dc = tracked.direct_close_client_order_id
    if dc is not None:
        fills = book.by_client_id(dc)
        if fills is not None and _ours(fills, tracked):
            known.add(fills.order_id)
            if (
                tracked.direct_close_generation == 1
                and fills.status == "FILLED"
                and _same(fills.filled, fills.quantity)
            ):
                reason = tracked.requested_close or state.exit_reason or CloseReason.MANUAL
                full.append((fills, None, reason))
    since = int(rec.opened_at.timestamp() * 1000)
    unknown = tuple(
        o.order_id
        for o in book.close_fills(rec.symbol, close_side, since)
        if o.order_id not in known
    )
    if unknown:
        return Verdict("need_link", unknown=unknown)
    if len(full) != 1:
        # 没有，或两路都报全成（不可能同时平掉同一笔仓）：交给轮询核实
        return Verdict("partial", partial[0], partial[1]) if partial and not full else None
    fills, hit, reason = full[0]
    if partial is not None or not _same(fills.filled, held):
        return None  # 成交量和入场量对不上：不是一张单平完的，交给轮询
    acct = book.account(rec.symbol)
    if (
        acct is None
        or acct.quantity != 0
        or fills.last_fill_ms is None
        or acct.event_ms < fills.last_fill_ms
    ):
        return Verdict("need_flat", fills, hit, reason)
    return Verdict("close", fills, hit, reason)
