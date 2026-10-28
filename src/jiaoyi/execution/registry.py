"""执行层自己的小账本：哪些持仓是我们开的、执行器 id 是什么、当天起始权益是多少。

重启后靠它认回自己的持仓和保命止损；对账员也拿它当“我们以为有什么”。
单文件 JSON，整文件原子替换（先写临时文件再改名），不会写一半。
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)

from jiaoyi.contracts import OrderIntent, PositionRecord, Side
from jiaoyi.contracts.position import CloseReason, PositionStatus
from jiaoyi.execution.exchange import WorkingType
from jiaoyi.execution.frozen_peak import FrozenPeakExit

LegPurpose = Literal["fixed", "trailing", "lifeboat", "tightened"]
"""条件单腿的来源；新的收紧来源在这里加自己的取值（tightened = 反向作战卡保本收紧价，#314）。"""

OPPOSITE_CARD_FIELDS = (
    "tightened_stop_price",
    "tightened_reason",
    "tightened_at",
    "opposite_anchor_price",
    "opposite_card_key",
    "trailing_distance_pct",
    "opposite_anchor_retry_since",
)
"""反向作战卡（#314）加在 ``ServerProtection`` 上的字段：为空时不落盘，没用过这条规则的账本和
加字段之前一字不差，回退到旧代码（``extra="forbid"``）也能读。用过之后回退先跑
``scripts/opposite_card_rollback.py``。"""


class ProtectiveLeg(BaseModel):
    """每次提交/撤销前持久化的条件单身份；旧代也保留以核对迟到触发。"""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    kind: Literal["stop_loss", "take_profit"]
    purpose: LegPurpose = "fixed"
    generation: int
    client_order_id: str
    quantity: float
    trigger_price: float
    state: Literal[
        "planned",
        "submit_pending",
        "confirmed",
        "cancel_pending",
        "cancelled",
        "rejected",
        "superseded",
    ] = "planned"
    algo_id: str | None = None
    algo_status: str | None = None
    actual_order_id: str | None = None
    actual_order_status: str | None = None
    filled_quantity: float | None = None
    triggered_at: datetime | None = None
    child_state: Literal["absent", "unknown", "identified"] = "unknown"
    acked_at: datetime | None = None
    """本程序提交后交易所回执已接受的时刻；首次回读到后清空。只用于回读宽限，不代表仍在。"""
    working_type: WorkingType = "CONTRACT_PRICE"
    """创建这条腿时定下的触发价格源，提交和每次回读都按它核对；改配置只影响之后新建的腿。
    旧账本没有这一项：保命单按 MARK_PRICE、普通止盈止损按 CONTRACT_PRICE（当时的固定行为）。"""

    @model_validator(mode="before")
    @classmethod
    def _legacy_working_type(cls, data: object) -> object:
        if isinstance(data, dict) and "working_type" not in data:
            lifeboat = data.get("purpose") == "lifeboat"
            data = {**data, "working_type": "MARK_PRICE" if lifeboat else "CONTRACT_PRICE"}
        return data

    @model_validator(mode="after")
    def _lifeboat_mark_price(self) -> ProtectiveLeg:
        if self.purpose == "lifeboat" and self.working_type != "MARK_PRICE":
            raise ValueError("保命单只按标记价触发")
        return self


