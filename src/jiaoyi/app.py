"""组装层（composition root）：把各角色的实现接起来，管启动、关闭和模块状态汇总。

- 唯一允许同时导入所有角色包（包括 ``jiaoyi.execution``）的地方。
- 每个角色按 ``AppConfig`` 选真实现或桩；还没合并的角色一律用桩（故障即关闭）。
- 这里不写交易逻辑；主循环（事件分发、定时任务）在 ``jiaoyi.loop``。

启动顺序（先能保命，再能看，最后才可能开仓）::

    决策日志 → 对账员先跑一遍 → 选币 → 订阅行情 → 各模块上报一次状态

角色需要自己的启动步骤时按这个原则插进去：执行层和对账在前，产生信号的在最后。
关闭顺序相反：先断行情，不再产生新信号；执行层最后停，已有持仓仍有币安上的保命止损。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, fields, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import ValidationError

from jiaoyi.alerting.log import LogAlerter
from jiaoyi.alerting.recorded import RecordedAlerter
from jiaoyi.alerting.stub import StubAlerter
from jiaoyi.alerting.telegram import TelegramAlerter
from jiaoyi.contracts import (
    Alert,
    AlertLevel,
    AppConfig,
    Bias,
    DecisionRecord,
    Health,
    Interval,
    ModuleStatus,
    Outcome,
    RiskConfig,
    Role,
    RunMode,
    SymbolPool,
)
from jiaoyi.core import profiles
from jiaoyi.core.against import MEETING_TRIGGER, AgainstSignalDesk
from jiaoyi.core.commands import read_paused, update_runtime, write_paused, write_runtime
from jiaoyi.core.config import ConfigError, load_risk_config, read_toml, with_profile
from jiaoyi.core.hotconfig import (
    HOT_APP,
    HOT_FAST,
    HOT_RISK,
    PROFILE_KEYS,
    ConfigFiles,
    describe,
    patch_inplace,
    restart_fingerprint,
)
from jiaoyi.core.observations import KeeperObservationCollector
from jiaoyi.core.pool_overrides import (
    POOL_OVERRIDES_FILE,
    POOL_OVERRIDES_PAUSE,
    PoolAction,
    PoolOverrides,
    backup_corrupt_pool_overrides,
    parse_category_command,
    read_pool_overrides,
    write_pool_overrides,
)
from jiaoyi.core.recorder import RoleRecorder
from jiaoyi.core.secrets import ChainedSecretStore
from jiaoyi.core.stop_quotes import StopQuoteRecorder
from jiaoyi.core.store import JsonlDecisionLog
from jiaoyi.execution.account_check import AccountMismatch
from jiaoyi.execution.factory import MissingSecret, build_execution
from jiaoyi.execution.income_daily import IncomeDaily
from jiaoyi.execution.ledger_sync import LedgerSync
from jiaoyi.execution.live_gate import LiveGate
from jiaoyi.execution.stub import (
    StubExecutorAdapter,
    StubLifeboatStop,
    StubLotRules,
    StubReconciler,
)
from jiaoyi.execution.user_stream import (
    DEMO_WS_BASE,
    LEGACY_TESTNET_WS_BASE,
    LIVE_WS_BASE,
    UserStream,
)
from jiaoyi.gate.context import EntryContextSource
from jiaoyi.gate.entry import JevEntryGate
from jiaoyi.gate.jev import JevDecisionModel, decision_model_from_config
from jiaoyi.gate.rule_model import RuleDecisionModel
from jiaoyi.gate.stub import StubDecisionModel, StubEntryGate
from jiaoyi.interfaces import (
    Alerter,
    BattleCardProvider,
    DecisionModel,
    EntryGate,
    ExecutorAdapter,
    FeatureEngine,
    Keeper,
    LifeboatStop,
    MarketDataFeed,
    Patrol,
    Predictor,
    Reconciler,
    RiskGuard,
    SecretStore,
    SymbolScreener,
    Trigger,
)
from jiaoyi.keeper.keeper import JevKeeper
from jiaoyi.keeper.stub import StubKeeper
from jiaoyi.market.binance_ws import BinanceFuturesFeed
from jiaoyi.market.derivs import DerivsFeed
from jiaoyi.market.derivs import rest_fetch as derivs_rest
from jiaoyi.market.features import RollingFeatureEngine
from jiaoyi.market.stub import StubMarketDataFeed
from jiaoyi.patrol.rules import RulePatrol
from jiaoyi.predictor import KronosForecaster
from jiaoyi.predictor.against import AgainstCard, AgainstCardWatch
from jiaoyi.predictor.kronos import KronosSettings
from jiaoyi.risk.guard import LotRulesProvider, RiskGuardImpl
from jiaoyi.sentinel.rules import RuleTrigger
from jiaoyi.slow.binance_stats import BinanceStatsSource
from jiaoyi.slow.brief import MarketBriefService
from jiaoyi.slow.commander import BattleCardCommander, StopScale
from jiaoyi.slow.credits import CreditMonitor
from jiaoyi.slow.liquidations import LiquidationSource
from jiaoyi.slow.majors import MajorsSource
from jiaoyi.slow.market_desk import MarketDesk, OpenRouterDeskModel
from jiaoyi.slow.market_log import MarketLog
from jiaoyi.slow.meeting_live import sweep_interrupted
from jiaoyi.slow.news import NewsHub, NewsSource, default_sources
from jiaoyi.slow.review import CardJournal
from jiaoyi.slow.screener import CodeSymbolScreener, UserOverrides, trusted_venue_off
from jiaoyi.slow.settings import (
    SlowConfig,
    market_desk_conflict,
    missing_slow_stack,
    slow_defaults,
)
from jiaoyi.slow.stub import StubBattleCardProvider, StubSymbolScreener
from jiaoyi.slow.tradingagents_engine import PROVIDER_KEY_ENV, TradingAgentsEngine
from jiaoyi.slow.us_market import EarningsCalendar, EarningsNews

log = logging.getLogger(__name__)

Clock = Callable[[], datetime]
Closer = Callable[[], Awaitable[None]]


BLOCKED_FILE = "blocked_symbols.json"
POOL_FILE = "symbol_pool.json"
"""最新候选池（``SymbolPool`` JSON），每次选币后覆盖写；面板选币页只读它。"""


@runtime_checkable
class _Blocks(Protocol):
    @property
    def blocked_symbols(self) -> dict[str, str]: ...

    def unblock_symbol(self, symbol: str) -> None: ...


def read_blocked(data_dir: str | Path) -> dict[str, str]:
    """``jiaoyi status`` 和面板读：被新闻（下架公告）封掉新开仓的币 → 原因。文件坏了当没有。"""
    path = Path(data_dir) / BLOCKED_FILE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}


@runtime_checkable
class _TakesOverrides(Protocol):
    """选币器接受选币页的固定 / 拉黑 / 手动加入（桩选币器没有）。"""

    def set_overrides(self, overrides: UserOverrides) -> None: ...


@runtime_checkable
class _TakesVenue(Protocol):
    """选币器接受执行交易所的可交易名单（桩选币器没有）。"""

    def set_venue(self, venue: Any, label: str = ...) -> None: ...


@runtime_checkable
class _TakesUntradeable(Protocol):
    def set_untradeable(self, untradeable: Mapping[str, str]) -> None: ...
    def card_symbols(self) -> set[str]: ...


@runtime_checkable
class _Refreshable(Protocol):
    async def refresh(self) -> bool: ...


@runtime_checkable
class _SupportsClose(Protocol):
    """同步 ``close()``，例如预测员关线程池。"""

    def close(self) -> None: ...


@runtime_checkable
class _SupportsWarmup(Protocol):
    """启动预热，例如 Jev 先打一次探测请求建好连接。"""

    async def warmup(self) -> float | None: ...


@runtime_checkable
class _SentinelDetail(Protocol):
    """哨兵给团队页的一句话和每币一行（为什么没信号）。"""

    def brief(self, now: datetime) -> str: ...

    def detail_lines(self, now: datetime, tz: str = "UTC") -> list[str]: ...


@runtime_checkable
class _SupportsHeartbeat(Protocol):
    """模块自己上报 ``ModuleStatus``。统一签名：``heartbeat(now)``，其余参数都要有默认值。

    ``runtime_checkable`` 只查方法名不查签名，签名不一致要到运行时才炸，
    所以 tests/test_app_startup.py 用真实模块类核对一遍。
    """

    def heartbeat(self, now: datetime) -> None: ...


@dataclass
class Modules:
    """一套接好的模块。字段名就是角色，测试可以替换任意一个。"""

    feed: MarketDataFeed
    features: FeatureEngine
    trigger: Trigger
    predictor: Predictor
    decision_model: DecisionModel
    gate: EntryGate
    risk: RiskGuard
    executor: ExecutorAdapter
    lifeboat: LifeboatStop
    keeper: Keeper
    patrol: Patrol
    reconciler: Reconciler
    screener: SymbolScreener
    cards: BattleCardProvider
    alerter: Alerter


# 字段 → 角色，用于没有心跳时在团队页上补一条状态
ROLE_OF: dict[str, Role] = {
    "feed": Role.BOOK_READER,
    "features": Role.BOOK_READER,
    "trigger": Role.SENTINEL,
    "predictor": Role.PREDICTOR,
    "decision_model": Role.GATE,
    "gate": Role.GATE,
    "risk": Role.RISK_GUARD,
    "executor": Role.EXECUTOR,
    "lifeboat": Role.LIFEBOAT,
    "keeper": Role.KEEPER,
    "patrol": Role.PATROL,
    "reconciler": Role.RECONCILER,
    "screener": Role.SCREENER,
    "cards": Role.COMMANDER,
    "alerter": Role.ALERTING,
}


def is_stub(module: object) -> bool:
    return type(module).__module__.endswith(".stub")


def _build_cards(
    risk_cfg: RiskConfig,
    app_cfg: AppConfig,
    decision_log: JsonlDecisionLog,
    secrets: SecretStore,
    clock: Clock,
    closers: list[Closer],
    liquidations: LiquidationSource | None = None,
    slow_cfg: SlowConfig | None = None,
    extra_news: Sequence[NewsSource] = (),
) -> tuple[BattleCardProvider, NewsHub | None]:
    """军师团：有 key 就用真的（没装 slow 依赖直接报错）；没有 key 才用桩（没卡 = 不开新仓）。

    新闻源只服务军师团（开会素材、大消息触发、下架封禁），军师团是桩时不拉新闻。
    """
    cfg = slow_cfg or slow_defaults(app_cfg.profile)  # 没有 config/slow.toml：默认值 + 档位
    key_env = PROVIDER_KEY_ENV[cfg.llm.provider]
    if not secrets.get(key_env):
        log.warning("没有 %s，军师团用桩：不出作战卡，快环不会开新仓", key_env)
        return StubBattleCardProvider(), None
    if problem := missing_slow_stack(cfg, secrets):
        raise ConfigError(problem)  # 有 key 就不许静默用桩，否则 M2 会空跑
    news = NewsHub([*default_sources(), *extra_news], decision_log, clock=clock)
    closers.append(news.aclose)
    # 市场简报每轮算一次，总盘会和各币会议共用
    brief = MarketBriefService(clock=clock, style=profiles.prompt_style(app_cfg.hold))
    closers.append(brief.aclose)
    fast = app_cfg.fast_loop
    stop = StopScale(
        fast.atr_period_min,
        fast.min_stop_atr,
        fast.max_stop_atr,
        fast.min_tp_r,
        fast.max_tp_r,
        long_period_min=fast.atr_long_period_min,
        stop_hint=profiles.stop_hint(app_cfg.hold),
    )
    engine = TradingAgentsEngine(
        risk_cfg,
        secrets,
        # 每笔单最多持有多久（档位的时间止损）：军师团的止损 / 止盈 / 关键位按这个时长的波动给
        settings=replace(
            cfg.engine_settings(),
            hold_minutes=max(fast.time_limit_s // 60, 1),
            # 提示词写法 / 指标周期 / 手续费占比按最长持仓档走（core/profiles.py 的 7 档词表）
            hold=app_cfg.hold,
            # 分析师 K 线、关键位找候选的周期都按最长持仓档走（slow.toml [meeting] 写了就按写的）
            analyst_klines=cfg.meeting.analyst_klines or profiles.analyst_klines(app_cfg.hold),
            meeting_min=app_cfg.slow_loop_interval_min,
            level_candidate_interval=profiles.level_interval(app_cfg.hold),
            live_dir=str(Path(app_cfg.data_dir) / "meeting_live"),
        ),
        today=clock,
        news=news,
        liquidations=liquidations,
        brief=brief,
        stop_scale=stop,
    )
    commander = BattleCardCommander(
        engine,
        risk_cfg,
        decision_log,
        pick_max=cfg.effective_pick_max(app_cfg.card_pick_max),
        clock=clock,
        brief=brief,
        # 每次真开会的标的追加到 data/card_review/cards.jsonl，供复盘脚本打分（只读，不调参）
        journal=CardJournal(app_cfg.data_dir),
        stop_scale=stop,
        us_hours=(fast.us_open_delay_min, fast.us_close_lead_min) if fast.us_hours_gate else None,
        # 当天已花的钱落盘（按曼谷日），重启不会把日上限清零
        budget_path=Path(app_cfg.data_dir) / "slow_budget.json",
        # 每开完一场会就把卡落盘，重启后还没过期的卡直接接着用
        card_path=Path(app_cfg.data_dir) / "battle_card.json",
        # 分档交易：中间档今天挑中了哪几个（曼谷日），重启不重挑
        tier_path=Path(app_cfg.data_dir) / "mid_tier.json",
        # 总盘会（大盘基调 + 挑币）的实况，和各币会议一起放 meeting_live/_MARKET.json
        live_dir=Path(app_cfg.data_dir) / "meeting_live",
        # 每轮总盘会的记录（池子里每个币的分数 / 数字 / 结论），事后回填实际结果
        market_log=MarketLog(Path(app_cfg.data_dir) / "market_meeting"),
        log_meta={
            "meeting_interval": app_cfg.meeting_interval,
            "hold": app_cfg.hold,
            "hold_hours": max(fast.time_limit_s / 3600.0, 1.0),
        },
        **cfg.commander_kwargs(app_cfg.profile),
    )
    return commander, news


@dataclass
class Built:
    """``build_modules`` 的结果。"""

    modules: Modules
    closers: list[Closer]
    recorders: dict[Role, RoleRecorder]
    gate_mode: str
    """``jev`` / ``rule``（规则拍板，只在模拟盘）/ ``none``（没有拍板模型，不开新仓）。"""
    lot_rules: LotRulesProvider
    """下单精度。不是角色，不进 ``Modules``；``App.start`` 和主循环负责定时刷新。"""
    news: NewsHub | None = None
    """新闻 / 公告源。只有真军师团时才有；主循环定时 ``poll_and_notify``。"""
    startup_checks: list[Closer] = field(default_factory=list)
    """启动前必须通过的核对（hummingbot 与 ccxt 是同一账户）；抛异常即拒绝启动。"""
    income: IncomeDaily | None = None
    """币安收入流水按日汇总（data/income_daily.json）；没连交易所时没有。主循环定时刷新。"""
    ledger: LedgerSync | None = None
    """账本按币安成交回填 + 收入流水刷新（用户数据流叫醒，60 秒兜底）；没连交易所时没有。"""
    against: AgainstCardWatch | None = None
    """预测员连续看反作战卡的事件 / 查询（慢环订阅它开单币临时会）。"""
    derivs: DerivsFeed | None = None
    """持仓量 / 多空比轮询（新信号 + 拍板人快照）；没连币安时没有。主循环跑 ``run``。"""
    halted: dict[str, str] = field(default_factory=dict)
    """合约临时不在交易（结算中等）的币 → 原因。主循环按币安推送增删，风控员只挡新开仓。"""
    observations: KeeperObservationCollector | None = None
    keeper_jev: JevDecisionModel | None = None
    """The keeper's own Jev instance when ``jev_shared_cooldown`` is off; None when shared."""


