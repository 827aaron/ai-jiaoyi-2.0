"""对账员：hummingbot-api、币安真实持仓、币安上的保命止损，三方每 30 秒核对一次。

为什么需要：hummingbot 的 main loop 遇到币安报错会静默重试，API 看起来一切正常，实际上
可能一直没成交、没平掉、止损没挂上。所以不信 hummingbot 的一面之词，直接问币安。

发现的问题：
  缺保命止损      → 立即补挂，critical 告警
  币安有仓位但不是我们开的 / 没有执行器管 → 只告警，不动（可能是 Aaron 手动开的）
  仓位已平但保命止损还挂着 → 撤掉
  数量对不上、执行器报错或消失 → 告警，人看
  方向对不上（币安和账本反向）/ 刚结束的仓被反向开了一笔等量的（平仓单在保命止损之后又成交，
  「抢跑反手仓」）→ 不管开关，当轮直连币安只减仓平掉，critical 告警；对不上方向时不补保命止损
  账本 OPEN 但币安连续 2 轮没有仓位 → 判定有人在币安手动平了：停执行器、按手动平仓结算、撤保命止损
重启后第一件事就是调一次 ``reconcile``。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timedelta
from typing import Any, Literal

from jiaoyi.contracts import Alert, Outcome, ReconcileReport, Role, Side
from jiaoyi.contracts.module_status import AlertLevel, Health
from jiaoyi.contracts.position import (
    CloseReason,
    PositionRecord,
    PositionStatus,
    ReconcileIssue,
    ReconcileIssueKind,
)
from jiaoyi.core.recorder import RoleRecorder
from jiaoyi.execution.exchange import (
    STOP_PREFIX,
    CloseFill,
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

_GONE_ROUNDS_BEFORE_FLATTEN = 2
_FLAT_ROUNDS_BEFORE_EXTERNAL_CLOSE = 2
_FLAT_ROUNDS_WITHOUT_FILL = 6
"""连续空仓但查不到任何平仓成交（没有第二个证据）时，要更多轮才结算，再用标记价估算。"""
REVERSE_WINDOW = timedelta(minutes=10)
"""执行器结束后这么久之内，币安上冒出同一个币、方向相反、数量相同的仓，就认定是我们的反手仓。"""
_CRITICAL = {
    ReconcileIssueKind.LIFEBOAT_MISSING,
    ReconcileIssueKind.ORPHAN_POSITION,
    ReconcileIssueKind.SIDE_MISMATCH,
}
_TITLES = {
    ReconcileIssueKind.LIFEBOAT_MISSING: "保命止损缺失",
    ReconcileIssueKind.ORPHAN_POSITION: "发现无人管理的仓位或执行器",
    ReconcileIssueKind.EXECUTOR_FAILED: "执行器异常",
    ReconcileIssueKind.QTY_MISMATCH: "持仓数量对不上",
    ReconcileIssueKind.STALE_LIFEBOAT: "仓位已平但保命止损还在",
    ReconcileIssueKind.EXTERNAL_CLOSE: "检测到手动平仓",
    ReconcileIssueKind.SIDE_MISMATCH: "币安仓位方向不对（反手仓）",
}


def _signed(side: Side, qty: float) -> float:
    return qty if side is Side.LONG else -qty


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
        self._gone_rounds: dict[str, int] = {}
        self._flat_rounds: dict[str, int] = {}
        self._rec = recorder
        self._close_unknown = close_unknown_positions
        self._heartbeats = heartbeats

    def set_next_run(self, at: datetime | None) -> None:
        """主循环按 ``reconcile_interval_s`` 排下一轮，团队页显示「下次对账」。"""
        if self._rec is not None:
            self._rec.set_next_run(at)

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
        # 带方向的数量（多 +、空 −）；单向持仓每个币只有一个仓位。exchange_qty 是绝对值
        signed: dict[str, float] = {}
        for p in exchange_positions:
            signed[p.symbol] = signed.get(p.symbol, 0.0) + _signed(p.side, p.quantity)
        exchange_qty = {sym: abs(q) for sym, q in signed.items() if q != 0.0}

        # 0. 账本 OPEN 但币安上已经没仓位：有人在币安手动平了。第 1 轮只记数（避免读取抖动），
        #    连续 2 轮才结算；这期间不补保命止损、不报数量对不上、不当成执行器丢失去平仓。
        running_ids = {str(e.get("executor_id")) for e in running}
        external: set[str] = set()
        for t in live:
            rec = t.record
            if rec.status is not PositionStatus.OPEN:
                continue
            if exchange_qty.get(rec.symbol, 0.0) > 0:
                self._flat_rounds.pop(rec.position_id, None)
                continue
            external.add(rec.position_id)
            rounds = self._flat_rounds.get(rec.position_id, 0) + 1
            self._flat_rounds[rec.position_id] = rounds
            if rounds >= _FLAT_ROUNDS_BEFORE_EXTERNAL_CLOSE:
                found = await self._external_close(t.record, running_ids, rounds)
                if found is not None:
                    issues.append(found)

        # 1. 我们以为开着的仓：方向对不对、保命止损在不在、数量对不对
        tracked_qty: dict[str, float] = {}
        flattened: set[str] = set()
        reversed_: set[str] = set()
        for t in live:
            rec = t.record
            if rec.status is PositionStatus.OPENING or rec.position_id in external:
                continue
            actual = signed.get(rec.symbol, 0.0)
            if actual != 0.0 and (actual > 0) != (rec.side is Side.LONG):
                # 方向反了：补保命止损（按账本方向下只减仓单）会失败，先处理反手仓
                reversed_.add(rec.symbol)
                issues.append(await self._side_mismatch(rec, exchange_positions))
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

        # 2. 币安上有仓位但账本里没有：刚结束的仓被反向开了等量的一笔 → 我们的反手仓，当轮平；
        #    其它不是我们开的，只告警（开关打开才平）
        live_symbols = {t.record.symbol for t in live}
        for symbol in exchange_qty:
            if symbol not in live_symbols:
                rev = await self._recent_reverse(symbol, exchange_positions, now)
                issues.append(rev or await self._unknown_position(symbol, exchange_positions))

        # 3. 残留的保命止损
        live_cids = {client_order_id(t.record.position_id) for t in live}
        for stop in stops:
            if (
                not stop.client_order_id.startswith(STOP_PREFIX)
                or stop.client_order_id in live_cids
                or stop.symbol in reversed_
            ):
                continue
            issues.append(await self._clear_stale(stop, exchange_qty))

        # 4. 执行器：报错、消失、没人认领
        known_ids = {t.record.executor_id for t in live if t.record.executor_id}
        # 执行器回来了 / 仓位没了的，清掉“消失”计数
        live_executor = {t.record.position_id: t.record.executor_id for t in live}
        running_now = {str(e.get("executor_id")) for e in running}
        for pid in list(self._gone_rounds):
            if live_executor.get(pid) is None or live_executor[pid] in running_now:
                self._gone_rounds.pop(pid, None)
        for t in live:
            if t.record.position_id in external or t.record.symbol in reversed_:
                continue
            if t.record.executor_id and t.record.executor_id not in running_ids:
                rec = t.record
                if (
                    rec.status is PositionStatus.OPEN
                    and rec.symbol not in flattened
                    and exchange_qty.get(rec.symbol, 0.0) > 0
                ):
                    # 执行器没了（hummingbot 重启 / 丢状态），止盈止损和时间限制都没人管，
                    # 币安上却还有仓。第 1 轮：本轮第 1 步已补挂保命止损兜底，只告警；
                    # 连续 2 轮仍没有执行器：直连只减仓平掉（避免一次读取抖动就平仓）。
                    rounds = self._gone_rounds.get(rec.position_id, 0) + 1
                    self._gone_rounds[rec.position_id] = rounds
                    if rounds < _GONE_ROUNDS_BEFORE_FLATTEN:
                        issues.append(
                            ReconcileIssue(
                                kind=ReconcileIssueKind.EXECUTOR_FAILED,
                                symbol=rec.symbol,
                                position_id=rec.position_id,
                                detail="hummingbot 里执行器已消失但币安仍有仓；保命止损在位，"
                                "下一轮仍然没有就直连平仓",
                                fixed=False,
                            )
                        )
                        continue
                    issue, was_flattened = await self._flatten(
                        rec.position_id,
                        "hummingbot 里执行器已消失但币安仍有仓",
                        kind=ReconcileIssueKind.EXECUTOR_FAILED,
                    )
                    issues.append(issue)
                    if was_flattened:
                        self._gone_rounds.pop(rec.position_id, None)
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
        open_ = sum(not i.fixed for i in issues)
        self._beat(now, healthy=open_ == 0, count=open_, fixed=len(issues) - open_)
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

    def _beat(self, now: datetime, *, healthy: bool, count: int, fixed: int = 0) -> None:
        """``count`` 只算没处理好的问题；已自动处理的（例如手动平仓已同步）不让对账灯继续报问题。"""
        health = Health.OK if healthy else Health.DEGRADED
        if count:
            headline = f"发现 {count} 个问题"
        elif fixed:
            headline = f"三方一致（本轮已处理 {fixed} 项）"
        else:
            headline = "三方一致"
        n = len(self._registry.live())
        if self._rec is not None:
            self._rec.note(now, f"对账：{headline}（在管 {n} 笔）", count=True)
            self._rec.heartbeat(now, health, headline)
        checked = getattr(self._lifeboat, "checked", None)
        if callable(checked):
            checked(now, f"对账员核对了 {n} 笔的保命止损：{headline}")
        for beat in self._heartbeats:
            beat(now, Health.OK, "")

    def _same_qty(self, a: float, b: float) -> bool:
        return abs(a - b) <= max(a, b) * self._tol

    async def _close_on_exchange(
        self, symbol: str, mine: list[ExchangePosition]
    ) -> ExchangeError | None:
        """直连币安只减仓市价平掉这个币在币安上的仓位（按币安上的方向），返回失败原因。"""
        try:
            for p in mine:
                side: Literal["buy", "sell"] = "sell" if p.side is Side.LONG else "buy"
                await self._exchange.close_position_market(symbol, side, p.quantity)
        except ExchangeError as e:
            return e
        return None

    async def _side_mismatch(
        self, rec: PositionRecord, exchange_positions: list[ExchangePosition]
    ) -> ReconcileIssue:
        """账本开着，但币安上同一个币的仓位方向相反。数量和账本一样 → 认定是平仓单抢跑开出的反手仓
        （原仓已被保命止损平掉），当轮平掉；数量不一样只告警，人看。账本那笔下一轮按空仓结算。"""
        mine = [p for p in exchange_positions if p.symbol == rec.symbol]
        qty = sum(p.quantity for p in mine)
        side_cn = "空" if rec.side is Side.LONG else "多"
        detail = (
            f"账本是{'多' if rec.side is Side.LONG else '空'} {rec.quantity}，币安是{side_cn} {qty}"
        )

        def issue(text: str, fixed: bool) -> ReconcileIssue:
            return ReconcileIssue(
                kind=ReconcileIssueKind.SIDE_MISMATCH,
                symbol=rec.symbol,
                position_id=rec.position_id,
                detail=text,
                fixed=fixed,
            )

        if not self._same_qty(qty, rec.quantity):
            return issue(f"{detail}，数量也对不上，未自动处理", False)
        err = await self._close_on_exchange(rec.symbol, mine)
        if err is not None:
            return issue(f"{detail}（反手仓），直连币安平仓失败：{err!r}", False)
        return issue(f"{detail}（反手仓），已直连币安只减仓平掉", True)

    async def _recent_reverse(
        self, symbol: str, exchange_positions: list[ExchangePosition], now: datetime
    ) -> ReconcileIssue | None:
        """币安上这个币的仓位，和最近 ``REVERSE_WINDOW`` 内刚结束的我们的仓方向相反、数量相同 →
        是平仓单在保命止损之后又成交开出来的反手仓：不管 close_unknown_positions 开关，当轮平掉。"""
        mine = [p for p in exchange_positions if p.symbol == symbol]
        if len(mine) != 1:
            return None
        pos = mine[0]
        recent = [
            t.record
            for t in self._registry.data.positions.values()
            if t.record.symbol == symbol
            and t.record.status is PositionStatus.CLOSED
            and t.record.closed_at is not None
            and timedelta(0) <= now - t.record.closed_at <= REVERSE_WINDOW
            and t.record.side is not pos.side
            and self._same_qty(pos.quantity, t.record.quantity)
        ]
        if not recent:
            return None
        rec = max(recent, key=lambda r: r.closed_at or now)
        detail = (
            f"{rec.position_id} 刚结束（{rec.close_reason.value if rec.close_reason else '?'}），"
            f"币安上却出现反向等量仓 {pos.quantity}（反手仓）"
        )
        err = await self._close_on_exchange(symbol, mine)
        fixed = err is None
        return ReconcileIssue(
            kind=ReconcileIssueKind.SIDE_MISMATCH,
            symbol=symbol,
            position_id=rec.position_id,
            detail=f"{detail}，{'已直连币安只减仓平掉' if fixed else f'直连平仓失败：{err!r}'}",
            fixed=fixed,
        )

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
                why = (
                    "价格已越过保命止损位（-2021），保命止损补不上"
                    if immediate
                    else f"保命止损连续 {fails} 轮补挂失败"
                )
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

    async def _external_close(
        self, rec: PositionRecord, running_ids: set[str], rounds: int
    ) -> ReconcileIssue | None:
        """币安上已没有这笔仓位（连续 2 轮）：按“有人手动平仓”结算。

        撤保命止损是不可逆的，所以结算前要有两个依据：(1) 单独再读一次持仓，读到有仓或读取失败
        就放弃并清零计数（接口连续误返回空列表不能让真实仓位失去止损）；(2) 平仓方向确有成交。
        没有成交时只告警，连续 ``_FLAT_ROUNDS_WITHOUT_FILL`` 轮仍空仓才结算并用标记价估算。
        不补止损、不再下平仓单（仓位已经是 0）；停执行器用 keep_position=True，免得它再发平仓单、
        在空仓上开出反向仓。"""
        pid = rec.position_id

        def issue(detail: str, fixed: bool) -> ReconcileIssue:
            return ReconcileIssue(
                kind=ReconcileIssueKind.EXTERNAL_CLOSE,
                symbol=rec.symbol,
                position_id=pid,
                detail=detail,
                fixed=fixed,
            )

        try:
            fresh = await self._exchange.positions()
        except ExchangeError as e:
            self._flat_rounds.pop(pid, None)
            return issue(f"币安上读不到持仓为空（重读失败），不按手动平仓处理：{e!r}", False)
        if any(p.symbol == rec.symbol and p.quantity > 0 for p in fresh):
            self._flat_rounds.pop(pid, None)  # 之前读到空是假象
            return None
        close_side: Literal["buy", "sell"] = "sell" if rec.side is Side.LONG else "buy"
        fill = await self._exchange.reduce_fill_since(rec.symbol, close_side, rec.opened_at)
        have_fill = fill is not None and fill.price is not None
        if not have_fill and rounds < _FLAT_ROUNDS_WITHOUT_FILL:
            return issue(
                "币安上已无仓位，但找不到平仓方向的成交，暂不按手动平仓结算（保命止损保留）", False
            )
        if rec.executor_id and rec.executor_id in running_ids:
            try:
                await self._api.stop_executor(rec.executor_id, keep_position=True)
            except HbApiError as e:
                return issue(
                    f"币安上已无仓位（疑似手动平仓），但停执行器失败，下一轮重试：{e!r}", False
                )
        source = "最近一笔只减仓成交"
        if fill is None or fill.price is None:
            fill = CloseFill(await self._exchange.mark_price(rec.symbol), None, estimated=True)
            source = "标记价（估算）" if fill.price is not None else "未取到价格，盈亏未记"
        self._adapter.mark_closed_directly(pid, CloseReason.MANUAL, fill)
        try:
            await self._adapter.release_lifeboat(pid)
        except Exception:
            log.exception("手动平仓后撤保命止损失败 %s", pid)
        self._flat_rounds.pop(pid, None)
        self._gone_rounds.pop(pid, None)
        self._lb_failures.pop(pid, None)
        return issue(f"币安上已无仓位，账本按手动平仓结算；平仓价取{source}", True)

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
                fill = await self._exchange.close_position_market(
                    rec.symbol, close_side, rec.quantity
                )
            except Exception as direct:
                return (
                    issue(
                        f"{why}，需平仓但失败：hummingbot {via_hb!r}；直连币安 {direct!r}", False
                    ),
                    False,
                )
            self._lb_failures.pop(position_id, None)
            self._adapter.mark_closed_directly(position_id, CloseReason.RECONCILER, fill)
            await self._adapter.release_lifeboat(position_id)
            return issue(f"{why}，hummingbot 平仓失败（{via_hb!r}），已直连币安平仓", True), True
        self._lb_failures.pop(position_id, None)
        return issue(f"{why}，已平仓", True), True

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
        if issue.kind in (ReconcileIssueKind.LIFEBOAT_MISSING, ReconcileIssueKind.SIDE_MISMATCH):
            level = AlertLevel.CRITICAL  # 处理好了也要让人知道为什么会发生
        if issue.kind is ReconcileIssueKind.EXTERNAL_CLOSE and issue.fixed:
            level = AlertLevel.INFO
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
