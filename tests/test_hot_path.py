"""行情热路径的增量算法（每条行情摊下来 O(1)）和直接对整个窗口算的结果对得上。

参考实现都写在测试里：巡逻兵 1 分钟最高 / 最低 = 对窗口直接 max / min；特征引擎的成交额 =
对 ``st.trades`` 直接求精确和（逐位相同），和改之前的逐个累加比误差在 1e-12 以内；
OFI 同理；7 天 ATR 中位数缓存 = 每次重算的 ``statistics.median``。
"""

from __future__ import annotations

import asyncio
import faulthandler
import json
import logging
import math
import os
import random
import signal
import statistics
import time
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from jiaoyi import contracts as c
from jiaoyi.core.loop_lag import LoopLagMonitor, register_stack_dump
from jiaoyi.market.features import RollingFeatureEngine, _ExactSum
from jiaoyi.market.snapshot_file import MarketSnapshotFile
from jiaoyi.patrol.rules import WINDOW, _Window

T0 = datetime(2026, 10, 22, 6, 0, tzinfo=UTC)
SYM = "BTCUSDT"


def test_patrol_window_hi_lo_equal_direct_max_min() -> None:
    rng = random.Random(3)
    win, ref = _Window(), deque[tuple[datetime, float]]()
    t = T0
    for _ in range(20_000):
        # 时间大多往前走，也有相同的、倒退的（乱序到达）；价格常有重复
        t += timedelta(
            milliseconds=rng.choices(
                [0, 5, 20, 80, 400, 3000, -700, -8000], [5, 20, 20, 20, 10, 5, 2, 1]
            )[0]
        )
        price = round(68000.0 + rng.gauss(0.0, 40.0), rng.choice([0, 1, 2]))
        win.push(t, price)
        win.prune(t - WINDOW)
        ref.append((t, price))
        while ref and ref[0][0] < t - WINDOW:
            ref.popleft()
        assert list(win.px) == list(ref)
        assert win.hi[0][1] == max(p for _, p in ref)
        assert win.lo[0][1] == min(p for _, p in ref)


def test_exact_sum_matches_fsum_of_what_is_left() -> None:
    rng = random.Random(5)
    s, live = _ExactSum(), deque[float]()
    for _ in range(20_000):
        if live and rng.random() < 0.45:
            s.add(live.popleft(), -1)
        else:
            x = rng.choice([rng.expovariate(1e-4), -rng.expovariate(1e-2), 1e-9, 3.3e7])
            s.add(x)
            live.append(x)
        assert s.value() == math.fsum(live)
    while live:
        s.add(live.popleft(), -1)
    assert s.value() == 0.0 and math.copysign(1.0, s.value()) == 1.0


def test_exact_sum_non_finite_follows_ieee_and_recovers() -> None:
    s = _ExactSum()
    s.add(1.5)
    s.add(math.inf)
    assert s.value() == math.inf
    s.add(-math.inf)
    assert math.isnan(s.value())
    s.add(math.inf, -1)
    s.add(-math.inf, -1)
    assert s.value() == 1.5


def _kline(i: int, close: float) -> c.Kline:
    ot = T0 + timedelta(minutes=i)
    return c.Kline(
        symbol=SYM,
        interval=c.Interval.M1,
        open_time=ot,
        close_time=ot + timedelta(seconds=59.999),
        open=close,
        high=close + 5.0,
        low=close - 5.0,
        close=close,
        volume=1.0,
        quote_volume=1_000_000.0,
    )


def _old_flow(trades: Any, now: float) -> dict[str, float]:
    """改之前 ``_values`` / ``_book_metrics`` 的逐个累加。"""
    buy = sell = buy_10 = sell_10 = cvd_30 = 0.0
    n = 0
    for t, usdt, is_buy in trades:
        if t < now - 60.0:
            continue
        n += 1
        if is_buy:
            buy += usdt
        else:
            sell += usdt
        if t >= now - 30.0:
            cvd_30 += usdt if is_buy else -usdt
        if t >= now - 10.0:
            if is_buy:
                buy_10 += usdt
            else:
                sell_10 += usdt
    return {
        "buy": buy,
        "sell": sell,
        "n": n,
        "cvd_30": cvd_30,
        "buy_10": buy_10,
        "sell_10": sell_10,
    }


def _exact_flow(trades: Any, now: float) -> dict[str, float]:
    rows = list(trades)

    def f(lo: float, buy: bool) -> float:
        return math.fsum(u for t, u, b in rows if t >= now - lo and b is buy)

    return {
        "buy": f(60.0, True),
        "sell": f(60.0, False),
        "n": sum(1 for t, _, _ in rows if t >= now - 60.0),
        "cvd_30": f(30.0, True) - f(30.0, False),
        "buy_10": f(10.0, True),
        "sell_10": f(10.0, False),
    }