def _note_predictor_against(sink: JsonlDecisionLog, a: AgainstCard) -> None:
    """预测员连续看反作战卡、但临时会开关关着：决策日志里记一条，事后对比用。"""
    side = "多" if a.card_bias is Bias.LONG else "空"
    try:
        sink.write(
            DecisionRecord(
                record_id=uuid.uuid4().hex,
                ts=a.ts,
                role=Role.PREDICTOR,
                symbol=a.symbol,
                outcome=Outcome.INFO,
                summary=f"{a.symbol} 预测员连续 {a.streak} 次看反作战卡（卡做{side}，上涨概率 "
                f"{a.p_up:.2f}）；临时会开关关着，只记录",
                card_id=a.card_id,
                output={
                    "predictor_against_card": True,
                    "card_bias": a.card_bias.value,
                    "p_up": a.p_up,
                    "streak": a.streak,
                    "since": a.since.isoformat(),
                    "meeting_requested": False,
                },
            )
        )
    except OSError:
        log.exception("写预测员看反记录失败")


def _build_alerter(
    secrets: SecretStore, closers: list[Closer], muted_kinds: frozenset[str] = frozenset()
) -> Alerter:
    """有 Telegram 密钥就发 Telegram，否则打到日志里。"""
    token, chat = secrets.get("TELEGRAM_BOT_TOKEN"), secrets.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        log.warning("没配 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID，告警只写日志")
        return LogAlerter()
    telegram = TelegramAlerter(token, chat, muted_kinds=muted_kinds)
    closers.append(telegram.close)
    return telegram


