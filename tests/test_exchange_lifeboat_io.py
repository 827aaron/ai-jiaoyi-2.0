"""新托管流程的保命单身份、未知提交和父/子清理；纯离线合成证据。"""

from __future__ import annotations

import asyncio
from typing import Any

import ccxt.async_support as ccxt
import pytest

from jiaoyi.execution.exchange import (
    BracketSubmissionRejected,
    BracketSubmissionUnknown,
    ExchangeError,
)
from tests.test_exchange_brackets_io import child, client, lb


@pytest.mark.parametrize("side", ["buy", "sell"])
async def test_new_lifeboat_request_has_owned_cid_and_mark_price(side: Any) -> None:
    obj, ex = client()
    ex.create_order.return_value = {"info": lb(side=side.upper())}
    order = await obj.place_lifeboat("SYNTHUSDT", side, 10, 0.9, "lb-position")
    assert order.kind == "stop_loss" and order.child_state == "absent"
    assert order.client_order_id == "lb-position" and order.working_type == "MARK_PRICE"
    ex.create_order.assert_awaited_once_with(
        "SYNTH/USDT:USDT",
        "market",
        side,
        "10.0",
        None,
        {
            "stopLossPrice": 0.9,
            "reduceOnly": True,
            "workingType": "MARK_PRICE",
            "priceProtect": False,
            "positionSide": "BOTH",
            "newClientOrderId": "lb-position",
        },
    )
    ex.fapiPrivateDeleteAlgoOrder.assert_not_awaited()


@pytest.mark.parametrize("cid", ["br-sl-position", "manual-position", "lb-", "lb-" + "x" * 34])
async def test_lifeboat_api_cannot_use_foreign_namespace(cid: str) -> None:
    obj, ex = client()
    with pytest.raises(ValueError):
        await obj.place_lifeboat("SYNTHUSDT", "sell", 10, 0.9, cid)
    with pytest.raises(ValueError):
        await obj.get_lifeboat("SYNTHUSDT", client_order_id=cid)
    with pytest.raises(ValueError):
        await obj.cancel_lifeboat("SYNTHUSDT", "17", expected_client_order_id=cid)
    ex.create_order.assert_not_awaited()
    ex.fapiPrivateGetAlgoOrder.assert_not_awaited()
    ex.fapiPrivateDeleteAlgoOrder.assert_not_awaited()


@pytest.mark.parametrize(
    "error",
    [
        ccxt.RequestTimeout("timeout"),
        ccxt.InvalidOrder("503 unknown"),
        ccxt.InvalidOrder("-4116 duplicate"),
        RuntimeError("unknown"),
    ],
)
async def test_lifeboat_unknown_only_queries_cid_and_cannot_be_resent(error: Exception) -> None:
    obj, ex = client()
    ex.create_order.side_effect = error
    ex.fapiPrivateGetAlgoOrder.side_effect = [ccxt.OrderNotFound("before"), lb()]
    with pytest.raises(BracketSubmissionUnknown) as caught:
        await obj.place_lifeboat("SYNTHUSDT", "sell", 10, 0.9, "lb-position")
    assert caught.value.client_order_id == "lb-position"
    assert caught.value.observed is not None and caught.value.observed.algo_id == "17"
    ex.fapiPrivateGetAlgoOrder.side_effect = ccxt.OrderNotFound("after unknown")
    with pytest.raises(BracketSubmissionUnknown):
        await obj.place_lifeboat("SYNTHUSDT", "sell", 10, 0.9, "lb-position")
    ex.create_order.assert_awaited_once()


async def test_lifeboat_explicit_rejection_is_distinct_without_market_compensation() -> None:
    obj, ex = client()
    ex.create_order.side_effect = ccxt.InvalidOrder("-2021 synthetic")
    with pytest.raises(BracketSubmissionRejected) as caught:
        await obj.place_lifeboat("SYNTHUSDT", "sell", 10, 0.9, "lb-position")
    assert caught.value.immediate_trigger
    ex.create_order.assert_awaited_once()
    ex.fapiPrivateDeleteAlgoOrder.assert_not_awaited()


