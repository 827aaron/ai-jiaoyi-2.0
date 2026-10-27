"""有效作战卡反向硬关闭：合成数据，不调用模型网络/交易所。"""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from jiaoyi import contracts as c
from jiaoyi.contracts.position import CloseReason
from jiaoyi.gate.jev import JevDecisionModel, MockJevTransport
from jiaoyi.keeper.keeper import JevKeeper
from jiaoyi.loop import Runner
from jiaoyi.slow.commander import BattleCardCommander
from tests.fastloop_helpers import FAST, card, features
from tests.test_keeper_patrol import close_p, position
from tests.test_loop import FakeExecutor, _app


async def drain(runner: Runner) -> None:
    while runner._tasks:
        await asyncio.gather(*runner._tasks)
        await asyncio.sleep(0)  # 等待done回调从Runner集合移除，不忙等已完成future。


def keeper(cd: c.BattleCard | None, now: datetime) -> tuple[JevKeeper, AsyncMock]:
    model = AsyncMock()
    model.ask.side_effect = AssertionError("硬规则不得调用模型")
    k = JevKeeper(model, FAST, lambda _: None, lambda _: cd)
    return k, model


@pytest.mark.asyncio
@pytest.mark.parametrize("side,bias", [("long", "short"), ("short", "long")])
async def test_opposite_closes_without_market_model_or_confirmations(
    now: datetime, side: str, bias: str
) -> None:
    k, model = keeper(card(now, bias=bias), now)
    k._features = lambda _: (_ for _ in ()).throw(AssertionError("不读行情"))
    verdict = await k.review(position(now, side=side), now)
    assert verdict.action == "close" and verdict.consecutive == 1
    assert verdict.jev_request_id is None and not verdict.review_succeeded
    assert "反向硬规则" in verdict.reason
    model.ask.assert_not_awaited()


@pytest.mark.parametrize(
    "change",
    [
        {"bias": "long"},
        {"bias": "both"},
        {"bias": "no_trade"},
        {"symbol": "ETHUSDT"},
        {"bias": "short", "expires_at": "past"},
        {"bias": "short", "issued_at": "future"},
    ],
)
def test_non_opposite_and_invalid_symbol_card_do_not_close(
    now: datetime, change: dict[str, Any]
) -> None:
    change = dict(change)
    if change.get("expires_at") == "past":
        change["expires_at"] = now - timedelta(seconds=1)
    if change.get("issued_at") == "future":
        change["issued_at"] = now + timedelta(seconds=1)
    k, _ = keeper(card(now, **change), now)
    assert k.review_card_rule(position(now), now) is None
    expired = card(now, bias="short").model_copy(update={"expires_at": now})
    k._card = lambda _: expired
    assert k.review_card_rule(position(now), now) is None


@pytest.mark.asyncio
async def test_card_short_position_short_signal_long_keeps_original_model_rule(
    now: datetime,
) -> None:
    transport = MockJevTransport(answer=close_p(0.11))
    k = JevKeeper(
        JevDecisionModel(transport, FAST, timeout_ms=200),
        FAST,
        lambda _: features(now),
        lambda _: card(now, bias="short"),
    )
    pos = position(now, side="short")
    signal = c.AgainstSignalEvent.model_validate(
        {
            "symbol": "BTCUSDT",
            "ts": now,
            "kind": "crowded_reversal",
            "side": "long",
            "desc": "合成拥挤反向信号",
            "strong": False,
        }
    )
    k.note_against(pos.position_id, signal, one_shot=True)
    verdict = await k.review(pos, now)
    assert verdict.action == "hold" and verdict.p_close == 0.11
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_queued_callback_reads_latest_card_not_superseded_snapshot(
    tmp_path: Path, now: datetime
) -> None:
    app = _app(tmp_path, now)
    ex = FakeExecutor()
    source = [card(now, bias="short")]
    k, _ = keeper(source[0], now)
    k._card = lambda _: source[0]
    app.m = dataclasses.replace(app.m, executor=ex, keeper=k)
    runner = Runner(app)
    runner.positions = [position(now)]
    runner._card_published()
    source[0] = card(now, bias="long")  # task尚未获调度，旧反卡不得平。
    await drain(runner)
    assert ex.closed == []
    source[0] = card(now, bias="short")
    runner._card_published()
    runner._card_published()
    await drain(runner)
    assert ex.closed == [("pos-1", CloseReason.KEEPER)]


class PendingExecutor(FakeExecutor):
    def __init__(self, pos: c.PositionRecord, result: c.PositionStatus) -> None:
        super().__init__()
        self.held = [pos]
        self.result = result

    def live_positions(self) -> list[c.PositionRecord]:
        return self.held

    async def close(self, position_id: str, reason: CloseReason) -> None:
        await super().close(position_id, reason)
        self.held = [self.held[0].model_copy(update={"status": self.result})]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [c.PositionStatus.OPEN, c.PositionStatus.CLOSING])
