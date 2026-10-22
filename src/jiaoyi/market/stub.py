"""桩：不连交易所，不产出任何行情；特征永远“数据不够”。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

from jiaoyi.contracts import FeatureSnapshot, MarketEvent


class StubMarketDataFeed:
    def __init__(self) -> None:
        self.symbols: list[str] = []

    async def subscribe(self, symbols: Sequence[str]) -> None:
        self.symbols = list(symbols)

    async def events(self) -> AsyncIterator[MarketEvent]:
        return
        yield  # pragma: no cover  让它成为异步生成器

    async def close(self) -> None:
        return None


class StubFeatureEngine:
    def update(self, event: MarketEvent) -> None:
        return None

    def snapshot(self, symbol: str) -> FeatureSnapshot | None:
        return None
