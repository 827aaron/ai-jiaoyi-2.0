"""角色记录器：每次判断写一条 ``DecisionRecord``，顺便累计今天的统计，定时发 ``ModuleStatus``。

快环各角色共用，省得每个模块各写一遍“写日志 + 算今日次数 / 平均耗时 / 花费”。
今日统计按配置的时区（默认曼谷）0 点清零，跨天自动重置。
落盘出错只记日志、不往外抛：日志坏了不能拖住交易回路（但会出现在 ``last_error`` 里）。
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from jiaoyi.contracts import DecisionRecord, Health, ModuleStatus, Outcome, Role, TodayStats
from jiaoyi.interfaces import DecisionLog

logger = logging.getLogger(__name__)


def local_day(now: datetime, tz: str) -> date:
    """``now`` 在 ``tz`` 时区里是哪一天。日亏熔断和今日统计都用它判断跨天。"""
    return now.astimezone(ZoneInfo(tz)).date()


class RoleRecorder:
    def __init__(
        self,
        role: Role,
        log: DecisionLog | None,
        *,
        day_reset_tz: str = "Asia/Bangkok",
        version: str = "",
    ) -> None:
        self.role = role
        self._log = log
        self._tz = day_reset_tz
        self._version = version
        self._day: date | None = None
        self._calls = self._passed = self._rejected = self._errors = 0
        self._latency_total = 0.0
        self._cost = 0.0
        self.last_error: str | None = None

    def record(
        self,
        now: datetime,
        outcome: Outcome,
        summary: str,
        *,
        symbol: str | None = None,
        signal_id: str | None = None,
        position_id: str | None = None,
        card_id: str | None = None,
        input: dict[str, Any] | None = None,
        output: dict[str, Any] | None = None,
        latency_ms: float = 0.0,
        cost_usd: float = 0.0,
        model_version: str | None = None,
    ) -> DecisionRecord:
        self._roll(now)
        self._calls += 1
        if outcome in (Outcome.PASS, Outcome.ACTION, Outcome.CLOSE):
            self._passed += 1
        elif outcome == Outcome.REJECT:
            self._rejected += 1
        elif outcome == Outcome.ERROR:
            self._errors += 1
            self.last_error = summary
        self._latency_total += latency_ms
        self._cost += cost_usd
        rec = DecisionRecord(
            record_id=f"{self.role.value}-{uuid.uuid4().hex[:16]}",
            ts=now,
            role=self.role,
            symbol=symbol,
            signal_id=signal_id,
            position_id=position_id,
            card_id=card_id,
            outcome=outcome,
            summary=summary,
            input=input or {},
            output=output or {},
            latency_ms=max(latency_ms, 0.0),
            cost_usd=max(cost_usd, 0.0),
            model_version=model_version,
        )
        if self._log is not None:
            try:
                self._log.write(rec)
            except Exception as e:  # 日志坏了不能拖住交易
                self.last_error = f"写决策日志失败：{e}"
                logger.exception("写决策日志失败")
        return rec

    def today(self, now: datetime) -> TodayStats:
        self._roll(now)
        return TodayStats(
            calls=self._calls,
            passed=self._passed,
            rejected=self._rejected,
            errors=self._errors,
            avg_latency_ms=self._latency_total / self._calls if self._calls else 0.0,
            cost_usd=self._cost,
        )

    def heartbeat(self, now: datetime, health: Health, headline: str = "") -> ModuleStatus:
        status = ModuleStatus(
            role=self.role,
            health=health,
            ts=now,
            headline=headline,
            today=self.today(now),
            last_error=self.last_error,
            version=self._version,
        )
        if self._log is not None:
            try:
                self._log.heartbeat(status)
            except Exception:
                logger.exception("写模块状态失败")
        return status

    def _roll(self, now: datetime) -> None:
        day = local_day(now, self._tz)
        if day != self._day:
            self._day = day
            self._calls = self._passed = self._rejected = self._errors = 0
            self._latency_total = 0.0
            self._cost = 0.0
