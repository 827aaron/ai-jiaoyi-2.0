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

from pydantic import BaseModel, ConfigDict

from jiaoyi.contracts import OrderIntent, PositionRecord
from jiaoyi.contracts.position import CloseReason, PositionStatus
from jiaoyi.execution.frozen_peak import FrozenPeakExit


class ProtectiveLeg(BaseModel):
    """每次提交/撤销前持久化的条件单身份；旧代也保留以核对迟到触发。"""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    kind: Literal["stop_loss", "take_profit"]
    purpose: Literal["fixed", "trailing", "lifeboat"] = "fixed"
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
    chase: OrderIntent | None = None
    """挂单到时没成交改市价追进用的指令（8h 以上档）；None = 撤单不开。"""
    server_protection: ServerProtection | None = None
    frozen_peak_exit: FrozenPeakExit | None = None


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


class PositionRegistry:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._symbol_locks: dict[str, asyncio.Lock] = {}
        self._symbol_owners: dict[str, asyncio.Task[object] | None] = {}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self.data = self._load()

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
