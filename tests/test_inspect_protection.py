"""scripts/inspect_protection.py：working_type 按腿记的值核对，新腿类型和终态腿不误报。"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

spec = importlib.util.spec_from_file_location(
    "inspect_protection", Path(__file__).resolve().parents[1] / "scripts" / "inspect_protection.py"
)
assert spec and spec.loader
ip = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ip)

TRACKED = {"record": {"symbol": "SOLUSDT", "side": "long"}}


def leg(**kw):
    base = {
        "purpose": "fixed",
        "client_order_id": "cid",
        "kind": "stop_loss",
        "quantity": 1.0,
        "trigger_price": 90.0,
        "algo_id": None,
    }
    return {**base, **kw}


def order(**kw):
    base = {
        "symbol": "SOLUSDT",
        "client_order_id": "cid",
        "kind": "stop_loss",
        "close_side": "sell",
        "position_side": "BOTH",
        "working_type": "CONTRACT_PRICE",
        "reduce_only": True,
        "price_protect": False,
        "close_position": False,
        "quantity": 1.0,
        "trigger_price": 90.0,
        "algo_id": "1",
    }
    return SimpleNamespace(**{**base, **kw})


def test_working_type_uses_the_legs_own_value() -> None:
    assert ip._accept_checks(TRACKED, leg(working_type="MARK_PRICE"), order()) == [
        "working_type 'CONTRACT_PRICE' != 'MARK_PRICE'"
    ]
    ok = order(working_type="MARK_PRICE")
    assert ip._accept_checks(TRACKED, leg(working_type="MARK_PRICE"), ok) == []


def test_working_type_falls_back_to_purpose_for_old_ledgers() -> None:
    lifeboat = order(working_type="MARK_PRICE")
    assert ip._accept_checks(TRACKED, leg(purpose="lifeboat"), lifeboat) == []
    assert ip._accept_checks(TRACKED, leg(purpose="trailing"), order()) == []
    assert ip._accept_checks(TRACKED, leg(purpose="tightened"), order()) == []


def test_leg_verdicts() -> None:
    v = ip._leg_verdict
    assert v({"state": "confirmed", "algo_status": "NEW", "child_state": "absent"}) == "live"
    assert v({"state": "cancelled"}) == "cancelled"
    assert v({"state": "superseded"}) == "superseded"
    assert v({"state": "submit_pending"}) == "interrupted"
    failed = {
        "state": "confirmed",
        "algo_status": "EXPIRED",
        "child_state": "absent",
        "triggered_at": "2026-10-28T00:00:00+00:00",
    }
    assert v(failed) == "trigger_failed"
    assert v({**failed, "actual_order_id": "9"}) == "triggered"
