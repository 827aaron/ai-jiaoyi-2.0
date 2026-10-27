"""只读执行器观测投影；不由行情推断止损状态，不参与任何交易判断。"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

FRESH_S = 15.0
MAX_EVENTS = 100


class StopEvent(BaseModel):
    ts: datetime
    stage: Literal[
        "check",
        "close_request",
        "close_return",
        "close_error",
        "order_request",
        "order_return",
        "order_error",
        "fill",
        "order_completed",
    ]
    kind: str
    price: float | None = None
    threshold: float | None = None
    triggered: bool | None = None
    condition_met: bool | None = None
    close_requested: bool | None = None
    check_action: Literal["activation", "close"] | None = None
    comparison: Literal["<", "<=", ">"] | None = None
    check_seq: int | None = None
    fill_amount: float | None = None


class StopDiagnostics(BaseModel):
    status: Literal["current", "stale", "unknown"] = "unknown"
    reason: str = "执行器尚未提供止损观测（旧镜像也可能没有此能力）"
    source: str | None = None
    observed_at: datetime | None = None
    check_at: datetime | None = None
    check_seq: int | None = None
    price_basis: str | None = None
    decision_price: float | None = None
    bid: float | None = None
    ask: float | None = None
    mark_price: float | None = None
    net_pnl_pct: float | None = None
    stop_loss_net_pnl_pct: float | None = None
    trailing_active: bool | None = None
    actual_trailing_trigger_net_pnl_pct: float | None = None
    coverage_since: datetime | None = None
    coverage_complete: bool = False
    gap_detected: bool = True
    dropped: int = 0
    write_errors: int = 0
    oversize: int = 0
    observer_errors: int = 0
    event_overflow: int = 0
    events: list[StopEvent] = Field(default_factory=list)


def _time(value: object) -> datetime | None:
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo is not None and dt.utcoffset() is not None else None
    except (ValueError, OverflowError):
        return None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (ValueError, OverflowError):
        return None


def _count(value: object) -> int:
    return min(value, 2**31 - 1) if type(value) is int and value >= 0 else 0


def parse_diagnostics(
    raw: object, executor_id: str | None, now: datetime, opened_at: datetime
) -> StopDiagnostics:
    """只接收同一执行器、已知能力的白名单字段；15 秒为显示质量期限。"""
    out = StopDiagnostics()
    if not executor_id or not isinstance(raw, dict):
        return out
    if (
        type(raw.get("schema_version")) is not int
        or raw.get("schema_version") != 1
        or raw.get("executor_id") != executor_id
        or raw.get("source") != "hb_runtime"
        or raw.get("capability") != "position_executor_stop_v1"
    ):
        out.reason = "执行器观测身份或能力不匹配"
        return out
    for field in ("runtime_epoch", "executor_instance_epoch"):
        epoch = raw.get(field)
        if (
            not isinstance(epoch, str)
            or len(epoch) != 32
            or any(c not in "0123456789abcdef" for c in epoch)
        ):
            out.reason = "执行器缺少有效进程或实例标识"
            return out
    out.source = "hb_runtime"
    out.observed_at, out.check_at = _time(raw.get("observed_at")), _time(raw.get("check_at"))
    out.coverage_since = _time(raw.get("coverage_since"))
    out.gap_detected = raw.get("gap_detected") is not False
    out.dropped, out.write_errors = _count(raw.get("dropped")), _count(raw.get("write_errors"))
    out.oversize = _count(raw.get("oversize"))
    out.observer_errors = _count(raw.get("observer_errors"))
    out.event_overflow = _count(raw.get("event_overflow"))
    out.coverage_complete = (
        raw.get("coverage_complete") is True
        and not out.gap_detected
        and not out.dropped
        and not out.write_errors
        and not out.oversize
        and not out.observer_errors
        and out.coverage_since is not None
        and out.coverage_since <= opened_at
    )
    out.check_seq = _count(raw.get("check_seq")) or None
    checks, history = raw.get("checks"), raw.get("events")
    events = (checks[-MAX_EVENTS:] if isinstance(checks, list) else []) + (
        history[-MAX_EVENTS:] if isinstance(history, list) else []
    )
    if events:
        for e in events:
            if not isinstance(e, dict):
                continue
            ts, stage = _time(e.get("ts")), e.get("stage")
            if ts is None or not opened_at <= ts <= now:
                continue
            if stage not in (
                "check",
                "close_request",
                "close_return",
                "close_error",
                "order_request",
                "order_return",
                "order_error",
                "fill",
                "order_completed",
            ):
                continue
            kind = e.get("kind")
            if stage == "fill" and kind != "native_order_filled":
                continue
            if stage == "order_completed" and kind != "native_order_completed":
                continue
            # 字符串不透传，避免上游异常正文、账户或订单信息进入网页。
            kind = (
                kind
                if kind
                in (
                    "stop_loss",
                    "trailing_stop",
                    "take_profit",
                    "time_limit",
                    "native_order_filled",
                    "native_order_completed",
                    "native_order",
                )
                else "stop_check"
            )
            out.events.append(
                StopEvent(
                    ts=ts,
                    stage=stage,
                    kind=kind,
                    price=_number(e.get("price")),
                    threshold=_number(e.get("threshold")),
                    triggered=e["triggered"] if type(e.get("triggered")) is bool else None,
                    condition_met=e["condition_met"]
                    if type(e.get("condition_met")) is bool
                    else None,
                    close_requested=e["close_requested"]
                    if type(e.get("close_requested")) is bool
                    else None,
                    check_action=e["check_action"]
                    if e.get("check_action") in ("activation", "close")
                    else None,
                    comparison=e["comparison"] if e.get("comparison") in ("<", "<=", ">") else None,
                    check_seq=_count(e.get("check_seq")) or None,
                    fill_amount=_number(e.get("fill_amount")),
                )
            )
        out.events.sort(key=lambda e: e.ts)
        out.events = out.events[-MAX_EVENTS:]
    if raw.get("check_failed") is True:
        out.reason = "执行器本次控制检查出错"
        return out
    if raw.get("net_pnl_observed") is not True:
        out.reason = "本轮控制未读取实际净收益，不能确认止损检查结果"
        return out
    if raw.get("stop_check_performed") is not True:
        out.reason = "本轮未确认实际执行止损条件检查"
        return out
    if out.observed_at is None or out.check_at is None or out.check_seq is None:
        out.reason = "缺少实际止损检查时间或采样序号"
        return out
    if out.check_at > out.observed_at:
        out.reason = "止损检查时间晚于观测时间"
        return out
    ages = [(now - ts).total_seconds() for ts in (out.observed_at, out.check_at)]
    if min(ages) < 0 or min(out.observed_at, out.check_at) < opened_at:
        out.reason = "止损观测时间无效或早于本次开仓"
        return out
    if max(ages) > FRESH_S:
        out.status, out.reason = "stale", "执行器止损观测已过期（超过 15 秒）"
        return out
    if type(raw.get("trailing_active")) is not bool:
        out.reason = "执行器尚未确认移动止损状态"
        return out
    trigger = _number(raw.get("actual_trailing_trigger_net_pnl_pct"))
    if raw["trailing_active"] and trigger is None:
        out.reason = "移动止损已启动但缺少有效触发阈值"
        return out
    if bool(trigger) != raw["trailing_active"]:
        out.reason = "移动止损状态与执行器实际阈值矛盾"
        return out
    out.status, out.reason = "current", "执行器实际检查观测"
    out.trailing_active = raw["trailing_active"]
    basis = raw.get("price_basis")
    out.price_basis = (
        basis
        if basis
        in (
            "mark",
            "mark_price",
            "bid",
            "ask",
            "best_bid",
            "best_ask",
            "mid",
            "mid_price",
            "last",
            "last_price",
        )
        else "unknown"
    )
    for key in (
        "decision_price",
        "bid",
        "ask",
        "mark_price",
        "net_pnl_pct",
        "stop_loss_net_pnl_pct",
        "actual_trailing_trigger_net_pnl_pct",
    ):
        value = _number(raw.get(key))
        if (
            key in ("decision_price", "bid", "ask", "mark_price")
            and value is not None
            and value <= 0
        ):
            value = None
        setattr(out, key, value)
    return out
