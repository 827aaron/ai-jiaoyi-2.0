"""行情解析、盘口员公式、特征引擎。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from jiaoyi import contracts as c
from jiaoyi import interfaces as i
from jiaoyi.market import book as bk
from jiaoyi.market.binance_ws import BinanceFuturesFeed, parse_message, streams_for
from jiaoyi.market.features import RollingFeatureEngine
from jiaoyi.market.provider import MarketData
from jiaoyi.market.rest import WeightBudget, parse_klines
from tests.fastloop_helpers import FakeMarketRest

T0 = datetime(2026, 10, 22, 6, 0, tzinfo=UTC)
MS = int(T0.timestamp() * 1000)


def test_interfaces() -> None:
    assert isinstance(BinanceFuturesFeed(), i.MarketDataFeed)
    assert isinstance(RollingFeatureEngine(), i.FeatureEngine)


def test_streams_per_symbol() -> None:
    assert streams_for("BTCUSDT") == [
        "btcusdt@aggTrade",
        "btcusdt@depth20@100ms",
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


def warm_engine(n: int = 20, rng: float = 10.0, period: int = 1) -> RollingFeatureEngine:
    """默认按 1 分钟 ATR 攒数据，测其它特征用；要测 15 分钟 ATR 就传 period=15、n≥240。"""
    eng = RollingFeatureEngine(atr_period_min=period)
    for k in range(n):
        eng.update(_kline(k, 68000.0, rng=rng))
    return eng


def test_snapshot_none_until_atr_ready() -> None:
    eng = RollingFeatureEngine(atr_period_min=1)
    for k in range(14):
        eng.update(_kline(k, 68000.0))
    assert eng.snapshot("BTCUSDT") is None
    eng.update(_kline(14, 68000.0))
    snap = eng.snapshot("BTCUSDT")
    assert snap is not None and snap.atr == pytest.approx(10.0) and snap.atr_period_min == 1


def test_atr_aggregates_1m_into_15m_bars() -> None:
    from jiaoyi.market.features import backfill_minutes

    eng = RollingFeatureEngine()  # 默认 15 分钟
    assert backfill_minutes(15) == 240
    # 从 06:07 开始：第一段 06:07–06:14 不完整，丢掉；之后每 15 根 1 分钟线一根 15 分钟线
    start = 7
    # 每根 15 分钟线内价格从 c 走到 c+150（1 分钟幅度 10，15 分钟幅度约 150+10）
    i = start
    while i < start + 8 + 15 * 15:
        bar_pos = i % 15
        close = 68000.0 + 10.0 * bar_pos
        eng.update(_kline(i, close))
        i += 1
        if i < start + 8 + 15 * 14:
            assert eng.snapshot("BTCUSDT") is None  # 还没攒够 15 根完整的 15 分钟线
    snap = eng.snapshot("BTCUSDT")
    assert snap is not None and snap.atr_period_min == 15
    # 每根 15 分钟线：高 68140+5，低 68000-5，前收 68140 → TR = 150
    assert snap.atr == pytest.approx(150.0)


def test_long_atr_alongside_short() -> None:
    """配了长周期：快照多带一个长周期 ATR，和单独按长周期算的一样；没攒够时为 None。"""
    from jiaoyi.market.features import backfill_minutes

    assert backfill_minutes(5, 60) == 16 * 60
    both = RollingFeatureEngine(atr_period_min=1, atr_long_period_min=15)
    alone = RollingFeatureEngine(atr_period_min=15)
    for m in range(16 * 15):
        k = _kline(m, 68000.0 + 10.0 * (m % 15))
        both.update(k)
        alone.update(k)
        if m == 30:
            early = both.snapshot("BTCUSDT")
            assert early is not None and early.atr_long is None and early.atr_long_period_min == 15
    snap, ref = both.snapshot("BTCUSDT"), alone.snapshot("BTCUSDT")
    assert snap is not None and ref is not None
    assert snap.atr_period_min == 1 and snap.atr_long == pytest.approx(ref.atr)
    assert RollingFeatureEngine().snapshot("BTCUSDT") is None
    with pytest.raises(ValueError):
        RollingFeatureEngine(atr_period_min=15, atr_long_period_min=15)


def test_atr_median_7d_from_seed_and_live_bars() -> None:
    eng = RollingFeatureEngine(atr_period_min=60)
    hours = [
        c.Kline(
            symbol="BTCUSDT",
            interval=c.Interval.H1,
            open=100.0,
            high=100.0 + w,
            low=100.0,
            close=100.0,
            volume=1.0,
            open_time=T0 - timedelta(hours=40 - i),
            close_time=T0 - timedelta(hours=39 - i),
        )
        for i, w in enumerate([10.0] * 40)
    ]
    assert eng.atr_median_7d("BTCUSDT") is None
    n = eng.seed_atr_history("BTCUSDT", hours[:20])
    assert n == 20 - 14  # 第 1 根没有前收，攒 14 个 TR 才有第一个 ATR
    assert eng.atr_median_7d("BTCUSDT") is None  # 不够 1 天（24 个点）
    assert eng.seed_atr_history("BTCUSDT", hours) == 40 - 14
    assert eng.seed_atr_history("BTCUSDT", hours) == 40 - 14  # 再补一次替换，不重复
    assert len(eng._s["BTCUSDT"].atr_hist) == 26
    assert eng.atr_median_7d("BTCUSDT") == pytest.approx(10.0)


def test_atr_drops_bar_with_missing_minute() -> None:
    eng = RollingFeatureEngine(atr_period_min=5)
    for m in range(5 * 15):
        if m == 12:  # 第 3 根 5 分钟线缺一分钟：整根丢掉，不当完整线算 TR
            continue
        eng.update(_kline(m, 68000.0))
    snap = eng.snapshot("BTCUSDT")
    assert snap is None  # 丢一根后只剩 14 根完整线 → 13 个 TR，不够
    for m in range(5 * 15, 5 * 16):
        eng.update(_kline(m, 68000.0))
    assert eng.snapshot("BTCUSDT") is not None


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


def test_kline_bootstrap_price_is_latest_close_until_first_trade() -> None:
    eng = RollingFeatureEngine(atr_period_min=1)
    for k in range(20):
        eng.update(_kline(k, 66000.0 + 100.0 * k))  # 回填从最老的往后喂
    snap = eng.snapshot("BTCUSDT")
    assert snap is not None and snap.last_price == 67900.0
    t = T0 + timedelta(minutes=20, seconds=5)
    eng.update(c.Trade(symbol="BTCUSDT", ts=t, price=67950.0, qty=1.0, taker_side=c.Side.LONG))
    eng.update(_kline(20, 68500.0))  # 来过成交，收盘价不再顶替现价
    snap = eng.snapshot("BTCUSDT")
    assert snap is not None and snap.last_price == 67950.0


def test_live_s_counts_continuous_stream_and_restarts_after_gap() -> None:
    eng = warm_engine()
    snap = eng.snapshot("BTCUSDT")
    assert snap is not None and "live_s" not in snap.values  # 只有回填 K 线
    t = T0 + timedelta(minutes=21)
    eng.update(c.Trade(symbol="BTCUSDT", ts=t, price=68000.0, qty=1.0, taker_side=c.Side.LONG))
    for s in range(60, 361, 60):  # 冷门币没成交，盘口照推
        eng.update(_book(t + timedelta(seconds=s), 67999.5, 1.0, 68000.5, 1.0))
    snap = eng.snapshot("BTCUSDT")
    assert snap is not None and snap.values["live_s"] == 360.0
    eng.update(_kline(27, 68000.0))  # K 线不算实时行情
    gap = t + timedelta(seconds=360 + 301)
    eng.update(c.Trade(symbol="BTCUSDT", ts=gap, price=68100.0, qty=1.0, taker_side=c.Side.LONG))
    snap = eng.snapshot("BTCUSDT")
    assert snap is not None and snap.values["live_s"] == 0.0


def test_live_s_kept_by_book_or_mark_between_sparse_trades() -> None:
    t = T0 + timedelta(minutes=21)
    quiet: list[Callable[[datetime], c.MarketEvent]] = [
        lambda at: _book(at, 67999.5, 1.0, 68000.5, 1.0),
        lambda at: c.MarkPrice(
            symbol="BTCUSDT", ts=at, mark_price=68000.0, index_price=68000.0, funding_rate=0.0
        ),
    ]
    for event in quiet:
        eng = warm_engine()
        eng.update(c.Trade(symbol="BTCUSDT", ts=t, price=68000.0, qty=1.0, taker_side=c.Side.LONG))
        for s in range(60, 361, 60):
            eng.update(event(t + timedelta(seconds=s)))
        last = t + timedelta(seconds=400)  # 两笔成交隔 400 秒，中间行情一直在推
        eng.update(
            c.Trade(symbol="BTCUSDT", ts=last, price=68010.0, qty=1.0, taker_side=c.Side.LONG)
        )
        snap = eng.snapshot("BTCUSDT")
        assert snap is not None and snap.values["live_s"] == 400.0


def _rest_trades(
    sym: str, start: datetime, end: datetime, step_s: int = 10, price: float = 68000.0
) -> list[tuple[datetime, float, float, bool]]:
    out = []
    at, i = start, 0
    while at <= end:
        out.append((at, price, 1000.0, i % 2 == 0))
        at, i = at + timedelta(seconds=step_s), i + 1
    return out


def _engine(symbols: list[str]) -> RollingFeatureEngine:
    eng = RollingFeatureEngine(atr_period_min=1)
    for sym in symbols:
        for k in range(20):
            eng.update(_kline(k, 68000.0).model_copy(update={"symbol": sym}))
    return eng


def test_rest_seed_fills_before_the_stream_without_double_counting() -> None:
    eng = warm_engine()
    t = T0 + timedelta(minutes=21)
    eng.update(c.Trade(symbol="BTCUSDT", ts=t, price=68000.0, qty=0.1, taker_side=c.Side.LONG))
    eng.update(_book(t, 67999.5, 1.0, 68000.5, 1.0))
    rows = _rest_trades("BTCUSDT", t - timedelta(seconds=300), t + timedelta(seconds=10))
    trades = [
        c.Trade(symbol="BTCUSDT", ts=at, price=px, qty=u / px, taker_side=c.Side.LONG)
        for at, px, u, _ in rows
    ]
    eng.seed_rest(
        "BTCUSDT",
        trades,
        None,
        covered_from=(t - timedelta(seconds=300)).timestamp(),
        fetched_at=(t + timedelta(seconds=11)).timestamp(),
    )
    late = c.Trade(  # 实时流晚到的一笔，REST 已经补过
        symbol="BTCUSDT", ts=t + timedelta(seconds=10), price=68000.0, qty=1000.0 / 68000.0,
        taker_side=c.Side.LONG,
    )  # fmt: skip
    eng.update(late)
    snap = eng.snapshot("BTCUSDT")
    assert snap is not None
    # 最近 60 秒：REST 的 t-50…t-10 五笔 + 实时流 t 那笔 + REST 的 t+10（晚到的不重复记）
    assert snap.values["trades_1m"] == 7.0
    assert snap.values["live_s"] == 10.0 and snap.values["trade_s"] == 310.0
    assert snap.values["ret_5m"] == 0.0 and snap.ts == t + timedelta(seconds=10)


async def test_market_data_parallel_callers_share_one_fetch_per_symbol() -> None:
    syms = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    now = T0 + timedelta(minutes=21)
    eng = _engine(syms)
    fake = FakeMarketRest(
        {s: _rest_trades(s, now - timedelta(seconds=400), now) for s in syms},
        now=now,
        delay=0.05,
    )
    md = MarketData(eng, enabled=lambda: True, clock=lambda: now, fetch=fake)
    loop = asyncio.get_running_loop()
    started = loop.time()
    reads = await asyncio.gather(*(md.snapshot(syms[i % 3], need=("ret_5m",)) for i in range(10)))
    elapsed = loop.time() - started
    assert [fake.agg_calls(s) for s in syms] == [1, 1, 1]
    assert fake.max_inflight >= 2 and elapsed < 0.25  # 三个币一起拉，不是一个接一个（0.3 秒）
    assert all(r.source == "rest" and r.snapshot is not None for r in reads)


async def test_market_data_caches_a_fetch_for_twenty_seconds() -> None:
    now = T0 + timedelta(minutes=21)
    eng = _engine(["BTCUSDT"])
    fake = FakeMarketRest(
        {"BTCUSDT": _rest_trades("BTCUSDT", now - timedelta(seconds=400), now)}, now=now
    )
    mono = [0.0]
    md = MarketData(eng, enabled=lambda: True, clock=lambda: now, fetch=fake, mono=lambda: mono[0])
    for _ in range(3):  # 爆仓历史 REST 补不了：每次都是 stale，但 20 秒内只拉一次
        r = await md.snapshot("BTCUSDT", need=("liq_ratio_1m",))
        assert r.source == "stale" and "liq_ratio_1m" in r.reason
    assert fake.agg_calls() == 1
    mono[0] = 21.0
    await md.snapshot("BTCUSDT", need=("liq_ratio_1m",))
    assert fake.agg_calls() == 2


async def test_market_data_flags_source_and_age() -> None:
    now = T0 + timedelta(minutes=21)
    clock = [now]
    eng = _engine(["BTCUSDT"])
    eng.update(c.Trade(symbol="BTCUSDT", ts=now, price=68000.0, qty=1.0, taker_side=c.Side.LONG))
    later = now + timedelta(seconds=60)
    fake = FakeMarketRest(
        {"BTCUSDT": _rest_trades("BTCUSDT", now, later - timedelta(seconds=1), price=68100.0)},
        now=later,
    )
    md = MarketData(eng, enabled=lambda: True, clock=lambda: clock[0], fetch=fake)
    r = await md.snapshot("BTCUSDT", max_age_s=5)
    assert (r.source, r.age_s, fake.calls) == ("ws", 0.0, [])
    clock[0] = later + timedelta(seconds=2)  # 实时流停了一分钟
    r = await md.snapshot("BTCUSDT", max_age_s=5)
    assert r.source == "rest" and r.age_s == 2.0
    assert r.snapshot is not None and r.snapshot.last_price == 68100.0


async def test_rest_refresh_after_a_stall_counts_each_trade_once_in_the_hour_flow() -> None:
    """冷门币断流 400 秒后补：REST 一页跨了一个多小时，成交流（留 1 小时）里实时流已经记过的
    秒不能再记一遍，也不能插到比它新的秒前面。"""
    t0 = T0 + timedelta(minutes=21)
    eng = _engine(["FILUSDT"])
    for at, px, u, buy in _rest_trades("FILUSDT", t0, t0 + timedelta(seconds=600)):
        side = c.Side.LONG if buy else c.Side.SHORT
        eng.update(c.Trade(symbol="FILUSDT", ts=at, price=px, qty=u / px, taker_side=side))
    now = t0 + timedelta(seconds=1000)
    rows = _rest_trades("FILUSDT", t0 - timedelta(hours=1), now - timedelta(seconds=5))
    fake = FakeMarketRest({"FILUSDT": rows}, now=now)
    md = MarketData(eng, enabled=lambda: True, clock=lambda: now, fetch=fake)
    r = await md.snapshot("FILUSDT", max_age_s=5)
    assert r.source == "rest" and fake.agg_calls() == 1
    flow = list(eng._s["FILUSDT"].flow)
    secs = [b[0] for b in flow]
    assert secs == sorted(set(secs))
    assert {b[1] + b[2] for b in flow} == {1000.0}  # 每秒一笔 1000 U，没有记两遍的秒


@pytest.mark.parametrize(
    ("first_s", "gap_min"),
    [(120, 22), (600, 6)],
    ids=["re-added symbol, old 1h flow still held", "reconnect after a gap over 5 minutes"],
)
def test_rest_seed_after_a_new_segment_reads_the_real_5m_move(first_s: int, gap_min: int) -> None:
    eng = _engine(["SOLUSDT"])
    t = T0 + timedelta(minutes=20)

    def tr(at: datetime, px: float) -> c.Trade:
        return c.Trade(symbol="SOLUSDT", ts=at, price=px, qty=1.0, taker_side=c.Side.LONG)

    for s in range(first_s):
        eng.update(tr(t + timedelta(seconds=s), 100.0))
    back = t + timedelta(seconds=first_s, minutes=gap_min)
    for s in range(5):  # 重新订上 / 重连：价格这 5 分钟已经从 100 走到 110
        eng.update(tr(back + timedelta(seconds=s), 110.0))
    fetched = back + timedelta(seconds=5)
    rows = [tr(back + timedelta(seconds=s), 100.0 + 10.0 * (s + 301) / 306) for s in range(-301, 6)]
    eng.seed_rest(
        "SOLUSDT",
        rows,
        None,
        covered_from=(fetched - timedelta(seconds=302)).timestamp(),
        fetched_at=fetched.timestamp(),
    )
    snap = eng.snapshot("SOLUSDT")
    assert snap is not None
    assert snap.values["trade_s"] >= 300 and snap.values["ret_5m"] > 0.09
    secs = [b[0] for b in eng._s["SOLUSDT"].flow]
    assert secs == sorted(set(secs)) and secs[0] >= (back - timedelta(seconds=302)).timestamp()


def test_forget_rest_drops_trade_s_but_keeps_the_trades() -> None:
    eng = _engine(["SOLUSDT"])
    t = T0 + timedelta(minutes=21)
    rows = [
        c.Trade(symbol="SOLUSDT", ts=t + timedelta(seconds=s), price=100.0, qty=1.0,
                taker_side=c.Side.LONG)
        for s in range(0, 300, 5)
    ]  # fmt: skip
    end = rows[-1].ts.timestamp()
    eng.seed_rest("SOLUSDT", rows, None, covered_from=t.timestamp(), fetched_at=end)
    snap = eng.snapshot("SOLUSDT")
    assert snap is not None and snap.values["trade_s"] >= 295
    eng.forget_rest()
    snap = eng.snapshot("SOLUSDT")
    assert snap is not None and "trade_s" not in snap.values and snap.values["trades_1m"] > 0


async def test_market_data_trims_rest_trades_older_than_the_window() -> None:
    """冷门币一页逐笔成交跨了几小时：只留 5 分钟窗口里的，覆盖也只从窗口起算。"""
    now = T0 + timedelta(minutes=21)
    eng = _engine(["FILUSDT"])
    rows = _rest_trades("FILUSDT", now - timedelta(hours=3), now - timedelta(seconds=1), 60)
    fake = FakeMarketRest({"FILUSDT": rows}, now=now)
    md = MarketData(eng, enabled=lambda: True, clock=lambda: now, fetch=fake)
    await md.seed("FILUSDT")
    flow = list(eng._s["FILUSDT"].flow)
    assert flow and flow[0][0] >= now.timestamp() - 302
    snap = eng.snapshot("FILUSDT")
    assert snap is not None and 300 <= snap.values["trade_s"] <= 302


async def test_market_data_refresh_gives_up_waiting_for_a_busy_budget() -> None:
    now = T0 + timedelta(minutes=21)
    eng = _engine(["BTCUSDT"])
    fake = FakeMarketRest({"BTCUSDT": _rest_trades("BTCUSDT", now, now)}, now=now)

    class Busy(WeightBudget):
        async def take(self, weight: int) -> None:
            await asyncio.Event().wait()  # 启动补行情排着队

    md = MarketData(
        eng, enabled=lambda: True, clock=lambda: now + timedelta(minutes=5), fetch=fake,
        budget=Busy(),
    )  # fmt: skip
    await asyncio.wait_for(md.refresh("BTCUSDT", 5, timeout_s=0.05), 1.0)
    assert fake.calls == []


async def test_market_data_off_reads_memory_only_and_fetches_without_the_queue() -> None:
    now = T0 + timedelta(minutes=21)
    eng = _engine(["BTCUSDT"])
    fake = FakeMarketRest({"BTCUSDT": _rest_trades("BTCUSDT", now, now)}, now=now)

    class NoBudget(WeightBudget):
        async def take(self, weight: int) -> None:
            raise AssertionError("关着时不排预算")

    md = MarketData(eng, enabled=lambda: False, clock=lambda: now, fetch=fake, budget=NoBudget())
    r = await md.snapshot("BTCUSDT", max_age_s=5, need=("ret_5m",))
    assert r.source == "stale" and "行情已旧" in r.reason
    await md.warm(["BTCUSDT"])
    await md.refresh("BTCUSDT", 5)
    assert fake.calls == []
    snap = eng.snapshot("BTCUSDT")
    assert snap is not None and "trade_s" not in snap.values
    await md.fetch("/fapi/v1/premiumIndex", {"symbol": "BTCUSDT"}, 1)  # 持仓量 / 快照补拉照旧
    assert [p for p, _, _ in fake.calls] == ["/fapi/v1/premiumIndex"]


def test_streams_without_per_symbol_liquidations() -> None:
    assert "btcusdt@forceOrder" not in streams_for("BTCUSDT", liquidations=False)
    assert len(streams_for("BTCUSDT", liquidations=False)) == 4


def _liq(sym: str, t: datetime, side: c.Side, usdt: float) -> c.Liquidation:
    return c.Liquidation(symbol=sym, ts=t, side=side, price=100.0, qty=usdt / 100.0)


def test_feed_records_all_market_liquidations_but_forwards_pool_only() -> None:
    feed = BinanceFuturesFeed()
    feed._symset = frozenset({"BTCUSDT"})
    assert feed.accept(_liq("BTCUSDT", T0, c.Side.LONG, 1000.0))
    assert not feed.accept(_liq("DOGEUSDT", T0, c.Side.SHORT, 5000.0))  # 池外：只记不发
    trade = c.Trade(symbol="DOGEUSDT", ts=T0, price=1.0, qty=1.0, taker_side=c.Side.LONG)
    assert not feed.accept(trade)
    now = T0 + timedelta(minutes=1)
    assert feed.liquidations.liquidations_1h_usdt("DOGEUSDT", now) == pytest.approx(5000.0)
    assert [s.symbol for s in feed.liquidations.all_stats(now)] == ["DOGEUSDT", "BTCUSDT"]


def test_liquidation_tracker_window() -> None:
    from jiaoyi.market.liquidations import LiquidationTracker

    tr = LiquidationTracker()
    tr.record(_liq("ETHUSDT", T0, c.Side.LONG, 1000.0))
    tr.record(_liq("ETHUSDT", T0 + timedelta(minutes=30), c.Side.SHORT, 400.0))
    s = tr.stats("ETHUSDT", T0 + timedelta(minutes=45))
    assert (s.long_usdt, s.short_usdt, s.count) == (pytest.approx(1000.0), pytest.approx(400.0), 2)
    # 第一笔已超过 1 小时
    assert tr.liquidations_1h_usdt("ETHUSDT", T0 + timedelta(minutes=61)) == pytest.approx(400.0)
    # 新事件把太旧的裁掉
    tr.record(_liq("ETHUSDT", T0 + timedelta(hours=2), c.Side.LONG, 10.0))
    assert tr.stats("ETHUSDT", T0 + timedelta(hours=2)).count == 1
    assert tr.liquidations_1h_usdt("NOPEUSDT", T0) == 0.0


def test_liquidation_tracker_drops_quiet_symbols() -> None:
    from jiaoyi.market.liquidations import LiquidationTracker

    tr = LiquidationTracker()
    tr.record(_liq("ETHUSDT", T0, c.Side.LONG, 1000.0))
    tr.record(_liq("BTCUSDT", T0 + timedelta(hours=2), c.Side.LONG, 10.0))
    assert [s.symbol for s in tr.all_stats(T0 + timedelta(hours=2))] == ["BTCUSDT"]
    assert set(tr._q) == {"BTCUSDT"}  # 很久没有新强平的币整体清掉，不占内存


def test_liquidation_tracker_readable_from_worker_thread() -> None:
    import threading

    from jiaoyi.market.liquidations import LiquidationTracker

    tr = LiquidationTracker()
    stop = threading.Event()
    errors: list[BaseException] = []

    def reader() -> None:
        while not stop.is_set():
            try:
                tr.liquidations_1h_usdt("ETHUSDT", T0 + timedelta(minutes=30))
                tr.all_stats(T0 + timedelta(minutes=30))
            except BaseException as e:  # 迭代时被改动会抛 RuntimeError
                errors.append(e)
                return

    th = threading.Thread(target=reader)
    th.start()
    for k in range(20000):
        tr.record(_liq("ETHUSDT", T0 + timedelta(milliseconds=k), c.Side.LONG, 1.0))
    stop.set()
    th.join()
    assert errors == []


def test_features_funding_countdown_and_5m_liquidations() -> None:
    eng = warm_engine()
    t = T0 + timedelta(minutes=21)
    eng.update(c.Trade(symbol="BTCUSDT", ts=t, price=68000.0, qty=0.1, taker_side=c.Side.LONG))
    eng.update(_liq("BTCUSDT", t - timedelta(minutes=3), c.Side.LONG, 7000.0))
    eng.update(_liq("BTCUSDT", t, c.Side.SHORT, 2000.0))
    eng.update(
        c.MarkPrice(
            symbol="BTCUSDT",
            ts=t,
            mark_price=68000.0,
            funding_rate=0.0001,
            next_funding_at=t + timedelta(minutes=12),
        )
    )
    snap = eng.snapshot("BTCUSDT")
    assert snap is not None
    v = snap.values
    assert v["funding_in_min"] == pytest.approx(12.0)
    assert v["liq_long_usdt_5m"] == pytest.approx(7000.0) and v["liq_long_usdt_1m"] == 0.0
    assert v["liq_short_usdt_5m"] == pytest.approx(2000.0)


class FakeWs:
    """``recv`` 依次返回给定消息，发完后关闭（或者一直不来，测看门狗）。"""

    def __init__(self, msgs: list[str], *, hang: bool = False) -> None:
        self.msgs = list(msgs)
        self.hang = hang

    async def recv(self) -> str:
        if self.msgs:
            return self.msgs.pop(0)
        if self.hang:
            await asyncio.Event().wait()
        from websockets.exceptions import ConnectionClosedOK
        from websockets.frames import Close

        raise ConnectionClosedOK(Close(1000, ""), Close(1000, ""), True)


def _agg(m: bool) -> str:
    import json

    data = {"e": "aggTrade", "E": MS, "s": "BTCUSDT", "p": "1", "q": "1", "T": MS, "m": m}
    return json.dumps({"stream": "btcusdt@aggTrade", "data": data})


def test_streams_split_by_route() -> None:
    """盘口类走 /public/stream，成交 / 标记价格 / K 线 / 强平走 /market/stream。"""
    from jiaoyi.market.binance_ws import route_of

    assert route_of("btcusdt@depth20@100ms") == "public"
    assert route_of("btcusdt@bookTicker") == "public"
    for s in ("btcusdt@aggTrade", "btcusdt@markPrice@1s", "btcusdt@kline_1m", "!forceOrder@arr"):
        assert route_of(s) == "market"


async def test_subscribe_opens_market_and_public_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feed = BinanceFuturesFeed()
    opened: list[tuple[str, str, float | None]] = []

    async def run(conn: str, url: str, silent_after: float | None = None) -> None:
        opened.append((conn, url, silent_after))

    monkeypatch.setattr(feed, "_run", run)
    await feed.subscribe([f"C{k}USDT" for k in range(41)])  # 41 个币 → 两组
    await asyncio.sleep(0)
    conns = {c: (u, w) for c, u, w in opened}
    assert set(conns) == {"market-0", "public-0", "market-1", "public-1", "liquidations"}
    m0, p0 = conns["market-0"][0], conns["public-0"][0]
    assert m0.startswith("wss://fstream.binance.com/market/stream?streams=")
    assert "@aggTrade" in m0 and "@kline_1m" in m0 and "@markPrice@1s" in m0 and "@depth" not in m0
    assert p0.startswith("wss://fstream.binance.com/public/stream?streams=")
    assert "@depth20@100ms" in p0 and "@aggTrade" not in p0
    assert conns["liquidations"][0].endswith("/market/stream?streams=!forceOrder@arr/!contractInfo")
    assert conns["liquidations"][1] is None and conns["market-0"][1] == feed._stale_s
    await feed.close()


async def test_silent_connection_times_out_for_reconnect() -> None:
    """连上但不推消息：看门狗到点抛 TimeoutError，_run 会记断线并重连。"""
    feed = BinanceFuturesFeed()
    with pytest.raises(TimeoutError):
        await feed._pump(FakeWs([], hang=True), "market-0", 0.05)  # type: ignore[arg-type]


async def test_feed_heartbeat_reports_book_reader_status(tmp_path: Path) -> None:
    """团队页「盘口员」：订阅、连接、今日消息数、分类计数；某条连接断流报 down。"""
    from jiaoyi.core.store import JsonlDecisionLog

    sink = JsonlDecisionLog(tmp_path)
    feed = BinanceFuturesFeed(status_sink=sink, day_reset_tz="Asia/Bangkok", stale_after_s=30)
    now = datetime.now(UTC)

    def status() -> c.ModuleStatus:
        feed.heartbeat(now)
        return sink.read_status()[c.Role.BOOK_READER.value]

    assert status().health == c.Health.STARTING  # 还没订阅

    async def forever() -> None:
        await asyncio.Event().wait()

    feed.symbols, feed._symset = ["BTCUSDT"], frozenset({"BTCUSDT"})
    feed._tasks = [asyncio.create_task(forever())]
    st = status()
    assert st.health == c.Health.STARTING and "等第一条行情" in st.headline

    feed._last["market-0"] = now
    await feed._pump(FakeWs([_agg(False), _agg(True), _agg(True)]), "market-0")  # type: ignore[arg-type]
    eng = RollingFeatureEngine()
    eng.update(_book(now, 100.0, 1.0, 100.1, 1.0))
    feed.set_coverage(eng.coverage)
    st = status()
    assert st.health == c.Health.OK and st.today.calls == 3
    assert "盯着 1 个币" in st.headline and "盘口新鲜 1 个" in st.headline
    assert "成交 3" in st.headline

    feed._down.add("public-0")
    feed.reconnects = 2
    st = status()
    assert (
        st.health == c.Health.DEGRADED and "断 1 条" in st.headline and "重连 2 次" in st.headline
    )

    now = feed._last["market-0"] + timedelta(seconds=45)
    st = status()
    assert st.health == c.Health.DOWN and "market-0 已 45 秒没消息" in st.headline
    await feed.close()
    assert status().health == c.Health.DOWN


# ---- 盘口深度档数 book_depth_levels（Aaron 10-26）：订阅固定 20 档，求和只取前 N 档 ----


def _deep_book(t: datetime) -> c.BookTop:
    bids = [(100.0 - i, 10.0) for i in range(20)]  # 每档 (100-i)×10 U
    asks = [(101.0 + i, 10.0) for i in range(20)]
    return c.BookTop(symbol="BTCUSDT", ts=t, bids=bids, asks=asks)


def test_depth_usdt_sums_only_the_first_n_levels() -> None:
    b = _deep_book(T0)
    for n in (1, 5, 20):
        bid, ask = bk.depth_usdt(b, n)
        assert bid == pytest.approx(sum((100.0 - i) * 10.0 for i in range(n)))
        assert ask == pytest.approx(sum((101.0 + i) * 10.0 for i in range(n)))


def test_parse_depth20_message_keeps_all_levels() -> None:
    msg = {
        "stream": "btcusdt@depth20@100ms",
        "data": {
            "e": "depthUpdate",
            "E": 1_700_000_000_000,
            "T": 1_700_000_000_000,
            "s": "BTCUSDT",
            "b": [[str(100 - i), "1"] for i in range(20)],
            "a": [[str(101 + i), "1"] for i in range(20)],
        },
    }
    ev = parse_message(msg)
    assert isinstance(ev, c.BookTop) and len(ev.bids) == 20 and len(ev.asks) == 20


def test_engine_depth_levels_hot_update() -> None:
    eng = warm_engine()
    eng.set_depth_levels(5)
    now = T0 + timedelta(minutes=21)
    eng.update(c.Trade(symbol="BTCUSDT", ts=now, price=100.5, qty=1.0, taker_side=c.Side.LONG))
    eng.update(_deep_book(now))
    snap = eng.snapshot("BTCUSDT")
    assert snap is not None and snap.book is not None
    d5 = snap.book.depth_usdt
    eng.set_depth_levels(20)
    snap20 = eng.snapshot("BTCUSDT")
    assert snap20 is not None and snap20.book is not None and snap20.book.depth_usdt > d5
    assert snap20.book.bid_depth_usdt == pytest.approx(sum((100.0 - i) * 10.0 for i in range(20)))
    for bad in (0, 21):
        with pytest.raises(ValueError):
            eng.set_depth_levels(bad)


def test_book_depth_levels_setting_is_validated_and_hot() -> None:
    from pydantic import ValidationError

    from jiaoyi.core.hotconfig import HOT_FAST

    assert c.FastLoopConfig().book_depth_levels == 5
    assert c.FastLoopConfig(book_depth_levels=1).book_depth_levels == 1
    for bad in (0, 21, -1):
        with pytest.raises(ValidationError):
            c.FastLoopConfig(book_depth_levels=bad)
    assert "book_depth_levels" in HOT_FAST
