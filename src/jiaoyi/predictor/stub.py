"""桩：等同于开关关闭。"""

from __future__ import annotations

from datetime import datetime

from jiaoyi.contracts import Forecast, Kline


class StubPredictor:
    @property
    def enabled(self) -> bool:
        return False

    async def on_kline(self, kline: Kline) -> None:
        return None

    def latest(self, symbol: str, now: datetime) -> Forecast | None:
        return None

    def set_wanted(self, symbols: set[str] | frozenset[str]) -> None:
        return None

    def request(self, symbol: str) -> None:
        return None

    def set_priority(self, symbols: set[str] | frozenset[str]) -> None:
        return None

    async def backfill(self, symbol: str, bars: list[Kline]) -> None:
        return None
