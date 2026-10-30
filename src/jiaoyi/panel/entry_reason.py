"""一笔仓为什么开：信号、开仓时卡上的方向和那场会的理由、拍板人的做这笔概率、风控员给的仓位档。

都从面板已经在读的记录里拿：哨兵记录（信号类型，账本也有）、拍板人记录（``jev.answers.take``）、
风控员放行记录（``detail.size_tier``），卡上方向按军师团记录重放（``card_dir``）。
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from pydantic import BaseModel

from jiaoyi.contracts import DecisionRecord, Outcome, PositionRecord, Role
from jiaoyi.panel.card_dir import DirStep


class EntryReason(BaseModel):
    signal_kind: str | None = None
    side: str
    card: str | None = None
    """开仓那一刻卡上生效的方向 long / short / neutral；读到的记录里开仓前没有这个币的会是 None。"""
    card_at: datetime | None = None
    """那个方向来自哪场会。"""
    card_reason: str = ""
    jev_take: float | None = None
    """拍板人「做这笔」的概率（0–1）。"""
    size_tier: str | None = None
    """风控员放行时用的仓位档 S / M / L。"""


def entry_facts(records: Iterable[DecisionRecord]) -> dict[str, tuple[float | None, str | None]]:
    """信号号 → (拍板人做这笔的概率, 风控员放行的仓位档)；同一信号有多条时取最后一条。"""
    take: dict[str, float] = {}
    tier: dict[str, str] = {}
    for r in records:
        if not r.signal_id:
            continue
        if r.role is Role.GATE:
            ans = (r.output.get("jev") or {}).get("answers") or {}
            p = (ans.get("take") or {}).get("take") if isinstance(ans, dict) else None
            if isinstance(p, int | float) and not isinstance(p, bool):
                take[r.signal_id] = float(p)
        elif r.role is Role.RISK_GUARD and r.outcome is Outcome.PASS:
            t = (r.output.get("detail") or {}).get("size_tier")
            if isinstance(t, str):
                tier[r.signal_id] = t
    return {s: (take.get(s), tier.get(s)) for s in take.keys() | tier.keys()}


def entry_reason(
    p: PositionRecord,
    steps: list[DirStep] | None,
    facts: dict[str, tuple[float | None, str | None]],
    kind: str | None = None,
) -> EntryReason:
    """``kind``：账本没记信号类型（旧仓位）时从哨兵记录查到的。"""
    before = [st for st in steps or [] if st.ts <= p.opened_at]
    at = before[-1] if before else None
    take, tier = facts.get(p.signal_id, (None, None))
    return EntryReason(
        signal_kind=p.signal_kind.value if p.signal_kind else kind,
        side=p.side.value,
        card=at.card if at else None,
        card_at=(at.since or at.ts) if at else None,
        card_reason=at.reason if at else "",
        jev_take=take,
        size_tier=tier,
    )
