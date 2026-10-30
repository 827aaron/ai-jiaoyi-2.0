"""币安 U 本位合约的直连客户端（ccxt）。

只做三件事：查真实持仓、挂 / 撤 / 查保命止损、查未平的条件单。它和 hummingbot-api 是两条
独立通道，对账员靠这条通道核对 hummingbot 说的话。开仓不走这里。

Protocol 在这里，ccxt 实现放在最后，测试用内存假件。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from typing import Any, Literal, Protocol, runtime_checkable

from jiaoyi.contracts import Side
from jiaoyi.contracts.common import PERPETUAL_CONTRACT_TYPES
from jiaoyi.execution.fills import TradeFill
from jiaoyi.execution.symbols import from_ccxt_symbol, to_ccxt_symbol

log = logging.getLogger(__name__)

STOP_PREFIX = "lb-"
"""保命止损的 clientOrderId 前缀，对账员靠它认出“自己的”条件单。"""


_MODE_MISMATCH = re.compile(r'"code"\s*:\s*-4061\b')


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


BracketKind = Literal["stop_loss", "take_profit", "trailing_stop"]
WorkingType = Literal["CONTRACT_PRICE", "MARK_PRICE"]
"""条件单触发价格源：合约最新成交价 / 标记价。"""
WORKING_TYPES: tuple[WorkingType, ...] = ("CONTRACT_PRICE", "MARK_PRICE")
BRACKET_PREFIXES: dict[BracketKind, str] = {
    "stop_loss": "br-sl-",
    "take_profit": "br-tp-",
    "trailing_stop": "br-ts-",
}
_ALGO_TYPES: dict[str, BracketKind] = {
    "STOP_MARKET": "stop_loss",
    "TAKE_PROFIT_MARKET": "take_profit",
    "TRAILING_STOP_MARKET": "trailing_stop",
}
_PARENT_TYPES: dict[BracketKind, str] = {kind: t for t, kind in _ALGO_TYPES.items()}
CALLBACK_RATE_STEP = Decimal("0.1")
"""币安 TRAILING_STOP_MARKET 的 callbackRate 单位是百分比（1 = 1%），步长 0.1。"""
_CHILD_TERMINAL = frozenset({"FILLED", "CANCELED", "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED"})


@dataclass(frozen=True)
class NormalizedBracketSpec:
    """按交易所精度量化后的请求；调用方应在提交前持久化这两个值。"""

    quantity: float
    trigger_price: float


@dataclass(frozen=True)
class EntryBasis:
    """已终态开仓订单及其完整实际成交账；费用只接受原始 USDT commission。"""

    symbol: str
    client_order_id: str
    order_id: str
    side: Literal["buy", "sell"]
    filled_quantity: float
    filled_quote: float
    fee_usdt: float
    entry_price: float
    order_status: str
    first_fill_at: datetime | None = None
    last_fill_at: datetime | None = None


@dataclass(frozen=True)
class ExecutionBook:
    """本交易客户端所属环境的盘口；source_at 为交易所时间，不以到达时间替代。"""

    symbol: str
    bid: float
    ask: float
    source_at: datetime
    observed_at: datetime


@dataclass(frozen=True)
class MarkEvidence:
    """本交易客户端所属环境的标记价；source_at 为交易所时间（premiumIndex 的 time）。"""

    symbol: str
    mark: float
    source_at: datetime
    observed_at: datetime


@dataclass(frozen=True)
class ExchangeBracket:
    """独立普通保护单；algo 状态不是成交证据，未知 child 不能补成不存在。"""

    symbol: str
    algo_id: str
    client_order_id: str
    kind: BracketKind
    close_side: Literal["buy", "sell"]
    quantity: float
    trigger_price: float
    position_side: str
    working_type: str
    reduce_only: bool
    price_protect: bool
    close_position: bool
    algo_status: str
    actual_order_id: str | None = None
    actual_order_status: str | None = None
    filled_quantity: float | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    triggered_at: datetime | None = None
    child_state: Literal["absent", "unknown", "identified"] = "unknown"
    activate_price: float | None = None
    """交易所移动止损单（``trailing_stop``）的激活价；没设（下单即激活）为 None。此时
    ``trigger_price`` 为 0，不是触发价。"""
    callback_rate: float | None = None
    """交易所移动止损单的回撤比例（百分比，1 = 1%）；其它单为 None。"""


class BracketSubmissionUnknown(ExchangeError):
    """提交可能已接收；只查原 CID，不能因暂时查无而重新提交。"""

    def __init__(self, client_order_id: str, observed: ExchangeBracket | None = None) -> None:
        super().__init__("条件单提交结果未知；须核实原CID，不能自动二次创建")
        self.client_order_id = client_order_id
        self.observed = observed


class BracketSubmissionRejected(ExchangeError):
    """本次提交被明确拒绝；并不证明旧保护、HB 退出或仓位已经终止。"""


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
    update_time: datetime | None = None
    """Binance ``updateTime``: for a FILLED market order, the time of its last fill. Display
    only; None when absent or unreadable, never a reason to distrust the order."""


class IdentifiedCloseUnknown(ExchangeError):
    """可能已创建；查原CID，不得因后续暂时查不到就再次提交。"""

    def __init__(self, client_order_id: str, observed: IdentifiedCloseOrder | None = None) -> None:
        super().__init__("直连平仓结果未知；保留原CID等待查证，禁止盲目重发")
        self.client_order_id = client_order_id
        self.observed = observed


class IdentifiedCloseRejected(ExchangeError):
    """明确业务拒绝；本请求无有效订单，仍不能据此认定仓位已平。"""


class IdentifiedCloseNotSent(ExchangeError):
    """下单请求还没发出就失败（模式 / 市场 / 原 CID 预查出错）；这个 CID 从没到过交易所。"""


class IdentifiedCloseCancelledBeforeSend(asyncio.CancelledError):
    """下单请求发出之前任务被取消（多半是停机）：这个 CID 从没到过交易所，取消照常往上传。"""


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


def _bracket_symbol(symbol: str) -> str:
    ccxt_symbol = to_ccxt_symbol(symbol)
    if from_ccxt_symbol(ccxt_symbol) != symbol or any(c.isspace() for c in symbol):
        raise ValueError("普通保护单标的不是规范 U 本位代码")
    return ccxt_symbol


def _bracket_cid(client_order_id: str, kind: BracketKind | None = None) -> None:
    if not re.fullmatch(r"[.A-Za-z0-9_:/-]{1,36}", client_order_id):
        raise ValueError("普通保护单CID不合法")
    prefixes = (BRACKET_PREFIXES[kind],) if kind else tuple(BRACKET_PREFIXES.values())
    if not any(client_order_id.startswith(p) and len(client_order_id) > len(p) for p in prefixes):
        raise ValueError("普通保护单CID必须与角色前缀一致，不能复用保命止损CID")


def _bracket_id(value: object) -> str:
    if not re.fullmatch(r"[1-9]\d*", str(value)):
        raise ValueError("普通保护单订单ID缺失或非法")
    return str(value)


def _lifeboat_cid(client_order_id: str) -> None:
    if not re.fullmatch(r"lb-[.A-Za-z0-9_:/-]{1,33}", client_order_id):
        raise ValueError("保命单CID必须使用lb-前缀且总长不超过36")


def _bracket_number(value: object, *, zero: bool = False) -> float:
    number = _close_decimal(value, zero=zero)
    result = float(number)
    # 持久模型用 float；不能让两个不同的交易所数值被舍成同一个预期值。
    if Decimal(str(result)) != number:
        raise ValueError("普通保护单数值不能无损保存")
    return result


def _bracket_time(value: object) -> datetime | None:
    if value is None:
        return None
    millis = _close_decimal(value, zero=True)
    if millis != millis.to_integral_value():
        raise ValueError("普通保护单时间戳非法")
    return datetime.fromtimestamp(float(millis) / 1000, UTC) if millis else None


def _bracket_from(raw: dict[str, Any], *, lifeboat: bool = False) -> ExchangeBracket:
    if not isinstance(raw, dict):
        raise ValueError("普通保护单响应不是对象")
    order_type = raw.get("orderType") or raw.get("type")
    if order_type not in _ALGO_TYPES:
        raise ValueError("不是支持的条件市价保护单")
    kind = _ALGO_TYPES[order_type]
    side = raw.get("side")
    if side not in {"BUY", "SELL"}:
        raise ValueError("普通保护单方向未知")
    algo_id, cid = _bracket_id(raw.get("algoId")), str(raw.get("clientAlgoId") or "")
    if lifeboat:
        _lifeboat_cid(cid)
        if kind != "stop_loss":
            raise ValueError("保命单类型或CID不符")
    else:
        _bracket_cid(cid, kind)
    symbol = str(raw.get("symbol") or "")
    _bracket_symbol(symbol)
    status, position_side, working_type = (
        raw.get("algoStatus"),
        raw.get("positionSide"),
        raw.get("workingType"),
    )
    if (
        not isinstance(status, str)
        or not status
        or position_side not in {"BOTH", "LONG", "SHORT"}
        or working_type not in {"CONTRACT_PRICE", "MARK_PRICE"}
    ):
        raise ValueError("普通保护单状态、模式或触发价格源未知")
    triggered_at = _bracket_time(raw.get("triggerTime"))
    activate: float | None = None
    rate: float | None = None
    if kind == "trailing_stop":
        # 激活价没设时交易所返回空串；回撤比例 algo 接口叫 callbackRate，旧接口叫 priceRate
        activate = _optional_number(raw.get("activatePrice"))
        rate = _optional_number(raw.get("callbackRate", raw.get("priceRate")))
        if rate is None:
            raise ValueError("交易所移动止损单缺回撤比例")
        trigger = activate or 0.0
    else:
        trigger = _bracket_number(raw.get("triggerPrice"))
    actual = raw.get("actualOrderId")
    actual_id = None
    child_state: Literal["absent", "unknown", "identified"] = "unknown"
    if actual is not None:
        if actual in (0, "0", "") and not isinstance(actual, bool):
            # Binance documents an empty actualOrderId before triggering; also accept legacy 0.
            # Require an untriggered status and explicit zero clock, never missing/null evidence.
            if (
                status in {"NEW", "CANCELED", "REJECTED", "EXPIRED"}
                and raw.get("triggerTime") in (0, "0")
                and not isinstance(raw.get("triggerTime"), bool)
            ):
                child_state = "absent"
        else:
            actual_id = _bracket_id(actual)
            child_state = "identified"
    return ExchangeBracket(
        symbol=symbol,
        algo_id=algo_id,
        client_order_id=cid,
        kind=kind,
        close_side="buy" if side == "BUY" else "sell",
        quantity=_bracket_number(raw.get("quantity")),
        trigger_price=trigger,
        position_side=str(position_side),
        working_type=str(working_type),
        reduce_only=_close_bool(raw.get("reduceOnly")),
        price_protect=_close_bool(raw.get("priceProtect")),
        close_position=_close_bool(raw.get("closePosition")),
        algo_status=status,
        actual_order_id=actual_id,
        # Algo ACK 中的 executedQty 不用作实际 child 成交证据。
        created_at=_bracket_time(raw.get("createTime")),
        updated_at=_bracket_time(raw.get("updateTime")),
        triggered_at=triggered_at,
        child_state=child_state,
        activate_price=activate,
        callback_rate=rate,
    )


def _optional_number(value: object) -> float | None:
    """可选的正数字段：缺、null、空串、0 都是 None；有值就必须能无损保存。"""
    if value is None or (value in ("", 0, "0") and not isinstance(value, bool)):
        return None
    return _bracket_number(value)


def callback_rate(distance_pct: float) -> Decimal:
    """回撤距离（百分比）按交易所步长 0.1 向下取整：只会比程序的回撤距离紧，不会松。
    不到 0.1 的得 0，范围由调用方核。"""
    return Decimal(str(distance_pct)).quantize(CALLBACK_RATE_STEP, rounding=ROUND_DOWN)


def _bracket_child(order: ExchangeBracket, raw: dict[str, Any]) -> ExchangeBracket:
    if not isinstance(raw, dict) or (
        raw.get("symbol") != order.symbol
        or _bracket_id(raw.get("orderId")) != order.actual_order_id
        or raw.get("side") != order.close_side.upper()
        or raw.get("positionSide") != "BOTH"
        or not _close_bool(raw.get("reduceOnly"))
    ):
        raise ValueError("普通保护单实际子订单身份或只减仓语义不符")
    parent_type = _PARENT_TYPES[order.kind]
    if raw.get("type") not in {"MARKET", parent_type}:
        raise ValueError("普通保护单实际子订单类型不符")
    if raw.get("origType") is not None and raw["origType"] not in {"MARKET", parent_type}:
        raise ValueError("普通保护单实际子订单原类型不符")
    # child CID 可由交易所生成，不假定等于 clientAlgoId；关联以已核实的 actualOrderId 为准。
    if not isinstance(raw.get("clientOrderId"), str) or not re.fullmatch(
        r"[.A-Za-z0-9_:/-]{1,36}", raw["clientOrderId"]
    ):
        raise ValueError("普通保护单实际子订单CID未知")
    qty = _bracket_number(raw.get("origQty"))
    filled = _bracket_number(raw.get("executedQty"), zero=True)
    status = raw.get("status")
    if status not in _CHILD_TERMINAL | {"NEW", "PARTIALLY_FILLED", "PENDING_CANCEL"}:
        raise ValueError("普通保护单实际子订单状态未知")
    if (
        qty != order.quantity
        or filled > qty
        or (status == "FILLED" and filled != qty)
        or (status == "NEW" and filled != 0)
        or (status == "PARTIALLY_FILLED" and not 0 < filled < qty)
    ):
        raise ValueError("普通保护单实际子订单数量与状态不一致")
    return replace(order, actual_order_status=str(status), filled_quantity=filled)


_TRIGGER_FAILED = frozenset({"REJECTED", "EXPIRED"})


def _trigger_failed(raw: dict[str, Any], order: ExchangeBracket) -> bool:
    """触发了但交易所没下出子单：终态 REJECTED/EXPIRED、明确的空 actualOrderId、非零 triggerTime。

    缺字段、null、CANCELED（撤单可能与触发竞速）都不算；调用方还须按 CID 复核没有子单。
    """
    actual, trigger = raw.get("actualOrderId"), raw.get("triggerTime")
    return (
        order.child_state == "unknown"
        and order.actual_order_id is None
        and order.algo_status in _TRIGGER_FAILED
        and order.triggered_at is not None
        and actual in ("", "0", 0)
        and not isinstance(actual, bool)
        and not isinstance(trigger, bool)
    )


def _bracket_settled(order: ExchangeBracket) -> bool:
    """仅说明这张 algo 及其已知 child 不再工作，不证明账户已平仓。"""
    if order.child_state == "identified":
        return (
            order.algo_status in {"CANCELED", "REJECTED", "EXPIRED", "TRIGGERED", "FINISHED"}
            and order.actual_order_status in _CHILD_TERMINAL
            and order.filled_quantity is not None
        )
    return order.child_state == "absent" and order.algo_status in {
        "CANCELED",
        "REJECTED",
        "EXPIRED",
    }


def _entry_basis_order(
    raw: dict[str, Any], symbol: str, cid: str
) -> tuple[str, Literal["buy", "sell"], Decimal, Decimal, str]:
    if not isinstance(raw, dict) or raw.get("symbol") != symbol or raw.get("clientOrderId") != cid:
        raise ValueError("开仓成本订单身份不符")
    if (
        raw.get("side") not in {"BUY", "SELL"}
        or raw.get("type") not in {"LIMIT", "MARKET"}
        or raw.get("positionSide") != "BOTH"
        or _close_bool(raw.get("reduceOnly"))
    ):
        raise ValueError("开仓成本订单类型或方向不符")
    status = raw.get("status")
    if status not in _CHILD_TERMINAL:
        raise ValueError("开仓尚未终态，不能冻结成本基准")
    qty, filled = _close_decimal(raw.get("origQty")), _close_decimal(raw.get("executedQty"))
    quote = _close_decimal(raw.get("cumQuote"))
    if filled > qty or (status == "FILLED" and filled != qty):
        raise ValueError("开仓累计成交量与状态不一致")
    return (
        _bracket_id(raw.get("orderId")),
        "buy" if raw["side"] == "BUY" else "sell",
        filled,
        quote,
        str(status),
    )


def _entry_basis_fills(
    rows: list[dict[str, Any]],
    symbol: str,
    order_id: str,
    side: Literal["buy", "sell"],
    filled: Decimal,
    quote: Decimal,
) -> Decimal:
    if not isinstance(rows, list) or not rows or len(rows) >= 1000:
        raise ValueError("开仓成交记录缺失或达到分页边界，完整性未知")
    total_qty, total_quote, total_fees = Decimal("0"), Decimal("0"), Decimal("0")
    ids: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or (
            row.get("symbol") != symbol
            or _bracket_id(row.get("orderId")) != order_id
            or row.get("side") != side.upper()
            or row.get("positionSide") != "BOTH"
            or row.get("commissionAsset") != "USDT"
            or _close_bool(row.get("buyer")) != (side == "buy")
        ):
            raise ValueError("开仓成交身份、方向或手续费币种不符")
        trade_id = str(row.get("id"))
        if not re.fullmatch(r"(?:0|[1-9][0-9]*)", trade_id):
            raise ValueError("开仓成交ID非法")
        if trade_id in ids:
            raise ValueError("开仓成交ID重复，不能累计费用")
        ids.add(trade_id)
        qty, value = _close_decimal(row.get("qty")), _close_decimal(row.get("quoteQty"))
        _close_decimal(row.get("price"))
        if isinstance(row.get("commission"), bool):
            raise ValueError("开仓手续费字段非法")
        fee = Decimal(str(row.get("commission")))
        realized = Decimal(str(row.get("realizedPnl")))
        if not fee.is_finite() or not math.isfinite(float(fee)) or realized != 0:
            raise ValueError("开仓手续费未知或成交包含平仓盈亏")
        total_qty += qty
        total_quote += value
        total_fees += fee
    if total_qty != filled or total_quote != quote:
        raise ValueError("开仓实际成交量/金额未完整覆盖订单累计量，不能估计费用")
    return total_fees


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
        update_time=_close_update_time(raw.get("updateTime")),
    )


def _close_update_time(value: object) -> datetime | None:
    try:
        return _bracket_time(value)
    except (ValueError, ArithmeticError, OverflowError, OSError):
        return None


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
        position_mode_cache_s: float = 0.0,
    ):
        import ccxt.async_support as ccxt

        def client() -> Any:
            ex = ccxt.binanceusdm(
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
                    ex.enable_demo_trading(True)
                else:
                    ex.set_sandbox_mode(True)
            return ex

        self._ex: Any = client()
        # The close lane: a second client with its own leaky bucket, used only by the direct
        # close (its position read, mode check, CID read and order). Every other call shares the
        # first bucket (20 weight/s), which the protection tick alone keeps full with a few
        # positions open, so a close queued there waited seconds per request before it was sent.
        # Closes are rare and light, well inside Binance's own per-account limit.
        self._close_ex: Any = client()
        self._close_markets: object = None
        self._markets_loaded = False
        self.untradeable_status: dict[str, str] = {}
        self._bracket_submit_locks: dict[str, asyncio.Lock] = {}
        self._bracket_submitted: set[str] = set()
        self.position_mode_cache_s = position_mode_cache_s
        for lane in (self._ex, self._close_ex):
            self._watch_mode_errors(lane)

    def _watch_mode_errors(self, lane: Any) -> None:
        inner = lane.handle_errors

        def handle_errors(code: Any, reason: Any, url: Any, method: Any, *rest: Any) -> Any:
            # Every HTTP response passes here, so a -4061 from any call site drops the cache.
            if rest and _MODE_MISMATCH.search(str(rest[1])):
                self.forget_position_mode()
            return inner(code, reason, url, method, *rest)

        lane.handle_errors = handle_errors

    def forget_position_mode(self) -> None:
        self._position_mode_cached: tuple[float, Literal["one_way", "hedge"]] | None = None

    def _close_lane(self) -> Any:
        return self.__dict__.get("_close_ex") or self._ex

    async def _close_ready(self) -> Any:
        """The close lane with the main client's markets: copied, not loaded a second time.

        Copied again whenever the main client holds a different markets object (``lot_filters``
        reloads them), else a symbol listed since the last copy fails the close lane's parse."""
        await self._ready()
        lane = self._close_lane()
        markets = self._ex.markets
        if lane is not self._ex and self.__dict__.get("_close_markets") is not markets:
            lane.set_markets(markets, self._ex.currencies)
            self._close_markets = markets
        if lane is not self._ex:
            # ccxt's position read loads leverage brackets first; share the main client's so the
            # close lane does not spend an extra request on them.
            options = getattr(self._ex, "options", None)
            brackets = options.get("leverageBrackets") if isinstance(options, dict) else None
            if brackets is not None and isinstance(lane.options, dict):
                lane.options["leverageBrackets"] = brackets
        return lane

    async def _close_position_mode(self, lane: Any) -> Literal["one_way", "hedge"]:
        """Position mode for a close: the cached answer when ``position_mode_cache_s`` allows
        one (it cannot change while the position exists), else a fresh read on the close lane."""
        ttl = getattr(self, "position_mode_cache_s", 0.0)
        hit = self._cached_position_mode(ttl) if ttl > 0 else None
        if hit is not None:
            return hit
        clock: Callable[[], float] = getattr(self, "_mode_clock", time.monotonic)
        raw = await lane.fapiPrivateGetPositionSideDual()
        mode: Literal["one_way", "hedge"] = (
            "hedge" if _close_bool((raw or {}).get("dualSidePosition")) else "one_way"
        )
        if ttl > 0:
            self._position_mode_cached = (clock(), mode)
        return mode

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
        return await self._positions_evidence(urgent=False)

    async def close_positions_evidence(self) -> list[ExchangePosition]:
        """The same evidence read on the close lane, for sizing a close order."""
        return await self._positions_evidence(urgent=True)

    async def _positions_evidence(self, *, urgent: bool) -> list[ExchangePosition]:
        try:
            if urgent:
                lane = await self._close_ready()
                mode = await self._close_position_mode(lane)
            else:
                mode = await self.position_mode()
                await self._ready()
                lane = self._ex
            if mode != "one_way":
                raise ValueError("只支持已确认的单向持仓")
            raw = await lane.fetch_positions()
            if not isinstance(raw, list):
                raise ValueError("持仓响应不是列表")
            out: list[ExchangePosition] = []
            for row in raw:
                if not isinstance(row, dict) or row.get("contracts") is None:
                    raise ValueError("持仓数量缺失")
                qty = _evidence_number(row["contracts"], zero=True)
                info = row.get("info")
                if not isinstance(info, dict) or info.get("positionSide") != "BOTH":
                    self.forget_position_mode()
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

    async def position_mode(self, *, fresh: bool = False) -> Literal["one_way", "hedge"]:
        """只读，不切换模式；未知响应不能当单向模式。

        ``position_mode_cache_s`` > 0 serves a successful read again for that many seconds
        (0 = every call asks Binance). The request weighs 30 of the client's 20-per-second
        rate budget, and Binance refuses to switch modes while a position or open order
        exists, so within a holding period the answer cannot change. ``fresh`` (preflight
        before opening) and any error, including a -4061 or a non-BOTH position, bypass or
        drop the cache."""
        ttl = getattr(self, "position_mode_cache_s", 0.0)
        if fresh or ttl <= 0:
            return await self._read_position_mode(ttl)
        hit = self._cached_position_mode(ttl)
        if hit is not None:
            return hit
        # Cold cache: callers arriving together share one request instead of each paying 30.
        lock = self.__dict__.setdefault("_position_mode_lock", asyncio.Lock())
        async with lock:
            hit = self._cached_position_mode(ttl)
            if hit is not None:
                return hit
            return await self._read_position_mode(ttl)

    def _cached_position_mode(self, ttl: float) -> Literal["one_way", "hedge"] | None:
        cached = getattr(self, "_position_mode_cached", None)
        clock: Callable[[], float] = getattr(self, "_mode_clock", time.monotonic)
        if cached is not None and clock() - cached[0] < ttl:
            mode: Literal["one_way", "hedge"] = cached[1]
            return mode
        return None

    async def _read_position_mode(self, ttl: float) -> Literal["one_way", "hedge"]:
        clock: Callable[[], float] = getattr(self, "_mode_clock", time.monotonic)
        self.forget_position_mode()
        try:
            raw = await self._ex.fapiPrivateGetPositionSideDual()
            hedged = _evidence_bool(raw, "dualSidePosition")
        except Exception as error:
            raise ExchangeError(
                "查询持仓模式失败或响应未知", transient=is_transient(error)
            ) from error
        mode: Literal["one_way", "hedge"] = "hedge" if hedged else "one_way"
        if ttl > 0:
            self._position_mode_cached = (clock(), mode)
        return mode

    async def normalized_bracket_spec(
        self, symbol: str, quantity: float, trigger_price: float
    ) -> NormalizedBracketSpec:
        """复用 CCXT 精度规则；零/非法/无法无损持久化的结果拒绝，不更改账户。

        这是请求量化，不表示实际成交量、当前仓位量或交易所已接受保护。
        """
        ccxt_symbol = _bracket_symbol(symbol)
        qty, price = _bracket_number(quantity), _bracket_number(trigger_price)
        await self._ready()
        amount = _bracket_number(self._ex.amount_to_precision(ccxt_symbol, qty))
        trigger = _bracket_number(self._ex.price_to_precision(ccxt_symbol, price))
        if amount > qty:
            raise ValueError("普通保护单量化后数量不能超过请求数量")
        return NormalizedBracketSpec(amount, trigger)

    async def _bracket_uncertain(
        self, symbol: str, cid: str, *, lifeboat: bool = False
    ) -> BracketSubmissionUnknown:
        observed = None
        with contextlib.suppress(ExchangeError):
            observed = (
                await self.get_lifeboat(symbol, client_order_id=cid)
                if lifeboat
                else await self.get_bracket(symbol, client_order_id=cid)
            )
        return BracketSubmissionUnknown(cid, observed)

    async def place_bracket(
        self,
        symbol: str,
        close_side: Literal["buy", "sell"],
        quantity: float,
        trigger_price: float,
        client_order_id: str,
        *,
        kind: BracketKind,
        working_type: WorkingType = "CONTRACT_PRICE",
    ) -> ExchangeBracket:
        """只单向、只减仓的普通 SL/TP；触发价格源按调用方持久化的值，默认合约最新成交价。

        调用方必须先持久化 CID/量化参数，并在进程重启后只查已提交或未知的 CID。
        本进程另加一次提交锁与记录，防并发调用或查无后重复 POST；不是持久化的替代品。
        不自动撤其他订单，不市价补偿，不更改持仓模式。
        """
        return await self._submit_protection(
            symbol,
            close_side,
            quantity,
            trigger_price,
            client_order_id,
            kind=kind,
            working_type=working_type,
        )

    async def place_lifeboat(
        self,
        symbol: str,
        close_side: Literal["buy", "sell"],
        quantity: float,
        trigger_price: float,
        client_order_id: str,
    ) -> ExchangeBracket:
        """新托管流程专用：唯一代 lb-CID、MARK_PRICE、完整回读、未知不重发。

        调用方先持久化量化参数和每代 CID；跨重启只查既有意图。旧 place_stop 路径不变。
        """
        return await self._submit_protection(
            symbol,
            close_side,
            quantity,
            trigger_price,
            client_order_id,
            kind="stop_loss",
            lifeboat=True,
            working_type="MARK_PRICE",
        )

    async def place_trailing(
        self,
        symbol: str,
        close_side: Literal["buy", "sell"],
        quantity: float,
        callback_rate: float,
        client_order_id: str,
        *,
        activate_price: float | None,
        working_type: WorkingType,
    ) -> ExchangeBracket:
        """交易所移动止损 TRAILING_STOP_MARKET（algo 接口，单向、只减仓、br-ts- CID）。

        ``activate_price`` 为 None 时不带激活价，交易所按下单时的最新价（随 ``working_type``）
        当场激活。和 ``place_bracket`` 同一套：先查原 CID、提交锁防重发、未知不重发。
        """
        _bracket_cid(client_order_id, "trailing_stop")
        if close_side not in {"buy", "sell"} or working_type not in WORKING_TYPES:
            raise ValueError("交易所移动止损方向或触发价格源不合法")
        _bracket_symbol(symbol)
        _bracket_number(quantity)
        rate = _bracket_number(callback_rate)
        if Decimal(str(rate)) % CALLBACK_RATE_STEP != 0:
            raise ValueError("交易所移动止损回撤比例不是 0.1 的整数倍")
        if activate_price is not None:
            _bracket_number(activate_price)
        if not hasattr(self, "_bracket_submit_locks"):
            self._bracket_submit_locks = {}
            self._bracket_submitted = set()
        async with self._bracket_submit_locks.setdefault(client_order_id, asyncio.Lock()):
            return await self._place_trailing_once(
                symbol,
                close_side,
                quantity,
                rate,
                client_order_id,
                activate_price=activate_price,
                working_type=working_type,
            )

    async def _place_trailing_once(
        self,
        symbol: str,
        close_side: Literal["buy", "sell"],
        quantity: float,
        rate: float,
        client_order_id: str,
        *,
        activate_price: float | None,
        working_type: WorkingType,
    ) -> ExchangeBracket:
        await self._ready()
        ccxt_symbol = _bracket_symbol(symbol)
        amount = self._ex.amount_to_precision(ccxt_symbol, quantity)
        if _bracket_number(amount) != quantity:
            raise ValueError("交易所移动止损数量须先按交易所精度量化")
        activate = None
        if activate_price is not None:
            activate = self._ex.price_to_precision(ccxt_symbol, activate_price)
            if _bracket_number(activate) != activate_price:
                raise ValueError("交易所移动止损激活价须先按交易所精度量化")
        if await self.position_mode() != "one_way":
            raise ExchangeError("交易所移动止损只支持已确认的单向持仓模式")

        def matches(order: ExchangeBracket) -> bool:
            return (
                order.symbol == symbol
                and order.client_order_id == client_order_id
                and order.kind == "trailing_stop"
                and order.close_side == close_side
                and order.quantity == quantity
                and order.callback_rate == rate
                and (activate_price is None or order.activate_price == activate_price)
                and order.position_side == "BOTH"
                and order.working_type == working_type
                and order.reduce_only
                and not order.close_position
            )

        async def query() -> ExchangeBracket | None:
            return await self.get_bracket(symbol, client_order_id=client_order_id)

        existing = await query()
        if existing is not None:
            self._bracket_submitted.add(client_order_id)
            if not matches(existing):
                raise ExchangeError("原CID交易所移动止损与本次请求不一致，禁止复用")
            return existing
        if client_order_id in self._bracket_submitted:
            raise BracketSubmissionUnknown(client_order_id)
        params: dict[str, Any] = {
            "trailingPercent": str(Decimal(str(rate)).quantize(CALLBACK_RATE_STEP)),
            "reduceOnly": True,
            "workingType": working_type,
            "positionSide": "BOTH",
            "newClientOrderId": client_order_id,
        }
        if activate is not None:
            params["trailingTriggerPrice"] = float(activate)
        self._bracket_submitted.add(client_order_id)
        try:
            result = await self._ex.create_order(
                ccxt_symbol, "market", close_side, amount, None, params
            )
        except Exception as error:
            raise await self._submit_failure(symbol, client_order_id, error) from error
        try:
            order = _bracket_from(result["info"])
            if not matches(order):
                raise ValueError("响应身份或移动止损参数不符")
            if order.child_state != "absent" or order.algo_status != "NEW":
                observed = await query()
                if observed is None or observed.algo_id != order.algo_id or not matches(observed):
                    raise ValueError("交易所移动止损提交回读身份或参数不一致")
                return observed
            return order
        except Exception as error:
            raise await self._bracket_uncertain(symbol, client_order_id) from error

    async def _submit_failure(
        self, symbol: str, cid: str, error: Exception, *, lifeboat: bool = False
    ) -> ExchangeError:
        """下单请求抛错：明确业务拒绝 → Rejected（-2021/-4142 = 会立刻触发）；其余都按未知。"""
        import ccxt.async_support as ccxt

        rejected = isinstance(
            error,
            ccxt.InvalidOrder
            | ccxt.AuthenticationError
            | ccxt.PermissionDenied
            | ccxt.InsufficientFunds
            | ccxt.BadSymbol,
        )
        uncertain_code = re.search(
            r"-(?:1000|1001|1006|1007|4111|4116)\b|\b(?:500|502|503|504)\b|5xx|duplicate",
            str(error),
            re.IGNORECASE,
        )
        if rejected and not uncertain_code:
            return BracketSubmissionRejected(
                "普通保护条件单被交易所明确拒绝",
                immediate_trigger=bool(re.search(r"-(?:2021|4142)\b", str(error))),
            )
        return await self._bracket_uncertain(symbol, cid, lifeboat=lifeboat)

    async def _submit_protection(
        self,
        symbol: str,
        close_side: Literal["buy", "sell"],
        quantity: float,
        trigger_price: float,
        client_order_id: str,
        *,
        kind: BracketKind,
        lifeboat: bool = False,
        working_type: WorkingType = "CONTRACT_PRICE",
    ) -> ExchangeBracket:
        if kind not in BRACKET_PREFIXES or close_side not in {"buy", "sell"}:
            raise ValueError("普通保护单类型或方向不合法")
        if working_type not in WORKING_TYPES:
            raise ValueError("条件单触发价格源不合法")
        if lifeboat:
            _lifeboat_cid(client_order_id)
            if kind != "stop_loss" or working_type != "MARK_PRICE":
                raise ValueError("保命单只能是按标记价触发的止损")
        else:
            _bracket_cid(client_order_id, kind)
        _bracket_symbol(symbol)
        _bracket_number(quantity)
        _bracket_number(trigger_price)
        # __new__ 构造的离线假件也走同一提交锁；生产实例在 __init__ 建立。
        if not hasattr(self, "_bracket_submit_locks"):
            self._bracket_submit_locks = {}
            self._bracket_submitted = set()
        key = client_order_id  # CID 属于账户命名空间，不因换 symbol 而允许重复 POST。
        async with self._bracket_submit_locks.setdefault(key, asyncio.Lock()):
            return await self._place_bracket_once(
                symbol,
                close_side,
                quantity,
                trigger_price,
                client_order_id,
                kind=kind,
                lifeboat=lifeboat,
                working_type=working_type,
            )

    async def _place_bracket_once(
        self,
        symbol: str,
        close_side: Literal["buy", "sell"],
        quantity: float,
        trigger_price: float,
        client_order_id: str,
        *,
        kind: BracketKind,
        lifeboat: bool,
        working_type: WorkingType,
    ) -> ExchangeBracket:

        spec = await self.normalized_bracket_spec(symbol, quantity, trigger_price)
        if await self.position_mode() != "one_way":
            raise ExchangeError("普通保护条件单只支持已确认的单向持仓模式")

        async def query() -> ExchangeBracket | None:
            return (
                await self.get_lifeboat(symbol, client_order_id=client_order_id)
                if lifeboat
                else await self.get_bracket(symbol, client_order_id=client_order_id)
            )

        def matches(order: ExchangeBracket) -> bool:
            return (
                order.symbol == symbol
                and order.client_order_id == client_order_id
                and order.kind == kind
                and order.close_side == close_side
                and order.quantity == spec.quantity
                and order.trigger_price == spec.trigger_price
                and order.position_side == "BOTH"
                and order.working_type == working_type
                and order.reduce_only
                and not order.price_protect
                and not order.close_position
            )

        existing = await query()
        key = client_order_id
        if existing is not None:
            self._bracket_submitted.add(key)
            if not matches(existing):
                raise ExchangeError("原CID条件单与本次请求不一致，禁止复用")
            return existing  # 包含终态；同 CID 从不隐式建下一张。
        if key in self._bracket_submitted:
            raise BracketSubmissionUnknown(client_order_id)
        ccxt_symbol = _bracket_symbol(symbol)
        amount = self._ex.amount_to_precision(ccxt_symbol, spec.quantity)
        price = self._ex.price_to_precision(ccxt_symbol, spec.trigger_price)
        if _bracket_number(amount) != spec.quantity or _bracket_number(price) != spec.trigger_price:
            raise ExchangeError("普通保护单精度在提交前变化，禁止提交不同于持久目标的请求")
        self._bracket_submitted.add(key)  # 在第一个 POST await 前锁定，取消协程也不能重发。
        try:
            result = await self._ex.create_order(
                ccxt_symbol,
                "market",
                close_side,
                amount,
                None,
                {
                    "stopLossPrice" if kind == "stop_loss" else "takeProfitPrice": float(price),
                    "reduceOnly": True,
                    "workingType": working_type,
                    "priceProtect": False,
                    "positionSide": "BOTH",
                    "newClientOrderId": client_order_id,
                },
            )
        except Exception as error:
            raise await self._submit_failure(
                symbol, client_order_id, error, lifeboat=lifeboat
            ) from error
        try:
            order = _bracket_from(result["info"], lifeboat=lifeboat)
            if not matches(order):
                raise ValueError("响应身份或保护语义不符")
            # 即时触发的 ACK 必须再读 algo 及真实 child；不把响应的 executedQty 当成交。
            if order.child_state != "absent" or order.algo_status != "NEW":
                observed = await query()
                if observed is None or observed.algo_id != order.algo_id or not matches(observed):
                    raise ValueError("普通保护单提交回读身份或参数不一致")
                return observed
            return order
        except Exception as error:
            raise await self._bracket_uncertain(
                symbol, client_order_id, lifeboat=lifeboat
            ) from error

    async def get_bracket(
        self,
        symbol: str,
        *,
        algo_id: str | None = None,
        client_order_id: str | None = None,
    ) -> ExchangeBracket | None:
        """仅明确 algo OrderNotFound 返回 None；未知提交后的 None 不能授权重发。

        child 缺 ID 保留 unknown；有 ID 就必须查其原始数量/方向/只减仓/状态，缺证据抛错。
        """
        import ccxt.async_support as ccxt

        _bracket_symbol(symbol)
        if (algo_id is None) == (client_order_id is None):
            raise ValueError("必须且仅指定algo_id或client_order_id")
        if client_order_id is not None:
            _bracket_cid(client_order_id)
        if algo_id is not None:
            _bracket_id(algo_id)
        request = {"algoId": algo_id} if algo_id is not None else {"clientAlgoId": client_order_id}
        try:
            raw = await self._ex.fapiPrivateGetAlgoOrder(request)
        except ccxt.OrderNotFound:
            return None
        except Exception as error:
            raise ExchangeError("查询普通保护条件单未知", transient=is_transient(error)) from error
        try:
            order = _bracket_from(raw)
            if (
                order.symbol != symbol
                or (algo_id is not None and order.algo_id != algo_id)
                or (client_order_id is not None and order.client_order_id != client_order_id)
            ):
                raise ValueError("普通保护条件单查询身份不符")
            if order.actual_order_id is not None:
                actual = await self._ex.fapiPrivateGetOrder(
                    {"symbol": symbol, "orderId": order.actual_order_id}
                )
                order = _bracket_child(order, actual)
            elif _trigger_failed(raw, order):
                order = await self._no_child_after_trigger(order)
            return order
        except Exception as error:
            raise ExchangeError(
                "普通保护条件单或实际子订单证据不完整", transient=is_transient(error)
            ) from error

    async def get_lifeboat(
        self,
        symbol: str,
        *,
        client_order_id: str,
        algo_id: str | None = None,
    ) -> ExchangeBracket | None:
        """按持久 lb-CID 及可选 algoID 精确查询；父单查无不证明 child 已结束。

        强制 STOP_MARKET/MARK_PRICE/BOTH/只减仓；child 使用原始订单身份与数量核验。
        """
        import ccxt.async_support as ccxt

        _bracket_symbol(symbol)
        _lifeboat_cid(client_order_id)
        if algo_id is not None:
            _bracket_id(algo_id)
        request = {"algoId": algo_id} if algo_id is not None else {"clientAlgoId": client_order_id}
        try:
            raw = await self._ex.fapiPrivateGetAlgoOrder(request)
        except ccxt.OrderNotFound:
            return None
        except Exception as error:
            raise ExchangeError("保命单查询未知", transient=is_transient(error)) from error
        try:
            order = _bracket_from(raw, lifeboat=True)
            if (
                order.symbol != symbol
                or order.client_order_id != client_order_id
                or (algo_id is not None and order.algo_id != algo_id)
                or order.working_type != "MARK_PRICE"
                or order.position_side != "BOTH"
                or not order.reduce_only
                or order.close_position
                or order.price_protect
            ):
                raise ValueError("保命单身份或保护语义不符")
            if order.actual_order_id is not None:
                actual = await self._ex.fapiPrivateGetOrder(
                    {"symbol": symbol, "orderId": order.actual_order_id}
                )
                order = _bracket_child(order, actual)
            elif _trigger_failed(raw, order):
                order = await self._no_child_after_trigger(order)
            return order
        except Exception as error:
            raise ExchangeError(
                "保命单或实际子订单证据不完整", transient=is_transient(error)
            ) from error

    async def _no_child_after_trigger(self, order: ExchangeBracket) -> ExchangeBracket:
        """触发后被拒的条件单：按 clientAlgoId 复核，查无或同名单已终态零成交才认作没有子单。

        Binance 不保证子单 CID 等于 clientAlgoId，这一查只能排除同名子单；查到在场或有成交的单、
        身份不符或查询失败都保留 unknown（由调用方抛错）。
        """
        import ccxt.async_support as ccxt

        try:
            raw = await self._ex.fapiPrivateGetOrder(
                {"symbol": order.symbol, "origClientOrderId": order.client_order_id}
            )
        except ccxt.OrderNotFound:
            return replace(order, child_state="absent")
        if (
            isinstance(raw, dict)
            and raw.get("symbol") == order.symbol
            and raw.get("clientOrderId") == order.client_order_id
            and raw.get("side") == order.close_side.upper()
            and _close_bool(raw.get("reduceOnly"))
            and raw.get("status") in _CHILD_TERMINAL - {"FILLED"}
            and _bracket_number(raw.get("executedQty"), zero=True) == 0
        ):
            return replace(order, child_state="absent")
        raise ValueError("触发后被拒的条件单仍查到在场或有成交的同名订单，子单不能认作不存在")

    async def open_brackets(self, symbol: str | None = None) -> list[ExchangeBracket]:
        """仅返回 br-sl/br-tp 角色；不接管 lifeboat 或用户单，不推导子单成交量。"""
        if symbol is not None:
            _bracket_symbol(symbol)
        try:
            raw = await self._ex.fapiPrivateGetOpenAlgoOrders({"symbol": symbol} if symbol else {})
            if not isinstance(raw, list):
                raise ValueError("开放条件单响应不是完整列表")
            orders: list[ExchangeBracket] = []
            ids: set[str] = set()
            cids: set[str] = set()
            for item in raw:
                if not isinstance(item, dict):
                    raise ValueError("开放条件单列表包含未知对象")
                cid = str(item.get("clientAlgoId") or "")
                if not cid.startswith(tuple(BRACKET_PREFIXES.values())):
                    continue
                order = _bracket_from(item)
                if (
                    (symbol is not None and order.symbol != symbol)
                    or order.algo_id in ids
                    or order.client_order_id in cids
                ):
                    raise ValueError("开放条件单标的或唯一身份不符")
                ids.add(order.algo_id)
                cids.add(order.client_order_id)
                orders.append(order)
            return orders
        except Exception as error:
            raise ExchangeError(
                "查询普通开放保护单失败或响应不完整", transient=is_transient(error)
            ) from error

    async def assert_entry_clear(self, symbol: str) -> None:
        """只读确认两类完整开放订单列表均为空；包含手工单及旧保命单，不撤任何单。

        两个 all-open 端点不分页，不能用带 cursor/包装对象的未知响应代替完整列表。
        这是检查时点的证据，并不阻止 HB 或其他客户端随后独立发单。
        """
        _bracket_symbol(symbol)
        try:
            normal, algo = await asyncio.gather(
                self._ex.fapiPrivateGetOpenOrders({"symbol": symbol}),
                self._ex.fapiPrivateGetOpenAlgoOrders({"symbol": symbol}),
            )
        except Exception as error:
            raise ExchangeError(
                "同币开放订单检查失败，禁止新开", transient=is_transient(error)
            ) from error
        if not isinstance(normal, list) or not isinstance(algo, list):
            raise ExchangeError("同币开放订单响应不完整，禁止新开")
        if normal or algo:
            raise ExchangeError("同币仍有开放订单或未知订单记录，禁止新开")

    async def lifeboat_ready(
        self,
        symbol: str,
        client_order_ids: tuple[str, ...],
        close_side: Literal["buy", "sell"],
        quantity: float,
        trigger_price: float,
    ) -> bool:
        """仅核指定保命单是否仍能覆盖实际余仓，不将旧 ExchangeStop 当完整保护证据。

        一个有效 NEW 不能掩盖另一个 CID 的查询/触发未知。仅明确无匹配返回 False；
        不撤旧保命单或陌生单，也不以这个检查推断后续 HB/市场不会同时退出。
        """
        import ccxt.async_support as ccxt

        if (
            not client_order_ids
            or len(set(client_order_ids)) != len(client_order_ids)
            or any(not re.fullmatch(r"lb-[.A-Za-z0-9_:/-]{1,33}", c) for c in client_order_ids)
            or close_side not in {"buy", "sell"}
        ):
            raise ValueError("保命单核验CID或方向非法")
        spec = await self.normalized_bracket_spec(symbol, quantity, trigger_price)
        actual_quantity = _bracket_number(quantity)
        ready = False
        for cid in client_order_ids:
            try:
                raw = await self._ex.fapiPrivateGetAlgoOrder({"clientAlgoId": cid})
            except ccxt.OrderNotFound:
                continue
            except Exception as error:
                raise ExchangeError("保命单覆盖查询未知", transient=is_transient(error)) from error
            try:
                order = _bracket_from(raw, lifeboat=True)
                if (
                    order.symbol != symbol
                    or order.client_order_id != cid
                    or order.close_side != close_side
                    or order.position_side != "BOTH"
                    or order.working_type != "MARK_PRICE"
                    or not order.reduce_only
                    or order.price_protect
                    or order.close_position
                ):
                    raise ValueError("保命单身份或保护语义不符")
                if order.child_state != "absent":
                    raise ValueError("保命单有触发或未知子订单，不能以另一张NEW掩盖")
                if order.algo_status in {"CANCELED", "REJECTED", "EXPIRED"}:
                    continue
                if order.algo_status != "NEW":
                    raise ValueError("保命单状态未知")
                if order.quantity >= actual_quantity and order.trigger_price == spec.trigger_price:
                    ready = True
            except Exception as error:
                raise ExchangeError("保命单完整覆盖证据未知") from error
        return ready

    async def entry_basis(self, symbol: str, client_order_id: str) -> EntryBasis:
        """按原始开仓 CID 读终态订单及完整成交账，不复用估算价格或默认零费用。

        仅接受单向账户、终态且正成交的开仓；成交量及金额须精确覆盖两次一致的订单快照。
        userTrades 达 1000 条边界直接 unknown，不把可能被截断的分页当作完整账本。
        """
        _bracket_symbol(symbol)
        if not re.fullmatch(r"[.A-Za-z0-9_:/-]{1,36}", client_order_id):
            raise ValueError("开仓成本CID非法")
        try:
            if await self.position_mode() != "one_way":
                raise ValueError("开仓成本只支持已确认的单向模式")
            request = {"symbol": symbol, "origClientOrderId": client_order_id}
            before = _entry_basis_order(
                await self._ex.fapiPrivateGetOrder(request), symbol, client_order_id
            )
            order_id, side, filled, quote, status = before
            rows = await self._ex.fapiPrivateGetUserTrades(
                {"symbol": symbol, "orderId": order_id, "limit": 1000}
            )
            fee = _entry_basis_fills(rows, symbol, order_id, side, filled, quote)
            after = _entry_basis_order(
                await self._ex.fapiPrivateGetOrder(request), symbol, client_order_id
            )
            if after != before:
                raise ValueError("开仓成本查询期间订单证据变化，不能冻结基准")
            quantity, notional = _bracket_number(filled), _bracket_number(quote)
            fee_float = float(fee)
            if not math.isfinite(fee_float) or Decimal(str(fee_float)) != fee:
                raise ValueError("开仓实际费用无法无损保存")
            entry_price = float(quote / filled)
            if not math.isfinite(entry_price) or entry_price <= 0:
                raise ValueError("开仓实际均价非法")
            try:
                fill_times = [_bracket_time(row.get("time")) for row in rows]
            except (ValueError, ArithmeticError, OverflowError, OSError):
                fill_times = [None]
            known_times = [t for t in fill_times if t is not None]
            times_complete = len(known_times) == len(rows)
            return EntryBasis(
                symbol=symbol,
                client_order_id=client_order_id,
                order_id=order_id,
                side=side,
                filled_quantity=quantity,
                filled_quote=notional,
                fee_usdt=fee_float,
                entry_price=entry_price,
                order_status=status,
                first_fill_at=min(known_times) if times_complete else None,
                last_fill_at=max(known_times) if times_complete else None,
            )
        except Exception as error:
            raise ExchangeError(
                "开仓实际成交成本或费用证据未知", transient=is_transient(error)
            ) from error

    async def execution_book(self, symbol: str, *, max_age_s: float = 5.0) -> ExecutionBook:
        """只从本交易客户端同环境读取盘口；缺少/未来/过期的交易所时间一律未知。

        5 秒是证据健康上界，不是策略阈值；调用方还须处理乱序及消费时再检查新鲜度。
        """
        _bracket_symbol(symbol)
        age_limit = _bracket_number(max_age_s)
        started_at = datetime.now(UTC)
        try:
            raw = await self._ex.fapiPublicGetTickerBookTicker({"symbol": symbol})
            observed_at = datetime.now(UTC)
            if not isinstance(raw, dict) or raw.get("symbol") != symbol:
                raise ValueError("同环境盘口身份不符")
            bid, ask = _bracket_number(raw.get("bidPrice")), _bracket_number(raw.get("askPrice"))
            _bracket_number(raw.get("bidQty"))
            _bracket_number(raw.get("askQty"))
            source_at = _bracket_time(raw.get("time"))
            if source_at is None:
                raise ValueError("同环境盘口没有交易所时间")
            age = (observed_at - source_at).total_seconds()
            if bid > ask or observed_at < started_at or age < 0 or age > age_limit:
                raise ValueError("同环境盘口交叉或时钟/新鲜度不可信")
            return ExecutionBook(symbol, bid, ask, source_at, observed_at)
        except Exception as error:
            raise ExchangeError("同环境执行盘口证据未知", transient=is_transient(error)) from error

    async def execution_mark(self, symbol: str, *, max_age_s: float = 5.0) -> MarkEvidence:
        """同环境标记价（带交易所时间）；缺少 / 未来 / 过期 / 非正数一律未知，不回退到别的价。

        和 ``execution_book`` 一样，5 秒是证据健康上界；调用方消费时还要再查新鲜度。
        """
        _bracket_symbol(symbol)
        age_limit = _bracket_number(max_age_s)
        started_at = datetime.now(UTC)
        try:
            raw = await self._ex.fapiPublicGetPremiumIndex({"symbol": symbol})
            observed_at = datetime.now(UTC)
            row = raw[0] if isinstance(raw, list) and len(raw) == 1 else raw
            if not isinstance(row, dict) or row.get("symbol") != symbol:
                raise ValueError("同环境标记价身份不符")
            mark = _bracket_number(row.get("markPrice"))
            source_at = _bracket_time(row.get("time"))
            if source_at is None:
                raise ValueError("同环境标记价没有交易所时间")
            age = (observed_at - source_at).total_seconds()
            if observed_at < started_at or age < 0 or age > age_limit:
                raise ValueError("同环境标记价时钟/新鲜度不可信")
            return MarkEvidence(symbol, mark, source_at, observed_at)
        except Exception as error:
            raise ExchangeError("同环境标记价证据未知", transient=is_transient(error)) from error

    async def cancel_bracket(
        self, symbol: str, algo_id: str, *, expected_client_order_id: str
    ) -> None:
        """精确核对所有权再撤单，回读明确终态才成功；未知/触发中的 child 不代撤。"""
        _bracket_cid(expected_client_order_id)
        await self._cancel_protection(
            symbol, algo_id, expected_client_order_id=expected_client_order_id
        )

    async def cancel_lifeboat(
        self, symbol: str, algo_id: str, *, expected_client_order_id: str
    ) -> None:
        """新托管流程保命单清理：核 exact lb-CID/algoID，回读父/子终态才返回。

        不调用旧 cancel_stop 的 OrderNotFound 即成功路径，不撤正在工作的实际子订单。
        """
        _lifeboat_cid(expected_client_order_id)
        await self._cancel_protection(
            symbol, algo_id, expected_client_order_id=expected_client_order_id, lifeboat=True
        )

    async def _cancel_protection(
        self,
        symbol: str,
        algo_id: str,
        *,
        expected_client_order_id: str,
        lifeboat: bool = False,
    ) -> None:
        _bracket_id(algo_id)

        async def query() -> ExchangeBracket | None:
            return (
                await self.get_lifeboat(
                    symbol, client_order_id=expected_client_order_id, algo_id=algo_id
                )
                if lifeboat
                else await self.get_bracket(symbol, algo_id=algo_id)
            )

        order = await query()
        if order is None:
            raise ExchangeError("保护单查无结果不能证明已撤，保留待核验")
        if order.client_order_id != expected_client_order_id:
            raise ExchangeError("保护单CID与预期仓位不符，禁止撤单")
        if _bracket_settled(order):
            return
        if order.algo_status != "NEW" or order.child_state != "absent":
            raise ExchangeError("保护单存在触发或未知子订单，不能认定可安全撤除")
        try:
            response = await self._ex.fapiPrivateDeleteAlgoOrder({"algoId": algo_id})
        except Exception as error:
            # 超时 / OrderNotFound 可能是 HB 独立退出或触发竞态，只读核验，不重发 DELETE。
            observed = None
            with contextlib.suppress(ExchangeError):
                observed = await query()
            if (
                observed is not None
                and observed.client_order_id == expected_client_order_id
                and _bracket_settled(observed)
            ):
                return
            raise ExchangeError("撤普通保护条件单结果未知，须重新查询") from error
        if (
            not isinstance(response, dict)
            or str(response.get("algoId")) != algo_id
            or response.get("clientAlgoId") != expected_client_order_id
            or str(response.get("code")) != "200"
        ):
            raise ExchangeError("撤普通保护条件单回执身份或状态不符，保留待核验")
        observed = await query()
        if (
            observed is None
            or observed.client_order_id != expected_client_order_id
            or not _bracket_settled(observed)
        ):
            raise ExchangeError("撤普通保护条件单后仍未证实终止，保留待核验")

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
            raw = await self._close_lane().fapiPrivateGetOrder(
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
        try:
            lane = await self._close_ready()
            if await self._close_position_mode(lane) != "one_way":
                raise ValueError("不支持双向持仓模式")
            ccxt_symbol = to_ccxt_symbol(symbol)
            amount = self._ex.amount_to_precision(ccxt_symbol, float(qty))
            requested = _close_decimal(amount)
            existing = await self.close_order(symbol, client_order_id)
        except asyncio.CancelledError as error:
            raise IdentifiedCloseCancelledBeforeSend() from error
        except Exception as error:
            # 这几步都在下单请求之前；调用方保证这个 CID 是新一代、此前从没发过。
            raise IdentifiedCloseNotSent("直连平仓下单前检查失败，请求没有发出") from error

        def matches(order: IdentifiedCloseOrder) -> bool:
            return order.close_side == close_side and order.quantity == float(requested)

        if existing is not None:
            if not matches(existing):
                raise ExchangeError("原直连平仓CID对应不同方向或数量，禁止复用")
            return existing  # 包括终态部分成交；绝不自动创建下一代。
        try:
            response = await lane.create_order(
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
        off: dict[str, str] = {}
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
                if info.get("symbol"):
                    off[str(info["symbol"])] = str(info.get("status") or "UNKNOWN")
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
        self.untradeable_status = off
        return out

    async def leverage_brackets(self) -> dict[str, tuple[tuple[float, int], ...]]:
        """Per-symbol ``(notionalCap, initialLeverage)`` tiers of this account on the execution
        venue (signed read-only GET /fapi/v1/leverageBracket), ascending by notional cap, so the
        first tier carries the symbol's maximum leverage. Symbols with unreadable tiers are left
        out."""
        try:
            rows = await self._ex.fapiPrivateGetLeverageBracket()
        except Exception as e:
            raise ExchangeError(f"读杠杆分层失败：{e}") from e
        out: dict[str, tuple[tuple[float, int], ...]] = {}
        for row in rows if isinstance(rows, list) else []:
            try:
                tiers = sorted(
                    (float(b["notionalCap"]), int(float(b["initialLeverage"])))
                    for b in row["brackets"]
                )
                symbol = str(row["symbol"])
            except (KeyError, TypeError, ValueError):
                continue
            if tiers and all(cap > 0 and lev >= 1 for cap, lev in tiers):
                out[symbol] = tuple(tiers)
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
                    maker=r["maker"] if isinstance(r.get("maker"), bool) else None,
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
        lane = self.__dict__.get("_close_ex")
        if lane is not None and lane is not self._ex:
            await lane.close()


def _fee_usdt(fee: dict[str, Any] | None) -> float | None:
    """ccxt 的 fee 字典 → USDT 数额；币种不是 USDT（如 BNB 抵扣）或没有就返回 None。"""
    if not fee or fee.get("cost") is None:
        return None
    if str(fee.get("currency") or "").upper() not in ("USDT", "USDC", "BUSD"):
        return None
    return abs(float(fee["cost"]))
