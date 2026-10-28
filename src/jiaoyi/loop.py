"""主循环：把行情事件分发给快环各角色，并跑盯仓、对账、选币、开会这些定时任务。

一笔交易的路径（设计文档 §三）::

    行情事件 → 特征 → 巡逻兵（有动作就平仓）→ 成交事件后哨兵出信号
    → 拍板人 → 风控员 → 放行才交给执行器开仓（执行器成交后自己挂保命止损）

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
import json
import logging
import os
import threading
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping, Sequence
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
    FeatureSnapshot,
    Interval,
    JevResponse,
    KeeperMarketProgress,
    KeeperVerdict,
    Kline,
    MarketEvent,
    MarketView,
    MarkPrice,
    OrderIntent,
    Outcome,
    PositionRecord,
    PositionStatus,
    RiskVerdict,
    Role,
    RunMode,
    Side,
    SymbolPool,
    Trade,
)
from jiaoyi.contracts.position import CloseReason
from jiaoyi.core import close_sources, paper_stop
from jiaoyi.core.commands import CommandInbox, CommandResult, PanelCommand
from jiaoyi.core.pool_overrides import CATEGORY_ACTIONS, POOL_ACTIONS, PoolAction
from jiaoyi.core.recorder import RoleRecorder
from jiaoyi.execution.brackets import CloseInProgress
from jiaoyi.execution.income_daily import INCOME_REFRESH_S
from jiaoyi.execution.mainnet_shadow import SHADOW_EVERY_S
from jiaoyi.gate.jev import JevDecisionModel
from jiaoyi.keeper.assistant import CLOSE_SOURCE, KeeperAssistant, TransportScorer
from jiaoyi.keeper.keeper import close_blocked
from jiaoyi.market.binance_ws import FINAL_STATUSES, ContractInfo
from jiaoyi.market.features import (
    ATR_PERIOD,
    BTC,
    HISTORY_DAYS,
    VOL_24H_MINUTES,
    backfill_minutes,
)
from jiaoyi.market.rest import (
    RateLimited,
    WeightBudget,
    fetch_klines_between,
    fetch_recent_klines,
    save_ban,
)
from jiaoyi.market.snapshot_file import WRITE_EVERY_S as SNAPSHOT_WRITE_EVERY_S
from jiaoyi.market.snapshot_file import MarketSnapshotFile
from jiaoyi.risk.guard import DELIST_WINDOW, entry_zone_miss, entry_zone_miss_at
from jiaoyi.sentinel.regime_flip import (
    Breadth,
    FlipJournal,
    KlineSource,
    RegimeFlip,
    check_diagnostics,
)
from jiaoyi.sentinel.regime_flip import (
    snapshot as regime_snapshot,
)
from jiaoyi.slow import binance_gate
from jiaoyi.slow.commander import MARKET_SNAPSHOT_FRESH, BattleCardCommander
from jiaoyi.slow.meeting_live import interrupt_open

log = logging.getLogger(__name__)

SLOW_CHECK_EVERY_S = 60.0
URGENT_CHECK_EVERY_S = 1.0
DAY_START_CHECK_EVERY_S = 60.0
"""Must stay well under ``DAY_START_TOLERANCE_S`` so a running process captures midnight on time."""
HEARTBEAT_EVERY_S = 30.0
SNAPSHOT_EVERY_S = SNAPSHOT_WRITE_EVERY_S
NEWS_POLL_EVERY_S = 120.0
CREDIT_CHECK_EVERY_S = 120.0
"""模型额度看护多久看一眼（撞 402 要及时发；余额每小时才真去查一次）。"""
COMMAND_POLL_EVERY_S = 1.0
CONFIG_WATCH_EVERY_S = 3.0  # 看设置文件变没变（热生效，core/hotconfig.py）
DESK_CHECK_EVERY_S = 30.0
"""大盘台：多久看一次该不该跑（定时格子 / 翻转 / 急动），顺便喂一次价给急动检测。"""
KEEPER_CONCURRENCY = 5
ACCOUNT_RETRY_S = 1.0
FROZEN_PEAK_CHECK_EVERY_S = 1.0  # 后台模式下多久看一眼哪笔仓该重读冻结盈利价证据
PAPER_STOP_RETRY_S = 30.0
"""A booked paper stop whose Demo position is still open asks for the close again this often."""
SIGNAL_CACHE = 200  # 记住最近这么多条处理过的信号（面板重新检查 / 放行用）
"""持仓管家一轮里最多几笔一起问 Jev。"""
RESTART_WAIT_CLOSE_S = 60.0
# 决定重启后：收尾最多等这么久；军师团开会跑在工作线程里取消不掉，收尾超时就直接退出
RESTART_SHUTDOWN_S = 20.0
# 取消任务前先等已经在跑的平仓 / 对账做完（不再发起新的），最多这么久
RESTART_DRAIN_S = 15.0
RESTART_HARD_EXIT_S = 45.0
TASK_CANCEL_WAIT_S = 10.0
PRUNE_EVERY = timedelta(hours=1)
SCREEN_FRESH = MARKET_SNAPSHOT_FRESH
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
class _FrozenPeakCache(Protocol):
    """管家：冻结盈利价证据在后台按仓读、复查只用缓存（JevKeeper）。"""

    def frozen_peak_background(self) -> bool: ...

    def frozen_peak_due(self, position_id: str, now: datetime) -> bool: ...

    async def refresh_frozen_peak(self, position_id: str, now: datetime) -> bool: ...


@runtime_checkable
class _CardRuleKeeper(Protocol):
    def card_rule_applies(self, position: PositionRecord, now: datetime) -> bool: ...

    def review_card_rule(self, position: PositionRecord, now: datetime) -> KeeperVerdict | None: ...


@runtime_checkable
class _TightensStop(Protocol):
    """执行器：反向卡没经价格确认时收紧交易所托管止损（HbExecutorAdapter，#314）。"""

    async def tighten_stop(
        self,
        position_id: str,
        *,
        reason: str,
        buffer_bps: float,
        mark_gap_bps: float,
        profit_mode: str,
        distance_mult: float,
        card_key: str | None,
        anchor_retry_s: float,
        allow_close: bool,
        label: str,
    ) -> dict[str, Any]: ...


@runtime_checkable
class _LedgerPositions(Protocol):
    """执行器：账本里此刻还没结束的持仓，不查 hummingbot（HbExecutorAdapter）。"""

    def live_positions(self) -> list[PositionRecord]: ...


@runtime_checkable
class _PaperStopLevels(Protocol):
    """Executor: the stops a position holds and the close detail (HbExecutorAdapter)."""

    def paper_stop_levels(self, position_id: str) -> list[tuple[str, float | None]]: ...

    def paper_stop_since(self, position_id: str) -> datetime | None: ...

    def note_close_detail(self, position_id: str, detail: str) -> None: ...


@runtime_checkable
class _ExitInFlight(Protocol):
    def protection_exit_in_flight(self, position_id: str) -> tuple[str, str, str] | None: ...

    def take_profit_exiting(self, position_id: str) -> bool: ...


@runtime_checkable
class _ServerProtection(Protocol):
    def server_managed(self, position_id: str) -> bool: ...

    async def protection_tick(self) -> None: ...


@runtime_checkable
class _DayStartAware(Protocol):
    def day_start_due(self, now: datetime) -> bool: ...

    def set_price_source(self, price_at: Callable[[str, datetime], float | None]) -> None: ...


@runtime_checkable
class _NewsAware(Protocol):
    def notify_news(self, symbol: str) -> None: ...

    def block_symbol(self, symbol: str, reason: str) -> None: ...


@runtime_checkable
class _ChaseRecheck(Protocol):
    """风控员：挂单没成交改市价追进前按此刻状态再审（``RiskGuardImpl.recheck_chase``）。"""

    def recheck_chase(
        self, intent: OrderIntent, card: BattleCard | None, account: AccountState, now: datetime
    ) -> list[str]: ...


@runtime_checkable
class _ManualReview(Protocol):
    """风控员：面板「重新检查 / 放行」（``RiskGuardImpl.review_manual``）。"""

    def review_manual(
        self,
        signal: CandidateSignal,
        jev: JevResponse,
        card: BattleCard | None,
        account: AccountState,
        now: datetime,
        *,
        market: FeatureSnapshot | None,
        release: bool,
    ) -> RiskVerdict: ...


@runtime_checkable
class _ChaseHook(Protocol):
    def set_chase_request(self, request: Callable[[OrderIntent], None]) -> None: ...

    async def open_chase(self, intent: OrderIntent) -> PositionRecord: ...


SPEND_ALERT_TITLE = "AI 花费超过本档默认日上限（不限模式，只提醒）"


