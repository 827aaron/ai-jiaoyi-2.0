"""作战卡上一个币的方向随时间怎么变：按军师团记录重放卡上生效的方向（同 ``slow/card_flip.py``
的滞回），再按任意一段时间截取。

作战卡页每行看的是今天（本地 0 点起）；持仓页、历史页看的是一笔仓的持有期（开仓 → 平仓 / 现在），
可以跨天：开仓时卡上是什么方向，持有中有没有转到持仓的反面，系统（管家的卡规则）怎么处理的。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from jiaoyi.contracts import DecisionRecord, Outcome, Role
from jiaoyi.contracts.battle_card import CardSymbol
from jiaoyi.slow.card_flip import card_direction
from jiaoyi.slow.card_outlook import card_from_output


class DirStep(BaseModel):
    """一个币卡上方向的一次变动机会：一场会，或不跟会的翻转确认 / 恢复。"""

    ts: datetime
    meeting: str | None
    """这场会自己的结论 long / short / neutral；不是会（管家助手确认、恢复原方向）是 None。"""
    card: str
    """这之后卡上生效的方向（交易系统真正用的，含翻转滞回）：long / short / neutral。"""
    event: str | None = None
    """``card_flip`` 记录：ignored（会判观望、按设置保留原方向）/ pending / confirmed / …。"""
    since: datetime | None = None
    """卡上生效的那条来自哪场会（会判观望被忽略时是更早那场）；没读到那场会是 None。"""
    reason: str = Field(default="", exclude=True)
    """卡上生效的那条的理由（只给开仓理由用，不进接口：每行每步都带会把作战卡数据撑大）。"""


@dataclass
class _Track:
    eff: CardSymbol | None = None
    before: CardSymbol | None = None
    """最近一场会之前卡上的那条：会判观望被忽略、或改判进了待确认时，卡上留的是它。"""
    fresh: CardSymbol | None = None
    stash: CardSymbol | None = None
    """待确认翻转期间被换成只看不做之前的那条（恢复原方向时用）。"""
    eff_at: datetime | None = None
    before_at: datetime | None = None
    fresh_at: datetime | None = None
    stash_at: datetime | None = None
    after_meeting: bool = False
    steps: list[DirStep] = field(default_factory=list)


def _meeting_cs(r: DecisionRecord) -> CardSymbol | None:
    if not (r.symbol and r.output and r.outcome is Outcome.ACTION):
        return None
    try:
        # 记录里还夹着 usage（token 用量）、meeting_setup（偏空整改的分组）等不属于作战指令
        # 的键：契约禁止多余字段，不去掉的话校验会整条丢掉；只记录的 outlook 单独校验，
        # 坏了不连累卡
        return card_from_output({k: v for k, v in r.output.items() if k in CardSymbol.model_fields})
    except ValueError:
        return None


def replay(cmd: list[DecisionRecord]) -> dict[str, _Track]:
    """按时间重放军师团记录，得到每个币卡上生效的那条指令和每次变动机会。

    会议结论记录写的是会自己的结论；之后紧跟的 ``card_flip`` 记录说明卡上实际怎么处理：
    ignored = 卡上留着会前那条，pending = 会前那条只看不做，confirmed = 换成待确认的那场结论。"""
    tracks: dict[str, _Track] = {}
    for r in cmd:
        if not r.symbol:
            continue
        t = tracks.setdefault(r.symbol, _Track())
        cs = _meeting_cs(r)
        if cs is not None:
            t.before, t.fresh, t.eff, t.after_meeting = t.eff, cs, cs, True
            t.before_at, t.fresh_at, t.eff_at = t.eff_at, r.ts, r.ts
            d = card_direction(cs)
            t.steps.append(DirStep(ts=r.ts, meeting=d, card=d, since=r.ts, reason=cs.reason))
            continue
        if r.outcome is Outcome.INFO and r.output.get("market_review") == "superseded":
            # 临时会开完时卡上这个币已换成别的研究：这场结论没上卡
            if t.after_meeting and t.steps:
                t.eff, t.eff_at, t.after_meeting = t.before, t.before_at, False
                t.steps.pop()
            continue
        ev = r.output.get("card_flip") if r.outcome is Outcome.INFO else None
        if ev not in ("ignored", "pending", "confirmed", "cancelled", "dropped"):
            continue
        right_after, t.after_meeting = t.after_meeting, False
        if ev == "ignored" and right_after and t.before is not None:
            t.eff = t.before.model_copy(update={"expires_at": t.eff.expires_at if t.eff else None})
            t.eff_at = t.before_at
        elif ev == "ignored" and not right_after and t.stash is not None:
            t.eff, t.eff_at, t.stash = (
                t.stash,
                t.stash_at,
                None,
            )  # 切到 ignore 时把转观望的候选恢复成原方向
        elif ev == "pending" and t.before is not None:
            if t.stash is None:
                t.stash, t.stash_at = t.before, t.before_at
            t.eff = t.before.model_copy(update={"allowed_signals": []})
            t.eff_at = t.before_at
        elif ev == "confirmed" and t.fresh is not None:
            t.eff, t.eff_at, t.stash = t.fresh, t.fresh_at, None
        elif ev in ("cancelled", "dropped"):
            t.stash = None
        if t.eff is None:
            continue
        if right_after and t.steps:
            t.steps[-1] = t.steps[-1].model_copy(
                update={
                    "card": card_direction(t.eff),
                    "event": ev,
                    "since": t.eff_at,
                    "reason": t.eff.reason,
                }
            )
        elif ev in ("ignored", "confirmed"):
            t.steps.append(
                DirStep(
                    ts=r.ts,
                    meeting=None,
                    card=card_direction(t.eff),
                    event=ev,
                    since=t.eff_at,
                    reason=t.eff.reason,
                )
            )
    return tracks


def commander_steps(records: list[DecisionRecord], until: datetime) -> dict[str, list[DirStep]]:
    """``until`` 之前的军师团记录重放出的每个币的方向变动（旧的在前）。"""
    cmd = [r for r in records if r.role is Role.COMMANDER and r.ts <= until]
    return {sym: t.steps for sym, t in replay(cmd).items()}


def window(
    steps: list[DirStep], start: datetime, end: datetime
) -> tuple[tuple[str | None, datetime | None], list[DirStep]]:
    """``start`` 时卡上的方向（和它从哪场会来），以及 ``[start, end]`` 里的变动机会。

    记录里 ``start`` 之前没有这个币的会时方向是 None（读的记录不够早，或这个币那时还没上卡）。"""
    before = [st for st in steps if st.ts < start]
    inside = [st for st in steps if start <= st.ts <= end]
    return ((before[-1].card, before[-1].ts) if before else (None, None)), inside


_LS = frozenset({"long", "short"})


def flips(start: str | None, steps: list[DirStep]) -> int:
    """做多 ↔ 做空 换了几次（转观望、从观望转出不算）。"""
    n, prev = 0, start
    for st in steps:
        if prev in _LS and st.card in _LS and st.card != prev:
            n += 1
        prev = st.card
    return n


# ---------------------------------------------------------------- 持有期


HoldState = Literal["against", "pending", "neutral", "neutral_kept"]
"""持有期里卡上发生过的、和这笔仓有关的最要紧的一件事（依次）：

