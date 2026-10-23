"""对账员：hummingbot-api、币安真实持仓、币安上的保命止损，三方每 30 秒核对一次。

为什么需要：hummingbot 的 main loop 遇到币安报错会静默重试，API 看起来一切正常，实际上
可能一直没成交、没平掉、止损没挂上。所以不信 hummingbot 的一面之词，直接问币安。

发现的问题：
  缺保命止损      → 立即补挂，critical 告警
  币安有仓位但不是我们开的 / 没有执行器管 → 只告警，不动（可能是 Aaron 手动开的）
  仓位已平但保命止损还挂着 → 撤掉
  数量对不上、执行器报错或消失 → 告警，人看
重启后第一件事就是调一次 ``reconcile``。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from typing import Any, Literal

from jiaoyi.contracts import Alert, Outcome, ReconcileReport, Role, Side
from jiaoyi.contracts.module_status import AlertLevel, Health
from jiaoyi.contracts.position import (
    CloseReason,
    PositionStatus,
    ReconcileIssue,
    ReconcileIssueKind,
)
from jiaoyi.core.recorder import RoleRecorder
from jiaoyi.execution.exchange import (
    STOP_PREFIX,
    ExchangeClient,
    ExchangeError,
    ExchangePosition,
    ExchangeStop,
)
from jiaoyi.execution.hb_client import HbApiError, HummingbotApi
from jiaoyi.execution.hb_executor import MARKER_PREFIX, HbExecutorAdapter
from jiaoyi.execution.lifeboat import client_order_id, lifeboat_price
from jiaoyi.execution.registry import PositionRegistry
from jiaoyi.execution.symbols import from_hb_pair
from jiaoyi.interfaces import Alerter, LifeboatStop

log = logging.getLogger(__name__)

_CRITICAL = {ReconcileIssueKind.LIFEBOAT_MISSING, ReconcileIssueKind.ORPHAN_POSITION}
_TITLES = {
    ReconcileIssueKind.LIFEBOAT_MISSING: "保命止损缺失",
    ReconcileIssueKind.ORPHAN_POSITION: "发现无人管理的仓位或执行器",
    ReconcileIssueKind.EXECUTOR_FAILED: "执行器异常",
    ReconcileIssueKind.QTY_MISMATCH: "持仓数量对不上",
    ReconcileIssueKind.STALE_LIFEBOAT: "仓位已平但保命止损还在",
}


class ReconcileError(Exception):
    """核对本身做不下去（hummingbot-api 或币安连不上）。已发 critical 告警。"""


class ThreeWayReconciler:
    def __init__(
        self,
        adapter: HbExecutorAdapter,
        api: HummingbotApi,
        exchange: ExchangeClient,
        lifeboat: LifeboatStop,
        registry: PositionRegistry,
        alerter: Alerter,
        *,
        account_name: str,
        qty_tolerance_pct: float = 1.0,
        unclaimed_grace_s: float = 60.0,
        max_lifeboat_failures: int = 2,
        recorder: RoleRecorder | None = None,
        heartbeats: Sequence[Callable[[datetime, Health, str], None]] = (),
        close_unknown_positions: bool = False,
    ) -> None:
        self._adapter = adapter
        self._api = api
        self._exchange = exchange
        self._lifeboat = lifeboat
        self._registry = registry
        self._alerter = alerter
        self._account = account_name
        self._tol = qty_tolerance_pct / 100.0
        self._grace_s = unclaimed_grace_s
        self._max_lb_failures = max_lifeboat_failures
        self._lb_failures: dict[str, int] = {}
        self._rec = recorder
        self._close_unknown = close_unknown_positions
        self._heartbeats = heartbeats

    async def reconcile(self, now: datetime) -> ReconcileReport:
        issues: list[ReconcileIssue] = []
        try:
            running = await self._api.running_executors(self._account)
            issues += await self._claim_unclaimed(running, now)
            await self._adapter.sync_all()
            exchange_positions = await self._exchange.positions()
            stops = await self._exchange.open_stops()
        except (HbApiError, ExchangeError) as e:
            await self._send(
                now,
                AlertLevel.CRITICAL,
                "对账失败：连不上 hummingbot-api 或币安",
                repr(e),
                None,
                "reconcile_unreachable",
            )
            if self._rec is not None:
                self._rec.record(now, Outcome.ERROR, f"对账失败：{e!r}")
                self._rec.heartbeat(now, Health.DOWN, "连不上 hummingbot-api 或币安")
            raise ReconcileError(str(e)) from e

        live = self._registry.live()
        stops_by_cid = {s.client_order_id: s for s in stops}
        exchange_qty: dict[str, float] = {}
        for p in exchange_positions:
            exchange_qty[p.symbol] = exchange_qty.get(p.symbol, 0.0) + p.quantity

        # 1. 我们以为开着的仓：保命止损在不在、数量对不对
        tracked_qty: dict[str, float] = {}
        flattened: set[str] = set()
        for t in live:
            rec = t.record
            if rec.status is PositionStatus.OPENING:
                continue
            tracked_qty[rec.symbol] = tracked_qty.get(rec.symbol, 0.0) + rec.quantity
            if rec.status is not PositionStatus.OPEN:
                continue
            stop = stops_by_cid.get(client_order_id(rec.position_id))
            if stop is None:
                issue, was_flattened = await self._repair_lifeboat(rec.position_id)
                issues.append(issue)
                if was_flattened:
                    flattened.add(rec.symbol)
            elif rec.lifeboat is None or rec.lifeboat.exchange_order_id != stop.order_id:
                self._adapter.record_lifeboat(
                    rec.position_id,
                    await self._lifeboat.fetch(rec),
                )
        for symbol, qty in tracked_qty.items():
            if symbol in flattened:
                continue
            actual = exchange_qty.get(symbol, 0.0)
            if abs(actual - qty) > qty * self._tol:
                issues.append(
                    ReconcileIssue(
                        kind=ReconcileIssueKind.QTY_MISMATCH,
                        symbol=symbol,
                        detail=f"账本 {qty}，币安 {actual}",
                        fixed=False,
                    )
                )

        # 2. 币安上有仓位但账本里没有：不是我们开的，只告警
        live_symbols = {t.record.symbol for t in live}
        for symbol in exchange_qty:
            if symbol not in live_symbols:
                issues.append(await self._unknown_position(symbol, exchange_positions))

        # 3. 残留的保命止损
        live_cids = {client_order_id(t.record.position_id) for t in live}
        for stop in stops:
            if (
                not stop.client_order_id.startswith(STOP_PREFIX)
                or stop.client_order_id in live_cids
            ):
                continue
            issues.append(await self._clear_stale(stop, exchange_qty))

        # 4. 执行器：报错、消失、没人认领
        known_ids = {t.record.executor_id for t in live if t.record.executor_id}
        running_ids = {str(e.get("executor_id")) for e in running}
        for t in live:
            if t.record.executor_id and t.record.executor_id not in running_ids:
                rec = t.record
                if (
                    rec.status is PositionStatus.OPEN
                    and rec.symbol not in flattened
                    and exchange_qty.get(rec.symbol, 0.0) > 0
                ):
                    # 执行器没了（hummingbot 重启 / 丢状态），止盈止损和时间限制都没人管，
                    # 币安上却还有仓：直连只减仓平掉，不能让它裸奔到只剩保命止损
                    issue, was_flattened = await self._flatten(
                        rec.position_id,
                        "hummingbot 里执行器已消失但币安仍有仓",
                        kind=ReconcileIssueKind.EXECUTOR_FAILED,
                    )
                    issues.append(issue)
                    if was_flattened:
                        flattened.add(rec.symbol)
                        continue
                issues.append(
                    ReconcileIssue(
                        kind=ReconcileIssueKind.EXECUTOR_FAILED,
                        symbol=t.record.symbol,
                        position_id=t.record.position_id,
                        detail="账本里还在持仓，但 hummingbot 里执行器已不在运行",
                        fixed=False,
                    )
                )
        for ex in running:
            pair = str(ex.get("trading_pair") or "")
            try:
                symbol = from_hb_pair(pair)
            except ValueError:
                continue
            if str(ex.get("executor_id")) not in known_ids:
                issues.append(await self._unknown_executor(symbol, str(ex.get("executor_id"))))
            elif int(ex.get("error_count") or 0) > 0:
                # main loop 的静默重试在这里露出来
                owner = next(
                    (t for t in live if t.record.executor_id == ex.get("executor_id")), None
                )
                issues.append(
                    ReconcileIssue(
                        kind=ReconcileIssueKind.EXECUTOR_FAILED,
                        symbol=symbol,
                        position_id=owner.record.position_id if owner else None,
                        detail=f"执行器报错 {ex.get('error_count')} 次：{ex.get('last_error')}",
                        fixed=False,
                    )
                )

        report = ReconcileReport(ts=now, checked_positions=len(live), issues=issues)
        for issue in issues:
            await self._send_issue(now, issue)
            self._record_issue(now, issue)
        self._beat(now, healthy=all(i.fixed for i in issues), count=len(issues))
        return report

    def _record_issue(self, now: datetime, issue: ReconcileIssue) -> None:
        """按验收约定：没修好的问题 = error，修好的 = action。"""
        if self._rec is not None:
            self._rec.record(
                now,
                Outcome.ACTION if issue.fixed else Outcome.ERROR,
                f"{_TITLES[issue.kind]}{'，已处理' if issue.fixed else ''}：{issue.detail}",
                symbol=issue.symbol,
                position_id=issue.position_id,
                output={"kind": issue.kind.value, "fixed": issue.fixed},
            )

    def _beat(self, now: datetime, *, healthy: bool, count: int) -> None:
        health = Health.OK if healthy else Health.DEGRADED
        headline = "三方一致" if count == 0 else f"发现 {count} 个问题"
        if self._rec is not None:
            self._rec.heartbeat(now, health, headline)
        for beat in self._heartbeats:
            beat(now, Health.OK, "")

    async def _unknown_position(
        self, symbol: str, exchange_positions: list[ExchangePosition]
    ) -> ReconcileIssue:
        """币安有仓位但账本里没有。默认只告警；打开开关后直连币安只减仓市价平掉。"""
        mine = [p for p in exchange_positions if p.symbol == symbol]
        qty = sum(p.quantity for p in mine)
        detail = f"币安有 {qty} 的仓位，但没有执行器在管"
        if not self._close_unknown or not mine:
            return ReconcileIssue(
                kind=ReconcileIssueKind.ORPHAN_POSITION, symbol=symbol, detail=detail, fixed=False
            )
        try:
            for p in mine:
                side: Literal["buy", "sell"] = "sell" if p.side is Side.LONG else "buy"
                await self._exchange.close_position_market(symbol, side, p.quantity)
        except ExchangeError as e:
            return ReconcileIssue(
                kind=ReconcileIssueKind.ORPHAN_POSITION,
                symbol=symbol,
                detail=f"{detail}；开关已打开但平仓失败：{e!r}",
                fixed=False,
            )
        return ReconcileIssue(
            kind=ReconcileIssueKind.ORPHAN_POSITION,
            symbol=symbol,
            detail=f"{detail}；已按开关直连币安平仓",
            fixed=True,
        )

    async def _unknown_executor(self, symbol: str, executor_id: str) -> ReconcileIssue:
        detail = f"hummingbot 里有运行中的执行器 {executor_id} 不在账本里"
        if not self._close_unknown:
            return ReconcileIssue(
                kind=ReconcileIssueKind.ORPHAN_POSITION, symbol=symbol, detail=detail, fixed=False
            )
        try:
            await self._api.stop_executor(executor_id, keep_position=False)
        except HbApiError as e:
            return ReconcileIssue(
                kind=ReconcileIssueKind.ORPHAN_POSITION,
                symbol=symbol,
                detail=f"{detail}；开关已打开但停止失败：{e!r}",
                fixed=False,
            )
        return ReconcileIssue(
            kind=ReconcileIssueKind.ORPHAN_POSITION,
            symbol=symbol,
            detail=f"{detail}；已按开关停止并平仓",
            fixed=True,
        )

    async def _repair_lifeboat(self, position_id: str) -> tuple[ReconcileIssue, bool]:
        """补挂保命止损。补不上的账本内持仓不能一直裸着：-2021 当轮平，其它原因连续几轮失败后平。"""
        tracked = self._registry.get(position_id)
        assert tracked is not None
        rec = tracked.record
        assert rec.entry_price is not None
        price = lifeboat_price(rec.side, rec.entry_price, tracked.lifeboat_stop_pct)
        try:
            order = await self._lifeboat.place(rec, price)
        except Exception as e:
            fails = self._lb_failures.get(position_id, 0) + 1
            self._lb_failures[position_id] = fails
            self._adapter.record_lifeboat(position_id, None)
            immediate = isinstance(e, ExchangeError) and e.immediate_trigger
            if immediate or fails >= self._max_lb_failures:
                why = "价格已越过保命止损位（-2021）" if immediate else f"连续 {fails} 轮补挂失败"
                return await self._flatten(position_id, why)
            return ReconcileIssue(
                kind=ReconcileIssueKind.LIFEBOAT_MISSING,
                symbol=rec.symbol,
                position_id=position_id,
                detail=f"币安上找不到保命止损，补挂失败（第 {fails} 次）：{e!r}",
                fixed=False,
            ), False
        self._lb_failures.pop(position_id, None)
        self._adapter.record_lifeboat(position_id, order)
        return ReconcileIssue(
            kind=ReconcileIssueKind.LIFEBOAT_MISSING,
            symbol=rec.symbol,
            position_id=position_id,
            detail="币安上找不到保命止损，已补挂",
            fixed=True,
        ), False

    async def _flatten(
        self,
        position_id: str,
        why: str,
        *,
        kind: ReconcileIssueKind = ReconcileIssueKind.LIFEBOAT_MISSING,
    ) -> tuple[ReconcileIssue, bool]:
        """平掉没有保命止损的仓。先走 hummingbot；它不可达时直连币安下只减仓市价单兜底。"""
        tracked = self._registry.get(position_id)
        assert tracked is not None
        rec = tracked.record

        def issue(detail: str, fixed: bool) -> ReconcileIssue:
            return ReconcileIssue(
                kind=kind,
                symbol=rec.symbol,
                position_id=position_id,
                detail=detail,
                fixed=fixed,
            )

        try:
            await self._adapter.close(position_id, CloseReason.RECONCILER)
        except Exception as via_hb:
            close_side: Literal["buy", "sell"] = "sell" if rec.side is Side.LONG else "buy"
            try:
                await self._exchange.close_position_market(rec.symbol, close_side, rec.quantity)
            except Exception as direct:
                return (
                    issue(
                        f"{why}，需平仓但失败：hummingbot {via_hb!r}；直连币安 {direct!r}", False
                    ),
                    False,
                )
            self._lb_failures.pop(position_id, None)
            self._adapter.mark_closed_directly(position_id, CloseReason.RECONCILER)
            await self._adapter.release_lifeboat(position_id)
            return issue(f"{why}，hummingbot 平仓失败（{via_hb!r}），已直连币安平仓", True), True
        self._lb_failures.pop(position_id, None)
        return issue(f"{why}，保命止损补不上，已平仓", True), True

    async def _claim_unclaimed(
        self, running: list[dict[str, Any]], now: datetime
    ) -> list[ReconcileIssue]:
        """认领带我们标记的执行器：建执行器时结果不明、账本里没有 executor_id 或已标 FAILED。"""
        issues: list[ReconcileIssue] = []
        by_marker = {str(e.get("controller_id")): e for e in running}
        for t in self._registry.data.positions.values():
            rec = t.record
            marker = f"{MARKER_PREFIX}{rec.position_id}"
            if rec.status is PositionStatus.CLOSED:
                continue
            if rec.executor_id is not None and rec.status is not PositionStatus.FAILED:
                continue
            found = by_marker.get(marker)
            if found is None and rec.status is not PositionStatus.FAILED:
                matches = await self._api.find_executors(self._account, marker)
                found = matches[0] if matches else None
            if found is not None:
                await self._adapter.claim_executor(rec.position_id, str(found["executor_id"]))
                issues.append(
                    ReconcileIssue(
                        kind=ReconcileIssueKind.EXECUTOR_FAILED,
                        symbol=rec.symbol,
                        position_id=rec.position_id,
                        detail=f"认领了带我们标记的执行器 {found['executor_id']}，继续挂保命止损",
                        fixed=True,
                    )
                )
            elif (
                rec.status is PositionStatus.OPENING
                and (now - rec.opened_at).total_seconds() > self._grace_s
            ):
                self._adapter.mark_failed(rec.position_id)
                issues.append(
                    ReconcileIssue(
                        kind=ReconcileIssueKind.EXECUTOR_FAILED,
                        symbol=rec.symbol,
                        position_id=rec.position_id,
                        detail="开仓结果不明且按标记查不到执行器，已标为失败",
                        fixed=False,
                    )
                )
        return issues

    async def _clear_stale(
        self, stop: ExchangeStop, exchange_qty: dict[str, float]
    ) -> ReconcileIssue:
        if exchange_qty.get(stop.symbol, 0.0) > 0:
            # 仓位还在却没有账本：撤掉保命止损会让它裸奔，只告警
            return ReconcileIssue(
                kind=ReconcileIssueKind.STALE_LIFEBOAT,
                symbol=stop.symbol,
                detail=f"保命止损 {stop.client_order_id} 不属于任何在管持仓，但币安仍有仓位，未撤",
                fixed=False,
            )
        try:
            await self._exchange.cancel_stop(stop.symbol, stop.order_id)
        except ExchangeError as e:
            return ReconcileIssue(
                kind=ReconcileIssueKind.STALE_LIFEBOAT,
                symbol=stop.symbol,
                detail=f"残留保命止损 {stop.client_order_id} 撤销失败：{e!r}",
                fixed=False,
            )
        return ReconcileIssue(
            kind=ReconcileIssueKind.STALE_LIFEBOAT,
            symbol=stop.symbol,
            detail=f"仓位已平，已撤残留保命止损 {stop.client_order_id}",
            fixed=True,
        )

    async def _send_issue(self, now: datetime, issue: ReconcileIssue) -> None:
        level = (
            AlertLevel.CRITICAL if issue.kind in _CRITICAL and not issue.fixed else AlertLevel.WARN
        )
        if issue.kind is ReconcileIssueKind.LIFEBOAT_MISSING:
            level = AlertLevel.CRITICAL  # 补上了也要让人知道为什么会丢
        title = _TITLES[issue.kind] + ("，已处理" if issue.fixed else "")
        await self._send(
            now,
            level,
            title,
            f"{issue.symbol} {issue.detail}",
            issue.symbol,
            self._dedup_key(now, issue),
        )

    @staticmethod
    def _dedup_key(now: datetime, issue: ReconcileIssue) -> str:
        key = f"{issue.kind.value}:{issue.position_id or issue.symbol}"
        if issue.kind is ReconcileIssueKind.LIFEBOAT_MISSING and not issue.fixed:
            # 没保命止损又没平掉的仓，每一轮都要响，不能被冷却吞掉
            key += f":{int(now.timestamp())}"
        return key

    async def _send(
        self,
        now: datetime,
        level: AlertLevel,
        title: str,
        body: str,
        symbol: str | None,
        dedup_key: str,
    ) -> None:
        try:
            await self._alerter.send(
                Alert(
                    ts=now,
                    level=level,
                    role=Role.RECONCILER,
                    title=title,
                    body=body,
                    symbol=symbol,
                    dedup_key=dedup_key,
                )
            )
        except Exception:
            log.exception("告警发送失败：%s", title)


async def run_reconcile_loop(
    reconciler: ThreeWayReconciler,
    interval_s: float,
    now: Callable[[], datetime],
    stop: asyncio.Event,
    on_report: Callable[[ReconcileReport], Awaitable[None]] | None = None,
) -> None:
    """每 ``interval_s`` 秒核对一次，直到 ``stop`` 被置位。单次失败只记日志（告警已在内部发出）。"""
    while not stop.is_set():
        try:
            report = await reconciler.reconcile(now())
            if on_report is not None:
                await on_report(report)
        except ReconcileError:
            pass
        except Exception:
            log.exception("对账员意外出错")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
