"""把契约套件套在现有实现上：桩必须过“安全”层，假实现 / 参考实现必须过“行为”层。

真实现做好后，在各自的测试文件里同样继承这些套件（见 tests/support/contracts.py 开头）。
"""

from __future__ import annotations

from typing import Any

from jiaoyi.contracts import RiskConfig
from jiaoyi.gate.stub import StubDecisionModel, StubEntryGate
from jiaoyi.interfaces import (
    DecisionModel,
    EntryGate,
    ExecutorAdapter,
    Keeper,
    LifeboatStop,
    Patrol,
    RiskGuard,
    Trigger,
)
from jiaoyi.keeper.stub import StubKeeper
from jiaoyi.patrol.stub import StubPatrol
from jiaoyi.risk.stub import StubRiskGuard
from jiaoyi.sentinel.stub import StubTrigger
from tests.support import builders as b
from tests.support.contracts import (
    DecisionModelContract,
    EntryGateBehaviorContract,
    EntryGateSafetyContract,
    ExecutorBehaviorContract,
    KeeperSafetyContract,
    LifeboatBehaviorContract,
    PatrolSafetyContract,
    RiskGuardApprovalContract,
    RiskGuardSafetyContract,
    TriggerSafetyContract,
)
from tests.support.fakes import (
    FakeEntryGate,
    FakeExecutorAdapter,
    FakeLifeboatStop,
    RuleDecisionModel,
    ScriptedDecisionModel,
)
from tests.support.reference_risk import ReferenceRiskGuard
from tests.support.simple_pipeline import SimpleTrigger


# ---- 桩：只要求“故障即关闭”
class TestStubRiskGuard(RiskGuardSafetyContract):
    def make_guard(self, cfg: RiskConfig) -> RiskGuard:
        return StubRiskGuard()


class TestStubEntryGate(EntryGateSafetyContract):
    def make_gate(self, model: Any) -> EntryGate:
        return StubEntryGate()


class TestStubTrigger(TriggerSafetyContract):
    def make_trigger(self) -> Trigger:
        return StubTrigger()


class TestStubKeeper(KeeperSafetyContract):
    def make_keeper(self, model: Any) -> Keeper:
        return StubKeeper()


class TestStubPatrol(PatrolSafetyContract):
    def make_patrol(self) -> Patrol:
        return StubPatrol()


# ---- 参考实现 / 假实现：要求完整行为
class TestReferenceRiskGuard(RiskGuardApprovalContract):
    def make_guard(self, cfg: RiskConfig) -> RiskGuard:
        return ReferenceRiskGuard(cfg)


class TestFakeEntryGate(EntryGateBehaviorContract):
    def make_gate(self, model: Any) -> EntryGate:
        return FakeEntryGate(model)


class TestScriptedDecisionModel(DecisionModelContract):
    def make_model(self) -> DecisionModel:
        return ScriptedDecisionModel()


class TestRuleDecisionModel(DecisionModelContract):
    def make_model(self) -> DecisionModel:
        return RuleDecisionModel()


class TestStubDecisionModel(DecisionModelContract):
    def make_model(self) -> DecisionModel:
        return StubDecisionModel()


class TestSimpleTrigger(TriggerSafetyContract):
    def make_trigger(self) -> Trigger:
        return SimpleTrigger()


class TestFakeExecutor(ExecutorBehaviorContract):
    def make_executor(self) -> ExecutorAdapter:
        return FakeExecutorAdapter()


class TestFakeLifeboat(LifeboatBehaviorContract):
    def make_lifeboat(self) -> LifeboatStop:
        return FakeLifeboatStop()


def test_builders_produce_valid_defaults() -> None:
    assert b.card().is_valid(b.T0)
    assert b.signal().features.book is not None
