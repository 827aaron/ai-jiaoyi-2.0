from __future__ import annotations

from datetime import datetime

from jiaoyi.slow.liquidations import describe_liquidations
from jiaoyi.slow.screener import CodeSymbolScreener, SymbolStats

NOW = datetime(2026, 10, 23, 9, 0).astimezone()


class FakeLiq:
    def __init__(self, table: dict[str, float]) -> None:
        self.table = table

    def liquidations_1h_usdt(self, symbol: str, now: datetime) -> float:
        return self.table.get(symbol, 0.0)


def test_describe() -> None:
    liq = FakeLiq({"BTCUSDT": 2_500_000})
    assert "2,500,000" in describe_liquidations(liq, "BTCUSDT", NOW)
    assert "无强平记录" in describe_liquidations(liq, "ETHUSDT", NOW)


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
