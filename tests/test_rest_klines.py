"""K 线回填：超过币安单次 1500 根的要往前分页。"""

from __future__ import annotations

import asyncio
import itertools
from typing import Any

import pytest

from jiaoyi.market import rest


def test_fetch_recent_klines_pages_back(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    total = 3840
    minute = 60_000

    def fake_get(url: str, timeout_s: float) -> list[list[Any]]:
        calls.append(url)
        q = dict(p.split("=") for p in url.split("?")[1].split("&"))
        end = int(q["endTime"]) if "endTime" in q else total * minute
        n = int(q["limit"])
        starts = [t for t in range(0, total * minute, minute) if t <= end][-n:]
        return [
            [t, "1", "2", "0.5", "1.5", "10", t + minute - 1, "15", 5, "5", "7", "0"]
            for t in starts
        ]

    monkeypatch.setattr(rest, "_get", fake_get)
    bars = asyncio.run(rest.fetch_recent_klines("BTCUSDT", limit=3840))
    assert len(bars) == 3840 and len(calls) == 3
    assert "endTime" not in calls[0] and "endTime" in calls[1]
    assert all(a.open_time < b.open_time for a, b in itertools.pairwise(bars))  # 时间升序


def test_fetch_klines_between_pages_forward(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import UTC, datetime

    minute = 60_000
    calls: list[dict[str, str]] = []

    def fake_get(url: str, timeout_s: float) -> list[list[Any]]:
        q = dict(p.split("=") for p in url.split("?")[1].split("&"))
        calls.append(q)
        lo, hi, n = int(q["startTime"]), int(q["endTime"]), int(q["limit"])
        first = -(-lo // minute) * minute
        starts = list(range(first, hi + 1, minute))[:n]
        return [
            [t, "1", "2", "0.5", "1.5", "10", t + minute - 1, "15", 5, "5", "7", "0"]
            for t in starts
        ]

    monkeypatch.setattr(rest, "_get", fake_get)
    start = datetime(2026, 10, 20, tzinfo=UTC)
    end = datetime(2026, 10, 22, tzinfo=UTC)  # 2881 根（两头都含）
    bars = asyncio.run(rest.fetch_klines_between("BTCUSDT", start, end))
    assert len(bars) == 2881 and len(calls) == 2
    assert all(a.open_time < b.open_time for a, b in itertools.pairwise(bars))
