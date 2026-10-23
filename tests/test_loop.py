from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from jiaoyi import contracts as c
from jiaoyi.app import App
from jiaoyi.contracts.position import CloseReason
from jiaoyi.core.config import load_app_config, load_risk_config
from jiaoyi.loop import Runner
from tests import fastloop_helpers as h
from tests.conftest import ROOT, load_example

RISK = ROOT / "config" / "risk.example.toml"
APP = ROOT / "config" / "app.example.toml"


def _app(tmp_path: Path, now: datetime) -> App:
    cfg = load_app_config(APP).model_copy(update={"data_dir": str(tmp_path)})
    return App(load_risk_config(RISK), cfg, clock=lambda: now, offline=True)


def _position(**over: Any) -> c.PositionRecord:
    return c.PositionRecord.model_validate({**load_example("position_record"), **over})


class FakeExecutor:
    def __init__(self, fail_open: bool = False) -> None:
        self.opened: list[str] = []
        self.closed: list[tuple[str, CloseReason]] = []
        self.fail_open = fail_open
        self.held: list[c.PositionRecord] = []

    async def open(self, intent: c.OrderIntent) -> c.PositionRecord:
        if self.fail_open:
            raise RuntimeError("hummingbot-api 503")
        self.opened.append(intent.intent_id)
        return _position()

    async def close(self, position_id: str, reason: CloseReason) -> None:
        self.closed.append((position_id, reason))

    async def positions(self) -> list[c.PositionRecord]:
        return self.held

    async def account(self) -> c.AccountState:
        return c.AccountState.model_validate(load_example("account_state"))


class FakeRisk:
    def __init__(self, approve: bool) -> None:
        self.approve = approve

    def review(self, *args: object) -> c.RiskVerdict:
        name = "risk_verdict_approved" if self.approve else "risk_verdict_rejected"
        return c.RiskVerdict.model_validate(load_example(name))


class FakeGate:
    def __init__(self) -> None:
        self.asked = 0

    async def evaluate(
        self, signal: c.CandidateSignal, card: c.BattleCard, now: datetime
    ) -> c.JevResponse:
        self.asked += 1
        return c.JevResponse.model_validate(load_example("jev_entry_response"))


class FakeCards:
    def __init__(self, card: c.BattleCard | None, reason: str | None = None) -> None:
        self.card = card
        self.reason = reason
        self.refreshed: list[str] = []

    def current(self, now: datetime) -> c.BattleCard | None:
        return self.card

    def refresh_reason(
        self, now: datetime, pool: c.SymbolPool, prices: Mapping[str, float]
    ) -> str | None:
        return self.reason

    async def refresh(self, pool: c.SymbolPool, reason: str) -> c.BattleCard:
        self.refreshed.append(reason)
        assert self.card is not None
        return self.card


class FakeKeeper:
    def __init__(self, action: str = "hold") -> None:
        self.action = action
        self.reviewed: list[str] = []
        self.forgotten: list[str] = []

    async def review(self, position: c.PositionRecord, now: datetime) -> c.KeeperVerdict:
        self.reviewed.append(position.position_id)
        return c.KeeperVerdict(
            position_id=position.position_id,
            ts=now,
            action="close" if self.action == "close" else "hold",
            p_close=0.9 if self.action == "close" else 0.1,
            consecutive=2,
        )

    def forget(self, position_id: str) -> None:
        self.forgotten.append(position_id)


class FakePatrol:
    def __init__(self, action: c.PatrolAction | None = None) -> None:
        self.action = action
        self.urgent: set[str] = set()

    def check(
        self, event: c.MarketEvent, positions: Sequence[c.PositionRecord]
    ) -> c.PatrolAction | None:
        return self.action


async def test_approved_signal_opens(tmp_path: Path, now: datetime) -> None:
    app = _app(tmp_path, now)
    ex, gate = FakeExecutor(), FakeGate()
    app.m = dataclasses.replace(
        app.m, executor=ex, gate=gate, risk=FakeRisk(True), cards=FakeCards(h.card(now))
    )
    r = Runner(app)
    await r.handle_signal(h.signal(now))
    assert gate.asked == 1 and ex.opened == ["int-0001"]
    assert [p.position_id for p in r.positions] == ["pos-0001"]


