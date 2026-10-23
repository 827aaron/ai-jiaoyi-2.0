"""持仓页走势图的 K 线：币安 U 本位公开接口，不需要 key。只给面板画图用，交易不依赖它。"""

from __future__ import annotations

import re
import time
from typing import Any, Literal

import httpx

from jiaoyi.market.features import wilder_atr

Interval = Literal["1m", "5m", "15m", "30m", "1h", "4h"]
ATR_INTERVAL: dict[int, Interval] = {5: "5m", 15: "15m", 30: "30m", 60: "1h", 240: "4h"}
"""保镖 ATR 用的分钟周期 → 币安原生 K 线周期（同样按 UTC 整点对齐）。"""
SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,20}USDT$")
BASE = "https://fapi.binance.com"


DECIMALS_CACHE_S = 6 * 3600.0


def tick_decimals(tick: str) -> int:
    """``"0.00010000"`` → 4；``"1"`` → 0。"""
    frac = tick.split(".", 1)[1].rstrip("0") if "." in tick else ""
    return len(frac)


class KlineError(Exception):
    pass


class KlineSource:
    def __init__(
        self,
        base_url: str = BASE,
        *,
        cache_s: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._http = httpx.AsyncClient(base_url=base_url, timeout=4.0, transport=transport)
        self._cache_s = cache_s
        self._cache: dict[tuple[str, str], tuple[float, list[dict[str, float]]]] = {}
        self._decimals: tuple[float, dict[str, int]] | None = None

    async def price_decimals(self) -> dict[str, int]:
        """每个 USDT 合约价格的小数位（exchangeInfo 的 PRICE_FILTER tickSize），缓存 6 小时。"""
        if self._decimals and time.monotonic() - self._decimals[0] < DECIMALS_CACHE_S:
            return self._decimals[1]
        try:
            resp = await self._http.get("/fapi/v1/exchangeInfo")
        except httpx.HTTPError as e:
            raise KlineError(f"币安行情连不上：{type(e).__name__}") from e
        if resp.status_code != 200:
            raise KlineError(f"币安行情返回 {resp.status_code}")
        out: dict[str, int] = {}
        for sym in resp.json().get("symbols", []):
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
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < self._cache_s:
            return hit[1]
        try:
            resp = await self._http.get(
                "/fapi/v1/klines", params={"symbol": symbol, "interval": interval, "limit": limit}
            )
        except httpx.HTTPError as e:
            raise KlineError(f"币安行情连不上：{type(e).__name__}") from e
        if resp.status_code != 200:
            raise KlineError(f"币安行情返回 {resp.status_code}")
        rows: list[Any] = resp.json()
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
