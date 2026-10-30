"""交易所托管保护单的只读投影：读 positions.json 里的 server_protection，不连交易所。

面板不导入执行层，这里按落盘 JSON 的字段自己解析；读不懂的一律归“未知”，不猜。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel

from jiaoyi.core.trailing_price import activation_price

LegState = Literal["live", "triggered", "cancelled", "rejected", "pending", "unknown", "none"]
LegRole = Literal["lifeboat", "stop_loss", "take_profit", "trailing_native"]

_TRIGGERED_ALGO = {"TRIGGERING", "TRIGGERED", "FINISHED"}
_CLOSED_ALGO = {"CANCELED", "CANCELLED", "EXPIRED", "REJECTED"}
_PENDING = {"planned", "submit_pending", "cancel_pending"}
_ROLE_ORDER: tuple[LegRole, ...] = ("lifeboat", "stop_loss", "take_profit", "trailing_native")
_WORKING_TYPES = {"MARK_PRICE", "CONTRACT_PRICE"}
_TRAIL_BASIS = ("trailing_entry_quote", "trailing_entry_quantity", "trailing_fees_quote")


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
    working_type: str | None = None
    """这条腿在交易所按哪个价格触发：``MARK_PRICE`` 标记价 / ``CONTRACT_PRICE`` 合约成交价。
    旧账本没记的按当时固定行为补（保命单标记价、其余成交价）；记了但读不懂的为 None。"""
    previous: LegView | None = None
    """最新一代还没在挂（计划 / 提交中 / 被拒 / 状态不明）时，同一角色仍在挂的上一代。
    换移动止损价时旧单要等新单确认才撤，这段时间真正在保护的是它；没有就是 None。"""
    activate_price: float | None = None
    """交易所移动止损（``trailing_native``）的激活价；None = 下单即激活。这种腿的
    ``trigger_price`` 不是触发价，面板不该当触发价画。"""
    callback_rate: float | None = None
    """交易所移动止损的回撤比例（%）。"""


class ProtectionView(BaseModel):
    stage: str
    ever_armed: bool
    legs: list[LegView]
    trailing_activated: bool = False
    trailing_stop_price: float | None = None
    trailing_activation_price: float | None = None
    """观察价（多仓买一 / 空仓卖一）越过它程序就激活移动止损；按首次观测落盘的成交额、数量、
    实收手续费算，还没观测过（没有这些基准）为 None。"""
    tightened_stop_price: float | None = None
    """反向作战卡没经价格确认时程序收紧到的止损目标价（#314）；交易所是否已换上看止损腿。"""
    tightened_reason: str | None = None
    tightened_at: datetime | None = None
    opposite_anchor_price: float | None = None
    """反向作战卡（trail 模式）强制启动移动止损时锚定的可成交价（#314）。"""
    trailing_distance_pct: float | None = None
    """锚定后这笔仓移动止损用的回撤距离（净收益百分点）。"""
    native_trailing: bool = False
    """交易所移动止损（币安跟价）在挂或已触发：这笔仓按设计不挂保命单，程序也不换代止损腿。"""
    native_pending: bool = False
    """该挂交易所移动止损、还没挂上（最多等 30 秒）：这段只有固定止损，按设计也不挂保命单。"""
    native_activated: bool = False
    native_stop_price: float | None = None
    """本地按币安同一规则推算的交易所移动止损当前价（币安不报）；未激活为 None。"""
    native_fallback: str | None = None
    """这笔仓为什么没用上交易所移动止损（照旧保命单 + 程序自己跟）。"""
    observed_at: datetime | None = None
    age_s: float | None = None
    stale: bool = False
    """观测时间距今超过 ``protection_stale_s``，或根本没有观测时间；阈值 0 = 不标记。"""
    last_error: str | None = None
    trailing_error: str | None = None
    trailing_error_book: bool = False
    """``trailing_error`` 是不是“这一轮没有可用盘口 / 标记价”（这种情况已挂的保护不受影响）。"""
    """最近一轮移动止损观测没做成的原因（和普通保护同步的 ``last_error`` 分开）；做成一轮清空。"""


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


def _trigger_failed(leg: dict[str, object]) -> bool:
    """触发了但交易所没下出子单（REJECTED/EXPIRED、无子单、无成交）。"""
    return bool(
        leg.get("triggered_at")
        and leg.get("algo_status") in _CLOSED_ALGO
        and leg.get("child_state") == "absent"
        and not leg.get("actual_order_id")
        and not _num(leg.get("filled_quantity"))
    )


def _triggered(leg: dict[str, object]) -> bool:
    filled = _num(leg.get("filled_quantity"))
    if _trigger_failed(leg):
        return False  # 和执行层一致，不算触发
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
    if kind == "trailing_stop":
        return "trailing_native"
    return "take_profit" if kind == "take_profit" else None


def _gen(leg: dict[str, object]) -> float:
    return _num(leg.get("generation")) or 0.0


def _working_type(leg: dict[str, object]) -> str | None:
    if "working_type" not in leg:  # 旧账本：和执行层读旧账本时的补法一致
        return "MARK_PRICE" if leg.get("purpose") == "lifeboat" else "CONTRACT_PRICE"
    wt = leg.get("working_type")
    return wt if isinstance(wt, str) and wt in _WORKING_TYPES else None


