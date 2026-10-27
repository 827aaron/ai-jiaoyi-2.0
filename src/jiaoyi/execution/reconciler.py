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
  无主仓 / 反手仓的直连平仓：同币串行，先落盘意图和 CID 再发；结果未知只查原 CID，不重发；
  交易所复查净仓为零才算平掉。账本里还有别的归属、方向变了、数量涨了、同币有不认识的执行器在跑，
  都不下单只告警。不认识的执行器按开关只停不平（keep_position=True），余仓由上面的通道平
  账本 OPEN 但币安连续 2 轮没有仓位 → 判定有人在币安手动平了：停执行器、按手动平仓结算、撤保命止损
重启后第一件事就是调一次 ``reconcile``。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
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
    IdentifiedCloseOrder,
    IdentifiedCloseRejected,
    IdentifiedCloseUnknown,
)
from jiaoyi.execution.exit_resolution import (
    ResolutionRequest,
    finish_resolution,
    pending_resolutions,
    resolutions_dir,
)
from jiaoyi.execution.hb_client import HbApiError, HummingbotApi, executor_query_state
from jiaoyi.execution.hb_executor import MARKER_PREFIX, HbExecutorAdapter
from jiaoyi.execution.lifeboat import lifeboat_cids, lifeboat_price
from jiaoyi.execution.registry import EmergencyExit, ForeignExecutorStop, PositionRegistry
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


_TRADES_PAGE = 1000
"""成交明细一页的条数；拿满一页可能不全，不能当归属证据。"""
_EVIDENCE_SLACK = timedelta(seconds=10)
"""归属证据从开仓时间往前多看这么久（和账本回填入场成交同口径）。"""
_EPS = 1e-9
"""意图数量上限的相对容差（只吸收浮点误差，不是数量对不上的容忍度）。"""
ExitSource = Literal["unknown", "reverse", "side_mismatch"]


@dataclass(frozen=True)
class _ExitTrigger:
    """这一轮判定要平的那笔：来源、触发时看到的仓位、相关账本仓、告警前缀。"""

    source: ExitSource
    position: ExchangePosition
    position_id: str | None
    detail: str


class ReconcileError(Exception):
    """核对本身做不下去（hummingbot-api 或币安连不上）。

    明确的错误（鉴权、限流、时间戳…）立即发 critical；超时 / 断连这类一时失败先在本轮里短退避重试，
    还不行只记日志，连续 ``UNREACHABLE_ALERT_ROUNDS`` 轮都这样才发。"""


RETRY_DELAYS_S = (0.5, 1.5)
"""一时失败（超时、断连）本轮内重试的退避。"""
UNREACHABLE_ALERT_ROUNDS = 2


