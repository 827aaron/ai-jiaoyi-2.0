"""Independent frozen-target boundary checks; synthetic evidence only."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from jiaoyi.contracts import Side
from jiaoyi.contracts.position import CloseReason, PositionStatus
from jiaoyi.execution.exchange import EntryBasis, ExchangeError, ExecutionBook
from jiaoyi.execution.frozen_peak import FrozenPeakExit, frozen_peak_update
from jiaoyi.execution.registry import TrackedPosition
from tests.test_keeper_patrol import position
from tests.test_server_protection_regression import AdapterHarness

START = datetime(2026, 10, 28, tzinfo=UTC)


def evidence(side=Side.LONG):
    rec = position(START, side=side, quantity=2, entry_price=100, opened_at=START)
    basis = EntryBasis(
        "BTCUSDT",
        "entry-review",
        "123",
        "buy" if side is Side.LONG else "sell",
        2,
        200,
        0.1,
        100,
        "FILLED",
        START,
        START,
    )
    return rec, basis


def quote(seconds, price, side=Side.LONG, *, lag=0):
    now = START + timedelta(seconds=seconds)
    # The other side of the spread must never define this position's target.
    bid, ask = (price, price + 20) if side is Side.LONG else (price - 20, price)
    return ExecutionBook("BTCUSDT", bid, ask, now - timedelta(seconds=lag), now), now


def step(rec, basis, state, seconds, price, *, lag=0):
    book, now = quote(seconds, price, rec.side, lag=lag)
    return frozen_peak_update(rec, state, basis, book, now)


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
def test_freeze_strict_window_then_fresh_executable_touch_after_restart(side):
    rec, basis = evidence(side)
    peak, stronger, weak = (102, 103, 101) if side is Side.LONG else (98, 97, 99)
    state = step(rec, basis, FrozenPeakExit(after_s=60, exit_fee_bps=5), 20, peak)
    assert state.peak_price == peak
    state = step(rec, basis, state, 60, stronger)
    assert state.status == "frozen" and state.target_price == peak
    assert state.peak_price == peak  # Boundary quote cannot move the target.
    state = FrozenPeakExit.model_validate_json(state.model_dump_json())
    state = step(rec, basis, state, 61, weak)
    assert state.status == "frozen" and state.target_price == peak
    state = step(rec, basis, state, 62, peak)
    assert state.status == "triggered" and state.target_price == peak


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
def test_gross_profit_without_fee_coverage_never_arms_later(side):
    rec, basis = evidence(side)
    small = 100.05 if side is Side.LONG else 99.95
    large = 105 if side is Side.LONG else 95
    state = step(rec, basis, FrozenPeakExit(after_s=60, exit_fee_bps=5), 20, small)
    assert state.peak_price is None
    state = step(rec, basis, state, 60, large)
    assert state.status == "no_positive_peak" and state.target_price is None
    state = step(rec, basis, state, 100, large)
    assert state.status == "no_positive_peak" and state.target_price is None


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
def test_late_first_observation_cannot_invent_prior_peak(side):
    rec, basis = evidence(side)
    good = 105 if side is Side.LONG else 95
    state = step(rec, basis, FrozenPeakExit(after_s=60, exit_fee_bps=5), 65, good)
    assert state.status == "no_positive_peak" and state.target_price is None


@pytest.mark.parametrize(
    "change",
    [
        {"order_id": "other"},
        {"fee_usdt": 0.2},
        {
            "first_fill_at": START + timedelta(seconds=1),
            "last_fill_at": START + timedelta(seconds=1),
        },
        {"last_fill_at": START + timedelta(seconds=1)},
    ],
)
def test_changed_entry_identity_permanently_disables_this_rule(change):
    rec, basis = evidence()
    state = step(rec, basis, FrozenPeakExit(after_s=60, exit_fee_bps=5), 20, 102)
    state = step(rec, replace(basis, **change), state, 30, 103)
    assert state.status == "unknown"
    state = step(rec, basis, state, 65, 105)
    assert state.status == "unknown" and state.target_price is None


@pytest.mark.parametrize(
    "bad_basis",
    [
        {"symbol": "ETHUSDT"},
        {"side": "sell"},
        {"order_status": "PARTIALLY_FILLED"},
        {"filled_quantity": 3},
        {"filled_quote": 201},
        {"fee_usdt": float("nan")},
        {"first_fill_at": None},
        {"last_fill_at": START + timedelta(seconds=25)},
    ],
)
def test_bad_basis_cannot_create_any_peak(bad_basis):
    rec, basis = evidence()
    with pytest.raises(ExchangeError):
        step(rec, replace(basis, **bad_basis), FrozenPeakExit(after_s=60, exit_fee_bps=5), 20, 102)


@pytest.mark.parametrize("lag", [-1, 5.001])
def test_future_or_stale_book_cannot_trigger(lag):
    rec, basis = evidence()
    state = step(rec, basis, FrozenPeakExit(after_s=60, exit_fee_bps=5), 20, 102)
    state = step(rec, basis, state, 60, 101)
    with pytest.raises(ExchangeError):
        step(rec, basis, state, 70, 105, lag=lag)
    assert state.status == "frozen" and state.target_price == 102


def test_duplicate_or_older_source_does_not_make_a_second_touch():
    rec, basis = evidence()
    state = step(rec, basis, FrozenPeakExit(after_s=60, exit_fee_bps=5), 20, 102)
    state = step(rec, basis, state, 60, 101)
    for lag in (1, 2):
        with pytest.raises(ExchangeError):
            step(rec, basis, state, 61, 105, lag=lag)


def test_delayed_predeadline_quote_cannot_rewrite_an_already_frozen_target():
    rec, basis = evidence()
    state = step(rec, basis, FrozenPeakExit(after_s=60, exit_fee_bps=5), 20, 102)
    state = step(rec, basis, state, 60, 103, lag=2)
    assert state.target_price == 102 and state.peak_price == 102
    state = step(rec, basis, state, 61, 105, lag=2)
    assert state.status == "frozen" and state.target_price == 102


def test_legacy_registry_defaults_to_no_opt_in():
    rec, _ = evidence()
    legacy = TrackedPosition(record=rec, lifeboat_stop_pct=1.5, stop_loss_pct=1, take_profit_pct=2)
    assert legacy.frozen_peak_exit is None


async def test_slow_sampling_does_not_delay_close_or_overwrite_final_position(tmp_path):
    h = AdapterHarness(tmp_path, Side.LONG)
    await h.protect()
    tracked = h.registry.get("pos-1")
    sample_at = tracked.record.opened_at + timedelta(seconds=20)
    basis = EntryBasis(
        "BTCUSDT",
        "entry-1",
        "100",
        "buy",
        2,
        200,
        0.1,
        100,
        "FILLED",
        tracked.record.opened_at,
        tracked.record.opened_at,
    )
    h.registry.put(
        tracked.model_copy(
            update={
                "frozen_peak_exit": FrozenPeakExit(after_s=60, exit_fee_bps=5),
            }
        )
    )
    h.adapter._clock = lambda: sample_at
    h.exchange.entry_basis = AsyncMock(return_value=basis)
    entered, release = asyncio.Event(), asyncio.Event()

    async def book(_symbol):
        entered.set()
        await release.wait()
        return ExecutionBook("BTCUSDT", 102, 102.1, sample_at, sample_at)

    h.exchange.execution_book = book
    sample_task = asyncio.create_task(h.adapter.frozen_peak_review("pos-1", sample_at))
    await entered.wait()
    close_task = asyncio.create_task(h.adapter.close("pos-1", CloseReason.KEEPER))
    await asyncio.wait_for(close_task, timeout=0.5)
    assert h.registry.get("pos-1").record.status is PositionStatus.CLOSED
    assert len(h.exchange.direct) == 1
    release.set()
    result = await sample_task
    assert result is None
    assert h.registry.get("pos-1").record.status is PositionStatus.CLOSED
    assert len(h.exchange.direct) == 1


async def test_consumption_time_is_refreshed_after_async_book_read(tmp_path):
    from tests.test_execution import START as RIG_START
    from tests.test_execution import Rig
    from tests.test_frozen_peak_exit_integration import install

    rig = Rig(tmp_path)
    rec = install(rig)

    async def book(_symbol):
        rig.t = RIG_START + timedelta(seconds=2)
        return ExecutionBook(rec.symbol, 102, 102.1, rig.t, rig.t)

    rig.adapter._exchange.execution_book.side_effect = book
    result = await rig.adapter.frozen_peak_review(rec.position_id, RIG_START)
    assert result["peak_price"] == 102 and result["detail"] == ""


async def test_settings_changes_only_affect_future_enrollments(tmp_path):
    from tests.test_execution import START as RIG_START
    from tests.test_execution import Rig
    from tests.test_frozen_peak_exit_integration import install

    rig = Rig(tmp_path)
    rec = install(rig)
    rig.adapter.configure_frozen_peak_exit(False, 10)
    result = await rig.adapter.frozen_peak_review(rec.position_id, RIG_START)
    assert result["after_s"] == 60 and result["peak_price"] == 102


async def test_stuck_evidence_read_has_total_timeout_and_next_review_recovers(
    tmp_path, monkeypatch
):
    from jiaoyi.execution import hb_executor
    from tests.test_execution import START as RIG_START
    from tests.test_execution import Rig
    from tests.test_frozen_peak_exit_integration import install

    rig = Rig(tmp_path)
    rec = install(rig)
    cancelled = asyncio.Event()
    assert hb_executor.FROZEN_PEAK_EVIDENCE_TIMEOUT_S == 12.0
    monkeypatch.setattr(hb_executor, "FROZEN_PEAK_EVIDENCE_TIMEOUT_S", 0.02)

    async def never_returns(*_args):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    rig.adapter._exchange.entry_basis.side_effect = never_returns
    result = await asyncio.wait_for(
        rig.adapter.frozen_peak_review(rec.position_id, RIG_START), timeout=0.5
    )
    assert cancelled.is_set()
    assert not result["triggered"] and "TimeoutError" in result["detail"]
    assert result["peak_price"] is None
    rig.adapter._exchange.entry_basis.side_effect = None
    recovered = await rig.adapter.frozen_peak_review(rec.position_id, RIG_START)
    assert recovered["peak_price"] == 102 and recovered["detail"] == ""
    assert not recovered["triggered"]


async def test_multisecond_evidence_reads_succeed_within_total_budget(tmp_path):
    from jiaoyi.execution import hb_executor
    from tests.test_execution import START as RIG_START
    from tests.test_execution import Rig
    from tests.test_frozen_peak_exit_integration import install

    assert hb_executor.FROZEN_PEAK_EVIDENCE_TIMEOUT_S == 12.0
    rig = Rig(tmp_path)
    rec = install(rig)
    basis = rig.adapter._exchange.entry_basis.return_value

    async def slow_basis(*_args):
        await asyncio.sleep(2.05)
        rig.t = RIG_START + timedelta(seconds=3)
        return basis

    async def fresh_book(_symbol):
        return ExecutionBook(rec.symbol, 102, 102.1, rig.t, rig.t)

    rig.adapter._exchange.entry_basis.side_effect = slow_basis
    rig.adapter._exchange.execution_book.side_effect = fresh_book
    result = await asyncio.wait_for(
        rig.adapter.frozen_peak_review(rec.position_id, RIG_START), timeout=3
    )
    assert result["peak_price"] == 102 and result["detail"] == ""
    assert not result["triggered"]


async def test_late_reader_cannot_overwrite_newer_frozen_state(tmp_path):
    from tests.test_execution import START as RIG_START
    from tests.test_execution import Rig
    from tests.test_frozen_peak_exit_integration import install

    rig = Rig(tmp_path)
    rec = install(rig)
    entered, release = asyncio.Event(), asyncio.Event()

    async def book(_symbol):
        entered.set()
        await release.wait()
        return ExecutionBook(rec.symbol, 102, 102.1, rig.t, rig.t)

    rig.adapter._exchange.execution_book.side_effect = book
    sample_task = asyncio.create_task(rig.adapter.frozen_peak_review(rec.position_id, RIG_START))
    await entered.wait()
    tracked = rig.registry.get(rec.position_id)
    newer = tracked.frozen_peak_exit.model_copy(update={"status": "unknown", "detail": "newer"})
    rig.registry.put(tracked.model_copy(update={"frozen_peak_exit": newer}))
    release.set()
    assert await sample_task is None
    assert rig.registry.get(rec.position_id).frozen_peak_exit == newer


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
def test_confirmed_touch_is_durable_even_after_adverse_price_or_missing_fee(side):
    rec, basis = evidence(side)
    peak, adverse = (102, 95) if side is Side.LONG else (98, 105)
    state = step(rec, basis, FrozenPeakExit(after_s=60, exit_fee_bps=5), 20, peak)
    state = step(rec, basis, state, 60, peak)
    state = step(rec, basis, state, 61, peak)
    assert state.status == "triggered"
    saved = FrozenPeakExit.model_validate_json(state.model_dump_json())
    assert step(rec, basis, saved, 65, adverse) == saved
    # A previously committed exit no longer needs fee evidence to trigger again.
    assert step(rec, replace(basis, fee_usdt=float("nan")), saved, 66, adverse) == saved


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_restart_after_touch_recovers_exit_and_unknown_close_never_resubmits(tmp_path, side):
    h = AdapterHarness(tmp_path, side)
    await h.protect()
    tracked = h.registry.get("pos-1")
    rec, first = tracked.record, tracked.record.opened_at
    basis = EntryBasis(
        rec.symbol,
        "entry-1",
        "100",
        "buy" if side is Side.LONG else "sell",
        2,
        200,
        0.1,
        100,
        "FILLED",
        first,
        first,
    )
    state = FrozenPeakExit(after_s=60, exit_fee_bps=5)
    peak, adverse = (102, 95) if side is Side.LONG else (98, 105)
    for seconds in (20, 60, 61):
        now = first + timedelta(seconds=seconds)
        bid, ask = (peak, peak + 0.1) if side is Side.LONG else (peak - 0.1, peak)
        state = frozen_peak_update(
            rec, state, basis, ExecutionBook(rec.symbol, bid, ask, now, now), now
        )
    assert state.status == "triggered"
    h.registry.put(tracked.model_copy(update={"frozen_peak_exit": state}))
    # Simulate a crash after durable triggering but before any exit request.
    h.restart_adapter()
    h.exchange.entry_basis = AsyncMock(side_effect=ExchangeError("fee unavailable after restart"))
    h.exchange.execution_book = AsyncMock(
        return_value=ExecutionBook(rec.symbol, adverse, adverse, first, first)
    )
    result = await h.adapter.frozen_peak_review("pos-1", first + timedelta(minutes=5))
    assert result["triggered"] and result["detail"] == "pending_exit"
    h.exchange.entry_basis.assert_not_awaited()
    h.exchange.execution_book.assert_not_awaited()
    # The existing #317 close path owns submission and recovery from uncertainty.
    h.exchange.direct_unknown = True
    await h.request_close()
    assert len(h.exchange.direct) == 1
    requested = h.registry.get("pos-1")
    assert requested.record.status is PositionStatus.CLOSING
    assert requested.requested_close is CloseReason.KEEPER
    assert requested.direct_close_pending
    assert h.exchange.held == 2 and h.backups() and not h.cancels()
    h.restart_adapter()
    assert await h.adapter.frozen_peak_review("pos-1", first + timedelta(minutes=10)) is None
    await h.request_close()
    assert len(h.exchange.direct) == 1
    assert h.registry.get("pos-1").record.status is PositionStatus.CLOSING
    assert h.exchange.held == 2 and h.backups() and not h.cancels()