@pytest.mark.parametrize(
    "change",
    [
        {"workingType": "CONTRACT_PRICE"},
        {"quantity": "9"},
        {"triggerPrice": "0.89"},
        {"clientAlgoId": "lb-another"},
        {"closePosition": True},
        {"reduceOnly": False},
    ],
)
async def test_lifeboat_bad_ack_is_unknown(change: dict[str, Any]) -> None:
    obj, ex = client()
    ex.create_order.return_value = {"info": lb() | change}
    with pytest.raises(BracketSubmissionUnknown):
        await obj.place_lifeboat("SYNTHUSDT", "sell", 10, 0.9, "lb-position")
    ex.create_order.assert_awaited_once()


async def test_lifeboat_existing_terminal_cid_cannot_start_a_new_generation() -> None:
    obj, ex = client()
    ex.fapiPrivateGetAlgoOrder.side_effect = None
    ex.fapiPrivateGetAlgoOrder.return_value = lb(algoStatus="CANCELED")
    observed = await obj.place_lifeboat("SYNTHUSDT", "sell", 10, 0.9, "lb-position")
    assert observed.algo_status == "CANCELED"
    ex.create_order.assert_not_awaited()
    with pytest.raises(ExchangeError):
        await obj.place_lifeboat("SYNTHUSDT", "sell", 10, 0.8, "lb-position")
    ex.create_order.assert_not_awaited()


@pytest.mark.parametrize(
    "change",
    [
        {"symbol": "OTHERUSDT"},
        {"clientAlgoId": "lb-other"},
        {"algoId": "18"},
        {"workingType": "CONTRACT_PRICE"},
        {"orderType": "TAKE_PROFIT_MARKET"},
        {"positionSide": "LONG"},
        {"priceProtect": True},
        {"closePosition": True},
        {"reduceOnly": False},
        {"reduceOnly": None},
    ],
)
async def test_get_lifeboat_binds_all_identity_and_protection_fields(
    change: dict[str, Any],
) -> None:
    obj, ex = client()
    ex.fapiPrivateGetAlgoOrder.side_effect = None
    ex.fapiPrivateGetAlgoOrder.return_value = lb() | change
    with pytest.raises(ExchangeError):
        await obj.get_lifeboat("SYNTHUSDT", client_order_id="lb-position", algo_id="17")
    ex.create_order.assert_not_awaited()


async def test_lifeboat_child_partial_and_terminal_partial_are_not_parent_completion() -> None:
    obj, ex = client()
    ex.fapiPrivateGetAlgoOrder.side_effect = None
    ex.fapiPrivateGetAlgoOrder.return_value = lb(algoStatus="FINISHED", actualOrderId="28")
    ex.fapiPrivateGetOrder.return_value = child()
    observed = await obj.get_lifeboat("SYNTHUSDT", client_order_id="lb-position", algo_id="17")
    assert observed is not None and observed.child_state == "identified"
    assert observed.filled_quantity == 3 and observed.actual_order_status == "PARTIALLY_FILLED"
    with pytest.raises(ExchangeError):
        await obj.cancel_lifeboat("SYNTHUSDT", "17", expected_client_order_id="lb-position")
    ex.fapiPrivateDeleteAlgoOrder.assert_not_awaited()
    ex.fapiPrivateGetOrder.return_value = child(status="CANCELED")
    await obj.cancel_lifeboat("SYNTHUSDT", "17", expected_client_order_id="lb-position")
    ex.fapiPrivateDeleteAlgoOrder.assert_not_awaited()


