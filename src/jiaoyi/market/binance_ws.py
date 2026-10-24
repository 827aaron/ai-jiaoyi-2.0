"""币安 U 本位永续 WebSocket 行情 → ``MarketEvent``。

每个标的订 4 路流：逐笔归集成交（aggTrade）、前 5 档盘口（depth5@100ms）、
标记价格和资金费率（markPrice@1s）、1 分钟 K 线（kline_1m，只发已收盘的）。
强平另开一个连接订全市场流（``!forceOrder@arr``）：候选池里的币照样发给下游，
所有币（包括池外的）都记进 ``liquidations``（``LiquidationTracker``），选币用它算“近 1 小时强平额”。
关掉 ``all_market_liquidations`` 就退回每个标的单独订 forceOrder。
币安把 U 本位 WS 按流的类型分了路由：盘口类（depth / bookTicker）走 ``/public/stream``，
成交、标记价格、K 线、强平走 ``/market/stream``。旧的 ``/stream`` 只剩盘口在推（Mac mini 实测），
所以每 40 个标的开两条连接（market 一条、public 一条），全部汇进一个队列。

为什么不用 ccxt 的 watch*：ccxt 每类流单独订阅、强平流在 ccxt 里支持不全，而这里一个连接
就能拿齐五类，少一层依赖、少一次转换。下单、查持仓、保命止损仍由执行层用 ccxt。

断线自动重连（1、2、4……最多 30 秒退避），重连期间 ``health`` 为 degraded。
看门狗：按标的订的连接（每秒都有标记价格 / 盘口）超过 ``stale_after_s`` 秒没有消息，
记警告并断开重连（连上但不推消息也算断流）；全市场强平流可以长时间安静，不看。
队列满时丢最旧的事件并计数，说明消费跟不上，也报 degraded。

团队页的「盘口员」卡由 ``heartbeat`` 上报：订阅几个币、连接几条（断几条）、今天收到多少条消息
（分成交 / 盘口 / K 线 / 标记价格）、重连几次。有连接超过 ``stale_after_s`` 秒没消息就报 down。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosedOK

from jiaoyi.contracts import (
    BookTop,
    Health,
    Interval,
    Kline,
    Liquidation,
    MarketEvent,
    MarkPrice,
    ModuleStatus,
    Role,
    Side,
    TodayStats,
    Trade,
)
from jiaoyi.core.recorder import local_day
from jiaoyi.interfaces import DecisionLog
from jiaoyi.market.liquidations import LiquidationTracker

logger = logging.getLogger(__name__)

DEFAULT_WS_BASE = "wss://fstream.binance.com"
ROUTE_PATHS = {"market": "/market/stream", "public": "/public/stream"}
_PUBLIC_MARKERS = ("@depth", "@bookTicker")
STREAMS_PER_SYMBOL = 5
SYMBOLS_PER_CONN = 40
STALE_AFTER_S = 30.0


def _dt(ms: Any) -> datetime:
    return datetime.fromtimestamp(int(ms) / 1000.0, UTC)


ALL_MARKET_LIQUIDATIONS = "!forceOrder@arr"
ALL_CONTRACT_INFO = "!contractInfo"
"""全市场合约信息（上新、下架、状态变化），和强平同一条连接；交给 ``contract_listener``。"""


@dataclass(frozen=True)
class ContractInfo:
    """``!contractInfo`` 推送的一条：合约状态（``TRADING`` 等）和排的交割 / 下架时间。"""

    symbol: str
    status: str
    delist_at: datetime | None
    """永续没排下架时币安给 2100 年，这里当 None。"""


FINAL_STATUSES = frozenset({"PRE_DELIVERING", "DELIVERING", "DELIVERED", "CLOSE"})
"""合约要交割 / 已下线的状态：当真下架处理。其余非 TRADING（结算中、待上线等）当临时停。"""


def parse_contract_info(d: dict[str, Any]) -> ContractInfo | None:
    if d.get("e") != "contractInfo" or not d.get("s"):
        return None
    dt = int(d.get("dt") or 0)
    at = _dt(dt) if dt > 0 else None
    return ContractInfo(
        symbol=str(d["s"]),
        status=str(d.get("cs") or ""),
        delist_at=at if at is not None and at.year < 2099 else None,
    )


def route_of(stream: str) -> str:
    """流走哪条路由：盘口类走 public，其余（成交、标记价格、K 线、强平）走 market。"""
    return "public" if any(m in stream for m in _PUBLIC_MARKERS) else "market"


_KIND = {Trade: "成交", BookTop: "盘口", Kline: "K 线", MarkPrice: "标记价", Liquidation: "强平"}


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
        base_url: str = DEFAULT_WS_BASE,
        *,
        queue_max: int = 20_000,
        max_backoff_s: float = 30.0,
        all_market_liquidations: bool = True,
        liquidations: LiquidationTracker | None = None,
        status_sink: DecisionLog | None = None,
        day_reset_tz: str = "UTC",
        stale_after_s: float = STALE_AFTER_S,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._all_liq = all_market_liquidations
        self.liquidations = liquidations or LiquidationTracker()
        """全市场强平统计（含候选池外的币），选币按它补“近 1 小时强平额”。"""
        self._symset: frozenset[str] = frozenset()
        self._queue: asyncio.Queue[MarketEvent] = asyncio.Queue(maxsize=queue_max)
        self._tasks: list[asyncio.Task[None]] = []
        self._max_backoff = max_backoff_s
        self._down: set[str] = set()
        self._last: dict[str, datetime] = {}  # 连接 → 最后一条消息（或连上）的时间，看门狗用
        self.by_kind: dict[str, int] = {}
        self._closed = False
        self.symbols: list[str] = []
        self.watch_note = ""
        """主循环写进来的订阅说明（「盯着 N 个币（总盘会 n 持仓 m）」），团队页卡片开头用。"""
        self.dropped = 0
        self.reconnects = 0
        self.messages = 0
        self.last_message_at: datetime | None = None
        self._sink = status_sink
        self._tz = day_reset_tz
        self._stale_s = stale_after_s
        self._day: date | None = None
        self._day_base = 0  # 今天开始时 messages 的值
        self._coverage: Callable[[datetime], tuple[int, int]] | None = None
        self.contract_listener: Callable[[ContractInfo], None] | None = None
        """主循环接上：合约要下架 / 不再交易时封新开仓（第二来源，比公告快）。"""

    def set_coverage(self, coverage: Callable[[datetime], tuple[int, int]]) -> None:
        """接上特征引擎的 ``coverage``：盘口员卡顺带报盘口新鲜几个币、ATR 就绪几个币。"""
        self._coverage = coverage

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
            for route in ROUTE_PATHS:
                mine = [s for s in streams if route_of(s) == route]
                if mine:
                    conn = f"{route}-{i // SYMBOLS_PER_CONN}"
                    self._spawn_conn(conn, self._url_for(route, mine), watch=True)
        if self._all_liq:
            # 全市场强平可以几分钟没有一条，不上看门狗
            streams = [ALL_MARKET_LIQUIDATIONS, ALL_CONTRACT_INFO]
            self._spawn_conn("liquidations", self._url_for("market", streams))

    def _spawn_conn(self, conn: str, url: str, *, watch: bool = False) -> None:
        if watch:
            self._last[conn] = datetime.now(UTC)
        silent_after = self._stale_s if watch else None
        self._tasks.append(asyncio.create_task(self._run(conn, url, silent_after)))

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
        self._last.clear()

    def _url_for(self, route: str, streams: Sequence[str]) -> str:
        return f"{self._base}{ROUTE_PATHS[route]}?streams={'/'.join(streams)}"

    async def _run(self, conn_id: str, url: str, silent_after: float | None = None) -> None:
        backoff = 1.0
        while not self._closed:
            try:
                async with connect(url, ping_interval=20, max_size=2**22) as ws:
                    self._down.discard(conn_id)
                    if conn_id in self._last:
                        self._last[conn_id] = datetime.now(UTC)
                    backoff = 1.0
                    await self._pump(ws, conn_id, silent_after)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("币安行情连接 %s 断开：%s，%.0f 秒后重连", conn_id, e, backoff)
            self._down.add(conn_id)
            self.reconnects += 1
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self._max_backoff)

    def _on_contract(self, info: ContractInfo) -> None:
        if self.contract_listener is None:
            return
        try:
            self.contract_listener(info)
        except Exception:  # 只是第二来源，出错不能断行情
            logger.exception("处理合约信息推送出错：%s", info)

    def accept(self, ev: MarketEvent) -> bool:
        """强平先记进全市场统计；只有订阅了的标的的事件才往下游发。"""
        if isinstance(ev, Liquidation):
            self.liquidations.record(ev)
        return ev.symbol in self._symset

    async def _pump(
        self, ws: ClientConnection, conn_id: str = "", silent_after: float | None = None
    ) -> None:
        """读到连接关闭为止；``silent_after`` 秒没有消息就抛 ``TimeoutError``，由 ``_run`` 重连。"""
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), silent_after)
            except TimeoutError:
                logger.warning("币安行情连接 %s %.0f 秒没有消息，断开重连", conn_id, silent_after)
                raise
            except ConnectionClosedOK:
                return
            try:
                msg = json.loads(raw)
                info = parse_contract_info(msg.get("data", msg)) if isinstance(msg, dict) else None
                if info is not None:
                    self._on_contract(info)
                    continue
                ev = parse_message(msg)
            except (ValueError, KeyError, TypeError) as e:
                logger.debug("忽略解析不了的行情消息：%s", e)
                continue
            if ev is None:
                continue
            self.messages += 1
            self.last_message_at = datetime.now(UTC)
            if conn_id in self._last:
                self._last[conn_id] = self.last_message_at
            kind = _KIND.get(type(ev), "其它")
            self.by_kind[kind] = self.by_kind.get(kind, 0) + 1
            if not self.accept(ev):
                continue
            if self._queue.full():
                self._queue.get_nowait()
                self.dropped += 1
            self._queue.put_nowait(ev)

    # ------------------------------------------------------------------ 状态上报

    def heartbeat(self, now: datetime) -> None:
        """团队页「盘口员」卡：连接、消息量、断流。没给 ``status_sink`` 就不报。"""
        if self._sink is None:
            return
        day = local_day(now, self._tz)
        if day != self._day:
            self._day, self._day_base = day, self.messages
        health, headline = self._status(now)
        try:
            self._sink.heartbeat(
                ModuleStatus(
                    role=Role.BOOK_READER,
                    health=health,
                    ts=now,
                    headline=headline,
                    today=TodayStats(calls=self.messages - self._day_base),
                    version="binance-ws",
                    last_run_at=self.last_message_at,
                    summary=self._summary(now),
                )
            )
        except OSError:
            logger.exception("盘口员上报状态失败")

    def _summary(self, now: datetime) -> str | None:
        """团队页「刚刚：…」：盯着几个币、最后一条行情多久前。"""
        if self.last_message_at is None:
            return None
        age = max((now - self.last_message_at).total_seconds(), 0.0)
        return f"盯着 {len(self.symbols)} 个币，最后一条行情 {age:.0f} 秒前"

    def _status(self, now: datetime) -> tuple[Health, str]:
        if self._closed:
            return Health.DOWN, "行情连接已关闭"
        if not self._tasks:
            return Health.STARTING, "等总盘会挑币再订阅行情"
        if not self.symbols:
            return Health.OK, "没有要盯的币（等总盘会 / 作战卡 / 持仓）"
        conns, down = len(self._tasks), len(self._down)
        links = f"{conns} 条连接" + (f"（断 {down} 条）" if down else "正常")
        tail = f"，重连 {self.reconnects} 次" if self.reconnects else ""
        silent = sorted(
            c for c, t in self._last.items() if (now - t).total_seconds() > self._stale_s
        )
        if silent:
            age = max((now - self._last[c]).total_seconds() for c in silent)
            return (
                Health.DOWN,
                f"行情断流：{'、'.join(silent)} 已 {age:.0f} 秒没消息，{links}{tail}",
            )
        watching = self.watch_note or f"盯着 {len(self.symbols)} 个币"
        if self.last_message_at is None:
            return Health.STARTING, f"{watching}，{links}，等第一条行情{tail}"
        age = (now - self.last_message_at).total_seconds()
        kinds = " / ".join(f"{k} {n}" for k, n in sorted(self.by_kind.items(), key=lambda x: -x[1]))
        head = f"{watching}，{links}，最后消息 {max(age, 0):.0f} 秒前{tail}"
        if kinds:
            head += f"；累计 {kinds}"
        if self._coverage is not None:
            books, ready = self._coverage(now)
            head += f"；盘口新鲜 {books} 个，ATR 就绪 {ready} 个"
        if self.dropped:
            return Health.DEGRADED, f"{head}；下游处理不过来，丢了 {self.dropped} 条"
        if down:
            return Health.DEGRADED, head
        return Health.OK, head
