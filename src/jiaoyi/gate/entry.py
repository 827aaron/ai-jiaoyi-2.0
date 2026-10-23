"""拍板人：候选信号 → 开仓 6 问 → 决策模型 → 原始概率。

拍板人不决定数量、不下单，“过不过门槛”由保镖按配置判断。这里只管：
- 没有有效作战卡（缺失、过期、卡里没这个标的）→ 不问模型，直接 ``ok=False``。
- 同一个标的上一次还没判断完，新信号直接作废（同币同秒的重复触发，属于去重，不算丢）；
  不同的币互不等待，可以同时问。
- 每次判断写一条决策日志（团队页的拍板人卡片读它）。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from jiaoyi.contracts import (
    BattleCard,
    CandidateSignal,
    EntryQuestion,
    JevRequest,
    JevResponse,
    Outcome,
    Side,
)
from jiaoyi.core.recorder import RoleRecorder
from jiaoyi.gate.questions import (
    DEFAULT_HOLD_MIN,
    entry_questions,
    entry_snapshot,
    jev_log_fields,
)
from jiaoyi.interfaces import DecisionModel


def _fail(request_id: str, msg: str) -> JevResponse:
    return JevResponse(
        request_id=request_id, ok=False, error=msg, model_version="none", latency_ms=0.0
    )


def summarize(resp: JevResponse) -> str:
    """一句话概括回答，日志和团队页用。必须先确认 ``ok``。"""
    if not resp.ok:
        return f"不可用：{resp.error}"
    parts = []
    for q, cn in (
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
    ) -> None:
        """``hold_min``：单笔最长持仓（分钟），写进快照，问题按这个时长问。"""
        self._model = model
        self._rec = recorder
        self._hold_min = hold_min
        self._busy: set[str] = set()  # 正在问的币（只按币互斥）

    async def evaluate(
        self, signal: CandidateSignal, card: BattleCard, now: datetime
    ) -> JevResponse:
        rid = f"jev-{signal.signal_id}-{uuid.uuid4().hex[:6]}"
        sym = signal.symbol
        if card is None or not card.is_valid(now) or card.for_symbol(sym) is None:
            resp = _fail(rid, "没有有效作战卡，不开新仓")
            self._log(now, signal, card, resp, None)
            return resp
        if sym in self._busy:
            resp = _fail(rid, "上一次判断还没完成，本次信号作废")
            self._log(now, signal, card, resp, None)
            return resp

        request = JevRequest(
            request_id=rid,
            kind="entry",
            symbol=sym,
            ts=now,
            signal_id=signal.signal_id,
            snapshot=entry_snapshot(signal, card, self._hold_min),
            questions=entry_questions(),
        )
        self._busy.add(sym)
        try:
            resp = await self._model.ask(request)
        finally:
            self._busy.discard(sym)
        self._log(now, signal, card, resp, request)
        return resp

    def _log(
        self,
        now: datetime,
        signal: CandidateSignal,
        card: BattleCard | None,
        resp: JevResponse,
        request: JevRequest | None,
    ) -> None:
        if self._rec is None:
            return
        side = "多" if signal.side_hint == Side.LONG else "空"
        # 拍板只记概率；放不放行由保镖按当前生效门槛判定（同一份口径），这里不自己标 pass/reject。
        if not resp.ok:
            outcome = Outcome.ERROR if request is not None else Outcome.REJECT
            tail = ""
        else:
            outcome = Outcome.INFO
            tail = "，门槛由保镖判定"
        inp, out = jev_log_fields(request, resp)
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
