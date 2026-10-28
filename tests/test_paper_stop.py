"""Paper stop on mainnet prices (``[fast_loop] paper_stop_on_mainnet``). No network."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from jiaoyi import contracts as c
from jiaoyi.alerting.stub import StubAlerter
from jiaoyi.contracts.position import CloseReason, PositionStatus
from jiaoyi.core import paper_stop as ps
from jiaoyi.core.config import load_risk_config
from jiaoyi.execution.registry import ProtectiveLeg, ServerProtection
from jiaoyi.loop import Runner
from jiaoyi.panel.files import PanelFiles
from jiaoyi.panel.hb import HbSnapshot
from jiaoyi.panel.overview import compute_kpis
from jiaoyi.panel.pages import _pnl_periods, build_funds, build_history
from tests.conftest import ROOT
from tests.test_execution import Rig, open_filled
from tests.test_loop import FakeExecutor, _app, _position

LONG = c.Side.LONG
SHORT = c.Side.SHORT
ENTRY = 68044.0
# Mirrored around the entry: stop 170.1 away, lifeboat 255.2 away.
STOP = {LONG: 67873.9, SHORT: 68214.1}
LIFEBOAT = {LONG: 67788.8, SHORT: 68299.2}
SIGN = {LONG: 1.0, SHORT: -1.0}


def _pos(side: c.Side, **over: Any) -> c.PositionRecord:
    lifeboat = {
        "client_order_id": "lb-pos-0001",
        "stop_price": LIFEBOAT[side],
        "status": "active",
        "updated_at": "2026-10-22T06:17:01Z",
    }
    return _position(side=side, stop_loss_price=STOP[side], lifeboat=lifeboat, **over)


def _beyond(side: c.Side, bps: float) -> float:
    """A price ``bps`` past the stop on the losing side (negative = short of it)."""
    return STOP[side] * (1.0 - SIGN[side] * bps / 1e4)


class LedgerExecutor(FakeExecutor):
    """Ledger-backed fake: ``close`` ends the position, as a filled Demo close would."""

    def __init__(self, held: list[c.PositionRecord]) -> None:
        super().__init__()
        self.held = held
        self.levels: dict[str, list[tuple[str, float | None]]] = {}
        self.details: list[tuple[str, str]] = []
        self.exiting: set[str] = set()
        self.tp_exiting: set[str] = set()
        self.since: dict[str, datetime] = {}
        self.keep_open = False

    def live_positions(self) -> list[c.PositionRecord]:
        return list(self.held)

    async def close(self, position_id: str, reason: CloseReason) -> None:
        await super().close(position_id, reason)
        if self.keep_open:  # close sent, not filled yet
            self.held = [
                p.model_copy(update={"status": PositionStatus.CLOSING})
                if p.position_id == position_id
                else p
                for p in self.held
            ]
        else:
            self.held = [p for p in self.held if p.position_id != position_id]

    def paper_stop_levels(self, position_id: str) -> list[tuple[str, float | None]]:
        pos = next(p for p in self.held if p.position_id == position_id)
        return self.levels.get(position_id) or [("ledger", pos.stop_loss_price)]

    def note_close_detail(self, position_id: str, detail: str) -> None:
        self.details.append((position_id, detail))

    def paper_stop_since(self, position_id: str) -> datetime | None:
        return self.since.get(position_id)

    def protection_exit_in_flight(self, position_id: str) -> tuple[str, str, str] | None:
        exiting = position_id in self.exiting | self.tp_exiting
        return ("exiting", "exiting", "leg_triggered") if exiting else None

    def take_profit_exiting(self, position_id: str) -> bool:
        return position_id in self.tp_exiting


def _runner(
    tmp_path: Path,
    now: datetime,
    pos: c.PositionRecord,
    *,
    mode: str = "last",
    run_mode: c.RunMode = c.RunMode.PAPER,
    **fast: Any,
) -> tuple[Runner, LedgerExecutor, StubAlerter]:
    app = _app(tmp_path, now)
    ex = LedgerExecutor([pos])
    alerts = StubAlerter()
    # The real patrol would close on the same moves (its shock line is one stop distance).
    app.m = dataclasses.replace(app.m, executor=ex, alerter=alerts, patrol=_Patrol())
    fl = app.app_cfg.fast_loop.model_copy(update={"paper_stop_on_mainnet": mode, **fast})
    app.app_cfg = app.app_cfg.model_copy(update={"fast_loop": fl, "mode": run_mode})
    runner = Runner(app)
    runner.positions = [pos]
    return runner, ex, alerts


async def _arm(runner: Runner, now: datetime, mark: bool = False) -> None:
    """A mainnet price on the safe side, which a cross needs to have been preceded by."""
    await runner.on_event(_mark(now, ENTRY) if mark else _trade(now, ENTRY))


def _trade(now: datetime, price: float) -> c.Trade:
    return c.Trade(symbol="BTCUSDT", ts=now, price=price, qty=0.01, taker_side=LONG)


def _mark(now: datetime, price: float) -> c.MarkPrice:
    return c.MarkPrice(symbol="BTCUSDT", ts=now, mark_price=price, funding_rate=0.0001)


async def _settle(runner: Runner) -> None:
    for _ in range(5):
        await asyncio.sleep(0)
    if runner._tasks:
        await asyncio.wait_for(asyncio.gather(*runner._tasks), 1)


# ---------------------------------------------------------------- pure helpers


@pytest.mark.parametrize("side", [LONG, SHORT])
def test_paper_stop_tightest_stop_is_first_reached(side: c.Side) -> None:
    trailing = ENTRY + SIGN[side] * 50.0  # trailing stop on the profit side of the entry
    got = ps.tightest_stop(
        side,
        [("ledger", STOP[side]), ("lifeboat", LIFEBOAT[side]), ("trailing", trailing), ("x", None)],
    )
    assert got == ("trailing", trailing)
    assert ps.tightest_stop(side, [("ledger", STOP[side]), ("lifeboat", LIFEBOAT[side])]) == (
        "ledger",
        STOP[side],
    )
    assert ps.tightest_stop(side, [("tightened", None)]) is None


@pytest.mark.parametrize("side", [LONG, SHORT])
def test_paper_stop_crossed_counts_touch_and_beyond_only(side: c.Side) -> None:
    assert ps.crossed(side, STOP[side], STOP[side])
    assert ps.crossed(side, _beyond(side, 5), STOP[side])
    assert not ps.crossed(side, _beyond(side, -1), STOP[side])


def test_paper_stop_exit_price_slips_against_the_position() -> None:
    assert ps.paper_exit_price(LONG, 100.0, 2.0) == pytest.approx(99.98)
    assert ps.paper_exit_price(SHORT, 100.0, 2.0) == pytest.approx(100.02)
    assert ps.paper_exit_price(LONG, 100.0, 0.0) == 100.0


def test_paper_stop_exit_price_takes_the_worse_of_slipped_stop_and_cross() -> None:
    assert ps.paper_exit_price(LONG, 100.0, 2.0, 99.99) == pytest.approx(99.98)
    assert ps.paper_exit_price(LONG, 100.0, 2.0, 99.5) == 99.5  # gapped through
    assert ps.paper_exit_price(SHORT, 100.0, 2.0, 100.01) == pytest.approx(100.02)
    assert ps.paper_exit_price(SHORT, 100.0, 2.0, 100.5) == 100.5


# ---------------------------------------------------------------- main loop


@pytest.mark.parametrize("side", [LONG, SHORT])
async def test_paper_stop_last_cross_closes_demo_and_books_stop(
    tmp_path: Path, now: datetime, side: c.Side
) -> None:
    pos = _pos(side)
    runner, ex, alerts = _runner(tmp_path, now, pos, mode="last")
    await _arm(runner, now)
    await runner.on_event(_trade(now, _beyond(side, -1)))  # 1 bp short of the stop
    await _settle(runner)
    assert ex.closed == [] and runner._paper_stops.get(pos.position_id) is None

    wick = _beyond(side, 30)
    await runner.on_event(_trade(now, wick))
    await _settle(runner)
    assert ex.closed == [(pos.position_id, CloseReason.STOP_LOSS)]
    assert ex.details == [(pos.position_id, "主网价格越过止损")]
    booked = runner._paper_stops.get(pos.position_id)
    assert booked is not None
    assert booked.stop_price == STOP[side] and booked.stop_source == "ledger"
    assert booked.cross_price == wick and booked.price_source == "last"
    assert booked.paper_exit_price == wick  # 30 bp past the stop: worse than stop - 2 bp
    assert booked.close_requested is True
    on_disk = json.loads((tmp_path / "paper_stops.json").read_text(encoding="utf-8"))
    assert on_disk[pos.position_id]["paper_exit_price"] == booked.paper_exit_price
    assert [a.title for a in alerts.sent].count("主网价格越过止损") == 1


@pytest.mark.parametrize("side", [LONG, SHORT])
@pytest.mark.parametrize("run_mode", [c.RunMode.PAPER, c.RunMode.TESTNET])
async def test_paper_stop_mark_mode_ignores_trades_and_uses_mark(
    tmp_path: Path, now: datetime, side: c.Side, run_mode: c.RunMode
) -> None:
    pos = _pos(side)
    runner, ex, _ = _runner(tmp_path, now, pos, mode="mark", run_mode=run_mode)
    await _arm(runner, now)  # a trade does not arm mark mode
    await runner.on_event(_trade(now, _beyond(side, 50)))
    await _settle(runner)
    await _arm(runner, now, mark=True)
    await _settle(runner)
    assert ex.closed == []
    await runner.on_event(_mark(now, _beyond(side, 1)))
    await _settle(runner)
    assert ex.closed == [(pos.position_id, CloseReason.STOP_LOSS)]
    booked = runner._paper_stops.get(pos.position_id)
    assert booked is not None and booked.price_source == "mark"
    # 1 bp past is better than stop - 2 bp: the slipped stop is booked.
    assert booked.paper_exit_price == pytest.approx(STOP[side] * (1 - SIGN[side] * 2e-4))


@pytest.mark.parametrize("side", [LONG, SHORT])
async def test_paper_stop_last_mode_ignores_mark(
    tmp_path: Path, now: datetime, side: c.Side
) -> None:
    pos = _pos(side)
    runner, ex, _ = _runner(tmp_path, now, pos, mode="last")
    await _arm(runner, now, mark=True)
    await _arm(runner, now)
    await runner.on_event(_mark(now, _beyond(side, 50)))
    await _settle(runner)
    assert ex.closed == [] and runner._paper_stops.get(pos.position_id) is None


@pytest.mark.parametrize("side", [LONG, SHORT])
async def test_paper_stop_uses_tightest_executor_level(
    tmp_path: Path, now: datetime, side: c.Side
) -> None:
    pos = _pos(side)
    runner, ex, _ = _runner(tmp_path, now, pos, paper_stop_slippage_bps=0.0)
    trailing = ENTRY + SIGN[side] * 20.0
    ex.levels[pos.position_id] = [("ledger", STOP[side]), ("trailing", trailing)]
    await runner.on_event(_trade(now, ENTRY + SIGN[side] * 40.0))  # safe side of the trailing
    await _settle(runner)
    assert ex.closed == []
    await runner.on_event(_trade(now, trailing))
    await _settle(runner)
    booked = runner._paper_stops.get(pos.position_id)
    assert booked is not None and (booked.stop_source, booked.stop_price) == ("trailing", trailing)
    assert booked.paper_exit_price == trailing


@pytest.mark.parametrize("side", [LONG, SHORT])
async def test_paper_stop_never_acts_in_live(tmp_path: Path, now: datetime, side: c.Side) -> None:
    pos = _pos(side)
    runner, ex, alerts = _runner(tmp_path, now, pos, mode="last", run_mode=c.RunMode.LIVE)
    await _arm(runner, now)
    await runner.on_event(_trade(now, _beyond(side, 100)))
    runner._paper_stop_retry()
    await _settle(runner)
    assert ex.closed == [] and ex.details == [] and alerts.sent == []
    assert not (tmp_path / "paper_stops.json").exists()


@pytest.mark.parametrize("side", [LONG, SHORT])
async def test_paper_stop_default_off_changes_nothing(
    tmp_path: Path, now: datetime, side: c.Side
) -> None:
    assert c.FastLoopConfig().paper_stop_on_mainnet == "off"
    pos = _pos(side)
    app = _app(tmp_path, now)
    ex = LedgerExecutor([pos])
    app.m = dataclasses.replace(app.m, executor=ex, patrol=_Patrol())
    runner = Runner(app)
    runner.positions = [pos]
    await _arm(runner, now)
    await _arm(runner, now, mark=True)
    await runner.on_event(_trade(now, _beyond(side, 100)))
    await runner.on_event(_mark(now, _beyond(side, 100)))
    await _settle(runner)
    assert ex.closed == [] and not (tmp_path / "paper_stops.json").exists()


@pytest.mark.parametrize("side", [LONG, SHORT])
async def test_paper_stop_books_once_and_closes_once(
    tmp_path: Path, now: datetime, side: c.Side
) -> None:
    pos = _pos(side)
    runner, ex, alerts = _runner(tmp_path, now, pos)
    ex.keep_open = True  # Demo close not filled yet: the ledger still says OPEN
    first = _beyond(side, 10)
    await _arm(runner, now)
    await runner.on_event(_trade(now, first))
    await runner.on_event(_trade(now, _beyond(side, 40)))
    await _settle(runner)
    assert ex.closed == [(pos.position_id, CloseReason.STOP_LOSS)]
    booked = runner._paper_stops.get(pos.position_id)
    assert booked is not None and booked.cross_price == first
    assert [a.title for a in alerts.sent].count("主网价格越过止损") == 1
    # Within the retry window the periodic refresh does not ask again.
    runner._paper_stop_retry()
    await _settle(runner)
    assert len(ex.closed) == 1


@pytest.mark.parametrize("side", [LONG, SHORT])
async def test_paper_stop_retries_close_after_restart(
    tmp_path: Path, now: datetime, side: c.Side
) -> None:
    pos = _pos(side)
    runner, ex, _ = _runner(tmp_path, now, pos)
    ex.keep_open = True
    await _arm(runner, now)
    await runner.on_event(_trade(now, _beyond(side, 10)))
    await _settle(runner)
    later = now + timedelta(seconds=31)
    restarted, ex2, alerts2 = _runner(tmp_path, later, pos)  # Demo still holds it OPEN
    assert restarted._paper_stops.get(pos.position_id) is not None
    await restarted.on_event(_trade(later, ENTRY))  # back inside: no new cross, no re-booking
    restarted._paper_stop_retry()
    await _settle(restarted)
    assert ex2.closed == [(pos.position_id, CloseReason.STOP_LOSS)]
    assert alerts2.sent == []


@pytest.mark.parametrize("side", [LONG, SHORT])
async def test_paper_stop_hosted_exit_in_flight_only_books(
    tmp_path: Path, now: datetime, side: c.Side
) -> None:
    pos = _pos(side)
    runner, ex, _ = _runner(tmp_path, now, pos)
    ex.exiting.add(pos.position_id)
    await _arm(runner, now)
    await runner.on_event(_trade(now, _beyond(side, 10)))
    runner._paper_stop_retry()
    await _settle(runner)
    assert ex.closed == [] and ex.details == []
    booked = runner._paper_stops.get(pos.position_id)
    assert booked is not None and booked.close_requested is False


@pytest.mark.parametrize("side", [LONG, SHORT])
async def test_paper_stop_take_profit_leg_exiting_books_nothing(
    tmp_path: Path, now: datetime, side: c.Side
) -> None:
    pos = _pos(side)
    runner, ex, alerts = _runner(tmp_path, now, pos)
    ex.tp_exiting.add(pos.position_id)
    await _arm(runner, now)
    await runner.on_event(_trade(now, _beyond(side, 10)))
    await _settle(runner)
    assert ex.closed == [] and alerts.sent == []
    assert runner._paper_stops.get(pos.position_id) is None


@pytest.mark.parametrize("side", [LONG, SHORT])
async def test_paper_stop_skips_position_already_closing(
    tmp_path: Path, now: datetime, side: c.Side
) -> None:
    pos = _pos(side)
    runner, ex, _ = _runner(tmp_path, now, pos)
    ex.held = [pos.model_copy(update={"status": PositionStatus.CLOSING})]
    await _arm(runner, now)
    await runner.on_event(_trade(now, _beyond(side, 10)))
    await _settle(runner)
    assert ex.closed == [] and runner._paper_stops.get(pos.position_id) is None
    runner2, ex2, _ = _runner(tmp_path / "b", now, pos)
    runner2._closing.add(pos.position_id)
    await _arm(runner2, now)
    await runner2.on_event(_trade(now, _beyond(side, 10)))
    await _settle(runner2)
    assert ex2.closed == [] and runner2._paper_stops.get(pos.position_id) is None


@pytest.mark.parametrize("side", [LONG, SHORT])
async def test_paper_stop_needs_safe_side_before_cross(
    tmp_path: Path, now: datetime, side: c.Side
) -> None:
    pos = _pos(side)
    runner, ex, _ = _runner(tmp_path, now, pos)
    # Restart / hot enable / fresh fill with the price already past the stop: not a cross.
    await runner.on_event(_trade(now, _beyond(side, 10)))
    await _settle(runner)
    assert ex.closed == [] and runner._paper_stops.get(pos.position_id) is None
    await _arm(runner, now)
    await runner.on_event(_trade(now, _beyond(side, 10)))
    await _settle(runner)
    assert ex.closed == [(pos.position_id, CloseReason.STOP_LOSS)]


@pytest.mark.parametrize("side", [LONG, SHORT])
@pytest.mark.parametrize("source", ["trailing", "hosted"])
async def test_paper_stop_new_level_placed_past_price_waits_for_safe_side(
    tmp_path: Path, now: datetime, side: c.Side, source: str
) -> None:
    pos = _pos(side)
    runner, ex, _ = _runner(tmp_path, now, pos, paper_stop_slippage_bps=0.0)
    price = ENTRY + SIGN[side] * 30.0
    await runner.on_event(_trade(now, price))  # safe against the ledger stop
    # A leg placed off Demo's mark lands 2 past mainnet's price (the basis).
    leg = price + SIGN[side] * 2.0
    ex.levels[pos.position_id] = [("ledger", STOP[side]), (source, leg)]
    await runner.on_event(_trade(now, price))
    await _settle(runner)
    assert ex.closed == [] and runner._paper_stops.get(pos.position_id) is None
    await runner.on_event(_trade(now, leg + SIGN[side] * 1.0))  # mainnet clears the leg
    await runner.on_event(_trade(now, leg - SIGN[side] * 1.0))  # then crosses it
    await _settle(runner)
    booked = runner._paper_stops.get(pos.position_id)
    assert booked is not None and (booked.stop_source, booked.stop_price) == (source, leg)
    assert booked.paper_exit_price == leg - SIGN[side] * 1.0


@pytest.mark.parametrize("side", [LONG, SHORT])
async def test_paper_stop_ignores_prices_before_the_position_existed(
    tmp_path: Path, now: datetime, side: c.Side
) -> None:
    pos = _pos(side)
    runner, ex, _ = _runner(tmp_path, now, pos)
    before = pos.opened_at - timedelta(seconds=1)
    await runner.on_event(_trade(before, ENTRY))
    await runner.on_event(_trade(before, _beyond(side, 10)))
    await _settle(runner)
    assert ex.closed == [] and pos.position_id not in runner._paper_safe
    filled = now - timedelta(seconds=5)
    ex.since[pos.position_id] = filled  # limit entry filled later than it was opened
    await runner.on_event(_trade(filled - timedelta(seconds=1), ENTRY))
    assert pos.position_id not in runner._paper_safe
    await runner.on_event(_trade(filled, ENTRY))
    await runner.on_event(_trade(now, _beyond(side, 10)))
    await _settle(runner)
    assert ex.closed == [(pos.position_id, CloseReason.STOP_LOSS)]


class _Patrol:
    def __init__(self) -> None:
        self.seen = 0
        self.urgent: set[str] = set()

    def check(self, event: object, positions: object) -> None:
        self.seen += 1


@pytest.mark.parametrize("side", [LONG, SHORT])
async def test_paper_stop_error_does_not_stop_patrol(
    tmp_path: Path, now: datetime, side: c.Side, monkeypatch: pytest.MonkeyPatch
) -> None:
    pos = _pos(side)
    runner, _, _ = _runner(tmp_path, now, pos)
    patrol = _Patrol()
    runner.m = runner.app.m = dataclasses.replace(runner.m, patrol=patrol)

    def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("synthetic")

    monkeypatch.setattr(runner, "_paper_stop_hit", boom)
    await _arm(runner, now)
    await runner.on_event(_trade(now, _beyond(side, 10)))
    assert patrol.seen == 2


@pytest.mark.parametrize("side", [LONG, SHORT])
async def test_paper_stop_write_failure_still_closes_and_retries_write(
    tmp_path: Path, now: datetime, side: c.Side, monkeypatch: pytest.MonkeyPatch
) -> None:
    pos = _pos(side)
    runner, ex, _ = _runner(tmp_path, now, pos)
    book = runner._paper_stops
    real_save = book._save
    fail = [True]

    def flaky() -> None:
        if fail[0]:
            raise OSError("disk full")
        real_save()

    monkeypatch.setattr(book, "_save", flaky)
    await _arm(runner, now)
    await runner.on_event(_trade(now, _beyond(side, 10)))
    await _settle(runner)
    assert ex.closed == [(pos.position_id, CloseReason.STOP_LOSS)]
    assert book.dirty and not (tmp_path / "paper_stops.json").exists()
    runner._paper_stop_retry()
    assert book.dirty
    fail[0] = False
    runner._paper_stop_retry()
    assert not book.dirty
    on_disk = json.loads((tmp_path / "paper_stops.json").read_text(encoding="utf-8"))
    assert pos.position_id in on_disk


async def test_paper_stop_only_on_demo(tmp_path: Path, now: datetime) -> None:
    pos = _pos(LONG)
    runner, ex, _ = _runner(tmp_path, now, pos)
    cfg = runner.app.app_cfg
    runner.app.app_cfg = cfg.model_copy(update={"sim_environment": "legacy_testnet"})
    await _arm(runner, now)
    await runner.on_event(_trade(now, _beyond(LONG, 10)))
    await _settle(runner)
    assert ex.closed == [] and runner._paper_stops.get(pos.position_id) is None


async def test_paper_stop_unreadable_file_alerts_and_is_kept(tmp_path: Path, now: datetime) -> None:
    (tmp_path / "paper_stops.json").write_text("{not json", encoding="utf-8")
    pos = _pos(LONG)
    runner, _, alerts = _runner(tmp_path, now, pos)
    runner._paper_stop_retry()
    runner._paper_stop_retry()
    await _settle(runner)
    assert [a.title for a in alerts.sent] == ["模拟止损记账文件读不懂"]
    await _arm(runner, now)
    await runner.on_event(_trade(now, _beyond(LONG, 10)))
    await _settle(runner)
    assert [p.read_text() for p in tmp_path.glob("paper_stops.json.bad-*")] == ["{not json"]
    assert pos.position_id in json.loads((tmp_path / "paper_stops.json").read_text())


# ---------------------------------------------------------------- executor


async def test_paper_stop_levels_list_every_stop_and_detail_reaches_close(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path, notices=True)
    pid = await open_filled(rig)
    tracked = rig.registry.get(pid)
    assert tracked is not None
    rec = tracked.record
    assert rec.lifeboat is not None
    levels = dict(rig.adapter.paper_stop_levels(pid))
    assert levels == {"ledger": rec.stop_loss_price, "lifeboat": rec.lifeboat.stop_price}

    legs = [
        ProtectiveLeg(
            kind="stop_loss",
            purpose="trailing",
            generation=2,
            client_order_id="sl-2",
            quantity=rec.quantity,
            trigger_price=68010.0,
            state="confirmed",
            algo_status="NEW",
            child_state="absent",
        ),
        ProtectiveLeg(
            kind="stop_loss",
            generation=1,
            client_order_id="sl-1",
            quantity=rec.quantity,
            trigger_price=67873.9,
            state="cancelled",
            algo_status="CANCELED",
            child_state="absent",
        ),
        ProtectiveLeg(
            kind="take_profit",
            generation=2,
            client_order_id="tp-2",
            quantity=rec.quantity,
            trigger_price=68384.2,
            state="confirmed",
            algo_status="NEW",
            child_state="absent",
        ),
    ]
    protection = ServerProtection(
        account_name="acct",
        connector_name="binance_perpetual_testnet",
        intended_quantity=rec.quantity,
        legs=legs,
        trailing_activated=True,
        trailing_stop_price=68020.0,
        tightened_stop_price=68030.0,
    )
    hosted = tracked.model_copy(update={"server_protection": protection})
    rig.registry.put(hosted)
    got = rig.adapter.paper_stop_levels(pid)
    assert ("hosted", 68010.0) in got and ("trailing", 68020.0) in got
    assert ("hosted", 67873.9) not in got and ("hosted", 68384.2) not in got
    assert all(src != "tightened" for src, _ in got)  # only once its leg is live
    assert ps.tightest_stop(LONG, got) == ("trailing", 68020.0)
    rig.registry.put(tracked)  # back to the HB-managed position for the close below

    rig.adapter.note_close_detail(pid, ps.PAPER_STOP_DETAIL)
    await rig.adapter.close(pid, CloseReason.STOP_LOSS)
    closed = rig.registry.get(pid)
    assert closed is not None and closed.record.close_reason is CloseReason.STOP_LOSS
    notice = [a for a in rig.alerts.sent if a.title == "平仓完成"]
    assert notice and "原因：主网价格越过止损" in notice[-1].body
    assert "close_detail" not in json.loads(rig.registry.path.read_text())["positions"][pid]


@pytest.mark.parametrize("reason", [CloseReason.KEEPER, CloseReason.RECONCILER])
async def test_paper_stop_detail_not_shown_on_other_close_reason(
    tmp_path: Path, reason: CloseReason
) -> None:
    rig = Rig(tmp_path, notices=True)
    pid = await open_filled(rig)
    rig.adapter.note_close_detail(pid, ps.PAPER_STOP_DETAIL)
    await rig.adapter.close(pid, reason)
    notice = [a for a in rig.alerts.sent if a.title == "平仓完成"]
    assert notice and "主网价格越过止损" not in notice[-1].body


async def test_paper_stop_detail_yields_to_reconciler_reason(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    pid = await open_filled(rig)
    rig.adapter.note_close_detail(pid, ps.PAPER_STOP_DETAIL)
    rig.adapter.note_close_detail(pid, "HB 报告了另一张开仓单")
    rig.adapter.note_close_detail(pid, ps.PAPER_STOP_DETAIL)
    tracked = rig.registry.get(pid)
    assert tracked is not None and tracked.close_detail == "HB 报告了另一张开仓单"


# ---------------------------------------------------------------- panel accounting


def _closed(side: c.Side, exit_price: float, pnl: float, pid: str) -> c.PositionRecord:
    return _pos(side).model_copy(
        update={
            "position_id": pid,
            "status": PositionStatus.CLOSED,
            "closed_at": datetime.fromisoformat("2026-10-22T06:20:00+00:00"),
            "close_reason": CloseReason.STOP_LOSS,
            "exit_price": exit_price,
            "realized_pnl_usdt": pnl,
            "fees_usdt": 1.0,
        }
    )


def _write_ledger(root: Path, recs: list[c.PositionRecord]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    data = {"positions": {r.position_id: {"record": r.model_dump(mode="json")} for r in recs}}
    (root / "positions.json").write_text(json.dumps(data), encoding="utf-8")


def _book(root: Path, rec: c.PositionRecord, now: datetime) -> ps.PaperStop:
    stop = ps.book_stop(
        rec,
        ("ledger", STOP[rec.side]),
        price=_beyond(rec.side, 1),
        price_source="last",
        crossed_at=now,
        now=now,
        slippage_bps=2.0,
        close_requested=True,
    )
    assert ps.PaperStopBook(root / ps.PAPER_STOP_FILE).add(stop)
    return stop


@pytest.mark.parametrize("side", [LONG, SHORT])
def test_paper_stop_panel_ledger_books_paper_exit(
    tmp_path: Path, now: datetime, side: c.Side
) -> None:
    # Demo filled 120 past the stop: the wick reverted, Demo's mark caught up much later.
    demo_exit = STOP[side] - SIGN[side] * 120.0
    qty = 0.015
    demo_pnl = SIGN[side] * qty * (demo_exit - ENTRY) - 1.0
    booked_rec = _closed(side, demo_exit, demo_pnl, "pos-booked")
    plain = _closed(side, demo_exit, demo_pnl, "pos-plain")
    _write_ledger(tmp_path, [booked_rec, plain])
    stop = _book(tmp_path, booked_rec, now)

    files = PanelFiles(tmp_path, "Asia/Bangkok")
    ledger = files.ledger()
    got = {p.position_id: p for p in ledger.positions}
    paper_pnl = SIGN[side] * qty * (stop.paper_exit_price - ENTRY) - 1.0
    assert got["pos-booked"].exit_price == stop.paper_exit_price
    assert got["pos-booked"].realized_pnl_usdt == pytest.approx(paper_pnl)
    assert got["pos-plain"] == plain
    assert ledger.demo(got["pos-booked"]) == booked_rec

    files.paper_book_pnl = lambda: False
    assert files.ledger().positions == [booked_rec, plain]


@pytest.mark.parametrize("side", [LONG, SHORT])
def test_paper_stop_booked_record_without_demo_exit_rebuilds_from_entry(side: c.Side) -> None:
    rec = _closed(side, 1.0, 0.0, "p").model_copy(update={"exit_price": None})
    stop = ps.PaperStop(
        position_id="p",
        symbol="BTCUSDT",
        side=side,
        quantity=rec.quantity,
        entry_price=ENTRY,
        stop_price=STOP[side],
        stop_source="ledger",
        price_source="mark",
        cross_price=STOP[side],
        crossed_at=rec.opened_at,
        booked_at=rec.opened_at,
        slippage_bps=0.0,
        paper_exit_price=STOP[side],
        close_requested=True,
    )
    got = ps.booked_record(rec, stop)
    assert got.realized_pnl_usdt == pytest.approx(-rec.quantity * 170.1 - 1.0)
    still_open = rec.model_copy(update={"status": PositionStatus.OPEN})
    assert ps.booked_record(still_open, stop) is still_open


@pytest.mark.parametrize("side", [LONG, SHORT])
def test_paper_stop_history_shows_both_fills_and_funds_stay_demo(
    tmp_path: Path, now: datetime, side: c.Side
) -> None:
    demo_exit = STOP[side] - SIGN[side] * 120.0
    demo_pnl = SIGN[side] * 0.015 * (demo_exit - ENTRY) - 1.0
    rec = _closed(side, demo_exit, demo_pnl, "pos-booked")
    _write_ledger(tmp_path, [rec])
    stop = _book(tmp_path, rec, now)
    files = PanelFiles(tmp_path, "Asia/Bangkok")
    risk = load_risk_config(ROOT / "config" / "risk.example.toml")
    page = build_history(files, risk, now + timedelta(hours=1), "today", include_tests=True)
    (row,) = page.rows
    assert row.exit_price == stop.paper_exit_price
    assert row.demo_exit_price == demo_exit and row.demo_pnl_usdt == pytest.approx(demo_pnl)
    assert row.paper_stop_price == STOP[side] and row.paper_price_source == "last"
    assert row.pnl_usdt > demo_pnl  # the stop fill loses less than the late Demo fill

    later = now + timedelta(hours=1)
    periods, source, _ = _pnl_periods(files, files.ledger(), later, risk.day_reset_tz)
    assert source == "ledger"
    today = next(p for p in periods if p.key == "today")
    assert today.trading_usdt == pytest.approx(demo_pnl + 1.0)
    kpis = compute_kpis(files, files.ledger(), HbSnapshot(ok=False), risk, later)
    assert kpis.day_closed_usdt == pytest.approx(demo_pnl)
    funds = build_funds(files, HbSnapshot(ok=False), risk, later)
    assert funds.realized_today_usdt == pytest.approx(demo_pnl)
