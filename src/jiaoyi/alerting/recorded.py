"""告警外面包一层：每推一条在团队页「告警」卡上留一句，今日次数 = 推了几条。

告警是事件驱动的，没有运行周期；启动后没推过也显示「待命」，不显示「等待首次运行」。
Telegram 发没发出去看 ``TelegramAlerter.last_error``（它自己不抛异常）。
"""

from __future__ import annotations

from datetime import datetime

from jiaoyi.contracts import Alert, Health, Outcome
from jiaoyi.core.recorder import RoleRecorder
from jiaoyi.interfaces import Alerter


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
        await self._inner.send(alert)
        if getattr(self._inner, "last_skipped", False):
            return  # 同一件事冷却期内没再发
        where = alert.symbol or ""
        title = f"{alert.title}{f'（{where}）' if where and where not in alert.title else ''}"
        if not self._telegram:
            self._rec.record(alert.ts, Outcome.INFO, f"{title}：Telegram 未配置，只写了日志")
            return
        error = getattr(self._inner, "last_error", None)
        self._failed = error
        if error:
            self._rec.record(alert.ts, Outcome.ERROR, f"{title}：Telegram 发送失败（{error}）")
        else:
            self._rec.record(alert.ts, Outcome.ACTION, f"{title}：已发 Telegram")

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
