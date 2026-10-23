"""把执行层各件按模式接起来。接线代码（jiaoyi.app）调这里，不用知道细节。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from jiaoyi.contracts import RiskConfig, Role, RunMode
from jiaoyi.core.commands import config_fingerprint
from jiaoyi.core.recorder import RoleRecorder
from jiaoyi.execution.account_check import verify_same_account
from jiaoyi.execution.exchange import CcxtBinanceFutures
from jiaoyi.execution.hb_client import HttpHummingbotApi
from jiaoyi.execution.hb_executor import HbExecutorAdapter
from jiaoyi.execution.lifeboat import BinanceLifeboat
from jiaoyi.execution.live_gate import LiveGate, LiveGateError
from jiaoyi.execution.lot_rules import ExchangeInfoLotRules
from jiaoyi.execution.reconciler import ThreeWayReconciler
from jiaoyi.execution.registry import PositionRegistry
from jiaoyi.interfaces import Alerter, DecisionLog, SecretStore

CONNECTOR_BY_MODE: dict[RunMode, str] = {
    # hummingbot 官方没有永续合约的“纯模拟”连接器；paper 与 testnet 都走币安合约测试网，
    # 区别只在 mode 标记和面板横幅。live 才是真连接器。
    RunMode.PAPER: "binance_perpetual_testnet",
    RunMode.TESTNET: "binance_perpetual_testnet",
    RunMode.LIVE: "binance_perpetual",
}


@dataclass
class ExecutionStack:
    adapter: HbExecutorAdapter
    lifeboat: BinanceLifeboat
    reconciler: ThreeWayReconciler
    lot_rules: ExchangeInfoLotRules
    api: HttpHummingbotApi
    exchange: CcxtBinanceFutures
    account_name: str = "master_account"
    connector_name: str = ""

    async def verify_same_account(self) -> None:
        """启动时调：hummingbot 与我们的 ccxt 必须是同一个币安账户，否则抛 AccountMismatch。"""
        await verify_same_account(self.api, self.exchange, self.account_name, self.connector_name)

    async def close(self) -> None:
        await self.api.close()
        await self.exchange.close()


class MissingSecret(Exception):
    """缺密钥。消息里只写名字，不写值。"""


HB_SIM_MARKER = "hb_sim_environment"


def check_sim_environment(data_dir: str | Path, expected: str) -> None:
    """hummingbot（`make hb-up` 写标记）与我们自己的 ccxt 必须连同一个模拟环境，否则执行器和
    保命止损会分处两个环境、仓位没有保护。标记缺失或不一致都拒绝启动。"""
    marker = Path(data_dir) / HB_SIM_MARKER
    try:
        actual = marker.read_text().strip()
    except OSError as e:
        raise SimEnvironmentMismatch(
            f"找不到 {marker}：先运行 make hb-up（它会记录 hummingbot 连的模拟环境）"
        ) from e
    if actual != expected:
        raise SimEnvironmentMismatch(
            f"hummingbot 连的是 {actual}，但配置 sim_environment 是 {expected}；"
            "两边必须一致（见 docs/M0-setup.md 第 3 步）"
        )


class SimEnvironmentMismatch(RuntimeError):
    pass


def build_execution(
    mode: RunMode,
    risk: RiskConfig,
    data_dir: str | Path,
    secrets: SecretStore,
    alerter: Alerter,
    *,
    live_gate: LiveGate | None = None,
    decision_log: DecisionLog | None = None,
    close_unknown_positions: bool = False,
    demo_trading: bool = True,
    hb_url: str = "http://127.0.0.1:8000",
    hb_username: str = "admin",
    account_name: str = "master_account",
) -> ExecutionStack:
    if mode is RunMode.LIVE:
        # 没有门槛对象或没解锁，一律不建立实盘连接
        if live_gate is None:
            raise LiveGateError("live 模式必须提供 LiveGate")
        live_gate.require_armed(config_fingerprint(risk))

    def need(name: str) -> str:
        value = secrets.get(name)
        if not value:
            raise MissingSecret(f"缺少密钥 {name}，先运行 jiaoyi setup-keys")
        return value

    # 测试网和主网的 key 分开存，不会因为配错模式把主网 key 用在测试网（或反过来）
    prefix = "BINANCE" if mode is RunMode.LIVE else "BINANCE_TESTNET"
    hb_password = need("HUMMINGBOT_API_PASSWORD")
    api_key, api_secret = need(f"{prefix}_API_KEY"), need(f"{prefix}_API_SECRET")
    if mode is not RunMode.LIVE:
        check_sim_environment(data_dir, "demo" if demo_trading else "legacy_testnet")
    api = HttpHummingbotApi(hb_url, hb_username, hb_password)
    exchange = CcxtBinanceFutures(
        api_key,
        api_secret,
        testnet=mode is not RunMode.LIVE,
        demo=demo_trading,
    )
    tz = risk.day_reset_tz

    def recorder(role: Role) -> RoleRecorder:
        return RoleRecorder(role, decision_log, day_reset_tz=tz)

    lifeboat = BinanceLifeboat(exchange, recorder=recorder(Role.LIFEBOAT))
    registry = PositionRegistry(Path(data_dir) / "positions.json")
    adapter = HbExecutorAdapter(
        api,
        lifeboat,
        registry,
        alerter,
        account_name=account_name,
        connector_name=CONNECTOR_BY_MODE[mode],
        day_reset_tz=risk.day_reset_tz,
        income_since=exchange.income_since,
        exchange=exchange,
        recorder=recorder(Role.EXECUTOR),
    )
    reconciler = ThreeWayReconciler(
        adapter,
        api,
        exchange,
        lifeboat,
        registry,
        alerter,
        account_name=account_name,
        recorder=recorder(Role.RECONCILER),
        close_unknown_positions=close_unknown_positions,
        heartbeats=(adapter.heartbeat, lifeboat.heartbeat),
    )
    lot_rules = ExchangeInfoLotRules(exchange, recorder=recorder(Role.EXECUTOR))
    return ExecutionStack(
        adapter,
        lifeboat,
        reconciler,
        lot_rules,
        api,
        exchange,
        account_name=account_name,
        connector_name=CONNECTOR_BY_MODE[mode],
    )
