"""主循环：把行情事件分发给快环各角色，并跑盯仓、对账、选币、开会这些定时任务。

一笔交易的路径（设计文档 §三）::

    行情事件 → 特征 → 巡逻兵（有动作就平仓）→ 成交事件后哨兵出信号
    → 拍板人 → 保镖 → 放行才交给执行器开仓（执行器成交后自己挂保命止损）

定时任务：
- 持仓管家：每 ``keeper_interval_s`` 秒复查每个持仓；巡逻兵标记的急查每秒看一次。
- 对账员：每 ``reconcile_interval_s`` 秒。
- 选币和下单精度：每小时一次，池子变了就重新订阅（新标的先回填 K 线）。
- 军师团：每分钟问一次 ``refresh_reason``（卡快到期、关键位突破、新入池……），
  另外距上次开会超过 ``slow_loop_interval_min`` 也会开；开会在后台跑，快环不等。
- 新闻 / 公告：每 2 分钟拉一次（docs/slow.md），大消息让军师团提前开会，
  币安下架公告封掉该币的新开仓。

这里只做分发和调度，判断都在各角色里。任何一个任务出错只记日志，不拖垮其它任务。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from jiaoyi.app import App, is_stub
from jiaoyi.contracts import (
    AlertLevel,
    CandidateSignal,
    Interval,
    Kline,
    MarketEvent,
    Outcome,
    PositionRecord,
    PositionStatus,
    Role,
    Side,
    SymbolPool,
    Trade,
)
from jiaoyi.contracts.position import CloseReason
from jiaoyi.core.commands import CommandInbox, CommandResult, PanelCommand
from jiaoyi.market.features import backfill_minutes
from jiaoyi.market.rest import fetch_recent_klines

log = logging.getLogger(__name__)

SCREEN_EVERY = timedelta(hours=1)
SLOW_CHECK_EVERY_S = 60.0
URGENT_CHECK_EVERY_S = 1.0
HEARTBEAT_EVERY_S = 30.0
NEWS_POLL_EVERY_S = 120.0
COMMAND_POLL_EVERY_S = 1.0
RESTART_WAIT_CLOSE_S = 60.0
# 决定重启后：收尾最多等这么久；军师团开会跑在工作线程里取消不掉，收尾超时就直接退出
RESTART_SHUTDOWN_S = 20.0
# 取消任务前先等已经在跑的平仓 / 对账做完（不再发起新的），最多这么久
RESTART_DRAIN_S = 15.0
RESTART_HARD_EXIT_S = 45.0
TASK_CANCEL_WAIT_S = 10.0
PRUNE_EVERY = timedelta(hours=1)
BACKFILL_CONCURRENCY = 5


@runtime_checkable
class _Forgets(Protocol):
    def forget(self, position_id: str) -> None: ...


@runtime_checkable
class _HasUrgent(Protocol):
    urgent: set[str]


@runtime_checkable
class _NewsAware(Protocol):
    def notify_news(self, symbol: str) -> None: ...

    def block_symbol(self, symbol: str, reason: str) -> None: ...


@runtime_checkable
class _RefreshGate(Protocol):
    """军师团开会的总闸：失败退避 / 日花费上限，以及要推送的提示。"""

    def may_refresh(self, now: datetime) -> bool: ...

    def take_notice(self) -> str | None: ...


@runtime_checkable
class _RefreshReason(Protocol):
    def refresh_reason(
        self, now: datetime, pool: SymbolPool, prices: Mapping[str, float]
    ) -> str | None: ...


@runtime_checkable
class _HoldsAware(Protocol):
    """开会前告诉军师团手上有哪些持仓币：它们要一直留在卡上，不然持仓管家会判“不再盯”。"""

    def set_held(self, held: Mapping[str, Side]) -> None: ...


KlineFetcher = Callable[[str], Awaitable[list[Kline]]]
PREDICTOR_INTERVAL = Interval.M5


class Runner:
    def __init__(
        self,
        app: App,
        *,
        fetch_klines: KlineFetcher | None = None,
        fetch_predictor_klines: KlineFetcher | None = None,
        restart_check: Callable[[], str | None] | None = None,
        restart_exit: Callable[[], None] | None = None,
    ) -> None:
        """``restart_check``：面板要求重启前的检查，返回不能重启的原因（None = 可以）。
        由命令行给：要在 launchd 托管下（退出后会被拉起），且新配置能通过校验。不给 = 不支持重启。

        ``restart_exit``：重启时真正结束进程（命令行给 ``os._exit(0)``）。不能靠 ``asyncio.run``
        正常返回：军师团开会跑在工作线程里，解释器退出前会一直等它开完（十几分钟）。
        """
        self.app = app
        self.m = app.m
        # 回填够算 ATR 的 1 分钟 K 线（15 分钟 ATR 要 240 根）
        limit = backfill_minutes(app.app_cfg.fast_loop.atr_period_min)
        self._fetch_klines = fetch_klines or (lambda sym: fetch_recent_klines(sym, limit=limit))
        # 预测员要的 5 分钟线（至少 64 根），启动时一次拉够，不用等 5 个多小时
        bars = app.app_cfg.predictor.backfill_bars
        self._fetch_predictor_klines = fetch_predictor_klines or (
            lambda sym: fetch_recent_klines(sym, limit=bars, interval=PREDICTOR_INTERVAL)
        )
        self.positions: list[PositionRecord] = []
        self._tasks: set[asyncio.Task[None]] = set()
        self._loops: list[asyncio.Task[None]] = []
        self._last_screen: datetime | None = None
        self._last_card_at: datetime | None = None
        self._refreshing = False
        # 读账户 → 保镖 → 开仓 必须串行，否则并发信号都按“开仓前”的账户算，绕过笔数和敞口上限
        self._open_lock = asyncio.Lock()
        self._closing: set[str] = set()
        self._last_prune: datetime | None = None
        # 面板“重启生效”：不开新仓，等在途动作做完再退出（launchd KeepAlive 拉起）
        self._restart_check = restart_check
        self._restarting = False
        # 重启已定：不再发起新的平仓 / 对账（交给重启后的进程，期间靠币安上的保命止损）
        self._leaving = False
        self._reconciling = False
        self._restart = asyncio.Event()
        self.restart_requested = False
        self._restart_exit = restart_exit
        # restart 的回执在进程真要退出前一刻才写（面板看到“完成”时进程确实在退）
        self._restart_receipt: tuple[CommandInbox, Path, CommandResult] | None = None
        self._exit_lock = threading.Lock()
        self._exited = False
        self._started_at: datetime | None = None

    # ------------------------------------------------------------ 生命周期
    async def run(self) -> None:
        """启动并一直跑，直到被取消，或面板发来 restart（之后 ``restart_requested`` 为 True）。"""
        self._started_at = self.app.clock()
        await self.app.start()
        self._last_screen = self.app.clock()
        if self.app.pool is not None:
            await self.backfill([e.symbol for e in self.app.pool.in_pool])
        await self.refresh_positions()
        # 重启后先拉一轮公告，下架封禁在第一个信号之前就生效
        try:
            await self.news_tick()
        except Exception:
            log.exception("启动拉新闻失败，主循环里再拉")
        cfg = self.app.app_cfg
        self._loops = [
            asyncio.create_task(self._market_loop()),
            self._every(cfg.keeper_interval_s, self.keeper_tick),
            self._every(URGENT_CHECK_EVERY_S, self.urgent_tick),
            self._every(cfg.reconcile_interval_s, self.reconcile_tick),
            self._every(SLOW_CHECK_EVERY_S, self.slow_tick),
            self._every(HEARTBEAT_EVERY_S, self._heartbeat_tick),
            self._every(NEWS_POLL_EVERY_S, self.news_tick),
            self._every(COMMAND_POLL_EVERY_S, self.command_tick),
        ]
        loops: asyncio.Future[Any] = asyncio.gather(*self._loops)
        stop: asyncio.Future[Any] = asyncio.ensure_future(self._restart.wait())
        try:
            await asyncio.wait([loops, stop], return_when=asyncio.FIRST_COMPLETED)
            if loops.done():
                loops.result()  # 循环异常退出：照旧抛出去
            self.restart_requested = stop.done()
        finally:
            stop.cancel()
            if self.restart_requested:
                await self._shutdown_for_restart()
            else:
                await self.shutdown()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await loops

    async def _shutdown_for_restart(self) -> None:
        """限时收尾，然后写回执、结束进程。收尾卡住（或连限时都卡住）也照样退出。"""
        if self._restart_exit is not None:
            timer = threading.Timer(RESTART_HARD_EXIT_S, self._exit_for_restart)
            timer.daemon = True
            timer.start()
        await self._drain(RESTART_DRAIN_S)
        try:
            await asyncio.wait_for(self.shutdown(), RESTART_SHUTDOWN_S)
        except TimeoutError:
            log.warning("重启收尾超过 %s 秒，直接退出", RESTART_SHUTDOWN_S)
        except Exception:
            log.exception("重启收尾出错，照样退出")
        self._exit_for_restart()

    async def _drain(self, timeout: float) -> None:
        """等已经在跑的平仓、对账做完，免得被取消打断（半平、止损补挂一半）。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while (self._closing or self._reconciling) and loop.time() < deadline:
            await asyncio.sleep(0.1)
        if self._closing or self._reconciling:
            log.warning(
                "重启：%s 秒内还有 %d 笔平仓%s没做完，照样退出",
                timeout,
                len(self._closing),
                "、对账" if self._reconciling else "",
            )

    def _exit_for_restart(self) -> None:
        """写 restart 回执，再结束进程。可能从超时线程调用，只执行一次。"""
        with self._exit_lock:
            if self._exited:
                return
            self._exited = True
            if self._restart_receipt is not None:
                inbox, path, result = self._restart_receipt
                try:
                    inbox.finish(path, result)
                except OSError:
                    log.exception("写重启回执失败")
            if self._restart_exit is not None:
                log.warning("面板要求重启：进程退出，launchd 会重新拉起")
                for handler in logging.getLogger().handlers:
                    with contextlib.suppress(Exception):
                        handler.flush()
                self._restart_exit()

    async def shutdown(self) -> None:
        tasks = [*self._loops, *self._tasks]
        for t in tasks:
            t.cancel()
        if tasks:
            # 只等一会儿：吞掉取消、不肯停的任务不能把退出卡死
            done, stuck = await asyncio.wait(tasks, timeout=TASK_CANCEL_WAIT_S)
            for t in done:
                if not t.cancelled():
                    t.exception()  # 取走异常，免得事件循环报“没人取的异常”
            if stuck:
                log.warning("%d 个后台任务取消后 %s 秒还没停", len(stuck), TASK_CANCEL_WAIT_S)
        self._loops, self._tasks = [], set()
        await self.app.stop()

    def _every(self, seconds: float, tick: Callable[[], Awaitable[None]]) -> asyncio.Task[None]:
        async def loop() -> None:
            while True:
                await asyncio.sleep(seconds)
                try:
                    await tick()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("定时任务 %s 出错", getattr(tick, "__name__", tick))

        return asyncio.create_task(loop())

    def _spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _heartbeat_tick(self) -> None:
        self.app.heartbeat_all()

    # ------------------------------------------------------------ 行情 → 信号
    async def _market_loop(self) -> None:
        async for event in self.m.feed.events():
            try:
                await self.on_event(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("处理行情事件出错")

    async def on_event(self, event: MarketEvent) -> None:
        self.m.features.update(event)
        if isinstance(event, Kline):
            self._sync_wanted()
            await self.m.predictor.on_kline(event)
        action = self.m.patrol.check(event, self.positions)
        if action is not None:
            targets = [
                p for p in self.positions if not action.symbols or p.symbol in action.symbols
            ]
            for pos in targets:
                # 平仓要等执行器结束（最长十几秒），放后台并行，行情和其它标的不能停
                if pos.position_id not in self._closing:
                    self._spawn(self.close(pos, CloseReason.PATROL))
        if isinstance(event, Trade):
            snap = self.m.features.snapshot(event.symbol)
            if snap is None:
                return
            now = self.app.clock()
            for signal in self.m.trigger.check(snap, self.m.cards.current(now), now):
                # 拍板要几百毫秒，放后台，不卡行情
                self._spawn(self.handle_signal(signal))

    def _sync_wanted(self) -> None:
        """预测员按需模式只算作战卡里的币和有持仓的币。"""
        card = self.m.cards.current(self.app.clock())
        wanted = {s.symbol for s in card.symbols} if card is not None else set()
        self.m.predictor.set_wanted(wanted | {p.symbol for p in self.positions})

    async def handle_signal(self, signal: CandidateSignal) -> None:
        now = self.app.clock()
        card = self.m.cards.current(now)
        if card is None:
            return
        if self._restarting:
            self._guard_note(Outcome.REJECT, f"{signal.symbol} 主程序正在重启，不处理信号", now)
            return
        if self.app.paused is not None:
            # 人工暂停：连 Jev 都不问（省钱），记在保镖名下
            self._guard_note(Outcome.REJECT, f"{signal.symbol} 人工暂停开新仓，不处理信号", now)
            return
        jev = await self.m.gate.evaluate(signal, card, now)  # 拍板可以并行
        async with self._open_lock:
            if self.app.paused is not None or self._restarting:
                # 问 Jev 期间下达的暂停 / 全平 / 重启：在途信号也要拦住
                self._guard_note(Outcome.REJECT, f"{signal.symbol} 拍板期间已暂停，不开仓", now)
                return
            # 问 Jev 期间可能换了新卡或封了币：按锁里这一刻的卡审，和问 Jev 时的卡不是同一版就不开
            now = self.app.clock()
            fresh = self.m.cards.current(now)
            if fresh is None or (fresh.card_id, fresh.version) != (card.card_id, card.version):
                self._guard_note(
                    Outcome.REJECT, f"{signal.symbol} 拍板期间作战卡已变，按新卡重新等信号", now
                )
                return
            account = await self.m.executor.account()
            # 拍板要几百毫秒，滑点和价差按现在的行情算，不按触发时那份
            market = self.m.features.snapshot(signal.symbol)
            verdict = self.m.risk.review(signal, jev, fresh, account, now, market=market)
            if not verdict.approved or verdict.intent is None:
                return
            intent = verdict.intent
            try:
                pos = await self.m.executor.open(intent)
            except Exception as e:
                log.exception("开仓失败")
                await self.app._alert(AlertLevel.WARN, f"{intent.symbol} 开仓失败", str(e)[:200])
                return
            # 执行器等成交后自己挂保命止损，挂不上就立即平掉（不留裸仓），这里不用再管
            self.positions = [*self.positions, pos]

    # ------------------------------------------------------------ 盯仓
    async def refresh_positions(self) -> None:
        try:
            all_pos = await self.m.executor.positions()
        except Exception:
            log.exception("读持仓失败，沿用上一次的列表")
            return
        self.positions = [
            p for p in all_pos if p.status in (PositionStatus.OPEN, PositionStatus.OPENING)
        ]

    async def close(self, pos: PositionRecord, reason: CloseReason) -> None:
        """同一笔正在平就不重复提交。重启已定就不再发起（会被退出打断），交给重启后的进程。"""
        if pos.position_id in self._closing:
            return
        if self._leaving:
            log.warning("%s 要平仓（%s），但主程序正在重启：交给重启后的进程", pos.symbol, reason)
            self._guard_note(
                Outcome.REJECT,
                f"{pos.symbol} 平仓（{reason.value}）留给重启后的进程，期间靠保命止损",
                self.app.clock(),
            )
            return
        self._closing.add(pos.position_id)
        try:
            await self.m.executor.close(pos.position_id, reason)
        except Exception as e:
            log.exception("平仓失败")
            await self.app._alert(AlertLevel.CRITICAL, f"{pos.symbol} 平仓失败", str(e)[:200])
            return
        finally:
            self._closing.discard(pos.position_id)
        self.positions = [p for p in self.positions if p.position_id != pos.position_id]
        if isinstance(self.m.keeper, _Forgets):
            self.m.keeper.forget(pos.position_id)
        if isinstance(self.m.patrol, _HasUrgent):
            self.m.patrol.urgent.discard(pos.position_id)

    async def _review(self, pos: PositionRecord) -> None:
        verdict = await self.m.keeper.review(pos, self.app.clock())
        if verdict.action == "close":
            await self.close(pos, CloseReason.KEEPER)

    async def keeper_tick(self) -> None:
        await self.refresh_positions()
        for pos in list(self.positions):
            if pos.status is PositionStatus.OPEN:
                await self._review(pos)

    async def urgent_tick(self) -> None:
        if not isinstance(self.m.patrol, _HasUrgent) or not self.m.patrol.urgent:
            return
        urgent = set(self.m.patrol.urgent)
        for pos in list(self.positions):
            if pos.position_id in urgent:
                self.m.patrol.urgent.discard(pos.position_id)
                await self._review(pos)
        # 已经不在持仓里的也清掉
        self.m.patrol.urgent -= urgent

    async def reconcile_tick(self) -> None:
        # 对账员自己发告警（带去重），连不上时抛异常，由 _every 记日志后下一轮再试
        if self._leaving:
            return  # 重启已定：不再开始新的一轮（补挂止损可能被退出打断），重启后先对一次账
        self._reconciling = True
        try:
            await self.m.reconciler.reconcile(self.app.clock())
        finally:
            self._reconciling = False

    # ------------------------------------------------------------ 慢环调度
    async def backfill(self, symbols: list[str]) -> None:
        """回填 1 分钟 K 线，ATR 不用干等几个小时。离线（行情是桩）时跳过。"""
        if is_stub(self.m.feed) or not symbols:
            return
        sem = asyncio.Semaphore(BACKFILL_CONCURRENCY)

        async def one(sym: str) -> None:
            async with sem:
                minutes: list[Kline] = []
                try:
                    minutes = await self._fetch_klines(sym)
                    for k in minutes:
                        self.m.features.update(k)
                except Exception:
                    log.warning("%s 回填 K 线失败，等实时数据攒够", sym, exc_info=True)
                if not self.m.predictor.enabled:
                    return
                try:
                    await self.m.predictor.backfill(sym, await self._fetch_predictor_klines(sym))
                    # 当前这根 5 分钟线已收盘的几分钟也喂进去，实时 1 分钟线才能接着拼出下一根，
                    # 否则从半截接入差一根被当成缺口、回填白做（更早的分钟拼出来是重复的，会丢掉）
                    for k in minutes:
                        await self.m.predictor.on_kline(k)
                except Exception:
                    log.warning("%s 回填预测员 K 线失败，等实时数据攒够", sym, exc_info=True)

        await asyncio.gather(*(one(s) for s in symbols))

    def _prices(self, pool: SymbolPool) -> dict[str, float]:
        out: dict[str, float] = {}
        for e in pool.in_pool:
            snap = self.m.features.snapshot(e.symbol)
            if snap is not None:
                out[e.symbol] = snap.last_price
        return out

    async def slow_tick(self) -> None:
        now = self.app.clock()
        if self._last_screen is None or now - self._last_screen >= SCREEN_EVERY:
            self._last_screen = now
            await self.app.refresh_lot_rules()
            await self._rescreen(now)
        pool = self.app.pool
        if pool is None or self._refreshing:
            return
        cards = self.m.cards
        reason = (
            cards.refresh_reason(now, pool, self._prices(pool))
            if isinstance(cards, _RefreshReason)
            else None
        )
        interval = timedelta(minutes=self.app.app_cfg.slow_loop_interval_min)
        if (
            reason is None
            and self._last_card_at is not None
            and now - self._last_card_at >= interval
        ):
            reason = "scheduled"
        if isinstance(cards, _RefreshGate):
            while (notice := cards.take_notice()) is not None:
                await self.app._alert(AlertLevel.WARN, "军师团暂停开会", notice)
            if not cards.may_refresh(now):
                return  # 失败退避中或当日花费超限；定时兜底也不能绕过
        if reason is None or is_stub(cards):
            return
        self._refreshing = True
        self._last_card_at = now
        self._spawn(self._refresh(pool, reason))

    async def news_tick(self) -> None:
        """拉新闻 / 公告：公告提到候选币 → 军师团提前开会；币安下架公告 → 该币只观察不开新仓。

        单个源失败 NewsHub 自己降级记日志；整轮失败也只记日志，下一轮再拉。
        """
        hub, cards = self.app.news, self.m.cards
        if hub is None or not isinstance(cards, _NewsAware):
            return
        # 候选池 + 手上的持仓，都算“我们交易的币”
        symbols = {e.symbol for e in self.app.pool.in_pool} if self.app.pool else set()
        symbols |= {p.symbol for p in self.positions}
        before = self.app.blocked_symbols()
        if symbols:
            await hub.poll_and_notify(sorted(symbols), cards.notify_news, cards.block_symbol)
        self.app.save_blocked()
        for sym, reason in self.app.blocked_symbols().items():
            if sym not in before:
                await self.app._alert(
                    AlertLevel.WARN,
                    f"{sym} 禁止新开仓",
                    f"{reason}（已有持仓约 40 秒后由持仓管家平掉）",
                )

    async def _refresh(self, pool: SymbolPool, reason: str) -> None:
        try:
            if isinstance(self.m.cards, _HoldsAware):
                self.m.cards.set_held({p.symbol: p.side for p in self.positions})
            await self.m.cards.refresh(pool, reason)
        except Exception:
            log.exception("军师团开会失败，沿用旧卡直到过期")
        finally:
            self._refreshing = False

    async def _rescreen(self, now: datetime) -> None:
        try:
            pool = await self.m.screener.screen(now)
        except Exception:
            log.exception("选币失败，沿用上一份候选池")
            return
        old = {e.symbol for e in self.app.pool.in_pool} if self.app.pool else set()
        new = [e.symbol for e in pool.in_pool]
        self.app.set_pool(pool)
        if set(new) != old:
            await self.backfill([s for s in new if s not in old])
            await self.m.feed.subscribe(new)

    # ------------------------------------------------------------ 面板指令
    def _guard_note(self, outcome: Outcome, summary: str, now: datetime) -> None:
        rec = self.app.recorders.get(Role.RISK_GUARD)
        if rec is not None:
            rec.record(now, outcome, summary)

    async def command_tick(self) -> None:
        """执行面板写来的指令（core/commands.py）。每条只执行一次，结果写回 done/。"""
        inbox = CommandInbox(self.app.app_cfg.data_dir)
        now = self.app.clock()
        if self._last_prune is None or now - self._last_prune >= PRUNE_EVERY:
            self._last_prune = now
            inbox.prune(now)
        for path, cmd, error in inbox.pending():
            now = self.app.clock()
            if cmd is None:
                result = CommandResult(
                    id=path.stem[:64], kind="?", status="rejected", detail=error, ts=now
                )
            elif inbox.is_done(cmd.id):
                result = CommandResult(id=cmd.id, kind=cmd.kind, status="rejected", ts=now)
            elif (why := inbox.check(cmd, now)) is not None:
                result = CommandResult(
                    id=cmd.id, kind=cmd.kind, status="expired", detail=why, ts=now
                )
            elif (
                cmd.kind == "restart" and self._started_at is not None and cmd.ts < self._started_at
            ):
                # 上一个进程退出前没来得及写回执：本进程就是重启后的，别再重启一次
                result = CommandResult(
                    id=cmd.id, kind=cmd.kind, status="done", detail="已重启", ts=now
                )
            else:
                status: Literal["done", "rejected", "failed"]
                try:
                    status, detail = await self._run_command(cmd)
                except Exception as e:
                    log.exception("面板指令执行失败")
                    status, detail = "failed", repr(e)[:200]
                result = CommandResult(
                    id=cmd.id, kind=cmd.kind, status=status, detail=detail, ts=now
                )
                outcome = Outcome.ACTION if status == "done" else Outcome.REJECT
                self._guard_note(outcome, f"面板指令 {cmd.kind}（{status}）：{detail}"[:200], now)
                if cmd.kind == "restart" and status == "done":
                    self._restart_receipt = (inbox, path, result)
                    continue
            try:
                inbox.finish(path, result)
            except OSError:
                log.exception("写面板指令结果失败")
        if self._restarting:  # 其余回执写完再退出；restart 自己的回执在退出前一刻写
            self._restart.set()

    async def _prepare_restart(self) -> tuple[Literal["done", "rejected"], str]:
        """不再接新信号 → 等在途开仓（开仓锁）和平仓做完 → 回执写完后退出（见 command_tick）。

        不写 paused.json：重启后按重启前的暂停状态继续。平仓一直没做完就放弃重启，免得打断平仓。
        """
        why = "这个进程不支持面板重启" if self._restart_check is None else self._restart_check()
        if why is not None:
            return "rejected", why
        self._restarting = True  # 只拦新开仓；管家、巡逻兵、对账、平仓照常跑
        async with self._open_lock:
            pass
        deadline = asyncio.get_running_loop().time() + RESTART_WAIT_CLOSE_S
        while self._closing and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.5)
        if self._closing:
            self._restarting = False
            return "rejected", f"还有 {len(self._closing)} 笔平仓没做完，没有重启，稍后再试"
        self._leaving = True
        await self.app._alert(AlertLevel.INFO, "主程序重启", "面板指令：重新加载配置")
        return "done", "在途动作已做完，主程序已退出，launchd 会自动拉起"

    async def _run_command(self, cmd: PanelCommand) -> tuple[Literal["done", "rejected"], str]:
        if cmd.kind == "pause":
            self.app.set_paused(True, reason=cmd.params.get("reason", ""), command_id=cmd.id)
            await self.app._alert(AlertLevel.WARN, "已暂停开新仓", "面板指令；已有持仓照常看管")
            return "done", "已暂停开新仓"
        if cmd.kind == "resume":
            self.app.set_paused(False, command_id=cmd.id)
            await self.app._alert(AlertLevel.INFO, "已恢复开新仓", "面板指令")
            return "done", "已恢复开新仓"
        if cmd.kind == "restart":
            return await self._prepare_restart()
        if cmd.kind == "close_all":
            # 先暂停再平：否则平的同时可能又开新仓。拿开仓锁等在途开仓做完，再读持仓
            self.app.set_paused(True, reason="面板全部平仓", command_id=cmd.id)
            async with self._open_lock:
                await self.refresh_positions()
                targets = list(self.positions)
            for p in targets:
                self._spawn(self.close(p, CloseReason.MANUAL))
            await self.app._alert(
                AlertLevel.WARN, "面板全部平仓", "已同时暂停开新仓，恢复需另发指令"
            )
            return "done", f"已暂停开新仓并提交平仓 {len(targets)} 笔"
        if cmd.kind == "close_position":
            pid = cmd.params.get("position_id", "")
            await self.refresh_positions()
            pos = next((p for p in self.positions if p.position_id == pid), None)
            if pos is None:
                return "rejected", f"没有持仓 {pid}"
            self._spawn(self.close(pos, CloseReason.MANUAL))
            return "done", f"已提交平仓 {pos.symbol} {pid}"
        if cmd.kind == "unblock_symbol":
            sym = cmd.params.get("symbol", "")
            if not self.app.unblock_symbol(sym):
                return "rejected", f"{sym} 没有被封"
            return "done", f"已解封 {sym}（只在本次运行有效）"
        # refresh_cards
        pool = self.app.pool
        if pool is None or is_stub(self.m.cards):
            return "rejected", "军师团未接入或还没有候选池"
        if self._refreshing:
            return "rejected", "军师团正在开会"
        self._refreshing = True
        self._last_card_at = self.app.clock()
        self._spawn(self._refresh(pool, "manual"))
        return "done", "军师团已开始开会"
