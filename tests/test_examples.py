"""每个 JSON 示例都必须能被对应契约解析，并且序列化回去完全一致。"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from jiaoyi import contracts as c
from tests.conftest import EXAMPLES, load_example

EXAMPLE_MODELS: dict[str, type[BaseModel]] = {
    "battle_card": c.BattleCard,
    "market_view": c.MarketView,
    "candidate_signal": c.CandidateSignal,
    "jev_entry_request": c.JevRequest,
    "jev_entry_response": c.JevResponse,
    "jev_hold_request": c.JevRequest,
    "jev_hold_response": c.JevResponse,
    "jev_failed_response": c.JevResponse,
    "account_state": c.AccountState,
    "order_intent": c.OrderIntent,
    "order_intent_trailing": c.OrderIntent,
    "risk_verdict_approved": c.RiskVerdict,
    "risk_verdict_rejected": c.RiskVerdict,
    "position_record": c.PositionRecord,
    "keeper_verdict": c.KeeperVerdict,
    "reconcile_report": c.ReconcileReport,
    "symbol_pool": c.SymbolPool,
    "decision_record": c.DecisionRecord,
    "module_status": c.ModuleStatus,
    "alert": c.Alert,
}


def test_every_example_file_is_covered() -> None:
    files = {p.stem for p in EXAMPLES.glob("*.json")}
    assert files == set(EXAMPLE_MODELS)


@pytest.mark.parametrize("name", sorted(EXAMPLE_MODELS))
def test_example_roundtrip(name: str) -> None:
    raw = load_example(name)
    model = EXAMPLE_MODELS[name].model_validate(raw)
    assert model.model_dump(mode="json") == raw
