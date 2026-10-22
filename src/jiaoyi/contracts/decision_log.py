"""决策日志：每个角色每做一次判断，按同一格式写一行。

团队页（每个角色一张卡、点开看今天每一次判断）和历史页（含被拒信号）都直接读它，
所以字段一旦发布只加不改。持久化见 ``jiaoyi.core.store``（``data/decision_log/YYYY-MM-DD.jsonl``）。

JSON 示例见 ``contracts/examples/decision_record.json``。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import AwareDatetime, Field

from jiaoyi.contracts.common import Contract, NonNegFloat, Role, Symbol

SCHEMA_VERSION = 1


class Outcome(StrEnum):
    PASS = "pass"
    """放行 / 通过。"""
    REJECT = "reject"
    """拒绝。"""
    HOLD = "hold"
    """维持现状。"""
    CLOSE = "close"
    """要求平仓。"""
    ACTION = "action"
    """做了一个动作（下单、挂保命止损、补挂、发告警、出作战卡……）。"""
    ERROR = "error"
    INFO = "info"


class DecisionRecord(Contract):
    schema_version: int = SCHEMA_VERSION
    record_id: str = Field(min_length=1)
    ts: AwareDatetime
    role: Role
    symbol: Symbol | None = None
    signal_id: str | None = None
    position_id: str | None = None
    card_id: str | None = None
    outcome: Outcome
    summary: str = Field(min_length=1, description="一句话中文，团队页卡片直接显示。")
    input: dict[str, Any] = Field(default_factory=dict, description="它看到了什么（可截断）。")
    output: dict[str, Any] = Field(default_factory=dict, description="它怎么判断的。")
    latency_ms: NonNegFloat = 0.0
    cost_usd: NonNegFloat = 0.0
    model_version: str | None = None
