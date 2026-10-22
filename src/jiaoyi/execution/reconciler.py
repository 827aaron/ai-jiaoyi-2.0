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
from collections.abc import Awaitable, Callable
from datetime import datetime

from jiaoyi.contracts import Alert, ReconcileReport, Role
from jiaoyi.contracts.module_status import AlertLevel
from jiaoyi.contracts.position import PositionStatus, ReconcileIssue, ReconcileIssueKind
from jiaoyi.execution.exchange import STOP_PREFIX, ExchangeClient, ExchangeError, ExchangeStop
from jiaoyi.execution.hb_client import HbApiError, HummingbotApi
from jiaoyi.execution.hb_executor import HbExecutorAdapter
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
    ) -> None:
        self._adapter = adapter
        self._api = api
        self._exchange = exchange
        self._lifeboat = lifeboat
        self._registry = registry
        self._alerter = alerter
        self._account = account_name
        self._tol = qty_tolerance_pct / 100.0

    async def reconcile(self, now: datetime) -> ReconcileReport:
        try:
            await self._adapter.sync_all()
            exchange_positions = await self._exchange.positions()
            stops = await self._exchange.open_stops()
            running = await self._api.running_executors(self._account)
        except (HbApiError, ExchangeError) as e:
            await self._send(
                now,
                AlertLevel.CRITICAL,
                "对账失败：连不上 hummingbot-api 或币安",
                repr(e),
                None,
                "reconcile_unreachable",
            )
            raise ReconcileError(str(e)) from e

        issues: list[ReconcileIssue] = []
        live = self._registry.live()
        stops_by_cid = {s.client_order_id: s for s in stops}
        exchange_qty: dict[str, float] = {}
        for p in exchange_positions:
            exchange_qty[p.symbol] = exchange_qty.get(p.symbol, 0.0) + p.quantity

        # 1. 我们以为开着的仓：保命止损在不在、数量对不对
        tracked_qty: dict[str, float] = {}
        for t in live:
            rec = t.record
            if rec.status is PositionStatus.OPENING:
                continue
            tracked_qty[rec.symbol] = tracked_qty.get(rec.symbol, 0.0) + rec.quantity
            if rec.status is not PositionStatus.OPEN:
                continue
            stop = stops_by_cid.get(client_order_id(rec.position_id))
            if stop is None:
                issues.append(await self._repair_lifeboat(rec.position_id, now))
            elif rec.lifeboat is None or rec.lifeboat.exchange_order_id != stop.order_id:
                self._adapter.record_lifeboat(
                    rec.position_id,
                    await self._lifeboat.fetch(rec),
                )
        for symbol, qty in tracked_qty.items():
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
        for symbol, qty in exchange_qty.items():
            if symbol not in live_symbols:
                issues.append(
                    ReconcileIssue(
                        kind=ReconcileIssueKind.ORPHAN_POSITION,
                        symbol=symbol,
                        detail=f"币安有 {qty} 的仓位，但没有执行器在管",
                        fixed=False,
                    )
                )

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
                issues.append(
                    ReconcileIssue(
                        kind=ReconcileIssueKind.ORPHAN_POSITION,
                        symbol=symbol,
                        detail=f"hummingbot 里有运行中的执行器 {ex.get('executor_id')} 不在账本里",
                        fixed=False,
                    )
                )
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
        return report

    async def _repair_lifeboat(self, position_id: str, now: datetime) -> ReconcileIssue:
        tracked = self._registry.get(position_id)
        assert tracked is not None
        rec = tracked.record
        assert rec.entry_price is not None
        price = lifeboat_price(rec.side, rec.entry_price, tracked.lifeboat_stop_pct)
        try:
            order = await self._lifeboat.place(rec, price)
        except Exception as e:
            self._adapter.record_lifeboat(position_id, None)
            return ReconcileIssue(
                kind=ReconcileIssueKind.LIFEBOAT_MISSING,
                symbol=rec.symbol,
                position_id=position_id,
                detail=f"币安上找不到保命止损，补挂失败：{e!r}",
                fixed=False,
            )
        self._adapter.record_lifeboat(position_id, order)
        return ReconcileIssue(
            kind=ReconcileIssueKind.LIFEBOAT_MISSING,
            symbol=rec.symbol,
            position_id=position_id,
            detail="币安上找不到保命止损，已补挂",
            fixed=True,
        )

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
            f"{issue.kind.value}:{issue.position_id or issue.symbol}",
        )

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
