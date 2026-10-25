"""美股交易时段（纯代码，无网络依赖；风控员和慢环共用）。

- ``us_session``：现在是常规盘（open）、盘前盘后（extended）还是休市（closed）。常规盘 9:30–16:00
  美东时间，周末和 NYSE 全天休市日（元旦、马丁·路德·金日、总统日、耶稣受难日、阵亡将士纪念日、
  六月节、独立日、劳动节、感恩节、圣诞节，周六顺延到前一个周五、周日顺延到后一个周一）都算休市。
  提前收盘的半天交易日按全天算，差几个小时，不影响用途。
- ``us_entry_window``：美股 / 加密股合约现在能不能开新仓（开盘后 N 分钟到收盘前 M 分钟）。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
Session = Literal["open", "extended", "closed"]


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