@pytest.mark.parametrize(
    "actual,status,expected",
    [
        ("0", "NEW", "absent"),
        (None, "NEW", "unknown"),
        ("", "NEW", "unknown"),
        ("0", "FINISHED", "unknown"),
    ],
)
async def test_lifeboat_missing_child_never_means_no_child(
    actual: Any, status: str, expected: str
) -> None:
    obj, ex = client()
    ex.fapiPrivateGetAlgoOrder.side_effect = None
    ex.fapiPrivateGetAlgoOrder.return_value = lb(actualOrderId=actual, algoStatus=status)
    observed = await obj.get_lifeboat("SYNTHUSDT", client_order_id="lb-position")
    assert (
        observed is not None
        and observed.child_state == expected
        and observed.filled_quantity is None
    )


async def test_lifeboat_child_identity_and_absence_are_strict() -> None:
    obj, ex = client()
    ex.fapiPrivateGetAlgoOrder.side_effect = None
    ex.fapiPrivateGetAlgoOrder.return_value = lb(algoStatus="TRIGGERED", actualOrderId="28")
    for response in [child(orderId="29"), child(side="BUY"), ccxt.OrderNotFound("child missing")]:
        ex.fapiPrivateGetOrder.side_effect = [response]
        with pytest.raises(ExchangeError):
            await obj.get_lifeboat("SYNTHUSDT", client_order_id="lb-position")


async def test_cancel_lifeboat_needs_exact_ack_and_actual_terminal_readback() -> None:
    obj, ex = client()
    ex.fapiPrivateGetAlgoOrder.side_effect = [lb(), lb(algoStatus="CANCELED")]
    ex.fapiPrivateDeleteAlgoOrder.return_value = {
        "algoId": "17",
        "clientAlgoId": "lb-position",
        "code": 200,
    }
    await obj.cancel_lifeboat("SYNTHUSDT", "17", expected_client_order_id="lb-position")
    ex.fapiPrivateDeleteAlgoOrder.assert_awaited_once_with({"algoId": "17"})
    assert ex.fapiPrivateGetAlgoOrder.await_count == 2


async def test_cancel_lifeboat_initial_or_post_delete_not_found_remains_unknown() -> None:
    obj, ex = client()
    with pytest.raises(ExchangeError):
        await obj.cancel_lifeboat("SYNTHUSDT", "17", expected_client_order_id="lb-position")
    ex.fapiPrivateDeleteAlgoOrder.assert_not_awaited()
    ex.fapiPrivateGetAlgoOrder.side_effect = [lb(), ccxt.OrderNotFound("post-delete unknown")]
    ex.fapiPrivateDeleteAlgoOrder.return_value = {
        "algoId": "17",
        "clientAlgoId": "lb-position",
        "code": 200,
    }
    with pytest.raises(ExchangeError):
        await obj.cancel_lifeboat("SYNTHUSDT", "17", expected_client_order_id="lb-position")
    ex.fapiPrivateDeleteAlgoOrder.assert_awaited_once()


async def test_cancel_lifeboat_lost_ack_queries_without_redelete_or_hiding_live_child() -> None:
    obj, ex = client()
    ex.fapiPrivateGetAlgoOrder.side_effect = [lb(), lb(algoStatus="TRIGGERED", actualOrderId="28")]
    ex.fapiPrivateGetOrder.return_value = child()
    ex.fapiPrivateDeleteAlgoOrder.side_effect = ccxt.RequestTimeout("synthetic")
    with pytest.raises(ExchangeError):
        await obj.cancel_lifeboat("SYNTHUSDT", "17", expected_client_order_id="lb-position")
    ex.fapiPrivateDeleteAlgoOrder.assert_awaited_once()


async def test_lifeboat_concurrent_calls_and_cancellation_keep_one_post() -> None:
    obj, ex = client()
    entered = asyncio.Event()

    async def stalled(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        await asyncio.Event().wait()

    ex.create_order.side_effect = stalled
    task = asyncio.create_task(obj.place_lifeboat("SYNTHUSDT", "sell", 10, 0.9, "lb-position"))
    await entered.wait()
    next_task = asyncio.create_task(obj.place_lifeboat("SYNTHUSDT", "sell", 10, 0.9, "lb-position"))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(BracketSubmissionUnknown):
        await next_task
    ex.create_order.assert_awaited_once()
