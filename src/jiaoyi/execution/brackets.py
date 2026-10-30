"""交易所托管保护（保命单 + 普通止损）的可恢复编排：真实保护回读完成前不宣称已托管。

盈利出场靠移动止损。``[fast_loop] trailing_native`` 开着时，带移动止损的仓在
交易所只挂两条腿：固定止损 + 币安自己跟价的 TRAILING_STOP_MARKET（``br-ts-``），不挂保命单，
程序也不再每秒换代止损腿；那张单被拒、提交结果未知、回读不了时这笔仓放弃它，照旧挂保命单、
程序自己跟（换代止损腿）。

后备止盈单（``fallback_take_profit``）：没有币安移动止损在保护盈利一侧（放弃了它、开关关着、
这笔没带移动止损）且程序自己跟的移动止损还没激活时，止损和保命单确认在挂之后再挂一张
``br-tp-`` 止盈单（账本 ``take_profit_price``）；移动止损一激活、或币安移动止损确认在挂，就撤掉它。
它是独立的一条腿：挂不上、查不到、撤不掉都只记日志和告警，不让止损 / 保命单的同步失败，
被拒后这笔不再挂。开关关着时还在的止盈腿（含旧版本挂下的）照样撤掉；它若先触发，按止盈退出。

程序锁只能串行本程序；HB 时限单和交易所条件子单仍能独立触发。未知提交只查原 CID，
任何一代触发/子单未知都会停止改价；保护撤清且开仓残单终态后才允许程序另发市价退出。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal, Protocol

from jiaoyi.contracts import LifeboatOrder, Side
from jiaoyi.contracts.position import CloseReason, LifeboatStatus
from jiaoyi.core.trailing_price import activation_price
from jiaoyi.execution.clock import Clock, utc_now
from jiaoyi.execution.exchange import (
    CALLBACK_RATE_STEP,
    BracketSubmissionRejected,
    BracketSubmissionUnknown,
    EntryBasis,
    ExchangeBracket,
    ExchangeEntryOrder,
    ExchangeError,
    ExchangePosition,
    ExecutionBook,
    MarkEvidence,
    NormalizedBracketSpec,
    WorkingType,
    callback_rate,
)
from jiaoyi.execution.registry import (
    LegPurpose,
    PositionRegistry,
    ProtectiveLeg,
    ServerProtection,
    TrackedPosition,
)
from jiaoyi.execution.trailing import (
    BOOK_MAX_AGE_S,
    TrailingBookUnavailable,
    trailing_update,
)

log = logging.getLogger(__name__)


class BracketExchange(Protocol):
    async def position_mode(self, *, fresh: bool = False) -> Literal["one_way", "hedge"]: ...
    async def positions_evidence(self) -> list[ExchangePosition]: ...
    async def assert_entry_clear(self, symbol: str) -> None: ...
    async def entry_order(self, symbol: str, client_order_id: str) -> ExchangeEntryOrder | None: ...
    async def normalized_bracket_spec(
        self, symbol: str, quantity: float, trigger_price: float
    ) -> NormalizedBracketSpec: ...
    async def place_bracket(
        self,
        symbol: str,
        close_side: Literal["buy", "sell"],
        quantity: float,
        trigger_price: float,
        client_order_id: str,
        *,
        kind: Literal["stop_loss", "take_profit"],
        working_type: WorkingType,
    ) -> ExchangeBracket: ...
    async def get_bracket(
        self, symbol: str, *, algo_id: str | None = None, client_order_id: str | None = None
    ) -> ExchangeBracket | None: ...
    async def open_brackets(self, symbol: str | None = None) -> list[ExchangeBracket]: ...
    async def cancel_bracket(
        self, symbol: str, algo_id: str, *, expected_client_order_id: str
    ) -> None: ...

    async def place_lifeboat(
        self,
        symbol: str,
        close_side: Literal["buy", "sell"],
        quantity: float,
        trigger_price: float,
        client_order_id: str,
    ) -> ExchangeBracket: ...
    async def get_lifeboat(
        self, symbol: str, *, client_order_id: str, algo_id: str | None = None
    ) -> ExchangeBracket | None: ...
    async def cancel_lifeboat(
        self, symbol: str, algo_id: str, *, expected_client_order_id: str
    ) -> None: ...
    async def execution_mark(self, symbol: str) -> MarkEvidence: ...
    async def place_trailing(
        self,
        symbol: str,
        close_side: Literal["buy", "sell"],
        quantity: float,
        callback_rate: float,
        client_order_id: str,
        *,
        activate_price: float | None,
        working_type: WorkingType,
    ) -> ExchangeBracket: ...


NATIVE_CALLBACK_MAX = Decimal("10")
"""交易所移动止损回撤比例上限（%）。币安 algo 文档写 0.1–20，旧下单接口是 0.1–10，取窄的；
超过就这笔仓不用交易所移动止损。"""
NATIVE_PENDING_MAX_S = 30.0
"""从第一次真去挂交易所移动止损（后台保护轮走到挂单这一步）起，这么多秒还没挂上（入场成本或
标记价一直读不到）就放弃，照旧挂保命单、程序自己跟：等的这段只有固定止损。不从武装或同步算：
别的路径（持仓轮询、对账）也会同步，后台保护轮第一次走到挂单前可能已经过了几十秒。"""
LEGACY_PENDING_FALLBACK = "武装后 30 秒还没挂上交易所移动止损"
"""旧版本从同步就开始计时，挂单一次都没试就按这个原因放弃了；这种仓（没有任何交易所移动止损
腿）下一轮同步时清掉原因，重新去挂。"""
NATIVE_READ_GRACE_S = 30.0
"""在挂的交易所移动止损连续这么多秒回读不好才放弃它（只在内存里计时，重启重新算）。"""
NativeMode = Literal["off", "pending", "confirmed"]


class AnchorBookSpike(ExchangeError):
    """反向卡锚定移动止损时盘口价偏离标记价太多（疑似插针）：这一轮不锚，可以下一轮再试。"""


class ProtectionIncomplete(ExchangeError):
    """状态未知或保护不完整；调用方保留已确认保护并报告，不应擅自重复提交。"""

    def __init__(self, message: str, *, read_lag: bool = False, backup_only: bool = False) -> None:
        super().__init__(message)
        self.read_lag = read_lag
        """已提交且回执已接受的条件单暂时查不到（可能是交易所读延迟），不是参数或状态不符。"""
        self.backup_only = backup_only
        """``stop_first`` 下只差保命单（被拒或提交未知），已确认止损在挂；可交给未托管宽限。"""


class ExitCleanupPending(RuntimeError):
    """同币上一笔仓已平但收尾还没核完（或对账员的退出在途），执行层拒绝新开。不是下单失败。"""


class CloseInProgress(ProtectionIncomplete):
    """The close was accepted and an exit is already working (a reduce-only order, an HB stop or
    a triggered leg); protection stays and the background tick finishes it. Not a failure."""


class _TakeProfitProblem(ExchangeError):
    """后备止盈腿自己的下单 / 回执 / 量化问题：只告警，不让这一轮同步失败。"""


class _PositionChanged(ProtectionIncomplete):
    """网络等待期间正常入场量变化；先保留已有保护，下一次用新证据继续。"""


def describe_error(error: BaseException) -> str:
    """类名 + 原文（含直接 cause），供 last_error 与告警定位具体哪一项核对没过；限长。"""

    def one(e: BaseException) -> str:
        text = str(e)
        return f"{type(e).__name__}: {text}" if text else type(e).__name__

    cause = error.__cause__
    return (one(error) + (f" <- {one(cause)}" if cause is not None else ""))[:300]


@dataclass(frozen=True)
class StopTarget:
    """普通止损这一轮的目标：价格、哪个来源定的（建腿时写进 ``purpose``）、不换到更紧价的原因。"""

    price: float | None
    purpose: LegPurpose
    hold: str | None = None


StopNotify = Callable[[str, str, str], Awaitable[None]]
"""(position_id, symbol, 正文) → 发紧急告警；在同币锁外调用。"""

TriggerNotify = Callable[[str, str, str, str], Awaitable[None]]
"""(position_id, symbol, 去重标签「角色-代」, 正文) → 发紧急告警；在同币锁外调用。"""


@dataclass(frozen=True)
class EntryEvidence:
    order_id: str
    status: str
    filled: float
    quantity: float
    terminal: bool


_CHILD_TERMINAL = {"FILLED", "CANCELED", "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED"}
_ALGO_CLOSED = {"CANCELED", "CANCELLED", "EXPIRED", "REJECTED"}
_TRIGGERED = {"TRIGGERING", "TRIGGERED", "FINISHED"}
_READBACK_POLL_S = 0.5  # 回读宽限内的重读间隔；总等待由 readback_grace_s 封顶。
LegOrder = Literal["lifeboat_first", "stop_first"]
_LEG_ORDER: dict[LegOrder, tuple[str, ...]] = {
    "lifeboat_first": ("lifeboat", "stop_loss"),
    "stop_first": ("stop_loss", "lifeboat"),
}


def _trigger_failed(leg: ProtectiveLeg) -> bool:
    """触发了但交易所没下出子单（REJECTED/EXPIRED 且已复核无子单）：什么都没成交，不算退出。"""
    return bool(
        leg.triggered_at is not None
        and leg.algo_status in _ALGO_CLOSED
        and leg.child_state == "absent"
        and leg.actual_order_id is None
        and not leg.filled_quantity
    )


def _triggered(leg: ProtectiveLeg) -> bool:
    if _trigger_failed(leg):
        return False
    return bool(
        leg.algo_status in _TRIGGERED
        or leg.actual_order_id is not None
        or leg.triggered_at is not None
        or (leg.filled_quantity is not None and leg.filled_quantity > 0)
    )


def _settled(leg: ProtectiveLeg) -> bool:
    if leg.state == "superseded":
        return True  # 尚未提交就被新入场量替代，不能用于任何覆盖判断。
    if leg.state == "rejected":
        return True  # 低层仅明确业务拒绝允许到这里；结果未知仍为 submit_pending。
    if leg.child_state == "unknown":
        return False
    if _triggered(leg):
        return bool(
            leg.child_state == "identified"
            and leg.actual_order_id is not None
            and leg.actual_order_status in _CHILD_TERMINAL
            and leg.filled_quantity is not None
        )
    return leg.algo_status in _ALGO_CLOSED and leg.child_state == "absent"


def _live(leg: ProtectiveLeg) -> bool:
    return bool(
        leg.state == "confirmed"
        and leg.algo_status == "NEW"
        and leg.child_state == "absent"
        and not _triggered(leg)
    )


def live_leg(leg: ProtectiveLeg) -> bool:
    """Confirmed on the exchange, still NEW and not triggered."""
    return _live(leg)


def trigger_failed(leg: ProtectiveLeg) -> bool:
    """Triggered, but the exchange placed no child order: nothing filled, not an exit."""
    return _trigger_failed(leg)


def _native(leg: ProtectiveLeg) -> bool:
    return leg.kind == "trailing_stop"


def native_stop(long: bool, extreme: float, rate_pct: float) -> float:
    """币安 TRAILING_STOP_MARKET 的触发线：多仓最高价 ×（1 − 回撤比例），空仓最低价 ×（1 + 它）。"""
    sign = Decimal(-1) if long else Decimal(1)
    value = Decimal(str(extreme)) * (1 + sign * Decimal(str(rate_pct)) / 100)
    return float(value)


def _tp(leg: ProtectiveLeg) -> bool:
    return leg.kind == "take_profit"


def _side_leg(leg: ProtectiveLeg) -> bool:
    """独立于止损 / 保命单的腿（交易所移动止损、后备止盈）：它的问题不挡止损和保命单。"""
    return _native(leg) or _tp(leg)


def _side_idle(leg: ProtectiveLeg) -> bool:
    """独立腿提交 / 撤销结果未知、也没触发：既不算在挂，也没法撤，留给零仓收尾。"""
    return _side_leg(leg) and not _live(leg) and not _triggered(leg) and not _settled(leg)


def _role(leg: ProtectiveLeg) -> str:
    return "lifeboat" if leg.purpose == "lifeboat" else leg.kind


def _reason(leg: ProtectiveLeg) -> CloseReason:
    if leg.purpose == "lifeboat":
        return CloseReason.LIFEBOAT
    if leg.purpose == "trailing":
        return CloseReason.TRAILING_STOP
    if leg.purpose == "tightened":
        return CloseReason.TIGHTENED_STOP
    return CloseReason.STOP_LOSS if leg.kind == "stop_loss" else CloseReason.TAKE_PROFIT


class BracketManager:
    def __init__(
        self,
        exchange: BracketExchange,
        registry: PositionRegistry,
        *,
        account_name: str,
        connector_name: str,
        clock: Clock = utc_now,
        readback_grace_s: float = 0.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        prearm_grace_s: float = 0.0,
        unknown_submit_grace_s: float = 0.0,
        stop_working_type: WorkingType = "CONTRACT_PRICE",
        take_profit_working_type: WorkingType = "CONTRACT_PRICE",
        fallback_take_profit: bool = False,
        trailing_book_mark_max_dev_bps: float = 30.0,
        trailing_stop_mark_buffer_bps: float = 5.0,
        trailing_min_step_bps: float = 5.0,
        trailing_book_critical_after_s: float = 300.0,
        leg_order: LegOrder = "lifeboat_first",
        native_trailing: Callable[[], bool] | bool = False,
    ) -> None:
        """``readback_grace_s``：本程序刚收到回执的条件单，回执后几秒内查无只重读；0 = 不等。

        ``prearm_grace_s``：从未完整托管时，可重试的同步失败最多连续容忍几秒；0 = 不容忍。

        ``unknown_submit_grace_s``：条件单提交结果未知时，锁内按原 CID 只读重查最多几秒（不重发），
        查到就当回执接着挂下一条腿；仍查不到的提交未知也交给 ``prearm_grace_s`` 宽限。0 = 原行为。

        ``stop_working_type``：之后新建的止损（含移动止损新一代）用哪个触发价格源，建腿时写进账本；
        已有的腿一直按自己记下的核对，保命单固定标记价。

        ``fallback_take_profit``：没有交易所移动止损在保护、程序的移动止损也没激活时挂后备止盈单；
        ``take_profit_working_type`` 是它的触发价格源。

        ``trailing_book_mark_max_dev_bps``：算峰值的盘口价往抬高峰值的方向偏离标记价超过
        这么多基点当插针，本轮不更新；0 = 不比对。

        ``trailing_stop_mark_buffer_bps``：新一代移动止损须在标记价正确一侧且至少隔这么多基点
        才挂，否则本轮不换代。

        ``trailing_min_step_bps``：比正在挂的止损至少收紧这么多基点才换代；0 = 一变就换。

        ``trailing_book_critical_after_s``：已激活的移动止损连续这么多秒没有有效盘口，告警升级；
        0 = 不升级。

        ``leg_order``：一代里几条腿的提交顺序。``stop_first`` 时已确认的止损就是最低保护，之后只差
        保命单的失败交给 ``prearm_grace``（被拒的下一轮换新代重挂）；止损失败照旧。

        ``native_trailing``：``[fast_loop] trailing_native``，每次现读（可传函数，热生效）。关掉后
        已挂的交易所移动止损在保命单确认后撤掉，程序接着自己跟。
        """
        self.exchange, self.registry = exchange, registry
        self.account, self.connector, self.clock = account_name, connector_name, clock
        self.readback_grace_s, self._sleep = readback_grace_s, sleep
        self.prearm_grace_s = prearm_grace_s
        self.unknown_submit_grace_s = unknown_submit_grace_s
        self.stop_working_type = stop_working_type
        self.take_profit_working_type = take_profit_working_type
        self.fallback_take_profit = fallback_take_profit
        self.trailing_book_mark_max_dev_bps = trailing_book_mark_max_dev_bps
        self.trailing_stop_mark_buffer_bps = trailing_stop_mark_buffer_bps
        self.trailing_min_step_bps = trailing_min_step_bps
        self.trailing_book_critical_after_s = trailing_book_critical_after_s
        if leg_order not in _LEG_ORDER:
            raise ValueError(f"未知的保护腿顺序：{leg_order}")
        self.leg_order: LegOrder = leg_order
        self._native_flag = native_trailing
        self.notify_native_fallback: StopNotify | None = None
        """某笔仓放弃交易所移动止损（改挂保命单、程序自己跟）时调用；执行器接上告警。"""
        self.notify_stop_clamped: StopNotify | None = None
        """移动止损目标已越过标记价（被夹到可挂的最紧价，或本轮没换）时调用；执行器接上告警。"""
        self.notify_trigger_rejected: TriggerNotify | None = None
        """某条腿触发后子单被交易所拒绝时调用；执行器接上告警。"""
        self.notify_take_profit: StopNotify | None = None
        """后备止盈单挂不上 / 撤不掉时调用；执行器接上告警。"""
        self._pending_tp_notes: dict[str, list[str]] = {}
        self._tp_unreadable: set[str] = set()
        self._pending_notes: dict[str, list[str]] = {}
        self._pending_rejections: dict[str, list[tuple[str, str]]] = {}
        self._pending_fallbacks: dict[str, list[str]] = {}
        self._native_read_failing: dict[str, datetime] = {}

    def initial(self, quantity: float, deadline_at: datetime | None = None) -> ServerProtection:
        return ServerProtection(
            account_name=self.account,
            connector_name=self.connector,
            intended_quantity=quantity,
            deadline_at=deadline_at,
        )

    def retains_confirmed_stop(self, pid: str) -> bool:
        """最后回读仍明确 NEW 的普通止损；不表示网络断开后仍能证明当前实时状态。"""
        return any(_live(leg) and _role(leg) == "stop_loss" for leg in self._current(pid)[1].legs)

    def _backup_gap(self, state: ServerProtection) -> bool:
        """``stop_first`` 且最后回读有明确 NEW 的止损：缺的保命单只是后备，不单独构成平仓理由。"""
        return self.leg_order == "stop_first" and any(
            _live(leg) and _role(leg) == "stop_loss" for leg in state.legs
        )

    def set_tightened_stop(
        self, pid: str, price: float, reason: str, card_key: str | None = None
    ) -> bool:
        """记下反向卡收紧价（#314）；已有同样紧或更紧的收紧价就不动，返回是否改了。

        只落盘目标价，交易所上的换代由随后的 ``sync`` 按原有顺序做（新代确认后才撤旧代）。
        调用方须持同币锁。"""
        tracked, state = self._current(pid)
        if not math.isfinite(price) or price <= 0:
            raise ProtectionIncomplete("收紧止损价无效")
        old = state.tightened_stop_price
        long = tracked.record.side is Side.LONG
        if old is not None and (price <= old if long else price >= old):
            return False
        self._save(
            pid,
            tightened_stop_price=price,
            tightened_reason=reason[:300],
            tightened_at=self.clock(),
            opposite_card_key=card_key,
        )
        return True

    async def anchor_trailing(
        self,
        pid: str,
        basis: EntryBasis,
        read_book: Callable[[str], Awaitable[ExecutionBook]],
        *,
        distance_mult: float,
        reason: str,
        card_key: str | None = None,
    ) -> float | None:
        """反向卡（#314，trail 模式）：在当前可成交价强制启动移动止损，之后照常只收紧。

        峰值 = 现价（多买一 / 空卖一）的净收益，回撤距离 = 开仓带的 ``trailing_stop_pct`` ×
        ``distance_mult``（落成 ``trailing_distance_pct``，之后的移动止损都按它），止损价用
        ``trailing_update`` 同一套扣费逆算。已经激活的移动止损峰值和止损价只取更紧的，不放松；
        和正在挂的止损谁紧由 ``_stop_target`` 定。已锚定过就不再锚（返回现在的移动止损价）。
        盘口先过和移动止损同样的标记价偏离核对（不记 ``trailing_last_error``）。调用方须持同币锁。
        """
        tracked, state = self._current(pid)
        if state.opposite_anchor_price is not None:
            return state.trailing_stop_price
        rec = tracked.record
        if rec.trailing_activation_pct is None or rec.trailing_stop_pct is None:
            raise ProtectionIncomplete("这笔仓开仓时没带移动止损，不能锚定")
        if self._native_mode(tracked, state) != "off":
            # 币安那张单的激活价和回撤比例改不了：锚定后按锚定距离由程序自己跟，它在保命单
            # 确认后撤掉（撤之前它的推算价当止损下限）
            self._native_give_up(pid, "反向卡锚定移动止损，改回程序自己跟")
            tracked, state = self._current(pid)
        if basis.client_order_id != tracked.entry_client_order_id:
            raise ProtectionIncomplete("锚定移动止损的入场身份不一致")
        if not math.isfinite(distance_mult) or distance_mult <= 0:
            raise ProtectionIncomplete("锚定移动止损的距离倍数无效")
        long = rec.side is Side.LONG
        mark = await self._fresh_mark(rec.symbol)
        book = await read_book(rec.symbol)
        price = book.bid if long else book.ask
        dev = (price - mark if long else mark - price) / mark * 10_000
        max_dev = self.trailing_book_mark_max_dev_bps
        if max_dev > 0 and (not math.isfinite(dev) or dev > max_dev):
            raise AnchorBookSpike(
                f"锚定用的盘口价 {price:g} 比标记价 {mark:g} 偏 {dev:.1f} 基点"
                f"（上限 {max_dev:g}），疑似插针，本轮不锚定"
            )
        distance = rec.trailing_stop_pct * distance_mult
        seed = state.model_copy(
            update={
                "trailing_activated": True,
                # 还没激活：峰值从现价起算；已激活：沿用更高的峰值（不放松）
                "trailing_peak_net": state.trailing_peak_net if state.trailing_activated else None,
                "trailing_peak_net_exact": state.trailing_peak_net_exact
                if state.trailing_activated
                else None,
                "trailing_source_at": None,  # 这一轮一定按这张盘口算
                "trailing_distance_pct": distance,
            }
        )
        update = trailing_update(rec, seed, basis, book, self.clock())
        if update is None or update.stop_price is None:
            raise ProtectionIncomplete("锚定移动止损没算出止损价")
        self._save(
            pid,
            trailing_peak_net=update.peak_net,
            trailing_threshold_net=update.threshold_net,
            trailing_peak_net_exact=update.peak_net_exact,
            trailing_threshold_net_exact=update.threshold_net_exact,
            trailing_activated=True,
            trailing_stop_price=update.stop_price,
            trailing_entry_quote=basis.filled_quote,
            trailing_entry_quantity=basis.filled_quantity,
            trailing_fees_quote=basis.fee_usdt,
            trailing_source_at=book.source_at,
            trailing_observed_at=book.observed_at,
            trailing_distance_pct=distance,
            opposite_anchor_price=price,
            opposite_anchor_retry_since=None,
            opposite_card_key=card_key,
            tightened_reason=reason[:300],
            tightened_at=self.clock(),
        )
        return update.stop_price

    def prearm_grace(self, pid: str, error: BaseException) -> float | None:
        """从未完整托管时本轮同步失败能否先不平仓：返回连续失败已持续的秒数，None = 照旧退出。

        只宽限交易所读延迟（回执已接受的单暂时查无）和网络层一时失败；还必须：未进入退出、
        最后回读（或回执）仍有明确 NEW 的保命单或止损、没有任何腿处于提交/撤销未知、
        已触发、子单未知或被明确拒绝。计时从第一次可宽限失败算并落盘，重启不重新计时。
        """
        if self.prearm_grace_s <= 0:
            return None
        if isinstance(error, ProtectionIncomplete):
            if not (error.read_lag or error.backup_only):
                return None
        elif not (isinstance(error, ExchangeError) and error.transient):
            return None
        try:
            _, state = self._current(pid)
        except ProtectionIncomplete:
            return None
        if state.ever_armed or state.stage in {"exiting", "cleanup", "done"}:
            return None
        backup = self._backup_gap(state)
        if not any(_live(leg) and _role(leg) in {"lifeboat", "stop_loss"} for leg in state.legs):
            return None
        unknown_ok = self.unknown_submit_grace_s > 0
        if not all(
            _native(leg)
            or leg.state in {"planned", "superseded"}
            or (unknown_ok and leg.state == "submit_pending")
            or _live(leg)
            or (leg.state == "cancelled" and _settled(leg) and not _triggered(leg))
            or (backup and _role(leg) == "lifeboat" and leg.state in {"submit_pending", "rejected"})
            for leg in state.legs
        ):
            return None
        now = self.clock()
        since = state.prearm_failing_since or now
        elapsed = (now - since).total_seconds()
        if elapsed >= self.prearm_grace_s:
            return None
        if state.prearm_failing_since is None:
            self._save(pid, prearm_failing_since=now)
        return elapsed

    def _current(self, pid: str) -> tuple[TrackedPosition, ServerProtection]:
        tracked = self.registry.get(pid)
        if tracked is None or tracked.server_protection is None:
            raise ProtectionIncomplete("不是本状态机的持仓")
        state = tracked.server_protection
        if state.account_name != self.account or state.connector_name != self.connector:
            raise ProtectionIncomplete("持久保护账户或环境不匹配")
        return tracked, state

    def _save(self, pid: str, **changes: object) -> ServerProtection:
        tracked, state = self._current(pid)
        state = state.model_copy(update=changes)
        self.registry.put(tracked.model_copy(update={"server_protection": state}))
        return state

    def _leg(self, pid: str, cid: str) -> ProtectiveLeg:
        _, state = self._current(pid)
        return next(leg for leg in state.legs if leg.client_order_id == cid)

    def _save_leg(self, pid: str, leg: ProtectiveLeg) -> None:
        _, state = self._current(pid)
        self._save(
            pid,
            legs=[leg if old.client_order_id == leg.client_order_id else old for old in state.legs],
        )

    @staticmethod
    def _positions(rows: list[ExchangePosition]) -> list[ExchangePosition]:
        if not isinstance(rows, list) or any(
            not isinstance(p, ExchangePosition)
            or not math.isfinite(p.quantity)
            or p.quantity <= 0
            or p.side not in (Side.LONG, Side.SHORT)
            for p in rows
        ):
            raise ProtectionIncomplete("真实仓位证据格式不完整")
        return rows

    async def preflight(self, symbol: str) -> None:
        async with self.registry.symbol_lock(symbol):
            if await self.exchange.position_mode(fresh=True) != "one_way":
                raise ProtectionIncomplete("只支持已确认的单向持仓，不修改账户模式")
            if any(
                p.symbol == symbol
                for p in self._positions(await self.exchange.positions_evidence())
            ):
                raise ProtectionIncomplete("已有真实仓位，不能覆盖手动或未知仓位")
            live_ids = {t.record.position_id for t in self.registry.live()}
            if any(
                t.record.symbol == symbol
                and (
                    t.record.position_id in live_ids
                    or t.exit_cleanup_pending
                    or (t.server_protection is not None and t.server_protection.stage != "done")
                )
                for t in self.registry.data.positions.values()
            ):
                raise ProtectionIncomplete("同币前一笔持仓或保护尚未清理")
            if await self.exchange.open_brackets(symbol):
                raise ProtectionIncomplete("同币普通条件单尚未清理，不能新开")
            await self.exchange.assert_entry_clear(symbol)

    async def _evidence(self, pid: str) -> EntryEvidence:
        tracked, state = self._current(pid)
        rec, cid = tracked.record, tracked.entry_client_order_id
        if cid is None:
            raise ProtectionIncomplete("开仓订单身份未知")
        order = await self.exchange.entry_order(rec.symbol, cid)
        if (
            not isinstance(order, ExchangeEntryOrder)
            or order.client_order_id != cid
            or order.symbol != rec.symbol
            or order.position_side != "BOTH"
            or order.reduce_only
            or order.side != ("buy" if rec.side is Side.LONG else "sell")
            or not all(math.isfinite(v) for v in (order.quantity, order.filled_quantity))
            or not math.isclose(order.quantity, state.intended_quantity, rel_tol=1e-10)
            or not 0 <= order.filled_quantity <= order.quantity
            or (order.status == "FILLED" and order.filled_quantity != order.quantity)
            or (state.entry_order_id is not None and state.entry_order_id != order.order_id)
        ):
            raise ProtectionIncomplete("开仓订单身份、状态或实际成交量不一致")
        rows = [
            p
            for p in self._positions(await self.exchange.positions_evidence())
            if p.symbol == rec.symbol
        ]
        if len(rows) > 1 or any(p.side is not rec.side for p in rows):
            raise ProtectionIncomplete("真实仓位方向或唯一归属不一致")
        quantity = sum(p.quantity for p in rows)
        if quantity > order.filled_quantity * (1 + 1e-10):
            raise ProtectionIncomplete("实际仓量超过已确认入场成交，不能覆盖未知增仓")
        now, _ = self._current(pid)
        if now.entry_client_order_id != cid or now.record.executor_id != rec.executor_id:
            raise ProtectionIncomplete("等待期间开仓归属已改变")
        self._save(
            pid,
            entry_order_id=order.order_id,
            entry_status=order.status,
            entry_filled_quantity=order.filled_quantity,
            entry_terminal=order.terminal,
            observed_quantity=quantity,
            observed_at=self.clock(),
        )
        return EntryEvidence(
            order.order_id, order.status, order.filled_quantity, quantity, order.terminal
        )

    async def _checkpoint(self, pid: str, before: EntryEvidence) -> None:
        if await self._evidence(pid) != before:
            raise _PositionChanged("入场或实际持仓量已变化，保留原保护等待新证据")

    def _accept(self, pid: str, leg: ProtectiveLeg, order: ExchangeBracket) -> ProtectiveLeg:
        tracked, _ = self._current(pid)
        if (
            order.symbol != tracked.record.symbol
            or order.client_order_id != leg.client_order_id
            or order.kind != leg.kind
            or order.close_side != ("sell" if tracked.record.side is Side.LONG else "buy")
            or order.position_side != "BOTH"
            or order.working_type != leg.working_type
            or not order.reduce_only
            or order.price_protect
            or order.close_position
            or order.quantity != leg.quantity
            or (
                (
                    order.callback_rate != leg.callback_rate
                    or (
                        leg.activate_price is not None
                        and order.activate_price != leg.activate_price
                    )
                )
                if _native(leg)
                else order.trigger_price != leg.trigger_price
            )
            or (leg.algo_id is not None and leg.algo_id != order.algo_id)
            or (leg.actual_order_id is not None and leg.actual_order_id != order.actual_order_id)
            or (
                order.filled_quantity is not None
                and (
                    not math.isfinite(order.filled_quantity)
                    or not 0 <= order.filled_quantity <= leg.quantity
                )
            )
        ):
            raise ProtectionIncomplete("条件单身份、触发价、数量或只减仓参数不匹配")
        updated = leg.model_copy(
            update={
                "state": "cancel_pending" if leg.state == "cancel_pending" else "confirmed",
                "algo_id": order.algo_id,
                "algo_status": order.algo_status,
                "actual_order_id": order.actual_order_id,
                "actual_order_status": order.actual_order_status,
                "filled_quantity": order.filled_quantity,
                "triggered_at": order.triggered_at,
                "child_state": order.child_state,
            }
        )
        if leg.state == "cancel_pending" and _settled(updated):
            updated = updated.model_copy(update={"state": "cancelled"})
        if _trigger_failed(updated):
            # 终态且已复核无子单：记为已撤，_refresh 不再每轮重查这张 algo。
            updated = updated.model_copy(update={"state": "cancelled"})
            if not _trigger_failed(leg):
                self._on_trigger_failed(pid, leg, updated)
        self._save_leg(pid, updated)
        if updated.purpose == "lifeboat" and _live(updated):
            current, _ = self._current(pid)
            lb = LifeboatOrder(
                exchange_order_id=updated.algo_id,
                client_order_id=updated.client_order_id,
                stop_price=updated.trigger_price,
                status=LifeboatStatus.ACTIVE,
                updated_at=self.clock(),
            )
            self.registry.put(
                current.model_copy(
                    update={"record": current.record.model_copy(update={"lifeboat": lb})}
                )
            )
        elif updated.purpose == "lifeboat" and _settled(updated):
            current, _ = self._current(pid)
            old_lb = current.record.lifeboat
            if old_lb is not None and old_lb.client_order_id == updated.client_order_id:
                old_lb = old_lb.model_copy(
                    update={
                        "status": LifeboatStatus.TRIGGERED
                        if _triggered(updated)
                        else LifeboatStatus.CANCELLED,
                        "updated_at": self.clock(),
                    }
                )
                self.registry.put(
                    current.model_copy(
                        update={"record": current.record.model_copy(update={"lifeboat": old_lb})}
                    )
                )
        if _triggered(updated):
            _, state = self._current(pid)
            reason = _reason(updated)
            self._save(pid, stage="exiting", exit_reason=state.exit_reason or reason)
        return updated

    def _on_trigger_failed(self, pid: str, old: ProtectiveLeg, leg: ProtectiveLeg) -> None:
        """止损/保命单被拒说明价格已越过止损线，同角色第二次被拒也退出；止盈腿被拒不退出、不补挂。"""
        _, state = self._current(pid)
        role = _role(leg)
        head = (
            f"{role} 条件单 {leg.client_order_id}（algo {leg.algo_id}）触发后交易所 "
            f"{leg.algo_status}，没有子单、没有成交"
        )
        tag = f"{role}-{leg.generation}"
        log.warning("%s %s", pid, head)
        if state.stage in {"exiting", "cleanup", "done"}:
            if old.algo_status in _TRIGGERED:
                self._pending_rejections.setdefault(pid, []).append(
                    (
                        tag,
                        f"{head}；此前读到 {old.algo_status} 已进入退出，"
                        "这次退出实为「触发后被拒」，按现有仓位继续退出",
                    )
                )
            return
        repeated = any(
            x.client_order_id != leg.client_order_id and _role(x) == role and _trigger_failed(x)
            for x in state.legs
        )
        if role == "take_profit":
            text = f"{head}；仓位不当作已平，不再挂止盈单，止损照常"
        else:
            reason = _reason(leg)
            self._save(pid, stage="exiting", exit_reason=reason)
            why = "同一角色第二次触发后被拒" if repeated else "止损线已被越过"
            text = f"{head}；{why}，不再补挂，按 {reason.value} 退出"
        self._pending_rejections.setdefault(pid, []).append((tag, text))

    async def _refresh(self, pid: str) -> ServerProtection:
        tracked, state = self._current(pid)
        for old in state.legs:
            if old.state in {"planned", "superseded", "rejected", "cancelled"}:
                continue
            if _native(old):
                await self._refresh_native(pid, old)
                continue
            if _tp(old):
                await self._refresh_take_profit(pid, old)
                continue
            read = (
                self.exchange.get_lifeboat
                if old.purpose == "lifeboat"
                else self.exchange.get_bracket
            )
            order = await read(tracked.record.symbol, client_order_id=old.client_order_id)
            if order is None:
                order = await self._await_readback(pid, old.client_order_id)
            if (
                order is None
                and old.purpose == "lifeboat"
                and old.state == "submit_pending"
                and self._backup_gap(self._current(pid)[1])
            ):
                raise ProtectionIncomplete(
                    "保命单提交结果仍未知，只查原CID；已确认止损在挂", backup_only=True
                )
            if order is None:
                raise ProtectionIncomplete(
                    "已提交条件单暂时不可查，不能再次创建或当作已撤",
                    read_lag=(old.state == "confirmed" and old.acked_at is not None)
                    or (old.state == "submit_pending" and self.unknown_submit_grace_s > 0),
                )
            leg = self._leg(pid, old.client_order_id)
            self._accept(pid, leg.model_copy(update={"acked_at": None}), order)
        _, state = self._current(pid)
        hit = next((leg for leg in state.legs if _triggered(leg)), None)
        if hit is not None:
            reason = _reason(hit)
            state = self._save(pid, stage="exiting", exit_reason=state.exit_reason or reason)
        return state

    async def _refresh_native(self, pid: str, old: ProtectiveLeg) -> None:
        """交易所移动止损腿的回读：读不了、查不到、对不上都不让整轮同步失败（另有固定止损）。
        在挂的腿连续 ``NATIVE_READ_GRACE_S`` 秒读不好（Demo 读延迟常见）才放弃它，照旧挂保命单、
        程序自己跟；提交结果本来就未知的腿查不到马上放弃。"""
        tracked, _ = self._current(pid)
        cid = old.client_order_id
        problem: str | None = None
        try:
            order = await self.exchange.get_bracket(tracked.record.symbol, client_order_id=cid)
            if order is None:
                order = await self._await_readback(pid, cid)
            if order is None:
                problem = f"交易所移动止损 {cid} 查不到（提交结果未知或已不在）"
            else:
                leg = self._leg(pid, cid)
                self._accept(pid, leg.model_copy(update={"acked_at": None}), order)
        except Exception as error:
            problem = f"交易所移动止损 {cid} 回读失败：{describe_error(error)}"
        if problem is None:
            self._native_read_failing.pop(pid, None)
            return
        now = self.clock()
        since = self._native_read_failing.setdefault(pid, now)
        if old.state == "submit_pending" or (now - since).total_seconds() >= NATIVE_READ_GRACE_S:
            self._native_read_failing.pop(pid, None)
            self._native_give_up(pid, problem)
        else:
            log.warning("%s %s（%.0f 秒内再读）", pid, problem, NATIVE_READ_GRACE_S)

    async def _refresh_take_profit(self, pid: str, old: ProtectiveLeg) -> None:
        """后备止盈腿的回读：查不到、读不了、对不上只记日志，留着最后一次回读的状态，不让整轮
        同步失败（止损和保命单另有回读）；提交结果未知的腿每轮只按原 CID 查，不重发。"""
        tracked, _ = self._current(pid)
        cid = old.client_order_id
        try:
            order = await self.exchange.get_bracket(tracked.record.symbol, client_order_id=cid)
            if order is None:
                order = await self._await_readback(pid, cid)
            if order is None:
                self._tp_read_problem(pid, cid, "查不到")
                return
            leg = self._leg(pid, cid)
            self._accept(pid, leg.model_copy(update={"acked_at": None}), order)
        except Exception as error:
            self._tp_read_problem(pid, cid, f"回读失败：{describe_error(error)}")
            return
        self._tp_unreadable.discard(cid)

    def _tp_read_problem(self, pid: str, cid: str, what: str) -> None:
        """同一条腿一直读不好只在第一次记 WARNING，之后 DEBUG，读好了再重新算。"""
        level = logging.DEBUG if cid in self._tp_unreadable else logging.WARNING
        self._tp_unreadable.add(cid)
        log.log(level, "%s 后备止盈单 %s %s，下一轮按原单号再查", pid, cid, what)

    async def _settle_orphan(self, pid: str, leg: ProtectiveLeg) -> None:
        """零仓收尾：提交 / 撤销结果未知的独立腿按原 CID 再查一次。查无就记成从没生效
        （之后同币新开前 ``preflight`` 还会直接查交易所的开放条件单）；查到还在挂就按它自己的
        algoId 撤掉。查不清的留着，收尾照旧不算完。"""
        tracked, _ = self._current(pid)
        symbol, cid = tracked.record.symbol, leg.client_order_id
        order = await self.exchange.get_bracket(symbol, client_order_id=cid)
        if order is None:
            self._save_leg(pid, leg.model_copy(update={"state": "rejected"}))
            return
        if order.client_order_id != cid or order.kind != leg.kind:
            raise ProtectionIncomplete("独立腿回读身份不符")
        if order.algo_status == "NEW" and order.child_state == "absent":
            await self.exchange.cancel_bracket(symbol, order.algo_id, expected_client_order_id=cid)
            order = await self.exchange.get_bracket(symbol, client_order_id=cid)
            if order is None:
                raise ProtectionIncomplete("撤单后身份不可查，不能当作清理完成")
        if order.algo_status in _ALGO_CLOSED and order.child_state == "absent":
            self._save_leg(
                pid,
                leg.model_copy(
                    update={
                        "state": "cancelled",
                        "algo_id": order.algo_id,
                        "algo_status": order.algo_status,
                        "child_state": "absent",
                    }
                ),
            )

    def _native_give_up(self, pid: str, reason: str) -> None:
        _, state = self._current(pid)
        if state.native_fallback is not None:
            return
        log.warning("%s 放弃交易所移动止损：%s", pid, reason)
        self._save(pid, native_fallback=reason[:300], native_pending_since=None)
        self._pending_fallbacks.setdefault(pid, []).append(reason)

    def _native_on(self) -> bool:
        flag = self._native_flag
        return bool(flag() if callable(flag) else flag)

    def _native_mode(self, tracked: TrackedPosition, state: ServerProtection) -> NativeMode:
        """``confirmed``：交易所移动止损在挂（或已触发），不挂保命单、程序不换代止损腿；
        ``pending``：该挂还没挂上，先不挂新保命单（已有的留着）、程序也不跟；``off``：照旧。"""
        rec = tracked.record
        if (
            not self._native_on()
            or rec.trailing_activation_pct is None
            or rec.trailing_stop_pct is None
            or state.native_fallback is not None
        ):
            return "off"
        if any(_native(leg) and (_live(leg) or _triggered(leg)) for leg in state.legs):
            return "confirmed"
        return "pending"

    def native_mode(self, pid: str) -> NativeMode:
        tracked, state = self._current(pid)
        return self._native_mode(tracked, state)

    async def _await_readback(self, pid: str, cid: str) -> ExchangeBracket | None:
        """回执已接受的 NEW 腿在宽限内查无：按 algoId 只读重试，不重发、不当已撤、不建新代。

        宽限从持久化的回执时刻算，重启不会重新计时；过时仍查无返回 None，由调用方照旧报错。
        """
        tracked, _ = self._current(pid)
        leg = self._leg(pid, cid)
        grace = self.readback_grace_s
        if grace <= 0 or leg.acked_at is None or leg.algo_id is None or not _live(leg):
            return None
        deadline = leg.acked_at + timedelta(seconds=grace)
        for _ in range(math.ceil(grace / _READBACK_POLL_S)):  # 时钟异常也不会无限等。
            remaining = (deadline - self.clock()).total_seconds()
            if remaining <= 0:
                return None
            await self._sleep(min(_READBACK_POLL_S, remaining))
            if leg.purpose == "lifeboat":
                order = await self.exchange.get_lifeboat(
                    tracked.record.symbol, client_order_id=cid, algo_id=leg.algo_id
                )
            else:
                order = await self.exchange.get_bracket(tracked.record.symbol, algo_id=leg.algo_id)
            if order is not None:
                return order
        return None

    async def _await_unknown_submit(
        self, pid: str, cid: str, unknown: BracketSubmissionUnknown
    ) -> ExchangeBracket | None:
        """提交结果未知：先用交易所层当场查到的，再按原 CID 只读重查到宽限用完；从不重发。"""
        grace = self.unknown_submit_grace_s
        if grace <= 0:
            return None
        if unknown.observed is not None:
            return unknown.observed
        tracked, _ = self._current(pid)
        leg = self._leg(pid, cid)
        read = (
            self.exchange.get_lifeboat if leg.purpose == "lifeboat" else self.exchange.get_bracket
        )
        deadline = self.clock() + timedelta(seconds=grace)
        for _ in range(math.ceil(grace / _READBACK_POLL_S)):  # 时钟异常也不会无限等。
            remaining = (deadline - self.clock()).total_seconds()
            if remaining <= 0:
                return None
            await self._sleep(min(_READBACK_POLL_S, remaining))
            with contextlib.suppress(ExchangeError):
                order = await read(tracked.record.symbol, client_order_id=cid)
                if order is not None:
                    return order
        return None

    async def _cancel(self, pid: str, cid: str) -> None:
        tracked, _ = self._current(pid)
        leg = self._leg(pid, cid)
        if _settled(leg):
            return
        if leg.state == "planned":
            self._save_leg(pid, leg.model_copy(update={"state": "superseded"}))
            return
        if leg.state == "cancel_pending":
            raise ProtectionIncomplete("撤单结果未知，只查证原条件单")
        if not _live(leg) or leg.algo_id is None:
            raise ProtectionIncomplete("条件单或子单不在明确可撤状态")
        self._save_leg(pid, leg.model_copy(update={"state": "cancel_pending"}))
        cancel = (
            self.exchange.cancel_lifeboat
            if leg.purpose == "lifeboat"
            else self.exchange.cancel_bracket
        )
        read = (
            self.exchange.get_lifeboat if leg.purpose == "lifeboat" else self.exchange.get_bracket
        )
        await cancel(tracked.record.symbol, leg.algo_id, expected_client_order_id=cid)
        order = await read(tracked.record.symbol, client_order_id=cid)
        if order is None:
            raise ProtectionIncomplete("撤单后身份不可查，不能当作清理完成")
        final = self._accept(pid, self._leg(pid, cid), order)
        if not _settled(final):
            raise ProtectionIncomplete("撤单或触发子单仍未终态")

    async def sync(self, pid: str) -> Literal["waiting", "provisional", "armed", "exit", "flat"]:
        tracked, _ = self._current(pid)
        try:
            async with self.registry.symbol_lock(tracked.record.symbol):
                try:
                    result = await self._sync(pid)
                except _PositionChanged as error:
                    if self._current(pid)[1].stage in {"exiting", "cleanup", "done"}:
                        return "exit"  # 撤旧单期间已确认触发，数量变化不能把退出退回保护阶段。
                    self._save(pid, stage="provisional", last_error=str(error))
                    return "provisional"
                except Exception as error:
                    self._save(pid, last_error=describe_error(error))
                    raise
                if self._current(pid)[1].prearm_failing_since is not None:
                    self._save(pid, prearm_failing_since=None)
                return result
        finally:
            await self._flush_notes(pid, tracked.record.symbol)  # 告警在锁外发，不拖住同币锁

    async def _flush_notes(self, pid: str, symbol: str) -> None:
        notes, clamped = self._pending_notes.pop(pid, []), self.notify_stop_clamped
        rejections, rejected = self._pending_rejections.pop(pid, []), self.notify_trigger_rejected
        fallbacks, fell_back = self._pending_fallbacks.pop(pid, []), self.notify_native_fallback
        tp_notes, tp_notify = self._pending_tp_notes.pop(pid, []), self.notify_take_profit
        sends: list[Awaitable[None]] = []
        if tp_notify is not None:
            sends += [tp_notify(pid, symbol, text) for text in tp_notes]
        if fell_back is not None:
            sends += [fell_back(pid, symbol, text) for text in fallbacks]
        if clamped is not None:
            sends += [clamped(pid, symbol, text) for text in notes]
        if rejected is not None:
            sends += [rejected(pid, symbol, tag, text) for tag, text in rejections]
        for send in sends:
            try:
                await send
            except Exception:  # 告警失败不能影响保护同步
                log.exception("保护告警发送失败：%s", pid)

    def live_stop_price(self, pid: str) -> float | None:
        """交易所上正在挂的普通止损（最高一代、最后回读明确 NEW）的触发价；没有就是 None。"""
        return self._live_stop(self._current(pid)[1])[0]

    @staticmethod
    def _live_stop(state: ServerProtection) -> tuple[float | None, LegPurpose | None]:
        live = [leg for leg in state.legs if _live(leg) and _role(leg) == "stop_loss"]
        if not live:
            return None, None
        leg = max(live, key=lambda x: x.generation)
        return leg.trigger_price, leg.purpose

    def _extra_stop_candidates(
        self, tracked: TrackedPosition, state: ServerProtection
    ) -> list[tuple[LegPurpose, float]]:
        """固定止损和移动止损以外的收紧候选（来源, 价格）。新的收紧来源从这里接入。

        反向卡收紧价（#314）：只从落盘的 ``tightened_stop_price`` 读，落了就每轮都给（卡变了也给），
        这样它一直是下限——非固定来源不会把在挂的止损换松，只有账本固定止损比它还紧时才轮到固定止损。

        交易所移动止损不再用（开关关了、这笔放弃了）：本地推算的它的当前价当移动止损候选，换回
        程序自己跟时止损腿先收到那里再撤它，不比它在时松。"""
        out: list[tuple[LegPurpose, float]] = []
        if state.tightened_stop_price is not None:
            out.append(("tightened", state.tightened_stop_price))
        if state.native_stop_price is not None and self._native_mode(tracked, state) == "off":
            out.append(("trailing", state.native_stop_price))
        return out

    async def _sync(self, pid: str) -> Literal["waiting", "provisional", "armed", "exit", "flat"]:
        result, evidence = await self._sync_core(pid)
        if result != "armed":
            return result
        return await self._sync_take_profit(pid, evidence)

    async def _sync_core(
        self, pid: str
    ) -> tuple[Literal["waiting", "provisional", "armed", "exit", "flat"], EntryEvidence]:
        evidence = await self._evidence(pid)
        state = await self._refresh(pid)
        if state.stage in {"exiting", "cleanup"}:
            return "exit", evidence
        if evidence.quantity == 0:
            return ("flat" if evidence.terminal else "waiting"), evidence
        tracked, state = self._current(pid)
        entry = tracked.record.entry_price
        if entry is None:
            raise ProtectionIncomplete("实际入场价尚不可证")
        sign = 1 if tracked.record.side is Side.LONG else -1
        if state.native_fallback == LEGACY_PENDING_FALLBACK and not any(
            _native(leg) for leg in state.legs
        ):
            log.warning("%s 旧版本没试挂就放弃了交易所移动止损，重新去挂", pid)
            state = self._save(pid, native_fallback=None)
        native = self._native_mode(tracked, state)
        since = state.native_pending_since
        if (
            native == "pending"
            and since is not None
            and (self.clock() - since).total_seconds() > NATIVE_PENDING_MAX_S
        ):
            why = f"，最近原因：{state.trailing_last_error}" if state.trailing_last_error else ""
            self._native_give_up(
                pid, f"开始挂交易所移动止损 {NATIVE_PENDING_MAX_S:g} 秒后还没挂上{why}"
            )
            tracked, state = self._current(pid)
            native = self._native_mode(tracked, state)
        stop = await self._stop_target(tracked, state, self._extra_stop_candidates(tracked, state))
        hold = stop.hold
        # 止盈腿不在这里：要不要留由 _sync_take_profit 在止损和保命单确认之后定。
        desired: dict[str, float | None] = {"stop_loss": stop.price}
        if native == "off" or (native == "pending" and not evidence.terminal):
            # 入场没结束还挂不了交易所移动止损（数量会变）：先照旧挂保命单，挂上后再撤
            desired["lifeboat"] = entry * (1 - sign * tracked.lifeboat_stop_pct / 100)
        # 交易所移动止损在挂：留着它；还没挂上：已有的保命单先留着，确认挂上后才撤。
        extra_keep = {
            leg.client_order_id
            for leg in state.legs
            if _live(leg)
            and (
                (native == "confirmed" and _native(leg))
                or (native == "pending" and leg.purpose == "lifeboat")
            )
        }
        specs: dict[str, NormalizedBracketSpec] = {}
        for kind, price in desired.items():
            if price is None or not math.isfinite(price) or price <= 0:
                raise ProtectionIncomplete("固定保护价格尚不可证")
            spec = await self.exchange.normalized_bracket_spec(
                tracked.record.symbol, evidence.quantity, price
            )
            if not all(math.isfinite(v) and v > 0 for v in (spec.quantity, spec.trigger_price)):
                raise ProtectionIncomplete("量化后的保护参数无效")
            if not math.isclose(spec.quantity, evidence.quantity, rel_tol=1e-10):
                raise ProtectionIncomplete("实际仓量无法完整量化，不能宣称无残量")
            specs[kind] = spec
        specs = {kind: specs[kind] for kind in _LEG_ORDER[self.leg_order] if kind in specs}
        await self._checkpoint(pid, evidence)
        _, state = self._current(pid)
        matching = {
            kind: [
                leg
                for leg in state.legs
                if _live(leg)
                and _role(leg) == kind
                and leg.quantity == spec.quantity
                and leg.trigger_price == spec.trigger_price
            ]
            for kind, spec in specs.items()
        }
        if all(len(legs) == 1 for legs in matching.values()):
            current_cids = {legs[0].client_order_id for legs in matching.values()} | extra_keep
            if all(
                leg.client_order_id in current_cids or _settled(leg) or _side_idle(leg) or _tp(leg)
                for leg in state.legs
            ):
                # 无改单：一轮完整回读 + 前后开仓/净仓一致即可；不重复跑提交专用检查点。
                self._save(
                    pid,
                    stage="armed" if evidence.terminal else "provisional",
                    ever_armed=state.ever_armed or evidence.terminal,
                    last_error=hold,
                )
                return ("armed" if evidence.terminal else "provisional"), evidence
        for leg in state.legs:
            if leg.state == "planned":
                self._save_leg(pid, leg.model_copy(update={"state": "superseded"}))
            elif _side_leg(leg):
                continue  # 独立的一条腿：它的提交 / 撤销未知不挡固定止损和保命单换代
            elif (
                leg.state == "rejected"
                and _role(leg) == "lifeboat"
                and not state.ever_armed
                and self._backup_gap(state)
            ):
                # 明确被拒、交易所上没有这张单：换新代、新 CID 重挂。只在从没完整托管时
                # （未托管宽限内）重试；曾完整托管后旧代保护还在，照旧停在这里，不每轮重发。
                continue
            elif leg.state in {"submit_pending", "cancel_pending", "rejected"}:
                raise ProtectionIncomplete("已有提交或撤销未确认，不能创建新代")
            elif not _settled(leg) and not _live(leg):
                raise ProtectionIncomplete("保护状态或子单未知，不能创建新代")
        _, state = self._current(pid)
        needed = [
            kind
            for kind, spec in specs.items()
            if not any(
                _live(leg)
                and _role(leg) == kind
                and leg.quantity == spec.quantity
                and leg.trigger_price == spec.trigger_price
                for leg in state.legs
            )
        ]
        if needed:
            generation = state.generation + 1
            legs = list(state.legs)
            for kind in needed:
                lifeboat = kind == "lifeboat"
                cid = f"lb-{pid}-{generation}" if lifeboat else f"br-sl-{pid}-{generation}"
                if len(cid) > 36:
                    raise ProtectionIncomplete("条件单身份超过交易所长度限制")
                spec = specs[kind]
                # 新一代（含移动止损换价）按现在的设置；旧腿照自己记下的核对，新腿确认后才撤旧腿。
                legs.append(
                    ProtectiveLeg(
                        kind="stop_loss",
                        generation=generation,
                        client_order_id=cid,
                        quantity=spec.quantity,
                        trigger_price=spec.trigger_price,
                        purpose="lifeboat" if lifeboat else stop.purpose,
                        working_type="MARK_PRICE" if lifeboat else self.stop_working_type,
                    )
                )
            self._save(pid, legs=legs, generation=generation, stage="provisional")
            for old in legs:
                if old.generation != generation or _native(old):
                    continue
                await self._checkpoint(pid, evidence)
                await self._refresh(pid)
                if self._current(pid)[1].stage == "exiting":
                    return "exit", evidence
                leg = self._leg(pid, old.client_order_id)
                self._save_leg(pid, leg.model_copy(update={"state": "submit_pending"}))
                try:
                    close_side: Literal["buy", "sell"] = (
                        "sell" if tracked.record.side is Side.LONG else "buy"
                    )
                    if leg.purpose == "lifeboat":
                        order = await self.exchange.place_lifeboat(
                            tracked.record.symbol,
                            close_side,
                            leg.quantity,
                            leg.trigger_price,
                            leg.client_order_id,
                        )
                    else:
                        order = await self.exchange.place_bracket(
                            tracked.record.symbol,
                            close_side,
                            leg.quantity,
                            leg.trigger_price,
                            leg.client_order_id,
                            kind="stop_loss",
                            working_type=leg.working_type,
                        )
                except BracketSubmissionRejected as rejected:
                    self._save_leg(
                        pid,
                        self._leg(pid, leg.client_order_id).model_copy(
                            update={"state": "rejected"}
                        ),
                    )
                    if rejected.immediate_trigger:
                        self._save(
                            pid,
                            stage="exiting",
                            exit_reason=_reason(leg),
                            last_error="immediate_trigger_rejected",
                        )
                        # 交易所明确已越触发价，复用退出核验；旧保护仍保留。
                        return "exit", evidence
                    if leg.purpose == "lifeboat" and self._backup_gap(self._current(pid)[1]):
                        raise ProtectionIncomplete(
                            "保命单被交易所明确拒绝；已确认止损在挂，下一轮换新CID重挂",
                            backup_only=True,
                        ) from rejected
                    raise ProtectionIncomplete(
                        "交易所明确拒绝普通保护，保留已有保护并退出"
                    ) from None
                except BracketSubmissionUnknown as unknown:
                    found = await self._await_unknown_submit(pid, leg.client_order_id, unknown)
                    if found is None:
                        raise ProtectionIncomplete(
                            "普通保护提交结果未知，只查原CID",
                            read_lag=self.unknown_submit_grace_s > 0,
                            backup_only=leg.purpose == "lifeboat"
                            and self._backup_gap(self._current(pid)[1]),
                        ) from (unknown.__cause__ or unknown)
                    order = found
                acked = self._leg(pid, leg.client_order_id)
                self._accept(pid, acked.model_copy(update={"acked_at": self.clock()}), order)
                await self._checkpoint(pid, evidence)
                state = await self._refresh(pid)
                if state.stage == "exiting":
                    return "exit", evidence
        state = await self._refresh(pid)
        if state.stage == "exiting":
            return "exit", evidence
        await self._checkpoint(pid, evidence)
        keep: set[str] = set()
        for kind, spec in specs.items():
            matches = [
                leg
                for leg in state.legs
                if _live(leg)
                and _role(leg) == kind
                and leg.quantity == spec.quantity
                and leg.trigger_price == spec.trigger_price
            ]
            if not matches:
                raise ProtectionIncomplete("两侧普通保护尚未完整确认")
            keep.add(matches[-1].client_order_id)
        keep |= extra_keep
        if not evidence.terminal:
            self._save(pid, stage="provisional")
            # 开仓残单可能继续填入，不能把此刻量当最终量，也不撤旧保护。
            return "provisional", evidence
        for leg in state.legs:
            # 止盈腿要不要留由 _sync_take_profit 在止损和保命单确认之后定。
            if _tp(leg):
                continue
            if leg.client_order_id not in keep and not _settled(leg) and not _side_idle(leg):
                await self._checkpoint(pid, evidence)
                await self._refresh(pid)
                if self._current(pid)[1].stage == "exiting":
                    return "exit", evidence
                if _native(leg):
                    try:
                        await self._cancel(pid, leg.client_order_id)
                    except Exception as error:
                        # 多出来的只减仓单，要留的腿已确认；下一轮再撤，零仓清理照样要撤清
                        log.warning(
                            "%s 撤交易所移动止损 %s 没成：%s",
                            pid,
                            leg.client_order_id,
                            describe_error(error),
                        )
                    continue
                await self._cancel(pid, leg.client_order_id)
                await self._checkpoint(pid, evidence)
        final = await self._refresh(pid)
        await self._checkpoint(pid, evidence)
        if final.stage == "exiting":
            return "exit", evidence
        if not all(_live(self._leg(pid, cid)) for cid in keep):
            raise ProtectionIncomplete("最后回读保护已改变")
        self._save(pid, stage="armed", ever_armed=True, last_error=hold)
        return "armed", evidence

    def _tp_wanted(self, tracked: TrackedPosition, state: ServerProtection) -> tuple[bool, bool]:
        """(该挂后备止盈, 已在挂的先留着)。

        该挂：开关开着、移动止损没激活过（程序自己跟的，或交易所移动止损的本地推算）、没有交易所
        移动止损在保护（``off``）、这笔的止盈腿没被拒过。``pending``（该挂交易所移动止损、还没挂上）不新挂，已在挂的留到它确认后再撤。"""
        if not self.fallback_take_profit or state.trailing_activated or state.native_activated:
            return False, False
        native = self._native_mode(tracked, state)
        gave_up = any(
            _tp(leg) and (leg.state == "rejected" or _trigger_failed(leg)) for leg in state.legs
        )
        return native == "off" and not gave_up, native == "pending"

    async def _sync_take_profit(
        self, pid: str, evidence: EntryEvidence
    ) -> Literal["armed", "exit"]:
        """止损和保命单已确认在挂（``_sync_core`` 返回 armed）之后才动后备止盈腿。

        新腿确认在挂后才撤旧腿；不该留的撤掉。止盈腿自己的问题只记日志和告警，返回 armed：
        挂不上不构成平仓理由。止损 / 保命单回读和入场核对失败照常往上抛。"""
        try:
            target = await self._place_take_profit(pid, evidence)
        except _TakeProfitProblem as error:
            self._tp_note(pid, f"后备止盈单这一轮没挂成：{describe_error(error)}")
            target = None
        if self._current(pid)[1].stage == "exiting":
            return "exit"
        tracked, state = self._current(pid)
        want, hold = self._tp_wanted(tracked, state)
        for leg in state.legs:
            if _tp(leg) and leg.state == "cancel_pending" and leg.algo_status == "NEW":
                await self._retry_tp_cancel(pid, leg)
                continue
            if not (_tp(leg) and _live(leg)) or leg.client_order_id == target:
                continue
            if hold or (want and target is None):
                continue  # 新的还没确认在挂（或交易所移动止损还没确认）：旧的先留着
            try:
                await self._cancel(pid, leg.client_order_id)
            except Exception as error:
                self._tp_note(
                    pid,
                    f"撤后备止盈单 {leg.client_order_id} 没成：{describe_error(error)}，下一轮再撤",
                )
            if self._current(pid)[1].stage == "exiting":
                return "exit"
        return "armed"

    async def _retry_tp_cancel(self, pid: str, leg: ProtectiveLeg) -> None:
        """撤单结果未知、回读仍明确 NEW 且没触发：按它自己的 algoId 再撤一次（撤单幂等），
        免得它一直挂到零仓收尾，期间在止盈价把跑单平掉。"""
        if leg.algo_id is None or leg.child_state != "absent" or _triggered(leg):
            return
        tracked, _ = self._current(pid)
        cid = leg.client_order_id
        try:
            await self.exchange.cancel_bracket(
                tracked.record.symbol, leg.algo_id, expected_client_order_id=cid
            )
            order = await self.exchange.get_bracket(tracked.record.symbol, client_order_id=cid)
            if order is None:
                raise ProtectionIncomplete("撤单后身份不可查")
            self._accept(pid, self._leg(pid, cid), order)
        except Exception as error:
            self._tp_note(pid, f"再撤后备止盈单 {cid} 没成：{describe_error(error)}，下一轮再撤")

    async def _place_take_profit(self, pid: str, evidence: EntryEvidence) -> str | None:
        """该挂时返回正在挂、价和量都对的止盈腿 CID（需要就新挂一代）；不该挂或还没确认返回 None。

        有提交 / 撤销结果未知的止盈腿时不新挂，只按原 CID 回读（``_refresh_take_profit``）。
        被交易所拒绝（含会立即触发）记成 rejected，这笔之后不再挂，不平仓。"""
        tracked, state = self._current(pid)
        want, _ = self._tp_wanted(tracked, state)
        rec = tracked.record
        price = rec.take_profit_price
        if not want or price is None or not math.isfinite(price) or price <= 0:
            return None
        try:
            spec = await self.exchange.normalized_bracket_spec(rec.symbol, evidence.quantity, price)
        except Exception as error:
            raise _TakeProfitProblem("止盈价量化失败") from error
        if not all(math.isfinite(v) and v > 0 for v in (spec.quantity, spec.trigger_price)):
            raise _TakeProfitProblem("量化后的止盈参数无效")
        if not math.isclose(spec.quantity, evidence.quantity, rel_tol=1e-10):
            raise _TakeProfitProblem("实际仓量无法完整量化，不挂止盈单")
        for leg in state.legs:
            if _tp(leg) and leg.state == "planned":  # 上一轮存了还没提交就中断：从没发出
                self._save_leg(pid, leg.model_copy(update={"state": "superseded"}))
        tracked, state = self._current(pid)
        tps = [leg for leg in state.legs if _tp(leg)]
        match = [
            leg
            for leg in tps
            if _live(leg)
            and leg.quantity == spec.quantity
            and leg.trigger_price == spec.trigger_price
        ]
        if match:
            return match[-1].client_order_id
        if any(_side_idle(leg) for leg in tps):
            return None
        generation = state.generation + 1
        cid = f"br-tp-{pid}-{generation}"
        if len(cid) > 36:
            raise _TakeProfitProblem("条件单身份超过交易所长度限制")
        leg = ProtectiveLeg(
            kind="take_profit",
            generation=generation,
            client_order_id=cid,
            quantity=spec.quantity,
            trigger_price=spec.trigger_price,
            purpose="fixed",
            working_type=self.take_profit_working_type,
        )
        self._save(pid, legs=[*state.legs, leg], generation=generation)
        await self._checkpoint(pid, evidence)
        if (await self._refresh(pid)).stage == "exiting":
            self._save_leg(pid, self._leg(pid, cid).model_copy(update={"state": "superseded"}))
            return None
        self._save_leg(pid, self._leg(pid, cid).model_copy(update={"state": "submit_pending"}))
        try:
            order = await self.exchange.place_bracket(
                rec.symbol,
                "sell" if rec.side is Side.LONG else "buy",
                leg.quantity,
                leg.trigger_price,
                cid,
                kind="take_profit",
                working_type=leg.working_type,
            )
        except BracketSubmissionRejected as rejected:
            self._save_leg(pid, self._leg(pid, cid).model_copy(update={"state": "rejected"}))
            cause = rejected.__cause__ or rejected
            self._tp_note(
                pid,
                f"后备止盈单 {cid}（{leg.trigger_price:g}）被交易所拒绝：{describe_error(cause)}；"
                "这笔不再挂止盈单",
            )
            return None
        except BracketSubmissionUnknown as unknown:
            found = await self._await_unknown_submit(pid, cid, unknown)
            if found is None:
                self._tp_note(pid, f"后备止盈单 {cid} 提交结果未知，之后只按原单号查、不重发")
                return None
            order = found
        except Exception as error:
            # 没归类的下单异常：结果按未知处理，留在 submit_pending，只按原 CID 回读
            raise _TakeProfitProblem(f"后备止盈单 {cid} 提交出错") from error
        try:
            self._accept(
                pid, self._leg(pid, cid).model_copy(update={"acked_at": self.clock()}), order
            )
        except ProtectionIncomplete as error:
            raise _TakeProfitProblem(f"后备止盈单 {cid} 回执对不上") from error
        await self._checkpoint(pid, evidence)
        await self._refresh(pid)
        return cid if _live(self._leg(pid, cid)) else None

    def _tp_note(self, pid: str, text: str) -> None:
        log.warning("%s %s", pid, text)
        self._pending_tp_notes.setdefault(pid, []).append(text)

    async def _stop_target(
        self,
        tracked: TrackedPosition,
        state: ServerProtection,
        candidates: list[tuple[LegPurpose, float]] | None = None,
    ) -> StopTarget:
        """普通止损这一轮的目标价、来源，以及（若有）为什么没换到更紧的价。

        候选：固定止损、移动止损、``candidates``（其它收紧来源），取最紧的。固定止损最紧时照旧
        （可以比正在挂的更松，例如入场价改了）；其它来源永远不把正在挂的止损换松。换代先过最小
        步长；再读标记价：目标在安全一侧就挂；已越过（含缓冲）就夹到可挂的最紧价（多仓
        标记价 ×（1 − 缓冲），空仓 ×（1 + 缓冲），且不松于固定止损），夹完仍不比正在挂的紧够一个
        步长就保留正在挂的，并发紧急告警。标记价读不到保留正在挂的。
        """
        long = tracked.record.side is Side.LONG
        tighter = max if long else min
        sign = 1 if long else -1
        fixed = tracked.record.stop_loss_price
        # (来源, 价格, 是不是账本固定止损)；同价时先列的赢，固定止损排第一。
        options: list[tuple[LegPurpose, float, bool]] = []
        if fixed is not None:
            options.append(("fixed", fixed, True))
        if state.trailing_stop_price is not None:
            options.append(("trailing", state.trailing_stop_price, False))
        options += [(p, v, False) for p, v in candidates or [] if math.isfinite(v) and v > 0]
        if not options:
            return StopTarget(None, "fixed")
        purpose, target, is_fixed = options[0]
        for p, v, f in options[1:]:
            if tighter(v, target) != target:
                purpose, target, is_fixed = p, v, f
        if is_fixed:
            return StopTarget(target, "fixed")  # 账本固定止损：照旧，可以换松
        current, current_purpose = self._live_stop(state)
        if current is None or (fixed is not None and tighter(fixed, current) != current):
            keep = StopTarget(fixed, "fixed")  # 没有在挂的，或固定止损比在挂的还紧
        else:
            keep = StopTarget(current, current_purpose or "fixed")

        def gain(price: float) -> float:
            return math.inf if current is None else sign * (price - current) / current * 10_000

        if gain(target) <= 0:
            return keep  # 收紧来源不把正在挂的止损换松
        if gain(target) < self.trailing_min_step_bps and purpose != "tightened":
            return keep  # 收紧太少，不值得撤旧挂新（反向卡收紧价只换一次，不受最小步长限制）
        buffer = self.trailing_stop_mark_buffer_bps
        try:
            mark = await self._fresh_mark(tracked.record.symbol)
        except Exception as error:
            return replace(
                keep,
                hold=f"止损新价 {target:g} 本轮不挂：标记价不可用（{describe_error(error)}），"
                "保留正在挂的止损",
            )
        limit = mark * (1 - sign * buffer / 10_000)
        if (target < limit) if long else (target > limit):
            return StopTarget(target, purpose)
        clamped = limit if fixed is None else tighter(limit, fixed)
        if gain(clamped) >= max(self.trailing_min_step_bps, 1e-9):
            note = (
                f"止损目标 {target:g} 已越过标记价 {mark:g}（缓冲 {buffer:g} 基点），"
                f"改挂可挂的最紧价 {clamped:g}"
            )
            result = StopTarget(clamped, purpose, hold=note)
        else:
            note = (
                f"止损目标 {target:g} 已越过标记价 {mark:g}（缓冲 {buffer:g} 基点），"
                f"可挂的最紧价 {clamped:g} 不比正在挂的 {current:g} 紧，保留正在挂的止损"
            )
            result = replace(keep, hold=note)
        self._pending_notes.setdefault(tracked.record.position_id, []).append(note)
        return result

    async def _fresh_mark(self, symbol: str) -> float:
        """同环境、带交易所时间、此刻不超过 5 秒的标记价；任何一项不满足都抛错，不回退。"""
        read = getattr(self.exchange, "execution_mark", None)
        if read is None:
            raise ProtectionIncomplete("缺同交易所标记价读取")
        ev = await read(symbol)
        now = self.clock()
        if (
            not isinstance(ev, MarkEvidence)
            or ev.symbol != symbol
            or not math.isfinite(ev.mark)
            or ev.mark <= 0
            or any(
                t.tzinfo is None or t.utcoffset() is None for t in (ev.source_at, ev.observed_at)
            )
            or not ev.source_at <= ev.observed_at <= now
            or (now - ev.source_at).total_seconds() > BOOK_MAX_AGE_S
        ):
            raise ExchangeError("标记价过期、来自未来或格式不对")
        return ev.mark

    async def observe_trailing(
        self,
        pid: str,
        basis: EntryBasis,
        read_book: Callable[[str], Awaitable[ExecutionBook]],
    ) -> bool:
        """锁内先核入场 / 实际仓 / 保护腿回读，最后才读盘口并立刻判断。

        盘口不能在这些查询之前读：入场 2 次 + 每条腿 1 次 + 检查点 2 次（回读宽限时再多等几秒），
        Demo 上常超过 5 秒，判断时必然“过期”。5 秒新鲜度、未来时刻和源时钟回退的检查一条不放松。
        失败原因记 ``trailing_last_error``，完成一轮清空；异常照常抛给调用方告警。
        """
        tracked, _ = self._current(pid)
        async with self.registry.symbol_lock(tracked.record.symbol):
            try:
                changed = await self._observe_trailing(pid, basis, read_book)
            except Exception as error:
                self.record_trailing_error(pid, error)
                raise
            state = self._current(pid)[1]
            if state.trailing_last_error is not None or state.trailing_failing_since is not None:
                self._save(pid, trailing_last_error=None, trailing_failing_since=None)
            return changed

    def begin_native_attempt(self, pid: str) -> None:
        """后台保护轮这一笔走到挂交易所移动止损这一步了（还没读入场成本）：从这一刻开始算
        ``NATIVE_PENDING_MAX_S``，已在算就不动。只对 ``pending`` 的仓有效。"""
        tracked, state = self._current(pid)
        if self._native_mode(tracked, state) == "pending" and state.native_pending_since is None:
            self._save(pid, native_pending_since=self.clock())

    async def place_native_trailing(self, pid: str, basis: EntryBasis) -> bool:
        """武装后挂交易所移动止损（这笔仓 ``native_mode`` 为 ``pending`` 时）；返回是否已确认在挂。

        激活价 = 净收益（扣实际开仓手续费）到启动线 ``trailing_activation_pct`` 的价格，和程序自己
        跟时的激活判断同一公式；标记价已越过它（或程序自己跟已激活）就不带激活价、下单即激活。
        回撤比例 = 回撤距离（``trailing_distance_pct`` 或开仓带的 ``trailing_stop_pct``）按 0.1
        向下取整，不到 0.1 或超过 ``NATIVE_CALLBACK_MAX`` 不挂。被拒、提交结果未知就放弃这笔
        （第一次因激活价已被越过被拒的，下一轮不带激活价再试一次）。调用方随后 ``sync``。
        """
        tracked, _ = self._current(pid)
        symbol = tracked.record.symbol
        async with self.registry.symbol_lock(symbol):
            try:
                return await self._place_native(pid, basis)
            except Exception as error:
                self.record_trailing_error(pid, error)
                raise
            finally:
                await self._flush_notes(pid, symbol)

    async def _place_native(self, pid: str, basis: EntryBasis) -> bool:
        evidence = await self._evidence(pid)
        await self._refresh(pid)
        tracked, state = self._current(pid)
        rec = tracked.record
        if (
            state.stage != "armed"
            or not evidence.terminal
            or evidence.quantity <= 0
            or self._native_mode(tracked, state) != "pending"
            or any(_native(leg) and not _settled(leg) for leg in state.legs)
        ):
            return False
        if basis.client_order_id != tracked.entry_client_order_id or basis.symbol != rec.symbol:
            raise ProtectionIncomplete("交易所移动止损的入场身份不一致")
        assert rec.trailing_activation_pct is not None and rec.trailing_stop_pct is not None
        distance = state.trailing_distance_pct or rec.trailing_stop_pct
        rate = callback_rate(distance)
        if not CALLBACK_RATE_STEP <= rate <= NATIVE_CALLBACK_MAX:
            # 不到 0.1 的距离挂上去会比程序自己跟还松
            self._native_give_up(
                pid,
                f"回撤距离 {distance:g}% 不在交易所移动止损范围 "
                f"{CALLBACK_RATE_STEP}–{NATIVE_CALLBACK_MAX}% 内",
            )
            return False
        long = rec.side is Side.LONG
        act = activation_price(
            long,
            basis.filled_quote,
            basis.filled_quantity,
            basis.fee_usdt,
            rec.trailing_activation_pct,
        )
        if act is None:
            self._native_give_up(pid, "交易所移动止损激活价算不出来")
            return False
        try:
            mark = await self._fresh_mark(rec.symbol)
        except Exception as error:
            raise TrailingBookUnavailable("挂交易所移动止损前标记价不可用") from error
        passed = state.trailing_activated or (mark >= act if long else mark <= act)
        spec = await self.exchange.normalized_bracket_spec(
            rec.symbol, evidence.quantity, mark if passed else act
        )
        if not math.isclose(spec.quantity, evidence.quantity, rel_tol=1e-10):
            raise ProtectionIncomplete("实际仓量无法完整量化，不能宣称无残量")
        activate = None if passed else spec.trigger_price
        attempt = sum(1 for leg in state.legs if _native(leg)) + 1
        cid = f"br-ts-{pid}-{attempt}"
        if len(cid) > 36:
            raise ProtectionIncomplete("条件单身份超过交易所长度限制")
        leg = ProtectiveLeg(
            kind="trailing_stop",
            purpose="trailing",
            generation=attempt,
            client_order_id=cid,
            quantity=spec.quantity,
            trigger_price=spec.trigger_price,
            activate_price=activate,
            callback_rate=float(rate),
            # 本地推算按标记价跟，交易所也必须按标记价跟
            working_type="MARK_PRICE",
        )
        self._save(pid, legs=[*state.legs, leg])
        await self._checkpoint(pid, evidence)
        self._save_leg(pid, leg.model_copy(update={"state": "submit_pending"}))
        try:
            order = await self.exchange.place_trailing(
                rec.symbol,
                "sell" if long else "buy",
                leg.quantity,
                float(rate),
                cid,
                activate_price=activate,
                working_type=leg.working_type,
            )
        except BracketSubmissionRejected as rejected:
            self._save_leg(pid, self._leg(pid, cid).model_copy(update={"state": "rejected"}))
            if rejected.immediate_trigger and activate is not None and attempt == 1:
                log.info("%s 交易所移动止损激活价已被越过，下一轮不带激活价再挂", pid)
                return False
            cause = rejected.__cause__ or rejected
            self._native_give_up(pid, f"交易所拒绝交易所移动止损：{describe_error(cause)}")
            return False
        except BracketSubmissionUnknown as unknown:
            found = await self._await_unknown_submit(pid, cid, unknown)
            if found is None:
                self._native_give_up(pid, "交易所移动止损提交结果未知，只查原CID")
                return False
            order = found
        now = self.clock()
        try:
            self._accept(pid, self._leg(pid, cid).model_copy(update={"acked_at": now}), order)
        except ProtectionIncomplete as error:
            self._native_give_up(pid, f"交易所移动止损回执对不上：{describe_error(error)}")
            return False
        if not _live(self._leg(pid, cid)):
            return False
        self._save(
            pid,
            native_pending_since=None,
            native_extreme=mark,
            native_activated=activate is None,
            native_stop_price=native_stop(long, mark, float(rate)) if activate is None else None,
            native_observed_at=now,
            native_mainnet_extreme=None,
            native_mainnet_activated=False,
        )
        return True

    async def observe_native(self, pid: str) -> bool:
        """交易所移动止损在挂时，按币安同一规则本地推算它现在的止损价（币安不报）：记下挂单后
        看到的最有利标记价，标记价越过激活价（或下单即激活）后止损价 = 它 ×（1 ∓ 回撤比例）。

        只读一次标记价，不回读腿、不改交易所上的任何单；返回推算价是否变了。程序每秒采样一次，
        币安按每次标记价更新跟，两次采样之间的极值看不到，推算价可能比真实的略松。"""
        tracked, _ = self._current(pid)
        async with self.registry.symbol_lock(tracked.record.symbol):
            try:
                changed = self._observe_native(pid, await self._fresh_mark(tracked.record.symbol))
            except Exception as error:
                wrapped = TrailingBookUnavailable("交易所移动止损推算用的标记价不可用")
                wrapped.__cause__ = error
                self.record_trailing_error(pid, wrapped)
                raise wrapped from error
            state = self._current(pid)[1]
            if state.trailing_last_error is not None or state.trailing_failing_since is not None:
                self._save(pid, trailing_last_error=None, trailing_failing_since=None)
            return changed

    def _observe_native(self, pid: str, mark: float) -> bool:
        tracked, state = self._current(pid)
        if self._native_mode(tracked, state) != "confirmed":
            return False
        leg = max(
            (x for x in state.legs if _native(x) and _live(x)),
            key=lambda x: x.generation,
            default=None,
        )
        if leg is None or leg.callback_rate is None:
            return False
        long = tracked.record.side is Side.LONG
        best = max if long else min
        extreme = mark if state.native_extreme is None else best(state.native_extreme, mark)
        activated = (
            state.native_activated
            or leg.activate_price is None
            or (extreme >= leg.activate_price if long else extreme <= leg.activate_price)
        )
        stop = native_stop(long, extreme, leg.callback_rate) if activated else None
        changed = stop != state.native_stop_price
        self._save(
            pid,
            native_extreme=extreme,
            native_activated=activated,
            native_stop_price=stop,
            native_observed_at=self.clock(),
        )
        return changed

    def record_trailing_error(self, pid: str, error: BaseException) -> None:
        """移动止损这一轮没做成：只记原因给面板看，不改任何保护状态。"""
        # 已不是本状态机的持仓：没有地方可记，原异常照常由调用方处理。
        with contextlib.suppress(ProtectionIncomplete):
            since = self._current(pid)[1].trailing_failing_since or self.clock()
            self._save(pid, trailing_last_error=describe_error(error), trailing_failing_since=since)

    async def _observe_trailing(
        self,
        pid: str,
        basis: EntryBasis,
        read_book: Callable[[str], Awaitable[ExecutionBook]],
    ) -> bool:
        evidence = await self._evidence(pid)
        state = await self._refresh(pid)
        if state.stage != "armed" or not evidence.terminal:
            return False
        await self._checkpoint(pid, evidence)
        tracked, state = self._current(pid)
        if basis.client_order_id != tracked.entry_client_order_id:
            raise ProtectionIncomplete("移动止损入场身份不一致")
        max_dev = self.trailing_book_mark_max_dev_bps
        mark: float | None = None
        if max_dev > 0:  # 先读标记价再读盘口，让盘口离判断最近
            try:
                mark = await self._fresh_mark(tracked.record.symbol)
            except Exception as error:
                raise TrailingBookUnavailable("移动止损标记价不可用，不能核对盘口") from error
        try:
            book = await read_book(tracked.record.symbol)
        except ExchangeError as error:
            raise TrailingBookUnavailable("移动止损盘口读取失败") from error
        if mark is not None:
            # 只拦会把峰值抬高的方向：多仓买一高出标记价、空仓卖一低于标记价。反方向（含盘口
            # 价差宽、买一低于标记价）只会让峰值更保守，不拦，免得 Demo 宽价差把移动止损整个停掉。
            long = tracked.record.side is Side.LONG
            price = book.bid if long else book.ask
            dev = (price - mark if long else mark - price) / mark * 10_000
            if not math.isfinite(dev) or dev > max_dev:
                raise TrailingBookUnavailable(
                    f"移动止损盘口价 {price:g} 比标记价 {mark:g} "
                    f"{'高' if long else '低'} {dev:.1f} 基点（上限 {max_dev:g}），疑似插针，"
                    "本轮不更新峰值"
                )
        update = trailing_update(tracked.record, state, basis, book, self.clock())
        if update is None:
            return False
        changed = update.stop_price != state.trailing_stop_price
        self._save(
            pid,
            trailing_peak_net=update.peak_net,
            trailing_threshold_net=update.threshold_net,
            trailing_peak_net_exact=update.peak_net_exact,
            trailing_threshold_net_exact=update.threshold_net_exact,
            trailing_activated=update.activated,
            trailing_stop_price=update.stop_price,
            trailing_entry_quote=basis.filled_quote,
            trailing_entry_quantity=basis.filled_quantity,
            trailing_fees_quote=basis.fee_usdt,
            trailing_source_at=book.source_at,
            trailing_observed_at=book.observed_at,
            trailing_observation_gap=update.observation_gap,
        )
        return changed

    def legs_quiet(self, pid: str) -> bool:
        """每条腿都是最后回读明确在挂（NEW、未触发）或从未生效；不发网络请求。"""
        _, state = self._current(pid)
        return all(
            _live(leg)
            or leg.state in {"planned", "superseded", "rejected"}
            or (leg.state == "cancelled" and not _triggered(leg) and _settled(leg))
            for leg in state.legs
        )

    def exit_leg_working(self, pid: str) -> bool:
        """A leg has triggered and its child order has not been read back terminal yet."""
        _, state = self._current(pid)
        return any(_triggered(leg) and not _settled(leg) for leg in state.legs)

    def leg_fills(self, pid: str) -> list[tuple[CloseReason, float]]:
        """已触发且子单有成交的腿：(按哪条腿退出, 成交量)；只看最后回读，不发网络请求。"""
        _, state = self._current(pid)
        return [
            (_reason(leg), leg.filled_quantity)
            for leg in state.legs
            if _triggered(leg) and leg.filled_quantity is not None and leg.filled_quantity > 0
        ]

    def mark_exiting(self, pid: str) -> None:
        """进入退出：之后不再换代、不再观测移动止损；已挂的腿原样保留。"""
        self._save(pid, stage="exiting", trailing_last_error=None, trailing_failing_since=None)

    async def cancel_after_flat(self, pid: str) -> None:
        """成交推送已证实净仓为 0（``stream_close``）：马上撤还明确在挂的腿，不等同币锁、不先读
        持仓。撤不掉的抛出，留给 ``flat_cleanup`` 照常核验后再撤。"""
        tracked, _ = self._current(pid)
        symbol = tracked.record.symbol
        if self.registry.symbol_busy(symbol):
            # 同币锁有人拿着（换代、准备退出）：不跟它抢着撤，交给持锁的 flat_cleanup
            log.info("%s 同币锁在用，推送平仓后的撤腿留给收尾 %s", symbol, pid)
            return
        async with self.registry.symbol_lock(symbol):
            try:
                for leg in self._current(pid)[1].legs:
                    if _live(leg):
                        await self._cancel(pid, leg.client_order_id)
            finally:
                await self._flush_notes(pid, symbol)

    async def prepare_exit(self, pid: str) -> bool:
        tracked, _ = self._current(pid)
        try:
            return await self._prepare_exit(pid)
        finally:
            await self._flush_notes(pid, tracked.record.symbol)

    async def flat_cleanup(self, pid: str) -> bool:
        tracked, _ = self._current(pid)
        try:
            return await self._flat_cleanup(pid)
        finally:
            await self._flush_notes(pid, tracked.record.symbol)

    async def _prepare_exit(self, pid: str) -> bool:
        """停入场并查清未决子单；普通只减仓保护留到真实零仓，不先撤最后 SL。

        用户接受条件单与 HB / 程序退出并发；只能用单向只减仓、实际余量和禁止新代仓限制风险，
        不存在跨系统原子 OCO。未知子单仍禁止另发程序市价单。
        """
        tracked, _ = self._current(pid)
        async with self.registry.symbol_lock(tracked.record.symbol):
            try:
                # 进入退出后移动止损不再观测：清掉它的错误，面板不留过时提示。
                self._save(
                    pid, stage="exiting", trailing_last_error=None, trailing_failing_since=None
                )
                evidence = await self._evidence(pid)
                if not evidence.terminal:
                    return False
                state = await self._refresh(pid)
                for leg in state.legs:
                    if leg.state == "planned":
                        self._save_leg(pid, leg.model_copy(update={"state": "superseded"}))
                    elif not (_live(leg) or _settled(leg)) and not (
                        _side_leg(leg) and not _triggered(leg)
                    ):
                        # 没触发的独立腿（提交 / 撤销未知）只是多一张只减仓单，不挡退出
                        return False
                await self._checkpoint(pid, evidence)
                return True
            except Exception as error:
                self._save(pid, last_error=describe_error(error))
                return False

    async def _flat_cleanup(self, pid: str) -> bool:
        tracked, _ = self._current(pid)
        async with self.registry.symbol_lock(tracked.record.symbol):
            try:
                evidence = await self._evidence(pid)
                if not evidence.terminal or evidence.quantity != 0:
                    return False
                state = await self._refresh(pid)
                for leg in state.legs:
                    if _side_idle(leg):
                        await self._settle_orphan(pid, leg)
                state = self._current(pid)[1]
                for leg in state.legs:
                    await self._checkpoint(pid, evidence)
                    await self._cancel(pid, leg.client_order_id)
                    await self._checkpoint(pid, evidence)
                state = await self._refresh(pid)
                await self._checkpoint(pid, evidence)
                if not all(_settled(leg) for leg in state.legs):
                    return False
                self._save(pid, stage="done", last_error=None)
                return True
            except Exception as error:
                self._save(pid, last_error=describe_error(error))
                return False
