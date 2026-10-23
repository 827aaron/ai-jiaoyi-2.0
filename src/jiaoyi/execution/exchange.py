"""币安 U 本位合约的直连客户端（ccxt）。

只做三件事：查真实持仓、挂 / 撤 / 查保命止损、查未平的条件单。它和 hummingbot-api 是两条
独立通道，对账员靠这条通道核对 hummingbot 说的话。开仓不走这里。

Protocol 在这里，ccxt 实现放在最后，测试用内存假件。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable

from jiaoyi.contracts import Side
from jiaoyi.execution.symbols import from_ccxt_symbol, to_ccxt_symbol

STOP_PREFIX = "lb-"
"""保命止损的 clientOrderId 前缀，对账员靠它认出“自己的”条件单。"""


class ExchangeError(Exception):
    """交易所调用失败（网络、鉴权、拒单）。调用方按故障即关闭处理。"""

    def __init__(self, message: str, *, immediate_trigger: bool = False) -> None:
        super().__init__(message)
        self.immediate_trigger = immediate_trigger
        """币安 -2021：触发价已被越过，挂上就会立刻成交。意味着价格已经穿过止损位。"""


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
class LotFilter:
    """币安 exchangeInfo 里一个合约的下单精度。"""

    step_size: float
    min_qty: float
    min_notional: float
    max_qty: float | None = None
    """市价单单笔最大数量（MARKET_LOT_SIZE.maxQty）；没有就是不限。"""


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
    ) -> None:
        """只减仓市价单。**兜底专用**：hummingbot 不可达而保命止损又挂不上时，对账员用它平仓。"""
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
            raise ExchangeError(f"查持仓失败：{e}") from e
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
            raise ExchangeError(f"查条件单失败：{e}") from e
        return [self._stop_from(o) for o in raw if self._is_stop(o)]

    @staticmethod
    def _is_stop(raw: dict[str, Any]) -> bool:
        """是不是 STOP_MARKET 条件单。algo 接口返回的单子 ccxt 解析出的 type 是 "market"，
        不能靠 type；以币安原始的 orderType 为准。"""
        info = raw.get("info") or {}
        order_type = str(info.get("orderType") or info.get("type") or raw.get("type") or "")
        return order_type.upper().startswith("STOP")

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
    ) -> None:
        try:
            await self._ready()
            ccxt_symbol = to_ccxt_symbol(symbol)
            await self._ex.create_order(
                ccxt_symbol,
                "market",
                close_side,
                self._ex.amount_to_precision(ccxt_symbol, quantity),
                None,
                {"reduceOnly": True},
            )
        except Exception as e:
            raise ExchangeError(f"直连平仓失败：{e}") from e

    async def lot_filters(self) -> dict[str, LotFilter]:
        try:
            markets = await self._ex.load_markets(True)
        except Exception as e:
            raise ExchangeError(f"读 exchangeInfo 失败：{e}") from e
        self._markets_loaded = True
        out: dict[str, LotFilter] = {}
        for m in markets.values():
            info = m.get("info") or {}
            if not (m.get("swap") and m.get("linear") and m.get("settle") == "USDT"):
                continue
            if info.get("contractType") != "PERPETUAL" or info.get("status") != "TRADING":
                continue
            try:
                symbol = from_ccxt_symbol(str(m["symbol"]))
                filters = {f["filterType"]: f for f in info.get("filters", [])}
                lot = filters["LOT_SIZE"]
                notional = filters.get("MIN_NOTIONAL", {}).get("notional", 0.0)
                market_max = float(filters.get("MARKET_LOT_SIZE", {}).get("maxQty") or 0.0)
                out[symbol] = LotFilter(
                    float(lot["stepSize"]),
                    float(lot["minQty"]),
                    float(notional),
                    max_qty=market_max if market_max > 0 else None,
                )
            except (KeyError, ValueError):
                continue  # 缺字段的合约不给精度，保镖会对它拒单
        return out

    _INCOME_TYPES = frozenset({"REALIZED_PNL", "COMMISSION", "FUNDING_FEE"})

    async def income_since(self, since: datetime) -> float:
        total = 0.0
        start_ms = int(since.timestamp() * 1000)
        try:
            while True:
                rows = await self._ex.fapiPrivateGetIncome({"startTime": start_ms, "limit": 1000})
                for row in rows:
                    if row.get("incomeType") in self._INCOME_TYPES:
                        total += float(row["income"])
                if len(rows) < 1000:
                    return total
                start_ms = max(int(r["time"]) for r in rows) + 1
        except Exception as e:
            raise ExchangeError(f"查收入流水失败：{e}") from e

    async def close(self) -> None:
        await self._ex.close()
