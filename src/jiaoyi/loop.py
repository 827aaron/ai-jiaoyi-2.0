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
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol, cast, runtime_checkable

from jiaoyi.app import App, is_stub
from jiaoyi.contracts import (
    AccountState,
    AgainstSignalEvent,
    AlertLevel,
    BattleCard,
    CandidateSignal,
    Interval,
    Kline,
    MarketEvent,
    OrderIntent,
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
from jiaoyi.core.pool_overrides import CATEGORY_ACTIONS, POOL_ACTIONS, PoolAction
from jiaoyi.execution.income_daily import INCOME_REFRESH_S
from jiaoyi.market.binance_ws import FINAL_STATUSES, ContractInfo
from jiaoyi.market.features import (
    ATR_PERIOD,
    BTC,
    HISTORY_DAYS,
    VOL_24H_MINUTES,
    backfill_minutes,
)
from jiaoyi.market.rest import fetch_recent_klines
from jiaoyi.market.snapshot_file import WRITE_EVERY_S as SNAPSHOT_WRITE_EVERY_S
from jiaoyi.market.snapshot_file import MarketSnapshotFile
from jiaoyi.risk.guard import DELIST_WINDOW, entry_zone_miss, entry_zone_miss_at

log = logging.getLogger(__name__)

SLOW_CHECK_EVERY_S = 60.0
URGENT_CHECK_EVERY_S = 1.0
HEARTBEAT_EVERY_S = 30.0
SNAPSHOT_EVERY_S = SNAPSHOT_WRITE_EVERY_S
NEWS_POLL_EVERY_S = 120.0
CREDIT_CHECK_EVERY_S = 120.0
"""模型额度看护多久看一眼（撞 402 要及时发；余额每小时才真去查一次）。"""
COMMAND_POLL_EVERY_S = 1.0
CONFIG_WATCH_EVERY_S = 3.0  # 看设置文件变没变（热生效，core/hotconfig.py）
KEEPER_CONCURRENCY = 5
"""持仓管家一轮里最多几笔一起问 Jev。"""
RESTART_WAIT_CLOSE_S = 60.0
# 决定重启后：收尾最多等这么久；军师团开会跑在工作线程里取消不掉，收尾超时就直接退出
RESTART_SHUTDOWN_S = 20.0
# 取消任务前先等已经在跑的平仓 / 对账做完（不再发起新的），最多这么久
RESTART_DRAIN_S = 15.0
RESTART_HARD_EXIT_S = 45.0
TASK_CANCEL_WAIT_S = 10.0
PRUNE_EVERY = timedelta(hours=1)
SCREEN_FRESH = timedelta(minutes=2)
"""整轮会议前选币：2 分钟内刚选过就不重复。"""
_POOL_DONE = {
    "pin_symbol": "已固定",
    "unpin_symbol": "已取消固定",
    "blacklist_symbol": "已拉黑（立即不开新仓）",
    "unblacklist_symbol": "已取消拉黑",
    "add_symbol": "已手动加入",
    "remove_symbol": "已取消手动加入",
}
BACKFILL_CONCURRENCY = 5


@runtime_checkable
class _Forgets(Protocol):
    def forget(self, position_id: str) -> None: ...


@runtime_checkable
class _HasUrgent(Protocol):
    urgent: set[str]


@runtime_checkable
class _AgainstAware(Protocol):
    """哨兵：和卡方向相反的信号（RuleTrigger）。"""

    def pop_against(self) -> list[AgainstSignalEvent]: ...

    def note_against_result(self, sig: AgainstSignalEvent, result: str) -> None: ...


@runtime_checkable
class _NotesAgainst(Protocol):
    """管家：下一次复查带上反向信号（JevKeeper）。"""

    def note_against(self, position_id: str, sig: AgainstSignalEvent, one_shot: bool) -> None: ...

    def pending_against(self, position_id: str) -> bool: ...


@runtime_checkable
class _LedgerPositions(Protocol):
    """执行器：账本里此刻还没结束的持仓，不查 hummingbot（HbExecutorAdapter）。"""

    def live_positions(self) -> list[PositionRecord]: ...


@runtime_checkable
class _NewsAware(Protocol):
    def notify_news(self, symbol: str) -> None: ...

    def block_symbol(self, symbol: str, reason: str) -> None: ...


@runtime_checkable
class _ChaseRecheck(Protocol):
    """保镖：挂单没成交改市价追进前按此刻状态再审（``RiskGuardImpl.recheck_chase``）。"""

    def recheck_chase(
        self, intent: OrderIntent, card: BattleCard | None, account: AccountState, now: datetime
    ) -> list[str]: ...


@runtime_checkable
class _ChaseHook(Protocol):
    def set_chase_request(self, request: Callable[[OrderIntent], None]) -> None: ...

    async def open_chase(self, intent: OrderIntent) -> PositionRecord: ...


@runtime_checkable
class _RefreshGate(Protocol):
    """军师团开会的总闸：失败退避 / 日花费上限，以及要推送的提示。"""

    def may_refresh(self, now: datetime) -> bool: ...

    def take_notice(self) -> str | None: ...


@runtime_checkable
class _RefreshReason(Protocol):
    def refresh_reason(
        self,
        now: datetime,
        pool: SymbolPool,
        prices: Mapping[str, float],
        atrs: Mapping[str, float] | None = None,
    ) -> str | None: ...


@runtime_checkable
class _EventMeetings(Protocol):
    """临时会：卡有效期里出了事件，只给一个币补开一场（BattleCardCommander）。"""

    def pop_event(self, now: datetime, pool: SymbolPool) -> tuple[str, str] | None: ...

    async def refresh_symbol(
        self, pool: SymbolPool, symbol: str, trigger: str
    ) -> BattleCard | None: ...

    def request_event(self, symbol: str, trigger: str) -> None: ...

    def schedule_interval(self, base: timedelta) -> timedelta: ...

    @property
    def fixed_time(self) -> bool: ...


@runtime_checkable
class _SeedsAtr(Protocol):
    def seed_atr_history(self, symbol: str, bars: list[Kline]) -> int: ...


@runtime_checkable
class _PicksAware(Protocol):
    """总盘会一挑完币就回调名单（BattleCardCommander）。"""

    def set_on_picks(self, callback: Callable[[list[str]], None]) -> None: ...


@runtime_checkable
class _WatchNote(Protocol):
    watch_note: str


@runtime_checkable
class _HoldsAware(Protocol):
    """开会前告诉军师团手上有哪些持仓币：它们要一直留在卡上，不然持仓管家会判“不再盯”。"""

    def set_held(self, held: Mapping[str, Side]) -> None: ...


KlineFetcher = Callable[[str], Awaitable[list[Kline]]]
ATR_INTERVAL = {
    5: Interval.M5,
    15: Interval.M15,
    30: Interval.M30,
    60: Interval.H1,
    240: Interval.H4,
}
MAX_REST_KLINES = 1500


class Runner:
    def __init__(
        self,
        app: App,
        *,
        fetch_klines: KlineFetcher | None = None,
        fetch_predictor_klines: KlineFetcher | None = None,
        fetch_atr_history: KlineFetcher | None = None,
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
        fl = app.app_cfg.fast_loop
        # 至少 24 小时：新信号要 24 小时 1 分钟波动、4 小时涨跌（一次请求最多 1500 根）
        limit = max(backfill_minutes(fl.atr_period_min, fl.atr_long_period_min), VOL_24H_MINUTES)
        self._fetch_klines = fetch_klines or (lambda sym: fetch_recent_klines(sym, limit=limit))
        # 预测员周期的 K 线（5m / 15m / 1h，至少 64 根），启动时一次拉够，不用等几个小时
        bars = app.app_cfg.predictor.backfill_bars
        pred_interval = Interval(app.app_cfg.predictor.interval)
        self._fetch_predictor_klines = fetch_predictor_klines or (
            lambda sym: fetch_recent_klines(sym, limit=bars, interval=pred_interval)
        )
        # 止损 ATR 下限要 7 天 ATR 历史：按 ATR 周期拉 7 天 K 线
        # （一次最多 1500 根，5 分钟线约 5 天）
        period = app.app_cfg.fast_loop.atr_period_min
        hist = min(HISTORY_DAYS * 1440 // period + ATR_PERIOD + 1, MAX_REST_KLINES)
        self._fetch_atr_history = fetch_atr_history or (
            lambda sym: fetch_recent_klines(sym, limit=hist, interval=ATR_INTERVAL[period])
        )
        self.positions: list[PositionRecord] = []
        self._tasks: set[asyncio.Task[None]] = set()
        self._loops: list[asyncio.Task[None]] = []
        self._last_screen: datetime | None = None
        self._last_card_at: datetime | None = None
        self._refreshing = False
        self._predictor_refilling: set[str] = set()  # 正在用 REST 补预测员历史的币
        # 读账户 → 保镖 → 开仓 必须串行，否则并发信号都按“开仓前”的账户算，绕过笔数和敞口上限
        self._open_lock = asyncio.Lock()
        if isinstance(self.m.executor, _ChaseHook):
            self.m.executor.set_chase_request(self._request_chase)
        self._closing: set[str] = set()
        self._against_pending: set[str] = set()  # 因反向信号叫了急查、管家还没给结论的持仓
        self._reviewing: set[str] = set()  # 管家正在问 Jev 的持仓：同一笔不同时问两次
        self._tp_past: dict[str, datetime] = {}
        """限价止盈：价格第一次穿过止盈价的时间（按仓位），待满 tp_limit_fallback_s 改市价平。"""
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
        self._inflight: set[str] = set()  # 还在跑、回执要等跑完才写的面板指令（重新开会）
        self._cmd_path: Path | None = None
        self._exit_lock = threading.Lock()
        self._exited = False
        self._started_at: datetime | None = None
        # 盘口员 / 预测员只盯：总盘会本轮挑中的 + 有效作战卡上的 + 有持仓的币（不盯整个候选池）
        self._meeting_picks: set[str] = set()
        self._watch_lock = asyncio.Lock()
        self._watched: set[str] | None = None  # 上一次订阅的集合；None = 还没订过
        if hasattr(self.m.feed, "contract_listener"):
            self.m.feed.contract_listener = self.on_contract_info  # 币安行情层：下架推送
        self._market_file = MarketSnapshotFile(app.app_cfg.data_dir, app.risk_cfg.day_reset_tz)
        if isinstance(self.m.cards, _PicksAware):
            self.m.cards.set_on_picks(self.set_meeting_picks)

    # ------------------------------------------------------------ 生命周期
    async def run(self) -> None:
        """启动并一直跑，直到被取消，或面板发来 restart（之后 ``restart_requested`` 为 True）。"""
        self._started_at = self.app.clock()
        await self.app.start()
        self._last_screen = self.app.clock()
        await self.refresh_positions()
        await self.sync_watch()
        # 重启后先拉一轮公告，下架封禁在第一个信号之前就生效
        try:
            await self.news_tick()
        except Exception:
            log.exception("启动拉新闻失败，主循环里再拉")
        try:
            await self.app.refresh_income()
        except Exception:
            log.exception("启动拉收入流水失败，主循环里再拉")
        cfg = self.app.app_cfg
        self._loops = [
            asyncio.create_task(self._market_loop()),
            self._every(cfg.keeper_interval_s, self.keeper_tick),
            self._every(URGENT_CHECK_EVERY_S, self.urgent_tick),
            self._every(cfg.reconcile_interval_s, self.reconcile_tick),
            self._every(SLOW_CHECK_EVERY_S, self.slow_tick),
            self._every(HEARTBEAT_EVERY_S, self._heartbeat_tick),
            self._every(SNAPSHOT_EVERY_S, self.market_snapshot_tick),
            self._every(NEWS_POLL_EVERY_S, self.news_tick),
            self._every(CREDIT_CHECK_EVERY_S, self.app.credit_tick),
            self._every(COMMAND_POLL_EVERY_S, self.command_tick),
            self._every(CONFIG_WATCH_EVERY_S, self.config_tick),
            # 有交易所连接时账本回填和收入流水由 LedgerSync 管（成交回报叫醒，60 秒兜底）
            (
                asyncio.create_task(self.app.ledger.run())
                if self.app.ledger is not None
                else self._every(INCOME_REFRESH_S, self.app.refresh_income)
            ),
        ]
        if self.app.derivs is not None:
            # 持仓量每 60 秒、多空比每 5 分钟（盯着的币；查失败自己跳过）
            self._loops.append(asyncio.create_task(self.app.derivs.run(self._derivs_symbols)))
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

    async def market_snapshot_tick(self) -> None:
        """面板「盘口员」读的 data/market_snapshot.json：正在盯的币此刻的行情和各路推送计数。"""
        symbols = sorted(self._watched or set())
        self._market_file.write(self.m.features, symbols, self.app.clock())

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
        self._market_file.count(event, self.app.clock())
        if isinstance(event, Kline):
            self._sync_wanted()
            await self.m.predictor.on_kline(event)
            for sym in self.m.predictor.take_gaps() - self._predictor_refilling:
                self._predictor_refilling.add(sym)
                self._spawn(self._refill_predictor(sym))
        action = self.m.patrol.check(event, self.positions)
        if action is not None:
            targets = [
                p for p in self.positions if not action.symbols or p.symbol in action.symbols
            ]
            for pos in targets:
                # 平仓要等执行器结束（最长十几秒），放后台并行，行情和其它标的不能停
                if pos.position_id not in self._closing:
                    self._spawn(self._patrol_close(pos, action.reason))
        if isinstance(event, Trade) and self.app.app_cfg.limit_take_profit_on:
            self._tp_fallback(event)
        if isinstance(event, Trade):
            snap = self.m.features.snapshot(event.symbol)
            if snap is None:
                return
            now = self.app.clock()
            for signal in self.m.trigger.check(snap, self.m.cards.current(now), now):
                # 拍板要几百毫秒，放后台，不卡行情
                self._spawn(self.handle_signal(signal))
            if isinstance(self.m.trigger, _AgainstAware):
                for sig in self.m.trigger.pop_against():
                    self._route_against(self.m.trigger, sig, now)

    def _ledger_positions(self) -> list[PositionRecord]:
        """账本里此刻的持仓（含正在开 / 正在平），读不到才用上一轮刷新的列表。"""
        ex = self.m.executor
        if isinstance(ex, _LedgerPositions):
            try:
                return ex.live_positions()
            except Exception:
                log.exception("读账本持仓失败，用上一轮的持仓列表")
        return list(self.positions)

    def _route_against(
        self, trigger: _AgainstAware, sig: AgainstSignalEvent, now: datetime
    ) -> None:
        """和卡相反的信号：按账本持仓分流（``jiaoyi.core.against``），要急查的放进急查通道。"""
        ev, rechecks = self.app.against_desk.route(
            sig, self._ledger_positions(), self._closing, now
        )
        trigger.note_against_result(sig, ev.result)
        for pos, one_shot in rechecks:
            if isinstance(self.m.keeper, _NotesAgainst):
                self.m.keeper.note_against(pos.position_id, sig, one_shot)
            self._against_pending.add(pos.position_id)
            if isinstance(self.m.patrol, _HasUrgent):
                self.m.patrol.urgent.add(pos.position_id)  # 和巡逻兵报警线同一条急查通道
            else:
                self._spawn(self._review(pos))

    def _tp_fallback(self, t: Trade) -> None:
        """限价止盈挂着却没成交：价格穿过止盈价 ``tp_limit_fallback_through_bps`` 以上、
        连续待满 ``tp_limit_fallback_s`` 秒，改市价平。

        只碰到止盈价不算：成交价等于限价时，限价单可能还排在别人后面没轮到（DASH 2026-10-24
        就是碰到 50.66 后 10 秒被改成市价、吃了 0.04% 手续费）。价格穿过去了还没成交才是真没挂上。
        """
        now = self.app.clock()
        fl = self.app.app_cfg.fast_loop
        wait, through = fl.tp_limit_fallback_s, fl.tp_limit_fallback_through_bps / 1e4
        for pos in self.positions:
            tp, pid = pos.take_profit_price, pos.position_id
            if pos.symbol != t.symbol or pos.status is not PositionStatus.OPEN or tp is None:
                continue
            past = (
                t.price >= tp * (1.0 + through)
                if pos.side is Side.LONG
                else t.price <= tp * (1.0 - through)
            )
            if not past:
                self._tp_past.pop(pid, None)
                continue
            first = self._tp_past.setdefault(pid, now)
            if (now - first).total_seconds() >= wait and pid not in self._closing:
                self._tp_past.pop(pid, None)
                self._spawn(self._tp_market(pos, t.price, wait))

    async def _tp_market(self, pos: PositionRecord, price: float, wait: float) -> None:
        log.warning(
            "%s 穿过止盈价 %s 已 %.0f 秒没成交，改市价平", pos.symbol, pos.take_profit_price, wait
        )
        await self.close(pos, CloseReason.TAKE_PROFIT)
        if any(p.position_id == pos.position_id for p in self.positions):
            return  # 没平掉（close 已发 CRITICAL）
        await self.app._alert(
            AlertLevel.INFO,
            "限价止盈没成交，已改市价平",
            f"{pos.symbol} 止盈价 {pos.take_profit_price:g}，现价 {price:g}，越过 {wait:.0f} 秒",
            "close_done",
        )

    def _sync_wanted(self) -> None:
        """预测员按需模式只算作战卡里的币和有持仓的币（总盘会挑中的走 ``set_priority``）。"""
        card = self.m.cards.current(self.app.clock())
        wanted = {s.symbol for s in card.symbols} if card is not None else set()
        self.m.predictor.set_wanted(wanted | {p.symbol for p in self.positions})

    # ------------------------------------------------------------ 盯哪些币
    def set_meeting_picks(self, symbols: list[str]) -> None:
        """总盘会挑完币（作战卡还要十几分钟）：预测员先算这几个，行情马上订上。"""
        self._meeting_picks = set(symbols)
        self.m.predictor.set_priority(self._meeting_picks)
        self._spawn(self.sync_watch())

    def watch_set(self) -> tuple[set[str], set[str], set[str]]:
        """（要盯的全部，其中总盘会 / 作战卡的，其中持仓的）。卡过期、平仓后自然掉出去。"""
        card = self.m.cards.current(self.app.clock())
        meeting = set(self._meeting_picks)
        if card is not None:
            meeting |= {s.symbol for s in card.symbols}
        held = {p.symbol for p in self.positions}
        # BTC 一直订：「BTC 先动、小币跟」和拍板人快照的 btc5z 要它（不在卡上也订）
        return meeting | held | {BTC}, meeting - held, held

    def _derivs_symbols(self) -> set[str]:
        """查持仓量 / 多空比的币：总盘会 / 作战卡上的（持仓的不另查，新信号只在卡上的币出）。"""
        return self.watch_set()[1]

    async def sync_watch(self) -> None:
        """订阅集变了就重订：新加的币先回填 K 线（ATR、预测员不用干等），退掉不再盯的。"""
        async with self._watch_lock:
            watch, meeting, held = self.watch_set()
            feed = self.m.feed
            if isinstance(feed, _WatchNote):
                feed.watch_note = (
                    f"盯着 {len(watch)} 个币（总盘会 {len(meeting)} 持仓 {len(held)}）"
                )
            self._sync_wanted()
            if watch == self._watched:
                return
            await self.backfill(sorted(watch - (self._watched or set())))
            # 空集也订一次：全市场强平那条连接要开着（选币用）
            await feed.subscribe(sorted(watch))
            self._watched = watch

    def on_contract_info(self, info: ContractInfo) -> None:
        """币安合约信息推送：盯着的币 / 候选池的币 / 持仓的币。

        - 真下架（排了 7 天内的下架时间，或状态是交割 / 已关）：和新闻源（币安下架公告）走同一个
          ``block_symbol``，卡里读成只看不做，已有持仓由持仓管家平掉，要手动解封。推送是即时的，
          公告要轮询，所以这是更快的第二来源。
        - 临时不在交易（结算中、待上线等）：只记进 ``app.halted``，保镖挡新开仓，已有持仓不动；
          状态回到 TRADING 自动放开。"""
        now = self.app.clock()
        sym = info.symbol
        soon = info.delist_at is not None and info.delist_at - now <= DELIST_WINDOW
        if not soon and info.status not in FINAL_STATUSES:
            self._contract_halt(info)
            return
        ours = set(self._watched or set()) | {p.symbol for p in self.positions}
        if self.app.pool is not None:
            ours |= {e.symbol for e in self.app.pool.in_pool}
        cards = self.m.cards
        if sym not in ours or not isinstance(cards, _NewsAware):
            return
        self.app.halted.pop(sym, None)  # 封了就不用再记临时停
        if sym in self.app.blocked_symbols():
            return
        if soon and info.delist_at is not None:
            reason = f"币安合约信息：{sym} 将于 {info.delist_at:%m-%d %H:%M} UTC 下架"
        else:
            reason = f"币安合约信息：{sym} 状态变成 {info.status}（交割 / 下线），不再交易"
        cards.block_symbol(info.symbol, reason)
        self.app.save_blocked()
        self._spawn(
            self.app._alert(
                AlertLevel.WARN,
                f"{info.symbol} 禁止新开仓",
                f"{reason}（已有持仓约 40 秒后由持仓管家平掉）",
                "news_block",
            )
        )

    def _request_chase(self, intent: OrderIntent) -> None:
        """执行层：挂单没成交要追。另起任务，别在执行层巡检里拿开仓锁（全平时持锁会调巡检）。"""
        self._spawn(self._chase(intent))

    async def _chase(self, intent: OrderIntent) -> None:
        """和 handle_signal 一样在开仓锁里读账户 → 复核 → 下单，笔数 / 敞口不会多算。"""
        executor = self.m.executor
        if not isinstance(executor, _ChaseHook):
            return
        async with self._open_lock:
            now = self.app.clock()
            try:
                why = await self._may_chase(intent)
            except Exception as e:
                why = [f"追单复核出错：{type(e).__name__}"]
            if why:
                self._guard_note(
                    Outcome.REJECT, f"{intent.symbol} 挂单没成交，不追了：{'；'.join(why)}", now
                )
                return
            try:
                pos = await executor.open_chase(intent)
            except Exception as e:
                log.warning("%s 挂单没成交后市价追进失败：%r", intent.symbol, e)
                return
            self.positions = [*self.positions, pos]

    async def _may_chase(self, intent: OrderIntent) -> list[str]:
        """挂单没成交、要改市价追进：按此刻的暂停 / 重启 / 拉黑 / 作战卡 / 账户再过一遍保镖。"""
        now = self.app.clock()
        why: list[str] = []
        if self.app.paused is not None:
            why.append("人工暂停开新仓")
        if self._restarting:
            why.append("主程序正在重启")
        if intent.symbol in self.app.pool_overrides.blacklist:
            why.append("你拉黑了这个币")
        if not isinstance(self.m.risk, _ChaseRecheck):
            return [*why, "保镖不支持追单复核"]
        account = await self.m.executor.account()
        card = self.m.cards.current(now)
        return (
            why
            + self._chase_zone(intent, card, now)
            + self.m.risk.recheck_chase(intent, card, account, now)
        )

    def _chase_zone(self, intent: OrderIntent, card: BattleCard | None, now: datetime) -> list[str]:
        """改市价追进 = 按此刻价格成交，所以按此刻价格再查一次卡的进场区间（只在区间内开仓）。"""
        fast = self.app.app_cfg.fast_loop
        cs = card.for_symbol(intent.symbol) if card is not None else None
        if card is None or cs is None or cs.entry_zone is None or not fast.entry_zone_only:
            return []
        snap = self.m.features.snapshot(intent.symbol)
        if snap is None:
            return ["没到区间：拿不到现价，没法确认还在卡的进场区间里"]
        miss = entry_zone_miss_at(
            intent.symbol, intent.side, snap.last_price, snap, card, fast, now
        )
        return [] if miss is None else [miss]

    async def handle_signal(self, signal: CandidateSignal) -> None:
        now = self.app.clock()
        card = self.m.cards.current(now)
        if card is None:
            return
        if self._restarting:
            self._guard_note(Outcome.REJECT, f"{signal.symbol} 主程序正在重启，不处理信号", now)
            return
        if signal.symbol in self.app.pool_overrides.blacklist:
            # 刚拉黑的币可能还在旧作战卡里：不等军师团下一轮，立即不开
            self._guard_note(Outcome.REJECT, f"{signal.symbol} 你拉黑了，不开新仓", now)
            return
        if self.app.paused is not None:
            # 人工暂停：连 Jev 都不问（省钱），记在保镖名下
            self._guard_note(Outcome.REJECT, f"{signal.symbol} 人工暂停开新仓，不处理信号", now)
            return
        miss = entry_zone_miss(signal, card, self.app.app_cfg.fast_loop, now)
        if miss is not None:
            # 卡的进场区间外：连 Jev 都不问（省钱），记在保镖名下
            rec = self.app.recorders.get(Role.RISK_GUARD)
            if rec is not None:
                side = "多" if signal.side_hint == Side.LONG else "空"
                rec.record(
                    now,
                    Outcome.REJECT,
                    f"{signal.symbol} {side}：{miss}",
                    symbol=signal.symbol,
                    signal_id=signal.signal_id,
                    card_id=card.card_id,
                    output={"approved": False, "reasons": [miss], "detail": {"by": "entry_zone"}},
                )
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
                await self.app._alert(
                    AlertLevel.WARN, f"{intent.symbol} 开仓失败", str(e)[:200], "order_failed"
                )
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
        before = {p.position_id for p in self.positions}
        self.positions = [
            p for p in all_pos if p.status in (PositionStatus.OPEN, PositionStatus.OPENING)
        ]
        if before - {p.position_id for p in self.positions}:
            self._reconcile_soon()  # 有仓刚结束（止损 / 止盈 / 到时平）：马上核对币安是不是 0

    def _contract_halt(self, info: ContractInfo) -> None:
        """临时不在交易 → 记进 ``app.halted``（只挡新开仓）；回到 TRADING → 放开。都只记日志。"""
        sym, halted = info.symbol, self.app.halted
        if info.status == "TRADING":
            if halted.pop(sym, None) is not None:
                log.info("%s 恢复交易（币安合约状态 TRADING），重新允许开新仓", sym)
            return
        reason = f"币安合约信息：{sym} 暂时不在交易（状态 {info.status}）"
        if halted.get(sym) != reason:
            halted[sym] = reason
            log.info("%s，先不开新仓；已有持仓不动，恢复交易后自动放开", reason)

    def _reconcile_soon(self) -> None:
        """平仓后立刻对一次账：万一平仓单在保命止损之后又成交开出反手仓，不等下一个 30 秒。"""
        if not self._reconciling and not self._leaving:
            self._spawn(self.reconcile_tick())
        if self.app.ledger is not None:
            self.app.ledger.poke()  # 出场均价、手续费、今日盈亏马上按币安成交回填

    async def _patrol_close(self, pos: PositionRecord, why: str) -> None:
        """巡逻兵冲击线全平，平完推一条 Telegram。"""
        await self.close(pos, CloseReason.PATROL)
        await self.app._alert(AlertLevel.WARN, "巡逻兵已全平", f"巡逻兵：{why}", "system")

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
            await self.app._alert(
                AlertLevel.CRITICAL, f"{pos.symbol} 平仓失败", str(e)[:200], "order_failed"
            )
            return
        finally:
            self._closing.discard(pos.position_id)
        self.positions = [p for p in self.positions if p.position_id != pos.position_id]
        self._reconcile_soon()
        if isinstance(self.m.keeper, _Forgets):
            self.m.keeper.forget(pos.position_id)
        if isinstance(self.m.patrol, _HasUrgent):
            self.m.patrol.urgent.discard(pos.position_id)
        self.app.against_desk.forget(pos.position_id)

    async def _review(self, pos: PositionRecord, *, urgent: bool = False) -> None:
        """同一笔仓同一时间只问一次 Jev：例行复查还在问时来了急查，等它问完下一秒再查
        （急查带的反向信号留在管家那里，例行那次没带上的话由这次补上）。

        反向急查没查成（Jev 繁忙重试后仍失败 / 超时 / 没行情）：管家把信号留着，不算查过，
        下一轮例行复查（``keeper_interval_s`` 内）带上再查；不每秒重试，免得 Jev 正忙时越催越忙。"""
        pid = pos.position_id
        if pid in self._reviewing:
            if urgent and isinstance(self.m.patrol, _HasUrgent):
                self.m.patrol.urgent.add(pid)
            return
        self._reviewing.add(pid)
        try:
            verdict = await self.m.keeper.review(pos, self.app.clock())
        finally:
            self._reviewing.discard(pid)
        if pid in self._against_pending:  # 这次复查带了反向信号：结论写回团队页
            keeper = self.m.keeper
            if isinstance(keeper, _NotesAgainst) and keeper.pending_against(pid):
                pass  # 没查成（Jev 繁忙 / 超时 / 没行情）：不算查过，信号留给下一轮例行复查
            else:
                self._against_pending.discard(pid)
                self.app.against_desk.settle(pid, verdict.action)
        if verdict.action == "close":
            await self.close(pos, CloseReason.KEEPER)

    async def _review_all(self, positions: list[PositionRecord]) -> None:
        """并发审（最多 ``KEEPER_CONCURRENCY`` 笔一起）：Jev 繁忙重试时每笔多等几秒，
        持仓多了按顺序问会拖过下一轮。每笔的「连续几次该平」各算各的。"""
        sem = asyncio.Semaphore(KEEPER_CONCURRENCY)

        async def one(pos: PositionRecord) -> None:
            async with sem:
                await self._review(pos)

        results = await asyncio.gather(*(one(p) for p in positions), return_exceptions=True)
        for pos, r in zip(positions, results, strict=True):
            if isinstance(r, BaseException):
                log.error("%s 持仓管家审查出错", pos.symbol, exc_info=r)

    async def keeper_tick(self) -> None:
        keeper = self.app.recorders.get(Role.KEEPER)
        if keeper is not None:
            now = self.app.clock()
            keeper.set_next_run(now + timedelta(seconds=self.app.app_cfg.keeper_interval_s))
        await self.refresh_positions()
        held = [p for p in self.positions if p.status is PositionStatus.OPEN]
        await self._review_all(held)
        if keeper is not None and not held:
            keeper.note(self.app.clock(), "看了一眼：没有持仓，不用审")
        # 开平仓、卡过期都在这里跟上订阅；放在审查之后，回填新币的几秒不推迟审持仓
        await self.sync_watch()

    async def urgent_tick(self) -> None:
        if not isinstance(self.m.patrol, _HasUrgent) or not self.m.patrol.urgent:
            return
        urgent = set(self.m.patrol.urgent)
        for pos in list(self.positions):
            if pos.position_id in urgent:
                self.m.patrol.urgent.discard(pos.position_id)
                await self._review(pos, urgent=True)
        # 已经不在持仓里的也清掉（正在审、刚重新排上的留着）
        self.m.patrol.urgent -= urgent - {p.position_id for p in self.positions}

    async def reconcile_tick(self) -> None:
        # 对账员自己发告警（带去重），连不上时抛异常，由 _every 记日志后下一轮再试
        if self._leaving:
            return  # 重启已定：不再开始新的一轮（补挂止损可能被退出打断），重启后先对一次账
        self._reconciling = True
        now = self.app.clock()
        plan = getattr(self.m.reconciler, "set_next_run", None)
        if callable(plan):
            plan(now + timedelta(seconds=self.app.app_cfg.reconcile_interval_s))
        try:
            await self.m.reconciler.reconcile(now)
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
                if isinstance(self.m.features, _SeedsAtr):
                    try:
                        self.m.features.seed_atr_history(sym, await self._fetch_atr_history(sym))
                    except Exception:  # 没有历史 = 没有下限，止损照当前 ATR 算
                        log.warning("%s 回填 7 天 ATR 历史失败", sym, exc_info=True)
                if not self.m.predictor.enabled:
                    return
                try:
                    await self.m.predictor.backfill(sym, await self._fetch_predictor_klines(sym))
                    # 当前这根预测员 K 线已收盘的几分钟也喂进去，实时 1 分钟线才能接着拼出下一根，
                    # 否则从半截接入差一根被当成缺口、回填白做（更早的分钟拼出来是重复的，会丢掉）
                    for k in minutes:
                        await self.m.predictor.on_kline(k)
                except Exception:
                    log.warning("%s 回填预测员 K 线失败，等实时数据攒够", sym, exc_info=True)

        await asyncio.gather(*(one(s) for s in symbols))

    async def _refill_predictor(self, sym: str) -> None:
        """实时 K 线断档、预测员缓冲被清空：用 REST 把预测员周期的历史补回来，马上能接着算。"""
        try:
            await self.m.predictor.backfill(sym, await self._fetch_predictor_klines(sym))
        except Exception:
            log.warning("%s 断档后补预测员 K 线失败，等实时数据攒够", sym, exc_info=True)
        finally:
            self._predictor_refilling.discard(sym)

    def _prices(self, pool: SymbolPool) -> dict[str, float]:
        out: dict[str, float] = {}
        for e in pool.in_pool:
            snap = self.m.features.snapshot(e.symbol)
            if snap is not None:
                out[e.symbol] = snap.last_price
        return out

    def _atrs(self, pool: SymbolPool) -> dict[str, float]:
        """各币的 ATR（最长持仓档对应周期），临时会判断「走了几倍 ATR」用。"""
        out: dict[str, float] = {}
        for e in pool.in_pool:
            snap = self.m.features.snapshot(e.symbol)
            if snap is not None and snap.atr > 0:
                out[e.symbol] = snap.atr
        return out

    async def config_tick(self) -> None:
        """设置文件改了就在这个空档换上热生效的项（core/hotconfig.py）；其余项仍要重启。"""
        self.app.reload_config()

    async def slow_tick(self) -> None:
        now = self.app.clock()
        if self._last_screen is None or now - self._last_screen >= timedelta(
            minutes=self.app.app_cfg.screener_interval_min
        ):
            await self._screen_now(now)
        pool = self.app.pool
        if pool is None or self._refreshing:
            return
        cards = self.m.cards
        events = cards if isinstance(cards, _EventMeetings) else None
        reason: str | None = None
        if isinstance(cards, _RefreshReason):
            if events is None:
                reason = cards.refresh_reason(now, pool, self._prices(pool))
            else:
                reason = cards.refresh_reason(now, pool, self._prices(pool), self._atrs(pool))
        interval = timedelta(minutes=self.app.app_cfg.slow_loop_interval_min)
        if events is not None:
            interval = events.schedule_interval(interval)  # 日花费到 80% 会放慢
        if (
            reason is None
            and self._last_card_at is not None
            and now - self._last_card_at >= interval
            and not (events is not None and events.fixed_time)  # 每天固定时间开的档不按间隔兜底
        ):
            reason = "scheduled"
        if isinstance(cards, _RefreshGate):
            while (notice := cards.take_notice()) is not None:
                await self.app._alert(AlertLevel.WARN, "军师团暂停开会", notice, "meeting_paused")
            if not cards.may_refresh(now):
                return  # 失败退避中或当日花费超限；定时兜底也不能绕过
        if is_stub(cards):
            return
        if reason is None:
            event = events.pop_event(now, pool) if events is not None else None
            if events is not None and event is not None:
                self._refreshing = True
                self._spawn(self._refresh_event(events, pool, *event))
            return
        # 顺序 选币 → 总盘会：每一整轮会议前先把候选池刷新（刚选过就不重复）
        if self._last_screen is None or now - self._last_screen >= SCREEN_FRESH:
            await self._screen_now(now)
            pool = self.app.pool or pool
        self._refreshing = True
        self._last_card_at = now
        self._spawn(self._refresh(pool, reason))

    async def _screen_now(self, now: datetime) -> None:
        self._last_screen = now
        await self.app.refresh_lot_rules()
        await self._rescreen(now)

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
                    "news_block",
                )

    async def _refresh(self, pool: SymbolPool, reason: str) -> None:
        await self._refresh_run(pool, reason)

    async def _refresh_run(self, pool: SymbolPool, reason: str) -> tuple[BattleCard | None, str]:
        """开一整轮会；返回 (新卡, 失败原因)，开会失败时新卡是 None。"""
        card: BattleCard | None = None
        err = ""
        try:
            if isinstance(self.m.cards, _HoldsAware):
                self.m.cards.set_held({p.symbol: p.side for p in self.positions})
            card = await self.m.cards.refresh(pool, reason)
        except Exception as exc:
            err = str(exc) or type(exc).__name__
            log.exception("军师团开会失败，沿用旧卡直到过期")
        finally:
            self._refreshing = False
            # 挑中的币已经在新卡上（开会失败就不再盯它们）
            self._meeting_picks = set()
            self.m.predictor.set_priority(set())
            try:
                await self.sync_watch()
            except Exception:
                log.exception("开完会重订行情失败，下一轮持仓管家再订")
        return card, err

    async def _refresh_chain(self, cmd: PanelCommand, path: Path | None) -> None:
        """面板「重新开会」：整条链真跑一遍——按同一套筛选重新选币（写 symbol_pool.json）→ 总盘会 →
        每个币完整分析会 → 新卡；不沿用上一轮结论。跑完才写回执：选了几个、上会几个、用时、花费。"""
        t0 = time.monotonic()
        started = self.app.clock()
        status: Literal["done", "failed"] = "failed"
        detail = ""
        try:
            try:
                await self.app.refresh_lot_rules()
                await self._rescreen(self.app.clock())
            except Exception:
                log.exception("重新开会前选币失败，沿用上一份候选池")
            pool = self.app.pool
            if pool is None:
                self._refreshing = False
                detail = "重新开会取消：还没有候选池"
            else:
                card, err = await self._refresh_run(pool, "manual")
                if card is None:
                    detail = f"重新开会失败（沿用旧卡）：{err}"
                else:
                    met = sum(1 for s in card.symbols if s.issued_at and s.issued_at >= started)
                    secs = int(time.monotonic() - t0)
                    status = "done"
                    detail = (
                        f"已重新开会：选了 {len(pool.in_pool)} 币 / 上会 {met} 币 / "
                        f"用时 {secs // 60} 分 {secs % 60} 秒 / 花费 ${card.llm_cost_usd:.3f}"
                    )
        except Exception as exc:
            self._refreshing = False
            detail = f"重新开会出错：{exc!r}"[:200]
            log.exception("重新开会出错")
        finally:
            self._inflight.discard(cmd.id)
        now = self.app.clock()
        self._guard_note(
            Outcome.ACTION if status == "done" else Outcome.REJECT,
            f"面板指令 refresh_cards（{status}）：{detail}"[:200],
            now,
        )
        if path is not None:
            try:
                CommandInbox(self.app.app_cfg.data_dir).finish(
                    path,
                    CommandResult(id=cmd.id, kind=cmd.kind, status=status, detail=detail, ts=now),
                )
            except OSError:
                log.exception("写重新开会回执失败")

    async def _refresh_event(
        self, events: _EventMeetings, pool: SymbolPool, symbol: str, trigger: str
    ) -> None:
        try:
            if isinstance(self.m.cards, _HoldsAware):
                self.m.cards.set_held({p.symbol: p.side for p in self.positions})
            await events.refresh_symbol(pool, symbol, trigger)
        except Exception:
            log.exception("%s 临时会失败，沿用现有作战卡", symbol)
        finally:
            self._refreshing = False

    async def _rescreen(self, now: datetime) -> None:
        self.app.reload_pool_overrides()  # 手改 data/pool_overrides.json 也在下一次选币生效
        try:
            pool = await self.m.screener.screen(now)
        except Exception:
            log.exception("选币失败，沿用上一份候选池")
            return
        self.app.set_pool(pool)

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
            if cmd is not None and cmd.id in self._inflight:
                continue
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
                self._cmd_path = path
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
                if cmd.kind == "refresh_cards" and status == "done":
                    continue  # 回执等整条链跑完再写（见 _refresh_chain）
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
        await self.app._alert(AlertLevel.INFO, "主程序重启", "面板指令：重新加载配置", "control")
        return "done", "在途动作已做完，主程序已退出，launchd 会自动拉起"

    async def _run_command(self, cmd: PanelCommand) -> tuple[Literal["done", "rejected"], str]:
        if cmd.kind == "pause":
            self.app.set_paused(True, reason=cmd.params.get("reason", ""), command_id=cmd.id)
            await self.app._alert(
                AlertLevel.WARN, "已暂停开新仓", "面板指令；已有持仓照常看管", "control"
            )
            return "done", "已暂停开新仓"
        if cmd.kind == "resume":
            if self.app.pool_overrides_corrupt:
                return "rejected", "选币覆盖项文件读不出（拉黑清单没生效），修好或删掉后重启主程序"
            self.app.set_paused(False, command_id=cmd.id)
            await self.app._alert(AlertLevel.INFO, "已恢复开新仓", "面板指令", "control")
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
                AlertLevel.WARN, "面板全部平仓", "已同时暂停开新仓，恢复需另发指令", "control"
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
        if cmd.kind in POOL_ACTIONS:
            sym = cmd.params.get("symbol", "")
            try:
                self.app.apply_pool_override(cast(PoolAction, cmd.kind), sym)
            except ValueError as e:
                return "rejected", str(e)
            await self._rescreen(self.app.clock())
            pool = self.app.pool
            inside = pool is not None and any(e.symbol == sym for e in pool.in_pool)
            return (
                "done",
                f"{_POOL_DONE[cmd.kind]} {sym}；已重新选币，现在{'在' if inside else '不在'}池里",
            )
        if cmd.kind in CATEGORY_ACTIONS:
            try:
                self.app.apply_category_override(cmd.kind, cmd.params)
            except ValueError as e:
                return "rejected", str(e)
            await self._rescreen(self.app.clock())
            what = "类别开关" if cmd.kind == "set_category" else "每类上限"
            return "done", f"已更新{what}（{cmd.params.get('category', '')}）；已重新选币"
        if cmd.kind == "unblock_symbol":
            sym = cmd.params.get("symbol", "")
            if not self.app.unblock_symbol(sym):
                return "rejected", f"{sym} 没有被封"
            return "done", f"已解封 {sym}（只在本次运行有效）"
        # refresh_cards：整条链重跑 选币 → 总盘会 → 军师团
        if is_stub(self.m.cards):
            return "rejected", "军师团未接入"
        if self._refreshing:
            return "rejected", "军师团正在开会"
        self._refreshing = True
        now = self.app.clock()
        self._last_card_at = self._last_screen = now
        self._inflight.add(cmd.id)
        self._spawn(self._refresh_chain(cmd, self._cmd_path))
        return "done", "已开始：选币 → 总盘会 → 军师团"
