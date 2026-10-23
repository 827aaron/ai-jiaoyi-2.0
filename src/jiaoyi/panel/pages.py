"""总览以外各页的数据。口径和总览共用（``compute_kpis``、``position_views``）。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from typing import Any, Literal
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
from jiaoyi.core import profiles
from jiaoyi.core.pool_overrides import PoolOverrides, read_pool_overrides
from jiaoyi.core.recorder import local_day
from jiaoyi.core.secrets import KeyNeed, key_needs
from jiaoyi.panel.actions import ProfileInfo, Section, editable_fields
from jiaoyi.panel.files import Ledger, PanelFiles, read_live_gate, read_pool
from jiaoyi.panel.hb import HbSnapshot
from jiaoyi.panel.meetings import MARKET, MeetingSummary, list_meetings
from jiaoyi.panel.overview import (
    STALE_S,
    LampState,
    PositionView,
    compute_kpis,
    position_views,
)
from jiaoyi.slow.settings import BudgetConfig, ScreenerSection, SlowConfig, slow_defaults

# 团队页按数据流排：选币 → 军师团 → 盘口员 → 预测员 → 哨兵 → 拍板 → 保镖 → 下单 → 持仓看管 → 告警
ROLE_NAMES: dict[Role, str] = {
    Role.SCREENER: "选币",
    Role.COMMANDER: "军师团",
    Role.BOOK_READER: "盘口员",
    Role.PREDICTOR: "预测员 Kronos",
    Role.SENTINEL: "哨兵",
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


Progress = Literal["err", "doing", "done", "wait"]


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
    doing: str | None = None
    """现在正在做的事（团队页流水线高亮这一步）；没在忙就是 None。见 ``_doing``。"""
    progress: Progress = "wait"
    """流程图上的点：出错 > 正在做 > 这一轮做完 > 等待。见 ``_progress``。"""


class TeamPage(BaseModel):
    roles: list[RoleCard]
    selected: Role
    records: list[TrailItem]
    """所选角色今天的记录，最新在前。"""
    meetings: list[MeetingSummary] = []
    """选中军师团时：每个币最近一场会（data/meeting_live/），正在开的在前。"""
    market: MeetingSummary | None = None
    """最近一场总盘会（挑哪些币上会、盘口员盯哪些币）；流程图上单独一个节点。"""
    market_progress: Progress = "wait"
    """总盘会节点的点：开会中 = 正在做，今天开完 = 做完，失败 / 中断 = 出错。"""
    predictor_mode: Literal["on_demand", "all_symbols"] | None = None
    """预测员算哪些币（``[predictor] mode``）。"""
    gate_thresholds: dict[str, float] = {}
    """拍板门槛（fast_loop 的 min_p_genuine / min_p_take / min_p_direction / max_p_toxic），
    拍板记录详情里标出哪一问没过。"""
    snapshot: dict[str, Any] | None = None
    """选中盘口员时：data/market_snapshot.json（逐币行情 + 各路推送今天的条数）。"""


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


ACTIVE_S = 120
"""角色这么多秒内写过决策记录，就算“正在忙”（团队页高亮这一步）。"""
_HOLDING_ROLES = (Role.LIFEBOAT, Role.KEEPER, Role.PATROL, Role.RECONCILER)
"""③ 第三层：有持仓时一直在干活，没持仓时等待。"""
_WATCHERS = {Role.BOOK_READER: "盯着行情", Role.SENTINEL: "盯着信号"}
"""主程序在跑、心跳正常时一直在干活的角色（常驻）。"""


def _doing(
    role: Role,
    records: list[DecisionRecord],
    meetings: list[MeetingSummary],
    holding: int,
    now: datetime,
) -> str | None:
    """这一步现在在做什么：正在开的会 > 刚写的记录 > 盯着的持仓；都没有 = None（不猜）。

    常驻的盘口员 / 哨兵在 ``build_team`` 里按心跳补上“盯着行情 / 盯着信号”。"""
    if role is Role.COMMANDER:
        live = [m for m in meetings if m.status == "running"]
        if live:
            names = "、".join(
                "总盘会" if m.symbol == MARKET else m.symbol.removesuffix("USDT") for m in live[:3]
            )
            return f"开会中：{names}{f' 等 {len(live)} 场' if len(live) > 3 else ''}"
    last = next((r for r in reversed(records) if r.role is role), None)
    if last and (now - last.ts).total_seconds() <= ACTIVE_S:
        text = last.summary if len(last.summary) <= 40 else last.summary[:40] + "…"
        return f"刚刚：{text}"
    if role in _HOLDING_ROLES and holding:
        return f"盯着 {holding} 笔持仓"
    return None


SIGNAL_CHAIN = (Role.GATE, Role.RISK_GUARD, Role.EXECUTOR)
"""哨兵发出信号后依次经过的角色（按 signal_id 串起来）；没信号时它们都在等。"""
_RAN_TODAY = (Role.SCREENER, Role.COMMANDER, Role.PREDICTOR)
"""① 定时干活的：今天跑过一轮 = 完成。"""


def _healthy(card: RoleCard, st: ModuleStatus | None) -> bool:
    return st is not None and (
        card.state == "ok" or (card.state == "slow" and st.health is Health.DEGRADED)
    )


def _progress(
    card: RoleCard,
    st: ModuleStatus | None,
    chain: set[Role] | None,
    holding: int,
    acted_today: bool,
    now: datetime,
    tz: str,
) -> Progress:
    """流程图的点 = 本轮流程走到哪（设计稿 team-flow/handoff.md）；心跳好坏另用角标，不看这里。

    出错 > 正在做 > 这一轮做完 > 等待：
    ① 选币 / 军师团 / 预测员今天跑过 = 做完；盘口员、哨兵心跳正常 = 一直在盯；
    ② 拍板人 / 保镖 / 执行器只跟本轮（最新作战卡之后）最新一个信号：链上有的 = 做完，其余 = 等待；
    ③ 管家 / 巡逻兵 / 对账员 / 保命止损有持仓 = 正在做，没持仓 = 等待；报警员今天推过 = 做完。"""
    if card.state == "bad":
        return "err"
    if card.doing:
        return "doing"
    role = card.role
    if card.state == "off" or not _healthy(card, st) or st is None:
        return "wait"
    if role in SIGNAL_CHAIN:
        return "done" if chain and role in chain else "wait"
    if role in _RAN_TODAY:
        ran = _opt_time(st, "last_run_at") or st.ts
        return "done" if local_day(ran, tz) == local_day(now, tz) else "wait"
    if role is Role.ALERTING:
        return "done" if acted_today else "wait"
    return "wait"


def _round_start(records: list[DecisionRecord]) -> datetime | None:
    """本轮从最新一张作战卡发出算起；今天还没出卡就是 None（今天整天算一轮）。"""
    issued = [
        r.ts
        for r in records
        if r.role is Role.COMMANDER and r.card_id and r.outcome is Outcome.ACTION
    ]
    return issued[-1] if issued else None


def _latest_chain(records: list[DecisionRecord], since: datetime | None = None) -> set[Role] | None:
    """本轮最新一个信号走到了哪些角色；本轮还没有信号就是 None（② 全部等待）。"""
    last = next((r for r in reversed(records) if r.signal_id), None)
    if last is None:
        return None
    mine = [r for r in records if r.signal_id == last.signal_id]
    if since is not None and min(r.ts for r in mine) < since:
        return None
    return {r.role for r in mine}


def build_team(
    files: PanelFiles,
    now: datetime,
    selected: Role | None,
    predictor_mode: Literal["on_demand", "all_symbols"] | None = None,
    gate_thresholds: dict[str, float] | None = None,
) -> TeamPage:
    status = files.module_status()
    records = files.decisions_today(now)
    run = _run_info(files, status, now)
    meetings = list_meetings(files.root, now)
    holding = len(files.ledger().live())
    chain = _latest_chain(records, _round_start(records))
    acted = {r.role for r in records}
    cards = []
    for role in ROLE_NAMES:
        st = status.get(role.value)
        card = _role_card(role, st, now, run)
        card.doing = _doing(role, records, meetings, holding, now)
        if card.doing is None and role in _WATCHERS and run.running and _healthy(card, st):
            card.doing = _WATCHERS[role]
        card.progress = _progress(card, st, chain, holding, role in acted, now, run.tz)
        cards.append(card)
    chosen = selected or Role.GATE
    mine = [_item(r) for r in reversed(records) if r.role is chosen][:TEAM_RECORD_LIMIT]
    meets = meetings if chosen is Role.COMMANDER else []
    market = next((m for m in meetings if m.symbol == MARKET), None)
    return TeamPage(
        roles=cards,
        selected=chosen,
        records=mine,
        meetings=meets,
        predictor_mode=predictor_mode,
        gate_thresholds=gate_thresholds or {},
        snapshot=files.market_snapshot() if chosen is Role.BOOK_READER else None,
        market=market,
        market_progress=_market_progress(market, now, run.tz),
    )


def _market_progress(m: MeetingSummary | None, now: datetime, tz: str) -> Progress:
    if m is None:
        return "wait"
    if m.status == "running":
        return "doing"
    if m.status in ("failed", "stale"):
        return "err"
    end = m.finished_at or m.updated_at
    return "done" if end and local_day(end, tz) == local_day(now, tz) else "wait"


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


_SCHEDULE_FIELDS = ("last_run_at", "next_run_at", "symbol_count")


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
        if nxt is None and st.headline:  # 模块自己说了卡在哪一步（例如预测员补 K 线中）
            return "slow", f"准备中：{st.headline}"
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
        # 定时任务（选币）的上次 / 下次 / 币数已经写进状态，摘要再写一遍就重复了
        headline=""
        if any(getattr(st, n, None) is not None for n in _SCHEDULE_FIELDS)
        else st.headline,
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


class PnlPeriod(BaseModel):
    """一段时间里钱是怎么赚 / 亏的。负数 = 亏 / 花出去。"""

    key: Literal["today", "7d", "all"]
    label: str
    trading_usdt: float
    """平仓盈亏（扣手续费前）。"""
    fees_usdt: float
    """手续费（负数）。"""
    funding_usdt: float | None
    """资金费（可正可负）；没有币安收入流水时拿不到。"""
    ai_usd: float
    """AI 调用花费（美元，按 1 U 算）。"""
    net_usdt: float
    """交易盈亏 + 手续费 + 资金费 − AI 费（资金费拿不到时不算它）。"""


AiRange = Literal["today", "7d", "all"]
AiKind = Literal["commander", "market", "jev", "other"]
AI_KIND_NAMES: dict[str, str] = {
    "commander": "军师团",
    "market": "总盘会",
    "jev": "Jev",
    "other": "其他",
}
AI_ROW_LIMIT = 300
"""明细最多给前端这么多条（最新在前）；小计按全部记录算。"""


class AiCall(BaseModel):
    """一次花了钱（或失败）的 AI 调用，来自决策日志。"""

    ts: datetime
    kind: AiKind
    who: str
    symbol: str | None
    cost_usd: float
    tokens: int | None
    """输入 + 输出 tokens；这条记录没写就是 None（面板显示“没记”）。"""
    result: Literal["ok", "failed", "timeout"]
    detail: str
    estimated: bool = False
    """失败的会拿不到实际费用时，慢环按 token 估的（记录里 ``output.cost_estimated``）。"""
    input_tokens: int | None = None
    cached_tokens: int | None = None
    """命中缓存的输入 tokens（记录里 ``output.usage.cached_tokens``）；旧记录没有就是 None。"""


class AiGroup(BaseModel):
    kind: AiKind
    name: str
    cost_usd: float
    calls: int


class AiCostDetail(BaseModel):
    range: AiRange
    since: date | None
    """这段从哪天算起（累计 = 最早一份决策日志那天）。"""
    total_usd: float
    groups: list[AiGroup]
    rows: list[AiCall]
    row_count: int
    input_tokens: int = 0
    """有缓存记录的调用的输入 tokens 合计（缓存命中率的分母）。"""
    cached_tokens: int = 0
    cache_hit_pct: float | None = None
    """缓存命中 % = cached / input（只算记了 cached_tokens 的调用）；一条都没记就是 None。"""


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
    pnl: list[PnlPeriod] = []
    """今天 / 近 7 天 / 累计。"""
    pnl_source: Literal["exchange", "ledger"] = "ledger"
    """exchange = 币安收入流水（data/income_daily.json，主程序每 5 分钟拉）；
    ledger = 账本（已平仓的单），没有资金费。"""
    pnl_since: date | None = None
    """“累计”从哪天算起。"""
    ai: AiCostDetail | None = None
    """AI 费用明细（资金页下半部分）。"""


def build_funds(
    files: PanelFiles,
    hb: HbSnapshot,
    risk: RiskConfig,
    now: datetime,
    ai_range: AiRange = "today",
) -> FundsPage:
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
    week = files.decisions_since(_range_start(now, tz, "7d"), now, _PAID)
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
    pnl, source, since = _pnl_periods(files, ledger, now, tz)
    return FundsPage(
        pnl=pnl,
        pnl_source=source,
        pnl_since=since,
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
        ai=build_ai_costs(files, now, tz, ai_range),
    )


def _int_or_none(v: object) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _ai_call(r: DecisionRecord) -> AiCall | None:
    """决策日志一条 → 一次 AI 调用；没花钱也没出错的（规则拒绝、预测员本地算）不算。"""
    if r.cost_usd <= 0 and r.outcome is not Outcome.ERROR:
        return None
    coin = r.symbol.removesuffix("USDT") if r.symbol else None
    kind: AiKind
    if r.role is Role.COMMANDER:
        if r.card_id and r.outcome is Outcome.ACTION:
            kind, who = "market", "总盘会（挑币上会）"
        else:
            kind, who = "commander", f"军师团 · {coin} 会议" if coin else "军师团"
    elif r.role is Role.GATE:
        kind, who = "jev", "Jev 拍板"
    elif r.role is Role.KEEPER:
        kind, who = "jev", "Jev 持仓审查"
    else:
        kind, who = "other", ROLE_NAMES.get(r.role, r.role.value)
    result: Literal["ok", "failed", "timeout"] = "ok"
    if r.outcome is Outcome.ERROR:
        result = "timeout" if "超时" in r.summary else "failed"
    usage = r.output.get("usage")
    tokens = None
    in_tok = cached = None
    if isinstance(usage, dict):
        in_tok, cached = (
            _int_or_none(usage.get("input_tokens")),
            _int_or_none(usage.get("cached_tokens")),
        )
        parts = [usage.get(k) for k in ("input_tokens", "output_tokens")]
        nums = [v for v in parts if isinstance(v, int) and not isinstance(v, bool)]
        tokens = sum(nums) if nums else None
    return AiCall(
        ts=r.ts, kind=kind, who=who, symbol=r.symbol, cost_usd=r.cost_usd, tokens=tokens,
        result=result, detail=r.summary, estimated=r.output.get("cost_estimated") is True,
        input_tokens=in_tok, cached_tokens=cached,
    )  # fmt: skip


def build_ai_costs(files: PanelFiles, now: datetime, tz: str, rng: AiRange) -> AiCostDetail:
    """资金页的 AI 费用明细：谁、哪个币、花多少、多少 tokens、成没成，按类别小计。"""
    since: date | None
    if rng == "all":
        since = files.first_log_day()
        day0 = since or local_day(now, tz)
        start = datetime.combine(day0, time(), tzinfo=ZoneInfo(tz)).astimezone(UTC)
    else:
        start = _range_start(now, tz, rng)
        since = local_day(start, tz)
    calls = [c for r in files.decisions_since(start, now, _PAID) if (c := _ai_call(r))]
    groups = []
    kinds: tuple[AiKind, ...] = ("commander", "market", "jev", "other")
    for kind in kinds:
        mine = [c for c in calls if c.kind == kind]
        if mine or kind != "other":
            groups.append(
                AiGroup(
                    kind=kind,
                    name=AI_KIND_NAMES[kind],
                    cost_usd=sum(c.cost_usd for c in mine),
                    calls=len(mine),
                )
            )
    cache_calls = [c for c in calls if c.cached_tokens is not None and c.input_tokens]
    in_sum = sum(c.input_tokens or 0 for c in cache_calls)
    cached_sum = sum(c.cached_tokens or 0 for c in cache_calls)
    return AiCostDetail(
        input_tokens=in_sum,
        cached_tokens=cached_sum,
        cache_hit_pct=round(cached_sum / in_sum * 100, 1) if in_sum else None,
        range=rng,
        since=since,
        total_usd=sum(c.cost_usd for c in calls),
        groups=groups,
        rows=list(reversed(calls))[:AI_ROW_LIMIT],
        row_count=len(calls),
    )


_PAID = frozenset({Role.GATE, Role.KEEPER, Role.COMMANDER, Role.PREDICTOR})


def _income_days(raw: object) -> dict[date, tuple[float, float, float]]:
    """income_daily.json 的 days：当地日 → (realized_pnl, commission, funding_fee)。坏的天跳过。"""
    out: dict[date, tuple[float, float, float]] = {}
    days = raw.get("days") if isinstance(raw, dict) else None
    for k, v in (days or {}).items() if isinstance(days, dict) else []:
        try:
            d = date.fromisoformat(k)
            row = tuple(float(v[f]) for f in ("realized_pnl", "commission", "funding_fee"))
        except (TypeError, ValueError, KeyError):
            continue
        out[d] = (row[0], row[1], row[2])
    return out


def _pnl_periods(
    files: PanelFiles, ledger: Ledger, now: datetime, tz: str
) -> tuple[list[PnlPeriod], Literal["exchange", "ledger"], date | None]:
    """今天 / 近 7 天 / 累计的 交易盈亏、手续费、资金费、AI 费、净盈亏。

    有 income_daily.json 就按币安收入流水（含资金费）；没有就按账本（已平仓的单），资金费不知道。
    """
    today = local_day(now, tz)
    income = _income_days(files.income_daily())
    closed = [
        p
        for p in ledger.positions
        if p.closed_at is not None
        and p.realized_pnl_usdt is not None  # 测试单也是真金白银，和币安流水口径一致
    ]
    if income:
        since: date | None = min(income)
    else:
        since = min((local_day(p.closed_at, tz) for p in closed if p.closed_at), default=None)
    # AI 费从最早的决策日志算起（可能早于第一笔交易），“累计”起点取两者更早的那天
    first = min(since or today, today - timedelta(days=6), files.first_log_day() or today)
    start = datetime.combine(first, time(), tzinfo=ZoneInfo(tz)).astimezone(UTC)
    ai_by_day: dict[date, float] = {}
    for r in files.decisions_since(start, now, _PAID):
        d = local_day(r.ts, tz)
        ai_by_day[d] = ai_by_day.get(d, 0.0) + r.cost_usd
    first_ai = min((d for d, v in ai_by_day.items() if v > 0), default=None)
    if first_ai is not None:
        since = min(since or first_ai, first_ai)
    periods: list[tuple[Literal["today", "7d", "all"], str, date]] = [
        ("today", "今天", today),
        ("7d", "近 7 天", today - timedelta(days=6)),
        ("all", "累计", since or today),
    ]
    out = []
    for key, label, lo in periods:
        ai = sum(v for d, v in ai_by_day.items() if lo <= d <= today)
        funding: float | None
        if income:
            rows = [v for d, v in income.items() if lo <= d <= today]
            trading = sum(r[0] for r in rows)
            fees = sum(r[1] for r in rows)
            funding = sum(r[2] for r in rows)
        else:
            mine = [p for p in closed if p.closed_at and lo <= local_day(p.closed_at, tz) <= today]
            fees = -sum(p.fees_usdt for p in mine)
            trading = sum(p.realized_pnl_usdt or 0.0 for p in mine) - fees
            funding = None
        net = trading + fees + (funding or 0.0) - ai
        out.append(PnlPeriod(key=key, label=label, trading_usdt=trading, fees_usdt=fees,
                             funding_usdt=funding, ai_usd=ai, net_usdt=net))  # fmt: skip
    return out, ("exchange" if income else "ledger"), since


# ---------------------------------------------------------------- 选币


CategorySource = Literal["pool", "slow", "default"]
"""这一项的值从哪来：选币页设的（pool_overrides.json）/ slow.toml / 都没写，用默认。"""


class PoolCategory(BaseModel):
    category: str
    enabled: bool
    """现在开着没有（选币页 > slow.toml > 默认开）。"""
    enabled_from: CategorySource
    cap: int
    """最多进池几个（选币页 > slow.toml 这一类 > 每类默认上限）。"""
    cap_from: CategorySource
    in_pool: int
    """上次选币这一类进池了几个（选币页改了要等下一次选币才变）。"""
    total: int
    """上次选币这一类一共看了几个。"""


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
    categories: list[PoolCategory] = []
    """每类币的开关和进池上限，固定顺序（``Category``）。"""
    default_category_cap: int = 30
    slow_error: str | None = None
    """slow.toml 读不出：“slow.toml 的值”按默认显示，以这句为准。"""


def _pool_categories(
    entries: list[SymbolPoolEntry], ov: PoolOverrides | None, scr: ScreenerSection
) -> list[PoolCategory]:
    out = []
    for c in Category:
        rule = scr.categories.get(c)
        mine_on = ov.category_enabled.get(c) if ov else None
        mine_cap = ov.category_caps.get(c) if ov else None
        slow_cap = rule.cap if rule else None
        enabled_from: CategorySource = (
            "pool" if mine_on is not None else "slow" if rule is not None else "default"
        )
        cap_from: CategorySource = (
            "pool" if mine_cap is not None else "slow" if slow_cap is not None else "default"
        )
        mine = [e for e in entries if e.category is c]
        out.append(
            PoolCategory(
                category=c.value,
                enabled=mine_on if mine_on is not None else (rule.enabled if rule else True),
                enabled_from=enabled_from,
                cap=next(
                    v for v in (mine_cap, slow_cap, scr.default_category_cap) if v is not None
                ),
                cap_from=cap_from,
                in_pool=sum(1 for e in mine if e.in_pool),
                total=len(mine),
            )
        )
    return out


def build_pool(
    files: PanelFiles, screener: ScreenerSection | None = None, slow_error: str | None = None
) -> PoolPage:
    """``screener``：slow.toml 的 ``[screener]``；读不出时给 None 和 ``slow_error``，按默认显示。"""
    pool = read_pool(files.root)
    ov = read_pool_overrides(files.root)
    entries = sorted(pool.entries, key=lambda e: (not e.in_pool, -e.score)) if pool else []
    scr = screener or ScreenerSection()
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
        categories=_pool_categories(entries, ov, scr),
        default_category_cap=scr.default_category_cap,
        slow_error=slow_error,
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
    multi: bool = False
    """多选：值是 ``choices`` 的子集（列表）。"""
    pattern: str | None = None
    """自由填写的字样要符合的格式（例如开会定点 ``08:00``）。"""


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
    budget: list[SettingItem] = []
    """军师团每日花费上限（config/slow.toml 的 [budget]，只放日上限）。"""
    """节奏配置可能造成换卡空档时的提示（只提示，不拒绝）。"""
    screener: list[SettingItem] = []
    """选币规则（config/slow.toml 的 [screener]）：门槛、打分满分线、权重。"""
    tiers: list[SettingItem] = []
    """分档交易（config/slow.toml 的 [screener.tiers]）。"""
    screener_cautions: list[str] = []
    """选币规则比推荐冒险的项（只提示，不拒绝）。"""
    tiers_cautions: list[str] = []
    """分档交易设置里用不上或不像设计稿的地方（只提示）。"""
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


SCREENER_RECOMMENDED: dict[str, tuple[float, str]] = {
    "min_quote_volume_usdt": (20_000_000.0, "冷门币进出容易滑点"),
    "min_depth_usdt": (100_000.0, "太浅的币大单吃不下"),
    "min_listed_days": (7, "新币波动和价差不稳"),
    "min_range_24h_pct": (1.5, "波动太小赚不回手续费"),
    "max_range_24h_pct": (30.0, "波动过大止损容易被针扫"),
}
"""选币门槛的推荐值（和 config/slow.example.toml 的注释一致）；只提示，填什么都收。"""


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
                multi=bool(spec and spec.multi),
                pattern=spec.pattern if spec else None,
            )
        )
    return out


def _plain(v: object) -> object:
    if isinstance(v, tuple):
        return [_plain(x) for x in v]
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
    slow = slow or slow_defaults(app.profile)
    tier_cautions = slow.screener.tiers.cautions()
    page = SettingsPage(
        mode=app.mode.value,
        risk=_items(risk, "risk"),
        risk_cautions=risk.cautions(equity_usdt),
        recommend_basis_usdt=equity_usdt,
        category_caps={k.value: v for k, v in risk.category_exposure_x.items()},
        app=_items(app, "app", frozenset({"mode"})),
        fast_loop=_items(app.fast_loop, "fast_loop"),
        predictor=_items(app.predictor, "predictor"),
        schedule=_items(slow.schedule, "schedule"),
        schedule_caution=slow.schedule.caution(),
        budget=_items(
            slow.budget, "budget", frozenset(BudgetConfig.model_fields) - {"daily_cost_limit_usd"}
        ),
        screener=_items(slow.screener, "screener"),
        tiers=_items(slow.screener.tiers, "tiers"),
        screener_cautions=[
            x for x in slow.screener.cautions(risk.max_spread_bps) if x not in tier_cautions
        ],
        tiers_cautions=tier_cautions,
        keys=keys,
        key_needs=key_needs(
            app.mode.value,
            app.fast_loop.jev_provider,
            slow.llm.provider,
        ),
        password_set=password_set,
        live_armed=armed,
        profile=profile,
    )
    recs = recommended_caps(equity_usdt)
    for item in page.risk:
        if rec := recs.get(item.key):
            item.recommended, item.recommended_why = rec.value, rec.why
        elif item.key == "max_slippage_bps" and app.hold in profiles.SLIPPAGE_BPS:
            item.recommended = profiles.SLIPPAGE_BPS[app.hold]
            item.recommended_why = f"按最长持仓 {app.hold}：约止损距离的 1/4（只拦不利方向）"
    for item in page.screener:
        if srec := SCREENER_RECOMMENDED.get(item.key):
            item.recommended, item.recommended_why = srec
    if profile:
        for section, hit in profile.pinned.items():
            for item in getattr(page, section):
                if item.key in hit:
                    item.pinned, item.profile_default = True, hit[item.key]
    return page