async def test_rejected_or_no_card_never_opens(tmp_path: Path, now: datetime) -> None:
    app = _app(tmp_path, now)
    ex, gate = FakeExecutor(), FakeGate()
    app.m = dataclasses.replace(
        app.m, executor=ex, gate=gate, risk=FakeRisk(False), cards=FakeCards(h.card(now))
    )
    await Runner(app).handle_signal(h.signal(now))
    assert ex.opened == []
    app.m = dataclasses.replace(app.m, risk=FakeRisk(True), cards=FakeCards(None))
    await Runner(app).handle_signal(h.signal(now))
    assert ex.opened == [] and gate.asked == 1  # 没卡连 Jev 都不问


async def test_open_failure_alerts(tmp_path: Path, now: datetime) -> None:
    app = _app(tmp_path, now)
    sent: list[c.Alert] = []

    class Alerter:
        async def send(self, alert: c.Alert) -> None:
            sent.append(alert)

    app.m = dataclasses.replace(
        app.m,
        executor=FakeExecutor(fail_open=True),
        gate=FakeGate(),
        risk=FakeRisk(True),
        cards=FakeCards(h.card(now)),
        alerter=Alerter(),
    )
    r = Runner(app)
    await r.handle_signal(h.signal(now))
    assert r.positions == [] and "开仓失败" in sent[0].title


async def test_patrol_action_closes_and_keeper_forgets(tmp_path: Path, now: datetime) -> None:
    app = _app(tmp_path, now)
    ex, keeper = FakeExecutor(), FakeKeeper()
    patrol = FakePatrol(c.PatrolAction(ts=now, symbols=["BTCUSDT"], reason="1 分钟急跌"))
    patrol.urgent.add("pos-0001")
    app.m = dataclasses.replace(app.m, executor=ex, keeper=keeper, patrol=patrol)
    r = Runner(app)
    r.positions = [_position(), _position(position_id="pos-0002", symbol="ETHUSDT")]
    await r.on_event(c.MarkPrice.model_validate(_mark(now)))
    await asyncio.gather(*r._tasks)
    assert ex.closed == [("pos-0001", CloseReason.PATROL)]
    assert keeper.forgotten == ["pos-0001"] and not patrol.urgent
    assert [p.position_id for p in r.positions] == ["pos-0002"]


def _mark(now: datetime) -> dict[str, Any]:
    return {
        "symbol": "BTCUSDT",
        "ts": now.isoformat(),
        "mark_price": 67000.0,
        "index_price": 67000.0,
        "funding_rate": 0.0001,
    }


async def test_urgent_review_closes_via_keeper(tmp_path: Path, now: datetime) -> None:
    app = _app(tmp_path, now)
    ex, keeper, patrol = FakeExecutor(), FakeKeeper("close"), FakePatrol()
    patrol.urgent |= {"pos-0001", "pos-gone"}
    app.m = dataclasses.replace(app.m, executor=ex, keeper=keeper, patrol=patrol)
    r = Runner(app)
    r.positions = [_position()]
    await r.urgent_tick()
    assert keeper.reviewed == ["pos-0001"]
    assert ex.closed == [("pos-0001", CloseReason.KEEPER)]
    assert not patrol.urgent  # 不在持仓里的也清掉


async def test_keeper_tick_reads_positions_and_holds(tmp_path: Path, now: datetime) -> None:
    app = _app(tmp_path, now)
    ex, keeper = FakeExecutor(), FakeKeeper("hold")
    ex.held = [_position(), _position(position_id="pos-c", status="closed")]
    app.m = dataclasses.replace(app.m, executor=ex, keeper=keeper)
    r = Runner(app)
    await r.keeper_tick()
    assert keeper.reviewed == ["pos-0001"] and ex.closed == []


async def test_slow_tick_schedules_meeting_only_with_real_cards(
    tmp_path: Path, now: datetime
) -> None:
    app = _app(tmp_path, now)
    app.pool = c.SymbolPool.model_validate(load_example("symbol_pool"))
    cards = FakeCards(h.card(now), reason="card_expiring")
    app.m = dataclasses.replace(app.m, cards=cards)
    r = Runner(app)
    r._last_screen = now
    await r.slow_tick()
    await asyncio.sleep(0)
    assert cards.refreshed == ["card_expiring"]

    # 没理由、离上次开会也不够久 → 不开
    cards.reason = None
    await r.slow_tick()
    await asyncio.sleep(0)
    assert cards.refreshed == ["card_expiring"]

    # 到了 slow_loop_interval_min → 定时开会
    r._last_card_at = now - timedelta(minutes=app.app_cfg.slow_loop_interval_min)
    await r.slow_tick()
    await asyncio.sleep(0)
    assert cards.refreshed == ["card_expiring", "scheduled"]


