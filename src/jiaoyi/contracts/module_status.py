"""模块状态（团队页的亮灯和今日要点）与告警。

每个角色定时上报一条 ``ModuleStatus``；持久化见 ``jiaoyi.core.store``
（``data/module_status.json`` 存每个角色最新一条）。

JSON 示例见 ``contracts/examples/module_status.json``。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import AwareDatetime, Field

from jiaoyi.contracts.common import Contract, NonNegFloat, Role, Symbol


class Health(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"
    """能跑但有问题，例如 Jev 慢、行情断线重连中。"""
    DOWN = "down"
    DISABLED = "disabled"
    """被开关关掉（例如预测员）。"""
    STARTING = "starting"


class TodayStats(Contract):
    """当天（按 day_reset_tz 清零）的统计。"""

    calls: int = Field(default=0, ge=0)
    passed: int = Field(default=0, ge=0)
    rejected: int = Field(default=0, ge=0)
    errors: int = Field(default=0, ge=0)
    avg_latency_ms: NonNegFloat = 0.0
    cost_usd: NonNegFloat = 0.0


class ModuleStatus(Contract):
    role: Role
    health: Health
    ts: AwareDatetime = Field(description="最后一次心跳。超过 3 个周期没更新，面板视为 down。")
    headline: str = Field(default="", description="今天的一句话要点。")
    today: TodayStats = Field(default_factory=TodayStats)
    last_error: str | None = None
    version: str = ""
    last_run_at: AwareDatetime | None = Field(
        default=None,
        description="上次干活的时间：按计划周期的角色（选币）是上次成功跑完；"
        "快环角色是最近一次写决策记录（有动作）的时间。",
    )
    next_run_at: AwareDatetime | None = Field(
        default=None, description="按周期干活的角色（选币、管家、对账员）：下次计划运行的时间。"
    )
    summary: str | None = Field(
        default=None, description="最近一次动作的一句话（团队页「刚刚：…」），没有动作过为 None。"
    )
    symbol_count: int | None = Field(default=None, ge=0, description="选币：上次选出的币数。")
    error: str | None = Field(
        default=None, description="当前还没恢复的失败原因（上次运行失败）；正常时为 None。"
    )


class AlertLevel(StrEnum):
    INFO = "info"
    WARN = "warn"
    CRITICAL = "critical"


class Alert(Contract):
    """发 Telegram 的告警（保命止损缺失、Jev 超时、熔断、切实盘等）。"""

    ts: AwareDatetime
    level: AlertLevel
    role: Role
    title: str = Field(min_length=1)
    body: str = ""
    symbol: Symbol | None = None
    dedup_key: str | None = Field(default=None, description="同一键短时间内只发一次。")
