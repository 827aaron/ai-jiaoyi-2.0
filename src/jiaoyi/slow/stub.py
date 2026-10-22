"""桩：池子为空、没有作战卡 → 快环不会开任何新仓。"""

from __future__ import annotations

from datetime import datetime

from jiaoyi.contracts import BattleCard, SymbolPool


class StubSymbolScreener:
    async def screen(self, now: datetime) -> SymbolPool:
        return SymbolPool(pool_id=f"stub-{now.isoformat()}", ts=now, entries=[])


class StubBattleCardProvider:
    def current(self, now: datetime) -> BattleCard | None:
        return None

    async def refresh(self, pool: SymbolPool, reason: str) -> BattleCard:
        raise NotImplementedError("stub: 军师团未接入")