def _snap_flow(snap: c.FeatureSnapshot) -> dict[str, float]:
    v, b = snap.values, snap.book
    assert b is not None
    return {
        "vol": v["vol_1m_usdt"],
        "n": v["trades_1m"],
        "cvd_1m": v["cvd_1m_usdt"],
        "cvd_30": v["cvd_30s_usdt"],
        "ratio_1m": v["taker_buy_ratio_1m"],
        "ratio_10s": v.get("taker_buy_ratio_10s", -1.0),
        "vol_10s": v.get("vol_ratio_10s", -1.0),
        "book_cvd": b.cvd,
        "book_ratio": b.taker_buy_ratio,
        "ofi": b.ofi,
    }


def _expect(f: dict[str, float], ofi: float, avg_qv: float) -> dict[str, float]:
    buy, sell, b10, s10 = f["buy"], f["sell"], f["buy_10"], f["sell_10"]
    return {
        "vol": buy + sell,
        "n": float(f["n"]),
        "cvd_1m": buy - sell,
        "cvd_30": f["cvd_30"],
        "ratio_1m": buy / (buy + sell) if buy + sell > 0 else 0.5,
        "ratio_10s": b10 / (b10 + s10) if b10 + s10 > 0 else -1.0,
        "vol_10s": (b10 + s10) * 6.0 / avg_qv,
        "book_cvd": buy - sell,
        "book_ratio": buy / (buy + sell) if buy + sell > 0 else 0.5,
        "ofi": ofi,
    }


def test_snapshot_flow_and_ofi_equal_direct_window_sums() -> None:
    """一个币的真实节奏：成交忽快忽慢（含停顿超过窗口）、偶尔乱序一笔、中途补 REST 成交，
    盘口每 100 毫秒、标记价每秒。每笔成交后的快照：成交额、笔数、CVD、主动买占比、OFI
    和直接对窗口求精确和逐位相同，和改之前的逐个累加相差不超过 1e-12 × 成交额。"""
    rng = random.Random(11)
    eng = RollingFeatureEngine(atr_period_min=1)
    for k in range(30):
        eng.update(_kline(k, 68000.0))
    st = eng._s[SYM]
    t = (T0 + timedelta(minutes=31)).timestamp()
    px, checked, reordered = 68000.0, 0, 0
    next_book = next_mark = t
    for step in range(4000):
        t += rng.choice([0.0, 0.001, 0.01, 0.05, 0.2, 1.5]) + (75.0 if step == 3000 else 0.0)
        while next_book <= t:
            spread = rng.uniform(0.1, 1.0)
            eng.update(
                c.BookTop(
                    symbol=SYM,
                    ts=datetime.fromtimestamp(next_book, UTC),
                    bids=[(px - spread, rng.uniform(0.1, 9.0))],
                    asks=[(px + spread, rng.uniform(0.1, 9.0))],
                )
            )
            next_book += 0.1
        while next_mark <= t:
            eng.update(
                c.MarkPrice(
                    symbol=SYM,
                    ts=datetime.fromtimestamp(next_mark, UTC),
                    mark_price=px,
                    funding_rate=0.0001,
                )
            )
            next_mark += 1.0
        px *= math.exp(rng.gauss(0.0, 2e-4))
        late = step > 100 and rng.random() < 0.003
        at = t - (rng.uniform(0.5, 40.0) if late else 0.0)
        reordered += at < t
        eng.update(
            c.Trade(
                symbol=SYM,
                ts=datetime.fromtimestamp(at, UTC),
                price=px,
                qty=rng.expovariate(1.0) * rng.choice([0.001, 0.1, 3.0]),
                taker_side=rng.choice([c.Side.LONG, c.Side.SHORT]),
            )
        )
        if step == 50:
            rest = [
                c.Trade(
                    symbol=SYM,
                    ts=datetime.fromtimestamp(st.trades[0][0] - 1.0 - k * 0.37, UTC),
                    price=px,
                    qty=0.2,
                    taker_side=c.Side.LONG if k % 3 else c.Side.SHORT,
                )
                for k in range(40)
            ][::-1]
            before = len(st.trades)
            assert st.flows is not None  # 下面补进来的成交要让增量窗口作废
            eng.seed_rest(SYM, rest, None, covered_from=rest[0].ts.timestamp(), fetched_at=t)
            assert len(st.trades) == before + 40
        snap = eng.snapshot(SYM)
        assert snap is not None and snap.book is not None
        now = st.last_ts
        trades = list(st.trades)
        avg_qv = 1_000_000.0
        got = _snap_flow(snap)
        assert got == _expect(_exact_flow(trades, now), math.fsum(e for _, e in st.ofi), avg_qv)
        old = _expect(_old_flow(trades, now), sum(e for _, e in st.ofi), avg_qv)
        scale = math.fsum(u for t_, u, _ in trades if t_ >= now - 60.0) + 1.0
        ofi_scale = math.fsum(abs(e) for _, e in st.ofi) + 1.0
        for key, value in got.items():
            tol = 1e-12 * (ofi_scale if key == "ofi" else scale)
            if key.startswith(("ratio", "book_ratio", "vol_10s")):
                tol = 1e-12
            assert value == pytest.approx(old[key], rel=0, abs=tol), key
        checked += 1
    assert checked == 4000 and reordered > 5


