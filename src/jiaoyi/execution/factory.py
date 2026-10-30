"""把执行层各件按模式接起来。接线代码（jiaoyi.app）调这里，不用知道细节。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from jiaoyi.contracts import RiskConfig, Role, RunMode
from jiaoyi.core.commands import config_fingerprint
from jiaoyi.core.recorder import RoleRecorder
from jiaoyi.execution.account_check import verify_same_account
from jiaoyi.execution.brackets import BracketManager, LegOrder
from jiaoyi.execution.clock import Clock, utc_now
from jiaoyi.execution.exchange import CcxtBinanceFutures, WorkingType
from jiaoyi.execution.exit_fills import FILE_NAME as EXIT_FILLS_FILE
from jiaoyi.execution.exit_fills import ExitFills
from jiaoyi.execution.hb_client import HttpHummingbotApi
from jiaoyi.execution.hb_executor import (
    HOSTED_CLOSE_SLOW_ALERT_S,
    HbExecutorAdapter,
    HostedCloseMode,
)
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
    executor_stop_retry_rounds: int = 0,
    reconcile_opening_grace_s: float = 0.0,
    reconcile_closing_grace_s: float = 0.0,
    clock: Clock = utc_now,
    demo_trading: bool = True,
    limit_take_profit: bool = False,
    frozen_peak_exit_on: bool = True,
    frozen_peak_exit_after_s: int = 3600,
    protection_readback_grace_s: float = 0.0,
    protection_prearm_grace_s: float = 0.0,
    protection_unknown_submit_grace_s: float = 0.0,
    protection_stop_working_type: WorkingType = "CONTRACT_PRICE",
    protection_trailing_book_mark_max_dev_bps: float = 30.0,
    protection_trailing_stop_mark_buffer_bps: float = 5.0,
    protection_trailing_min_step_bps: float = 5.0,
    protection_trailing_book_critical_after_s: float = 300.0,
    protection_leg_order: LegOrder = "lifeboat_first",
    hosted_close_mode: HostedCloseMode = "hb_first",
    protection_tick_alert_after: int = 1,
    hb_stop_unknown_retries: int = 0,
    hosted_close_direct_when_exiting: bool = False,
    hosted_close_slow_alert_s: float = HOSTED_CLOSE_SLOW_ALERT_S,
    position_mode_cache_s: float = 0.0,
    leverage_brackets_wanted: Callable[[], bool] | None = None,
    trailing_native: Callable[[], bool] | bool = False,
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
        position_mode_cache_s=position_mode_cache_s,
    )
    tz = risk.day_reset_tz

    def recorder(role: Role) -> RoleRecorder:
        return RoleRecorder(role, decision_log, day_reset_tz=tz)

    lifeboat = BinanceLifeboat(exchange, recorder=recorder(Role.LIFEBOAT))
    lot_rules = ExchangeInfoLotRules(
        exchange, recorder=recorder(Role.EXECUTOR), brackets_wanted=leverage_brackets_wanted
    )

    def tick_size(symbol: str) -> float | None:
        rules = lot_rules.lot_rules(symbol)
        return rules.tick_size if rules is not None else None

    registry = PositionRegistry(Path(data_dir) / "positions.json")
    # 当前只批准 Demo 新仓托管。实盘和旧测试网不因更新代码切换出场策略。
    protection = (
        BracketManager(
            exchange,
            registry,
            account_name=account_name,
            connector_name=CONNECTOR_BY_MODE[mode],
            readback_grace_s=protection_readback_grace_s,
            prearm_grace_s=protection_prearm_grace_s,
            unknown_submit_grace_s=protection_unknown_submit_grace_s,
            stop_working_type=protection_stop_working_type,
            trailing_book_mark_max_dev_bps=protection_trailing_book_mark_max_dev_bps,
            trailing_stop_mark_buffer_bps=protection_trailing_stop_mark_buffer_bps,
            trailing_min_step_bps=protection_trailing_min_step_bps,
            trailing_book_critical_after_s=protection_trailing_book_critical_after_s,
            leg_order=protection_leg_order,
            native_trailing=trailing_native,
        )
        if mode is not RunMode.LIVE and demo_trading
        else None
    )
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
        open_fee_bps=risk.taker_fee_bps,
        limit_take_profit=limit_take_profit,
        tick_size=tick_size,
        protection=protection,
        frozen_peak_exit_on=frozen_peak_exit_on and mode is not RunMode.LIVE and demo_trading,
        frozen_peak_exit_after_s=frozen_peak_exit_after_s,
        hosted_close_mode=hosted_close_mode,
        protection_tick_alert_after=protection_tick_alert_after,
        hb_stop_unknown_retries=hb_stop_unknown_retries,
        hosted_close_direct_when_exiting=hosted_close_direct_when_exiting,
        hosted_close_slow_alert_s=hosted_close_slow_alert_s,
        exit_fills=ExitFills(Path(data_dir) / EXIT_FILLS_FILE),
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
        executor_stop_retry_rounds=executor_stop_retry_rounds,
        opening_grace_s=reconcile_opening_grace_s,
        closing_grace_s=reconcile_closing_grace_s,
        heartbeats=(adapter.heartbeat, lifeboat.heartbeat),
        clock=clock,
    )
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
