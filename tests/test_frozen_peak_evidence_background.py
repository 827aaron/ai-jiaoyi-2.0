"""冻结盈利价证据后台读：复查不等交易所，一轮不等最慢那笔；默认（sync）行为不变。"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from jiaoyi import contracts as c
from jiaoyi.contracts import Role
from jiaoyi.contracts.position import CloseReason
from jiaoyi.core.hotconfig import HOT_FAST
from jiaoyi.core.recorder import RoleRecorder
from jiaoyi.core.store import JsonlDecisionLog
from jiaoyi.gate.jev import JevDecisionModel, MockJevTransport
from jiaoyi.keeper.keeper import JevKeeper
from jiaoyi.loop import Runner
from tests.fastloop_helpers import FAST
from tests.test_keeper_patrol import close_p, position
from tests.test_loop import FakeExecutor, FakeKeeper, FakePatrol, _app

BACKGROUND = FAST.model_copy(
    update={
        "frozen_peak_evidence_mode": "background",
        "frozen_peak_evidence_interval_s": 20,
        "frozen_peak_evidence_max_age_s": 60,
    }
)
TRIGGERED = {"triggered": True, "target_price": 102.0, "status": "triggered"}


class Evidence:
    """交易所证据读的替身：``hang`` 里的仓一直等（能被取消），其余立刻给 ``result``。"""

    def __init__(self, result: dict[str, Any] | None, hang: set[str] | None = None) -> None:
        self.result, self.hang = result, hang or set()
        self.calls: list[tuple[str, datetime]] = []
        self.release = asyncio.Event()
        self.cancelled: list[str] = []

    async def __call__(self, pid: str, now: datetime) -> dict[str, Any] | None:
        self.calls.append((pid, now))
        if pid in self.hang:
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled.append(pid)
                raise
        return self.result


def _keeper(cfg: c.FastLoopConfig, evidence: Evidence, log: JsonlDecisionLog | None = None):
    return JevKeeper(
        JevDecisionModel(MockJevTransport(answer=close_p(0.1)), cfg, timeout_ms=200),
        cfg,
        lambda _: None,  # 没行情：不触发规则时走「维持现状」，不问模型
        lambda _: None,
        RoleRecorder(Role.KEEPER, log) if log is not None else None,
        frozen_peak_review=evidence,
    )


def test_defaults_keep_sync_and_settings_are_validated_and_hot() -> None:
    assert FAST.frozen_peak_evidence_mode == "sync"
    assert FAST.frozen_peak_evidence_interval_s == 30
    assert FAST.frozen_peak_evidence_max_age_s == 90
    assert c.FastLoopConfig(frozen_peak_evidence_max_age_s=0).frozen_peak_evidence_max_age_s == 0
    with pytest.raises(ValidationError):
        c.FastLoopConfig(frozen_peak_evidence_interval_s=30, frozen_peak_evidence_max_age_s=59)
    ok = c.FastLoopConfig(frozen_peak_evidence_interval_s=20, frozen_peak_evidence_max_age_s=50)
    assert ok.frozen_peak_evidence_max_age_s == 50
    with pytest.raises(ValidationError):
        c.FastLoopConfig(frozen_peak_evidence_mode="async")
    with pytest.raises(ValidationError):
        c.FastLoopConfig(frozen_peak_evidence_interval_s=1)
    assert {
        "frozen_peak_evidence_mode",
        "frozen_peak_evidence_interval_s",
        "frozen_peak_evidence_max_age_s",
    } <= HOT_FAST


async def test_sync_mode_reads_evidence_inside_every_review(now: datetime) -> None:
    evidence = Evidence(TRIGGERED)
    keeper = _keeper(FAST, evidence)
    assert not keeper.frozen_peak_background()
    verdict = await keeper.review(position(now), now)
    assert verdict.action == "close" and evidence.calls == [("pos-1", now)]


async def test_fresh_cache_triggers_exactly_like_the_sync_path(
    tmp_path: Path, now: datetime
) -> None:
    sync_log = JsonlDecisionLog(tmp_path / "sync")
    bg_log = JsonlDecisionLog(tmp_path / "bg")
    sync_keeper = _keeper(FAST, Evidence(TRIGGERED), sync_log)
    bg_evidence = Evidence(TRIGGERED)
    bg_keeper = _keeper(BACKGROUND, bg_evidence, bg_log)

    await bg_keeper.refresh_frozen_peak("pos-1", now)
    at = now + timedelta(seconds=60)  # 正好到 max_age 还算新鲜
    sync_verdict = await sync_keeper.review(position(now), at)
    bg_verdict = await bg_keeper.review(position(now), at)

    assert bg_evidence.calls == [("pos-1", now)]  # 复查本身没读交易所
    assert bg_verdict == sync_verdict
    assert bg_verdict.action == "close" and bg_verdict.p_close == 1.0
    day = now.date().isoformat()
    sync_row, bg_row = sync_log.scan_day(day, "")[-1], bg_log.scan_day(day, "")[-1]
    for key in ("outcome", "summary", "output"):
        assert bg_row[key] == sync_row[key]
    assert bg_row["output"]["frozen_peak_exit"] == TRIGGERED


async def test_stale_or_missing_cache_skips_rule_and_warns_once_per_streak(
    tmp_path: Path, now: datetime, caplog: pytest.LogCaptureFixture
) -> None:
    log = JsonlDecisionLog(tmp_path / "d")
    evidence = Evidence(TRIGGERED)
    keeper = _keeper(BACKGROUND, evidence, log)
    caplog.set_level(logging.WARNING, logger="jiaoyi.keeper.keeper")

    def stale_warnings() -> int:
        return sum("is stale" in r.getMessage() for r in caplog.records)

    never_read = await keeper.review(position(now), now)
    assert never_read.action == "hold" and evidence.calls == []
    row = log.scan_day(now.date().isoformat(), "")[-1]
    assert "frozen_peak_exit" not in row["output"]  # 还没读过：不算过期
    assert stale_warnings() == 0

    assert await keeper.refresh_frozen_peak("pos-1", now) is True
    late = now + timedelta(seconds=61)
    assert (await keeper.review(position(now), late)).action == "hold"
    row = log.scan_day(now.date().isoformat(), "")[-1]
    assert row["output"]["frozen_peak_exit"] == {
        "triggered": False,
        "detail": "evidence_stale",
        "evidence_as_of": now.isoformat(),
    }
    assert (await keeper.review(position(now), late + timedelta(seconds=5))).action == "hold"
    assert stale_warnings() == 1  # 同一段过期只记一次
    assert all(r.levelno < logging.CRITICAL for r in caplog.records)

    await keeper.refresh_frozen_peak("pos-1", late)
    assert (await keeper.review(position(now), late)).action == "close"
    assert (await keeper.review(position(now), late + timedelta(seconds=61))).action == "hold"
    assert stale_warnings() == 2  # 新鲜过一次，再过期是新的一段


async def test_position_without_frozen_peak_never_reports_stale(
    tmp_path: Path, now: datetime, caplog: pytest.LogCaptureFixture
) -> None:
    log = JsonlDecisionLog(tmp_path / "d")
    keeper = _keeper(BACKGROUND, Evidence(None), log)
    caplog.set_level(logging.WARNING, logger="jiaoyi.keeper.keeper")
    assert await keeper.refresh_frozen_peak("pos-1", now) is False
    await keeper.review(position(now), now + timedelta(hours=1))
    assert "frozen_peak_exit" not in log.scan_day(now.date().isoformat(), "")[-1]["output"]
    assert not any("is stale" in r.getMessage() for r in caplog.records)


async def test_max_age_zero_means_no_limit(now: datetime) -> None:
    cfg = BACKGROUND.model_copy(update={"frozen_peak_evidence_max_age_s": 0})
    keeper = _keeper(cfg, Evidence(TRIGGERED))
    await keeper.refresh_frozen_peak("pos-1", now)
    assert (await keeper.review(position(now), now + timedelta(days=1))).action == "close"


async def test_refresher_cadence_is_per_position_and_never_overlaps(now: datetime) -> None:
    evidence = Evidence(None, hang={"pos-slow"})
    keeper = _keeper(BACKGROUND, evidence)
    slow = asyncio.create_task(keeper.refresh_frozen_peak("pos-slow", now))
    await keeper.refresh_frozen_peak("pos-1", now)
    await asyncio.sleep(0)
    later = now + timedelta(seconds=25)
    assert not keeper.frozen_peak_due("pos-slow", later)  # 还在读：不叠第二次
    assert not keeper.frozen_peak_due("pos-1", now + timedelta(seconds=19))
    assert keeper.frozen_peak_due("pos-1", now + timedelta(seconds=20))
    keeper.forget("pos-slow")
    evidence.release.set()
    await asyncio.wait_for(slow, 1)
    assert "pos-slow" not in keeper._fp_cache  # 读到一半平掉的仓不留缓存


def _runner(tmp_path: Path, now: datetime, keeper: Any, ids: list[str]) -> tuple[Runner, Any]:
    app = _app(tmp_path, now)
    ex = FakeExecutor()
    ex.held = [position(now, position_id=pid, symbol=f"S{i}USDT") for i, pid in enumerate(ids)]
    app.m = dataclasses.replace(app.m, executor=ex, keeper=keeper)
    return Runner(app), ex


async def test_slow_evidence_read_does_not_gate_the_review_round(
    tmp_path: Path, now: datetime
) -> None:
    evidence = Evidence(None, hang={"pos-slow"})
    keeper = _keeper(BACKGROUND, evidence)
    r, _ = _runner(tmp_path, now, keeper, ["pos-slow", "pos-fast"])
    await r.refresh_positions()
    await r.frozen_peak_tick()
    await asyncio.sleep(0)
    assert sorted(p for p, _ in evidence.calls) == ["pos-fast", "pos-slow"]

    await asyncio.wait_for(r.keeper_tick(), 1)  # 交易所那一笔一直不回，这一轮照样跑完
    await asyncio.wait_for(asyncio.gather(*(t for t in r._tasks if _is_review(t))), 1)
    assert len(evidence.calls) == 2  # 复查没再去读交易所

    for t in list(r._tasks):
        t.cancel()
    await asyncio.gather(*r._tasks, return_exceptions=True)
    assert evidence.cancelled == ["pos-slow"]


async def test_sync_mode_still_waits_for_the_slowest_evidence_read(
    tmp_path: Path, now: datetime
) -> None:
    evidence = Evidence(None, hang={"pos-slow"})
    r, _ = _runner(tmp_path, now, _keeper(FAST, evidence), ["pos-slow", "pos-fast"])
    await r.frozen_peak_tick()  # sync：后台不读
    assert evidence.calls == []
    tick = asyncio.create_task(r.keeper_tick())
    done, _ = await asyncio.wait({tick}, timeout=0.2)
    assert not done  # 默认行为：一轮等最慢那笔
    tick.cancel()
    await asyncio.gather(tick, return_exceptions=True)
    assert evidence.cancelled == ["pos-slow"]


def _is_review(task: asyncio.Task[Any]) -> bool:
    return "_review_all" in repr(task.get_coro())


class SlowReviewKeeper(FakeKeeper):
    """后台模式下一笔仓的复查本身很慢（例如 Jev 重试），别的仓下一轮照常。"""

    def __init__(self) -> None:
        super().__init__("hold")
        self.release = asyncio.Event()

    def frozen_peak_background(self) -> bool:
        return True

    def frozen_peak_due(self, position_id: str, now: datetime) -> bool:
        return False

    async def refresh_frozen_peak(self, position_id: str, now: datetime) -> None:
        raise AssertionError("not due")

    async def review(self, position: c.PositionRecord, now: datetime) -> c.KeeperVerdict:
        if position.position_id == "pos-slow":
            await self.release.wait()
        return await super().review(position, now)


async def test_one_slow_review_does_not_delay_the_next_round_of_others(
    tmp_path: Path, now: datetime
) -> None:
    keeper = SlowReviewKeeper()
    r, _ = _runner(tmp_path, now, keeper, ["pos-slow", "pos-fast"])
    await asyncio.wait_for(r.keeper_tick(), 1)
    for _ in range(5):
        await asyncio.sleep(0)
    await asyncio.wait_for(r.keeper_tick(), 1)
    for _ in range(5):
        await asyncio.sleep(0)
    assert keeper.reviewed == ["pos-fast", "pos-fast"]  # 慢的那笔还在问，不重叠也不拖别人
    keeper.release.set()
    await asyncio.wait_for(asyncio.gather(*r._tasks), 1)
    assert keeper.reviewed.count("pos-slow") == 1


async def _drain(r: Runner) -> None:
    for _ in range(3):
        await asyncio.wait_for(asyncio.gather(*list(r._tasks)), 1)


async def test_triggered_background_read_closes_without_waiting_for_keeper_round(
    tmp_path: Path, now: datetime
) -> None:
    keeper = _keeper(BACKGROUND, Evidence(TRIGGERED))
    r, ex = _runner(tmp_path, now, keeper, ["pos-1"])
    await r.refresh_positions()
    await r.frozen_peak_tick()  # 没有 keeper_tick：同一秒里读到触发就复查、请求全平
    await _drain(r)
    assert ex.closed == [("pos-1", CloseReason.KEEPER)]


async def test_triggered_read_during_running_review_queues_an_urgent_recheck(
    tmp_path: Path, now: datetime
) -> None:
    keeper = _keeper(BACKGROUND, Evidence(TRIGGERED))
    r, ex = _runner(tmp_path, now, keeper, ["pos-1"])
    r.app.m = dataclasses.replace(r.app.m, patrol=FakePatrol())
    r.m = r.app.m
    await r.refresh_positions()
    r._reviewing.add("pos-1")
    await r.frozen_peak_tick()
    await _drain(r)
    assert ex.closed == [] and r.m.patrol.urgent == {"pos-1"}
    r._reviewing.discard("pos-1")
    await r.urgent_tick()  # 下一秒的急查
    assert ex.closed == [("pos-1", CloseReason.KEEPER)]


async def test_untriggered_read_does_not_review(tmp_path: Path, now: datetime) -> None:
    keeper = _keeper(BACKGROUND, Evidence({"triggered": False}))
    reviewed: list[str] = []
    real_review = keeper.review

    async def review(pos: c.PositionRecord, at: datetime) -> c.KeeperVerdict:
        reviewed.append(pos.position_id)
        return await real_review(pos, at)

    keeper.review = review  # type: ignore[method-assign]
    r, _ = _runner(tmp_path, now, keeper, ["pos-1"])
    await r.refresh_positions()
    await r.frozen_peak_tick()
    await _drain(r)
    assert reviewed == []
