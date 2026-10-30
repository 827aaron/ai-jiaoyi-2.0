"""Mainnet-equivalent shadow ledger: pricing, idempotent backfill, upgrades, live marks, panel."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from jiaoyi import contracts as c
from jiaoyi.core import shadow_ledger as sl
from jiaoyi.core.config import load_risk_config
from jiaoyi.execution import mainnet_shadow as ms
from jiaoyi.execution.fills import TradeFill
from jiaoyi.market import rest
from jiaoyi.market.features import RollingFeatureEngine
from tests.conftest import ROOT

RISK = load_risk_config(ROOT / "config" / "risk.example.toml")
T0 = datetime(2026, 10, 24, 3, 0, tzinfo=UTC)
CLOSE = T0 + timedelta(minutes=30)
BP = 5.0


def _rec(side: c.Side, pid: str = "pos-1", **kw: Any) -> c.PositionRecord:
    long = side is c.Side.LONG
    base: dict[str, Any] = {
        "position_id": pid,
        "group_id": "g",
        "intent_id": "i",
        "signal_id": "s",
        "card_id": "card",
        "symbol": "BTCUSDT",
        "category": "major",
        "side": side,
        "status": c.PositionStatus.CLOSED,
        "quantity": 2.0,
        "leverage": 2,
        "entry_price": 100.0,
        "exit_price": 101.0 if long else 99.0,
        "stop_loss_price": 99.0 if long else 101.0,
        "time_limit_s": 600,
        "opened_at": T0,
        "closed_at": CLOSE,
        "close_reason": c.CloseReason.KEEPER,
        "realized_pnl_usdt": 1.6,
        "fees_usdt": 0.4,
    }
    base.update(kw)
    return c.PositionRecord(**base)


class FakePrices:
    """Mainnet is 10 bp worse than Demo on both legs, in the trade's direction."""

    def __init__(self, table: dict[datetime, float], source: str = "agg_trade") -> None:
        self.table = table
        self.source = source
        self.calls: list[datetime] = []

    async def price_at(self, symbol: str, t: datetime) -> ms.PricePoint | None:
        self.calls.append(t)
        p = self.table.get(t)
        return None if p is None else ms.PricePoint(p, t, self.source)


def _table(side: c.Side) -> dict[datetime, float]:
    if side is c.Side.LONG:
        return {T0: 100.1, CLOSE: 100.9}
    return {T0: 99.9, CLOSE: 99.1}


def _fills(side: c.Side, entry_t: datetime, exit_t: datetime, **kw: Any) -> list[TradeFill]:
    buy, sell = ("buy", "sell") if side is c.Side.LONG else ("sell", "buy")
    exit_px = 101.0 if side is c.Side.LONG else 99.0
    return [
        TradeFill(entry_t, "1", buy, 100.0, 2.0, kw.get("fee_in", 0.1), 0.0, kw.get("mk_in")),
        TradeFill(exit_t, "2", sell, exit_px, 2.0, kw.get("fee_out", 0.3), 2.0, kw.get("mk_out")),
    ]


@pytest.mark.parametrize(
    ("side", "fee_gap"),
    [
        (c.Side.LONG, -0.199),  # 5 bp * (200.2 + 201.8) - 0.4
        (c.Side.SHORT, -0.201),  # 5 bp * (199.8 + 198.2) - 0.4
    ],
)
async def test_mainnet_shadow_update_long_short_mirrored(
    tmp_path: Path, side: c.Side, fee_gap: float
) -> None:
    shadow = ms.MainnetShadow(tmp_path, FakePrices(_table(side)), lambda: BP)
    res = await shadow.update([_rec(side)], T0 + timedelta(hours=1))
    assert (res.added, res.failed) == (1, 0)
    row = sl.read_shadow(tmp_path)["pos-1"]
    assert row["mainnet_gross_usdt"] == pytest.approx(1.6)
    assert row["fee_source"] == "ledger"
    assert row["mainnet_fees_usdt"] == pytest.approx(0.4)  # Demo's 0.4 over the same notional
    assert row["mainnet_net_usdt"] == pytest.approx(1.2)
    assert (row["gap_usdt"], row["price_gap_usdt"]) == pytest.approx((0.4, 0.4))
    assert (row["gap_bp"], row["price_gap_bp"]) == pytest.approx((20.0, 20.0))
    assert row["fee_gap_usdt"] == pytest.approx(fee_gap)
    assert (row["demo_r"], row["mainnet_r"], row["gap_r"]) == pytest.approx((0.8, 0.6, 0.2))
    assert (row["entry_type"], row["exit_type"], row["time_source"]) == (
        "market",
        "market",
        "ledger",
    )


