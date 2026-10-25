"""全市场强平统计：按标的累计最近一段时间被强平的名义价值，多空分开。

数据来自币安全市场强平流（``!forceOrder@arr``），候选池外的币也算，所以选币能用它给
“近 1 小时强平集中”打分；快环的强平瀑布反弹信号仍用特征引擎里候选池内的 1 分钟数据。
注意币安每个标的每秒最多推一笔强平，这里的数是下限估计。

行情连接在事件循环里写，慢环可能在工作线程（``asyncio.to_thread``）里读，所以读写都加锁；
锁内只做几百次以内的加法，不会卡住事件循环。
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from jiaoyi.contracts import Liquidation, Side

DEFAULT_WINDOW = timedelta(hours=1)


@dataclass(frozen=True)
class LiquidationSummary:
    """某个标的一段时间内的强平汇总（USDT）。"""

    symbol: str
    long_usdt: float
    """被强平的多头（价格下砸）。"""
    short_usdt: float
    """被强平的空头（价格上拉）。"""
    count: int

    @property
    def total_usdt(self) -> float:
        return self.long_usdt + self.short_usdt


class LiquidationTracker:
    def __init__(self, keep: timedelta = DEFAULT_WINDOW) -> None:
        self._keep = keep
        self._q: dict[str, deque[tuple[datetime, float, bool]]] = {}
        self._lock = threading.Lock()
        self._last_sweep: datetime | None = None
        self._born = datetime.now(UTC)

    def feed_minutes(self, now: datetime) -> float:
        """强平数据流已经开着多少分钟（从统计对象创建算）；刚开不久时 0 不代表没有强平。"""
        return max((now - self._born).total_seconds() / 60.0, 0.0)

    def record(self, ev: Liquidation) -> None:
        with self._lock:
            dq = self._q.setdefault(ev.symbol, deque())
            dq.append((ev.ts, ev.price * ev.qty, ev.side == Side.LONG))
            cutoff = ev.ts - self._keep
            while dq and dq[0][0] < cutoff:
                dq.popleft()
            # 不再有新强平的币不会走到上面的裁剪，每隔一个窗口整体清一次
            if self._last_sweep is None or ev.ts - self._last_sweep >= self._keep:
                self._last_sweep = ev.ts
                for sym in [k for k, q in self._q.items() if not q or q[-1][0] < cutoff]:
                    del self._q[sym]

    def stats(
        self, symbol: str, now: datetime, window: timedelta = DEFAULT_WINDOW
    ) -> LiquidationSummary:
        long_u = short_u = 0.0
        n = 0
        with self._lock:
            rows = list(self._q.get(symbol, ()))
        for ts, usdt, was_long in rows:
            if now - window <= ts <= now:
                n += 1
                if was_long:
                    long_u += usdt
                else:
                    short_u += usdt
        return LiquidationSummary(symbol, long_u, short_u, n)

    def liquidations_1h_usdt(self, symbol: str, now: datetime) -> float:
        """选币要的那个数：近 1 小时强平总额。没有数据返回 0。"""
        return self.stats(symbol, now, DEFAULT_WINDOW).total_usdt

    def all_stats(
        self, now: datetime, window: timedelta = DEFAULT_WINDOW
    ) -> list[LiquidationSummary]:
        """所有有记录的标的，按强平总额从大到小。"""
        with self._lock:
            symbols = list(self._q)
        out = [self.stats(sym, now, window) for sym in symbols]
        return sorted((s for s in out if s.count), key=lambda s: -s.total_usdt)
