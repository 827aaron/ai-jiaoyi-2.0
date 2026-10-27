"""Synthetic evidence only; no exchange or runtime connections."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiaoyi.contracts import Outcome, Role
from jiaoyi.core.hotconfig import HOT_FAST
from jiaoyi.core.recorder import RoleRecorder
from jiaoyi.core.store import JsonlDecisionLog
from jiaoyi.execution.exchange import EntryBasis, ExchangePosition, ExecutionBook
from jiaoyi.execution.frozen_peak import FrozenPeakExit
from jiaoyi.execution.registry import PositionRegistry, ServerProtection, TrackedPosition
from jiaoyi.gate.jev import JevDecisionModel, MockJevTransport
from jiaoyi.keeper.keeper import JevKeeper
from tests.fastloop_helpers import FAST
from tests.test_exchange_execution_evidence import CID, client, fills
from tests.test_execution import START, Rig
from tests.test_keeper_patrol import close_p, position


async def test_exchange_basis_keeps_raw_first_and_last_order_fill_times():
    obj, ex = client()
    rows = fills()
    rows[0]["time"] = int(START.timestamp() * 1000)
    rows[1]["time"] = int((START + timedelta(seconds=3)).timestamp() * 1000)
    ex.fapiPrivateGetUserTrades.return_value = rows
    basis = await obj.entry_basis("SYNTHUSDT", CID)
    assert basis.first_fill_at == START
    assert basis.last_fill_at == START + timedelta(seconds=3)
    rows[0].pop("time")
    assert (await obj.entry_basis("SYNTHUSDT", CID)).first_fill_at is None


def install(rig):
    rec = position(START, opened_at=START, quantity=1, entry_price=100)
    rig.registry.put(
        TrackedPosition(
            record=rec,
            stop_loss_pct=1,
            take_profit_pct=2,
            lifeboat_stop_pct=3,
            entry_client_order_id="entry-1",
            server_protection=ServerProtection(
                account_name="acct",
                connector_name="demo",
                intended_quantity=1,
                stage="armed",
                entry_order_id="17",
                entry_status="FILLED",
                entry_terminal=True,
                observed_quantity=1,
            ),
            frozen_peak_exit=FrozenPeakExit(after_s=60, exit_fee_bps=5),
        )
    )
    basis = EntryBasis(
        rec.symbol,
        "entry-1",
        "17",
        "buy",
        1,
        100,
        0.05,
        100,
        "FILLED",
        START,
        START,
    )
    book = ExecutionBook(rec.symbol, 102, 102.1, rig.t, rig.t)
    rig.adapter._protection = SimpleNamespace()
    rig.adapter._exchange = SimpleNamespace(
        entry_basis=AsyncMock(return_value=basis),
        execution_book=AsyncMock(return_value=book),
        positions_evidence=AsyncMock(return_value=[ExchangePosition(rec.symbol, rec.side, 1)]),
    )
    return rec


async def test_executor_samples_persists_freezes_and_requests_only_fresh_retouch(tmp_path):
    rig = Rig(tmp_path)
    rec = install(rig)
    result = await rig.adapter.frozen_peak_review(rec.position_id, START)
    assert result["peak_price"] == 102 and not result["triggered"]
    rig.registry = PositionRegistry(tmp_path / "positions.json")
    rig.adapter._registry = rig.registry
    rig.t = START + timedelta(seconds=60)
    rig.adapter._exchange.execution_book.return_value = ExecutionBook(
        rec.symbol, 103, 103.1, rig.t, rig.t
    )
    result = await rig.adapter.frozen_peak_review(rec.position_id, rig.t)
    assert result["target_price"] == 102 and not result["triggered"]
    rig.t += timedelta(seconds=1)
    rig.adapter._exchange.execution_book.return_value = ExecutionBook(
        rec.symbol, 102, 102.1, rig.t, rig.t
    )
    result = await rig.adapter.frozen_peak_review(rec.position_id, rig.t)
    assert result["triggered"]
    rig.t += timedelta(seconds=10)
    result = await rig.adapter.frozen_peak_review(rec.position_id, rig.t)
    assert result["triggered"] and result["detail"] == "pending_exit"
    rig.adapter._exchange.execution_book.return_value = ExecutionBook(
        rec.symbol, 101, 101.1, rig.t, rig.t
    )
    result = await rig.adapter.frozen_peak_review(rec.position_id, rig.t)
    assert result["triggered"]


@pytest.mark.parametrize("quantity,side", [(0.5, "long"), (1, "short")])
async def test_executor_rejects_changed_actual_position(tmp_path, quantity, side):
    rig = Rig(tmp_path)
    rec = install(rig)
    rig.adapter._exchange.positions_evidence.return_value = [
        ExchangePosition(rec.symbol, type(rec.side)(side), quantity)
    ]
    result = await rig.adapter.frozen_peak_review(rec.position_id, START)
    assert not result["triggered"] and result["peak_price"] is None


async def test_keeper_rule_uses_execution_evidence_without_model_or_mainnet_features(tmp_path):
    transport = MockJevTransport(answer=close_p(0.1))
    log = JsonlDecisionLog(tmp_path / "decisions")
    keeper = JevKeeper(
        JevDecisionModel(transport, FAST, timeout_ms=200),
        FAST,
        lambda _: None,
        lambda _: None,
        RoleRecorder(Role.KEEPER, log),
        frozen_peak_review=AsyncMock(return_value={"triggered": True, "target_price": 102}),
    )
    verdict = await keeper.review(position(START), START)
    assert verdict.action == "close" and verdict.jev_request_id is None
    rows = log.scan_day(START.date().isoformat(), "")
    assert rows[-1]["outcome"] == Outcome.CLOSE.value
    assert rows[-1]["output"]["frozen_peak_exit"]["target_price"] == 102


async def test_existing_position_is_never_migrated(tmp_path):
    rig = Rig(tmp_path)
    rec = install(rig)
    tracked = rig.registry.get(rec.position_id)
    rig.registry.put(tracked.model_copy(update={"frozen_peak_exit": None}))
    rig.adapter.configure_frozen_peak_exit(True, 600)
    assert await rig.adapter.frozen_peak_review(rec.position_id, START) is None
    rig.adapter._exchange.entry_basis.assert_not_awaited()
    assert "frozen_peak_exit_on" in HOT_FAST and "frozen_peak_exit_after_s" in HOT_FAST
