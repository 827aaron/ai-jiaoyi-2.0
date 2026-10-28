"""Paper-mode stop booking against mainnet prices (``[fast_loop] paper_stop_on_mainnet``).

Demo's hosted stops trigger on Demo's mark price, which does not follow mainnet wicks, so a
simulated position can survive a wick that would have stopped it on mainnet. The main loop
watches the mainnet stream it already receives; on the first cross it books the simulated
exit at the stop price here and asks for the Demo close separately. The booking does not
depend on how fast that close happens.

The bookings live in ``data/paper_stops.json`` and never in ``positions.json``, so a ledger
written by this code stays readable by code that predates it; deleting the file only drops
the paper exits (the panel falls back to Demo's fills).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, ValidationError

from jiaoyi.contracts import PositionRecord, PositionStatus, Side

log = logging.getLogger(__name__)

PAPER_STOP_FILE = "paper_stops.json"
PAPER_STOP_DETAIL = "主网价格越过止损"
"""``close_detail`` of the Demo close this rule requests; the close record shows it."""

PriceSource = Literal["mark", "last"]


class PaperStop(BaseModel):
    """One booked paper exit. ``paper_exit_price`` replaces Demo's fill in panel accounting."""

    # Newer code may add fields; an older reader must still load the rest.
    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)

    position_id: str
    symbol: str
    side: Side
    quantity: float
    entry_price: float
    stop_price: float
    stop_source: str
    """Which candidate was tightest: ledger / hosted / trailing / tightened / lifeboat."""
    price_source: PriceSource
    cross_price: float
    """The mainnet price that crossed the stop (may be past it on a gap)."""
    crossed_at: AwareDatetime
    """Exchange timestamp of that mainnet event."""
    booked_at: AwareDatetime
    slippage_bps: float
    paper_exit_price: float
    close_requested: bool
    """False when Demo's own protection was already exiting, so only the booking was made."""


def tightest_stop(
    side: Side, candidates: Iterable[tuple[str, float | None]]
) -> tuple[str, float] | None:
    """The candidate a falling (long) or rising (short) price reaches first."""
    valid = [(src, p) for src, p in candidates if p is not None and p > 0]
    if not valid:
        return None
    if side is Side.LONG:
        return max(valid, key=lambda c: c[1])
    return min(valid, key=lambda c: c[1])


def crossed(side: Side, price: float, stop: float) -> bool:
    """Touching the stop counts: a stop-market order triggers at the stop price."""
    return price <= stop if side is Side.LONG else price >= stop


def paper_exit_price(side: Side, stop: float, slippage_bps: float) -> float:
    frac = slippage_bps / 1e4
    return stop * (1.0 - frac) if side is Side.LONG else stop * (1.0 + frac)


def booked_record(rec: PositionRecord, stop: PaperStop) -> PositionRecord:
    """``rec`` with its exit and net P&L moved to the paper fill.

    Demo's fees stay as they are: with Demo's exit known, the net P&L shifts by the price
    difference of the two exits; without it, it is rebuilt from the entry. A record that is
    not closed or has no P&L yet is returned unchanged, so a missing backfill stays visible.
    """
    if rec.status is not PositionStatus.CLOSED or rec.realized_pnl_usdt is None:
        return rec
    sign = 1.0 if rec.side is Side.LONG else -1.0
    paper = stop.paper_exit_price
    if rec.exit_price is not None:
        pnl = rec.realized_pnl_usdt + sign * rec.quantity * (paper - rec.exit_price)
    elif rec.entry_price is not None:
        pnl = sign * rec.quantity * (paper - rec.entry_price) - rec.fees_usdt
    else:
        return rec
    return rec.model_copy(update={"exit_price": paper, "realized_pnl_usdt": pnl})


def read_paper_stops(path: Path) -> dict[str, PaperStop]:
    """Every readable booking; a missing or unreadable file or entry is skipped, not raised."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        log.warning("读不懂 %s，按没有模拟止损记账处理", path)
        return {}
    out: dict[str, PaperStop] = {}
    if not isinstance(raw, dict):
        return out
    for pid, item in raw.items():
        try:
            out[str(pid)] = PaperStop.model_validate(item)
        except ValidationError:
            log.warning("跳过读不懂的模拟止损记账：%s", pid)
    return out


class PaperStopBook:
    """Write side, owned by the main loop. One booking per position, never overwritten."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._items = read_paper_stops(self._path)

    @property
    def path(self) -> Path:
        return self._path

    def get(self, position_id: str) -> PaperStop | None:
        return self._items.get(position_id)

    def items(self) -> dict[str, PaperStop]:
        return dict(self._items)

    def add(self, stop: PaperStop) -> bool:
        """False when the position already has a booking (the first cross wins)."""
        with self._lock:
            if stop.position_id in self._items:
                return False
            self._items[stop.position_id] = stop
            self._save()
            return True

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        data = {pid: s.model_dump(mode="json") for pid, s in self._items.items()}
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)


def book_stop(
    rec: PositionRecord,
    stop: tuple[str, float],
    *,
    price: float,
    price_source: PriceSource,
    crossed_at: datetime,
    now: datetime,
    slippage_bps: float,
    close_requested: bool,
) -> PaperStop:
    assert rec.entry_price is not None
    source, level = stop
    return PaperStop(
        position_id=rec.position_id,
        symbol=rec.symbol,
        side=rec.side,
        quantity=rec.quantity,
        entry_price=rec.entry_price,
        stop_price=level,
        stop_source=source,
        price_source=price_source,
        cross_price=price,
        crossed_at=crossed_at,
        booked_at=now,
        slippage_bps=slippage_bps,
        paper_exit_price=paper_exit_price(rec.side, level, slippage_bps),
        close_requested=close_requested,
    )