class ServerProtection(BaseModel):
    """只有新仓显式启用；旧账本默认 None，不能自动改为交易所托管。"""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    version: Literal[1] = 1
    account_name: str
    connector_name: str
    intended_quantity: float
    stage: Literal["entry", "provisional", "armed", "exiting", "cleanup", "done"] = "entry"
    ever_armed: bool = False
    generation: int = 0
    legs: list[ProtectiveLeg] = []
    entry_order_id: str | None = None
    entry_status: str | None = None
    entry_filled_quantity: float | None = None
    entry_terminal: bool = False
    observed_quantity: float | None = None
    observed_at: datetime | None = None
    exit_reason: CloseReason | None = None
    last_error: str | None = None
    prearm_failing_since: datetime | None = None
    """未曾完整托管时，可宽限的同步失败从何时开始连续出现；成功一轮清空。"""
    deadline_at: datetime | None = None
    trailing_peak_net: float | None = None
    trailing_threshold_net: float | None = None
    trailing_peak_net_exact: str | None = None
    trailing_threshold_net_exact: str | None = None
    trailing_activated: bool = False
    trailing_observed_at: datetime | None = None
    trailing_entry_quote: float | None = None
    trailing_entry_quantity: float | None = None
    trailing_fees_quote: float | None = None
    trailing_source_at: datetime | None = None
    trailing_stop_price: float | None = None
    trailing_observation_gap: bool = False
    trailing_last_error: str | None = None
    """最近一轮移动止损观测为什么没完成（缺盘口、盘口过期、证据不一致等）；完成一轮清空。
    和 ``last_error``（普通保护同步）分开记，免得一个盖掉另一个。"""
    trailing_failing_since: datetime | None = None
    """移动止损观测从何时起连续没做成（落盘，重启不重新计时）；做成一轮或进入退出清空。"""
    tightened_stop_price: float | None = None
    """反向作战卡没经价格确认时收紧到的保本止损价（#314，``opposite_profit_mode = "breakeven"``）；
    作为止损候选，只收紧不放松。不改账本止损价（R 仍按原始止损距离算）。"""
    tightened_reason: str | None = None
    """反向卡为什么改了保护（两种模式都写）。"""
    tightened_at: datetime | None = None
    opposite_anchor_price: float | None = None
    """反向卡（#314，``opposite_profit_mode = "trail"``）强制启动移动止损时锚定的可成交价
    （多买一 / 空卖一）；落了就不再锚（换卡版本、重启都不重锚）。"""
    opposite_card_key: str | None = None
    """触发这次改保护的反向指令（币|issued_at|方向）。"""
    trailing_distance_pct: float | None = None
    """移动止损回撤距离（净收益百分点）的覆盖值：反向卡锚定时 = 开仓带的距离 × 倍数；
    None = 用开仓指令里的 ``trailing_stop_pct``。"""
    opposite_anchor_retry_since: datetime | None = None
    """锚定移动止损因盘口疑似插针没做成，从何时起连续重试（落盘，重启不重新计时）。"""

    @model_serializer(mode="wrap")
    def _omit_unused_opposite_fields(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, object]:
        data: dict[str, object] = handler(self)
        for name in OPPOSITE_CARD_FIELDS:
            if data.get(name) is None:
                data.pop(name, None)
        return data


class TrackedPosition(BaseModel):
    """执行层内部结构，不是共享契约。"""

    model_config = ConfigDict(extra="forbid")

    record: PositionRecord
    lifeboat_stop_pct: float
    """开仓指令里的保命止损距离，晚成交（限价单）时用来补挂。"""
    notional_usdt: float = 0.0
    """开仓指令里的名义价值；还没成交（没有入场价）时敞口按它算，不能算成 0。"""
    stop_loss_pct: float
    take_profit_pct: float
    tp_tick: float | None = None
    """限价止盈：开仓时用的价格最小变动（tickSize）；有就按挂单价取整记止盈价。None = 市价止盈。"""
    requested_close: CloseReason | None = None
    """我们主动要求平仓的原因；执行器自己结束（止损、止盈等）时为空。"""
    limit_wait_s: int | None = None
    """挂单等回踩：最多等几秒（从建执行器算），到时没成交撤单。None = 市价进。"""
    hold_s: int | None = None
    """挂单进场时按成交时刻算的最长持仓（执行器的 time_limit 加了等待时间，这里自己卡）。"""
    filled_at: datetime | None = None
    """挂单成交的时间（按它算最长持仓）。"""
    exit_cleanup_pending: bool = False
    entry_client_order_id: str | None = None
    entry_original_quantity: float | None = None
    hb_close_pending: bool = False
    hb_close_unknown: bool = False
    direct_close_client_order_id: str | None = None
    direct_close_generation: int = 0
    direct_close_quantity: float | None = None
    direct_close_order_status: str | None = None
    direct_close_order_id: str | None = None
    direct_close_pending: bool = False
    """直连市价平仓提交意图已落盘；未知/部分成交只核对，不盲目发送第二张。"""
    direct_close_price: float | None = None
    direct_close_fee: float | None = None
    direct_first: bool = False
    """这次平仓走了 ``direct``：先下直连、后停 HB。"""
    close_detail: str | None = None
    """程序自己决定平仓（reconciler 等）的具体原因，写进平仓记录和推送；None = 只有平仓原因。"""
    chase: OrderIntent | None = None
    """挂单到时没成交改市价追进用的指令（8h 以上档）；None = 撤单不开。"""
    server_protection: ServerProtection | None = None
    frozen_peak_exit: FrozenPeakExit | None = None

    @model_serializer(mode="wrap")
    def _omit_in_flight_close_fields(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, object]:
        # 只在平仓进行中落盘：没用到这两项的仓和已结束的仓与加字段之前一致，
        # 回退旧代码（extra="forbid"）仍能读。
        data: dict[str, object] = handler(self)
        ended = self.record.status in (PositionStatus.CLOSED, PositionStatus.FAILED)
        if not self.direct_first or ended:
            data.pop("direct_first", None)
        if self.close_detail is None or ended:
            data.pop("close_detail", None)  # 已写进平仓记录和推送
        return data


