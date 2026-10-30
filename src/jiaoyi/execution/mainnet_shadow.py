"""Mainnet-equivalent P&L shadow ledger: ``data/mainnet_shadow.jsonl``.

Orders fill on Binance Demo, whose fills run a few bp better than the mainnet price at the
same moment, so Demo P&L overstates the edge. For every closed position this re-prices
both legs at the Binance mainnet price at the Demo fill times (same quantity, Demo's actual
fee rate per leg, funding ignored like the ledger's net P&L) and appends one JSON line. A
leg that filled as a resting limit order keeps its own limit price and is flagged.

It is a separate append-only file, never ``positions.json``: an older build that does not
know it simply ignores it. A line is never rewritten; a better-sourced line for the same
position is appended instead and readers keep the best one (``core.shadow_ledger.quality``),
so the CLI backfill and the running loop may both append without coordination.

The running loop makes no Binance request of its own. Prices come from what the process
already holds: the mainnet trade stream (per-second last price, five minutes deep), price
marks it takes of that stream when a position opens or closes, and the streamed 1-minute
bars. Fill times come from the Demo ``userTrades`` answers the ledger backfill already
fetched. Only the CLI may call the public mainnet REST API, and only when asked to.
"""

from __future__ import annotations

import json
import logging
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from jiaoyi.contracts import CloseReason, Interval, PositionRecord, PositionStatus, Side
from jiaoyi.core.shadow_ledger import (
    SHADOW_FILE,
    SHADOW_VERSION,
    Quality,
    quality,
    read_shadow,
)
from jiaoyi.execution.fills import (
    OPEN_SLACK,
    TradeFill,
    UserTrades,
    close_side,
    entry_fill,
    exit_fill,
    open_side,
)
from jiaoyi.market.rest import (
    DEFAULT_REST_URL,
    RateLimited,
    WeightBudget,
    fetch_json,
)

log = logging.getLogger(__name__)

MARKS_FILE = "mainnet_shadow_marks.json"
SHADOW_EVERY_S = 30.0
LOOP_BATCH = 5
MARK_WINDOW = timedelta(minutes=4)
"""Marks are taken while the trade stream still covers the moment (it keeps five minutes)."""
MARK_AFTER_OPEN = timedelta(seconds=120)
"""A limit or slow entry may fill well after ``opened_at``; keep this much of the stream."""
MARK_BEFORE_CLOSE = timedelta(seconds=120)
"""``closed_at`` is when the close was seen, after the exit fill."""
MARK_EDGE = timedelta(seconds=10)
MARK_MATCH = timedelta(seconds=5)
MARK_KEEP = timedelta(hours=30)
MAX_FAILURES = 10
LIVE_LOOKBACK = timedelta(hours=24)
"""The stream keeps 1500 one-minute bars; older closes are left to the CLI backfill."""
AGG_TRADES_WEIGHT = 20
USER_TRADES_WEIGHT = 5
LOOKAHEAD_MS = 60_000
MINUTE_S = 60.0


@dataclass(frozen=True)
class PricePoint:
    price: float
    ts: datetime
    source: str
    """``ws_trade`` / ``mark``: last mainnet trade in that second from the stream;
    ``agg_trade``: first mainnet trade at or after the moment (REST);
    ``ws_kline_1m`` / ``cache_kline_1m``: open-to-close interpolation inside the 1-minute bar."""


class MainnetPrices(Protocol):
    async def price_at(self, symbol: str, t: datetime) -> PricePoint | None: ...


def interpolate(open_: float, close: float, bar_open: datetime, t: datetime) -> float:
    frac = min(max((t - bar_open).total_seconds() / MINUTE_S, 0.0), 1.0)
    return open_ + (close - open_) * frac


FeedLookup = Callable[[str, datetime], "tuple[float, datetime, str] | None"]


class FeedPrices:
    """Prices from the running process's mainnet stream (``RollingFeatureEngine.price_at``)."""

    def __init__(self, lookup: FeedLookup) -> None:
        self._lookup = lookup

    async def price_at(self, symbol: str, t: datetime) -> PricePoint | None:
        got = self._lookup(symbol, t)
        return PricePoint(*got) if got is not None else None


Span = Callable[[str, datetime, datetime], "list[tuple[datetime, float]]"]
"""(symbol, start, end) -> per-second stream prices in that span
(``RollingFeatureEngine.prices_between``)."""


