"""桩：永远维持现状。"""

from __future__ import annotations

from datetime import datetime

from jiaoyi.contracts import KeeperVerdict, PositionRecord


class StubKeeper:
    async def review(self, position: PositionRecord, now: datetime) -> KeeperVerdict:
        return KeeperVerdict(
            position_id=position.position_id,
            ts=now,
            action="hold",
            p_close=0.0,
            consecutive=1,
            reason="stub: 管家未接入，维持现状",
        )