async def test_backfill_skipped_for_stub_feed_and_runs_for_real(
    tmp_path: Path, now: datetime
) -> None:
    app = _app(tmp_path, now)
    calls: list[str] = []

    async def fetch(sym: str) -> list[c.Kline]:
        calls.append(sym)
        if sym == "ETHUSDT":
            raise RuntimeError("超时")
        return []

    r = Runner(app, fetch_klines=fetch)
    await r.backfill(["BTCUSDT"])
    assert calls == []  # 行情是桩

    class Feed:
        async def subscribe(self, symbols: Sequence[str]) -> None: ...

        async def events(self) -> AsyncIterator[c.MarketEvent]:
            if False:
                yield  # pragma: no cover

        async def close(self) -> None: ...

    app.m = dataclasses.replace(app.m, feed=Feed())
    await Runner(app, fetch_klines=fetch).backfill(["BTCUSDT", "ETHUSDT"])
    assert sorted(calls) == ["BTCUSDT", "ETHUSDT"]  # 一个失败不影响别的


async def test_run_until_cancelled_then_stops(tmp_path: Path, now: datetime) -> None:
    app = _app(tmp_path, now)
    r = Runner(app)
    task = asyncio.create_task(r.run())
    for _ in range(20):
        await asyncio.sleep(0)
        if app.started:
            break
    assert app.started
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not app.started


async def test_concurrent_signals_respect_max_open_positions(tmp_path: Path, now: datetime) -> None:
    app = _app(tmp_path, now)

    class SlowExecutor(FakeExecutor):
        async def open(self, intent: c.OrderIntent) -> c.PositionRecord:
            await asyncio.sleep(0.01)  # 开仓要等成交，期间另一个信号到达
            return await super().open(intent)

        async def account(self) -> c.AccountState:
            acc = await super().account()
            return acc.model_copy(update={"open_positions": len(self.opened)})

    class OneSlot:
        """max_open_positions=1 / 敞口只够一笔：已有持仓就拒。"""

        def review(self, *args: object) -> c.RiskVerdict:
            account = args[3]
            assert isinstance(account, c.AccountState)
            ok = account.open_positions == 0
            name = "risk_verdict_approved" if ok else "risk_verdict_rejected"
            return c.RiskVerdict.model_validate(load_example(name))

    ex = SlowExecutor()
    app.m = dataclasses.replace(
        app.m, executor=ex, gate=FakeGate(), risk=OneSlot(), cards=FakeCards(h.card(now))
    )
    r = Runner(app)
    await asyncio.gather(r.handle_signal(h.signal(now)), r.handle_signal(h.signal(now)))
    assert len(ex.opened) == 1


async def test_stuck_close_does_not_block_market_events(tmp_path: Path, now: datetime) -> None:
    app = _app(tmp_path, now)
    release = asyncio.Event()

    class StuckExecutor(FakeExecutor):
        async def close(self, position_id: str, reason: CloseReason) -> None:
            self.closed.append((position_id, reason))
            await release.wait()

    ex = StuckExecutor()
    patrol = FakePatrol(c.PatrolAction(ts=now, symbols=["BTCUSDT"], reason="1 分钟急跌"))
    app.m = dataclasses.replace(app.m, executor=ex, keeper=FakeKeeper(), patrol=patrol)
    r = Runner(app)
    r.positions = [_position(), _position(position_id="pos-0002")]
    ev = c.MarkPrice.model_validate(_mark(now))
    await asyncio.wait_for(r.on_event(ev), 0.5)  # 平仓卡住，行情照常处理
    await asyncio.sleep(0)
    await asyncio.wait_for(r.on_event(ev), 0.5)  # 同一笔不重复提交
    await asyncio.sleep(0)
    assert sorted(pid for pid, _ in ex.closed) == ["pos-0001", "pos-0002"]  # 两笔并行
    release.set()
    await asyncio.gather(*r._tasks)
    assert r.positions == []
