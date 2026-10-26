"""独立回归：只更新市场背景，不改已有交易授权，全部外部边界使用离线替身。"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import sys
import threading
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from zoneinfo import ZoneInfo

import pytest

from jiaoyi.contracts import BattleCard, FastLoopConfig, MarketView, SymbolPool, SymbolPoolEntry
from jiaoyi.core.hotconfig import HOT_FAST, patch_inplace
from jiaoyi.core.store import JsonlDecisionLog
from jiaoyi.gate.questions import entry_snapshot, entry_snapshot_one, hold_snapshot
from jiaoyi.loop import Runner
from jiaoyi.risk.guard import RiskGuardImpl
from jiaoyi.sentinel.regime_flip import RegimeFlip, RegimeReference
from jiaoyi.slow.commander import (
    Analysis,
    BattleCardCommander,
    CardRefreshError,
    EventRules,
    Pick,
    PickResult,
)
from jiaoyi.slow.screener import CodeSymbolScreener
from jiaoyi.slow.tradingagents_engine import TradingAgentsEngine, TradingAgentsSettings
from tests import fastloop_helpers as h
from tests.test_limit_pullback import TICK_LOT, _ChaseExec
from tests.test_loop import FakeCards, FakeExecutor, FakeRisk, _app, _position
from tests.test_regime_flip import bar, records
from tests.test_slow_commander import RISK, card_symbol, entry, pool
from tests.test_slow_screener import FakeSource, stat


class OfflineResearch:
    """只模拟引擎边界：记录调用、可控阻塞和失败，不重写生产调度规则。"""

    def __init__(self) -> None:
        self.market_calls = 0
        self.market_input: list[SymbolPoolEntry] = []
        self.pick_calls = 0
        self.pick_started = asyncio.Event()
        self.pick_release = asyncio.Event()
        self.pick_release.set()
        self.symbol_calls: list[tuple[str, str]] = []
        self.direction = "看涨"
        self.market_text = "当前市场：上涨扩散，BTC +2%"
        self.market_started = asyncio.Event()
        self.market_release = asyncio.Event()
        self.market_release.set()
        self.symbol_started = asyncio.Event()
        self.symbol_release = asyncio.Event()
        self.symbol_release.set()
        self.problem: Exception | None = None
        self.aborted: list[str] = []

    async def pick(
        self,
        candidates: Sequence[SymbolPoolEntry],
        max_n: int,
        previous: BattleCard | None,
        pool: Sequence[SymbolPoolEntry] = (),
    ) -> PickResult:
        self.pick_calls += 1
        self.pick_started.set()
        await self.pick_release.wait()
        return PickResult(
            "旧研究背景：震荡偏弱，BTC -1%",
            [Pick(e.symbol, "成交量 +10%") for e in candidates[:max_n]],
            cost_usd=0.01,
            regime_direction="看跌",
        )

    async def refresh_market(
        self,
        pool: Sequence[SymbolPoolEntry],
        previous: MarketView | None,
        *,
        brief_text: str = "",
    ) -> PickResult:
        self.market_calls += 1
        self.market_input = list(pool)
        self.market_started.set()
        await self.market_release.wait()
        if self.problem:
            raise self.problem
        return PickResult(
            self.market_text,
            [],
            cost_usd=0.03,
            regime_direction=self.direction,  # type: ignore[arg-type]
        )

    async def analyze(
        self, candidate: SymbolPoolEntry, regime: str, pick_reason: str = ""
    ) -> Analysis:
        self.symbol_calls.append((candidate.symbol, regime))
        self.symbol_started.set()
        await self.symbol_release.wait()
        return Analysis(card_symbol(candidate.symbol), 0.02)

    def partial_cost(self, key: str) -> float | None:
        return 0.013

    def abort(self, key: str) -> None:
        self.aborted.append(key)


async def research(tmp: Path, now: datetime, **kwargs: object):
    clock = [now]
    engine = OfflineResearch()
    log = JsonlDecisionLog(tmp / "log")
    commander = BattleCardCommander(
        engine,
        RISK,
        log,
        clock=lambda: clock[0],
        card_path=tmp / "battle_card.json",
        budget_path=tmp / "budget.json",
        events=EventRules(enabled=True, triggers=frozenset({"signal_against_card"})),
        **kwargs,  # type: ignore[arg-type]
    )
    commander.set_regime_price(lambda: 100.0)

    async def snapshot(original: SymbolPool, observed_at: datetime) -> SymbolPool:
        return original.model_copy(update={"ts": observed_at})

    commander.set_market_snapshot(snapshot)
    candidates = pool(now, entry("AAUSDT"), entry("BBUSDT"))
    await commander.refresh(candidates, "scheduled")
    return commander, engine, clock, candidates, log


def authority(card: BattleCard) -> dict:
    return card.model_dump(exclude={"current_market"})


async def test_market_only_runs_analysis_without_coin_meetings_or_authority_changes(
    tmp_path: Path, now: datetime
) -> None:
    commander, engine, clock, candidates, _ = await research(tmp_path, now)
    before = commander.current(now)
    assert before is not None
    original = authority(before)
    original_pool = candidates.model_dump()
    due = commander._scheduled_due
    last_refresh = commander._last_refresh
    clock[0] += timedelta(minutes=5)
    view = await commander.refresh_market(candidates)
    after = commander.current(clock[0])
    assert after is not None
    assert engine.market_calls == 1 and engine.pick_calls == 1
    assert len(engine.symbol_calls) == 2
    assert authority(after) == original
    assert candidates.model_dump() == original_pool
    assert commander._scheduled_due == due and commander._last_refresh == last_refresh
    assert view == commander.latest_market_view == after.current_market
    assert view.regime == engine.market_text and view.source == "market_only"
    assert view.observed_at == clock[0]
    # 外部存储中的研究内容也不能被市场背景刷新悄悄重签。
    saved = json.loads((tmp_path / "battle_card.json").read_text())
    assert saved["card"]["version"] == before.version
    assert saved["card"]["symbols"] == before.model_dump(mode="json")["symbols"]


async def test_future_symbol_meeting_uses_latest_context_and_preserves_other_source(
    tmp_path: Path, now: datetime
) -> None:
    commander, engine, clock, candidates, _ = await research(tmp_path, now)
    original = commander.current(now)
    assert original is not None
    old_aa, old_bb = original.for_symbol("AAUSDT"), original.for_symbol("BBUSDT")
    clock[0] += timedelta(minutes=5)
    latest = await commander.refresh_market(candidates)
    await commander.refresh_symbol(candidates, "AAUSDT", "signal_against_card")
    current = commander.current(clock[0])
    assert current is not None
    aa, bb = current.for_symbol("AAUSDT"), current.for_symbol("BBUSDT")
    assert aa is not None and old_aa is not None
    assert engine.symbol_calls[-1] == ("AAUSDT", latest.regime)
    assert aa.source_regime == latest.regime
    assert aa.source_market_view_id == latest.market_view_id
    assert bb == old_bb
    assert old_aa.source_regime != latest.regime
    assert current.expires_at == original.expires_at


@pytest.mark.parametrize("mode", ["one", "six", "keeper"])
async def test_decision_snapshots_include_current_market_and_original_research(
    tmp_path: Path, now: datetime, mode: str
) -> None:
    commander, engine, clock, candidates, _ = await research(tmp_path, now)
    before = commander.current(now)
    assert before is not None
    clock[0] += timedelta(minutes=5)
    newest = await commander.refresh_market(candidates)
    current = commander.current(clock[0])
    assert current is not None
    feature = h.features(clock[0]).model_copy(update={"symbol": "AAUSDT"})
    signal = h.signal(clock[0], f=feature).model_copy(update={"symbol": "AAUSDT"})
    if mode == "one":
        snapshot = entry_snapshot_one(signal, current)
    elif mode == "six":
        snapshot = entry_snapshot(signal, current)
    else:
        position = _position(symbol="AAUSDT", opened_at=now)
        snapshot = hold_snapshot(position, feature, current, clock[0])
    payload = json.loads(snapshot)
    # 实际交给Jev的JSON必须同时区分「当前大盘」与「原卡研究背景」。
    assert newest.regime in snapshot and before.regime in snapshot
    assert newest.market_view_id in snapshot
    assert payload["card"]["bias"] == before.for_symbol("AAUSDT").bias.value
    assert len(engine.symbol_calls) == 2


async def test_single_coin_and_market_meetings_run_concurrently(
    tmp_path: Path, now: datetime
) -> None:
    commander, engine, clock, candidates, _ = await research(tmp_path, now)
    clock[0] += timedelta(minutes=5)
    engine.symbol_started.clear()
    engine.symbol_release.clear()
    symbol_task = asyncio.create_task(
        commander.refresh_symbol(candidates, "AAUSDT", "signal_against_card")
    )
    try:
        await asyncio.wait_for(engine.symbol_started.wait(), 1)
        view = await asyncio.wait_for(commander.refresh_market(candidates), 1)
        assert not symbol_task.done()
        assert commander.latest_market_view == view
    finally:
        engine.symbol_release.set()
        await symbol_task
    assert commander.current(clock[0]).current_market == view
    # 在市场刷新之前启动的研究必须保留自己的旧背景。
    aa = commander.current(clock[0]).for_symbol("AAUSDT")
    assert aa.source_regime == engine.symbol_calls[-1][1]
    assert aa.source_regime != view.regime


@pytest.mark.parametrize("phase", ["pick", "coin"])
@pytest.mark.parametrize("new_direction", ["看涨", "看跌"])
async def test_full_round_finishing_late_does_not_replace_newer_market_view(
    tmp_path: Path, now: datetime, phase: str, new_direction: str
) -> None:
    commander, engine, clock, candidates, log = await research(tmp_path, now)
    original_ref = commander.regime_reference
    engine.symbol_started.clear()
    engine.pick_started.clear()
    gate = engine.pick_release if phase == "pick" else engine.symbol_release
    started = engine.pick_started if phase == "pick" else engine.symbol_started
    gate.clear()
    engine.direction = new_direction
    clock[0] += timedelta(minutes=5)
    old_round = asyncio.create_task(commander.refresh(candidates, "manual"))
    try:
        await asyncio.wait_for(started.wait(), 1)
        clock[0] += timedelta(minutes=5)
        newest = await commander.refresh_market(candidates)
        new_ref = commander.regime_reference
    finally:
        gate.set()
        await old_round
    assert commander.latest_market_view == newest
    assert commander.current(clock[0]).current_market == newest
    assert commander.current(clock[0]).regime_direction == "看跌"
    cards = [
        record
        for record in log.read_day(now.date().isoformat())
        if "total_cost_usd" in record.output
    ]
    # 出卡审计的历史方向与同条记录里的当前大盘必须分别来自各自的观点。
    assert cards[-1].output["regime_direction"] == "看跌"
    assert cards[-1].output["market_view"]["regime_direction"] == new_direction
    assert commander.regime_reference == new_ref
    assert commander.regime_reference.direction == newest.regime_direction
    if new_direction == "看跌":
        assert commander.regime_reference == original_ref


async def test_duplicate_market_requests_share_one_inflight_analysis(
    tmp_path: Path, now: datetime
) -> None:
    commander, engine, _, candidates, _ = await research(tmp_path, now)
    engine.market_release.clear()
    first = asyncio.create_task(commander.refresh_market(candidates))
    try:
        await asyncio.wait_for(engine.market_started.wait(), 1)
        second = asyncio.create_task(commander.refresh_market(candidates))
        await asyncio.sleep(0)
        engine.market_release.set()
        a, b = await asyncio.gather(first, second)
        assert a == b and engine.market_calls == 1
    finally:
        engine.market_release.set()
        await asyncio.gather(first, return_exceptions=True)


async def test_market_started_first_but_finishing_late_is_discarded_and_charged(
    tmp_path: Path, now: datetime
) -> None:
    commander, engine, clock, candidates, log = await research(tmp_path, now)
    engine.market_release.clear()
    earlier_market = asyncio.create_task(commander.refresh_market(candidates))
    try:
        await asyncio.wait_for(engine.market_started.wait(), 1)
        clock[0] += timedelta(minutes=5)
        await commander.refresh(candidates, "manual")
        newer_view = commander.latest_market_view
        newer_ref = commander.regime_reference
        newer_card = authority(commander.current(clock[0]))
        engine.market_release.set()
        await earlier_market
        assert commander.latest_market_view == newer_view
        assert commander.regime_reference == newer_ref
        assert authority(commander.current(clock[0])) == newer_card
        discarded = [
            r for r in log.read_day(now.date().isoformat()) if r.output.get("accepted") is False
        ]
        assert len(discarded) == 1 and discarded[0].cost_usd == pytest.approx(0.03)
    finally:
        engine.market_release.set()
        await asyncio.gather(earlier_market, return_exceptions=True)


@pytest.mark.parametrize("failure", ["error", "timeout", "cancel"])
async def test_failed_or_cancelled_refresh_preserves_market_card_and_reference(
    tmp_path: Path, now: datetime, failure: str
) -> None:
    commander, engine, clock, candidates, log = await research(tmp_path, now, symbol_timeout_s=0.03)
    clock[0] += timedelta(minutes=1)
    old_view = await commander.refresh_market(candidates)
    old_ref = commander.regime_reference
    original = authority(commander.current(clock[0]))
    old_cost = commander._stats.cost_usd
    clock[0] += timedelta(minutes=1)
    engine.market_started.clear()
    if failure == "error":
        engine.problem = RuntimeError("离线模拟失败")
    else:
        engine.market_release.clear()
    task = asyncio.create_task(commander.refresh_market(candidates))
    try:
        await asyncio.wait_for(engine.market_started.wait(), 1)
        if failure == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises((CardRefreshError, RuntimeError, TimeoutError)):
                await asyncio.wait_for(task, 1)
        assert commander.latest_market_view == old_view
        assert commander.regime_reference == old_ref
        assert authority(commander.current(clock[0])) == original
        assert commander._stats.cost_usd > old_cost
        assert engine.aborted or failure == "error"
    finally:
        engine.market_release.set()
        await asyncio.gather(task, return_exceptions=True)
    assert log.read_day(now.date().isoformat())


async def test_exhausted_daily_budget_prevents_any_market_analysis(
    tmp_path: Path, now: datetime
) -> None:
    commander, engine, _, candidates, _ = await research(tmp_path, now)
    commander._cost_limit = 0.001
    with pytest.raises((CardRefreshError, RuntimeError)):
        await commander.refresh_market(candidates)
    assert engine.market_calls == 0


async def test_market_snapshot_updates_statistics_without_reselecting_pool(
    tmp_path: Path, now: datetime
) -> None:
    app = _app(tmp_path, now)
    commander, engine, clock, candidates, _ = await research(tmp_path / "research", now)
    screener = CodeSymbolScreener(
        FakeSource(
            [
                stat("AAUSDT", range_24h_pct=9.0, range_hold_pct=4.0),
                stat("BBUSDT", range_24h_pct=8.0, range_hold_pct=3.0),
                stat("ZZUSDT", range_24h_pct=100.0),
            ]
        )
    )
    app.clock = lambda: clock[0]
    app.pool = candidates
    app.m = dataclasses.replace(app.m, cards=commander, screener=screener)
    runner = Runner(app)
    commander.set_regime_price(lambda: 102.0)
    previous_pool = candidates.model_dump()
    previous_card = authority(commander.current(now))
    clock[0] += timedelta(minutes=20)
    latest = await commander.refresh_market(candidates)
    assert [e.symbol for e in engine.market_input] == ["AAUSDT", "BBUSDT"]
    assert all(e.metrics for e in engine.market_input)
    assert latest.source_ts == clock[0]
    assert app.pool is candidates and candidates.model_dump() == previous_pool
    assert runner._last_screen is None and screener._last_run is None
    assert authority(commander.current(clock[0])) == previous_card


@pytest.mark.parametrize("bad_snapshot", ["stale", "membership", "error"])
async def test_invalid_fresh_snapshot_keeps_last_market_without_calling_model(
    tmp_path: Path, now: datetime, bad_snapshot: str
) -> None:
    commander, engine, clock, candidates, _ = await research(tmp_path, now)
    old_view, old_ref = commander.latest_market_view, commander.regime_reference
    clock[0] += timedelta(minutes=20)

    async def snapshot(original: SymbolPool, observed_at: datetime) -> SymbolPool:
        if bad_snapshot == "error":
            raise RuntimeError("行情暂不可用")
        if bad_snapshot == "membership":
            return pool(observed_at, entry("ZZUSDT"))
        return original

    commander.set_market_snapshot(snapshot)
    with pytest.raises((CardRefreshError, RuntimeError)):
        await commander.refresh_market(candidates)
    assert engine.market_calls == 0
    assert commander.latest_market_view == old_view and commander.regime_reference == old_ref


async def test_market_cost_across_midnight_charges_actual_amount_to_new_day(
    tmp_path: Path,
) -> None:
    before_midnight = datetime(2026, 10, 22, 23, 59, tzinfo=ZoneInfo(RISK.day_reset_tz))
    commander, engine, clock, candidates, _ = await research(
        tmp_path, before_midnight.astimezone(UTC)
    )
    engine.market_release.clear()
    task = asyncio.create_task(commander.refresh_market(candidates))
    try:
        await asyncio.wait_for(engine.market_started.wait(), 1)
        clock[0] += timedelta(minutes=2)
        engine.market_release.set()
        await task
        assert commander._stats.cost_usd == pytest.approx(0.03)
        saved = json.loads((tmp_path / "budget.json").read_text())
        assert saved["cost_usd"] == pytest.approx(0.03)
    finally:
        engine.market_release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("phase", ["jev", "account"])
@pytest.mark.parametrize("direction", ["看涨", "看跌"])
async def test_entry_rechecks_market_direction_after_await_and_allows_same_direction_text(
    tmp_path: Path, now: datetime, phase: str, direction: str
) -> None:
    app = _app(tmp_path, now)
    commander, engine, clock, candidates, _ = await research(tmp_path / "research", now)
    entered, release = asyncio.Event(), asyncio.Event()

    class Gate:
        async def evaluate(self, *args):
            if phase == "jev":
                entered.set()
                await release.wait()
            return h.jev()

    class Executor(FakeExecutor):
        async def account(self):
            if phase == "account":
                entered.set()
                await release.wait()
            return await super().account()

    executor = Executor()
    app.clock = lambda: clock[0]
    app.pool = candidates
    app.m = dataclasses.replace(
        app.m, cards=commander, gate=Gate(), executor=executor, risk=FakeRisk(True)
    )
    runner = Runner(app)
    commander.set_regime_price(lambda: 102.0)
    signal = h.signal(now).model_copy(update={"symbol": "AAUSDT"})
    task = asyncio.create_task(runner.handle_signal(signal))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        clock[0] += timedelta(seconds=1)
        engine.direction = direction
        await commander.refresh_market(candidates)
        release.set()
        await task
        assert bool(executor.opened) is (direction == "看跌")
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("direction", ["看涨", "看跌"])
async def test_signed_limit_intent_rechecks_market_direction_after_account_await(
    tmp_path: Path, now: datetime, direction: str
) -> None:
    original_market = MarketView(
        market_view_id="original-market",
        version=1,
        observed_at=now,
        regime="原始市场背景 -1%，上涨占比25%",
        regime_direction="看跌",
    )
    original_card = h.card(now).model_copy(update={"current_market": original_market})
    fast = h.FAST.model_copy(update={"entry_limit_pullback": True, "entry_limit_chase": True})
    risk = RiskGuardImpl(h.RISK, fast, h.Lots(TICK_LOT))
    signal = h.signal(now)
    verdict = risk.review(
        signal, h.jev(), original_card, h.account(now), now, market=signal.features
    )
    assert verdict.approved, verdict.reasons
    signed = verdict.intent
    assert signed is not None and signed.source_market_direction == "看跌"
    assert signed.order_type == "limit" and signed.limit_chase
    entered, release = asyncio.Event(), asyncio.Event()

    class Executor(_ChaseExec):
        async def account(self):
            entered.set()
            await release.wait()
            return h.account(now)

    app = _app(tmp_path, now)
    cards = FakeCards(original_card)
    executor = Executor(app.m.executor, now)
    app.m = dataclasses.replace(app.m, cards=cards, risk=risk, executor=executor)
    runner = Runner(app)
    task = asyncio.create_task(runner._chase(signed))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        next_market = original_market.model_copy(
            update={"market_view_id": "new-market", "version": 2, "regime_direction": direction}
        )
        cards.card = original_card.model_copy(update={"current_market": next_market})
        release.set()
        await task
        assert bool(executor.chased) is (direction == "看跌")
        assert signed.source_market_direction == "看跌"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_same_direction_keeps_original_detection_reference_and_survives_restart(
    tmp_path: Path, now: datetime
) -> None:
    commander, engine, clock, candidates, _ = await research(tmp_path, now)
    original = commander.regime_reference
    original_card = authority(commander.current(now))
    engine.direction = "看跌"
    commander.set_regime_price(lambda: 97.0)
    clock[0] += timedelta(minutes=5)
    same = await commander.refresh_market(candidates)
    assert same.observed_at == clock[0]
    assert commander.regime_reference == original
    restored = BattleCardCommander(
        OfflineResearch(),
        RISK,
        JsonlDecisionLog(tmp_path / "restored-log"),
        clock=lambda: clock[0],
        card_path=tmp_path / "battle_card.json",
        budget_path=tmp_path / "budget.json",
    )
    assert restored.regime_reference == original
    assert restored.latest_market_view == same
    assert authority(restored.current(clock[0])) == original_card


@pytest.mark.parametrize("empty_pool", [False, True])
async def test_real_market_entry_invokes_model_without_pick_or_coin_analysis(
    now: datetime, empty_pool: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Model:
        def __init__(self) -> None:
            self.schema = None
            self.prompts: list[str] = []

        def with_structured_output(self, schema):
            self.schema = schema
            return self

        def bind(self, **kwargs):
            return self

        def invoke(self, prompt, config):
            self.prompts.append(prompt)
            return self.schema(regime="BTC +2%，上涨占比 75%", regime_direction="看涨")

    model = Model()
    # CI 只装 dev，不要求可选 slow；模型边界用到的外部回调基类也用离线替身。
    callbacks = ModuleType("langchain_core.callbacks")
    callbacks.BaseCallbackHandler = object
    langchain = ModuleType("langchain_core")
    langchain.callbacks = callbacks
    monkeypatch.setitem(sys.modules, "langchain_core", langchain)
    monkeypatch.setitem(sys.modules, "langchain_core.callbacks", callbacks)
    # 绕过读取密钥的构造器，只替换模型边界，真实异步入口和解析仍照常执行。
    engine = TradingAgentsEngine.__new__(TradingAgentsEngine)
    engine._s = TradingAgentsSettings(hold="4h", hold_minutes=240)
    engine._today = lambda: now
    engine._news = None
    engine._brief = None
    engine._inflight = {}
    engine._inflight_lock = threading.Lock()
    monkeypatch.setattr(engine, "_graph", lambda *args, **kwargs: None)
    monkeypatch.setattr(engine, "_llm", lambda *args, **kwargs: model)

    async def forbidden(*args, **kwargs):
        raise AssertionError("独立市场更新不得选币或启动单币研究")

    monkeypatch.setattr(engine, "pick", forbidden)
    monkeypatch.setattr(engine, "analyze", forbidden)
    result = await engine.refresh_market(
        [] if empty_pool else [entry("AAUSDT")], None, brief_text="最新市场摘要独立标记"
    )
    assert len(model.prompts) == 1
    assert "最新市场摘要独立标记" in model.prompts[0]
    assert result.regime_direction == "看涨" and result.picks == []
    assert result.regime == "BTC +2%，上涨占比 75%"
    assert not engine._inflight


@pytest.mark.parametrize("up,down,px", [(30, 10, 102.0), (10, 30, 98.0)])
def test_neutral_reference_accepts_both_mirrored_emergences(
    now: datetime, up: int, down: int, px: float
) -> None:
    trigger = RegimeFlip(FastLoopConfig(regime_flip_confirm=2))
    ref = RegimeReference(ts=now, direction="中性", btc_px=100, regime_text="中性背景")
    trigger.arm(ref)
    assert trigger.step(now, up, down, 40, px).count == 1
    assert trigger.step(now + timedelta(minutes=5), up, down, 40, px).kind == "confirmed"
    assert trigger.reference == ref


def test_neutral_streak_cannot_combine_opposite_directions(now: datetime) -> None:
    trigger = RegimeFlip(FastLoopConfig(regime_flip_confirm=2))
    trigger.arm(RegimeReference(ts=now, direction="中性", btc_px=100, regime_text="中性"))
    assert trigger.step(now, 30, 10, 40, 102).count == 1
    switched = trigger.step(now + timedelta(minutes=5), 10, 30, 40, 98)
    assert switched.count == 1 and switched.kind != "confirmed"
    assert trigger.step(now + timedelta(minutes=10), 10, 30, 40, 98).kind == "confirmed"


async def test_busy_round_still_samples_and_can_refresh_only_market(
    tmp_path: Path, now: datetime
) -> None:
    app = _app(tmp_path, now)
    commander, engine, clock, candidates, _ = await research(tmp_path / "research", now)
    app.clock = lambda: clock[0]
    app.pool = candidates
    app.m = dataclasses.replace(app.m, cards=commander)
    sampled: list[str] = []

    async def fetch(symbol: str, start: datetime, end: datetime):
        sampled.append(symbol)
        return [bar(symbol, end - timedelta(hours=1), 100), bar(symbol, end, 102)]

    cfg = app.app_cfg.fast_loop
    patch_inplace(
        cfg,
        cfg.model_copy(update={"regime_flip_confirm": 1, "regime_flip_min_symbols": 1}),
        HOT_FAST,
    )
    runner = Runner(app, fetch_regime_klines=fetch)
    commander.set_regime_price(lambda: 102.0)
    runner._refreshing = True
    old_ref = commander.regime_reference
    clock[0] += timedelta(minutes=5)
    await runner.regime_flip_tick()
    if runner._tasks:
        await asyncio.gather(*tuple(runner._tasks))
    assert "BTCUSDT" in sampled and engine.market_calls == 1
    assert runner._refreshing  # 原单币/整轮标记不被market任务错误清空。
    assert engine.pick_calls == 1 and len(engine.symbol_calls) == 2
    assert old_ref is not None and old_ref.direction == "看跌"
    assert commander.regime_reference.direction == "看涨"
    event = records(tmp_path / "regime_flip")
    assert any(r["kind"] == "rerun_result" for r in event)


async def test_hot_disable_during_sampling_stops_market_call(tmp_path: Path, now: datetime) -> None:
    app = _app(tmp_path, now)
    commander, engine, clock, candidates, _ = await research(tmp_path / "research", now)
    app.clock = lambda: clock[0]
    app.pool = candidates
    app.m = dataclasses.replace(app.m, cards=commander)
    cfg = app.app_cfg.fast_loop
    patch_inplace(cfg, cfg.model_copy(update={"regime_flip_confirm": 1}), HOT_FAST)

    async def fetch(symbol: str, start: datetime, end: datetime):
        patch_inplace(cfg, cfg.model_copy(update={"regime_flip_on": False}), HOT_FAST)
        return [bar(symbol, end - timedelta(hours=1), 100), bar(symbol, end, 102)]

    runner = Runner(app, fetch_regime_klines=fetch)
    clock[0] += timedelta(minutes=5)
    await runner.regime_flip_tick()
    assert engine.market_calls == 0 and engine.pick_calls == 1
    assert not runner._tasks


@pytest.mark.parametrize("started", [False, True])
async def test_hot_disable_cancels_inflight_market_and_keeps_last_valid_state(
    tmp_path: Path, now: datetime, started: bool
) -> None:
    app = _app(tmp_path, now)
    commander, engine, clock, candidates, _ = await research(tmp_path / "research", now)
    app.clock = lambda: clock[0]
    app.pool = candidates
    app.m = dataclasses.replace(app.m, cards=commander)
    cfg = app.app_cfg.fast_loop
    patch_inplace(
        cfg,
        cfg.model_copy(update={"regime_flip_confirm": 1, "regime_flip_min_symbols": 1}),
        HOT_FAST,
    )

    async def fetch(symbol: str, start: datetime, end: datetime):
        return [bar(symbol, end - timedelta(hours=1), 100), bar(symbol, end, 102)]

    runner = Runner(app, fetch_regime_klines=fetch)
    commander.set_regime_price(lambda: 102.0)
    original_view, original_ref = commander.latest_market_view, commander.regime_reference
    original_card = authority(commander.current(clock[0]))
    original_cost = commander._stats.cost_usd
    engine.market_release.clear()
    clock[0] += timedelta(minutes=5)
    await runner.regime_flip_tick()
    pending = tuple(runner._tasks)
    try:
        if started:
            await asyncio.wait_for(engine.market_started.wait(), 1)
        patch_inplace(cfg, cfg.model_copy(update={"regime_flip_on": False}), HOT_FAST)
        await runner._regime_periodic_tick()
        await asyncio.gather(*pending, return_exceptions=True)
        if started:
            assert engine.aborted
        assert not runner._market_refreshing
        assert commander.latest_market_view == original_view
        assert commander.regime_reference == original_ref
        assert authority(commander.current(clock[0])) == original_card
        if started:
            assert commander._stats.cost_usd > original_cost
        else:
            assert engine.market_calls == 0
    finally:
        engine.market_release.set()
        await asyncio.gather(*pending, return_exceptions=True)
