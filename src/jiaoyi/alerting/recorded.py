"""告警外面包一层：每推一条在团队页「告警」卡上留一句，今日次数 = 推了几条。

告警是事件驱动的，没有运行周期；启动后没推过也显示「待命」，不显示「等待首次运行」。
Telegram 发没发出去看 ``TelegramAlerter.last_error``（它自己不抛异常）。

``send`` 只入队、马上返回：执行器常在退出锁里发告警，Telegram 一次最长等 5 秒，不能让锁跟着等。
后台单个任务按入队顺序逐条发，冷却去重仍在 Telegram 那层按同一顺序判断。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import datetime
from typing import Any

from jiaoyi.contracts import Alert, AlertLevel, Health, Outcome
from jiaoyi.core.recorder import RoleRecorder
from jiaoyi.interfaces import Alerter

log = logging.getLogger(__name__)

_QUEUE_MAX = 1000
_FLUSH_S = 10.0

_LEVEL_CN = {AlertLevel.INFO: "通知", AlertLevel.WARN: "警告", AlertLevel.CRITICAL: "紧急"}


class RecordedAlerter:
    def __init__(
        self,
        inner: Alerter,
        recorder: RoleRecorder,
        *,
        telegram: bool,
        flush_s: float = _FLUSH_S,
        queue_max: int = _QUEUE_MAX,
    ) -> None:
        """``flush_s``：关闭时最多等这么久把队列里的发完，剩下的只记日志丢掉。
        ``queue_max``：Telegram 长时间不通时最多积压几条，满了新告警只写日志。"""
        self._inner = inner
        self._rec = recorder
        self._telegram = telegram
        self._failed: str | None = None
        self._flush_s = flush_s
        self._queue: asyncio.Queue[Alert] = asyncio.Queue(maxsize=queue_max)
        self._worker: asyncio.Task[None] | None = None

    @property
    def inner(self) -> Alerter:
        return self._inner

    async def send(self, alert: Alert) -> None:
        try:
            self._queue.put_nowait(alert)
        except asyncio.QueueFull:
            log.error("告警积压已满，这条只写日志：%s %s", alert.title, alert.body)
            return
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._drain())

    async def _drain(self) -> None:
        while True:
            alert = await self._queue.get()
            try:
                await self._deliver(alert)
            except Exception:
                log.exception("告警发送失败：%s", alert.title)
            finally:
                self._queue.task_done()

    async def flush(self, timeout_s: float | None = None) -> bool:
        """等队列发完；超时返回 False（队列里的照常在后台继续发）。"""
        try:
            await asyncio.wait_for(self._queue.join(), timeout_s)
        except TimeoutError:
            return False
        return True

    async def _deliver(self, alert: Alert) -> None:
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
        if not await self.flush(self._flush_s):
            left = self._queue.qsize()
            log.error("关闭时还有 %d 条告警没发出去（等了 %.0f 秒），只写日志", left, self._flush_s)
            while not self._queue.empty():
                alert = self._queue.get_nowait()
                log.error("未发出的告警：%s %s", alert.title, alert.body)
                self._queue.task_done()
        if self._worker is not None:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker
            self._worker = None
        close = getattr(self._inner, "close", None)
        if callable(close):
            await close()