def test_atr_median_cache_follows_every_change() -> None:
    eng = RollingFeatureEngine(atr_period_min=1)
    rng = random.Random(2)
    hist = []
    for k in range(1500):
        ot = T0 - timedelta(minutes=1500 - k)
        close = 68000.0 + rng.gauss(0.0, 30.0)
        hist.append(
            c.Kline(
                symbol=SYM,
                interval=c.Interval.M1,
                open_time=ot,
                close_time=ot + timedelta(seconds=59.999),
                open=close,
                high=close + rng.uniform(1.0, 30.0),
                low=close - rng.uniform(1.0, 30.0),
                close=close,
                volume=1.0,
            )
        )
    eng.seed_atr_history(SYM, hist)
    st = eng._s[SYM]
    for k in range(40):
        eng.update(_kline(k, 68000.0 + rng.gauss(0.0, 30.0)))
        got = eng.atr_median_7d(SYM)
        assert got == statistics.median(a for _, a in st.atr_hist)
        assert eng.atr_median_7d(SYM) == got
    eng.seed_atr_history(SYM, hist[:-300])
    assert eng.atr_median_7d(SYM) == statistics.median(a for _, a in st.atr_hist)


def test_loop_lag_summary_and_warning(caplog: pytest.LogCaptureFixture) -> None:
    mon = LoopLagMonitor(tick_s=1.0, report_every_s=3.0)
    assert mon.status() == {"last_ms": None, "last_minute": None}
    mon.record(0.001)
    mon.record(0.002)
    assert mon.summary is None
    with caplog.at_level(logging.WARNING):
        mon.record(1.5)
    assert mon.status() == {
        "last_ms": 1500.0,
        "last_minute": {"max_ms": 1500.0, "avg_ms": 501.0, "samples": 3},
    }
    assert "事件循环被卡" in caplog.text
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            mon.record(0.01)
    assert mon.summary == {"max_ms": 10.0, "avg_ms": 10.0, "samples": 3}
    assert caplog.text == ""


async def test_loop_lag_measures_a_blocked_loop() -> None:
    mon = LoopLagMonitor(tick_s=0.05, report_every_s=60.0)
    task = asyncio.create_task(mon.run())
    await asyncio.sleep(0.01)
    assert mon.last_s is None
    time.sleep(0.2)  # 占住事件循环

    async def first() -> None:
        while mon.last_s is None:
            await asyncio.sleep(0.001)

    await asyncio.wait_for(first(), 5.0)
    task.cancel()
    assert mon.last_s >= 0.1


@pytest.mark.skipif(not hasattr(signal, "SIGUSR1"), reason="没有 SIGUSR1")
def test_sigusr1_dumps_all_thread_stacks(tmp_path: Path) -> None:
    f = register_stack_dump(tmp_path / "logs")
    assert f is not None
    try:
        os.kill(os.getpid(), signal.SIGUSR1)
        f.flush()
        text = (tmp_path / "logs" / "faulthandler.log").read_text()
        assert "test_sigusr1_dumps_all_thread_stacks" in text
    finally:
        faulthandler.unregister(signal.SIGUSR1)
        f.close()


class _NoFeatures:
    def snapshot(self, symbol: str) -> None:
        return None


def test_snapshot_file_carries_loop_lag(tmp_path: Path) -> None:
    out = MarketSnapshotFile(tmp_path, "UTC")
    lag = {"last_ms": 0.5, "last_minute": None}
    out.write(_NoFeatures(), [], T0, loop_lag=lag)  # type: ignore[arg-type]
    assert json.loads((tmp_path / "market_snapshot.json").read_text())["loop_lag"] == lag
