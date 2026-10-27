"""执行层测试用的内存假件：假 hummingbot-api、假币安。"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from typing import Any, Literal

from jiaoyi.contracts import Side
from jiaoyi.execution.exchange import (
    CloseFill,
    ExchangeEntryOrder,
    ExchangeError,
    ExchangePosition,
    ExchangeStop,
    IdentifiedCloseOrder,
    IdentifiedCloseUnknown,
    IncomeRow,
    LotFilter,
)
from jiaoyi.execution.fills import TradeFill
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
        self.exchange: FakeExchange | None = None
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
            # 和 hummingbot 一样：没成交时 current_position_average_price 也可能有数（计划价）
            "is_trading": False,
            "filled_amount_quote": 0.0,
            "custom_info": {"current_position_average_price": 0, "order_ids": []},
        }
        if (
            self.exchange is not None
            and executor_config.get("side") in (1, 2)
            and executor_config.get("amount") is not None
        ):
            cid = f"o-{eid}"
            self.executors[eid]["custom_info"]["order_ids"] = [cid]
            self.exchange.entry_orders[cid] = ExchangeEntryOrder(
                symbol=str(executor_config["trading_pair"]).replace("-", ""),
                client_order_id=cid,
                order_id=str(self._n),
                side="buy" if executor_config["side"] == 1 else "sell",
                quantity=float(executor_config["amount"]),
                filled_quantity=0.0,
                status="NEW",
                position_side="BOTH",
            )
        if self.create_error is not None:
            raise self.create_error
        return {"executor_id": eid}

    def fill(self, eid: str, price: float, *, filled_quantity: float | None = None) -> None:
        ex = self.executors[eid]
        ex["custom_info"]["current_position_average_price"] = price
        ex["custom_info"]["order_ids"] = [f"o-{eid}"]
        ex["is_trading"] = True
        ex["filled_amount_quote"] = price
        if self.exchange is not None and f"o-{eid}" in self.exchange.entry_orders:
            cid = f"o-{eid}"
            order = self.exchange.entry_orders[cid]
            quantity = order.quantity if filled_quantity is None else filled_quantity
            if not 0 < quantity <= order.quantity:
                raise ValueError("合成成交量必须在已注册原单数量内")
            self.exchange.entry_orders[cid] = replace(
                order,
                filled_quantity=quantity,
                status="FILLED" if quantity == order.quantity else "PARTIALLY_FILLED",
            )
            self.exchange.set_position(
                order.symbol, Side.LONG if order.side == "buy" else Side.SHORT, quantity
            )
            ex["filled_amount_quote"] = price * quantity

    def insufficient_balance(self, eid: str, planned_price: float) -> None:
        """hummingbot 核保证金不够：不下单直接结束，计划价照样写在 custom_info 里。"""
        self.executors[eid]["custom_info"]["current_position_average_price"] = planned_price
        self.terminate(eid, "INSUFFICIENT_BALANCE", leaves_position=True)

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
        self.executors[eid].update(
            status="TERMINATED", is_active=False, is_trading=False, close_type=close_type
        )
        if self.exchange is not None:
            cid = f"o-{eid}"
            order = self.exchange.entry_orders.get(cid)
            if order is not None and not order.terminal:
                self.exchange.entry_orders[cid] = replace(order, status="CANCELED")
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
            self.terminate(executor_id, "EARLY_STOP", leaves_position=keep_position)

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
        self.trades: list[TradeFill] = []
        self.listen_keys = 0
        self.wallet = 1000.0
        self.exit_price: float | None = None
        self.exit_fee: float | None = None
        self.positions_queue: list[list[ExchangePosition] | ExchangeError] = []
        """非空时 positions() 依次返回这些结果，用完才回到 self.pos（模拟读取抖动）。"""
        self.manual_fill: CloseFill | None = None
        """手动平仓后“最近一笔只减仓成交”；None = 查不到。"""
        self.mark: float | None = None
        self.leverage_set: list[tuple[str, int]] = []
        self.leverage_error: str | None = None
        self.filters: dict[str, LotFilter] = {"BTCUSDT": LotFilter(0.001, 0.001, 100.0)}
        self.filters_down = False
        self.income_down = False
        self.down = False
        self.mode: Literal["one_way", "hedge"] = "one_way"
        self.entry_orders: dict[str, ExchangeEntryOrder] = {}
        self.close_orders: dict[str, IdentifiedCloseOrder] = {}
        self.close_submissions: dict[str, tuple[str, str, float]] = {}
        self._n = 0

    def set_position(self, symbol: str, side: Side, qty: float) -> None:
        self.pos = [p for p in self.pos if p.symbol != symbol]
        if qty:
            self.pos.append(ExchangePosition(symbol, side, qty))

    async def positions(self) -> list[ExchangePosition]:
        if self.down:
            raise ExchangeError("币安不可达")
        if self.positions_queue:
            nxt = self.positions_queue.pop(0)
            if isinstance(nxt, ExchangeError):
                raise nxt
            return nxt
        return list(self.pos)

    async def positions_evidence(self) -> list[ExchangePosition]:
        """沿用显式假件队列/错误；只有合法合成持仓才构成净仓证据。"""
        rows = await self.positions()
        if not isinstance(rows, list) or any(
            not isinstance(p, ExchangePosition)
            or p.side not in (Side.LONG, Side.SHORT)
            or isinstance(p.quantity, bool)
            or not math.isfinite(p.quantity)
            or p.quantity <= 0
            for p in rows
        ):
            raise ExchangeError("合成持仓证据缺失或非法")
        return rows

    async def position_mode(self) -> Literal["one_way", "hedge"]:
        if self.down:
            raise ExchangeError("合成持仓模式查询不可用")
        return self.mode

    async def entry_order(self, symbol: str, client_order_id: str) -> ExchangeEntryOrder | None:
        if self.down:
            raise ExchangeError("合成开仓订单查询不可用")
        order = self.entry_orders.get(client_order_id)
        if order is not None and order.symbol != symbol:
            raise ExchangeError("合成开仓订单标的不符")
        return order  # 未注册 CID 就是未知，不合成 FILLED。

    async def close_order(self, symbol: str, client_order_id: str) -> IdentifiedCloseOrder | None:
        if self.down:
            raise ExchangeError("合成平仓订单查询不可用")
        order = self.close_orders.get(client_order_id)
        if order is not None and order.symbol != symbol:
            raise ExchangeError("合成平仓订单标的不符")
        return order

    async def place_close(
        self,
        symbol: str,
        close_side: Literal["buy", "sell"],
        quantity: float,
        client_order_id: str,
    ) -> IdentifiedCloseOrder:
        args = (symbol, close_side, quantity)
        if client_order_id in self.close_submissions:
            if self.close_submissions[client_order_id] != args:
                raise ExchangeError("合成平仓 CID 参数冲突")
            existing = await self.close_order(symbol, client_order_id)
            if existing is None:
                raise IdentifiedCloseUnknown(client_order_id)
            return existing
        self.close_submissions[client_order_id] = args
        expected_side = Side.LONG if close_side == "sell" else Side.SHORT
        before = sum(p.quantity for p in self.pos if p.symbol == symbol and p.side is expected_side)
        try:
            # 保留既有故障注入点；其明确改变的合成持仓差额作为测试成交证据。
            fill = await self.close_position_market(symbol, close_side, quantity)
        except ExchangeError as error:
            raise IdentifiedCloseUnknown(client_order_id) from error
        after = sum(p.quantity for p in self.pos if p.symbol == symbol and p.side is expected_side)
        filled = min(quantity, max(0.0, before - after))
        status = "FILLED" if filled == quantity else "PARTIALLY_FILLED" if filled > 0 else "NEW"
        order = IdentifiedCloseOrder(
            symbol=symbol,
            client_order_id=client_order_id,
            order_id=str(len(self.close_submissions)),
            close_side=close_side,
            quantity=quantity,
            filled_quantity=filled,
            remaining_quantity=quantity - filled,
            average_price=fill.price,
            status=status,
            position_side="BOTH",
            reduce_only=True,
            is_terminal=status == "FILLED",
        )
        self.close_orders[client_order_id] = order
        return order

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

    async def user_trades(self, symbol: str, since: datetime) -> list[TradeFill]:
        return [t for t in self.trades if t.time >= since]

    async def new_listen_key(self) -> str:
        self.listen_keys += 1
        return f"key-{self.listen_keys}"

    async def keepalive_listen_key(self) -> None:
        return None

    async def income_rows(self, since: datetime) -> list[IncomeRow]:
        if self.income_down:
            raise ExchangeError("收入流水不可用")
        return [IncomeRow(since, "REALIZED_PNL", self.income)] if self.income else []

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
