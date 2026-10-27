from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from jiaoyi.slow.brief import MarketBriefService
from jiaoyi.slow.ticks import TICKS

NOW = datetime(2026, 10, 27, 12, 2, tzinfo=UTC)


async def test_cache_preserves_generation_and_returns_independent_metadata() -> None:
    wall = [NOW]
    calls = 0

    async def part(client: httpx.AsyncClient) -> str:
        nonlocal calls
        calls += 1
        wall[0] += timedelta(seconds=3)
        return "original text"

    service = MarketBriefService(parts=[part], clock=lambda: NOW, observation_clock=lambda: wall[0])
    try:
        text, first = await service.refresh_with_provenance()
        expected = (
            "## Market brief (2026-10-27 12:02 UTC, shared by every meeting this round)\n"
            "original text"
        )
        assert text == expected
        assert first["content"]["generated_at"] == (NOW + timedelta(seconds=3)).isoformat()
        assert first["cache_clock_at"] == NOW.isoformat()
        assert first["attempt"]["started_at"] == NOW.isoformat()
        first["content"]["parts"][0]["sources"].append({"mutated": True})
        wall[0] += timedelta(minutes=2)
        cached_text, cached = await service.refresh_with_provenance()
        assert cached_text == text == service.text
        assert cached["cache_hit"] is True and cached["attempt"] is None
        assert cached["content"]["generated_at"] == (NOW + timedelta(seconds=3)).isoformat()
        assert cached["content"]["parts"][0]["sources"] == []
        assert await service.refresh() == text and calls == 1
    finally:
        await service.aclose()


async def test_failed_attempt_retains_old_content_clock_and_exposes_part_failure() -> None:
    clock = [NOW]
    fail = [False]

    async def part(client: httpx.AsyncClient) -> str:
        if fail[0]:
            raise RuntimeError("private exception detail must not enter provenance")
        return "kept"

    service = MarketBriefService(
        parts=[part], clock=lambda: clock[0], observation_clock=lambda: clock[0]
    )
    try:
        first_text, first = await service.refresh_with_provenance()
        fail[0] = True
        clock[0] += timedelta(minutes=11)
        text, failed = await service.refresh_with_provenance()
        assert text == first_text
        assert failed["status"] == "refresh_failed_retained_cache"
        assert failed["content"] == first["content"]
        assert failed["attempt"]["started_at"] == clock[0].isoformat()
        assert failed["attempt"]["parts"][0]["error_kind"] == "RuntimeError"
        assert "private exception detail" not in json.dumps(failed)
        assert service.failed_parts == 1
    finally:
        await service.aclose()


async def test_concurrent_refresh_binds_each_return_under_original_lock() -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def part(client: httpx.AsyncClient) -> str:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return "same generation"

    service = MarketBriefService(parts=[part], clock=lambda: NOW, observation_clock=lambda: NOW)
    try:
        a = asyncio.create_task(service.refresh_with_provenance())
        await entered.wait()
        b = asyncio.create_task(service.refresh_with_provenance())
        release.set()
        first, second = await asyncio.gather(a, b)
        assert calls == 1 and first[0] == second[0]
        assert first[1]["status"] == "refreshed"
        assert second[1]["status"] == "cache_hit"
        assert first[1]["content"] == second[1]["content"]
    finally:
        await service.aclose()


def _bars(interval: str, count: int) -> list[list[Any]]:
    seconds = {"5m": 300, "1h": 3600, "4h": 14400, "1d": 86400}[interval]
    last_open = int(NOW.timestamp()) // seconds * seconds
    result = []
    for index in range(count):
        opened = (last_open - (count - index - 1) * seconds) * 1000
        close = 100 + index / 100
        result.append([opened, close, close + 1, close - 1, close, 10, opened + seconds * 1000 - 1])
    return result


