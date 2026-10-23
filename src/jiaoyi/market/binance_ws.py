"""币安 U 本位永续 WebSocket 行情 → ``MarketEvent``。

每个标的订 4 路流：逐笔归集成交（aggTrade）、前 5 档盘口（depth5@100ms）、
标记价格和资金费率（markPrice@1s）、1 分钟 K 线（kline_1m，只发已收盘的）。
强平另开一个连接订全市场流（``!forceOrder@arr``）：候选池里的币照样发给下游，
所有币（包括池外的）都记进 ``liquidations``（``LiquidationTracker``），选币用它算“近 1 小时强平额”。
关掉 ``all_market_liquidations`` 就退回每个标的单独订 forceOrder。
币安一个连接最多 200 路流，所以每 40 个标的开一个连接，全部汇进一个队列。

为什么不用 ccxt 的 watch*：ccxt 每类流单独订阅、强平流在 ccxt 里支持不全，而这里一个连接
就能拿齐五类，少一层依赖、少一次转换。下单、查持仓、保命止损仍由执行层用 ccxt。

断线自动重连（1、2、4……最多 30 秒退避），重连期间 ``health`` 为 degraded。
队列满时丢最旧的事件并计数，说明消费跟不上，也报 degraded。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import Any

from websockets.asyncio.client import ClientConnection, connect

from jiaoyi.contracts import (
    BookTop,
    Health,
    Interval,
    Kline,
    Liquidation,
    MarketEvent,
    MarkPrice,
    Side,
    Trade,
)
from jiaoyi.market.liquidations import LiquidationTracker

logger = logging.getLogger(__name__)

DEFAULT_WS_URL = "wss://fstream.binance.com/stream"
STREAMS_PER_SYMBOL = 5
SYMBOLS_PER_CONN = 40


def _dt(ms: Any) -> datetime:
    return datetime.fromtimestamp(int(ms) / 1000.0, UTC)


ALL_MARKET_LIQUIDATIONS = "!forceOrder@arr"


def streams_for(symbol: str, *, liquidations: bool = True) -> list[str]:
    """一个标的要订的流。全市场强平流打开时不再单独订它的 forceOrder，免得重复。"""
    s = symbol.lower()
    out = [f"{s}@aggTrade", f"{s}@depth5@100ms"]
    if liquidations:
        out.append(f"{s}@forceOrder")
    return [*out, f"{s}@markPrice@1s", f"{s}@kline_1m"]


def parse_message(msg: dict[str, Any]) -> MarketEvent | None:
    """把一条组合流消息（``{"stream":..., "data":...}``）或裸消息转成事件。不认识的返回 None。"""
    d = msg.get("data", msg)
    if not isinstance(d, dict):
        return None
    e = d.get("e")
    if e == "aggTrade":
        qty = float(d["q"])
        if qty <= 0:
            return None
        # m=True：买方是挂单方，所以主动方是卖方
        return Trade(
            symbol=d["s"],
            ts=_dt(d["T"]),
            price=float(d["p"]),
            qty=qty,
            taker_side=Side.SHORT if d["m"] else Side.LONG,
        )
    if e == "depthUpdate":
        bids = [(float(p), float(q)) for p, q in d["b"] if float(q) > 0]
        asks = [(float(p), float(q)) for p, q in d["a"] if float(q) > 0]
        if not bids or not asks:
            return None
        return BookTop(symbol=d["s"], ts=_dt(d.get("T", d["E"])), bids=bids, asks=asks)
    if e == "forceOrder":
        o = d["o"]
        qty = float(o.get("z") or o["q"])
        price = float(o.get("ap") or o["p"])
        if qty <= 0 or price <= 0:
            return None
        # 强平单方向 SELL = 被强平的是多头
        return Liquidation(
            symbol=o["s"],
            ts=_dt(o["T"]),
            side=Side.LONG if o["S"] == "SELL" else Side.SHORT,
            price=price,
            qty=qty,
        )
    if e == "markPriceUpdate":
        index = float(d.get("i") or 0.0)
        nxt = int(d.get("T") or 0)
        return MarkPrice(
            symbol=d["s"],
            ts=_dt(d["E"]),
            mark_price=float(d["p"]),
            index_price=index if index > 0 else None,
            funding_rate=float(d.get("r") or 0.0),
            next_funding_at=_dt(nxt) if nxt > 0 else None,
        )
    if e == "kline":
        k = d["k"]
        if not k.get("x"):
            return None  # 未收盘的不发
        return Kline(
            symbol=d["s"],
            interval=Interval(k["i"]),
            open_time=_dt(k["t"]),
            close_time=_dt(k["T"]),
            open=float(k["o"]),
            high=float(k["h"]),
            low=float(k["l"]),
            close=float(k["c"]),
            volume=float(k["v"]),
            quote_volume=float(k["q"]),
        )
    return None


class BinanceFuturesFeed:
    """``MarketDataFeed`` 的实现。"""

    def __init__(
        self,
        url: str = DEFAULT_WS_URL,
        *,
        queue_max: int = 20_000,
        max_backoff_s: float = 30.0,
        all_market_liquidations: bool = True,
        liquidations: LiquidationTracker | None = None,
    ) -> None:
        self._url = url
        self._all_liq = all_market_liquidations
        self.liquidations = liquidations or LiquidationTracker()
        """全市场强平统计（含候选池外的币），选币按它补“近 1 小时强平额”。"""
        self._symset: frozenset[str] = frozenset()
        self._queue: asyncio.Queue[MarketEvent] = asyncio.Queue(maxsize=queue_max)
        self._tasks: list[asyncio.Task[None]] = []
        self._max_backoff = max_backoff_s
        self._down: set[int] = set()
        self._closed = False
        self.symbols: list[str] = []
        self.dropped = 0
        self.reconnects = 0

    @property
    def health(self) -> Health:
        if self._closed:
            return Health.DOWN
        if not self._tasks:
            return Health.STARTING
        if self._down or self.dropped:
            return Health.DEGRADED
        return Health.OK

    async def subscribe(self, symbols: Sequence[str]) -> None:
        new = sorted(set(symbols))
        if new == self.symbols and self._tasks:
            return
        await self._stop_tasks()
        self.symbols = new
        self._symset = frozenset(new)
        self.dropped = 0
        per_symbol_liq = not self._all_liq
        for i in range(0, len(new), SYMBOLS_PER_CONN):
            chunk = new[i : i + SYMBOLS_PER_CONN]
            streams = [s for sym in chunk for s in streams_for(sym, liquidations=per_symbol_liq)]
            self._tasks.append(asyncio.create_task(self._run(i, self._url_for(streams))))
        if self._all_liq:
            url = self._url_for([ALL_MARKET_LIQUIDATIONS])
            self._tasks.append(asyncio.create_task(self._run(-1, url)))

    async def events(self) -> AsyncIterator[MarketEvent]:
        while not self._closed:
            yield await self._queue.get()

    async def close(self) -> None:
        self._closed = True
        await self._stop_tasks()

    async def _stop_tasks(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t
        self._tasks = []
        self._down.clear()

    def _url_for(self, streams: Sequence[str]) -> str:
        return f"{self._url}?streams={'/'.join(streams)}"

    async def _run(self, conn_id: int, url: str) -> None:
        backoff = 1.0
        while not self._closed:
            try:
                async with connect(url, ping_interval=20, max_size=2**22) as ws:
                    self._down.discard(conn_id)
                    backoff = 1.0
                    await self._pump(ws)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("币安行情连接 %s 断开：%s，%.0f 秒后重连", conn_id, e, backoff)
            self._down.add(conn_id)
            self.reconnects += 1
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self._max_backoff)

    def accept(self, ev: MarketEvent) -> bool:
        """强平先记进全市场统计；只有订阅了的标的的事件才往下游发。"""
        if isinstance(ev, Liquidation):
            self.liquidations.record(ev)
        return ev.symbol in self._symset

    async def _pump(self, ws: ClientConnection) -> None:
        async for raw in ws:
            try:
                ev = parse_message(json.loads(raw))
            except (ValueError, KeyError, TypeError) as e:
                logger.debug("忽略解析不了的行情消息：%s", e)
                continue
            if ev is None:
                continue
            if not self.accept(ev):
                continue
            if self._queue.full():
                self._queue.get_nowait()
                self.dropped += 1
            self._queue.put_nowait(ev)