def _transient(e: BaseException) -> bool:
    return bool(getattr(e, "transient", False))


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
        retry_delays_s: Sequence[float] = RETRY_DELAYS_S,
        executor_stop_retry_rounds: int = 0,
    ) -> None:
        self._stop_retry_rounds = executor_stop_retry_rounds
        """停执行器结果未知后再过几轮仍没终止，就再发一次 keep_position=True 停止；0 = 永不。"""
        self._retry_delays = tuple(retry_delays_s)
        self._unreachable_rounds = 0
        """连续几轮因为一时失败（超时 / 断连）没对成。"""
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
        self._extra_seen: dict[str, int] = {}
        self._closing_rounds: dict[str, int] = {}
        """正在平仓的持仓连续几轮还没结算完。"""
        """同一笔多出来的保命止损（order_id）连续看到几轮。"""
        self._rec = recorder
        self._close_unknown = close_unknown_positions
        self._heartbeats = heartbeats
        self._foreign_running: dict[str, set[str]] = {}
        """本轮运行列表里不归账本管的执行器（按币种）；有就不另发直连平仓。"""

    async def _retry(self, fn: Callable[[], Awaitable[Any]]) -> Any:
        """只读查询：超时 / 断连这类一时失败短退避重试；明确的错误直接抛。"""
        for delay in (*self._retry_delays, None):
            try:
                return await fn()
            except (HbApiError, ExchangeError) as e:
                if delay is None or not _transient(e):
                    raise
                log.warning("对账查询一时失败，%.1f 秒后重试：%r", delay, e)
                await asyncio.sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover

    async def _unreachable(self, now: datetime, e: Exception) -> None:
        """明确的错误立即告警；一时失败（重试过还不行）连续 2 轮才告警，单轮只记日志。"""
        transient = _transient(e)
        self._unreachable_rounds = self._unreachable_rounds + 1 if transient else 0
        if transient and self._unreachable_rounds < UNREACHABLE_ALERT_ROUNDS:
            log.warning("对账这轮没对成（%r），重试过了，下轮再对", e)
            if self._rec is not None:
                self._rec.note(now, "这轮查持仓超时，重试过还不行，下轮再对（连续 2 轮才告警）")
                self._rec.heartbeat(now, Health.DEGRADED, "上轮查持仓超时，下轮再对")
            return
        rounds = f"（连续 {self._unreachable_rounds} 轮）" if transient else ""
        await self._send(
            now,
            AlertLevel.CRITICAL,
            f"对账失败：连不上 hummingbot-api 或币安{rounds}",
            repr(e),
            None,
            "reconcile_unreachable",
            "reconcile",
        )
        if self._rec is not None:
            self._rec.record(now, Outcome.ERROR, f"对账失败{rounds}：{e!r}")
            self._rec.heartbeat(now, Health.DOWN, "连不上 hummingbot-api 或币安")

    def set_next_run(self, at: datetime | None) -> None:
        """主循环按 ``reconcile_interval_s`` 排下一轮，团队页显示「下次对账」。"""
        if self._rec is not None:
            self._rec.set_next_run(at)

    async def reconcile(self, now: datetime) -> ReconcileReport:
        issues: list[ReconcileIssue] = []
        try:
            running = await self._retry(lambda: self._api.running_executors(self._account))
            issues += await self._claim_unclaimed(running, now)
            await self._retry(self._adapter.sync_all)
            positions_read = getattr(self._exchange, "positions_evidence", None)
            if positions_read is None:
                raise ExchangeError("缺严格持仓查询能力，不能把未知当零仓")
            exchange_positions = await self._retry(positions_read)
            stops = await self._retry(self._exchange.open_stops)
        except (HbApiError, ExchangeError) as e:
            # 本轮没有完整核验，不可把上次终态与下次终态拼成「连续两轮」。
            self._gone_rounds.clear()
            await self._unreachable(now, e)
            raise ReconcileError(str(e)) from e
        self._unreachable_rounds = 0
        await self._apply_resolutions(now)

        live = self._registry.live()
        # 正在平仓（平仓单已发、执行器还没结算）：币安已平、账本还没结是正常的几秒，
        # 第 1 轮不报「数量对不上」「执行器异常」，连续 2 轮还在平才报
        self._closing_rounds = {
            t.record.position_id: self._closing_rounds.get(t.record.position_id, 0) + 1
            for t in live
            if t.record.status is PositionStatus.CLOSING
        }
        pending_direct = {
            t.record.position_id
            for t in live
            if t.direct_close_pending
            or t.hb_close_unknown
            or t.direct_close_client_order_id is not None
        }
        settling = {
            pid for pid, n in self._closing_rounds.items() if n < 2 and pid not in pending_direct
        }
        for t in live:
            if t.record.position_id in pending_direct:
                issues.append(
                    ReconcileIssue(
                        kind=ReconcileIssueKind.EXECUTOR_FAILED,
                        symbol=t.record.symbol,
                        position_id=t.record.position_id,
                        detail="平仓结果尚未确认，余仓与保护保留；禁止未知结果重复下单",
                        fixed=False,
                    )
                )
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
        extra_now: set[str] = set()
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
            if rec.position_id in settling:
                continue
            if t.server_protection is not None:
                protection_state = t.server_protection
                observed = protection_state.observed_quantity
                tracked_qty[rec.symbol] = tracked_qty.get(rec.symbol, 0.0) + (
                    observed if observed is not None else rec.quantity
                )
                if protection_state.stage != "armed":
                    issues.append(
                        ReconcileIssue(
                            kind=ReconcileIssueKind.EXECUTOR_FAILED,
                            symbol=rec.symbol,
                            position_id=rec.position_id,
                            detail=f"交易所托管仍在{protection_state.stage}阶段；保留已确认保护，由持久订单状态核验，未宣称已平",
                            fixed=False,
                        )
                    )
                continue  # 所有代的 MARK/普通腿由同一协调器管理，不能走旧两CID补挂/删多单。
            tracked_qty[rec.symbol] = tracked_qty.get(rec.symbol, 0.0) + rec.quantity
            if rec.status is not PositionStatus.OPEN and rec.position_id not in pending_direct:
                continue
            # 重挂时短暂会有两张（新 cid 带后缀）：账本记着的那张优先
            known = rec.lifeboat.client_order_id if rec.lifeboat is not None else None
            cids = sorted(lifeboat_cids(rec.position_id), key=lambda c: c != known)
            stop = next((stops_by_cid[c] for c in cids if c in stops_by_cid), None)
            if stop is not None:
                for c in cids:
                    extra = stops_by_cid.get(c)
                    if extra is None or extra.order_id == stop.order_id:
                        continue
                    # 重挂进行中也会短暂两张（新单已挂、账本还没改）：同一张连续两轮都多出来才撤
                    extra_now.add(extra.order_id)
                    if self._extra_seen.get(extra.order_id, 0) >= 1:
                        issues.append(await self._drop_extra(rec, extra, stop))
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
        self._extra_seen = {oid: self._extra_seen.get(oid, 0) + 1 for oid in extra_now}
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
        #    其它不是我们开的，只告警（开关打开才平）。已有未完成退出意图的币只核对原意图。
        await self._settle_foreign_stops(count_round=True)
        self._foreign_running = self._foreign_executors(running)
        exits = self._registry.data.emergency_exits
        handled = set(reversed_)
        live_symbols = {t.record.symbol for t in live}
        for symbol in exchange_qty:
            if symbol not in live_symbols:
                handled.add(symbol)
                if symbol in exits:
                    found = await self._emergency_exit(symbol, None)
                    if found is not None:
                        issues.append(found)
                        continue
                pending_owner = next(
                    (
                        t
                        for t in self._registry.data.positions.values()
                        if t.record.symbol == symbol
                        and t.server_protection is not None
                        and t.server_protection.stage != "done"
                    ),
                    None,
                )
                if pending_owner is not None:
                    issues.append(
                        ReconcileIssue(
                            kind=ReconcileIssueKind.EXECUTOR_FAILED,
                            symbol=symbol,
                            position_id=pending_owner.record.position_id,
                            detail="托管订单/子单尚未清理且出现余仓，归属需核验；不另起无身份平仓通道",
                            fixed=False,
                        )
                    )
                    continue
                rev = await self._recent_reverse(symbol, exchange_positions, now)
                issues.append(rev or await self._unknown_position(symbol, exchange_positions))
        # 本轮没轮到的未完成意图（例如已经平到零）：只核对原 CID 和交易所净仓
        for symbol in [s for s in exits if s not in handled]:
            found = await self._emergency_exit(symbol, None)
            if found is not None:
                issues.append(found)

        # 3. 残留的保命止损
        live_ids = {t.record.position_id for t in live}
        owned = [
            t
            for t in self._registry.data.positions.values()
            if t.record.position_id in live_ids
            or t.exit_cleanup_pending
            or (t.server_protection is not None and t.server_protection.stage != "done")
        ]
        live_cids = {c for t in owned for c in lifeboat_cids(t.record.position_id)}
        live_cids.update(
            leg.client_order_id
            for t in owned
            if t.server_protection is not None
            for leg in t.server_protection.legs
            if leg.purpose == "lifeboat"
        )
        for stop in stops:
            if (
                not stop.client_order_id.startswith(STOP_PREFIX)
                or stop.client_order_id in live_cids
                or stop.symbol in reversed_
                or stop.symbol in exits
            ):
                continue
            issues.append(await self._clear_stale(stop, exchange_qty))

        # 4. 执行器：报错、消失、没人认领
        known_ids = {t.record.executor_id for t in owned if t.record.executor_id}
        # 执行器回来了 / 仓位没了的，清掉“消失”计数
        live_executor = {t.record.position_id: t.record.executor_id for t in live}
        running_now = {str(e.get("executor_id")) for e in running}
        for pid in list(self._gone_rounds):
            if live_executor.get(pid) is None or live_executor[pid] in running_now:
                self._gone_rounds.pop(pid, None)
        for t in live:
            if (
                t.record.position_id in external
                or t.record.position_id in settling
                or t.record.symbol in reversed_
            ):
                self._gone_rounds.pop(t.record.position_id, None)
                continue
            if t.record.executor_id and t.record.executor_id not in running_ids:
                rec = t.record
                if (
                    rec.status is PositionStatus.OPEN
                    and rec.symbol not in flattened
                    and exchange_qty.get(rec.symbol, 0.0) > 0
                ):
                    # RUNNING 搜索有状态过滤和分页；列表缺席不是终止证据。
                    # 只对缺席且仍持仓的执行器补一次逐项查询，不缓存跨轮状态。
                    executor_id = t.record.executor_id
                    unknown = "逐项查询未找到（上游 404/未命中不能证明执行器消失）"
                    try:
                        detail = await self._retry(partial(self._api.get_executor, executor_id))
                    except HbApiError:
                        detail = None
                        unknown = "逐项查询失败或响应无效，状态无法确认"
                    state = executor_query_state(detail, executor_id)
                    if state != "terminal":
                        self._gone_rounds.pop(rec.position_id, None)
                        if detail is not None and state == "unknown":
                            unknown = "逐项查询的标识或状态字段不一致，状态无法确认"
                        evidence = (
                            "逐项查询仍报告 active（不代表实时管理健康）"
                            if state == "active"
                            else f"{unknown}（unknown）"
                        )
                        issues.append(
                            ReconcileIssue(
                                kind=ReconcileIssueKind.EXECUTOR_FAILED,
                                symbol=rec.symbol,
                                position_id=rec.position_id,
                                detail=f"运行列表未返回执行器，但{evidence}；"
                                "已清零终止计数，不按执行器消失平仓，继续原保命止损处理",
                                fixed=False,
                            )
                        )
                        continue
                    # 查询期间其它路径可能已经平仓或更换执行器，不能继续沿用旧持仓。
                    current = self._registry.get(rec.position_id)
                    if (
                        current is None
                        or current.record.status is not PositionStatus.OPEN
                        or current.record.executor_id != executor_id
                    ):
                        self._gone_rounds.pop(rec.position_id, None)
                        continue
                    # 明确 TERMINATED/is_active=false 且币安仍有仓，保留原两轮失管保护。
                    rounds = self._gone_rounds.get(rec.position_id, 0) + 1
                    self._gone_rounds[rec.position_id] = rounds
                    if rounds < _GONE_ROUNDS_BEFORE_FLATTEN:
                        issues.append(
                            ReconcileIssue(
                                kind=ReconcileIssueKind.EXECUTOR_FAILED,
                                symbol=rec.symbol,
                                position_id=rec.position_id,
                                detail="hummingbot 里执行器已消失但币安仍有仓；"
                                "逐项确认 TERMINATED/is_active=false（第 1/2 轮），"
                                "保命止损在位，下一轮仍明确终止就按原保护平仓",
                                fixed=False,
                            )
                        )
                        continue
                    issue, was_flattened = await self._flatten(
                        rec.position_id,
                        "hummingbot 里执行器已消失但币安仍有仓；"
                        "连续 2 轮逐项确认 TERMINATED/is_active=false",
                        kind=ReconcileIssueKind.EXECUTOR_FAILED,
                    )
                    issues.append(issue)
                    if was_flattened:
                        self._gone_rounds.pop(rec.position_id, None)
                        flattened.add(rec.symbol)
                        continue
                else:
                    self._gone_rounds.pop(rec.position_id, None)
                issues.append(
                    ReconcileIssue(
                        kind=ReconcileIssueKind.EXECUTOR_FAILED,
                        symbol=t.record.symbol,
                        position_id=t.record.position_id,
                        detail="账本里还在持仓，但运行列表未返回执行器；列表缺席不等于已终止",
                        fixed=False,
                    )
                )
        for ex in running:
            pair = str(ex.get("trading_pair") or "")
            try:
                symbol = from_hb_pair(pair)
            except ValueError:
                continue
            eid = str(ex.get("executor_id"))
            if eid not in known_ids:
                # 我们已结束的仓的执行器还在跑：它不归任何在管持仓，不管开关都只停不平
                own = next(
                    (
                        t.record.position_id
                        for t in self._registry.data.positions.values()
                        if t.record.executor_id == eid and t.record.status is PositionStatus.CLOSED
                    ),
                    None,
                )
                issues.append(await self._unknown_executor(symbol, eid, now, own))
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

        if rec.symbol in self._registry.data.emergency_exits:
            found = await self._emergency_exit(rec.symbol, None)
            if found is not None:
                return found
        if not self._same_qty(qty, rec.quantity):
            return issue(f"{detail}，数量也对不上，未自动处理", False)
        tracked = self._registry.get(rec.position_id)
        if tracked is not None and tracked.server_protection is not None:
            return issue(
                f"{detail}，托管仓归属不一致，保留保护并等待核验，未自动平未知反向仓", False
            )
        if len(mine) != 1:
            return issue(f"{detail}，同币读到多笔持仓，未自动处理", False)
        unproven = await self._ownership_unproven(rec, mine[0])
        if unproven is not None:
            return issue(f"{detail}，不能证明是我们的反手仓（{unproven}），不下单，人看", False)
        trigger = _ExitTrigger("side_mismatch", mine[0], rec.position_id, f"{detail}（反手仓）")
        return await self._emergency_exit(rec.symbol, trigger) or issue(detail, False)

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
        tracked = self._registry.get(rec.position_id)
        if tracked is not None and tracked.server_protection is not None:
            return ReconcileIssue(
                kind=ReconcileIssueKind.SIDE_MISMATCH,
                symbol=symbol,
                position_id=rec.position_id,
                detail=f"{detail}；托管使用只减仓，不能凭等量断言归属，未自动平未知仓",
                fixed=False,
            )
        unproven = await self._ownership_unproven(rec, pos)
        if unproven is not None:
            return ReconcileIssue(
                kind=ReconcileIssueKind.SIDE_MISMATCH,
                symbol=symbol,
                position_id=rec.position_id,
                detail=f"{detail}；不能证明是我们的（{unproven}），不下单，人看",
                fixed=False,
            )
        return await self._emergency_exit(
            symbol, _ExitTrigger("reverse", pos, rec.position_id, detail)
        )

    async def _unknown_position(
        self, symbol: str, exchange_positions: list[ExchangePosition]
    ) -> ReconcileIssue:
        """币安有仓位但账本里没有。默认只告警；打开开关后直连币安只减仓平掉（走带 CID 的意图）。"""
        mine = [p for p in exchange_positions if p.symbol == symbol]
        qty = sum(p.quantity for p in mine)
        detail = f"币安有 {qty} 的仓位，但没有执行器在管"
        trigger = None
        if self._close_unknown and len(mine) == 1:
            trigger = _ExitTrigger("unknown", mine[0], None, detail)
        if trigger is not None or symbol in self._registry.data.emergency_exits:
            found = await self._emergency_exit(symbol, trigger)
            if found is not None:
                return found
        return ReconcileIssue(
            kind=ReconcileIssueKind.ORPHAN_POSITION, symbol=symbol, detail=detail, fixed=False
        )

    def _foreign_executors(self, running: list[dict[str, Any]]) -> dict[str, set[str]]:
        """运行列表里不归账本管的执行器（按币种）。"""
        owned = {
            t.record.executor_id
            for t in self._registry.data.positions.values()
            if t.record.executor_id
            and (
                t.record.status not in (PositionStatus.CLOSED, PositionStatus.FAILED)
                or t.exit_cleanup_pending
                or (t.server_protection is not None and t.server_protection.stage != "done")
            )
        }
        out: dict[str, set[str]] = {}
        for ex in running:
            eid = str(ex.get("executor_id"))
            try:
                symbol = from_hb_pair(str(ex.get("trading_pair") or ""))
            except ValueError:
                continue
            if eid not in owned:
                out.setdefault(symbol, set()).add(eid)
        return out

    async def _settle_foreign_stops(self, *, count_round: bool = False) -> None:
        """停过的执行器：逐项查到明确终态才删记录；查不到、查失败都保留（同币不另发直连）。

        ``count_round``（每轮对账一次）：停止结果未知的，没终止就记一轮；配置了
        ``executor_stop_retry_rounds`` 且到数，再发一次 keep_position=True 停止。"""
        stops = self._registry.data.foreign_executor_stops
        for eid in list(stops):
            try:
                detail = await self._retry(partial(self._api.get_executor, eid))
            except HbApiError:
                continue
            if executor_query_state(detail, eid) == "terminal":
                stops.pop(eid, None)
                self._registry.save()
                continue
            stop = stops[eid]
            if not count_round or stop.error is None:
                continue
            rounds = stop.unresolved_rounds + 1
            if self._stop_retry_rounds <= 0 or rounds < self._stop_retry_rounds:
                stops[eid] = stop.model_copy(update={"unresolved_rounds": rounds})
                self._registry.save()
                continue
            stops[eid] = stop.model_copy(update={"unresolved_rounds": 0})
            self._registry.save()
            try:
                await self._api.stop_executor(eid, keep_position=True)
            except HbApiError as e:
                if e.definite_rejection:
                    stops.pop(eid, None)
                else:
                    stops[eid] = stops[eid].model_copy(update={"error": repr(e)})
            else:
                stops[eid] = stops[eid].model_copy(update={"error": None})
            self._registry.save()

    async def _ownership_unproven(self, rec: PositionRecord, pos: ExchangePosition) -> str | None:
        """反手仓归属证据：从 ``rec`` 开仓前起，这个币的每一笔成交都来自认得出的我方订单，
        且我方成交的净量正好等于币安现在的净仓。证实返回 None；否则返回原因（不下单）。"""
        trades_read = getattr(self._exchange, "user_trades", None)
        if trades_read is None or getattr(self._exchange, "entry_order", None) is None:
            return "缺成交明细 / 订单查询能力"
        since = rec.opened_at - _EVIDENCE_SLACK
        try:
            trades = [t for t in await trades_read(rec.symbol, since) if t.time >= since]
            ours = await self._our_order_ids(rec.symbol, since)
        except (ExchangeError, HbApiError) as e:
            return f"查成交或订单失败：{e!r}"
        if ours is None:
            return "有我方订单的身份查不到"
        if len(trades) >= _TRADES_PAGE:
            return "成交明细可能不全"
        foreign = [t for t in trades if t.order_id not in ours]
        if foreign:
            return f"{len(foreign)} 笔成交不是认得出的我方订单（可能是手动单）"
        net = sum(t.qty if t.side == "buy" else -t.qty for t in trades)
        want = _signed(pos.side, pos.quantity)
        if not math.isclose(net, want, rel_tol=1e-6, abs_tol=1e-12):
            return f"我方成交净量 {net:g} 解释不了币安净仓 {want:g}"
        return None

    async def _our_order_ids(self, symbol: str, since: datetime) -> set[str] | None:
        """这段时间里这个币上我方订单的交易所订单号：执行器订单、保命止损、直连平仓、托管腿、
        对账员平仓单。执行器的订单清单拿不到就返回 None（认不全就不能当证据）。"""
        read = self._exchange.entry_order  # type: ignore[attr-defined]
        out: set[str] = set()

        async def by_cid(cid: str) -> None:
            order = await read(symbol, cid)
            if order is not None:
                out.add(str(order.order_id))

        for t in self._registry.data.positions.values():
            rec = t.record
            if rec.symbol != symbol or (rec.closed_at is not None and rec.closed_at < since):
                continue
            if rec.executor_id:
                detail = await self._retry(partial(self._api.get_executor, rec.executor_id))
                info = detail.get("custom_info") if isinstance(detail, dict) else None
                ids = info.get("order_ids") if isinstance(info, dict) else None
                if not isinstance(ids, list) or not all(isinstance(c, str) and c for c in ids):
                    return None
                for cid in ids:
                    await by_cid(cid)
            if rec.lifeboat is not None and rec.lifeboat.exchange_order_id:
                out.add(rec.lifeboat.exchange_order_id)
            for cid in lifeboat_cids(rec.position_id):
                await by_cid(cid)
            for g in range(1, t.direct_close_generation + 1):
                await by_cid(f"dc-{rec.position_id}-{g}")
            if t.direct_close_order_id:
                out.add(t.direct_close_order_id)
            if t.server_protection is not None:
                out.update(
                    leg.actual_order_id for leg in t.server_protection.legs if leg.actual_order_id
                )
        plan = self._registry.data.emergency_exits.get(symbol)
        if plan is not None:
            for g in range(1, plan.generation + 1):
                await by_cid(f"dc-{plan.exit_id}-{g}")
            if plan.order_id:
                out.add(plan.order_id)
        return out

    async def _apply_resolutions(self, now: datetime) -> None:
        """执行 ``jiaoyi resolve-exit`` 写下的人工结束请求：同币锁里核对 CID 和净仓再删意图。"""
        root = resolutions_dir(self._registry.path)
        for path, req in pending_resolutions(root):
            if req is None:
                finish_resolution(root, path, None, False, "请求文件格式不对，未执行")
                continue
            async with self._registry.symbol_lock(req.symbol):
                applied, text, removed = await self._resolve_one(req)
            finish_resolution(root, path, req, applied, text, removed)
            if self._rec is not None:
                self._rec.record(
                    now,
                    Outcome.ACTION if applied else Outcome.ERROR,
                    f"人工结束平仓意图{'已执行' if applied else '未执行'}：{text}",
                    symbol=req.symbol,
                    input=req.model_dump(mode="json"),
                    output={"applied": applied, "removed": removed},
                )
            await self._send(
                now,
                AlertLevel.WARN,
                "人工结束对账员平仓意图" + ("" if applied else "（未执行）"),
                f"{req.symbol} {text}",
                req.symbol,
                f"exit_resolution:{req.id}",
                "reconcile",
            )

    async def _resolve_one(self, req: ResolutionRequest) -> tuple[bool, str, dict[str, Any] | None]:
        exits = self._registry.data.emergency_exits
        plan = exits.get(req.symbol)
        if plan is None:
            return False, f"{req.symbol} 已没有未完成的平仓意图", None
        if plan.client_order_id != req.client_order_id:
            return (
                False,
                f"CID 对不上：账本里是 {plan.client_order_id}，请求的是 {req.client_order_id}",
                None,
            )
        if not req.force:
            read = getattr(self._exchange, "positions_evidence", None)
            try:
                if read is None:
                    raise ExchangeError("缺严格持仓查询能力")
                left = [p for p in await read() if p.symbol == req.symbol]
            except ExchangeError as e:
                return False, f"读不到严格持仓（{e!r}），没加 --force 不结束", None
            if left:
                qty = sum(_signed(p.side, p.quantity) for p in left)
                return False, f"币安仍有净仓 {qty:g}，没加 --force 不结束", None
        exits.pop(req.symbol, None)
        self._registry.save()
        removed = plan.model_dump(mode="json")
        why = f"（{req.reason}）" if req.reason else ""
        force = "，--force" if req.force else ""
        return True, f"已结束意图 {plan.exit_id}（{plan.client_order_id}{force}）{why}", removed

    def _exit_blocker(
        self, symbol: str, source: ExitSource, position_id: str | None, side: Side
    ) -> str | None:
        """提交前在同币锁内重新核实归属；返回不能下单的原因。"""
        done = (PositionStatus.CLOSED, PositionStatus.FAILED)
        for t in self._registry.data.positions.values():
            rec = t.record
            if rec.symbol != symbol:
                continue
            if source == "side_mismatch" and rec.position_id == position_id:
                if (
                    rec.status not in (PositionStatus.OPEN, PositionStatus.CLOSING)
                    or t.server_protection is not None
                    or rec.side is side
                ):
                    return f"账本 {position_id} 不再是开着的反向仓（{rec.status.value}），不平"
                continue
            if (
                rec.status not in done
                or t.exit_cleanup_pending
                or (t.server_protection is not None and t.server_protection.stage != "done")
            ):
                return f"账本里 {rec.position_id}（{rec.status.value}）仍管这个币，归属未明，不下单"
        if source == "side_mismatch" and self._registry.get(position_id or "") is None:
            return f"账本里已没有 {position_id}，不按反手仓平"
        foreign = set(self._foreign_running.get(symbol, set()))
        foreign.update(
            s.executor_id
            for s in self._registry.data.foreign_executor_stops.values()
            if s.symbol == symbol
        )
        if foreign:
            return (
                f"同币有不在账本里的执行器 {', '.join(sorted(foreign))} 在跑或停止未确认，"
                "先隔离，不另发直连平仓"
            )
        return None

    async def _emergency_exit(
        self, symbol: str, trigger: _ExitTrigger | None
    ) -> ReconcileIssue | None:
        """无主仓 / 反手仓唯一的直连退出入口：同币串行（和开仓共用锁），先落盘 CID 再发，
        结果未知只查原 CID，交易所复查净仓为零才算平掉。``trigger`` 为 None 时只核对已有意图。"""
        async with self._registry.symbol_lock(symbol):
            return await self._emergency_exit_locked(symbol, trigger)

    async def _emergency_exit_locked(
        self, symbol: str, trigger: _ExitTrigger | None
    ) -> ReconcileIssue | None:
        exits = self._registry.data.emergency_exits
        plan = exits.get(symbol)
        source: ExitSource
        if plan is not None:
            source, pid = plan.source, plan.position_id
            head = trigger.detail if trigger is not None else f"对账员平仓意图 {plan.exit_id}"
        elif trigger is not None:
            source, pid, head = trigger.source, trigger.position_id, trigger.detail
        else:
            return None
        kind = (
            ReconcileIssueKind.ORPHAN_POSITION
            if source == "unknown"
            else ReconcileIssueKind.SIDE_MISMATCH
        )

        def issue(text: str, fixed: bool) -> ReconcileIssue:
            return ReconcileIssue(
                kind=kind, symbol=symbol, position_id=pid, detail=f"{head}；{text}", fixed=fixed
            )

        if plan is not None and plan.pending:
            order = await self._recover_exit(plan)
            if order is None or order.is_terminal is not True:
                status = order.status if order is not None else "查不到或结果未知"
                return issue(
                    f"平仓单 {plan.client_order_id} {status}，未确认终态；只查原 CID，不重发",
                    False,
                )
            plan = exits[symbol]
        read = getattr(self._exchange, "positions_evidence", None)
        if read is None:
            return issue("缺严格持仓查询能力，不下单", False)
        try:
            fresh = [p for p in await read() if p.symbol == symbol]
        except ExchangeError as e:
            return issue(f"下单前复查持仓失败，不下单：{e!r}", False)
        if not fresh:
            if plan is None:
                return issue("复查时币安已无仓位，未下单", True)
            exits.pop(symbol, None)
            self._registry.save()
            last = f"{plan.client_order_id} {plan.order_status}" if plan.generation else "无"
            return issue(f"币安复查已无仓位，已平（最后一张平仓单：{last}）", True)
        if len(fresh) != 1:
            return issue("同币读到多笔持仓，不下单", False)
        pos = fresh[0]
        if plan is None:
            assert trigger is not None
            seen = trigger.position
            if pos.side is not seen.side or pos.quantity > seen.quantity * (1 + _EPS):
                return issue(
                    f"复查是 {pos.side.value} {pos.quantity}，和触发时不一致，不下单", False
                )
            cap = pos.quantity
        else:
            cap = plan.initial_quantity - plan.closed_quantity
            if pos.side is not plan.side:
                return issue("复查方向和原意图相反（可能有人新开），未下单，人看", False)
            if pos.quantity > cap * (1 + _EPS):
                return issue(
                    f"余仓 {pos.quantity} 超过本意图剩余上限 {cap:g}（可能有人加仓），未下单，人看",
                    False,
                )
        blocker = self._exit_blocker(symbol, source, pid, pos.side)
        if blocker is not None:
            return issue(blocker, False)
        if source == "unknown" and not self._close_unknown:
            return issue("开关已关，不再下新的平仓单", False)
        place = getattr(self._exchange, "place_close", None)
        if place is None:
            return issue("交易所客户端缺带 CID 的平仓能力，不降级成无身份市价单", False)
        if plan is None:
            plan = EmergencyExit(
                exit_id=f"em-{uuid.uuid4().hex[:12]}",
                symbol=symbol,
                source=source,
                position_id=pid,
                side=pos.side,
                initial_quantity=pos.quantity,
                created_at=datetime.now(UTC),
            )
        generation = plan.generation + 1
        cid = f"dc-{plan.exit_id}-{generation}"
        quantity = min(pos.quantity, cap)
        plan = plan.model_copy(
            update={
                "generation": generation,
                "client_order_id": cid,
                "quantity": quantity,
                "order_id": None,
                "order_status": "SUBMIT_PENDING",
                "pending": True,
            }
        )
        exits[symbol] = plan
        self._registry.save()  # 先落盘再发：超时 / 重启后只能按这个 CID 查
        close_side: Literal["buy", "sell"] = "sell" if plan.side is Side.LONG else "buy"
        try:
            order = await place(symbol, close_side, quantity, cid)
        except IdentifiedCloseRejected as e:
            exits[symbol] = plan.model_copy(update={"pending": False, "order_status": "REJECTED"})
            self._registry.save()
            return issue(f"平仓单 {cid} 被币安明确拒绝：{e!r}；下轮按真实余仓再判断", False)
        except IdentifiedCloseUnknown as e:
            if e.observed is None:
                return issue(f"平仓单 {cid} 结果未知（{e!r}）；只查原 CID，不重发", False)
            order = e.observed
        except Exception as e:
            return issue(f"平仓单 {cid} 结果未知（{e!r}）；只查原 CID，不重发", False)
        if not isinstance(order, IdentifiedCloseOrder) or not self._accept_exit(plan, order):
            return issue(f"平仓单 {cid} 的回报和请求对不上，按结果未知处理，不重发", False)
        if order.is_terminal is not True:
            return issue(f"平仓单 {cid} 已受理（{order.status}），还没终态；下轮只查原 CID", False)
        try:
            rest = [p for p in await read() if p.symbol == symbol]
        except ExchangeError as e:
            return issue(f"平仓单 {cid} 已终态，但复查持仓失败，下轮再核：{e!r}", False)
        if rest:
            left = sum(p.quantity for p in rest)
            return issue(
                f"平仓单 {cid} 已终态（{order.status}，成交 {order.filled_quantity}），"
                f"币安仍有 {left}；下轮按真实余仓处理",
                False,
            )
        exits.pop(symbol, None)
        self._registry.save()
        return issue(f"已直连币安只减仓平掉（{cid} {order.status}），复查已无仓位", True)

    def _accept_exit(self, plan: EmergencyExit, order: IdentifiedCloseOrder) -> bool:
        """回报必须就是本代请求；终态时把成交计入累计，之后不再查这代。"""
        if (
            plan.quantity is None
            or order.client_order_id != plan.client_order_id
            or order.symbol != plan.symbol
            or order.close_side != ("sell" if plan.side is Side.LONG else "buy")
            or order.position_side != "BOTH"
            or not order.reduce_only
            or (plan.order_id is not None and order.order_id != plan.order_id)
            or not all(
                math.isfinite(v)
                for v in (order.quantity, order.filled_quantity, order.remaining_quantity)
            )
            or order.filled_quantity < 0
            or order.remaining_quantity < 0
            or not math.isclose(
                order.quantity, order.filled_quantity + order.remaining_quantity, rel_tol=1e-10
            )
            or not math.isclose(order.quantity, plan.quantity, rel_tol=1e-10)
        ):
            return False
        terminal = order.is_terminal is True
        self._registry.data.emergency_exits[plan.symbol] = plan.model_copy(
            update={
                "order_id": order.order_id,
                "order_status": order.status,
                "pending": not terminal,
                "closed_quantity": plan.closed_quantity
                + (order.filled_quantity if terminal else 0.0),
            }
        )
        self._registry.save()
        return True

    async def _recover_exit(self, plan: EmergencyExit) -> IdentifiedCloseOrder | None:
        """只读查原 CID；查不到不等于没下过，不能据此重发。"""
        read = getattr(self._exchange, "close_order", None)
        if read is None or plan.client_order_id is None:
            return None
        try:
            order = await read(plan.symbol, plan.client_order_id)
        except Exception:
            log.warning("对账员平仓单 %s 暂不可查", plan.client_order_id, exc_info=True)
            return None
        if isinstance(order, IdentifiedCloseOrder) and self._accept_exit(plan, order):
            return order
        return None

    async def _unknown_executor(
        self, symbol: str, executor_id: str, now: datetime, own_position_id: str | None = None
    ) -> ReconcileIssue:
        """不归在管持仓的执行器：只停不平（keep_position=True），免得它和直连平仓同时退出；
        余仓下一轮按无主仓走带 CID 的意图。不在账本里的看开关；已结束仓的（``own_position_id``）
        不看开关，它不下单。停止请求先落盘，结果未知不重发（除非配置了重试轮数）。"""
        if own_position_id is not None:
            detail = f"已结束的 {own_position_id} 的执行器 {executor_id} 还在运行列表里"
            kind = ReconcileIssueKind.EXECUTOR_FAILED
            how = ""
        else:
            detail = f"hummingbot 里有运行中的执行器 {executor_id} 不在账本里"
            kind = ReconcileIssueKind.ORPHAN_POSITION
            how = "开关已打开但"

        def issue(text: str, fixed: bool) -> ReconcileIssue:
            return ReconcileIssue(
                kind=kind, symbol=symbol, position_id=own_position_id, detail=text, fixed=fixed
            )

        if not self._close_unknown and own_position_id is None:
            return issue(detail, False)
        stops = self._registry.data.foreign_executor_stops
        pending = stops.get(executor_id)
        if pending is not None:
            why = f"，请求结果未知（{pending.error}）" if pending.error else ""
            return issue(f"{detail}；已请求停止（保留仓位）{why}，等它明确终止，不重发", False)
        stops[executor_id] = ForeignExecutorStop(
            executor_id=executor_id,
            symbol=symbol,
            requested_at=now,
            position_id=own_position_id,
        )
        self._registry.save()
        try:
            await self._api.stop_executor(executor_id, keep_position=True)
        except HbApiError as e:
            if e.definite_rejection:
                stops.pop(executor_id, None)
                self._registry.save()
                return issue(f"{detail}；{how}停止被明确拒绝：{e!r}", False)
            stops[executor_id] = stops[executor_id].model_copy(update={"error": repr(e)})
            self._registry.save()
            return issue(f"{detail}；{how}停止结果未知：{e!r}，只查不重发", False)
        await self._settle_foreign_stops()
        if executor_id in stops:
            return issue(f"{detail}；已请求停止（保留仓位），还没确认终止", False)
        return issue(f"{detail}；已停止（保留仓位），余仓按无主仓走带 CID 的平仓", True)

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
        source = "最近一笔只减仓成交"
        if fill is None or fill.price is None:
            fill = CloseFill(await self._exchange.mark_price(rec.symbol), None, estimated=True)
            source = "标记价（估算）" if fill.price is not None else "未取到价格，盈亏未记"
        if not await self._adapter.settle_external_close(pid, fill):
            return issue("真实余仓/开仓残单/在途平仓尚未同时核实，保留保护继续核对", False)
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
            # 两个兜底入口共享适配器持久 pending，不能在其已超时后再发第二张。
            try:
                closed = await self._adapter.request_direct_close(
                    position_id, CloseReason.RECONCILER
                )
            except Exception as direct:
                return issue(f"{why}，平仓尚未确认：{via_hb!r}；{direct!r}", False), False
            if not closed:
                return issue(f"{why}，平仓尚未确认，原保护保留", False), False
        current = self._registry.get(position_id)
        if current is None or current.record.status is not PositionStatus.CLOSED:
            return issue(f"{why}，平仓尚未确认，原保护保留", False), False
        self._lb_failures.pop(position_id, None)
        return issue(f"{why}，已平仓：币安已确认无剩余仓位", True), True

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

    async def _drop_extra(
        self, rec: PositionRecord, extra: ExchangeStop, keep: ExchangeStop
    ) -> ReconcileIssue:
        """同一笔仓位挂着两张保命止损（重挂时撤旧失败）：撤掉账本没记的那张。"""
        try:
            await self._exchange.cancel_stop(extra.symbol, extra.order_id)
        except ExchangeError as e:
            detail, fixed = f"多出的保命止损 {extra.client_order_id} 撤销失败：{e!r}", False
        else:
            detail, fixed = (
                f"同一笔多挂了一张保命止损 {extra.client_order_id}（{extra.stop_price:g}），"
                f"已撤，保留 {keep.client_order_id}（{keep.stop_price:g}）",
                True,
            )
        return ReconcileIssue(
            kind=ReconcileIssueKind.STALE_LIFEBOAT,
            symbol=rec.symbol,
            position_id=rec.position_id,
            detail=detail,
            fixed=fixed,
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
            "lifeboat_missing"
            if issue.kind is ReconcileIssueKind.LIFEBOAT_MISSING
            else "reconcile",
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
        kind: str,
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
                    kind=kind,
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
