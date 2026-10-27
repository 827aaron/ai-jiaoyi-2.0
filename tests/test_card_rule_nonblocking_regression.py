"""独立调度回归：反向仓慢平不能阻塞同轮正常复核；排队规则必须重读新卡。"""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from jiaoyi import contracts as c
from jiaoyi.contracts.position import CloseReason
from jiaoyi.gate.jev import JevDecisionModel, MockJevTransport
from jiaoyi.keeper.keeper import JevKeeper
from jiaoyi.loop import Runner
from tests.fastloop_helpers import FAST, card, features
from tests.test_keeper_patrol import close_p, position
from tests.test_loop import FakeExecutor, _app


class BlockingExecutor(FakeExecutor):
    def __init__(self, held: list[c.PositionRecord]) -> None:
        super().__init__()
        self.held = held
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    def live_positions(self) -> list[c.PositionRecord]:
        return list(self.held)

    async def close(self, position_id: str, reason: CloseReason) -> None:
        await super().close(position_id, reason)
        self.held = [
            p.model_copy(update={"status": c.PositionStatus.CLOSING})
            if p.position_id == position_id
            else p
            for p in self.held
        ]
        self.started.set()
        await self.release.wait()


def rig(
    tmp_path: Path, now: datetime
) -> tuple[Runner, BlockingExecutor, list[c.BattleCard], asyncio.Event]:
    normal_review = asyncio.Event()

    class ObservedTransport(MockJevTransport):
        async def evaluate(self, *args: Any, **kwargs: Any) -> Any:
            normal_review.set()
            return await super().evaluate(*args, **kwargs)

    cd = card(now, bias="short")
    same_side = cd.symbols[0].model_copy(update={"symbol": "ETHUSDT", "bias": c.Bias.LONG})
    source = [cd.model_copy(update={"symbols": [cd.symbols[0], same_side]})]
    held = [position(now), position(now, position_id="pos-normal", symbol="ETHUSDT")]
    ex, app = BlockingExecutor(held), _app(tmp_path, now)
    model = JevDecisionModel(ObservedTransport(answer=close_p(0.1)), FAST, timeout_ms=200)
    keeper = JevKeeper(
        model,
        FAST,
        lambda symbol: features(now).model_copy(update={"symbol": symbol}),
        lambda _: source[0],
    )
    app.m = dataclasses.replace(app.m, executor=ex, keeper=keeper)
    runner = Runner(app)
    runner.positions = list(held)
    runner.sync_watch = AsyncMock()
    return runner, ex, source, normal_review


async def drain(runner: Runner) -> None:
    while runner._tasks:
        await asyncio.gather(*tuple(runner._tasks))


@pytest.mark.asyncio
async def test_same_side_review_starts_before_opposite_close_finishes(
    tmp_path: Path, now: datetime
) -> None:
    runner, ex, _, normal_review = rig(tmp_path, now)
    tick = asyncio.create_task(runner.keeper_tick())
    try:
        await asyncio.wait_for(ex.started.wait(), 1)
        await asyncio.wait_for(normal_review.wait(), 1)
        assert not ex.release.is_set()
        assert ex.closed == [("pos-1", CloseReason.KEEPER)]
    finally:
        ex.release.set()
        await tick
        await drain(runner)


@pytest.mark.asyncio
async def test_concurrent_card_notifications_do_not_repeat_pending_close(
    tmp_path: Path, now: datetime
) -> None:
    runner, ex, source, _ = rig(tmp_path, now)
    runner._card_published()
    try:
        await asyncio.wait_for(ex.started.wait(), 1)
        source[0] = source[0].model_copy(update={"version": source[0].version + 1})
        for _ in range(3):
            runner._card_published()
        await asyncio.sleep(0)
        assert ex.closed == [("pos-1", CloseReason.KEEPER)]
    finally:
        ex.release.set()
        await drain(runner)
    assert ex.closed == [("pos-1", CloseReason.KEEPER)]


@pytest.mark.asyncio
async def test_queued_card_rule_rechecks_new_same_side_card_before_close(
    tmp_path: Path, now: datetime
) -> None:
    runner, ex, source, _ = rig(tmp_path, now)
    runner._card_published()
    source[0] = card(now, bias="long")
    ex.release.set()
    await drain(runner)
    assert ex.closed == []
