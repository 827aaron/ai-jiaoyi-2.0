"""持久直连订单的重启/残量闭环；所有订单均由假件生成。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from jiaoyi.contracts import Side
from jiaoyi.contracts.position import PositionStatus
from jiaoyi.execution.exchange import CloseFill, ExchangeError, IdentifiedCloseRejected
from jiaoyi.execution.registry import PositionRegistry
from tests.test_direct_close_protection_regression import request_close, unavailable_hb
from tests.test_execution import Rig, open_filled


def restart(rig: Rig, tmp_path: Path) -> None:
    rig.registry = PositionRegistry(tmp_path / "positions.json")
    rig.adapter = rig._adapter()


@pytest.mark.parametrize("side", ["long", "short"])
async def test_terminal_partial_recovers_exact_remaining_with_new_cid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    side: str,
) -> None:
    rig = Rig(tmp_path)
    pid = await open_filled(rig, side=side)
    original = rig.ex.close_position_market
    calls: list[float] = []

    async def partial(symbol: str, close_side: Any, qty: float) -> CloseFill:
        calls.append(qty)
        if len(calls) == 1:
            rig.ex.set_position(symbol, Side(side), qty / 2)
            return CloseFill(68000, None)
        return await original(symbol, close_side, qty)

    monkeypatch.setattr(rig.api, "stop_executor", unavailable_hb)
    monkeypatch.setattr(rig.ex, "close_position_market", partial)
    await request_close(rig, pid)
    first_cid = rig.registry.get(pid).direct_close_client_order_id
    first = rig.ex.close_orders[first_cid]
    assert first.is_terminal is False
    restart(rig, tmp_path)
    await request_close(rig, pid)
    assert len(calls) == 1  # 在途 partial 不能生成第二张
    rig.ex.close_orders[first_cid] = replace(first, status="EXPIRED", is_terminal=True)
    await request_close(rig, pid)
    assert calls == [0.015, 0.0075]
    state = rig.registry.get(pid)
    assert state.direct_close_generation == 2
    assert state.direct_close_client_order_id != first_cid
    assert state.record.status is PositionStatus.CLOSED
    assert state.record.realized_pnl_usdt is None  # 多代成交，不能拿最后一笔均价冒充总净收益


@pytest.mark.parametrize("side", ["long", "short"])
async def test_unknown_submit_not_found_after_restart_is_not_resubmitted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    side: str,
) -> None:
    rig = Rig(tmp_path)
    pid = await open_filled(rig, side=side)
    calls = 0

    async def unknown(*args: Any, **kwargs: Any) -> CloseFill:
        nonlocal calls
        calls += 1
        raise ExchangeError("synthetic accepted then timeout", transient=True)

    monkeypatch.setattr(rig.api, "stop_executor", unavailable_hb)
    monkeypatch.setattr(rig.ex, "close_position_market", unknown)
    await request_close(rig, pid)
    cid = rig.registry.get(pid).direct_close_client_order_id
    restart(rig, tmp_path)
    await request_close(rig, pid)
    assert calls == 1 and rig.registry.get(pid).direct_close_client_order_id == cid
    assert rig.registry.get(pid).record.status is PositionStatus.CLOSING
    assert rig.ex.stops


@pytest.mark.parametrize("side", ["long", "short"])
async def test_definite_rejection_can_retry_next_generation_from_fresh_remaining(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    side: str,
) -> None:
    rig = Rig(tmp_path)
    pid = await open_filled(rig, side=side)
    original = rig.ex.place_close
    cids: list[str] = []

    async def rejected_once(symbol: str, close_side: Any, qty: float, cid: str) -> Any:
        cids.append(cid)
        if len(cids) == 1:
            raise IdentifiedCloseRejected("synthetic business rejection")
        return await original(symbol, close_side, qty, cid)

    monkeypatch.setattr(rig.api, "stop_executor", unavailable_hb)
    monkeypatch.setattr(rig.ex, "place_close", rejected_once)
    await request_close(rig, pid)
    assert rig.registry.get(pid).direct_close_order_status == "REJECTED"
    restart(rig, tmp_path)
    await request_close(rig, pid)
    assert len(cids) == 2 and cids[0] != cids[1]
    assert rig.registry.get(pid).record.status is PositionStatus.CLOSED


@pytest.mark.parametrize("side", ["long", "short"])
async def test_zero_does_not_settle_while_opening_order_can_fill_again(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    side: str,
) -> None:
    rig = Rig(tmp_path)
    pid = await open_filled(rig, side=side)
    cid = rig.registry.get(pid).entry_client_order_id
    entry = rig.ex.entry_orders[cid]
    rig.ex.entry_orders[cid] = replace(entry, status="PARTIALLY_FILLED", filled_quantity=0.0075)
    monkeypatch.setattr(rig.api, "stop_executor", unavailable_hb)
    await request_close(rig, pid)
    assert await rig.ex.positions_evidence() == []
    assert rig.registry.get(pid).record.status is PositionStatus.CLOSING
    assert rig.ex.stops  # 未完成的开仓单还可能产生下一笔仓位
    restart(rig, tmp_path)
    await request_close(rig, pid)
    assert rig.registry.get(pid).record.status is PositionStatus.CLOSING
    rig.ex.entry_orders[cid] = replace(rig.ex.entry_orders[cid], status="CANCELED")
    await request_close(rig, pid)
    assert rig.registry.get(pid).record.status is PositionStatus.CLOSED
