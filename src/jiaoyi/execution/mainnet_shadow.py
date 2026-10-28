"""Mainnet-equivalent P&L shadow ledger: ``data/mainnet_shadow.jsonl``.

Orders fill on Binance Demo, whose fills run a few bp better than the mainnet price at the
same moment, so Demo P&L overstates the edge. For every closed position this re-prices
both legs at the Binance mainnet price at the Demo fill times (same quantity, taker fee on
both legs, funding ignored like the ledger's net P&L) and appends one JSON line.

It is a separate append-only file, never ``positions.json``: an older build that does not
know it simply ignores it. Rows are keyed by ``position_id``; a row once written is never
rewritten, and readers keep the first row per id, so the CLI backfill and the running loop
may both append without coordination.

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
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from jiaoyi.contracts import Interval, PositionRecord, PositionStatus, Side
from jiaoyi.core.shadow_ledger import SHADOW_FILE, read_shadow
from jiaoyi.execution.fills import OPEN_SLACK, TradeFill, UserTrades, entry_fill, exit_fill
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
"""A mark is taken while the trade stream still covers the moment (it keeps five minutes)."""
MARK_MATCH = timedelta(seconds=5)
MARK_KEEP = timedelta(days=2)
MAX_FAILURES = 10
LIVE_LOOKBACK = timedelta(hours=24)
"""The stream keeps 1500 one-minute bars; older closes are left to the CLI backfill."""
AGG_TRADES_WEIGHT = 20
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


class PriceMarks:
    """Per-symbol mainnet price samples taken from the stream at entry / exit moments,
    persisted so a restart between open and close does not lose the entry price."""

    def __init__(self, data_dir: str | Path) -> None:
        self._path = Path(data_dir) / MARKS_FILE
        self._marks: dict[str, list[tuple[datetime, float, datetime]]] = {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            for sym, rows in raw.items():
                self._marks[sym] = sorted(
                    (datetime.fromisoformat(a), float(p), datetime.fromisoformat(b))
                    for a, p, b in rows
                )
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, AttributeError):
            log.warning("%s 读不出，从空的开始", self._path, exc_info=True)
            self._marks = {}

    def has(self, symbol: str, t: datetime) -> bool:
        return self.get(symbol, t) is not None

    def get(self, symbol: str, t: datetime) -> PricePoint | None:
        best: tuple[datetime, float, datetime] | None = None
        for row in self._marks.get(symbol, []):
            if abs(row[0] - t) <= MARK_MATCH and (
                best is None or abs(row[0] - t) < abs(best[0] - t)
            ):
                best = row
        return PricePoint(best[1], best[2], "mark") if best is not None else None

    def add(self, symbol: str, t: datetime, point: PricePoint) -> None:
        self._marks.setdefault(symbol, []).append((t, point.price, point.ts))
        self._marks[symbol].sort()

    def prune_and_save(self, now: datetime) -> None:
        cut = now - MARK_KEEP
        self._marks = {
            s: kept for s, rows in self._marks.items() if (kept := [r for r in rows if r[0] >= cut])
        }
        doc = {
            s: [[a.isoformat(), p, b.isoformat()] for a, p, b in rows]
            for s, rows in self._marks.items()
        }
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


def eligible(rec: PositionRecord) -> bool:
    return (
        rec.status is PositionStatus.CLOSED
        and rec.closed_at is not None
        and rec.entry_price is not None
        and rec.exit_price is not None
        and rec.realized_pnl_usdt is not None
    )


def shadow_row(
    rec: PositionRecord,
    entry: PricePoint,
    exit_: PricePoint,
    *,
    entry_time: datetime,
    exit_time: datetime,
    time_source: str,
    taker_fee_bps: float,
    now: datetime,
) -> dict[str, Any]:
    """One ledger line. ``demo_net_usdt`` is the ledger's net (Binance-charged fees, no
    funding); the mainnet net charges ``taker_fee_bps`` on both mainnet notionals. R is the
    ledger's stop distance times quantity, the same unit for both."""
    assert rec.entry_price is not None and rec.exit_price is not None
    assert rec.realized_pnl_usdt is not None and rec.closed_at is not None
    sign = 1.0 if rec.side is Side.LONG else -1.0
    qty = rec.quantity
    notional = rec.entry_price * qty
    demo_gross = sign * (rec.exit_price - rec.entry_price) * qty
    gross = sign * (exit_.price - entry.price) * qty
    fees = taker_fee_bps / 10_000 * qty * (entry.price + exit_.price)
    net = gross - fees
    demo_net = rec.realized_pnl_usdt
    risk = abs(rec.entry_price - rec.stop_loss_price) * qty if rec.stop_loss_price else 0.0
    gap = demo_net - net

    def r(v: float) -> float | None:
        return round(v / risk, 6) if risk > 0 else None

    def bp(v: float) -> float | None:
        return round(v / notional * 10_000, 4) if notional > 0 else None

    return {
        "position_id": rec.position_id,
        "symbol": rec.symbol,
        "side": rec.side.value,
        "quantity": qty,
        "opened_at": rec.opened_at.isoformat(),
        "closed_at": rec.closed_at.isoformat(),
        "entry_time": entry_time.isoformat(),
        "exit_time": exit_time.isoformat(),
        "time_source": time_source,
        "demo_entry": rec.entry_price,
        "demo_exit": rec.exit_price,
        "demo_gross_usdt": round(demo_gross, 8),
        "demo_fees_usdt": rec.fees_usdt,
        "demo_net_usdt": demo_net,
        "mainnet_entry": entry.price,
        "mainnet_exit": exit_.price,
        "mainnet_entry_ts": entry.ts.isoformat(),
        "mainnet_exit_ts": exit_.ts.isoformat(),
        "entry_source": entry.source,
        "exit_source": exit_.source,
        "taker_fee_bps": taker_fee_bps,
        "mainnet_gross_usdt": round(gross, 8),
        "mainnet_fees_usdt": round(fees, 8),
        "mainnet_net_usdt": round(net, 8),
        "notional_usdt": round(notional, 8),
        "gap_usdt": round(gap, 8),
        "gap_bp": bp(gap),
        "price_gap_bp": bp(demo_gross - gross),
        "risk_usdt": round(risk, 8) if risk > 0 else None,
        "demo_r": r(demo_net),
        "mainnet_r": r(net),
        "gap_r": r(gap),
        "computed_at": now.isoformat(),
    }


