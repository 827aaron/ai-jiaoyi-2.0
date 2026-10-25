"""持仓记录，以及盯仓四层（执行器、持仓管家、巡逻兵、对账员）的输出。

一笔持仓 = 一个 hummingbot-api 开仓执行器 + 一张币安上的保命止损。
“先平一半”用两个执行器实现，所以一个 ``position_id`` 只对应一个 ``executor_id``，
同一笔交易拆成的两个执行器共用 ``group_id``。

JSON 示例见 ``contracts/examples/position_record.json``。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, Field

from jiaoyi.contracts.common import Category, Contract, NonNegFloat, PosFloat, Side, Symbol


class LifeboatStatus(StrEnum):
    PENDING = "pending"
    """开仓已成交，保命止损还没挂上。对账员看到超时会补挂并告警。"""
    ACTIVE = "active"
    CANCELLED = "cancelled"
    TRIGGERED = "triggered"
    MISSING = "missing"
    """应该在但币安上找不到。"""


class LifeboatOrder(Contract):
    """挂在币安服务器上的只减仓 STOP_MARKET 单。"""

    exchange_order_id: str | None = None
    client_order_id: str = Field(min_length=1)
    stop_price: PosFloat
    status: LifeboatStatus
    updated_at: AwareDatetime


class PositionStatus(StrEnum):
    OPENING = "opening"
    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"
    FAILED = "failed"


class CloseReason(StrEnum):
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    TRAILING_STOP = "trailing_stop"
    TIME_LIMIT = "time_limit"
    KEEPER = "keeper"
    PATROL = "patrol"
    LIFEBOAT = "lifeboat"
    MANUAL = "manual"
    RECONCILER = "reconciler"


class PositionRecord(Contract):
    position_id: str = Field(min_length=1)
    group_id: str = Field(min_length=1)
    executor_id: str | None = Field(default=None, description="hummingbot-api 执行器 id。")
    intent_id: str = Field(min_length=1)
    signal_id: str = Field(min_length=1)
    card_id: str = Field(min_length=1)
    symbol: Symbol
    category: Category
    side: Side
    status: PositionStatus
    quantity: PosFloat
    leverage: int = Field(ge=1, le=125)
    entry_price: PosFloat | None = None
    stop_loss_price: PosFloat | None = None
    take_profit_price: PosFloat | None = None
    time_limit_s: int = Field(gt=0)
    lifeboat: LifeboatOrder | None = None
    opened_at: AwareDatetime
    closed_at: AwareDatetime | None = None
    close_reason: CloseReason | None = None
    realized_pnl_usdt: float | None = None
    fees_usdt: NonNegFloat = 0.0
    intended_price: PosFloat | None = Field(
        default=None,
        description="按币安成交均价回填入场价之前账本里的入场价（执行器报的 / 意向价）；"
        "None = 还没回填。",
    )
    exit_price: PosFloat | None = Field(
        default=None,
        description="币安平仓成交均价（含外部 / 手动平仓）；None = 没平或还没回填。",
    )
    stop_loss_pct: PosFloat | None = Field(
        default=None,
        description="风控员算的止损距离（%），开仓时写入，按成交价回填时不变；旧记录为 None。",
    )
    lifeboat_stop_pct: PosFloat | None = Field(
        default=None,
        description="风控员算的保命止损距离（%），开仓时写入，按成交价回填时不变；旧记录为 None。",
    )
    trailing_activation_pct: PosFloat | None = Field(
        default=None,
        description="保本 / 移动止损启动线（%，按扣过开仓手续费的盈亏算），同 OrderIntent；"
        "None = 这笔没带移动止损（或是旧记录）。",
    )
    trailing_stop_pct: PosFloat | None = Field(
        default=None, description="移动止损回撤距离（%），同 OrderIntent。"
    )

    @property
    def notional_usdt(self) -> float:
        return (self.entry_price or 0.0) * self.quantity


class KeeperVerdict(Contract):
    """持仓管家的判断：只能拿住或全平，不能加仓、不能放宽止损。

    Jev 失败时 ``action`` 必须是 ``hold``（维持现状）。平仓要求概率明显过线或连续两次一致，
    ``consecutive`` 记录连续几次给出同一结论。
    """

    position_id: str = Field(min_length=1)
    ts: AwareDatetime
    action: Literal["hold", "close"]
    p_close: float = Field(ge=0.0, le=1.0)
    consecutive: int = Field(ge=1)
    reason: str = ""
    jev_request_id: str | None = None


class PatrolAction(Contract):
    """巡逻兵的动作：只平仓。``symbols`` 为空表示全部持仓。"""

    ts: AwareDatetime
    action: Literal["close"] = "close"
    symbols: list[Symbol] = Field(default_factory=list)
    reason: str = Field(min_length=1)


class ReconcileIssueKind(StrEnum):
    LIFEBOAT_MISSING = "lifeboat_missing"
    ORPHAN_POSITION = "orphan_position"
    """币安上有仓位但没有执行器管。"""
    EXECUTOR_FAILED = "executor_failed"
    QTY_MISMATCH = "qty_mismatch"
    STALE_LIFEBOAT = "stale_lifeboat"
    """仓位已平但保命止损还挂着。"""
    EXTERNAL_CLOSE = "external_close"
    """账本里还开着，币安上已经没仓位了：有人在币安 App / 网页上手动平了。"""
    SIDE_MISMATCH = "side_mismatch"
    """币安上的仓位方向和账本相反（或刚平的仓被反向开了一笔）：多半是平仓单抢跑开出的反手仓。"""


class ReconcileIssue(Contract):
    kind: ReconcileIssueKind
    symbol: Symbol
    position_id: str | None = None
    detail: str = Field(min_length=1)
    fixed: bool = Field(description="是否已自动修正（补挂 / 撤单）。")


class ReconcileReport(Contract):
    """对账员一次三方核对（执行器、币安持仓、保命止损）的结果。"""

    ts: AwareDatetime
    checked_positions: int = Field(ge=0)
    issues: list[ReconcileIssue] = Field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.issues
