"""总览以外各页的数据。口径和总览共用（``compute_kpis``、``position_views``）。"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

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
from jiaoyi.core.recorder import local_day
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


# ---------------------------------------------------------------- 历史

HistoryRange = Literal["today", "7d", "30d"]
REJECT_LIMIT = 300


class TradeRow(BaseModel):
    position_id: str
    signal_id: str
    symbol: str
    side: str
    quantity: float
    leverage: int
    entry_price: float | None
    opened_at: datetime
    closed_at: datetime
    close_reason: str | None
    pnl_usdt: float
    """hummingbot 的 net_pnl_quote（已扣手续费）。"""
    fees_usdt: float
    signal_kind: str | None


class KindStats(BaseModel):
    kind: str
    trades: int
    win_rate: float | None
    pnl_usdt: float


class CurvePoint(BaseModel):
    ts: datetime
    cum_pnl_usdt: float


class HistoryPage(BaseModel):
    range: HistoryRange
    net_pnl_usdt: float
    trades: int
    win_rate: float | None
    avg_win_loss: float | None
    """平均盈利 ÷ 平均亏损（绝对值）。"""
    max_drawdown_usdt: float
    """按平仓顺序累计盈亏的最大回撤。"""
    curve: list[CurvePoint]
    by_kind: list[KindStats]
    rows: list[TradeRow]
    """最新在前。"""
    rejected: list[TrailItem]
    """范围内被拍板放弃 / 保镖拒绝的信号，最新在前，最多 ``REJECT_LIMIT`` 条。"""


def _range_start(now: datetime, tz: str, rng: HistoryRange) -> datetime:
    day0 = datetime.combine(local_day(now, tz), time(), tzinfo=ZoneInfo(tz))
    days = {"today": 0, "7d": 6, "30d": 29}[rng]
    return (day0 - timedelta(days=days)).astimezone(UTC)


def build_history(
    files: PanelFiles, risk: RiskConfig, now: datetime, rng: HistoryRange
) -> HistoryPage:
    start = _range_start(now, risk.day_reset_tz, rng)
    closed = sorted(
        (
            p
            for p in files.ledger().positions
            if p.closed_at is not None
            and p.realized_pnl_usdt is not None
            and start <= p.closed_at <= now
        ),
        key=lambda p: p.closed_at or now,
    )
    # 信号类型在哨兵 / 保镖的记录里；开仓可能早于范围起点，往前多读一天
    first_open = min((p.opened_at for p in closed), default=start)
    recs = files.decisions_since(
        min(first_open, start) - timedelta(days=1),
        now,
        frozenset({Role.SENTINEL, Role.GATE, Role.RISK_GUARD}),
    )
    kind_of: dict[str, str] = {}
    for r in recs:
        k = r.output.get("kind") if r.role is Role.SENTINEL else r.input.get("kind")
        if r.signal_id and isinstance(k, str):
            kind_of.setdefault(r.signal_id, k)

    rows: list[TradeRow] = []
    cum = peak = dd = 0.0
    curve: list[CurvePoint] = []
    for p in closed:
        pnl = p.realized_pnl_usdt or 0.0
        cum += pnl
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
        assert p.closed_at is not None
        curve.append(CurvePoint(ts=p.closed_at, cum_pnl_usdt=cum))
        rows.append(
            TradeRow(
                position_id=p.position_id,
                signal_id=p.signal_id,
                symbol=p.symbol,
                side=p.side.value,
                quantity=p.quantity,
                leverage=p.leverage,
                entry_price=p.entry_price,
                opened_at=p.opened_at,
                closed_at=p.closed_at,
                close_reason=p.close_reason.value if p.close_reason else None,
                pnl_usdt=pnl,
                fees_usdt=p.fees_usdt,
                signal_kind=kind_of.get(p.signal_id),
            )
        )
    wins = [t.pnl_usdt for t in rows if t.pnl_usdt > 0]
    losses = [-t.pnl_usdt for t in rows if t.pnl_usdt < 0]
    by: dict[str, list[float]] = {}
    for t in rows:
        by.setdefault(t.signal_kind or "unknown", []).append(t.pnl_usdt)
    rejected = [
        _item(r)
        for r in reversed(recs)
        if r.ts >= start
        and (
            (r.role is Role.GATE and r.outcome in (Outcome.REJECT, Outcome.ERROR))
            or (r.role is Role.RISK_GUARD and r.outcome is Outcome.REJECT)
        )
    ][:REJECT_LIMIT]
    return HistoryPage(
        range=rng,
        net_pnl_usdt=cum,
        trades=len(rows),
        win_rate=len(wins) / len(rows) if rows else None,
        avg_win_loss=(
            (sum(wins) / len(wins)) / (sum(losses) / len(losses)) if wins and losses else None
        ),
        max_drawdown_usdt=dd,
        curve=curve,
        by_kind=[
            KindStats(
                kind=k,
                trades=len(v),
                win_rate=sum(1 for x in v if x > 0) / len(v),
                pnl_usdt=sum(v),
            )
            for k, v in sorted(by.items(), key=lambda kv: -sum(kv[1]))
        ],
        rows=list(reversed(rows)),
        rejected=rejected,
    )


# ---------------------------------------------------------------- 资金


class CostRow(BaseModel):
    role: Role
    name: str
    today_usd: float
    week_usd: float


class FundsPage(BaseModel):
    equity_usdt: float | None
    equity_source: str
    wallet_usdt: float | None
    available_usdt: float | None
    unrealized_usdt: float | None
    day_start_equity_usdt: float | None
    realized_today_usdt: float
    """今天平仓的净盈亏（已扣手续费）。"""
    fees_today_usdt: float
    other_today_usdt: float | None
    """权益变化里不是已实现、也不是浮盈亏的部分：资金费、转入转出、口径差。"""
    halt_line_usdt: float | None
    """权益跌到这里触发日亏熔断。"""
    halted: bool
    costs: list[CostRow]
    cost_today_usd: float
    cost_week_usd: float


def build_funds(files: PanelFiles, hb: HbSnapshot, risk: RiskConfig, now: datetime) -> FundsPage:
    ledger = files.ledger()
    kpis = compute_kpis(files, ledger, hb, risk, now)
    tz = risk.day_reset_tz
    today = local_day(now, tz)
    closed_today = [
        p
        for p in ledger.positions
        if p.closed_at is not None
        and p.realized_pnl_usdt is not None
        and local_day(p.closed_at, tz) == today
    ]
    realized = sum(p.realized_pnl_usdt or 0.0 for p in closed_today)
    unrealized = hb.unrealized_usdt if hb.ok else None
    start = kpis.day_start_equity_usdt
    other = None
    if kpis.equity_usdt is not None and start is not None and unrealized is not None:
        other = kpis.equity_usdt - start - realized - unrealized
    paid = frozenset({Role.GATE, Role.KEEPER, Role.COMMANDER, Role.PREDICTOR})
    week = files.decisions_since(_range_start(now, tz, "7d"), now, paid)
    costs = []
    for role in (Role.GATE, Role.KEEPER, Role.COMMANDER, Role.PREDICTOR):
        mine = [r for r in week if r.role is role]
        costs.append(
            CostRow(
                role=role,
                name=ROLE_NAMES[role],
                today_usd=sum(r.cost_usd for r in mine if local_day(r.ts, tz) == today),
                week_usd=sum(r.cost_usd for r in mine),
            )
        )
    return FundsPage(
        equity_usdt=kpis.equity_usdt,
        equity_source=kpis.equity_source,
        wallet_usdt=hb.wallet_usdt if hb.ok else None,
        available_usdt=hb.available_usdt if hb.ok else None,
        unrealized_usdt=unrealized,
        day_start_equity_usdt=start,
        realized_today_usdt=realized,
        fees_today_usdt=sum(p.fees_usdt for p in closed_today),
        other_today_usdt=other,
        halt_line_usdt=start * (1 - risk.daily_loss_halt_pct / 100.0) if start else None,
        halted=kpis.halted,
        costs=costs,
        cost_today_usd=sum(c.today_usd for c in costs),
        cost_week_usd=sum(c.week_usd for c in costs),
    )
