"""行情的唯一出口：哨兵这层（实时流 + 特征引擎）拉数据，管家、管家助手、拍板人快照、大盘翻转都
找它拿，不各自去敲币安，免得重启时一起抢权重被封 IP。

- 实时流新鲜、要的字段已经算得出：直接读内存，不排队、不加锁，多少个调用方都立刻返回。
- 否则（``rest_warm_start`` 开着）：从 REST 补这个币最近的逐笔归集成交（``/fapi/v1/aggTrades``）
  和标记价 / 资金费率（``/fapi/v1/premiumIndex``）进特征引擎，再读一次。同一个币同时来几个调用
  方只拉一次（共用一个在途任务），拉过的 20 秒内不再拉；不同的币并发拉，最多
  ``market_rest_concurrency`` 个。
- 所有请求先过同一个 :class:`~jiaoyi.market.rest.WeightBudget`（本程序每分钟最多用本 IP 权重的
  30%，响应头里本 IP 已用过半也先等）和封禁检查（``rest_ban.json``）。被封就不请求，返回手上
  最好的数据并标 ``stale``，调用方照旧等实时行情。
- REST 补不出来的照样等：爆仓历史（强平瀑布反弹的 30 分钟基线）只有实时流有。

``rest_warm_start`` 关着时（默认）：读内存照旧；K 线 / 持仓量 / 拍板人快照补拉照旧直接请求（不进
预算排队），和改之前一样。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Collection, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import Any, Literal, Protocol

from jiaoyi.contracts import FeatureSnapshot, Interval, Kline, MarkPrice, Side, Trade
from jiaoyi.market.features import (
    LIQ_WINDOW_S,
    TRADE_WINDOW_S,
    RollingFeatureEngine,
    trade_warm_s,
)
from jiaoyi.market.rest import (
    IP_WEIGHT_PER_MIN,
    RateLimited,
    WeightBudget,
    check_blocked,
    fetch_json,
    fetch_klines_between,
    fetch_recent_klines,
)

log = logging.getLogger(__name__)

Fetch = Callable[[str, dict[str, Any], int], Awaitable[Any]]
"""(路径, 参数, 权重) → JSON。测试换成假的。"""
Source = Literal["ws", "rest", "stale"]

REST_SHARE = 0.3
REST_WEIGHT_PER_MIN = int(IP_WEIGHT_PER_MIN * REST_SHARE)
"""本程序补行情、回填 K 线、轮询持仓量每分钟最多花的权重；其余留给同一 IP 上的面板、慢环和下单。"""
CACHE_S = 20.0
AGG_LIMIT = 1000
AGG_WEIGHT = 20
MAX_AGG_PAGES = 3
"""一个币最多往回翻几页逐笔成交（每页权重 20）；BTC 3 页通常够 5 分钟，不够 5 分钟的字段接着等。"""
PREMIUM_WEIGHT = 1

TRADE_1M_FIELDS = frozenset(
    {
        "ret_1m",
        "vol_1m_usdt",
        "trades_1m",
        "taker_buy_ratio_1m",
        "cvd_1m_usdt",
        "cvd_30s_usdt",
        "vol_ratio_1m",
        "vol_ratio_10s",
        "taker_buy_ratio_10s",
    }
)
TRADE_5M_FIELDS = frozenset({"ret_5m", "realized_vol_5m"})
LIQ_FIELDS = frozenset(
    {
        "liq_ratio_1m",
        "liq_long_usdt_1m",
        "liq_short_usdt_1m",
        "liq_long_usdt_5m",
        "liq_short_usdt_5m",
    }
)


@dataclass(frozen=True)
class MarketRead:
    """一次取行情的结果。``source``：``ws`` = 实时流新鲜且字段齐；``rest`` = 刚从 REST 补过才齐；
    ``stale`` = 不新鲜或字段不齐（被封、补失败、开关关着、REST 补不出的字段），``reason`` 写原因，
    由调用方决定等还是照用。``age_s``：快照时间到现在几秒，没有快照为 None。"""

    snapshot: FeatureSnapshot | None
    source: Source
    age_s: float | None
    reason: str = ""


class SnapshotProvider(Protocol):
    """管家助手等调用方接的签名：``await provider(symbol, max_age_s=30, need=("ret_5m",))``。"""

    def __call__(
        self, symbol: str, *, max_age_s: float | None = None, need: Collection[str] = ()
    ) -> Awaitable[MarketRead]: ...


def _default_fetch(path: str, params: dict[str, Any], weight: int) -> Awaitable[Any]:
    return fetch_json(path, params, weight=weight)


def parse_agg_trades(symbol: str, rows: list[dict[str, Any]]) -> list[Trade]:
    """``/fapi/v1/aggTrades`` 的行 → ``Trade``（和实时流 aggTrade 同一套换算）。"""
    out: list[Trade] = []
    for r in rows:
        qty = float(r["q"])
        if qty <= 0:
            continue
        out.append(
            Trade(
                symbol=symbol,
                ts=datetime.fromtimestamp(int(r["T"]) / 1000.0, UTC),
                price=float(r["p"]),
                qty=qty,
                # m=True：买方是挂单方，主动方是卖方
                taker_side=Side.SHORT if r["m"] else Side.LONG,
            )
        )
    return out


def parse_premium(symbol: str, d: dict[str, Any]) -> MarkPrice:
    """``/fapi/v1/premiumIndex`` → ``MarkPrice``（同实时流 markPriceUpdate 的字段）。"""
    index = float(d.get("indexPrice") or 0.0)
    nxt = int(d.get("nextFundingTime") or 0)
    return MarkPrice(
        symbol=symbol,
        ts=datetime.fromtimestamp(int(d["time"]) / 1000.0, UTC),
        mark_price=float(d["markPrice"]),
        index_price=index or None,
        funding_rate=float(d["lastFundingRate"]),
        next_funding_at=datetime.fromtimestamp(nxt / 1000.0, UTC) if nxt else None,
    )


class MarketData:
    """见模块说明。``enabled()`` 每次现读（``rest_warm_start`` 热生效）。"""

    def __init__(
        self,
        engine: RollingFeatureEngine,
        *,
        enabled: Callable[[], bool],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        fetch: Fetch = _default_fetch,
        budget: WeightBudget | None = None,
        concurrency: int = 4,
        cache_s: float = CACHE_S,
        max_pages: int = MAX_AGG_PAGES,
        mono: Callable[[], float] = time.monotonic,
    ) -> None:
        self._engine = engine
        self._enabled = enabled
        self._clock = clock
        self._fetch = fetch
        self.budget = budget or WeightBudget(REST_WEIGHT_PER_MIN)
        self._sem = asyncio.Semaphore(max(1, concurrency))
        self._cache_s = cache_s
        self._max_pages = max_pages
        self._mono = mono
        self._inflight: dict[str, asyncio.Task[str | None]] = {}
        self._done: dict[str, tuple[float, str | None]] = {}
        self._ban_logged: float | None = None

    @property
    def on(self) -> bool:
        try:
            return bool(self._enabled())
        except Exception:
            return False

    # ------------------------------------------------------------ 共用的 REST 出口

    async def fetch(self, path: str, params: dict[str, Any], weight: int) -> Any:
        """公开行情 GET。开着时先过共用预算；关着时和直接 ``fetch_json`` 一样（只查封禁）。"""
        if self.on:
            await self.budget.take(weight)
        else:
            check_blocked()
        return await self._fetch(path, params, weight)

    async def klines(
        self, symbol: str, *, limit: int, interval: Interval = Interval.M1
    ) -> list[Kline]:
        budget = self.budget if self.on else None
        return await fetch_recent_klines(symbol, limit=limit, interval=interval, budget=budget)

    async def klines_between(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        *,
        fallback: WeightBudget | None = None,
    ) -> list[Kline]:
        """开着时走共用预算；关着时用调用方原来那份预算 ``fallback``。"""
        budget = self.budget if self.on else fallback
        return await fetch_klines_between(symbol, start, end, budget=budget)

    # ------------------------------------------------------------ 取快照

    async def snapshot(
        self, symbol: str, *, max_age_s: float | None = None, need: Collection[str] = ()
    ) -> MarketRead:
        """见 :class:`MarketRead`。``max_age_s`` None / 0：不看新鲜度。``need`` 空：有快照就行。"""
        snap = self._engine.snapshot(symbol)
        why = self._unusable(snap, max_age_s, need)
        if why is None:
            return self._read(symbol, snap, None)
        if not self.on:
            return MarketRead(snap, "stale", self._age(snap), why)
        failed = await self.seed(symbol)
        snap = self._engine.snapshot(symbol)
        why = self._unusable(snap, max_age_s, need)
        if why is None:
            return self._read(symbol, snap, None)
        return MarketRead(snap, "stale", self._age(snap), failed or why)

    async def refresh(self, symbol: str, max_age_s: float | None = None) -> None:
        """决策前调用：快照旧了 / 还没有就从 REST 补一次（开关关着什么都不做）。"""
        if self.on:
            await self.snapshot(symbol, max_age_s=max_age_s)

    def _read(self, symbol: str, snap: FeatureSnapshot | None, why: str | None) -> MarketRead:
        src: Source = "rest" if self._engine.data_source(symbol) == "rest" else "ws"
        return MarketRead(snap, src, self._age(snap), why or "")

    def _age(self, snap: FeatureSnapshot | None) -> float | None:
        if snap is None:
            return None
        return round((self._clock() - snap.ts).total_seconds(), 3)

    def _unusable(
        self, snap: FeatureSnapshot | None, max_age_s: float | None, need: Collection[str]
    ) -> str | None:
        if snap is None:
            return "还没有行情快照"
        age = self._age(snap) or 0.0
        if max_age_s and age > max_age_s:
            return f"行情已旧 {age:.0f} 秒（上限 {max_age_s:g} 秒）"
        v = snap.values
        trade = trade_warm_s(v) or 0.0
        cold = []
        for name in need:
            if name == "book":
                ok = snap.book is not None
            elif name in LIQ_FIELDS:
                ok = v.get("live_s", 0.0) >= LIQ_WINDOW_S and name in v
            elif name in TRADE_5M_FIELDS:
                ok = trade >= TRADE_WINDOW_S and name in v
            elif name in TRADE_1M_FIELDS:
                ok = trade >= 60.0 and name in v
            else:
                ok = name in v
            if not ok:
                cold.append(name)
        return f"还没预热：{'、'.join(cold)}" if cold else None

    # ------------------------------------------------------------ REST 补

    async def warm(self, symbols: Iterable[str]) -> None:
        """刚订上的币按顺序补（调用方把 BTC、持仓币排前面）；预算和并发数管着节奏。"""
        if not self.on:
            return
        await asyncio.gather(*(self.seed(s) for s in dict.fromkeys(symbols)))

    async def seed(self, symbol: str) -> str | None:
        """补一次；返回没补成的原因（None = 补上了）。20 秒内补过的直接返回上次的结果。"""
        if not self.on:
            return "rest_warm_start 关着，等实时行情"
        done = self._done.get(symbol)
        if done is not None and self._mono() - done[0] < self._cache_s:
            return done[1]
        task = self._inflight.get(symbol)
        if task is None:
            task = asyncio.ensure_future(self._seed(symbol))
            self._inflight[symbol] = task
            task.add_done_callback(partial(self._forget, symbol))
        return await asyncio.shield(task)

    def _forget(self, symbol: str, task: asyncio.Task[str | None]) -> None:
        if self._inflight.get(symbol) is task:
            del self._inflight[symbol]

    async def _seed(self, symbol: str) -> str | None:
        reason: str | None = None
        async with self._sem:
            fetched_at = self._clock().timestamp()
            try:
                trades, covered_from = await self._agg_trades(symbol, fetched_at)
                mark = parse_premium(
                    symbol,
                    await self.fetch("/fapi/v1/premiumIndex", {"symbol": symbol}, PREMIUM_WEIGHT),
                )
            except RateLimited as e:
                reason = f"币安限流中，不从 REST 补，等实时行情（{e}）"
                if self._ban_logged != e.until:
                    self._ban_logged = e.until
                    log.warning("%s %s", symbol, reason)
            except Exception as e:
                reason = f"REST 补行情失败，等实时行情（{e!r}）"
                log.warning("%s %s", symbol, reason)
            else:
                self._engine.seed_rest(
                    symbol, trades, mark, covered_from=covered_from, fetched_at=fetched_at
                )
            self._done[symbol] = (self._mono(), reason)
        return reason

    async def _agg_trades(self, symbol: str, fetched_at: float) -> tuple[list[Trade], float]:
        """最近的逐笔成交，往回翻到 5 分钟前（或接上已有覆盖）为止，最多 ``max_pages`` 页。

        返回 (成交, 从哪一秒起是全的)。"""
        target = fetched_at - TRADE_WINDOW_S - 1.0
        cover = self._engine.trade_cover(symbol)
        if cover is not None and cover[0] <= target:
            target = max(target, cover[1])  # 已有的覆盖够长：只要补上断掉的那段
        rows: list[dict[str, Any]] = []
        from_id: int | None = None
        exhausted = False
        for _ in range(self._max_pages):
            params: dict[str, Any] = {"symbol": symbol, "limit": AGG_LIMIT}
            if from_id is not None:
                params["fromId"] = from_id
            page = await self.fetch("/fapi/v1/aggTrades", params, AGG_WEIGHT)
            if not isinstance(page, list):
                raise ValueError(f"aggTrades 返回的不是列表：{type(page).__name__}")
            if rows:
                first_id = int(rows[0]["a"])
                page = [r for r in page if int(r["a"]) < first_id]
            if not page:
                exhausted = True
                break
            rows = [*page, *rows]
            first_id = int(rows[0]["a"])
            if int(rows[0]["T"]) / 1000.0 <= target:
                break
            if len(page) < AGG_LIMIT or first_id == 0:
                exhausted = True  # 上市以来的成交全在这里了
                break
            from_id = max(first_id - AGG_LIMIT, 0)
        trades = parse_agg_trades(symbol, rows)
        if not rows:
            return trades, target if exhausted else fetched_at
        first_t = int(rows[0]["T"]) / 1000.0
        return trades, min(first_t, target) if exhausted else first_t
