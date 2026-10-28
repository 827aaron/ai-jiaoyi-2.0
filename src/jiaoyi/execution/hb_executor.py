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
from jiaoyi.core.paper_stop import PAPER_STOP_DETAIL
from jiaoyi.core.recorder import RoleRecorder
from jiaoyi.execution.brackets import (
    AnchorBookSpike,
    BracketManager,
    CloseInProgress,
    ProtectionIncomplete,
    describe_error,
    live_leg,
)
from jiaoyi.execution.clock import Clock, utc_now
from jiaoyi.execution.exchange import (
    CloseFill,
    EntryBasis,
    ExchangeClient,
    ExchangeEntryOrder,
    ExchangeError,
    ExchangePosition,
    ExecutionBook,
    IdentifiedCloseCancelledBeforeSend,
    IdentifiedCloseNotSent,
    IdentifiedCloseOrder,
    IdentifiedCloseRejected,
    IdentifiedCloseUnknown,
    MarkEvidence,
)
from jiaoyi.execution.exit_fills import ExitFills
from jiaoyi.execution.fills import (
    LOOKBACK,
    OPEN_SLACK,
    TradeFill,
    UserTrades,
    entry_fill,
    exit_fill,
    no_open_fill,
)
from jiaoyi.execution.frozen_peak import BOOK_MAX_AGE_S as FROZEN_BOOK_MAX_AGE_S
from jiaoyi.execution.frozen_peak import (
    FrozenPeakExit,
    frozen_peak_evidence,
    frozen_peak_update,
)
from jiaoyi.execution.hb_client import HbApiError, HummingbotApi, executor_query_state
from jiaoyi.execution.lifeboat import lifeboat_price
from jiaoyi.execution.registry import DayStart, PositionRegistry, TrackedPosition
from jiaoyi.execution.symbols import to_hb_pair
from jiaoyi.execution.tighten import breakeven_stop_price
from jiaoyi.execution.trailing import TrailingBookUnavailable
from jiaoyi.interfaces import Alerter, LifeboatStop

log = logging.getLogger(__name__)


def _frozen_peak_size(
    state: FrozenPeakExit, side: Side, stop_loss_pct: float, trigger_price: float | None = None
) -> dict[str, float]:
    # Record-only: a bad persisted value must never turn a latched trigger into an error.
    try:
        return frozen_peak_evidence(state, side, stop_loss_pct, trigger_price)
    except Exception as error:
        log.warning("Frozen peak size not recorded: %s: %s", type(error).__name__, error)
        return {}


# hummingbot 的 OrderType 枚举值
_ORDER_MARKET = 1
HostedCloseMode = Literal["hb_first", "direct"]
_HB_FAILED_TITLE = "hummingbot 没能平掉仓位，已直连币安平仓"
HOSTED_CLOSE_SLOW_ALERT_S = 120.0
"""Floor for the slow hosted close alert: hb_first closes take 15-110 s from decision to fill."""
_DIRECT_CLOSE_FAILED = frozenset(
    {"REJECTED", "NOT_SENT", "EXPIRED", "EXPIRED_IN_MATCH", "CANCELED"}
)
_HB_STOP_RETRY_BASE_S = 5.0  # 结果未知的只停不平：第 n 次补发至少隔 5×2^n 秒
_ORDER_LIMIT = 2
_ORDER_LIMIT_MAKER = 3
"""hummingbot OrderType.LIMIT_MAKER：只挂单（post-only），挂单等回踩进场用。"""

MARKER_PREFIX = "jy-"
"""写进 hummingbot ``controller_id`` 的认领标记前缀：``jy-<position_id>``。"""
DAY_START_TOLERANCE_S = 300.0
VENUE_MARK_REUSE_S = 2.0
"""管家读执行所标记价：同一个币这么多秒内复用上一次读到的（标记价本身每秒才更新一次）。"""
VENUE_MARK_READ_MAX_AGE_S = 600.0
"""管家读标记价时交给 ``execution_mark`` 的新鲜度上限（``keeper_price_max_age_s`` 的上限）：
旧到多少算过旧由管家按自己的设置判，这里只挡更离谱的，免得过旧被当成读不到。"""

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
    CloseReason.TIGHTENED_STOP: "反向卡收紧止损",
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
    elif tracked.server_protection is not None:
        lines.append("保命单稍后由托管保护补挂")
    else:
        lines.append("保命止损还没挂上，对账员会补挂")
    return "\n".join(lines)


def _tick_error_detail(error: BaseException) -> str:
    """异常本身没有文字时（ccxt / httpx 包装常见），补上触发它的那个异常。"""
    detail = describe_error(error)
    context = error.__context__
    if error.__cause__ is None and context is not None and not error.__suppress_context__:
        detail = f"{detail} <- {type(context).__name__}: {context}"[:300]
    return detail


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


