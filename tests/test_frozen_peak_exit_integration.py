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


def install(rig, side="long"):
    rec = position(START, opened_at=START, quantity=1, entry_price=100, side=side)
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
        "buy" if side == "long" else "sell",
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


PEAK_KEYS = {"peak_gain_r", "peak_net_bps", "peak_after_min"}
TRIGGER_KEYS = {"trigger_price", "trigger_gain_r"}


@pytest.mark.parametrize(
    "side,peak,frozen_at,touch,net_bps",
    [("long", 102, 101, 102.5, 189.9), ("short", 98, 99, 97.5, 190.1)],
)
async def test_executor_records_frozen_peak_size_at_freeze_and_trigger(
    tmp_path, side, peak, frozen_at, touch, net_bps
):
    rig = Rig(tmp_path)
    rec = install(rig, side)

    def book_at(seconds, price):
        rig.t = START + timedelta(seconds=seconds)
        rig.adapter._exchange.execution_book.return_value = ExecutionBook(
            rec.symbol, price, price, rig.t, rig.t
        )

    book_at(30, peak)
    result = await rig.adapter.frozen_peak_review(rec.position_id, rig.t)
    assert result["status"] == "collecting" and not (PEAK_KEYS | TRIGGER_KEYS) & result.keys()

    book_at(60, frozen_at)
    result = await rig.adapter.frozen_peak_review(rec.position_id, rig.t)
    assert result["status"] == "frozen" and not TRIGGER_KEYS & result.keys()
    assert result["peak_gain_r"] == 2.0
    assert result["peak_net_bps"] == net_bps
    assert result["peak_after_min"] == 0.5

    book_at(61, touch)
    result = await rig.adapter.frozen_peak_review(rec.position_id, rig.t)
    assert result["triggered"] and result["target_price"] == peak
    assert result["peak_gain_r"] == 2.0 and result["peak_net_bps"] == net_bps
    assert result["trigger_price"] == touch and result["trigger_gain_r"] == 2.5

    book_at(70, frozen_at)
    result = await rig.adapter.frozen_peak_review(rec.position_id, rig.t)
    assert result["detail"] == "pending_exit" and result["peak_gain_r"] == 2.0
    assert not TRIGGER_KEYS & result.keys()

    stored = rig.registry.get(rec.position_id).frozen_peak_exit.model_dump()
    assert not (PEAK_KEYS | TRIGGER_KEYS) & stored.keys()


async def review_at(rig, rec, seconds, price):
    rig.t = START + timedelta(seconds=seconds)
    rig.adapter._exchange.execution_book.return_value = ExecutionBook(
        rec.symbol, price, price, rig.t, rig.t
    )
    return await rig.adapter.frozen_peak_review(rec.position_id, rig.t)


@pytest.mark.parametrize("side,peak,touch", [("long", 102, 102.5), ("short", 98, 97.5)])
async def test_executor_keeps_latched_trigger_when_recorded_size_is_corrupt(
    tmp_path, side, peak, touch
):
    rig = Rig(tmp_path)
    rec = install(rig, side)
    await review_at(rig, rec, 30, peak)
    await review_at(rig, rec, 60, peak)
    assert (await review_at(rig, rec, 61, touch))["triggered"]
    tracked = rig.registry.get(rec.position_id)
    corrupt = tracked.frozen_peak_exit.model_copy(update={"peak_net_usdt": "corrupt"})
    rig.registry.put(tracked.model_copy(update={"frozen_peak_exit": corrupt}))
    result = await review_at(rig, rec, 70, peak)
    assert result["triggered"] is True and result["detail"] == "pending_exit"
    assert not (PEAK_KEYS | TRIGGER_KEYS) & result.keys()


@pytest.mark.parametrize("side,peak,touch", [("long", 102, 102.5), ("short", 98, 97.5)])
async def test_executor_triggers_when_recording_the_size_fails(
    tmp_path, monkeypatch, caplog, side, peak, touch
):
    def broken(*_args):
        raise ValueError("bad size")

    monkeypatch.setattr("jiaoyi.execution.hb_executor.frozen_peak_evidence", broken)
    rig = Rig(tmp_path)
    rec = install(rig, side)
    await review_at(rig, rec, 30, peak)
    await review_at(rig, rec, 60, peak)
    result = await review_at(rig, rec, 61, touch)
    assert result["triggered"] is True and not TRIGGER_KEYS & result.keys()
    assert rig.registry.get(rec.position_id).frozen_peak_exit.status == "triggered"
    assert "Frozen peak size not recorded: ValueError: bad size" in caplog.text


@pytest.mark.parametrize("side,peak,touch", [("long", 102, 102.5), ("short", 98, 97.5)])
async def test_executor_keeps_trigger_price_without_a_usable_stop_distance(
    tmp_path, side, peak, touch
):
    rig = Rig(tmp_path)
    rec = install(rig, side)
    tracked = rig.registry.get(rec.position_id)
    rig.registry.put(tracked.model_copy(update={"stop_loss_pct": 0.0}))
    await review_at(rig, rec, 30, peak)
    await review_at(rig, rec, 60, peak)
    result = await review_at(rig, rec, 61, touch)
    assert result["triggered"] and result["trigger_price"] == touch
    assert not {"peak_gain_r", "trigger_gain_r"} & result.keys()


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