def _leg_view(
    role: LegRole, leg: dict[str, object] | None, previous: LegView | None = None
) -> LegView:
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
    elif _trigger_failed(leg):
        detail = "触发后被拒（无子单）"
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
        working_type=_working_type(leg),
        previous=previous,
        activate_price=_num(leg.get("activate_price")),
        callback_rate=_num(leg.get("callback_rate")),
    )


def parse_protection(
    raw: object,
    now: datetime,
    stale_s: int,
    long: bool | None = None,
    activation_pct: float | None = None,
) -> ProtectionView | None:
    """``TrackedPosition.server_protection`` 的落盘 JSON → 面板视图；旧仓位（None）返回 None。"""
    if not isinstance(raw, dict):
        return None
    basis = [_num(raw.get(k)) for k in _TRAIL_BASIS]
    act_price = None
    if long is not None and activation_pct is not None and None not in basis:
        quote, quantity, fee = (v or 0.0 for v in basis)
        act_price = activation_price(long, quote, quantity, fee, activation_pct)
    legs_raw = [x for x in raw.get("legs") or [] if isinstance(x, dict)]
    legs: list[LegView] = []
    for role in _ROLE_ORDER:
        mine = [x for x in legs_raw if _role_of(x) == role]
        if role in ("take_profit", "trailing_native") and all(
            leg_state(x) in {"cancelled", "rejected"} and not _trigger_failed(x) for x in mine
        ):
            # 不挂止盈单，旧版本挂的撤掉后不再显示；交易所移动止损没用上（被拒 / 已撤）也不显示，
            # 原因见 ``native_fallback``。触发后被拒的照旧亮出来。
            continue
        # 任何一代被触发都要亮出来（迟到触发）；否则看最新一代
        hit = [x for x in mine if _triggered(x)]
        pick = hit[-1] if hit else max(mine, key=_gen, default=None)
        previous = None
        if pick is not None and leg_state(pick) not in {"live", "triggered"}:
            # 新一代还没挂上时旧一代仍在挂（交易所先确认新单才撤旧单），一并亮出来
            older = [x for x in mine if _gen(x) < _gen(pick) and leg_state(x) == "live"]
            if older:
                previous = _leg_view(role, max(older, key=_gen))
        legs.append(_leg_view(role, pick, previous))
    observed = _time(raw.get("observed_at"))
    age = max(0.0, (now - observed).total_seconds()) if observed else None
    err = raw.get("last_error")
    # 只在已武装时显示：进入退出 / 清理后移动止损不再观测，旧原因没有意义。
    trailing_err = raw.get("trailing_last_error") if raw.get("stage") == "armed" else None
    native_leg = next((x for x in legs if x.role == "trailing_native"), None)
    native_on = native_leg is not None and native_leg.state in {"live", "triggered"}
    return ProtectionView(
        stage=str(raw.get("stage") or "entry"),
        ever_armed=bool(raw.get("ever_armed")),
        legs=legs,
        trailing_activated=bool(raw.get("trailing_activated")),
        trailing_stop_price=_num(raw.get("trailing_stop_price")),
        trailing_activation_price=act_price,
        tightened_stop_price=_num(raw.get("tightened_stop_price")),
        tightened_reason=str(raw["tightened_reason"]) if raw.get("tightened_reason") else None,
        tightened_at=_time(raw.get("tightened_at")),
        opposite_anchor_price=_num(raw.get("opposite_anchor_price")),
        trailing_distance_pct=_num(raw.get("trailing_distance_pct")),
        native_trailing=native_on,
        native_pending=not native_on
        and bool(raw.get("native_pending_since"))
        and not raw.get("native_fallback"),
        native_activated=native_on and bool(raw.get("native_activated")),
        native_stop_price=_num(raw.get("native_stop_price")) if native_on else None,
        native_fallback=str(raw["native_fallback"]) if raw.get("native_fallback") else None,
        observed_at=observed.astimezone(UTC) if observed else None,
        age_s=age,
        stale=bool(stale_s > 0 and (age is None or age > stale_s)),
        last_error=str(err) if err else None,
        trailing_error=str(trailing_err) if trailing_err else None,
        trailing_error_book=bool(
            trailing_err and str(trailing_err).startswith("TrailingBookUnavailable")
        ),
    )


def lifeboat_summary(p: ProtectionView) -> tuple[str, float | None]:
    """并进旧的 ``lifeboat_status`` / ``lifeboat_price``：已挂但数据过期单独标出，不当作在位。

    交易所移动止损在挂时按设计不挂保命单：没有在挂的保命单就是 ``native``（不算缺）。"""
    leg = next(x for x in p.legs if x.role == "lifeboat")
    if leg.state != "live" and leg.previous is not None:
        leg = leg.previous  # 新一代保命单还没挂上，旧一代仍在挂
    if leg.state == "live":
        return ("stale" if p.stale else "active"), leg.trigger_price
    if (p.native_trailing or p.native_pending) and leg.state in {"none", "cancelled"}:
        return "native", None
    status = {
        "triggered": "triggered",
        "cancelled": "cancelled",
        "rejected": "missing",
        "pending": "pending",
    }.get(leg.state, "unknown")
    return status, leg.trigger_price
