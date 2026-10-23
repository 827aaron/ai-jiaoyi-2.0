"""币安 U 本位用户数据流：成交回报一到就让账本回填、收入流水重算。

- ``POST /fapi/v1/listenKey`` 拿 key，连 ``<ws_base>/private/ws?listenKey=…``（币安 2025 年
  拆路由后的私有流地址），连不上再试老地址 ``<ws_base>/ws/<key>``。
- 每 ``KEEPALIVE_S``（30 分钟）续期一次；断线按 1、2、4…最多 60 秒退避重连，重连前重新拿 key。
- 只关心 ``ORDER_TRADE_UPDATE``（有成交：x = TRADE）和 ``ACCOUNT_UPDATE``（余额 / 持仓变了），
  收到就调 ``on_fill``；``listenKeyExpired`` 立刻换 key 重连。别的事件忽略。
- Demo 环境的私有流域名币安文档没写清（``demo-fstream.binance.com`` 待在 Mac 上实测），
  连不上时主循环照样每 60 秒轮询兜底，``status()`` 里写明「只走 60 秒轮询」。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from websockets.asyncio.client import connect

log = logging.getLogger(__name__)

LIVE_WS_BASE = "wss://fstream.binance.com"
DEMO_WS_BASE = "wss://demo-fstream.binance.com"
LEGACY_TESTNET_WS_BASE = "wss://stream.binancefuture.com"
KEEPALIVE_S = 30 * 60.0
MAX_BACKOFF_S = 60.0
FILL_EVENTS = ("ORDER_TRADE_UPDATE", "ACCOUNT_UPDATE")


def ws_urls(base: str, key: str) -> list[str]:
    return [f"{base}/private/ws?listenKey={key}", f"{base}/ws/{key}"]


def is_fill(msg: dict[str, Any]) -> bool:
    """要不要回填：订单有成交，或账户余额 / 持仓变了。"""
    e = msg.get("e")
    if e == "ORDER_TRADE_UPDATE":
        o = msg.get("o") or {}
        return o.get("x") == "TRADE" or float(o.get("l") or 0) > 0
    return e == "ACCOUNT_UPDATE"


@dataclass
class StreamStatus:
    connected: bool = False
    url: str | None = None
    last_event_at: datetime | None = None
    last_error: str | None = None

    def text(self) -> str:
        if self.connected:
            return "成交回报实时（用户数据流已连）"
        why = f"：{self.last_error}" if self.last_error else ""
        return f"用户数据流没连上，只走 60 秒轮询{why}"


class UserStream:
    def __init__(
        self,
        new_key: Callable[[], Awaitable[str]],
        keepalive: Callable[[], Awaitable[None]],
        on_fill: Callable[[], Awaitable[None]],
        *,
        ws_base: str,
        connector: Callable[[str], Any] = connect,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._new_key = new_key
        self._keepalive = keepalive
        self._on_fill = on_fill
        self._base = ws_base
        self._connect = connector
        self._sleep = sleep
        self.status = StreamStatus()

    async def run(self) -> None:
        """一直跑到被取消。"""
        backoff = 1.0
        while True:
            try:
                key = await self._new_key()
                await self._session(key)
                backoff = 1.0  # 连上过：服务器正常断开（24 小时换线）就马上重连
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.status.last_error = f"{type(e).__name__}: {e}"[:200]
                log.warning("用户数据流断了（%s），%.0f 秒后重连", self.status.last_error, backoff)
            self.status.connected = False
            await self._sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_S)

    async def _session(self, key: str) -> bool:
        """连上一个地址读到断；两个地址都连不上就抛最后一个错。返回这次是否收到过成交。"""
        last: Exception | None = None
        for url in ws_urls(self._base, key):
            try:
                ws = await self._connect(url)
            except Exception as e:
                last = e
                continue
            async with ws:
                self.status.connected, self.status.url = True, url.split("?")[0]
                self.status.last_error = None
                log.info("用户数据流已连：%s", self.status.url)
                ticker = asyncio.create_task(self._keep_alive())
                try:
                    return await self._read(ws)
                finally:
                    ticker.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await ticker
        raise last or RuntimeError("没有可连的地址")

    async def _read(self, ws: Any) -> bool:
        got = False
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("e") == "listenKeyExpired":
                self.status.last_error = "listenKey 过期，换新的"
                return got
            if is_fill(msg):
                got = True
                self.status.last_event_at = datetime.now(UTC)
                try:
                    await self._on_fill()
                except Exception:
                    log.exception("成交回报处理失败（60 秒轮询会补上）")
        return got

    async def _keep_alive(self) -> None:
        while True:
            await self._sleep(KEEPALIVE_S)
            try:
                await self._keepalive()
            except Exception:
                log.warning("用户数据流续期失败，断线后会换 key 重连", exc_info=True)
