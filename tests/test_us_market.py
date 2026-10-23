from __future__ import annotations

from datetime import UTC, date, datetime

import httpx

from jiaoyi.slow.us_market import EarningsCalendar, EarningsNews, us_holidays, us_session


def test_us_session_hours_weekends_and_holidays() -> None:
    assert (
        us_session(datetime(2026, 10, 23, 14, 0, tzinfo=UTC)) == "open"
    )  # 周五 10:00 美东（夏令时）
    assert us_session(datetime(2026, 10, 23, 12, 0, tzinfo=UTC)) == "extended"  # 8:00 盘前
    assert us_session(datetime(2026, 10, 23, 3, 0, tzinfo=UTC)) == "closed"  # 夜里
    assert us_session(datetime(2026, 10, 24, 15, 0, tzinfo=UTC)) == "closed"  # 周六
    assert us_session(datetime(2026, 11, 26, 15, 0, tzinfo=UTC)) == "closed"  # 感恩节
    assert us_session(datetime(2026, 12, 15, 15, 0, tzinfo=UTC)) == "open"  # 冬令时 10:00
    assert us_session(datetime(2026, 12, 15, 14, 0, tzinfo=UTC)) == "extended"  # 冬令时 9:00


def test_us_holidays_2026() -> None:
    assert us_holidays(2026) == {
        date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
        date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
        date(2026, 11, 26), date(2026, 12, 25),
    }  # fmt: skip
    assert date(2027, 12, 24) in us_holidays(2027)  # 圣诞节周六 → 前一个周五
    assert date(2028, 1, 1) not in us_holidays(2028)  # 元旦周六：不顺延到前一年


def _calendar(rows_by_day: dict[str, list[dict[str, str]]], fail: bool = False) -> EarningsCalendar:
    def handler(req: httpx.Request) -> httpx.Response:
        if fail:
            return httpx.Response(500)
        day = req.url.params["date"]
        return httpx.Response(200, json={"data": {"rows": rows_by_day.get(day, [])}})

    return EarningsCalendar(httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_earnings_calendar_notes_and_news() -> None:
    now = datetime(2026, 10, 23, 14, 0, tzinfo=UTC)
    cal = _calendar(
        {
            "2026-10-23": [{"symbol": "TSLA", "time": "time-after-hours"}],
            "2026-10-26": [{"symbol": "COIN", "time": "time-pre-market"}, {"symbol": ""}],
            "2026-11-30": [{"symbol": "FAR", "time": ""}],
        }
    )
    await cal.refresh(now)
    assert cal.note("TSLA", now) == "财报：10-23 盘后（今天）"
    assert cal.note("COIN", now) == "财报：10-26 盘前（3 天后）"
    assert cal.note("NVDA", now) is None and cal.note("FAR", now) is None
    news = await EarningsNews(cal, lambda: {"TSLA", "NVDA"}, lambda: now).fetch()
    assert [n.title for n in news] == ["($TSLA) 财报：10-23 盘后（今天）"]
    assert news[0].key == "earnings:TSLA-2026-10-23"


async def test_earnings_calendar_failure_keeps_old_cache() -> None:
    now = datetime(2026, 10, 23, 14, 0, tzinfo=UTC)
    cal = _calendar({"2026-10-23": [{"symbol": "TSLA"}]})
    await cal.refresh(now)
    bad = _calendar({}, fail=True)
    bad._events, bad._fetched_at = dict(cal._events), cal._fetched_at
    await bad.refresh(datetime(2026, 10, 23, 23, 0, tzinfo=UTC))  # 缓存过期，拉失败
    assert bad.near("TSLA", now) is not None  # 沿用旧缓存，不抛
