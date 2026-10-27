"""``ExecutorAdapter`` 的 hummingbot-api 实现：一笔持仓 = 一个 position executor + 一张保命止损。

流程：
  open   建执行器（开仓 + 止损 / 止盈 / 时间限制，由 hummingbot 每秒盯）
         → 等成交 → 立刻在币安挂保命止损；挂不上就把刚开的仓平掉（故障即关闭）
  close  停执行器并市价全平 → 确认执行器结束后才撤保命止损
  sync   （positions() 每次都会跑）执行器自己结束了（止损、止盈、时间到）就记账并撤保命止损

这里是全项目唯一建 / 停执行器的地方，只接受风控员签发的 ``OrderIntent``；出场永远不经过 AI。
"""

from __future__ import annotations

import asyncio
import logging
import math
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any, Literal, Protocol
from zoneinfo import ZoneInfo

from jiaoyi.contracts import (
    AccountState,
    Alert,
    LifeboatOrder,
    OrderIntent,
    Outcome,
    PositionRecord,
    Role,
    Side,
)
from jiaoyi.contracts.module_status import AlertLevel, Health
from jiaoyi.contracts.position import CloseReason, LifeboatStatus, PositionStatus
from jiaoyi.core.recorder import RoleRecorder
from jiaoyi.execution.brackets import BracketManager, ProtectionIncomplete
from jiaoyi.execution.clock import Clock, utc_now
from jiaoyi.execution.exchange import (
    CloseFill,
    EntryBasis,
    ExchangeClient,
    ExchangeEntryOrder,
    ExchangeError,
    ExchangePosition,
    IdentifiedCloseOrder,
    IdentifiedCloseRejected,
    IdentifiedCloseUnknown,
)
from jiaoyi.execution.fills import (
    LOOKBACK,
    OPEN_SLACK,
    TradeFill,
    UserTrades,
    entry_fill,
    exit_fill,
    no_open_fill,
)
from jiaoyi.execution.frozen_peak import FrozenPeakExit, frozen_peak_update
from jiaoyi.execution.hb_client import HbApiError, HummingbotApi, executor_query_state
from jiaoyi.execution.lifeboat import lifeboat_price
from jiaoyi.execution.registry import DayStart, PositionRegistry, TrackedPosition
from jiaoyi.execution.symbols import to_hb_pair
from jiaoyi.interfaces import Alerter, LifeboatStop

log = logging.getLogger(__name__)

# hummingbot 的 OrderType 枚举值
_ORDER_MARKET = 1
_ORDER_LIMIT = 2
_ORDER_LIMIT_MAKER = 3
"""hummingbot OrderType.LIMIT_MAKER：只挂单（post-only），挂单等回踩进场用。"""

MARKER_PREFIX = "jy-"
"""写进 hummingbot ``controller_id`` 的认领标记前缀：``jy-<position_id>``。"""
DAY_START_TOLERANCE_S = 300.0

IncomeSince = Callable[[datetime], Awaitable[float]]


class ChaseRequest(Protocol):
    """挂单没成交要改市价追进：交回主循环（拿开仓锁、按此刻状态复核，过了再调 ``open_chase``）。"""

    def __call__(self, intent: OrderIntent, /) -> None: ...


_CLOSE_TYPE_MAP = {
    "STOP_LOSS": CloseReason.STOP_LOSS,
    "TAKE_PROFIT": CloseReason.TAKE_PROFIT,
    "TRAILING_STOP": CloseReason.TRAILING_STOP,
    "TIME_LIMIT": CloseReason.TIME_LIMIT,
    "EXPIRED": CloseReason.TIME_LIMIT,
}

_NOT_FILLED_CN = {
    "INSUFFICIENT_BALANCE": "保证金不够，hummingbot 没下单（INSUFFICIENT_BALANCE）",
    "FAILED": "下单失败（FAILED）",
    "EARLY_STOP": "开仓前被停掉（EARLY_STOP）",
}


class SymbolAlreadyHeld(RuntimeError):
    """同一个币已有在管持仓，执行层拒绝再开。"""


_UNKNOWN_EXECUTOR_ROUNDS_BEFORE_SETTLE = 2
PHANTOM_SETTLE = timedelta(minutes=5)
"""「手动平」的仓平掉这么久后，币安上还查不到一笔开仓成交，才改记为没开成（成交明细有延迟）。"""
"""平仓中（CLOSING）的仓，连续这么多次同步都查不到执行器，就按币安持仓结账。"""

_TRADE_BUY = 1
_TRADE_SELL = 2


FROZEN_PEAK_EVIDENCE_TIMEOUT_S = 12.0

RELAY_LIFEBOAT_BP = 2.0
"""真实入场价改了以后，保命止损价差超过这么多（万分之几）才撤旧挂新。"""
_SIDE_CN = {Side.LONG: "多", Side.SHORT: "空"}
_REASON_CN = {
    CloseReason.STOP_LOSS: "止损",
    CloseReason.TAKE_PROFIT: "止盈",
    CloseReason.TRAILING_STOP: "保本 / 移动止损",
    CloseReason.TIME_LIMIT: "到时平仓",
    CloseReason.KEEPER: "管家平仓",
    CloseReason.PATROL: "巡逻兵平仓",
    CloseReason.LIFEBOAT: "保命止损",
    CloseReason.MANUAL: "手动 / 外部平仓",
    CloseReason.RECONCILER: "对账员平仓",
}


