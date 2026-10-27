"""普通 SL/TP 低层纯离线验收；所有账户、订单及网络边界均为合成假件。"""

from __future__ import annotations

import asyncio
import copy
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Any
from unittest.mock import AsyncMock

import ccxt.async_support as ccxt
import pytest

from jiaoyi.execution.exchange import (
    BracketSubmissionRejected,
    BracketSubmissionUnknown,
    CcxtBinanceFutures,
    ExchangeError,
    NormalizedBracketSpec,
)


def raw(kind: str = "stop_loss", **changes: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "symbol": "SYNTHUSDT",
        "algoId": "17",
        "clientAlgoId": "br-sl-synthetic",
        "orderType": "STOP_MARKET" if kind == "stop_loss" else "TAKE_PROFIT_MARKET",
        "side": "SELL",
        "quantity": "10",
        "triggerPrice": "0.9",
        "positionSide": "BOTH",
        "workingType": "CONTRACT_PRICE",
        "reduceOnly": True,
        "priceProtect": False,
        "closePosition": False,
        "algoStatus": "NEW",
        "createTime": 1793100000000,
        "triggerTime": 0,
        # Binance Query Algo Order / openAlgoOrders use an empty string before triggering.
        "actualOrderId": "",
    }
    if kind == "take_profit":
        item.update(clientAlgoId="br-tp-synthetic", triggerPrice="1.1")
    item.update(changes)
    return item


def child(**changes: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "symbol": "SYNTHUSDT",
        "orderId": "28",
        "clientOrderId": "exchange-generated-child",
        "side": "SELL",
        "type": "MARKET",
        "origType": "STOP_MARKET",
        "positionSide": "BOTH",
        "reduceOnly": True,
        "origQty": "10",
        "executedQty": "3",
        "status": "PARTIALLY_FILLED",
    }
    item.update(changes)
    return item


def client() -> tuple[CcxtBinanceFutures, Any]:
    obj = CcxtBinanceFutures.__new__(CcxtBinanceFutures)
    obj._markets_loaded = True
    ex = AsyncMock()
    ex.amount_to_precision = lambda symbol, qty: str(qty)
    ex.price_to_precision = lambda symbol, price: str(price)
    ex.fapiPrivateGetPositionSideDual.return_value = {"dualSidePosition": False}
    ex.fapiPrivateGetAlgoOrder.side_effect = ccxt.OrderNotFound("synthetic absent")
    ex.create_order.return_value = {"info": raw()}
    ex.fapiPrivateDeleteAlgoOrder.return_value = {
        "algoId": "17",
        "clientAlgoId": "br-sl-synthetic",
        "code": "200",
    }
    obj._ex = ex
    return obj, ex


async def place(obj: CcxtBinanceFutures) -> Any:
    return await obj.place_bracket(
        "SYNTHUSDT", "sell", 10, 0.9, "br-sl-synthetic", kind="stop_loss"
    )


@pytest.mark.parametrize(
    "kind,field,price,cid,side",
    [
        ("stop_loss", "stopLossPrice", 0.9, "br-sl-synthetic", "sell"),
        ("take_profit", "takeProfitPrice", 1.1, "br-tp-synthetic", "sell"),
        ("stop_loss", "stopLossPrice", 1.1, "br-sl-synthetic", "buy"),
        ("take_profit", "takeProfitPrice", 0.9, "br-tp-synthetic", "buy"),
    ],
)
async def test_submit_explicit_price_source_and_role(
    kind: Any, field: str, price: float, cid: str, side: Any
) -> None:
    obj, ex = client()
    ex.create_order.return_value = {"info": raw(kind, side=side.upper(), triggerPrice=str(price))}
    order = await obj.place_bracket("SYNTHUSDT", side, 10, price, cid, kind=kind)
    ex.create_order.assert_awaited_once_with(
        "SYNTH/USDT:USDT",
        "market",
        side,
        "10.0",
        None,
        {
            field: price,
            "reduceOnly": True,
            "workingType": "CONTRACT_PRICE",
            "priceProtect": False,
            "positionSide": "BOTH",
            "newClientOrderId": cid,
        },
    )
    assert order.algo_status == "NEW" and order.filled_quantity is None
    assert order.actual_order_id is None and order.triggered_at is None
    assert order.child_state == "absent"
    ex.fapiPrivateDeleteAlgoOrder.assert_not_awaited()


