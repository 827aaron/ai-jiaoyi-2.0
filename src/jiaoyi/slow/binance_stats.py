"""币安 U 本位永续的公开行情 → ``SymbolStats``（选币用）。只读公开接口，不需要 key。

先用一次性接口（交易规则、24h 行情、盘口顶、资金费率）筛出成交额最大的 ``top_k`` 个，
再对这些逐个补深度、近 1 小时量能、持仓量变化，控制请求权重。强平数据来自快环行情层的全市场强平统计
（见 ``liquidations.LiquidationSource``），没传时填 0。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import httpx

from jiaoyi.slow.liquidations import LiquidationSource
from jiaoyi.slow.screener import SymbolStats

_BASE = "https://fapi.binance.com"


class BinanceStatsSource:
    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        top_k: int = 150,
        concurrency: int = 8,
        timeout_s: float = 10.0,
        liquidations: LiquidationSource | None = None,
    ) -> None:
        self._liq = liquidations
        self._client = client or httpx.AsyncClient(base_url=_BASE, timeout=timeout_s)
        self._top_k = top_k
        self._sem = asyncio.Semaphore(concurrency)

    async def _get(self, path: str, **params: Any) -> Any:
        async with self._sem:
            r = await self._client.get(path, params=params)
        r.raise_for_status()
        return r.json()

    async def fetch(self, now: datetime) -> list[SymbolStats]:
        info, tick24, book, premium = await asyncio.gather(
            self._get("/fapi/v1/exchangeInfo"),
            self._get("/fapi/v1/ticker/24hr"),
            self._get("/fapi/v1/ticker/bookTicker"),
            self._get("/fapi/v1/premiumIndex"),
        )
        onboard = {
            s["symbol"]: int(s["onboardDate"])
            for s in info["symbols"]
            if s["status"] == "TRADING"
            and s["contractType"] == "PERPETUAL"
            and s["quoteAsset"] == "USDT"
        }
        books = {b["symbol"]: b for b in book}
        funding = {p["symbol"]: float(p["lastFundingRate"] or 0.0) for p in premium}
        top = sorted(
            (t for t in tick24 if t["symbol"] in onboard),
            key=lambda t: -float(t["quoteVolume"]),
        )[: self._top_k]
        results = await asyncio.gather(
            *(self._one(t, onboard, books, funding, now) for t in top), return_exceptions=True
        )
        stats = [r for r in results if isinstance(r, SymbolStats)]
        if not stats:
            raise RuntimeError("币安行情全部请求失败")
        return stats

    async def _one(
        self,
        t: dict[str, Any],
        onboard: dict[str, int],
        books: dict[str, Any],
        funding: dict[str, float],
        now: datetime,
    ) -> SymbolStats:
        sym = t["symbol"]
        last = float(t["lastPrice"])
        b = books[sym]
        bid, ask = float(b["bidPrice"]), float(b["askPrice"])
        mid = (bid + ask) / 2 or last
        depth, klines, oi = await asyncio.gather(
            self._get("/fapi/v1/depth", symbol=sym, limit=100),
            self._get("/fapi/v1/klines", symbol=sym, interval="1h", limit=25),
            self._get("/futures/data/openInterestHist", symbol=sym, period="1h", limit=2),
        )
        # 深度：中间价上下 0.5% 内双边挂单额里较小的一边
        lo, hi = mid * 0.995, mid * 1.005
        bid_usdt = sum(float(p) * float(q) for p, q in depth["bids"] if float(p) >= lo)
        ask_usdt = sum(float(p) * float(q) for p, q in depth["asks"] if float(p) <= hi)
        vols = [float(k[7]) for k in klines]  # 小时成交额（USDT）
        avg = sum(vols[:-1]) / max(len(vols) - 1, 1)
        surge = vols[-1] / avg if avg > 0 else 1.0
        oi_change = 0.0
        if len(oi) >= 2 and float(oi[0]["sumOpenInterestValue"]) > 0:
            first, last_oi = (
                float(oi[0]["sumOpenInterestValue"]),
                float(oi[-1]["sumOpenInterestValue"]),
            )
            oi_change = (last_oi - first) / first * 100.0
        listed = datetime.fromtimestamp(onboard[sym] / 1000, tz=UTC)
        return SymbolStats(
            symbol=sym,
            quote_volume_24h_usdt=float(t["quoteVolume"]),
            spread_bps=(ask - bid) / mid * 1e4 if mid else 1e9,
            depth_usdt=min(bid_usdt, ask_usdt),
            listed_days=max((now - listed).days, 0),
            range_24h_pct=(float(t["highPrice"]) - float(t["lowPrice"])) / last * 100.0,
            volume_surge=surge,
            funding_rate=funding.get(sym, 0.0),
            oi_change_pct=oi_change,
            liquidations_1h_usdt=(
                self._liq.liquidations_1h_usdt(sym, now) if self._liq is not None else 0.0
            ),
        )

    async def aclose(self) -> None:
        await self._client.aclose()
