"""把执行层各件按模式接起来。接线代码（jiaoyi.app）调这里，不用知道细节。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from jiaoyi.contracts import RiskConfig, RunMode
from jiaoyi.execution.exchange import CcxtBinanceFutures
from jiaoyi.execution.hb_client import HttpHummingbotApi
from jiaoyi.execution.hb_executor import HbExecutorAdapter
from jiaoyi.execution.lifeboat import BinanceLifeboat
from jiaoyi.execution.live_gate import LiveGate, LiveGateError
from jiaoyi.execution.reconciler import ThreeWayReconciler
from jiaoyi.execution.registry import PositionRegistry
from jiaoyi.interfaces import Alerter, SecretStore

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
    api: HttpHummingbotApi
    exchange: CcxtBinanceFutures

    async def close(self) -> None:
        await self.api.close()
        await self.exchange.close()


class MissingSecret(Exception):
    """缺密钥。消息里只写名字，不写值。"""


def build_execution(
    mode: RunMode,
    risk: RiskConfig,
    data_dir: str | Path,
    secrets: SecretStore,
    alerter: Alerter,
    *,
    live_gate: LiveGate | None = None,
    hb_url: str = "http://127.0.0.1:8000",
    hb_username: str = "admin",
    account_name: str = "master_account",
) -> ExecutionStack:
    if mode is RunMode.LIVE:
        # 没有门槛对象或没解锁，一律不建立实盘连接
        if live_gate is None:
            raise LiveGateError("live 模式必须提供 LiveGate")
        live_gate.require_armed()

    def need(name: str) -> str:
        value = secrets.get(name)
        if not value:
            raise MissingSecret(f"缺少密钥 {name}，先运行 jiaoyi setup-keys")
        return value

    # 测试网和主网的 key 分开存，不会因为配错模式把主网 key 用在测试网（或反过来）
    prefix = "BINANCE" if mode is RunMode.LIVE else "BINANCE_TESTNET"
    api = HttpHummingbotApi(hb_url, hb_username, need("HUMMINGBOT_API_PASSWORD"))
    exchange = CcxtBinanceFutures(
        need(f"{prefix}_API_KEY"), need(f"{prefix}_API_SECRET"), testnet=mode is not RunMode.LIVE
    )
    lifeboat = BinanceLifeboat(exchange)
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
    )
    reconciler = ThreeWayReconciler(
        adapter, api, exchange, lifeboat, registry, alerter, account_name=account_name
    )
    return ExecutionStack(adapter, lifeboat, reconciler, api, exchange)
