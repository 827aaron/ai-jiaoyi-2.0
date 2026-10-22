"""桩：从不动作。"""

from __future__ import annotations

from collections.abc import Sequence

from jiaoyi.contracts import MarketEvent, PatrolAction, PositionRecord


class StubPatrol:
    def check(self, event: MarketEvent, positions: Sequence[PositionRecord]) -> PatrolAction | None:
        return None
