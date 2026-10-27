"""持久化CID直连平仓低层：合成订单与CCXT拦截，不向交易所发请求。"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import ccxt.async_support as ccxt
import pytest

from jiaoyi.execution.exchange import (
    CcxtBinanceFutures,
    ExchangeError,
    IdentifiedCloseRejected,
    IdentifiedCloseUnknown,
)


def raw(**change: Any) -> dict[str, Any]:
    order: dict[str, Any] = {
        "symbol": "SYNTHUSDT",
        "clientOrderId": "dc-synthetic",
        "orderId": "17",
        "side": "SELL",
        "type": "MARKET",
        "positionSide": "BOTH",
        "reduceOnly": True,
        "status": "NEW",
        "origQty": "10",
        "executedQty": "0",
        "avgPrice": "0",
    }
    order.update(change)
    return order


def client() -> tuple[CcxtBinanceFutures, Any]:
    obj = CcxtBinanceFutures.__new__(CcxtBinanceFutures)
    obj._markets_loaded = True
    ex = AsyncMock()
    ex.amount_to_precision = lambda symbol, qty: str(qty)
    ex.fapiPrivateGetPositionSideDual.return_value = {"dualSidePosition": False}
    ex.fapiPrivateGetOrder.side_effect = ccxt.OrderNotFound("synthetic absent")
    ex.create_order.return_value = {"info": raw()}
    obj._ex = ex
    return obj, ex


@pytest.mark.asyncio
async def test_place_once_explicit_cid_reduce_only_and_not_filled_on_new() -> None:
    obj, ex = client()
    order = await obj.place_close("SYNTHUSDT", "sell", 10, "dc-synthetic")
    ex.create_order.assert_awaited_once_with(
        "SYNTH/USDT:USDT",
        "market",
        "sell",
        "10.0",
        None,
        {
            "newClientOrderId": "dc-synthetic",
            "reduceOnly": True,
            "positionSide": "BOTH",
            "newOrderRespType": "RESULT",
        },
    )
    assert order.filled_quantity == 0 and order.remaining_quantity == 10
    assert order.average_price is None and order.is_terminal is False
    ex.fapiPrivateGetOrder.assert_awaited_once_with(
        {"symbol": "SYNTHUSDT", "origClientOrderId": "dc-synthetic"}
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [{}, {"dualSidePosition": True}, {"dualSidePosition": 0}, None])
async def test_unknown_or_hedge_mode_cannot_submit(mode: Any) -> None:
    obj, ex = client()
    ex.fapiPrivateGetPositionSideDual.return_value = mode
    with pytest.raises(ExchangeError):
        await obj.place_close("SYNTHUSDT", "sell", 10, "dc-synthetic")
    ex.create_order.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "qty,cid",
    [
        (0, "dc-x"),
        (float("nan"), "dc-x"),
        (float("inf"), "dc-x"),
        (10, "lb-x"),
        (10, "dc-" + "x" * 34),
    ],
)
async def test_bad_inputs_never_create(qty: float, cid: str) -> None:
    obj, ex = client()
    with pytest.raises(ValueError):
        await obj.place_close("SYNTHUSDT", "sell", qty, cid)
    ex.create_order.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        ccxt.RequestTimeout("synthetic"),
        ccxt.ExchangeNotAvailable("503 synthetic"),
        ccxt.ExchangeError("-1007 synthetic"),
        ccxt.InvalidOrder("503 synthetic"),
        ccxt.InvalidOrder("Duplicate order sent"),
    ],
)
async def test_ambiguous_submission_retains_cid_and_never_retries(error: Exception) -> None:
    obj, ex = client()
    ex.create_order.side_effect = error
    ex.fapiPrivateGetOrder.side_effect = [
        ccxt.OrderNotFound("before"),
        raw(status="PARTIALLY_FILLED", executedQty="3", avgPrice="1.1"),
    ]
    with pytest.raises(IdentifiedCloseUnknown) as caught:
        await obj.place_close("SYNTHUSDT", "sell", 10, "dc-synthetic")
    assert caught.value.client_order_id == "dc-synthetic"
    assert caught.value.observed is not None and caught.value.observed.filled_quantity == 3
    ex.create_order.assert_awaited_once()
    assert ex.fapiPrivateGetOrder.await_count == 2


@pytest.mark.asyncio
async def test_explicit_rejection_is_not_unknown_or_market_retry() -> None:
    obj, ex = client()
    ex.create_order.side_effect = ccxt.InvalidOrder("-2022 ReduceOnly Order is rejected")
    with pytest.raises(IdentifiedCloseRejected):
        await obj.place_close("SYNTHUSDT", "sell", 10, "dc-synthetic")
    ex.create_order.assert_awaited_once()
    assert ex.fapiPrivateGetOrder.await_count == 1


@pytest.mark.asyncio
async def test_existing_terminal_partial_is_returned_and_not_recreated() -> None:
    obj, ex = client()
    ex.fapiPrivateGetOrder.side_effect = None
    ex.fapiPrivateGetOrder.return_value = raw(status="CANCELED", executedQty="3", avgPrice="1.2")
    order = await obj.place_close("SYNTHUSDT", "sell", 10, "dc-synthetic")
    assert order.is_terminal is True and order.remaining_quantity == 7
    assert order.average_price == 1.2
    ex.create_order.assert_not_awaited()
    with pytest.raises(ExchangeError, match="不同方向或数量"):
        await obj.place_close("SYNTHUSDT", "buy", 10, "dc-synthetic")
    ex.create_order.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"symbol": "OTHERUSDT"},
        {"clientOrderId": "dc-other"},
        {"side": None},
        {"positionSide": "LONG"},
        {"reduceOnly": False},
        {"origQty": None},
        {"executedQty": None},
        {"executedQty": "NaN"},
        {"executedQty": "11"},
        {"status": "FILLED", "executedQty": "2"},
        {"type": "LIMIT"},
    ],
)
async def test_query_incomplete_or_wrong_order_is_unknown(change: dict[str, Any]) -> None:
    obj, ex = client()
    ex.fapiPrivateGetOrder.side_effect = None
    ex.fapiPrivateGetOrder.return_value = raw(**change)
    with pytest.raises(IdentifiedCloseUnknown):
        await obj.close_order("SYNTHUSDT", "dc-synthetic")
    ex.create_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_ack_without_actual_quantity_is_unknown_not_filled() -> None:
    obj, ex = client()
    ex.create_order.return_value = {"info": {"orderId": "17", "clientOrderId": "dc-synthetic"}}
    with pytest.raises(IdentifiedCloseUnknown) as caught:
        await obj.place_close("SYNTHUSDT", "sell", 10, "dc-synthetic")
    assert caught.value.observed is None
    ex.create_order.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_notfound_is_distinct_from_timeout_and_does_not_resubmit() -> None:
    obj, ex = client()
    assert await obj.close_order("SYNTHUSDT", "dc-synthetic") is None
    ex.fapiPrivateGetOrder.side_effect = ccxt.RequestTimeout("synthetic")
    with pytest.raises(IdentifiedCloseUnknown):
        await obj.close_order("SYNTHUSDT", "dc-synthetic")
    ex.create_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_ccxt_market_request_is_intercepted_without_network() -> None:
    ex = ccxt.binanceusdm()
    ex.enable_demo_trading(True)
    market: dict[str, Any] = {
        "id": "SYNTHUSDT",
        "symbol": "SYNTH/USDT:USDT",
        "type": "swap",
        "spot": False,
        "swap": True,
        "future": False,
        "option": False,
        "contract": True,
        "linear": True,
        "inverse": False,
        "info": {"orderTypes": ["MARKET"]},
        "precision": {"amount": 0.01, "price": 0.0001},
        "base": "SYNTH",
        "quote": "USDT",
        "settle": "USDT",
    }
    ex.set_markets([market])
    requests: list[dict[str, Any]] = []

    async def intercepted(request: dict[str, Any]) -> dict[str, Any]:
        requests.append(dict(request))
        return raw(status="FILLED", executedQty="10", avgPrice="1.2")

    ex.fapiPrivatePostOrder = intercepted
    try:
        await ex.create_order(
            "SYNTH/USDT:USDT",
            "market",
            "sell",
            10,
            None,
            {
                "newClientOrderId": "dc-synthetic",
                "reduceOnly": True,
                "positionSide": "BOTH",
                "newOrderRespType": "RESULT",
            },
        )
        assert len(requests) == 1
        request = requests[0]
        assert request["newClientOrderId"] == "dc-synthetic" and request["type"] == "MARKET"
        assert request["reduceOnly"] is True and request["positionSide"] == "BOTH"
        assert request["quantity"] == "10" and "triggerPrice" not in request
        assert ex.urls["api"]["fapiPrivate"] == "https://demo-fapi.binance.com/fapi/v1"
    finally:
        await ex.close()
