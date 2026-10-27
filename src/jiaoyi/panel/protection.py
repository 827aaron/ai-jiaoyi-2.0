"""交易所托管保护单的只读投影：读 positions.json 里的 server_protection，不连交易所。

面板不导入执行层，这里按落盘 JSON 的字段自己解析；读不懂的一律归“未知”，不猜。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel

LegState = Literal["live", "triggered", "cancelled", "rejected", "pending", "unknown", "none"]
LegRole = Literal["lifeboat", "stop_loss", "take_profit"]

_TRIGGERED_ALGO = {"TRIGGERING", "TRIGGERED", "FINISHED"}
_CLOSED_ALGO = {"CANCELED", "CANCELLED", "EXPIRED", "REJECTED"}
_PENDING = {"planned", "submit_pending", "cancel_pending"}
_ROLE_ORDER: tuple[LegRole, ...] = ("lifeboat", "stop_loss", "take_profit")


class LegView(BaseModel):
    role: LegRole
    purpose: str | None = None
    """fixed / trailing / lifeboat；没有这条腿时为 None。"""
    state: LegState
    trigger_price: float | None = None
    """交易所已确认（含已触发、已撤）那条腿的触发价；还没确认的不填，见 ``planned_price``。"""
    planned_price: float | None = None
    """只是计划（planned / 提交中 / 撤销中），不代表交易所上有这张单。"""
    quantity: float | None = None
    algo_status: str | None = None
    child_state: str | None = None
    detail: str = ""


class ProtectionView(BaseModel):
    stage: str
    ever_armed: bool
    legs: list[LegView]
    trailing_activated: bool = False
    trailing_stop_price: float | None = None
    observed_at: datetime | None = None
    age_s: float | None = None
    stale: bool = False
    """观测时间距今超过 ``protection_stale_s``，或根本没有观测时间；阈值 0 = 不标记。"""
    last_error: str | None = None


def _num(v: object) -> float | None:
    if isinstance(v, bool) or not isinstance(v, int | float):
        return None
    return float(v)


def _time(v: object) -> datetime | None:
    if not isinstance(v, str):
        return None
    try:
        t = datetime.fromisoformat(v)
    except ValueError:
        return None
    return t if t.tzinfo else None


def _triggered(leg: dict[str, object]) -> bool:
    filled = _num(leg.get("filled_quantity"))
    return bool(
        leg.get("algo_status") in _TRIGGERED_ALGO
        or leg.get("actual_order_id")
        or leg.get("triggered_at")
        or (filled is not None and filled > 0)
    )


def leg_state(leg: dict[str, object]) -> LegState:
    """单条腿的状态：只有 confirmed + NEW + 子单确认不存在才算“已挂”。"""
    state, algo, child = leg.get("state"), leg.get("algo_status"), leg.get("child_state")
    if _triggered(leg):
        return "triggered"
    if state in _PENDING:
        return "pending"
    if state == "rejected":
        return "rejected"
    if state in {"cancelled", "superseded"}:
        return "cancelled"
    if state == "confirmed":
        if algo == "NEW" and child == "absent":
            return "live"
        if algo in _CLOSED_ALGO and child == "absent":
            return "cancelled"
    return "unknown"


def _role_of(leg: dict[str, object]) -> LegRole | None:
    if leg.get("purpose") == "lifeboat":
        return "lifeboat"
    kind = leg.get("kind")
    if kind == "stop_loss":
        return "stop_loss"
    return "take_profit" if kind == "take_profit" else None


def _gen(leg: dict[str, object]) -> float:
    return _num(leg.get("generation")) or 0.0


def _leg_view(role: LegRole, leg: dict[str, object] | None) -> LegView:
    if leg is None:
        return LegView(role=role, state="none")
    st = leg_state(leg)
    price = _num(leg.get("trigger_price"))
    # 已提交过的腿（confirmed / 已撤 / 被替代 / 已触发）价格是交易所上真有过的；其余只是计划
    real = leg.get("state") in {"confirmed", "cancelled", "superseded"} or st == "triggered"
    detail = ""
    if st == "pending":
        detail = "撤销中，未确认" if leg.get("state") == "cancel_pending" else "提交中，未确认"
    elif st == "unknown":
        detail = "子单状态未确认" if leg.get("child_state") != "absent" else "状态不明"
    return LegView(
        role=role,
        purpose=str(leg.get("purpose") or "fixed"),
        state=st,
        trigger_price=price if real else None,
        planned_price=None if real else price,
        quantity=_num(leg.get("quantity")),
        algo_status=str(leg["algo_status"]) if leg.get("algo_status") else None,
        child_state=str(leg["child_state"]) if leg.get("child_state") else None,
        detail=detail,
    )


def parse_protection(raw: object, now: datetime, stale_s: int) -> ProtectionView | None:
    """``TrackedPosition.server_protection`` 的落盘 JSON → 面板视图；旧仓位（None）返回 None。"""
    if not isinstance(raw, dict):
        return None
    legs_raw = [x for x in raw.get("legs") or [] if isinstance(x, dict)]
    legs: list[LegView] = []
    for role in _ROLE_ORDER:
        mine = [x for x in legs_raw if _role_of(x) == role]
        # 任何一代被触发都要亮出来（迟到触发）；否则看最新一代
        hit = [x for x in mine if _triggered(x)]
        pick = hit[-1] if hit else max(mine, key=_gen, default=None)
        legs.append(_leg_view(role, pick))
    observed = _time(raw.get("observed_at"))
    age = max(0.0, (now - observed).total_seconds()) if observed else None
    err = raw.get("last_error")
    return ProtectionView(
        stage=str(raw.get("stage") or "entry"),
        ever_armed=bool(raw.get("ever_armed")),
        legs=legs,
        trailing_activated=bool(raw.get("trailing_activated")),
        trailing_stop_price=_num(raw.get("trailing_stop_price")),
        observed_at=observed.astimezone(UTC) if observed else None,
        age_s=age,
        stale=bool(stale_s > 0 and (age is None or age > stale_s)),
        last_error=str(err) if err else None,
    )


def lifeboat_summary(p: ProtectionView) -> tuple[str, float | None]:
    """并进旧的 ``lifeboat_status`` / ``lifeboat_price``：已挂但数据过期单独标出，不当作在位。"""
    leg = next(x for x in p.legs if x.role == "lifeboat")
    if leg.state == "live":
        return ("stale" if p.stale else "active"), leg.trigger_price
    status = {
        "triggered": "triggered",
        "cancelled": "cancelled",
        "rejected": "missing",
        "pending": "pending",
    }.get(leg.state, "unknown")
    return status, leg.trigger_price
