from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError

from jiaoyi import contracts as c
from tests.conftest import load_example


def test_battle_card_validity_window(now: datetime) -> None:
    card = c.BattleCard.model_validate(load_example("battle_card"))
    assert card.is_valid(card.created_at)
    assert not card.is_valid(card.expires_at)
    assert card.for_symbol("BTCUSDT") is not None
    assert card.for_symbol("ETHUSDT") is None


def test_battle_card_rejects_duplicates_and_bad_expiry() -> None:
    raw = load_example("battle_card")
    dup = {**raw, "symbols": [raw["symbols"][0], raw["symbols"][0]]}
    with pytest.raises(ValidationError, match="多次"):
        c.BattleCard.model_validate(dup)
    with pytest.raises(ValidationError, match="expires_at"):
        c.BattleCard.model_validate({**raw, "expires_at": raw["created_at"]})


def test_range_must_be_ordered() -> None:
    with pytest.raises(ValidationError):
        c.Range(min=3, max=1)


@pytest.mark.parametrize("bad", ["BTCUSD", "BTC-USDT", "btcusdt", "BTCUSDC"])
def test_symbol_only_usdt_perps(bad: str) -> None:
    raw = load_example("alert")
    with pytest.raises(ValidationError):
        c.Alert.model_validate({**raw, "symbol": bad})


def test_contracts_forbid_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        c.Alert.model_validate({**load_example("alert"), "colour": "red"})


def test_jev_request_requires_exact_question_set() -> None:
    raw = load_example("jev_entry_request")
    with pytest.raises(ValidationError, match="问题和选项"):
        c.JevRequest.model_validate({**raw, "questions": raw["questions"][:5]})
    with pytest.raises(ValidationError, match="signal_id"):
        c.JevRequest.model_validate({**raw, "signal_id": None})


def test_jev_answer_probabilities_sum_to_one() -> None:
    with pytest.raises(ValidationError, match="概率和"):
        c.JevAnswer(id="take", probs={"take": 0.9, "skip": 0.3})
    assert c.JevAnswer(id="take", probs={"take": 0.7, "skip": 0.3}).top() == ("take", 0.7)


def test_jev_failed_response_has_no_answers() -> None:
    with pytest.raises(ValidationError):
        c.JevResponse(request_id="x", ok=True, model_version="m", latency_ms=1)
    with pytest.raises(ValidationError):
        c.JevResponse(request_id="x", ok=False, model_version="m", latency_ms=1)


def test_order_intent_lifeboat_must_be_wider() -> None:
    raw = load_example("order_intent")
    with pytest.raises(ValidationError, match="保命止损"):
        c.OrderIntent.model_validate({**raw, "lifeboat_stop_pct": raw["stop_loss_pct"]})
    with pytest.raises(ValidationError):
        c.OrderIntent.model_validate({**raw, "approved_by": "gate"})


def test_risk_verdict_consistency() -> None:
    with pytest.raises(ValidationError):
        c.RiskVerdict(signal_id="s", approved=True)
    with pytest.raises(ValidationError):
        c.RiskVerdict(signal_id="s", approved=False)


def test_blacklisted_symbol_never_in_pool() -> None:
    raw = load_example("symbol_pool")["entries"][2]
    with pytest.raises(ValidationError, match="拉黑"):
        c.SymbolPoolEntry.model_validate({**raw, "in_pool": True})


def test_account_day_pnl(now: datetime) -> None:
    acct = c.AccountState(
        ts=now,
        equity_usdt=9700,
        available_usdt=9700,
        day_start_equity_usdt=10000,
        open_notional_usdt=0,
        open_positions=0,
    )
    assert acct.day_pnl_pct == pytest.approx(-3.0)


def test_contracts_are_immutable(now: datetime) -> None:
    alert = c.Alert(ts=now, level=c.AlertLevel.INFO, role=c.Role.GATE, title="t")
    with pytest.raises(ValidationError):
        alert.title = "x"  # type: ignore[misc]


def test_naive_datetime_rejected(now: datetime) -> None:
    with pytest.raises(ValidationError):
        c.Alert(ts=now.replace(tzinfo=None) + timedelta(0), level="info", role="gate", title="t")


def test_failed_jev_response_cannot_carry_answers() -> None:
    with pytest.raises(ValidationError, match="故障即关闭"):
        c.JevResponse(
            request_id="x",
            ok=False,
            error="timeout",
            model_version="m",
            latency_ms=1000,
            answers=[c.JevAnswer(id="take", probs={"take": 0.9, "skip": 0.1})],
        )
    failed = c.JevResponse.model_validate(load_example("jev_failed_response"))
    assert failed.answer("take") is None


def test_order_intent_and_verdict_are_sealed() -> None:
    intent = c.OrderIntent.model_validate(load_example("order_intent"))
    with pytest.raises(TypeError, match="重新签发"):
        intent.model_copy(update={"quantity": 9.0})
    with pytest.raises(TypeError, match="跳过校验"):
        c.OrderIntent.model_construct()
    verdict = c.RiskVerdict.model_validate(load_example("risk_verdict_approved"))
    with pytest.raises(TypeError):
        verdict.model_copy()


def test_trailing_stop_needs_activation() -> None:
    raw = load_example("order_intent_trailing")
    assert c.OrderIntent.model_validate(raw).trailing_activation_pct == 0.4
    with pytest.raises(ValidationError, match="同时给"):
        c.OrderIntent.model_validate({**raw, "trailing_activation_pct": None})
    with pytest.raises(ValidationError, match="同时给"):
        c.OrderIntent.model_validate({**raw, "trailing_stop_pct": None})
    with pytest.raises(ValidationError, match="大于回撤"):
        c.OrderIntent.model_validate({**raw, "trailing_activation_pct": 0.1})
