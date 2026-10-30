"""Freeze an observed executable profit target for newly opted-in positions."""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from jiaoyi.contracts import PositionRecord, Side
from jiaoyi.execution.exchange import EntryBasis, ExchangeError, ExecutionBook

BOOK_MAX_AGE_S = 5.0
"""判断时盘口源时刻最多多旧；也是两次观测之间算“断流”的间隔。"""


class FrozenPeakExit(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    version: Literal[1] = 1
    after_s: int = Field(gt=0)
    exit_fee_bps: float = Field(ge=0, le=100)
    entry_order_id: str | None = None
    first_fill_at: AwareDatetime | None = None
    last_fill_at: AwareDatetime | None = None
    entry_quantity: float | None = None
    entry_quote: float | None = None
    entry_fee: float | None = None
    peak_price: float | None = None
    peak_net_usdt: str | None = None
    peak_source_at: AwareDatetime | None = None
    last_source_at: AwareDatetime | None = None
    frozen_at: AwareDatetime | None = None
    target_price: float | None = None
    observation_gap: bool = False
    status: Literal[
        "collecting", "frozen", "no_positive_peak", "unknown", "triggered", "trailing_active"
    ] = "collecting"
    """``trailing_active``：移动止损激活过，这笔不是平静仓，规则对它永久不再起作用。"""
    detail: str = ""


def trailing_active(protection: object) -> bool:
    """程序自己跟的移动止损已激活，或交易所移动止损的本地推算已越过激活价。两者落盘后都不回退。"""
    return bool(
        getattr(protection, "trailing_activated", False)
        or getattr(protection, "native_activated", False)
    )


def drop_for_trailing(state: FrozenPeakExit) -> FrozenPeakExit:
    """这条规则只管移动止损一直没激活的平静仓：激活了的跑单在冻结时刻正贴着峰值，按冻结价
    回访会马上把它平掉。冻结前激活就不再冻结，冻结后激活就丢掉目标价；峰值和冻结时刻留作记录。
    已触发的不改（平仓已在请求）。"""
    if state.status in ("triggered", "trailing_active"):
        return state
    return state.model_copy(
        update={"status": "trailing_active", "target_price": None, "detail": "trailing_activated"}
    )


def frozen_peak_update(
    rec: PositionRecord,
    state: FrozenPeakExit,
    basis: EntryBasis,
    book: ExecutionBook,
    now: datetime,
) -> FrozenPeakExit:
    if state.status in ("triggered", "trailing_active"):
        return state
    times = (basis.first_fill_at, basis.last_fill_at, book.source_at, book.observed_at, now)
    if any(t is None or t.tzinfo is None or t.utcoffset() is None for t in times):
        raise ExchangeError("Frozen peak requires exchange fill and book timestamps")
    first, last = basis.first_fill_at, basis.last_fill_at
    assert first is not None and last is not None
    if (
        basis.symbol != rec.symbol
        or book.symbol != rec.symbol
        or basis.side != ("buy" if rec.side is Side.LONG else "sell")
        or basis.order_status != "FILLED"
        or not all(
            math.isfinite(v)
            for v in (
                basis.filled_quantity,
                basis.filled_quote,
                basis.fee_usdt,
                basis.entry_price,
                book.bid,
                book.ask,
                rec.quantity,
            )
        )
        or min(basis.filled_quantity, basis.filled_quote, basis.entry_price, book.bid) <= 0
        or book.ask < book.bid
        or not math.isclose(rec.quantity, basis.filled_quantity, rel_tol=1e-10)
        or not math.isclose(
            basis.entry_price * basis.filled_quantity, basis.filled_quote, rel_tol=1e-10
        )
        or not rec.opened_at <= first <= last <= book.source_at <= book.observed_at <= now
        or (now - book.source_at).total_seconds() > BOOK_MAX_AGE_S
    ):
        raise ExchangeError("Frozen peak entry, quantity or executable book evidence is invalid")
    identity = (
        basis.order_id,
        first,
        last,
        basis.filled_quantity,
        basis.filled_quote,
        basis.fee_usdt,
    )
    previous = (
        state.entry_order_id,
        state.first_fill_at,
        state.last_fill_at,
        state.entry_quantity,
        state.entry_quote,
        state.entry_fee,
    )
    if state.entry_order_id is not None and identity != previous:
        return state.model_copy(update={"status": "unknown", "detail": "entry_basis_changed"})
    if state.status == "unknown":
        return state
    if state.last_source_at is not None and book.source_at <= state.last_source_at:
        raise ExchangeError("Frozen peak book is duplicate or out of order")
    updates: dict[str, object] = dict(
        entry_order_id=basis.order_id,
        first_fill_at=first,
        last_fill_at=last,
        entry_quantity=basis.filled_quantity,
        entry_quote=basis.filled_quote,
        entry_fee=basis.fee_usdt,
        last_source_at=book.source_at,
        observation_gap=state.observation_gap
        or (
            state.last_source_at is None
            and (book.source_at - first).total_seconds() > BOOK_MAX_AGE_S
        )
        or bool(
            state.last_source_at is not None
            and (book.source_at - state.last_source_at).total_seconds() > BOOK_MAX_AGE_S
        ),
        detail="",
    )
    price = Decimal(str(book.bid if rec.side is Side.LONG else book.ask))
    qty, quote, fee = (
        Decimal(str(v)) for v in (basis.filled_quantity, basis.filled_quote, basis.fee_usdt)
    )
    sign = Decimal(1 if rec.side is Side.LONG else -1)
    exit_fee = price * qty * Decimal(str(state.exit_fee_bps)) / Decimal(10000)
    net = sign * (price * qty - quote) - fee - exit_fee
    deadline = first + timedelta(seconds=state.after_s)
    if now < deadline and book.source_at < deadline:
        old_peak = Decimal(state.peak_net_usdt) if state.peak_net_usdt else Decimal(0)
        if not old_peak.is_finite():
            raise ExchangeError("Frozen peak persisted profit is invalid")
        if net > max(old_peak, Decimal(0)):
            updates.update(
                peak_price=float(price), peak_net_usdt=str(net), peak_source_at=book.source_at
            )
        return state.model_copy(update=updates)
    if state.frozen_at is None:
        updates.update(frozen_at=now, target_price=state.peak_price)
        updates["status"] = "frozen" if state.peak_price is not None else "no_positive_peak"
        return state.model_copy(update=updates)
    target = state.target_price
    if target is not None and not math.isfinite(target):
        raise ExchangeError("Frozen peak persisted target is invalid")
    crossed = False
    if target is not None:
        if (
            target <= 0
            or target != state.peak_price
            or state.peak_source_at is None
            or state.peak_source_at >= deadline
            or state.peak_net_usdt is None
            or not Decimal(state.peak_net_usdt).is_finite()
            or Decimal(state.peak_net_usdt) <= 0
        ):
            raise ExchangeError("Frozen peak target evidence is invalid")
        crossed = (
            price >= Decimal(str(target))
            if rec.side is Side.LONG
            else (price <= Decimal(str(target)))
        )
    if book.source_at > state.frozen_at and crossed and net > 0:
        updates["status"] = "triggered"
    return state.model_copy(update=updates)


def frozen_peak_evidence(
    state: FrozenPeakExit,
    side: Side,
    stop_loss_pct: float,
    trigger_price: float | None = None,
) -> dict[str, float]:
    """Record-only size of a frozen peak, kept to judge a possible minimum-peak threshold.

    R is the gross executable move from the actual fill VWAP over the initial stop distance
    (``stop_loss_pct`` of that entry price, as the server stop is placed). ``peak_net_bps`` is
    the rule's own net profit at the peak (actual entry fee plus estimated exit taker fee) over
    the entry quote. Empty until a positive peak has been frozen; the exit decision never
    reads it.
    """
    if (
        state.frozen_at is None
        or state.peak_price is None
        or state.peak_net_usdt is None
        or state.peak_source_at is None
        or state.first_fill_at is None
        or not state.entry_quote
        or not state.entry_quantity
    ):
        return {}
    entry = state.entry_quote / state.entry_quantity
    sign = 1 if side is Side.LONG else -1
    out = {
        "peak_net_bps": round(float(state.peak_net_usdt) / state.entry_quote * 1e4, 2),
        "peak_after_min": round(
            (state.peak_source_at - state.first_fill_at).total_seconds() / 60, 2
        ),
    }
    if trigger_price is not None:
        out["trigger_price"] = trigger_price
    if not (math.isfinite(stop_loss_pct) and stop_loss_pct > 0):
        return out
    risk = entry * stop_loss_pct / 100
    out["peak_gain_r"] = round(sign * (state.peak_price - entry) / risk, 4)
    if trigger_price is not None:
        out["trigger_gain_r"] = round(sign * (trigger_price - entry) / risk, 4)
    return out
