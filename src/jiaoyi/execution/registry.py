"""执行层自己的小账本：哪些持仓是我们开的、执行器 id 是什么、当天起始权益是多少。

重启后靠它认回自己的持仓和保命止损；对账员也拿它当“我们以为有什么”。
单文件 JSON，整文件原子替换（先写临时文件再改名），不会写一半。
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from jiaoyi.contracts import PositionRecord
from jiaoyi.contracts.position import CloseReason, PositionStatus


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
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self.data = self._load()

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
