"""执行层测试用的内存假件：假 hummingbot-api、假币安。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any, Literal

from jiaoyi.contracts import Side
from jiaoyi.execution.exchange import (
    CloseFill,
    ExchangeError,
    ExchangePosition,
    ExchangeStop,
    LotFilter,
)
from jiaoyi.execution.hb_client import HbApiError


class FakeHbApi:
    def __init__(self) -> None:
        self.executors: dict[str, dict[str, Any]] = {}
        self.on_terminate: Callable[[str], None] | None = None
        self.created: list[dict[str, Any]] = []
        self.stopped: list[tuple[str, bool]] = []
        self.balances: list[dict[str, Any]] = [
            {"token": "USDT", "units": 1000.0, "available_units": 900.0}
        ]
        self.pos: list[dict[str, Any]] = []
        self.down = False
        self.auto_terminate_on_stop = True
        self.controller_ids: list[str] = []
        self.create_error: HbApiError | None = None
        """设了就在“建好执行器之后”抛出，模拟超时 / 5xx：执行器其实已经建了。"""
        self.create_error_before: HbApiError | None = None
        """设了就在建之前抛出，模拟明确拒绝。"""
        self.find_down = False
        self.stop_error: HbApiError | None = None
        self._n = 0

    def _check(self) -> None:
        if self.down:
            raise HbApiError("hummingbot-api 不可达")

    async def create_executor(
        self, executor_config: dict[str, Any], account_name: str, controller_id: str
    ) -> dict[str, Any]:
        self._check()
        if self.create_error_before is not None:
            raise self.create_error_before
        self._n += 1
        eid = f"ex{self._n}"
        self.created.append(executor_config)
        self.controller_ids.append(controller_id)
        self.executors[eid] = {
            "executor_id": eid,
            "controller_id": controller_id,
            "trading_pair": executor_config["trading_pair"],
            "status": "RUNNING",
            "is_active": True,
            "close_type": None,
            "net_pnl_quote": 0.0,
            "cum_fees_quote": 0.0,
            "error_count": 0,
            "last_error": None,
            "custom_info": {"current_position_average_price": 0},
        }
        if self.create_error is not None:
            raise self.create_error
        return {"executor_id": eid}

    def fill(self, eid: str, price: float) -> None:
        self.executors[eid]["custom_info"]["current_position_average_price"] = price

    def terminate(
        self,
        eid: str,
        close_type: str,
        pnl: float | None = None,
        *,
        leaves_position: bool = False,
    ) -> None:
        """执行器结束。默认像真的 hummingbot 一样把仓位一起平掉；``leaves_position=True`` 模拟
        重启被 SYSTEM_CLEANUP 终止、币安上仓位还在。"""
        self.executors[eid].update(status="TERMINATED", is_active=False, close_type=close_type)
        if self.on_terminate is not None and not leaves_position:
            self.on_terminate(eid)
        if pnl is not None:
            self.executors[eid]["net_pnl_quote"] = pnl

    async def get_executor(self, executor_id: str) -> dict[str, Any] | None:
        self._check()
        return self.executors.get(executor_id)

    async def stop_executor(self, executor_id: str, *, keep_position: bool) -> None:
        self._check()
        if self.stop_error is not None:
            raise self.stop_error
        self.stopped.append((executor_id, keep_position))
        if self.auto_terminate_on_stop:
            self.terminate(executor_id, "EARLY_STOP")

    async def find_executors(self, account_name: str, controller_id: str) -> list[dict[str, Any]]:
        self._check()
        if self.find_down:
            raise HbApiError("查询超时")
        return [e for e in self.executors.values() if e["controller_id"] == controller_id]

    async def running_executors(self, account_name: str) -> list[dict[str, Any]]:
        self._check()
        return [e for e in self.executors.values() if e["is_active"]]

    async def portfolio_state(self, account_name: str, connector_name: str) -> list[dict[str, Any]]:
        self._check()
        return self.balances

    async def positions(self, account_name: str, connector_name: str) -> list[dict[str, Any]]:
        self._check()
        return self.pos

    async def close(self) -> None: ...


class FakeExchange:
    def __init__(self) -> None:
        self.pos: list[ExchangePosition] = []
        self.stops: dict[str, ExchangeStop] = {}
        self.placed: list[tuple[str, str, float, float, str]] = []
        self.cancelled: list[str] = []
        self.fail_place: ExchangeError | bool = False
        self.fail_cancel = False
        self.market_closed: list[tuple[str, str, float]] = []
        self.fail_market_close = False
        self.income = 0.0
        self.wallet = 1000.0
        self.exit_price: float | None = None
        self.exit_fee: float | None = None
        self.manual_fill: CloseFill | None = None
        """手动平仓后“最近一笔只减仓成交”；None = 查不到。"""
        self.mark: float | None = None
        self.leverage_set: list[tuple[str, int]] = []
        self.leverage_error: str | None = None
        self.filters: dict[str, LotFilter] = {"BTCUSDT": LotFilter(0.001, 0.001, 100.0)}
        self.filters_down = False
        self.income_down = False
        self.down = False
        self._n = 0

    def set_position(self, symbol: str, side: Side, qty: float) -> None:
        self.pos = [p for p in self.pos if p.symbol != symbol]
        if qty:
            self.pos.append(ExchangePosition(symbol, side, qty))

    async def positions(self) -> list[ExchangePosition]:
        if self.down:
            raise ExchangeError("币安不可达")
        return list(self.pos)

    async def place_stop(
        self,
        symbol: str,
        close_side: Literal["buy", "sell"],
        quantity: float,
        stop_price: float,
        client_order_id: str,
    ) -> ExchangeStop:
        if self.fail_place:
            raise (
                self.fail_place
                if isinstance(self.fail_place, ExchangeError)
                else ExchangeError("拒单")
            )
        for s in self.stops.values():
            if s.client_order_id == client_order_id:
                return s
        self._n += 1
        stop = ExchangeStop(symbol, f"o{self._n}", client_order_id, stop_price, True)
        self.stops[stop.order_id] = stop
        self.placed.append((symbol, close_side, quantity, stop_price, client_order_id))
        return stop

    async def cancel_stop(self, symbol: str, order_id: str) -> None:
        if self.fail_cancel:
            raise ExchangeError("撤单失败")
        self.stops.pop(order_id, None)
        self.cancelled.append(order_id)

    async def open_stops(self, symbol: str | None = None) -> list[ExchangeStop]:
        if self.down:
            raise ExchangeError("币安不可达")
        return [s for s in self.stops.values() if symbol is None or s.symbol == symbol]

    async def close(self) -> None: ...

    async def close_position_market(
        self, symbol: str, close_side: Literal["buy", "sell"], quantity: float
    ) -> CloseFill:
        if self.fail_market_close:
            raise ExchangeError("直连平仓失败")
        self.market_closed.append((symbol, close_side, quantity))
        self.set_position(symbol, Side.LONG, 0)
        return CloseFill(self.exit_price, self.exit_fee)

    async def reduce_fill_since(
        self, symbol: str, close_side: Literal["buy", "sell"], since: datetime
    ) -> CloseFill | None:
        return self.manual_fill

    async def mark_price(self, symbol: str) -> float | None:
        return self.mark

    async def income_since(self, since: datetime) -> float:
        if self.income_down:
            raise ExchangeError("收入流水不可用")
        return self.income

    async def wallet_usdt(self) -> float:
        return self.wallet

    async def ensure_leverage(self, symbol: str, leverage: int) -> None:
        if self.leverage_error:
            raise ExchangeError(self.leverage_error)
        self.leverage_set.append((symbol, leverage))

    async def lot_filters(self) -> dict[str, LotFilter]:
        if self.filters_down:
            raise ExchangeError("exchangeInfo 不可用")
        return dict(self.filters)