@runtime_checkable
class _SpendAlerts(Protocol):
    """日上限设为不限时，花费过了本档默认上限的提醒（不停会）。"""

    def take_spend_alert(self) -> str | None: ...


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
        fetch_regime_klines: KlineSource | None = None,
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
        self._refreshing = False  # 整轮（定时 / 面板重新开会）在开
        self._event_running: set[str] = set()  # 在开临时会的币
        self._slow_lock = asyncio.Lock()
        self._regime_tick_lock = asyncio.Lock()
        self._market_refreshing = False
        self._market_task: asyncio.Task[None] | None = None
        self._desk_task: asyncio.Task[None] | None = None
        self._keeper_market: dict[str, KeeperMarketProgress] = {}
        self._keeper_market_path = Path(app.app_cfg.data_dir) / "keeper_market.json"
        self._keeper_slots = asyncio.Semaphore(KEEPER_CONCURRENCY)
        self._keeper_queued: set[str] = set()
        self._card_rule_queued: set[str] = set()
        self._tighten_logged: dict[str, tuple[str, str, str]] = {}  # 每仓上次记下的收紧结果
        self._tighten_done: dict[str, str] = {}  # 每仓已收紧 / 已够紧的反向指令，不再进执行器锁
        self._restore_keeper_market()
        self._regime_period: float | None = None
        self._regime_next: datetime | None = None
        self._regime = RegimeFlip(fl)
        self._regime_journal = FlipJournal(
            Path(app.app_cfg.data_dir) / "regime_flip", app.recorders.get(Role.SENTINEL)
        )
        regime_budget = WeightBudget()
        self._fetch_regime = fetch_regime_klines or (
            lambda sym, start, end: fetch_klines_between(sym, start, end, budget=regime_budget)
        )
        if isinstance(self.m.cards, BattleCardCommander):
            self.m.cards.set_regime_price(self._regime_btc_price)
            self.m.cards.set_on_market(self._market_published)
            self.m.cards.set_on_card(self._card_published)
            market_source = getattr(self.m.keeper, "set_market_source", None)
            if callable(market_source):
                cards = self.m.cards
                market_source(lambda: cards.latest_market_view)
            self.m.cards.set_market_review_enabled(
                lambda: self.app.app_cfg.fast_loop.market_review_on
            )
            snapshot = getattr(self.m.screener, "market_snapshot", None)
            if callable(snapshot):
                self.m.cards.set_market_snapshot(snapshot)
        self._shutting_down = False
        self.assistant = self._build_assistant()
        self._predictor_refilling: set[str] = set()  # 正在用 REST 补预测员历史的币
        # 读账户 → 风控员 → 开仓 必须串行，否则并发信号都按“开仓前”的账户算，绕过笔数和敞口上限
        self._open_lock = asyncio.Lock()
        # 处理过的信号（面板「风控员拒绝」详情的重新检查 / 放行要按 signal_id 找回）
        self._signals: OrderedDict[str, CandidateSignal] = OrderedDict()
        if isinstance(self.m.executor, _ChaseHook):
            self.m.executor.set_chase_request(self._request_chase)
        self._closing: set[str] = set()
        self._against_pending: set[str] = set()  # 因反向信号叫了急查、管家还没给结论的持仓
        self._reviewing: set[str] = set()  # 管家正在问 Jev 的持仓：同一笔不同时问两次
        self._paper_stops = paper_stop.PaperStopBook(
            Path(app.app_cfg.data_dir) / paper_stop.PAPER_STOP_FILE
        )
        self._paper_close_at: dict[str, datetime] = {}  # last Demo close asked for a paper stop
        self._paper_safe: dict[str, set[float]] = {}
        """Position -> every current stop level the mainnet price has been seen safely beyond."""
        self.paper_unseen_crosses = 0
        """Crosses ignored because that stop level was never seen on its safe side."""
        self._paper_unseen_logged: set[tuple[str, float]] = set()
        self._paper_load_alerted = False
        self._tp_past: dict[str, datetime] = {}
        """限价止盈：价格第一次穿过止盈价的时间（按仓位），待满 tp_limit_fallback_s 改市价平。"""
        self._last_prune: datetime | None = None
        # 面板“重启生效”：不开新仓，等在途动作做完再退出（launchd KeepAlive 拉起）
        self._restart_check = restart_check
        self._restarting = False
        # 重启已定：不再发起新的平仓 / 对账（交给重启后的进程，期间靠币安上的保命止损）
        self._leaving = False
        self._reconciling = False
        self._day_start_failing = False
        self._restart = asyncio.Event()
        self.restart_requested = False
        self._restart_exit = restart_exit
        # restart 的回执在进程真要退出前一刻才写（面板看到“完成”时进程确实在退）
        self._restart_receipt: tuple[CommandInbox, Path, CommandResult] | None = None
        self._inflight: set[str] = (
            set()
        )  # 还在跑、回执要等跑完才写的面板指令（重新开会、重新检查 / 放行）
        self._manual_busy: set[str] = set()  # 正在重新检查 / 放行的币：同一个币同时只跑一个
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
        self._card_published()  # 启动已恢复的有效反向卡不等第一轮30秒巡检。
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
            self._every(FROZEN_PEAK_CHECK_EVERY_S, self.frozen_peak_tick),
            self._every(cfg.reconcile_interval_s, self.reconcile_tick),
            self._every(SLOW_CHECK_EVERY_S, self.slow_tick),
            self._every(CONFIG_WATCH_EVERY_S, self._regime_periodic_tick),
            self._every(HEARTBEAT_EVERY_S, self._heartbeat_tick),
            self._every(SNAPSHOT_EVERY_S, self.market_snapshot_tick),
            self._every(NEWS_POLL_EVERY_S, self.news_tick),
            self._every(CREDIT_CHECK_EVERY_S, self.app.credit_tick),
            self._every(COMMAND_POLL_EVERY_S, self.command_tick),
            self._every(CONFIG_WATCH_EVERY_S, self.config_tick),
            self._every(SHADOW_EVERY_S, self.app.mainnet_shadow_tick),
            # 有交易所连接时账本回填和收入流水由 LedgerSync 管（成交回报叫醒，60 秒兜底）
            (
                asyncio.create_task(self.app.ledger.run())
                if self.app.ledger is not None
                else self._every(INCOME_REFRESH_S, self.app.refresh_income)
            ),
        ]
        if isinstance(self.m.executor, _DayStartAware):
            self.m.executor.set_price_source(self._local_price_at)
            self._loops.append(self._every(DAY_START_CHECK_EVERY_S, self.day_start_tick))
        if isinstance(self.m.executor, _ServerProtection):
            # 每轮结束后再等待，避免慢交易所请求使保护轮询重叠。
            self._loops.append(self._every(1.0, self.m.executor.protection_tick))
        if self.app.derivs is not None:
            # 持仓量每 60 秒、多空比每 5 分钟（盯着的币；查失败自己跳过）
            self._loops.append(asyncio.create_task(self.app.derivs.run(self._derivs_symbols)))
        if getattr(self.app, "desk", None) is not None:
            # 大盘台自己的定时器：不占 _slow_lock / _refreshing，和开会互不等待
            self._loops.append(self._every(DESK_CHECK_EVERY_S, self.desk_tick))
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
        # 先停管家助手：它在途的判断不能在下面取消后台任务的空档里再交出平仓
        self._shutting_down = True
        await self.assistant.aclose()
        # 会议跑在线程池里取消不掉：先把它们的实况标成中断，线程之后写的会被忽略
        interrupt_open()
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

    def _spawn(self, coro: Awaitable[None], role: Role | None = None) -> None:
        """``role``: whose decision log gets an ERROR record if the task dies with an exception;
        without one the failure is only logged."""
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(lambda t: self._task_failed(t, role))

    def _task_failed(self, task: asyncio.Future[None], role: Role | None) -> None:
        if task.cancelled() or (exc := task.exception()) is None:
            return
        coro = task.get_coro() if isinstance(task, asyncio.Task) else None
        what = getattr(coro, "__qualname__", None) or repr(task)
        log.error("后台任务 %s 出错", what, exc_info=(type(exc), exc, exc.__traceback__))
        rec = self.app.recorders.get(role) if role is not None else None
        if rec is not None:
            rec.record(
                self.app.clock(),
                Outcome.ERROR,
                f"后台任务 {what} 出错：{type(exc).__name__}: {exc}"[:300],
                output={"task": what, "error": repr(exc)[:500]},
            )

    async def _heartbeat_tick(self) -> None:
        self.app.heartbeat_all()
        try:
            self.assistant.tick(self.app.clock())
            self.assistant.heartbeat(self.app.clock())
        except Exception:
            log.exception("管家助手心跳失败")
        save_ban(self.app.app_cfg.data_dir)

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
        if self.app.observations is not None:
            with contextlib.suppress(Exception):
                self.app.observations.on_event(event)
        self._market_file.count(event, self.app.clock())
        if isinstance(event, Kline):
            self._sync_wanted()
            await self.m.predictor.on_kline(event)
            for sym in self.m.predictor.take_gaps() - self._predictor_refilling:
                self._predictor_refilling.add(sym)
                self._spawn(self._refill_predictor(sym))
        if isinstance(event, Trade | MarkPrice):
            # Before the patrol: on mainnet the stop fills before any market close it sends.
            self._paper_stop_check(event)
        action = self.m.patrol.check(event, self.positions)
        if action is not None:
            targets = [
                p for p in self.positions if not action.symbols or p.symbol in action.symbols
            ]
            for pos in targets:
                # 平仓要等执行器结束（最长十几秒），放后台并行，行情和其它标的不能停
                if pos.position_id not in self._closing:
                    self._spawn(self._patrol_close(pos, action.reason), Role.PATROL)
        if isinstance(event, Trade) and self.app.app_cfg.limit_take_profit_on:
            self._tp_fallback(event)
        if isinstance(event, Trade):
            snap = self.m.features.snapshot(event.symbol)
            if snap is None:
                return
            now = self.app.clock()
            for signal in self.m.trigger.check(snap, self.m.cards.current(now), now):
                # 拍板要几百毫秒，放后台，不卡行情
                self._spawn(self.handle_signal(signal), Role.RISK_GUARD)
            if isinstance(self.m.trigger, _AgainstAware):
                for sig in self.m.trigger.pop_against():
                    self._route_against(self.m.trigger, sig, now)
            fl = self.app.app_cfg.fast_loop
            if fl.keeper_assistant_on and fl.keeper_assistant_key_level:
                self._assistant_levels(event.symbol, event.price, now)

    def _build_assistant(self) -> KeeperAssistant:
        app, fl = self.app, self.app.app_cfg.fast_loop
        model = app.keeper_jev or self.m.decision_model
        jev = (
            TransportScorer(
                model.transport,
                usd_per_mtok=fl.jev_usd_per_mtok,
                max_errors=fl.jev_max_consecutive_errors,
                cooldown_s=fl.jev_error_cooldown_s,
            )
            if isinstance(model, JevDecisionModel)
            else None
        )
        cards = self.m.cards if isinstance(self.m.cards, BattleCardCommander) else None
        rec = app.recorders.get(Role.KEEPER_ASSISTANT) or RoleRecorder(
            Role.KEEPER_ASSISTANT, app.log, day_reset_tz=app.risk_cfg.day_reset_tz
        )
        assistant = KeeperAssistant(
            lambda: self.app.app_cfg.fast_loop,
            rec,
            features=self.m.features.snapshot,
            close=self._assistant_close,
            clock=app.clock,
            data_dir=Path(app.app_cfg.data_dir),
            jev=jev,
            b_scorer=getattr(app, "assistant_b", None),
            cards=cards,
            alert=self._assistant_alert,
            day_reset_tz=app.risk_cfg.day_reset_tz,
            close_source=lambda pid: (
                close_sources.read(self.app.app_cfg.data_dir).get(pid) or {}
            ).get("source"),
        )
        if cards is not None:
            cards.set_on_flip_pending(assistant.on_flip_pending)
        return assistant

    def _assistant_close(self, pos: PositionRecord, reason: str) -> str | None:
        """管家助手要平：过管家同一道平仓前检查，经正常平仓路径另起任务去平（同一笔在平 / 已平
        就跳过）。返回 None = 已交出去，否则是没平的原因。平仓原因枚举照旧记「持仓管家」，
        是管家助手平的另记在 ``close_sources.json``。"""
        if self._leaving or self._shutting_down:
            return "主程序正在重启或退出"
        current = next((p for p in self.positions if p.position_id == pos.position_id), None)
        if current is None or current.status is not PositionStatus.OPEN:
            return "这笔已不在持仓里"
        if pos.position_id in self._closing:
            return "这笔正在平"
        now = self.app.clock()
        guard = getattr(self.m.keeper, "pre_close_guard", None)
        if callable(guard):
            blocked = guard(pos.position_id, now)
            if blocked:
                return f"平仓前检查拦下：{blocked}"
        self._guard_note(Outcome.ACTION, f"{pos.symbol} 平仓：{reason}", now)
        self._spawn(
            self.close(current, CloseReason.KEEPER, source=CLOSE_SOURCE, detail=reason),
            Role.EXECUTOR,
        )
        return None

    def _record_source(self, pos: PositionRecord, source: str | None, detail: str) -> None:
        if source is not None:
            close_sources.record(
                self.app.app_cfg.data_dir, pos.position_id, source, detail, self.app.clock()
            )

    def _assistant_alert(self, title: str, text: str) -> None:
        if not self._leaving:
            self._spawn(self.app._alert(AlertLevel.INFO, title, text, "system"))

    async def _close_targets(self) -> list[PositionRecord]:
        """Positions a panel close may target. With a ledger this is a local read: a full
        ``refresh_positions`` syncs every position under its exit lock and queues behind the
        exchange rate limiter and any other close in flight, which delayed a click by up to
        20 s before the close even started. ``close`` re-checks the ledger itself."""
        if isinstance(self.m.executor, _LedgerPositions):
            return self._ledger_positions()
        await self.refresh_positions()
        return list(self.positions)

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
        # 管家助手和管家急查各走各的，谁先平谁算（平仓去重在 close 里）
        for pos in self._ledger_positions():
            if pos.symbol == sig.symbol:
                self.assistant.on_reverse_signal(pos, sig.side, f"{sig.kind.value}：{sig.desc}")
        for pos, one_shot in rechecks:
            if isinstance(self.m.keeper, _NotesAgainst):
                self.m.keeper.note_against(pos.position_id, sig, one_shot)
            self._against_pending.add(pos.position_id)
            if isinstance(self.m.patrol, _HasUrgent):
                self.m.patrol.urgent.add(pos.position_id)  # 和巡逻兵报警线同一条急查通道
            else:
                self._spawn(self._review(pos), Role.KEEPER)

    def _assistant_levels(self, symbol: str, px: float, now: datetime) -> None:
        held = [p for p in self.positions if p.symbol == symbol]
        if not held:
            return
        card = self.m.cards.current(now)
        cs = card.for_symbol(symbol) if card is not None else None
        for pos in held:
            self.assistant.on_price(pos, cs.key_levels if cs is not None else [], px)

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
            if isinstance(self.m.executor, _ServerProtection):
                try:
                    if self.m.executor.server_managed(pid):
                        self._tp_past.pop(pid, None)
                        continue
                except Exception:
                    # 无法确认所有权时不能用另一价格源发出平仓。
                    log.exception("止盈兜底无法确认交易所保护所有权: %s", pid)
                    self._tp_past.pop(pid, None)
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

    def _paper_stop_mode(self) -> paper_stop.PriceSource | None:
        """None = off. Live never runs it: there the exchange stop is the real one. Only Demo
        Trading: the legacy testnet prices drift too far from mainnet for its stops to be
        compared with mainnet prices."""
        cfg = self.app.app_cfg
        mode = cfg.fast_loop.paper_stop_on_mainnet
        if mode == "off" or cfg.mode == RunMode.LIVE or cfg.sim_environment != "demo":
            return None
        return mode

    def _paper_stop_check(self, event: Trade | MarkPrice) -> None:
        """First mainnet cross of a position's tightest stop: book the paper exit at the stop
        and ask for the Demo close (``[fast_loop] paper_stop_on_mainnet``)."""
        mode = self._paper_stop_mode()
        if mode is None:
            return
        if isinstance(event, Trade):
            if mode != "last":
                return
            price = event.price
        else:
            if mode != "mark":
                return
            price = event.mark_price
        for pos in self.positions:
            if (
                pos.symbol != event.symbol
                or pos.status is not PositionStatus.OPEN
                or pos.entry_price is None
                or self._paper_stops.get(pos.position_id) is not None
            ):
                continue
            try:
                self._paper_stop_one(pos, price, event.ts, mode)
            except Exception:
                # Patrol and entry triggers run after this on the same event.
                log.exception("主网止损检查出错 %s", pos.position_id)

    def _paper_stop_one(
        self, pos: PositionRecord, price: float, ts: datetime, mode: paper_stop.PriceSource
    ) -> None:
        pid = pos.position_id
        since = self._paper_stop_since(pos)
        if ts < since:
            return  # a price from before the position existed (buffered or replayed)
        levels = self._paper_stop_levels(pos)
        if not levels:
            return
        # A cross counts only against a stop level the price was first seen safely beyond:
        # a leg placed off Demo's mark can sit past mainnet's price from the moment it is set
        # (the Demo-mainnet basis), and after a restart, a hot enable or a fresh fill we have
        # not yet seen where the price stood. Tracked per level, so a tighter stop that never
        # had a safe sighting does not mask a looser one that did.
        current = {p for _, p in levels}
        safe = self._paper_safe.setdefault(pid, set())
        safe &= current  # levels that moved away start over
        armed: list[tuple[str, float]] = []
        unseen: list[tuple[str, float]] = []
        for src, level in levels:
            if not paper_stop.crossed(pos.side, price, level):
                safe.add(level)
            elif level in safe:
                armed.append((src, level))
            else:
                unseen.append((src, level))
        stop = paper_stop.tightest_stop(pos.side, armed)
        if stop is None:
            if unseen:
                self._paper_unseen(pos, unseen, price)
            return
        self._paper_stop_hit(pos, stop, price, ts, mode)

    def _paper_stop_since(self, pos: PositionRecord) -> datetime:
        ex = self.m.executor
        if isinstance(ex, _PaperStopLevels):
            try:
                since = ex.paper_stop_since(pos.position_id)
            except Exception:
                log.exception("读不到 %s 的成交时间，按开仓时间算", pos.position_id)
            else:
                if since is not None:
                    return since
        return pos.opened_at

    def _paper_stop_levels(self, pos: PositionRecord) -> list[tuple[str, float]]:
        ex = self.m.executor
        candidates: list[tuple[str, float | None]] = [("ledger", pos.stop_loss_price)]
        if isinstance(ex, _PaperStopLevels):
            try:
                candidates = ex.paper_stop_levels(pos.position_id) or candidates
            except Exception:
                log.exception("读不到 %s 的止损，主网止损这次只按账本止损判", pos.position_id)
        return [(src, p) for src, p in candidates if p is not None and p > 0]

    def _paper_unseen(
        self, pos: PositionRecord, unseen: list[tuple[str, float]], price: float
    ) -> None:
        """Count a cross of stops never seen safe; one decision record per (position, level)."""
        self.paper_unseen_crosses += 1
        fresh = [(s, p) for s, p in unseen if (pos.position_id, p) not in self._paper_unseen_logged]
        if not fresh:
            return
        self._paper_unseen_logged.update((pos.position_id, p) for _, p in fresh)
        rec = self.app.recorders.get(Role.PATROL)
        if rec is None:
            return
        text = "、".join(f"{s} {p:g}" for s, p in fresh)
        rec.record(
            self.app.clock(),
            Outcome.INFO,
            f"{pos.symbol} 主网价 {price:g} 已在止损 {text} 外，但没见过它在安全一侧，不算越过",
            symbol=pos.symbol,
            position_id=pos.position_id,
            output={
                "paper_stop_unseen": [{"source": s, "stop": p} for s, p in fresh],
                "price": price,
                "unseen_crosses_total": self.paper_unseen_crosses,
            },
        )

    def _exit_in_flight(self, position_id: str) -> Literal["stop", "take_profit"] | None:
        """Demo's own hosted protection is already exiting, and through which side."""
        ex = self.m.executor
        if not isinstance(ex, _ExitInFlight):
            return None
        try:
            found = ex.protection_exit_in_flight(position_id)
            if found is None or found[0] != "exiting":
                return None
            return "take_profit" if ex.take_profit_exiting(position_id) else "stop"
        except Exception:
            log.exception("读不到 %s 的托管保护状态，按没在退出处理", position_id)
            return None

    def _paper_stop_hit(
        self,
        pos: PositionRecord,
        stop: tuple[str, float],
        price: float,
        crossed_at: datetime,
        mode: paper_stop.PriceSource,
    ) -> None:
        pid = pos.position_id
        if pid in self._closing:
            return  # a close decided before the cross would have filled before it on mainnet
        current = next((p for p in self._ledger_positions() if p.position_id == pid), None)
        if current is None or current.status is not PositionStatus.OPEN:
            return
        if current.entry_price is None:
            return
        now = self.app.clock()
        exit_kind = self._exit_in_flight(pid)
        if exit_kind == "take_profit":
            return  # Demo filled its target first; booking a stop would turn a win into a loss
        exiting = exit_kind is not None
        fl = self.app.app_cfg.fast_loop
        booked = paper_stop.book_stop(
            current,
            stop,
            price=price,
            price_source=mode,
            crossed_at=crossed_at,
            now=now,
            slippage_bps=fl.paper_stop_slippage_bps,
            close_requested=not exiting,
        )
        if not self._paper_stops.add(booked):
            return
        source_cn = "标记价" if mode == "mark" else "成交价"
        sign = 1.0 if current.side is Side.LONG else -1.0
        gross = sign * current.quantity * (booked.paper_exit_price - current.entry_price)
        then = "Demo 的止损已在退出，只记账" if exiting else "已要求平 Demo 仓"
        summary = (
            f"{current.symbol} 主网{source_cn} {price:g} 越过止损 {stop[1]:g}，"
            f"模拟按 {booked.paper_exit_price:g} 平（毛利 {gross:+.2f}U），{then}"
        )
        rec = self.app.recorders.get(Role.PATROL)
        if rec is not None:
            rec.record(
                now,
                Outcome.CLOSE,
                summary,
                symbol=current.symbol,
                position_id=pid,
                output={"paper_stop": booked.model_dump(mode="json"), "gross_usdt": gross},
            )
        log.warning("%s", summary)
        self._spawn(
            self.app._alert(AlertLevel.INFO, "主网价格越过止损", summary, "close_done"),
            Role.PATROL,
        )
        if not exiting:
            self._paper_stop_close(current, now)

    def _paper_stop_close(self, pos: PositionRecord, now: datetime) -> None:
        self._paper_close_at[pos.position_id] = now
        ex = self.m.executor
        if isinstance(ex, _PaperStopLevels):
            try:
                ex.note_close_detail(pos.position_id, paper_stop.PAPER_STOP_DETAIL)
            except Exception:
                log.exception("记平仓原因失败 %s", pos.position_id)
        self._spawn(self.close(pos, CloseReason.STOP_LOSS), Role.PATROL)

    def _paper_stop_retry(self) -> None:
        """A booked position still open on Demo (the close failed, or the process restarted
        before it went through) is asked to close again; the cross itself is never re-booked."""
        if self._paper_stop_mode() is None:
            return
        book = self._paper_stops
        if book.load_error is not None and not self._paper_load_alerted:
            self._paper_load_alerted = True
            self._spawn(
                self.app._alert(
                    AlertLevel.WARN,
                    "模拟止损记账文件读不懂",
                    f"{book.load_error}；这些仓按 Demo 实际成交算，原文件下次写入前另存为 .bad-*",
                    "system",
                ),
                Role.PATROL,
            )
        book.flush()
        now = self.app.clock()
        for pos in self.positions:
            pid = pos.position_id
            if pos.status is not PositionStatus.OPEN or pid in self._closing:
                continue
            if self._paper_stops.get(pid) is None or self._exit_in_flight(pid) is not None:
                continue
            last = self._paper_close_at.get(pid)
            if last is not None and (now - last).total_seconds() < PAPER_STOP_RETRY_S:
                continue
            self._paper_stop_close(pos, now)

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
        - 临时不在交易（结算中、待上线等）：只记进 ``app.halted``，风控员挡新开仓，已有持仓不动；
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
        self._spawn(self._chase(intent), Role.RISK_GUARD)

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
            self._card_published()  # 开仓在途期间卡可能更新，立即按当前卡复核。

    @staticmethod
    def _same_decision_context(card: BattleCard, fresh: BattleCard | None) -> bool:
        if fresh is None or (fresh.card_id, fresh.version) != (card.card_id, card.version):
            return False
        before = card.current_market.regime_direction if card.current_market else None
        after = fresh.current_market.regime_direction if fresh.current_market else None
        return before == after

    async def _may_chase(self, intent: OrderIntent) -> list[str]:
        """挂单没成交、要改市价追进：按此刻的暂停 / 重启 / 拉黑 / 作战卡 / 账户再过一遍风控员。"""
        now = self.app.clock()
        why: list[str] = []
        if self.app.paused is not None:
            why.append("人工暂停开新仓")
        if self._restarting:
            why.append("主程序正在重启")
        if intent.symbol in self.app.pool_overrides.blacklist:
            why.append("你拉黑了这个币")
        if not isinstance(self.m.risk, _ChaseRecheck):
            return [*why, "风控员不支持追单复核"]
        account = await self.m.executor.account()
        now = self.app.clock()
        card = self.m.cards.current(now)
        direction = card.current_market.regime_direction if card and card.current_market else None
        if (
            intent.source_market_direction is not None
            and direction != intent.source_market_direction
        ):
            why.append("大盘方向已变，原拍板背景已过时，等新信号重新拍板")
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
        age = (now - snap.ts).total_seconds()
        if fast.chase_price_max_age_s > 0 and age > fast.chase_price_max_age_s:
            return [f"行情已旧 {age:.1f} 秒（上限 {fast.chase_price_max_age_s:g} 秒），这次不追"]
        miss = entry_zone_miss_at(
            intent.symbol, intent.side, snap.last_price, snap, card, fast, now
        )
        return [] if miss is None else [miss]

    async def handle_signal(self, signal: CandidateSignal) -> None:
        now = self.app.clock()
        card = self.m.cards.current(now)
        if card is None:
            return
        self._signals[signal.signal_id] = signal
        while len(self._signals) > SIGNAL_CACHE:
            self._signals.popitem(last=False)
        if self._restarting:
            self._guard_note(Outcome.REJECT, f"{signal.symbol} 主程序正在重启，不处理信号", now)
            return
        if signal.symbol in self.app.pool_overrides.blacklist:
            # 刚拉黑的币可能还在旧作战卡里：不等军师团下一轮，立即不开
            self._guard_note(Outcome.REJECT, f"{signal.symbol} 你拉黑了，不开新仓", now)
            return
        if self.app.paused is not None:
            # 人工暂停：连 Jev 都不问（省钱），记在风控员名下
            self._guard_note(Outcome.REJECT, f"{signal.symbol} 人工暂停开新仓，不处理信号", now)
            return
        miss = entry_zone_miss(signal, card, self.app.app_cfg.fast_loop, now)
        if miss is not None:
            # 卡的进场区间外：连 Jev 都不问（省钱），记在风控员名下
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
            if not self._same_decision_context(card, fresh):
                self._guard_note(
                    Outcome.REJECT, f"{signal.symbol} 拍板期间作战卡或大盘方向已变，重新等信号", now
                )
                return
            account = await self._read_account(signal, card)
            if account is None:
                return
            now = self.app.clock()
            fresh = self.m.cards.current(now)
            if not self._same_decision_context(card, fresh):
                self._guard_note(
                    Outcome.REJECT,
                    f"{signal.symbol} 等账户期间大盘方向或作战卡已变，重新等信号",
                    now,
                )
                return
            assert fresh is not None
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
            self._card_published()  # 开仓在途期间卡可能更新，立即按当前卡复核。

    async def _read_account(self, signal: CandidateSignal, card: BattleCard) -> AccountState | None:
        """Account state for the open, or None after recording why this signal is given up.

        A re-read only waits while the signal can still pass the guard's age check, and a
        re-read that succeeds is checked again for pause / restart and staleness, because the
        caller's checks ran before the wait."""
        retries = self.app.app_cfg.account_read_retries
        max_age = self.app.app_cfg.fast_loop.signal_max_age_s
        for attempt in range(retries + 1):
            try:
                account = await self.m.executor.account()
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                if attempt == retries:
                    self._account_reject(signal, card, f"读账户失败，本信号放弃：{err}", e)
                    return None
                left = max_age - (self.app.clock() - signal.ts).total_seconds()
                if left <= ACCOUNT_RETRY_S:
                    why = (
                        f"读账户失败，信号只剩 {max(left, 0.0):.1f} 秒有效，不再重读，"
                        f"本信号放弃：{err}"
                    )
                    self._account_reject(signal, card, why, e)
                    return None
                log.warning(
                    "读账户失败（第 %d 次），%g 秒后重读：%r", attempt + 1, ACCOUNT_RETRY_S, e
                )
                await asyncio.sleep(ACCOUNT_RETRY_S)
                continue
            if attempt == 0:
                return account
            if self.app.paused is not None or self._restarting:
                self._account_reject(signal, card, "重读账户期间已暂停或正在重启，不开仓", None)
                return None
            age = (self.app.clock() - signal.ts).total_seconds()
            if age > max_age:
                why = f"重读账户后信号已过 {age:.1f} 秒（上限 {max_age:g} 秒），本信号放弃"
                self._account_reject(signal, card, why, None)
                return None
            return account
        raise AssertionError("unreachable")

    def _account_reject(
        self, signal: CandidateSignal, card: BattleCard, why: str, error: BaseException | None
    ) -> None:
        """The gate has already been paid for this signal, so the loss is recorded where the
        panel shows rejects instead of vanishing with the task."""
        log.warning("%s %s", signal.symbol, why)
        rec = self.app.recorders.get(Role.RISK_GUARD)
        if rec is None:
            return
        why = why[:300]
        side = "多" if signal.side_hint == Side.LONG else "空"
        rec.record(
            self.app.clock(),
            Outcome.REJECT,
            f"{signal.symbol} {side}：{why}",
            symbol=signal.symbol,
            signal_id=signal.signal_id,
            card_id=card.card_id,
            output={
                "approved": False,
                "reasons": [why],
                "detail": {
                    "by": "account_read",
                    "error": repr(error)[:500] if error is not None else None,
                    "retries": self.app.app_cfg.account_read_retries,
                },
            },
        )

    async def _manual_chain(self, cmd: PanelCommand, path: Path) -> None:
        """后台跑重新检查 / 放行，跑完写回执（文字和同步时一样）。"""
        status: Literal["done", "rejected", "failed"]
        try:
            status, detail = await self._manual_signal(cmd, release=cmd.kind == "release_signal")
        except Exception as e:
            log.exception("面板指令执行失败")
            status, detail = "failed", repr(e)[:200]
        finally:
            self._manual_busy.discard(cmd.params.get("symbol", ""))
            self._inflight.discard(cmd.id)
        now = self.app.clock()
        outcome = Outcome.ACTION if status == "done" else Outcome.REJECT
        self._guard_note(outcome, f"面板指令 {cmd.kind}（{status}）：{detail}"[:200], now)
        try:
            CommandInbox(self.app.app_cfg.data_dir).finish(
                path, CommandResult(id=cmd.id, kind=cmd.kind, status=status, detail=detail, ts=now)
            )
        except OSError:
            log.exception("写面板指令结果失败")

    async def _manual_signal(
        self, cmd: PanelCommand, *, release: bool
    ) -> tuple[Literal["done", "rejected"], str]:
        """面板「风控员拒绝」详情的两个按钮（Aaron 10-25）：按现价重建信号，重新拍板、重新过风控员。

        重新检查 = 干跑，不下单，结果写进风控员记录（``by=recheck``）；放行 = 软规则不拦（见
        ``RiskGuardImpl.review_manual``），过了就按现价开仓，记「人工放行」并推 Telegram。"""
        sid, sym = cmd.params.get("signal_id", ""), cmd.params.get("symbol", "")
        sig = self._signals.get(sid)
        if sig is None or sig.symbol != sym:
            return "rejected", "信号记录已过期（主程序重启过），等下一个信号"
        if not isinstance(self.m.risk, _ManualReview):
            return "rejected", "这个风控员不支持重新检查 / 放行"
        if self._restarting:
            return "rejected", "主程序正在重启"
        if self.app.paused is not None:
            return "rejected", "人工暂停开新仓中，先恢复"
        if sym in self.app.pool_overrides.blacklist:  # 拉黑 = 立即不开新仓（风控员不管这个）
            return "rejected", f"{sym} 你拉黑了，不开新仓"
        now = self.app.clock()
        card = self.m.cards.current(now)
        if card is None or card.for_symbol(sym) is None:
            return "rejected", f"{sym} 现行作战卡已过期或没有这个币，等新卡"
        await self.refresh_positions()
        if any(p.symbol == sym for p in self.positions):
            return "rejected", f"{sym} 已经有持仓"
        snap = self.m.features.snapshot(sym)
        if snap is None:
            return "rejected", f"拿不到 {sym} 的现价"
        new = sig.model_copy(
            update={
                "ts": now,
                "trigger_price": snap.last_price,
                "features": snap,
                "card_id": card.card_id,
                "card_version": card.version,
            }
        )
        label = "人工放行" if release else "重新检查"
        if not release:
            miss = entry_zone_miss(new, card, self.app.app_cfg.fast_loop, now)
            if miss is not None:
                self._manual_note(new, card, now, f"重新检查：现在仍拒绝：{miss}", "recheck", miss)
                return "done", f"现在仍拒绝：{miss}"
        jev = await self.m.gate.evaluate(new, card, now)
        async with self._open_lock:
            now = self.app.clock()
            if (
                self.app.paused is not None
                or self._restarting
                or sym in self.app.pool_overrides.blacklist
            ):
                return "rejected", "拍板期间已暂停、重启或拉黑，没有开仓"
            fresh = self.m.cards.current(now)
            if not self._same_decision_context(card, fresh):
                return "rejected", "拍板期间作战卡或大盘方向换了，请再点一次"
            account = await self.m.executor.account()
            now = self.app.clock()
            fresh = self.m.cards.current(now)
            if not self._same_decision_context(card, fresh):
                return "rejected", "等账户期间作战卡或大盘方向换了，请再点一次"
            assert fresh is not None
            market = self.m.features.snapshot(sym)
            verdict = self.m.risk.review_manual(
                new, jev, fresh, account, now, market=market, release=release
            )
            intent = verdict.intent
            if not verdict.approved or intent is None:
                why = verdict.reasons[0] if verdict.reasons else "未通过"
                if release:
                    return "rejected", f"{label}没成功：{why}"[:200]
                return "done", f"现在仍拒绝：{why}"[:200]
            if not release:
                return (
                    "done",
                    f"重新检查：现在放行（{intent.quantity} 张≈{intent.notional_usdt:.0f}U）",
                )
            try:
                pos = await self.m.executor.open(intent)
            except Exception as e:
                log.exception("人工放行开仓失败")
                await self.app._alert(
                    AlertLevel.WARN,
                    f"{intent.symbol} 人工放行开仓失败",
                    str(e)[:200],
                    "order_failed",
                )
                return "rejected", f"开仓失败：{str(e)[:150]}"
            self.positions = [*self.positions, pos]
            self._card_published()  # 开仓在途期间卡可能更新，立即按当前卡复核。
        side = "多" if intent.side == Side.LONG else "空"
        await self.app._alert(
            AlertLevel.WARN,
            f"人工放行 {sym}",
            f"面板放行：{sym} {side} {intent.quantity} 张≈{intent.notional_usdt:.0f}U，"
            f"止损 {intent.stop_loss_pct:.2f}%",
            "control",
        )
        return (
            "done",
            f"已人工放行开仓：{sym} {side} {intent.quantity} 张≈{intent.notional_usdt:.0f}U",
        )

    def _manual_note(
        self, signal: CandidateSignal, card: BattleCard, now: datetime, summary: str, by: str,
        why: str,
    ) -> None:  # fmt: skip
        """重新检查在进场区间这步就拒绝：风控员自己没跑，按风控员记录的格式补一条（面板详情要看）。"""
        rec = self.app.recorders.get(Role.RISK_GUARD)
        if rec is not None:
            side = "多" if signal.side_hint == Side.LONG else "空"
            rec.record(
                now,
                Outcome.REJECT,
                f"{signal.symbol} {side} {summary}",
                symbol=signal.symbol,
                signal_id=signal.signal_id,
                card_id=card.card_id,
                output={"approved": False, "reasons": [why], "detail": {"by": by}},
            )

    # ------------------------------------------------------------ 盯仓
    async def refresh_positions(self) -> None:
        try:
            all_pos = await self.m.executor.positions()
        except Exception:
            log.exception("读持仓失败，沿用上一次的列表")
            return
        before_status = {p.position_id: p.status for p in self.positions}
        previous = {p.position_id: p for p in self.positions}
        before = set(before_status)
        self.positions = [
            p
            for p in all_pos
            if p.status in (PositionStatus.OPEN, PositionStatus.OPENING, PositionStatus.CLOSING)
        ]
        self._sync_keeper_market()
        if any(
            p.status is PositionStatus.OPEN
            and before_status.get(p.position_id) is PositionStatus.OPENING
            for p in self.positions
        ):
            self._card_published()  # 异步入场由OPENING真正变OPEN时补查。
        ended = before - {p.position_id for p in self.positions}
        if ended:
            self._reconcile_soon()  # 有仓刚结束（止损 / 止盈 / 到时平）：马上核对币安是不是 0
            self._assistant_ended(ended, all_pos, previous)
        live = {p.position_id for p in self.positions}
        for pid in set(self._paper_close_at) - live:
            self._paper_close_at.pop(pid, None)
        for pid in set(self._paper_safe) - live:
            self._paper_safe.pop(pid, None)
        self._paper_unseen_logged = {k for k in self._paper_unseen_logged if k[0] in live}
        self._paper_stop_retry()

    def _assistant_ended(
        self,
        ended: set[str],
        latest: Sequence[PositionRecord],
        previous: Mapping[str, PositionRecord],
    ) -> None:
        """仓位结束：交给管家助手记实际出场（账本里结束后的那条优先，没有就用结束前的）。"""
        by_id = {p.position_id: p for p in latest}
        for pid in ended:
            rec = by_id.get(pid) or previous.get(pid)
            if rec is None:
                continue
            snap = self.m.features.snapshot(rec.symbol)
            self.assistant.on_closed(rec, snap.last_price if snap is not None else None)

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
            self._spawn(self.reconcile_tick(), Role.RECONCILER)
        if self.app.ledger is not None:
            self.app.ledger.poke()  # 出场均价、手续费、今日盈亏马上按币安成交回填

    async def _patrol_close(self, pos: PositionRecord, why: str) -> None:
        """巡逻兵冲击线全平，平完推一条 Telegram。"""
        await self.close(pos, CloseReason.PATROL)
        await self.app._alert(AlertLevel.WARN, "巡逻兵已全平", f"巡逻兵：{why}", "system")

    async def close(
        self,
        pos: PositionRecord,
        reason: CloseReason,
        *,
        source: str | None = None,
        detail: str = "",
    ) -> None:
        """同一笔正在平就不重复提交。重启已定就不再发起（会被退出打断），交给重启后的进程。
        ``source``：谁发起的（记进 ``close_sources.json``），只在平仓真交给执行器、执行器收下了
        才记。"""
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
        executor = self.m.executor
        # 账本确认这一刻没有别的平仓申请在途：执行器说「退出进行中」时，那是这次申请的退出
        first_request = False
        if isinstance(executor, _LedgerPositions):
            try:
                current = next(
                    (p for p in executor.live_positions() if p.position_id == pos.position_id), None
                )
            except Exception:
                log.exception("平仓前无法确认账本状态，保持跟踪等待核实")
                return
            if current is None or current.status is PositionStatus.CLOSING:
                return  # 已结束或已有平仓申请，不能重复发单。
            first_request = True
        self._closing.add(pos.position_id)
        try:
            await self.m.executor.close(pos.position_id, reason)
            self._record_source(pos, source, detail)
        except CloseInProgress as e:
            # The exit is already working and the executor's background tick settles it; the
            # reconciler's closing grace still escalates one that never completes.
            log.warning("%s 平仓进行中（%s）：%s", pos.symbol, reason.value, e)
            if first_request:
                self._record_source(pos, source, detail)
            if not isinstance(executor, _LedgerPositions):
                return
        except Exception as e:
            log.exception("平仓失败")
            await self.app._alert(
                AlertLevel.CRITICAL, f"{pos.symbol} 平仓失败", str(e)[:200], "order_failed"
            )
            return
        finally:
            self._closing.discard(pos.position_id)
        if isinstance(executor, _LedgerPositions):
            try:
                pending = next(
                    (p for p in executor.live_positions() if p.position_id == pos.position_id), None
                )
            except Exception:
                log.exception("平仓返回后无法确认账本状态，继续跟踪")
                pending = pos
            if pending is not None:
                self.positions = [
                    pending if p.position_id == pos.position_id else p for p in self.positions
                ]
                if not any(p.position_id == pos.position_id for p in self.positions):
                    self.positions.append(pending)
                rec = self.app.recorders.get(Role.KEEPER)
                if rec is not None:
                    rec.record(
                        self.app.clock(),
                        Outcome.INFO,
                        f"{pos.symbol} 平仓未确认，等核实",
                        position_id=pos.position_id,
                        symbol=pos.symbol,
                        output={"close_pending": True, "status": pending.status.value},
                    )
                return
        self.positions = [p for p in self.positions if p.position_id != pos.position_id]
        self._keeper_market.pop(pos.position_id, None)
        self._tighten_logged.pop(pos.position_id, None)
        self._tighten_done.pop(pos.position_id, None)
        self._save_keeper_market()
        self._reconcile_soon()
        if isinstance(self.m.keeper, _Forgets):
            self.m.keeper.forget(pos.position_id)
        if isinstance(self.m.patrol, _HasUrgent):
            self.m.patrol.urgent.discard(pos.position_id)
        self.app.against_desk.forget(pos.position_id)
        self._assistant_ended({pos.position_id}, [], {pos.position_id: pos})

    def _restore_keeper_market(self) -> None:
        try:
            raw = json.loads(self._keeper_market_path.read_text(encoding="utf-8"))
            self._keeper_market = {
                pid: KeeperMarketProgress.model_validate(value) for pid, value in raw.items()
            }
        except (OSError, ValueError, TypeError, AttributeError):
            self._keeper_market = {}

    def _save_keeper_market(self) -> None:
        path = self._keeper_market_path
        tmp = path.with_suffix(".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(
                json.dumps(
                    {
                        pid: progress.model_dump(mode="json")
                        for pid, progress in self._keeper_market.items()
                    }
                ),
                encoding="utf-8",
            )
            os.replace(tmp, path)
        except OSError:
            log.exception("持仓市场复核进度保存失败，巡检继续")
            with contextlib.suppress(OSError):
                tmp.unlink()

    def _sync_keeper_market(self, view: MarketView | None = None) -> None:
        if view is None and isinstance(self.m.cards, BattleCardCommander):
            view = self.m.cards.latest_market_view
        open_ids = {p.position_id for p in self.positions}
        self._keeper_market = {
            pid: progress for pid, progress in self._keeper_market.items() if pid in open_ids
        }
        if view is not None:
            for pos in self.positions:
                if pos.status is not PositionStatus.OPEN:
                    continue
                previous = self._keeper_market.get(
                    pos.position_id, KeeperMarketProgress(position_id=pos.position_id)
                )
                if (
                    max(
                        previous.processed_market_version or 0, previous.pending_market_version or 0
                    )
                    < view.version
                ):
                    self._keeper_market[pos.position_id] = previous.model_copy(
                        update={
                            "pending_market_view_id": view.market_view_id,
                            "pending_market_version": view.version,
                        }
                    )
                    rec = self.app.recorders.get(Role.KEEPER)
                    if rec is not None:
                        rec.record(
                            self.app.clock(),
                            Outcome.INFO,
                            f"{pos.symbol} 等待按大盘 v{view.version} 复核持仓",
                            position_id=pos.position_id,
                            symbol=pos.symbol,
                            output={
                                "market_review": "pending",
                                "market_view_id": view.market_view_id,
                                "market_version": view.version,
                            },
                        )
        self._save_keeper_market()

    def _card_published(self) -> None:
        """不带旧卡快照，不排Jev队列；实际运行时读取最新卡与账本。"""

        async def check() -> None:
            await self._review_card_rules()

        if not self._leaving:
            self._spawn(check())

    async def _review_card_rules(self) -> set[str]:
        keeper = self.m.keeper
        if not isinstance(keeper, _CardRuleKeeper):
            return set()
        attempted: set[str] = set()

        async def one(pid: str) -> None:
            try:
                # 真正开始时再读账本/最新卡，排队期间换卡不能用旧结论。
                pos = next((p for p in self._ledger_positions() if p.position_id == pid), None)
                if pos is None or pos.status is not PositionStatus.OPEN or pid in self._closing:
                    return
                verdict = keeper.review_card_rule(pos, self.app.clock())
                if verdict is not None and verdict.action == "close":
                    await self.close(pos, CloseReason.KEEPER)
                elif verdict is not None and close_blocked(verdict):
                    # 平仓前检查拦下的平仓：收紧本身可能换代或平仓，也不做
                    return
                elif verdict is not None:  # 反向卡价格未确认（#314）/ 卡转中性：收紧，不行就平
                    spec_of = getattr(keeper, "card_rule_tighten", None)
                    if callable(spec_of):
                        spec = spec_of(pos, self.app.clock())
                        if spec is not None:  # None：中性卡 close 遇到旧行情，这轮先拿住
                            await self._opposite_tighten(
                                pos,
                                verdict.reason,
                                spec["key"],
                                neutral=spec["kind"] == "neutral_card",
                                allow_close=spec["allow_close"],
                            )
                    else:
                        key_of = getattr(keeper, "card_rule_key", None)
                        key = key_of(pos, self.app.clock()) if callable(key_of) else None
                        await self._opposite_tighten(pos, verdict.reason, key)
            finally:
                self._card_rule_queued.discard(pid)

        def closes_now(pos: PositionRecord) -> bool:
            # 走「价格确认或收紧」的仓模型照常复查；立刻全平的不再排模型复查。
            watch = getattr(keeper, "card_rule_watch", None)
            return not (callable(watch) and watch(pos, self.app.clock()))

        # 只作无副作用预检并登记后台任务，不等平仓返回，其他持仓立即进入正常管家复核。
        for pos in self._ledger_positions():
            pid = pos.position_id
            if pid in self._card_rule_queued:
                if keeper.card_rule_applies(pos, self.app.clock()) and closes_now(pos):
                    attempted.add(pid)
                continue
            if pos.status is not PositionStatus.OPEN or pid in self._closing:
                continue
            if keeper.card_rule_applies(pos, self.app.clock()):
                if closes_now(pos):
                    attempted.add(pid)
                self._card_rule_queued.add(pid)
                self._spawn(one(pid))
        return attempted

    async def _opposite_tighten(
        self,
        pos: PositionRecord,
        reason: str,
        key: str | None = None,
        *,
        neutral: bool = False,
        allow_close: bool = True,
    ) -> None:
        """反向卡价格未确认：让执行器收紧托管止损（trail：现价强制启动移动止损；breakeven：
        挪到扣费保本）；执行器说收紧不了（旧通道、从没完整托管 / 开仓宽限期、证据读不到、
        亏着或赚得不够）就照原规则全平。

        ``neutral``：作战卡转中性，同一条路，固定 trail，距离倍数用
        ``card_neutral_trail_distance_mult``。``allow_close = False``：只收紧，这次不平。

        同一条反向指令（``key``）已收紧 / 已够紧就记在内存里，之后每轮不再进执行器的锁；
        结果换了才记一条管家决策记录。"""
        if key is not None and self._tighten_done.get(pos.position_id) == key:
            return
        executor = self.m.executor
        result: dict[str, Any]
        if isinstance(executor, _TightensStop):
            fl = self.app.app_cfg.fast_loop
            try:
                result = await executor.tighten_stop(
                    pos.position_id,
                    reason=reason,
                    buffer_bps=fl.opposite_tighten_buffer_bps,
                    mark_gap_bps=fl.opposite_tighten_mark_gap_bps,
                    profit_mode="trail" if neutral else fl.opposite_profit_mode,
                    distance_mult=fl.card_neutral_trail_distance_mult
                    if neutral
                    else fl.opposite_trail_distance_mult,
                    card_key=key,
                    anchor_retry_s=fl.opposite_anchor_retry_s,
                    allow_close=allow_close,
                    label="作战卡转中性" if neutral else "反向卡",
                )
            except Exception as e:
                log.exception("%s 收紧止损出错，按原规则平", pos.symbol)
                result = {"action": "close", "why": "tighten_error", "error": str(e)[:200]}
        else:
            result = {"action": "close", "why": "executor_cannot_tighten"}
        if result.get("action") == "close" and not allow_close:
            result = {**result, "action": "hold", "why": f"no_close_{result.get('why')}"}
        action, why = str(result.get("action")), str(result.get("why"))
        if key is not None and action in ("tightened", "kept"):
            self._tighten_done[pos.position_id] = key
        kind = "neutral_card" if neutral else "opposite_card"
        if self._tighten_logged.get(pos.position_id) != (kind, action, why):
            self._tighten_logged[pos.position_id] = (kind, action, why)
            rec = self.app.recorders.get(Role.KEEPER)
            if rec is not None:
                what = "作战卡转中性" if neutral else "反向卡未确认"
                text = {
                    "tightened": f"{what}，托管止损已收紧",
                    "pending": f"{what}，收紧价已记下，等交易所确认换单",
                    "exiting": "收紧时交易所说已越过触发价，保护进入退出",
                    "kept": f"{what}，止损已经够紧，不再动",
                    "skip": f"{what}，仓已在平，不收紧",
                    "close": f"{what}，亏着或收紧不了，全平"
                    if neutral
                    else "反向卡未确认但收紧不了，按原规则全平",
                    "hold": f"{what}，行情没有或过旧：收紧不了这次也不平",
                }.get(action, f"{what}，收紧结果未知")
                rec.record(
                    self.app.clock(),
                    Outcome.CLOSE if action == "close" else Outcome.ACTION,
                    f"{pos.symbol} {text}（{why}）",
                    symbol=pos.symbol,
                    signal_id=pos.signal_id,
                    position_id=pos.position_id,
                    output={
                        "opposite_tighten": result,
                        "reason": reason,
                        "instruction_key": key,
                        **({"card_rule_kind": "neutral_card"} if neutral else {}),
                        "counterfactual": {
                            "estimated_target": result.get("target"),
                            "placeable": result.get("placeable"),
                        },
                    },
                )
        if action == "close":
            await self.close(pos, CloseReason.KEEPER)

    def _market_published(self, view: MarketView) -> None:
        """只唤醒原管家通道；同仓在途不并行重问，旧回复仅确认实际使用的版本。"""
        self._sync_keeper_market(view)
        held = [
            p
            for p in self.positions
            if p.status is PositionStatus.OPEN
            and p.position_id in self._keeper_market
            and self._keeper_market[p.position_id].pending_market_view_id is not None
        ]
        if held and not self._leaving:
            self._spawn(self._review_all(held), Role.KEEPER)

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
        if not any(
            p.position_id == pid and p.status is PositionStatus.OPEN for p in self.positions
        ):
            return  # 模型在途时硬规则可能已平/正在平，旧回复不能再次下单。
        if pid in self._keeper_market:
            acknowledged = False
            progress = self._keeper_market[pid]
            if (
                verdict.review_succeeded
                and verdict.source_market_view_id is not None
                and verdict.source_market_version is not None
            ):
                acknowledged = True
                updates: dict[str, Any] = {}
                if verdict.source_market_version >= (progress.processed_market_version or 0):
                    updates.update(
                        processed_market_view_id=verdict.source_market_view_id,
                        processed_market_version=verdict.source_market_version,
                    )
                if (
                    verdict.source_market_view_id == progress.pending_market_view_id
                    or verdict.source_market_version > (progress.pending_market_version or 0)
                ):
                    updates.update(pending_market_view_id=None, pending_market_version=None)
                self._keeper_market[pid] = progress.model_copy(update=updates)
            self._save_keeper_market()
            rec = self.app.recorders.get(Role.KEEPER)
            if rec is not None:
                rec.record(
                    self.app.clock(),
                    Outcome.INFO,
                    f"{pos.symbol} 持仓市场复核" + ("完成" if acknowledged else "未完成，留待巡检"),
                    position_id=pid,
                    symbol=pos.symbol,
                    output={
                        "market_review": "processed" if acknowledged else "retry",
                        "source_market_view_id": verdict.source_market_view_id,
                        "source_market_version": verdict.source_market_version,
                        "progress": self._keeper_market[pid].model_dump(mode="json"),
                    },
                )
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

        async def one(pos: PositionRecord) -> None:
            pid = pos.position_id
            if pid in self._reviewing or pid in self._keeper_queued:
                return
            self._keeper_queued.add(pid)
            try:
                async with self._keeper_slots:
                    if pid not in self._closing and any(
                        current.position_id == pid and current.status is PositionStatus.OPEN
                        for current in self.positions
                    ):
                        await self._review(pos)
            finally:
                self._keeper_queued.discard(pid)

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
        attempted = await self._review_card_rules()  # 新卡事件漏唤醒 / 上次失败的例行兜底。
        held = [
            p
            for p in self.positions
            if p.status is PositionStatus.OPEN and p.position_id not in attempted
        ]
        if self._frozen_peak_background():
            # 一笔仓慢（交易所证据、Jev 重试）不推迟别的仓下一轮：不等这一轮问完；
            # 还在问的仓下一轮由 _review_all 跳过，不会重叠。
            self._spawn(self._review_all(held), Role.KEEPER)
        else:
            await self._review_all(held)
        if keeper is not None and not self.positions:
            keeper.note(self.app.clock(), "看了一眼：没有持仓，不用审")
        # 开平仓、卡过期都在这里跟上订阅；放在审查之后，回填新币的几秒不推迟审持仓
        await self.sync_watch()

    def _frozen_peak_background(self) -> bool:
        keeper = self.m.keeper
        return isinstance(keeper, _FrozenPeakCache) and keeper.frozen_peak_background()

    async def frozen_peak_tick(self) -> None:
        """后台模式：每笔持仓按自己的间隔读一次冻结盈利价证据，各读各的，不进复查关键路径。"""
        keeper = self.m.keeper
        if not isinstance(keeper, _FrozenPeakCache) or not keeper.frozen_peak_background():
            return
        now = self.app.clock()
        for pos in self.positions:
            pid = pos.position_id
            if (
                pos.status is PositionStatus.OPEN
                and pid not in self._closing
                and keeper.frozen_peak_due(pid, now)
            ):
                self._spawn(self._refresh_frozen_peak(keeper, pid, now))

    async def _refresh_frozen_peak(self, keeper: _FrozenPeakCache, pid: str, now: datetime) -> None:
        """后台读到冻结盈利价已触发：马上复查这笔仓，不等下一轮例行复查。正在问的仓交给急查，
        下一秒再审（_review 本身也是这样处理在途的）。"""
        if not await keeper.refresh_frozen_peak(pid, now):
            return
        pos = next(
            (p for p in self.positions if p.position_id == pid and p.status is PositionStatus.OPEN),
            None,
        )
        if pos is None or pid in self._closing:
            return
        if pid in self._keeper_queued and isinstance(self.m.patrol, _HasUrgent):
            self.m.patrol.urgent.add(pid)
            return
        await self._review(pos, urgent=True)

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

    def _local_price_at(self, symbol: str, t: datetime) -> float | None:
        read = getattr(self.m.features, "price_at", None)
        hit = read(symbol, t) if callable(read) else None
        return float(hit[0]) if hit is not None else None

    async def day_start_tick(self) -> None:
        executor = self.m.executor
        if not isinstance(executor, _DayStartAware) or not executor.day_start_due(self.app.clock()):
            return
        try:
            await executor.account()
        except Exception as e:
            # hummingbot-api down around midnight would otherwise dump a stack trace every minute
            if self._day_start_failing:
                log.warning("0 点定日亏基准读账户又失败，下分钟再试：%r", e)
            else:
                log.exception("0 点定日亏基准读账户失败，下分钟再试")
            self._day_start_failing = True
        else:
            self._day_start_failing = False

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

    def _regime_enabled(self) -> bool:
        cfg = self.app.app_cfg
        return (
            cfg.fast_loop.regime_flip_on
            and cfg.fast_loop.regime_flip_check_min > 0
            and (cfg.mode == "paper" or cfg.fast_loop.regime_flip_live)
        )

    def _regime_btc_price(self) -> float | None:
        if not self._regime_enabled():
            return None
        f = self.m.features.snapshot(BTC)
        max_age = self.app.app_cfg.fast_loop.regime_flip_price_max_age_min
        if f is None:
            return None
        age = (self.app.clock() - f.ts).total_seconds() / 60
        if age < 0 or (max_age > 0 and age > max_age):
            return None
        return f.last_price

    async def _regime_periodic_tick(self) -> None:
        now = self.app.clock()
        period = self.app.app_cfg.fast_loop.regime_flip_check_min
        if not self._regime_enabled():
            self._regime.reset()
            if self._market_task is not None and not self._market_task.done():
                self._market_task.cancel()
            self._regime_next = None
            self._regime_period = period
            return
        if self._regime_period != period or self._regime_next is None:
            self._regime_period = period
            self._regime_next = now + timedelta(minutes=period)
        if now >= self._regime_next:
            self._regime_next = now + timedelta(minutes=period)
            await self.regime_flip_tick()

    def _flip_row(self, now: datetime, sample: Breadth | None = None) -> dict[str, Any]:
        ref = self._regime.reference
        return {
            "ts": now.isoformat(),
            "checked_at": now.isoformat(),
            "kind": "blocked",
            "blocked_reason": "",
            "regime_text": ref.regime_text if ref else "",
            "regime_dir": ref.direction if ref else None,
            "reference_at": ref.ts.isoformat() if ref else None,
            "reference_btc_px": ref.btc_px if ref else None,
            "regime_age_min": (now - ref.ts).total_seconds() / 60 if ref else None,
            "thresholds": self._regime.thresholds(),
            "flat_1h": sample.flat if sample else None,
            "missing_1h": sample.missing if sample else None,
            "requested_symbols": sample.requested if sample else None,
            "window_start": (sample.ts - timedelta(hours=1)).isoformat() if sample else None,
            "window_end": sample.ts.isoformat() if sample else None,
            "confirm_count_before": self._regime.count,
            "last_rerun_at": self._regime.last_rerun.isoformat()
            if self._regime.last_rerun
            else None,
            "up_1h": sample.up if sample else 0,
            "down_1h": sample.down if sample else 0,
            "n": sample.n if sample else 0,
            "median_chg_1h_pct": sample.median_pct if sample else None,
            "btc_px": sample.btc_px if sample else None,
            "btc_ret_since_round_pct": None,
            "sample_at": sample.ts.isoformat() if sample else None,
            "prices": sample.prices if sample else {},
            "rerun": False,
            "rerun_card_id": None,
            "new_regime_text": None,
            "regime_changed": None,
        }

    def _flip_blocked(self, row: dict[str, Any], reason: str) -> None:
        row.update(kind="blocked", blocked_reason=reason)  # 旁路最终检查结果；不改变判定。
        self._regime_journal.emit(
            {
                **row,
                "ts": self.app.clock().isoformat(),
                "kind": "blocked",
                "blocked_reason": reason,
                "rerun": False,
            },
            f"大盘翻转被拦：{reason}",
        )

    async def regime_flip_tick(self) -> None:
        if not self._regime_enabled():
            self._regime.reset()
            self._regime_journal.quiet()
            return
        cards = self.m.cards
        if not isinstance(cards, BattleCardCommander) or self._regime_tick_lock.locked():
            return
        async with self._regime_tick_lock:
            now = self.app.clock()
            cards.ensure_regime_reference()
            self._regime.arm(cards.regime_reference)
            self._regime.last_rerun = cards.last_regime_flip_at
            row = self._flip_row(now)
            ref = self._regime.reference
            try:
                pool = self.app.pool
                if self._restarting:
                    self._regime.reset()
                    self._flip_blocked(row, "正在重启")
                elif ref is None:
                    check = self._regime.step(now, 0, 0, 0, None)
                    self._flip_blocked(row, check.reason)
                elif pool is None:
                    self._regime.reset()
                    self._flip_blocked(row, "数据不足：没有候选池")
                elif binance_gate.banned_until():
                    self._regime.reset()
                    self._flip_blocked(row, "限流")
                else:
                    sample = await regime_snapshot(
                        [e.symbol for e in pool.in_pool], now, self._fetch_regime
                    )
                    row = self._flip_row(now, sample)
                    self._feed_desk_intra(sample, pool)
                    if not self._regime_enabled():
                        self._regime.reset()
                        return
                    if cards.regime_reference != ref:
                        self._regime.arm(cards.regime_reference)
                        self._flip_blocked(row, "取行情期间总盘会参照已更新")
                    else:
                        check = self._regime.step(
                            now, sample.up, sample.down, sample.n, sample.btc_px
                        )
                        row.update(
                            kind=check.kind,
                            blocked_reason=check.reason,
                            btc_ret_since_round_pct=check.btc_ret,
                            confirm_count=check.count,
                            candidate_direction=check.candidate_direction,
                            baseline_at=check.baseline_at.isoformat()
                            if check.baseline_at
                            else None,
                            baseline_btc_px=check.baseline_btc_px,
                        )
                        if check.kind == "confirmed":
                            await self.slow_tick(flip=row)
                        elif check.kind == "blocked":
                            self._flip_blocked(row, check.reason)
                        elif check.kind == "quiet":
                            self._regime_journal.quiet()
                        else:
                            which = "池面" if check.kind == "suspect_breadth" else "BTC"
                            self._regime_journal.emit(
                                row,
                                f"大盘翻转疑似：只有{which}满足；"
                                f"{sample.up} 涨/{sample.down} 跌，BTC {check.btc_ret:+.2f}%",
                            )
            except RateLimited:
                self._regime.reset()
                self._flip_blocked(row, "限流")
            except Exception:
                self._regime.reset()
                log.exception("大盘翻转本轮检查失败")
                self._flip_blocked(row, "数据不足：检查失败")
            finally:
                try:
                    row["check_completed_at"] = self.app.clock().isoformat()
                    row["confirm_count_after"] = self._regime.count
                    row["market_view_id"] = (
                        cards.latest_market_view.market_view_id
                        if cards.latest_market_view
                        else None
                    )
                    self._regime_journal.audit_check(check_diagnostics(row))
                except Exception:
                    log.warning("翻转完整检查轨迹不可用，原判断不变", exc_info=True)
            # 回填失败不改变本轮判断或连续确认次数。
            if self._regime_enabled() and not binance_gate.banned_until():
                try:
                    await self._regime_journal.backfill(self.app.clock(), self._fetch_regime)
                except Exception:
                    log.warning("大盘翻转事后收益回填失败，留待下次", exc_info=True)

    async def slow_tick(self, *, flip: dict[str, Any] | None = None) -> None:
        if flip is not None:
            self._start_market_refresh(flip)
            return
        async with self._slow_lock:
            await self._slow_tick()

    def _start_market_refresh(self, flip: dict[str, Any]) -> None:
        cards = self.m.cards
        pool = self.app.pool
        if not isinstance(cards, BattleCardCommander) or pool is None:
            self._flip_blocked(flip, "数据不足：没有候选池")
            return
        target = self.app.app_cfg.fast_loop.regime_flip_target
        if target != "flash_market_only":
            self._flip_to_target(cards, flip, target)
            return
        why = cards.regime_flip_blocked(self.app.clock())
        ref = cards.regime_reference
        if not self._regime_enabled():
            why = "设置已关闭"
        elif self._restarting:
            why = "正在重启"
        elif self._market_refreshing:
            why = "大盘独立更新中"
        elif ref is None or ref.ts.isoformat() != flip["reference_at"]:
            why = "市场参照已更新"
        elif self._regime.thresholds() != flip["thresholds"]:
            why = "阈值已更新，下一轮按新值重新确认"
        if why:
            self._flip_blocked(flip, why)
            return
        cards.mark_regime_flip(self.app.clock())
        self._regime.last_rerun = cards.last_regime_flip_at
        self._regime.fired(flip["candidate_direction"])
        flip = self._regime_journal.emit(
            {**flip, "ts": self.app.clock().isoformat(), "rerun": True, "scope": "market_only"},
            f"大盘方向变化：{flip['up_1h']} 涨/{flip['down_1h']} 跌，"
            f"BTC {flip['btc_ret_since_round_pct']:+.2f}%"
            f"（连续 {flip['confirm_count']} 次）→ 独立更新大盘",
        )
        self._market_refreshing = True
        task = asyncio.create_task(self._refresh_market(cards, pool, flip))
        self._market_task = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(self._market_finished)

    def _flip_to_target(
        self, cards: BattleCardCommander, flip: dict[str, Any], target: str
    ) -> None:
        """``regime_flip_target`` 不是 Flash 时：off = 照常检查和落盘，只是不更新大盘；
        desk = 叫大盘台立即加跑一轮（大盘台自己按 min_run_gap_min 限间隔，结果走它的滞回）。"""
        desk = getattr(self.app, "desk", None)
        ref = cards.regime_reference
        why = ""
        if target == "off":
            why = "regime_flip_target = off：只记录，不更新大盘"
        elif desk is None:
            why = "regime_flip_target = desk，但大盘台没开（slow.toml [market_desk] mode = off）"
        elif not self._regime_enabled():
            why = "设置已关闭"
        elif self._restarting:
            why = "正在重启"
        elif binance_gate.banned_until():
            why = "限流"
        elif ref is None or ref.ts.isoformat() != flip["reference_at"]:
            why = "市场参照已更新"
        elif self._regime.thresholds() != flip["thresholds"]:
            why = "阈值已更新，下一轮按新值重新确认"
        if why or desk is None:
            self._flip_blocked(flip, why)
            return
        cards.mark_regime_flip(self.app.clock())
        self._regime.last_rerun = cards.last_regime_flip_at
        self._regime.fired(flip["candidate_direction"])
        self._regime_journal.emit(
            {**flip, "ts": self.app.clock().isoformat(), "rerun": True, "scope": "market_desk"},
            f"大盘方向变化：{flip['up_1h']} 涨/{flip['down_1h']} 跌，"
            f"BTC {flip['btc_ret_since_round_pct']:+.2f}%"
            f"（连续 {flip['confirm_count']} 次）→ 大盘台加跑",
        )
        desk.request_run("regime_flip")
        self._start_desk_if_due()

    async def desk_tick(self) -> None:
        """大盘台：转发提示、喂价给急动检测、到点（定时格子 / 加跑请求）就开跑。"""
        desk = getattr(self.app, "desk", None)
        if desk is None:
            return
        check_owner = getattr(desk, "check_owner", None)
        if callable(check_owner):
            check_owner(self.app.clock())  # 不可用就按边沿切回 Flash（记录、提醒一次）
        while (notice := desk.take_notice()) is not None:
            await self.app._alert(AlertLevel.WARN, "大盘台", notice, "system")
        pool = self.app.pool
        if pool is not None:
            btc = self.m.features.snapshot(BTC)
            desk.observe_prices(
                self.app.clock(), btc.last_price if btc is not None else None, self._prices(pool)
            )
        self._start_desk_if_due()

    def _feed_desk_intra(self, sample: Breadth, pool: SymbolPool) -> None:
        """大盘翻转检查刚取到的 1 分钟线顺手给大盘台刷新「短线现状」（不另发请求）；
        出错不影响翻转判断。"""
        desk = getattr(self.app, "desk", None)
        observe = getattr(desk, "observe_breadth", None)
        if not callable(observe):
            return
        try:
            observe(sample.ts, [e.symbol for e in pool.in_pool], sample.rets_15m, sample.rets_1h)
        except Exception:
            log.exception("大盘台刷新短线现状失败")

    def _start_desk_if_due(self) -> None:
        desk = getattr(self.app, "desk", None)
        if desk is None or self._restarting:
            return
        if self._desk_task is not None and not self._desk_task.done():
            return
        reason = desk.due(self.app.clock())
        if reason is None:
            return
        task = asyncio.create_task(self._desk_run(desk, reason))
        self._desk_task = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _desk_run(self, desk: Any, reason: str) -> None:
        try:
            await desk.run(self.app.pool, reason)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("大盘台这一轮出错")

    def _market_finished(self, task: asyncio.Task[None]) -> None:
        if self._market_task is task:
            self._market_task = None
            self._market_refreshing = False

    async def _refresh_market(
        self, cards: BattleCardCommander, pool: SymbolPool, flip: dict[str, Any]
    ) -> None:
        view = None
        error = ""
        try:
            view = await cards.refresh_market(
                pool,
                valid=lambda: (
                    self._regime_enabled()
                    and not self._restarting
                    and self._regime.thresholds() == flip["thresholds"]
                    and cards.regime_reference is not None
                    and cards.regime_reference.ts.isoformat() == flip["reference_at"]
                ),
            )
        except asyncio.CancelledError:
            error = "市场更新已取消，保留上一份观点"
            raise
        except Exception as exc:
            error = str(exc) or type(exc).__name__
            log.exception("大盘独立更新失败，保留上一份观点")
        finally:
            self._market_refreshing = False
            self._market_task = None
            direction = view.regime_direction if view else None
            superseded = cards.market_refresh_discarded
            changed = direction != flip["regime_dir"] if view and not superseded else None
            result = (
                "flip_failed"
                if view is None
                else "flip_superseded"
                if cards.market_refresh_discarded
                else "flip_changed"
                if changed
                else "flip_no_change"
            )
            self._regime_journal.emit(
                {
                    **flip,
                    "kind": "rerun_result",
                    "ts": self.app.clock().isoformat(),
                    "origin_ts": flip["ts"],
                    "scope": "market_only",
                    "market_view_id": view.market_view_id if view else None,
                    "new_regime_text": view.regime if view else None,
                    "new_regime_dir": direction,
                    "regime_changed": changed,
                    "result": result,
                    "error": error,
                    "cost_usd": max(cards.market_refresh_cost_usd, 0.0),
                    "cost_estimated": True,
                },
                f"独立大盘更新：{result}，"
                + (
                    "较新任务已发布，此结果未采用"
                    if superseded
                    else f"{flip['regime_dir']} → {direction}"
                    if view
                    else error[:120]
                ),
            )

    async def _slow_tick(self) -> None:
        now = self.app.clock()
        if self._last_screen is None or now - self._last_screen >= timedelta(
            minutes=self.app.app_cfg.screener_interval_min
        ):
            await self._screen_now(now)
        pool = self.app.pool
        if pool is None:
            return
        cards = self.m.cards
        events = cards if isinstance(cards, _EventMeetings) else None
        limit = max(1, int(getattr(cards, "event_concurrency", 1))) if events else 1
        if limit == 1 and (self._refreshing or self._event_running):
            return  # 一场一场开：整轮或临时会在开，什么都不派
        if self._refreshing:
            # 整轮在开：本轮开哪些币定了以后，只给本轮不开的币派临时会（commander 挡掉本轮的币），
            # 和整轮共用并发名额；还在选币 / 挑币时不派
            if (
                events is not None
                and getattr(cards, "round_symbols_known", False) is True
                and await self._may_meet(cards, now)
                and not is_stub(cards)
            ):
                self._dispatch_events(events, pool, now, limit)
            return
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
        if not await self._may_meet(cards, now):
            return  # 失败退避中或当日花费超限；定时兜底也不能绕过
        if is_stub(cards):
            return
        if reason is None:
            if events is not None:
                self._dispatch_events(events, pool, now, limit)
            return
        if self._event_running:
            return  # 整轮要开：不再派临时会，等在开的开完（和一场一场开时一样等）
        # 顺序 选币 → 总盘会：每一整轮会议前先把候选池刷新（刚选过就不重复）
        self._refreshing = True
        try:
            if self._last_screen is None or now - self._last_screen >= SCREEN_FRESH:
                await self._screen_now(now)
                pool = self.app.pool or pool
            self._last_card_at = now
            self._spawn(self._refresh(pool, reason))
        except BaseException:
            self._refreshing = False
            raise

    async def _may_meet(self, cards: object, now: datetime) -> bool:
        if not isinstance(cards, _RefreshGate):
            return True
        while (notice := cards.take_notice()) is not None:
            await self.app._alert(AlertLevel.WARN, "军师团暂停开会", notice, "meeting_paused")
        allowed = cards.may_refresh(now)
        if isinstance(cards, _SpendAlerts):  # 日上限设为不限：花费过线只提醒，单独一类、单独标题
            while (alert := cards.take_spend_alert()) is not None:
                await self.app._alert(AlertLevel.WARN, SPEND_ALERT_TITLE, alert, "spend_alert")
        return allowed

    def _dispatch_events(
        self, events: _EventMeetings, pool: SymbolPool, now: datetime, limit: int
    ) -> None:
        """派临时会，直到同时在开的到 ``limit`` 场。取下一场和登记在开之间没有 await：
        commander 的 ``claim_event`` 同步预留币、当天场数和预估花费，同一刻派的几场不会撞。"""
        claim = getattr(events, "claim_event", None)
        while len(self._event_running) < limit:
            event = claim(now, pool) if callable(claim) else events.pop_event(now, pool)
            if event is None or event[0] in self._event_running:
                return
            self._event_running.add(event[0])
            self._spawn(self._refresh_event(events, pool, *event))
            if not callable(claim):
                return  # 不会预留的实现：一次只派一场

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
            # 挑中的币已经在新卡上（开会失败就不再盯它们）
            self._meeting_picks = set()
            self.m.predictor.set_priority(set())
            try:
                await self.sync_watch()
            except Exception:
                log.exception("开完会重订行情失败，下一轮持仓管家再订")
            finally:
                self._refreshing = False
        return card, err

    async def _refresh_chain(
        self, cmd: PanelCommand, path: Path | None, *, wait_events: bool = False
    ) -> None:
        """面板「重新开会」：整条链真跑一遍——按同一套筛选重新选币（写 symbol_pool.json）→ 总盘会 →
        每个币完整分析会 → 新卡；不沿用上一轮结论。跑完才写回执：选了几个、上会几个、用时、花费。"""
        t0 = time.monotonic()
        started = self.app.clock()
        status: Literal["done", "failed"] = "failed"
        detail = ""
        try:
            if wait_events:
                await self._wait_events_done()
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

    async def _wait_events_done(self) -> None:
        """排队的整轮重开：等在开的临时会开完，最多等一场会的超时；超时了照样开。"""
        timeout = float(getattr(self.m.cards, "symbol_timeout_s", 900.0))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while self._event_running and loop.time() < deadline:
            await asyncio.sleep(0.5)
        if self._event_running:
            log.warning("等临时会开完超过 %.0f 秒，照样开始重新开会", timeout)

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
            self._event_running.discard(symbol)

    async def _rescreen(self, now: datetime) -> None:
        self.app.reload_pool_overrides()  # 手改 data/pool_overrides.json 也在下一次选币生效
        try:
            pool = await self.m.screener.screen(now)
        except Exception:
            log.exception("选币失败，沿用上一份候选池")
            self.app.sync_untradeable(now)
            return
        self.app.set_pool(pool)
        self.app.sync_untradeable(now)

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
            elif cmd.kind in ("recheck_signal", "release_signal"):
                # 要等拍板人，放后台跑，别卡住后面的暂停 / 平仓指令；回执跑完才写
                sym = cmd.params.get("symbol", "")
                if sym in self._manual_busy:
                    result = CommandResult(
                        id=cmd.id,
                        kind=cmd.kind,
                        status="rejected",
                        detail=f"{sym} 上一次重新检查 / 放行还没跑完，稍后再点",
                        ts=now,
                    )
                else:
                    self._manual_busy.add(sym)
                    self._inflight.add(cmd.id)
                    self._spawn(self._manual_chain(cmd, path))
                    continue
            else:
                status: Literal["done", "queued", "rejected", "failed"]
                self._cmd_path = path
                try:
                    status, detail = await self._run_command(cmd)
                except Exception as e:
                    log.exception("面板指令执行失败")
                    status, detail = "failed", repr(e)[:200]
                result = CommandResult(
                    id=cmd.id, kind=cmd.kind, status=status, detail=detail, ts=now
                )
                outcome = Outcome.ACTION if status in ("done", "queued") else Outcome.REJECT
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

    async def _run_command(
        self, cmd: PanelCommand
    ) -> tuple[Literal["done", "queued", "rejected"], str]:
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
                targets = await self._close_targets()
            for p in targets:
                self._spawn(self.close(p, CloseReason.MANUAL), Role.EXECUTOR)
            await self.app._alert(
                AlertLevel.WARN, "面板全部平仓", "已同时暂停开新仓，恢复需另发指令", "control"
            )
            return "done", f"已暂停开新仓并提交平仓 {len(targets)} 笔"
        if cmd.kind == "close_position":
            pid = cmd.params.get("position_id", "")
            pos = next((p for p in await self._close_targets() if p.position_id == pid), None)
            if pos is None:
                return "rejected", f"没有持仓 {pid}"
            self._spawn(self.close(pos, CloseReason.MANUAL), Role.EXECUTOR)
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
        queued = len(self._event_running)
        # 先占住整轮：慢环不再派新的临时会，在开的开完后链条才开始
        self._refreshing = True
        now = self.app.clock()
        self._last_card_at = self._last_screen = now
        self._inflight.add(cmd.id)
        if queued:
            self._spawn(self._refresh_chain(cmd, None, wait_events=True))
            return "queued", f"等 {queued} 场临时会开完后开始：选币 → 总盘会 → 军师团"
        self._spawn(self._refresh_chain(cmd, self._cmd_path))
        return "done", "已开始：选币 → 总盘会 → 军师团"
