"""拍板人：候选信号 → 开仓问题 → 决策模型 → 原始概率。

默认只问一道「做不做」（``questions="one"``，Aaron 10-24），问的时长跟着最长持仓档走；快照另加
技术面、成交流、衍生品、24 小时行情（``gate.context``），这些数据在信号进来时就开始准备，和排队
同时进行，REST 最多等 1 秒。``questions="six"`` 是旧的 6 道题（回放对照用）。

拍板人不决定数量、不下单，“过不过门槛”由风控员按配置判断。这里只管：
- 没有有效作战卡（缺失、过期、卡里没这个标的）→ 不问模型，直接 ``ok=False``。
- 不同的币同时问，最多 ``max_concurrent`` 个（面板「拍板人同时最多判几个」）。
- 同一个币一次只问一个：上一次还没判完，新信号排队（记一行「排队等拍板人」），排队的只留
  最新一条（更早排队的让位作废）；等过 ``max_wait_s``（信号有效期减拍板超时：再问也来不及，
  风控员会按年龄拒，白花一次钱）也作废。
  以前是直接作废（10-24 一天作废的信号里大半是这个）。
- 每次判断写一条决策日志（团队页的拍板人卡片读它）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Callable
from datetime import datetime

from jiaoyi.contracts import (
    BattleCard,
    CandidateSignal,
    EntryQuestion,
    JevRequest,
    JevResponse,
    Outcome,
    Side,
    SignalKind,
)
from jiaoyi.core.recorder import RoleRecorder
from jiaoyi.gate.context import EntryContextSource, EntryExtras
from jiaoyi.gate.questions import (
    DEFAULT_HOLD_MIN,
    entry_atr_bps,
    entry_levels_bps,
    entry_questions,
    entry_snapshot,
    entry_snapshot_one,
    jev_log_fields,
)
from jiaoyi.interfaces import DecisionModel

log = logging.getLogger(__name__)


def _fail(request_id: str, msg: str) -> JevResponse:
    return JevResponse(
        request_id=request_id, ok=False, error=msg, model_version="none", latency_ms=0.0
    )


def summarize(resp: JevResponse) -> str:
    """一句话概括回答，日志和团队页用。必须先确认 ``ok``。"""
    if not resp.ok:
        return f"不可用：{resp.error}"
    parts = []
    for q, cn in (  # 一道题问法只有「做」「止损」「仓位」，没回答的题跳过
        (EntryQuestion.GENUINE, "真"),
        (EntryQuestion.TAKE, "做"),
        (EntryQuestion.TOXICITY, "毒性"),
        (EntryQuestion.STOP_WIDTH, "止损"),
        (EntryQuestion.SIZE, "仓位"),
    ):
        a = resp.answer(q.value)
        if a is not None:
            top, p = a.top()
            parts.append(f"{cn} {top} {p:.2f}")
    return " / ".join(parts)


class JevEntryGate:
    """``EntryGate`` 的实现。"""

    def __init__(
        self,
        model: DecisionModel,
        recorder: RoleRecorder | None = None,
        *,
        hold_min: int = DEFAULT_HOLD_MIN,
        max_concurrent: int = 4,
        max_wait_s: float = 10.0,
        questions: str = "one",
        context: EntryContextSource | None = None,
        take_floor: Callable[[SignalKind], float | None] | None = None,
    ) -> None:
        """``take_floor``: the per-signal-kind take floor in force, None = the global one; a
        per-kind floor is written into the record so the panel judges it as it was.

        ``hold_min``：单笔最长持仓（分钟），写进快照，问题按这个时长问。

        ``max_concurrent``：同时最多问几个（不同的币）；``max_wait_s``：同币排队最多等几秒
        （信号有效期 − 拍板超时，从信号触发算起）。

        ``questions``：``"one"`` 一道题 / ``"six"`` 旧 6 道题；``context``：一道题问法的额外快照
        数据（None = 只用信号自带的特征）。"""
        self._questions = questions
        self._context = context
        self._take_floor = take_floor
        self._model = model
        self._rec = recorder
        self._hold_min = hold_min
        self._max_wait_s = max_wait_s
        self._slots = asyncio.Semaphore(max_concurrent)
        self._locks: dict[str, asyncio.Lock] = {}  # 每个币一把：同币一次只问一个
        self._queued: dict[str, asyncio.Event] = {}  # 同币排队的那一条；set() = 被更新的让掉

    def reconfigure(self, *, max_wait_s: float, questions: str) -> None:
        """配置热生效：排队最长等多久、问法。同时问几个（信号量）启动时定，换不了。"""
        self._max_wait_s = max_wait_s
        self._questions = questions

    async def evaluate(
        self, signal: CandidateSignal, card: BattleCard, now: datetime
    ) -> JevResponse:
        rid = f"jev-{signal.signal_id}-{uuid.uuid4().hex[:6]}"
        sym = signal.symbol
        if card is None or not card.is_valid(now) or card.for_symbol(sym) is None:
            resp = _fail(rid, "没有有效作战卡，不开新仓")
            self._log(now, signal, card, resp, None)
            return resp
        # 额外快照数据现在就开始准备（REST 并发补拉），和排队同时进行，不让拍板变慢
        extras: asyncio.Future[EntryExtras] | None = None
        if self._questions == "one" and self._context is not None:
            extras = asyncio.ensure_future(
                self._context(
                    sym,
                    self._hold_min,
                    entry_atr_bps(signal),
                    entry_levels_bps(signal, card),
                    signal.features.values,
                )
            )
        lock = self._locks.setdefault(sym, asyncio.Lock())
        if lock.locked():
            wait_s = self._max_wait_s - (now - signal.ts).total_seconds()
            why = await self._queue(sym, lock, signal, card, now, wait_s)
            if why is not None:
                if extras is not None:
                    extras.cancel()
                resp = _fail(rid, why)
                self._log(now, signal, card, resp, None)
                return resp
        else:
            await lock.acquire()
        try:
            return await self._ask(rid, signal, card, now, extras)
        finally:
            lock.release()

    async def _queue(
        self,
        sym: str,
        lock: asyncio.Lock,
        signal: CandidateSignal,
        card: BattleCard,
        now: datetime,
        wait_s: float,
    ) -> str | None:
        """同币上一次还没判完：排队等。拿到锁返回 None（调用方负责放锁），否则返回作废原因。"""
        old = self._queued.pop(sym, None)
        if old is not None:
            old.set()  # 只留最新一条：更早排队的让位
        me = self._queued[sym] = asyncio.Event()
        if self._rec is not None:
            side = "多" if signal.side_hint == Side.LONG else "空"
            self._rec.record(
                now,
                Outcome.INFO,
                f"{sym} {side}：排队等拍板人（同一个币上一个信号还在判）",
                symbol=sym,
                signal_id=signal.signal_id,
                card_id=card.card_id,
            )
        acquire = asyncio.ensure_future(lock.acquire())
        replaced = asyncio.ensure_future(me.wait())
        try:
            await asyncio.wait({acquire, replaced}, timeout=max(wait_s, 0.0),
                               return_when=asyncio.FIRST_COMPLETED)  # fmt: skip
        finally:
            replaced.cancel()
            if self._queued.get(sym) is me:
                del self._queued[sym]
        if acquire.done() and not acquire.cancelled():
            return None
        acquire.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await acquire
        if acquire.done() and not acquire.cancelled():
            return None  # 撤的那一刻刚好拿到锁：照常问
        if me.is_set():
            return "同一个币来了更新的信号，这条让位作废"
        return f"排队太久已作废（等了 {self._max_wait_s:g} 秒，再问就判不完在信号有效期内）"

    async def _ask(
        self,
        rid: str,
        signal: CandidateSignal,
        card: BattleCard,
        now: datetime,
        extras: asyncio.Future[EntryExtras] | None = None,
    ) -> JevResponse:
        sym = signal.symbol
        ex: EntryExtras | None = None
        if self._questions == "one":
            if extras is not None:
                try:
                    ex = await extras
                except asyncio.CancelledError:
                    raise
                except Exception:  # 额外数据只是参考：出错就按没有，照常问
                    log.exception("%s 拍板人额外快照数据出错，这次不带", sym)
            snapshot = entry_snapshot_one(signal, card, self._hold_min, ex)
        else:
            snapshot = entry_snapshot(signal, card, self._hold_min)
        request = JevRequest(
            request_id=rid,
            kind="entry",
            symbol=sym,
            ts=now,
            signal_id=signal.signal_id,
            snapshot=snapshot,
            questions=entry_questions(self._questions, self._hold_min),
        )
        async with self._slots:
            resp = await self._model.ask(request)
        self._log(now, signal, card, resp, request, ex)
        return resp

    def _log(
        self,
        now: datetime,
        signal: CandidateSignal,
        card: BattleCard | None,
        resp: JevResponse,
        request: JevRequest | None,
        extras: EntryExtras | None = None,
    ) -> None:
        if self._rec is None:
            return
        side = "多" if signal.side_hint == Side.LONG else "空"
        # 拍板只记概率；放不放行由风控员按当前生效门槛判定（同一份口径），这里不自己标 pass/reject。
        if not resp.ok:
            outcome = Outcome.ERROR if request is not None else Outcome.REJECT
            tail = ""
        else:
            outcome = Outcome.INFO
            tail = "，门槛由风控员判定"
        inp, out = jev_log_fields(request, resp, extras.log() if extras is not None else None)
        inp["signal_kind"] = signal.kind.value
        floor = self._take_floor(signal.kind) if self._take_floor is not None else None
        if floor is not None:
            inp["take_floor_kind"] = signal.kind.value
            inp["take_floor"] = floor
        self._rec.record(
            now,
            outcome,
            f"{signal.symbol} {side}：{summarize(resp)}（{resp.latency_ms:.0f}ms{tail}）",
            symbol=signal.symbol,
            signal_id=signal.signal_id,
            card_id=card.card_id if card is not None else None,
            input=inp,
            output=out,
            latency_ms=resp.latency_ms,
            cost_usd=resp.cost_usd,
            model_version=resp.model_version,
        )