async def test_mainnet_shadow_update_charges_each_legs_actual_fee_rate(tmp_path: Path) -> None:
    fills = _fills(c.Side.LONG, T0, CLOSE, fee_in=0.1, fee_out=0.0404)

    async def trades(symbol: str, since: datetime) -> list[TradeFill]:
        return fills

    rec = _rec(c.Side.LONG, fees_usdt=0.1404, realized_pnl_usdt=1.8596)
    shadow = ms.MainnetShadow(tmp_path, FakePrices(_table(c.Side.LONG)), lambda: BP, trades)
    await shadow.update([rec], T0 + timedelta(hours=1))
    row = sl.read_shadow(tmp_path)["pos-1"]
    assert row["fee_source"] == "fills"
    assert row["demo_fee_rate_bps"] == pytest.approx([5.0, 2.0])
    # 5 bp * 200.2 + 2 bp * 201.8
    assert row["mainnet_fees_usdt"] == pytest.approx(0.10010 + 0.04036)
    assert row["mainnet_net_usdt"] == pytest.approx(1.6 - 0.14046)
    assert row["fee_gap_usdt"] == pytest.approx(0.201 - 0.14046)


async def test_mainnet_shadow_update_backfill_is_idempotent(tmp_path: Path) -> None:
    prices = FakePrices(_table(c.Side.LONG))
    now = T0 + timedelta(hours=1)
    recs = [_rec(c.Side.LONG), _rec(c.Side.LONG, "pos-2")]
    assert (await ms.MainnetShadow(tmp_path, prices, lambda: BP).update(recs, now)).added == 2
    calls = len(prices.calls)
    again = await ms.MainnetShadow(tmp_path, prices, lambda: BP).update(recs, now)
    assert (again.added, again.upgraded, again.skipped) == (0, 0, 2)
    assert len(prices.calls) == calls
    assert len((tmp_path / sl.SHADOW_FILE).read_text(encoding="utf-8").splitlines()) == 2


async def test_mainnet_shadow_update_upgrades_rows_from_better_sources(tmp_path: Path) -> None:
    now = T0 + timedelta(hours=1)
    rec = _rec(c.Side.LONG)
    bars = FakePrices({T0: 100.3, CLOSE: 100.5}, source="cache_kline_1m")
    first = await ms.MainnetShadow(tmp_path, bars, lambda: BP, precise=False).update([rec], now)
    assert first.added == 1
    again = await ms.MainnetShadow(tmp_path, bars, lambda: BP, precise=False).update([rec], now)
    assert (again.added, again.skipped) == (0, 1)

    precise = FakePrices(_table(c.Side.LONG))
    up = await ms.MainnetShadow(tmp_path, precise, lambda: BP).update([rec], now)
    assert (up.added, up.upgraded) == (0, 1)
    assert sl.read_shadow(tmp_path)["pos-1"]["mainnet_entry"] == 100.1

    async def trades(symbol: str, since: datetime) -> list[TradeFill]:
        return _fills(c.Side.LONG, T0, CLOSE)

    with_fills = ms.MainnetShadow(tmp_path, precise, lambda: BP, trades)
    assert (await with_fills.update([rec], now)).upgraded == 1
    assert sl.read_shadow(tmp_path)["pos-1"]["time_source"] == "fills"

    # a lower-quality line written later never replaces a better one
    worse = await ms.MainnetShadow(tmp_path, bars, lambda: BP, precise=False).update(
        [rec], now, recompute=True
    )
    assert worse.upgraded == 1
    assert sl.read_shadow(tmp_path)["pos-1"]["time_source"] == "fills"
    assert len((tmp_path / sl.SHADOW_FILE).read_text().splitlines()) == 4

    redo = await with_fills.update([rec], now, recompute=True)
    assert redo.upgraded == 1
    lines = (tmp_path / sl.SHADOW_FILE).read_text().splitlines()
    assert sl.read_shadow(tmp_path)["pos-1"]["computed_at"] == json.loads(lines[-1])["computed_at"]


