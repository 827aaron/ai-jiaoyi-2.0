"""告警外面包一层：每推一条在团队页「告警」卡上留一句，今日次数 = 推了几条。

告警是事件驱动的，没有运行周期；启动后没推过也显示「待命」，不显示「等待首次运行」。
Telegram 发没发出去看 ``TelegramAlerter.last_error``（它自己不抛异常）。
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from jiaoyi.contracts import Alert, AlertLevel, Health, Outcome
from jiaoyi.core.recorder import RoleRecorder
from jiaoyi.interfaces import Alerter

_LEVEL_CN = {AlertLevel.INFO: "通知", AlertLevel.WARN: "警告", AlertLevel.CRITICAL: "紧急"}


class RecordedAlerter:
    def __init__(self, inner: Alerter, recorder: RoleRecorder, *, telegram: bool) -> None:
        self._inner = inner
        self._rec = recorder
        self._telegram = telegram
        self._failed: str | None = None

    @property
    def inner(self) -> Alerter:
        return self._inner

    async def send(self, alert: Alert) -> None:
        start = time.perf_counter()
        await self._inner.send(alert)
        ms = (time.perf_counter() - start) * 1000.0
        if getattr(self._inner, "last_skipped", False):
            return  # 同一件事冷却期内没再发
        where = alert.symbol or ""
        title = f"{alert.title}{f'（{where}）' if where and where not in alert.title else ''}"
        # 团队页点开这条：看到的 = 触发的事件；判断 = 发出去的正文和结果
        seen: dict[str, Any] = {"事件": alert.title, "级别": _LEVEL_CN[alert.level]}
        if alert.symbol:
            seen["币"] = alert.symbol
        if alert.body:
            seen["内容"] = alert.body
        fmt = getattr(self._inner, "format", None)
        text = fmt(alert) if callable(fmt) else f"{alert.title}\n{alert.body}".strip()
        kw: dict[str, Any] = {"symbol": alert.symbol, "input": seen, "latency_ms": round(ms, 1)}
        if not self._telegram:
            out = {"Telegram 正文": text, "结果": "Telegram 未配置，只写了日志"}
            self._rec.record(
                alert.ts, Outcome.INFO, f"{title}：Telegram 未配置，只写了日志", output=out, **kw
            )
            return
        error = getattr(self._inner, "last_error", None)
        self._failed = error
        result = f"发送失败：{error}" if error else "已发送"
        out = {"Telegram 正文": text, "结果": result, "耗时": f"{ms:.0f} ms"}
        if error:
            summary, outcome = f"{title}：Telegram 发送失败（{error}）", Outcome.ERROR
        else:
            summary, outcome = f"{title}：已发 Telegram", Outcome.ACTION
        self._rec.record(alert.ts, outcome, summary, output=out, **kw)

    def heartbeat(self, now: datetime) -> None:
        if not self._telegram:
            health, headline = Health.DEGRADED, "Telegram 未配置，告警只写日志"
        elif self._failed:
            health, headline = Health.DEGRADED, f"Telegram 上一条发送失败：{self._failed}"
        else:
            n = self._rec.today(now).calls
            health, headline = (
                Health.OK,
                (f"正常 · 今日推了 {n} 条" if n else "待命 · Telegram 已配置"),
            )
        self._rec.heartbeat(now, health, headline)

    async def close(self) -> None:
        close = getattr(self._inner, "close", None)
        if callable(close):
            await close()