async def test_return_without_terminal_evidence_keeps_tracking(
    tmp_path: Path, now: datetime, status: c.PositionStatus
) -> None:
    app = _app(tmp_path, now)
    pos = position(now)
    ex = PendingExecutor(pos, status)
    k, _ = keeper(card(now, bias="short"), now)
    forgotten: list[str] = []
    k.forget = forgotten.append
    app.m = dataclasses.replace(app.m, executor=ex, keeper=k)
    runner = Runner(app)
    runner.positions = [pos]
    await runner._review_card_rules()
    await drain(runner)
    assert runner.positions[0].status == status and forgotten == []
    if status is c.PositionStatus.CLOSING:
        await runner._review_card_rules()
        await drain(runner)
        await runner.close(pos, CloseReason.KEEPER)
        assert len(ex.closed) == 1
        await runner.refresh_positions()
        assert runner.positions[0].status is c.PositionStatus.CLOSING
    else:
        await runner._review_card_rules()  # 未申请成功OPEN允许下轮重试。
        await drain(runner)
        assert len(ex.closed) == 2


@pytest.mark.asyncio
async def test_model_inflight_does_not_block_card_rule_or_repeat_close(
    tmp_path: Path, now: datetime
) -> None:
    started, release = asyncio.Event(), asyncio.Event()

    class BlockingTransport(MockJevTransport):
        async def evaluate(self, *args: Any, **kwargs: Any) -> Any:
            started.set()
            await release.wait()
            return await super().evaluate(*args, **kwargs)

    source = [card(now, bias="long")]
    transport = BlockingTransport(answer=close_p(0.99))
    k = JevKeeper(
        JevDecisionModel(transport, FAST, timeout_ms=2000),
        FAST,
        lambda _: features(now),
        lambda _: source[0],
    )
    ex, app = FakeExecutor(), _app(tmp_path, now)
    app.m = dataclasses.replace(app.m, executor=ex, keeper=k)
    runner = Runner(app)
    pos = position(now)
    runner.positions = [pos]
    task = asyncio.create_task(runner._review(pos))
    await started.wait()
    source[0] = card(now, bias="short")
    await runner._review_card_rules()
    await drain(runner)
    assert ex.closed == [("pos-1", CloseReason.KEEPER)]
    release.set()
    await task
    assert len(ex.closed) == 1


def test_card_notification_failure_cannot_undo_published_card(now: datetime) -> None:
    commander = BattleCardCommander.__new__(BattleCardCommander)
    commander._card = card(now, bias="short")

    def fail() -> None:
        raise RuntimeError("synthetic callback failure")

    commander.set_on_card(fail)
    commander._notify_card()
    assert (
        commander._card is not None and commander._card.for_symbol("BTCUSDT").bias is c.Bias.SHORT
    )


@pytest.mark.asyncio
async def test_slow_first_close_does_not_block_second_position(
    tmp_path: Path, now: datetime
) -> None:
    release, both = asyncio.Event(), asyncio.Event()

    class SlowExecutor(FakeExecutor):
        async def close(self, position_id: str, reason: CloseReason) -> None:
            await super().close(position_id, reason)
            if len(self.closed) == 2:
                both.set()
            await release.wait()

    ex, app = SlowExecutor(), _app(tmp_path, now)
    k, _ = keeper(card(now, bias="short"), now)
    app.m = dataclasses.replace(app.m, executor=ex, keeper=k)
    runner = Runner(app)
    runner.positions = [position(now), position(now, position_id="pos-2")]
    task = asyncio.create_task(runner._review_card_rules())
    await asyncio.wait_for(both.wait(), 1)
    assert {pid for pid, _ in ex.closed} == {"pos-1", "pos-2"}
    release.set()
    await task
    await drain(runner)


@pytest.mark.asyncio
async def test_watch_only_direction_is_not_opposite_trade_card(now: datetime) -> None:
    k, model = keeper(card(now, bias="short", allowed_signals=[]), now)
    assert k.review_card_rule(position(now), now) is None
    verdict = await k.review(position(now), now)
    assert verdict.action == "hold"  # 没行情维持现状，规则没有误平。
    model.ask.assert_not_awaited()


