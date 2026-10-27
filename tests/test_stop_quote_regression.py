"""独立盘口旁路验收：保留插针与采样缺口，不替执行器判断。"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from jiaoyi import app as app_module
from jiaoyi.core import stop_quotes
from jiaoyi.core.stop_quotes import StopQuoteRecorder
from tests.test_app import _app

NOW = datetime(2026, 10, 27, 16, 0, tzinfo=UTC)


def book(bid: str, ask: str, *, symbol: str = "BTCUSDT", **extra: Any) -> str:
    return json.dumps({"e": "bookTicker", "s": symbol, "b": bid, "a": ask, **extra})


def test_raw_spike_between_checks_is_retained_without_claiming_executor_received_it(
    tmp_path: Path,
) -> None:
    rows: list[dict[str, Any]] = []
    clock = [NOW]
    recorder = StopQuoteRecorder(tmp_path, "demo", writer=rows.append, wall=lambda: clock[0])
    try:
        recorder.update_symbols({"BTCUSDT"})
        recorder.capture(book("100", "100.1", E=int(NOW.timestamp() * 1000)), epoch="c1")
        clock[0] += timedelta(milliseconds=100)
        recorder.capture(book("110", "110.1", E=int(clock[0].timestamp() * 1000)), epoch="c1")
        clock[0] += timedelta(milliseconds=100)
        recorder.capture(book("100", "100.1", E=int(clock[0].timestamp() * 1000)), epoch="c1")
        recorder._queue.join()
        quotes = [r for r in rows if r["kind"] == "book"]
        assert [r["bid"] for r in quotes] == [100, 110, 100]
        assert len({r["seq"] for r in quotes}) == 3
        assert all(r["source_env"] == "demo" and r["connection_epoch"] == "c1" for r in quotes)
        assert all(
            r["coverage_complete"] is False and r["participates_in_decision"] is False
            for r in quotes
        )
        assert all(r["source"] == "independent_exchange_stream" for r in quotes)
    finally:
        recorder.close()


def test_missing_exchange_clock_remains_null_and_nonheld_symbol_is_filtered(tmp_path: Path) -> None:
    rows: list[dict[str, Any]] = []
    recorder = StopQuoteRecorder(tmp_path, "demo", writer=rows.append, wall=lambda: NOW)
    try:
        recorder.update_symbols({"BTCUSDT"})
        recorder.capture(book("100", "101"))
        recorder.capture(book("100", "101", symbol="ETHUSDT"))
        recorder._queue.join()
        [quote] = [r for r in rows if r["kind"] == "book"]
        assert quote["event_at"] is None and quote["transaction_at"] is None
        assert quote["received_at"] == NOW.isoformat()
        assert quote["symbol"] == "BTCUSDT"
    finally:
        recorder.close()


@pytest.mark.parametrize("environment", ["paper", "legacy_testnet", "", "DEMO"])
def test_unknown_environment_cannot_fall_back_to_live(tmp_path: Path, environment: str) -> None:
    with pytest.raises(ValueError):
        StopQuoteRecorder(tmp_path, environment)


def test_full_queue_drops_observations_without_blocking_capture_and_reports_gap(
    tmp_path: Path,
) -> None:
    started, release = threading.Event(), threading.Event()
    rows: list[dict[str, Any]] = []

    def slow(record: dict[str, Any]) -> None:
        started.set()
        assert release.wait(2)
        rows.append(record)

    recorder = StopQuoteRecorder(tmp_path, "demo", max_queue=1, writer=slow)
    try:
        recorder.update_symbols({"BTCUSDT"})
        assert started.wait(1)
        recorder.capture(book("100", "101"))
        recorder.capture(book("110", "111"))  # 磁盘线程被阻塞，但入队路径立即返回。
        assert recorder.stats["dropped"] == 1 and recorder.stats["queued"] == 1
        release.set()
        recorder._queue.join()
        recorder.capture(book("101", "102"))
        recorder._queue.join()
        last = [r for r in rows if r["kind"] == "book"][-1]
        assert last["dropped"] == 1 and last["coverage_complete"] is False
        assert last["seq"] == 4  # 丢样也占序号，不能假装连续。
    finally:
        release.set()
        recorder.close()


def test_writer_failure_is_counted_without_stopping_subsequent_capture(tmp_path: Path) -> None:
    rows: list[dict[str, Any]] = []
    calls = 0

    def sometimes_fails(record: dict[str, Any]) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("synthetic disk failure")
        rows.append(record)

    recorder = StopQuoteRecorder(tmp_path, "demo", writer=sometimes_fails)
    try:
        recorder.update_symbols({"BTCUSDT"})
        recorder._queue.join()
        recorder.capture(book("100", "101"))
        recorder._queue.join()
        assert recorder.stats["write_errors"] == 1
        assert rows[-1]["write_errors"] == 1 and rows[-1]["kind"] == "book"
    finally:
        recorder.close()


async def test_reconnect_fallback_stays_in_the_selected_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows: list[dict[str, Any]] = []
    recorder = StopQuoteRecorder(tmp_path, "demo", writer=rows.append)
    urls: list[str] = []

    class FailingConnection:
        async def __aenter__(self) -> Any:
            raise OSError("synthetic disconnected")

        async def __aexit__(self, *args: Any) -> None:
            return None

    def connect(url: str, **kwargs: Any) -> FailingConnection:
        urls.append(url)
        if len(urls) == 2:
            recorder._closed = True
        return FailingConnection()

    async def no_delay(seconds: float) -> None:
        return None

    monkeypatch.setattr(stop_quotes, "connect", connect)
    monkeypatch.setattr(stop_quotes.asyncio, "sleep", no_delay)
    try:
        await recorder._connection("public", frozenset({"BTCUSDT"}))
        recorder._queue.join()
        assert len(urls) == 2
        assert all(url.startswith(stop_quotes.BASES["demo"] + "/") for url in urls)
        assert "/public/stream?" in urls[0] and "/stream?" in urls[1]
        assert any(r["kind"] == "connection_gap" for r in rows)
    finally:
        recorder._closed = False
        recorder.close()


async def test_irrelevant_messages_do_not_hide_a_silent_price_stream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows: list[dict[str, Any]] = []
    recorder = StopQuoteRecorder(tmp_path, "demo", writer=rows.append)
    monotonic = [0.0]

    class IrrelevantMessages:
        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def recv(self) -> str:
            monotonic[0] = 16.0
            return '{"result": null}'

    async def stop_after_first_gap(seconds: float) -> None:
        recorder._closed = True

    monkeypatch.setattr(stop_quotes, "connect", lambda *a, **kw: IrrelevantMessages())
    monkeypatch.setattr(
        stop_quotes,
        "time",
        SimpleNamespace(
            monotonic=lambda: monotonic[0],
            monotonic_ns=lambda: int(monotonic[0] * 1e9),
        ),
    )
    monkeypatch.setattr(stop_quotes.asyncio, "sleep", stop_after_first_gap)
    try:
        await recorder._connection("public", frozenset({"BTCUSDT"}))
        recorder._queue.join()
        assert any(
            r["kind"] == "connection_gap" and r["error_type"] == "TimeoutError" for r in rows
        )
        assert recorder.stats["valid"] == 0
    finally:
        recorder._closed = False
        recorder.close()


def test_real_writer_keeps_a_bounded_number_of_segments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(stop_quotes, "SEGMENT_BYTES", 700)
    monkeypatch.setattr(stop_quotes, "MAX_SEGMENTS", 2)
    recorder = StopQuoteRecorder(tmp_path, "demo")
    recorder.update_symbols({"BTCUSDT"})
    for i in range(8):
        recorder.capture(book(str(100 + i), str(101 + i)))
    recorder._queue.join()
    recorder.close()
    files = list(recorder.root.glob("quotes-*.jsonl"))
    assert 1 <= len(files) <= 2
    for path in files:
        assert all(
            json.loads(line)["coverage_complete"] is False for line in path.read_text().splitlines()
        )


async def test_offline_app_never_starts_the_diagnostic_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app(tmp_path, NOW, predictor_enabled=False)

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("offline mode must not create a diagnostic recorder")

    monkeypatch.setattr(app_module, "StopQuoteRecorder", forbidden)
    await app.start()
    assert app.stop_quotes is None
    await app.stop()


async def test_diagnostic_start_failure_does_not_block_original_app_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _app(tmp_path, NOW, predictor_enabled=False, sim_environment="demo")
    app._quote_diagnostics_enabled = True
    monkeypatch.setattr(app_module, "BinanceFuturesFeed", type(app.m.feed))

    def failed(*args: Any, **kwargs: Any) -> None:
        raise OSError("synthetic diagnostic initialization failure")

    monkeypatch.setattr(app_module, "StopQuoteRecorder", failed)
    await app.start()
    assert app.started and app.stop_quotes is None
    await app.stop()
    assert not app.started


async def test_original_feed_stops_before_diagnostic_shutdown_even_when_it_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _app(tmp_path, NOW, predictor_enabled=False)
    seen: list[str] = []
    feed_close = app.m.feed.close

    async def close_feed() -> None:
        seen.append("feed")
        await feed_close()

    class BrokenDiagnostics:
        async def aclose(self, task: Any) -> None:
            seen.append("diagnostics")
            raise OSError("synthetic diagnostic close failure")

    monkeypatch.setattr(app.m.feed, "close", close_feed)
    await app.start()
    app.stop_quotes = BrokenDiagnostics()  # type: ignore[assignment]
    app._stop_quotes_task = object()  # type: ignore[assignment]
    await app.stop()
    assert seen == ["feed", "diagnostics"]
    assert not app.started
