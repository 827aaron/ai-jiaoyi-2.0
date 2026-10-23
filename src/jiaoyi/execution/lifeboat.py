"""保命止损：开仓成交后在币安服务器上挂一张更宽的只减仓 STOP_MARKET。

它是最后一道防线：hummingbot-api、大脑程序、Mac mini 全挂了，这张单子还在币安那边。
正常止损（执行器每秒检查）永远先到；保命止损只在它们都没来得及动的时候才会被触发。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from jiaoyi.contracts import LifeboatOrder, Outcome, PositionRecord, Side
from jiaoyi.contracts.module_status import Health
from jiaoyi.contracts.position import LifeboatStatus
from jiaoyi.core.recorder import RoleRecorder
from jiaoyi.execution.clock import Clock, utc_now
from jiaoyi.execution.exchange import STOP_PREFIX, ExchangeClient, ExchangeStop


def lifeboat_price(side: Side, entry_price: float, lifeboat_stop_pct: float) -> float:
    """多单在入场价下方，空单在上方。"""
    delta = entry_price * lifeboat_stop_pct / 100.0
    return entry_price - delta if side is Side.LONG else entry_price + delta


def client_order_id(position_id: str) -> str:
    cid = f"{STOP_PREFIX}{position_id}"
    if len(cid) > 36:  # 币安 clientOrderId 上限 36 字符
        raise ValueError(f"position_id 太长，生成不了合法的 clientOrderId：{position_id}")
    return cid


class BinanceLifeboat:
    """``LifeboatStop`` 的实现。place 幂等：同一仓位重复调用不会挂两张。"""

    def __init__(
        self,
        exchange: ExchangeClient,
        clock: Clock = utc_now,
        recorder: RoleRecorder | None = None,
    ) -> None:
        self._exchange = exchange
        self._clock = clock
        self._rec = recorder

    def heartbeat(self, now: datetime, health: Health = Health.OK, headline: str = "") -> None:
        if self._rec is not None:
            self._rec.heartbeat(now, health, headline or "保命止损待命")

    def _order(self, stop: ExchangeStop, status: LifeboatStatus, now: datetime) -> LifeboatOrder:
        return LifeboatOrder(
            exchange_order_id=stop.order_id,
            client_order_id=stop.client_order_id,
            stop_price=stop.stop_price,
            status=status,
            updated_at=now,
        )

    async def place(self, position: PositionRecord, stop_price: float) -> LifeboatOrder:
        if position.entry_price is None:
            raise ValueError("还没成交，不能挂保命止损")
        if position.side is Side.LONG and stop_price >= position.entry_price:
            raise ValueError("多单的保命止损必须低于入场价")
        if position.side is Side.SHORT and stop_price <= position.entry_price:
            raise ValueError("空单的保命止损必须高于入场价")
        close_side: Literal["buy", "sell"] = "sell" if position.side is Side.LONG else "buy"
        try:
            stop = await self._exchange.place_stop(
                position.symbol,
                close_side,
                position.quantity,
                stop_price,
                client_order_id(position.position_id),
            )
        except Exception as e:
            self._log(Outcome.ERROR, f"{position.symbol} 保命止损挂单失败：{e!r}", position)
            raise
        order = self._order(stop, LifeboatStatus.ACTIVE, self._clock())
        self._log(
            Outcome.ACTION,
            f"{position.symbol} 保命止损已挂在 {stop.stop_price}",
            position,
            {"stop_price": stop.stop_price, "client_order_id": stop.client_order_id},
        )
        return order

    def _log(
        self,
        outcome: Outcome,
        summary: str,
        position: PositionRecord,
        output: dict[str, float | str] | None = None,
    ) -> None:
        if self._rec is not None:
            self._rec.record(
                self._clock(),
                outcome,
                summary,
                symbol=position.symbol,
                signal_id=position.signal_id,
                position_id=position.position_id,
                output=dict(output or {}),
            )

    async def cancel(self, position: PositionRecord) -> None:
        stop = await self._find(position)
        if stop is not None:
            await self._exchange.cancel_stop(position.symbol, stop.order_id)
            self._log(Outcome.INFO, f"{position.symbol} 保命止损已撤", position)

    async def fetch(self, position: PositionRecord) -> LifeboatOrder | None:
        stop = await self._find(position)
        return None if stop is None else self._order(stop, LifeboatStatus.ACTIVE, self._clock())

    async def _find(self, position: PositionRecord) -> ExchangeStop | None:
        cid = client_order_id(position.position_id)
        for stop in await self._exchange.open_stops(position.symbol):
            if stop.client_order_id == cid:
                return stop
        return None