async def test_mainnet_shadow_update_prices_limit_legs_at_their_limit(tmp_path: Path) -> None:
    prices = FakePrices(_table(c.Side.SHORT))
    rec = _rec(c.Side.SHORT, close_reason=c.CloseReason.TAKE_PROFIT)
    hints = {"pos-1": ms.LegHints(limit_entry=True, limit_take_profit=True)}
    shadow = ms.MainnetShadow(tmp_path, prices, lambda: BP)
    await shadow.update([rec], T0 + timedelta(hours=1), hints=hints)
    row = sl.read_shadow(tmp_path)["pos-1"]
    assert (row["entry_type"], row["exit_type"]) == ("limit", "limit")
    assert (row["mainnet_entry"], row["mainnet_exit"]) == (100.0, 99.0)
    assert (row["entry_source"], row["price_gap_usdt"]) == ("limit_price", 0.0)
    assert prices.calls == []


@pytest.mark.parametrize(
    ("reason", "mk_in", "mk_out", "legs"),
    [
        (c.CloseReason.STOP_LOSS, False, False, ("market", "stop")),
        (c.CloseReason.TAKE_PROFIT, True, True, ("limit", "limit")),
        (c.CloseReason.TAKE_PROFIT, False, False, ("market", "market")),
        (c.CloseReason.KEEPER, None, None, ("market", "market")),
    ],
)
def test_legs_order_type_from_maker_flags(
    reason: c.CloseReason, mk_in: bool | None, mk_out: bool | None, legs: tuple[str, str]
) -> None:
    rec = _rec(c.Side.LONG, close_reason=reason)
    fills = _fills(c.Side.LONG, T0, CLOSE, mk_in=mk_in, mk_out=mk_out)
    e, x, source = ms.legs(rec, fills, ms.LegHints(limit_take_profit=True))
    assert (e.kind, x.kind, source) == (*legs, "fills")


async def test_mainnet_shadow_update_skips_unsettled_and_respects_since_and_limit(
    tmp_path: Path,
) -> None:
    prices = FakePrices(_table(c.Side.LONG))
    shadow = ms.MainnetShadow(tmp_path, prices, lambda: BP)
    open_ = _rec(c.Side.LONG, "open", status=c.PositionStatus.OPEN, closed_at=None)
    no_exit = _rec(c.Side.LONG, "no-exit", exit_price=None)
    old = _rec(
        c.Side.LONG, "old", closed_at=T0 - timedelta(days=2), opened_at=T0 - timedelta(days=3)
    )
    newer = [_rec(c.Side.LONG, f"p{i}") for i in range(3)]
    res = await shadow.update(
        [open_, no_exit, old, *newer], T0 + timedelta(hours=1), since=T0, limit=2
    )
    assert (res.added, res.pending) == (2, 1)
    assert set(sl.read_shadow(tmp_path)) <= {"p0", "p1", "p2"}


async def test_mainnet_shadow_update_missing_price_retries_then_gives_up(tmp_path: Path) -> None:
    shadow = ms.MainnetShadow(tmp_path, FakePrices({}), lambda: BP)
    for _ in range(ms.MAX_FAILURES):
        assert (await shadow.update([_rec(c.Side.LONG)], T0 + timedelta(hours=1))).failed == 1
    res = await shadow.update([_rec(c.Side.LONG)], T0 + timedelta(hours=1))
    assert (res.failed, res.pending) == (0, 1)
    assert not (tmp_path / sl.SHADOW_FILE).exists()


