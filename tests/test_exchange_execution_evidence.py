"""成本与盘口严格证据：合成的原始 Binance 回包，不连接任何环境。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import ccxt.async_support as ccxt
import pytest

from jiaoyi.execution import exchange as module
from jiaoyi.execution.exchange import CcxtBinanceFutures, EntryBasis, ExchangeError

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
CID = "hb-entry-synthetic"


def order(**change: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "symbol": "SYNTHUSDT",
        "clientOrderId": CID,
        "orderId": "17",
        "side": "BUY",
        "type": "LIMIT",
        "positionSide": "BOTH",
        "reduceOnly": False,
        "status": "FILLED",
        "origQty": "10",
        "executedQty": "10",
        "cumQuote": "11",
        "avgPrice": "1.1",
    }
    raw.update(change)
    return raw


def fills() -> list[dict[str, Any]]:
    shared = {
        "symbol": "SYNTHUSDT",
        "orderId": "17",
        "side": "BUY",
        "buyer": True,
        "positionSide": "BOTH",
        "commissionAsset": "USDT",
        "realizedPnl": "0",
        "price": "1.1",
    }
    return [
        {**shared, "id": "0", "qty": "4", "quoteQty": "4.4", "commission": "0.0044"},
        {**shared, "id": "1", "qty": "6", "quoteQty": "6.6", "commission": "0.0066"},
    ]


def book(**change: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "symbol": "SYNTHUSDT",
        "bidPrice": "1.09",
        "askPrice": "1.11",
        "bidQty": "100",
        "askQty": "200",
        "time": int(NOW.timestamp() * 1000) - 100,
    }
    raw.update(change)
    return raw


def client() -> tuple[CcxtBinanceFutures, Any]:
    obj = CcxtBinanceFutures.__new__(CcxtBinanceFutures)
    obj._markets_loaded = True
    ex = AsyncMock()
    ex.fapiPrivateGetPositionSideDual.return_value = {"dualSidePosition": False}
    ex.fapiPrivateGetOrder.return_value = order()
    ex.fapiPrivateGetUserTrades.return_value = fills()
    ex.fapiPublicGetTickerBookTicker.return_value = book()
    obj._ex = ex
    return obj, ex


def freeze(monkeypatch: pytest.MonkeyPatch, *times: datetime) -> None:
    values = iter(times or (NOW, NOW))

    class Clock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return next(values)

    monkeypatch.setattr(module, "datetime", Clock)


async def test_entry_basis_exact_order_fills_and_usdt_fees() -> None:
    obj, ex = client()
    result = await obj.entry_basis("SYNTHUSDT", CID)
    assert result == EntryBasis("SYNTHUSDT", CID, "17", "buy", 10, 11, 0.011, 1.1, "FILLED")
    ex.fapiPrivateGetUserTrades.assert_awaited_once_with(
        {"symbol": "SYNTHUSDT", "orderId": "17", "limit": 1000}
    )
    assert ex.fapiPrivateGetOrder.await_count == 2
    assert all(
        call.args == ({"symbol": "SYNTHUSDT", "origClientOrderId": CID},)
        for call in ex.fapiPrivateGetOrder.await_args_list
    )
    ex.create_order.assert_not_awaited()
    ex.cancel_order.assert_not_awaited()


async def test_short_terminal_partial_entry_is_supported_with_actual_not_requested_quantity() -> (
    None
):
    obj, ex = client()
    ex.fapiPrivateGetOrder.return_value = order(
        side="SELL", type="MARKET", status="CANCELED", origQty="12"
    )
    rows = fills()
    for row in rows:
        row.update(side="SELL", buyer=False)
    ex.fapiPrivateGetUserTrades.return_value = rows
    result = await obj.entry_basis("SYNTHUSDT", CID)
    assert result.side == "sell" and result.filled_quantity == 10
    assert result.order_status == "CANCELED"


@pytest.mark.parametrize("fee,expected", [("0", 0.0), ("-0.001", -0.002)])
async def test_explicit_zero_or_actual_rebate_is_not_missing_fee(fee: str, expected: float) -> None:
    obj, ex = client()
    rows = fills()
    for row in rows:
        row["commission"] = fee
    ex.fapiPrivateGetUserTrades.return_value = rows
    assert (await obj.entry_basis("SYNTHUSDT", CID)).fee_usdt == expected


@pytest.mark.parametrize(
    "change",
    [
        {"symbol": "OTHERUSDT"},
        {"clientOrderId": "other-entry"},
        {"orderId": "0"},
        {"reduceOnly": True},
        {"reduceOnly": None},
        {"positionSide": "LONG"},
        {"type": "STOP_MARKET"},
        {"side": "OTHER"},
        {"status": "PARTIALLY_FILLED"},
        {"status": "NEW"},
        {"executedQty": "0"},
        {"executedQty": "11"},
        {"executedQty": "3"},
        {"executedQty": None},
        {"cumQuote": None},
        {"cumQuote": "NaN"},
    ],
)
async def test_missing_or_unfinished_entry_cannot_supply_basis(change: dict[str, Any]) -> None:
    obj, ex = client()
    ex.fapiPrivateGetOrder.return_value = order(**change)
    with pytest.raises(ExchangeError, match="证据未知"):
        await obj.entry_basis("SYNTHUSDT", CID)
    ex.fapiPrivateGetUserTrades.assert_not_awaited()


@pytest.mark.parametrize(
    "change",
    [
        {"symbol": "OTHERUSDT"},
        {"orderId": "18"},
        {"id": "1"},
        {"id": None},
        {"side": "SELL"},
        {"buyer": False},
        {"buyer": None},
        {"positionSide": "SHORT"},
        {"commission": None},
        {"commission": "NaN"},
        {"commission": True},
        {"commissionAsset": "BNB"},
        {"realizedPnl": None},
        {"realizedPnl": "1"},
        {"qty": "3"},
        {"quoteQty": "4.3"},
        {"qty": None},
        {"quoteQty": None},
        {"price": "NaN"},
    ],
)
async def test_fill_identity_fee_and_coverage_missing_cannot_default_to_zero(
    change: dict[str, Any],
) -> None:
    obj, ex = client()
    rows = fills()
    rows[0].update(change)
    ex.fapiPrivateGetUserTrades.return_value = rows
    with pytest.raises(ExchangeError, match="证据未知"):
        await obj.entry_basis("SYNTHUSDT", CID)


@pytest.mark.parametrize("rows", [None, [], {"trades": []}, fills()[:1], fills() * 500])
async def test_missing_or_paginated_fills_are_unknown(rows: Any) -> None:
    obj, ex = client()
    ex.fapiPrivateGetUserTrades.return_value = rows
    with pytest.raises(ExchangeError):
        await obj.entry_basis("SYNTHUSDT", CID)


async def test_entry_evidence_changing_during_fill_query_cannot_freeze_basis() -> None:
    obj, ex = client()
    ex.fapiPrivateGetOrder.side_effect = [order(), order(orderId="18")]
    with pytest.raises(ExchangeError):
        await obj.entry_basis("SYNTHUSDT", CID)


@pytest.mark.parametrize("mode", [True, None, 0])
async def test_entry_basis_requires_confirmed_one_way_mode(mode: Any) -> None:
    obj, ex = client()
    ex.fapiPrivateGetPositionSideDual.return_value = {"dualSidePosition": mode}
    with pytest.raises(ExchangeError):
        await obj.entry_basis("SYNTHUSDT", CID)
    ex.fapiPrivateGetOrder.assert_not_awaited()


async def test_book_uses_exchange_transaction_time_and_same_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    obj, ex = client()
    freeze(monkeypatch)
    result = await obj.execution_book("SYNTHUSDT")
    assert result.bid == 1.09 and result.ask == 1.11
    assert result.source_at == NOW - timedelta(milliseconds=100)
    assert result.observed_at == NOW and result.source_at != result.observed_at
    ex.fapiPublicGetTickerBookTicker.assert_awaited_once_with({"symbol": "SYNTHUSDT"})
    ex.fetch_ticker.assert_not_awaited()
    ex.create_order.assert_not_awaited()


@pytest.mark.parametrize(
    "change",
    [
        {"symbol": "OTHERUSDT"},
        {"bidPrice": "NaN"},
        {"askPrice": "Infinity"},
        {"bidPrice": "0"},
        {"bidPrice": "1.12"},
        {"bidQty": "0"},
        {"askQty": None},
        {"time": None},
        {"time": 0},
        {"time": True},
        {"time": "NaN"},
        {"time": NOW.timestamp() * 1000 + 1},
        {"time": NOW.timestamp() * 1000 - 5001},
    ],
)
async def test_unknown_or_unfresh_book_is_not_current_price(
    monkeypatch: pytest.MonkeyPatch, change: dict[str, Any]
) -> None:
    obj, ex = client()
    freeze(monkeypatch)
    ex.fapiPublicGetTickerBookTicker.return_value = book(**change)
    with pytest.raises(ExchangeError, match="盘口证据未知"):
        await obj.execution_book("SYNTHUSDT")


async def test_book_request_delay_and_local_clock_rollback_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    obj, _ex = client()
    freeze(monkeypatch, NOW, NOW + timedelta(seconds=6))
    with pytest.raises(ExchangeError):
        await obj.execution_book("SYNTHUSDT")
    freeze(monkeypatch, NOW, NOW - timedelta(milliseconds=1))
    with pytest.raises(ExchangeError):
        await obj.execution_book("SYNTHUSDT")


@pytest.mark.parametrize("max_age", [0, -1, True, float("nan")])
async def test_invalid_freshness_argument_does_not_query(max_age: Any) -> None:
    obj, ex = client()
    with pytest.raises(ValueError):
        await obj.execution_book("SYNTHUSDT", max_age_s=max_age)
    ex.fapiPublicGetTickerBookTicker.assert_not_awaited()


async def test_read_network_failure_is_unknown_without_order_or_price_fallback() -> None:
    obj, ex = client()
    ex.fapiPrivateGetUserTrades.side_effect = ccxt.RequestTimeout("synthetic")
    with pytest.raises(ExchangeError):
        await obj.entry_basis("SYNTHUSDT", CID)
    ex.fapiPublicGetTickerBookTicker.side_effect = ccxt.RequestTimeout("synthetic")
    with pytest.raises(ExchangeError):
        await obj.execution_book("SYNTHUSDT")
    ex.create_order.assert_not_awaited()
    ex.fetch_ticker.assert_not_awaited()