async def test_mid_sources_and_actual_field_windows_keep_partial_bar_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/fapi/v1/klines":
            symbol, interval = request.url.params["symbol"], request.url.params["interval"]
            requests.append((symbol, interval))
            return httpx.Response(200, json=_bars(interval, int(request.url.params["limit"])))
        return httpx.Response(
            200,
            json={
                "data": [
                    {"value": "50", "value_classification": "Neutral", "timestamp": "1793059200"}
                ]
            },
        )

    monkeypatch.setattr(TICKS, "get", lambda symbol: "0.1")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = MarketBriefService(
        client=client, style="mid", clock=lambda: NOW, observation_clock=lambda: NOW
    )
    try:
        text, meta = await service.refresh_with_provenance()
        assert "1h RSI" in text and "3-day % change" in text
        assert set(requests) == {(s, i) for s in ("BTCUSDT", "ETHUSDT") for i in ("5m", "1h", "1d")}
        parts = meta["content"]["parts"]
        assert [p.get("symbol") for p in parts[:2]] == ["BTCUSDT", "ETHUSDT"]
        for part in parts[:2]:
            assert part["status"] == "ok"
            assert {s["symbol"] for s in part["sources"]} == {part["symbol"]}
            fields = part["fields"]
            assert set(fields) == {
                "chg_4h",
                "chg_24h",
                "chg_3d",
                "range_3d",
                "rsi_1h",
                "sma20",
                "chg_7d",
            }
            assert fields["chg_4h"]["required_bars"] == 49
            assert fields["chg_4h"]["available_bars"] == 49
            assert fields["chg_4h"]["last_bar_closed_at_observation"] is False
            assert fields["chg_4h"]["source_price_event_at"] is None
            assert fields["chg_4h"]["observed_at"] == NOW.isoformat()
            assert fields["rsi_1h"]["available_bars"] == 15
            assert fields["range_3d"]["available_bars"] == 72
        assert parts[2]["sources"][0]["source"] == "alternative_me_fear_greed"
        assert await service.refresh() == text and len(requests) == 6
        json.dumps(meta, allow_nan=False)
    finally:
        await service.aclose()


@pytest.mark.parametrize("bad_clock", ["raising", "naive"])
async def test_diagnostic_clock_failure_does_not_change_brief(bad_clock: str) -> None:
    async def part(client: httpx.AsyncClient) -> str:
        return "unchanged"

    def wall() -> datetime:
        if bad_clock == "raising":
            raise RuntimeError("clock unavailable")
        return NOW.replace(tzinfo=None)

    service = MarketBriefService(parts=[part], clock=lambda: NOW, observation_clock=wall)
    try:
        text, meta = await service.refresh_with_provenance()
        assert text.endswith("\nunchanged")
        assert meta["content"]["generated_at"] is None and meta["returned_at"] is None
        assert meta["content"]["parts"][0]["status"] == "ok"
    finally:
        await service.aclose()


async def test_late_sources_from_failed_part_cannot_rewrite_retained_provenance() -> None:
    release, done = asyncio.Event(), asyncio.Event()
    completed = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal completed
        if request.url.path != "/fapi/v1/klines":
            return httpx.Response(
                200, json={"data": [{"value": "50", "value_classification": "Neutral"}]}
            )
        interval = request.url.params["interval"]
        if interval == "1h":
            return httpx.Response(500)
        await release.wait()
        completed += 1
        if completed == 4:
            done.set()
        return httpx.Response(200, json=_bars(interval, int(request.url.params["limit"])))

    service = MarketBriefService(
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        style="mid",
        clock=lambda: NOW,
        observation_clock=lambda: NOW,
    )
    try:
        text, first = await service.refresh_with_provenance()
        assert first["content"]["status"] == "partial" and service.failed_parts == 2
        assert first["content"]["parts"][0]["sources"] == []
        release.set()
        await asyncio.wait_for(done.wait(), timeout=1)
        again, cached = await service.refresh_with_provenance()
        assert again == text and cached["content"] == first["content"]
    finally:
        release.set()
        await service.aclose()
