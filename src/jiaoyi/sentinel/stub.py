"""桩：永远不出信号。"""

from __future__ import annotations

from datetime import datetime

from jiaoyi.contracts import BattleCard, CandidateSignal, FeatureSnapshot


class StubTrigger:
    def check(
        self, features: FeatureSnapshot, card: BattleCard | None, now: datetime
    ) -> list[CandidateSignal]:
        return []
