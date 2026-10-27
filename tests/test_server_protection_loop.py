from __future__ import annotations

import asyncio
import dataclasses
from datetime import datetime
from pathlib import Path

import pytest

from jiaoyi import contracts as c
from jiaoyi.loop import Runner
from tests.test_loop import FakeExecutor, _app, _position


class ProtectionExecutor(FakeExecutor):
    def __init__(self, managed: bool | None) -> None:
        super().__init__()
        self.managed = managed

    def server_managed(self, position_id: str) -> bool:
        if self.managed is None:
            raise RuntimeError("ownership unavailable")
        return self.managed

    async def protection_tick(self) -> None:
        pass


@pytest.mark.parametrize("managed", [True, False, None])
async def test_tp_fallback_skips_server_or_unknown_but_keeps_legacy(
    tmp_path: Path, now: datetime, managed: bool | None
) -> None:
    app = _app(tmp_path, now)
    app.m = dataclasses.replace(app.m, executor=ProtectionExecutor(managed))
    runner = Runner(app)
    position = _position(side=c.Side.LONG, take_profit_price=100.0)
    runner.positions = [position]
    runner._tp_past[position.position_id] = now
    trade = c.Trade(symbol=position.symbol, ts=now, price=110.0, qty=1.0, taker_side=c.Side.LONG)
    runner._tp_fallback(trade)
    assert (position.position_id in runner._tp_past) is (managed is False)


async def test_periodic_tick_does_not_overlap_and_cancels(tmp_path: Path, now: datetime) -> None:
    runner = Runner(_app(tmp_path, now))
    entered = asyncio.Event()
    release = asyncio.Event()
    count = 0

    async def tick() -> None:
        nonlocal count
        count += 1
        entered.set()
        await release.wait()

    task = runner._every(0.01, tick)
    await asyncio.wait_for(entered.wait(), 1)
    await asyncio.sleep(0.04)
    assert count == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
