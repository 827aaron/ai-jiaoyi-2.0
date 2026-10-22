"""桩：决策模型永远返回“不可用”，拍板人据此必然被保镖拒绝（故障即关闭）。"""

from __future__ import annotations

from datetime import datetime

from jiaoyi.contracts import BattleCard, CandidateSignal, JevRequest, JevResponse


class StubDecisionModel:
    async def ask(self, request: JevRequest) -> JevResponse:
        return JevResponse(
            request_id=request.request_id,
            ok=False,
            error="stub: 决策模型未接入",
            model_version="stub",
            latency_ms=0.0,
        )


class StubEntryGate:
    async def evaluate(
        self, signal: CandidateSignal, card: BattleCard, now: datetime
    ) -> JevResponse:
        return JevResponse(
            request_id=f"stub-{signal.signal_id}",
            ok=False,
            error="stub: 拍板人未接入",
            model_version="stub",
            latency_ms=0.0,
        )
