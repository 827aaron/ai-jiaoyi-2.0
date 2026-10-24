"""币安永续合约的价格精度（exchangeInfo 的 PRICE_FILTER.tickSize），缓存 6 小时。

军师团在工作线程里同步取（走全局限流闸），拿不到就返回 None，提示词里去掉价格精度那半句。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from jiaoyi.slow import binance_gate

logger = logging.getLogger(__name__)

URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"
CACHE_S = 6 * 3600.0
RETRY_S = 60.0


class TickSizes:
    def __init__(
        self,
        fetch: Callable[[], dict[str, str]] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetch = fetch or self._http
        self._clock = clock
        self._lock = threading.Lock()
        self._ticks: dict[str, str] = {}
        self._at = float("-inf")

    @staticmethod
    def _http() -> dict[str, str]:
        r = binance_gate.get(URL, {})
        r.raise_for_status()
        out: dict[str, str] = {}
        for s in r.json().get("symbols", []):
            for f in s.get("filters", []):
                if f.get("filterType") == "PRICE_FILTER":
                    out[str(s["symbol"])] = str(f["tickSize"])
        return out

    def get(self, symbol: str) -> str | None:
        """tickSize 原文（如 ``"0.00010000"``）；取不到返回 None（失败后下次再试）。"""
        with self._lock:
            if self._clock() - self._at > CACHE_S:
                try:
                    self._ticks = self._fetch()
                    self._at = self._clock()
                except binance_gate.RateLimited:
                    raise
                except Exception:
                    logger.warning("读 exchangeInfo 的 tickSize 失败", exc_info=True)
                    self._at = self._clock() - CACHE_S + RETRY_S  # 一分钟后再试，不要每场会都打
            return self._ticks.get(symbol)
