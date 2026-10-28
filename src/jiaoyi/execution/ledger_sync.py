"""账本和收入流水的「实时」刷新：用户数据流来成交回报就马上跑，没有回报每 60 秒兜底一次。

一次刷新 = 按币安成交回填账本（``HbExecutorAdapter.backfill_fills``）+ 重算
``data/income_daily.json``。回报只是「叫醒」，真正的数从 REST 接口拉，回报丢了也不会错账。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime

from jiaoyi.execution.clock import Clock, utc_now
from jiaoyi.execution.fills import FILLS_REFRESH_S, UserTrades
from jiaoyi.execution.hb_executor import HbExecutorAdapter
from jiaoyi.execution.income_daily import IncomeDaily
from jiaoyi.execution.user_stream import StreamStatus, UserStream

log = logging.getLogger(__name__)

SETTLE_S = 1.5
"""回报到了等一下再查成交明细：userTrades 比推送晚一点点。"""


class LedgerSync:
    def __init__(
        self,
        adapter: HbExecutorAdapter,
        trades: UserTrades,
        income: IncomeDaily | None,
        stream: UserStream | None = None,
        *,
        clock: Clock = utc_now,
    ) -> None:
        self._adapter = adapter
        self._trades = trades
        self._income = income
        self._stream = stream
        self._clock = clock
        self._wake = asyncio.Event()
        self.last_run_at: datetime | None = None

    @property
    def income(self) -> IncomeDaily | None:
        return self._income

    @property
    def trades(self) -> UserTrades:
        return self._trades

    def attach(self, stream: UserStream) -> None:
        self._stream = stream

    def poke(self) -> None:
        """有成交 / 平仓了：马上刷新一次（主循环平仓后、用户数据流收到回报时调）。"""
        self._wake.set()

    async def on_fill(self) -> None:
        self.poke()

    @property
    def stream_status(self) -> StreamStatus | None:
        return self._stream.status if self._stream is not None else None

    async def refresh(self) -> None:
        try:
            n = await self._adapter.backfill_fills(self._trades)
            if n:
                log.info("账本按币安成交回填了 %d 笔", n)
        except Exception:
            log.exception("账本回填失败")
        if self._income is not None:
            try:
                await self._income.refresh(self._clock())
            except Exception:
                log.exception("收入流水刷新失败")
        self.last_run_at = self._clock()
        status = self.stream_status
        self._adapter.fills_note = status.text() if status is not None else "成交回报只走 60 秒轮询"

    async def run(self) -> None:
        """一直跑到被取消：等回报或 60 秒，刷新；同时跑用户数据流。"""
        stream = asyncio.create_task(self._stream.run()) if self._stream is not None else None
        try:
            while True:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), FILLS_REFRESH_S)
                if self._wake.is_set():
                    self._wake.clear()
                    await asyncio.sleep(SETTLE_S)
                    self._wake.clear()  # 等待期间来的回报并进这一次
                await self.refresh()
        finally:
            if stream is not None:
                stream.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await stream
