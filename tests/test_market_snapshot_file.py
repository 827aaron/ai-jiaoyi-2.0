"""盘口员的行情快照文件：面板读 data/market_snapshot.json。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from jiaoyi.contracts import BookTop, Interval, Kline, Liquidation, Side, Trade
from jiaoyi.market.features import RollingFeatureEngine
from jiaoyi.market.snapshot_file import SNAPSHOT_FILE, MarketSnapshotFile

T0 = datetime(2026, 10, 22, 16, 50, tzinfo=UTC)  # 曼谷 23:50


def _bar(t: datetime) -> Kline:
    return Kline(
        symbol="BTCUSDT", interval=Interval.M1, open_time=t,
        close_time=t + timedelta(minutes=1) - timedelta(milliseconds=1),
        open=100.0, high=100.5, low=99.5, close=100.0, volume=10.0, quote_volume=1000.0,
    )  # fmt: skip


def test_snapshot_file_symbols_and_stream_counts(tmp_path: Path) -> None:
    eng = RollingFeatureEngine(atr_period_min=1)
    snap = MarketSnapshotFile(tmp_path, "Asia/Bangkok")
    events: list[object] = [_bar(T0 - timedelta(minutes=30 - m)) for m in range(30)]
    now = T0
    events += [
        Trade(
            symbol="BTCUSDT", ts=now - timedelta(seconds=30), price=100.0, qty=1.0,
            taker_side=Side.LONG,
        ),
        Trade(symbol="BTCUSDT", ts=now, price=101.0, qty=1.0, taker_side=Side.SHORT),
        BookTop(
            symbol="BTCUSDT", ts=now - timedelta(seconds=2),
            bids=[(100.99, 5.0)], asks=[(101.01, 5.0)],
        ),
        Liquidation(symbol="BTCUSDT", ts=now, side=Side.LONG, price=101.0, qty=10.0),
    ]  # fmt: skip
    for ev in events:
        eng.update(ev)  # type: ignore[arg-type]
        snap.count(ev, now)  # type: ignore[arg-type]
    snap.write(eng, ["BTCUSDT", "ETHUSDT"], now)
    data = json.loads((tmp_path / SNAPSHOT_FILE).read_text(encoding="utf-8"))
    btc = data["symbols"]["BTCUSDT"]
    assert set(data["symbols"]) == {"BTCUSDT"}  # 还没有行情的币不写
    assert btc["price"] == 101.0 and btc["taker_buy_ratio_1m"] == round(100 / 201, 4)
    assert btc["liq_long_usdt_1m"] == 1010.0 and btc["liq_short_usdt_1m"] == 0.0
    assert btc["book_age_s"] == 2.0 and btc["spread_bps"] is not None
    st = data["streams"]
    assert st["kline"]["count_today"] == 30 and st["trade"]["count_today"] == 2
    assert st["book"]["last_at"] == now.isoformat()
    # 曼谷过了 0 点：计数清零，新事件重新计
    later = T0 + timedelta(minutes=15)
    snap.count(Trade(symbol="BTCUSDT", ts=later, price=101.0, qty=1.0, taker_side=Side.LONG), later)
    snap.write(eng, ["BTCUSDT"], later)
    data = json.loads((tmp_path / SNAPSHOT_FILE).read_text(encoding="utf-8"))
    assert data["streams"]["trade"]["count_today"] == 1
    assert data["streams"]["kline"]["count_today"] == 0