def _build_execution(
    risk_cfg: RiskConfig,
    app_cfg: AppConfig,
    secrets: SecretStore,
    alerter: Alerter,
    clock: Clock,
    closers: list[Closer],
    decision_log: JsonlDecisionLog,
    startup_checks: list[Closer],
) -> tuple[ExecutorAdapter, LifeboatStop, Reconciler, LotRulesProvider, LedgerSync | None]:
    """执行层按模式选币安密钥（testnet/paper 用测试网的，live 用主网的）。

    live 没解锁（确认词 + 面板密码）或缺密钥：抛异常，拒绝启动。
    paper / testnet 缺密钥：执行层用桩，照常看行情但不开仓。
    """
    mode = RunMode(app_cfg.mode)
    try:
        stack = build_execution(
            mode,
            risk_cfg,
            app_cfg.data_dir,
            secrets,
            alerter,
            live_gate=LiveGate(Path(app_cfg.data_dir) / "live_gate.json"),
            decision_log=decision_log,
            close_unknown_positions=app_cfg.reconciler_close_unknown_positions,
            executor_stop_retry_rounds=app_cfg.reconciler_executor_stop_retry_rounds,
            clock=clock,
            reconcile_opening_grace_s=app_cfg.reconcile_opening_grace_s,
            reconcile_closing_grace_s=app_cfg.reconcile_closing_grace_s,
            demo_trading=app_cfg.sim_environment == "demo",
            limit_take_profit=app_cfg.limit_take_profit_on,
            frozen_peak_exit_on=app_cfg.fast_loop.frozen_peak_exit_on,
            frozen_peak_exit_after_s=app_cfg.fast_loop.frozen_peak_exit_after_s,
            protection_readback_grace_s=app_cfg.protection_readback_grace_s,
            protection_prearm_grace_s=app_cfg.protection_prearm_grace_s,
            protection_unknown_submit_grace_s=app_cfg.protection_unknown_submit_grace_s,
            protection_stop_working_type=app_cfg.protection_stop_working_type,
            protection_take_profit_working_type=app_cfg.protection_take_profit_working_type,
            protection_trailing_book_mark_max_dev_bps=(
                app_cfg.protection_trailing_book_mark_max_dev_bps
            ),
            protection_trailing_stop_mark_buffer_bps=app_cfg.protection_trailing_stop_mark_buffer_bps,
            protection_trailing_min_step_bps=app_cfg.protection_trailing_min_step_bps,
            protection_trailing_book_critical_after_s=(
                app_cfg.protection_trailing_book_critical_after_s
            ),
            protection_leg_order=app_cfg.protection_leg_order,
            hosted_close_mode=app_cfg.hosted_close_mode,
            protection_tick_alert_after=app_cfg.protection_tick_alert_after,
            hb_stop_unknown_retries=app_cfg.hb_stop_unknown_retries,
            hosted_close_direct_when_exiting=app_cfg.hosted_close_direct_when_exiting,
            position_mode_cache_s=app_cfg.position_mode_cache_s,
            leverage_brackets_wanted=lambda: app_cfg.fast_loop.leverage_clamp_to_bracket,
        )
    except MissingSecret:
        if mode is RunMode.LIVE:
            raise
        log.warning("执行层缺密钥，%s 模式下不开仓", mode.value, exc_info=True)
        stubs = StubExecutorAdapter(clock()), StubLifeboatStop(), StubReconciler()
        return *stubs, StubLotRules(), None
    closers.append(stack.close)
    startup_checks.append(stack.verify_same_account)
    # Demo / paper 也拉：面板的盈利口径看的就是这个账户的流水
    income = IncomeDaily(stack.exchange.income_rows, app_cfg.data_dir, risk_cfg.day_reset_tz)
    ex = stack.exchange
    if mode is RunMode.LIVE:
        ws_base = LIVE_WS_BASE
    else:
        ws_base = DEMO_WS_BASE if app_cfg.sim_environment == "demo" else LEGACY_TESTNET_WS_BASE
    ledger = LedgerSync(stack.adapter, ex.user_trades, income, clock=clock)
    ledger.attach(
        UserStream(ex.new_listen_key, ex.keepalive_listen_key, ledger.on_fill, ws_base=ws_base)
    )
    return stack.adapter, stack.lifeboat, stack.reconciler, stack.lot_rules, ledger


