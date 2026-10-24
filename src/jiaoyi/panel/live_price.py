"""持仓币的实时价：一条币安 WebSocket 订所有持仓币的 ``<币>@kline_1m``（约 250 毫秒推一次，
带这一分钟还没走完的那根 K 线），持仓表的现价、浮盈和持仓页 K 线图最后一根都用它，同一个数。

连哪儿按运行模式定（面板 server 里选）：live 连主网 ``fstream.binance.com``，paper / testnet 连
Demo 撮合的 ``demo-fstream.binance.com``，和账户实际成交的盘子一致。只画图、算显示，交易不依赖它。
主网的 K 线在 ``/market/stream``；Demo 有没有拆路由没实测，连不上就换老的 ``/stream`` 再试。
持仓币变了就断开按新名单重连；断线按 1、2、4……最多 30 秒退避重连，
30 秒没有消息也算断流。断线期间 :meth:`LivePrices.price` 拿不到（超过 ``fresh_s`` 的不算），
由面板退回一次全量 REST 行情并标注。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Iterable
from typing import Any

from websockets.asyncio.client import connect

logger = logging.getLogger(__name__)

WS_BASE = "wss://fstream.binance.com"
DEMO_WS_BASE = "wss://demo-fstream.binance.com"
ROUTES = ("/market/stream", "/stream")
FRESH_S = 10.0
SILENT_S = 30.0


def merge_live(
    bars: list[dict[str, float]], live: dict[str, float] | None, interval: str
) -> list[dict[str, float]]:
    """REST 拉到的 K 线（有缓存，会旧几秒）接上推送来的这一分钟：1 分钟线直接替换 / 追加最后一根，
    别的周期把最后一根的收盘、最高、最低按现价改掉。返回新列表，不动缓存里的。"""
    if not live or not bars:
        return bars
    out = list(bars)
    last = out[-1]
    if interval == "1m":
        if live["time"] == last["time"]:
            out[-1] = dict(live)
        elif live["time"] > last["time"]:
            out.append(dict(live))
        return out
    if live["time"] >= last["time"]:
        px = live["close"]
        out[-1] = {**last, "close": px, "high": max(last["high"], px), "low": min(last["low"], px)}
    return out


class LivePrices:
    def __init__(self, base_url: str = WS_BASE, *, fresh_s: float = FRESH_S) -> None:
        self._base = base_url
        self._fresh_s = fresh_s
        self._want: frozenset[str] = frozenset()
        self._bars: dict[str, tuple[float, dict[str, float]]] = {}
        self._changed = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.connected = False
        self.reconnects = 0
        self._route = 0

    def watch(self, symbols: Iterable[str]) -> None:
        """要盯的币（持仓币）；变了就按新名单重连，第一次有币时才开连接。"""
        want = frozenset(symbols)
        if want != self._want:
            self._want = want
            self._changed.set()
        if want and self._task is None:
            self._task = asyncio.create_task(self._run())

    def bar(self, symbol: str) -> dict[str, float] | None:
        """这一分钟还没走完的那根 1 分钟 K 线；超过 ``fresh_s`` 没更新就是 None。"""
        hit = self._bars.get(symbol)
        if hit is None or time.monotonic() - hit[0] > self._fresh_s:
            return None
        return hit[1]

    def price(self, symbol: str) -> float | None:
        b = self.bar(symbol)
        return b["close"] if b else None

    def on_message(self, raw: str | bytes) -> None:
        """一条组合流消息 ``{"stream": "...", "data": {"e": "kline", "k": {...}}}``。"""
        try:
            k: dict[str, Any] = json.loads(raw)["data"]["k"]
            bar = {
                "time": int(k["t"]) // 1000,
                "open": float(k["o"]),
                "high": float(k["h"]),
                "low": float(k["l"]),
                "close": float(k["c"]),
            }
            sym = str(k["s"])
        except (ValueError, KeyError, TypeError):
            return
        self._bars[sym] = (time.monotonic(), bar)

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        backoff = 1.0
        while True:
            want = self._want
            self._changed.clear()
            if not want:
                self.connected = False
                await self._changed.wait()
                continue
            streams = "/".join(f"{s.lower()}@kline_1m" for s in sorted(want))
            url = f"{self._base}{ROUTES[self._route]}?streams={streams}"
            got = False
            try:
                async with connect(url, ping_interval=20) as ws:
                    self.connected, backoff = True, 1.0
                    silent = 0.0
                    while not self._changed.is_set():
                        try:
                            self.on_message(await asyncio.wait_for(ws.recv(), 1.0))
                            silent, got = 0.0, True
                        except TimeoutError:
                            silent += 1.0
                            if silent >= SILENT_S:
                                raise
                    continue  # 持仓币变了：按新名单重连，不算断线
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("面板实时价推送断开：%s，%.0f 秒后重连", e, backoff)
            if not got:  # 这条路由一条消息都没收到：下次换另一条试
                self._route = (self._route + 1) % len(ROUTES)
            self.connected = False
            self.reconnects += 1
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