- against：卡上生效的方向转成了持仓的反面（管家的反向卡规则管的就是它）；
- pending：会判了反面（或观望），卡在等确认翻转，原方向只看不做，持仓不按卡规则平；
- neutral：卡上生效的方向转成观望（``card_neutral_action`` 管）；
- neutral_kept：会判观望，``card_neutral_flip = ignore`` 卡上保留原方向。"""

ActionKind = Literal[
    "close",
    "confirmed_close",
    "tighten",
    "tighten_close",
    "tighten_hold",
    "blocked",
    "neutral_close",
    "neutral_tighten",
    "neutral_hold",
]


class HoldAction(BaseModel):
    """管家的卡规则对这笔仓做过的一件事（同一种连着出现只记第一次，``count`` 计次）。"""

    ts: datetime
    kind: ActionKind
    text: str
    """决策记录的原话（去掉币名）。"""
    count: int = 1


class HoldCard(BaseModel):
    """一笔仓持有期间作战卡上这个币的方向和系统的处理。"""

    side: str
    start: str | None
    """开仓时卡上生效的方向；读到的记录里开仓前没有这个币的会是 None。"""
    start_ts: datetime | None = None
    """``start`` 来自哪场会。"""
    steps: list[DirStep] = []
    """持有期（开仓 → 平仓 / 现在）的变动机会，旧的在前。"""
    state: HoldState | None = None
    state_ts: datetime | None = None
    state_from: str | None = None
    state_to: str | None = None
    """``state`` 那一刻卡上从什么方向变成什么（pending / neutral_kept 的 to 是会的结论）。"""
    pending_open: bool = False
    """pending：到平仓 / 现在还没确认也没取消。"""
    actions: list[HoldAction] = []
    closed_by: Literal["opposite_card", "neutral_card"] | None = None
    """管家平仓（close_reason = keeper）时，最后一次判平是不是卡规则：反向卡 / 观望卡。"""


CARD_CLOSE: dict[str, Literal["opposite_card", "neutral_card"]] = {
    "close": "opposite_card",
    "confirmed_close": "opposite_card",
    "tighten_close": "opposite_card",
    "neutral_close": "neutral_card",
}


_PRIORITY: dict[str, HoldState] = {
    "against": "against",
    "pending": "pending",
    "neutral": "neutral",
    "pending_neutral": "pending",
    "neutral_kept": "neutral_kept",
}
"""同一笔仓碰到几件事时小标写哪件：先反向，再待确认反向、转观望、待确认转观望、会判观望。"""


def action_kind(r: DecisionRecord) -> ActionKind | None:
    o = r.output
    rule = o.get("card_rule")
    if isinstance(rule, dict) and rule.get("kind") in ("opposite_card", "neutral_card"):
        neutral = rule.get("kind") == "neutral_card"
        if "close_blocked" in o:
            return "blocked"
        if r.outcome is Outcome.CLOSE:
            if neutral:
                return "neutral_close"
            return "confirmed_close" if rule.get("branch") == "confirmed_close" else "close"
        return "neutral_hold" if neutral else "tighten"
    if isinstance(o.get("opposite_tighten"), dict):
        neutral = o.get("card_rule_kind") == "neutral_card"
        if r.outcome is Outcome.CLOSE:
            return "neutral_close" if neutral else "tighten_close"
        return "neutral_tighten" if neutral else "tighten_hold"
    return None


def hold_card(
    steps: list[DirStep] | None,
    side: str,
    opened_at: datetime,
    end: datetime,
    keeper: list[DecisionRecord],
    keeper_closed: bool = False,
) -> HoldCard | None:
    """``steps``：这个币重放出的全部变动机会；``keeper``：这笔仓的管家记录（时间升序）；
    ``keeper_closed``：这笔是管家平的（账本 close_reason = keeper），才看最后一次判平是谁。

    卡上没有这个币的记录、管家也没按卡处理过时返回 None。"""
    (start, start_ts), inside = window(steps or [], opened_at, end)
    opp = "short" if side == "long" else "long"
    mine = [r for r in keeper if r.role is Role.KEEPER and opened_at <= r.ts <= end]
    actions: list[HoldAction] = []
    for r in mine:
        kind = action_kind(r)
        if kind is None:
            continue
        if actions and actions[-1].kind == kind:
            actions[-1].count += 1
            continue
        text = r.summary.removeprefix(f"{r.symbol} ") if r.symbol else r.summary
        actions.append(HoldAction(ts=r.ts, kind=kind, text=text))
    if start is None and not inside and not actions:
        return None

    found: dict[str, tuple[datetime, str | None, str]] = {}
    pending_at: dict[str, int] = {}
    prev = start
    for i, st in enumerate(inside):
        if st.card == opp and prev != opp:
            found.setdefault("against", (st.ts, prev, st.card))
        elif st.card == "neutral" and prev != "neutral":
            found.setdefault("neutral", (st.ts, prev, st.card))
        if st.event == "pending" and st.meeting in (opp, "neutral"):
            slot = "pending" if st.meeting == opp else "pending_neutral"
            if slot not in found:
                found[slot], pending_at[slot] = (st.ts, st.card, st.meeting or ""), i
        if st.event == "ignored" and st.meeting == "neutral" and st.card == side:
            found.setdefault("neutral_kept", (st.ts, st.card, "neutral"))
        prev = st.card
    if "against" not in found and any(CARD_CLOSE.get(a.kind) == "opposite_card" for a in actions):
        # 开仓那会儿卡已经反了（会的记录早于开仓几秒）：按管家第一次处理的时间算
        first = next(a for a in actions if CARD_CLOSE.get(a.kind) == "opposite_card")
        found["against"] = (first.ts, start, opp)
    key = next((k for k in _PRIORITY if k in found), None)
    state = _PRIORITY[key] if key is not None else None
    pending_open = False
    if key in pending_at:
        later = inside[pending_at[key] + 1 :]
        pending_open = not any(
            st.event in ("confirmed", "cancelled", "dropped") or st.meeting == side for st in later
        )

    closed_by = None
    if keeper_closed:
        last_close = next((r for r in reversed(mine) if r.outcome is Outcome.CLOSE), None)
        kind = action_kind(last_close) if last_close is not None else None
        closed_by = CARD_CLOSE.get(kind) if kind is not None else None

    hit: tuple[datetime | None, str | None, str | None] = (
        found[key] if key is not None else (None, None, None)
    )
    ts, frm, to = hit
    return HoldCard(
        side=side,
        start=start,
        start_ts=start_ts,
        steps=inside,
        state=state,
        state_ts=ts,
        state_from=frm,
        state_to=to,
        pending_open=pending_open,
        actions=actions,
        closed_by=closed_by,
    )
