"""对账员开平仓过渡的标注：宽限内记「信息」，宽限外 / 宽限 0 照旧记「出错」，处理一概不变。"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from jiaoyi.acceptance import read_records
from jiaoyi.alerting.telegram import TelegramAlerter
from jiaoyi.contracts import AlertLevel, Outcome, Role, Side
from jiaoyi.contracts.position import (
    CloseReason,
    PositionStatus,
    ReconcileIssue,
    ReconcileIssueKind,
)
from jiaoyi.core.recorder import RoleRecorder
from jiaoyi.core.store import JsonlDecisionLog
from jiaoyi.execution.hb_client import HbApiError
from jiaoyi.execution.reconciler import ThreeWayReconciler
from jiaoyi.execution.registry import ServerProtection
from jiaoyi.interfaces import Alerter
from tests.test_execution import Rig, open_filled

ARMED_PENDING = "交易所托管仍在"
LIST_ABSENT_ACTIVE = "运行列表未返回执行器，但"
LIST_ABSENT = "账本里还在持仓，但运行列表未返回执行器"
CLOSE_UNCONFIRMED = "平仓结果尚未确认"
QTY = "账本 "


def _wire(
    rig: Rig,
    tmp_path: Path,
    *,
    opening: float = 0.0,
    closing: float = 0.0,
    alerter: Alerter | None = None,
) -> None:
    rig.rec = ThreeWayReconciler(
        rig.adapter,
        rig.api,
        rig.ex,
        rig.lifeboat,
        rig.registry,
        alerter or rig.alerts,
        account_name="acct",
        recorder=RoleRecorder(Role.RECONCILER, JsonlDecisionLog(tmp_path / "data")),
        opening_grace_s=opening,
        closing_grace_s=closing,
    )


def _hide_from_running_list(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    async def absent(account: str) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(rig.api, "running_executors", absent)


def _set(rig: Rig, pid: str, **update: Any) -> None:
    tracked = rig.registry.get(pid)
    assert tracked is not None
    rig.registry.put(tracked.model_copy(update=update))


def _protection(stage: str) -> ServerProtection:
    return ServerProtection(
        account_name="acct",
        connector_name="binance_perpetual_testnet",
        intended_quantity=0.015,
        stage=stage,  # type: ignore[arg-type]
    )


def _issue(issues: list[ReconcileIssue], text: str) -> ReconcileIssue:
    return next(i for i in issues if i.detail.startswith(text))


def _outcome(tmp_path: Path, text: str) -> Outcome:
    records = [
        r
        for r in read_records(tmp_path / "data")
        if r.role is Role.RECONCILER and text in r.summary
    ]
    assert records
    return records[-1].outcome


def _side_effects(rig: Rig) -> tuple[object, ...]:
    return (
        list(rig.api.stopped),
        list(rig.ex.market_closed),
        sorted(rig.ex.stops),
        list(rig.ex.cancelled),
        sorted((t.record.symbol, t.record.status) for t in rig.registry.data.positions.values()),
    )


@pytest.mark.parametrize(
    "opening,after_s,informational",
    [(60.0, 10, True), (60.0, 61, False), (0.0, 10, False)],
    ids=["inside_grace", "after_grace", "grace_off"],
)
async def test_opening_list_absence_with_active_detail_label(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    opening: float,
    after_s: int,
    informational: bool,
) -> None:
    _wire(rig := Rig(tmp_path), tmp_path, opening=opening)
    await open_filled(rig)
    _hide_from_running_list(rig, monkeypatch)
    report = await rig.rec.reconcile(rig.t + timedelta(seconds=after_s))
    issue = _issue(report.issues, LIST_ABSENT_ACTIVE)
    assert issue.kind is ReconcileIssueKind.EXECUTOR_FAILED and not issue.fixed
    assert issue.informational is informational
    expected = Outcome.INFO if informational else Outcome.ERROR
    assert _outcome(tmp_path, LIST_ABSENT_ACTIVE) is expected
    levels = [a.level for a in rig.alerts.sent if LIST_ABSENT_ACTIVE in a.body]
    assert levels == ([] if informational else [AlertLevel.WARN])


async def test_opening_round_started_before_the_record_is_inside_grace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _wire(rig := Rig(tmp_path), tmp_path, opening=60.0)
    await open_filled(rig)
    _hide_from_running_list(rig, monkeypatch)
    report = await rig.rec.reconcile(rig.t - timedelta(seconds=20))
    assert _issue(report.issues, LIST_ABSENT_ACTIVE).informational


@pytest.mark.parametrize(
    "opening,after_s,informational",
    [(60.0, 10, True), (60.0, 61, False), (0.0, 10, False)],
    ids=["inside_grace", "after_grace", "grace_off"],
)
async def test_opening_protection_not_armed_label(
    tmp_path: Path, opening: float, after_s: int, informational: bool
) -> None:
    _wire(rig := Rig(tmp_path), tmp_path, opening=opening)
    pid = await open_filled(rig)
    _set(rig, pid, server_protection=_protection("provisional"))
    report = await rig.rec.reconcile(rig.t + timedelta(seconds=after_s))
    issue = _issue(report.issues, ARMED_PENDING)
    assert "provisional" in issue.detail and issue.informational is informational
    expected = Outcome.INFO if informational else Outcome.ERROR
    assert _outcome(tmp_path, ARMED_PENDING) is expected


async def test_opening_hint_writes_no_error_record(tmp_path: Path) -> None:
    _wire(rig := Rig(tmp_path), tmp_path, opening=60.0)
    pid = await open_filled(rig)
    _set(rig, pid, server_protection=_protection("provisional"))
    await rig.rec.reconcile(rig.t)
    statuses = [r for r in read_records(tmp_path / "data") if r.role is Role.RECONCILER]
    assert not any(r.outcome is Outcome.ERROR for r in statuses)


@pytest.mark.parametrize(
    "closing,second_round_s,informational",
    [(60.0, 30, True), (60.0, 61, False), (0.0, 30, False)],
    ids=["inside_grace", "after_grace", "grace_off"],
)
async def test_closing_list_absence_label(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closing: float,
    second_round_s: int,
    informational: bool,
) -> None:
    _wire(rig := Rig(tmp_path), tmp_path, closing=closing)
    pid = await open_filled(rig)
    rig.api.auto_terminate_on_stop = False
    await rig.adapter.close(pid, CloseReason.MANUAL)
    _hide_from_running_list(rig, monkeypatch)
    await rig.rec.reconcile(rig.t)  # first CLOSING round is settling: nothing reported yet
    report = await rig.rec.reconcile(rig.t + timedelta(seconds=second_round_s))
    issue = _issue(report.issues, LIST_ABSENT)
    assert issue.informational is informational
    expected = Outcome.INFO if informational else Outcome.ERROR
    assert _outcome(tmp_path, LIST_ABSENT) is expected


@pytest.mark.parametrize(
    "closing,after_s,informational",
    [(60.0, 0, True), (60.0, 61, False), (0.0, 0, False)],
    ids=["inside_grace", "after_grace", "grace_off"],
)
async def test_closing_unconfirmed_and_exiting_protection_label(
    tmp_path: Path, closing: float, after_s: int, informational: bool
) -> None:
    _wire(rig := Rig(tmp_path), tmp_path, closing=closing)
    pid = await open_filled(rig)
    _set(rig, pid, direct_close_pending=True, server_protection=_protection("exiting"))
    first = await rig.rec.reconcile(rig.t)
    report = await rig.rec.reconcile(rig.t + timedelta(seconds=after_s)) if after_s else first
    unconfirmed = _issue(report.issues, CLOSE_UNCONFIRMED)
    exiting = _issue(report.issues, ARMED_PENDING)
    assert "exiting" in exiting.detail
    assert unconfirmed.informational is informational
    assert exiting.informational is informational


async def test_closing_grace_does_not_cover_an_open_position(tmp_path: Path) -> None:
    _wire(rig := Rig(tmp_path), tmp_path, closing=600.0)
    pid = await open_filled(rig)
    _set(rig, pid, server_protection=_protection("provisional"))
    report = await rig.rec.reconcile(rig.t)
    assert not _issue(report.issues, ARMED_PENDING).informational


async def test_explicit_terminated_path_is_unchanged_inside_both_graces(tmp_path: Path) -> None:
    _wire(rig := Rig(tmp_path), tmp_path, opening=600.0, closing=600.0)
    await open_filled(rig)
    rig.api.auto_terminate_on_stop = False
    rig.api.terminate("ex1", "SYSTEM_CLEANUP", leaves_position=True)
    first = await rig.rec.reconcile(rig.t)
    gone = next(i for i in first.issues if "TERMINATED" in i.detail)
    assert not gone.informational and not gone.fixed
    assert rig.ex.market_closed == []
    second = await rig.rec.reconcile(rig.t)
    assert all(not i.informational for i in second.issues)
    assert rig.ex.market_closed == [("BTCUSDT", "sell", 0.015)]


async def test_failed_terminated_flatten_stays_error_inside_both_graces(tmp_path: Path) -> None:
    _wire(rig := Rig(tmp_path), tmp_path, opening=600.0, closing=600.0)
    await open_filled(rig)
    rig.api.auto_terminate_on_stop = False
    rig.api.terminate("ex1", "SYSTEM_CLEANUP", leaves_position=True)
    rig.ex.fail_market_close = True
    rig.api.stop_executor = _always_down  # type: ignore[method-assign]
    await rig.rec.reconcile(rig.t)
    second = await rig.rec.reconcile(rig.t)
    assert _issue(second.issues, LIST_ABSENT).informational is False
    assert all(not i.informational for i in second.issues)


def _closing_on_exchange(rig: Rig, pid: str, qty: float, side: Side = Side.LONG) -> None:
    tracked = rig.registry.get(pid)
    assert tracked is not None
    _set(
        rig,
        pid,
        record=tracked.record.model_copy(update={"status": PositionStatus.CLOSING}),
        direct_close_pending=True,
        server_protection=_protection("exiting"),
    )
    rig.ex.set_position("BTCUSDT", side, qty)


def _qty_issue(issues: list[ReconcileIssue]) -> ReconcileIssue:
    return next(i for i in issues if i.kind is ReconcileIssueKind.QTY_MISMATCH)


@pytest.mark.parametrize(
    "closing,after_s,exchange_qty,informational",
    [
        (60.0, 0, 0.0, True),
        (60.0, 30, 0.005, True),
        (60.0, 61, 0.0, False),
        (60.0, 0, 0.03, False),
        (0.0, 0, 0.0, False),
    ],
    ids=["inside_grace", "inside_grace_partial", "after_grace", "exchange_larger", "grace_off"],
)
async def test_closing_qty_mismatch_label(
    tmp_path: Path, closing: float, after_s: int, exchange_qty: float, informational: bool
) -> None:
    _wire(rig := Rig(tmp_path), tmp_path, closing=closing)
    pid = await open_filled(rig)
    _closing_on_exchange(rig, pid, exchange_qty)
    first = await rig.rec.reconcile(rig.t)
    report = await rig.rec.reconcile(rig.t + timedelta(seconds=after_s)) if after_s else first
    issue = _qty_issue(report.issues)
    assert issue.detail.startswith(f"账本 0.015，币安 {exchange_qty}") and not issue.fixed
    assert issue.informational is informational
    expected = Outcome.INFO if informational else Outcome.ERROR
    assert _outcome(tmp_path, QTY) is expected
    levels = [a.level for a in rig.alerts.sent if "持仓数量对不上" in a.title + a.body]
    assert (levels == []) is informational


async def test_closing_grace_does_not_cover_qty_mismatch_on_an_open_position(
    tmp_path: Path,
) -> None:
    _wire(rig := Rig(tmp_path), tmp_path, closing=600.0)
    await open_filled(rig)
    rig.ex.set_position("BTCUSDT", Side.LONG, 0.005)
    report = await rig.rec.reconcile(rig.t)
    assert not _qty_issue(report.issues).informational


async def test_closing_grace_does_not_cover_a_reversed_exchange_position(tmp_path: Path) -> None:
    _wire(rig := Rig(tmp_path), tmp_path, closing=600.0)
    pid = await open_filled(rig)
    _closing_on_exchange(rig, pid, 0.005, side=Side.SHORT)
    report = await rig.rec.reconcile(rig.t)
    reversed_ = [i for i in report.issues if i.kind is ReconcileIssueKind.SIDE_MISMATCH]
    assert reversed_ and not any(i.informational for i in reversed_)
    assert not any(
        i.informational for i in report.issues if i.kind is ReconcileIssueKind.QTY_MISMATCH
    )


async def _always_down(executor_id: str, *, keep_position: bool) -> None:
    raise HbApiError("hummingbot-api 不可达")


Hook = Callable[[int], None]


async def _scenario(rig: Rig, monkeypatch: pytest.MonkeyPatch, name: str) -> Hook:
    def nothing(k: int) -> None:
        return None

    pid = await open_filled(rig)
    if name in ("terminated", "terminated_then_active"):
        active = dict(rig.api.executors["ex1"])
        rig.api.auto_terminate_on_stop = False
        rig.api.terminate("ex1", "SYSTEM_CLEANUP", leaves_position=True)
        terminated = dict(rig.api.executors["ex1"])
        if name == "terminated_then_active":
            _hide_from_running_list(rig, monkeypatch)

            def flip(k: int) -> None:
                rig.api.executors["ex1"] = active if k == 1 else terminated

            return flip
    elif name == "list_absent":
        _hide_from_running_list(rig, monkeypatch)
    elif name == "closing":
        rig.api.auto_terminate_on_stop = False
        await rig.adapter.close(pid, CloseReason.MANUAL)
        _hide_from_running_list(rig, monkeypatch)
    elif name == "missing_lifeboat":
        rig.ex.stops.clear()
    elif name in ("closing_exchange_flat", "closing_exchange_larger"):
        _closing_on_exchange(rig, pid, 0.0 if name == "closing_exchange_flat" else 0.03)
    else:
        _set(rig, pid, direct_close_pending=True, server_protection=_protection("exiting"))
    return nothing


@pytest.mark.parametrize(
    "name",
    [
        "terminated",
        "terminated_then_active",
        "list_absent",
        "closing",
        "missing_lifeboat",
        "unconfirmed",
        "closing_exchange_flat",
        "closing_exchange_larger",
    ],
)
async def test_graces_change_no_close_flatten_or_protection_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    effects = []
    for grace, sub in ((0.0, "off"), (600.0, "on")):
        with monkeypatch.context() as patch:
            _wire(rig := Rig(tmp_path / sub), tmp_path / sub, opening=grace, closing=grace)
            before_round = await _scenario(rig, patch, name)
            trail = []
            for k in range(4):
                before_round(k)
                report = await rig.rec.reconcile(rig.t + timedelta(seconds=30 * k))
                kinds = [(i.kind, i.fixed, i.detail.split("（")[0]) for i in report.issues]
                trail.append((kinds, sorted(rig.rec._gone_rounds.values()), _side_effects(rig)))
            effects.append(trail)
    assert effects[0] == effects[1]


async def test_hot_reload_switches_the_grace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _wire(rig := Rig(tmp_path), tmp_path)
    await open_filled(rig)
    _hide_from_running_list(rig, monkeypatch)
    before = await rig.rec.reconcile(rig.t)
    rig.rec.configure_issue_grace(60.0, 0.0)
    after = await rig.rec.reconcile(rig.t)
    assert not _issue(before.issues, LIST_ABSENT_ACTIVE).informational
    assert _issue(after.issues, LIST_ABSENT_ACTIVE).informational


def _telegram(sent: list[str]) -> TelegramAlerter:
    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content)["text"])
        return httpx.Response(200, json={"ok": True})

    return TelegramAlerter(
        "t", "1", cooldown_s=300.0, monotonic=lambda: 1000.0, transport=httpx.MockTransport(handler)
    )


async def test_hint_does_not_swallow_the_warning_at_grace_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[str] = []
    tg = _telegram(sent)
    _wire(rig := Rig(tmp_path), tmp_path, opening=60.0, alerter=tg)
    await open_filled(rig)
    _hide_from_running_list(rig, monkeypatch)
    await rig.rec.reconcile(rig.t + timedelta(seconds=10))
    assert not any(LIST_ABSENT_ACTIVE in m for m in sent)
    await rig.rec.reconcile(rig.t + timedelta(seconds=61))
    assert sum(LIST_ABSENT_ACTIVE in m for m in sent) == 1
    await tg.close()


async def test_hint_does_not_swallow_a_real_executor_failure_next_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[str] = []
    tg = _telegram(sent)
    _wire(rig := Rig(tmp_path), tmp_path, opening=600.0, alerter=tg)
    await open_filled(rig)
    _hide_from_running_list(rig, monkeypatch)
    await rig.rec.reconcile(rig.t)
    rig.api.auto_terminate_on_stop = False
    rig.api.terminate("ex1", "SYSTEM_CLEANUP", leaves_position=True)
    await rig.rec.reconcile(rig.t + timedelta(seconds=30))
    assert sum("TERMINATED" in m and "第 1/2 轮" in m for m in sent) == 1
    await tg.close()


async def test_closing_window_survives_a_close_that_flips_back(tmp_path: Path) -> None:
    _wire(rig := Rig(tmp_path), tmp_path, closing=60.0)
    pid = await open_filled(rig)
    _set(rig, pid, direct_close_pending=True, server_protection=_protection("exiting"))
    first = await rig.rec.reconcile(rig.t)
    assert _issue(first.issues, CLOSE_UNCONFIRMED).informational
    _set(rig, pid, direct_close_pending=False, server_protection=_protection("provisional"))
    between = await rig.rec.reconcile(rig.t + timedelta(seconds=30))
    assert not _issue(between.issues, ARMED_PENDING).informational
    _set(rig, pid, direct_close_pending=True, server_protection=_protection("exiting"))
    again = await rig.rec.reconcile(rig.t + timedelta(seconds=61))
    assert not _issue(again.issues, CLOSE_UNCONFIRMED).informational


async def test_opening_clock_jump_beyond_grace_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _wire(rig := Rig(tmp_path), tmp_path, opening=60.0)
    await open_filled(rig)
    _hide_from_running_list(rig, monkeypatch)
    report = await rig.rec.reconcile(rig.t - timedelta(seconds=61))
    assert not _issue(report.issues, LIST_ABSENT_ACTIVE).informational