async def test_mainnet_shadow_update_uses_cached_fill_times(tmp_path: Path) -> None:
    entry_t, exit_t = T0 + timedelta(seconds=2), T0 + timedelta(minutes=29, seconds=50)
    calls: list[tuple[str, datetime]] = []

    async def user_trades(symbol: str, since: datetime) -> list[TradeFill]:
        calls.append((symbol, since))
        return _fills(c.Side.LONG, entry_t, exit_t)

    recent = ms.RecentTrades(user_trades)
    rec = _rec(c.Side.LONG)
    assert await recent.cached(rec.symbol, rec.opened_at - timedelta(seconds=10)) == []
    await recent(rec.symbol, rec.opened_at - timedelta(seconds=10))
    prices = FakePrices({entry_t: 100.1, exit_t: 100.9})
    shadow = ms.MainnetShadow(tmp_path, prices, lambda: BP, recent.cached)
    assert (await shadow.update([rec], T0 + timedelta(hours=1))).added == 1
    assert len(calls) == 1
    row = sl.read_shadow(tmp_path)["pos-1"]
    assert (row["time_source"], row["entry_time"]) == ("fills", entry_t.isoformat())


def _trade(t: datetime, price: float) -> c.Trade:
    return c.Trade(symbol="BTCUSDT", ts=t, price=price, qty=1.0, taker_side=c.Side.LONG)


def _bar(minute: datetime, o: float, close: float) -> c.Kline:
    return c.Kline(
        symbol="BTCUSDT",
        interval=c.Interval.M1,
        open_time=minute,
        close_time=minute + timedelta(seconds=59.999),
        open=o,
        high=max(o, close),
        low=min(o, close),
        close=close,
        volume=1.0,
        quote_volume=100.0,
    )


def test_features_price_at_trade_window_then_bar_interpolation() -> None:
    eng = RollingFeatureEngine(atr_period_min=1)
    eng.update(_bar(T0, 100.0, 106.0))
    eng.update(_trade(T0 + timedelta(minutes=2), 110.0))
    eng.update(_trade(T0 + timedelta(minutes=2, seconds=5), 111.0))
    got = eng.price_at("BTCUSDT", T0 + timedelta(minutes=2, seconds=3, milliseconds=500))
    assert got == (110.0, T0 + timedelta(minutes=2), "ws_trade")
    assert eng.price_at("BTCUSDT", T0 + timedelta(seconds=30)) == (103.0, T0, "ws_kline_1m")
    assert eng.price_at("BTCUSDT", T0 - timedelta(minutes=5)) is None
    assert eng.price_at("ETHUSDT", T0) is None
    span = eng.prices_between(
        "BTCUSDT", T0 + timedelta(minutes=2), T0 + timedelta(minutes=2, seconds=4)
    )
    assert span == [(T0 + timedelta(minutes=2), 110.0)]


class Stream:
    """Stand-in for the feature engine: per-second prices that can be cleared."""

    def __init__(self) -> None:
        self.prices: dict[datetime, float] = {}

    def price_at(self, symbol: str, t: datetime) -> tuple[float, datetime, str] | None:
        secs = [s for s in self.prices if s <= t]
        if not secs:
            return None
        s = max(secs)
        return self.prices[s], s, "ws_trade"

    def between(self, symbol: str, a: datetime, b: datetime) -> list[tuple[datetime, float]]:
        return sorted((s, p) for s, p in self.prices.items() if a <= s <= b)


async def test_live_shadow_tick_marks_entry_and_prices_close_without_network(
    tmp_path: Path,
) -> None:
    stream = Stream()
    rec_open = _rec(c.Side.SHORT, status=c.PositionStatus.OPEN, closed_at=None, exit_price=None)
    stream.prices[T0] = 99.9
    live = ms.LiveShadow(tmp_path, stream.price_at, stream.between, lambda: BP)
    assert (await live.tick([rec_open], {}, T0 + timedelta(seconds=20))).added == 0
    assert ms.PriceMarks(tmp_path).get("BTCUSDT", T0) == ms.PricePoint(99.9, T0, "mark")

    stream.prices = {CLOSE: 99.1}  # the trade window has moved on
    restarted = ms.LiveShadow(tmp_path, stream.price_at, stream.between, lambda: BP)
    res = await restarted.tick([_rec(c.Side.SHORT)], {}, CLOSE + timedelta(seconds=30))
    assert res.added == 1
    row = sl.read_shadow(tmp_path)["pos-1"]
    assert (row["entry_source"], row["exit_source"]) == ("mark", "mark")
    assert row["mainnet_net_usdt"] == pytest.approx(1.2)


