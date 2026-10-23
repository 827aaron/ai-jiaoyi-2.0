"""持仓页走势图的 K 线：币安 U 本位公开接口，不需要 key。只给面板画图用，交易不依赖它。"""

from __future__ import annotations

import re
import time
from typing import Any, Literal

import httpx

Interval = Literal["1m", "5m", "15m", "1h"]
SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,20}USDT$")
BASE = "https://fapi.binance.com"


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

    async def close(self) -> None:
        await self._http.aclose()