def _hold_text(seconds: float) -> str:
    m = int(seconds // 60)
    return f"{m // 60} 小时 {m % 60} 分" if m >= 60 else f"{m} 分钟"


def _open_text(tracked: TrackedPosition) -> str:
    """开仓成交的推送：币种 / 方向 / 名义 / 杠杆 / 入场价 / 止损 / 保命止损。"""
    r = tracked.record
    entry = r.entry_price or 0.0
    lines = [
        f"{r.symbol} 做{_SIDE_CN[r.side]}，名义 {entry * r.quantity:.0f} U，杠杆 {r.leverage} 倍",
        f"入场价 {entry:g}，数量 {r.quantity:g}",
        f"止损 {r.stop_loss_price:.6g}（距离 {tracked.stop_loss_pct:.2f}%），"
        f"止盈 {r.take_profit_price:.6g}",
    ]
    if r.lifeboat is not None:
        lines.append(f"保命止损 {r.lifeboat.stop_price:.6g}（已挂在币安）")
    else:
        lines.append("保命止损还没挂上，对账员会补挂")
    return "\n".join(lines)


def _close_text(r: PositionRecord) -> str:
    """平仓完成的推送：原因、毛利 / 手续费 / 净利、持仓时长。"""
    reason = _REASON_CN.get(r.close_reason, "未知") if r.close_reason else "未知"
    lines = [f"{r.symbol} 做{_SIDE_CN[r.side]}，{reason}"]
    if r.exit_price is not None:
        lines.append(f"入场 {r.entry_price:g} → 出场 {r.exit_price:g}")
    net = r.realized_pnl_usdt
    if net is None:
        lines.append("盈亏：成交价没拿到，稍后按币安成交回填")
    else:
        gross = net + r.fees_usdt
        lines.append(f"毛利 {gross:+.2f} U，手续费 {r.fees_usdt:.2f} U，净利 {net:+.2f} U")
    if r.closed_at is not None:
        lines.append(f"持仓 {_hold_text((r.closed_at - r.opened_at).total_seconds())}")
    return "\n".join(lines)


def _lifeboat_hit(rec: PositionRecord, exit_price: float) -> bool:
    """出场价到了保命止损价（离它不到入场→保命止损距离的 10%）就算保命止损触发。"""
    lb, entry = rec.lifeboat, rec.entry_price
    if lb is None or entry is None:
        return False
    dist = abs(entry - lb.stop_price)
    if dist <= 0:
        return False
    beyond = (lb.stop_price - exit_price) if rec.side is Side.LONG else (exit_price - lb.stop_price)
    return beyond >= -0.1 * dist


def _positive(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def filled_entry(detail: dict[str, Any]) -> float | None:
    """执行器的开仓单真成交了才返回入场价，否则 None。

    ``custom_info.current_position_average_price`` 不能单独当成交：开仓单没成交时它是计划价 /
    当前价。保证金不够时 hummingbot 根本不下单（INSUFFICIENT_BALANCE「Not enough budget」，
    ``order_ids`` 为空），我们却按它记了开仓、挂了保命止损、推了「开仓成交」（BTC / ENA 4 笔）。
    要求：下过单（``order_ids`` 非空），且开仓有成交
    （``is_trading`` 或 ``filled_amount_quote`` > 0）。
    """
    info = detail.get("custom_info") or {}
    entry = _positive(info.get("current_position_average_price"))
    if entry is None:
        return None
    ids = info.get("order_ids")
    if isinstance(ids, list) and not ids:
        return None
    reported = "is_trading" in detail or "filled_amount_quote" in detail
    traded = detail.get("is_trading") or _positive(detail.get("filled_amount_quote")) is not None
    return entry if traded or not reported else None


def _never_submitted(tracked: TrackedPosition, detail: dict[str, Any]) -> bool:
    """HB 启动预算校验在控制循环下单前退出；空订单列表本身不能证明没下过单。

    只认目标 HB 的这一条明确路径，不把 SYSTEM_CLEANUP、404、未知字段或历史成交抹成失败。
    调用方仍须持退出锁并核对两次交易所零仓；这里不授权撤保护或发任何订单。
    """
    rec = tracked.record
    if (
        rec.executor_id is None
        or executor_query_state(detail, rec.executor_id) != "terminal"
        or detail.get("controller_id") != f"{MARKER_PREFIX}{rec.position_id}"
        or detail.get("trading_pair") != to_hb_pair(rec.symbol)
        or str(detail.get("close_type") or "").upper().removeprefix("CLOSETYPE.")
        != "INSUFFICIENT_BALANCE"
        or detail.get("is_trading") is not False
        or rec.status is not PositionStatus.OPENING
        or rec.entry_price is not None
        or rec.intended_price is not None
        or rec.exit_price is not None
        or rec.closed_at is not None
        or rec.close_reason is not None
        or rec.lifeboat is not None
        or rec.realized_pnl_usdt is not None
        or rec.fees_usdt != 0
        or tracked.filled_at is not None
        or tracked.entry_client_order_id is not None
        or tracked.entry_original_quantity is not None
        or tracked.requested_close is not None
        or tracked.exit_cleanup_pending
        or tracked.hb_close_pending
        or tracked.hb_close_unknown
        or tracked.direct_close_pending
        or tracked.direct_close_generation != 0
        or any(
            value is not None
            for value in (
                tracked.direct_close_client_order_id,
                tracked.direct_close_order_id,
                tracked.direct_close_order_status,
                tracked.direct_close_quantity,
                tracked.direct_close_price,
                tracked.direct_close_fee,
            )
        )
    ):
        return False
    info = detail.get("custom_info")
    if (
        not isinstance(info, dict)
        or info.get("order_ids") != []
        or "open_order_last_update" not in info
        or info["open_order_last_update"] is not None
        or info.get("held_position_orders") != []
    ):
        return False
    # HB 的 JSON 数值可能序列化为字符串；不接受布尔、缺项、NaN 或带费用的矛盾记录。
    for field in ("filled_amount_quote", "cum_fees_quote", "net_pnl_quote"):
        value = detail.get(field)
        if not isinstance(value, (str, int, float, Decimal)) or isinstance(value, bool):
            return False
        try:
            number = Decimal(str(value))
        except ArithmeticError:
            return False
        if not number.is_finite() or number != 0:
            return False
    return True


def tp_price(tracked: TrackedPosition, entry: float) -> float:
    """账本里的止盈价。限价止盈 = 币安上那张限价单的价格（按 tickSize 往有利方向取整：多单卖出
    向下、空单买入向上，和 ``_executor_config`` 的挂法一致），快环判「穿过止盈价没成交」也按它。"""
    side = tracked.record.side
    sign = 1.0 if side is Side.LONG else -1.0
    raw = entry * (1.0 + sign * tracked.take_profit_pct / 100.0)
    tick = tracked.tp_tick
    if not tick:
        return raw
    q = Decimal(str(tick))
    steps = Decimal(str(raw)) / q
    steps = steps.to_integral_value(ROUND_FLOOR if side is Side.LONG else ROUND_CEILING)
    return float(steps * q)


class HbExecutorAdapter:
    def __init__(
        self,
        api: HummingbotApi,
        lifeboat: LifeboatStop,
        registry: PositionRegistry,
        alerter: Alerter,
        *,
        account_name: str,
        connector_name: str,
        day_reset_tz: str = "Asia/Bangkok",
        fill_timeout_s: float = 15.0,
        poll_s: float = 0.5,
        lifeboat_attempts: int = 3,
        income_since: IncomeSince | None = None,
        exchange: ExchangeClient | None = None,
        recorder: RoleRecorder | None = None,
        clock: Clock = utc_now,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        open_fee_bps: float = 0.0,
        limit_take_profit: bool = False,
        tick_size: Callable[[str], float | None] | None = None,
        chase_request: ChaseRequest | None = None,
        protection: BracketManager | None = None,
        frozen_peak_exit_on: bool = False,
        frozen_peak_exit_after_s: int = 3600,
    ) -> None:
        """``tick_size``：合约的价格最小变动（限价止盈取整用），拿不到返回 None。

        ``open_fee_bps``：开仓手续费（单边吃单费）。hummingbot 按「扣了开仓手续费的净盈亏」
        触发止损止盈，传过去时补上这一截，让实际触发价离开仓价的距离 = 风控算的止损 / 止盈距离。"""
        self._chase_request = chase_request
        self._protection = protection
        self._frozen_peak_on = frozen_peak_exit_on
        self._frozen_peak_after_s = frozen_peak_exit_after_s
        self._api = api
        self._open_fee = open_fee_bps / 1e4
        self._lifeboat = lifeboat
        self._registry = registry
        self._alerter = alerter
        self._account = account_name
        self._connector = connector_name
        self._tz = ZoneInfo(day_reset_tz)
        self._fill_timeout_s = fill_timeout_s
        self._poll_s = poll_s
        self._lifeboat_attempts = lifeboat_attempts
        self._income_since = income_since
        self._unknown_executor_rounds: dict[str, int] = {}
        self._finalizing: set[str] = set()
        self._direct_locks: dict[str, asyncio.Lock] = {}
        self._exit_owners: dict[str, asyncio.Task[Any] | None] = {}
        self._limit_tp = limit_take_profit
        self._tick_size = tick_size or (lambda _s: None)
        self.fills_note: str | None = None
        """成交回报通道的状态（「成交回报实时」/「只走 60 秒轮询」），由 LedgerSync 写。"""
        self._exchange = exchange
        self._rec = recorder
        self._clock = clock
        self._sleep = sleep

    # ------------------------------------------------------------------ 开仓

    def _executor_config(self, intent: OrderIntent, *, market: bool = False) -> dict[str, Any]:
        """``market``：挂单等回踩没成交、改市价追进时用同一份指令下市价单。"""
        limit = intent.order_type == "limit" and not market
        # hummingbot 的止损止盈看 net_pnl_pct（已扣开仓手续费）：不补的话价格止损比风控员算的近一个
        # 手续费（约 5bp），市价止盈远一个手续费。止损加上、止盈减去，触发价就和 OrderIntent 一致
        # 限价止盈（E4）：hummingbot 按「入场价 × (1 ± 止盈%)」直接挂只减仓限价单，不看净盈亏，不减
        stop = intent.stop_loss_pct / 100.0 + self._open_fee
        if self._limit_tp:
            take = intent.take_profit_pct / 100.0
            tick = self._tick_size(intent.symbol)
            if intent.side is Side.SHORT and tick:
                # hummingbot 把限价往下取整：空单的买入平仓价会比止盈价远一档（DASH 50.6677 挂成
                # 50.66，最低只到 50.67 没成交）。少算一档，取整后正好落在止盈价上方最近一档
                ref = intent.notional_usdt / intent.quantity
                take = max(take - tick / ref, 0.0001)
        else:
            take = max(intent.take_profit_pct / 100.0 - self._open_fee, 0.0001)
        if limit:
            # 挂单等回踩：只挂单；执行器的时限从建执行器算，所以加上等待时间（持仓时长我们按成交
            # 时刻自己卡，见 _limit_housekeeping）
            open_type = _ORDER_LIMIT_MAKER if intent.limit_wait_s else _ORDER_LIMIT
        else:
            open_type = _ORDER_MARKET
        barrier: dict[str, Any] = {
            "stop_loss": str(round(stop, 8)),
            "take_profit": str(round(take, 8)),
            "time_limit": intent.time_limit_s + (intent.limit_wait_s or 0 if limit else 0),
            "open_order_type": open_type,
            "take_profit_order_type": _ORDER_LIMIT if self._limit_tp else _ORDER_MARKET,
        }
        if intent.trailing_stop_pct is not None and intent.trailing_activation_pct is not None:
            barrier["trailing_stop"] = {
                "activation_price": str(intent.trailing_activation_pct / 100.0),
                "trailing_delta": str(intent.trailing_stop_pct / 100.0),
            }
        if self._protection is not None:
            # 仅新仓：普通/移动止损由交易所条件单托管。按用户选择保留 HB 原生时限与原起算方式。
            barrier.update(stop_loss=None, take_profit=None, trailing_stop=None)
        config: dict[str, Any] = {
            "type": "position_executor",
            "connector_name": self._connector,
            "trading_pair": to_hb_pair(intent.symbol),
            # hummingbot 的 TradeType 枚举：1 = BUY，2 = SELL；传字符串会被 HTTP 400 拒绝
            "side": _TRADE_BUY if intent.side is Side.LONG else _TRADE_SELL,
            "amount": str(intent.quantity),
            "leverage": intent.leverage,
            "triple_barrier_config": barrier,
        }
        if limit:
            config["entry_price"] = str(intent.limit_price)
        return config

    async def open(self, intent: OrderIntent) -> PositionRecord:
        return await self._open(intent)

    async def _open(self, intent: OrderIntent, *, chase: bool = False) -> PositionRecord:
        async with self._registry.symbol_lock(intent.symbol):
            return await self._open_locked(intent, chase=chase)

    async def _open_locked(self, intent: OrderIntent, *, chase: bool = False) -> PositionRecord:
        """``chase``：挂单没成交后按同一份风控指令改市价追进（账本里另记一笔，指令号带 -chase）。"""
        intent_id = f"{intent.intent_id}-chase" if chase else intent.intent_id
        limit = intent.order_type == "limit" and not chase
        existing = self._registry.by_intent(intent_id)
        if existing is not None:  # 重试同一条指令不重复开仓
            return existing.record
        # 执行侧兜底（风控员也会拦）：同一个币已有在管持仓就拒绝。单向持仓模式下，第二笔——尤其反向——
        # 会把原仓对冲掉甚至反手，保命止损还会错位。放在改杠杆之前：ensure_leverage 改的是整个合约。
        held = next((t for t in self._registry.live() if t.record.symbol == intent.symbol), None)
        if held is not None:
            raise SymbolAlreadyHeld(
                f"{intent.symbol} 已有在管持仓 {held.record.position_id}，拒绝再开"
            )
        if any(
            t.record.symbol == intent.symbol and t.exit_cleanup_pending
            for t in self._registry.data.positions.values()
        ):
            raise SymbolAlreadyHeld(f"{intent.symbol} 上一笔退出清理未确认，拒绝新开")
        if intent.symbol in self._registry.data.emergency_exits:
            raise SymbolAlreadyHeld(f"{intent.symbol} 对账员的无主/反手仓平仓尚未确认，拒绝新开")
        if self._protection is not None:
            await self._protection.preflight(intent.symbol)
        now = self._clock()
        record = PositionRecord(
            position_id=f"pos-{uuid.uuid4().hex[:12]}",
            group_id=f"grp-{uuid.uuid4().hex[:12]}",
            intent_id=intent_id,
            signal_id=intent.signal_id,
            card_id=intent.card_id,
            symbol=intent.symbol,
            category=intent.category,
            side=intent.side,
            status=PositionStatus.OPENING,
            quantity=intent.quantity,
            leverage=intent.leverage,
            time_limit_s=intent.time_limit_s,
            opened_at=now,
            stop_loss_pct=intent.stop_loss_pct,
            lifeboat_stop_pct=intent.lifeboat_stop_pct,
            trailing_activation_pct=intent.trailing_activation_pct,
            trailing_stop_pct=intent.trailing_stop_pct,
        )
        # 先落账再建执行器：中途崩溃时重启能看到“有一笔开到一半的仓”
        tracked = TrackedPosition(
            record=record,
            lifeboat_stop_pct=intent.lifeboat_stop_pct,
            notional_usdt=intent.notional_usdt,
            stop_loss_pct=intent.stop_loss_pct,
            take_profit_pct=intent.take_profit_pct,
            tp_tick=self._tick_size(intent.symbol) if self._limit_tp else None,
            limit_wait_s=intent.limit_wait_s if limit else None,
            hold_s=intent.time_limit_s if limit and intent.limit_wait_s else None,
            chase=intent if limit and intent.limit_wait_s and intent.limit_chase else None,
            frozen_peak_exit=FrozenPeakExit(
                after_s=self._frozen_peak_after_s, exit_fee_bps=self._open_fee * 1e4
            )
            if self._frozen_peak_on and self._protection is not None
            else None,
            server_protection=self._protection.initial(intent.quantity)
            if self._protection is not None
            else None,
        )
        self._registry.put(tracked)
        if self._exchange is not None:
            # hummingbot 建执行器时不会替我们设杠杆（冒烟实测账户仍是 20x）。开仓前先设并回读，
            # 对不上就不开：杠杆直接决定爆仓线，宁可不开。
            try:
                await self._exchange.ensure_leverage(intent.symbol, intent.leverage)
            except ExchangeError as e:
                self._set(tracked, status=PositionStatus.FAILED, closed_at=self._clock())
                await self._finish_uncreated_server(record.position_id)
                await self._alert(
                    AlertLevel.CRITICAL,
                    "开仓取消：杠杆没设成功",
                    f"{intent.symbol} 目标 {intent.leverage}x：{e}",
                    intent.symbol,
                    f"leverage_failed:{record.position_id}",
                    kind="order_failed",
                )
                raise
        marker = f"{MARKER_PREFIX}{record.position_id}"
        try:
            created = await self._api.create_executor(
                self._executor_config(intent, market=chase), self._account, marker
            )
            executor_id = str(created["executor_id"])
        except HbApiError as e:
            if e.definite_rejection:
                return await self._open_failed(tracked, intent.symbol, raise_error=e)
            # 超时、断连、5xx：hummingbot 可能已经建好并成交。先按标记查，查到就认领
            try:
                found = await self._api.find_executors(self._account, marker)
            except HbApiError:
                # 连查都查不了：结果不明。留着 OPENING，由对账员按标记认领，绝不当没发生
                await self._alert(
                    AlertLevel.CRITICAL,
                    "开仓结果不明，等待认领",
                    f"{intent.symbol} {record.position_id}：{e}",
                    intent.symbol,
                    f"open_unknown:{record.position_id}",
                    kind="order_failed",
                )
                return record
            if not found:
                return await self._open_failed(tracked, intent.symbol, raise_error=e)
            executor_id = str(found[0]["executor_id"])
            await self._alert(
                AlertLevel.WARN,
                "建执行器超时但已生效，已认领",
                f"{intent.symbol} {record.position_id} → {executor_id}",
                intent.symbol,
                f"claimed:{record.position_id}",
                kind="order_failed",
            )
        tracked = self._set(tracked, executor_id=executor_id)

        deadline = self._clock().timestamp() + self._fill_timeout_s
        while tracked.record.status is PositionStatus.OPENING:
            tracked = await self._sync_one(tracked)
            if tracked.record.status is not PositionStatus.OPENING:
                break
            if self._clock().timestamp() >= deadline:
                break
            await self._sleep(self._poll_s)

        if tracked.record.status is PositionStatus.OPEN and tracked.record.lifeboat is None:
            # 已成交但保命止损没挂上：不留裸仓，立即平掉
            await self._alert(
                AlertLevel.CRITICAL,
                "保命止损挂不上，已平掉刚开的仓",
                f"{intent.symbol} {record.position_id}",
                intent.symbol,
                f"lifeboat_failed_on_open:{record.position_id}",
                kind="lifeboat_missing",
            )
            await self.close(record.position_id, CloseReason.RECONCILER)
        return self._registry.data.positions[record.position_id].record

    async def _open_failed(
        self, tracked: TrackedPosition, symbol: str, *, raise_error: HbApiError
    ) -> PositionRecord:
        self._set(tracked, status=PositionStatus.FAILED, closed_at=self._clock())
        if raise_error.definite_rejection:
            await self._finish_uncreated_server(tracked.record.position_id)
        await self._alert(
            AlertLevel.CRITICAL,
            "开仓失败：建执行器出错",
            f"{symbol} {tracked.record.position_id}",
            symbol,
            f"open_failed:{tracked.record.position_id}",
            kind="order_failed",
        )
        raise raise_error

    async def _finish_uncreated_server(self, position_id: str) -> None:
        """仅调用于尚未发送 create 或明确拒绝；未知 create 永不凭零仓释放同币门控。"""
        tracked = self._registry.get(position_id)
        if tracked is None or tracked.server_protection is None or self._exchange is None:
            return
        if tracked.record.executor_id is not None or tracked.server_protection.legs:
            return
        if await self._held_quantity(tracked.record.symbol) != 0:
            return
        if await self._held_quantity(tracked.record.symbol) != 0:
            return
        current = self._registry.get(position_id)
        if (
            current is None
            or current.record.executor_id is not None
            or current.server_protection is None
        ):
            return
        if current.record.status is PositionStatus.FAILED and not current.server_protection.legs:
            self._registry.put(
                current.model_copy(
                    update={
                        "server_protection": current.server_protection.model_copy(
                            update={"stage": "done"}
                        )
                    }
                )
            )

    async def claim_executor(self, position_id: str, executor_id: str) -> None:
        """把带我们标记的执行器认领回账本（建执行器时结果不明、之后才发现它存在）。

        已标 FAILED 的也认回来：继续等成交、挂保命止损，由 ``positions()`` 的同步完成。
        """
        tracked = self._registry.get(position_id)
        if tracked is None:
            return
        if tracked.record.status in (PositionStatus.FAILED, PositionStatus.OPENING):
            self._set(
                tracked,
                executor_id=executor_id,
                status=PositionStatus.OPENING,
                closed_at=None,
            )

    async def _cancel_lifeboat(self, rec: PositionRecord) -> None:
        """仓已平后撤掉它的保命止损，失败重试几次。

        残留的只减仓止损在同币随后开新仓时会把新仓平掉，所以直连平仓、执行器结束两条路径都要撤。
        撤不掉就告警，对账员下一轮会清扫残留。
        """
        last: Exception | None = None
        for attempt in range(self._lifeboat_attempts):
            try:
                await self._lifeboat.cancel(rec)
                self._mark_lifeboat_cancelled(rec.position_id)
                return
            except Exception as e:
                last = e
                log.warning("撤保命止损第 %d 次失败 %s：%r", attempt + 1, rec.position_id, e)
                await self._sleep(self._poll_s)
        await self._alert(
            AlertLevel.WARN,
            "平仓后撤保命止损失败",
            f"{rec.symbol} {rec.position_id}：{last!r}",
            rec.symbol,
            f"lifeboat_cancel_failed:{rec.position_id}",
            kind="lifeboat_missing",
        )

    def _mark_lifeboat_cancelled(self, position_id: str) -> None:
        """撤成功后账本里的保命止损状态改成 cancelled，免得平仓记录里还显示 active。"""
        tracked = self._registry.get(position_id)
        if tracked is not None and tracked.record.lifeboat is not None:
            order = tracked.record.lifeboat.model_copy(
                update={"status": LifeboatStatus.CANCELLED, "updated_at": self._clock()}
            )
            self._set(tracked, lifeboat=order)

    async def release_lifeboat(self, position_id: str) -> None:
        """对账员直连平仓成功、记为已平之后调：撤掉该仓的保命止损。"""
        tracked = self._registry.get(position_id)
        if tracked is not None:
            if tracked.server_protection is not None:
                async with self._exit_lock(position_id):
                    await self._server_flat_ready(position_id)
            else:
                await self._cancel_lifeboat(tracked.record)

    async def _server_flat_ready(self, position_id: str) -> bool:
        """同仓锁内统一收尾：HB 原单、全部条件单子单与真实零仓都已核实。"""
        tracked = self._registry.get(position_id)
        if tracked is None or tracked.server_protection is None or self._protection is None:
            return False
        if not await self._hb_exit_finished(tracked):
            return False
        if tracked.direct_close_pending:
            order = await self._recover_direct_order(tracked)
            if order is None or order.is_terminal is not True:
                return False
        if not await self._protection.flat_cleanup(position_id):
            return False
        return (
            await self.entry_finished(position_id)
            and await self._held_quantity(tracked.record.symbol) == 0
        )

    async def settle_external_close(
        self,
        position_id: str,
        fill: CloseFill | None,
    ) -> bool:
        """对账结算也必须持同一退出锁，重新核验残单、在途平仓和实际零仓。"""
        async with self._exit_lock(position_id):
            tracked = self._registry.get(position_id)
            if tracked is None:
                return False
            rec = tracked.record
            if rec.status is PositionStatus.CLOSED:
                return True
            if not await self.entry_finished(position_id):
                return False
            if await self._held_quantity(rec.symbol) != 0:
                return False
            if tracked.direct_close_pending:
                order = await self._recover_direct_order(tracked)
                if order is None or order.is_terminal is not True:
                    return False
            if not tracked.hb_close_pending and rec.executor_id is not None:
                tracked = (self._registry.get(position_id) or tracked).model_copy(
                    update={
                        "hb_close_pending": True,
                    }
                )
                self._registry.put(tracked)
                try:
                    await self._api.stop_executor(rec.executor_id, keep_position=True)
                except Exception:
                    current = self._registry.get(position_id) or tracked
                    self._registry.put(current.model_copy(update={"hb_close_unknown": True}))
                    return False
            if not await self._hb_exit_finished(tracked):
                return False
            if tracked.server_protection is not None and (
                not await self._server_flat_ready(position_id)
            ):
                return False
            if await self._held_quantity(rec.symbol) != 0:
                return False
            self.mark_closed_directly(position_id, CloseReason.MANUAL, fill)
            await self._finish_exit_cleanup(position_id)
            return True

    def mark_closed_directly(
        self, position_id: str, reason: CloseReason, fill: CloseFill | None = None
    ) -> None:
        """直连币安平仓成功后，把账本记为已平，免得每轮重复告警。

        有成交均价就按 数量×(平仓价-入场价) 算毛盈亏，扣掉成交回报里的手续费后记进当日统计；
        没有成交价就记 None，并留一条日志，当日统计会漏这一笔。
        """
        tracked = self._registry.get(position_id)
        if tracked is None:
            return
        if tracked.server_protection is not None and tracked.server_protection.stage != "done":
            # 同步记账函数不能自行核查交易所；托管仓必须先经锁内完整收尾，禁止旧入口绕过。
            log.error("托管仓尚未完整核验收尾，拒绝记为已平 %s", position_id)
            return
        tracked = tracked.model_copy(update={"exit_cleanup_pending": True})
        self._registry.put(tracked)
        rec = tracked.record
        pnl: float | None = None
        fee = (fill.fee_usdt if fill is not None else None) or 0.0
        if (
            fill is not None
            and fill.price is not None
            and fill.fee_usdt is not None
            and rec.entry_price is not None
        ):
            sign = 1.0 if rec.side is Side.LONG else -1.0
            pnl = sign * rec.quantity * (fill.price - rec.entry_price) - fee
        else:
            log.warning("直连平仓成交价或手续费未知，净盈亏等待成交回填 %s", position_id)
        tracked = self._set(
            tracked,
            status=PositionStatus.CLOSED,
            closed_at=self._clock(),
            close_reason=reason,
            realized_pnl_usdt=pnl,
            fees_usdt=fee,
        )
        self._record(
            Outcome.CLOSE,
            f"{rec.symbol} 直连平仓（{reason.value}），净盈亏 "
            + (f"{pnl:+.2f}U" if pnl is not None else "未知")
            + ("（平仓价为估算）" if fill is not None and fill.estimated else ""),
            tracked.record,
            output={
                "net": pnl,
                "reason": reason.value,
                "direct": True,
                "fees_known": fill is not None and fill.fee_usdt is not None,
                "estimated": bool(fill is not None and fill.estimated),
            },
        )

    def mark_failed(self, position_id: str) -> None:
        tracked = self._registry.get(position_id)
        if tracked is not None and tracked.record.status is PositionStatus.OPENING:
            self._set(tracked, status=PositionStatus.FAILED, closed_at=self._clock())

    # ------------------------------------------------------------------ 平仓

    @asynccontextmanager
    async def _exit_lock(self, position_id: str) -> AsyncIterator[None]:
        tracked = self._registry.get(position_id)
        if tracked is None:
            async with self._position_exit_lock(position_id):
                yield
            return
        async with (
            self._registry.symbol_lock(tracked.record.symbol),
            self._position_exit_lock(position_id),
        ):
            yield

    @asynccontextmanager
    async def _position_exit_lock(self, position_id: str) -> AsyncIterator[None]:
        """所有退出入口共用每仓可重入锁；同步结账可以在同一任务内递归兜底。"""
        task = asyncio.current_task()
        if task is not None and self._exit_owners.get(position_id) is task:
            yield
            return
        async with self._direct_locks.setdefault(position_id, asyncio.Lock()):
            self._exit_owners[position_id] = task
            try:
                yield
            finally:
                self._exit_owners.pop(position_id, None)

    async def close(self, position_id: str, reason: CloseReason) -> None:
        async with self._exit_lock(position_id):
            await self._close_locked(position_id, reason)

    async def _close_locked(self, position_id: str, reason: CloseReason) -> None:
        tracked = self._registry.get(position_id)
        if tracked is None:
            raise KeyError(f"不认识的持仓：{position_id}")
        if tracked.record.status in (PositionStatus.CLOSED, PositionStatus.FAILED):
            return
        if tracked.server_protection is not None:
            tracked = self._set(
                tracked,
                status=PositionStatus.CLOSING,
                requested_close=tracked.requested_close or reason,
            )
            if not await self._close_directly(tracked, tracked.requested_close or reason, None):
                raise ProtectionIncomplete("托管退出尚未确认，已有保护保留")
            return
        # 已有直连意图时先恢复同一订单，不能再让 HB 另外下平仓单。
        if tracked.direct_close_pending or tracked.direct_close_client_order_id is not None:
            if not await self._close_directly(tracked, tracked.requested_close or reason, None):
                raise HbApiError("原直连平仓尚未确认，仓位与保护保留")
            return
        executor_id = tracked.record.executor_id
        if executor_id is None:
            raise HbApiError(f"{position_id} 还没有执行器，无法平仓")
        tracked = self._set(
            tracked,
            status=PositionStatus.CLOSING,
            requested_close=tracked.requested_close or reason,
        )
        if not tracked.hb_close_pending:
            tracked = tracked.model_copy(update={"hb_close_pending": True})
            self._registry.put(tracked)  # 请求发送前保存，重启不得盲发另一个停止请求
            try:
                await self._api.stop_executor(executor_id, keep_position=False)
            except Exception as via_hb:
                if isinstance(via_hb, HbApiError) and via_hb.definite_rejection:
                    tracked = (self._registry.get(position_id) or tracked).model_copy(
                        update={"hb_close_pending": False}
                    )
                    self._registry.put(tracked)
                    if await self._close_directly(tracked, reason, via_hb):
                        return
                else:
                    current = self._registry.get(position_id) or tracked
                    self._registry.put(current.model_copy(update={"hb_close_unknown": True}))
                # 断连/超时可能已接收并正在平仓；不并行开启另一个退出通道。
                raise
        # 等执行器真的结束再撤保命止损：平仓没成功之前，币安上的保命止损必须还在
        deadline = self._clock().timestamp() + self._fill_timeout_s
        while tracked.record.status is PositionStatus.CLOSING:
            tracked = await self._sync_one(tracked)
            if tracked.record.status is not PositionStatus.CLOSING:
                return
            if self._clock().timestamp() >= deadline:
                await self._alert(
                    AlertLevel.WARN,
                    "平仓后执行器迟迟没结束",
                    f"{tracked.record.symbol} {position_id}，保命止损保留，等对账员复查",
                    tracked.record.symbol,
                    f"close_slow:{position_id}",
                    kind="order_failed",
                )
                return
            await self._sleep(self._poll_s)

    async def request_direct_close(self, position_id: str, reason: CloseReason) -> bool:
        """对账兜底也走同一持久提交入口，不能绕过未知订单的去重。"""
        tracked = self._registry.get(position_id)
        if tracked is None:
            return False
        if tracked.record.status is PositionStatus.CLOSED:
            return True
        return await self._close_directly(tracked, reason, None)

    async def _close_directly(
        self,
        tracked: TrackedPosition,
        reason: CloseReason,
        via_hb: Exception | None,
        *,
        title: str = "hummingbot 没能平掉仓位，已直连币安平仓",
    ) -> bool:
        async with self._exit_lock(tracked.record.position_id):
            current = self._registry.get(tracked.record.position_id) or tracked
            if current.record.status is PositionStatus.CLOSED:
                return True
            return await self._close_directly_locked(current, reason, via_hb, title=title)

    async def _close_directly_locked(
        self,
        tracked: TrackedPosition,
        reason: CloseReason,
        via_hb: Exception | None,
        *,
        title: str,
    ) -> bool:
        """hummingbot 停不掉执行器时，直连币安下只减仓市价单。成功返回 True 并记为已平。

        只对已成交的仓做；开仓中的单子币安上可能还没有仓位，直连没有意义。
        """
        rec = tracked.record
        if self._exchange is None or rec.status not in (
            PositionStatus.OPEN,
            PositionStatus.CLOSING,
        ):
            return False
        # 先核实实际余仓，ACK/成交均价都不能证明已全平。提交前落盘，超时或部分成交
        # 后重启也不发送另一张无法关联的市价单；已确认保护一直保留。
        tracked = self._registry.get(rec.position_id) or tracked
        tracked = self._set(
            tracked,
            status=PositionStatus.CLOSING,
            requested_close=tracked.requested_close or reason,
        )
        if tracked.server_protection is not None and not await self._prepare_server_exit(tracked):
            return False
        tracked = self._registry.get(rec.position_id) or tracked
        rec = tracked.record
        if tracked.hb_close_pending and not await self._hb_exit_finished(tracked):
            return False
        tracked = self._registry.get(rec.position_id) or tracked
        held = await self._held_quantity(rec.symbol)
        if held is None or held > (tracked.entry_original_quantity or rec.quantity) * (1 + 1e-10):
            return False
        if tracked.direct_close_pending:
            order = await self._recover_direct_order(tracked)
            if order is None or order.is_terminal is not True:
                return False
            tracked = self._registry.get(rec.position_id) or tracked
            held = await self._held_quantity(rec.symbol)
            if held is None:
                return False
        fill = CloseFill(tracked.direct_close_price, tracked.direct_close_fee)
        if held > 0:
            place = getattr(self._exchange, "place_close", None)
            if place is None:
                return False  # 缺身份可恢复的能力，不能偷偷降级成无 CID 下单
            generation = tracked.direct_close_generation + 1
            cid = f"dc-{rec.position_id}-{generation}"
            if len(cid) > 36:
                return False
            quantity = min(rec.quantity, held)
            tracked = tracked.model_copy(
                update={
                    "direct_close_pending": True,
                    "direct_close_client_order_id": cid,
                    "direct_close_generation": generation,
                    "direct_close_quantity": quantity,
                    "direct_close_order_status": "SUBMIT_PENDING",
                    "direct_close_order_id": None,
                }
            )
            self._registry.put(tracked)
            close_side: Literal["buy", "sell"] = "sell" if rec.side is Side.LONG else "buy"
            try:
                order = await place(rec.symbol, close_side, quantity, cid)
            except IdentifiedCloseRejected:
                current = self._registry.get(rec.position_id) or tracked
                self._registry.put(
                    current.model_copy(
                        update={
                            "direct_close_pending": False,
                            "direct_close_order_status": "REJECTED",
                        }
                    )
                )
                return False  # 明确本请求未接收；下轮仅依据新鲜余仓生成下一代
            except IdentifiedCloseUnknown as error:
                order = error.observed
                if order is None:
                    return False
            except Exception:
                return False  # 保留原 CID / pending，禁止暂时查无结果后重发
            if not self._accept_direct_order(tracked, order):
                return False
            if order.is_terminal is not True:
                return False
            tracked = self._registry.get(rec.position_id) or tracked
            held = await self._held_quantity(rec.symbol)
            if held is None or held > 0:
                return False  # 终态部分成交；下一轮确认真实残量后才允许下一代
            fill = CloseFill(order.average_price, None)
        if tracked.direct_close_generation > 1:
            fill = CloseFill()  # 多代成交均价交成交明细回填，不拿最后一笔冒充总均价
        if not await self.entry_finished(rec.position_id):
            return False
        if await self._held_quantity(rec.symbol) != 0:
            return False  # entry 查询期间可能出现最后一笔迟到成交
        if tracked.server_protection is not None and not await self._server_flat_ready(
            rec.position_id
        ):
            return False
        self.mark_closed_directly(rec.position_id, reason, fill)
        closed = self._registry.get(rec.position_id)
        if closed is not None:
            await self._notice("平仓完成", _close_text(closed.record), closed.record, "closed")
        cause = repr(via_hb) if via_hb is not None else "执行器已结束但币安仍有仓"
        await self._alert(
            AlertLevel.WARN,
            title,
            f"{rec.symbol} {rec.position_id}：{cause}",
            rec.symbol,
            f"closed_directly:{rec.position_id}",
            kind="order_failed",
        )
        await self._finish_exit_cleanup(rec.position_id)
        return True

    async def _finish_exit_cleanup(self, position_id: str) -> bool:
        """实际平完后仍认领旧执行器/残单，清理确认前禁止同币新开。"""
        async with self._exit_lock(position_id):
            tracked = self._registry.get(position_id)
            if tracked is None or not tracked.exit_cleanup_pending:
                return True
            rec = tracked.record
            if (
                not await self.entry_finished(position_id)
                or await self._held_quantity(rec.symbol) != 0
            ):
                return False
            if not await self._hb_exit_finished(tracked):
                tracked = self._registry.get(position_id) or tracked
                if tracked.hb_close_pending or rec.executor_id is None:
                    return False
                tracked = tracked.model_copy(update={"hb_close_pending": True})
                self._registry.put(tracked)
                try:
                    await self._api.stop_executor(rec.executor_id, keep_position=True)
                except Exception:
                    current = self._registry.get(position_id) or tracked
                    self._registry.put(current.model_copy(update={"hb_close_unknown": True}))
                    return False
                if not await self._hb_exit_finished(tracked):
                    return False
            if tracked.server_protection is not None:
                if self._protection is None or not await self._protection.flat_cleanup(position_id):
                    return False
            else:
                await self._cancel_lifeboat(rec)
                try:
                    if await self._lifeboat.fetch(rec) is not None:
                        return False
                except Exception:
                    return False
            if await self._held_quantity(rec.symbol) != 0:
                return False
            current = self._registry.get(position_id) or tracked
            self._registry.put(current.model_copy(update={"exit_cleanup_pending": False}))
            return True

    def server_managed(self, position_id: str) -> bool:
        tracked = self._registry.get(position_id)
        return tracked is not None and tracked.server_protection is not None

    async def protection_tick(self) -> None:
        """后台单轮，仅维护已持久托管仓；逐仓故障隔离，不开仓、不补造丢失峰值。"""
        for initial in list(self._registry.data.positions.values()):
            if initial.server_protection is None or initial.server_protection.stage == "done":
                continue
            pid = initial.record.position_id
            try:
                if initial.exit_cleanup_pending:
                    await self._finish_exit_cleanup(pid)
                    continue
                if initial.record.status in {PositionStatus.CLOSED, PositionStatus.FAILED}:
                    continue  # 未明 create 的 FAILED 仍阻同币；不能在后台把未知归为没提交。
                current = await self._sync_one(initial)
                if current.record.status is PositionStatus.OPEN:
                    await self._update_server_trailing(pid)
            except Exception as error:
                await self._alert(
                    AlertLevel.CRITICAL,
                    "托管保护本轮未确认",
                    f"{initial.record.symbol} {pid}：{type(error).__name__}；"
                    "保留最后确认保护并等待核验",
                    initial.record.symbol,
                    f"server_tick:{pid}",
                    kind="order_failed",
                )

    def configure_frozen_peak_exit(self, enabled: bool, after_s: int) -> None:
        self._frozen_peak_on = enabled
        self._frozen_peak_after_s = after_s

    async def frozen_peak_review(self, position_id: str, now: datetime) -> dict[str, Any] | None:
        initial = self._registry.get(position_id)
        if self._protection is None or initial is None or initial.frozen_peak_exit is None:
            return None
        if initial.record.status is not PositionStatus.OPEN:
            return None
        if initial.frozen_peak_exit.status == "triggered":
            async with self._registry.symbol_lock(initial.record.symbol):
                current = self._registry.get(position_id)
                if (
                    current is None
                    or current.record.status is not PositionStatus.OPEN
                    or current.frozen_peak_exit != initial.frozen_peak_exit
                    or current.entry_client_order_id != initial.entry_client_order_id
                ):
                    return None
                return {
                    **initial.frozen_peak_exit.model_dump(mode="json"),
                    "triggered": True,
                    "detail": "pending_exit",
                }
        basis = book = None
        held: list[ExchangePosition] = []
        error_detail = ""
        try:
            read_basis = getattr(self._exchange, "entry_basis", None)
            read_book = getattr(self._exchange, "execution_book", None)
            read_positions = getattr(self._exchange, "positions_evidence", None)
            if (
                read_basis is None
                or read_book is None
                or read_positions is None
                or initial.entry_client_order_id is None
            ):
                raise ExchangeError("Execution evidence reader or entry CID is unavailable")
            async with asyncio.timeout(FROZEN_PEAK_EVIDENCE_TIMEOUT_S):
                basis = await read_basis(initial.record.symbol, initial.entry_client_order_id)
                held = [p for p in await read_positions() if p.symbol == initial.record.symbol]
                book = await read_book(initial.record.symbol)
        except Exception as error:
            error_detail = f"evidence_unavailable:{type(error).__name__}:{str(error)[:200]}"
        async with self._registry.symbol_lock(initial.record.symbol):
            tracked = self._registry.get(position_id)
            if (
                tracked is None
                or tracked.record.status is not PositionStatus.OPEN
                or tracked.frozen_peak_exit != initial.frozen_peak_exit
                or tracked.entry_client_order_id != initial.entry_client_order_id
            ):
                return None
            state, protection = tracked.frozen_peak_exit, tracked.server_protection
            assert state is not None
            try:
                if error_detail:
                    raise ExchangeError(error_detail)
                if (
                    protection is None
                    or protection.stage != "armed"
                    or not protection.entry_terminal
                    or protection.entry_status != "FILLED"
                    or not isinstance(basis, EntryBasis)
                    or book is None
                    or basis.client_order_id != tracked.entry_client_order_id
                    or basis.order_id != protection.entry_order_id
                    or protection.observed_quantity is None
                    or not math.isclose(
                        protection.observed_quantity, basis.filled_quantity, rel_tol=1e-10
                    )
                    or len(held) != 1
                    or held[0].side is not tracked.record.side
                    or not math.isfinite(held[0].quantity)
                    or not math.isclose(held[0].quantity, basis.filled_quantity, rel_tol=1e-10)
                ):
                    raise ExchangeError(
                        "Entry order or remaining position evidence is inconsistent"
                    )
                state = frozen_peak_update(tracked.record, state, basis, book, self._clock())
            except Exception as error:
                detail = error_detail or (
                    f"evidence_unavailable:{type(error).__name__}:{str(error)[:200]}"
                )
                state = state.model_copy(update={"detail": detail})
                self._registry.put(tracked.model_copy(update={"frozen_peak_exit": state}))
                return {**state.model_dump(mode="json"), "triggered": False}
            self._registry.put(tracked.model_copy(update={"frozen_peak_exit": state}))
            return {**state.model_dump(mode="json"), "triggered": state.status == "triggered"}

    async def _update_server_trailing(self, position_id: str) -> None:
        async with self._exit_lock(position_id):
            tracked = self._registry.get(position_id)
            if (
                tracked is None
                or tracked.record.status is not PositionStatus.OPEN
                or self._protection is None
            ):
                return
            state, rec = tracked.server_protection, tracked.record
            if state is None or state.stage != "armed" or tracked.entry_client_order_id is None:
                return
            read_basis = getattr(self._exchange, "entry_basis", None)
            read_book = getattr(self._exchange, "execution_book", None)
            if read_basis is None:
                raise ProtectionIncomplete("缺实际入场成交与费用证据，保持最后确认保护")
            if (
                state.trailing_entry_quote is not None
                and state.trailing_entry_quantity is not None
                and state.trailing_fees_quote is not None
                and state.entry_order_id is not None
            ):
                basis = EntryBasis(
                    symbol=rec.symbol,
                    client_order_id=tracked.entry_client_order_id,
                    order_id=state.entry_order_id,
                    side="buy" if rec.side is Side.LONG else "sell",
                    filled_quantity=state.trailing_entry_quantity,
                    filled_quote=state.trailing_entry_quote,
                    fee_usdt=state.trailing_fees_quote,
                    entry_price=state.trailing_entry_quote / state.trailing_entry_quantity,
                    order_status=state.entry_status or "UNKNOWN",
                )
            else:
                basis = await read_basis(rec.symbol, tracked.entry_client_order_id)
                if (
                    not isinstance(basis, EntryBasis)
                    or basis.client_order_id != tracked.entry_client_order_id
                    or basis.order_id != state.entry_order_id
                    or basis.symbol != rec.symbol
                    or basis.side != ("buy" if rec.side is Side.LONG else "sell")
                    or basis.order_status != "FILLED"
                ):
                    raise ProtectionIncomplete("入场基准身份或完整性不一致")
                current = self._registry.get(position_id) or tracked
                current_state = current.server_protection
                assert current_state is not None
                self._registry.put(
                    current.model_copy(
                        update={
                            "server_protection": current_state.model_copy(
                                update={
                                    "trailing_entry_quote": basis.filled_quote,
                                    "trailing_entry_quantity": basis.filled_quantity,
                                    "trailing_fees_quote": basis.fee_usdt,
                                }
                            )
                        }
                    )
                )
            tracked = self._registry.get(position_id) or tracked
            if basis.entry_price != tracked.record.entry_price:
                sign = 1 if rec.side is Side.LONG else -1
                tracked = self._set(
                    tracked,
                    entry_price=basis.entry_price,
                    stop_loss_price=basis.entry_price * (1 - sign * tracked.stop_loss_pct / 100),
                    take_profit_price=tp_price(tracked, basis.entry_price),
                )
                await self._protection.sync(position_id)
            if rec.trailing_activation_pct is None or rec.trailing_stop_pct is None:
                return
            if read_book is None:
                raise ProtectionIncomplete("缺同交易所盘口源时钟，保持最后确认保护")
            book = await read_book(rec.symbol)
            if await self._protection.observe_trailing(position_id, basis, book):
                await self._protection.sync(position_id)

    async def _prepare_server_exit(self, tracked: TrackedPosition) -> bool:
        """不再让 HB 发第二路平仓；先停入场、查原单，再清理条件单/已触发子单。"""
        if self._protection is None:
            return False  # 重启后缺能力不能退回旧流程，留下真实保护等待处理。
        rec = tracked.record
        if not await self._hb_exit_finished(tracked):
            current = self._registry.get(rec.position_id) or tracked
            if current.hb_close_pending or rec.executor_id is None:
                return False
            current = current.model_copy(update={"hb_close_pending": True})
            self._registry.put(current)
            try:
                await self._api.stop_executor(rec.executor_id, keep_position=True)
            except Exception:
                current = self._registry.get(rec.position_id) or current
                self._registry.put(current.model_copy(update={"hb_close_unknown": True}))
                return False
            if not await self._hb_exit_finished(current):
                return False
        if not await self._protection.prepare_exit(rec.position_id):
            return False
        current = self._registry.get(rec.position_id) or tracked
        state = current.server_protection
        if (
            state is not None
            and state.entry_terminal
            and state.entry_filled_quantity is not None
            and state.entry_filled_quantity > 0
            and current.record.quantity != state.entry_filled_quantity
        ):
            # 撤余单后实际只开成一部分：账本总成交量按原入场订单证据记，不把余仓当总入场量。
            self._set(current, quantity=state.entry_filled_quantity)
        return True

    async def _sync_server(
        self, tracked: TrackedPosition, detail: dict[str, Any]
    ) -> TrackedPosition:
        rec, manager = tracked.record, self._protection
        if manager is None:
            await self._alert(
                AlertLevel.CRITICAL,
                "托管保护能力未接上",
                f"{rec.symbol} {rec.position_id}：保留原保护，不降级为旧执行方式",
                rec.symbol,
                f"server_unavailable:{rec.position_id}",
                kind="order_failed",
            )
            return tracked
        if _never_submitted(tracked, detail):
            finalized = await self._finalize(tracked, detail, None)
            if finalized.record.status is PositionStatus.FAILED:
                state = finalized.server_protection
                if state is not None and not state.legs:
                    self._registry.put(
                        finalized.model_copy(
                            update={"server_protection": state.model_copy(update={"stage": "done"})}
                        )
                    )
            return self._registry.get(rec.position_id) or finalized
        close_type = str(detail.get("close_type") or "").upper().removeprefix("CLOSETYPE.")
        if tracked.requested_close is not None or detail.get("status") in {
            "SHUTTING_DOWN",
            "TERMINATED",
        }:
            reason = (
                tracked.requested_close or _CLOSE_TYPE_MAP.get(close_type) or CloseReason.RECONCILER
            )
            tracked = self._set(tracked, status=PositionStatus.CLOSING, requested_close=reason)
            await self._close_directly(tracked, reason, None)
            return self._registry.get(rec.position_id) or tracked
        if rec.entry_price is None:
            return tracked  # 尚无已成交价格，不能用计划价挂普通保护。
        try:
            result = await manager.sync(rec.position_id)
        except Exception as error:
            current = self._registry.get(rec.position_id) or tracked
            state = current.server_protection
            keep_previous = (
                state is not None
                and state.ever_armed
                and state.stage != "exiting"
                and manager.retains_confirmed_stop(rec.position_id)
            )
            await self._alert(
                AlertLevel.CRITICAL,
                "普通托管保护未完整确认",
                f"{rec.symbol} {rec.position_id}：{type(error).__name__}；"
                + (
                    "保留最后确认止损与 HB 时限，等待回读"
                    if keep_previous
                    else "保留已确认保护，停止入场并核验退出"
                ),
                rec.symbol,
                f"server_incomplete:{rec.position_id}",
                kind="order_failed",
            )
            if keep_previous:
                return current
            tracked = self._set(
                self._registry.get(rec.position_id) or tracked,
                status=PositionStatus.CLOSING,
                requested_close=CloseReason.RECONCILER,
            )
            await self._close_directly(tracked, CloseReason.RECONCILER, None)
            return self._registry.get(rec.position_id) or tracked
        if result in {"exit", "flat"}:
            tracked = self._registry.get(rec.position_id) or tracked
            state = tracked.server_protection
            reason = (state.exit_reason if state else None) or CloseReason.MANUAL
            tracked = self._set(tracked, status=PositionStatus.CLOSING, requested_close=reason)
            await self._close_directly(tracked, reason, None)
        elif result == "provisional":
            await self._alert(
                AlertLevel.WARN,
                "入场未结束，普通保护仍在核验",
                f"{rec.symbol} {rec.position_id}：按已确认成交量补保护，不能宣称全部入场量已托管",
                rec.symbol,
                f"server_provisional:{rec.position_id}",
                kind="order_failed",
            )
        return self._registry.get(rec.position_id) or tracked

    def _accept_direct_order(self, tracked: TrackedPosition, order: IdentifiedCloseOrder) -> bool:
        rec = tracked.record
        if (
            order.client_order_id != tracked.direct_close_client_order_id
            or order.symbol != rec.symbol
            or order.close_side != ("sell" if rec.side is Side.LONG else "buy")
            or order.position_side != "BOTH"
            or not order.reduce_only
            or tracked.direct_close_quantity is None
            or (
                tracked.direct_close_order_id is not None
                and order.order_id != tracked.direct_close_order_id
            )
            or not all(
                math.isfinite(value)
                for value in (order.quantity, order.filled_quantity, order.remaining_quantity)
            )
            or order.filled_quantity < 0
            or order.remaining_quantity < 0
            or not math.isclose(
                order.quantity, order.filled_quantity + order.remaining_quantity, rel_tol=1e-10
            )
            or not math.isclose(order.quantity, tracked.direct_close_quantity, rel_tol=1e-10)
        ):
            return False
        current = self._registry.get(rec.position_id) or tracked
        self._registry.put(
            current.model_copy(
                update={
                    "direct_close_pending": order.is_terminal is not True,
                    "direct_close_order_status": order.status,
                    "direct_close_order_id": order.order_id,
                    "direct_close_price": order.average_price,
                }
            )
        )
        return True

    async def _recover_direct_order(self, tracked: TrackedPosition) -> IdentifiedCloseOrder | None:
        read = getattr(self._exchange, "close_order", None)
        cid = tracked.direct_close_client_order_id
        if read is None or cid is None:
            return None
        try:
            order = await read(tracked.record.symbol, cid)
            if isinstance(order, IdentifiedCloseOrder) and self._accept_direct_order(
                tracked, order
            ):
                return order
        except Exception:
            log.warning("原平仓订单尚不可证，保留保护 %s", tracked.record.position_id)
        return None

    async def _hb_exit_finished(self, tracked: TrackedPosition) -> bool:
        """HB 请求未知时，只有终态执行器及其实际订单证据才允许换直连通道。"""
        executor_id = tracked.record.executor_id
        read = getattr(self._exchange, "entry_order", None)
        if executor_id is None or read is None:
            return False
        try:
            detail = await self._api.get_executor(executor_id)
            if executor_query_state(detail, executor_id) != "terminal":
                return False
            info = (detail or {}).get("custom_info")
            ids = info.get("order_ids") if isinstance(info, dict) else None
            if not isinstance(ids, list) or not ids:
                return False
            for cid in ids:
                if not isinstance(cid, str) or not cid:
                    return False
                order = await read(tracked.record.symbol, cid)
                if order is None or not order.terminal:
                    return False
            current = self._registry.get(tracked.record.position_id) or tracked
            self._registry.put(
                current.model_copy(update={"hb_close_pending": False, "hb_close_unknown": False})
            )
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------ 查询与同步

    async def positions(self) -> list[PositionRecord]:
        for tracked in list(self._registry.data.positions.values()):
            if tracked.exit_cleanup_pending:
                await self._finish_exit_cleanup(tracked.record.position_id)
        for tracked in self._registry.live():
            tracked = await self._sync_one(tracked)
            if tracked.limit_wait_s:
                await self._limit_housekeeping(tracked)
        return [t.record for t in self._registry.live()]

    async def _limit_housekeeping(self, tracked: TrackedPosition) -> None:
        """挂单等回踩：等到时还没成交就撤单（8h 以上档改市价追）；成交后按成交时刻卡最长持仓。"""
        rec, now = tracked.record, self._clock()
        if (
            rec.status is PositionStatus.OPENING
            and rec.executor_id is not None
            and tracked.limit_wait_s is not None
            and (now - rec.opened_at).total_seconds() >= tracked.limit_wait_s
        ):
            await self._cancel_unfilled_limit(tracked)
        elif (
            rec.status is PositionStatus.OPEN
            and tracked.hold_s is not None
            and tracked.filled_at is not None
            and (now - tracked.filled_at).total_seconds() >= tracked.hold_s
        ):
            try:
                await self.close(rec.position_id, CloseReason.TIME_LIMIT)
            except Exception:
                log.warning(
                    "挂单进场的仓到时平仓失败 %s，下一轮再试", rec.position_id, exc_info=True
                )

    async def _cancel_unfilled_limit(self, tracked: TrackedPosition) -> None:
        async with self._exit_lock(tracked.record.position_id):
            current = self._registry.get(tracked.record.position_id) or tracked
            if current.record.status is not PositionStatus.OPENING:
                return
            await self._cancel_unfilled_limit_locked(current)

    async def _cancel_unfilled_limit_locked(self, tracked: TrackedPosition) -> None:
        rec = tracked.record
        assert rec.executor_id is not None
        if tracked.server_protection is not None:
            if not await self._prepare_server_exit(tracked):
                self._set(
                    tracked, status=PositionStatus.CLOSING, requested_close=CloseReason.TIME_LIMIT
                )
                return
            if await self._held_quantity(rec.symbol) != 0:
                tracked = self._set(
                    tracked, status=PositionStatus.CLOSING, requested_close=CloseReason.TIME_LIMIT
                )
                await self._close_directly(tracked, CloseReason.TIME_LIMIT, None)
                return
            if self._protection is None or not await self._protection.flat_cleanup(rec.position_id):
                return
        else:
            try:
                await self._api.stop_executor(rec.executor_id, keep_position=False)
            except Exception:
                log.warning("撤挂单失败 %s，下一轮再试", rec.position_id, exc_info=True)
                return
        current = self._registry.get(rec.position_id)
        if current is None or current.record.status is not PositionStatus.OPENING:
            return  # 撤的同时成交了 / 别的路径已经结账：按正常流程走
        if self._exchange is not None and (
            not await self.entry_finished(rec.position_id)
            or await self._held_quantity(rec.symbol) != 0
        ):
            self._set(
                current, status=PositionStatus.CLOSING, requested_close=CloseReason.TIME_LIMIT
            )
            return  # 停止请求不是撤单成功；可能有部分/迟到成交，不能追第二笔
        minutes = (tracked.limit_wait_s or 0) // 60
        chase = tracked.chase
        self._set(current, status=PositionStatus.FAILED, closed_at=self._clock())
        self._record(
            Outcome.INFO,
            f"{rec.symbol} 挂单没成交已撤（等了 {minutes} 分钟）"
            + ("，改市价追进" if chase is not None else "，不开仓"),
            rec,
            output={"not_filled": True, "limit_cancelled": True, "chase": chase is not None},
        )
        if chase is None:
            return
        # 批准是挂单时给的（最多一小时前）：交回主循环拿开仓锁、按此刻状态再审，过了才追
        if self._chase_request is None:
            log.warning("%s 挂单没成交，没接上追单复核，不追", rec.symbol)
            return
        self._chase_request(chase)

    def set_chase_request(self, request: ChaseRequest) -> None:
        """主循环接上追单（它拿开仓锁复核后调 ``open_chase``）。没接 = 不追。"""
        self._chase_request = request

    async def open_chase(self, intent: OrderIntent) -> PositionRecord:
        """按同一份风控员指令下市价单追进（主循环复核过才调）；账本里另记一笔，指令号带 -chase。"""
        return await self._open(intent, chase=True)

    def live_positions(self) -> list[PositionRecord]:
        """账本里此刻还没结束的持仓（开仓中 / 持有中 / 平仓中），不查 hummingbot，马上返回。"""
        return [t.record for t in self._registry.live()]

    async def sync_all(self) -> None:
        """只同步，不返回；对账员用。"""
        await self.positions()

    def record_lifeboat(self, position_id: str, order: LifeboatOrder | None) -> None:
        """对账员补挂 / 发现保命止损后回写账本。"""
        tracked = self._registry.get(position_id)
        if tracked is not None and tracked.record.status in (
            PositionStatus.OPEN,
            PositionStatus.CLOSING,
        ):
            self._set(tracked, lifeboat=order)

    async def _sync_one(self, tracked: TrackedPosition) -> TrackedPosition:
        rec = tracked.record
        if rec.executor_id is None:
            return self._registry.get(rec.position_id) or tracked
        # 慢查询不占退出锁。返回后重新读取账本，不能把查询前的 OPEN 快照覆盖已提交的平仓。
        detail = await self._api.get_executor(rec.executor_id)
        async with self._exit_lock(rec.position_id):
            current = self._registry.get(rec.position_id) or tracked
            if current.record.executor_id != rec.executor_id or current.record.status in (
                PositionStatus.CLOSED,
                PositionStatus.FAILED,
            ):
                return current
            return await self._apply_executor_snapshot(current, detail)

    async def _apply_executor_snapshot(
        self,
        tracked: TrackedPosition,
        detail: dict[str, Any] | None,
    ) -> TrackedPosition:
        """调用方已持同仓退出锁；所有状态判断必须基于锁内最新账本。"""
        rec = tracked.record
        if detail is None:
            if tracked.server_protection is not None:
                # 查无执行器不证明原平仓订单终态。尤其崩溃可能发生在 CLOSING 与停止意图之间。
                await self._alert(
                    AlertLevel.CRITICAL,
                    "托管执行器状态未知",
                    f"{rec.symbol} {rec.position_id}：保留保护与同币门控，不能按查无结果结账",
                    rec.symbol,
                    f"server_executor_unknown:{rec.position_id}",
                    kind="order_failed",
                )
                if rec.status is PositionStatus.CLOSING:
                    await self._close_directly(
                        tracked, tracked.requested_close or CloseReason.RECONCILER, None
                    )
                return self._registry.get(rec.position_id) or tracked
            # hummingbot 不认识这个执行器。OPEN 的交给对账员（EXECUTOR_FAILED，2 轮后直连平）；
            # 我们已经要求平仓（CLOSING）的，连续几轮都查不到就按“执行器已结束”走以币安为准的结账，
            # 否则账本会一直卡在 CLOSING。
            if rec.status is not PositionStatus.CLOSING or self._exchange is None:
                return tracked
            rounds = self._unknown_executor_rounds.get(rec.position_id, 0) + 1
            self._unknown_executor_rounds[rec.position_id] = rounds
            if rounds < _UNKNOWN_EXECUTOR_ROUNDS_BEFORE_SETTLE:
                return tracked
            self._unknown_executor_rounds.pop(rec.position_id, None)
            return await self._finalize(
                tracked, {"close_type": "EXECUTOR_UNKNOWN"}, rec.entry_price
            )
        self._unknown_executor_rounds.pop(rec.position_id, None)
        info = detail.get("custom_info")
        ids = info.get("order_ids") if isinstance(info, dict) else None
        if (
            tracked.server_protection is not None
            and tracked.entry_client_order_id is not None
            and isinstance(ids, list)
            and ids
            and ids[0] != tracked.entry_client_order_id
        ):
            await self._alert(
                AlertLevel.CRITICAL,
                "托管开仓订单身份发生变化",
                f"{rec.symbol} {rec.position_id}：HB报告了另一张开仓单，停止新增风险并核验全部订单",
                rec.symbol,
                f"server_entry_changed:{rec.position_id}",
                kind="order_failed",
            )
            tracked = self._set(
                tracked, status=PositionStatus.CLOSING, requested_close=CloseReason.RECONCILER
            )
            await self._close_directly(tracked, CloseReason.RECONCILER, None)
            return self._registry.get(rec.position_id) or tracked
        if (
            tracked.entry_client_order_id is None
            and isinstance(ids, list)
            and ids
            and isinstance(ids[0], str)
            and ids[0]
        ):
            tracked = tracked.model_copy(
                update={
                    "entry_client_order_id": ids[0],
                    "entry_original_quantity": rec.quantity,
                }
            )
            self._registry.put(tracked)
        entry = filled_entry(detail)

        if entry is not None and (
            rec.status is PositionStatus.OPENING
            or (rec.status is PositionStatus.CLOSING and rec.entry_price is None)
        ):
            was_closing = rec.status is PositionStatus.CLOSING
            tracked = self._mark_filled(tracked, entry)
            if was_closing:
                tracked = self._set(tracked, status=PositionStatus.CLOSING)
            if tracked.server_protection is None:
                tracked = await self._try_place_lifeboat(tracked)
            await self._notice("开仓成交", _open_text(tracked), tracked.record, "opened")
        elif (
            tracked.server_protection is not None
            and entry is not None
            and tracked.record.status is PositionStatus.OPEN
            and tracked.record.entry_price != entry
            and tracked.server_protection.trailing_entry_quote is None
        ):
            sign = 1 if rec.side is Side.LONG else -1
            tracked = self._set(
                tracked,
                entry_price=entry,
                stop_loss_price=entry * (1 - sign * tracked.stop_loss_pct / 100),
                take_profit_price=tp_price(tracked, entry),
            )

        if tracked.server_protection is not None:
            return await self._sync_server(tracked, detail)
        if not detail.get("is_active", True):
            tracked = await self._finalize(tracked, detail, entry)
        return tracked

    def _mark_filled(self, tracked: TrackedPosition, entry: float) -> TrackedPosition:
        sign = 1.0 if tracked.record.side is Side.LONG else -1.0
        rec = tracked.record
        self._record(
            Outcome.ACTION,
            f"{rec.symbol} {rec.side.value} 开仓成交，入场价 {entry}",
            rec,
            output={
                "entry_price": entry,
                "quantity": rec.quantity,
                "side": rec.side.value,
                "intent_id": rec.intent_id,
                "executor_id": rec.executor_id,
            },
        )
        # 止损止盈价只用于展示和对账，真正的触发由 hummingbot 执行器判断
        return self._set(
            tracked,
            status=PositionStatus.OPEN,
            filled_at=self._clock() if tracked.limit_wait_s else tracked.filled_at,
            entry_price=entry,
            stop_loss_price=entry * (1.0 - sign * tracked.stop_loss_pct / 100.0),
            take_profit_price=tp_price(tracked, entry),
        )

    async def _try_place_lifeboat(self, tracked: TrackedPosition) -> TrackedPosition:
        rec = tracked.record
        assert rec.entry_price is not None
        price = lifeboat_price(rec.side, rec.entry_price, tracked.lifeboat_stop_pct)
        last: Exception | None = None
        for attempt in range(self._lifeboat_attempts):
            try:
                order = await self._lifeboat.place(rec, price)
            except Exception as e:  # 交易所 / 网络 / 参数错误一视同仁：重试，仍失败则交给上层
                last = e
                log.warning("保命止损第 %d 次挂单失败 %s：%r", attempt + 1, rec.position_id, e)
                await self._sleep(self._poll_s)
                continue
            return self._set(tracked, lifeboat=order)
        log.error("保命止损挂不上 %s：%r", rec.position_id, last)
        return tracked

    async def entry_finished(self, position_id: str) -> bool:
        """真实开仓残单必须终态，净仓暂为零不能证明不会再成交。"""
        tracked = self._registry.get(position_id)
        if tracked is None or self._exchange is None:
            return False
        read_order = getattr(self._exchange, "entry_order", None)
        if read_order is None:
            log.warning("缺开仓残单查询能力，不能确认平仓 %s", position_id)
            return False
        try:
            cid = tracked.entry_client_order_id
            if cid is None and tracked.record.executor_id is not None:
                detail = await self._api.get_executor(tracked.record.executor_id)
                info = (detail or {}).get("custom_info")
                ids = info.get("order_ids") if isinstance(info, dict) else None
                if isinstance(ids, list) and ids and isinstance(ids[0], str) and ids[0]:
                    cid = ids[0]
                    current = self._registry.get(position_id) or tracked
                    tracked = current.model_copy(
                        update={
                            "entry_client_order_id": cid,
                            "entry_original_quantity": tracked.entry_original_quantity
                            or tracked.record.quantity,
                        }
                    )
                    self._registry.put(tracked)
            if cid is None:
                return False
            order = await read_order(tracked.record.symbol, cid)
            return bool(
                isinstance(order, ExchangeEntryOrder)
                and math.isfinite(order.filled_quantity)
                and 0 <= order.filled_quantity <= order.quantity
                and (order.status != "FILLED" or order.filled_quantity == order.quantity)
                and order.terminal
                and not order.reduce_only
                and order.symbol == tracked.record.symbol
                and order.client_order_id == cid
                and order.side == ("buy" if tracked.record.side is Side.LONG else "sell")
                and order.position_side == "BOTH"
                and order.quantity == (tracked.entry_original_quantity or tracked.record.quantity)
            )
        except Exception:
            log.warning("开仓残单终态未确认，保留保护 %s", position_id, exc_info=True)
            return False

    async def _held_quantity(self, symbol: str) -> float | None:
        """币安上该币的持仓数量；读不到返回 None。"""
        assert self._exchange is not None
        try:
            read = getattr(self._exchange, "positions_evidence", None)
            if read is None:
                return None
            positions = await read()
            if not isinstance(positions, list) or any(
                not isinstance(p, ExchangePosition)
                or not math.isfinite(p.quantity)
                or p.quantity <= 0
                or p.side not in (Side.LONG, Side.SHORT)
                for p in positions
            ):
                return None
            return float(sum(p.quantity for p in positions if p.symbol == symbol))
        except Exception as error:
            log.warning("核对币安持仓证据失败 %s：%s", symbol, type(error).__name__)
            return None

    async def _executor_ended_position_remains(
        self, tracked: TrackedPosition, close_type: str, held: float
    ) -> TrackedPosition:
        """执行器已结束但币安仍有仓。

        我们自己要求平仓的：停执行器“成功”却没平掉，直连只减仓市价单补平（并撤保命止损）。
        执行器自己结束的（hummingbot 重启 / 丢状态）：账本保持持仓，保命止损留着（缺了对账员补挂），
        由对账员按“执行器消失”流程处理，不能当作已平。
        """
        rec = tracked.record
        if held != rec.quantity:
            # 账本数量是下单数量；限价单可能只成交一部分。以币安实际持仓为准，直连平仓和对账都按它算
            tracked = self._set(tracked, quantity=held)
            rec = tracked.record
        if tracked.requested_close is not None:
            if await self._close_directly(tracked, tracked.requested_close, None):
                return self._registry.data.positions[rec.position_id]
            return tracked
        if rec.status is PositionStatus.CLOSING:
            tracked = self._set(tracked, status=PositionStatus.OPEN)
        await self._alert(
            AlertLevel.CRITICAL,
            "执行器已终止但币安仍有仓",
            f"{rec.symbol} {rec.position_id}：执行器结束类型 {close_type or '未知'}，"
            f"币安持仓 {held}。账本保持持仓，保命止损保留，对账员接管",
            rec.symbol,
            f"executor_ended_position_remains:{rec.position_id}",
            kind="order_failed",
        )
        return tracked

    async def _finalize(
        self, tracked: TrackedPosition, detail: dict[str, Any], entry: float | None
    ) -> TrackedPosition:
        """结账只做一次：平仓里的等待循环和主循环的同步可能同时走到这里（APT 那笔记了两条
        平仓、两条「止损已撤」）。正在结账或已经结完的直接返回账本里的那份。"""
        pid = tracked.record.position_id
        if pid in self._finalizing:
            return self._registry.get(pid) or tracked
        self._finalizing.add(pid)
        try:
            async with self._exit_lock(pid):
                return await self._finalize_once(self._registry.get(pid) or tracked, detail, entry)
        finally:
            self._finalizing.discard(pid)

    async def _finalize_once(
        self, tracked: TrackedPosition, detail: dict[str, Any], entry: float | None
    ) -> TrackedPosition:
        rec = tracked.record
        if tracked.server_protection is not None and not (
            rec.entry_price is None
            and entry is None
            and not tracked.server_protection.legs
            and _never_submitted(tracked, detail)
        ):
            # 所有旧结账入口统一收口。HB 报终态/404、净仓暂时为零都不能绕过托管子单核验。
            await self._close_directly(
                tracked, tracked.requested_close or CloseReason.RECONCILER, None
            )
            return self._registry.get(rec.position_id) or tracked
        if tracked.direct_close_client_order_id is not None or tracked.direct_close_pending:
            # 直连订单的成交/费用不能用 HB 的净收益（可能仍为0）代替。
            await self._close_directly(
                tracked, tracked.requested_close or CloseReason.RECONCILER, None
            )
            return self._registry.get(rec.position_id) or tracked
        if tracked.hb_close_pending and not await self._hb_exit_finished(tracked):
            return tracked
        if tracked.direct_close_pending:
            order = await self._recover_direct_order(tracked)
            if order is None or order.is_terminal is not True:
                return tracked
            tracked = self._registry.get(rec.position_id) or tracked
        close_type = str(detail.get("close_type") or "").upper().removeprefix("CLOSETYPE.")
        never_filled = rec.entry_price is None and entry is None
        never_submitted = never_filled and _never_submitted(tracked, detail)
        if (
            never_filled
            and tracked.entry_client_order_id is None
            and (not never_submitted or self._exchange is None)
        ):
            return tracked  # 没有真实订单身份时，只能用明确未提交证据 + 交易所零仓收尾。
        if self._exchange is not None:
            # 执行器说它结束了，不等于币安上的仓平了（hummingbot 重启时执行器被 SYSTEM_CLEANUP
            # 终止，仓位还在）。以币安为准：仓还在就不能记成已平、更不能撤保命止损。
            held = await self._held_quantity(rec.symbol)
            if held is None:
                return tracked  # 读不到币安：先不结账，下一轮同步再核对
            if held > 0:  # 币安只返回非零持仓；部分成交只剩零头也算还在
                if never_filled:
                    return tracked  # 入场证据不完整，不能把有仓位的开仓记录标 FAILED
                return await self._executor_ended_position_remains(tracked, close_type, held)
        if self._exchange is not None and (
            (not never_submitted and not await self.entry_finished(rec.position_id))
            or await self._held_quantity(rec.symbol) != 0
        ):
            return tracked
        current = self._registry.get(rec.position_id)
        if current is not None and current.record.status in (
            PositionStatus.CLOSED,
            PositionStatus.FAILED,
        ):
            return current  # 等币安回话的时候别的路径（直连平仓 / 对账员）已经结过账
        if never_submitted and (current is None or not _never_submitted(current, detail)):
            return current or tracked  # 等查仓期间的认领 / 成交证据不能被旧快照覆盖。
        if never_filled:
            status, reason = PositionStatus.FAILED, None
        else:
            status = PositionStatus.CLOSED
            # 按实际怎么成交的记（止盈 / 止损 / 移动止损 / 到时）；执行器说不出来（我们自己要求
            # 平仓，它报 EARLY_STOP 之类）才用要求平仓的原因。别因为有人下过令就把止盈单记成他平的
            reason = (
                _CLOSE_TYPE_MAP.get(close_type) or tracked.requested_close or CloseReason.MANUAL
            )
        tracked = self._set(
            tracked,
            status=status,
            closed_at=self._clock(),
            close_reason=reason,
            realized_pnl_usdt=None
            if never_submitted
            else float(detail.get("net_pnl_quote") or 0.0),
            fees_usdt=abs(float(detail.get("cum_fees_quote") or 0.0)),
        )
        why = _NOT_FILLED_CN.get(close_type, close_type or "未知")
        if never_filled:
            self._record(
                Outcome.ERROR,
                f"{rec.symbol} 开仓没成交，执行器已结束：{why}",
                rec,
                output={
                    "not_filled": True,
                    "close_type": close_type or None,
                    **({"never_submitted": True} if never_submitted else {}),
                },
            )
        else:
            closed = tracked.record
            net = closed.realized_pnl_usdt or 0.0
            self._record(
                Outcome.CLOSE,
                f"{rec.symbol} 平仓（{reason.value if reason else '未知'}），净盈亏 {net:+.2f}U",
                closed,
                output={
                    "net": net,
                    "reason": reason.value if reason else None,
                    "fees": closed.fees_usdt,
                    # 曾下令平仓的人（面板可以显示「巡逻兵曾下令」）；和 reason 一样就不重复写
                    **(
                        {"requested_by": tracked.requested_close.value}
                        if tracked.requested_close not in (None, reason)
                        else {}
                    ),
                },
            )
            await self._notice("平仓完成", _close_text(closed), closed, "closed")
        if never_filled:
            await self._alert(
                AlertLevel.WARN,
                "开仓没成交，执行器已结束",
                f"{rec.symbol} {rec.position_id}：{why}",
                rec.symbol,
                f"never_filled:{rec.position_id}",
                kind="order_failed",
            )
        if not never_submitted:
            await self._cancel_lifeboat(tracked.record)
        return self._registry.get(rec.position_id) or tracked

    # ------------------------------------------------------------------ 按币安成交回填

    async def backfill_fills(self, trades: UserTrades) -> int:
        """账本的入场 / 出场均价、手续费、净盈亏按币安实际成交回填（见 ``fills``）。

        返回改了几笔。"""
        now = self._clock()
        changed = 0
        for tracked in list(self._registry.data.positions.values()):
            rec = tracked.record
            if rec.entry_price is None or rec.status in (
                PositionStatus.OPENING,
                PositionStatus.FAILED,
            ):
                continue
            need_entry = rec.intended_price is None
            need_exit = rec.status is PositionStatus.CLOSED and (
                rec.exit_price is None or rec.realized_pnl_usdt is None
            )
            if not (need_entry or need_exit) or (rec.closed_at or now) < now - LOOKBACK:
                continue
            try:
                rows = await trades(rec.symbol, rec.opened_at - OPEN_SLACK)
            except Exception:
                log.warning("查 %s 的成交明细失败，下一轮再回填", rec.symbol, exc_info=True)
                continue
            async with self._exit_lock(rec.position_id):
                tracked = self._registry.get(rec.position_id) or tracked
                rec = tracked.record
                if rec.entry_price is None or rec.status in (
                    PositionStatus.OPENING,
                    PositionStatus.FAILED,
                ):
                    continue
                need_entry = rec.intended_price is None
                need_exit = rec.status is PositionStatus.CLOSED and (
                    rec.exit_price is None or rec.realized_pnl_usdt is None
                )
                entry = entry_fill(rec, rows)
                if entry is None:
                    if self._phantom(rec, rows, now):
                        self._mark_never_opened(tracked)
                        changed += 1
                    continue  # 开仓成交还没查到（或数量对不上）：都不动
                changes: dict[str, Any] = {}
                if need_entry:
                    sign = 1.0 if rec.side is Side.LONG else -1.0
                    changes.update(
                        intended_price=rec.entry_price,
                        entry_price=entry.price,
                        stop_loss_price=entry.price * (1.0 - sign * tracked.stop_loss_pct / 100.0),
                        take_profit_price=tp_price(tracked, entry.price),
                    )
                exit_ = exit_fill(rec, rows, entry) if need_exit else None
                lifeboat_hit = exit_ is not None and _lifeboat_hit(rec, exit_.price)
                if exit_ is not None:
                    changes["exit_price"] = exit_.price
                    if lifeboat_hit:
                        changes["close_reason"] = CloseReason.LIFEBOAT
                    if entry.commission_usdt is not None and exit_.commission_usdt is not None:
                        fees = entry.commission_usdt + exit_.commission_usdt
                        changes.update(fees_usdt=fees, realized_pnl_usdt=exit_.realized_pnl - fees)
                    else:
                        log.info(
                            "%s 手续费不是 USDT 计价（BNB 抵扣等），手续费沿用执行器报的",
                            rec.symbol,
                        )
                if not changes:
                    continue
                tracked = self._set(tracked, **changes)
                changed += 1
                if need_entry and tracked.record.status is PositionStatus.OPEN:
                    tracked = await self._relay_lifeboat(tracked)
                parts = []
                if need_entry:
                    parts.append(f"入场 {rec.entry_price:g} → {entry.price:g}")
                if exit_ is not None:
                    parts.append(f"出场 {exit_.price:g}")
                if "realized_pnl_usdt" in changes:
                    fees, net = changes["fees_usdt"], changes["realized_pnl_usdt"]
                    parts.append(f"手续费 {fees:.4f}U，净盈亏 {net:+.2f}U")
                self._record(
                    Outcome.INFO,
                    f"{rec.symbol} 账本按币安成交回填：" + "，".join(parts),
                    tracked.record,
                    output={"backfill": changes},
                )
                if lifeboat_hit and exit_ is not None:
                    net = tracked.record.realized_pnl_usdt
                    await self._notice(
                        "保命止损触发",
                        f"{rec.symbol} {_SIDE_CN[rec.side]} 在 {exit_.price:g} 被保命止损平掉"
                        + (f"，净盈亏 {net:+.2f}U" if net is not None else ""),
                        tracked.record,
                        "lifeboat_hit",
                    )
        return changed

    @staticmethod
    def _phantom(rec: PositionRecord, rows: list[TradeFill], now: datetime) -> bool:
        """修旧账：以前把 hummingbot 的计划价当成交（保证金不够根本没下单），对账员几秒后见币安
        没仓又记成「手动平」。这种记录币安上前后一笔开仓成交都没有，平仓价也从没回填过。
        入场价可能被上一笔同币的平仓成交错误回填过（ENA），所以不看 ``intended_price``。"""
        return (
            rec.status is PositionStatus.CLOSED
            and rec.close_reason is CloseReason.MANUAL
            and rec.exit_price is None
            and rec.closed_at is not None
            and rec.closed_at + PHANTOM_SETTLE <= now
            and no_open_fill(rec, rows)
        )

    def _mark_never_opened(self, tracked: TrackedPosition) -> None:
        rec = tracked.record
        # 以前可能把上一笔同币的平仓成交当成了入场回填过：入场价改回计划价
        planned = rec.intended_price if rec.intended_price is not None else rec.entry_price
        self._set(
            tracked,
            status=PositionStatus.FAILED,
            close_reason=None,
            realized_pnl_usdt=0.0,
            fees_usdt=0.0,
            entry_price=planned,
            intended_price=None,
        )
        self._record(
            Outcome.INFO,
            f"{rec.symbol} 这笔币安上没有任何开仓成交，改记为「未成交」（不是手动平）",
            tracked.record,
            output={"not_filled": True, "was": "closed_manual"},
        )

    async def _relay_lifeboat(self, tracked: TrackedPosition) -> TrackedPosition:
        async with self._exit_lock(tracked.record.position_id):
            current = self._registry.get(tracked.record.position_id) or tracked
            if current.record.status is not PositionStatus.OPEN:
                return current
            return await self._relay_lifeboat_locked(current)

    async def _relay_lifeboat_locked(self, tracked: TrackedPosition) -> TrackedPosition:
        """入场价按真实成交改了：保命止损按新入场价（仍是 ×倍数的距离）重挂。

        差不到 ``RELAY_LIFEBOAT_BP`` 就不动。先挂新（另一个 cid）再撤旧，中间币安上一直有保命止损；
        挂新失败旧的原样留着。新价已被越过（-2021）就直接平仓。
        """
        rec = tracked.record
        if tracked.server_protection is not None:
            if self._protection is not None:
                await self._protection.sync(rec.position_id)
            return self._registry.get(rec.position_id) or tracked
        old = rec.lifeboat
        if old is None or rec.entry_price is None:
            return tracked
        new = lifeboat_price(rec.side, rec.entry_price, tracked.lifeboat_stop_pct)
        if abs(new - old.stop_price) / old.stop_price * 1e4 <= RELAY_LIFEBOAT_BP:
            return tracked
        relay = getattr(self._lifeboat, "relay", None)
        try:
            if relay is not None:
                order: LifeboatOrder = await relay(rec, new)
            else:  # 不支持先挂后撤的实现：只能撤了再挂
                await self._lifeboat.cancel(rec)
                order = await self._lifeboat.place(rec, new)
        except Exception as e:
            return await self._relay_failed(tracked, e, kept_old=relay is not None)
        tracked = self._set(tracked, lifeboat=order)
        self._record(
            Outcome.ACTION,
            f"{rec.symbol} 保命止损按真实入场价重挂：{old.stop_price:g} → {order.stop_price:g}",
            tracked.record,
            output={"old_stop": old.stop_price, "new_stop": order.stop_price},
        )
        return tracked

    async def _relay_failed(
        self, tracked: TrackedPosition, e: Exception, *, kept_old: bool
    ) -> TrackedPosition:
        rec = tracked.record
        old = rec.lifeboat
        assert old is not None
        log.warning("按真实入场价重挂保命止损失败 %s：%r", rec.position_id, e)
        if isinstance(e, ExchangeError) and e.immediate_trigger:
            closed = await self._close_directly(
                tracked, CloseReason.LIFEBOAT, e, title="价格已越过新的保命止损位，已直连币安平仓"
            )
            if closed:
                return self._registry.get(rec.position_id) or tracked
        elif kept_old:
            await self._alert(
                AlertLevel.WARN,
                "按真实入场价重挂保命止损失败",
                f"{rec.symbol} {rec.position_id}：{e!r}；旧的保命止损 {old.stop_price:g} 还在",
                rec.symbol,
                f"lifeboat_relay_failed:{rec.position_id}",
                kind="lifeboat_missing",
            )
            return tracked
        else:
            try:  # 旧的已经撤了：马上按旧价挂回去
                order = await self._lifeboat.place(rec, old.stop_price)
            except Exception as again:
                e = again
            else:
                await self._alert(
                    AlertLevel.WARN,
                    "按真实入场价重挂保命止损失败",
                    f"{rec.symbol} {rec.position_id}：{e!r}；已按旧价 {old.stop_price:g} 挂回",
                    rec.symbol,
                    f"lifeboat_relay_failed:{rec.position_id}",
                    kind="lifeboat_missing",
                )
                return self._set(tracked, lifeboat=order)
        await self._alert(
            AlertLevel.CRITICAL,
            "保命止损缺失",
            f"{rec.symbol} {rec.position_id}：重挂失败（{e!r}），可能没有保命止损，对账员会补挂",
            rec.symbol,
            f"lifeboat_missing:{rec.position_id}",
            kind="lifeboat_missing",
        )
        return self._set(tracked, lifeboat=None)

    async def _notice(self, title: str, body: str, rec: PositionRecord, kind: str) -> None:
        """成功路径的 INFO 通知（开仓成交 / 平仓完成 / 保命止损触发），同一笔同一类只发一次。
        Telegram 推不推由设置里对应的 ``notify_*`` 开关管。"""
        shown = {"opened": "open_filled", "closed": "close_done"}.get(kind, kind)
        await self._alert(
            AlertLevel.INFO, title, body, rec.symbol, f"{kind}:{rec.position_id}", shown
        )

    def _set(self, tracked: TrackedPosition, **changes: Any) -> TrackedPosition:
        tracked = self._registry.get(tracked.record.position_id) or tracked
        requested = changes.pop("requested_close", tracked.requested_close)
        filled_at = changes.pop("filled_at", tracked.filled_at)
        record = tracked.record.model_copy(update=changes)
        updated = tracked.model_copy(
            update={"record": record, "requested_close": requested, "filled_at": filled_at}
        )
        self._registry.put(updated)
        return updated

    # ------------------------------------------------------------------ 账户

    async def account(self) -> AccountState:
        now = self._clock()
        balances = await self._api.portfolio_state(self._account, self._connector)
        usdt = next((b for b in balances if str(b.get("token")) == "USDT"), None)
        if usdt is None:
            raise HbApiError("账户里读不到 USDT 余额")
        wallet = float(usdt.get("units") or 0.0)
        available = float(usdt.get("available_units") or 0.0)
        unrealized = sum(
            float(p.get("unrealized_pnl") or 0.0)
            for p in await self._api.positions(self._account, self._connector)
        )
        equity = wallet + unrealized
        if equity <= 0:
            raise HbApiError(f"账户权益读到 {equity}，拒绝继续")

        live = self._registry.live()
        # 已经交给 hummingbot、还没成交的单会占保证金，币安的可用余额里可能还没扣
        pending_margin = sum(
            t.notional_usdt / t.record.leverage
            for t in live
            if t.record.status is PositionStatus.OPENING
        )
        by_category: dict[Any, float] = {}
        for t in live:
            # 还没成交的（OPENING）没有入场价，按指令里的名义价值算，否则连发的信号会少算敞口
            notional = t.record.notional_usdt if t.record.entry_price else t.notional_usdt
            by_category[t.record.category] = by_category.get(t.record.category, 0.0) + notional
        open_risk = 0.0
        for t in live:
            base = (
                t.record.quantity * t.record.entry_price
                if t.record.entry_price
                else t.notional_usdt
            )
            open_risk += base * t.lifeboat_stop_pct / 100.0
        midnight = now.astimezone(self._tz).replace(hour=0, minute=0, second=0, microsecond=0)
        day_realized = sum(
            t.record.realized_pnl_usdt or 0.0
            for t in self._registry.data.positions.values()
            if t.record.status is PositionStatus.CLOSED
            and t.record.closed_at is not None
            and t.record.closed_at >= midnight
        )
        return AccountState(
            ts=now,
            equity_usdt=equity,
            available_usdt=max(available - pending_margin, 0.0),
            day_start_equity_usdt=await self._day_start(now, wallet, equity),
            open_notional_usdt=sum(by_category.values()),
            open_notional_by_category=by_category,
            open_positions=len(live),
            open_symbols=sorted({t.record.symbol for t in live}),
            open_risk_usdt=open_risk,
            day_realized_pnl_usdt=day_realized,
        )

    async def _day_start(self, now: datetime, wallet: float, equity: float) -> float:
        """日亏熔断的当天基准，必须是 0 点时的权益，不能是“0 点后第一次读到的权益”。

        大脑 0 点前后没在跑（崩溃、重启）时，第一次读取距 0 点已经很久，当时的权益里
        可能已经亏掉一部分，直接拿来当基准会放宽熔断。所以：
          - 距 0 点不超过几分钟：当前权益就是基准
          - 否则用币安收入流水（已实现盈亏 + 手续费 + 资金费）倒推 0 点的钱包余额
          - 拿不到流水：取“当前权益”和“上次落盘权益”里更大的（更严格），并发 warn
        """
        local = now.astimezone(self._tz)
        today = local.date().isoformat()
        registry = self._registry
        current = registry.data.day_start
        if current is None or current.day != today:
            midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
            late_s = (now - midnight).total_seconds()
            if late_s <= DAY_START_TOLERANCE_S:
                base = equity
            else:
                base = await self._late_day_base(midnight, now, wallet, equity)
            current = DayStart(day=today, equity_usdt=base)
            registry.data.day_start = current
        registry.data.last_equity_usdt = equity
        registry.save()
        return current.equity_usdt

    async def _late_day_base(
        self, midnight: datetime, now: datetime, wallet: float, equity: float
    ) -> float:
        if self._income_since is not None:
            try:
                income = await self._income_since(midnight)
            except Exception as e:
                log.warning("查收入流水失败，日亏基准改用保守估计：%r", e)
            else:
                base = wallet - income
                if base > 0:
                    return base
        previous = self._registry.data.last_equity_usdt
        base = max(equity, previous) if previous is not None else equity
        await self._alert(
            AlertLevel.WARN,
            "日亏基准无法从 0 点倒推，已取保守值",
            f"晚于 0 点才读到账户；基准取 {base:.2f} USDT（当前 {equity:.2f}，上次 {previous}）",
            None,
            f"day_start_fallback:{midnight.date().isoformat()}",
            kind="system",
        )
        return base

    # ------------------------------------------------------------------ 决策日志

    def _record(
        self,
        outcome: Outcome,
        summary: str,
        rec: PositionRecord,
        *,
        output: dict[str, Any] | None = None,
    ) -> None:
        """按验收约定写 executor 记录：成交 = action，平仓 = close（output.net 是净盈亏）。"""
        if self._rec is not None:
            self._rec.record(
                self._clock(),
                outcome,
                summary,
                symbol=rec.symbol,
                signal_id=rec.signal_id,
                position_id=rec.position_id,
                card_id=rec.card_id,
                output=output,
            )

    def heartbeat(self, now: datetime, health: Health = Health.OK, headline: str = "") -> None:
        if self._rec is not None:
            live = len(self._registry.live())
            note = f"；{self.fills_note}" if self.fills_note else ""
            self._rec.heartbeat(now, health, headline or f"在管持仓 {live} 笔{note}")

    # ------------------------------------------------------------------ 告警

    async def _alert(
        self,
        level: AlertLevel,
        title: str,
        body: str,
        symbol: str | None,
        dedup_key: str,
        kind: str = "",
    ) -> None:
        try:
            await self._alerter.send(
                Alert(
                    ts=self._clock(),
                    level=level,
                    role=Role.EXECUTOR,
                    title=title,
                    body=body,
                    symbol=symbol,
                    dedup_key=dedup_key,
                    kind=kind,
                )
            )
        except Exception:  # 告警失败不能拖住交易回路
            log.exception("告警发送失败：%s", title)