def build_modules(
    risk_cfg: RiskConfig,
    app_cfg: AppConfig,
    decision_log: JsonlDecisionLog,
    clock: Clock,
    secrets: SecretStore,
    *,
    offline: bool = False,
    slow_cfg: SlowConfig | None = None,
) -> Built:
    """按配置选实现。新角色合并后在这里换掉对应的桩。

    ``offline=True`` 时不连任何网络（测试、回放）：行情、选币、军师团都用桩，快环其余部分照常。
    ``closers`` 在 ``App.stop`` 里依次调用，关掉网络连接等资源。
    """
    fast = app_cfg.fast_loop
    tz = risk_cfg.day_reset_tz
    closers: list[Closer] = []
    startup_checks: list[Closer] = []
    recorders = {
        role: RoleRecorder(role, decision_log, day_reset_tz=tz)
        for role in (
            Role.SENTINEL,
            Role.GATE,
            Role.RISK_GUARD,
            Role.KEEPER,
            Role.PATROL,
            Role.SCREENER,
            Role.ALERTING,
        )
    }

    screener: SymbolScreener
    cards: BattleCardProvider
    feed: MarketDataFeed
    news: NewsHub | None = None
    if offline:
        feed = StubMarketDataFeed()
        screener, cards = StubSymbolScreener(), StubBattleCardProvider()
    else:
        bfeed = BinanceFuturesFeed(status_sink=decision_log, day_reset_tz=tz)
        feed = bfeed
        # 选币只读币安公开接口，不需要 key
        # 强平统计由行情层的全市场强平流产出，选币和军师团共用，不另开连接
        source = BinanceStatsSource(
            liquidations=bfeed.liquidations,
            hold_minutes=max(app_cfg.fast_loop.time_limit_s // 60, 1),
        )
        closers.append(source.aclose)
        scfg = slow_cfg or slow_defaults(app_cfg.profile)
        earnings = EarningsCalendar() if scfg.screener.us_events else None
        if earnings is not None:
            closers.append(earnings.aclose)
        majors = MajorsSource(Path(app_cfg.data_dir) / "majors.json")
        closers.append(majors.aclose)
        screener = CodeSymbolScreener(
            source,
            # 门槛和权重来自 slow.toml [screener]；价差默认跟硬上限一致，免得进池的币风控员一律拒
            (slow_cfg or slow_defaults(app_cfg.profile)).screener_config(
                app_cfg.pool_size, risk_cfg.max_spread_bps, risk_cfg.taker_fee_bps * 2
            ),
            recorder=recorders[Role.SCREENER],
            earnings=earnings,
            majors=majors,
            every=timedelta(minutes=app_cfg.screener_interval_min),
            tz=tz,
            market_log=MarketLog(Path(app_cfg.data_dir) / "market_meeting"),
        )
        cards, news = _build_cards(
            risk_cfg,
            app_cfg,
            decision_log,
            secrets,
            clock,
            closers,
            bfeed.liquidations,
            slow_cfg,
            # 美股类别近期的财报日作为背景新闻进军师团（按选币器最近选出的美股代码拉）
            [EarningsNews(earnings, screener.us_tickers, clock)] if earnings is not None else [],
        )

    alerter: Alerter
    executor: ExecutorAdapter
    lifeboat: LifeboatStop
    reconciler: Reconciler
    lot_rules: LotRulesProvider
    income: IncomeDaily | None = None
    ledger: LedgerSync | None = None
    if offline:
        alerter = StubAlerter()
        executor, lifeboat, reconciler, lot_rules = (
            StubExecutorAdapter(clock()),
            StubLifeboatStop(),
            StubReconciler(),
            StubLotRules(),
        )
    else:
        inner = _build_alerter(secrets, closers, app_cfg.muted_alert_kinds)
        alerter = RecordedAlerter(
            inner, recorders[Role.ALERTING], telegram=isinstance(inner, TelegramAlerter)
        )
        executor, lifeboat, reconciler, lot_rules, ledger = _build_execution(
            risk_cfg, app_cfg, secrets, alerter, clock, closers, decision_log, startup_checks
        )
        income = ledger.income if ledger is not None else None

    predictor = KronosForecaster(
        app_cfg.predictor_enabled,
        # 周期和预测长度跟最长持仓档走（4h 及以下 5m，8h / 12h 15m，24h 1h）
        KronosSettings(
            interval=Interval(app_cfg.predictor.interval),
            pred_len=app_cfg.predictor.pred_len,
            lookback=app_cfg.predictor.lookback,
            paths=app_cfg.predictor.paths,
            temperature=app_cfg.predictor.temperature,
        ),
        log_sink=decision_log,
        clock=clock,
        cfg=app_cfg.predictor,
        display_tz=tz,
    )
    # 预测员连续看反作战卡 → 事件；慢环订阅（App.against.subscribe）用来开单币临时会
    sched = (slow_cfg or slow_defaults(app_cfg.profile)).schedule
    against = AgainstCardWatch(cards.current, streak=sched.predictor_against_runs)
    predictor.add_listener(against.observe)
    request_event = getattr(cards, "request_event", None)
    if app_cfg.predictor.against_card_meeting and callable(request_event):
        against.subscribe(lambda a: request_event(a.symbol, "predictor_against_card"))
    else:  # 默认关：只记录，不申请会（预测员还在观察）
        against.subscribe(lambda a: _note_predictor_against(decision_log, a))
    features = RollingFeatureEngine(
        fast.book_depth_levels,
        atr_period_min=fast.atr_period_min,
        atr_long_period_min=fast.atr_long_period_min,
        oi_window_min=fast.oi_window_min,
    )
    derivs: DerivsFeed | None = None
    halted: dict[str, str] = {}
    if isinstance(feed, BinanceFuturesFeed):
        feed.set_coverage(features.coverage)
        derivs = DerivsFeed(
            oi_window_min=fast.oi_window_min, ratio_window_min=fast.top_crowd_window_min
        )
        features.set_extra(derivs.values)

    # 拍板模型是启动时的一次性选择：有 Jev 用 Jev；模拟盘且明确打开才用规则版；否则不开新仓。
    # Jev 运行中出错只返回 ok=False，绝不切到规则版。
    model = decision_model_from_config(
        secrets, fast, app_cfg.mode, timeout_ms=app_cfg.jev_timeout_ms, day_reset_tz=tz
    )
    decision_model: DecisionModel
    gate: EntryGate
    keeper: Keeper
    for name in app_cfg.ignored_paper_switches:
        log.warning("%s 只在模拟盘生效，已忽略", name)
    observations: KeeperObservationCollector | None = None
    keeper_jev: JevDecisionModel | None = None
    if model is None:
        gate_mode = "none"
        log.warning("没有可用的拍板模型（缺 JEV_API_KEY 或没锁 jev_model），不开新仓")
        decision_model, gate, keeper = StubDecisionModel(), StubEntryGate(), StubKeeper()
    else:
        try:
            observations = KeeperObservationCollector(app_cfg.data_dir)

            async def close_observations() -> None:
                if observations is not None:
                    observations.close()

            closers.append(close_observations)
        except Exception:
            observations = None
        gate_mode = "rule" if isinstance(model, RuleDecisionModel) else "jev"
        decision_model = model
        gate = JevEntryGate(
            model,
            recorders[Role.GATE],
            hold_min=fast.time_limit_s // 60,
            max_concurrent=app_cfg.jev_max_concurrent,
            # 排队再等就来不及在信号有效期内判完（风控员按年龄拒，白花一次 Jev）
            max_wait_s=max(fast.signal_max_age_s - app_cfg.jev_timeout_ms / 1000.0, 0.0),
            questions=fast.gate_questions,
            market_age=cards.market_age if isinstance(cards, BattleCardCommander) else None,
            take_floor=lambda kind: fast.min_p_take_by_kind.get(kind),
            # 一道题问法的额外快照：技术面 / 成交流 / 盘口平均（内存）
            # + 信号来时补拉的 REST（实盘行情才拉）
            context=EntryContextSource(
                features.entry_context,
                adl=derivs.adl_risk if derivs is not None else None,
                fetch=derivs_rest if isinstance(feed, BinanceFuturesFeed) else None,
                # 补拉最多等 1 秒，但不能把信号拖过有效期（有效期 − 拍板超时）
                timeout_s=min(
                    1.0, max(fast.signal_max_age_s - app_cfg.jev_timeout_ms / 1000.0, 0.0)
                ),
            ),
        )
        keeper_model: DecisionModel = model
        if not app_cfg.jev_shared_cooldown and isinstance(model, JevDecisionModel):
            keeper_model = keeper_jev = model.with_own_errors(
                timeout_ms=app_cfg.keeper_jev_timeout_ms or None
            )
        elif app_cfg.keeper_jev_timeout_ms and isinstance(model, JevDecisionModel):
            keeper_model = model.with_timeout(app_cfg.keeper_jev_timeout_ms)
        keeper = JevKeeper(
            keeper_model,
            fast,
            features.snapshot,
            cards.current,
            recorders[Role.KEEPER],
            blocked=(lambda: cards.blocked_symbols) if isinstance(cards, _Blocks) else None,
            fee_bps=risk_cfg.taker_fee_bps,
            forecast=predictor.latest,
            scan_day=decision_log.scan_day,
            frozen_peak_review=getattr(executor, "frozen_peak_review", None),
            current_market=(lambda: cards.latest_market_view)
            if isinstance(cards, BattleCardCommander)
            else None,
            market_intra=cards.keeper_intra if isinstance(cards, BattleCardCommander) else None,
            market_age=cards.market_age if isinstance(cards, BattleCardCommander) else None,
            observation_source=features.diagnostic_observation if observations else None,
            observation_sink=observations.offer if observations else None,
            opposite_state_path=Path(app_cfg.data_dir) / "keeper_opposite.json",
            review_interval_s=float(app_cfg.keeper_interval_s),
            # 实盘的执行所就是主网行情，不另读；模拟盘 / 测试网读同环境标记价（Demo）
            venue_price=getattr(executor, "venue_mark", None)
            if RunMode(app_cfg.mode) is not RunMode.LIVE
            else None,
            exit_in_flight=getattr(executor, "protection_exit_in_flight", None),
            alerter=alerter,
            kronos_snapshot=app_cfg.predictor.keeper_snapshot,
        )

    modules = Modules(
        feed=feed,
        features=features,
        trigger=RuleTrigger(
            fast,
            dedup_s=app_cfg.signal_dedup_s,
            recorder=recorders[Role.SENTINEL],
            predictor=predictor,
            live=RunMode(app_cfg.mode) is not RunMode.PAPER,
        ),
        predictor=predictor,
        decision_model=decision_model,
        gate=gate,
        risk=RiskGuardImpl(
            risk_cfg,
            fast,
            # 交易所精度由执行层从 exchangeInfo 读；拿不到（没刷新成功、过期、下架）风控员一律拒单
            lot_rules,
            recorders[Role.RISK_GUARD],
            # 日亏熔断状态落盘，重启后当天仍锁住
            state_dir=app_cfg.data_dir,
            # paper 默认拍板只记不拦（攒样本校准门槛）；testnet / live 永远拦
            gate_record_only=app_cfg.gate_record_only,
            # 「不做」过滤：封币理由写全；自动减仓风险高不开新仓（新信号研究 10-24）
            blocked=(lambda: cards.blocked_symbols) if isinstance(cards, _Blocks) else None,
            adl_risk=derivs.adl_risk if derivs is not None else None,
            live=RunMode(app_cfg.mode) is not RunMode.PAPER,
            halted=halted.get,
        ),
        executor=executor,
        lifeboat=lifeboat,
        keeper=keeper,
        patrol=RulePatrol(fast, recorders[Role.PATROL], fee_bps=risk_cfg.taker_fee_bps),
        reconciler=reconciler,
        screener=screener,
        cards=cards,
        alerter=alerter,
    )
    return Built(
        modules,
        closers,
        recorders,
        gate_mode,
        lot_rules,
        news,
        startup_checks,
        income,
        ledger,
        against,
        derivs,
        halted,
        observations,
        keeper_jev,
    )


class App:
    """进程里唯一的一套模块。``start`` / ``stop`` 管生命周期，``status`` 给团队页和命令行。"""

    def __init__(
        self,
        risk_cfg: RiskConfig,
        app_cfg: AppConfig,
        modules: Modules | None = None,
        decision_log: JsonlDecisionLog | None = None,
        clock: Clock = lambda: datetime.now(UTC),
        secrets: SecretStore | None = None,
        *,
        offline: bool = False,
        slow_cfg: SlowConfig | None = None,
        config_files: ConfigFiles | None = None,
    ) -> None:
        self.risk_cfg = risk_cfg
        self.app_cfg = app_cfg
        self.clock = clock
        self.log = decision_log or JsonlDecisionLog(app_cfg.data_dir)
        self._closers: list[Closer] = []
        self._startup_checks: list[Closer] = []
        self.recorders: dict[Role, RoleRecorder] = {}
        self.gate_mode = "none"
        self.lot_rules: LotRulesProvider = StubLotRules()
        self.income: IncomeDaily | None = None
        self.ledger: LedgerSync | None = None
        self.news: NewsHub | None = None
        self.against: AgainstCardWatch | None = None
        self.derivs: DerivsFeed | None = None
        self.observations: KeeperObservationCollector | None = None
        self.keeper_jev: JevDecisionModel | None = None
        self._quote_diagnostics_enabled = not offline
        self.stop_quotes: StopQuoteRecorder | None = None
        self._stop_quotes_task: asyncio.Task[None] | None = None
        # 合约临时不在交易的币（主循环按 !contractInfo 推送增删），和风控员共用一个 dict
        self.halted: dict[str, str] = {}
        self.credits: CreditMonitor | None = None
        self.slow_cfg = slow_cfg or slow_defaults(app_cfg.profile)
        # 配置热生效（core/hotconfig.py）：给了文件路径，主循环定时看文件变没变
        self.config_files = config_files
        self._config_stamp = config_files.stamp() if config_files else None
        self.hot_applied_at: str | None = None
        # 人工暂停开新仓（面板 pause 指令），落盘在 data/paused.json，重启后仍生效
        self.paused: dict[str, Any] | None = read_paused(app_cfg.data_dir)
        if modules is None:
            built = build_modules(
                risk_cfg,
                app_cfg,
                self.log,
                clock,
                secrets or ChainedSecretStore(),
                offline=offline,
                slow_cfg=slow_cfg,
            )
            modules, self._closers = built.modules, built.closers
            self._startup_checks = built.startup_checks
            self.recorders, self.gate_mode = built.recorders, built.gate_mode
            self.lot_rules = built.lot_rules
            self.income = built.income
            self.ledger = built.ledger
            self.news = built.news
            self.against = built.against
            self.derivs = built.derivs
            self.observations = built.observations
            self.keeper_jev = built.keeper_jev
            self.halted = built.halted
            desk_on = self.slow_cfg.market_desk.mode != "off"
            if (self.slow_cfg.llm.provider == "openrouter" or desk_on) and self.news is not None:
                store = secrets or ChainedSecretStore()  # 真军师团才有 news；桩没有额度可看
                self.credits = CreditMonitor(
                    app_cfg.data_dir,
                    lambda: store.get("OPENROUTER_API_KEY"),
                    low_floor_usd=app_cfg.credit_low_floor_usd,
                    resend_after=timedelta(hours=app_cfg.credit_resend_hours),
                )
        self.m = modules
        # 大盘台（slow.toml [market_desk]）：mode = off 时不建，一次模型调用都没有
        self.desk: MarketDesk | None = None
        if self.slow_cfg.market_desk.mode != "off" and isinstance(
            modules.cards, BattleCardCommander
        ):
            self.desk = self._build_desk(modules.cards, secrets or ChainedSecretStore())
        # 和卡相反的信号：主循环按账本持仓分流（有仓叫管家急查、反复出申请单币临时会）
        cards = modules.cards
        bind_credit = getattr(cards, "bind_credit_alarm", None)
        if callable(bind_credit) and self.credits is not None:
            bind_credit(lambda: self.credits.note_exhausted(self.clock()))
        request_event = getattr(cards, "request_event", None)
        accepts = getattr(cards, "accepts_event", None)
        self.against_desk = AgainstSignalDesk(
            app_cfg.fast_loop,
            request_event=request_event if callable(request_event) else None,
            meeting_ok=(lambda: bool(accepts(MEETING_TRIGGER))) if callable(accepts) else None,
            recorder=self.recorders.get(Role.SENTINEL),
        )
        self.pool: SymbolPool | None = None
        self.started = False
        # 选币页的固定 / 拉黑 / 手动加入（data/pool_overrides.json）；读不出按没有覆盖项、启动时告警
        loaded = read_pool_overrides(app_cfg.data_dir)
        self.pool_overrides_corrupt = loaded is None
        if loaded is None:
            backup_corrupt_pool_overrides(app_cfg.data_dir, clock())
        self.pool_overrides = loaded or PoolOverrides()
        self._push_overrides()

    async def _run_startup_checks(self) -> None:
        """账户核对不过一律拒绝启动；读不到（网络）时 live 也拒绝，模拟盘只告警。"""
        for check in self._startup_checks:
            try:
                await check()
            except AccountMismatch as e:
                await self._alert(AlertLevel.CRITICAL, "拒绝启动：账户核对不一致", str(e), "system")
                raise
            except Exception as e:
                if self.app_cfg.mode == RunMode.LIVE:
                    await self._alert(
                        AlertLevel.CRITICAL, "拒绝启动：读不到账户做核对", repr(e), "system"
                    )
                    raise
                log.warning("启动账户核对读不到数据，模拟盘继续：%r", e)

    async def start(self) -> None:
        """按保命优先的顺序启动。对账失败不阻止启动，但会告警（重启后先对账）。"""
        if self.started:
            return
        now = self.clock()
        swept = sweep_interrupted(Path(self.app_cfg.data_dir) / "meeting_live")
        if swept:
            log.info("上个进程留下 %d 场没开完的会，已标成中断：%s", len(swept), "、".join(swept))
        auto_resumed = self._pause_for_pool_overrides()
        await self._run_startup_checks()
        try:
            report = await self.m.reconciler.reconcile(now)
            problems = [i for i in report.issues if not i.informational]
            if problems:
                log.warning("启动对账发现 %d 个问题", len(problems))
        except Exception:
            # 对账员已发告警；照常启动，主循环里每轮再对
            log.exception("启动对账失败")
        await self.refresh_lot_rules()
        if isinstance(self.m.screener, _TakesVenue):
            label = "实盘" if self.app_cfg.mode == RunMode.LIVE else "Demo"
            self.m.screener.set_venue(self.lot_rules, label)
        try:
            self.set_pool(await self.m.screener.screen(now))
        except Exception:
            # 选币失败不阻止启动：没有池子军师不开会，快环自然不开新仓
            log.exception("启动选币失败，下一轮选币再试")
        self.sync_untradeable(now)
        # 行情只订要盯的币（总盘会挑中 + 作战卡 + 持仓），由主循环 Runner.sync_watch 订
        self.save_blocked()  # 封禁只在内存里，重启后清掉旧文件，由新闻源重新补上
        self._write_runtime(now)
        if isinstance(self.m.decision_model, _SupportsWarmup):
            # 先打一次很小的探测请求把连接建好，第一笔真信号不吃冷启动；失败不影响启动
            await self.m.decision_model.warmup()
        self.heartbeat_all()
        self.started = True
        # 独立诊断连接只读落盘账本，绝不把 Demo 行情传入策略，也不调用含副作用的 positions()。
        if self._quote_diagnostics_enabled and isinstance(self.m.feed, BinanceFuturesFeed):
            environment = (
                "live" if self.app_cfg.mode == RunMode.LIVE else self.app_cfg.sim_environment
            )
            if environment in ("demo", "live"):
                try:
                    self.stop_quotes = StopQuoteRecorder(self.app_cfg.data_dir, environment)
                    self._stop_quotes_task = asyncio.create_task(self.stop_quotes.run())
                except Exception:
                    log.warning("持仓盘口诊断未启动（不影响交易）", exc_info=True)
        if self.paused is not None and self.paused.get("corrupt"):
            await self._alert(
                AlertLevel.CRITICAL,
                "暂停状态文件损坏，按已暂停处理",
                "不开新仓；确认后在面板发恢复指令",
                "system",
            )
        if self.pool_overrides_corrupt:
            await self._alert(
                AlertLevel.CRITICAL,
                "选币覆盖项文件读不出，暂停开新仓",
                "data/pool_overrides.json 损坏（已备份成 .corrupt-时间戳）：拉黑清单这次没生效，"
                "所以不开新仓（已有持仓照常看管），选币页的操作也会被拒；修好或删掉后重启主程序恢复",
                "system",
            )
        elif auto_resumed:
            await self._alert(
                AlertLevel.INFO, "选币覆盖项已修好，恢复开新仓", "上次因文件损坏自动暂停", "system"
            )
        if self.app_cfg.mode == RunMode.LIVE:
            # 实盘启动一定要通知（设计文档 §十）；能走到这里说明上线确认已经通过
            await self._alert(
                AlertLevel.CRITICAL, "实盘已启动", f"拍板：{self.gate_label()}", "live_started"
            )
        log.info("已启动：模式 %s，%d 个角色仍是桩", self.app_cfg.mode, len(self.stub_fields()))

    async def stop(self) -> None:
        """先断行情（不再产生新信号），最后才停执行层；网络连接等资源无论是否启动过都会释放。"""
        if self.started:
            await self.m.feed.close()
            for name in ("predictor", "keeper", "patrol", "gate", "decision_model", "cards"):
                module = getattr(self.m, name)
                if isinstance(module, _SupportsClose):
                    try:
                        module.close()
                    except Exception:
                        log.exception("关闭 %s 失败", name)
        if self.stop_quotes is not None and self._stop_quotes_task is not None:
            try:
                await self.stop_quotes.aclose(self._stop_quotes_task)
            except Exception:
                log.warning("停止盘口诊断失败（不影响交易）", exc_info=True)
            self.stop_quotes, self._stop_quotes_task = None, None
        for closer in self._closers:
            try:
                await closer()
            except Exception:
                log.exception("释放资源失败")
        self._closers = []
        self.started = False

    async def refresh_lot_rules(self) -> None:
        """拉一次交易所精度。失败沿用旧缓存，过期后风控员自动拒单。"""
        if isinstance(self.lot_rules, _Refreshable) and not await self.lot_rules.refresh():
            log.warning("下单精度刷新失败")

    def sync_untradeable(self, now: datetime) -> None:
        """卡上已有、但交易所现在不能交易的币，让作战卡读成只看不做（持仓币不动）。

        交易所名单没拉到或开关关着时清空，不误伤。"""
        cards = self.m.cards
        if not isinstance(cards, _TakesUntradeable):
            return
        gone: dict[str, str] = {}
        unavailable = getattr(self.lot_rules, "unavailable", None)
        if self.slow_cfg.screener.exclude_untradeable and callable(unavailable):
            syms = cards.card_symbols()
            if syms:
                # judged against the pool too, so a partial fetch reads the same as in the screener
                pool = [e.symbol for e in self.pool.in_pool] if self.pool is not None else []
                judged = [*syms, *pool]
                label = "实盘" if self.app_cfg.mode == RunMode.LIVE else "Demo"
                gone = trusted_venue_off(unavailable(judged), judged, label, "作战卡")
                gone = {k: v for k, v in gone.items() if k in set(syms)}
        cards.set_untradeable(gone)

    async def credit_tick(self) -> None:
        """模型额度看护：每小时只读查一次 OpenRouter 余额，快用完或会议撞 402 就推 Telegram。"""
        if self.credits is None:
            return
        try:
            alerts = await asyncio.to_thread(self.credits.tick, self.clock())
        except Exception:
            log.exception("额度看护出错（不影响交易）")
            return
        for level, title, body in alerts:
            await self._alert(
                AlertLevel.CRITICAL if level == "critical" else AlertLevel.WARN,
                title,
                body,
                "model_credit",
            )

    async def refresh_income(self) -> None:
        """刷新 data/income_daily.json（面板的盈利口径）。拉不到沿用文件里的旧数。"""
        if self.income is not None:
            await self.income.refresh(self.clock())

    def blocked_symbols(self) -> dict[str, str]:
        """当前禁止新开仓的币 → 原因（来自币安下架公告）。"""
        cards = self.m.cards
        return cards.blocked_symbols if isinstance(cards, _Blocks) else {}

    def unblock_symbol(self, symbol: str) -> bool:
        """人工解封（面板以后调这里）。只在本进程生效；下一条新的下架公告会再封上。"""
        cards = self.m.cards
        if not isinstance(cards, _Blocks) or symbol not in cards.blocked_symbols:
            return False
        cards.unblock_symbol(symbol)
        log.warning("人工解封 %s 的新开仓", symbol)
        self.save_blocked()
        return True

    def save_blocked(self) -> None:
        """把封禁名单写到 data_dir，给 ``jiaoyi status`` 和面板读（它们不在主循环进程里）。"""
        self._write_atomic(BLOCKED_FILE, json.dumps(self.blocked_symbols(), ensure_ascii=False))

    def _pause_for_pool_overrides(self) -> bool:
        """覆盖项文件读不出 → 拉黑清单没生效，暂停开新仓（落盘，面板总览显示原因）。

        只有这条自动暂停会在文件修好后的下一次启动自动解除，返回是否解除了；
        人工暂停照旧要在面板恢复。已经人工暂停着就不改它的原因。
        """
        source = (self.paused or {}).get("source")
        if self.pool_overrides_corrupt:
            if self.paused is None:
                self.set_paused(
                    True,
                    reason=f"选币覆盖项 data/{POOL_OVERRIDES_FILE} 读不出（拉黑清单没生效）；"
                    "修好或删掉后重启主程序自动恢复",
                    source=POOL_OVERRIDES_PAUSE,
                )
            return False
        if source == POOL_OVERRIDES_PAUSE:
            self.set_paused(False)
            return True
        return False

    def set_paused(
        self, paused: bool, *, reason: str = "", command_id: str = "", source: str = ""
    ) -> None:
        """人工暂停 / 恢复开新仓。只拦新开仓，已有持仓照常由管家、巡逻兵和止损看着。"""
        now = self.clock().isoformat()
        info = {"since": now, "reason": reason, "command_id": command_id}
        if source:
            info["source"] = source
        self.paused = {"paused": True, **info} if paused else None
        try:
            write_paused(self.app_cfg.data_dir, paused, **info)
        except OSError:
            log.exception("写暂停状态失败")

    def _build_desk(self, cards: BattleCardCommander, secrets: SecretStore) -> MarketDesk:
        cfg = self.slow_cfg.market_desk
        if not secrets.get("OPENROUTER_API_KEY"):
            raise ConfigError(
                "slow.toml [market_desk] 开了，但没有 OPENROUTER_API_KEY（大盘台经 OpenRouter 调 "
                "DeepSeek V4.1 Flash）；先 setup-keys，或把 mode 改回 off"
            )
        credits = self.credits
        return MarketDesk(
            cfg,
            cards,
            OpenRouterDeskModel(lambda: secrets.get("OPENROUTER_API_KEY"), cfg),
            self.log,
            self.app_cfg.data_dir,
            clock=self.clock,
            day_tz=self.risk_cfg.day_reset_tz,
            balance=(lambda: credits.balance_usd()) if credits is not None else None,
            credit_alarm=(lambda: credits.note_exhausted(self.clock()))
            if credits is not None
            else None,
        )

    def reload_config(self) -> dict[str, tuple[Any, Any]]:
        """配置文件变了就读出来，把热生效的项换到正在用的配置对象上（不用重启）。

        整份读出来、按启动时一样的规则校验，读不好就沿用旧的（面板设置页会显示错误）。
        返回真换了的项 ``{段.键: (旧, 新)}``。必须重启才生效的项不在这里动。"""
        files = self.config_files
        if files is None or (stamp := files.stamp()) == self._config_stamp:
            return {}
        self._config_stamp = stamp
        try:
            raw = read_toml(files.app)
            # 档位（hold / meeting_interval / mode）没重启前不换：重读时仍按正在跑的垫默认值
            for key in PROFILE_KEYS:
                val = getattr(self.app_cfg, key)
                raw[key] = getattr(val, "value", val)
            raw.pop("profile", None)
            new_app = AppConfig.model_validate(with_profile(raw))
            new_risk = load_risk_config(files.risk)
            if problem := market_desk_conflict(self.slow_cfg, new_app.fast_loop.regime_flip_target):
                raise ConfigError(problem)
        except (ConfigError, ValidationError) as e:
            log.warning("配置文件改过但读不好，继续用旧配置：%s", e)
            return {}
        changed: dict[str, tuple[Any, Any]] = {}
        for section, target, new, names in (
            ("risk", self.risk_cfg, new_risk, HOT_RISK),
            ("fast_loop", self.app_cfg.fast_loop, new_app.fast_loop, HOT_FAST),
            ("app", self.app_cfg, new_app, HOT_APP),
        ):
            for name, pair in patch_inplace(target, new, names).items():
                changed[f"{section}.{name}"] = pair
        if not changed:
            return {}
        self._apply_hot(changed)
        self.hot_applied_at = self.clock().isoformat()
        try:
            update_runtime(self.app_cfg.data_dir, hot_applied_at=self.hot_applied_at)
        except OSError:
            log.exception("写 runtime.json 的热生效时间失败（只影响面板显示）")
        log.info("配置已热生效（不用重启）：%s", describe(changed))
        return changed

    def _apply_hot(self, changed: Mapping[str, tuple[Any, Any]]) -> None:
        """通知启动时把值抄进了自己变量的部件重新算。"""
        m, fast, cfg = self.m, self.app_cfg.fast_loop, self.app_cfg
        if "risk.daily_loss_halt_pct" in changed and hasattr(m.risk, "breaker"):
            m.risk.breaker.set_halt_pct(self.risk_cfg.daily_loss_halt_pct)
        if hasattr(m.risk, "set_record_only"):
            m.risk.set_record_only(cfg.gate_record_only)
        if "fast_loop.book_depth_levels" in changed and hasattr(m.features, "set_depth_levels"):
            m.features.set_depth_levels(fast.book_depth_levels)
        if hasattr(m.trigger, "refresh"):
            m.trigger.refresh(cfg.signal_dedup_s)
        if hasattr(m.gate, "reconfigure"):
            m.gate.reconfigure(
                max_wait_s=max(fast.signal_max_age_s - cfg.jev_timeout_ms / 1000.0, 0.0),
                questions=fast.gate_questions,
            )
        if hasattr(m.executor, "configure_frozen_peak_exit"):
            m.executor.configure_frozen_peak_exit(
                fast.frozen_peak_exit_on, fast.frozen_peak_exit_after_s
            )
        if hasattr(m.reconciler, "configure_issue_grace"):
            m.reconciler.configure_issue_grace(
                cfg.reconcile_opening_grace_s, cfg.reconcile_closing_grace_s
            )
        telegram = getattr(m.alerter, "inner", m.alerter)
        if hasattr(telegram, "set_muted"):
            telegram.set_muted(cfg.muted_alert_kinds)
        if self.credits is not None:
            self.credits.reconfigure(
                cfg.credit_low_floor_usd, timedelta(hours=cfg.credit_resend_hours)
            )

    def _write_runtime(self, now: datetime) -> None:
        """给面板对比：配置指纹不一致 → 总览提示「配置已改、未生效」。"""
        try:
            write_runtime(
                self.app_cfg.data_dir,
                started_at=now.isoformat(),
                mode=self.app_cfg.mode.value,
                pid=os.getpid(),
                # 只算必须重启才生效的那部分；热生效的项改了不算「待重启」
                config_fingerprint=restart_fingerprint(self.risk_cfg, self.app_cfg, self.slow_cfg),
                stub_roles=self.stub_roles(),
            )
        except OSError:
            log.exception("写 runtime.json 失败")

    def _push_overrides(self) -> None:
        if isinstance(self.m.screener, _TakesOverrides):
            self.m.screener.set_overrides(UserOverrides.from_file(self.pool_overrides))

    def reload_pool_overrides(self) -> bool:
        """选币前热读文件（手改也生效）。读不出沿用当前的，返回是否有变化。"""
        loaded = read_pool_overrides(self.app_cfg.data_dir)
        if loaded is None:
            return False
        self.pool_overrides_corrupt = False  # 运行中修好了：面板可以恢复开新仓
        if loaded == self.pool_overrides:
            return False
        self.pool_overrides = loaded
        self._push_overrides()
        return True

    def apply_pool_override(self, action: PoolAction, symbol: str) -> PoolOverrides:
        """面板选币页的一次操作：改内存、写文件、交给选币器。下一次选币生效（调用方立即重选）。"""
        if read_pool_overrides(self.app_cfg.data_dir) is None:
            # 文件坏了：内存里可能是空的，写回去会把原来的拉黑清单冲掉
            raise ValueError("data/pool_overrides.json 读不出，先在终端修好或删掉再操作")
        self.reload_pool_overrides()  # 先合上手改的部分，免得覆盖掉
        new = self.pool_overrides.apply(action, symbol)
        write_pool_overrides(self.app_cfg.data_dir, new)
        self.pool_overrides = new
        self._push_overrides()
        return new

    def apply_category_override(self, kind: str, params: dict[str, str]) -> PoolOverrides:
        """面板选币页的类别开关 / 每类上限：和 ``apply_pool_override`` 同样的读-改-写流程。"""
        if read_pool_overrides(self.app_cfg.data_dir) is None:
            raise ValueError("data/pool_overrides.json 读不出，先在终端修好或删掉再操作")
        category, value = parse_category_command(kind, params)
        self.reload_pool_overrides()
        if kind == "set_category":
            assert value is None or isinstance(value, bool)
            new = self.pool_overrides.set_category(category, value)
        else:
            assert value is None or isinstance(value, int)
            new = self.pool_overrides.set_category_cap(category, value)
        write_pool_overrides(self.app_cfg.data_dir, new)
        self.pool_overrides = new
        self._push_overrides()
        return new

    def set_pool(self, pool: SymbolPool) -> None:
        """换上新的候选池，并写到 data_dir 给面板选币页读。"""
        self.pool = pool
        self._write_atomic(POOL_FILE, pool.model_dump_json())

    def _write_atomic(self, name: str, text: str) -> None:
        path = Path(self.app_cfg.data_dir) / name
        try:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(path)
        except OSError:
            log.exception("写 %s 失败", name)

    async def _alert(self, level: AlertLevel, title: str, body: str = "", kind: str = "") -> None:
        try:
            await self.m.alerter.send(
                Alert(
                    ts=self.clock(),
                    level=level,
                    role=Role.ALERTING,
                    title=title,
                    body=body,
                    kind=kind,
                )
            )
        except Exception:
            log.exception("发告警失败：%s", title)

    def gate_label(self) -> str:
        return {
            "jev": "Jev",
            "rule": "规则拍板（模拟盘，不是 Jev）",
            "none": "未接拍板模型，不开新仓",
        }[self.gate_mode]

    def stub_fields(self) -> list[str]:
        return [f.name for f in fields(self.m) if is_stub(getattr(self.m, f.name))]

    def stub_roles(self) -> list[str]:
        """这次启动全由桩顶着的角色（面板团队页显示“未启用”，不当成还没报到）。"""
        roles: dict[Role, bool] = {}
        for f in fields(self.m):
            role = ROLE_OF[f.name]
            roles[role] = roles.get(role, True) and is_stub(getattr(self.m, f.name))
        return sorted(r.value for r, stub in roles.items() if stub)

    def heartbeat_all(self) -> None:
        """各角色上报一次状态；桩由 ``status`` 补齐。"""
        now = self.clock()
        for f in fields(self.m):
            module = getattr(self.m, f.name)
            if isinstance(module, _SupportsHeartbeat):
                module.heartbeat(now)
        modules_of = {
            Role.SENTINEL: self.m.trigger,
            Role.GATE: self.m.decision_model,
            Role.RISK_GUARD: self.m.risk,
            Role.KEEPER: self.m.keeper,
            Role.PATROL: self.m.patrol,
        }
        for role, rec in self.recorders.items():
            module = modules_of.get(role)
            if module is None or is_stub(module):
                continue
            health = getattr(module, "health", Health.OK)
            headline = self.gate_label() if role is Role.GATE else ""
            if role is Role.KEEPER and self.keeper_jev is not None:
                health, headline = self._keeper_jev_lamp(health, headline)
            extra: dict[str, Any] | None = None
            if isinstance(module, _SentinelDetail):  # 为什么没信号：每币一行
                headline = module.brief(now)
                extra = {
                    "detail": module.detail_lines(now, rec.tz),
                    "against_signals": self.against_desk.recent(now),
                }
            rec.heartbeat(now, health if isinstance(health, Health) else Health.OK, headline, extra)

    def _keeper_jev_lamp(self, health: object, headline: str) -> tuple[object, str]:
        """With separate cooldowns the gate lamp no longer shows the keeper's Jev errors, so the
        keeper lamp carries them; it only ever gets worse, never better, than the keeper's own."""
        jev = self.keeper_jev
        assert jev is not None
        own = jev.health
        if own is Health.OK:
            return health, headline
        if own is Health.DOWN:
            note = f"管家的 Jev 连续出错，冷却中（还剩 {jev.cooldown_left_s():.0f} 秒）"
        else:
            note = f"管家的 Jev 连续出错 {jev.consecutive_errors} 次"
        rank = {Health.OK: 0, Health.DEGRADED: 1, Health.DOWN: 2}
        if health in rank and rank[own] > rank[health]:  # type: ignore[index]
            health = own
        return health, f"{headline}；{note}" if headline else note

    def status(self) -> dict[Role, ModuleStatus]:
        """每个角色一条最新状态。没心跳的角色：桩显示“未接入”，真实现显示“启动中”。"""
        reported = {Role(k): v for k, v in self.log.read_status().items()}
        now = self.clock()
        out: dict[Role, ModuleStatus] = {}
        for f in fields(self.m):
            role = ROLE_OF[f.name]
            if role in reported or role in out:
                continue
            stub = is_stub(getattr(self.m, f.name))
            out[role] = ModuleStatus(
                role=role,
                health=Health.DISABLED if stub else Health.STARTING,
                ts=now,
                headline="未启用（离线 / 回放模式，故障即关闭）" if stub else "等待第一次上报",
            )
        out.update(reported)
        if self.gate_mode == "rule" and Role.GATE in out:
            # 规则拍板一定要在团队页和 jiaoyi status 上标出来，不能被误当成 Jev
            st = out[Role.GATE]
            if not st.headline.startswith("【规则拍板】"):
                out[Role.GATE] = st.model_copy(update={"headline": f"【规则拍板】{st.headline}"})
        return {r: out[r] for r in Role if r in out}
