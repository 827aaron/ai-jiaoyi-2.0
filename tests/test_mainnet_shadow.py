"""Mainnet-equivalent shadow ledger: pricing, idempotent backfill, live marks, panel."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from jiaoyi import contracts as c
from jiaoyi.core import shadow_ledger as sl
from jiaoyi.core.config import load_risk_config
from jiaoyi.execution import mainnet_shadow as ms
from jiaoyi.execution.fills import TradeFill
from jiaoyi.market.features import RollingFeatureEngine
from tests.conftest import ROOT

RISK = load_risk_config(ROOT / "config" / "risk.example.toml")
T0 = datetime(2026, 10, 24, 3, 0, tzinfo=UTC)
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
        "closed_at": T0 + timedelta(minutes=30),
        "realized_pnl_usdt": 1.6,
        "fees_usdt": 0.4,
    }
    base.update(kw)
    return c.PositionRecord(**base)


class FakePrices:
    """Mainnet is 10 bp worse than Demo on both legs, in the trade's direction."""

    def __init__(self, table: dict[datetime, float]) -> None:
        self.table = table
        self.calls: list[datetime] = []

    async def price_at(self, symbol: str, t: datetime) -> ms.PricePoint | None:
        self.calls.append(t)
        p = self.table.get(t)
        return None if p is None else ms.PricePoint(p, t, "agg_trade")


def _table(side: c.Side) -> dict[datetime, float]:
    if side is c.Side.LONG:
        return {T0: 100.1, T0 + timedelta(minutes=30): 100.9}
    return {T0: 99.9, T0 + timedelta(minutes=30): 99.1}


@pytest.mark.parametrize(
    ("side", "fees", "net", "gap_bp", "mainnet_r"),
    [
        (c.Side.LONG, 0.201, 1.399, 10.05, 0.6995),  # 5 bp * 2 * (100.1 + 100.9)
        (c.Side.SHORT, 0.199, 1.401, 9.95, 0.7005),  # 5 bp * 2 * (99.9 + 99.1)
    ],
)
async def test_mainnet_shadow_update_long_short_mirrored(
    tmp_path: Path, side: c.Side, fees: float, net: float, gap_bp: float, mainnet_r: float
) -> None:
    shadow = ms.MainnetShadow(tmp_path, FakePrices(_table(side)), lambda: BP)
    res = await shadow.update([_rec(side)], T0 + timedelta(hours=1))
    assert (res.added, res.failed) == (1, 0)
    row = sl.read_shadow(tmp_path)["pos-1"]
    assert row["mainnet_gross_usdt"] == pytest.approx(1.6)
    assert row["price_gap_bp"] == pytest.approx(20.0)
    assert row["mainnet_fees_usdt"] == pytest.approx(fees)
    assert row["mainnet_net_usdt"] == pytest.approx(net)
    assert row["gap_usdt"] == pytest.approx(fees)
    assert row["gap_bp"] == pytest.approx(gap_bp)
    assert row["risk_usdt"] == pytest.approx(2.0)
    assert (row["demo_r"], row["mainnet_r"]) == pytest.approx((0.8, mainnet_r))
    assert row["time_source"] == "ledger"


async def test_mainnet_shadow_update_backfill_is_idempotent(tmp_path: Path) -> None:
    prices = FakePrices(_table(c.Side.LONG))
    shadow = ms.MainnetShadow(tmp_path, prices, lambda: BP)
    now = T0 + timedelta(hours=1)
    recs = [_rec(c.Side.LONG), _rec(c.Side.LONG, "pos-2")]
    assert (await shadow.update(recs, now)).added == 2
    calls = len(prices.calls)
    again = await ms.MainnetShadow(tmp_path, prices, lambda: BP).update(recs, now)
    assert (again.added, again.skipped) == (0, 2)
    assert len(prices.calls) == calls
    lines = (tmp_path / sl.SHADOW_FILE).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    with (tmp_path / sl.SHADOW_FILE).open("a", encoding="utf-8") as f:
        f.write(json.dumps({**first, "mainnet_net_usdt": -99.0}) + "\nnot json\n")
    assert sl.read_shadow(tmp_path)[first["position_id"]]["mainnet_net_usdt"] == pytest.approx(
        1.399
    )


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
    fills = [
        TradeFill(entry_t, "1", "buy", 100.0, 2.0, 0.2, 0.0),
        TradeFill(exit_t, "2", "sell", 101.0, 2.0, 0.2, 2.0),
    ]
    calls: list[tuple[str, datetime]] = []

    async def user_trades(symbol: str, since: datetime) -> list[TradeFill]:
        calls.append((symbol, since))
        return fills

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


async def test_live_shadow_tick_marks_entry_and_prices_close_without_network(
    tmp_path: Path,
) -> None:
    stream: dict[datetime, tuple[float, datetime, str]] = {}

    def feed(symbol: str, t: datetime) -> tuple[float, datetime, str] | None:
        return stream.get(t)

    rec_open = _rec(c.Side.SHORT, status=c.PositionStatus.OPEN, closed_at=None, exit_price=None)
    stream[T0] = (99.9, T0, "ws_trade")
    live = ms.LiveShadow(tmp_path, feed, lambda: BP)
    assert (await live.tick([rec_open], T0 + timedelta(seconds=20))).added == 0
    assert ms.PriceMarks(tmp_path).get("BTCUSDT", T0) == ms.PricePoint(99.9, T0, "mark")

    stream.clear()  # the trade window has moved on; only the close is still covered
    close_t = T0 + timedelta(minutes=30)
    stream[close_t] = (99.1, close_t, "ws_trade")
    restarted = ms.LiveShadow(tmp_path, feed, lambda: BP)
    res = await restarted.tick([_rec(c.Side.SHORT)], close_t + timedelta(seconds=30))
    assert res.added == 1
    row = sl.read_shadow(tmp_path)["pos-1"]
    assert (row["entry_source"], row["exit_source"]) == ("mark", "mark")
    assert row["mainnet_net_usdt"] == pytest.approx(1.401)