@pytest.mark.asyncio
async def test_hard_rule_log_is_not_model_probability(tmp_path: Path, now: datetime) -> None:
    from jiaoyi.core.recorder import RoleRecorder
    from jiaoyi.core.store import JsonlDecisionLog

    journal = JsonlDecisionLog(tmp_path)
    recorder = RoleRecorder(c.Role.KEEPER, journal)
    latest = card(now, bias="short").model_copy(update={"card_id": "new-card-rule"})
    k, _ = keeper(latest, now)
    k._rec = recorder
    await k.review(position(now, card_id="old-entry-card"), now)
    rows = journal.read_day(now.date().isoformat())
    assert len(rows) == 1
    assert rows[0].role is c.Role.KEEPER and rows[0].outcome is c.Outcome.CLOSE
    assert "规则判平，等待执行" in rows[0].summary
    assert rows[0].output["p_close"] is None and rows[0].output["model_called"] is False
    assert rows[0].output["card_rule"]["card_bias"] == "short"
    assert rows[0].card_id == "new-card-rule"
    assert rows[0].output["card_rule"]["entry_card_id"] == "old-entry-card"


@pytest.mark.asyncio
async def test_startup_hard_check_runs_before_slow_watch_and_periodic(
    tmp_path: Path, now: datetime
) -> None:
    closed, watch = asyncio.Event(), asyncio.Event()

    class StartupExecutor(FakeExecutor):
        async def close(self, position_id: str, reason: CloseReason) -> None:
            await super().close(position_id, reason)
            closed.set()

    app, ex = _app(tmp_path, now), StartupExecutor()
    ex.held = [position(now)]
    k, _ = keeper(card(now, bias="short"), now)
    app.m = dataclasses.replace(app.m, executor=ex, keeper=k)
    app.start = AsyncMock()
    runner = Runner(app)

    async def slow_watch() -> None:
        await watch.wait()

    runner.sync_watch = slow_watch
    task = asyncio.create_task(runner.run())
    try:
        await asyncio.wait_for(closed.wait(), 1)
        assert ex.closed == [("pos-1", CloseReason.KEEPER)]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        if runner._tasks:
            await drain(runner)


@pytest.mark.asyncio
async def test_opening_to_open_immediately_wakes_rule(tmp_path: Path, now: datetime) -> None:
    app, ex = _app(tmp_path, now), FakeExecutor()
    k, _ = keeper(card(now, bias="short"), now)
    app.m = dataclasses.replace(app.m, executor=ex, keeper=k)
    runner = Runner(app)
    runner.positions = [position(now, status="opening")]
    ex.held = [position(now)]
    await runner.refresh_positions()
    await drain(runner)
    assert ex.closed == [("pos-1", CloseReason.KEEPER)]


@pytest.mark.asyncio
async def test_slow_opposite_close_does_not_delay_other_keeper_review(
    tmp_path: Path, now: datetime
) -> None:
    release, started = asyncio.Event(), asyncio.Event()

    class SlowExecutor(FakeExecutor):
        async def close(self, position_id: str, reason: CloseReason) -> None:
            await super().close(position_id, reason)
            started.set()
            await release.wait()

    cd = card(now, bias="short")
    cd = cd.model_copy(
        update={
            "symbols": [
                cd.symbols[0],
                cd.symbols[0].model_copy(update={"symbol": "ETHUSDT", "bias": c.Bias.LONG}),
            ]
        }
    )
    transport = MockJevTransport(answer=close_p(0.1))
    k = JevKeeper(
        JevDecisionModel(transport, FAST, timeout_ms=200),
        FAST,
        lambda _: features(now),
        lambda _: cd,
    )
    app, ex = _app(tmp_path, now), SlowExecutor()
    app.m = dataclasses.replace(app.m, executor=ex, keeper=k)
    ex.held = [position(now), position(now, position_id="pos-other", symbol="ETHUSDT")]
    runner = Runner(app)
    await asyncio.wait_for(runner.keeper_tick(), 1)
    await asyncio.wait_for(started.wait(), 1)
    assert len(transport.calls) == 1  # 其他仓未等反向仓平完就完成普通复核。
    await asyncio.wait_for(runner.keeper_tick(), 1)
    assert len(ex.closed) == 1 and "pos-1" in runner._card_rule_queued
    release.set()
    await drain(runner)
    assert runner._card_rule_queued == set()


@pytest.mark.asyncio
async def test_queued_rule_changed_to_same_direction_does_not_skip_normal_review(
    tmp_path: Path, now: datetime
) -> None:
    source = [card(now, bias="short")]
    transport = MockJevTransport(answer=close_p(0.1))
    k = JevKeeper(
        JevDecisionModel(transport, FAST, timeout_ms=200),
        FAST,
        lambda _: features(now),
        lambda _: source[0],
    )
    app, ex = _app(tmp_path, now), FakeExecutor()
    app.m = dataclasses.replace(app.m, executor=ex, keeper=k)
    ex.held = [position(now)]
    runner = Runner(app)
    runner.positions = list(ex.held)
    assert await runner._review_card_rules() == {"pos-1"}  # 创建后台task，尚未让其运行。
    source[0] = card(now, bias="long")
    await runner.keeper_tick()
    await drain(runner)
    assert ex.closed == [] and len(transport.calls) == 1