@pytest.mark.parametrize("fill_after_s", [40, 200])
async def test_live_shadow_tick_marks_the_actual_entry_fill_time(
    tmp_path: Path, fill_after_s: int
) -> None:
    fill_t = T0 + timedelta(seconds=fill_after_s)
    stream = Stream()
    stream.prices = {T0: 99.0, fill_t: 99.9}
    cache: dict[str, list[TradeFill]] = {}

    async def cached(symbol: str, since: datetime) -> list[TradeFill]:
        return cache.get(symbol, [])

    rec_open = _rec(c.Side.SHORT, status=c.PositionStatus.OPEN, closed_at=None, exit_price=None)
    live = ms.LiveShadow(tmp_path, stream.price_at, stream.between, lambda: BP, cached)
    # the ledger backfill fetched the entry fill shortly after it happened
    cache["BTCUSDT"] = _fills(c.Side.SHORT, fill_t, CLOSE)[:1]
    await live.tick([rec_open], {}, fill_t + timedelta(seconds=20))

    cache["BTCUSDT"] = _fills(c.Side.SHORT, fill_t, CLOSE)
    stream.prices = {CLOSE: 99.1}
    res = await live.tick([_rec(c.Side.SHORT)], {}, CLOSE + timedelta(seconds=30))
    assert res.added == 1
    row = sl.read_shadow(tmp_path)["pos-1"]
    assert (row["time_source"], row["entry_time"]) == ("fills", fill_t.isoformat())
    assert (row["entry_source"], row["mainnet_entry"]) == ("mark", 99.9)


async def test_live_shadow_tick_leaves_old_closes_to_the_cli(tmp_path: Path) -> None:
    calls: list[datetime] = []

    def feed(symbol: str, t: datetime) -> tuple[float, datetime, str] | None:
        calls.append(t)
        return None

    live = ms.LiveShadow(tmp_path, feed, lambda s, a, b: [], lambda: BP)
    res = await live.tick([_rec(c.Side.LONG)], {}, T0 + timedelta(days=3))
    assert (res.added, res.failed, calls) == (0, 0, [])


async def test_cached_kline_prices_reads_packs_then_close_cache(tmp_path: Path) -> None:
    cache = tmp_path / "market_desk" / "score_cache" / "1m" / "BTCUSDT"
    cache.mkdir(parents=True)
    closes: list[float | None] = [None] * 1440
    day0 = datetime(2026, 10, 24, tzinfo=UTC)
    closes[180], closes[181] = 100.0, 102.0  # 03:00 and 03:01 closes
    (cache / "2026-10-24.json").write_text(json.dumps({"end_ms": 0, "c": closes}))
    prices = ms.CachedKlinePrices(ms.disk_day_bars(tmp_path))
    got = await prices.price_at("BTCUSDT", day0 + timedelta(hours=3, minutes=1, seconds=15))
    assert got == ms.PricePoint(100.5, day0 + timedelta(hours=3, minutes=1), "cache_kline_1m")
    assert await prices.price_at("BTCUSDT", day0 + timedelta(hours=5)) is None
    assert await prices.price_at("ETHUSDT", day0) is None


def test_by_day_splits_on_reset_zone() -> None:
    base = {"price_gap_usdt": 0.5, "fee_gap_usdt": 0.1, "notional_usdt": 1000.0}
    rows = [
        {
            **base,
            "closed_at": "2026-10-24T16:30:00+00:00",  # 23:30 Bangkok
            "demo_net_usdt": 2.0,
            "mainnet_net_usdt": 1.0,
            "gap_r": 0.1,
            "demo_r": 0.2,
            "mainnet_r": 0.1,
            "entry_type": "limit",
        },
        {
            **base,
            "closed_at": "2026-10-24T17:30:00+00:00",  # 00:30 next Bangkok day
            "demo_net_usdt": -1.0,
            "mainnet_net_usdt": -3.0,
            "gap_r": None,
        },
    ]
    a, b = sl.by_day(rows, "Asia/Bangkok")
    assert (a.day, a.gap_usdt, a.gap_bp, a.avg(a.gap_r_sum)) == ("2026-10-24", 1.0, 10.0, 0.1)
    assert (a.price_gap_usdt, a.fee_gap_usdt, a.limit_legs) == (0.5, 0.1, 1)
    assert (b.day, b.gap_usdt, b.avg(b.gap_r_sum)) == ("2026-10-25", 2.0, None)


