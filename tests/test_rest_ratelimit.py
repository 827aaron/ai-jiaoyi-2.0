"""币安限流：418 / 429 后整段停请求；离线工具按权重预算排队；结果 K 线先用日包缓存。"""

from __future__ import annotations

import asyncio
import email.message
import io
import time
import urllib.error
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from jiaoyi.contracts import Interval, Kline
from jiaoyi.gate import outcomes
from jiaoyi.gate.bars import OutcomeBars
from jiaoyi.market import rest


@pytest.fixture(autouse=True)
def _clear(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rest, "_blocked", None)
    monkeypatch.setattr(rest, "_used_weight", None)


def _http_error(code: int, retry_after: str | None) -> urllib.error.HTTPError:
    h = email.message.Message()
    if retry_after is not None:
        h["Retry-After"] = retry_after
    return urllib.error.HTTPError("u", code, "teapot", h, io.BytesIO(b""))


def test_418_blocks_every_later_request(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def urlopen(url: str, timeout: float) -> Any:
        calls.append(url)
        raise _http_error(418, "300")

    monkeypatch.setattr(rest.urllib.request, "urlopen", urlopen)
    with pytest.raises(rest.RateLimited) as e:
        rest._get("https://x/fapi/v1/klines?a=1", 1.0)
    assert e.value.status == 418 and e.value.wait_min() == 5
    with pytest.raises(rest.RateLimited):  # 限流期内不再敲币安
        rest._get("https://x/fapi/v1/klines?a=2", 1.0)
    assert len(calls) == 1 and rest.blocked() is e.value
    rest.check_blocked(now=time.time() + 301)  # 到期就放行


def test_429_without_header_waits_a_minute(monkeypatch: pytest.MonkeyPatch) -> None:
    def urlopen(url: str, timeout: float) -> Any:
        raise _http_error(429, None)

    monkeypatch.setattr(rest.urllib.request, "urlopen", urlopen)
    with pytest.raises(rest.RateLimited) as e:
        rest._get("https://x", 1.0)
    assert e.value.wait_min() == 1


def test_budget_queues_past_per_minute_weight() -> None:
    t = [0.0]
    slept: list[float] = []

    async def sleep(s: float) -> None:
        slept.append(s)
        t[0] += s

    budget = rest.WeightBudget(10, clock=lambda: t[0], sleep=sleep)

    async def go() -> None:
        for _ in range(3):
            await budget.take(5)

    asyncio.run(go())
    assert len(slept) == 1 and 59.9 < slept[0] < 60.1  # 第三笔等第一笔滚出 60 秒窗口


def test_budget_backs_off_when_ip_is_busy(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []

    async def sleep(s: float) -> None:
        slept.append(s)

    monkeypatch.setattr(rest, "_used_weight", 1500)  # 同机交易程序已用了一大半
    asyncio.run(rest.WeightBudget(100, sleep=sleep).take(2))
    assert len(slept) == 1 and rest._used_weight is None


def test_klines_limit_sized_to_window(monkeypatch: pytest.MonkeyPatch) -> None:
    limits: list[int] = []

    def fake_get(url: str, timeout_s: float) -> list[list[Any]]:
        limits.append(int(dict(p.split("=") for p in url.split("?")[1].split("&"))["limit"]))
        return []

    monkeypatch.setattr(rest, "_get", fake_get)
    start = datetime(2026, 10, 20, tzinfo=UTC)
    asyncio.run(rest.fetch_klines_between("BTCUSDT", start, start + timedelta(hours=4)))
    assert limits == [242] and rest.klines_weight(242) == 2


def _bars(symbol: str, day: date) -> list[Kline]:
    t0 = datetime(day.year, day.month, day.day, tzinfo=UTC)
    return [
        Kline(
            symbol=symbol,
            interval=Interval.M1,
            open_time=t0 + timedelta(minutes=i),
            close_time=t0 + timedelta(minutes=i + 1) - timedelta(milliseconds=1),
            open=1.0,
            high=1.0,
            low=1.0,
            close=1.0,
            volume=1.0,
            quote_volume=1.0,
        )
        for i in range(1440)
    ]


def test_outcome_bars_prefers_daily_files() -> None:
    loaded: list[date] = []
    rest_calls: list[tuple[datetime, datetime]] = []

    def load(symbol: str, day: date, cache: Path) -> list[Kline]:
        loaded.append(day)
        if day >= date(2026, 10, 23):
            raise OSError("404 还没出")
        return _bars(symbol, day)

    async def fetch(sym: str, a: datetime, b: datetime, budget: rest.WeightBudget) -> list[Kline]:
        rest_calls.append((a, b))
        return []

    bars = OutcomeBars(now=lambda: datetime(2026, 10, 24, 1, tzinfo=UTC), load=load, rest=fetch)
    a = datetime(2026, 10, 21, 22, tzinfo=UTC)
    got = asyncio.run(bars("SOLUSDT", a, a + timedelta(hours=4)))  # 跨两天，都有日包
    assert len(got) == 241 and got[0].open_time == a and not rest_calls
    asyncio.run(bars("SOLUSDT", a, a + timedelta(hours=1)))  # 同一天再要：不重读
    assert loaded == [date(2026, 10, 21), date(2026, 10, 22)]
    b = datetime(2026, 10, 23, 1, tzinfo=UTC)
    asyncio.run(bars("SOLUSDT", b, b + timedelta(hours=4)))  # 日包没出 → 走 API
    c = datetime(2026, 10, 24, 0, tzinfo=UTC)
    asyncio.run(bars("SOLUSDT", c, c + timedelta(minutes=30)))  # 今天 → 走 API
    assert len(rest_calls) == 2 and (bars.from_cache, bars.from_rest) == (2, 2)


def test_backfill_stops_quietly_when_rate_limited(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    c = [
        outcomes.Candidate(
            signal_id=f"s{i}",
            symbol="SOLUSDT",
            side="long",
            ts=datetime(2026, 10, 20, tzinfo=UTC),
            entry=100.0,
            hold_min=60,
            template="q3",
            verdict="opened",
            gate_would_block=False,
            stop_bps=80.0,
            tp_bps=120.0,
            stop_src="guard",
            p_take=0.5,
            p_real=0.5,
            p_toxic=0.5,
        )
        for i in range(3)
    ]
    monkeypatch.setattr(outcomes, "candidates", lambda *a: c)

    async def fetch(sym: str, a: datetime, b: datetime) -> list[Kline]:
        raise rest.RateLimited(418, time.time() + 120)

    n, failed = asyncio.run(
        outcomes.backfill(
            tmp_path,
            since=datetime(2026, 10, 19, tzinfo=UTC),
            now=datetime(2026, 10, 24, tzinfo=UTC),
            default_hold_min=60,
            fee_bps=4.0,
            fetch=fetch,
        )
    )
    assert (n, failed) == (0, 3) and "拉行情失败" not in caplog.text
