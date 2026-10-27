"""止损观测独立验收：展示证据与缺口，不能由参考价制造真实执行器状态。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from jiaoyi.panel.stop_diagnostics import parse_diagnostics

NOW = datetime(2026, 10, 27, 16, 0, tzinfo=UTC)
OPENED = NOW - timedelta(minutes=20)


def observed(**changes: Any) -> dict[str, Any]:
    raw = {
        "schema_version": 1,
        "executor_id": "ex-synthetic",
        "source": "hb_runtime",
        "capability": "position_executor_stop_v1",
        "runtime_epoch": "a" * 32,
        "executor_instance_epoch": "b" * 32,
        "observed_at": NOW.isoformat(),
        "check_at": (NOW - timedelta(seconds=1)).isoformat(),
        "check_seq": 4,
        "price_basis": "bid",
        "decision_price": "101.2",
        "bid": "101.2",
        "ask": None,
        "mark_price": None,
        "net_pnl_pct": "0.011",
        "net_pnl_observed": True,
        "stop_check_performed": True,
        "stop_loss_net_pnl_pct": "-0.01",
        "trailing_active": True,
        "actual_trailing_trigger_net_pnl_pct": "0.008",
        "coverage_since": OPENED.isoformat(),
        "coverage_complete": False,
        "gap_detected": True,
        "dropped": 0,
        "write_errors": 0,
        "events": [],
    }
    raw.update(changes)
    return raw


def parse(raw: object) -> Any:
    return parse_diagnostics(raw, "ex-synthetic", NOW, OPENED)


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": True},
        {"schema_version": 2},
        {"executor_id": "other"},
        {"source": "cached_candles"},
        {"capability": None},
        {"capability": "unknown-version"},
        {"runtime_epoch": None},
        {"executor_instance_epoch": "not-an-epoch"},
    ],
)
def test_diagnostic_identity_or_old_image_cannot_claim_an_active_stop(
    changes: dict[str, Any],
) -> None:
    result = parse(observed(**changes))
    assert result.status == "unknown"
    assert result.trailing_active is None and result.actual_trailing_trigger_net_pnl_pct is None
    assert result.decision_price is None and result.events == []


@pytest.mark.parametrize("raw", [None, {}, [], {"trailing_active": True, "stop_price": 101.0}])
def test_no_runtime_observation_does_not_invent_state_from_a_price(raw: object) -> None:
    result = parse(raw)
    assert result.status == "unknown" and result.trailing_active is None
    assert result.coverage_complete is False


@pytest.mark.parametrize("field", ["check_at", "observed_at"])
def test_new_fetch_does_not_make_an_old_check_current(field: str) -> None:
    changes = {field: (NOW - timedelta(seconds=16)).isoformat()}
    if field == "observed_at":
        changes["check_at"] = changes[field]
    result = parse(observed(**changes))
    assert result.status == "stale"
    assert result.trailing_active is None and result.decision_price is None


@pytest.mark.parametrize(
    "changes",
    [
        {"check_at": (NOW + timedelta(seconds=1)).isoformat()},
        {"observed_at": (NOW + timedelta(seconds=1)).isoformat()},
        {"check_at": (OPENED - timedelta(seconds=1)).isoformat()},
        {"check_at": NOW.replace(tzinfo=None).isoformat()},
        {"observed_at": (NOW - timedelta(seconds=2)).isoformat()},
        {"check_seq": True},
        {"check_seq": 0},
    ],
)
def test_impossible_or_unattributed_runtime_clock_is_unknown(changes: dict[str, Any]) -> None:
    result = parse(observed(**changes))
    assert result.status == "unknown" and result.trailing_active is None


@pytest.mark.parametrize("threshold", [None, "NaN", float("inf"), True])
def test_active_without_a_finite_runtime_trigger_is_unknown(threshold: object) -> None:
    result = parse(observed(actual_trailing_trigger_net_pnl_pct=threshold))
    assert result.status == "unknown" and result.trailing_active is None


@pytest.mark.parametrize("observed_net", [False, None, "missing"])
def test_control_cycle_without_a_net_pnl_read_is_not_a_fresh_stop_check(
    observed_net: object,
) -> None:
    raw = observed(net_pnl_observed=observed_net)
    if observed_net == "missing":
        raw.pop("net_pnl_observed")
    result = parse(raw)
    assert result.status == "unknown" and result.trailing_active is None


def test_failed_check_does_not_refresh_last_armed_state() -> None:
    result = parse(observed(check_failed=True))
    assert result.status == "unknown" and result.trailing_active is None


@pytest.mark.parametrize("performed", [False, None, "missing"])
def test_nonbarrier_net_read_is_not_a_performed_stop_check(performed: object) -> None:
    raw = observed(stop_check_performed=performed)
    if performed == "missing":
        raw.pop("stop_check_performed")
    result = parse(raw)
    assert result.status == "unknown" and result.trailing_active is None


def test_runtime_threshold_zero_is_valid_and_unobserved_quote_side_stays_unknown() -> None:
    # 当前执行器用 if not trigger 判未启动：0 仍走未启动分支，不能改成 is not None。
    result = parse(
        observed(
            trailing_active=False,
            actual_trailing_trigger_net_pnl_pct=0,
            coverage_complete=True,
            gap_detected=False,
        )
    )
    assert result.status == "current" and result.trailing_active is False
    assert result.actual_trailing_trigger_net_pnl_pct == 0
    assert result.bid == pytest.approx(101.2)
    assert result.ask is None and result.mark_price is None


def test_current_unarmed_state_does_not_claim_never_armed_over_missing_history() -> None:
    result = parse(observed(trailing_active=False, actual_trailing_trigger_net_pnl_pct=None))
    assert result.status == "current" and result.trailing_active is False
    assert result.coverage_complete is False and result.gap_detected is True


@pytest.mark.parametrize("value", [0, -1, True, float("nan"), float("inf")])
def test_invalid_prices_are_not_market_observations(value: object) -> None:
    result = parse(observed(decision_price=value, bid=value, ask=value, mark_price=value))
    assert result.decision_price is None and result.bid is None
    assert result.ask is None and result.mark_price is None
    assert result.stop_loss_net_pnl_pct == -0.01  # 阈值允许负数，不能和价格同判。


@pytest.mark.parametrize(
    "changes",
    [
        {"gap_detected": True},
        {"dropped": 1},
        {"write_errors": 1},
        {"coverage_since": None},
        {"coverage_since": (OPENED + timedelta(seconds=1)).isoformat()},
    ],
)
def test_incomplete_coverage_cannot_be_presented_as_complete(changes: dict[str, Any]) -> None:
    raw = observed(coverage_complete=True, gap_detected=False)
    raw.update(changes)
    result = parse(raw)
    assert result.coverage_complete is False


def test_events_distinguish_close_request_and_return_from_a_fill() -> None:
    events = [
        {
            "ts": (NOW - timedelta(seconds=2)).isoformat(),
            "stage": stage,
            "kind": "trailing_stop",
            "price": 101.2,
            "threshold": 0.008,
        }
        for stage in ("check", "close_request", "close_return", "close_error")
    ]
    events += [
        {"ts": (OPENED - timedelta(seconds=1)).isoformat(), "stage": "check"},
        {"ts": (NOW + timedelta(seconds=1)).isoformat(), "stage": "close_return"},
        {"ts": NOW.isoformat(), "stage": "fill", "kind": "unverified"},
    ]
    result = parse(observed(events=events))
    assert [e.stage for e in result.events] == [
        "check",
        "close_request",
        "close_return",
        "close_error",
    ]


def test_diagnostics_do_not_expose_arbitrary_upstream_strings() -> None:
    raw = observed(
        debug="PRIVATE_SENTINEL", account="PRIVATE_SENTINEL", request_url="PRIVATE_SENTINEL"
    )
    raw["events"] = [
        {
            "ts": NOW.isoformat(),
            "stage": "check",
            "kind": "PRIVATE_SENTINEL",
            "exception": "PRIVATE_SENTINEL",
            "request": {"password": "PRIVATE_SENTINEL"},
        }
    ]
    result = parse(raw)
    assert "PRIVATE_SENTINEL" not in result.model_dump_json()
    assert result.events[0].kind == "stop_check"


def test_order_submission_error_remains_an_error_not_a_fill() -> None:
    raw = observed(
        events=[
            {
                "ts": NOW.isoformat(),
                "stage": "order_error",
                "kind": "native_order",
                "error_type": "SyntheticOrderRejected",
                "private_response": "PRIVATE_SENTINEL",
            }
        ]
    )
    result = parse(raw)
    assert len(result.events) == 1
    assert result.events[0].stage == "order_error"
    assert result.events[0].kind == "native_order"
    assert result.events[0].fill_amount is None
    assert "PRIVATE_SENTINEL" not in result.model_dump_json()
