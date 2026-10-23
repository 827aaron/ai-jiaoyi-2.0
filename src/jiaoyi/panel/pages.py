"""总览以外各页的数据。口径和总览共用（``compute_kpis``、``position_views``）。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from enum import StrEnum
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from jiaoyi.contracts import (
    AppConfig,
    Category,
    DecisionRecord,
    Health,
    ModuleStatus,
    Outcome,
    RiskConfig,
    Role,
    SymbolPoolEntry,
)
from jiaoyi.contracts.risk_config import recommended_caps
from jiaoyi.core.pool_overrides import read_pool_overrides
from jiaoyi.core.recorder import local_day
from jiaoyi.core.secrets import KeyNeed, key_needs
from jiaoyi.panel.actions import ProfileInfo, Section, editable_fields
from jiaoyi.panel.files import PanelFiles, read_live_gate, read_pool
from jiaoyi.panel.hb import HbSnapshot
from jiaoyi.panel.meetings import MeetingSummary, list_meetings
from jiaoyi.panel.overview import (
    STALE_S,
    LampState,
    PositionView,
    compute_kpis,
    position_views,
)
from jiaoyi.slow.settings import SlowConfig, slow_defaults

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
    status: str
    """明确的状态：未启用 / 等待首次运行 / 正常（上次 hh:mm） / 失败：原因 / 无心跳超过 N 分钟。"""
    headline: str
    """模块自己报的今日要点（可能为空）。"""
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
    meetings: list[MeetingSummary] = []
    """选中军师团时：每个币最近一场会（data/meeting_live/），正在开的在前。"""


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
    run = _run_info(files, status, now)
    cards = [_role_card(role, status.get(role.value), now, run) for role in ROLE_NAMES]
    chosen = selected or Role.GATE
    mine = [_item(r) for r in reversed(records) if r.role is chosen][:TEAM_RECORD_LIMIT]
    meets = list_meetings(files.root, now) if chosen is Role.COMMANDER else []
    return TeamPage(roles=cards, selected=chosen, records=mine, meetings=meets)


@dataclass(frozen=True)
class _RunInfo:
    """团队页判断“没心跳”是哪种情况用：主程序在不在跑、哪些角色这次启动是桩（没接入）。"""

    running: bool
    started_at: datetime | None
    stub_roles: frozenset[str]
    tz: str


def _run_info(files: PanelFiles, status: dict[str, ModuleStatus], now: datetime) -> _RunInfo:
    beats = [s.ts for s in status.values()]
    running = bool(beats) and now - max(beats) <= timedelta(seconds=STALE_S)
    rt = files.runtime()
    started = rt.get("started_at")
    try:
        started_at = datetime.fromisoformat(started) if isinstance(started, str) else None
    except ValueError:
        started_at = None
    stubs = rt.get("stub_roles")
    stub_roles = (
        frozenset(x for x in stubs if isinstance(x, str))
        if isinstance(stubs, list)
        else frozenset()
    )
    return _RunInfo(running, started_at, stub_roles, files.tz)


def _opt_time(st: ModuleStatus, name: str) -> datetime | None:
    v = getattr(st, name, None)
    return v if isinstance(v, datetime) else None


def role_state(
    role: Role, st: ModuleStatus | None, now: datetime, run: _RunInfo
) -> tuple[LampState, str]:
    """灯 + 一句明确的状态（Aaron 2026-10-23：不许写“A 或 B”这种猜测）。

    未启用 / 等待首次运行 / 正常（上次 hh:mm） / 失败：原因 / 无心跳超过 N 分钟。
    """

    def hm(t: datetime) -> str:
        return t.astimezone(ZoneInfo(run.tz)).strftime("%H:%M")

    if st is None:
        if role.value in run.stub_roles:
            return "off", "未启用（这次启动没接入）"
        if not run.running:
            return "off", "主程序没在运行"
        since = f"（主程序 {hm(run.started_at)} 启动）" if run.started_at else ""
        return "slow", f"等待首次运行{since}"
    # 选币等定时任务另外报上次 / 下次运行、币数、失败原因（字段由慢环加进契约，旧文件没有就按心跳）
    last = _opt_time(st, "last_run_at") or st.ts
    nxt = _opt_time(st, "next_run_at")
    count = getattr(st, "symbol_count", None)
    error = getattr(st, "error", None)
    extra = "".join(
        [f"，{count} 个币" if isinstance(count, int) else "", f"，下次 {hm(nxt)}" if nxt else ""]
    )
    age = now - st.ts
    if age > timedelta(seconds=STALE_S):
        mins = max(1, int(age.total_seconds() // 60))
        return "bad", f"无心跳超过 {mins} 分钟（最后 {hm(st.ts)}）"
    if st.health is Health.DISABLED:
        return "off", "未启用"
    if st.health is Health.DOWN or (isinstance(error, str) and error):
        why = error if isinstance(error, str) and error else st.last_error or st.headline
        return "bad", f"失败：{why or '没写原因'}"
    if st.health is Health.STARTING or (_opt_time(st, "last_run_at") is None and nxt):
        return "slow", f"等待首次运行{f' {hm(nxt)}' if nxt else ''}"
    if st.health is Health.DEGRADED:
        return "slow", f"降级运行（上次 {hm(last)}{extra}）"
    return "ok", f"正常（上次 {hm(last)}{extra}）"


def _role_card(role: Role, st: ModuleStatus | None, now: datetime, run: _RunInfo) -> RoleCard:
    state, text = role_state(role, st, now, run)
    if st is None:
        return RoleCard(
            role=role, name=ROLE_NAMES[role], state=state, status=text, headline="",
            last_beat=None, last_error=None, calls=0, passed=0, rejected=0, errors=0,
            avg_latency_ms=0.0, cost_usd=0.0,
        )  # fmt: skip
    t = st.today
    return RoleCard(
        role=role,
        name=ROLE_NAMES[role],
        state=state,
        status=text,
        headline=st.headline,
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
    test: bool = False
    """没有哨兵 / 拍板 / 保镖记录的单：冒烟测试或手动下的，不是机器人自己的交易。"""


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
    test_trades: int = 0
    """范围内的测试单笔数；``include_tests`` 为假时它们不在 rows 和各项统计里。"""
    rejected: list[TrailItem]
    """范围内被拍板放弃 / 保镖拒绝的信号，最新在前，最多 ``REJECT_LIMIT`` 条。"""


def _range_start(now: datetime, tz: str, rng: HistoryRange) -> datetime:
    day0 = datetime.combine(local_day(now, tz), time(), tzinfo=ZoneInfo(tz))
    days = {"today": 0, "7d": 6, "30d": 29}[rng]
    return (day0 - timedelta(days=days)).astimezone(UTC)


def build_history(
    files: PanelFiles,
    risk: RiskConfig,
    now: datetime,
    rng: HistoryRange,
    include_tests: bool = False,
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
    tests = 0
    for p in closed:
        test = p.signal_id not in kind_of
        tests += test
        if test and not include_tests:
            continue
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
                test=test,
            )
        )
    wins = [t.pnl_usdt for t in rows if t.pnl_usdt > 0]
    losses = [-t.pnl_usdt for t in rows if t.pnl_usdt < 0]
    by: dict[str, list[float]] = {}
    for t in rows:
        by.setdefault("test" if t.test else (t.signal_kind or "unknown"), []).append(t.pnl_usdt)
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
        test_trades=tests,
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


# ---------------------------------------------------------------- 选币


class PoolPage(BaseModel):
    ts: datetime | None
    pool_id: str | None
    in_pool: int
    picked: int
    rejected: int
    entries: list[SymbolPoolEntry]
    """进池的在前，按分数从高到低；没进池的（没过硬条件、拉黑）在后。"""
    blocked: dict[str, str]
    pinned: list[str] = []
    """你固定的币（data/pool_overrides.json，比候选池文件新：改完要等主循环重新选币才进池）。"""
    blacklist: list[str] = []
    manual: list[str] = []
    overrides_error: bool = False
    """pool_overrides.json 读不出：主循环沿用上一份，页面不让改。"""


def build_pool(files: PanelFiles) -> PoolPage:
    pool = read_pool(files.root)
    ov = read_pool_overrides(files.root)
    entries = sorted(pool.entries, key=lambda e: (not e.in_pool, -e.score)) if pool else []
    return PoolPage(
        ts=pool.ts if pool else None,
        pool_id=pool.pool_id if pool else None,
        in_pool=sum(1 for e in entries if e.in_pool),
        picked=sum(1 for e in entries if e.picked_by_card),
        rejected=sum(1 for e in entries if not e.in_pool),
        entries=entries,
        blocked=files.blocked(),
        pinned=sorted(ov.pinned) if ov else [],
        blacklist=sorted(ov.blacklist) if ov else [],
        manual=sorted(ov.manual) if ov else [],
        overrides_error=ov is None,
    )


# ---------------------------------------------------------------- 设置


class SettingItem(BaseModel):
    key: str
    label: str
    value: object
    bounds: str
    """合理范围，例如 “1–10”。超出程序拒绝启动。"""
    editable: bool = False
    """能在网页上改（要面板密码）。"""
    choices: list[object] = []
    """只能从这几个里选（下拉）。"""
    nullable: bool = False
    """可以留空 = 不写这一项，用默认值。"""
    profiled: bool = False
    """档位给默认值的键：留空 = 回到档位默认。"""
    pinned: bool = False
    """配置文件里写死了值，切档位不会跟着变。"""
    profile_default: object = None
    """当前档位给的默认值（``pinned`` 时显示）。"""
    recommended: float | None = None
    """硬上限的推荐值（按资金算，见 ``recommended_caps``）；只提示，填什么都收。"""
    recommended_why: str = ""


class SettingsPage(BaseModel):
    mode: str
    risk: list[SettingItem]
    risk_cautions: list[str] = []
    """硬上限比推荐值冒险的项（只提示，不拒绝）。"""
    recommend_basis_usdt: float | None = None
    """推荐值按多少资金算的（账户权益；拿不到时 None，按 1000U 算）。"""
    category_caps: dict[str, float]
    app: list[SettingItem]
    fast_loop: list[SettingItem]
    predictor: list[SettingItem] = []
    schedule: list[SettingItem] = []
    """慢环节奏（config/slow.toml 的 [schedule]）。"""
    schedule_caution: str | None = None
    """节奏配置可能造成换卡空档时的提示（只提示，不拒绝）。"""
    profile: ProfileInfo | None = None
    keys: dict[str, bool]
    key_needs: dict[str, KeyNeed] = {}
    """按当前模式和通道：必须 / 可选 / 用不到。"""
    """密钥名 → 配了没有。只有这个，没有值。"""
    password_set: bool
    live_armed: bool
    writable: bool = False
    """能不能在网页上改（面板拿到了配置文件路径、文件读得出）。"""
    file_error: str | None = None
    """配置文件现在读不出或不合格（有人在终端改坏了）。"""
    changes: list[dict[str, object]] = []
    """最近的配置改动（data/config_changes.jsonl），新的在前。"""


def _items(
    model: BaseModel, section: Section, skip: frozenset[str] = frozenset()
) -> list[SettingItem]:
    specs = editable_fields(section)
    out = []
    for name, field in type(model).model_fields.items():
        if name in skip:
            continue
        value = getattr(model, name)
        if isinstance(value, BaseModel | dict):
            continue
        lo = hi = None
        strict = False
        for m in field.metadata:
            if hasattr(m, "gt"):
                lo, strict = m.gt, True
            lo = getattr(m, "ge", lo)
            hi = getattr(m, "le", getattr(m, "lt", hi))
        if lo is not None and hi is not None:
            bounds = f"{lo:g}–{hi:g}"
        else:
            bounds = f"{'>' if strict else '≥'}{lo:g}" if lo is not None else ""
        desc = (field.description or "").split("。")[0]
        spec = specs.get(name)
        out.append(
            SettingItem(
                key=name,
                label=desc or name,
                value=_plain(value),
                bounds=bounds,
                editable=spec is not None,
                choices=list(spec.choices) if spec else [],
                nullable=bool(spec and spec.nullable),
                profiled=bool(spec and spec.profiled),
            )
        )
    return out


def _plain(v: object) -> object:
    return v.value if isinstance(v, StrEnum) else v


def build_settings(
    files: PanelFiles,
    risk: RiskConfig,
    app: AppConfig,
    keys: dict[str, bool],
    slow: SlowConfig | None = None,
    profile: ProfileInfo | None = None,
    equity_usdt: float | None = None,
) -> SettingsPage:
    password_set, armed = read_live_gate(files.root)
    page = SettingsPage(
        mode=app.mode.value,
        risk=_items(risk, "risk"),
        risk_cautions=risk.cautions(equity_usdt),
        recommend_basis_usdt=equity_usdt,
        category_caps={k.value: v for k, v in risk.category_exposure_x.items()},
        app=_items(app, "app", frozenset({"mode"})),
        fast_loop=_items(app.fast_loop, "fast_loop"),
        predictor=_items(app.predictor, "predictor"),
        schedule=_items((slow or slow_defaults(app.profile)).schedule, "schedule"),
        schedule_caution=(slow or slow_defaults(app.profile)).schedule.caution(),
        keys=keys,
        key_needs=key_needs(
            app.mode.value,
            app.fast_loop.jev_provider,
            (slow or slow_defaults(app.profile)).llm.provider,
        ),
        password_set=password_set,
        live_armed=armed,
        profile=profile,
    )
    recs = recommended_caps(equity_usdt)
    for item in page.risk:
        if rec := recs.get(item.key):
            item.recommended, item.recommended_why = rec.value, rec.why
    if profile:
        for section, hit in profile.pinned.items():
            for item in getattr(page, section):
                if item.key in hit:
                    item.pinned, item.profile_default = True, hit[item.key]
    return page
