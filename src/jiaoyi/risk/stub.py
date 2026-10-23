"""桩：全部拒绝。"""

from __future__ import annotations

from datetime import datetime

from jiaoyi.contracts import (
    AccountState,
    BattleCard,
    CandidateSignal,
    FeatureSnapshot,
    JevResponse,
    RiskVerdict,
)


class StubRiskGuard:
    def review(
        self,
        signal: CandidateSignal,
        jev: JevResponse,
        card: BattleCard | None,
        account: AccountState,
        now: datetime,
        *,
        market: FeatureSnapshot | None,
    ) -> RiskVerdict:
        return RiskVerdict(signal_id=signal.signal_id, approved=False, reasons=["stub: 保镖未接入"])