async def test_keeper_records_why_the_rule_is_off_for_an_activated_trailing(tmp_path):
    log = JsonlDecisionLog(tmp_path / "decisions")
    keeper = JevKeeper(
        JevDecisionModel(MockJevTransport(answer=close_p(0.1)), FAST, timeout_ms=200),
        FAST,
        lambda _: None,
        lambda _: None,
        RoleRecorder(Role.KEEPER, log),
        frozen_peak_review=AsyncMock(
            return_value={"triggered": False, "status": "trailing_active", "target_price": None}
        ),
    )
    verdict = await keeper.review(position(START), START)
    assert verdict.action != "close"
    rules = log.scan_day(START.date().isoformat(), "")[-1]["output"]["rules"]
    assert {
        "rule": "frozen_peak",
        "vote": "off",
        "why": "移动止损已激活，不按冻结盈利价平",
    } in rules


async def test_existing_position_is_never_migrated(tmp_path):
    rig = Rig(tmp_path)
    rec = install(rig)
    tracked = rig.registry.get(rec.position_id)
    rig.registry.put(tracked.model_copy(update={"frozen_peak_exit": None}))
    rig.adapter.configure_frozen_peak_exit(True, 600)
    assert await rig.adapter.frozen_peak_review(rec.position_id, START) is None
    rig.adapter._exchange.entry_basis.assert_not_awaited()
    assert "frozen_peak_exit_on" in HOT_FAST and "frozen_peak_exit_after_s" in HOT_FAST


def activate_trailing(rig, pid, field="trailing_activated"):
    tracked = rig.registry.get(pid)
    protection = tracked.server_protection.model_copy(update={field: True})
    rig.registry.put(tracked.model_copy(update={"server_protection": protection}))


@pytest.mark.parametrize("field", ["trailing_activated", "native_activated"])
async def test_trailing_activated_before_freeze_never_freezes(tmp_path, field):
    rig = Rig(tmp_path)
    rec = install(rig)
    await rig.adapter.frozen_peak_review(rec.position_id, START)
    activate_trailing(rig, rec.position_id, field)
    rig.t = START + timedelta(seconds=60)
    result = await rig.adapter.frozen_peak_review(rec.position_id, rig.t)
    assert result["status"] == "trailing_active" and not result["triggered"]
    assert result["frozen_at"] is None and result["target_price"] is None
    rig.t += timedelta(seconds=1)
    result = await rig.adapter.frozen_peak_review(rec.position_id, rig.t)
    assert result["status"] == "trailing_active" and not result["triggered"]
    assert rig.adapter._exchange.execution_book.await_count == 1
    stored = rig.registry.get(rec.position_id).frozen_peak_exit
    assert (stored.status, stored.peak_price, stored.detail) == (
        "trailing_active",
        102,
        "trailing_activated",
    )


async def test_trailing_activated_after_freeze_drops_the_target_and_never_exits(tmp_path):
    rig = Rig(tmp_path)
    rec = install(rig)
    await rig.adapter.frozen_peak_review(rec.position_id, START)
    rig.t = START + timedelta(seconds=60)
    rig.adapter._exchange.execution_book.return_value = ExecutionBook(
        rec.symbol, 103, 103.1, rig.t, rig.t
    )
    result = await rig.adapter.frozen_peak_review(rec.position_id, rig.t)
    assert result["status"] == "frozen" and result["target_price"] == 102
    activate_trailing(rig, rec.position_id)
    rig.t += timedelta(seconds=1)
    rig.adapter._exchange.execution_book.return_value = ExecutionBook(
        rec.symbol, 102, 102.1, rig.t, rig.t
    )
    result = await rig.adapter.frozen_peak_review(rec.position_id, rig.t)
    assert result["status"] == "trailing_active" and not result["triggered"]
    assert result["target_price"] is None and result["peak_price"] == 102
    assert result["frozen_at"] == "2026-10-22T06:18:00Z"


async def test_quiet_position_without_trailing_still_freezes_and_exits(tmp_path):
    rig = Rig(tmp_path)
    rec = install(rig)
    await rig.adapter.frozen_peak_review(rec.position_id, START)
    rig.t = START + timedelta(seconds=60)
    rig.adapter._exchange.execution_book.return_value = ExecutionBook(
        rec.symbol, 101, 101.1, rig.t, rig.t
    )
    result = await rig.adapter.frozen_peak_review(rec.position_id, rig.t)
    assert result["status"] == "frozen" and result["target_price"] == 102
    rig.t += timedelta(seconds=1)
    rig.adapter._exchange.execution_book.return_value = ExecutionBook(
        rec.symbol, 102, 102.1, rig.t, rig.t
    )
    result = await rig.adapter.frozen_peak_review(rec.position_id, rig.t)
    assert result["status"] == "triggered" and result["triggered"]
