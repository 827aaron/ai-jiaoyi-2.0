"""真实 HB 预算拒绝没有创建订单；空 ID 本身不是未提交证明。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from jiaoyi.contracts import Side
from jiaoyi.contracts.position import CloseReason, PositionRecord, PositionStatus
from jiaoyi.execution.exchange import ExchangeError, ExchangePosition
from jiaoyi.execution.hb_client import HbApiError
from tests.test_execution import Rig, intent


async def budget_rejected_open(
    rig: Rig,
    side: Side,
    *,
    defect: str | None = None,
    prior_cid: bool = False,
) -> PositionRecord:
    original = rig.api.create_executor

    async def create(cfg: dict[str, Any], account: str, marker: str) -> dict[str, Any]:
        out = await original(cfg, account, marker)
        eid = out["executor_id"]
        # Fake create 默认注册 NEW；本用例明确模拟 HB 预算检查在下单前失败。
        rig.ex.entry_orders.pop(f"o-{eid}")
        detail = rig.api.executors[eid]
        detail.update(
            status="TERMINATED",
            is_active=False,
            close_type="INSUFFICIENT_BALANCE",
            is_trading=False,
            filled_amount_quote=0.0,
        )
        info = detail["custom_info"]
        info.update(
            current_position_average_price=68044.0,
            order_ids=[],
            open_order_last_update=None,
            held_position_orders=[],
        )
        if defect == "missing_order_ids":
            info.pop("order_ids")
        elif defect == "missing_filled":
            detail.pop("filled_amount_quote")
        elif defect == "cleanup":
            detail["close_type"] = "SYSTEM_CLEANUP"
        elif defect == "positive_fill":
            detail["filled_amount_quote"] = 1.0
        elif defect == "trading":
            detail["is_trading"] = True
        elif defect == "filled_bool":
            detail["filled_amount_quote"] = False
        elif defect == "missing_order_timestamp":
            info.pop("open_order_last_update")
        elif defect == "present_order_timestamp":
            info["open_order_last_update"] = 1.0
        elif defect == "held_orders":
            info["held_position_orders"] = [{"client_order_id": "other-order"}]
        elif defect == "wrong_executor":
            detail["executor_id"] = "other-executor"
        elif defect == "unknown_active":
            detail["is_active"] = None
        elif defect == "running_status":
            detail["status"] = "RUNNING"
        elif defect == "active":
            detail.update(status="RUNNING", is_active=True, close_type=None)
        if prior_cid:
            tracked = next(iter(rig.registry.data.positions.values()))
            rig.registry.put(tracked.model_copy(update={"entry_client_order_id": f"o-{eid}"}))
        return out

    rig.api.create_executor = create
    return await rig.adapter.open(intent(side=side.value))


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_proven_budget_rejection_with_zero_position_finishes_unfilled(
    tmp_path: Path, side: Side
) -> None:
    rig = Rig(tmp_path, notices=True)
    rec = await budget_rejected_open(rig, side)
    assert rec.status is PositionStatus.FAILED
    assert rec.entry_price is None and rec.close_reason is None
    assert rec.realized_pnl_usdt is None
    assert not rig.ex.entry_orders
    assert rig.ex.placed == [] and rig.ex.market_closed == []
    assert not any(a.title in {"开仓成交", "平仓完成"} for a in rig.alerts.sent)
    assert any("开仓没成交" in a.title for a in rig.alerts.sent)
    await rig.adapter.positions()
    assert rig.registry.get(rec.position_id).record.status is PositionStatus.FAILED


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
@pytest.mark.parametrize(
    "defect",
    [
        "missing_order_ids",
        "missing_filled",
        "cleanup",
        "positive_fill",
        "trading",
        "filled_bool",
        "missing_order_timestamp",
        "present_order_timestamp",
        "held_orders",
        "wrong_executor",
        "unknown_active",
        "running_status",
    ],
)
async def test_missing_or_contradictory_hb_evidence_does_not_claim_unsubmitted(
    tmp_path: Path, side: Side, defect: str
) -> None:
    rig = Rig(tmp_path)
    rec = await budget_rejected_open(rig, side, defect=defect)
    assert rec.status not in {PositionStatus.FAILED, PositionStatus.CLOSED}
    assert rec.closed_at is None
    assert rig.ex.market_closed == [] and rig.ex.cancelled == []


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
@pytest.mark.parametrize("fault", ["missing", "error", "empty_object", "residual"])
async def test_unsubmitted_hb_claim_still_requires_valid_zero_position(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, side: Side, fault: str
) -> None:
    rig = Rig(tmp_path)
    if fault == "missing":
        monkeypatch.setattr(rig.ex, "positions_evidence", None)
    else:

        async def positions() -> Any:
            if fault == "error":
                raise ExchangeError("synthetic position query timeout", transient=True)
            if fault == "empty_object":
                return {}
            return [ExchangePosition("BTCUSDT", side, 0.001)]

        monkeypatch.setattr(rig.ex, "positions_evidence", positions)
    rec = await budget_rejected_open(rig, side)
    assert rec.status not in {PositionStatus.FAILED, PositionStatus.CLOSED}
    assert rec.closed_at is None
    assert rig.ex.market_closed == [] and rig.ex.cancelled == []


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_prior_known_entry_cid_blocks_no_submission_claim(tmp_path: Path, side: Side) -> None:
    rig = Rig(tmp_path)
    rec = await budget_rejected_open(rig, side, prior_cid=True)
    assert rec.status not in {PositionStatus.FAILED, PositionStatus.CLOSED}
    assert rec.closed_at is None
    assert rig.ex.market_closed == [] and rig.ex.cancelled == []


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_late_position_after_initial_zero_prevents_unfilled_settlement(
    tmp_path: Path, side: Side
) -> None:
    rig = Rig(tmp_path)
    rig.ex.set_position("BTCUSDT", side, 0.001)
    rig.ex.positions_queue = [[]]
    rec = await budget_rejected_open(rig, side)
    assert rec.status not in {PositionStatus.FAILED, PositionStatus.CLOSED}
    assert rec.closed_at is None
    assert rig.ex.market_closed == [] and rig.ex.cancelled == []


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_hb_accepted_close_timeout_is_not_erased_by_later_budget_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, side: Side
) -> None:
    rig = Rig(tmp_path)
    rec = await budget_rejected_open(rig, side, defect="active")
    assert rec.status is PositionStatus.OPENING
    calls = 0

    async def accepted_timeout(*args: Any, **kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        raise HbApiError("synthetic accepted stop then timeout")

    monkeypatch.setattr(rig.api, "stop_executor", accepted_timeout)
    with pytest.raises(HbApiError):
        await rig.adapter.close(rec.position_id, CloseReason.KEEPER)
    assert rec.executor_id is not None
    rig.api.executors[rec.executor_id].update(
        status="TERMINATED", is_active=False, close_type="INSUFFICIENT_BALANCE"
    )
    await rig.adapter.positions()
    await rig.adapter.positions()
    tracked = rig.registry.get(rec.position_id)
    assert tracked.record.status is PositionStatus.CLOSING
    assert tracked.hb_close_pending is True and tracked.hb_close_unknown is True
    assert calls == 1
    assert rig.ex.market_closed == [] and rig.ex.cancelled == []
