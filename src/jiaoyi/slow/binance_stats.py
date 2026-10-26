"""币安 U 本位永续的公开行情 → ``SymbolStats``（选币用）。只读公开接口，不需要 key。

先用一次性接口（交易规则、24h 行情、盘口顶、资金费率）筛出成交额最大的 ``top_k`` 个，
再对这些逐个补深度、近 1 小时量能、持仓量变化，控制请求权重。强平数据来自快环行情层的全市场强平统计
（见 ``liquidations.LiquidationSource``），没传时填 0。
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import httpx

from jiaoyi.contracts.common import PERPETUAL_CONTRACT_TYPES
from jiaoyi.slow.liquidations import LiquidationSource
from jiaoyi.slow.screener import SymbolStats

_BASE = "https://fapi.binance.com"


def _kind(sym: dict[str, Any]) -> str:
    """exchangeInfo 里说明标的是什么的字段拼成一段小写文字，给选币分类用。"""
    sub = sym.get("underlyingSubType") or []
    parts = [sym.get("contractType", ""), sym.get("underlyingType", ""), *map(str, sub)]
    return " ".join(str(p) for p in parts if p).lower()


class BinanceStatsSource:
    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        top_k: int = 150,
        concurrency: int = 8,
        timeout_s: float = 10.0,
        liquidations: LiquidationSource | None = None,
        hold_minutes: int = 120,
    ) -> None:
        self._liq = liquidations
        self._hold_h = max(hold_minutes / 60.0, 1.0)
        self._n = max(math.ceil(self._hold_h), 1)  # 一个持仓窗口几根 1 小时 K 线
        # 3 个完整窗口 + 最后一根半截小时；至少 26 根（整点后头 15 分钟也有完整的 24 小时基准）
        self._limit = max(26, 3 * self._n + 1)
        self._client = client or httpx.AsyncClient(base_url=_BASE, timeout=timeout_s)
        self._top_k = top_k
        self._sem = asyncio.Semaphore(concurrency)

    async def _get(self, path: str, **params: Any) -> Any:
        async with self._sem:
            r = await self._client.get(path, params=params)
        r.raise_for_status()
        return r.json()

    async def fetch(self, now: datetime) -> list[SymbolStats]:
        return await self._fetch(now)

    async def fetch_symbols(self, symbols: Sequence[str], now: datetime) -> list[SymbolStats]:
        """原池成员的当前统计，不受新成交额排名 top_k 截断；BTC 只用于相关性背景。"""
        return await self._fetch(now, set(symbols) | {"BTCUSDT"})

    async def _fetch(self, now: datetime, symbols: set[str] | None = None) -> list[SymbolStats]:
        info, tick24, book, premium = await asyncio.gather(
            self._get("/fapi/v1/exchangeInfo"),
            self._get("/fapi/v1/ticker/24hr"),
            self._get("/fapi/v1/ticker/bookTicker"),
            self._get("/fapi/v1/premiumIndex"),
        )
        live = [
            s
            for s in info["symbols"]
            if s["status"] == "TRADING"
            and s["contractType"] in PERPETUAL_CONTRACT_TYPES  # 含美股 / 金属的 TRADIFI_PERPETUAL
            and s["quoteAsset"] == "USDT"
        ]
        onboard = {s["symbol"]: int(s["onboardDate"]) for s in live}
        kinds = {s["symbol"]: _kind(s) for s in live}
        books = {b["symbol"]: b for b in book}
        funding = {p["symbol"]: float(p["lastFundingRate"] or 0.0) for p in premium}
        ranked = sorted(
            (t for t in tick24 if t["symbol"] in onboard),
            key=lambda t: -float(t["quoteVolume"]),
        )
        top = (
            [t for t in ranked if t["symbol"] in symbols]
            if symbols is not None
            else ranked[: self._top_k]
        )
        results = await asyncio.gather(
            *(self._one(t, onboard, books, funding, kinds, now) for t in top),
            return_exceptions=True,
        )
        got = [r for r in results if isinstance(r, tuple)]
        if not got:
            raise RuntimeError("币安行情全部请求失败")
        btc = next((c for s, c in got if s.symbol == "BTCUSDT"), None)
        out: list[SymbolStats] = []
        for st, closes in got:
            corr = _corr(closes, btc) if btc is not None else None
            out.append(
                replace(st, metrics={**st.metrics, "corr_btc_24h": corr})
                if corr is not None
                else st
            )
        return out

    async def _one(
        self,
        t: dict[str, Any],
        onboard: dict[str, int],
        books: dict[str, Any],
        funding: dict[str, float],
        kinds: dict[str, str],
        now: datetime,
    ) -> tuple[SymbolStats, list[float]]:
        sym = t["symbol"]
        last = float(t["lastPrice"])
        b = books[sym]
        bid, ask = float(b["bidPrice"]), float(b["askPrice"])
        mid = (bid + ask) / 2 or last
        depth, klines, oi = await asyncio.gather(
            self._get("/fapi/v1/depth", symbol=sym, limit=100),
            self._get("/fapi/v1/klines", symbol=sym, interval="1h", limit=self._limit),
            self._get("/futures/data/openInterestHist", symbol=sym, period="1h", limit=2),
        )
        # 深度：中间价上下 0.5% 内双边挂单额里较小的一边
        lo, hi = mid * 0.995, mid * 1.005
        bid_usdt = sum(float(p) * float(q) for p, q in depth["bids"] if float(p) >= lo)
        ask_usdt = sum(float(p) * float(q) for p, q in depth["asks"] if float(p) <= hi)
        now_ms = now.timestamp() * 1000.0
        # 最后一根是还没走完的小时（半截）；前面都是完整小时
        partial = klines[-1] if klines and float(klines[-1][6]) > now_ms else None
        complete = klines[:-1] if partial is not None else list(klines)
        vols = [float(k[7]) for k in complete]  # 完整小时的成交额（USDT）
        surge, surge_old = _surge(vols, float(partial[7]) if partial else None, partial, now_ms)
        oi_change = 0.0
        if len(oi) >= 2 and float(oi[0]["sumOpenInterestValue"]) > 0:
            first, last_oi = (
                float(oi[0]["sumOpenInterestValue"]),
                float(oi[-1]["sumOpenInterestValue"]),
            )
            oi_change = (last_oi - first) / first * 100.0
        listed = datetime.fromtimestamp(onboard[sym] / 1000, tz=UTC)
        range_hold = self._range_hold(complete, last)
        liq = self._liq.liquidations_1h_usdt(sym, now) if self._liq is not None else 0.0
        stats = SymbolStats(
            symbol=sym,
            quote_volume_24h_usdt=float(t["quoteVolume"]),
            spread_bps=(ask - bid) / mid * 1e4 if mid else 1e9,
            depth_usdt=min(bid_usdt, ask_usdt),
            listed_days=max((now - listed).days, 0),
            range_24h_pct=(float(t["highPrice"]) - float(t["lowPrice"])) / last * 100.0,
            volume_surge=surge,
            kind=kinds.get(sym, ""),
            funding_rate=funding.get(sym, 0.0),
            oi_change_pct=oi_change,
            liquidations_1h_usdt=liq,
            range_hold_pct=range_hold,
            hold_hours=self._hold_h,
            volume_surge_old=surge_old,
            metrics=self._metrics(
                t,
                last,
                complete,
                partial,
                vols,
                surge,
                liq,
                oi_change,
                funding.get(sym, 0.0),
                bid,
                ask,
                mid,
                min(bid_usdt, ask_usdt),
                now,
            ),
            bars=tuple(
                (int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4])) for k in klines
            ),
        )
        return stats, [float(k[4]) for k in complete[-25:]]

    def _range_hold(self, complete: list[Any], last: float) -> float | None:
        """最近 3 个完整持仓窗口各自的（最高 − 最低）÷ 现价，取平均；凑不齐一个完整窗口是 None。"""
        n = self._n
        spans: list[float] = []
        for i in range(3):
            end = len(complete) - i * n
            win = complete[end - n : end] if end - n >= 0 else []
            if len(win) == n and last > 0:
                spans.append(
                    (max(float(k[2]) for k in win) - min(float(k[3]) for k in win)) / last * 100.0
                )
        return sum(spans) / len(spans) if spans else None

    def _metrics(
        self,
        t: dict[str, Any],
        last: float,
        complete: list[Any],
        partial: Any,
        vols: list[float],
        surge: float,
        liq: float,
        oi_change: float,
        funding: float,
        bid: float,
        ask: float,
        mid: float,
        depth_usdt: float,
        now: datetime,
    ) -> dict[str, float]:
        """给总盘会表格用的数字，全部用已经拉到的数据算；拿不到的不写（表里显示 n/a）。"""
        m: dict[str, float] = {
            "price": last,
            "chg_24h": float(t["priceChangePercent"]),
            "funding": funding * 100.0,
            "oi_chg_1h": oi_change,
            "vol_ratio_1h": surge,
            "spread_bps": (ask - bid) / mid * 1e4 if mid else 0.0,
            "depth_k": depth_usdt / 1000.0,
            "qv_24h_m": float(t["quoteVolume"]) / 1e6,
        }
        hi, lo = float(t["highPrice"]), float(t["lowPrice"])
        if hi > lo:
            m["pos_24h"] = (last - lo) / (hi - lo) * 100.0
        if partial is not None and float(partial[1]) > 0:
            m["chg_this_hour"] = (last / float(partial[1]) - 1.0) * 100.0
        if complete:
            o, c = float(complete[-1][1]), float(complete[-1][4])
            if o > 0:
                m["chg_1h"] = (c / o - 1.0) * 100.0
            qv = float(complete[-1][7])
            if qv > 0:
                m["taker_buy_1h"] = float(complete[-1][10]) / qv * 100.0
        if len(complete) >= 5 and float(complete[-5][4]) > 0:
            m["chg_4h"] = (float(complete[-1][4]) / float(complete[-5][4]) - 1.0) * 100.0
        feed_min = None
        feed = getattr(self._liq, "feed_minutes", None)
        if callable(feed):
            feed_min = float(feed(now))
        if feed_min is not None and feed_min < 60.0:
            m["liq_feed_min"] = feed_min  # 强平流刚连上：写 n/a，不写 0
        else:
            m["liq_1h"] = liq
            if vols and vols[-1] > 0:
                m["liq_share"] = liq / vols[-1] * 100.0
        return m

    async def aclose(self) -> None:
        await self._client.aclose()


def _surge(
    vols: list[float], partial_vol: float | None, partial: Any, now_ms: float
) -> tuple[float, float | None]:
    """量能比值（新, 旧）。新：当前这一小时走了 ≥ 15 分钟，按已走分钟折成整小时去比
    最近 24 个完整小时的均值；不到 15 分钟用上一个完整小时比它前面 24 个完整小时。
    旧（只用来对照）：半截小时的成交额直接比前 24 个完整小时的均值。"""
    base = vols[-24:]
    avg = sum(base) / len(base) if base else 0.0
    old = partial_vol / avg if partial_vol is not None and avg > 0 else None
    elapsed = (now_ms - float(partial[0])) / 60000.0 if partial is not None else 0.0
    if partial_vol is not None and elapsed >= 15.0 and avg > 0:
        return partial_vol / (elapsed / 60.0) / avg, old
    if len(vols) >= 2:
        prev = vols[-25:-1]
        pavg = sum(prev) / len(prev) if prev else 0.0
        if pavg > 0:
            return vols[-1] / pavg, old
    return 1.0, old


def _corr(a: list[float], b: list[float]) -> float | None:
    """两串收盘价的对数收益相关系数；长度不够 25 根或任何一边没波动是 None。"""
    n = min(len(a), len(b), 25)
    if n < 25:
        return None
    ra = [math.log(a[-n + i + 1] / a[-n + i]) for i in range(n - 1) if a[-n + i] > 0]
    rb = [math.log(b[-n + i + 1] / b[-n + i]) for i in range(n - 1) if b[-n + i] > 0]
    if len(ra) != len(rb) or len(ra) < 2:
        return None
    ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
    va = sum((x - ma) ** 2 for x in ra)
    vb = sum((x - mb) ** 2 for x in rb)
    if va <= 0 or vb <= 0:
        return None
    return sum((x - ma) * (y - mb) for x, y in zip(ra, rb, strict=True)) / math.sqrt(va * vb)