def test_read_shadow_follows_file_changes(tmp_path: Path) -> None:
    path = tmp_path / sl.SHADOW_FILE
    path.write_text(json.dumps({"position_id": "a", "v": 2}) + "\n")
    assert set(sl.read_shadow(tmp_path)) == {"a"}
    with path.open("a") as f:
        f.write(json.dumps({"position_id": "b", "v": 2}) + "\n")
    assert set(sl.read_shadow(tmp_path)) == {"a", "b"}


def test_history_page_shows_mainnet_compare(tmp_path: Path) -> None:
    from jiaoyi.panel.files import PanelFiles
    from jiaoyi.panel.pages import build_history

    recs = [_rec(c.Side.LONG, "pos-1"), _rec(c.Side.SHORT, "pos-2")]
    _write_ledger(tmp_path, recs)
    leg = ms.Leg(T0, "market", None)
    row = ms.shadow_row(
        recs[0],
        ms.PricePoint(100.1, T0, "agg_trade"),
        ms.PricePoint(100.9, T0, "agg_trade"),
        leg,
        ms.Leg(CLOSE, "limit", None),
        time_source="ledger",
        taker_fee_bps=BP,
        now=T0,
    )
    (tmp_path / sl.SHADOW_FILE).write_text(json.dumps(row) + "\n")
    files = PanelFiles(tmp_path, RISK.day_reset_tz)
    page = build_history(files, RISK, T0 + timedelta(hours=2), "all", include_tests=True)
    by_id = {t.position_id: t for t in page.rows}
    one = by_id["pos-1"]
    assert (one.mainnet_net_usdt, one.mainnet_gap_bp) == pytest.approx((1.2, 20.0))
    assert (one.mainnet_price_gap_usdt, one.mainnet_fee_gap_usdt) == pytest.approx((0.4, -0.199))
    assert one.mainnet_legs == ["market", "limit"]
    assert by_id["pos-2"].mainnet_net_usdt is None
    m = page.mainnet
    assert m is not None and (m.covered, m.trades, m.limit_legs) == (1, 2, 1)
    assert (m.demo_net_usdt, m.mainnet_net_usdt) == pytest.approx((1.6, 1.2))
    assert (m.price_gap_bp, m.fee_gap_usdt) == pytest.approx((20.0, -0.199))
    [day] = m.days
    assert day.cum_mainnet_usdt == pytest.approx(1.2) and day.gap_r == pytest.approx(0.2)


def _write_ledger(data: Path, recs: list[c.PositionRecord], **wrapper: Any) -> None:
    doc = {
        "positions": {r.position_id: {"record": r.model_dump(mode="json"), **wrapper} for r in recs}
    }
    (data / "positions.json").write_text(json.dumps(doc))


def test_load_ledger_reads_order_hints(tmp_path: Path) -> None:
    _write_ledger(tmp_path, [_rec(c.Side.LONG)], limit_wait_s=30, tp_tick=0.1)
    recs, hints = ms.load_ledger(tmp_path)
    assert [r.position_id for r in recs] == ["pos-1"]
    assert hints["pos-1"] == ms.LegHints(limit_entry=True, limit_take_profit=True)


def test_load_ledger_reads_archive(tmp_path: Path) -> None:
    from jiaoyi.core.position_archive import write_archive

    _write_ledger(tmp_path, [_rec(c.Side.LONG)])
    old = {"record": _rec(c.Side.SHORT, "pos-0").model_dump(mode="json"), "tp_tick": 0.1}
    write_archive(tmp_path, [("2026-10-01", "pos-0", json.dumps(old))])
    recs, hints = ms.load_ledger(tmp_path)
    assert sorted(r.position_id for r in recs) == ["pos-0", "pos-1"]
    assert hints["pos-0"].limit_take_profit


