"""普通止盈/止损的可恢复编排：真实保护回读完成前不宣称已托管。

程序锁只能串行本程序；HB 时限单和交易所条件子单仍能独立触发。未知提交只查原 CID，
任何一代触发/子单未知都会停止改价；保护撤清且开仓残单终态后才允许程序另发市价退出。
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Protocol

from jiaoyi.contracts import LifeboatOrder, Side
from jiaoyi.contracts.position import CloseReason, LifeboatStatus
from jiaoyi.execution.clock import Clock, utc_now
from jiaoyi.execution.exchange import (
    BracketSubmissionRejected,
    BracketSubmissionUnknown,
    EntryBasis,
    ExchangeBracket,
    ExchangeEntryOrder,
    ExchangeError,
    ExchangePosition,
    ExecutionBook,
    NormalizedBracketSpec,
    WorkingType,
)
from jiaoyi.execution.registry import (
    PositionRegistry,
    ProtectiveLeg,
    ServerProtection,
    TrackedPosition,
)
from jiaoyi.execution.trailing import trailing_update


class BracketExchange(Protocol):
    async def position_mode(self) -> Literal["one_way", "hedge"]: ...
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


class ProtectionIncomplete(ExchangeError):
    """状态未知或保护不完整；调用方保留已确认保护并报告，不应擅自重复提交。"""

    def __init__(self, message: str, *, read_lag: bool = False) -> None:
        super().__init__(message)
        self.read_lag = read_lag
        """已提交且回执已接受的条件单暂时查不到（可能是交易所读延迟），不是参数或状态不符。"""


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


def _triggered(leg: ProtectiveLeg) -> bool:
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
        stop_working_type: WorkingType = "CONTRACT_PRICE",
        take_profit_working_type: WorkingType = "CONTRACT_PRICE",
    ) -> None:
        """``readback_grace_s``：本程序刚收到回执的条件单，回执后几秒内查无只重读；0 = 不等。

        ``prearm_grace_s``：从未完整托管时，可重试的同步失败最多连续容忍几秒；0 = 不容忍。

        ``stop_working_type`` / ``take_profit_working_type``：之后新建的止损（含移动止损新一代）/
        止盈腿用哪个触发价格源，建腿时写进账本；已有的腿一直按自己记下的核对，保命单固定标记价。
        """
        self.exchange, self.registry = exchange, registry
        self.account, self.connector, self.clock = account_name, connector_name, clock
        self.readback_grace_s, self._sleep = readback_grace_s, sleep
        self.prearm_grace_s = prearm_grace_s
        self.stop_working_type = stop_working_type
        self.take_profit_working_type = take_profit_working_type

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

    def set_tightened_stop(self, pid: str, price: float, reason: str) -> bool:
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
        )
        return True

    def prearm_grace(self, pid: str, error: BaseException) -> float | None:
        """从未完整托管时本轮同步失败能否先不平仓：返回连续失败已持续的秒数，None = 照旧退出。

        只宽限交易所读延迟（回执已接受的单暂时查无）和网络层一时失败；还必须：未进入退出、
        最后回读（或回执）仍有明确 NEW 的保命单或止损、没有任何腿处于提交/撤销未知、
        已触发、子单未知或被明确拒绝。计时从第一次可宽限失败算并落盘，重启不重新计时。
        """
        if self.prearm_grace_s <= 0:
            return None
        if isinstance(error, ProtectionIncomplete):
            if not error.read_lag:
                return None
        elif not (isinstance(error, ExchangeError) and error.transient):
            return None
        try:
            _, state = self._current(pid)
        except ProtectionIncomplete:
            return None
        if state.ever_armed or state.stage in {"exiting", "cleanup", "done"}:
            return None
        if not any(_live(leg) and _role(leg) in {"lifeboat", "stop_loss"} for leg in state.legs):
            return None
        if not all(
            leg.state in {"planned", "superseded"}
            or _live(leg)
            or (leg.state == "cancelled" and _settled(leg) and not _triggered(leg))
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
            if await self.exchange.position_mode() != "one_way":
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
            or order.trigger_price != leg.trigger_price
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

    async def _refresh(self, pid: str) -> ServerProtection:
        tracked, state = self._current(pid)
        for old in state.legs:
            if old.state in {"planned", "superseded", "rejected", "cancelled"}:
                continue
            read = (
                self.exchange.get_lifeboat
                if old.purpose == "lifeboat"
                else self.exchange.get_bracket
            )
            order = await read(tracked.record.symbol, client_order_id=old.client_order_id)
            if order is None:
                order = await self._await_readback(pid, old.client_order_id)
            if order is None:
                raise ProtectionIncomplete(
                    "已提交条件单暂时不可查，不能再次创建或当作已撤",
                    read_lag=old.state == "confirmed" and old.acked_at is not None,
                )
            leg = self._leg(pid, old.client_order_id)
            self._accept(pid, leg.model_copy(update={"acked_at": None}), order)
        _, state = self._current(pid)
        hit = next((leg for leg in state.legs if _triggered(leg)), None)
        if hit is not None:
            reason = _reason(hit)
            state = self._save(pid, stage="exiting", exit_reason=state.exit_reason or reason)
        return state

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

    async def _sync(self, pid: str) -> Literal["waiting", "provisional", "armed", "exit", "flat"]:
        evidence = await self._evidence(pid)
        state = await self._refresh(pid)
        if state.stage in {"exiting", "cleanup"}:
            return "exit"
        if evidence.quantity == 0:
            return "flat" if evidence.terminal else "waiting"
        tracked, state = self._current(pid)
        if tracked.record.entry_price is None:
            raise ProtectionIncomplete("实际入场价尚不可证")
        sign = 1 if tracked.record.side is Side.LONG else -1
        desired = {
            "lifeboat": tracked.record.entry_price * (1 - sign * tracked.lifeboat_stop_pct / 100),
            "stop_loss": tracked.record.stop_loss_price,
            "take_profit": tracked.record.take_profit_price,
        }
        if state.trailing_stop_price is not None and desired["stop_loss"] is not None:
            fixed_stop = desired["stop_loss"]
            assert fixed_stop is not None
            desired["stop_loss"] = (
                max(fixed_stop, state.trailing_stop_price)
                if tracked.record.side is Side.LONG
                else min(fixed_stop, state.trailing_stop_price)
            )
        tightened_wins = False
        if state.tightened_stop_price is not None and desired["stop_loss"] is not None:
            # 反向卡收紧价（#314）：固定 / 移动 / 收紧取最紧，换价走下面同一套新代先确认、再撤旧代。
            current_stop = desired["stop_loss"]
            tightened_wins = (
                state.tightened_stop_price > current_stop
                if tracked.record.side is Side.LONG
                else state.tightened_stop_price < current_stop
            )
            if tightened_wins:
                desired["stop_loss"] = state.tightened_stop_price
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
            current_cids = {legs[0].client_order_id for legs in matching.values()}
            if all(leg.client_order_id in current_cids or _settled(leg) for leg in state.legs):
                # 无改单：一轮完整三腿回读 + 前后开仓/净仓一致即可；不重复跑提交专用检查点。
                self._save(
                    pid,
                    stage="armed" if evidence.terminal else "provisional",
                    ever_armed=state.ever_armed or evidence.terminal,
                    last_error=None,
                )
                return "armed" if evidence.terminal else "provisional"
        for leg in state.legs:
            if leg.state == "planned":
                self._save_leg(pid, leg.model_copy(update={"state": "superseded"}))
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
                role = "sl" if kind == "stop_loss" else "tp"
                cid = (
                    f"lb-{pid}-{generation}"
                    if kind == "lifeboat"
                    else f"br-{role}-{pid}-{generation}"
                )
                if len(cid) > 36:
                    raise ProtectionIncomplete("条件单身份超过交易所长度限制")
                spec = specs[kind]
                # 新一代（含移动止损换价）按现在的设置；旧腿照自己记下的核对，新腿确认后才撤旧腿。
                working_type: WorkingType = (
                    "MARK_PRICE"
                    if kind == "lifeboat"
                    else (
                        self.stop_working_type
                        if kind == "stop_loss"
                        else self.take_profit_working_type
                    )
                )
                legs.append(
                    ProtectiveLeg(
                        kind="take_profit" if kind == "take_profit" else "stop_loss",
                        generation=generation,
                        client_order_id=cid,
                        quantity=spec.quantity,
                        trigger_price=spec.trigger_price,
                        purpose="lifeboat"
                        if kind == "lifeboat"
                        else (
                            "tightened"
                            if kind == "stop_loss" and tightened_wins
                            else "trailing"
                            if kind == "stop_loss" and state.trailing_activated
                            else "fixed"
                        ),
                        working_type=working_type,
                    )
                )
            self._save(pid, legs=legs, generation=generation, stage="provisional")
            for old in legs:
                if old.generation != generation:
                    continue
                await self._checkpoint(pid, evidence)
                await self._refresh(pid)
                if self._current(pid)[1].stage == "exiting":
                    return "exit"
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
                            kind=leg.kind,
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
                        return "exit"  # 交易所明确已越触发价，复用退出核验；旧保护仍保留。
                    raise ProtectionIncomplete(
                        "交易所明确拒绝普通保护，保留已有保护并退出"
                    ) from None
                except BracketSubmissionUnknown:
                    raise ProtectionIncomplete("普通保护提交结果未知，只查原CID") from None
                acked = self._leg(pid, leg.client_order_id)
                self._accept(pid, acked.model_copy(update={"acked_at": self.clock()}), order)
                await self._checkpoint(pid, evidence)
                state = await self._refresh(pid)
                if state.stage == "exiting":
                    return "exit"
        state = await self._refresh(pid)
        if state.stage == "exiting":
            return "exit"
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
        if not evidence.terminal:
            self._save(pid, stage="provisional")
            return "provisional"  # 开仓残单可能继续填入，不能把此刻量当最终量，也不撤旧保护。
        for leg in state.legs:
            if leg.client_order_id not in keep and not _settled(leg):
                await self._checkpoint(pid, evidence)
                await self._refresh(pid)
                if self._current(pid)[1].stage == "exiting":
                    return "exit"
                await self._cancel(pid, leg.client_order_id)
                await self._checkpoint(pid, evidence)
        final = await self._refresh(pid)
        await self._checkpoint(pid, evidence)
        if final.stage == "exiting":
            return "exit"
        if not all(_live(self._leg(pid, cid)) for cid in keep):
            raise ProtectionIncomplete("最后回读保护已改变")
        self._save(pid, stage="armed", ever_armed=True, last_error=None)
        return "armed"

    async def observe_trailing(self, pid: str, basis: EntryBasis, book: ExecutionBook) -> bool:
        tracked, _ = self._current(pid)
        async with self.registry.symbol_lock(tracked.record.symbol):
            evidence = await self._evidence(pid)
            state = await self._refresh(pid)
            if state.stage != "armed" or not evidence.terminal:
                return False
            await self._checkpoint(pid, evidence)
            tracked, state = self._current(pid)
            if basis.client_order_id != tracked.entry_client_order_id:
                raise ProtectionIncomplete("移动止损入场身份不一致")
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

    async def prepare_exit(self, pid: str) -> bool:
        """停入场并查清未决子单；普通只减仓保护留到真实零仓，不先撤最后 SL。

        用户接受条件单与 HB / 程序退出并发；只能用单向只减仓、实际余量和禁止新代仓限制风险，
        不存在跨系统原子 OCO。未知子单仍禁止另发程序市价单。
        """
        tracked, _ = self._current(pid)
        async with self.registry.symbol_lock(tracked.record.symbol):
            try:
                self._save(pid, stage="exiting")
                evidence = await self._evidence(pid)
                if not evidence.terminal:
                    return False
                state = await self._refresh(pid)
                for leg in state.legs:
                    if leg.state == "planned":
                        self._save_leg(pid, leg.model_copy(update={"state": "superseded"}))
                    elif not (_live(leg) or _settled(leg)):
                        return False
                await self._checkpoint(pid, evidence)
                return True
            except Exception as error:
                self._save(pid, last_error=describe_error(error))
                return False

    async def flat_cleanup(self, pid: str) -> bool:
        tracked, _ = self._current(pid)
        async with self.registry.symbol_lock(tracked.record.symbol):
            try:
                evidence = await self._evidence(pid)
                if not evidence.terminal or evidence.quantity != 0:
                    return False
                state = await self._refresh(pid)
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
