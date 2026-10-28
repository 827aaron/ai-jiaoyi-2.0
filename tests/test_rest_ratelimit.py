"""币安限流：418 / 429 后整段停请求；离线工具按权重预算排队；结果 K 线先用日包缓存。"""

from __future__ import annotations

import asyncio
import email.message
import io
import json
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
    monkeypatch.setattr(rest, "_own", None)
    monkeypatch.setattr(rest, "_used_weight", None)


def _http_error(code: int, retry_after: str | None, body: bytes = b"") -> urllib.error.HTTPError:
    h = email.message.Message()
    if retry_after is not None:
        h["Retry-After"] = retry_after
    return urllib.error.HTTPError("u", code, "teapot", h, io.BytesIO(body))


BAN_BODY = (
    b'{"code":-1003,"msg":"Way too many requests; IP(203.0.113.7) banned until 1761660000000. '
    b'Please use the websocket for live updates to avoid bans."}'
)


def test_parse_banned_until_reads_the_1003_message() -> None:
    assert rest.parse_banned_until(BAN_BODY) == 1761660000.0
    assert rest.parse_banned_until(BAN_BODY.decode()) == 1761660000.0
    assert rest.parse_banned_until('{"code":-1003,"msg":"Too many requests."}') is None
    assert rest.parse_banned_until(None) is None


def test_1003_ban_until_outlasts_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    until_ms = int((time.time() + 3 * 3600) * 1000)
    body = BAN_BODY.replace(b"1761660000000", str(until_ms).encode())

    def urlopen(url: str, timeout: float) -> Any:
        raise _http_error(418, "120", body)

    monkeypatch.setattr(rest.urllib.request, "urlopen", urlopen)
    with pytest.raises(rest.RateLimited) as e:
        rest._get("https://x", 1.0)
    assert e.value.until == until_ms / 1000 and e.value.wait_min() >= 179


def test_shared_ban_is_honoured_by_another_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rest.use_shared_ban(tmp_path)
    rest.mark_blocked(418, {"Retry-After": "600"})
    saved = json.loads((tmp_path / rest.BAN_FILE).read_text(encoding="utf-8"))
    assert saved["status"] == 418 and saved["until"] > time.time() + 590
    assert not list(tmp_path.glob(".*.tmp"))

    monkeypatch.setattr(rest, "_own", None)  # a fresh process: nothing in memory
    monkeypatch.setattr(rest, "_own", None)
    rest.use_shared_ban(tmp_path)
    monkeypatch.setattr(
        rest.urllib.request, "urlopen", lambda *a, **k: pytest.fail("封禁期内不该发请求")
    )
    with pytest.raises(rest.RateLimited) as e:
        rest._get("https://x", 1.0)
    assert e.value.refused and e.value.status == 418
    assert str(e.value).startswith("币安限流中，至 ") and str(e.value).endswith(" 解除，本次不请求")
    assert rest.blocked() is not None  # adopted, so the CLI still reports it at the end


