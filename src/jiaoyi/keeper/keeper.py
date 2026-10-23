"""持仓管家：每个持仓每 15–30 秒（巡逻兵报警时立刻）问一次决策模型 3 个问题，判断拿住还是全平。

规则（设计文档 §五）：
- 只能让仓位更安全：v1 只有 hold / close（全平），不加仓、不放宽止损、不部分减仓。
- 决策模型失败、没有行情 → hold（维持现状），并且连续计数清零，不拿旧结论凑次数。
- 平仓要概率明显过线（``keeper_close_p_strong``），或者过线（``keeper_close_p``）且连续
  ``keeper_confirmations`` 次一致，防止反复横跳。
- 代码规则加一票：只有新作战卡的方向和持仓相反时，平仓概率至少按过线算（仍需连续确认）。
  新卡改成只看不做、或卡里没有这个币：不投票，持仓交给决策模型 / 止损 / 最长持仓时间处理
  （不开新仓由拍板人和保镖按卡管）。例外：被封的币（币安下架公告等，``blocked``）照样投票，
  要下架的币留仓更危险。规则只会推向更安全，不会把模型的“平”改成“拿”。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from datetime import datetime

from jiaoyi.contracts import (
    BattleCard,
    Bias,
    FastLoopConfig,
    FeatureSnapshot,
    HoldQuestion,
    JevRequest,
    JevResponse,
    KeeperVerdict,
    Outcome,
    PositionRecord,
)
from jiaoyi.core.recorder import RoleRecorder
from jiaoyi.gate.questions import hold_questions, hold_snapshot, jev_log_fields
from jiaoyi.interfaces import DecisionModel


class JevKeeper:
    """``Keeper`` 的实现。行情和作战卡通过回调拿，免得管家依赖具体模块。"""

    def __init__(
        self,
        model: DecisionModel,
        cfg: FastLoopConfig,
        features: Callable[[str], FeatureSnapshot | None],
        card: Callable[[datetime], BattleCard | None],
        recorder: RoleRecorder | None = None,
        blocked: Callable[[], Mapping[str, str]] | None = None,
    ) -> None:
        """``blocked``：被封禁新开仓的币 → 原因（军师团 ``blocked_symbols``），这些币的持仓要平。"""
        self._model = model
        self._blocked = blocked or dict
        self._cfg = cfg
        self._features = features
        self._card = card
        self._rec = recorder
        self._streak: dict[str, int] = {}  # 每个持仓连续“该平”的次数

    def forget(self, position_id: str) -> None:
        """持仓平掉后调用，清掉它的计数。"""
        self._streak.pop(position_id, None)

    async def review(self, position: PositionRecord, now: datetime) -> KeeperVerdict:
        pid = position.position_id
        f = self._features(position.symbol)
        if f is None:
            self._streak.pop(pid, None)
            return self._verdict(
                position, now, "hold", 0.0, 1, "没有行情数据，维持现状", None, None
            )
        card = self._card(now)
        request = JevRequest(
            request_id=f"hold-{pid}-{uuid.uuid4().hex[:6]}",
            kind="hold",
            symbol=position.symbol,
            ts=now,
            position_id=pid,
            snapshot=hold_snapshot(position, f, card, now),
            questions=hold_questions(),
        )
        resp = await self._model.ask(request)
        if not resp.ok:
            self._streak.pop(pid, None)
            return self._verdict(
                position,
                now,
                "hold",
                0.0,
                1,
                f"决策模型不可用，维持现状：{resp.error}",
                request,
                resp,
            )
        ans = resp.answer(HoldQuestion.ACTION.value)
        if ans is None:
            self._streak.pop(pid, None)
            return self._verdict(
                position, now, "hold", 0.0, 1, "决策模型没回答 action，维持现状", request, resp
            )

        p_close = ans.probs.get("close", 0.0)
        why = [f"平仓概率 {p_close:.2f}"]
        why_blocked = self._blocked().get(position.symbol)
        rule = (
            f"已禁止新开仓（{why_blocked}）"
            if why_blocked is not None
            else self._card_rule(position, card, now)
        )
        if rule is not None:
            p_close = max(p_close, self._cfg.keeper_close_p)
            why.append(rule)

        if p_close >= self._cfg.keeper_close_p:
            n = self._streak.get(pid, 0) + 1
            self._streak[pid] = n
            if p_close >= self._cfg.keeper_close_p_strong or n >= self._cfg.keeper_confirmations:
                return self._verdict(
                    position, now, "close", p_close, n, "，".join(why), request, resp
                )
            why.append(f"第 {n} 次，等确认")
            return self._verdict(position, now, "hold", p_close, n, "，".join(why), request, resp)
        self._streak.pop(pid, None)
        return self._verdict(position, now, "hold", p_close, 1, "，".join(why), request, resp)

    @staticmethod
    def _card_rule(position: PositionRecord, card: BattleCard | None, now: datetime) -> str | None:
        if card is None or not card.is_valid(now):
            return None  # 卡过期只停开新仓，已有持仓照常盯
        cs = card.for_symbol(position.symbol)
        # 卡里没有 / 只看不做：不投平仓票（研究 v2：只有方向反了才投）
        if cs is None or cs.bias in (Bias.NO_TRADE, Bias.BOTH):
            return None
        if cs.bias.value != position.side.value:
            return f"新作战卡方向改成 {cs.bias.value}"
        return None

    def _verdict(
        self,
        position: PositionRecord,
        now: datetime,
        action: str,
        p_close: float,
        consecutive: int,
        reason: str,
        request: JevRequest | None,
        r: JevResponse | None,
    ) -> KeeperVerdict:
        v = KeeperVerdict(
            position_id=position.position_id,
            ts=now,
            action="close" if action == "close" else "hold",
            p_close=min(max(p_close, 0.0), 1.0),
            consecutive=max(consecutive, 1),
            reason=reason,
            jev_request_id=request.request_id if request is not None else None,
        )
        if self._rec is not None:
            outcome = Outcome.CLOSE if v.action == "close" else Outcome.HOLD
            if r is not None and not r.ok:
                outcome = Outcome.ERROR
            jev_in, jev_out = jev_log_fields(request, r) if r is not None else ({}, {})
            self._rec.record(
                now,
                outcome,
                f"{position.symbol} {'全平' if v.action == 'close' else '拿住'}：{reason}",
                symbol=position.symbol,
                signal_id=position.signal_id,
                position_id=position.position_id,
                card_id=position.card_id,
                input=jev_in,
                output={
                    "action": v.action,
                    "p_close": v.p_close,
                    "consecutive": v.consecutive,
                    **jev_out,
                },
                latency_ms=r.latency_ms if r is not None else 0.0,
                cost_usd=r.cost_usd if r is not None else 0.0,
                model_version=r.model_version if r is not None else None,
            )
        return v
