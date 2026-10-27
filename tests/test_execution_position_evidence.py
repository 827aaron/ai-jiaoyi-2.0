"""空仓/开仓残单证据的离线边界；不连接真实账户。"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from jiaoyi.execution.exchange import CcxtBinanceFutures, ExchangeError


def client() -> tuple[CcxtBinanceFutures, Any]:
    obj = CcxtBinanceFutures.__new__(CcxtBinanceFutures)
    obj._markets_loaded = True
    ex = AsyncMock()
    ex.fapiPrivateGetPositionSideDual.return_value = {"dualSidePosition": False}
    obj._ex = ex
    return obj, ex


def position(**changes: Any) -> dict[str, Any]:
    row = {
        "symbol": "BTC/USDT:USDT",
        "contracts": 1,
        "side": "long",
        "entryPrice": 100,
        "info": {"positionSide": "BOTH", "positionAmt": "1"},
    }
    row.update(changes)
    return row


@pytest.mark.parametrize(
    "rows",
    [
        None,
        {},
        [None],
        [position(contracts=None)],
        [position(contracts=float("nan"))],
        [position(contracts=float("inf"))],
        [position(side="mystery")],
        [position(side="short")],
        [position(info={})],
        [position(contracts=0)],
        [position(info={"positionSide": "LONG", "positionAmt": "1"})],
    ],
)
async def test_malformed_position_evidence_is_never_zero(rows: Any) -> None:
    obj, ex = client()
    ex.fetch_positions.return_value = rows
    with pytest.raises(ExchangeError):
        await obj.positions_evidence()


@pytest.mark.parametrize(
    "rows",
    [[], [position(contracts=0, side=None, info={"positionSide": "BOTH", "positionAmt": "0"})]],
)
async def test_explicit_zero_is_valid(rows: Any) -> None:
    obj, ex = client()
    ex.fetch_positions.return_value = rows
    assert await obj.positions_evidence() == []


@pytest.mark.parametrize("side,amount", [("long", "1"), ("short", "-1")])
async def test_position_direction_preserved(side: str, amount: str) -> None:
    obj, ex = client()
    ex.fetch_positions.return_value = [
        position(side=side, info={"positionSide": "BOTH", "positionAmt": amount})
    ]
    got = await obj.positions_evidence()
    assert got[0].side.value == side and got[0].quantity == 1


def entry(**changes: Any) -> dict[str, Any]:
    row = {
        "symbol": "BTCUSDT",
        "clientOrderId": "entry-example",
        "orderId": "123",
        "side": "BUY",
        "positionSide": "BOTH",
        "origQty": "1",
        "executedQty": "1",
        "status": "FILLED",
        "reduceOnly": False,
    }
    row.update(changes)
    return row


@pytest.mark.parametrize(
    "row",
    [
        None,
        [],
        {},
        entry(symbol="ETHUSDT"),
        entry(clientOrderId="other"),
        entry(status="UNKNOWN"),
        entry(status="FILLED", executedQty="0.5"),
        entry(executedQty="nan"),
        entry(positionSide="LONG"),
        entry(side="UNKNOWN"),
        entry(reduceOnly=None),
        entry(orderId="bad"),
    ],
)
async def test_entry_unknown_is_not_terminal(row: Any) -> None:
    obj, ex = client()
    ex.fapiPrivateGetOrder.return_value = row
    with pytest.raises(ExchangeError):
        await obj.entry_order("BTCUSDT", "entry-example")


@pytest.mark.parametrize(
    "status,filled,terminal",
    [
        ("NEW", "0", False),
        ("PARTIALLY_FILLED", "0.5", False),
        ("CANCELED", "0.5", True),
        ("FILLED", "1", True),
    ],
)
async def test_entry_terminal_is_separate_from_filled_quantity(
    status: str, filled: str, terminal: bool
) -> None:
    obj, ex = client()
    ex.fapiPrivateGetOrder.return_value = entry(status=status, executedQty=filled)
    result = await obj.entry_order("BTCUSDT", "entry-example")
    assert (
        result is not None
        and result.terminal is terminal
        and result.filled_quantity == float(filled)
    )
