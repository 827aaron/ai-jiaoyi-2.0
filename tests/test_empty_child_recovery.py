"""Offline recovery of persisted exits after an empty venue child ID."""

import pytest

from jiaoyi.contracts import Side
from jiaoyi.contracts.position import CloseReason, PositionStatus
from jiaoyi.execution.exchange import _bracket_from
from tests.test_server_protection_regression import AdapterHarness


async def incident(tmp_path, side, monkeypatch, *, response_changes=None):
    h = AdapterHarness(tmp_path, side)
    await h.protect()
    tracked = h.registry.get("pos-1")
    h.registry.put(
        tracked.model_copy(
            update={
                "record": tracked.record.model_copy(
                    update={"status": PositionStatus.CLOSING, "lifeboat": None}
                ),
                "requested_close": CloseReason.RECONCILER,
                "server_protection": tracked.server_protection.model_copy(
                    update={
                        "stage": "exiting",
                        "ever_armed": False,
                        "last_error": "ProtectionIncomplete",
                        "legs": [
                            leg.model_copy(update={"child_state": "unknown"})
                            for leg in tracked.server_protection.legs
                        ],
                    }
                ),
            }
        )
    )
    h.api.executors["owned-executor"].update(
        status="TERMINATED", is_active=False, is_trading=False, close_type="POSITION_HOLD"
    )
    original_get = h.exchange._get

    def read_venue(algo_id, client_order_id):
        order = original_get(algo_id, client_order_id)
        if order is None:
            return None
        raw = {
            "symbol": order.symbol,
            "algoId": order.algo_id,
            "clientAlgoId": order.client_order_id,
            "orderType": "STOP_MARKET" if order.kind == "stop_loss" else "TAKE_PROFIT_MARKET",
            "side": order.close_side.upper(),
            "quantity": str(order.quantity),
            "triggerPrice": str(order.trigger_price),
            "positionSide": order.position_side,
            "workingType": order.working_type,
            "reduceOnly": order.reduce_only,
            "priceProtect": order.price_protect,
            "closePosition": order.close_position,
            "algoStatus": order.algo_status,
            "triggerTime": 0,
            "actualOrderId": "",
        }
        raw.update(response_changes or {})
        return _bracket_from(raw, lifeboat=order.client_order_id.startswith("lb-"))

    monkeypatch.setattr(h.exchange, "_get", read_venue)
    h.exchange.events.clear()
    h.restart_adapter()
    return h


@pytest.mark.parametrize("side", [Side.SHORT, Side.LONG])
async def test_persisted_exit_automatically_closes_fresh_residual_without_removing_protection(
    tmp_path, monkeypatch, side
):
    h = await incident(tmp_path, side, monkeypatch)
    h.exchange.held = 1.25
    original_place = h.exchange.place_close

    async def checked_place(symbol, close_side, quantity, client_order_id):
        assert h.registry.get("pos-1").requested_close is CloseReason.RECONCILER
        assert close_side == ("buy" if side is Side.SHORT else "sell")
        assert quantity == 1.25
        assert len(h.exchange.orders) == 2
        assert all(order.algo_status == "NEW" for order in h.exchange.orders.values())
        assert not h.cancels()
        return await original_place(symbol, close_side, quantity, client_order_id)

    monkeypatch.setattr(h.exchange, "place_close", checked_place)
    await h.adapter.positions()
    await h.adapter.positions()
    h.restart_adapter()
    await h.adapter.positions()
    final = h.registry.get("pos-1")
    assert h.exchange.direct == [("dc-pos-1-1", 1.25)]
    assert h.exchange.held == 0
    assert final.record.status is PositionStatus.CLOSED
    assert final.record.close_reason is CloseReason.RECONCILER
    assert final.server_protection.stage == "done"
    assert len(h.cancels()) == 2
    assert not h.creates() and not h.api.stopped


@pytest.mark.parametrize("side", [Side.SHORT, Side.LONG])
async def test_unknown_recovered_exit_retains_all_protection_and_same_persisted_cid(
    tmp_path, monkeypatch, side
):
    h = await incident(tmp_path, side, monkeypatch)
    h.exchange.held = 1.25
    h.exchange.direct_unknown = True
    await h.adapter.positions()
    h.restart_adapter()
    await h.adapter.positions()
    await h.adapter.positions()
    tracked = h.registry.get("pos-1")
    assert h.exchange.direct == [("dc-pos-1-1", 1.25)]
    assert tracked.direct_close_pending
    assert tracked.direct_close_client_order_id == "dc-pos-1-1"
    assert tracked.direct_close_generation == 1
    assert tracked.requested_close is CloseReason.RECONCILER
    assert tracked.record.status is PositionStatus.CLOSING
    assert tracked.record.lifeboat is not None
    assert tracked.record.lifeboat.status.value == "active"
    assert all(order.algo_status == "NEW" for order in h.exchange.orders.values())
    assert not h.cancels() and not h.creates() and not h.api.stopped


@pytest.mark.parametrize("status", ["TRIGGERED", "FINISHED"])
@pytest.mark.parametrize("side", [Side.SHORT, Side.LONG])
async def test_triggered_empty_child_still_blocks_recovered_exit(
    tmp_path, monkeypatch, side, status
):
    h = await incident(tmp_path, side, monkeypatch, response_changes={"algoStatus": status})
    await h.adapter.positions()
    tracked = h.registry.get("pos-1")
    assert tracked.record.status is PositionStatus.CLOSING
    assert all(leg.child_state == "unknown" for leg in tracked.server_protection.legs)
    assert not h.exchange.direct and not h.cancels() and not h.creates()


@pytest.mark.parametrize(
    "changes",
    [
        {"symbol": "OTHERUSDT"},
        {"algoId": "999"},
        {"quantity": "3"},
        {"triggerPrice": "150"},
        {"reduceOnly": False},
        {"positionSide": "LONG"},
        {"triggerTime": 1793123194000},
    ],
)
async def test_empty_child_cannot_bypass_identity_or_trigger_evidence(
    tmp_path, monkeypatch, changes
):
    h = await incident(tmp_path, Side.LONG, monkeypatch, response_changes=changes)
    await h.adapter.positions()
    assert h.registry.get("pos-1").record.status is PositionStatus.CLOSING
    assert not h.exchange.direct and not h.cancels() and not h.creates()
