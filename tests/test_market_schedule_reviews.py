"""市场事件更新后的有依据复核：只使用离线模型、行情和执行边界。"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock

import pytest

from jiaoyi import contracts as c
from jiaoyi.core.store import JsonlDecisionLog
from jiaoyi.gate.jev import JevDecisionModel, MockJevTransport
from jiaoyi.keeper.keeper import JevKeeper
from jiaoyi.loop import KEEPER_CONCURRENCY, Runner
from jiaoyi.slow.commander import BattleCardCommander, EventRules, PickResult
from jiaoyi.slow.tradingagents_engine import TradingAgentsEngine, TradingAgentsSettings
from tests import fastloop_helpers as h
from tests.test_keeper_patrol import close_p
from tests.test_loop import FakeExecutor, FakePatrol, _app, _mark, _position
from tests.test_market_refresh_independent import OfflineResearch, authority
from tests.test_regime_flip import bar
from tests.test_slow_commander import RISK, entry, pool


class Research(OfflineResearch):
    def __init__(self) -> None:
        super().__init__()
        self.direction = "中性"
        self.market_text = "中性：BTC +0.2%，上涨占比 50%"
        self.nominations: list[c.MarketReviewCandidate] = []
        self.reviews_valid = True
        self.pick_reviews_valid = True
        self.contexts: list[tuple[c.MarketReviewCard, ...]] = []

    async def pick(self, candidates, max_n, previous, pool=()):
        result = await super().pick(candidates[:2], max_n, previous, pool)
        return dataclasses.replace(result, regime_direction="中性")

    async def pick_with_reviews(
        self, candidates, max_n, previous, pool=(), *, brief_text="", card_context=()
    ):
        result = await self.pick(candidates, max_n, previous, pool)
        return dataclasses.replace(
            result, review_candidates=[], reviews_valid=self.pick_reviews_valid
        )

    async def refresh_market_with_reviews(
        self, members, previous, *, brief_text="", card_context=()
    ) -> PickResult:
        self.contexts.append(tuple(card_context))
        result = await self.refresh_market(members, previous, brief_text=brief_text)
        return dataclasses.replace(
            result, review_candidates=list(self.nominations), reviews_valid=self.reviews_valid
        )


async def settle(runner: Runner) -> None:
    for _ in range(8):
        tasks = tuple(runner._tasks)
        if not tasks:
            return
        await asyncio.gather(*tasks)
    assert not runner._tasks, "后台任务不应在一次发布后持续自我重排"


async def setup(tmp: Path, now: datetime, *, gap=60, cap=12):
    app = _app(tmp, now)
    clock = [now]
    app.clock = lambda: clock[0]
    engine = Research()
    commander = BattleCardCommander(
        engine,
        RISK,
        JsonlDecisionLog(tmp / "research"),
        clock=app.clock,
        ttl=timedelta(hours=4),
        card_path=tmp / "battle_card.json",
        budget_path=tmp / "budget.json",
        events=EventRules(
            enabled=True,
            triggers=frozenset({"market_review", "signal_against_card", "key_level_break"}),
            symbol_gap=timedelta(minutes=gap),
            max_per_day=cap,
        ),
    )
    candidates = pool(
        now,
        *(
            entry(s).model_copy(update={"metrics": {"chg_1h": 3.0}})
            for s in ("AAUSDT", "BBUSDT", "CCUSDT")
        ),
    )
    app.pool = candidates
    app.m = dataclasses.replace(app.m, cards=commander)

    async def snapshot(original, observed_at):
        return original.model_copy(update={"ts": observed_at})

    async def fetch(symbol, start, end):
        return [bar(symbol, end - timedelta(hours=1), 100), bar(symbol, end, 102)]

    commander.set_regime_price(lambda: 100.0)
    commander.set_market_snapshot(snapshot)
    await commander.refresh(candidates, "scheduled")
    runner = Runner(app, fetch_regime_klines=fetch)
    runner.sync_watch = AsyncMock()
    runner._screen_now = AsyncMock()
    commander.set_regime_price(lambda: 102.0)
    commander.set_market_snapshot(snapshot)
    return app, runner, commander, engine, clock, candidates


def context(card: c.BattleCard) -> list[c.MarketReviewCard]:
    return [
        c.MarketReviewCard(
            symbol=s.symbol,
            research_token=s.research_token(),
            bias=s.bias,
            reason=s.reason,
            issued_at=s.issued_at,
            source_regime=s.source_regime,
            source_market_view_id=s.source_market_view_id,
            key_levels=s.key_levels,
        )
        for s in card.symbols
    ]


def nominee(card: c.BattleCard, symbol="AAUSDT", *, value=3.0, token=None, field="metrics.chg_1h"):
    old = card.for_symbol(symbol)
    return c.MarketReviewCandidate(
        symbol=symbol,
        research_token=token or (old.research_token() if old else "not-an-existing-research"),
        reason="最新1h涨幅达到3%，需要核对原研究中的弱势背景",
        evidence=[c.MarketReviewEvidence(field=field, value=value)],
    )


@pytest.mark.parametrize("invalid", ["not_in_card", "not_in_pool", "number", "field", "stale"])
async def test_only_existing_research_with_current_token_and_real_numeric_evidence_is_queued(
    tmp_path: Path, now: datetime, invalid: str
) -> None:
    _, _, commander, _, clock, candidates = await setup(tmp_path, now, gap=0)
    card = commander.current(now)
    summaries = context(card)
    original = nominee(card)
    if invalid == "not_in_card":
        original = nominee(card, "CCUSDT")
    elif invalid == "not_in_pool":
        candidates = candidates.model_copy(
            update={"entries": [e for e in candidates.entries if e.symbol != "AAUSDT"]}
        )
    elif invalid == "number":
        original = nominee(card, value=987.0)
    elif invalid == "field":
        original = nominee(card, field="metrics.invented_number")
    else:
        clock[0] += timedelta(minutes=1)
        await commander.refresh_symbol(candidates, "AAUSDT", "signal_against_card")
    view = commander.latest_market_view
    commander.enqueue_market_reviews([original], candidates, summaries, view)
    assert commander.pop_event(clock[0], candidates) is None


async def test_unrelated_coin_version_change_does_not_invalidate_same_coin_nomination(
    tmp_path: Path, now: datetime
) -> None:
    _, _, commander, _, clock, candidates = await setup(tmp_path, now, gap=0)
    card = commander.current(now)
    summaries, candidate = context(card), nominee(card)
    clock[0] += timedelta(minutes=1)
    await commander.refresh_symbol(candidates, "BBUSDT", "signal_against_card")
    assert commander.current(clock[0]).version > card.version
    commander.enqueue_market_reviews(
        [candidate], candidates, summaries, commander.latest_market_view
    )
    assert commander.pop_event(clock[0], candidates) == ("AAUSDT", "market_review")


async def test_nominations_survive_sixty_minute_cooldown_and_start_with_latest_market(
    tmp_path: Path, now: datetime
) -> None:
    _, _, commander, engine, clock, candidates = await setup(tmp_path, now)
    card = commander.current(now)
    summaries, candidate = context(card), nominee(card)
    clock[0] += timedelta(minutes=5)
    first = await commander.refresh_market(candidates)
    commander.enqueue_market_reviews([candidate], candidates, summaries, first)
    assert commander.pop_event(clock[0], candidates) is None
    clock[0] = now + timedelta(minutes=35)
    latest = await commander.refresh_market(candidates)
    commander.enqueue_market_reviews([candidate], candidates, summaries, latest)
    assert commander.pop_event(clock[0], candidates) is None
    clock[0] = now + timedelta(minutes=60)
    assert commander.pop_event(clock[0], candidates) == ("AAUSDT", "market_review")
    await commander.refresh_symbol(candidates, "AAUSDT", "market_review")
    updated = commander.current(clock[0]).for_symbol("AAUSDT")
    assert engine.symbol_calls[-1] == ("AAUSDT", latest.regime)
    assert updated.source_market_view_id == latest.market_view_id
    assert updated.research_token() != candidate.research_token
    assert commander.pop_event(clock[0], candidates) is None


async def test_market_without_nominations_does_not_restart_every_old_coin(
    tmp_path: Path, now: datetime
) -> None:
    _, _, commander, engine, clock, candidates = await setup(tmp_path, now, gap=0)
    card = commander.current(now)
    clock[0] += timedelta(minutes=10)
    await commander.refresh_market(candidates)
    assert commander.pop_event(clock[0], candidates) is None
    assert authority(commander.current(clock[0])) == authority(card)
    assert engine.contexts and {s.symbol for s in engine.contexts[-1]} == {"AAUSDT", "BBUSDT"}
    assert len(engine.symbol_calls) == 2


async def test_actual_market_result_enqueues_only_its_valid_nomination(
    tmp_path: Path, now: datetime
) -> None:
    _, _, commander, engine, clock, candidates = await setup(tmp_path, now, gap=0)
    card = commander.current(now)
    engine.nominations = [nominee(card)]
    clock[0] += timedelta(minutes=10)
    await commander.refresh_market(candidates)
    assert authority(commander.current(clock[0])) == authority(card)
    assert commander.pop_event(clock[0], candidates) == ("AAUSDT", "market_review")
    await commander.refresh_symbol(candidates, "AAUSDT", "market_review")
    assert commander.pop_event(clock[0], candidates) is None
    assert len(engine.symbol_calls) == 3


async def test_new_accepted_market_empty_candidate_set_withdraws_old_market_only_review(
    tmp_path: Path, now: datetime
) -> None:
    _, _, commander, engine, clock, candidates = await setup(tmp_path, now)
    card = commander.current(now)
    engine.nominations = [nominee(card)]
    await commander.refresh_market(candidates)
    clock[0] += timedelta(minutes=10)
    engine.nominations = []
    newest = await commander.refresh_market(candidates)
    clock[0] = now + timedelta(minutes=60)
    assert commander.latest_market_view == newest
    assert commander.pop_event(clock[0], candidates) is None
    assert len(engine.symbol_calls) == 2


async def test_failed_market_does_not_withdraw_last_accepted_nomination(
    tmp_path: Path, now: datetime
) -> None:
    _, _, commander, engine, clock, candidates = await setup(tmp_path, now)
    engine.nominations = [nominee(commander.current(now))]
    await commander.refresh_market(candidates)
    clock[0] += timedelta(minutes=10)
    engine.nominations = []
    engine.problem = RuntimeError("本次市场模型未完成")
    with pytest.raises(RuntimeError):
        await commander.refresh_market(candidates)
    clock[0] = now + timedelta(minutes=60)
    assert commander.pop_event(clock[0], candidates) == ("AAUSDT", "market_review")


@pytest.mark.parametrize("path", ["market_only", "scheduled"])
@pytest.mark.parametrize("next_valid", ["empty", "reconfirmed"])
async def test_unknown_review_metadata_preserves_queue_but_suspends_outdated_market_reason(
    tmp_path: Path, now: datetime, path: str, next_valid: str
) -> None:
    _, _, commander, engine, clock, candidates = await setup(tmp_path, now)
    candidate = nominee(commander.current(now))
    engine.nominations = [candidate]
    await commander.refresh_market(candidates)
    original = json.loads((tmp_path / "market_view.json").read_text())["market_reviews"]["AAUSDT"]
    clock[0] += timedelta(minutes=10)
    engine.nominations = []
    if path == "market_only":
        engine.reviews_valid = False
        await commander.refresh_market(candidates)
    else:
        engine.pick_reviews_valid = False
        await commander.refresh(candidates, "scheduled")
    clock[0] = now + timedelta(minutes=60)
    assert commander.pop_event(clock[0], candidates) is None
    saved = json.loads((tmp_path / "market_view.json").read_text())["market_reviews"]["AAUSDT"]
    assert saved["first_seen"] == original["first_seen"]
    assert saved["eligible_at"] == original["eligible_at"]
    assert saved["expires_at"] == original["expires_at"]
    engine.reviews_valid = True
    engine.nominations = [] if next_valid == "empty" else [candidate]
    await commander.refresh_market(candidates)
    event = commander.pop_event(clock[0], candidates)
    assert event == (None if next_valid == "empty" else ("AAUSDT", "market_review"))


async def test_unknown_market_metadata_does_not_suspend_real_key_level_reason(
    tmp_path: Path, now: datetime
) -> None:
    _, _, commander, engine, clock, candidates = await setup(tmp_path, now)
    engine.nominations = [nominee(commander.current(now))]
    await commander.refresh_market(candidates)
    clock[0] += timedelta(minutes=50)
    commander.request_event("AAUSDT", "key_level_break")
    clock[0] += timedelta(minutes=5)
    engine.nominations = []
    engine.reviews_valid = False
    await commander.refresh_market(candidates)
    clock[0] = now + timedelta(minutes=60)
    assert commander.pop_event(clock[0], candidates) == ("AAUSDT", "key_level_break")


async def test_withdrawing_market_reason_keeps_actual_later_key_level_timestamp(
    tmp_path: Path, now: datetime
) -> None:
    _, _, commander, engine, clock, candidates = await setup(tmp_path, now)
    engine.nominations = [nominee(commander.current(now))]
    await commander.refresh_market(candidates)
    clock[0] += timedelta(minutes=50)
    commander.request_event("AAUSDT", "key_level_break")
    clock[0] += timedelta(minutes=5)
    engine.nominations = []
    await commander.refresh_market(candidates)
    clock[0] = now + timedelta(minutes=60)
    assert commander.pop_event(clock[0], candidates) == ("AAUSDT", "key_level_break")
    await commander.refresh_symbol(candidates, "AAUSDT", "key_level_break")
    assert len(engine.symbol_calls) == 3


async def test_repeated_market_nomination_does_not_rejuvenate_expiring_queue(
    tmp_path: Path, now: datetime
) -> None:
    _, _, commander, engine, clock, candidates = await setup(tmp_path, now)
    engine.nominations = [nominee(commander.current(now))]
    await commander.refresh_market(candidates)
    clock[0] += timedelta(minutes=70)
    await commander.refresh_market(candidates)
    assert commander.pop_event(clock[0], candidates) == ("AAUSDT", "market_review")
    clock[0] = now + timedelta(minutes=91)
    assert commander.pop_event(clock[0], candidates) is None


async def test_scheduled_market_clears_prior_market_review_when_same_coin_research_is_reused(
    tmp_path: Path, now: datetime
) -> None:
    _, _, commander, engine, clock, candidates = await setup(tmp_path, now)
    card = commander.current(now)
    original_token = card.for_symbol("AAUSDT").research_token()
    engine.nominations = [nominee(card)]
    await commander.refresh_market(candidates)
    clock[0] += timedelta(minutes=55)
    await commander.refresh(candidates, "scheduled")
    assert commander.current(clock[0]).for_symbol("AAUSDT").research_token() == original_token
    clock[0] = now + timedelta(minutes=60)
    assert commander.pop_event(clock[0], candidates) is None


@pytest.mark.parametrize("block", ["off", "budget", "daily_cap"])
async def test_market_nomination_obeys_hot_switch_budget_and_event_day_limit(
    tmp_path: Path, now: datetime, block: str
) -> None:
    _, _, commander, _, clock, candidates = await setup(tmp_path, now, gap=0, cap=1)
    card = commander.current(now)
    summaries = context(card)
    commander.enqueue_market_reviews(
        [nominee(card)], candidates, summaries, commander.latest_market_view
    )
    if block == "daily_cap":
        assert commander.pop_event(clock[0], candidates) == ("AAUSDT", "market_review")
        await commander.refresh_symbol(candidates, "AAUSDT", "market_review")
        card = commander.current(clock[0])
        commander.enqueue_market_reviews(
            [nominee(card, "BBUSDT")], candidates, context(card), commander.latest_market_view
        )
    elif block == "budget":
        commander._cost_limit = 0.001
    else:
        commander.set_market_review_enabled(lambda: False)
    assert commander.pop_event(clock[0], candidates) is None


async def test_expiry_is_thirty_minutes_after_eligibility_without_sliding_on_poll(
    tmp_path: Path, now: datetime
) -> None:
    _, _, commander, _, clock, candidates = await setup(tmp_path, now)
    card = commander.current(now)
    clock[0] += timedelta(minutes=5)
    commander.enqueue_market_reviews(
        [nominee(card)], candidates, context(card), commander.latest_market_view
    )
    clock[0] = now + timedelta(minutes=70)
    assert commander.pop_event(clock[0], candidates) == ("AAUSDT", "market_review")
    clock[0] = now + timedelta(minutes=91)
    assert commander.pop_event(clock[0], candidates) is None


async def test_queued_review_restores_original_cooldown_after_restart(
    tmp_path: Path, now: datetime
) -> None:
    app, _, commander, engine, clock, candidates = await setup(tmp_path, now)
    card = commander.current(now)
    clock[0] += timedelta(minutes=5)
    commander.enqueue_market_reviews(
        [nominee(card)], candidates, context(card), commander.latest_market_view
    )
    restored = BattleCardCommander(
        engine,
        RISK,
        app.log,
        clock=app.clock,
        card_path=tmp_path / "battle_card.json",
        budget_path=tmp_path / "budget.json",
        events=EventRules(enabled=True, symbol_gap=timedelta(minutes=60)),
    )
    clock[0] = now + timedelta(minutes=59)
    assert restored.pop_event(clock[0], candidates) is None
    clock[0] += timedelta(minutes=1)
    assert restored.pop_event(clock[0], candidates) == ("AAUSDT", "market_review")


async def test_same_symbol_inflight_nomination_merges_without_cancelling_or_restarting(
    tmp_path: Path, now: datetime
) -> None:
    _, _, commander, engine, clock, candidates = await setup(tmp_path, now, gap=0)
    card = commander.current(now)
    candidate = nominee(card)
    commander.enqueue_market_reviews(
        [candidate], candidates, context(card), commander.latest_market_view
    )
    engine.symbol_started.clear()
    engine.symbol_release.clear()
    before = len(engine.symbol_calls)
    task = asyncio.create_task(commander.refresh_symbol(candidates, "AAUSDT", "market_review"))
    try:
        await asyncio.wait_for(engine.symbol_started.wait(), 1)
        engine.nominations = [candidate]
        clock[0] += timedelta(minutes=10)
        await commander.refresh_market(candidates)
        assert not task.done()
        assert commander.pop_event(clock[0], candidates) is None
        assert len(engine.symbol_calls) == before + 1
    finally:
        engine.symbol_release.set()
        await task
    assert len(engine.symbol_calls) == before + 1


async def test_real_keeper_acknowledges_actual_request_market_not_newer_return_time_market(
    tmp_path: Path, now: datetime
) -> None:
    _, _, commander, _, clock, candidates = await setup(tmp_path, now)
    started, release = asyncio.Event(), asyncio.Event()

    class Transport(MockJevTransport):
        async def evaluate(self, state, questions, timeout_s):
            started.set()
            await release.wait()
            return await super().evaluate(state, questions, timeout_s)

    transport = Transport(answer=close_p(0.1))
    fast = h.FAST.model_copy(update={"giveback_on": False})
    keeper = JevKeeper(
        JevDecisionModel(transport, fast, timeout_ms=2000),
        fast,
        lambda symbol: h.features(clock[0]).model_copy(update={"symbol": symbol}),
        commander.current,
    )
    original = commander.latest_market_view
    task = asyncio.create_task(keeper.review(_position(symbol="AAUSDT"), clock[0]))
    try:
        await asyncio.wait_for(started.wait(), 1)
        clock[0] += timedelta(minutes=10)
        latest = await commander.refresh_market(candidates)
        release.set()
        verdict = await task
        assert verdict.review_succeeded and verdict.action == "hold"
        assert verdict.source_market_view_id == original.market_view_id
        assert verdict.source_market_version == original.version
        assert verdict.source_market_view_id != latest.market_view_id
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_real_keeper_after_restart_uses_market_without_any_valid_battle_card(
    tmp_path: Path, now: datetime
) -> None:
    app, _, _, engine, clock, _ = await setup(tmp_path, now)
    clock[0] += timedelta(hours=5)
    restored = BattleCardCommander(
        engine,
        RISK,
        app.log,
        clock=app.clock,
        card_path=tmp_path / "battle_card.json",
        budget_path=tmp_path / "budget.json",
    )
    assert restored.current(clock[0]) is None
    assert restored.latest_market_view is not None
    transport = MockJevTransport(answer=close_p(0.1))
    fast = h.FAST.model_copy(update={"giveback_on": False})
    keeper = JevKeeper(
        JevDecisionModel(transport, fast, timeout_ms=2000),
        fast,
        lambda symbol: h.features(clock[0]).model_copy(update={"symbol": symbol}),
        restored.current,
    )
    executor = FakeExecutor()
    position = _position(symbol="AAUSDT", opened_at=clock[0])
    executor.held = [position]
    app.m = dataclasses.replace(app.m, cards=restored, keeper=keeper, executor=executor)
    runner = Runner(app)
    runner.positions = [position]
    verdict = await keeper.review(position, clock[0])
    snapshot = json.loads(transport.calls[-1])
    latest = restored.latest_market_view
    assert snapshot["card"]["status"] == "none"
    assert snapshot["current_market"]["market_view_id"] == latest.market_view_id
    assert verdict.review_succeeded
    assert verdict.source_market_view_id == latest.market_view_id
    assert verdict.source_market_version == latest.version
    assert restored.current(clock[0]) is None


@pytest.mark.parametrize("metadata_present", [False, True])
async def test_real_market_engine_transmits_research_tokens_and_parses_evidence_nomination(
    tmp_path: Path, now: datetime, monkeypatch: pytest.MonkeyPatch, metadata_present: bool
) -> None:
    _, _, commander, _, _, candidates = await setup(tmp_path, now)
    card = commander.current(now)
    candidate = nominee(card)

    class Model:
        def with_structured_output(self, schema):
            self.schema = schema
            return self

        def bind(self, **kwargs):
            return self

        def invoke(self, prompt, config):
            self.prompt = prompt
            data = {"regime": "BTC +2%，上涨占比 75%", "regime_direction": "中性"}
            if metadata_present:
                data["review_candidates"] = [candidate]
            return self.schema.model_validate(data)

    model = Model()
    callbacks, langchain = ModuleType("langchain_core.callbacks"), ModuleType("langchain_core")
    callbacks.BaseCallbackHandler = object
    langchain.callbacks = callbacks
    monkeypatch.setitem(sys.modules, "langchain_core", langchain)
    monkeypatch.setitem(sys.modules, "langchain_core.callbacks", callbacks)
    engine = TradingAgentsEngine.__new__(TradingAgentsEngine)
    engine._s = TradingAgentsSettings(hold="4h", hold_minutes=240)
    engine._today = lambda: now
    engine._news = engine._brief = None
    engine._inflight = {}
    engine._inflight_lock = threading.Lock()
    monkeypatch.setattr(engine, "_graph", lambda *args: None)
    monkeypatch.setattr(engine, "_llm", lambda *args: model)
    result = await engine.refresh_market_with_reviews(
        candidates.in_pool,
        commander.latest_market_view,
        card_context=context(card),
    )
    assert result.reviews_valid is metadata_present
    assert result.review_candidates == ([candidate] if metadata_present else [])
    assert result.picks == []
    assert candidate.research_token in model.prompt
    assert "metrics.chg_1h" in model.prompt and "3.0" in model.prompt
    assert card.for_symbol("AAUSDT").reason in model.prompt
    assert not engine._inflight


class Keeper:
    def __init__(self, commander, clock) -> None:
        self.commander, self.clock = commander, clock
        self.calls: list[tuple[str, str | None]] = []
        self.started, self.release = asyncio.Event(), asyncio.Event()
        self.release.set()
        self.fail = False
        self.problem: Exception | None = None
        self.forgotten: list[str] = []

    async def review(self, pos, now):
        market = self.commander.current(now).current_market
        self.calls.append((pos.position_id, market.market_view_id if market else None))
        self.started.set()
        await self.release.wait()
        if self.problem is not None:
            raise self.problem
        return c.KeeperVerdict(
            position_id=pos.position_id,
            ts=self.clock[0],
            action="hold",
            p_close=0.1,
            consecutive=1,
            review_succeeded=not self.fail,
            source_market_view_id=market.market_view_id if market else None,
            source_market_version=market.version if market else None,
        )

    def forget(self, position_id):
        self.forgotten.append(position_id)


async def positions_setup(tmp, now):
    app, _, commander, engine, clock, candidates = await setup(tmp, now)
    executor = FakeExecutor()
    executor.held = [_position(symbol="AAUSDT"), _position(position_id="closed", status="closed")]
    keeper = Keeper(commander, clock)
    app.m = dataclasses.replace(app.m, executor=executor, keeper=keeper, patrol=FakePatrol())
    runner = Runner(app)
    runner.sync_watch = AsyncMock()
    runner.positions = list(executor.held)
    commander.set_regime_price(lambda: 102.0)
    return runner, commander, engine, clock, candidates, executor, keeper


async def test_each_new_market_reviews_open_positions_once_and_coalesces_duplicate_notice(
    tmp_path: Path, now: datetime
) -> None:
    runner, commander, _, clock, candidates, _, keeper = await positions_setup(tmp_path, now)
    clock[0] += timedelta(minutes=10)
    view = await commander.refresh_market(candidates)
    await settle(runner)
    assert keeper.calls == [("pos-0001", view.market_view_id)]
    runner._market_published(view)
    await settle(runner)
    assert len(keeper.calls) == 1
    clock[0] += timedelta(minutes=10)
    newest = await commander.refresh_market(candidates)
    await settle(runner)
    assert keeper.calls[-1] == ("pos-0001", newest.market_view_id)
    assert len(keeper.calls) == 2


@pytest.mark.parametrize("succeeded", [False, True])
async def test_keeper_restart_preserves_processed_or_pending_market_without_fake_ack(
    tmp_path: Path, now: datetime, succeeded: bool
) -> None:
    runner, commander, _, clock, candidates, executor, keeper = await positions_setup(tmp_path, now)
    keeper.fail = not succeeded
    clock[0] += timedelta(minutes=10)
    latest = await commander.refresh_market(candidates)
    await settle(runner)
    assert len(keeper.calls) == 1
    keeper.fail = False
    restarted = Runner(runner.app)
    restarted.positions = list(executor.held)
    restarted.sync_watch = AsyncMock()
    restarted._market_published(latest)
    await settle(restarted)
    assert len(keeper.calls) == (1 if succeeded else 2)
    assert keeper.calls[-1] == ("pos-0001", latest.market_view_id)


async def test_old_inflight_keeper_reply_does_not_acknowledge_new_market(
    tmp_path: Path, now: datetime
) -> None:
    runner, commander, _, clock, candidates, executor, keeper = await positions_setup(tmp_path, now)
    keeper.release.clear()
    old = asyncio.create_task(runner._review(executor.held[0]))
    try:
        await asyncio.wait_for(keeper.started.wait(), 1)
        clock[0] += timedelta(minutes=10)
        newest = await commander.refresh_market(candidates)
        await asyncio.sleep(0)
        assert len(keeper.calls) == 1
        keeper.release.set()
        await old
        await settle(runner)
        await runner.keeper_tick()
        await settle(runner)
        assert keeper.calls[-1] == ("pos-0001", newest.market_view_id)
        assert keeper.calls[0][1] != keeper.calls[-1][1]
    finally:
        keeper.release.set()
        await asyncio.gather(old, return_exceptions=True)


@pytest.mark.parametrize("failure", ["failed", "raised", "cancelled"])
async def test_failed_or_cancelled_keeper_check_leaves_new_market_for_real_retry(
    tmp_path: Path, now: datetime, failure: str
) -> None:
    runner, commander, _, clock, candidates, _, keeper = await positions_setup(tmp_path, now)
    keeper.fail = failure == "failed"
    if failure == "raised":
        keeper.problem = RuntimeError("持仓复核临时失败")
    if failure == "cancelled":
        keeper.release.clear()
    clock[0] += timedelta(minutes=10)
    newest = await commander.refresh_market(candidates)
    if failure == "cancelled":
        await asyncio.wait_for(keeper.started.wait(), 1)
        for task in tuple(runner._tasks):
            task.cancel()
        await asyncio.gather(*tuple(runner._tasks), return_exceptions=True)
        keeper.release.set()
    else:
        await settle(runner)
    keeper.fail = False
    keeper.problem = None
    before = len(keeper.calls)
    await runner.keeper_tick()
    await settle(runner)
    assert len(keeper.calls) > before
    assert keeper.calls[-1] == ("pos-0001", newest.market_view_id)
    runner._market_published(newest)
    await settle(runner)
    assert keeper.calls[-1] == ("pos-0001", newest.market_view_id)


async def test_no_open_positions_creates_no_keeper_work_on_market_publication(
    tmp_path: Path, now: datetime
) -> None:
    runner, commander, _, clock, candidates, executor, keeper = await positions_setup(tmp_path, now)
    executor.held = runner.positions = []
    clock[0] += timedelta(minutes=10)
    await commander.refresh_market(candidates)
    await settle(runner)
    assert keeper.calls == []


async def test_position_closed_while_waiting_keeper_slot_is_not_reviewed_after_slot_frees(
    tmp_path: Path, now: datetime
) -> None:
    runner, commander, _, clock, candidates, executor, keeper = await positions_setup(tmp_path, now)
    positions = [
        _position(position_id=f"position-{index}", symbol="AAUSDT")
        for index in range(KEEPER_CONCURRENCY + 1)
    ]
    executor.held = runner.positions = positions
    keeper.release.clear()
    clock[0] += timedelta(minutes=10)
    await commander.refresh_market(candidates)
    try:
        for _ in range(30):
            if len(keeper.calls) == KEEPER_CONCURRENCY:
                break
            await asyncio.sleep(0)
        assert len(keeper.calls) == KEEPER_CONCURRENCY
        victim = positions[-1]
        await runner.close(victim, c.CloseReason.PATROL)
        executor.held = [pos for pos in positions if pos.position_id != victim.position_id]
        keeper.release.set()
        await settle(runner)
        assert victim.position_id not in {pid for pid, _ in keeper.calls}
        assert executor.closed == [(victim.position_id, c.CloseReason.PATROL)]
    finally:
        keeper.release.set()
        await settle(runner)


async def test_patrol_close_remains_available_while_market_keeper_review_waits(
    tmp_path: Path, now: datetime
) -> None:
    runner, commander, _, clock, candidates, executor, keeper = await positions_setup(tmp_path, now)
    keeper.release.clear()
    clock[0] += timedelta(minutes=10)
    await commander.refresh_market(candidates)
    try:
        await asyncio.wait_for(keeper.started.wait(), 1)
        runner.m.patrol.action = c.PatrolAction(ts=clock[0], symbols=["AAUSDT"], reason="硬保护")
        await runner.on_event(c.MarkPrice.model_validate({**_mark(clock[0]), "symbol": "AAUSDT"}))
        await asyncio.sleep(0)
        assert executor.closed == [("pos-0001", c.CloseReason.PATROL)]
    finally:
        keeper.release.set()
        await settle(runner)
    executor.held = []
    before = len(keeper.calls)
    await runner.keeper_tick()
    await settle(runner)
    assert len(keeper.calls) == before