class PriceMarks:
    """Per-second mainnet stream prices kept around each entry and exit (the stream itself
    keeps five minutes), persisted so a restart between open and close loses nothing."""

    def __init__(self, data_dir: str | Path) -> None:
        self._path = Path(data_dir) / MARKS_FILE
        self._marks: dict[str, dict[int, float]] = {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            for sym, rows in raw.items():
                self._marks[sym] = {int(sec): float(p) for sec, p in rows}
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, AttributeError):
            log.warning("%s 读不出，从空的开始", self._path, exc_info=True)
            self._marks = {}

    def add(self, symbol: str, samples: Iterable[tuple[datetime, float]]) -> bool:
        got = self._marks.setdefault(symbol, {})
        before = len(got)
        for t, p in samples:
            got.setdefault(int(t.timestamp()), p)
        return len(got) != before

    def get(self, symbol: str, t: datetime) -> PricePoint | None:
        """Last sampled second at or before ``t``, at most ``MARK_MATCH`` earlier."""
        ts = t.timestamp()
        reach = MARK_MATCH.total_seconds()
        marks = self._marks.get(symbol, {})
        best = max((sec for sec in marks if 0 <= ts - sec <= reach), default=None)
        if best is None:
            return None
        return PricePoint(self._marks[symbol][best], datetime.fromtimestamp(best, UTC), "mark")

    def prune_and_save(self, now: datetime) -> None:
        cut = (now - MARK_KEEP).timestamp()
        self._marks = {
            s: kept
            for s, rows in self._marks.items()
            if (kept := {sec: p for sec, p in rows.items() if sec >= cut})
        }
        doc = {s: sorted(rows.items()) for s, rows in self._marks.items()}
        tmp = self._path.with_name(f".{self._path.name}.tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(doc), encoding="utf-8")
            tmp.replace(self._path)
        except OSError:
            log.warning("写 %s 失败", self._path, exc_info=True)

    async def price_at(self, symbol: str, t: datetime) -> PricePoint | None:
        return self.get(symbol, t)


DayBars = Callable[[str, date], "dict[int, tuple[float, float]] | None"]
"""(symbol, UTC day) -> {bar open minute (epoch // 60): (open, close)}; None = not on disk."""


def disk_day_bars(data_dir: str | Path) -> DayBars:
    """1-minute bars already on disk: ``data/history`` daily packs (open and close), else the
    market-score close cache (previous close stands in for the open). Never downloads."""
    from jiaoyi.backtest.data import load_klines

    root = Path(data_dir)

    def load(symbol: str, day: date) -> dict[int, tuple[float, float]] | None:
        pack = root / "history" / symbol / f"{symbol}-1m-{day.isoformat()}.zip"
        if pack.exists():
            try:
                return {
                    int(k.open_time.timestamp() // 60): (k.open, k.close)
                    for k in load_klines(pack, symbol, Interval.M1)
                }
            except (OSError, ValueError, KeyError, StopIteration):
                log.warning("读不出 %s", pack, exc_info=True)
        cache = root / "market_desk" / "score_cache" / "1m" / symbol / f"{day.isoformat()}.json"
        try:
            closes = json.loads(cache.read_text(encoding="utf-8"))["c"]
        except (OSError, ValueError, KeyError, TypeError):
            return None
        day0 = int(datetime.combine(day, datetime.min.time(), UTC).timestamp() // 60)
        out: dict[int, tuple[float, float]] = {}
        for i in range(1, len(closes)):
            if closes[i] is not None and closes[i - 1] is not None:
                out[day0 + i] = (float(closes[i - 1]), float(closes[i]))
        return out

    return load


class CachedKlinePrices:
    """Offline: interpolates inside the 1-minute bar from bars already on disk."""

    def __init__(self, day_bars: DayBars, keep: int = 16) -> None:
        self._load = day_bars
        self._days: OrderedDict[tuple[str, date], dict[int, tuple[float, float]] | None] = (
            OrderedDict()
        )
        self._keep = keep

    async def price_at(self, symbol: str, t: datetime) -> PricePoint | None:
        t = t.astimezone(UTC)
        key = (symbol, t.date())
        if key not in self._days:
            self._days[key] = self._load(symbol, t.date())
            while len(self._days) > self._keep:
                self._days.popitem(last=False)
        bars = self._days[key]
        minute = int(t.timestamp() // 60)
        if not bars or minute not in bars:
            return None
        o, c = bars[minute]
        bar_open = datetime.fromtimestamp(minute * 60, UTC)
        return PricePoint(interpolate(o, c, bar_open, t), bar_open, "cache_kline_1m")


class RestMainnetPrices:
    """Public mainnet REST (fapi.binance.com), throttled by ``budget``. CLI only."""

    def __init__(
        self, budget: WeightBudget, base_url: str = DEFAULT_REST_URL, timeout_s: float = 5.0
    ) -> None:
        self._budget = budget
        self._base = base_url
        self._timeout = timeout_s

    async def price_at(self, symbol: str, t: datetime) -> PricePoint | None:
        ms = int(t.timestamp() * 1000)
        rows = await fetch_json(
            "/fapi/v1/aggTrades",
            {"symbol": symbol, "startTime": ms, "endTime": ms + LOOKAHEAD_MS, "limit": 1},
            weight=AGG_TRADES_WEIGHT,
            base_url=self._base,
            timeout_s=self._timeout,
            budget=self._budget,
        )
        if rows:
            return PricePoint(float(rows[0]["p"]), _ms(int(rows[0]["T"])), "agg_trade")
        return None


class ChainPrices:
    """First source that has the moment wins."""

    def __init__(self, sources: Sequence[MainnetPrices]) -> None:
        self._sources = list(sources)

    async def price_at(self, symbol: str, t: datetime) -> PricePoint | None:
        for s in self._sources:
            got = await s.price_at(symbol, t)
            if got is not None:
                return got
        return None


def _ms(v: int) -> datetime:
    return datetime.fromtimestamp(v / 1000, UTC)


class RecentTrades:
    """Wraps the ledger backfill's Demo ``userTrades`` call and remembers its recent answers,
    so the shadow reads exact fill times without asking Binance again."""

    def __init__(self, inner: UserTrades, keep: int = 64) -> None:
        self._inner = inner
        self._keep = keep
        self._seen: OrderedDict[tuple[str, datetime], list[TradeFill]] = OrderedDict()

    async def __call__(self, symbol: str, since: datetime) -> list[TradeFill]:
        rows = await self._inner(symbol, since)
        self._seen[(symbol, since)] = rows
        self._seen.move_to_end((symbol, since))
        while len(self._seen) > self._keep:
            self._seen.popitem(last=False)
        return rows

    async def cached(self, symbol: str, since: datetime) -> list[TradeFill]:
        return self._seen.get((symbol, since), [])


STOP_REASONS = frozenset(
    {
        CloseReason.STOP_LOSS,
        CloseReason.TRAILING_STOP,
        CloseReason.TIGHTENED_STOP,
        CloseReason.PROFIT_LOCK,
        CloseReason.LIFEBOAT,
    }
)


@dataclass(frozen=True)
class LegHints:
    """What the ledger wrapper says about the orders: the entry waited as a limit order
    (``limit_wait_s``), the take-profit was a resting limit (``tp_tick``)."""

    limit_entry: bool = False
    limit_take_profit: bool = False


@dataclass(frozen=True)
class Leg:
    time: datetime
    kind: str
    """``market`` / ``limit`` / ``stop``."""
    fee_usdt: float | None
    """Demo fee actually charged on this leg; None = not known per leg."""


def eligible(rec: PositionRecord) -> bool:
    return (
        rec.status is PositionStatus.CLOSED
        and rec.closed_at is not None
        and rec.entry_price is not None
        and rec.exit_price is not None
        and rec.realized_pnl_usdt is not None
    )


def _maker(trades: Iterable[TradeFill], side: str, start: datetime, last: datetime) -> bool | None:
    flags = [t.maker for t in trades if t.side == side and start <= t.time <= last and t.qty > 0]
    if not flags or any(f is None for f in flags):
        return None
    return all(flags)


def _entry_kind(maker: bool | None, hints: LegHints) -> str:
    if maker is not None:
        return "limit" if maker else "market"
    return "limit" if hints.limit_entry else "market"


def _exit_kind(maker: bool | None, rec: PositionRecord, hints: LegHints) -> str:
    if maker:
        return "limit"
    if rec.close_reason in STOP_REASONS:
        return "stop"
    if maker is None and rec.close_reason is CloseReason.TAKE_PROFIT and hints.limit_take_profit:
        return "limit"
    return "market"


def legs(
    rec: PositionRecord, trades: list[TradeFill] | None, hints: LegHints
) -> tuple[Leg, Leg, str]:
    """Entry and exit legs from Demo fills when they match the position, else from the
    ledger's ``opened_at`` / ``closed_at``; the third value is the time source."""
    assert rec.closed_at is not None
    if trades:
        e = entry_fill(rec, trades)
        x = exit_fill(rec, trades, e)
        if e is not None and x is not None:
            e_maker = _maker(trades, open_side(rec.side), rec.opened_at - OPEN_SLACK, e.last)
            x_maker = _maker(trades, close_side(rec.side), e.last, x.last)
            return (
                Leg(e.last, _entry_kind(e_maker, hints), e.commission_usdt),
                Leg(x.last, _exit_kind(x_maker, rec, hints), x.commission_usdt),
                "fills",
            )
    return (
        Leg(rec.opened_at, _entry_kind(None, hints), None),
        Leg(rec.closed_at, _exit_kind(None, rec, hints), None),
        "ledger",
    )


def shadow_row(
    rec: PositionRecord,
    entry: PricePoint,
    exit_: PricePoint,
    entry_leg: Leg,
    exit_leg: Leg,
    *,
    time_source: str,
    taker_fee_bps: float,
    now: datetime,
) -> dict[str, Any]:
    """One ledger line. ``demo_net_usdt`` is the ledger's net (Binance-charged fees, no
    funding). The mainnet net charges Demo's actual fee rate of each leg on that leg's
    mainnet notional (per-leg rate from the fills, else the ledger's total fees spread over
    both Demo notionals), so the gap is the price difference; ``fee_gap_usdt`` is what an
    all-taker fee at ``taker_fee_bps`` would add on top. R is the ledger's stop distance
    times quantity, the same unit for both."""
    assert rec.entry_price is not None and rec.exit_price is not None
    assert rec.realized_pnl_usdt is not None and rec.closed_at is not None
    sign = 1.0 if rec.side is Side.LONG else -1.0
    qty = rec.quantity
    demo_in, demo_out = rec.entry_price * qty, rec.exit_price * qty
    main_in, main_out = entry.price * qty, exit_.price * qty
    demo_gross = sign * (rec.exit_price - rec.entry_price) * qty
    gross = sign * (exit_.price - entry.price) * qty
    if entry_leg.fee_usdt is not None and exit_leg.fee_usdt is not None:
        rate_in, rate_out = entry_leg.fee_usdt / demo_in, exit_leg.fee_usdt / demo_out
        fee_source = "fills"
    else:
        rate_in = rate_out = rec.fees_usdt / (demo_in + demo_out)
        fee_source = "ledger"
    fees = rate_in * main_in + rate_out * main_out
    taker_fees = taker_fee_bps / 10_000 * (main_in + main_out)
    net = gross - fees
    demo_net = rec.realized_pnl_usdt
    risk = abs(rec.entry_price - rec.stop_loss_price) * qty if rec.stop_loss_price else 0.0
    gap = demo_net - net

    def r(v: float) -> float | None:
        return round(v / risk, 6) if risk > 0 else None

    def bp(v: float) -> float | None:
        return round(v / demo_in * 10_000, 4) if demo_in > 0 else None

    return {
        "v": SHADOW_VERSION,
        "position_id": rec.position_id,
        "symbol": rec.symbol,
        "side": rec.side.value,
        "quantity": qty,
        "opened_at": rec.opened_at.isoformat(),
        "closed_at": rec.closed_at.isoformat(),
        "entry_time": entry_leg.time.isoformat(),
        "exit_time": exit_leg.time.isoformat(),
        "time_source": time_source,
        "entry_type": entry_leg.kind,
        "exit_type": exit_leg.kind,
        "demo_entry": rec.entry_price,
        "demo_exit": rec.exit_price,
        "demo_gross_usdt": round(demo_gross, 8),
        "demo_fees_usdt": rec.fees_usdt,
        "demo_net_usdt": demo_net,
        "fee_source": fee_source,
        "demo_fee_rate_bps": [round(rate_in * 10_000, 4), round(rate_out * 10_000, 4)],
        "mainnet_entry": entry.price,
        "mainnet_exit": exit_.price,
        "mainnet_entry_ts": entry.ts.isoformat(),
        "mainnet_exit_ts": exit_.ts.isoformat(),
        "entry_source": entry.source,
        "exit_source": exit_.source,
        "mainnet_gross_usdt": round(gross, 8),
        "mainnet_fees_usdt": round(fees, 8),
        "mainnet_net_usdt": round(net, 8),
        "taker_fee_bps": taker_fee_bps,
        "mainnet_taker_fees_usdt": round(taker_fees, 8),
        "fee_gap_usdt": round(taker_fees - fees, 8),
        "notional_usdt": round(demo_in, 8),
        "price_gap_usdt": round(demo_gross - gross, 8),
        "price_gap_bp": bp(demo_gross - gross),
        "gap_usdt": round(gap, 8),
        "gap_bp": bp(gap),
        "risk_usdt": round(risk, 8) if risk > 0 else None,
        "demo_r": r(demo_net),
        "mainnet_r": r(net),
        "gap_r": r(gap),
        "computed_at": now.isoformat(),
    }


def load_ledger(data_dir: str | Path) -> tuple[list[PositionRecord], dict[str, LegHints]]:
    """Records from ``positions.json`` plus the order hints from their wrappers; a
    malformed record is skipped, not fatal."""
    try:
        raw = json.loads((Path(data_dir) / "positions.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], {}
    recs: list[PositionRecord] = []
    hints: dict[str, LegHints] = {}
    for t in ((raw or {}).get("positions") or {}).values():
        try:
            rec = PositionRecord.model_validate(t["record"])
        except (ValueError, KeyError, TypeError):
            continue
        recs.append(rec)
        hints[rec.position_id] = LegHints(
            limit_entry=t.get("limit_wait_s") is not None,
            limit_take_profit=t.get("tp_tick") is not None,
        )
    return recs, hints


@dataclass
class UpdateResult:
    added: int = 0
    upgraded: int = 0
    """Replaced an existing row with a better-sourced one."""
    skipped: int = 0
    """Already in the file at the best quality this run can reach (or no better found)."""
    failed: int = 0
    """No price for a leg (or an error); retried on later runs."""
    pending: int = 0
    """Eligible but left for a later run (batch limit, rate limit, too many failures)."""
    sources: dict[str, int] = field(default_factory=dict)


class MainnetShadow:
    def __init__(
        self,
        data_dir: str | Path,
        prices: MainnetPrices,
        taker_fee_bps: Callable[[], float],
        trades: UserTrades | None = None,
        *,
        precise: bool = True,
    ) -> None:
        """``trades`` (Demo userTrades) gives exact fill times, fees and maker flags; without
        it, or when the fills cannot be matched, the ledger's ``opened_at`` / ``closed_at``
        stand in. ``precise`` = ``prices`` can price to the second; with it off (bars only)
        an existing second-level row is never attempted again."""
        self._dir = data_dir
        self._path = Path(data_dir) / SHADOW_FILE
        self._prices = prices
        self._fee = taker_fee_bps
        self._trades = trades
        self._best: Quality = (SHADOW_VERSION, 2 if precise else 1, 1 if trades else 0)
        self._failures: dict[str, int] = {}
        self._no_better: set[str] = set()

    async def update(
        self,
        records: Iterable[PositionRecord],
        now: datetime,
        *,
        hints: Mapping[str, LegHints] | None = None,
        since: datetime | None = None,
        limit: int | None = None,
        recompute: bool = False,
    ) -> UpdateResult:
        """Prices closes that have no row, or whose row this run could improve on (see
        ``quality``), newest first, at most ``limit`` attempts. ``recompute`` rewrites every
        row in range; the newer line wins at equal quality.

        ``RateLimited`` stops the run and propagates; any other error only skips that
        position, which is retried on later calls up to ``MAX_FAILURES`` per process."""
        done = read_shadow(self._dir)
        res = UpdateResult()
        todo: list[PositionRecord] = []
        for rec in records:
            if not eligible(rec) or rec.closed_at is None:
                continue
            if since is not None and rec.closed_at < since:
                continue
            old = done.get(rec.position_id)
            if (
                old is not None
                and not recompute
                and (quality(old) >= self._best or rec.position_id in self._no_better)
            ):
                res.skipped += 1
            elif self._failures.get(rec.position_id, 0) >= MAX_FAILURES:
                res.pending += 1
            else:
                todo.append(rec)
        todo.sort(key=lambda p: p.closed_at or now, reverse=True)
        tried = 0
        for i, rec in enumerate(todo):
            if limit is not None and tried >= limit:
                res.pending += len(todo) - i
                break
            tried += 1
            hint = (hints or {}).get(rec.position_id, LegHints())
            try:
                row = await self._one(rec, hint, now)
            except RateLimited:
                res.pending += len(todo) - i
                raise
            except Exception as e:
                log.warning("按主网价格复算 %s 失败：%r", rec.position_id, e)
                row = None
            if row is None:
                self._failures[rec.position_id] = self._failures.get(rec.position_id, 0) + 1
                res.failed += 1
                continue
            old = done.get(rec.position_id)
            if old is not None and not recompute and quality(row) <= quality(old):
                self._no_better.add(rec.position_id)
                res.skipped += 1
                continue
            self._append(row)
            if old is None:
                res.added += 1
            else:
                res.upgraded += 1
            for key in (row["entry_source"], row["exit_source"]):
                res.sources[key] = res.sources.get(key, 0) + 1
        return res

    async def _leg_price(self, rec: PositionRecord, leg: Leg, demo: float) -> PricePoint | None:
        if leg.kind == "limit":
            # A resting limit order fills at its own price on mainnet too; whether mainnet
            # would have traded through it is not checked, the row flags the leg instead.
            return PricePoint(demo, leg.time, "limit_price")
        return await self._prices.price_at(rec.symbol, leg.time)

    async def _one(
        self, rec: PositionRecord, hints: LegHints, now: datetime
    ) -> dict[str, Any] | None:
        assert rec.entry_price is not None and rec.exit_price is not None
        trades = (
            await self._trades(rec.symbol, rec.opened_at - OPEN_SLACK)
            if self._trades is not None
            else None
        )
        e_leg, x_leg, source = legs(rec, trades, hints)
        entry = await self._leg_price(rec, e_leg, rec.entry_price)
        exit_ = await self._leg_price(rec, x_leg, rec.exit_price) if entry is not None else None
        if entry is None or exit_ is None:
            return None
        return shadow_row(
            rec,
            entry,
            exit_,
            e_leg,
            x_leg,
            time_source=source,
            taker_fee_bps=self._fee(),
            now=now,
        )

    def _append(self, row: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


class LiveShadow:
    """The running loop's side: keeps the stream's per-second prices around every entry
    (from ``opened_at`` and, once the ledger backfill has fetched it, the actual entry fill
    time) and every exit while the stream still covers them, then prices settled closes
    from those marks, the stream and the streamed bars. No network calls."""

    def __init__(
        self,
        data_dir: str | Path,
        feed: FeedLookup,
        span: Span,
        taker_fee_bps: Callable[[], float],
        fill_trades: UserTrades | None = None,
    ) -> None:
        self._span = span
        self._trades = fill_trades
        self.marks = PriceMarks(data_dir)
        self.shadow = MainnetShadow(
            data_dir, ChainPrices([self.marks, FeedPrices(feed)]), taker_fee_bps, fill_trades
        )

    async def _windows(self, rec: PositionRecord) -> list[tuple[datetime, datetime]]:
        out = [(rec.opened_at - MARK_EDGE, rec.opened_at + MARK_AFTER_OPEN)]
        if rec.closed_at is not None:
            out.append((rec.closed_at - MARK_BEFORE_CLOSE, rec.closed_at + MARK_EDGE))
        if self._trades is not None:
            trades = await self._trades(rec.symbol, rec.opened_at - OPEN_SLACK)
            e = entry_fill(rec, trades) if trades else None
            if e is not None:
                out.append((e.last - MARK_EDGE, e.last + MARK_EDGE))
        return out

    async def tick(
        self, records: list[PositionRecord], hints: Mapping[str, LegHints], now: datetime
    ) -> UpdateResult:
        changed = False
        for rec in records:
            if rec.status is PositionStatus.FAILED:
                continue
            if rec.closed_at is not None and rec.closed_at + MARK_EDGE < now - MARK_WINDOW:
                continue
            for a, b in await self._windows(rec):
                if b < now - MARK_WINDOW or a > now:
                    continue
                changed |= self.marks.add(rec.symbol, self._span(rec.symbol, a, min(b, now)))
        if changed:
            self.marks.prune_and_save(now)
        return await self.shadow.update(
            records, now, hints=hints, since=now - LIVE_LOOKBACK, limit=LOOP_BATCH
        )
