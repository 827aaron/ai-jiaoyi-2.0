"""持久化CID直连平仓低层：合成订单与CCXT拦截，不向交易所发请求。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import ccxt.async_support as ccxt
import pytest

from jiaoyi.execution.exchange import (
    CcxtBinanceFutures,
    ExchangeError,
    IdentifiedCloseCancelledBeforeSend,
    IdentifiedCloseNotSent,
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
@pytest.mark.parametrize(
    ("update_time", "expected"),
    [
        (1793167827512, datetime(2026, 10, 28, 6, 10, 27, 512000, tzinfo=UTC)),
        ("1793167827512", datetime(2026, 10, 28, 6, 10, 27, 512000, tzinfo=UTC)),
        (None, None),
        ("garbage", None),
        ("1.5", None),
    ],
)
async def test_close_order_update_time_is_read_but_never_blocks_identity(
    update_time: Any, expected: datetime | None
) -> None:
    obj, ex = client()
    ex.fapiPrivateGetOrder.side_effect = None
    ex.fapiPrivateGetOrder.return_value = raw(
        status="FILLED", executedQty="10", avgPrice="1.2", updateTime=update_time
    )
    order = await obj.close_order("SYNTHUSDT", "dc-synthetic")
    assert order is not None and order.is_terminal is True
    assert order.update_time == expected


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


@pytest.mark.asyncio
@pytest.mark.parametrize("step", ["markets", "mode", "precheck"])
async def test_failure_before_create_is_definitely_not_sent(step: str) -> None:
    obj, ex = client()
    if step == "markets":
        obj._markets_loaded = False
        ex.load_markets.side_effect = ccxt.RequestTimeout("synthetic")
    elif step == "mode":
        ex.fapiPrivateGetPositionSideDual.side_effect = ccxt.RequestTimeout("synthetic")
    else:
        ex.fapiPrivateGetOrder.side_effect = ccxt.RequestTimeout("synthetic")
    with pytest.raises(IdentifiedCloseNotSent):
        await obj.place_close("SYNTHUSDT", "sell", 10, "dc-synthetic")
    ex.create_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_timeout_stays_unknown_not_not_sent() -> None:
    obj, ex = client()
    ex.create_order.side_effect = ccxt.RequestTimeout("synthetic")
    with pytest.raises(IdentifiedCloseUnknown):
        await obj.place_close("SYNTHUSDT", "sell", 10, "dc-synthetic")


@pytest.mark.asyncio
@pytest.mark.parametrize("step", ["mode", "precheck"])
async def test_cancel_before_create_is_not_sent_and_still_a_cancellation(step: str) -> None:
    obj, ex = client()
    if step == "mode":
        ex.fapiPrivateGetPositionSideDual.side_effect = asyncio.CancelledError()
    else:
        ex.fapiPrivateGetOrder.side_effect = asyncio.CancelledError()
    with pytest.raises(IdentifiedCloseCancelledBeforeSend) as caught:
        await obj.place_close("SYNTHUSDT", "sell", 10, "dc-synthetic")
    assert isinstance(caught.value, asyncio.CancelledError)
    ex.create_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_during_create_stays_unknown() -> None:
    obj, ex = client()
    ex.create_order.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError) as caught:
        await obj.place_close("SYNTHUSDT", "sell", 10, "dc-synthetic")
    assert not isinstance(caught.value, IdentifiedCloseCancelledBeforeSend)


def with_close_lane() -> tuple[CcxtBinanceFutures, Any, Any]:
    obj, main = client()
    lane = AsyncMock()
    lane.fapiPrivateGetPositionSideDual.return_value = {"dualSidePosition": False}
    lane.fapiPrivateGetOrder.side_effect = ccxt.OrderNotFound("synthetic absent")
    lane.create_order.return_value = {"info": raw()}
    lane.set_markets = lambda markets, currencies=None: None
    obj._close_ex = lane
    return obj, main, lane


@pytest.mark.asyncio
async def test_close_requests_bypass_the_shared_rate_limit_queue() -> None:
    obj, main, lane = with_close_lane()
    await obj.place_close("SYNTHUSDT", "sell", 10, "dc-synthetic")
    lane.create_order.assert_awaited_once()
    lane.fapiPrivateGetOrder.assert_awaited_once()
    main.create_order.assert_not_awaited()
    main.fapiPrivateGetOrder.assert_not_awaited()
    main.fapiPrivateGetPositionSideDual.assert_not_awaited()
    lane.fetch_positions.return_value = []
    assert await obj.close_positions_evidence() == []
    lane.fetch_positions.assert_awaited_once()
    main.fetch_positions.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(("ttl", "reads"), [(0.0, 2), (300.0, 1)])
async def test_close_mode_check_reuses_a_cached_one_way_answer(ttl: float, reads: int) -> None:
    obj, _, lane = with_close_lane()
    obj.position_mode_cache_s = ttl
    obj._mode_clock = lambda: 1000.0
    obj.forget_position_mode()
    await obj.place_close("SYNTHUSDT", "sell", 10, "dc-synthetic")
    await obj.place_close("SYNTHUSDT", "sell", 10, "dc-synthetic")
    assert lane.fapiPrivateGetPositionSideDual.await_count == reads


@pytest.mark.asyncio
async def test_close_lane_has_its_own_bucket_and_same_environment() -> None:
    obj = CcxtBinanceFutures("k", "s", testnet=True)
    try:
        assert obj._close_ex is not obj._ex
        assert obj._close_ex.throttler is not obj._ex.throttler
        assert obj._close_ex.urls["api"] == obj._ex.urls["api"]
    finally:
        await obj.close()