async def test_live_shadow_tick_leaves_old_closes_to_the_cli(tmp_path: Path) -> None:
    calls: list[datetime] = []

    def feed(symbol: str, t: datetime) -> tuple[float, datetime, str] | None:
        calls.append(t)
        return None

    live = ms.LiveShadow(tmp_path, feed, lambda: BP)
    res = await live.tick([_rec(c.Side.LONG)], T0 + timedelta(days=3))
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
    rows = [
        {
            "closed_at": "2026-10-24T16:30:00+00:00",  # 23:30 Bangkok
            "demo_net_usdt": 2.0,
            "mainnet_net_usdt": 1.0,
            "notional_usdt": 1000.0,
            "gap_r": 0.1,
            "demo_r": 0.2,
            "mainnet_r": 0.1,
        },
        {
            "closed_at": "2026-10-24T17:30:00+00:00",  # 00:30 next Bangkok day
            "demo_net_usdt": -1.0,
            "mainnet_net_usdt": -3.0,
            "notional_usdt": 1000.0,
            "gap_r": None,
        },
    ]
    a, b = sl.by_day(rows, "Asia/Bangkok")
    assert (a.day, a.gap_usdt, a.gap_bp, a.avg(a.gap_r_sum)) == ("2026-10-24", 1.0, 10.0, 0.1)
    assert (b.day, b.gap_usdt, b.avg(b.gap_r_sum)) == ("2026-10-25", 2.0, None)


def test_history_page_shows_mainnet_compare(tmp_path: Path) -> None:
    from jiaoyi.panel.files import PanelFiles
    from jiaoyi.panel.pages import build_history

    recs = [_rec(c.Side.LONG, "pos-1"), _rec(c.Side.SHORT, "pos-2")]
    doc = {"positions": {r.position_id: {"record": r.model_dump(mode="json")} for r in recs}}
    (tmp_path / "positions.json").write_text(json.dumps(doc))
    row = ms.shadow_row(
        recs[0],
        ms.PricePoint(100.1, T0, "agg_trade"),
        ms.PricePoint(100.9, T0, "agg_trade"),
        entry_time=T0,
        exit_time=T0,
        time_source="ledger",
        taker_fee_bps=BP,
        now=T0,
    )
    (tmp_path / sl.SHADOW_FILE).write_text(json.dumps(row) + "\n")
    files = PanelFiles(tmp_path, RISK.day_reset_tz)
    page = build_history(files, RISK, T0 + timedelta(hours=2), "all", include_tests=True)
    by_id = {t.position_id: t for t in page.rows}
    assert by_id["pos-1"].mainnet_net_usdt == pytest.approx(1.399)
    assert by_id["pos-1"].mainnet_gap_bp == pytest.approx(10.05)
    assert by_id["pos-2"].mainnet_net_usdt is None
    m = page.mainnet
    assert m is not None and (m.covered, m.trades) == (1, 2)
    assert (m.demo_net_usdt, m.mainnet_net_usdt) == pytest.approx((1.6, 1.399))
    [day] = m.days
    assert day.cum_mainnet_usdt == pytest.approx(1.399) and day.gap_r == pytest.approx(0.1005)


def _write_ledger(data: Path, recs: list[c.PositionRecord]) -> None:
    doc = {"positions": {r.position_id: {"record": r.model_dump(mode="json")} for r in recs}}
    (data / "positions.json").write_text(json.dumps(doc))


@pytest.mark.parametrize("on", [False, True])
async def test_app_mainnet_shadow_tick_follows_switch(tmp_path: Path, on: bool) -> None:
    from jiaoyi.app import App
    from jiaoyi.core.config import load_app_config

    close_t = T0 + timedelta(minutes=30)
    now = close_t + timedelta(seconds=30)
    cfg = load_app_config(ROOT / "config" / "app.example.toml").model_copy(
        update={"data_dir": str(tmp_path), "mainnet_shadow_on": on}
    )
    app = App(RISK, cfg, clock=lambda: now, offline=True)
    _write_ledger(tmp_path, [_rec(c.Side.LONG)])
    app.m.features.update(_bar(T0, 100.0, 100.2))  # entry at T0: the bar's open
    app.m.features.update(_trade(close_t, 100.9))
    await app.mainnet_shadow_tick()
    rows = sl.read_shadow(tmp_path)
    if not on:
        assert rows == {} and not (tmp_path / ms.MARKS_FILE).exists()
        return
    row = rows["pos-1"]
    assert (row["entry_source"], row["exit_source"]) == ("ws_kline_1m", "mark")
    assert (row["mainnet_entry"], row["mainnet_exit"]) == (100.0, 100.9)


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
    argv = [
        "mainnet-shadow",
        "--since",
        "2026-10-24",
        "--data-dir",
        str(tmp_path),
        "--app",
        str(ROOT / "config" / "app.example.toml"),
        "--risk",
        str(ROOT / "config" / "risk.example.toml"),
    ]
    assert main(argv) == 0
    out = capsys.readouterr().out
    assert "新算 1 笔" in out and "找不到主网价 1 笔" in out
    assert main(argv) == 0
    assert "新算 0 笔，已有 1 笔" in capsys.readouterr().out
    assert len((tmp_path / sl.SHADOW_FILE).read_text().splitlines()) == 1
    row = sl.read_shadow(tmp_path)["pos-1"]
    assert (row["mainnet_entry"], row["mainnet_exit"]) == (100.1, 100.9)
    assert row["entry_source"] == "cache_kline_1m"
