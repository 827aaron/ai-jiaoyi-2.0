"""币安 U 本位合约的直连客户端（ccxt）。

只做三件事：查真实持仓、挂 / 撤 / 查保命止损、查未平的条件单。它和 hummingbot-api 是两条
独立通道，对账员靠这条通道核对 hummingbot 说的话。开仓不走这里。

Protocol 在这里，ccxt 实现放在最后，测试用内存假件。
"""

from __future__ import annotations

import contextlib
import logging
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal, Protocol, runtime_checkable

from jiaoyi.contracts import Side
from jiaoyi.contracts.common import PERPETUAL_CONTRACT_TYPES
from jiaoyi.execution.fills import TradeFill
from jiaoyi.execution.symbols import from_ccxt_symbol, to_ccxt_symbol

log = logging.getLogger(__name__)

STOP_PREFIX = "lb-"
"""保命止损的 clientOrderId 前缀，对账员靠它认出“自己的”条件单。"""


class ExchangeError(Exception):
    """交易所调用失败（网络、鉴权、拒单）。调用方按故障即关闭处理。"""

    def __init__(
        self, message: str, *, immediate_trigger: bool = False, transient: bool = False
    ) -> None:
        super().__init__(message)
        self.immediate_trigger = immediate_trigger
        """币安 -2021：触发价已被越过，挂上就会立刻成交。意味着价格已经穿过止损位。"""
        self.transient = transient
        """网络层一时失败（超时、断连、交易所暂不可用），只读查询可以稍后重试。
        限流 / 封 IP（418、429）、时间戳（-1021）、鉴权（-2015）这类明确错误不算。"""


def is_transient(e: BaseException) -> bool:
    """ccxt 的网络层一时失败：超时、断连、交易所暂不可用。限流和时间戳错误不算。"""
    try:
        import ccxt
    except ImportError:  # pragma: no cover
        return False
    return isinstance(e, ccxt.NetworkError) and not isinstance(
        e, ccxt.DDoSProtection | ccxt.RateLimitExceeded | ccxt.InvalidNonce
    )


@dataclass(frozen=True)
class ExchangePosition:
    symbol: str
    side: Side
    quantity: float
    entry_price: float | None = None
    unrealized_pnl_usdt: float = 0.0


@dataclass(frozen=True)
class ExchangeStop:
    """币安上一张未触发的 STOP_MARKET 条件单。"""

    symbol: str
    order_id: str
    client_order_id: str
    stop_price: float
    reduce_only: bool


@dataclass(frozen=True)
class ExchangeEntryOrder:
    """原始开仓订单的只读身份与累计成交；不能从执行器 is_trading 推断终态。"""

    symbol: str
    client_order_id: str
    order_id: str
    side: Literal["buy", "sell"]
    quantity: float
    filled_quantity: float
    status: str
    position_side: str
    reduce_only: bool = False

    @property
    def terminal(self) -> bool:
        return self.status in {"FILLED", "CANCELED", "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED"}


def _evidence_number(value: object, *, zero: bool = False) -> float:
    if isinstance(value, bool):
        raise ValueError("条件单数值不能是布尔值")
    number = float(str(value))
    if not math.isfinite(number) or number < 0 or (not zero and number == 0):
        raise ValueError("条件单数值必须有限且为正")
    return number


def _evidence_bool(raw: dict[str, Any], key: str) -> bool:
    value = raw.get(key)
    if value is True or value == "true":
        return True
    if value is False or value == "false":
        return False
    raise ValueError(f"条件单缺少明确布尔字段：{key}")


@dataclass(frozen=True)
class IdentifiedCloseOrder:
    """按调用方已持久化CID核实的只减仓市价单；ACK不等于成交或仓位归零。"""

    symbol: str
    client_order_id: str
    order_id: str
    close_side: Literal["buy", "sell"]
    quantity: float
    filled_quantity: float
    remaining_quantity: float
    average_price: float | None
    status: str
    position_side: str
    reduce_only: bool
    is_terminal: bool | None


class IdentifiedCloseUnknown(ExchangeError):
    """可能已创建；查原CID，不得因后续暂时查不到就再次提交。"""

    def __init__(self, client_order_id: str, observed: IdentifiedCloseOrder | None = None) -> None:
        super().__init__("直连平仓结果未知；保留原CID等待查证，禁止盲目重发")
        self.client_order_id = client_order_id
        self.observed = observed


