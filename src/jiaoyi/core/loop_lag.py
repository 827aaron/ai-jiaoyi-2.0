"""事件循环有没有被卡住：一个任务每秒睡一次，醒来比该醒的晚多少就是这一刻的延迟。

行情、下单、Telegram 都在同一个事件循环上；循环被纯 Python 计算占满时，本机连接也会超时
（127.0.0.1:8000 ConnectTimeout），从外面看像网络问题。延迟数字能直接区分这两种。
每分钟汇总一次：最大延迟到 ``WARN_S`` 记一条警告（默认日志级别是 WARNING），面板读
``market_snapshot.json`` 里的 ``loop_lag``。

另外把 ``faulthandler`` 挂在 SIGUSR1 上：``kill -USR1 <pid>`` 把所有线程的栈追加写进
``data/logs/faulthandler.log``，卡住时不用停进程就能看在忙什么。
"""

from __future__ import annotations

import asyncio
import faulthandler
import logging
import signal
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any

log = logging.getLogger(__name__)

TICK_S = 1.0
REPORT_EVERY_S = 60.0
WARN_S = 1.0
STACKS_FILE = "faulthandler.log"


class LoopLagMonitor:
    def __init__(self, *, tick_s: float = TICK_S, report_every_s: float = REPORT_EVERY_S) -> None:
        self._tick = tick_s
        self._report_every = report_every_s
        self.last_s: float | None = None
        self._window: list[float] = []
        self.summary: dict[str, Any] | None = None
        """上一分钟的汇总：``max_ms`` / ``avg_ms`` / ``samples``，还没满一分钟是 None。"""

    def status(self) -> dict[str, Any]:
        last = None if self.last_s is None else round(self.last_s * 1000.0, 1)
        return {"last_ms": last, "last_minute": self.summary}

    def record(self, lag_s: float) -> None:
        self.last_s = lag_s
        self._window.append(lag_s)
        if len(self._window) * self._tick < self._report_every:
            return
        lags, self._window = self._window, []
        worst = max(lags)
        self.summary = {
            "max_ms": round(worst * 1000.0, 1),
            "avg_ms": round(sum(lags) / len(lags) * 1000.0, 1),
            "samples": len(lags),
        }
        if worst >= WARN_S:
            log.warning(
                "事件循环被卡：最近 %d 秒最大延迟 %.1f 秒、平均 %.0f 毫秒",
                round(len(lags) * self._tick),
                worst,
                self.summary["avg_ms"],
            )

    async def run(self, clock: Callable[[], float] | None = None) -> None:
        now = clock or asyncio.get_running_loop().time
        while True:
            start = now()
            await asyncio.sleep(self._tick)
            self.record(max(now() - start - self._tick, 0.0))


def register_stack_dump(log_dir: Path) -> IO[str] | None:
    """SIGUSR1 → 所有线程的栈追加进 ``log_dir/faulthandler.log``。返回的文件要一直开着
    （faulthandler 只记文件描述符）；平台不支持或打不开返回 None。"""
    sig = getattr(signal, "SIGUSR1", None)
    if sig is None:
        return None
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        f = (log_dir / STACKS_FILE).open("a", encoding="utf-8")
    except OSError as e:
        log.warning("faulthandler 文件打不开：%s", e)
        return None
    try:
        faulthandler.register(sig, file=f, all_threads=True)
    except (RuntimeError, ValueError, OSError) as e:
        f.close()
        log.warning("faulthandler 挂不上 SIGUSR1：%s", e)
        return None
    return f