async def test_normalized_spec_is_persistable_and_used_for_request_and_readback() -> None:
    obj, ex = client()
    ex.amount_to_precision = lambda symbol, qty: str(
        Decimal(str(qty)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    )
    ex.price_to_precision = lambda symbol, price: str(
        Decimal(str(price)).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
    )
    spec = await obj.normalized_bracket_spec("SYNTHUSDT", 10.129, 0.91256)
    assert spec == NormalizedBracketSpec(10.12, 0.913)
    assert await obj.normalized_bracket_spec("SYNTHUSDT", spec.quantity, spec.trigger_price) == spec
    ex.create_order.return_value = {"info": raw(quantity="10.12", triggerPrice="0.913")}
    observed = await obj.place_bracket(
        "SYNTHUSDT", "sell", spec.quantity, spec.trigger_price, "br-sl-synthetic", kind="stop_loss"
    )
    assert observed.quantity == spec.quantity and observed.trigger_price == spec.trigger_price
    args = ex.create_order.await_args.args
    assert args[3] == "10.12" and args[5]["stopLossPrice"] == 0.913


@pytest.mark.parametrize("mode", [{"dualSidePosition": True}, {}, {"dualSidePosition": 0}, None])
async def test_mode_must_be_known_one_way(mode: Any) -> None:
    obj, ex = client()
    ex.fapiPrivateGetPositionSideDual.return_value = mode
    with pytest.raises(ExchangeError):
        await place(obj)
    ex.create_order.assert_not_awaited()


@pytest.mark.parametrize(
    "qty,price,cid,side,kind",
    [
        (0, 0.9, "br-sl-x", "sell", "stop_loss"),
        (-1, 0.9, "br-sl-x", "sell", "stop_loss"),
        (True, 0.9, "br-sl-x", "sell", "stop_loss"),
        (float("nan"), 0.9, "br-sl-x", "sell", "stop_loss"),
        (10, float("inf"), "br-sl-x", "sell", "stop_loss"),
        (10, 0, "br-sl-x", "sell", "stop_loss"),
        (10, 0.9, "lb-x", "sell", "stop_loss"),
        (10, 0.9, "br-tp-x", "sell", "stop_loss"),
        (10, 0.9, "br-sl-", "sell", "stop_loss"),
        (10, 0.9, "br-sl-" + "x" * 31, "sell", "stop_loss"),
        (10, 0.9, "br-sl-x", "bad", "stop_loss"),
        (10, 0.9, "br-sl-x", "sell", "trailing"),
    ],
)
async def test_invalid_inputs_never_send(
    qty: Any, price: Any, cid: str, side: Any, kind: Any
) -> None:
    obj, ex = client()
    with pytest.raises(ValueError):
        await obj.place_bracket("SYNTHUSDT", side, qty, price, cid, kind=kind)
    ex.create_order.assert_not_awaited()


@pytest.mark.parametrize("value", ["0", "NaN", "Infinity", "0.123456789012345678901", "11"])
async def test_bad_normalized_quantity_never_creates(value: str) -> None:
    obj, ex = client()
    ex.amount_to_precision = lambda symbol, qty: value
    with pytest.raises(ValueError):
        await place(obj)
    ex.create_order.assert_not_awaited()


@pytest.mark.parametrize("value", ["0", "NaN", "Infinity", "0.123456789012345678901"])
async def test_bad_normalized_price_never_creates(value: str) -> None:
    obj, ex = client()
    ex.price_to_precision = lambda symbol, price: value
    with pytest.raises(ValueError):
        await place(obj)
    ex.create_order.assert_not_awaited()


@pytest.mark.parametrize(
    "error",
    [
        ccxt.RequestTimeout("synthetic"),
        ccxt.ExchangeNotAvailable("503 synthetic"),
        ccxt.ExchangeError("-1007 synthetic"),
        ccxt.InvalidOrder("503 synthetic"),
        ccxt.InvalidOrder("Duplicate order sent"),
        ccxt.InvalidOrder("-4116 synthetic"),
        ccxt.RateLimitExceeded("429 synthetic"),
        RuntimeError("unexpected failure"),
    ],
)
async def test_unknown_submission_queries_original_cid_and_latches_no_resend(
    error: Exception,
) -> None:
    obj, ex = client()
    ex.create_order.side_effect = error
    ex.fapiPrivateGetAlgoOrder.side_effect = [ccxt.OrderNotFound("before"), raw()]
    with pytest.raises(BracketSubmissionUnknown) as caught:
        await place(obj)
    assert caught.value.client_order_id == "br-sl-synthetic"
    assert caught.value.observed is not None and caught.value.observed.algo_id == "17"
    assert ex.fapiPrivateGetAlgoOrder.await_count == 2
    ex.fapiPrivateGetAlgoOrder.side_effect = ccxt.OrderNotFound("after unknown")
    with pytest.raises(BracketSubmissionUnknown):
        await place(obj)
    ex.create_order.assert_awaited_once()


@pytest.mark.parametrize("code,immediate", [("-2021", True), ("-4142", True), ("-2022", False)])
async def test_clear_rejection_does_not_cancel_or_market_fallback(
    code: str, immediate: bool
) -> None:
    obj, ex = client()
    ex.create_order.side_effect = ccxt.InvalidOrder(f"{code} synthetic")
    with pytest.raises(BracketSubmissionRejected) as caught:
        await place(obj)
    assert caught.value.immediate_trigger is immediate
    ex.create_order.assert_awaited_once()
    assert ex.fapiPrivateGetAlgoOrder.await_count == 1
    ex.fapiPrivateDeleteAlgoOrder.assert_not_awaited()


@pytest.mark.parametrize(
    "change",
    [
        {"symbol": "OTHERUSDT"},
        {"clientAlgoId": "br-sl-other"},
        {"side": "BUY"},
        {"workingType": "MARK_PRICE"},
        {"reduceOnly": False},
        {"priceProtect": True},
        {"positionSide": "LONG"},
        {"closePosition": True},
        {"quantity": "9"},
        {"triggerPrice": "0.901"},
        {"algoId": "0"},
        {"quantity": True},
    ],
)
async def test_bad_ack_is_unknown_not_success(change: dict[str, Any]) -> None:
    obj, ex = client()
    ex.create_order.return_value = {"info": raw(**change)}
    with pytest.raises(BracketSubmissionUnknown):
        await place(obj)
    ex.create_order.assert_awaited_once()


@pytest.mark.parametrize(
    "change",
    [{"triggerPrice": "0.901"}, {"workingType": "MARK_PRICE"}, {"quantity": "9"}, {"side": "BUY"}],
)
async def test_existing_same_cid_different_semantics_never_creates(change: dict[str, Any]) -> None:
    obj, ex = client()
    ex.fapiPrivateGetAlgoOrder.side_effect = None
    ex.fapiPrivateGetAlgoOrder.return_value = raw(**change)
    with pytest.raises(ExchangeError, match="不一致"):
        await place(obj)
    ex.create_order.assert_not_awaited()


async def test_existing_terminal_same_cid_is_not_recreated() -> None:
    obj, ex = client()
    ex.fapiPrivateGetAlgoOrder.side_effect = None
    ex.fapiPrivateGetAlgoOrder.return_value = raw(algoStatus="CANCELED")
    order = await place(obj)
    assert order.algo_status == "CANCELED"
    ex.create_order.assert_not_awaited()


async def test_preflight_query_error_is_not_missing_and_cannot_create() -> None:
    obj, ex = client()
    ex.fapiPrivateGetAlgoOrder.side_effect = ccxt.RequestTimeout("synthetic")
    with pytest.raises(ExchangeError):
        await place(obj)
    ex.create_order.assert_not_awaited()


@pytest.mark.parametrize("status", ["NEW", "CANCELED", "REJECTED", "EXPIRED"])
async def test_explicit_untriggered_child_is_distinct_from_missing_or_null(status: str) -> None:
    obj, ex = client()
    ex.fapiPrivateGetAlgoOrder.side_effect = None
    for actual, expected in [("0", "absent"), (0, "absent"), (None, "unknown"), ("", "absent")]:
        ex.fapiPrivateGetAlgoOrder.return_value = raw(algoStatus=status, actualOrderId=actual)
        order = await obj.get_bracket("SYNTHUSDT", algo_id="17")
        assert order is not None and order.child_state == expected
        assert order.filled_quantity is None
    absent = raw(algoStatus=status)
    absent.pop("actualOrderId")
    ex.fapiPrivateGetAlgoOrder.return_value = absent
    order = await obj.get_bracket("SYNTHUSDT", algo_id="17")
    assert order is not None and order.child_state == "unknown"
    ex.fapiPrivateGetOrder.assert_not_awaited()


@pytest.mark.parametrize("actual", ["", "0", 0])
@pytest.mark.parametrize(
    "change",
    [
        {"algoStatus": "TRIGGERING"},
        {"algoStatus": "TRIGGERED"},
        {"algoStatus": "FINISHED"},
        {"triggerTime": 1793100001000},
        {"algoStatus": "CANCELED", "triggerTime": 1793100001000},
        {"triggerTime": None},
        {"algoStatus": "FUTURE_UNKNOWN_STATUS"},
    ],
)
async def test_trigger_or_missing_clock_overrides_no_child_sentinel(
    actual: str | int, change: dict[str, Any]
) -> None:
    obj, ex = client()
    ex.fapiPrivateGetAlgoOrder.side_effect = None
    ex.fapiPrivateGetAlgoOrder.return_value = raw(actualOrderId=actual, **change)
    order = await obj.get_bracket("SYNTHUSDT", algo_id="17")
    assert order is not None and order.child_state == "unknown" and order.filled_quantity is None


async def test_actual_child_partial_fill_is_verified_by_pointer_not_parent_cid() -> None:
    obj, ex = client()
    ex.fapiPrivateGetAlgoOrder.side_effect = None
    ex.fapiPrivateGetAlgoOrder.return_value = raw(
        algoStatus="TRIGGERED", actualOrderId="28", triggerTime=1793100001000, executedQty="10"
    )
    ex.fapiPrivateGetOrder.return_value = child()
    order = await obj.get_bracket("SYNTHUSDT", algo_id="17")
    assert order is not None and order.child_state == "identified"
    assert order.actual_order_id == "28" and order.filled_quantity == 3
    assert order.actual_order_status == "PARTIALLY_FILLED"
    ex.fapiPrivateGetOrder.assert_awaited_once_with({"symbol": "SYNTHUSDT", "orderId": "28"})


@pytest.mark.parametrize(
    "change",
    [
        {"symbol": "OTHERUSDT"},
        {"orderId": "29"},
        {"orderId": "0"},
        {"side": "BUY"},
        {"positionSide": "LONG"},
        {"reduceOnly": False},
        {"reduceOnly": "false"},
        {"origQty": None},
        {"origQty": "9"},
        {"executedQty": None},
        {"executedQty": "NaN"},
        {"executedQty": "11"},
        {"status": "FILLED"},
        {"status": "NEW"},
        {"status": "future"},
        {"type": "LIMIT"},
        {"origType": "TAKE_PROFIT_MARKET"},
        {"clientOrderId": None},
    ],
)
async def test_child_wrong_identity_or_incomplete_quantity_is_unknown(
    change: dict[str, Any],
) -> None:
    obj, ex = client()
    ex.fapiPrivateGetAlgoOrder.side_effect = None
    ex.fapiPrivateGetAlgoOrder.return_value = raw(algoStatus="FINISHED", actualOrderId="28")
    ex.fapiPrivateGetOrder.return_value = child(**change)
    with pytest.raises(ExchangeError, match="证据不完整"):
        await obj.get_bracket("SYNTHUSDT", algo_id="17")
    ex.create_order.assert_not_awaited()


async def test_child_not_found_is_unknown_not_algo_missing() -> None:
    obj, ex = client()
    ex.fapiPrivateGetAlgoOrder.side_effect = None
    ex.fapiPrivateGetAlgoOrder.return_value = raw(algoStatus="TRIGGERED", actualOrderId="28")
    ex.fapiPrivateGetOrder.side_effect = ccxt.OrderNotFound("child absent")
    with pytest.raises(ExchangeError):
        await obj.get_bracket("SYNTHUSDT", algo_id="17")


async def test_get_exact_symbol_cid_and_algo_identity() -> None:
    obj, ex = client()
    ex.fapiPrivateGetAlgoOrder.side_effect = None
    for change, query in [
        ({"algoId": "18"}, {"algo_id": "17"}),
        ({"clientAlgoId": "br-sl-other"}, {"client_order_id": "br-sl-synthetic"}),
        ({"symbol": "OTHERUSDT"}, {"algo_id": "17"}),
    ]:
        ex.fapiPrivateGetAlgoOrder.return_value = raw(**change)
        with pytest.raises(ExchangeError):
            await obj.get_bracket("SYNTHUSDT", **query)


@pytest.mark.parametrize("actual", [False, True, " ", " 0 ", "-1", "abc", "17.5"])
async def test_malformed_child_identity_is_not_absent(actual: Any) -> None:
    obj, ex = client()
    ex.fapiPrivateGetAlgoOrder.side_effect = None
    ex.fapiPrivateGetAlgoOrder.return_value = raw(actualOrderId=actual)
    with pytest.raises(ExchangeError):
        await obj.get_bracket("SYNTHUSDT", algo_id="17")


async def test_open_roles_do_not_include_lifeboat_or_imply_child_fill() -> None:
    obj, ex = client()
    ex.fapiPrivateGetOpenAlgoOrders.return_value = [
        raw(),
        raw("take_profit", algoId="18"),
        raw(clientAlgoId="lb-x", algoId="19"),
    ]
    found = await obj.open_brackets()
    assert [o.kind for o in found] == ["stop_loss", "take_profit"]
    assert all(o.filled_quantity is None for o in found)
    ex.fapiPrivateGetOrder.assert_not_awaited()


@pytest.mark.parametrize(
    "response",
    [{}, [None], [raw(), raw()], [raw(reduceOnly=None)], [raw(clientAlgoId="br-tp-wrong-role")]],
)
async def test_bad_open_list_is_not_empty_or_partial_success(response: Any) -> None:
    obj, ex = client()
    ex.fapiPrivateGetOpenAlgoOrders.return_value = response
    with pytest.raises(ExchangeError):
        await obj.open_brackets()


async def test_entry_clear_checks_all_normal_and_algo_orders_without_role_filter() -> None:
    obj, ex = client()
    ex.fapiPrivateGetOpenOrders.return_value = []
    ex.fapiPrivateGetOpenAlgoOrders.return_value = []
    await obj.assert_entry_clear("SYNTHUSDT")
    ex.fapiPrivateGetOpenOrders.assert_awaited_once_with({"symbol": "SYNTHUSDT"})
    ex.fapiPrivateGetOpenAlgoOrders.assert_awaited_once_with({"symbol": "SYNTHUSDT"})
    ex.fapiPrivateDeleteAlgoOrder.assert_not_awaited()
    ex.create_order.assert_not_awaited()


@pytest.mark.parametrize(
    "normal,algo",
    [
        ([{"clientOrderId": "manual-entry"}], []),
        ([], [raw(clientAlgoId="lb-old")]),
        ([], [raw(clientAlgoId="manual-exit")]),
        ([], [raw()]),
        (None, []),
        ([], None),
        ({"orders": [], "hasMore": False}, []),
        ([], {"orders": [], "nextCursor": "page2"}),
        ([None], []),
        ([], [{}]),
    ],
)
async def test_entry_clear_never_accepts_nonempty_partial_or_unknown(
    normal: Any, algo: Any
) -> None:
    obj, ex = client()
    ex.fapiPrivateGetOpenOrders.return_value = normal
    ex.fapiPrivateGetOpenAlgoOrders.return_value = algo
    with pytest.raises(ExchangeError, match="禁止新开"):
        await obj.assert_entry_clear("SYNTHUSDT")
    ex.fapiPrivateDeleteAlgoOrder.assert_not_awaited()
    ex.create_order.assert_not_awaited()


@pytest.mark.parametrize("method", ["fapiPrivateGetOpenOrders", "fapiPrivateGetOpenAlgoOrders"])
async def test_entry_clear_query_failure_is_not_empty(method: str) -> None:
    obj, ex = client()
    ex.fapiPrivateGetOpenOrders.return_value = []
    ex.fapiPrivateGetOpenAlgoOrders.return_value = []
    getattr(ex, method).side_effect = ccxt.RequestTimeout("offline synthetic")
    with pytest.raises(ExchangeError, match="检查失败"):
        await obj.assert_entry_clear("SYNTHUSDT")


def lb(**changes: Any) -> dict[str, Any]:
    return raw(clientAlgoId="lb-position", workingType="MARK_PRICE", **changes)


async def test_lifeboat_ready_uses_exact_owned_raw_evidence_and_covers_actual_quantity() -> None:
    obj, ex = client()
    ex.fapiPrivateGetAlgoOrder.side_effect = [lb(), ccxt.OrderNotFound("other slot absent")]
    assert await obj.lifeboat_ready("SYNTHUSDT", ("lb-position", "lb-position-r"), "sell", 9, 0.9)
    assert [c.args for c in ex.fapiPrivateGetAlgoOrder.await_args_list] == [
        ({"clientAlgoId": "lb-position"},),
        ({"clientAlgoId": "lb-position-r"},),
    ]
    ex.fapiPrivateDeleteAlgoOrder.assert_not_awaited()
    ex.create_order.assert_not_awaited()


async def test_lifeboat_ready_does_not_round_actual_position_down_to_claim_coverage() -> None:
    obj, ex = client()
    ex.amount_to_precision = lambda symbol, qty: "10"
    ex.fapiPrivateGetAlgoOrder.side_effect = None
    ex.fapiPrivateGetAlgoOrder.return_value = lb()
    assert not await obj.lifeboat_ready("SYNTHUSDT", ("lb-position",), "sell", 10.001, 0.9)


@pytest.mark.parametrize(
    "response",
    [
        lb(quantity="9"),
        lb(triggerPrice="0.89"),
        lb(algoStatus="CANCELED"),
        ccxt.OrderNotFound("absent"),
    ],
)
async def test_lifeboat_known_no_cover_is_false(response: Any) -> None:
    obj, ex = client()
    ex.fapiPrivateGetAlgoOrder.side_effect = [response]
    assert not await obj.lifeboat_ready("SYNTHUSDT", ("lb-position",), "sell", 10, 0.9)


@pytest.mark.parametrize(
    "change",
    [
        {"symbol": "OTHERUSDT"},
        {"side": "BUY"},
        {"quantity": None},
        {"triggerPrice": None},
        {"reduceOnly": None},
        {"reduceOnly": False},
        {"positionSide": "LONG"},
        {"closePosition": True},
        {"priceProtect": True},
        {"actualOrderId": None},
        {"algoStatus": "TRIGGERED"},
        {"triggerTime": 1793100001000},
    ],
)
async def test_lifeboat_missing_or_unsafe_evidence_is_unknown(change: dict[str, Any]) -> None:
    obj, ex = client()
    ex.fapiPrivateGetAlgoOrder.side_effect = None
    ex.fapiPrivateGetAlgoOrder.return_value = lb(**change)
    with pytest.raises(ExchangeError):
        await obj.lifeboat_ready("SYNTHUSDT", ("lb-position",), "sell", 10, 0.9)
    ex.fapiPrivateDeleteAlgoOrder.assert_not_awaited()


async def test_lifeboat_new_cannot_mask_old_cid_unknown_or_triggered() -> None:
    obj, ex = client()
    for other in [
        ccxt.RequestTimeout("synthetic"),
        raw(
            clientAlgoId="lb-position-r",
            workingType="MARK_PRICE",
            algoStatus="FINISHED",
            actualOrderId="28",
        ),
    ]:
        ex.fapiPrivateGetAlgoOrder.side_effect = [lb(), other]
        with pytest.raises(ExchangeError):
            await obj.lifeboat_ready("SYNTHUSDT", ("lb-position", "lb-position-r"), "sell", 10, 0.9)


async def test_lifeboat_cannot_use_bracket_namespace_or_wrong_raw_cid_or_price_source() -> None:
    obj, ex = client()
    with pytest.raises(ValueError):
        await obj.lifeboat_ready("SYNTHUSDT", ("br-sl-position",), "sell", 10, 0.9)
    ex.fapiPrivateGetAlgoOrder.assert_not_awaited()
    for response in [raw(clientAlgoId="lb-position"), lb() | {"clientAlgoId": "lb-other"}]:
        ex.fapiPrivateGetAlgoOrder.side_effect = [response]
        with pytest.raises(ExchangeError):
            await obj.lifeboat_ready("SYNTHUSDT", ("lb-position",), "sell", 10, 0.9)


async def test_cancel_verifies_exact_identity_then_reads_terminal() -> None:
    obj, ex = client()
    ex.fapiPrivateGetAlgoOrder.side_effect = [raw(), raw(algoStatus="CANCELED")]
    await obj.cancel_bracket("SYNTHUSDT", "17", expected_client_order_id="br-sl-synthetic")
    ex.fapiPrivateDeleteAlgoOrder.assert_awaited_once_with({"algoId": "17"})
    assert ex.fapiPrivateGetAlgoOrder.await_count == 2


@pytest.mark.parametrize(
    "response",
    [
        raw(clientAlgoId="br-sl-other-position"),
        raw(clientAlgoId="lb-other"),
        raw(symbol="OTHERUSDT"),
        raw(algoId="18"),
        raw(actualOrderId=None),
        raw(algoStatus="TRIGGERED"),
        raw(triggerTime=1793100001000),
    ],
)
async def test_cancel_unknown_or_wrong_owner_never_sends_delete(response: dict[str, Any]) -> None:
    obj, ex = client()
    ex.fapiPrivateGetAlgoOrder.side_effect = None
    ex.fapiPrivateGetAlgoOrder.return_value = response
    with pytest.raises(ExchangeError):
        await obj.cancel_bracket("SYNTHUSDT", "17", expected_client_order_id="br-sl-synthetic")
    ex.fapiPrivateDeleteAlgoOrder.assert_not_awaited()


async def test_cancel_initial_notfound_is_not_confirmed_cancellation() -> None:
    obj, ex = client()
    with pytest.raises(ExchangeError, match="不能证明已撤"):
        await obj.cancel_bracket("SYNTHUSDT", "17", expected_client_order_id="br-sl-synthetic")
    ex.fapiPrivateDeleteAlgoOrder.assert_not_awaited()


@pytest.mark.parametrize("after", [raw(), raw(algoStatus="TRIGGERED"), ccxt.OrderNotFound("gone")])
async def test_cancel_ack_does_not_prove_terminal_or_no_child(after: Any) -> None:
    obj, ex = client()
    ex.fapiPrivateGetAlgoOrder.side_effect = [raw(), after]
    with pytest.raises(ExchangeError, match="未证实终止"):
        await obj.cancel_bracket("SYNTHUSDT", "17", expected_client_order_id="br-sl-synthetic")
    ex.fapiPrivateDeleteAlgoOrder.assert_awaited_once()


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"algoId": "18", "clientAlgoId": "br-sl-synthetic", "code": 200},
        {"algoId": "17", "clientAlgoId": "br-sl-other", "code": 200},
    ],
)
async def test_cancel_bad_ack_is_unknown(response: Any) -> None:
    obj, ex = client()
    ex.fapiPrivateGetAlgoOrder.side_effect = None
    ex.fapiPrivateGetAlgoOrder.return_value = raw()
    ex.fapiPrivateDeleteAlgoOrder.return_value = response
    with pytest.raises(ExchangeError, match="回执身份"):
        await obj.cancel_bracket("SYNTHUSDT", "17", expected_client_order_id="br-sl-synthetic")
    ex.fapiPrivateDeleteAlgoOrder.assert_awaited_once()