@pytest.mark.parametrize("on", [False, True])
async def test_app_mainnet_shadow_tick_follows_switch(tmp_path: Path, on: bool) -> None:
    from jiaoyi.app import App
    from jiaoyi.core.config import load_app_config

    now = CLOSE + timedelta(seconds=30)
    cfg = load_app_config(ROOT / "config" / "app.example.toml").model_copy(
        update={"data_dir": str(tmp_path), "mainnet_shadow_on": on}
    )
    app = App(RISK, cfg, clock=lambda: now, offline=True)
    _write_ledger(tmp_path, [_rec(c.Side.LONG)])
    app.m.features.update(_bar(T0, 100.0, 100.2))  # entry at T0: the bar's open
    app.m.features.update(_trade(CLOSE, 100.9))
    await app.mainnet_shadow_tick()
    rows = sl.read_shadow(tmp_path)
    if not on:
        assert rows == {} and not (tmp_path / ms.MARKS_FILE).exists()
        return
    row = rows["pos-1"]
    assert (row["entry_source"], row["exit_source"]) == ("ws_kline_1m", "mark")
    assert (row["mainnet_entry"], row["mainnet_exit"]) == (100.0, 100.9)


def _cli_argv(tmp_path: Path, *extra: str) -> list[str]:
    return [
        "mainnet-shadow",
        "--since",
        "2026-10-24",
        "--data-dir",
        str(tmp_path),
        "--app",
        str(ROOT / "config" / "app.example.toml"),
        "--risk",
        str(ROOT / "config" / "risk.example.toml"),
        *extra,
    ]


def test_cli_mainnet_shadow_offline_backfill_is_idempotent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from jiaoyi.cli import main

    cache = tmp_path / "market_desk" / "score_cache" / "1m" / "BTCUSDT"
    cache.mkdir(parents=True)
    closes: list[float | None] = [None] * 1440
    closes[179], closes[180], closes[209], closes[210] = 100.1, 100.1, 100.9, 100.9
    (cache / "2026-10-24.json").write_text(json.dumps({"end_ms": 0, "c": closes}))
    _write_ledger(tmp_path, [_rec(c.Side.LONG), _rec(c.Side.LONG, "nodata", symbol="ETHUSDT")])
    assert main(_cli_argv(tmp_path)) == 0
    out = capsys.readouterr().out
    assert "新算 1 笔" in out and "找不到主网价 1 笔" in out
    assert main(_cli_argv(tmp_path)) == 0
    assert "新算 0 笔，改进 0 笔，已有 1 笔" in capsys.readouterr().out
    assert len((tmp_path / sl.SHADOW_FILE).read_text().splitlines()) == 1
    row = sl.read_shadow(tmp_path)["pos-1"]
    assert (row["mainnet_entry"], row["mainnet_exit"]) == (100.1, 100.9)
    assert row["entry_source"] == "cache_kline_1m"


def test_cli_mainnet_shadow_rest_refuses_while_main_process_is_banned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from jiaoyi.cli import main

    monkeypatch.setattr(rest, "_own", rest.RateLimited(418, time.time() + 600))
    rest.save_ban(tmp_path)
    monkeypatch.setattr(rest, "_own", None)

    async def no_network(*a: Any, **k: Any) -> Any:
        raise AssertionError("must not request")

    monkeypatch.setattr(ms, "fetch_json", no_network)
    _write_ledger(tmp_path, [_rec(c.Side.LONG)])
    assert main(_cli_argv(tmp_path, "--source", "rest")) == 1
    assert "限流" in capsys.readouterr().err
    assert not (tmp_path / sl.SHADOW_FILE).exists()


def test_rest_save_ban_writes_live_ban_and_removes_expired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rest, "_own", rest.RateLimited(418, 2_000.0))
    rest.save_ban(tmp_path, now=1_000.0)
    monkeypatch.setattr(rest, "_own", None)
    ban = rest.load_ban(tmp_path, now=1_500.0)
    assert ban is not None and (ban.status, ban.until) == (418, 2_000.0)
    assert rest.blocked() is ban
    monkeypatch.setattr(rest, "_own", None)
    assert rest.load_ban(tmp_path, now=2_500.0) is None
    rest.save_ban(tmp_path, now=2_500.0)
    assert not (tmp_path / rest.BAN_FILE).exists()
