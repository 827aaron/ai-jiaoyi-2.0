"""角色记录器：每次判断写一条 ``DecisionRecord``，顺便累计今天的统计，定时发 ``ModuleStatus``。

快环各角色共用，省得每个模块各写一遍“写日志 + 算今日次数 / 平均耗时 / 花费”。
今日统计按配置的时区（默认曼谷）0 点清零，跨天自动重置。
落盘出错只记日志、不往外抛：日志坏了不能拖住交易回路（但会出现在 ``last_error`` 里）。
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from jiaoyi.contracts import DecisionRecord, Health, ModuleStatus, Outcome, Role, TodayStats
from jiaoyi.interfaces import DecisionLog

logger = logging.getLogger(__name__)

SUMMARY_MAX = 120


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
        # 团队页「刚刚：…」：最近一次动作的时间和一句话；有周期的角色另报下次运行时间
        self.last_run_at: datetime | None = None
        self.last_summary: str | None = None
        self.next_run_at: datetime | None = None

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
        self._count(outcome, latency_ms, cost_usd)
        if outcome == Outcome.ERROR:
            self.last_error = summary
        self.last_run_at, self.last_summary = now, summary[:SUMMARY_MAX]
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

    @property
    def tz(self) -> str:
        return self._tz

    @property
    def log(self) -> DecisionLog | None:
        return self._log

    def note(self, now: datetime, summary: str, *, count: bool = False) -> None:
        """只更新「刚刚：…」不写决策记录（例如对账员一轮三方一致，不值得每 30 秒写一条）。

        ``count=True``：这次检查也算进今日次数（团队页「今日 N 次」= 干了几次活）。"""
        self.last_run_at, self.last_summary = now, summary[:SUMMARY_MAX]
        if count:
            self._roll(now)
            self._calls += 1

    def set_next_run(self, at: datetime | None) -> None:
        """按周期干活的角色（管家、对账员）：下次计划什么时候跑，下一次心跳带出去。"""
        self.next_run_at = at

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

    def heartbeat(
        self,
        now: datetime,
        health: Health,
        headline: str = "",
        extra: dict[str, Any] | None = None,
    ) -> ModuleStatus:
        status = ModuleStatus(
            role=self.role,
            health=health,
            ts=now,
            headline=headline,
            today=self.today(now),
            last_error=self.last_error,
            version=self._version,
            **{
                "last_run_at": self.last_run_at,
                "next_run_at": self.next_run_at,
                "summary": self.last_summary,
                **(extra or {}),
            },
        )
        if self._log is not None:
            try:
                self._log.heartbeat(status)
            except Exception:
                logger.exception("写模块状态失败")
        return status

    def _count(self, outcome: Outcome, latency_ms: float, cost_usd: float) -> None:
        self._calls += 1
        if outcome in (Outcome.PASS, Outcome.ACTION, Outcome.CLOSE):
            self._passed += 1
        elif outcome == Outcome.REJECT:
            self._rejected += 1
        elif outcome == Outcome.ERROR:
            self._errors += 1
        self._latency_total += latency_ms
        self._cost += cost_usd

    def _roll(self, now: datetime) -> None:
        day = local_day(now, self._tz)
        if self._day is not None and day < self._day:
            return  # 调用方给的时刻可能比上一次早（一轮开始时刻 vs 写入时刻）：日期只往前走
        if day != self._day:
            first = self._day is None
            self._day = day
            self._calls = self._passed = self._rejected = self._errors = 0
            self._latency_total = 0.0
            self._cost = 0.0
            if first:
                self._restore(day)

    def _restore(self, day: date) -> None:
        """进程重启后，今天的次数和花费从决策日志里捡回来，面板不清零。"""
        read_day = getattr(self._log, "read_day", None)
        if read_day is None:
            return
        try:
            # 本地一天横跨两个 UTC 日（曼谷 0 点 = 前一天 UTC 17 点）
            for utc_day in (day - timedelta(days=1), day):
                for rec in read_day(utc_day.isoformat()):
                    if rec.role == self.role and local_day(rec.ts, self._tz) == day:
                        self._count(rec.outcome, rec.latency_ms, rec.cost_usd)
        except Exception:  # 读不出来就从零开始，不影响交易
            logger.exception("从决策日志恢复今日统计失败")