@pytest.mark.parametrize("error", [ccxt.RequestTimeout("timeout"), ccxt.OrderNotFound("raced")])
async def test_cancel_timeout_or_hb_exit_race_only_queries_once(error: Exception) -> None:
    obj, ex = client()
    ex.fapiPrivateDeleteAlgoOrder.side_effect = error
    ex.fapiPrivateGetAlgoOrder.side_effect = [raw(), raw(algoStatus="EXPIRED")]
    await obj.cancel_bracket("SYNTHUSDT", "17", expected_client_order_id="br-sl-synthetic")
    ex.fapiPrivateDeleteAlgoOrder.assert_awaited_once()
    assert ex.fapiPrivateGetAlgoOrder.await_count == 2


async def test_cancel_raced_with_child_partial_keeps_unknown_until_actual_terminal() -> None:
    obj, ex = client()
    triggered = raw(algoStatus="FINISHED", actualOrderId="28", triggerTime=1793100001000)
    ex.fapiPrivateGetAlgoOrder.side_effect = [raw(), triggered]
    ex.fapiPrivateGetOrder.return_value = child(status="PARTIALLY_FILLED", executedQty="3")
    with pytest.raises(ExchangeError):
        await obj.cancel_bracket("SYNTHUSDT", "17", expected_client_order_id="br-sl-synthetic")
    ex.fapiPrivateGetAlgoOrder.side_effect = None
    ex.fapiPrivateGetAlgoOrder.return_value = triggered
    ex.fapiPrivateGetOrder.return_value = child(status="CANCELED", executedQty="3")
    await obj.cancel_bracket("SYNTHUSDT", "17", expected_client_order_id="br-sl-synthetic")
    # 已核实这张 child 终态，只说明它不再工作；不会再次 DELETE 或宣称残仓为零。
    ex.fapiPrivateDeleteAlgoOrder.assert_awaited_once()