def read_ledger(data_dir: str | Path) -> list[PositionRecord]:
    """Records from ``positions.json``; a malformed record is skipped, not fatal."""
    try:
        raw = json.loads((Path(data_dir) / "positions.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out: list[PositionRecord] = []
    for t in ((raw or {}).get("positions") or {}).values():
        try:
            out.append(PositionRecord.model_validate(t["record"]))
        except (ValueError, KeyError, TypeError):
            continue
    return out


@dataclass
class UpdateResult:
    added: int = 0
    skipped: int = 0
    """Already in the file."""
    failed: int = 0
    """No price for a leg (or an error); retried on later runs."""
    pending: int = 0
    """Eligible but left for a later run (batch limit, rate limit, too many failures)."""
    sources: dict[str, int] | None = None


class MainnetShadow:
    def __init__(
        self,
        data_dir: str | Path,
        prices: MainnetPrices,
        taker_fee_bps: Callable[[], float],
        trades: UserTrades | None = None,
    ) -> None:
        """``trades`` (Demo userTrades) gives the exact fill times; without it, or when the
        fills cannot be matched, the ledger's ``opened_at`` / ``closed_at`` stand in."""
        self._dir = data_dir
        self._path = Path(data_dir) / SHADOW_FILE
        self._prices = prices
        self._fee = taker_fee_bps
        self._trades = trades
        self._failures: dict[str, int] = {}

    async def _times(self, rec: PositionRecord) -> tuple[datetime, datetime, str]:
        assert rec.closed_at is not None
        if self._trades is not None:
            trades = await self._trades(rec.symbol, rec.opened_at - OPEN_SLACK)
            entry = entry_fill(rec, trades)
            exit_ = exit_fill(rec, trades, entry)
            if entry is not None and exit_ is not None:
                return entry.last, exit_.last, "fills"
        return rec.opened_at, rec.closed_at, "ledger"

    async def update(
        self,
        records: Iterable[PositionRecord],
        now: datetime,
        *,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> UpdateResult:
        """Prices closes not yet in the file, newest first, at most ``limit`` attempts.

        ``RateLimited`` stops the run and propagates; any other error only skips that
        position, which is retried on later calls up to ``MAX_FAILURES`` per process."""
        done = read_shadow(self._dir)
        res = UpdateResult(sources={})
        todo: list[PositionRecord] = []
        for rec in records:
            if not eligible(rec) or rec.closed_at is None:
                continue
            if since is not None and rec.closed_at < since:
                continue
            if rec.position_id in done:
                res.skipped += 1
            elif self._failures.get(rec.position_id, 0) >= MAX_FAILURES:
                res.pending += 1
            else:
                todo.append(rec)
        todo.sort(key=lambda p: p.closed_at or now, reverse=True)
        for i, rec in enumerate(todo):
            if limit is not None and res.added + res.failed >= limit:
                res.pending += len(todo) - i
                break
            try:
                row = await self._one(rec, now)
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
            self._append(row)
            res.added += 1
            assert res.sources is not None
            for key in (row["entry_source"], row["exit_source"]):
                res.sources[key] = res.sources.get(key, 0) + 1
        return res

    async def _one(self, rec: PositionRecord, now: datetime) -> dict[str, Any] | None:
        entry_t, exit_t, source = await self._times(rec)
        entry = await self._prices.price_at(rec.symbol, entry_t)
        exit_ = await self._prices.price_at(rec.symbol, exit_t) if entry is not None else None
        if entry is None or exit_ is None:
            return None
        return shadow_row(
            rec,
            entry,
            exit_,
            entry_time=entry_t,
            exit_time=exit_t,
            time_source=source,
            taker_fee_bps=self._fee(),
            now=now,
        )

    def _append(self, row: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


class LiveShadow:
    """The running loop's side: marks the stream price at each open / close while the stream
    still covers it, then prices settled closes from marks, the stream and streamed bars.
    No network calls."""

    def __init__(
        self,
        data_dir: str | Path,
        feed: FeedLookup,
        taker_fee_bps: Callable[[], float],
        fill_trades: UserTrades | None = None,
    ) -> None:
        self._feed = FeedPrices(feed)
        self.marks = PriceMarks(data_dir)
        self.shadow = MainnetShadow(
            data_dir, ChainPrices([self.marks, self._feed]), taker_fee_bps, fill_trades
        )

    async def tick(self, records: list[PositionRecord], now: datetime) -> UpdateResult:
        changed = False
        for rec in records:
            for t in (rec.opened_at, rec.closed_at):
                if t is None or not (now - MARK_WINDOW <= t <= now):
                    continue
                if self.marks.has(rec.symbol, t):
                    continue
                got = await self._feed.price_at(rec.symbol, t)
                if got is not None and got.source == "ws_trade":
                    self.marks.add(rec.symbol, t, got)
                    changed = True
        if changed:
            self.marks.prune_and_save(now)
        return await self.shadow.update(records, now, since=now - LIVE_LOOKBACK, limit=LOOP_BATCH)
