"""复盘 / 回放算结果用的 1 分钟 K 线：能用历史日包就不打币安 API。

- 已经过去的整天（UTC）用 data.binance.vision 的日包：下载过就直接读本机缓存，不占币安 API 权重；
- 窗口碰到今天、或日包还没出（一般第二天才有），这一段才走 ``/fapi/v1/klines``，受
  :class:`~jiaoyi.market.rest.WeightBudget` 管；被限流后直接抛
  :class:`~jiaoyi.market.rest.RateLimited`，不再去敲。
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from jiaoyi.backtest.data import BASE_URL, DEFAULT_CACHE, download, load_klines
from jiaoyi.contracts import Interval, Kline
from jiaoyi.market.rest import WeightBudget, fetch_klines_between

DayLoader = Callable[[str, date, Path], list[Kline]]
RestFetch = Callable[[str, datetime, datetime, WeightBudget], Awaitable[list[Kline]]]
KEEP_DAYS = 32
"""内存里最多留几个（币, 天）的日包，够一次回放里同一个币前后几笔复用。"""


def load_day(symbol: str, day: date, cache: Path) -> list[Kline]:
    name = f"{symbol}-1m-{day.isoformat()}.zip"
    path = download(f"{BASE_URL}/klines/{symbol}/1m/{name}", cache / symbol / name)
    return load_klines(path, symbol, Interval.M1)


async def _rest(symbol: str, start: datetime, end: datetime, budget: WeightBudget) -> list[Kline]:
    return await fetch_klines_between(symbol, start, end, budget=budget)


class OutcomeBars:
    """``KlineRange``：``await bars(symbol, start, end)`` → 开盘在 [start, end] 的已收盘 K 线。"""

    def __init__(
        self,
        *,
        cache: Path = DEFAULT_CACHE,
        budget: WeightBudget | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        load: DayLoader = load_day,
        rest: RestFetch = _rest,
    ) -> None:
        self._cache = cache
        self.budget = budget or WeightBudget()
        self._now = now
        self._load = load
        self._rest = rest
        self._days: OrderedDict[tuple[str, date], list[Kline] | None] = OrderedDict()
        self.from_cache = 0
        self.from_rest = 0

    async def _day(self, symbol: str, day: date) -> list[Kline] | None:
        key = (symbol, day)
        if key in self._days:
            self._days.move_to_end(key)
            return self._days[key]
        try:
            bars: list[Kline] | None = await asyncio.to_thread(self._load, symbol, day, self._cache)
        except OSError:  # 日包还没出 / 下载失败：这段走 API
            bars = None
        self._days[key] = bars
        if len(self._days) > KEEP_DAYS:
            self._days.popitem(last=False)
        return bars

    async def __call__(self, symbol: str, start: datetime, end: datetime) -> list[Kline]:
        today = self._now().astimezone(UTC).date()
        first, last = start.astimezone(UTC).date(), end.astimezone(UTC).date()
        days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
        if all(d < today for d in days):
            got: list[Kline] = []
            for d in days:
                bars = await self._day(symbol, d)
                if bars is None:
                    break
                got.extend(bars)
            else:
                self.from_cache += 1
                return [k for k in got if start <= k.open_time <= end]
        self.from_rest += 1
        return await self._rest(symbol, start, end, self.budget)