class EmergencyExit(BaseModel):
    """对账员对无主仓 / 反手仓的直连退出意图（不是有入场证据的 PositionRecord）。

    先落盘本代 CID 再发；结果未知或未终态只查原 CID，不重发。只有原单明确终态、
    同向余仓不超过剩余上限才建下一代；交易所复查净仓为零才算完成并删除。
    """

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    exit_id: str
    symbol: str
    source: Literal["unknown", "reverse", "side_mismatch"]
    position_id: str | None = None
    """触发它的账本仓（反手 / 方向不对）；无主仓为 None。"""
    side: Side
    """交易所上实际方向，平仓方向由它推出。"""
    initial_quantity: float
    """首次提交时复查到的数量，也是本意图可关闭的上限。"""
    closed_quantity: float = 0.0
    """已终态各代的累计成交。"""
    created_at: datetime
    generation: int = 0
    client_order_id: str | None = None
    quantity: float | None = None
    order_id: str | None = None
    order_status: str | None = None
    pending: bool = False
    """本代已（可能）提交但还没确认终态。"""


class ForeignExecutorStop(BaseModel):
    """对不归在管持仓的执行器（不在账本里，或属于已结束的仓）发过 keep_position=True 停止；
    结果未知默认不重发（可按配置隔几轮再发一次），查到终态才删。"""

    model_config = ConfigDict(extra="forbid")

    executor_id: str
    symbol: str
    requested_at: datetime
    error: str | None = None
    position_id: str | None = None
    """属于我们已结束的哪笔仓；不在账本里为 None。"""
    unresolved_rounds: int = 0
    """停止结果未知后又过了几轮对账仍没确认终止。"""


class DayStart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    day: str
    equity_usdt: float


class RegistryData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    positions: dict[str, TrackedPosition] = {}
    day_start: DayStart | None = None
    last_equity_usdt: float | None = None
    """上一次读到的权益，晚启动又拿不到收入流水时用它给日亏基准兜底。"""
    emergency_exits: dict[str, EmergencyExit] = {}
    """按币种，最多一个未完成的对账员退出意图；未完成时同币禁止新开。"""
    foreign_executor_stops: dict[str, ForeignExecutorStop] = {}
    """按执行器 id。"""


class PositionRegistry:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._symbol_locks: dict[str, asyncio.Lock] = {}
        self._symbol_owners: dict[str, asyncio.Task[object] | None] = {}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self.data = self._load()

    @property
    def path(self) -> Path:
        return self._path

    @asynccontextmanager
    async def symbol_lock(self, symbol: str) -> AsyncIterator[None]:
        """开仓、旧仓退出和条件单共用同币锁；同任务重入避免清理路径死锁。"""
        task = asyncio.current_task()
        if task is not None and self._symbol_owners.get(symbol) is task:
            yield
            return
        async with self._symbol_locks.setdefault(symbol, asyncio.Lock()):
            self._symbol_owners[symbol] = task
            try:
                yield
            finally:
                self._symbol_owners.pop(symbol, None)

    def _load(self) -> RegistryData:
        if not self._path.is_file():
            return RegistryData()
        # 账本损坏时直接抛出：宁可不启动，也不能在不认识自己持仓的情况下开新仓
        return RegistryData.model_validate(json.loads(self._path.read_text(encoding="utf-8")))

    def save(self) -> None:
        with self._lock:
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(self.data.model_dump_json(indent=2), encoding="utf-8")
            os.replace(tmp, self._path)

    def get(self, position_id: str) -> TrackedPosition | None:
        return self.data.positions.get(position_id)

    def by_intent(self, intent_id: str) -> TrackedPosition | None:
        for t in self.data.positions.values():
            if t.record.intent_id == intent_id:
                return t
        return None

    def put(self, tracked: TrackedPosition) -> None:
        self.data.positions[tracked.record.position_id] = tracked
        self.save()

    def live(self) -> list[TrackedPosition]:
        """还没结束的持仓（开仓中、持有中、平仓中）。"""
        done = {PositionStatus.CLOSED, PositionStatus.FAILED}
        return [t for t in self.data.positions.values() if t.record.status not in done]
