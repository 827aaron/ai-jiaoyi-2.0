"""美股开盘时段和财报日历（只给美股相关的类别用：crypto_stock / us_tech）。

- ``us_session``：纯代码判断美股现在是常规盘（开盘中）、盘前盘后还是休市。常规盘 9:30–16:00
  美东时间，周末和 NYSE 全天休市日（元旦、马丁·路德·金日、总统日、耶稣受难日、阵亡将士纪念日、
  六月节、独立日、劳动节、感恩节、圣诞节，周六顺延到前一个周五、周日顺延到后一个周一）都算休市。
  提前收盘的半天交易日（感恩节次日、平安夜、7 月 3 日）按全天算，差几个小时，不影响用途。
- ``EarningsCalendar``：Nasdaq 免费财报日历接口（无需 key），按日期拉，缓存 ``ttl``。
  拉不到就沿用旧缓存、状态变化时记一条日志，不影响选币和开会。
- ``EarningsNews``：把关注的美股代码近期的财报日做成背景新闻条目，让军师团的新闻里带上它（只当背景，
  不触发开会）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

import httpx

from jiaoyi.slow.news import NewsItem, NewsKind

log = logging.getLogger(__name__)

NY = ZoneInfo("America/New_York")
Session = Literal["open", "extended", "closed"]
EARNINGS_URL = "https://api.nasdaq.com/api/calendar/earnings"
_WHEN_ZH = {"pre": "盘前", "after": "盘后", "unknown": "时间未定"}


def _when(raw: str) -> Literal["pre", "after", "unknown"]:
    return (
        "pre" if raw == "time-pre-market" else "after" if raw == "time-after-hours" else "unknown"
    )


def _easter(year: int) -> date:
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    g = (8 * b + 13) // 25
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    el = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 19 * el) // 433
    month = (h + el - 7 * m + 114) // 31
    return date(year, month, (h + el - 7 * m + 114) % 31 + 1)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """第 n 个星期几（n=-1 为最后一个）。"""
    if n > 0:
        first = date(year, month, 1)
        return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    last = nxt - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed(d: date) -> date | None:
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def us_holidays(year: int) -> frozenset[date]:
    """NYSE 全天休市日。"""
    days = {
        _nth_weekday(year, 1, 0, 3),  # 马丁·路德·金日
        _nth_weekday(year, 2, 0, 3),  # 总统日
        _easter(year) - timedelta(days=2),  # 耶稣受难日
        _nth_weekday(year, 5, 0, -1),  # 阵亡将士纪念日
        _nth_weekday(year, 9, 0, 1),  # 劳动节
        _nth_weekday(year, 11, 3, 4),  # 感恩节
    }
    for month, day in ((6, 19), (7, 4), (12, 25)):
        obs = _observed(date(year, month, day))
        if obs is not None:
            days.add(obs)
    new_year = date(year, 1, 1)
    if new_year.weekday() != 5:  # 元旦落在周六：不顺延到前一年的周五
        obs = _observed(new_year)
        if obs is not None:
            days.add(obs)
    return frozenset(days)


def us_session(now: datetime) -> Session:
    """美股现在的时段：open 常规盘 / extended 盘前盘后（4:00–20:00 美东）/ closed 休市。"""
    t = now.astimezone(NY)
    if t.weekday() >= 5 or t.date() in us_holidays(t.year):
        return "closed"
    clock = t.time()
    if time(9, 30) <= clock < time(16, 0):
        return "open"
    if time(4, 0) <= clock < time(20, 0):
        return "extended"
    return "closed"


def us_entry_window(now: datetime, open_delay_min: int = 15, close_lead_min: int = 15) -> bool:
    """美股 / 加密股合约现在能不能开新仓：常规盘开盘后 ``open_delay_min`` 分钟到收盘前
    ``close_lead_min`` 分钟之间。按美东时间算（自动跟夏令时），周末和 NYSE 休市日都不行。"""
    t = now.astimezone(NY)
    if t.weekday() >= 5 or t.date() in us_holidays(t.year):
        return False
    start = datetime.combine(t.date(), time(9, 30), NY) + timedelta(minutes=open_delay_min)
    end = datetime.combine(t.date(), time(16, 0), NY) - timedelta(minutes=close_lead_min)
    return start <= t < end


@dataclass(frozen=True)
class EarningsEvent:
    ticker: str
    day: date
    when: Literal["pre", "after", "unknown"]

    def note(self, today: date) -> str:
        delta = (self.day - today).days
        rel = {0: "今天", 1: "明天", -1: "昨天"}.get(delta, f"{delta} 天后" if delta > 0 else "")
        return f"财报：{self.day:%m-%d} {_WHEN_ZH[self.when]}（{rel}）"


class EarningsCalendar:
    """Nasdaq 财报日历的缓存。``near`` / ``note`` 同步读缓存，``refresh`` 才碰网络。"""

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        days_ahead: int = 3,
        ttl: timedelta = timedelta(hours=6),
        url: str = EARNINGS_URL,
    ) -> None:
        self._c = client or httpx.AsyncClient(
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
        )
        self._days_ahead = days_ahead
        self._ttl = ttl
        self._url = url
        self._events: dict[str, EarningsEvent] = {}
        self._fetched_at: datetime | None = None
        self._down = False

    async def refresh(self, now: datetime) -> None:
        """缓存没过期就不拉。每个日期一次请求；全部失败才算失败（沿用旧缓存，状态变化时记日志）。"""
        if self._fetched_at is not None and now - self._fetched_at < self._ttl:
            return
        today = now.astimezone(NY).date()
        found: dict[str, EarningsEvent] = {}
        errors: list[str] = []
        days = [today + timedelta(days=i) for i in range(-1, self._days_ahead + 1)]
        for day in days:
            try:
                for ev in await self._one(day):
                    old = found.get(ev.ticker)
                    if old is None or abs((ev.day - today).days) < abs((old.day - today).days):
                        found[ev.ticker] = ev
            except Exception as exc:
                errors.append(repr(exc))
        if len(errors) == len(days):
            if not self._down:
                log.warning("财报日历拉取失败，沿用旧缓存：%s", errors[0])
            self._down = True
            return
        if self._down:
            log.info("财报日历恢复")
        self._down = False
        self._events, self._fetched_at = found, now

    async def _one(self, day: date) -> list[EarningsEvent]:
        r = await self._c.get(self._url, params={"date": day.isoformat()})
        r.raise_for_status()
        body: Any = r.json()
        rows = (body.get("data") or {}).get("rows") or []
        return [
            EarningsEvent(str(row["symbol"]).upper(), day, _when(str(row.get("time", ""))))
            for row in rows
            if row.get("symbol")
        ]

    def near(self, ticker: str, now: datetime) -> EarningsEvent | None:
        """这个代码在昨天到 ``days_ahead`` 天后之间有财报吗。"""
        ev = self._events.get(ticker.upper())
        if ev is None:
            return None
        delta = (ev.day - now.astimezone(NY).date()).days
        return ev if -1 <= delta <= self._days_ahead else None

    def note(self, ticker: str, now: datetime) -> str | None:
        ev = self.near(ticker, now)
        return ev.note(now.astimezone(NY).date()) if ev is not None else None

    async def aclose(self) -> None:
        await self._c.aclose()


class EarningsNews:
    """满足 ``news.NewsSource``：关注的美股代码近期有财报就出一条背景新闻。"""

    name = "earnings"

    def __init__(
        self,
        calendar: EarningsCalendar,
        tickers: Callable[[], Iterable[str]],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._cal = calendar
        self._tickers = tickers
        self._clock = clock

    async def fetch(self) -> list[NewsItem]:
        now = self._clock()
        await self._cal.refresh(now)
        out: list[NewsItem] = []
        for t in sorted(set(self._tickers())):
            ev = self._cal.near(t, now)
            if ev is None:
                continue
            out.append(
                NewsItem(
                    source=self.name,
                    item_id=f"{ev.ticker}-{ev.day.isoformat()}",
                    title=f"(${ev.ticker}) {ev.note(now.astimezone(NY).date())}",
                    published_at=now,
                    kind=NewsKind.MEDIA,
                    summary="财报日前后波动通常放大，开仓前留意",
                )
            )
        return out
