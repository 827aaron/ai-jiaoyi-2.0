"""执行器查询证据回归：纯本地替身，无真实订单或服务请求。"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from jiaoyi.contracts import Side
from jiaoyi.contracts.position import PositionStatus, ReconcileIssueKind
from jiaoyi.execution.exchange import ExchangeError
from jiaoyi.execution.hb_client import HbApiError
from jiaoyi.execution.reconciler import ReconcileError
from tests.test_execution import Rig, explain_reverse, open_filled
from tests.test_execution_io import hb


@pytest.mark.parametrize(
    "status,payload",
    [
        (404, None),
        (200, None),
        (200, {}),
        (200, {"data": None}),
        (200, {"data": "not a list"}),
        (200, {"data": [None]}),
        (200, {"data": [{"executor_id": ""}]}),
    ],
)
async def test_invalid_search_response_is_unknown_not_an_empty_inventory(
    status: int,
    payload: Any,
) -> None:
    api = hb(lambda request: httpx.Response(status, content=json.dumps(payload)))
    try:
        with pytest.raises(HbApiError):
            await api.running_executors("acct")
    finally:
        await api.close()


@pytest.mark.parametrize(
    "pagination",
    [
        None,
        {},
        {"has_more": "false"},
        {"has_more": True, "next_cursor": "next-page", "total_count": 2},
        {"has_more": False, "next_cursor": "next-page", "total_count": 1},
        {"has_more": False, "next_cursor": None, "total_count": 2},
    ],
)
@pytest.mark.parametrize("query", ["running", "find"])
async def test_incomplete_or_ambiguous_search_page_is_not_an_inventory(
    pagination: Any,
    query: str,
) -> None:
    payload = {"data": [{"executor_id": "e1"}], "pagination": pagination}
    api = hb(lambda request: httpx.Response(200, json=payload))
    try:
        with pytest.raises(HbApiError):
            if query == "running":
                await api.running_executors("acct")
            else:
                await api.find_executors("acct", "jy-p1")
    finally:
        await api.close()


async def test_complete_empty_search_page_is_a_valid_empty_result() -> None:
    payload = {
        "data": [],
        "pagination": {
            "limit": 200,
            "has_more": False,
            "next_cursor": None,
            "total_count": 0,
        },
    }
    api = hb(lambda request: httpx.Response(200, json=payload))
    try:
        assert await api.running_executors("acct") == []
        assert await api.find_executors("acct", "jy-p1") == []
    finally:
        await api.close()


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"executor_id": "other", "status": "TERMINATED", "is_active": False},
        {"executor_id": "e1", "status": "TERMINATED", "is_active": "false"},
        {"executor_id": "e1", "status": "TERMINATED", "is_active": True},
        {"executor_id": "e1", "status": "RUNNING", "is_active": False},
        {"executor_id": "e1", "status": "UNKNOWN", "is_active": False},
    ],
)
async def test_invalid_executor_detail_is_unknown_not_terminal(payload: Any) -> None:
    api = hb(lambda request: httpx.Response(200, content=json.dumps(payload)))
    try:
        with pytest.raises(HbApiError):
            await api.get_executor("e1")
    finally:
        await api.close()


@pytest.mark.parametrize("status,active", [("RUNNING", True), ("TERMINATED", False)])
async def test_valid_executor_detail_preserves_business_fields(status: str, active: bool) -> None:
    payload = {
        "executor_id": "e1",
        "status": status,
        "is_active": active,
        "close_type": "SYSTEM_CLEANUP",
        "filled_amount_quote": 100.0,
    }
    api = hb(lambda request: httpx.Response(200, json=payload))
    try:
        assert await api.get_executor("e1") == payload
    finally:
        await api.close()


async def test_detail_404_remains_unconfirmed_missing() -> None:
    api = hb(lambda request: httpx.Response(404))
    try:
        assert await api.get_executor("e1") is None
    finally:
        await api.close()


@pytest.mark.parametrize("side", ["long", "short"])
@pytest.mark.parametrize("detail_state", ["active", "unconfirmed_missing"])
async def test_search_absence_does_not_authorize_new_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    side: str,
    detail_state: str,
) -> None:
    rig = Rig(tmp_path)
    pid = await open_filled(rig, side=side)

    async def absent(account: str) -> list[dict[str, Any]]:
        return []  # 可能是过滤、未完整分页或服务恢复中，不能单独证明失管。

    async def unknown(executor_id: str) -> None:
        return None  # 现有vendor可由DB失败产生裸404；这里没有权威不存在证据。

    monkeypatch.setattr(rig.api, "running_executors", absent)
    if detail_state == "unconfirmed_missing":
        monkeypatch.setattr(rig.api, "get_executor", unknown)
    for _ in range(3):
        await rig.rec.reconcile(rig.t)
        rig.t += timedelta(seconds=30)
    assert rig.api.stopped == [] and rig.ex.market_closed == []
    assert rig.registry.get(pid).record.status is PositionStatus.OPEN
    assert len(rig.ex.stops) == 1


@pytest.mark.parametrize("side", ["long", "short"])
@pytest.mark.parametrize("failure_at", ["search", "detail", "exchange"])
async def test_unknown_round_interrupts_consecutive_terminal_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    side: str,
    failure_at: str,
) -> None:
    rig = Rig(tmp_path)
    rig.rec._retry_delays = ()
    pid = await open_filled(rig, side=side)
    rig.api.auto_terminate_on_stop = False
    rig.api.terminate("ex1", "SYSTEM_CLEANUP", leaves_position=True)
    await rig.rec.reconcile(rig.t)
    assert rig.api.stopped == [] and rig.ex.market_closed == []

    async def hb_failure(*args: Any) -> Any:
        raise HbApiError("synthetic query unavailable", transient=True)

    async def exchange_failure(*args: Any) -> Any:
        raise ExchangeError("synthetic query unavailable", transient=True)

    with monkeypatch.context() as patch:
        target, attribute, failing = {
            "search": (rig.api, "running_executors", hb_failure),
            "detail": (rig.api, "get_executor", hb_failure),
            "exchange": (rig.ex, "positions", exchange_failure),
        }[failure_at]
        patch.setattr(target, attribute, failing)
        rig.t += timedelta(seconds=30)
        with pytest.raises(ReconcileError):
            await rig.rec.reconcile(rig.t)
    assert rig.api.stopped == [] and rig.ex.market_closed == []
    rig.t += timedelta(seconds=30)
    await rig.rec.reconcile(rig.t)  # 未知之后重新开始第1次确认。
    assert rig.api.stopped == [] and rig.ex.market_closed == []
    assert rig.registry.get(pid).record.status is PositionStatus.OPEN
    rig.t += timedelta(seconds=30)
    await rig.rec.reconcile(rig.t)  # 同样的2轮保护仍有效，没有抬高阈值。
    expected_side = "sell" if side == "long" else "buy"
    assert rig.ex.market_closed == [("BTCUSDT", expected_side, 0.015)]


@pytest.mark.parametrize("side", ["long", "short"])
@pytest.mark.parametrize("interruption", ["active", "not_found"])
async def test_nonterminal_detail_restarts_terminal_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    side: str,
    interruption: str,
) -> None:
    rig = Rig(tmp_path)
    pid = await open_filled(rig, side=side)
    active = dict(rig.api.executors["ex1"])
    rig.api.auto_terminate_on_stop = False
    rig.api.terminate("ex1", "SYSTEM_CLEANUP", leaves_position=True)
    await rig.rec.reconcile(rig.t)
    assert rig.api.stopped == [] and rig.ex.market_closed == []

    async def nonterminal(executor_id: str) -> dict[str, Any] | None:
        return active if interruption == "active" else None

    with monkeypatch.context() as patch:
        patch.setattr(rig.api, "get_executor", nonterminal)
        rig.t += timedelta(seconds=30)
        await rig.rec.reconcile(rig.t)
    assert rig.api.stopped == [] and rig.ex.market_closed == []
    rig.t += timedelta(seconds=30)
    await rig.rec.reconcile(rig.t)
    assert rig.api.stopped == [] and rig.ex.market_closed == []
    assert rig.registry.get(pid).record.status is PositionStatus.OPEN
    rig.t += timedelta(seconds=30)
    await rig.rec.reconcile(rig.t)
    expected_side = "sell" if side == "long" else "buy"
    assert rig.ex.market_closed == [("BTCUSDT", expected_side, 0.015)]


@pytest.mark.parametrize("side", ["long", "short"])
async def test_list_omission_does_not_disable_missing_stop_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    side: str,
) -> None:
    rig = Rig(tmp_path)
    await open_filled(rig, side=side)
    rig.ex.stops.clear()

    async def absent(account: str) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(rig.api, "running_executors", absent)
    report = await rig.rec.reconcile(rig.t)
    assert any(i.kind is ReconcileIssueKind.LIFEBOAT_MISSING and i.fixed for i in report.issues)
    assert len(rig.ex.stops) == 1
    assert rig.api.stopped == [] and rig.ex.market_closed == []


@pytest.mark.parametrize("side", ["long", "short"])
async def test_list_omission_does_not_disable_confirmed_reverse_position_protection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    side: str,
) -> None:
    rig = Rig(tmp_path)
    pid = await open_filled(rig, side=side)
    explain_reverse(rig, pid)
    position = rig.registry.get(pid).record
    rig.ex.set_position(
        position.symbol, Side.SHORT if side == "long" else Side.LONG, position.quantity
    )
    placed_before = len(rig.ex.placed)

    async def absent(account: str) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(rig.api, "running_executors", absent)
    report = await rig.rec.reconcile(rig.t)
    assert any(i.kind is ReconcileIssueKind.SIDE_MISMATCH and i.fixed for i in report.issues)
    assert len(rig.ex.placed) == placed_before
    expected_side = "buy" if side == "long" else "sell"
    assert rig.ex.market_closed == [("BTCUSDT", expected_side, 0.015)]
