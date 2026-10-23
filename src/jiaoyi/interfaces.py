"""模块边界：每个角色对外只暴露这里定义的接口。

- 每个接口在对应角色包里有一个 ``stub.py`` 桩实现，行为一律“故障即关闭”：
  拍板人桩说不做、保镖桩全拒、管家桩维持现状、执行器桩不碰交易所。
- 真实现写在同一个包的其它文件里，实现同一个接口，然后在 ``jiaoyi.app`` 里接线。
- 只有 ``ExecutorAdapter`` 和 ``LifeboatStop`` 会动交易所，且 ``open`` 只接受保镖签发的
  ``OrderIntent``。AI 角色（拍板人、管家、巡逻兵的 Jev 部分、军师团、预测员）只输出判断，
  不得导入 ``jiaoyi.execution``（tests/test_boundaries.py 会检查）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable

from jiaoyi.contracts import (
    AccountState,
    Alert,
    BattleCard,
    CandidateSignal,
    DecisionRecord,
    FeatureSnapshot,
    Forecast,
    JevRequest,
    JevResponse,
    KeeperVerdict,
    Kline,
    LifeboatOrder,
    MarketEvent,
    ModuleStatus,
    OrderIntent,
    PatrolAction,
    PositionRecord,
    ReconcileReport,
    RiskVerdict,
    SymbolPool,
)
from jiaoyi.contracts.position import CloseReason

# ---------------------------------------------------------------- 快环：行情 → 信号


@runtime_checkable
class MarketDataFeed(Protocol):
    """行情接入（ccxt WebSocket）。负责断线重连，重连期间上报 degraded。"""

    async def subscribe(self, symbols: Sequence[str]) -> None:
        """替换订阅列表（候选池变了就调一次）。"""
        ...

    def events(self) -> AsyncIterator[MarketEvent]:
        """按到达顺序产出行情事件。"""
        ...

    async def close(self) -> None: ...


@runtime_checkable
class FeatureEngine(Protocol):
    """特征引擎 + 盘口员公式：增量更新，每个标的一份状态。"""

    def update(self, event: MarketEvent) -> None: ...

    def snapshot(self, symbol: str) -> FeatureSnapshot | None:
        """数据还不够（例如刚订阅）时返回 None。"""
        ...


@runtime_checkable
class Trigger(Protocol):
    """哨兵：特征 + 作战卡 → 候选信号。负责去重和冷却。没有有效作战卡就不出信号。"""

    def check(
        self, features: FeatureSnapshot, card: BattleCard | None, now: datetime
    ) -> list[CandidateSignal]: ...


@runtime_checkable
class Predictor(Protocol):
    """预测员（Kronos）。关掉、算不出来或过期都返回 None，快环照常跑。"""

    @property
    def enabled(self) -> bool: ...

    async def on_kline(self, kline: Kline) -> None:
        """K 线收盘后在后台算，不占交易延迟。"""
        ...

    def latest(self, symbol: str, now: datetime) -> Forecast | None: ...


# ---------------------------------------------------------------- 快环：判断 → 风控


@runtime_checkable
class DecisionModel(Protocol):
    """快速决策模型（Jev API，以后可换本地模型）。拍板人和持仓管家共用。

    超时、报错时不抛异常，返回 ``ok=False`` 的 ``JevResponse``。
    """

    async def ask(self, request: JevRequest) -> JevResponse: ...


@runtime_checkable
class EntryGate(Protocol):
    """拍板人：把候选信号打包成开仓 6 问，问决策模型，返回原始回复。

    不决定数量，不下单；“过不过门槛”由保镖按配置判断。
    """

    async def evaluate(
        self, signal: CandidateSignal, card: BattleCard, now: datetime
    ) -> JevResponse: ...


@runtime_checkable
class RiskGuard(Protocol):
    """保镖：唯一能签发 ``OrderIntent`` 的地方。

    查硬上限、作战卡方向和范围、相关组敞口、日亏熔断、价差和手续费门槛；
    按“风险金额 ÷ 止损距离”算数量。``jev`` 为 ``ok=False`` 时必须拒绝。

    ``market`` 是拍板回来、拿到开仓锁之后重新取的行情快照（``features.snapshot(symbol)``）：
    滑点、价差、入场价都按它算。没有（``None``）就拒绝。
    """

    def review(
        self,
        signal: CandidateSignal,
        jev: JevResponse,
        card: BattleCard | None,
        account: AccountState,
        now: datetime,
        *,
        market: FeatureSnapshot | None,
    ) -> RiskVerdict: ...


# ---------------------------------------------------------------- 执行（只有这里碰交易所）


@runtime_checkable
class ExecutorAdapter(Protocol):
    """hummingbot-api 开仓执行器的适配层。"""

    async def open(self, intent: OrderIntent) -> PositionRecord:
        """建执行器。只接受保镖签发的 ``OrderIntent``。"""
        ...

    async def close(self, position_id: str, reason: CloseReason) -> None:
        """停执行器并市价全平。"""
        ...

    async def positions(self) -> list[PositionRecord]:
        """当前由执行器管理的持仓。"""
        ...

    async def account(self) -> AccountState: ...


@runtime_checkable
class LifeboatStop(Protocol):
    """保命止损：开仓成交后在币安挂一张更宽的只减仓 STOP_MARKET，平仓后撤掉。"""

    async def place(self, position: PositionRecord, stop_price: float) -> LifeboatOrder: ...

    async def cancel(self, position: PositionRecord) -> None: ...

    async def fetch(self, position: PositionRecord) -> LifeboatOrder | None:
        """查币安上这张单的现状；找不到返回 None。"""
        ...


# ---------------------------------------------------------------- 盯仓（只能更安全）


@runtime_checkable
class Keeper(Protocol):
    """持仓管家：每 15–30 秒判断拿住还是全平。Jev 失败 → hold。"""

    async def review(self, position: PositionRecord, now: datetime) -> KeeperVerdict: ...


@runtime_checkable
class Patrol(Protocol):
    """巡逻兵：盯暴涨暴跌和异常行情，只会要求平仓。"""

    def check(
        self, event: MarketEvent, positions: Sequence[PositionRecord]
    ) -> PatrolAction | None: ...


@runtime_checkable
class Reconciler(Protocol):
    """对账员：执行器、币安持仓、保命止损三方核对；缺止损立即补挂并告警。重启后先跑一次。"""

    async def reconcile(self, now: datetime) -> ReconcileReport: ...


# ---------------------------------------------------------------- 慢环


@runtime_checkable
class SymbolScreener(Protocol):
    """选币：每小时代码筛候选池，不用 AI。你的固定 / 拉黑 / 手动加入优先。"""

    async def screen(self, now: datetime) -> SymbolPool: ...


@runtime_checkable
class BattleCardProvider(Protocol):
    """军师团（TradingAgents + DeepSeek）。快环只调 ``current``，不等开会。"""

    def current(self, now: datetime) -> BattleCard | None:
        """最新一张有效的卡；没有或已过期返回 None。"""
        ...

    async def refresh(self, pool: SymbolPool, reason: str) -> BattleCard:
        """开会出新卡。可能要 30 秒到几分钟，在后台跑。"""
        ...


# ---------------------------------------------------------------- 记录与告警


@runtime_checkable
class DecisionLog(Protocol):
    """决策日志与模块状态的落盘，团队页 / 历史页直接读。"""

    def write(self, record: DecisionRecord) -> None: ...

    def heartbeat(self, status: ModuleStatus) -> None: ...


@runtime_checkable
class Alerter(Protocol):
    """告警（Telegram）。发送失败只记日志，不能拖住交易回路。"""

    async def send(self, alert: Alert) -> None: ...


@runtime_checkable
class SecretStore(Protocol):
    """读密钥：正式环境 macOS 钥匙串（setup-keys 写入），开发时环境变量。"""

    def get(self, name: str) -> str | None: ...
