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
import time
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, ValidationError

from jiaoyi.contracts import CloseReason, PositionRecord, PositionStatus, Side

log = logging.getLogger(__name__)

PAPER_STOP_FILE = "paper_stops.json"
PAPER_STOP_DETAIL = "主网价格越过止损"
"""``close_detail`` of the Demo close this rule requests; the close record shows it."""

PriceSource = Literal["mark", "last"]

_STOP_SOURCE_CN = {
    "ledger": "固定止损",
    "hosted": "交易所止损单",
    "trailing": "移动止损",
    "native_trailing": "交易所移动止损（推算）",
    "tightened": "反向卡收紧止损",
    "lifeboat": "保命单",
}


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
    """Which candidate was tightest: ledger / hosted / trailing / native_trailing / tightened /
    lifeboat."""
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


def paper_exit_price(
    side: Side, stop: float, slippage_bps: float, cross: float | None = None
) -> float:
    """The stop with slippage, or the crossing price when that is worse.

    A stop-market order fills at the market: when the first price we see is already past the
    stop (a gap, a reconnect, the rule switched on mid-move), mainnet would have filled there.
    """
    frac = slippage_bps / 1e4
    if side is Side.LONG:
        fill = stop * (1.0 - frac)
        return fill if cross is None else min(fill, cross)
    fill = stop * (1.0 + frac)
    return fill if cross is None else max(fill, cross)


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


def load_paper_stops(path: Path) -> tuple[dict[str, PaperStop], str | None]:
    """Every readable booking, and what could not be read (None = all of it). Never raises."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, None
    except (OSError, ValueError) as e:
        return {}, f"{path.name} 读不懂（{type(e).__name__}）"
    if not isinstance(raw, dict):
        return {}, f"{path.name} 不是对象"
    out: dict[str, PaperStop] = {}
    bad: list[str] = []
    for pid, item in raw.items():
        try:
            out[str(pid)] = PaperStop.model_validate(item)
        except ValidationError:
            bad.append(str(pid))
    return out, (f"{path.name} 有 {len(bad)} 条读不懂：{', '.join(bad[:5])}" if bad else None)


def read_paper_stops(path: Path) -> dict[str, PaperStop]:
    items, error = load_paper_stops(path)
    if error is not None:
        log.warning("%s，这些按没有模拟止损记账处理", error)
    return items


class PaperStopCache:
    """Panel read side: re-parses the file only when its mtime or size changes."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._stamp: tuple[int, int] | None = None
        self._items: dict[str, PaperStop] = {}
        self._lock = threading.Lock()

    def get(self) -> dict[str, PaperStop]:
        try:
            st = self._path.stat()
            stamp: tuple[int, int] | None = (st.st_mtime_ns, st.st_size)
        except OSError:
            stamp = None
        with self._lock:
            if stamp != self._stamp:
                self._items = read_paper_stops(self._path) if stamp is not None else {}
                self._stamp = stamp
            return self._items


class PaperStopBook:
    """Write side, owned by the main loop. One booking per position, never overwritten."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._items, self.load_error = load_paper_stops(self._path)
        """What could not be read at start (None = all of it); the loop alerts on it once."""
        self._set_aside = self.load_error is None
        self.dirty = False
        """A booking is in memory but the last write failed; ``flush`` retries it."""

    @property
    def path(self) -> Path:
        return self._path

    def get(self, position_id: str) -> PaperStop | None:
        return self._items.get(position_id)

    def items(self) -> dict[str, PaperStop]:
        return dict(self._items)

    def add(self, stop: PaperStop) -> bool:
        """False when the position already has a booking (the first cross wins).

        The booking counts from here even when the write fails: ``dirty`` stays set and
        ``flush`` retries, so a full disk does not undo a cross already acted on.
        """
        with self._lock:
            if stop.position_id in self._items:
                return False
            self._items[stop.position_id] = stop
            self.dirty = True
            self._try_save()
            return True

    def flush(self) -> bool:
        """Retry a failed write; True when nothing is left unwritten."""
        with self._lock:
            if self.dirty:
                self._try_save()
            return not self.dirty

    def _try_save(self) -> None:
        try:
            self._save()
            self.dirty = False
        except OSError:
            log.exception("模拟止损记账写盘失败，稍后重试：%s", self._path)

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._set_aside:
            # Keep the unreadable file for inspection instead of overwriting it.
            if self._path.exists():
                os.replace(
                    self._path, self._path.with_name(f"{self._path.name}.bad-{int(time.time())}")
                )
            self._set_aside = True
        tmp = self._path.with_suffix(".tmp")
        data = {pid: s.model_dump(mode="json") for pid, s in self._items.items()}
        with tmp.open("w", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False, indent=2))
            f.flush()
            os.fsync(f.fileno())
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
        paper_exit_price=paper_exit_price(rec.side, level, slippage_bps, price),
        close_requested=close_requested,
    )


def stop_source_cn(source: str) -> str:
    return _STOP_SOURCE_CN.get(source, source)


def close_detail(booked: PaperStop) -> str:
    """The noted close detail: which stop the mainnet price crossed, and at what price.

    Starts with ``PAPER_STOP_DETAIL`` because the executor recognises the note by that prefix.
    """
    src = stop_source_cn(booked.stop_source)
    price = "标记价" if booked.price_source == "mark" else "成交价"
    return (
        f"{PAPER_STOP_DETAIL}：主网{price} {booked.cross_price:g} 碰到{src} {booked.stop_price:g}"
    )


def close_reason(booked: PaperStop | None) -> CloseReason:
    """The reason the Demo close is filed under.

    A stop on the profit side of entry can only be a trailing / breakeven one, whichever level
    carried it (an exchange stop leg that the trailing rule already moved shows up as
    ``hosted``), so a winning exit is never filed as a stop loss.
    """
    if booked is None:
        return CloseReason.STOP_LOSS
    long = booked.side is Side.LONG
    if (
        (booked.stop_price > booked.entry_price)
        if long
        else (booked.stop_price < booked.entry_price)
    ):
        return CloseReason.TRAILING_STOP
    return {
        "trailing": CloseReason.TRAILING_STOP,
        "native_trailing": CloseReason.TRAILING_STOP,
        "tightened": CloseReason.TIGHTENED_STOP,
        "lifeboat": CloseReason.LIFEBOAT,
    }.get(booked.stop_source, CloseReason.STOP_LOSS)