async def test_concurrent_duplicate_calls_send_only_once_even_if_followup_get_is_missing() -> None:
    obj, ex = client()
    entered, finish = asyncio.Event(), asyncio.Event()

    async def stalled(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        await finish.wait()
        raise ccxt.RequestTimeout("synthetic")

    ex.create_order.side_effect = stalled
    first = asyncio.create_task(place(obj))
    await entered.wait()
    second = asyncio.create_task(place(obj))
    finish.set()
    result = await asyncio.gather(first, second, return_exceptions=True)
    assert all(isinstance(e, BracketSubmissionUnknown) for e in result)
    ex.create_order.assert_awaited_once()


async def test_cancelled_submit_coroutine_cannot_enable_repeat_post() -> None:
    obj, ex = client()
    entered = asyncio.Event()

    async def stalled(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        await asyncio.Event().wait()

    ex.create_order.side_effect = stalled
    task = asyncio.create_task(place(obj))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(BracketSubmissionUnknown):
        await place(obj)
    ex.create_order.assert_awaited_once()


async def test_unknown_cid_cannot_be_reused_on_another_symbol() -> None:
    obj, ex = client()
    ex.create_order.side_effect = ccxt.RequestTimeout("synthetic")
    with pytest.raises(BracketSubmissionUnknown):
        await place(obj)
    with pytest.raises(BracketSubmissionUnknown):
        await obj.place_bracket("OTHERUSDT", "sell", 10, 0.9, "br-sl-synthetic", kind="stop_loss")
    ex.create_order.assert_awaited_once()


@pytest.mark.parametrize("working_type", ["CONTRACT_PRICE", "MARK_PRICE"])
async def test_real_ccxt_maps_both_types_and_rounding_to_algo_without_network(
    working_type: str,
) -> None:
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
        "info": {"orderTypes": ["MARKET", "STOP_MARKET", "TAKE_PROFIT_MARKET"]},
        "precision": {"amount": 0.01, "price": 0.0001},
        "base": "SYNTH",
        "quote": "USDT",
        "settle": "USDT",
    }
    ex.set_markets([market])
    ex.fetch = AsyncMock(side_effect=AssertionError("offline test cannot use HTTP"))
    captured: list[dict[str, Any]] = []

    async def response(request: dict[str, Any]) -> dict[str, Any]:
        captured.append(copy.deepcopy(request))
        return raw("take_profit" if request["type"] == "TAKE_PROFIT_MARKET" else "stop_loss")

    ex.fapiPrivatePostAlgoOrder = response
    try:
        for _kind, field, cid in [
            ("stop_loss", "stopLossPrice", "br-sl-synthetic"),
            ("take_profit", "takeProfitPrice", "br-tp-synthetic"),
        ]:
            await ex.create_order(
                "SYNTH/USDT:USDT",
                "market",
                "sell",
                10.129,
                None,
                {
                    field: 0.91256,
                    "reduceOnly": True,
                    "workingType": working_type,
                    "priceProtect": False,
                    "positionSide": "BOTH",
                    "newClientOrderId": cid,
                },
            )
        assert [r["type"] for r in captured] == ["STOP_MARKET", "TAKE_PROFIT_MARKET"]
        assert [r["clientAlgoId"] for r in captured] == ["br-sl-synthetic", "br-tp-synthetic"]
        assert all(r["algoType"] == "CONDITIONAL" for r in captured)
        assert all(r["triggerPrice"] == "0.9126" and r["quantity"] == "10.12" for r in captured)
        assert all(r["workingType"] == working_type and r["reduceOnly"] is True for r in captured)
        assert all(r["priceProtect"] is False and r["positionSide"] == "BOTH" for r in captured)
        assert all("callbackRate" not in r and "closePosition" not in r for r in captured)
        ex.fetch.assert_not_awaited()
        assert ex.urls["api"]["fapiPrivate"] == "https://demo-fapi.binance.com/fapi/v1"
        assert ex.urls["api"]["fapiPublic"] == "https://demo-fapi.binance.com/fapi/v1"
    finally:
        await ex.close()


@pytest.mark.parametrize("status", ["REJECTED", "EXPIRED"])
async def test_trigger_failed_without_child_is_absent_only_after_cid_lookup(status: str) -> None:
    obj, ex = client()
    ex.fapiPrivateGetAlgoOrder.side_effect = None
    ex.fapiPrivateGetAlgoOrder.return_value = raw(
        algoStatus=status, triggerTime=1793100001000, actualOrderId=""
    )
    ex.fapiPrivateGetOrder.side_effect = ccxt.OrderNotFound("Order does not exist")
    order = await obj.get_bracket("SYNTHUSDT", algo_id="17")
    assert order is not None and order.child_state == "absent"
    assert order.actual_order_id is None and order.filled_quantity is None
    ex.fapiPrivateGetOrder.assert_awaited_once_with(
        {"symbol": "SYNTHUSDT", "origClientOrderId": "br-sl-synthetic"}
    )
    ex.fapiPrivateGetOrder.side_effect = ccxt.RequestTimeout("synthetic")
    with pytest.raises(ExchangeError, match="证据不完整"):
        await obj.get_bracket("SYNTHUSDT", algo_id="17")


async def test_lifeboat_trigger_failed_without_child_is_absent_only_after_cid_lookup() -> None:
    obj, ex = client()
    ex.fapiPrivateGetAlgoOrder.side_effect = None
    ex.fapiPrivateGetAlgoOrder.return_value = raw(
        clientAlgoId="lb-position",
        workingType="MARK_PRICE",
        algoStatus="REJECTED",
        triggerTime=1793100001000,
    )
    ex.fapiPrivateGetOrder.side_effect = ccxt.OrderNotFound("Order does not exist")
    order = await obj.get_lifeboat("SYNTHUSDT", client_order_id="lb-position")
    assert order is not None and order.child_state == "absent"
    ex.fapiPrivateGetOrder.side_effect = None
    ex.fapiPrivateGetOrder.return_value = child(
        clientOrderId="lb-position", status="NEW", executedQty="0"
    )
    with pytest.raises(ExchangeError, match="证据不完整"):
        await obj.get_lifeboat("SYNTHUSDT", client_order_id="lb-position")
