"""币安 U 本位合约的直连客户端（ccxt）。

只做三件事：查真实持仓、挂 / 撤 / 查保命止损、查未平的条件单。它和 hummingbot-api 是两条
独立通道，对账员靠这条通道核对 hummingbot 说的话。开仓不走这里。

Protocol 在这里，ccxt 实现放在最后，测试用内存假件。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from jiaoyi.contracts import Side
from jiaoyi.execution.symbols import from_ccxt_symbol, to_ccxt_symbol

STOP_PREFIX = "lb-"
"""保命止损的 clientOrderId 前缀，对账员靠它认出“自己的”条件单。"""


class ExchangeError(Exception):
    """交易所调用失败（网络、鉴权、拒单）。调用方按故障即关闭处理。"""


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

    async def close(self) -> None: ...


class CcxtBinanceFutures:
    """ccxt 实现。``testnet=True`` 走币安合约测试网。

    币安 2025-12 起把条件单挪到 algo 订单接口，ccxt 4.5 的 ``trigger`` 参数会自动走新接口；
    升级 ccxt 要先在测试网验证挂单、查单、撤单三步（docs/M0-setup.md）。
    """

    def __init__(self, api_key: str, api_secret: str, *, testnet: bool, timeout_ms: int = 5000):
        import ccxt.async_support as ccxt

        self._ex: Any = ccxt.binanceusdm(
            {
                "apiKey": api_key,
                "secret": api_secret,
                "timeout": timeout_ms,
                "enableRateLimit": True,
                "options": {"defaultType": "future"},
            }
        )
        if testnet:
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
            raise ExchangeError(f"挂保命止损失败：{e}") from e
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
        return [
            self._stop_from(o) for o in raw if str(o.get("type", "")).lower().startswith("stop")
        ]

    async def close(self) -> None:
        await self._ex.close()
