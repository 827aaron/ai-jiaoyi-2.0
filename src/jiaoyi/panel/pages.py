"""总览以外各页的数据。口径和总览共用（``compute_kpis``、``position_views``）。"""

from __future__ import annotations

from datetime import datetime, timedelta

from pydantic import BaseModel

from jiaoyi.contracts import (
    Category,
    DecisionRecord,
    Health,
    ModuleStatus,
    Outcome,
    RiskConfig,
    Role,
)
from jiaoyi.panel.files import PanelFiles
from jiaoyi.panel.hb import HbSnapshot
from jiaoyi.panel.overview import (
    STALE_S,
    LampState,
    PositionView,
    compute_kpis,
    position_views,
)

ROLE_NAMES: dict[Role, str] = {
    Role.COMMANDER: "军师团",
    Role.SCREENER: "选币",
    Role.SENTINEL: "哨兵",
    Role.BOOK_READER: "盘口员",
    Role.PREDICTOR: "预测员 Kronos",
    Role.GATE: "拍板人",
    Role.RISK_GUARD: "保镖",
    Role.EXECUTOR: "执行器",
    Role.LIFEBOAT: "保命止损",
    Role.KEEPER: "持仓管家",
    Role.PATROL: "巡逻兵",
    Role.RECONCILER: "对账员",
    Role.ALERTING: "告警",
}
TEAM_RECORD_LIMIT = 200


class TrailItem(BaseModel):
    ts: datetime
    role: Role
    outcome: Outcome
    text: str
    input: dict[str, object] = {}
    """它看到了什么（哨兵的快照等）。"""
    output: dict[str, object] = {}
    """拍板人的概率、保镖的拒绝理由等，原样给页面挑着显示。"""
    latency_ms: float = 0.0
    cost_usd: float = 0.0


class GroupExposure(BaseModel):
    category: str
    exposure_x: float
    cap_x: float


class PositionsPage(BaseModel):
    positions: list[PositionView]
    unrealized_usdt: float | None
    lifeboats_ok: int
    groups: list[GroupExposure]
    trails: dict[str, list[TrailItem]]
    """position_id → 这笔从信号到现在的每一步（今天的记录）。"""


class RoleCard(BaseModel):
    role: Role
    name: str
    state: LampState
    headline: str
    last_beat: datetime | None
    last_error: str | None
    calls: int
    passed: int
    rejected: int
    errors: int
    avg_latency_ms: float
    cost_usd: float


class TeamPage(BaseModel):
    roles: list[RoleCard]
    selected: Role
    records: list[TrailItem]
    """所选角色今天的记录，最新在前。"""


def build_positions(
    files: PanelFiles, hb: HbSnapshot, risk: RiskConfig, now: datetime
) -> PositionsPage:
    ledger = files.ledger()
    records = files.decisions_today(now)
    live = ledger.live()
    views = position_views(live, records, hb)
    kpis = compute_kpis(files, ledger, hb, risk, now)
    equity = kpis.equity_usdt
    groups = []
    for cat in Category:
        notional = sum(ledger.notional(p) for p in live if p.category is cat)
        groups.append(
            GroupExposure(
                category=cat.value,
                exposure_x=notional / equity if equity else 0.0,
                cap_x=risk.category_exposure_x[cat],
            )
        )
    trails: dict[str, list[TrailItem]] = {}
    for p in live:
        trails[p.position_id] = [
            _item(r)
            for r in records
            if r.position_id == p.position_id or (r.signal_id and r.signal_id == p.signal_id)
        ]
    pnl = [v.unrealized_usdt for v in views if v.unrealized_usdt is not None]
    return PositionsPage(
        positions=views,
        unrealized_usdt=sum(pnl) if pnl else None,
        lifeboats_ok=sum(1 for v in views if v.lifeboat_status == "active"),
        groups=groups,
        trails=trails,
    )


def build_team(files: PanelFiles, now: datetime, selected: Role | None) -> TeamPage:
    status = files.module_status()
    records = files.decisions_today(now)
    cards = [_role_card(role, status.get(role.value), now) for role in ROLE_NAMES]
    chosen = selected or Role.GATE
    mine = [_item(r) for r in reversed(records) if r.role is chosen][:TEAM_RECORD_LIMIT]
    return TeamPage(roles=cards, selected=chosen, records=mine)


_HEALTH: dict[Health, LampState] = {
    Health.OK: "ok",
    Health.DEGRADED: "slow",
    Health.STARTING: "slow",
    Health.DOWN: "bad",
    Health.DISABLED: "off",
}


def _role_card(role: Role, st: ModuleStatus | None, now: datetime) -> RoleCard:
    if st is None:
        return RoleCard(
            role=role, name=ROLE_NAMES[role], state="off", headline="没有心跳（未接入或还没启动）",
            last_beat=None, last_error=None, calls=0, passed=0, rejected=0, errors=0,
            avg_latency_ms=0.0, cost_usd=0.0,
        )  # fmt: skip
    stale = now - st.ts > timedelta(seconds=STALE_S)
    state: LampState = "bad" if stale else _HEALTH[st.health]
    t = st.today
    return RoleCard(
        role=role,
        name=ROLE_NAMES[role],
        state=state,
        headline=st.headline or ("掉线" if stale else ""),
        last_beat=st.ts,
        last_error=st.last_error,
        calls=t.calls,
        passed=t.passed,
        rejected=t.rejected,
        errors=t.errors,
        avg_latency_ms=t.avg_latency_ms,
        cost_usd=t.cost_usd,
    )


def build_signal(files: PanelFiles, now: datetime, signal_id: str) -> list[TrailItem]:
    """一个信号今天的整条链：哨兵 → 拍板 → 保镖 → 执行 → 盯仓。"""
    return [_item(r) for r in files.decisions_today(now) if r.signal_id == signal_id]


def _item(r: DecisionRecord) -> TrailItem:
    return TrailItem(
        ts=r.ts,
        role=r.role,
        outcome=r.outcome,
        text=r.summary,
        input=r.input,
        output=r.output,
        latency_ms=r.latency_ms,
        cost_usd=r.cost_usd,
    )
