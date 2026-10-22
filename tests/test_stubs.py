"""桩实现必须满足接口，并且全部“故障即关闭”。"""

from __future__ import annotations

from datetime import datetime

import pytest

from jiaoyi import contracts as c
from jiaoyi import interfaces as i
from jiaoyi.alerting.stub import StubAlerter
from jiaoyi.execution.stub import StubExecutorAdapter, StubLifeboatStop, StubReconciler
from jiaoyi.gate.stub import StubDecisionModel, StubEntryGate
from jiaoyi.keeper.stub import StubKeeper
from jiaoyi.market.stub import StubFeatureEngine, StubMarketDataFeed
from jiaoyi.patrol.stub import StubPatrol
from jiaoyi.predictor.stub import StubPredictor
from jiaoyi.risk.stub import StubRiskGuard
from jiaoyi.sentinel.stub import StubTrigger
from jiaoyi.slow.stub import StubBattleCardProvider, StubSymbolScreener
from tests.conftest import load_example


def test_stubs_satisfy_interfaces(now: datetime) -> None:
    pairs = [
        (StubMarketDataFeed(), i.MarketDataFeed),
        (StubFeatureEngine(), i.FeatureEngine),
        (StubTrigger(), i.Trigger),
        (StubPredictor(), i.Predictor),
        (StubDecisionModel(), i.DecisionModel),
        (StubEntryGate(), i.EntryGate),
        (StubRiskGuard(), i.RiskGuard),
        (StubExecutorAdapter(now), i.ExecutorAdapter),
        (StubLifeboatStop(), i.LifeboatStop),
        (StubKeeper(), i.Keeper),
        (StubPatrol(), i.Patrol),
        (StubReconciler(), i.Reconciler),
        (StubSymbolScreener(), i.SymbolScreener),
        (StubBattleCardProvider(), i.BattleCardProvider),
        (StubAlerter(), i.Alerter),
    ]
    for obj, proto in pairs:
        assert isinstance(obj, proto), f"{type(obj).__name__} 不满足 {proto.__name__}"


async def test_fast_loop_stubs_never_open(now: datetime) -> None:
    signal = c.CandidateSignal.model_validate(load_example("candidate_signal"))
    card = c.BattleCard.model_validate(load_example("battle_card"))
    account = c.AccountState.model_validate(load_example("account_state"))

    jev = await StubEntryGate().evaluate(signal, card, now)
    assert not jev.ok
    verdict = StubRiskGuard().review(signal, jev, card, account, now)
    assert not verdict.approved and verdict.intent is None

    intent = c.OrderIntent.model_validate(load_example("order_intent"))
    with pytest.raises(NotImplementedError):
        await StubExecutorAdapter(now).open(intent)


async def test_monitoring_stubs_keep_status_quo(now: datetime) -> None:
    pos = c.PositionRecord.model_validate(load_example("position_record"))
    assert (await StubKeeper().review(pos, now)).action == "hold"
    req = c.JevRequest.model_validate(load_example("jev_hold_request"))
    assert not (await StubDecisionModel().ask(req)).ok
    assert StubBattleCardProvider().current(now) is None
    assert (await StubReconciler().reconcile(now)).clean
