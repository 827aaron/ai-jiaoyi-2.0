"""移动止损 / 冻结峰值：判断时用的盘口必须是新鲜的；缺盘口和真保护失败分开告警、分开记错。

只有合成订单和假时钟，不连接任何服务。每次假 REST 调用让时钟前进一点，模拟 Demo 的慢回读。
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest

from jiaoyi.contracts import Side
from jiaoyi.contracts.module_status import AlertLevel
from jiaoyi.execution.brackets import BracketManager
from jiaoyi.execution.exchange import EntryBasis, ExchangeError, ExecutionBook
from jiaoyi.execution.hb_executor import HbExecutorAdapter
from jiaoyi.execution.trailing import TrailingBookUnavailable
from tests.test_execution import START
from tests.test_server_protection_regression import AdapterHarness

LATENCY_S = 0.8
"""每次假 REST 调用耗时；锁内 7 次（入场 2 + 三腿 3 + 检查点 2）共 5.6 秒，超过 5 秒新鲜度。"""


class SlowHarness(AdapterHarness):
    """时钟随假 REST 调用和假 sleep 前进；盘口按“读到那一刻”的时钟打源时刻。"""

    def __init__(self, root: Path, side: Side, *, latency: float = LATENCY_S, grace: float = 0):
        self.now, self.latency, self.grace = START, 0.0, grace
        self.sleeps, self.visible_after = 0, 2
        super().__init__(root, side)
        tracked = self.registry.get("pos-1")
        self.registry.put(
            tracked.model_copy(
                update={
                    "record": tracked.record.model_copy(
                        update={"trailing_activation_pct": 0.3, "trailing_stop_pct": 0.5}
                    )
                }
            )
        )
        self.book_reads: list[ExecutionBook] = []
        self.book_error: Exception | None = None
        self.book_lag = 0.0
        ex = self.exchange
        for name in ("entry_order", "positions_evidence"):
            setattr(ex, name, self._slow(getattr(ex, name)))
        original_get = ex._get

        def slow_get(algo_id, client_order_id):
            self.now += timedelta(seconds=self.latency)
            return original_get(algo_id, client_order_id)

        ex._get = slow_get
        ex.entry_basis = self.entry_basis
        ex.execution_book = self.execution_book
        self.latency = latency

    def _slow(self, call):
        async def slow(*args, **kwargs):
            self.now += timedelta(seconds=self.latency)
            return await call(*args, **kwargs)

        return slow

    def build(self) -> BracketManager:
        self.exchange.registry = self.registry
        return BracketManager(
            self.exchange,
            self.registry,
            account_name="synthetic-account",
            connector_name="synthetic-demo",
            clock=lambda: self.now,
            readback_grace_s=self.grace,
            sleep=self.sleep,
        )

    def make_adapter(self) -> HbExecutorAdapter:
        adapter = super().make_adapter()
        adapter._clock = lambda: self.now
        return adapter

    async def sleep(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)
        self.sleeps += 1
        if self.sleeps >= self.visible_after:
            self.exchange.hidden.clear()

    async def entry_basis(self, symbol: str, client_order_id: str) -> EntryBasis:
        buy = self.registry.get("pos-1").record.side is Side.LONG
        return EntryBasis(
            "BTCUSDT", "entry-1", "100", "buy" if buy else "sell", 2.0, 200.0, 0.2, 100.0, "FILLED"
        )

    async def execution_book(self, symbol: str) -> ExecutionBook:
        self.now += timedelta(seconds=self.latency)
        if self.book_error is not None:
            raise self.book_error
        source = self.now - timedelta(seconds=self.book_lag)
        book = ExecutionBook(symbol, 100.4, 100.5, source, self.now)
        self.book_reads.append(book)
        return book

    async def armed(self) -> None:
        latency, self.latency = self.latency, 0.0
        assert await self.manager.sync("pos-1") == "armed"
        self.latency = latency

    def titles(self) -> list[str]:
        return [a.title for a in self.alerter.sent]


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_slow_readback_inside_lock_does_not_stale_the_trailing_book(
    tmp_path: Path, side: Side
) -> None:
    # 线上：每轮都报“移动止损盘口过期”，峰值从不更新。锁内 7 次 REST 比 5 秒还长。
    h = SlowHarness(tmp_path, side)
    await h.armed()
    await h.adapter.protection_tick()
    state = h.state()
    assert h.titles() == []
    assert state.trailing_source_at == h.book_reads[-1].source_at
    assert state.trailing_peak_net is not None and state.trailing_last_error is None
    assert state.stage == "armed" and state.last_error is None


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_readback_grace_wait_inside_lock_still_decides_on_a_fresh_book(
    tmp_path: Path, side: Side
) -> None:
    # protection_readback_grace_s = 3：刚回执的腿查无时锁内最多再等 3 秒。
    h = SlowHarness(tmp_path, side, latency=0.5, grace=3.0)
    await h.armed()
    state = h.state()
    lb = next(leg for leg in state.legs if leg.client_order_id.startswith("lb-"))
    h.manager._save_leg("pos-1", lb.model_copy(update={"acked_at": h.now}))
    h.exchange.hidden.add(lb.client_order_id)
    started = h.now
    basis = await h.entry_basis("BTCUSDT", "entry-1")
    assert await h.manager.observe_trailing("pos-1", basis, h.execution_book) is False
    assert h.sleeps == 2  # 确实在锁内按回读宽限等过
    assert (h.now - started).total_seconds() > 5  # 锁内确实走了超过 5 秒
    assert h.state().trailing_source_at == h.book_reads[-1].source_at
    assert h.state().trailing_last_error is None


@pytest.mark.parametrize("lag", [5.5, -1.0])
async def test_genuinely_stale_or_future_book_is_still_rejected(tmp_path: Path, lag: float) -> None:
    h = SlowHarness(tmp_path, Side.LONG, latency=0.0)
    await h.armed()
    h.book_lag = lag
    basis = await h.entry_basis("BTCUSDT", "entry-1")
    with pytest.raises(TrailingBookUnavailable):
        await h.manager.observe_trailing("pos-1", basis, h.execution_book)
    state = h.state()
    assert state.trailing_source_at is None and state.trailing_peak_net is None
    assert state.trailing_last_error is not None and "盘口" in state.trailing_last_error


async def test_book_alert_has_its_own_key_and_does_not_hide_real_protection_failure(
    tmp_path: Path,
) -> None:
    h = SlowHarness(tmp_path, Side.LONG, latency=0.0)
    await h.armed()
    h.book_error = ExchangeError("同环境执行盘口证据未知", transient=True)
    await h.adapter.protection_tick()
    book_alert = h.alerter.sent[-1]
    assert book_alert.level is AlertLevel.WARN
    assert book_alert.dedup_key == "server_trailing_book:pos-1"
    assert "同环境执行盘口证据未知" in book_alert.body
    h.book_error = None
    # 冷却期内紧接着出真问题：止盈腿查无（不在回执宽限内）。它必须用自己的键推出去。
    tp = next(cid for cid in h.exchange.orders if cid.startswith("br-tp-"))
    h.exchange.hidden.add(tp)
    await h.adapter.protection_tick()
    real = [a for a in h.alerter.sent if a.title == "托管保护本轮未确认"]
    assert len(real) == 1 and real[0].level is AlertLevel.CRITICAL
    assert real[0].dedup_key == "server_tick:pos-1" != book_alert.dedup_key
    assert "已提交条件单暂时不可查" in real[0].body
    assert h.state().trailing_last_error is not None
    assert "已提交条件单暂时不可查" in h.state().trailing_last_error


async def test_trailing_failure_is_recorded_for_the_panel_and_cleared_after_success(
    tmp_path: Path,
) -> None:
    h = SlowHarness(tmp_path, Side.LONG, latency=0.0)
    await h.armed()
    h.book_error = ExchangeError("同环境执行盘口证据未知")
    await h.adapter.protection_tick()
    state = h.state()
    assert state.trailing_last_error == (
        "TrailingBookUnavailable: 移动止损盘口读取失败 <- ExchangeError: 同环境执行盘口证据未知"
    )
    assert state.last_error is None  # 普通保护同步没坏，不混进它的错误
    h.book_error = None
    await h.adapter.protection_tick()
    assert h.state().trailing_last_error is None
    assert h.state().trailing_source_at is not None


async def test_frozen_peak_rereads_book_once_when_waiting_for_the_lock_made_it_stale(
    tmp_path: Path,
) -> None:
    from tests.test_execution import START as RIG_START
    from tests.test_execution import Rig
    from tests.test_frozen_peak_exit_integration import install

    rig = Rig(tmp_path)
    rec = install(rig)
    reads: list[ExecutionBook] = []
    entered, release = asyncio.Event(), asyncio.Event()

    async def book(_symbol: str) -> ExecutionBook:
        quote = ExecutionBook(rec.symbol, 102, 102.1, rig.t, rig.t)
        reads.append(quote)
        entered.set()
        return quote

    async def protection_round() -> None:
        # 同币托管保护那一轮正占着锁：回读宽限 + 慢 REST，放锁时已过 6 秒。
        async with rig.registry.symbol_lock(rec.symbol):
            await entered.wait()
            await release.wait()
            rig.t = RIG_START + timedelta(seconds=6)

    rig.adapter._exchange.execution_book.side_effect = book
    holder = asyncio.create_task(protection_round())
    await asyncio.sleep(0)
    review = asyncio.create_task(rig.adapter.frozen_peak_review(rec.position_id, RIG_START))
    await entered.wait()
    release.set()
    await holder
    result = await review
    assert len(reads) == 2 and reads[-1].source_at == RIG_START + timedelta(seconds=6)
    assert result is not None and result["detail"] == "" and result["peak_price"] == 102
    assert result["last_source_at"] == reads[-1].source_at.isoformat().replace("+00:00", "Z")


async def test_frozen_peak_still_rejects_a_book_stale_after_the_reread(tmp_path: Path) -> None:
    from tests.test_execution import START as RIG_START
    from tests.test_execution import Rig
    from tests.test_frozen_peak_exit_integration import install

    rig = Rig(tmp_path)
    rec = install(rig)
    reads: list[ExecutionBook] = []

    async def lagging(_symbol: str) -> ExecutionBook:
        quote = ExecutionBook(rec.symbol, 102, 102.1, rig.t, rig.t)
        reads.append(quote)
        rig.t += timedelta(seconds=6)  # 交易所源时刻一直落后 6 秒：不是等锁造成的
        return quote

    rig.adapter._exchange.execution_book.side_effect = lagging
    result = await rig.adapter.frozen_peak_review(rec.position_id, RIG_START)
    assert len(reads) == 2  # 只重读一次，不无限重试
    assert result is not None and result["peak_price"] is None
    assert result["detail"].startswith("evidence_unavailable:ExchangeError:")
