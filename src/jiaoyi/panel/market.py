"""持仓页走势图的 K 线：币安 U 本位公开接口，不需要 key。只给面板画图用，交易不依赖它。"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any, Literal

import httpx

from jiaoyi.core.ticks import tick_decimals
from jiaoyi.market.features import wilder_atr

Interval = Literal["1m", "5m", "15m", "30m", "1h", "4h"]
ATR_INTERVAL: dict[int, Interval] = {5: "5m", 15: "15m", 30: "30m", 60: "1h", 240: "4h"}
"""保镖 ATR 用的分钟周期 → 币安原生 K 线周期（同样按 UTC 整点对齐）。"""
SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,20}USDT$")
BASE = "https://fapi.binance.com"
DEMO_BASE = "https://demo-fapi.binance.com"
"""paper / testnet 的持仓在币安 Demo 撮合：现价、持仓页 K 线从这里拿，和账户对得上。"""


DECIMALS_CACHE_S = 6 * 3600.0
PRICES_CACHE_S = 2.0
DEFAULT_RETRY_AFTER_S = {429: 60.0, 418: 120.0}
"""币安没给 ``Retry-After`` 时的保守等待（同 ``market/rest.py``）。"""


class KlineError(Exception):
    pass


class KlineSource:
    def __init__(
        self,
        base_url: str = BASE,
        *,
        cache_s: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._http = httpx.AsyncClient(base_url=base_url, timeout=4.0, transport=transport)
        self._cache_s = cache_s
        self._cache: dict[tuple[str, str], tuple[float, list[dict[str, float]]]] = {}
        self._decimals: tuple[float, dict[str, int]] | None = None
        self._prices: tuple[float, dict[str, float]] | None = None
        self._prices_lock = asyncio.Lock()
        self._kline_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._blocked_until = 0.0
        """被币安 429 / 418 之后，到这个时刻（monotonic）之前一个请求都不发。"""

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """发一个公开 GET。面板和交易程序是同一个 IP：被限流就按 ``Retry-After`` 停发，
        免得 429 升级成 418 封 IP，连累交易程序平仓。"""
        left = self._blocked_until - time.monotonic()
        if left > 0:
            raise KlineError(f"币安限流中，{left:.0f} 秒后再试")
        try:
            resp = await self._http.get(path, params=params)
        except httpx.HTTPError as e:
            raise KlineError(f"币安行情连不上：{type(e).__name__}") from e
        if resp.status_code in DEFAULT_RETRY_AFTER_S:
            try:
                wait = max(float(resp.headers.get("Retry-After", "")), 1.0)
            except ValueError:
                wait = DEFAULT_RETRY_AFTER_S[resp.status_code]
            self._blocked_until = time.monotonic() + wait
            raise KlineError(f"币安限流（{resp.status_code}），{wait:.0f} 秒内不再请求")
        if resp.status_code != 200:
            raise KlineError(f"币安行情返回 {resp.status_code}")
        return resp.json()

    async def last_prices(self) -> dict[str, float]:
        """全部 U 本位合约的最新成交价：一次 ``/fapi/v1/ticker/price`` 拿全，缓存 2 秒；
        同时进来的请求共用同一次拉取。"""
        async with self._prices_lock:
            if self._prices and time.monotonic() - self._prices[0] < PRICES_CACHE_S:
                return self._prices[1]
            rows = await self._get("/fapi/v1/ticker/price")
            out = {
                str(r["symbol"]): float(r["price"])
                for r in rows
                if isinstance(r, dict) and "symbol" in r and "price" in r
            }
            self._prices = (time.monotonic(), out)
            return out

    async def price_decimals(self) -> dict[str, int]:
        """每个 USDT 合约价格的小数位（exchangeInfo 的 PRICE_FILTER tickSize），缓存 6 小时。"""
        if self._decimals and time.monotonic() - self._decimals[0] < DECIMALS_CACHE_S:
            return self._decimals[1]
        info = await self._get("/fapi/v1/exchangeInfo")
        out: dict[str, int] = {}
        for sym in info.get("symbols", []):
            name = sym.get("symbol", "")
            if not isinstance(name, str) or not SYMBOL_RE.match(name):
                continue
            for f in sym.get("filters", []):
                if f.get("filterType") == "PRICE_FILTER" and f.get("tickSize"):
                    out[name] = tick_decimals(str(f["tickSize"]))
        self._decimals = (time.monotonic(), out)
        return out

    async def klines(
        self, symbol: str, interval: Interval, limit: int = 300
    ) -> list[dict[str, float]]:
        """``[{time(秒), open, high, low, close}]``，时间升序。"""
        if not SYMBOL_RE.match(symbol):
            raise KlineError("币种代码不对")
        key = (symbol, interval)
        async with self._kline_locks.setdefault(key, asyncio.Lock()):  # 同时进来的共用一次拉取
            hit = self._cache.get(key)
            if hit and time.monotonic() - hit[0] < self._cache_s:
                return hit[1]
            return await self._fetch_klines(key, limit)

    async def _fetch_klines(self, key: tuple[str, str], limit: int) -> list[dict[str, float]]:
        symbol, interval = key
        rows: list[Any] = await self._get(
            "/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": limit}
        )
        bars = [
            {
                "time": int(r[0]) // 1000,
                "open": float(r[1]),
                "high": float(r[2]),
                "low": float(r[3]),
                "close": float(r[4]),
            }
            for r in rows
        ]
        if len(self._cache) >= 64:  # 只画持仓的币，64 个足够；超了就整个清掉
            self._cache.clear()
        self._cache[key] = (time.monotonic(), bars)
        return bars

    async def atr(self, symbol: str, period_min: int) -> float | None:
        """``period_min`` 分钟线的 ATR（Wilder 14，同实时引擎），最后一根没走完的不算。"""
        interval = ATR_INTERVAL.get(period_min)
        if interval is None:
            raise KlineError(f"没有 {period_min} 分钟的 K 线周期")
        bars = (await self.klines(symbol, interval))[:-1]
        return wilder_atr((b["high"], b["low"], b["close"]) for b in bars)

    async def close(self) -> None:
        await self._http.aclose()
