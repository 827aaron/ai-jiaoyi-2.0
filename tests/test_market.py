"""行情解析、盘口员公式、特征引擎。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from jiaoyi import contracts as c
from jiaoyi import interfaces as i
from jiaoyi.market import book as bk
from jiaoyi.market.binance_ws import BinanceFuturesFeed, parse_message, streams_for
from jiaoyi.market.features import RollingFeatureEngine
from jiaoyi.market.rest import parse_klines

T0 = datetime(2026, 10, 22, 6, 0, tzinfo=UTC)
MS = int(T0.timestamp() * 1000)


def test_interfaces() -> None:
    assert isinstance(BinanceFuturesFeed(), i.MarketDataFeed)
    assert isinstance(RollingFeatureEngine(), i.FeatureEngine)


def test_streams_per_symbol() -> None:
    assert streams_for("BTCUSDT") == [
        "btcusdt@aggTrade",
        "btcusdt@depth5@100ms",
        "btcusdt@forceOrder",
        "btcusdt@markPrice@1s",
        "btcusdt@kline_1m",
    ]


def test_parse_agg_trade_taker_side() -> None:
    msg = {
        "stream": "btcusdt@aggTrade",
        "data": {
            "e": "aggTrade",
            "E": MS,
            "s": "BTCUSDT",
            "p": "68000.1",
            "q": "0.5",
            "T": MS,
            "m": True,
        },
    }
    ev = parse_message(msg)
    assert isinstance(ev, c.Trade)
    assert ev.taker_side == c.Side.SHORT and ev.price == 68000.1 and ev.ts == T0
    msg["data"]["m"] = False  # type: ignore[index]
    ev = parse_message(msg)
    assert isinstance(ev, c.Trade) and ev.taker_side == c.Side.LONG


def test_parse_depth() -> None:
    ev = parse_message(
        {
            "e": "depthUpdate",
            "E": MS,
            "T": MS,
            "s": "BTCUSDT",
            "b": [["68000", "1.5"], ["67999", "0"]],
            "a": [["68000.5", "2"]],
        }
    )
    assert isinstance(ev, c.BookTop)
    assert ev.bids == [(68000.0, 1.5)] and ev.asks == [(68000.5, 2.0)]


def test_parse_force_order_side_is_liquidated_position() -> None:
    ev = parse_message(
        {
            "e": "forceOrder",
            "E": MS,
            "o": {
                "s": "BTCUSDT",
                "S": "SELL",
                "q": "0.2",
                "p": "67900",
                "ap": "67910",
                "z": "0.2",
                "T": MS,
            },
        }
    )
    assert isinstance(ev, c.Liquidation)
    assert ev.side == c.Side.LONG and ev.price == 67910.0 and ev.qty == 0.2


def test_parse_mark_price() -> None:
    ev = parse_message(
        {
            "e": "markPriceUpdate",
            "E": MS,
            "s": "BTCUSDT",
            "p": "68001",
            "i": "67990",
            "r": "0.0001",
            "T": MS + 3600_000,
        }
    )
    assert isinstance(ev, c.MarkPrice)
    assert ev.funding_rate == 0.0001 and ev.index_price == 67990.0
    assert ev.next_funding_at == T0 + timedelta(hours=1)


def test_parse_kline_only_closed() -> None:
    k = {
        "t": MS,
        "T": MS + 59_999,
        "i": "1m",
        "o": "1",
        "h": "2",
        "l": "0.5",
        "c": "1.5",
        "v": "10",
        "q": "15",
    }
    assert parse_message({"e": "kline", "E": MS, "s": "BTCUSDT", "k": {**k, "x": False}}) is None
    ev = parse_message({"e": "kline", "E": MS, "s": "BTCUSDT", "k": {**k, "x": True}})
    assert isinstance(ev, c.Kline) and ev.close == 1.5 and ev.quote_volume == 15.0


def test_parse_unknown_returns_none() -> None:
    assert parse_message({"e": "somethingNew"}) is None
    assert parse_message({"result": None, "id": 1}) is None


def test_rest_klines_drop_unclosed() -> None:
    rows = [
        [MS, "1", "2", "0.5", "1.5", "10", MS + 59_999, "15", 1, "5", "7", "0"],
        [MS + 60_000, "1", "2", "0.5", "1.5", "10", MS + 119_999, "15", 1, "5", "7", "0"],
    ]
    out = parse_klines("BTCUSDT", rows, T0 + timedelta(seconds=90))
    assert len(out) == 1 and out[0].open_time == T0


def _book(t: datetime, bid: float, bq: float, ask: float, aq: float) -> c.BookTop:
    return c.BookTop(symbol="BTCUSDT", ts=t, bids=[(bid, bq)], asks=[(ask, aq)])


def test_book_formulas() -> None:
    b = _book(T0, 100.0, 3.0, 100.2, 1.0)
    assert bk.mid(b) == pytest.approx(100.1)
    assert bk.spread_bps(b) == pytest.approx(0.2 / 100.1 * 1e4)
    assert bk.microprice(b) == pytest.approx((100.0 * 1.0 + 100.2 * 3.0) / 4.0)
    assert bk.imbalance(b) > 0
    # 买一抬高 → 买压为正；卖一被吃掉上移 → 也偏买
    assert bk.ofi_event(b, _book(T0, 100.1, 2.0, 100.2, 1.0)) > 0
    assert bk.ofi_event(b, _book(T0, 99.9, 2.0, 100.1, 2.0)) < 0


def _kline(i: int, close: float, rng: float = 10.0, qv: float = 1_000_000.0) -> c.Kline:
    ot = T0 + timedelta(minutes=i)
    return c.Kline(
        symbol="BTCUSDT",
        interval=c.Interval.M1,
        open_time=ot,
        close_time=ot + timedelta(seconds=59.999),
        open=close,
        high=close + rng / 2,
        low=close - rng / 2,
        close=close,
        volume=qv / close,
        quote_volume=qv,
    )


def warm_engine(n: int = 20, rng: float = 10.0) -> RollingFeatureEngine:
    eng = RollingFeatureEngine()
    for k in range(n):
        eng.update(_kline(k, 68000.0, rng=rng))
    return eng


def test_snapshot_none_until_atr_ready() -> None:
    eng = RollingFeatureEngine()
    for k in range(14):
        eng.update(_kline(k, 68000.0))
    assert eng.snapshot("BTCUSDT") is None
    eng.update(_kline(14, 68000.0))
    snap = eng.snapshot("BTCUSDT")
    assert snap is not None and snap.atr == pytest.approx(10.0)


def test_features_from_trades_book_liqs_and_mark() -> None:
    eng = warm_engine()
    t = T0 + timedelta(minutes=21)
    # 先在 68000 成交，1 分钟后在 68136 放量主动买
    eng.update(c.Trade(symbol="BTCUSDT", ts=t, price=68000.0, qty=0.01, taker_side=c.Side.LONG))
    t2 = t + timedelta(seconds=61)
    for s in range(30):
        eng.update(
            c.Trade(
                symbol="BTCUSDT",
                ts=t2 + timedelta(seconds=s),
                price=68136.0,
                qty=1.0,
                taker_side=c.Side.LONG,
            )
        )
    tl = t2 + timedelta(seconds=29)
    eng.update(c.Trade(symbol="BTCUSDT", ts=tl, price=68136.0, qty=0.5, taker_side=c.Side.SHORT))
    eng.update(_book(tl, 68135.9, 2.0, 68136.1, 1.0))
    eng.update(_book(tl, 68136.0, 2.0, 68136.2, 1.0))
    eng.update(c.Liquidation(symbol="BTCUSDT", ts=tl, side=c.Side.SHORT, price=68136.0, qty=2.0))
    eng.update(
        c.MarkPrice(
            symbol="BTCUSDT", ts=tl, mark_price=68140.0, index_price=68130.0, funding_rate=0.0006
        )
    )

    snap = eng.snapshot("BTCUSDT")
    assert snap is not None
    v = snap.values
    assert v["ret_1m"] == pytest.approx(68136 / 68000 - 1)
    assert v["vol_1m_usdt"] == pytest.approx(30.5 * 68136)
    assert v["vol_ratio_1m"] == pytest.approx(30.5 * 68136 / 1_000_000)
    assert v["taker_buy_ratio_1m"] == pytest.approx(30 / 30.5)
    assert v["cvd_30s_usdt"] > 0
    assert v["liq_short_usdt_1m"] == pytest.approx(2 * 68136)
    assert v["liq_ratio_1m"] == pytest.approx(30.0)  # 30 分钟只有这一笔 → 1 分钟是均值的 30 倍
    assert v["funding_rate"] == 0.0006
    assert v["mark_premium_bps"] == pytest.approx((68140 / 68130 - 1) * 1e4)
    assert snap.book is not None and snap.book.ofi > 0 and snap.book.spread_bps > 0
    assert snap.ts == tl


def test_old_book_is_dropped() -> None:
    eng = warm_engine()
    t = T0 + timedelta(minutes=21)
    eng.update(_book(t, 68000.0, 1.0, 68000.5, 1.0))
    eng.update(
        c.Trade(
            symbol="BTCUSDT",
            ts=t + timedelta(seconds=30),
            price=68000.0,
            qty=1.0,
            taker_side=c.Side.LONG,
        )
    )
    snap = eng.snapshot("BTCUSDT")
    assert snap is not None and snap.book is None


def test_duplicate_klines_ignored() -> None:
    eng = warm_engine()
    before = eng.snapshot("BTCUSDT")
    eng.update(_kline(5, 99999.0, rng=5000.0))  # 回填和推送重叠的旧 K 线
    after = eng.snapshot("BTCUSDT")
    assert before is not None and after is not None and before.atr == after.atr
