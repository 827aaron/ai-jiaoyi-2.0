"""独立验证响应后一次性行情旁路：双时钟、限量、过期和磁盘故障隔离。"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from jiaoyi import contracts as c
from jiaoyi.core.observations import KeeperObservationCollector


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.wall = now
        self.mono = 0.0


class _Records:
    def __init__(self) -> None:
        self.values: list[dict[str, Any]] = []
        self.condition = threading.Condition()

    def write(self, record: dict[str, Any]) -> None:
        with self.condition:
            self.values.append(record)
            self.condition.notify_all()

    def wait(self, predicate: Callable[[list[dict[str, Any]]], bool]) -> None:
        with self.condition:
            assert self.condition.wait_for(lambda: predicate(self.values), timeout=2.0)


def _record(now: datetime, request_id: str = "hold-1") -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": "keeper_observation",
        "symbol": "BTCUSDT",
        "position_id": "pos-1",
        "request_id": request_id,
        "response_completed_at": now.isoformat(),
        "observed_at": now.isoformat(),
        "attempt_count": 1,
        "model_ok": True,
        "is_execution_fill": False,
    }


def _event(kind: str, at: datetime, price: float = 101.0) -> c.MarketEvent:
    if kind == "trade":
        return c.Trade(symbol="BTCUSDT", ts=at, price=price, qty=2, taker_side=c.Side.LONG)
    if kind == "book":
        return c.BookTop(symbol="BTCUSDT", ts=at, bids=[(price, 2)], asks=[(price + 1, 3)])
    return c.MarkPrice(symbol="BTCUSDT", ts=at, mark_price=price, funding_rate=0)


def test_first_valid_event_per_source_requires_both_clocks_after_response(
    tmp_path: Path, now: datetime
) -> None:
    clock = _Clock(now)
    records = _Records()
    collector = KeeperObservationCollector(
        tmp_path, wall=lambda: clock.wall, monotonic=lambda: clock.mono, writer=records.write
    )
    try:
        collector.offer(_record(now))
        for kind in ("trade", "book", "mark"):
            # 旧交易所点迟到、恰好同刻、记录时钟回退、来源点跑到未来：均不能占首个有效槽。
            collector.on_event(_event(kind, now - timedelta(seconds=1)), now + timedelta(seconds=1))
            collector.on_event(_event(kind, now), now + timedelta(seconds=1))
            collector.on_event(_event(kind, now + timedelta(seconds=1)), now)
            collector.on_event(_event(kind, now + timedelta(seconds=3)), now + timedelta(seconds=2))
            collector.on_event(_event(kind, now + timedelta(seconds=1)), now + timedelta(seconds=2))
            collector.on_event(
                _event(kind, now + timedelta(seconds=2), price=999), now + timedelta(seconds=3)
            )
        records.wait(lambda rows: any(row.get("reason") == "all_sources_processed" for row in rows))
        samples = [row for row in records.values if row["kind"] == "keeper_first_newer_event"]
        assert [sample["source"] for sample in samples] == ["trade", "book", "mark"]
        assert collector.stats["pending"] == 0
        ended = next(row for row in records.values if row.get("reason") == "all_sources_processed")
        assert ended["delivery_status"] == "enqueued_receipts_required"
        assert ended["dropped_sources"] == []
        assert collector.stats["clock_inconsistent"] >= 3
        for sample in samples:
            assert sample["source_event_ts"] == (now + timedelta(seconds=1)).isoformat()
            assert sample["observed_at"] == (now + timedelta(seconds=2)).isoformat()
            assert sample["strict_after_response"] is True
            assert sample["is_execution_fill"] is False
            assert 999 not in sample["quote"].values()
    finally:
        collector.close()


def test_ttl_uses_monotonic_despite_wall_clock_rollback(tmp_path: Path, now: datetime) -> None:
    clock = _Clock(now)
    records = _Records()
    collector = KeeperObservationCollector(
        tmp_path,
        ttl_s=5,
        wall=lambda: clock.wall,
        monotonic=lambda: clock.mono,
        writer=records.write,
    )
    try:
        collector.offer(_record(now))
        clock.wall = now - timedelta(days=1)
        clock.mono = 5.0
        collector.on_event(_event("trade", now + timedelta(seconds=1)), clock.wall)
        records.wait(lambda rows: any(row.get("reason") == "ttl_expired" for row in rows))
        expired = next(row for row in records.values if row.get("reason") == "ttl_expired")
        assert expired["missing_sources"] == ["book", "mark", "trade"]
        assert collector.stats["pending"] == 0 and collector.stats["expired"] == 1
        assert not any(row["kind"] == "keeper_first_newer_event" for row in records.values)
    finally:
        collector.close()


def test_capacity_and_no_response_are_explicit_without_global_tick_logging(
    tmp_path: Path, now: datetime
) -> None:
    records = _Records()
    collector = KeeperObservationCollector(tmp_path, max_pending=1, writer=records.write)
    try:
        for _ in range(100):
            collector.on_event(_event("trade", now))
        no_model = {**_record(now), "request_id": None, "response_completed_at": None}
        collector.offer(no_model)
        assert collector.stats["pending"] == 0
        collector.offer(_record(now, "hold-1"))
        collector.offer(_record(now, "hold-2"))
        assert collector.stats["pending"] == 1
        records.wait(lambda rows: any(row.get("reason") == "pending_capacity" for row in rows))
        assert len(records.values) == 4  # 3次判断+1次容量收尾，无全市场tick记录。
        ended = next(row for row in records.values if row.get("reason") == "pending_capacity")
        assert ended["request_id"] == "hold-1"
    finally:
        collector.close()


def test_full_queue_and_close_never_wait_for_a_blocked_writer(
    tmp_path: Path, now: datetime
) -> None:
    entered, release = threading.Event(), threading.Event()

    def blocked(record: dict[str, Any]) -> None:
        entered.set()
        release.wait(timeout=2.0)

    collector = KeeperObservationCollector(tmp_path, max_queue=1, max_pending=1, writer=blocked)
    try:
        collector.offer({"kind": "keeper_observation"})
        assert entered.wait(timeout=2.0)
        collector.offer({"kind": "keeper_observation"})
        start = time.monotonic()
        for i in range(30):
            collector.offer(_record(now, f"hold-{i}"))
        collector.on_event(_event("trade", now + timedelta(seconds=1)), now + timedelta(seconds=2))
        collector.close()
        assert time.monotonic() - start < 0.5
        assert collector.stats["queued"] <= 1
        assert collector.stats["pending"] == 0
        assert collector.stats["dropped"] > 0
        dropped = collector.stats["dropped"]
        collector.offer(_record(now))
        assert collector.stats["dropped"] == dropped + 1
    finally:
        release.set()
        collector.close()


def test_writer_failure_does_not_kill_later_observations(tmp_path: Path, now: datetime) -> None:
    records = _Records()
    first = True

    def flaky(record: dict[str, Any]) -> None:
        nonlocal first
        if first:
            first = False
            raise OSError("模拟不可写磁盘")
        records.write(record)

    collector = KeeperObservationCollector(tmp_path, writer=flaky)
    try:
        collector.offer({"kind": "keeper_observation"})
        collector.offer({"kind": "keeper_observation", "response_completed_at": None})
        records.wait(lambda rows: len(rows) == 1)
        assert collector.stats["write_errors"] == 1
        assert collector.stats["capture_errors"] == 0
    finally:
        collector.close()


@pytest.mark.parametrize("bad_clock", ["naive", "raising"])
def test_bad_diagnostic_clock_does_not_escape_into_event_processing(
    tmp_path: Path, now: datetime, bad_clock: str
) -> None:
    def wall() -> datetime:
        if bad_clock == "naive":
            return now.replace(tzinfo=None)
        raise RuntimeError("诊断时钟异常")

    collector = KeeperObservationCollector(tmp_path, wall=wall, writer=lambda record: None)
    try:
        collector.offer(_record(now))
        collector.on_event(_event("trade", now + timedelta(seconds=1)))
        assert collector.stats["pending"] == 1
        if bad_clock == "raising":
            assert collector.stats["capture_errors"] == 1
    finally:
        collector.close()


def test_offered_nested_values_are_frozen_before_background_writer(
    tmp_path: Path, now: datetime
) -> None:
    entered, release = threading.Event(), threading.Event()
    records = _Records()
    first = True

    def writer(record: dict[str, Any]) -> None:
        nonlocal first
        if first:
            first = False
            entered.set()
            release.wait(timeout=2.0)
        records.write(record)

    collector = KeeperObservationCollector(tmp_path, writer=writer)
    try:
        collector.offer({"kind": "blocker"})
        assert entered.wait(timeout=2.0)
        row = {**_record(now), "post_response_view": {"price": {"value": 101}, "path": [1, 2]}}
        collector.offer(row)
        row["post_response_view"]["price"]["value"] = 999
        row["post_response_view"]["path"].append(999)
        release.set()
        records.wait(lambda rows: len(rows) >= 2)
        assert records.values[1]["post_response_view"] == {"price": {"value": 101}, "path": [1, 2]}
    finally:
        release.set()
        collector.close()


def test_queue_dropped_source_remains_unknown_in_completed_collection(
    tmp_path: Path, now: datetime
) -> None:
    entered, release = threading.Event(), threading.Event()
    records = _Records()
    first = True

    def writer(record: dict[str, Any]) -> None:
        nonlocal first
        if first:
            first = False
            entered.set()
            release.wait(timeout=2.0)
        records.write(record)

    collector = KeeperObservationCollector(tmp_path, max_queue=4, writer=writer)
    try:
        collector.offer({"kind": "blocker"})
        assert entered.wait(timeout=2.0)
        for _ in range(4):
            collector.offer({"kind": "queued"})
        collector.offer(_record(now))
        collector.on_event(_event("trade", now + timedelta(seconds=1)), now + timedelta(seconds=2))
        release.set()
        records.wait(lambda rows: len(rows) == 5)
        for kind in ("book", "mark"):
            collector.on_event(_event(kind, now + timedelta(seconds=1)), now + timedelta(seconds=2))
        records.wait(lambda rows: any(row.get("reason") == "all_sources_processed" for row in rows))
        ended = next(row for row in records.values if row.get("reason") == "all_sources_processed")
        assert ended["dropped_sources"] == ["trade"]
        assert ended["missing_sources"] == []
        assert ended["delivery_status"] == "unknown_due_to_queue_drop"
        assert collector.stats["pending"] == 0
    finally:
        release.set()
        collector.close()


def test_any_symbol_event_expires_pending_and_inconsistent_round_never_registers(
    tmp_path: Path, now: datetime
) -> None:
    clock = _Clock(now)
    records = _Records()
    collector = KeeperObservationCollector(
        tmp_path, ttl_s=5, monotonic=lambda: clock.mono, writer=records.write
    )
    try:
        collector.offer({**_record(now), "clock_consistent": False})
        assert collector.stats["pending"] == 0
        collector.offer(_record(now, "hold-2"))
        clock.mono = 5
        event = c.Trade(
            symbol="ETHUSDT", ts=now + timedelta(seconds=1), price=10, qty=1, taker_side=c.Side.LONG
        )
        collector.on_event(event, now + timedelta(seconds=2))
        records.wait(lambda rows: any(row.get("reason") == "ttl_expired" for row in rows))
        assert collector.stats["pending"] == 0 and collector.stats["expired"] == 1
        assert not any(row["kind"] == "keeper_first_newer_event" for row in records.values)
    finally:
        collector.close()


async def test_runner_updates_features_and_preserves_patrol_close_when_observer_raises(
    tmp_path: Path, now: datetime
) -> None:
    import asyncio
    import dataclasses
    from typing import cast
    from unittest.mock import patch

    from jiaoyi.contracts.position import CloseReason
    from jiaoyi.loop import Runner
    from tests.test_loop import FakeExecutor, FakePatrol, _app, _position

    app = _app(tmp_path, now)
    executor = FakeExecutor()
    patrol = FakePatrol(c.PatrolAction(ts=now, symbols=["BTCUSDT"], reason="测试巡逻兵"))
    app.m = dataclasses.replace(app.m, executor=executor, patrol=patrol)
    stages: list[str] = []
    original_update = app.m.features.update

    def update(event: c.MarketEvent) -> None:
        original_update(event)
        stages.append("features_updated")

    class BrokenObserver:
        def on_event(self, event: c.MarketEvent) -> None:
            stages.append("observer")
            raise OSError("模拟诊断队列异常")

    app.observations = cast(Any, BrokenObserver())
    runner = Runner(app)
    runner.positions = [_position()]
    with patch.object(app.m.features, "update", side_effect=update):
        await runner.on_event(_event("mark", now))
        await asyncio.gather(*runner._tasks)
    assert stages == ["features_updated", "observer"]
    assert executor.closed == [("pos-0001", CloseReason.PATROL)]
