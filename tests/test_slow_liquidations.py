from __future__ import annotations

import contextlib
from datetime import datetime

from jiaoyi.slow.liquidations import describe_total
from jiaoyi.slow.screener import CodeSymbolScreener, SymbolStats

NOW = datetime(2026, 10, 23, 9, 0).astimezone()


class FakeLiq:
    def __init__(self, table: dict[str, float]) -> None:
        self.table = table

    def liquidations_1h_usdt(self, symbol: str, now: datetime) -> float:
        return self.table.get(symbol, 0.0)


def test_describe() -> None:
    assert "2,500,000" in describe_total(2_500_000)
    assert "无强平记录" in describe_total(0.0)


class Src:
    async def fetch(self, now: datetime) -> list[SymbolStats]:
        liq = FakeLiq({"AAUSDT": 5e6})
        return [
            SymbolStats(
                s, 1e8, 1, 5e5, 100, 1.0, liquidations_1h_usdt=liq.liquidations_1h_usdt(s, now)
            )
            for s in ("AAUSDT", "BBUSDT")
        ]


async def test_liquidations_raise_screener_score() -> None:
    pool = await CodeSymbolScreener(Src()).screen(NOW)
    by = {e.symbol: e for e in pool.entries}
    assert by["AAUSDT"].score > by["BBUSDT"].score
    assert by["AAUSDT"].score_breakdown["liquidations"] > 0.9


async def test_engine_snapshots_in_event_loop_before_worker_thread() -> None:
    """统计对象只能在事件循环线程里被读；工作线程只读快照。"""
    import threading

    from jiaoyi.contracts import Category, HardFilters, SymbolPoolEntry
    from jiaoyi.slow import tradingagents_engine as te

    main_thread = threading.get_ident()
    seen: list[int] = []

    class Liq:
        def liquidations_1h_usdt(self, symbol: str, now: datetime) -> float:
            seen.append(threading.get_ident())
            return 42.0

    eng = te.TradingAgentsEngine.__new__(te.TradingAgentsEngine)
    eng._liq = Liq()
    eng._today = lambda: NOW
    entry = SymbolPoolEntry(
        symbol="BTCUSDT", category=Category.MAJOR, score=0.5, in_pool=True,
        filters=HardFilters(
            quote_volume_24h_usdt=1e8, spread_bps=1, depth_usdt=1e6, listed_days=100, passed=True
        ),
    )  # fmt: skip

    def fake_sync(e: object, regime: str) -> None:
        seen.append(threading.get_ident())
        raise RuntimeError("stop")  # 只关心线程，不跑真会议

    eng._analyze_sync = fake_sync  # type: ignore[method-assign]
    with contextlib.suppress(RuntimeError):
        await eng.analyze(entry, "x")
    assert seen[0] == main_thread  # 统计对象在事件循环线程里读的
    assert seen[1] != main_thread  # 会议在工作线程里跑
    assert te._LIQ_TOTALS["BTCUSDT"] == 42.0
    te.set_liquidation_totals({})
