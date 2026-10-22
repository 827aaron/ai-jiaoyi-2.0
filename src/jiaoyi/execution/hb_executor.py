"""``ExecutorAdapter`` 的 hummingbot-api 实现：一笔持仓 = 一个 position executor + 一张保命止损。

流程：
  open   建执行器（开仓 + 止损 / 止盈 / 时间限制，由 hummingbot 每秒盯）
         → 等成交 → 立刻在币安挂保命止损；挂不上就把刚开的仓平掉（故障即关闭）
  close  停执行器并市价全平 → 确认执行器结束后才撤保命止损
  sync   （positions() 每次都会跑）执行器自己结束了（止损、止盈、时间到）就记账并撤保命止损

这里是全项目唯一建 / 停执行器的地方，只接受保镖签发的 ``OrderIntent``；出场永远不经过 AI。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from jiaoyi.contracts import (
    AccountState,
    Alert,
    LifeboatOrder,
    OrderIntent,
    PositionRecord,
    Role,
    Side,
)
from jiaoyi.contracts.module_status import AlertLevel
from jiaoyi.contracts.position import CloseReason, PositionStatus
from jiaoyi.execution.clock import Clock, utc_now
from jiaoyi.execution.hb_client import HbApiError, HummingbotApi
from jiaoyi.execution.lifeboat import lifeboat_price
from jiaoyi.execution.registry import DayStart, PositionRegistry, TrackedPosition
from jiaoyi.execution.symbols import to_hb_pair
from jiaoyi.interfaces import Alerter, LifeboatStop

log = logging.getLogger(__name__)

# hummingbot 的 OrderType 枚举值
_ORDER_MARKET = 1
_ORDER_LIMIT = 2

MARKER_PREFIX = "jy-"
"""写进 hummingbot ``controller_id`` 的认领标记前缀：``jy-<position_id>``。"""
DAY_START_TOLERANCE_S = 300.0

IncomeSince = Callable[[datetime], Awaitable[float]]

_CLOSE_TYPE_MAP = {
    "STOP_LOSS": CloseReason.STOP_LOSS,
    "TAKE_PROFIT": CloseReason.TAKE_PROFIT,
    "TRAILING_STOP": CloseReason.TRAILING_STOP,
    "TIME_LIMIT": CloseReason.TIME_LIMIT,
    "EXPIRED": CloseReason.TIME_LIMIT,
}


def _positive(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


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
        clock: Clock = utc_now,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._api = api
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
        self._clock = clock
        self._sleep = sleep

    # ------------------------------------------------------------------ 开仓

    def _executor_config(self, intent: OrderIntent) -> dict[str, Any]:
        barrier: dict[str, Any] = {
            "stop_loss": str(intent.stop_loss_pct / 100.0),
            "take_profit": str(intent.take_profit_pct / 100.0),
            "time_limit": intent.time_limit_s,
            "open_order_type": _ORDER_LIMIT if intent.order_type == "limit" else _ORDER_MARKET,
        }
        if intent.trailing_stop_pct is not None and intent.trailing_activation_pct is not None:
            barrier["trailing_stop"] = {
                "activation_price": str(intent.trailing_activation_pct / 100.0),
                "trailing_delta": str(intent.trailing_stop_pct / 100.0),
            }
        config: dict[str, Any] = {
            "type": "position_executor",
            "connector_name": self._connector,
            "trading_pair": to_hb_pair(intent.symbol),
            "side": "BUY" if intent.side is Side.LONG else "SELL",
            "amount": str(intent.quantity),
            "leverage": intent.leverage,
            "triple_barrier_config": barrier,
        }
        if intent.order_type == "limit":
            config["entry_price"] = str(intent.limit_price)
        return config

    async def open(self, intent: OrderIntent) -> PositionRecord:
        existing = self._registry.by_intent(intent.intent_id)
        if existing is not None:  # 重试同一条指令不重复开仓
            return existing.record
        now = self._clock()
        record = PositionRecord(
            position_id=f"pos-{uuid.uuid4().hex[:12]}",
            group_id=f"grp-{uuid.uuid4().hex[:12]}",
            intent_id=intent.intent_id,
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
        )
        # 先落账再建执行器：中途崩溃时重启能看到“有一笔开到一半的仓”
        tracked = TrackedPosition(
            record=record,
            lifeboat_stop_pct=intent.lifeboat_stop_pct,
            notional_usdt=intent.notional_usdt,
            stop_loss_pct=intent.stop_loss_pct,
            take_profit_pct=intent.take_profit_pct,
        )
        self._registry.put(tracked)
        marker = f"{MARKER_PREFIX}{record.position_id}"
        try:
            created = await self._api.create_executor(
                self._executor_config(intent), self._account, marker
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
            )
            await self.close(record.position_id, CloseReason.RECONCILER)
        return self._registry.data.positions[record.position_id].record

    async def _open_failed(
        self, tracked: TrackedPosition, symbol: str, *, raise_error: HbApiError
    ) -> PositionRecord:
        self._set(tracked, status=PositionStatus.FAILED, closed_at=self._clock())
        await self._alert(
            AlertLevel.CRITICAL,
            "开仓失败：建执行器出错",
            f"{symbol} {tracked.record.position_id}",
            symbol,
            f"open_failed:{tracked.record.position_id}",
        )
        raise raise_error

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

    def mark_closed_directly(self, position_id: str, reason: CloseReason) -> None:
        """对账员绕过 hummingbot 直连币安平仓成功后，把账本记为已平，免得每轮重复告警。"""
        tracked = self._registry.get(position_id)
        if tracked is not None:
            self._set(
                tracked,
                status=PositionStatus.CLOSED,
                closed_at=self._clock(),
                close_reason=reason,
            )

    def mark_failed(self, position_id: str) -> None:
        tracked = self._registry.get(position_id)
        if tracked is not None and tracked.record.status is PositionStatus.OPENING:
            self._set(tracked, status=PositionStatus.FAILED, closed_at=self._clock())

    # ------------------------------------------------------------------ 平仓

    async def close(self, position_id: str, reason: CloseReason) -> None:
        tracked = self._registry.get(position_id)
        if tracked is None:
            raise KeyError(f"不认识的持仓：{position_id}")
        if tracked.record.status in (PositionStatus.CLOSED, PositionStatus.FAILED):
            return
        executor_id = tracked.record.executor_id
        if executor_id is None:
            raise HbApiError(f"{position_id} 还没有执行器，无法平仓")
        before = tracked
        tracked = self._set(
            tracked,
            status=PositionStatus.CLOSING,
            requested_close=tracked.requested_close or reason,
        )
        try:
            await self._api.stop_executor(executor_id, keep_position=False)
        except Exception:
            # 没停成：退回原状态，下一轮对账还能继续处理这笔仓
            self._registry.put(before)
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
                )
                return
            await self._sleep(self._poll_s)

    # ------------------------------------------------------------------ 查询与同步

    async def positions(self) -> list[PositionRecord]:
        for tracked in self._registry.live():
            await self._sync_one(tracked)
        return [t.record for t in self._registry.live()]

    async def sync_all(self) -> None:
        """只同步，不返回；对账员用。"""
        await self.positions()

    def record_lifeboat(self, position_id: str, order: LifeboatOrder | None) -> None:
        """对账员补挂 / 发现保命止损后回写账本。"""
        tracked = self._registry.get(position_id)
        if tracked is not None:
            self._set(tracked, lifeboat=order)

    async def _sync_one(self, tracked: TrackedPosition) -> TrackedPosition:
        rec = tracked.record
        if rec.executor_id is None:
            return tracked
        detail = await self._api.get_executor(rec.executor_id)
        if detail is None:
            return tracked  # hummingbot 不认识这个执行器：交给对账员报 EXECUTOR_FAILED
        info = detail.get("custom_info") or {}
        entry = _positive(info.get("current_position_average_price"))

        if rec.status is PositionStatus.OPENING and entry is not None:
            tracked = self._mark_filled(tracked, entry)
            tracked = await self._try_place_lifeboat(tracked)

        if not detail.get("is_active", True):
            tracked = await self._finalize(tracked, detail, entry)
        return tracked

    def _mark_filled(self, tracked: TrackedPosition, entry: float) -> TrackedPosition:
        sign = 1.0 if tracked.record.side is Side.LONG else -1.0
        # 止损止盈价只用于展示和对账，真正的触发由 hummingbot 执行器判断
        return self._set(
            tracked,
            status=PositionStatus.OPEN,
            entry_price=entry,
            stop_loss_price=entry * (1.0 - sign * tracked.stop_loss_pct / 100.0),
            take_profit_price=entry * (1.0 + sign * tracked.take_profit_pct / 100.0),
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

    async def _finalize(
        self, tracked: TrackedPosition, detail: dict[str, Any], entry: float | None
    ) -> TrackedPosition:
        rec = tracked.record
        close_type = str(detail.get("close_type") or "").upper().removeprefix("CLOSETYPE.")
        never_filled = rec.entry_price is None and entry is None
        if never_filled:
            status, reason = PositionStatus.FAILED, None
        else:
            status = PositionStatus.CLOSED
            reason = tracked.requested_close or _CLOSE_TYPE_MAP.get(close_type, CloseReason.MANUAL)
        tracked = self._set(
            tracked,
            status=status,
            closed_at=self._clock(),
            close_reason=reason,
            realized_pnl_usdt=float(detail.get("net_pnl_quote") or 0.0),
            fees_usdt=abs(float(detail.get("cum_fees_quote") or 0.0)),
        )
        if never_filled:
            await self._alert(
                AlertLevel.WARN,
                "开仓没成交，执行器已结束",
                f"{rec.symbol} {rec.position_id}（{close_type or '未知'}）",
                rec.symbol,
                f"never_filled:{rec.position_id}",
            )
        try:
            await self._lifeboat.cancel(tracked.record)
        except Exception as e:
            # 撤不掉不致命（仓位已平，只减仓单不会开出新仓），对账员会清扫残留
            await self._alert(
                AlertLevel.WARN,
                "平仓后撤保命止损失败",
                f"{rec.symbol} {rec.position_id}：{e!r}",
                rec.symbol,
                f"lifeboat_cancel_failed:{rec.position_id}",
            )
        return tracked

    def _set(self, tracked: TrackedPosition, **changes: Any) -> TrackedPosition:
        requested = changes.pop("requested_close", tracked.requested_close)
        record = tracked.record.model_copy(update=changes)
        updated = tracked.model_copy(update={"record": record, "requested_close": requested})
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
        by_category: dict[Any, float] = {}
        for t in live:
            # 还没成交的（OPENING）没有入场价，按指令里的名义价值算，否则连发的信号会少算敞口
            notional = t.record.notional_usdt if t.record.entry_price else t.notional_usdt
            by_category[t.record.category] = by_category.get(t.record.category, 0.0) + notional
        return AccountState(
            ts=now,
            equity_usdt=equity,
            available_usdt=max(available, 0.0),
            day_start_equity_usdt=await self._day_start(now, wallet, equity),
            open_notional_usdt=sum(by_category.values()),
            open_notional_by_category=by_category,
            open_positions=len(live),
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
        )
        return base

    # ------------------------------------------------------------------ 告警

    async def _alert(
        self, level: AlertLevel, title: str, body: str, symbol: str | None, dedup_key: str
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
                )
            )
        except Exception:  # 告警失败不能拖住交易回路
            log.exception("告警发送失败：%s", title)
