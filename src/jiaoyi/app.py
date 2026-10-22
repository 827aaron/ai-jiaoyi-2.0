"""组装层（composition root）：把各角色的实现接起来，管启动、关闭和模块状态汇总。

- 唯一允许同时导入所有角色包（包括 ``jiaoyi.execution``）的地方。
- 每个角色按 ``AppConfig`` 选真实现或桩；还没合并的角色一律用桩（故障即关闭）。
- 这里不写交易逻辑。行情分发和快环主循环由快环接入时加在 ``App`` 上。

启动顺序（先能保命，再能看，最后才可能开仓）::

    决策日志 → 对账员先跑一遍 → 选币 → 订阅行情 → 各模块上报一次状态

角色需要自己的启动步骤时按这个原则插进去：执行层和对账在前，产生信号的在最后。
关闭顺序相反：先断行情，不再产生新信号；执行层最后停，已有持仓仍有币安上的保命止损。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from jiaoyi.alerting.stub import StubAlerter
from jiaoyi.contracts import AppConfig, Health, ModuleStatus, RiskConfig, Role
from jiaoyi.core.store import JsonlDecisionLog
from jiaoyi.execution.stub import StubExecutorAdapter, StubLifeboatStop, StubReconciler
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
    SymbolScreener,
    Trigger,
)
from jiaoyi.keeper.stub import StubKeeper
from jiaoyi.market.stub import StubFeatureEngine, StubMarketDataFeed
from jiaoyi.patrol.stub import StubPatrol
from jiaoyi.predictor import KronosForecaster
from jiaoyi.risk.stub import StubRiskGuard
from jiaoyi.sentinel.stub import StubTrigger
from jiaoyi.slow.stub import StubBattleCardProvider, StubSymbolScreener

log = logging.getLogger(__name__)

Clock = Callable[[], datetime]


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


def build_modules(app_cfg: AppConfig, decision_log: JsonlDecisionLog, clock: Clock) -> Modules:
    """按配置选实现。新角色合并后在这里换掉对应的桩。"""
    return Modules(
        feed=StubMarketDataFeed(),
        features=StubFeatureEngine(),
        trigger=StubTrigger(),
        # 开关关闭时 KronosForecaster 不加载模型、不导入 torch，latest() 返回 None
        predictor=KronosForecaster(app_cfg.predictor_enabled, log_sink=decision_log, clock=clock),
        decision_model=StubDecisionModel(),
        gate=StubEntryGate(),
        risk=StubRiskGuard(),
        executor=StubExecutorAdapter(clock()),
        lifeboat=StubLifeboatStop(),
        keeper=StubKeeper(),
        patrol=StubPatrol(),
        reconciler=StubReconciler(),
        screener=StubSymbolScreener(),
        cards=StubBattleCardProvider(),
        alerter=StubAlerter(),
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
    ) -> None:
        self.risk_cfg = risk_cfg
        self.app_cfg = app_cfg
        self.clock = clock
        self.log = decision_log or JsonlDecisionLog(app_cfg.data_dir)
        self.m = modules or build_modules(app_cfg, self.log, clock)
        self.started = False

    async def start(self) -> None:
        """按保命优先的顺序启动。对账失败不阻止启动，但会告警（重启后先对账）。"""
        if self.started:
            return
        now = self.clock()
        report = await self.m.reconciler.reconcile(now)
        if not report.clean:
            log.warning("启动对账发现 %d 个问题", len(report.issues))
        pool = await self.m.screener.screen(now)
        await self.m.feed.subscribe([e.symbol for e in pool.in_pool])
        self.heartbeat_all()
        self.started = True
        log.info("已启动：模式 %s，%d 个角色仍是桩", self.app_cfg.mode, len(self.stub_fields()))

    async def stop(self) -> None:
        """先断行情（不再产生新信号），最后才停执行层。"""
        if not self.started:
            return
        await self.m.feed.close()
        for name in ("predictor", "keeper", "patrol", "gate", "decision_model", "cards"):
            module = getattr(self.m, name)
            if isinstance(module, _SupportsClose):
                try:
                    module.close()
                except Exception:
                    log.exception("关闭 %s 失败", name)
        self.started = False

    def stub_fields(self) -> list[str]:
        return [f.name for f in fields(self.m) if is_stub(getattr(self.m, f.name))]

    def heartbeat_all(self) -> None:
        """让会自己上报的模块各报一次；桩由 ``status`` 补齐。"""
        for f in fields(self.m):
            module = getattr(self.m, f.name)
            if isinstance(module, _SupportsHeartbeat):
                module.heartbeat()

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
        return {r: out[r] for r in Role if r in out}
