"""桩：不连 hummingbot-api、不连币安。开仓直接报错，查询返回空。"""

from __future__ import annotations

from datetime import datetime

from jiaoyi.contracts import (
    AccountState,
    LifeboatOrder,
    OrderIntent,
    PositionRecord,
    ReconcileReport,
)
from jiaoyi.contracts.position import CloseReason


class StubExecutorAdapter:
    def __init__(self, now: datetime) -> None:
        self._now = now

    async def open(self, intent: OrderIntent) -> PositionRecord:
        raise NotImplementedError("stub: 执行器未接入，拒绝开仓")

    async def close(self, position_id: str, reason: CloseReason) -> None:
        raise NotImplementedError("stub: 执行器未接入")

    async def positions(self) -> list[PositionRecord]:
        return []

    async def account(self) -> AccountState:
        return AccountState(
            ts=self._now,
            equity_usdt=0.0,
            available_usdt=0.0,
            day_start_equity_usdt=1.0,
            open_notional_usdt=0.0,
            open_positions=0,
            paused=True,
        )


class StubLifeboatStop:
    async def place(self, position: PositionRecord, stop_price: float) -> LifeboatOrder:
        raise NotImplementedError("stub: 保命止损未接入")

    async def cancel(self, position: PositionRecord) -> None:
        raise NotImplementedError("stub: 保命止损未接入")

    async def fetch(self, position: PositionRecord) -> LifeboatOrder | None:
        return None


class StubReconciler:
    async def reconcile(self, now: datetime) -> ReconcileReport:
        return ReconcileReport(ts=now, checked_positions=0)


class StubLotRules:
    """桩：拿不到交易所精度，保镖据此一律拒单。真实现由执行层从 exchangeInfo 读。"""

    def lot_rules(self, symbol: str) -> None:
        return None
