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

import importlib.util
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from jiaoyi.alerting.log import LogAlerter
from jiaoyi.alerting.stub import StubAlerter
from jiaoyi.alerting.telegram import TelegramAlerter
from jiaoyi.contracts import (
    Alert,
    AlertLevel,
    AppConfig,
    Health,
    ModuleStatus,
    RiskConfig,
    Role,
    RunMode,
    SymbolPool,
)
from jiaoyi.core.recorder import RoleRecorder
from jiaoyi.core.secrets import ChainedSecretStore
from jiaoyi.core.store import JsonlDecisionLog
from jiaoyi.execution.factory import MissingSecret, build_execution
from jiaoyi.execution.live_gate import LiveGate
from jiaoyi.execution.stub import (
    StubExecutorAdapter,
    StubLifeboatStop,
    StubLotRules,
    StubReconciler,
)
from jiaoyi.gate.entry import JevEntryGate
from jiaoyi.gate.jev import decision_model_from_config
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
from jiaoyi.market.features import RollingFeatureEngine
from jiaoyi.market.stub import StubMarketDataFeed
from jiaoyi.patrol.rules import RulePatrol
from jiaoyi.predictor import KronosForecaster
from jiaoyi.risk.guard import LotRulesProvider, RiskGuardImpl
from jiaoyi.sentinel.rules import RuleTrigger
from jiaoyi.slow.binance_stats import BinanceStatsSource
from jiaoyi.slow.brief import MarketBriefService
from jiaoyi.slow.commander import BattleCardCommander
from jiaoyi.slow.liquidations import LiquidationSource
from jiaoyi.slow.news import NewsHub, default_sources
from jiaoyi.slow.review import CardJournal
from jiaoyi.slow.screener import CodeSymbolScreener, ScreenerConfig
from jiaoyi.slow.settings import SlowConfig
from jiaoyi.slow.stub import StubBattleCardProvider, StubSymbolScreener
from jiaoyi.slow.tradingagents_engine import TradingAgentsEngine

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
class _Refreshable(Protocol):
    async def refresh(self) -> bool: ...


@runtime_checkable
class _SupportsClose(Protocol):
    """同步 ``close()``，例如预测员关线程池。"""

    def close(self) -> None: ...