def _shown_detail(tracked: TrackedPosition, reason: CloseReason | None) -> str | None:
    """The noted close detail when it belongs to ``reason``.

    A detail noted by one rule must not label a close that something else caused: a paper
    stop note only shows on a stop-loss close, a reconciler note only on a reconciler close.
    """
    detail = tracked.close_detail
    if detail is None:
        return None
    paper = detail.startswith(PAPER_STOP_DETAIL)
    if reason is CloseReason.RECONCILER and not paper:
        return detail
    if reason is CloseReason.STOP_LOSS and paper:
        return detail
    return None


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
        hosted_close_mode: HostedCloseMode = "hb_first",
        protection_tick_alert_after: int = 1,
        hb_stop_unknown_retries: int = 0,
        hosted_close_direct_when_exiting: bool = False,
        exit_fills: ExitFills | None = None,
        hosted_close_slow_alert_s: float = HOSTED_CLOSE_SLOW_ALERT_S,
    ) -> None:
        """``hosted_close_mode``：交易所托管仓的平仓顺序。``hb_first`` = 先停 HB 执行器、等它终态并
        核完条件单再下直连平仓（原行为）；``direct`` = 我方决定平仓、HB 执行器仍在跑且没有自己的
        平仓单、保护腿状态都明确时，先按持久化的直连 CID 下只减仓市价单，再只停不平 HB，交易所止损
        一直挂到确认零仓后才撤。其余情况照旧走 ``hb_first``。
        ``hosted_close_direct_when_exiting``: under ``direct``, still go direct-first once the
        protection is already ``exiting`` (an earlier attempt could not read the position, or an
        hb_first exit preparation ran) as long as every other direct-first condition holds; False
        sends an exiting position to hb_first.

        ``hosted_close_slow_alert_s``: a hosted close still CLOSING this many seconds after it was
        first seen in progress gets one WARN alert.

        ``exit_fills``: side file for the exchange exit fill time (panel display); None = not kept.

        ``protection_tick_alert_after``：同一仓的后台保护轮连续失败几轮才发紧急告警，之前只记
        日志；1 = 每轮失败都发（原行为）。
        ``hb_stop_unknown_retries``：只停不平的停止请求结果未知、HB 仍报 RUNNING 时，本进程最多
        再补发几次（退避 5、10、20… 秒）；0 = 不补发（原行为）。

        ``tick_size``：合约的价格最小变动（限价止盈取整用），拿不到返回 None。

        ``open_fee_bps``：开仓手续费（单边吃单费）。hummingbot 按「扣了开仓手续费的净盈亏」
        触发止损止盈，传过去时补上这一截，让实际触发价离开仓价的距离 = 风控算的止损 / 止盈距离。"""
        self._chase_request = chase_request
        self._protection = protection
        if protection is not None:
            protection.notify_stop_clamped = self._alert_stop_clamped
            protection.notify_trigger_rejected = self._alert_trigger_rejected
        if hosted_close_mode not in ("hb_first", "direct"):
            raise ValueError(f"未知的托管平仓顺序：{hosted_close_mode}")
        self._hosted_close_mode: HostedCloseMode = hosted_close_mode
        self._direct_when_exiting = hosted_close_direct_when_exiting
        self._exit_fills = exit_fills
        self._close_slow_after_s = hosted_close_slow_alert_s
        self._exit_started: dict[str, datetime] = {}
        self._close_slow_alerted: set[str] = set()
        self._flat_awaiting_cleanup: set[str] = set()
        self._hb_stop_sent: set[str] = set()
        if protection_tick_alert_after < 1:
            raise ValueError("protection_tick_alert_after 至少为 1")
        self._tick_alert_after = protection_tick_alert_after
        self._tick_failures: dict[tuple[str, str], int] = {}
        """(仓, 同步 / 收尾) → 连续失败轮数。"""
        self._hb_stop_unknown_retries = hb_stop_unknown_retries
        self._hb_stop_retries: dict[str, tuple[int, datetime]] = {}
        """结果未知的停止：本进程已补发几次、上次（或第一次未知）的时刻。"""
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
        self._venue_marks: dict[str, tuple[datetime, MarkEvidence]] = {}
        self._venue_reads: dict[str, asyncio.Task[MarkEvidence]] = {}
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
            signal_kind=intent.signal_kind,
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

        if (
            tracked.record.status is PositionStatus.OPEN
            and tracked.record.lifeboat is None
            and not self._stop_first_covered(tracked)
        ):
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
            if not await self._hb_exit_finished(tracked) and not (
                await self._stop_hb_keep_position(position_id)
                and await self._hb_exit_finished(tracked)
            ):
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
        self._hb_stop_retries.pop(position_id, None)
        detail = _shown_detail(tracked, reason)
        self._record(
            Outcome.CLOSE,
            f"{rec.symbol} 直连平仓（{reason.value}"
            + (f"：{detail}" if detail else "")
            + "），净盈亏 "
            + (f"{pnl:+.2f}U" if pnl is not None else "未知")
            + ("（平仓价为估算）" if fill is not None and fill.estimated else ""),
            tracked.record,
            output={
                "net": pnl,
                "reason": reason.value,
                "direct": True,
                "fees_known": fill is not None and fill.fee_usdt is not None,
                "estimated": bool(fill is not None and fill.estimated),
            }
            | ({"detail": detail} if detail else {}),
        )

    def _note_close_detail(self, tracked: TrackedPosition, detail: str) -> TrackedPosition:
        """只记第一次的原因：之后几轮重试平仓不覆盖当初为什么要平。"""
        current = self._registry.get(tracked.record.position_id) or tracked
        # A paper stop note only asks for a stop-loss close; the reconciler's own reason for
        # closing replaces it, or its close would be labelled with the wrong cause.
        if current.close_detail is not None and not (
            current.close_detail.startswith(PAPER_STOP_DETAIL)
            and not detail.startswith(PAPER_STOP_DETAIL)
        ):
            return current
        current = current.model_copy(update={"close_detail": detail[:300]})
        self._registry.put(current)
        return current

    def note_close_detail(self, position_id: str, detail: str) -> None:
        tracked = self._registry.get(position_id)
        if tracked is not None:
            self._note_close_detail(tracked, detail)

    def paper_stop_levels(self, position_id: str) -> list[tuple[str, float | None]]:
        """Every stop this position holds, as (source, trigger price), for the paper stop rule.

        ``ledger`` is the ledger stop, ``hosted`` every stop-side hosted leg live on the exchange
        (fixed, trailing, tightened or the lifeboat leg), ``trailing`` the program's activated
        trailing stop even before its leg is replaced, ``lifeboat`` the legacy lifeboat order
        while active. Empty for an unknown position.

        The opposite-card target (``tightened_stop_price``) counts only once its leg is live:
        it is placed away from Demo's mark, not mainnet's, and as a bare target it could close a
        position the moment it is set.
        """
        tracked = self._registry.get(position_id)
        if tracked is None:
            return []
        rec = tracked.record
        out: list[tuple[str, float | None]] = [("ledger", rec.stop_loss_price)]
        if rec.lifeboat is not None and rec.lifeboat.status is LifeboatStatus.ACTIVE:
            out.append(("lifeboat", rec.lifeboat.stop_price))
        state = tracked.server_protection
        if state is None:
            return out
        out += [
            ("hosted", leg.trigger_price)
            for leg in state.legs
            if leg.kind == "stop_loss" and live_leg(leg)
        ]
        if state.trailing_activated:
            out.append(("trailing", state.trailing_stop_price))
        return out

    def paper_stop_since(self, position_id: str) -> datetime | None:
        """When the position started to exist on the exchange: the fill of a limit entry, else
        the open; None for an unknown position."""
        tracked = self._registry.get(position_id)
        if tracked is None:
            return None
        return tracked.filled_at or tracked.record.opened_at

    def take_profit_exiting(self, position_id: str) -> bool:
        """A hosted take-profit leg has triggered: Demo is exiting at its target, not a stop."""
        tracked = self._registry.get(position_id)
        state = tracked.server_protection if tracked is not None else None
        if state is None:
            return False
        return any(leg.kind == "take_profit" and leg.triggered_at is not None for leg in state.legs)

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
                if position_id in self._flat_awaiting_cleanup:
                    return  # 直连平仓已确认零仓，只差收尾核验；后台每秒一轮接着做，不算平仓失败。
                if self._hosted_exit_working(position_id):
                    self._exit_started.setdefault(position_id, self._clock())
                    raise CloseInProgress("托管退出进行中，已有保护保留，后台每秒接着核验")
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

    async def tighten_stop(
        self,
        position_id: str,
        *,
        reason: str,
        buffer_bps: float,
        mark_gap_bps: float = 0.0,
        profit_mode: str = "breakeven",
        distance_mult: float = 1.0,
        card_key: str | None = None,
        anchor_retry_s: float = 0.0,
        allow_close: bool = True,
        label: str = "反向卡",
    ) -> dict[str, Any]:
        """反向作战卡没经价格确认（#314）/ 作战卡转中性：收紧交易所托管止损，不平仓。

        先判「亏 / 赚得不够」：保本价 = 开仓价 ±（实际开仓手续费 + 按吃单费率估的平仓手续费 +
        ``buffer_bps`` 开仓名义），离触发价源不够 gap 就按原规则平（用户决定，正常分支）。
        够的话按 ``profit_mode``：

        - ``trail``：``BracketManager.anchor_trailing`` 在当前可成交价强制启动移动止损（距离 =
          开仓带的移动止损距离 × ``distance_mult``），之后移动止损照常只收紧；开仓没带移动止损的仓
          退回 breakeven。只锚一次（落盘，换卡版本、重启都不重锚）。
        - ``breakeven``：保本价落成 ``tightened_stop_price``。

        两种都作为 ``_stop_target`` 的候选，和固定 / 移动 / 正在挂的止损取最紧，换代先确认新代
        再撤旧代，不改账本止损价。返回 ``{"action", "why", ...}``：

        - ``tightened`` 新止损已确认；``pending`` 目标已落盘、换代还没确认（保护轮询接着做，
          旧止损保留；上一代移动止损还在提交 / 撤销中时先等它，不算失败）；
        - ``exiting`` 换代时交易所说已越触发价等，保护状态机已进入退出，由保护轮询按止损原因平；
        - ``kept`` 已收紧过，或现有止损已同样紧，或换代后在挂的止损没变；``skip`` 仓已不在 /
          已在平 / 保护在退出，或赚得够但这一轮临时保护且没有确认在挂的止损（交给保护状态机）；
        - trail 锚定时盘口疑似插针：``anchor_retry_s`` 秒内返回 ``pending`` 下一轮再试（不告警，
          起始时刻落盘），过了就平 + CRITICAL；0 = 立刻平。
        - ``close`` 不能收紧，调用方按原反向卡规则全平：旧通道（没有托管保护）、从没完整托管
          （含 #325 开仓宽限期）、入场费用或触发价源读不到、目标价离触发价源（新止损腿的触发价格源，
          现在配置是标记价）不到 ``mark_gap_bps``：多要 目标 < 价 ×（1 − gap），
          空要 目标 > 价 ×（1 + gap）；挂不稳就不挂，不靠交易所 -2021 拒单。
        曾完整托管、这一轮临时保护但旧止损仍确认在挂：目标落盘，返回 ``pending``。
        读不到证据、换单没确认写 ``server_protection.last_error`` 并发 CRITICAL；挂不稳是正常分支，
        只进决策记录。

        ``allow_close = False``（中性卡、管家行情没有或过旧）：上面每个 ``close`` 改成
        ``{"action": "hold", "why": "no_close_<原 why>"}``，只收紧，收紧不了这次也不平。
        ``label``：告警标题里的规则名（「反向卡」/「作战卡转中性」）。
        """
        result = await self._tighten_stop(
            position_id,
            reason=reason,
            buffer_bps=buffer_bps,
            mark_gap_bps=mark_gap_bps,
            profit_mode=profit_mode,
            distance_mult=distance_mult,
            card_key=card_key,
            anchor_retry_s=anchor_retry_s,
            then_close="按原规则全平" if allow_close else "行情过旧，这次不平",
            label=label,
        )
        if result.get("action") == "close" and not allow_close:
            return {**result, "action": "hold", "why": f"no_close_{result.get('why')}"}
        return result

    async def _tighten_stop(
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
        then_close: str,
        label: str,
    ) -> dict[str, Any]:
        manager = self._protection
        initial = self._registry.get(position_id)
        if initial is None:
            return {"action": "skip", "why": "unknown_position"}
        if initial.server_protection is None or manager is None:
            return {"action": "close", "why": "not_server_managed"}
        async with self._exit_lock(position_id):
            tracked = self._registry.get(position_id)
            if (
                tracked is None
                or tracked.record.status is not PositionStatus.OPEN
                or tracked.requested_close is not None
            ):
                return {"action": "skip", "why": "not_open"}
            state, rec = tracked.server_protection, tracked.record
            if state is None:
                return {"action": "close", "why": "not_server_managed"}
            if state.tightened_stop_price is not None or state.opposite_anchor_price is not None:
                # 已收紧 / 已锚定过：不再改（换卡版本、重启都不重来），之后由移动止损自己收紧。
                return {
                    "action": "kept",
                    "why": "already_tightened",
                    "target": state.tightened_stop_price,
                    "anchor_price": state.opposite_anchor_price,
                }
            if state.stage in {"exiting", "cleanup", "done"}:
                return {"action": "skip", "why": "protection_exiting", "stage": state.stage}
            if (
                not state.ever_armed
                or not state.entry_terminal
                or state.prearm_failing_since is not None
                or tracked.entry_client_order_id is None
                or rec.stop_loss_price is None
            ):
                # 从没完整托管过 / #325 开仓宽限期里：按原规则平。
                return {"action": "close", "why": "not_armed", "stage": state.stage}
            armed = state.stage == "armed"
            working_type = manager.stop_working_type
            long = rec.side is Side.LONG
            try:
                basis = await self._tighten_basis(tracked)
                target = breakeven_stop_price(
                    rec.side,
                    basis.filled_quantity,
                    basis.filled_quote,
                    basis.fee_usdt,
                    self._open_fee,
                    buffer_bps,
                )
                reference = await self._trigger_reference(manager, rec.symbol, long, working_type)
            except Exception as error:
                detail = describe_error(error)
                await self._tighten_failed(
                    position_id, f"{label}收紧止损的证据读不到", detail, then_close
                )
                return {"action": "close", "why": "evidence_unavailable", "error": detail}
            # 和交易所上正在挂的止损比（最后回读明确 NEW 的最高一代）；没有就和账本固定止损比。
            live = manager.live_stop_price(position_id)
            existing = live if live is not None else rec.stop_loss_price
            # 和保护状态机换代时的标记价缓冲取大的：这里过了，换代时也过（价格没动的话）。
            gap_bps = max(mark_gap_bps, manager.trailing_stop_mark_buffer_bps)
            gap = gap_bps / 1e4
            limit = reference * (1 - gap) if long else reference * (1 + gap)
            placeable = target < limit if long else target > limit
            info: dict[str, Any] = {
                "target": target,
                "placeable": placeable,
                "existing_stop": existing,
                "reference_price": reference,
                "placeable_limit": limit,
                "working_type": working_type,
                "entry_fee_usdt": basis.fee_usdt,
                "entry_quote": basis.filled_quote,
                "exit_fee_bps": self._open_fee * 1e4,
                "buffer_bps": buffer_bps,
                "mark_gap_bps": gap_bps,
            }
            if not placeable:
                # 亏着或赚得不够（保本价离触发价源不到 gap）：按原规则直接平（用户决定）。
                # 正常分支，不是故障：只进决策记录，不写 last_error、不发 CRITICAL。
                return {"action": "close", "why": "target_crossed", **info}
            if not armed and not manager.retains_confirmed_stop(position_id):
                # 赚得够，但曾完整托管、这一轮退回临时保护且最后回读没有在挂的止损：
                # 交给保护状态机，不平也不收紧，下一轮再看。
                return {"action": "skip", "why": "protection_unconfirmed", **info}
            trail = profit_mode == "trail"
            if trail and (rec.trailing_activation_pct is None or rec.trailing_stop_pct is None):
                trail = False  # 开仓没带移动止损：没有距离可用，退回保本收紧
                info["fallback"] = "no_trailing_config_breakeven"
            info["mode"] = "trail" if trail else "breakeven"
            if trail:
                read_book = getattr(self._exchange, "execution_book", None)
                try:
                    if read_book is None:
                        raise ProtectionIncomplete("缺同交易所盘口")
                    anchored = await manager.anchor_trailing(
                        position_id,
                        basis,
                        read_book,
                        distance_mult=distance_mult,
                        reason=reason,
                        card_key=card_key,
                    )
                except AnchorBookSpike as error:
                    detail = describe_error(error)
                    now = self._clock()
                    since = manager._current(position_id)[1].opposite_anchor_retry_since or now
                    waited = (now - since).total_seconds()
                    if anchor_retry_s > 0 and waited < anchor_retry_s:
                        if waited == 0:
                            manager._save(position_id, opposite_anchor_retry_since=now)
                        return {
                            "action": "pending",
                            "why": "anchor_book_spike_retry",
                            "error": detail,
                            "waited_s": waited,
                            **info,
                        }
                    await self._tighten_failed(
                        position_id, f"{label}锚定移动止损没做成", detail, then_close
                    )
                    return {"action": "close", "why": "evidence_unavailable", "error": detail}
                except Exception as error:
                    detail = describe_error(error)
                    await self._tighten_failed(
                        position_id, f"{label}锚定移动止损没做成", detail, then_close
                    )
                    return {"action": "close", "why": "evidence_unavailable", "error": detail}
                after = manager._current(position_id)[1]
                info.update(
                    anchor_price=after.opposite_anchor_price,
                    anchored_stop=anchored,
                    trailing_distance_pct=after.trailing_distance_pct,
                )
            else:
                if existing >= target if long else existing <= target:
                    return {"action": "kept", "why": "existing_stop_tighter", **info}
                manager.set_tightened_stop(position_id, target, reason, card_key)
            if not armed:
                # 曾完整托管、旧止损仍在挂，这一轮在临时保护：已落盘，由保护轮询换代。
                return {"action": "pending", "why": "waiting_protection_sync", **info}
            if any(leg.state in {"submit_pending", "cancel_pending"} for leg in state.legs):
                # 上一代（移动止损换代）还在提交 / 撤销中：已落盘，
                # 等它确认后由保护轮询再换，不算失败。
                return {"action": "pending", "why": "waiting_previous_generation", **info}
            try:
                result = await manager.sync(position_id)
            except Exception as error:
                detail = describe_error(error)  # sync 已把它写进 last_error
                await self._tighten_failed(
                    position_id,
                    f"{label}收紧止损换单未确认",
                    detail,
                    "已落盘，旧止损保留，保护轮询继续换",
                )
                return {"action": "pending", "why": "sync_unconfirmed", "error": detail, **info}
            info["live_stop_after"] = after_stop = manager.live_stop_price(position_id)
            if result == "exit":
                return {"action": "exiting", "why": "protection_exit", **info}
            if result == "armed" and after_stop == live:
                # 候选不比正在挂的紧（或没够最小步长）：交易所上没换，如实记「没动」。
                return {"action": "kept", "why": "live_stop_unchanged", **info}
            return {
                "action": "tightened" if result == "armed" else "pending",
                "why": f"sync_{result}",
                **info,
            }

    async def _tighten_failed(self, position_id: str, title: str, detail: str, then: str) -> None:
        """收紧失败：写托管保护的 last_error（不碰移动止损自己的错误字段），发 CRITICAL。"""
        tracked = self._registry.get(position_id)
        if tracked is None or tracked.server_protection is None:
            return
        text = f"{title}：{detail}"[:300]
        if tracked.server_protection.last_error != text:
            state = tracked.server_protection.model_copy(update={"last_error": text})
            self._registry.put(tracked.model_copy(update={"server_protection": state}))
        await self._alert(
            AlertLevel.CRITICAL,
            title,
            f"{tracked.record.symbol} {position_id}：{detail}；{then}",
            tracked.record.symbol,
            f"opposite_tighten:{position_id}",
            kind="order_failed",
        )

    async def _tighten_basis(self, tracked: TrackedPosition) -> EntryBasis:
        """实际开仓成交与手续费：移动止损已冻结的基准优先，没有再按开仓 CID 读。"""
        state, rec = tracked.server_protection, tracked.record
        cid = tracked.entry_client_order_id
        assert state is not None and cid is not None
        side: Literal["buy", "sell"] = "buy" if rec.side is Side.LONG else "sell"
        if (
            state.trailing_entry_quote is not None
            and state.trailing_entry_quantity is not None
            and state.trailing_fees_quote is not None
            and state.entry_order_id is not None
        ):
            return EntryBasis(
                symbol=rec.symbol,
                client_order_id=cid,
                order_id=state.entry_order_id,
                side=side,
                filled_quantity=state.trailing_entry_quantity,
                filled_quote=state.trailing_entry_quote,
                fee_usdt=state.trailing_fees_quote,
                entry_price=state.trailing_entry_quote / state.trailing_entry_quantity,
                order_status=state.entry_status or "UNKNOWN",
            )
        read_basis = getattr(self._exchange, "entry_basis", None)
        if read_basis is None:
            raise ProtectionIncomplete("缺实际入场成交与费用证据")
        basis = await read_basis(rec.symbol, cid)
        if (
            not isinstance(basis, EntryBasis)
            or basis.client_order_id != cid
            or basis.order_id != state.entry_order_id
            or basis.symbol != rec.symbol
            or basis.side != side
            or basis.order_status != "FILLED"
        ):
            raise ProtectionIncomplete("入场基准身份或完整性不一致")
        return basis

    async def _trigger_reference(
        self, manager: BracketManager, symbol: str, long: bool, working_type: str
    ) -> float:
        """新止损腿按哪个价触发就拿哪个价比：带交易所时间、5 秒内的同环境标记价（和保护状态机
        换代时同一个读法），或同环境盘口（多看买一、空看卖一）。"""
        value: float | None = None
        if working_type == "MARK_PRICE":
            value = await manager._fresh_mark(symbol)
        else:
            read_book = getattr(self._exchange, "execution_book", None)
            if read_book is not None:
                book = await read_book(symbol)
                value = book.bid if long else book.ask
        if value is None or not math.isfinite(value) or value <= 0:
            raise ExchangeError(f"收紧止损的触发价源（{working_type}）读不到")
        return float(value)

    async def _close_directly(
        self,
        tracked: TrackedPosition,
        reason: CloseReason,
        via_hb: Exception | None,
        *,
        title: str = _HB_FAILED_TITLE,
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
        if await self._direct_first_ready(tracked):
            return await self._close_hosted_direct_first(tracked, reason, via_hb, title=title)
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
        if held > 0:
            order = await self._submit_direct_close(tracked, held)
            if order is None or order.is_terminal is not True:
                return False
            tracked = self._registry.get(rec.position_id) or tracked
            held = await self._held_quantity(rec.symbol)
            if held is None or held > 0:
                return False  # 终态部分成交；下一轮确认真实残量后才允许下一代
        return await self._settle_direct_close(tracked, reason, via_hb, title=title)

    async def _submit_direct_close(
        self, tracked: TrackedPosition, held: float
    ) -> IdentifiedCloseOrder | None:
        """按新鲜余仓提交下一代只减仓市价单；CID 与数量先落盘，结果未知不重发。

        返回已核对身份的订单（可能尚未终态），被拒、未知或无法核对返回 None。"""
        rec = tracked.record
        place = getattr(self._exchange, "place_close", None)
        if place is None:
            return None  # 缺身份可恢复的能力，不能偷偷降级成无 CID 下单
        generation = tracked.direct_close_generation + 1
        cid = f"dc-{rec.position_id}-{generation}"
        if len(cid) > 36:
            return None
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
        order: IdentifiedCloseOrder | None
        try:
            order = await place(rec.symbol, close_side, quantity, cid)
        except (IdentifiedCloseRejected, IdentifiedCloseNotSent) as error:
            status = "REJECTED" if isinstance(error, IdentifiedCloseRejected) else "NOT_SENT"
            self._direct_close_void(rec.position_id, tracked, status)
            return None  # 明确本请求未生效；下轮仅依据新鲜余仓生成下一代
        except IdentifiedCloseCancelledBeforeSend:
            self._direct_close_void(rec.position_id, tracked, "NOT_SENT")
            raise
        except IdentifiedCloseUnknown as error:
            order = error.observed
            if order is None:
                return None
        except Exception:
            return None  # 保留原 CID / pending，禁止暂时查无结果后重发
        if order is None or not self._accept_direct_order(tracked, order):
            return None
        return order

    def _direct_close_void(self, pid: str, tracked: TrackedPosition, status: str) -> None:
        current = self._registry.get(pid) or tracked
        outcomes = {**current.direct_close_outcomes, str(current.direct_close_generation): status}
        self._registry.put(
            current.model_copy(
                update={
                    "direct_close_pending": False,
                    "direct_close_order_status": status,
                    "direct_close_outcomes": outcomes,
                }
            )
        )

    def _hosted_exit_working(self, position_id: str) -> bool:
        """An exit is already working and the background tick will finish it: a reduce-only order
        still open or filled awaiting the flat read, a keep-position HB stop whose request was
        accepted, or a triggered leg whose child order is not terminal yet. A definitely failed
        direct order (rejected, never sent, expired or cancelled) or an HB stop with an unknown
        result is not, so those stay loud failures."""
        tracked = self._registry.get(position_id)
        if (
            tracked is None
            or tracked.server_protection is None
            or tracked.record.status is not PositionStatus.CLOSING
            or tracked.direct_close_order_status in _DIRECT_CLOSE_FAILED
        ):
            return False
        if tracked.direct_close_pending or tracked.direct_close_order_status == "FILLED":
            return True
        if tracked.hb_close_pending and not tracked.hb_close_unknown:
            return True
        try:
            return self._protection is not None and self._protection.exit_leg_working(position_id)
        except ProtectionIncomplete:
            return False

    async def _direct_first_ready(self, tracked: TrackedPosition) -> bool:
        """``direct`` 模式下这次能否先下直连：只在一切都明确时，否则走 ``hb_first``。

        HB 已在自己平仓（时限到、SHUTTING_DOWN、有入场单以外的订单）、任何一条腿提交 / 撤单 /
        触发状态不明、入场未终态、已有未决的停止或直连请求，都不先下单。"""
        state, manager = tracked.server_protection, self._protection
        rec = tracked.record
        if (
            self._hosted_close_mode != "direct"
            or state is None
            or manager is None
            or self._exchange is None
            or state.stage in {"cleanup", "done"}
            or (state.stage == "exiting" and not self._direct_when_exiting)
            or not state.entry_terminal
            or tracked.direct_close_pending
            or tracked.hb_close_pending
            or tracked.hb_close_unknown
            or rec.executor_id is None
            or tracked.entry_client_order_id is None
            or not manager.legs_quiet(rec.position_id)
        ):
            return False
        try:
            detail = await self._api.get_executor(rec.executor_id)
        except Exception:
            return False
        info = (detail or {}).get("custom_info")
        return bool(
            executor_query_state(detail, rec.executor_id) == "active"
            and (detail or {}).get("status") == "RUNNING"
            and isinstance(info, dict)
            and info.get("order_ids") == [tracked.entry_client_order_id]
        )

    async def _close_hosted_direct_first(
        self,
        tracked: TrackedPosition,
        reason: CloseReason,
        via_hb: Exception | None,
        *,
        title: str,
    ) -> bool:
        """先下只减仓市价单，再只停不平 HB；交易所止损腿保留到核实零仓后由收尾撤掉。

        HB 的平仓单在单向模式也是只减仓，止损子单同样只减仓：任何一路先成交，其余只会被拒或
        少成交，不会反向开仓。下单后的结果未知仍按原 CID 只查不重发。"""
        rec = tracked.record
        pid = rec.position_id
        assert self._protection is not None
        self._protection.mark_exiting(pid)
        tracked = (self._registry.get(pid) or tracked).model_copy(update={"direct_first": True})
        self._registry.put(tracked)
        held = await self._held_quantity(rec.symbol)
        if held is None or held > (tracked.entry_original_quantity or rec.quantity) * (1 + 1e-10):
            return False
        order = await self._submit_direct_close(tracked, held) if held > 0 else None
        await self._stop_hb_keep_position(pid)
        if held > 0 and (order is None or order.is_terminal is not True):
            if order is None and await self._held_quantity(rec.symbol) == 0:
                self._flat_awaiting_cleanup.add(pid)  # 被拒但已零仓：多半止损或 HB 先平了
            return False
        held = await self._held_quantity(rec.symbol)
        if held is None or held > 0:
            return False  # 终态部分成交；下一轮按真实余仓走原流程补下一代
        self._flat_awaiting_cleanup.add(pid)
        if not await self._prepare_server_exit(self._registry.get(pid) or tracked):
            return False
        return await self._settle_direct_close(
            self._registry.get(pid) or tracked, reason, via_hb, title=title
        )

    async def _stop_hb_keep_position(self, position_id: str) -> bool:
        """只停不平：HB 不再动这笔仓。请求前落盘；返回 False = 没发、结果未知或已发过还在等终态。

        HB 仍报 RUNNING 时才补发：已落盘待停、但不是本进程发的（落盘后、请求前崩溃），或上次
        结果未知（hb_close_unknown）且 ``hb_stop_unknown_retries`` 还有次数、退避已到。
        keep_position=True 的停止从不卖出，重发无害；对账员不替本程序的执行器重发停止。
        次数按进程计，重启后重新计。"""
        current = self._registry.get(position_id)
        if current is None or current.record.executor_id is None:
            return False
        eid = current.record.executor_id
        retry = current.hb_close_pending and current.hb_close_unknown
        if current.hb_close_pending:
            if retry and not self._hb_stop_retry_due(position_id):
                return False
            if not retry and position_id in self._hb_stop_sent:
                return False
            try:
                detail = await self._api.get_executor(eid)
            except Exception:
                return False
            if not (
                executor_query_state(detail, eid) == "active"
                and (detail or {}).get("status") == "RUNNING"
            ):
                return False
        else:
            self._registry.put(current.model_copy(update={"hb_close_pending": True}))
        self._hb_stop_sent.add(position_id)
        if retry:
            attempts, _ = self._hb_stop_retries.get(position_id, (0, self._clock()))
            self._hb_stop_retries[position_id] = (attempts + 1, self._clock())
        try:
            await self._api.stop_executor(eid, keep_position=True)
        except Exception:
            latest = self._registry.get(position_id) or current
            self._registry.put(latest.model_copy(update={"hb_close_unknown": True}))
            self._hb_stop_retries.setdefault(position_id, (0, self._clock()))
            return False
        if retry:
            latest = self._registry.get(position_id) or current
            self._registry.put(latest.model_copy(update={"hb_close_unknown": False}))
        self._hb_stop_retries.pop(position_id, None)
        return True

    def _hb_stop_retry_due(self, position_id: str) -> bool:
        attempts, since = self._hb_stop_retries.get(position_id, (0, self._clock()))
        self._hb_stop_retries.setdefault(position_id, (attempts, since))
        if attempts >= self._hb_stop_unknown_retries:
            return False
        wait: float = _HB_STOP_RETRY_BASE_S * 2.0**attempts
        return bool((self._clock() - since).total_seconds() >= wait)

    async def _close_evidence(
        self, tracked: TrackedPosition
    ) -> tuple[float | None, IdentifiedCloseOrder | None]:
        """我方直连单各代一共成交多少（None = 说不清）和最后一代的订单回读。"""
        generation = tracked.direct_close_generation
        if generation == 0:
            return 0.0, None
        order = await self._recover_direct_order(tracked)
        if order is not None and order.is_terminal is True:
            latest = order.filled_quantity
        elif order is None and tracked.direct_close_order_status in {"REJECTED", "NOT_SENT"}:
            latest = 0.0
        else:
            return None, order
        earlier = await self._earlier_direct_fills(tracked)
        return (None if earlier is None else earlier + latest), order

    async def _earlier_direct_fills(self, tracked: TrackedPosition) -> float | None:
        """前几代各成交多少；任何一代说不清就返回 None。

        记为 NOT_SENT / REJECTED 的那代没进交易所，成交 0、不必查；记为交易所终态的按确定的
        CID 回读，查无、出错或对不上都算说不清（它确实到过交易所）；没有记录的（加记录之前
        的仓）同样说不清。"""
        read = getattr(self._exchange, "close_order", None)
        if read is None:
            return None
        rec = tracked.record
        close_side = "sell" if rec.side is Side.LONG else "buy"
        total = 0.0
        for generation in range(1, tracked.direct_close_generation):
            outcome = tracked.direct_close_outcomes.get(str(generation))
            if outcome in {"NOT_SENT", "REJECTED"}:
                continue
            if outcome is None:
                return None
            cid = f"dc-{rec.position_id}-{generation}"
            try:
                order = await read(rec.symbol, cid)
            except Exception:
                return None
            if not (
                isinstance(order, IdentifiedCloseOrder)
                and order.client_order_id == cid
                and order.symbol == rec.symbol
                and order.close_side == close_side
                and order.reduce_only
                and order.is_terminal is True
                and math.isfinite(order.filled_quantity)
            ):
                return None
            total += order.filled_quantity
        return total

    def _close_fill(
        self, tracked: TrackedPosition, order: IdentifiedCloseOrder | None
    ) -> CloseFill:
        """只有唯一一代、它独自全额平掉整笔入场量时，它的均价才是出场价；否则交成交明细回填。"""
        state = tracked.server_protection
        entry = (
            state.entry_filled_quantity
            if state is not None and state.entry_filled_quantity
            else tracked.record.quantity
        )
        if (
            tracked.direct_close_generation == 1
            and order is not None
            and order.is_terminal is True
            and math.isclose(order.filled_quantity, order.quantity, rel_tol=1e-10)
            and math.isclose(order.quantity, entry, rel_tol=1e-10)
        ):
            return CloseFill(order.average_price, tracked.direct_close_fee)
        return CloseFill()

    def _hosted_close_cause(
        self, tracked: TrackedPosition, ours: float | None, legs: list[tuple[CloseReason, float]]
    ) -> str:
        path = (
            "先直连、后停 HB（direct）" if tracked.direct_first else "先停 HB、后直连（hb_first）"
        )
        generation = tracked.direct_close_generation
        if generation == 0:
            ours_text = "没有下直连单"
        else:
            filled = "成交量未知" if ours is None else f"我方成交 {ours:g}"
            ours_text = f"直连只减仓单 {generation} 代，{filled}"
        parts = [path, ours_text]
        parts += [f"交易所 {reason.value} 腿成交 {qty:g}" for reason, qty in legs]
        return "；".join(parts)

    async def _settle_direct_close(
        self,
        tracked: TrackedPosition,
        reason: CloseReason,
        via_hb: Exception | None,
        *,
        title: str,
    ) -> bool:
        rec = tracked.record
        if not await self.entry_finished(rec.position_id):
            return False
        if await self._held_quantity(rec.symbol) != 0:
            return False  # entry 查询期间可能出现最后一笔迟到成交
        if tracked.server_protection is not None and not await self._server_flat_ready(
            rec.position_id
        ):
            return False
        tracked = self._registry.get(rec.position_id) or tracked
        ours, order = await self._close_evidence(tracked)
        tracked = self._registry.get(rec.position_id) or tracked
        legs = (
            self._protection.leg_fills(rec.position_id)
            if tracked.server_protection is not None and self._protection is not None
            else []
        )
        if ours == 0 and legs:
            reason = legs[0][0]  # 我方一笔没成交，是交易所的腿平掉的
        self.mark_closed_directly(rec.position_id, reason, self._close_fill(tracked, order))
        if (
            self._exit_fills is not None
            and order is not None
            and order.is_terminal is True
            and order.filled_quantity > 0
            and order.update_time is not None
        ):
            self._exit_fills.record(rec.position_id, order.update_time, "order")
        closed = self._registry.get(rec.position_id)
        hosted_normal = (
            tracked.server_protection is not None and via_hb is None and title == _HB_FAILED_TITLE
        )
        self._flat_awaiting_cleanup.discard(rec.position_id)
        self._exit_started.pop(rec.position_id, None)
        self._close_slow_alerted.discard(rec.position_id)
        if closed is not None:
            body = _close_text(closed.record)
            if (shown := _shown_detail(closed, closed.record.close_reason)) is not None:
                body += f"\n原因：{shown}"
            if hosted_normal:
                body += "\n" + self._hosted_close_cause(tracked, ours, legs)
            await self._notice("平仓完成", body, closed.record, "closed")
        if not hosted_normal:
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
                if rec.executor_id is None or not await self._stop_hb_keep_position(position_id):
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
            pid = initial.record.position_id
            ended = initial.record.status in {PositionStatus.CLOSED, PositionStatus.FAILED}
            if ended:
                self._hb_stop_retries.pop(pid, None)
            if initial.record.status is not PositionStatus.CLOSING:
                self._exit_started.pop(pid, None)
                self._close_slow_alerted.discard(pid)
            # 同步和收尾分开计数：进了收尾，同步阶段的失败不再累计；结束的仓不留计数。
            key = (pid, "cleanup" if initial.exit_cleanup_pending else "sync")
            if ended or initial.exit_cleanup_pending:
                self._tick_failures.pop((pid, "sync"), None)
            if ended and not initial.exit_cleanup_pending:
                self._tick_failures.pop((pid, "cleanup"), None)
            if initial.server_protection is None or initial.server_protection.stage == "done":
                self._tick_failures.pop((pid, "cleanup"), None)
                continue
            if initial.record.status is PositionStatus.CLOSING:
                await self._alert_hosted_close_slow(initial)
            try:
                if initial.exit_cleanup_pending:
                    await self._finish_exit_cleanup(pid)
                    self._tick_failures.pop(key, None)
                    continue
                if ended:
                    continue  # 未明 create 的 FAILED 仍阻同币；不能在后台把未知归为没提交。
                current = await self._sync_one(initial)
                if current.record.status is PositionStatus.OPEN:
                    await self._update_server_trailing(pid)
            except TrailingBookUnavailable as error:
                self._tick_failures.pop(key, None)  # 同步已成功，只是盘口不可用；另有告警
                await self._alert_trailing_book(pid, initial.record.symbol, error)
            except Exception as error:
                await self._protection_tick_failed(key, initial.record.symbol, error)
            else:
                self._tick_failures.pop(key, None)

    async def _alert_hosted_close_slow(self, tracked: TrackedPosition) -> None:
        """A quiet in-progress close (CloseInProgress) must still surface if it never settles,
        e.g. an accepted HB stop whose executor never reaches terminal: warn once per close."""
        pid = tracked.record.position_id
        started = self._exit_started.setdefault(pid, self._clock())
        waited = (self._clock() - started).total_seconds()
        if waited < self._close_slow_after_s or pid in self._close_slow_alerted:
            return
        self._close_slow_alerted.add(pid)
        state = tracked.server_protection
        stage = state.stage if state is not None else "?"
        await self._alert(
            AlertLevel.WARN,
            "平仓后迟迟没结束",
            f"{tracked.record.symbol} {pid}：平仓进行中已 {waited:.0f} 秒（托管阶段 {stage}，"
            f"HB 停止待终态 {'是' if tracked.hb_close_pending else '否'}，"
            f"直连单 {tracked.direct_close_order_status or '无'}），保护保留，同币不能新开；请核实",
            tracked.record.symbol,
            f"close_slow:{pid}",
            kind="order_failed",
        )

    async def _protection_tick_failed(
        self, key: tuple[str, str], symbol: str, error: Exception
    ) -> None:
        """连续失败到 ``protection_tick_alert_after`` 轮才发紧急告警，之前只记日志。"""
        pid = key[0]
        rounds = self._tick_failures.get(key, 0) + 1
        self._tick_failures[key] = rounds
        detail = _tick_error_detail(error)
        if rounds < self._tick_alert_after:
            log.warning("托管保护本轮未确认（第 %d 轮）%s：%s", rounds, pid, detail)
            return
        streak = f"连续 {rounds} 轮，" if self._tick_alert_after > 1 else ""
        await self._alert(
            AlertLevel.CRITICAL,
            "托管保护本轮未确认",
            f"{symbol} {pid}：{streak}{detail}；保留最后确认保护并等待核验",
            symbol,
            f"server_tick:{pid}",
            kind="order_failed",
        )

    async def _alert_stop_clamped(self, pid: str, symbol: str, text: str) -> None:
        """止损目标已越过标记价：被夹到可挂的最紧价，或本轮没换。价格已经穿过想要的止损位。"""
        await self._alert(
            AlertLevel.CRITICAL,
            "止损目标已越过标记价",
            f"{symbol} {pid}：{text}",
            symbol,
            f"server_stop_clamped:{pid}",
            kind="order_failed",
        )

    async def _alert_trigger_rejected(self, pid: str, symbol: str, tag: str, text: str) -> None:
        await self._alert(
            AlertLevel.CRITICAL,
            "条件单触发后子单被交易所拒绝",
            f"{symbol} {pid}：{text}",
            symbol,
            f"server_trigger_rejected:{pid}:{tag}",
            kind="order_failed",
        )

    async def _alert_trailing_book(
        self, pid: str, symbol: str, error: TrailingBookUnavailable
    ) -> None:
        """这一轮没拿到能用的盘口：不更新移动止损，已确认保护不变。单独的去重键，不占真正保护
        失败（server_tick）的冷却名额；已激活的移动止损连续太久拿不到，升级成紧急。"""
        tracked = self._registry.get(pid)
        state = tracked.server_protection if tracked is not None else None
        after = self._protection.trailing_book_critical_after_s if self._protection else 0.0
        since = state.trailing_failing_since if state is not None else None
        held_s = (self._clock() - since).total_seconds() if since is not None else 0.0
        if state is not None and state.trailing_activated and after > 0 and held_s >= after:
            last = state.trailing_observed_at
            last_text = last.strftime("%m-%d %H:%M:%S UTC") if last is not None else "从没有"
            await self._alert(
                AlertLevel.CRITICAL,
                "移动止损长时间没有有效盘口",
                f"{symbol} {pid}：已激活的移动止损连续 {held_s / 60:.0f} 分钟没有有效盘口，"
                f"最后一次成功观测 {last_text}；最近原因：{describe_error(error)}。"
                "峰值和止损价停在最后一次成功观测，已挂的止损 / 止盈 / 保命单不变",
                symbol,
                f"server_trailing_book_stuck:{pid}",
                kind="order_failed",
            )
            return
        await self._alert(
            AlertLevel.WARN,
            "移动止损本轮缺有效盘口",
            f"{symbol} {pid}：{describe_error(error)}；"
            "本轮不更新移动止损，已确认的止损 / 止盈 / 保命单不变",
            symbol,
            f"server_trailing_book:{pid}",
            kind="order_failed",
        )

    async def venue_mark(self, symbol: str) -> tuple[float, datetime] | None:
        """持仓管家用的执行所标记价（和保护状态机同一个读法 ``execution_mark``）和它的交易所时间。

        同一个币 ``VENUE_MARK_REUSE_S`` 秒内只读一次、并发的复查共用一次读，免得每笔复查各打一次
        REST；读不到返回 None（管家这轮不按价判）。"""
        read = getattr(self._exchange, "execution_mark", None)
        if read is None:
            return None
        cached = self._venue_marks.get(symbol)
        now = self._clock()
        if cached is not None and 0 <= (now - cached[0]).total_seconds() < VENUE_MARK_REUSE_S:
            return cached[1].mark, cached[1].source_at
        task = self._venue_reads.get(symbol)
        if task is None:
            task = asyncio.ensure_future(read(symbol, max_age_s=VENUE_MARK_READ_MAX_AGE_S))
            self._venue_reads[symbol] = task
            task.add_done_callback(lambda _t: self._venue_reads.pop(symbol, None))
        try:
            ev = await asyncio.shield(task)
        except Exception:
            log.debug("执行所标记价读不到 %s", symbol, exc_info=True)
            return None
        if not isinstance(ev, MarkEvidence) or ev.symbol != symbol or not ev.mark > 0:
            return None
        self._venue_marks[symbol] = (now, ev)
        return ev.mark, ev.source_at

    def protection_exit_in_flight(self, position_id: str) -> tuple[str, str, str] | None:
        """交易所保护腿在途（种类, 身份, 说明）：``exiting`` 已触发 / 正在退出收尾（身份就是
        ``exiting``，触发 → 退出 → 收尾算同一次）；``replacing`` 托管过的仓正在换代（有腿在提交
        或撤单中，身份是保护代数）。都不是返回 None。"""
        tracked = self._registry.get(position_id)
        state = tracked.server_protection if tracked is not None else None
        if state is None:
            return None
        if state.stage in ("exiting", "cleanup"):
            return "exiting", "exiting", state.stage
        if any(leg.triggered_at is not None and leg.state == "confirmed" for leg in state.legs):
            return "exiting", "exiting", "leg_triggered"
        moving = [leg for leg in state.legs if leg.state in ("submit_pending", "cancel_pending")]
        if state.ever_armed and moving:
            return (
                "replacing",
                f"gen-{state.generation}",
                f"第 {state.generation} 代 {moving[0].kind} {moving[0].state}",
            )
        return None

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
                    **_frozen_peak_size(
                        initial.frozen_peak_exit, initial.record.side, initial.stop_loss_pct
                    ),
                    "triggered": True,
                    "detail": "pending_exit",
                }
        basis = book = None
        held: list[ExchangePosition] = []
        error_detail = ""
        symbol = initial.record.symbol
        read_book = getattr(self._exchange, "execution_book", None)
        try:
            read_basis = getattr(self._exchange, "entry_basis", None)
            read_positions = getattr(self._exchange, "positions_evidence", None)
            if (
                read_basis is None
                or read_book is None
                or read_positions is None
                or initial.entry_client_order_id is None
            ):
                raise ExchangeError("Execution evidence reader or entry CID is unavailable")
            async with asyncio.timeout(FROZEN_PEAK_EVIDENCE_TIMEOUT_S):
                basis = await read_basis(symbol, initial.entry_client_order_id)
                held = [p for p in await read_positions() if p.symbol == symbol]
        except Exception as error:
            error_detail = f"evidence_unavailable:{type(error).__name__}:{str(error)[:200]}"
        # 盘口在锁外读（慢采样不能拖住平仓），但等同币锁时托管保护那一轮可能正占着锁好几秒
        # （回读宽限时更久），拿到锁时盘口常已超过 5 秒。这种情况放锁重读一次；5 秒新鲜度、
        # 未来时刻、乱序的检查照旧由 frozen_peak_update 在判断时做，一条不放宽。
        for last_try in (False, True):
            if not error_detail:
                try:
                    assert read_book is not None
                    # 盘口超过 5 秒本来就不能用：读盘口单独按 5 秒超时，不占满 12 秒总预算。
                    async with asyncio.timeout(FROZEN_BOOK_MAX_AGE_S):
                        book = await read_book(symbol)
                except TimeoutError:
                    error_detail = "evidence_unavailable:book_timeout:"
                except Exception as error:
                    error_detail = (
                        f"evidence_unavailable:book:{type(error).__name__}:{str(error)[:200]}"
                    )
            async with self._registry.symbol_lock(symbol):
                tracked = self._registry.get(position_id)
                if (
                    tracked is None
                    or tracked.record.status is not PositionStatus.OPEN
                    or tracked.frozen_peak_exit != initial.frozen_peak_exit
                    or tracked.entry_client_order_id != initial.entry_client_order_id
                ):
                    return None
                if (
                    not last_try
                    and isinstance(book, ExecutionBook)
                    and book.source_at.utcoffset() is not None
                    and (self._clock() - book.source_at).total_seconds() > FROZEN_BOOK_MAX_AGE_S
                ):
                    continue
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
                    return {
                        **state.model_dump(mode="json"),
                        **_frozen_peak_size(state, tracked.record.side, tracked.stop_loss_pct),
                        "triggered": False,
                    }
                self._registry.put(tracked.model_copy(update={"frozen_peak_exit": state}))
                triggered = state.status == "triggered"
                assert book is not None
                at = book.bid if tracked.record.side is Side.LONG else book.ask
                return {
                    **state.model_dump(mode="json"),
                    **_frozen_peak_size(
                        state,
                        tracked.record.side,
                        tracked.stop_loss_pct,
                        at if triggered else None,
                    ),
                    "triggered": triggered,
                }
        return None  # 不会到这里：第二次一定在锁内返回。

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
                missing = ProtectionIncomplete("缺实际入场成交与费用证据，保持最后确认保护")
                self._protection.record_trailing_error(position_id, missing)
                raise missing
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
                try:
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
                except Exception as error:
                    self._protection.record_trailing_error(position_id, error)
                    raise
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
                missing = ProtectionIncomplete("缺同交易所盘口源时钟，保持最后确认保护")
                self._protection.record_trailing_error(position_id, missing)
                raise missing
            # 盘口交给 observe_trailing 在三腿回读之后再读：在这里先读，回读完常已超过 5 秒。
            if await self._protection.observe_trailing(position_id, basis, read_book):
                await self._protection.sync(position_id)

    async def _prepare_server_exit(self, tracked: TrackedPosition) -> bool:
        """不再让 HB 发第二路平仓；先停入场、查原单，再清理条件单/已触发子单。"""
        if self._protection is None:
            return False  # 重启后缺能力不能退回旧流程，留下真实保护等待处理。
        rec = tracked.record
        if not await self._hb_exit_finished(tracked):
            if rec.executor_id is None or not await self._stop_hb_keep_position(rec.position_id):
                return False
            if not await self._hb_exit_finished(tracked):
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

    def _stop_first_covered(self, tracked: TrackedPosition) -> bool:
        """``stop_first`` 托管仓、交易所止损已确认：缺保命单交给保护状态机的宽限，开仓时不平。"""
        manager = self._protection
        state = tracked.server_protection
        return bool(
            manager is not None
            and state is not None
            and manager.leg_order == "stop_first"
            and state.stage not in {"exiting", "cleanup", "done"}
            and manager.retains_confirmed_stop(tracked.record.position_id)
        )

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
            if state is not None and state.stage == "exiting" and state.exit_reason is not None:
                # 另一条腿已触发：没查清的腿只影响退出核验（prepare_exit 仍会等它），
                # 不是对账平仓的理由。
                reason = state.exit_reason
                await self._alert(
                    AlertLevel.WARN,
                    "托管保护已触发，按原因退出",
                    f"{rec.symbol} {rec.position_id}：已触发 {reason.value}，按此原因退出；"
                    f"其余腿尚未查清（{describe_error(error)}），退出前继续核验",
                    rec.symbol,
                    f"server_triggered_exit:{rec.position_id}",
                    kind="reconcile",
                )
                current = self._set(current, status=PositionStatus.CLOSING, requested_close=reason)
                await self._close_directly(current, reason, None)
                return self._registry.get(rec.position_id) or current
            keep_previous = (
                state is not None
                and state.ever_armed
                and state.stage != "exiting"
                and manager.retains_confirmed_stop(rec.position_id)
            )
            held = None if keep_previous else manager.prearm_grace(rec.position_id, error)
            if held is not None:
                await self._alert(
                    AlertLevel.CRITICAL,
                    "普通托管保护未完整确认",
                    f"{rec.symbol} {rec.position_id}：{describe_error(error)}；"
                    f"尚未完整托管，在未托管宽限内（已 {held:.0f} / "
                    f"{manager.prearm_grace_s:.0f} 秒），"
                    "保留已确认保命单/止损，暂不平仓，下一轮重试；超时仍未确认就平仓",
                    rec.symbol,
                    f"server_prearm_grace:{rec.position_id}",
                    kind="lifeboat_missing"
                    if isinstance(error, ProtectionIncomplete) and error.backup_only
                    else "order_failed",
                )
                return current
            await self._alert(
                AlertLevel.CRITICAL,
                "普通托管保护未完整确认",
                f"{rec.symbol} {rec.position_id}：{describe_error(error)}；"
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
            tracked = self._note_close_detail(
                tracked, f"托管保护未完整确认，不在未托管宽限内：{describe_error(error)}"
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
        outcomes = current.direct_close_outcomes
        if order.is_terminal is True:
            outcomes = {**outcomes, str(current.direct_close_generation): order.status}
        self._registry.put(
            current.model_copy(
                update={
                    "direct_close_pending": order.is_terminal is not True,
                    "direct_close_order_status": order.status,
                    "direct_close_order_id": order.order_id,
                    "direct_close_price": order.average_price,
                    "direct_close_outcomes": outcomes,
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
            tracked = self._note_close_detail(tracked, "HB 报告了另一张开仓单")
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
            shown = _shown_detail(tracked, reason)
            self._record(
                Outcome.CLOSE,
                f"{rec.symbol} 平仓（{reason.value if reason else '未知'}"
                + (f"：{shown}" if shown else "")
                + f"），净盈亏 {net:+.2f}U",
                closed,
                output={
                    "net": net,
                    "reason": reason.value if reason else None,
                    "fees": closed.fees_usdt,
                    **({"detail": shown} if shown else {}),
                    # 曾下令平仓的人（面板可以显示「巡逻兵曾下令」）；和 reason 一样就不重复写
                    **(
                        {"requested_by": tracked.requested_close.value}
                        if tracked.requested_close not in (None, reason)
                        else {}
                    ),
                },
            )
            body = _close_text(closed) + (f"\n原因：{shown}" if shown else "")
            await self._notice("平仓完成", body, closed, "closed")
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
                if exit_ is not None and self._exit_fills is not None:
                    self._exit_fills.record(rec.position_id, exit_.last, "trades")
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
        by_kind: dict[Any, int] = {}
        for t in live:
            if t.record.signal_kind is not None:
                by_kind[t.record.signal_kind] = by_kind.get(t.record.signal_kind, 0) + 1
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
            open_by_kind=by_kind,
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