def test_shared_ban_expires(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    rest.use_shared_ban(tmp_path)
    rest.mark_blocked(429, {"Retry-After": "30"})
    monkeypatch.setattr(rest, "_own", None)
    with pytest.raises(rest.RateLimited):
        rest.check_blocked()
    monkeypatch.setattr(rest, "_own", None)
    rest.check_blocked(now=time.time() + 31)
    assert rest.active_ban(now=time.time() + 31) is None


def test_shorter_ban_does_not_shorten_the_shared_one(tmp_path: Path) -> None:
    other = tmp_path / rest.BAN_FILE
    until = time.time() + 3600
    other.write_text(json.dumps({"status": 418, "until": until}), encoding="utf-8")
    rest.use_shared_ban(tmp_path)
    rest.mark_blocked(429, {"Retry-After": "60"})
    assert json.loads(other.read_text(encoding="utf-8"))["until"] == until
    ban = rest.active_ban()
    assert ban is not None and ban.until == until


def test_corrupt_ban_file_does_not_block(tmp_path: Path) -> None:
    (tmp_path / rest.BAN_FILE).write_text("{not json", encoding="utf-8")
    rest.use_shared_ban(tmp_path)
    rest.check_blocked()


def test_without_shared_dir_nothing_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    rest.use_shared_ban(None)
    rest.mark_blocked(418, {"Retry-After": "60"})
    assert not list(tmp_path.rglob(rest.BAN_FILE))


def test_save_ban_keeps_a_longer_ban_another_process_wrote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / rest.BAN_FILE
    path.write_text(json.dumps({"status": 418, "until": 9_000.0}), encoding="utf-8")
    rest.save_ban(tmp_path, now=1_000.0)  # nothing live in this process
    monkeypatch.setattr(rest, "_own", rest.RateLimited(429, 1_100.0))
    rest.save_ban(tmp_path, now=1_000.0)
    assert json.loads(path.read_text(encoding="utf-8"))["until"] == 9_000.0


def _write_ban(path: Path, until: float, status: int = 418) -> None:
    path.write_text(json.dumps({"status": status, "until": until}), encoding="utf-8")


def test_deleting_the_ban_file_lifts_an_adopted_ban(tmp_path: Path) -> None:
    path = tmp_path / rest.BAN_FILE
    _write_ban(path, time.time() + 600)
    rest.use_shared_ban(tmp_path)
    with pytest.raises(rest.RateLimited):
        rest.check_blocked()
    assert rest.load_ban(tmp_path) is not None
    path.unlink()  # the operator clears a bad ban
    rest.check_blocked()
    assert rest.active_ban() is None
    rest.save_ban(tmp_path)
    assert not path.exists()


def test_ban_end_beyond_three_days_or_infinite_is_ignored_with_one_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / rest.BAN_FILE
    rest.use_shared_ban(tmp_path)
    for until in (time.time() + 10 * 86400, float("inf")):
        caplog.clear()
        _write_ban(path, until)
        rest.check_blocked()
        rest.check_blocked()
        assert rest.load_ban(tmp_path) is None
        assert len([r for r in caplog.records if "不合理" in r.getMessage()]) == 1


def test_rate_limited_survives_absurd_end_times() -> None:
    for until in (float("inf"), 1e300, float("nan")):
        assert rest.RateLimited(418, until).wait_min() >= 1
        assert "本次不请求" in str(rest.RateLimited(418, until, refused=True))


def test_absurd_retry_after_and_banned_until_fall_back(tmp_path: Path) -> None:
    rest.use_shared_ban(tmp_path)
    far_ms = int((time.time() + 10 * 86400) * 1000)
    ban = rest.mark_blocked(418, {"Retry-After": "1e12"}, f"banned until {far_ms}.")
    assert ban.until < time.time() + 121
    saved = json.loads((tmp_path / rest.BAN_FILE).read_text(encoding="utf-8"))
    assert saved["until"] == ban.until


def test_corrupt_file_is_removed_once_with_one_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / rest.BAN_FILE
    path.write_text("{not json", encoding="utf-8")
    rest.use_shared_ban(tmp_path)
    rest.check_blocked()
    rest.save_ban(tmp_path)
    rest.save_ban(tmp_path)
    assert not path.exists()
    assert len([r for r in caplog.records if "读不了" in r.getMessage()]) == 1


def test_corrupt_file_is_replaced_by_this_process_own_ban(tmp_path: Path) -> None:
    path = tmp_path / rest.BAN_FILE
    path.write_text("garbage", encoding="utf-8")
    rest.use_shared_ban(tmp_path)
    ban = rest.mark_blocked(429, {"Retry-After": "60"})
    assert json.loads(path.read_text(encoding="utf-8"))["until"] == ban.until


def test_save_ban_does_not_delete_a_file_replaced_meanwhile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / rest.BAN_FILE
    _write_ban(path, 500.0)
    real_read = rest._read
    fresh = 1_000.0 + 600

    def read_then_other_process_writes(p: Path, now: float) -> Any:
        got = real_read(p, now)
        _write_ban(path, fresh)
        path.touch()
        return got

    monkeypatch.setattr(rest, "_read", read_then_other_process_writes)
    rest.save_ban(tmp_path, now=1_000.0)
    assert json.loads(path.read_text(encoding="utf-8"))["until"] == fresh


def test_missing_data_dir_is_not_created(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    gone = tmp_path / "no-such-dir"
    rest.use_shared_ban(gone)
    rest.mark_blocked(418, {"Retry-After": "60"})
    rest.save_ban(gone)
    assert not gone.exists()
    assert len([r for r in caplog.records if "不存在" in r.getMessage()]) == 1


def test_demo_budget_ignores_the_mainnet_ban(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rest, "_own", rest.RateLimited(418, time.time() + 600))
    asyncio.run(rest.WeightBudget(100, check_ban=False).take(1))
    with pytest.raises(rest.RateLimited):
        asyncio.run(rest.WeightBudget(100).take(1))


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