@runtime_checkable
class _SupportsHeartbeat(Protocol):
    """模块自己上报 ``ModuleStatus``。"""

    def heartbeat(self) -> None: ...


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
    "feed": Role.SENTINEL,
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
) -> tuple[BattleCardProvider, NewsHub | None]:
    """军师团：有 DeepSeek key 且装了 ``jiaoyi[slow]`` 才用真的，否则用桩（没卡 = 不开新仓）。

    新闻源只服务军师团（开会素材、大消息触发、下架封禁），军师团是桩时不拉新闻。
    """
    if not secrets.get("DEEPSEEK_API_KEY"):
        log.warning("没有 DEEPSEEK_API_KEY，军师团用桩：不出作战卡，快环不会开新仓")
        return StubBattleCardProvider(), None
    if importlib.util.find_spec("tradingagents") is None:
        log.warning("没装 tradingagents（uv pip install -e '.[slow]'），军师团用桩")
        return StubBattleCardProvider(), None
    cfg = slow_cfg or SlowConfig()  # 没有 config/slow.toml 就用默认值，行为不变
    news = NewsHub(default_sources(), decision_log, clock=clock)
    closers.append(news.aclose)
    # 市场简报每轮算一次，总盘会和各币会议共用
    brief = MarketBriefService(clock=clock)
    closers.append(brief.aclose)
    engine = TradingAgentsEngine(
        risk_cfg,
        secrets,
        settings=cfg.engine_settings(),
        today=clock,
        news=news,
        liquidations=liquidations,
        brief=brief,
    )
    commander = BattleCardCommander(
        engine,
        risk_cfg,
        decision_log,
        pick_max=app_cfg.card_pick_max,
        clock=clock,
        brief=brief,
        # 每次真开会的标的追加到 data/card_review/cards.jsonl，供复盘脚本打分（只读，不调参）
        journal=CardJournal(app_cfg.data_dir),
        **cfg.commander_kwargs(),
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


def _build_alerter(secrets: SecretStore, closers: list[Closer]) -> Alerter:
    """有 Telegram 密钥就发 Telegram，否则打到日志里。"""
    token, chat = secrets.get("TELEGRAM_BOT_TOKEN"), secrets.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        log.warning("没配 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID，告警只写日志")
        return LogAlerter()
    telegram = TelegramAlerter(token, chat)
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
) -> tuple[ExecutorAdapter, LifeboatStop, Reconciler, LotRulesProvider]:
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
            demo_trading=app_cfg.sim_environment == "demo",
        )
    except MissingSecret:
        if mode is RunMode.LIVE:
            raise
        log.warning("执行层缺密钥，%s 模式下不开仓", mode.value, exc_info=True)
        return StubExecutorAdapter(clock()), StubLifeboatStop(), StubReconciler(), StubLotRules()
    closers.append(stack.close)
    return stack.adapter, stack.lifeboat, stack.reconciler, stack.lot_rules


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
    recorders = {
        role: RoleRecorder(role, decision_log, day_reset_tz=tz)
        for role in (
            Role.SENTINEL,
            Role.GATE,
            Role.RISK_GUARD,
            Role.KEEPER,
            Role.PATROL,
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
        bfeed = BinanceFuturesFeed()
        feed = bfeed
        # 选币只读币安公开接口，不需要 key
        # 强平统计由行情层的全市场强平流产出，选币和军师团共用，不另开连接
        source = BinanceStatsSource(liquidations=bfeed.liquidations)
        closers.append(source.aclose)
        screener = CodeSymbolScreener(
            source,
            ScreenerConfig(
                pool_size=app_cfg.pool_size,
                # 价差和手续费门槛跟硬上限一致，免得进池的币保镖一律拒
                max_spread_bps=risk_cfg.max_spread_bps,
                round_trip_fee_bps=risk_cfg.taker_fee_bps * 2,
            ),
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
        )

    alerter: Alerter
    executor: ExecutorAdapter
    lifeboat: LifeboatStop
    reconciler: Reconciler
    lot_rules: LotRulesProvider
    if offline:
        alerter = StubAlerter()
        executor, lifeboat, reconciler, lot_rules = (
            StubExecutorAdapter(clock()),
            StubLifeboatStop(),
            StubReconciler(),
            StubLotRules(),
        )
    else:
        alerter = _build_alerter(secrets, closers)
        executor, lifeboat, reconciler, lot_rules = _build_execution(
            risk_cfg, app_cfg, secrets, alerter, clock, closers, decision_log
        )

    predictor = KronosForecaster(app_cfg.predictor_enabled, log_sink=decision_log, clock=clock)
    features = RollingFeatureEngine()

    # 拍板模型是启动时的一次性选择：有 Jev 用 Jev；模拟盘且明确打开才用规则版；否则不开新仓。
    # Jev 运行中出错只返回 ok=False，绝不切到规则版。
    model = decision_model_from_config(
        secrets, fast, app_cfg.mode, timeout_ms=app_cfg.jev_timeout_ms, day_reset_tz=tz
    )
    decision_model: DecisionModel
    gate: EntryGate
    keeper: Keeper
    if model is None:
        gate_mode = "none"
        log.warning("没有可用的拍板模型（缺 JEV_API_KEY 或没锁 jev_model），不开新仓")
        decision_model, gate, keeper = StubDecisionModel(), StubEntryGate(), StubKeeper()
    else:
        gate_mode = "rule" if isinstance(model, RuleDecisionModel) else "jev"
        decision_model = model
        gate = JevEntryGate(model, recorders[Role.GATE])
        keeper = JevKeeper(model, fast, features.snapshot, cards.current, recorders[Role.KEEPER])

    modules = Modules(
        feed=feed,
        features=features,
        trigger=RuleTrigger(
            fast,
            dedup_s=app_cfg.signal_dedup_s,
            recorder=recorders[Role.SENTINEL],
            predictor=predictor,
        ),
        predictor=predictor,
        decision_model=decision_model,
        gate=gate,
        risk=RiskGuardImpl(
            risk_cfg,
            fast,
            # 交易所精度由执行层从 exchangeInfo 读；拿不到（没刷新成功、过期、下架）保镖一律拒单
            lot_rules,
            recorders[Role.RISK_GUARD],
            # 日亏熔断状态落盘，重启后当天仍锁住
            state_dir=app_cfg.data_dir,
        ),
        executor=executor,
        lifeboat=lifeboat,
        keeper=keeper,
        patrol=RulePatrol(fast, recorders[Role.PATROL]),
        reconciler=reconciler,
        screener=screener,
        cards=cards,
        alerter=alerter,
    )
    return Built(modules, closers, recorders, gate_mode, lot_rules, news)


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
    ) -> None:
        self.risk_cfg = risk_cfg
        self.app_cfg = app_cfg
        self.clock = clock
        self.log = decision_log or JsonlDecisionLog(app_cfg.data_dir)
        self._closers: list[Closer] = []
        self.recorders: dict[Role, RoleRecorder] = {}
        self.gate_mode = "none"
        self.lot_rules: LotRulesProvider = StubLotRules()
        self.news: NewsHub | None = None
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
            self.recorders, self.gate_mode = built.recorders, built.gate_mode
            self.lot_rules = built.lot_rules
            self.news = built.news
        self.m = modules
        self.pool: SymbolPool | None = None
        self.started = False

    async def start(self) -> None:
        """按保命优先的顺序启动。对账失败不阻止启动，但会告警（重启后先对账）。"""
        if self.started:
            return
        now = self.clock()
        try:
            report = await self.m.reconciler.reconcile(now)
            if not report.clean:
                log.warning("启动对账发现 %d 个问题", len(report.issues))
        except Exception:
            # 对账员已发告警；照常启动，主循环里每轮再对
            log.exception("启动对账失败")
        await self.refresh_lot_rules()
        try:
            self.set_pool(await self.m.screener.screen(now))
        except Exception:
            # 选币失败不阻止启动：没有池子就不订阅、军师不开会，快环自然不开新仓
            log.exception("启动选币失败，暂不订阅行情，下一轮选币再试")
        if self.pool is not None:
            await self.m.feed.subscribe([e.symbol for e in self.pool.in_pool])
        self.save_blocked()  # 封禁只在内存里，重启后清掉旧文件，由新闻源重新补上
        self.heartbeat_all()
        self.started = True
        if self.app_cfg.mode == RunMode.LIVE:
            # 实盘启动一定要通知（设计文档 §十）；能走到这里说明上线确认已经通过
            await self._alert(AlertLevel.CRITICAL, "实盘已启动", f"拍板：{self.gate_label()}")
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
        for closer in self._closers:
            try:
                await closer()
            except Exception:
                log.exception("释放资源失败")
        self._closers = []
        self.started = False

    async def refresh_lot_rules(self) -> None:
        """拉一次交易所精度。失败沿用旧缓存，过期后保镖自动拒单。"""
        if isinstance(self.lot_rules, _Refreshable) and not await self.lot_rules.refresh():
            log.warning("下单精度刷新失败")

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

    async def _alert(self, level: AlertLevel, title: str, body: str = "") -> None:
        try:
            await self.m.alerter.send(
                Alert(ts=self.clock(), level=level, role=Role.ALERTING, title=title, body=body)
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

    def heartbeat_all(self) -> None:
        """各角色上报一次状态；桩由 ``status`` 补齐。"""
        for f in fields(self.m):
            module = getattr(self.m, f.name)
            if isinstance(module, _SupportsHeartbeat):
                module.heartbeat()
        now = self.clock()
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
            rec.heartbeat(now, health if isinstance(health, Health) else Health.OK, headline)

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
                headline="未接入（桩，故障即关闭）" if stub else "已接入，等待第一次心跳",
            )
        out.update(reported)
        if self.gate_mode == "rule" and Role.GATE in out:
            # 规则拍板一定要在团队页和 jiaoyi status 上标出来，不能被误当成 Jev
            st = out[Role.GATE]
            if not st.headline.startswith("【规则拍板】"):
                out[Role.GATE] = st.model_copy(update={"headline": f"【规则拍板】{st.headline}"})
        return {r: out[r] for r in Role if r in out}