class IdentifiedCloseRejected(ExchangeError):
    """明确业务拒绝；本请求无有效订单，仍不能据此认定仓位已平。"""


def _close_decimal(value: object, *, zero: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("订单数值不能为布尔值")
    number = Decimal(str(value))
    if not number.is_finite() or number < 0 or (not zero and number == 0):
        raise ValueError("订单数值缺失或非法")
    if not math.isfinite(float(number)):
        raise ValueError("订单数值超范围")
    return number


def _close_bool(value: object) -> bool:
    if value is True or value == "true":
        return True
    if value is False or value == "false":
        return False
    raise ValueError("订单布尔字段未知")


def _identified_close(raw: dict[str, Any], symbol: str, cid: str) -> IdentifiedCloseOrder:
    if raw.get("symbol") != symbol or raw.get("clientOrderId") != cid:
        raise ValueError("直连平仓订单身份不符")
    if raw.get("type") != "MARKET" or raw.get("positionSide") != "BOTH":
        raise ValueError("直连平仓订单类型或持仓模式不符")
    if not _close_bool(raw.get("reduceOnly")):
        raise ValueError("直连平仓订单不是只减仓")
    side, status = str(raw.get("side") or "").lower(), str(raw.get("status") or "")
    if (
        side not in {"buy", "sell"}
        or not status
        or not re.fullmatch(r"[1-9]\d*", str(raw.get("orderId")))
    ):
        raise ValueError("直连平仓订单方向/状态/ID未知")
    qty = _close_decimal(raw.get("origQty"))
    filled = _close_decimal(raw.get("executedQty"), zero=True)
    if filled > qty:
        raise ValueError("直连平仓累计成交量超过原数量")
    if status == "FILLED" and filled != qty:
        raise ValueError("直连平仓完成状态与实际累计成交量不一致")
    average = raw.get("avgPrice")
    price = _close_decimal(average, zero=True) if average is not None else None
    terminal = {"FILLED", "CANCELED", "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED"}
    return IdentifiedCloseOrder(
        symbol=symbol,
        client_order_id=cid,
        order_id=str(raw["orderId"]),
        close_side="buy" if side == "buy" else "sell",
        quantity=float(qty),
        filled_quantity=float(filled),
        remaining_quantity=float(qty - filled),
        average_price=float(price) if price else None,
        status=status,
        position_side="BOTH",
        reduce_only=True,
        is_terminal=True
        if status in terminal
        else False
        if status in {"NEW", "PARTIALLY_FILLED", "PENDING_CANCEL"}
        else None,
    )


@dataclass(frozen=True)
class CloseFill:
    """直连平仓的成交回报：均价和手续费（USDT）。任一拿不到就是 None。"""

    price: float | None = None
    fee_usdt: float | None = None
    estimated: bool = False
    """价格不是真实成交价（例如手动平仓后取不到成交，用的标记价）。"""


INCOME_TYPES = frozenset({"REALIZED_PNL", "COMMISSION", "FUNDING_FEE"})


@dataclass(frozen=True)
class IncomeRow:
    """一条 U 本位收入流水。``kind`` 是币安的 incomeType，``income`` 用币安的正负号。"""

    time: datetime
    kind: str
    income: float


@dataclass(frozen=True)
class LotFilter:
    """币安 exchangeInfo 里一个合约的下单精度。"""

    step_size: float
    min_qty: float
    min_notional: float
    max_qty: float | None = None
    """市价单单笔最大数量（MARKET_LOT_SIZE.maxQty）；没有就是不限。"""
    tick_size: float | None = None
    """价格最小变动（PRICE_FILTER.tickSize）；没有就是不知道。"""
    onboard_at: datetime | None = None
    """上线时间（onboardDate）；没有就是不知道。"""
    delist_at: datetime | None = None
    """排了下架 / 交割的时间（deliveryDate；永续没排时是 2100 年，当没有）。"""


def _ms_time(raw: object) -> datetime | None:
    """exchangeInfo 的毫秒时间戳；没有、读不懂或 2099 年以后（永续的「不交割」）返回 None。"""
    try:
        ms = int(str(raw))
    except (TypeError, ValueError):
        return None
    if ms <= 0:
        return None
    t = datetime.fromtimestamp(ms / 1000, UTC)
    return None if t.year >= 2099 else t


@runtime_checkable
class ExchangeClient(Protocol):
    async def positions(self) -> list[ExchangePosition]: ...

    async def place_stop(
        self,
        symbol: str,
        close_side: Literal["buy", "sell"],
        quantity: float,
        stop_price: float,
        client_order_id: str,
    ) -> ExchangeStop:
        """挂只减仓 STOP_MARKET，触发价按标记价。同一 client_order_id 已存在时返回已有的那张。"""
        ...

    async def cancel_stop(self, symbol: str, order_id: str) -> None:
        """撤单。单子已经不在（已触发或已撤）不算错误。"""
        ...

    async def open_stops(self, symbol: str | None = None) -> list[ExchangeStop]: ...

    async def close_position_market(
        self, symbol: str, close_side: Literal["buy", "sell"], quantity: float
    ) -> CloseFill:
        """只减仓市价单。**兜底专用**：hummingbot 不可达、执行器停不掉或平完仓位还在时用它平仓。

        返回成交均价和手续费（拿不到的为 None），用来算盈亏记进当日统计。"""
        ...

    async def reduce_fill_since(
        self, symbol: str, close_side: Literal["buy", "sell"], since: datetime
    ) -> CloseFill | None:
        """``since`` 之后 ``close_side`` 方向的最近成交（数量加权均价 + USDT 手续费总和）。

        用来给“有人在币安手动平仓”估算平仓价；取不到返回 None，不抛错。"""
        ...

    async def mark_price(self, symbol: str) -> float | None:
        """标记价；取不到返回 None，不抛错。"""
        ...

    async def wallet_usdt(self) -> float:
        """合约账户 USDT 钱包余额（不含浮动盈亏）。"""
        ...

    async def ensure_leverage(self, symbol: str, leverage: int) -> None:
        """把该合约的杠杆设成 ``leverage`` 并回读核对；设不成或对不上抛 ExchangeError。"""
        ...

    async def lot_filters(self) -> dict[str, LotFilter]:
        """所有在交易的 USDT 永续合约的 stepSize / minQty / minNotional（读 exchangeInfo）。"""
        ...

    async def income_since(self, since: datetime) -> float:
        """从 ``since`` 起的已实现盈亏 + 手续费 + 资金费净额（USDT，亏损为负）。"""
        ...

    async def income_rows(self, since: datetime) -> list[IncomeRow]:
        """从 ``since`` 起的已实现盈亏、手续费、资金费流水（币安正负号），给按日汇总用。"""
        ...

    async def close(self) -> None: ...


class CcxtBinanceFutures:
    """ccxt 实现。``testnet=True`` 走币安的模拟环境：``demo=True``（默认）是 Demo Trading
    （demo-fapi.binance.com，币安已用它取代旧测试网），``demo=False`` 是旧的
    testnet.binancefuture.com（仅保留作回退）。

    币安 2025-12 起把条件单挪到 algo 订单接口，ccxt 4.5 的 ``trigger`` 参数会自动走新接口；
    升级 ccxt 要先在测试网验证挂单、查单、撤单三步（docs/M0-setup.md）。
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        *,
        testnet: bool,
        demo: bool = True,
        timeout_ms: int = 5000,
    ):
        import ccxt.async_support as ccxt

        self._ex: Any = ccxt.binanceusdm(
            {
                "apiKey": api_key,
                "secret": api_secret,
                "timeout": timeout_ms,
                "enableRateLimit": True,
                # 不带 symbol 查全部挂单是对账员的正常用法，别让 ccxt 为此抛警告异常
                "options": {
                    "defaultType": "future",
                    "fetchOpenOrders": {"warnWithoutSymbol": False},
                },
            }
        )
        if testnet:
            if demo:
                self._ex.enable_demo_trading(True)
            else:
                self._ex.set_sandbox_mode(True)
        self._markets_loaded = False

    async def _ready(self) -> None:
        if not self._markets_loaded:
            await self._ex.load_markets()
            self._markets_loaded = True

    @staticmethod
    def _stop_from(raw: dict[str, Any]) -> ExchangeStop:
        info = raw.get("info") or {}
        price = raw.get("stopPrice") or raw.get("triggerPrice") or info.get("triggerPrice")
        if not price:
            raise ExchangeError(f"条件单缺触发价：{raw.get('id')}")
        return ExchangeStop(
            symbol=from_ccxt_symbol(str(raw["symbol"])),
            order_id=str(raw["id"]),
            client_order_id=str(raw.get("clientOrderId") or ""),
            stop_price=float(price),
            reduce_only=bool(raw.get("reduceOnly")),
        )

    async def positions(self) -> list[ExchangePosition]:
        try:
            await self._ready()
            raw = await self._ex.fetch_positions()
        except Exception as e:
            raise ExchangeError(f"查持仓失败：{e}", transient=is_transient(e)) from e
        out: list[ExchangePosition] = []
        for p in raw:
            qty = abs(float(p.get("contracts") or 0.0))
            if qty == 0.0:
                continue
            side = Side.LONG if p.get("side") == "long" else Side.SHORT
            entry = p.get("entryPrice")
            out.append(
                ExchangePosition(
                    symbol=from_ccxt_symbol(str(p["symbol"])),
                    side=side,
                    quantity=qty,
                    entry_price=float(entry) if entry else None,
                    unrealized_pnl_usdt=float(p.get("unrealizedPnl") or 0.0),
                )
            )
        return out

    async def positions_evidence(self) -> list[ExchangePosition]:
        """平仓结算用严格证据。缺量/未知方向/模式不能伪装成零仓。"""
        try:
            if await self.position_mode() != "one_way":
                raise ValueError("只支持已确认的单向持仓")
            await self._ready()
            raw = await self._ex.fetch_positions()
            if not isinstance(raw, list):
                raise ValueError("持仓响应不是列表")
            out: list[ExchangePosition] = []
            for row in raw:
                if not isinstance(row, dict) or row.get("contracts") is None:
                    raise ValueError("持仓数量缺失")
                qty = _evidence_number(row["contracts"], zero=True)
                info = row.get("info")
                if not isinstance(info, dict) or info.get("positionSide") != "BOTH":
                    raise ValueError("持仓模式字段未知")
                amount = Decimal(str(info.get("positionAmt")))
                if not amount.is_finite() or abs(amount) != Decimal(str(qty)):
                    raise ValueError("原始持仓量与解析数量不一致")
                symbol = from_ccxt_symbol(str(row["symbol"]))
                if qty == 0:
                    continue
                if row.get("side") not in {"long", "short"}:
                    raise ValueError("非零仓位方向未知")
                if (amount > 0) != (row["side"] == "long"):
                    raise ValueError("原始持仓符号与解析方向不一致")
                entry = row.get("entryPrice")
                out.append(
                    ExchangePosition(
                        symbol=symbol,
                        side=Side(row["side"]),
                        quantity=qty,
                        entry_price=_evidence_number(entry) if entry is not None else None,
                        unrealized_pnl_usdt=0.0,
                    )
                )
            return out
        except ExchangeError:
            raise
        except Exception as error:
            raise ExchangeError(
                "真实持仓证据未知，不能确认空仓", transient=is_transient(error)
            ) from error

    async def entry_order(self, symbol: str, client_order_id: str) -> ExchangeEntryOrder | None:
        """按 HB 开仓客户端订单号查询，不将空响应/查单失败当已撤。"""
        import ccxt.async_support as ccxt

        try:
            raw = await self._ex.fapiPrivateGetOrder(
                {
                    "symbol": symbol,
                    "origClientOrderId": client_order_id,
                }
            )
        except ccxt.OrderNotFound:
            return None
        except Exception as error:
            raise ExchangeError("开仓订单查询结果未知", transient=is_transient(error)) from error
        try:
            if not isinstance(raw, dict):
                raise ValueError("响应不是订单")
            if raw.get("symbol") != symbol or raw.get("clientOrderId") != client_order_id:
                raise ValueError("开仓订单身份不匹配")
            status = raw.get("status")
            if status not in {
                "NEW",
                "PARTIALLY_FILLED",
                "FILLED",
                "CANCELED",
                "EXPIRED",
                "EXPIRED_IN_MATCH",
                "REJECTED",
            }:
                raise ValueError("订单状态未知")
            if raw.get("positionSide") != "BOTH" or raw.get("side") not in {"BUY", "SELL"}:
                raise ValueError("订单模式/方向未知")
            qty = _evidence_number(raw.get("origQty"))
            filled = _evidence_number(raw.get("executedQty"), zero=True)
            if (
                filled > qty
                or (status == "FILLED" and filled != qty)
                or not re.fullmatch(r"[1-9]\d*", str(raw.get("orderId")))
            ):
                raise ValueError("订单成交数量或订单身份不一致")
            return ExchangeEntryOrder(
                symbol=symbol,
                client_order_id=client_order_id,
                order_id=str(raw["orderId"]),
                side="buy" if raw["side"] == "BUY" else "sell",
                quantity=qty,
                filled_quantity=filled,
                status=status,
                position_side="BOTH",
                reduce_only=_evidence_bool(raw, "reduceOnly"),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ExchangeError("开仓订单证据不完整") from error

    async def place_stop(
        self,
        symbol: str,
        close_side: Literal["buy", "sell"],
        quantity: float,
        stop_price: float,
        client_order_id: str,
    ) -> ExchangeStop:
        try:
            await self._ready()
            for existing in await self.open_stops(symbol):
                if existing.client_order_id == client_order_id:
                    return existing
            ccxt_symbol = to_ccxt_symbol(symbol)
            raw = await self._ex.create_order(
                ccxt_symbol,
                "market",
                close_side,
                self._ex.amount_to_precision(ccxt_symbol, quantity),
                None,
                {
                    "stopLossPrice": float(self._ex.price_to_precision(ccxt_symbol, stop_price)),
                    "reduceOnly": True,
                    "workingType": "MARK_PRICE",
                    "newClientOrderId": client_order_id,
                },
            )
        except ExchangeError:
            raise
        except Exception as e:
            raise ExchangeError(f"挂保命止损失败：{e}", immediate_trigger="-2021" in str(e)) from e
        stop = self._stop_from(raw) if raw.get("stopPrice") or raw.get("triggerPrice") else None
        if stop is not None:
            return stop
        # 个别响应不回显触发价，再查一次，查不到就当失败
        for existing in await self.open_stops(symbol):
            if existing.client_order_id == client_order_id:
                return existing
        raise ExchangeError(f"挂单后查不到保命止损：{client_order_id}")

    async def cancel_stop(self, symbol: str, order_id: str) -> None:
        import ccxt.async_support as ccxt

        try:
            await self._ready()
            await self._ex.cancel_order(order_id, to_ccxt_symbol(symbol), {"trigger": True})
        except ccxt.OrderNotFound:
            return
        except Exception as e:
            raise ExchangeError(f"撤保命止损失败：{e}") from e

    async def open_stops(self, symbol: str | None = None) -> list[ExchangeStop]:
        try:
            await self._ready()
            ccxt_symbol = to_ccxt_symbol(symbol) if symbol else None
            raw = await self._ex.fetch_open_orders(ccxt_symbol, None, None, {"trigger": True})
        except Exception as e:
            raise ExchangeError(f"查条件单失败：{e}", transient=is_transient(e)) from e
        return [self._stop_from(o) for o in raw if self._is_stop(o)]

    @staticmethod
    def _is_stop(raw: dict[str, Any]) -> bool:
        """是不是 STOP_MARKET 条件单。algo 接口返回的单子 ccxt 解析出的 type 是 "market"，
        不能靠 type；以币安原始的 orderType 为准。"""
        info = raw.get("info") or {}
        order_type = str(info.get("orderType") or info.get("type") or raw.get("type") or "")
        return order_type.upper().startswith("STOP")

    async def position_mode(self) -> Literal["one_way", "hedge"]:
        """只读，不切换模式；未知响应不能当单向模式。"""
        try:
            raw = await self._ex.fapiPrivateGetPositionSideDual()
            hedged = _evidence_bool(raw, "dualSidePosition")
        except Exception as error:
            raise ExchangeError(
                "查询持仓模式失败或响应未知", transient=is_transient(error)
            ) from error
        return "hedge" if hedged else "one_way"

    @staticmethod
    def _close_cid(client_order_id: str) -> None:
        if not re.fullmatch(r"dc-[.A-Za-z0-9_:/-]{1,33}", client_order_id):
            raise ValueError("直连平仓CID必须使用dc-前缀且总长不超过36")

    async def close_order(self, symbol: str, client_order_id: str) -> IdentifiedCloseOrder | None:
        """GET原CID；明确未找到不证明之前未知提交未创建，不能因此重发。

        身份/类型/只减仓/数量缺失一律未知，不用状态或CCXT推导的filled补全。
        """
        import ccxt.async_support as ccxt

        self._close_cid(client_order_id)
        to_ccxt_symbol(symbol)  # 请求前验证U本位标的格式。
        try:
            raw = await self._ex.fapiPrivateGetOrder(
                {"symbol": symbol, "origClientOrderId": client_order_id}
            )
        except ccxt.OrderNotFound:
            return None
        except Exception as error:
            raise IdentifiedCloseUnknown(client_order_id) from error
        try:
            return _identified_close(raw, symbol, client_order_id)
        except Exception as error:
            raise IdentifiedCloseUnknown(client_order_id) from error

    async def _close_unknown(self, symbol: str, cid: str) -> IdentifiedCloseUnknown:
        observed = None
        with contextlib.suppress(ExchangeError):
            observed = await self.close_order(symbol, cid)
        return IdentifiedCloseUnknown(cid, observed)

    async def place_close(
        self,
        symbol: str,
        close_side: Literal["buy", "sell"],
        quantity: float,
        client_order_id: str,
    ) -> IdentifiedCloseOrder:
        """只提交一次的直连平仓，调用方必须先持久化CID和请求参数。

        仅支持只读确认的单向模式，不更改账户；5xx/网络/解析异常保持结果未知。
        终态部分成交也返回原CID证据，新CID/余仓由上层另作决定，不自动再下单。
        """
        import ccxt.async_support as ccxt

        self._close_cid(client_order_id)
        if close_side not in {"buy", "sell"}:
            raise ValueError("直连平仓方向非法")
        qty = _close_decimal(quantity)
        await self._ready()
        try:
            mode = await self._ex.fapiPrivateGetPositionSideDual()
            if _close_bool(mode.get("dualSidePosition")):
                raise ValueError("不支持双向持仓模式")
        except Exception as error:
            raise ExchangeError("未能确认单向持仓模式，禁止提交直连平仓") from error
        ccxt_symbol = to_ccxt_symbol(symbol)
        amount = self._ex.amount_to_precision(ccxt_symbol, float(qty))
        requested = _close_decimal(amount)
        existing = await self.close_order(symbol, client_order_id)

        def matches(order: IdentifiedCloseOrder) -> bool:
            return order.close_side == close_side and order.quantity == float(requested)

        if existing is not None:
            if not matches(existing):
                raise ExchangeError("原直连平仓CID对应不同方向或数量，禁止复用")
            return existing  # 包括终态部分成交；绝不自动创建下一代。
        try:
            response = await self._ex.create_order(
                ccxt_symbol,
                "market",
                close_side,
                amount,
                None,
                {
                    "newClientOrderId": client_order_id,
                    "reduceOnly": True,
                    "positionSide": "BOTH",
                    "newOrderRespType": "RESULT",
                },
            )
        except Exception as error:
            # 只承认CCXT明确的业务/参数/权限拒绝。通用ExchangeError/5xx均可能已创建。
            rejected = isinstance(
                error,
                ccxt.InvalidOrder
                | ccxt.AuthenticationError
                | ccxt.PermissionDenied
                | ccxt.InsufficientFunds
                | ccxt.BadSymbol,
            )
            uncertain_code = re.search(
                r"-(?:1000|1001|1006|1007|4116)\b|\b(?:500|502|503|504)\b|5xx|duplicate",
                str(error),
                re.IGNORECASE,
            )
            if rejected and not uncertain_code:
                raise IdentifiedCloseRejected("直连平仓被交易所明确拒绝") from error
            raise await self._close_unknown(symbol, client_order_id) from error
        try:
            order = _identified_close(response["info"], symbol, client_order_id)
            if not matches(order):
                raise ValueError("直连平仓响应参数不一致")
            return order
        except Exception as error:
            # ACK或不完整返回不等于未创建；只读查证一次，不重发。
            raise await self._close_unknown(symbol, client_order_id) from error

    async def wallet_usdt(self) -> float:
        try:
            await self._ready()
            bal = await self._ex.fetch_balance()
            info_assets = (bal.get("info") or {}).get("assets") or []
            for a in info_assets:  # 币安的 walletBalance 才是不含浮盈的钱包余额
                if a.get("asset") == "USDT" and a.get("walletBalance") is not None:
                    return float(a["walletBalance"])
            return float((bal.get("total") or {}).get("USDT") or 0.0)
        except Exception as e:
            raise ExchangeError(f"查钱包余额失败：{e}") from e

    async def ensure_leverage(self, symbol: str, leverage: int) -> None:
        try:
            await self._ready()
            ccxt_symbol = to_ccxt_symbol(symbol)
            await self._ex.set_leverage(leverage, ccxt_symbol)
            got = await self._ex.fetch_leverage(ccxt_symbol)
        except Exception as e:
            raise ExchangeError(f"设杠杆失败：{e}") from e
        raw = (
            got.get("longLeverage")
            or got.get("leverage")
            or (got.get("info") or {}).get("leverage")
        )
        try:
            actual = int(float(str(raw)))
        except (TypeError, ValueError) as e:
            raise ExchangeError(f"读不到 {symbol} 的杠杆：{got}") from e
        if actual != leverage:
            raise ExchangeError(f"{symbol} 杠杆 {actual}x，不是目标 {leverage}x")

    async def close_position_market(
        self, symbol: str, close_side: Literal["buy", "sell"], quantity: float
    ) -> CloseFill:
        try:
            await self._ready()
            ccxt_symbol = to_ccxt_symbol(symbol)
            order = await self._ex.create_order(
                ccxt_symbol,
                "market",
                close_side,
                self._ex.amount_to_precision(ccxt_symbol, quantity),
                None,
                {"reduceOnly": True, "newOrderRespType": "RESULT"},
            )
        except Exception as e:
            raise ExchangeError(f"直连平仓失败：{e}") from e
        return await self._fill_info(order, ccxt_symbol)

    async def _fill_info(self, order: dict[str, Any] | None, ccxt_symbol: str) -> CloseFill:
        """成交均价和手续费。下单响应里没有就再查这张单、再查它的成交明细；
        都拿不到的字段是 None（单已成交，不能因此报错）。"""
        price = (order or {}).get("average")
        order_id = (order or {}).get("id")
        fee = _fee_usdt((order or {}).get("fee"))
        if order_id and not price:
            try:
                price = (await self._ex.fetch_order(order_id, ccxt_symbol)).get("average")
            except Exception:
                log.warning("查直连平仓成交价失败 %s", order_id, exc_info=True)
        if order_id and fee is None:
            try:
                trades = await self._ex.fetch_my_trades(
                    ccxt_symbol, None, None, {"orderId": order_id}
                )
                costs = [_fee_usdt(t.get("fee")) for t in trades]
                if costs and all(c is not None for c in costs):
                    fee = sum(c for c in costs if c is not None)
            except Exception:
                log.warning("查直连平仓手续费失败 %s", order_id, exc_info=True)
        return CloseFill(float(price) if price else None, fee)

    async def reduce_fill_since(
        self, symbol: str, close_side: Literal["buy", "sell"], since: datetime
    ) -> CloseFill | None:
        try:
            await self._ready()
            trades = await self._ex.fetch_my_trades(
                to_ccxt_symbol(symbol), int(since.timestamp() * 1000), 100
            )
        except Exception:
            log.warning("查 %s 的最近成交失败", symbol, exc_info=True)
            return None
        mine = [t for t in trades if t.get("side") == close_side and t.get("amount")]
        qty = sum(float(t["amount"]) for t in mine)
        if qty <= 0:
            return None
        price = sum(float(t["amount"]) * float(t["price"]) for t in mine) / qty
        fees = [_fee_usdt(t.get("fee")) for t in mine]
        fee = sum(f for f in fees if f is not None) if all(f is not None for f in fees) else None
        return CloseFill(price, fee)

    async def mark_price(self, symbol: str) -> float | None:
        try:
            await self._ready()
            raw = await self._ex.fapiPublicGetPremiumIndex({"symbol": symbol})
            row = raw[0] if isinstance(raw, list) else raw
            return float(row["markPrice"])
        except Exception:
            log.warning("查 %s 的标记价失败", symbol, exc_info=True)
            return None

    async def lot_filters(self) -> dict[str, LotFilter]:
        try:
            markets = await self._ex.load_markets(True)
        except Exception as e:
            raise ExchangeError(f"读 exchangeInfo 失败：{e}") from e
        self._markets_loaded = True
        out: dict[str, LotFilter] = {}
        for m in markets.values():
            info = m.get("info") or {}
            if info.get("contractType") not in PERPETUAL_CONTRACT_TYPES:
                continue
            tradfi = (
                info.get("contractType") != "PERPETUAL"
            )  # 美股 / 金属永续：ccxt 不一定标成 swap
            if tradfi:
                if info.get("quoteAsset") != "USDT":
                    continue
            elif not (m.get("swap") and m.get("linear") and m.get("settle") == "USDT"):
                continue
            if info.get("status") != "TRADING":
                continue
            try:
                symbol = str(info["symbol"]) if tradfi else from_ccxt_symbol(str(m["symbol"]))
                filters = {f["filterType"]: f for f in info.get("filters", [])}
                lot = filters["LOT_SIZE"]
                notional = filters.get("MIN_NOTIONAL", {}).get("notional", 0.0)
                market_max = float(filters.get("MARKET_LOT_SIZE", {}).get("maxQty") or 0.0)
                tick = float(filters.get("PRICE_FILTER", {}).get("tickSize") or 0.0)
                out[symbol] = LotFilter(
                    float(lot["stepSize"]),
                    float(lot["minQty"]),
                    float(notional),
                    max_qty=market_max if market_max > 0 else None,
                    tick_size=tick if tick > 0 else None,
                    onboard_at=_ms_time(info.get("onboardDate")),
                    delist_at=_ms_time(info.get("deliveryDate")),
                )
            except (KeyError, ValueError):
                continue  # 缺字段的合约不给精度，风控员会对它拒单
        return out

    async def income_since(self, since: datetime) -> float:
        return sum(r.income for r in await self.income_rows(since))

    async def income_rows(self, since: datetime) -> list[IncomeRow]:
        """从 ``since`` 起的收入流水（只要已实现盈亏、手续费、资金费三种），按币安的正负号。"""
        out: list[IncomeRow] = []
        start_ms = int(since.timestamp() * 1000)
        try:
            while True:
                rows = await self._ex.fapiPrivateGetIncome({"startTime": start_ms, "limit": 1000})
                for row in rows:
                    if row.get("incomeType") in INCOME_TYPES:
                        out.append(
                            IncomeRow(
                                datetime.fromtimestamp(int(row["time"]) / 1000, UTC),
                                str(row["incomeType"]),
                                float(row["income"]),
                            )
                        )
                if len(rows) < 1000:
                    return out
                start_ms = max(int(r["time"]) for r in rows) + 1
        except Exception as e:
            raise ExchangeError(f"查收入流水失败：{e}") from e

    async def user_trades(self, symbol: str, since: datetime) -> list[TradeFill]:
        """``since`` 起这个币的成交明细（/fapi/v1/userTrades，最多 1000 条，账本回填够用）。"""
        try:
            rows = await self._ex.fapiPrivateGetUserTrades(
                {"symbol": symbol, "startTime": int(since.timestamp() * 1000), "limit": 1000}
            )
        except Exception as e:
            raise ExchangeError(f"查成交明细失败：{e}") from e
        out = []
        for r in rows:
            usdt = str(r.get("commissionAsset") or "").upper() in ("USDT", "USDC", "BUSD")
            out.append(
                TradeFill(
                    time=datetime.fromtimestamp(int(r["time"]) / 1000, UTC),
                    order_id=str(r.get("orderId")),
                    side=str(r["side"]).lower(),
                    price=float(r["price"]),
                    qty=float(r["qty"]),
                    commission_usdt=abs(float(r.get("commission") or 0.0)) if usdt else None,
                    realized_pnl=float(r.get("realizedPnl") or 0.0),
                )
            )
        return sorted(out, key=lambda t: t.time)

    async def new_listen_key(self) -> str:
        """开用户数据流（POST /fapi/v1/listenKey）。已有有效的会返回同一个并顺带续期。"""
        try:
            raw = await self._ex.fapiPrivatePostListenKey()
        except Exception as e:
            raise ExchangeError(f"申请用户数据流失败：{e}") from e
        return str(raw["listenKey"])

    async def keepalive_listen_key(self) -> None:
        """续期（PUT /fapi/v1/listenKey），60 分钟不续就失效。"""
        try:
            await self._ex.fapiPrivatePutListenKey()
        except Exception as e:
            raise ExchangeError(f"续期用户数据流失败：{e}") from e

    async def close(self) -> None:
        await self._ex.close()


def _fee_usdt(fee: dict[str, Any] | None) -> float | None:
    """ccxt 的 fee 字典 → USDT 数额；币种不是 USDT（如 BNB 抵扣）或没有就返回 None。"""
    if not fee or fee.get("cost") is None:
        return None
    if str(fee.get("currency") or "").upper() not in ("USDT", "USDC", "BUSD"):
        return None
    return abs(float(fee["cost"]))
