"""总览以外各页的数据。口径和总览共用（``compute_kpis``、``position_views``）。"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from jiaoyi.contracts import (
    AgainstSignalEvent,
    AppConfig,
    Category,
    DecisionRecord,
    Health,
    ModuleStatus,
    Outcome,
    PositionStatus,
    RiskConfig,
    Role,
    SymbolPoolEntry,
)
from jiaoyi.contracts.risk_config import recommended_caps
from jiaoyi.core import profiles
from jiaoyi.core.pool_overrides import PoolOverrides, read_pool_overrides
from jiaoyi.core.recorder import local_day
from jiaoyi.core.secrets import KeyNeed, key_needs
from jiaoyi.panel.actions import PAPER_ONLY_SWITCHES, ProfileInfo, Section, editable_fields
from jiaoyi.panel.files import Ledger, PanelFiles, read_live_gate, read_pool
from jiaoyi.panel.hb import HbSnapshot
from jiaoyi.panel.meaning import SettingsMeaning, build_meaning
from jiaoyi.panel.meetings import MARKET, MeetingSummary, list_meetings
from jiaoyi.panel.overview import (
    STALE_S,
    LampState,
    PositionView,
    PriceSrc,
    _card,
    compute_kpis,
    fee_rate_bps,
    position_views,
)
from jiaoyi.slow.review import CardJournal, JournalRow
from jiaoyi.slow.screener import FALLBACK_MAJORS
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
    tag: str | None = None
    """额外标签，例如管家的「反向急查」（不是例行复查）。"""


def is_against_check(r: DecisionRecord) -> bool:
    """管家这条是不是反向信号急查：先认决策记录的 ``output.against``，旧记录认理由里的字样。"""
    return r.role is Role.KEEPER and ("against" in r.output or "反向信号急查" in r.summary)


class GroupExposure(BaseModel):
    category: str
    exposure_x: float
    cap_x: float
    notional_usdt: float
    cap_usdt: float | None
    """上限折合金额 = cap_x × 现在的本金；没拿到本金就是 None。"""


class PositionsPage(BaseModel):
    positions: list[PositionView]
    unrealized_usdt: float | None
    lifeboats_ok: int
    groups: list[GroupExposure]
    trails: dict[str, list[TrailItem]]
    """position_id → 这笔从信号到现在的每一步（今天的记录）。"""
    price_env: Literal["live", "demo"] | None = None
    """现价从哪个盘子来：live 主网；demo = paper / testnet 撮合的币安 Demo；None = 没接实时行情。"""


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
    last_run_at: datetime | None = None
    next_run_at: datetime | None = None
    summary: str | None = None
    """模块报的「刚刚做了什么」；没报就是 None（面板不显示这一行，不猜）。"""
    detail: list[str] = []
    """模块报的每币一行（哨兵：量比 / 主动占比 / 离阈值多远 / 跳过原因）。
    哨兵只留现在作战卡上的币和持仓币（它会列启动以来看过的所有币）。"""
    detail_hidden: int = 0
    """哨兵：掉出作战卡、也没持仓、所以没显示的币数。"""


class AgainstCheck(BaseModel):
    """一次反向信号急查：哨兵哪个时候看到什么，管家怎么判的（团队页管家卡固定显示 30 分钟）。"""

    symbol: str
    ts: datetime
    """哨兵看到反向信号的时间。"""
    desc: str = ""
    strong: bool = False
    verdict: Literal["close", "hold"] | None = None
    """None = 管家还没查完。"""
    p_close: float | None = None
    reason: str | None = None
    """管家这次的理由（决策日志原话，去掉「币 拿住：」前缀）。"""
    checked_at: datetime | None = None
    failed: bool = False
    """管家这次没问成（行情没有 / 决策模型出错），按规矩维持现状。"""


class PredictorMeeting(BaseModel):
    """预测员连续看反作战卡、申请的单币临时会（作战卡流水里 ``event:predictor_against_card``）。"""

    symbol: str
    ts: datetime
    """临时会出结论的时间。"""
    bias: str
    """会上给的新方向。"""


class PromptsInfo(BaseModel):
    """军师团现在用的提示词是哪一档（data/prompts_active.json）；原文走 /api/prompts。"""

    hold: str
    """按哪个最长持仓档写的提示词。"""
    style: str
    """short / mid / long（短线 / 中线 / 长线写法）。"""
    written_at: datetime
    """引擎启动写文件的时间（= 这份提示词从什么时候开始用）。"""


def prompts_info(files: PanelFiles) -> PromptsInfo | None:
    got = files.prompts_active()
    if got is None:
        return None
    raw, at = got
    hold, style = raw.get("hold"), raw.get("style")
    if not isinstance(hold, str) or not isinstance(style, str):
        return None
    return PromptsInfo(hold=hold, style=style, written_at=at)


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
    market_role: RoleCard | None = None
    """总盘会的模块状态（``Role.MARKET``：上次 / 下次 / 挑了几个）；主程序没报过就是 None。"""
    snapshot: dict[str, Any] | None = None
    """选中盘口员时：data/market_snapshot.json（逐币行情 + 各路推送今天的条数）。"""
    against: list[AgainstSignalEvent] = []
    """最近 30 分钟哨兵交出去的反向信号（新的在前）；流程图上对应的线平时不画，有才亮。"""
    predictor_meetings: list[PredictorMeeting] = []
    """最近 30 分钟预测员申请、已经开完的临时会（新的在前）；流程图上预测员 → 军师团的线有才亮。"""
    against_checks: list[AgainstCheck] = []
    """最近 30 分钟叫管家急查的反向信号和管家的判断（新的在前），不被例行复查冲掉。"""
    prompts: PromptsInfo | None = None
    """军师团现在用的提示词档（团队页军师团 / 总盘会显示）。"""


def build_positions(
    files: PanelFiles,
    hb: HbSnapshot,
    risk: RiskConfig,
    now: datetime,
    prices: dict[str, tuple[float, PriceSrc]] | None = None,
) -> PositionsPage:
    ledger = files.ledger()
    records = files.decisions_today(now)
    live = ledger.live()
    views = position_views(
        live, records, hb, risk.taker_fee_bps, fee_rate_bps(ledger.positions), prices
    )
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
                notional_usdt=notional,
                cap_usdt=risk.category_exposure_x[cat] * equity if equity else None,
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


AGAINST_SHOW = timedelta(minutes=30)


def _recent_against(status: dict[str, ModuleStatus], now: datetime) -> list[AgainstSignalEvent]:
    """哨兵心跳里最近 30 分钟、真的交出去了（叫了管家或申请了临时会）的反向信号，新的在前。"""
    st = status.get(Role.SENTINEL.value)
    if st is None:
        return []
    hits = [e for e in st.against_signals if e.actions and now - e.ts <= AGAINST_SHOW]
    return sorted(hits, key=lambda e: e.ts, reverse=True)


def _same(r: DecisionRecord, e: AgainstSignalEvent) -> bool:
    """管家记录对的是不是这次信号：带 ``output.against.ts`` 的按时间对，旧记录不带就算。"""
    ag = r.output.get("against")
    ts = ag.get("ts") if isinstance(ag, dict) else None
    if not isinstance(ts, str):
        return True
    try:
        return datetime.fromisoformat(ts) == e.ts
    except ValueError:
        return True


PREDICTOR_EVENT = "event:predictor_against_card"


TAIL_BYTES = 256 * 1024
"""出卡流水只增不减；30 分钟内的行一定在末尾，只读最后这么多（够几千行）。"""


def _journal_tail(files: PanelFiles) -> list[JournalRow]:
    """出卡流水末尾的行（团队页每 5 秒读一次，不整份读）；读不了就是空。"""
    path = CardJournal(files.root).path
    try:
        with path.open("rb") as f:
            size = f.seek(0, 2)
            f.seek(max(0, size - TAIL_BYTES))
            raw = f.read()
    except OSError:
        return []
    lines = raw.decode("utf-8", errors="replace").splitlines()
    if size > TAIL_BYTES:
        lines = lines[1:]  # 第一行多半被截了半截
    rows = []
    for ln in lines:
        try:
            rows.append(JournalRow.model_validate_json(ln))
        except ValueError:
            continue
    return rows


def _predictor_meetings(files: PanelFiles, now: datetime) -> list[PredictorMeeting]:
    rows = [
        r
        for r in _journal_tail(files)
        if r.refresh_reason == PREDICTOR_EVENT
        and timedelta(0) <= now - r.created_at <= AGAINST_SHOW
    ]
    rows.sort(key=lambda r: r.created_at, reverse=True)
    return [PredictorMeeting(symbol=r.symbol, ts=r.created_at, bias=r.bias) for r in rows]


def _against_checks(
    events: list[AgainstSignalEvent], records: list[DecisionRecord]
) -> list[AgainstCheck]:
    """叫了管家急查的反向信号，各配上管家那次判断：同一笔仓、信号之后、理由里带「反向信号急查」的第一条。"""
    keeper = sorted(
        (r for r in records if is_against_check(r)),
        key=lambda r: r.ts,
    )
    out = []
    for e in events:
        if "keeper_recheck" not in e.actions:
            continue
        mine = [
            r for r in keeper if r.position_id == e.position_id and r.ts >= e.ts and _same(r, e)
        ]
        # 没查成的急查会留到下一轮再查（记录里带同一个 against）：有查成的取最后一次查成的
        ok = [r for r in mine if r.outcome is not Outcome.ERROR]
        hit = (ok or mine)[-1] if mine else None
        verdict = e.keeper_verdict
        p_close = reason = None
        if hit is not None:
            act = hit.output.get("action")
            verdict = act if act in ("close", "hold") else verdict
            p = hit.output.get("p_close")
            p_close = float(p) if isinstance(p, int | float) else None
            reason = hit.summary.split("：", 1)[-1]
        out.append(
            AgainstCheck(
                symbol=e.symbol,
                ts=e.ts,
                desc=e.desc,
                strong=e.strong,
                verdict=verdict,
                p_close=p_close,
                reason=reason,
                checked_at=hit.ts if hit is not None else None,
                failed=hit is not None and hit.outcome is Outcome.ERROR,
            )
        )
    return out


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
    live = files.ledger().live()
    holding = len(live)
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
        if role is Role.SENTINEL and card.detail:
            held = {p.symbol for p in live}
            cv = _card(records, now, held)
            keep = held | ({r.symbol for r in cv.symbols} if cv is not None and cv.valid else set())
            shown = [ln for ln in card.detail if ln.split(" ", 1)[0] in keep]
            card.detail_hidden = len(card.detail) - len(shown)
            card.detail = shown
        cards.append(card)
    chosen = selected or Role.GATE
    mine = [_item(r) for r in reversed(records) if r.role is chosen][:TEAM_RECORD_LIMIT]
    meets = meetings if chosen is Role.COMMANDER else []
    market = next((m for m in meetings if m.symbol == MARKET), None)
    against = _recent_against(status, now)
    return TeamPage(
        roles=cards,
        selected=chosen,
        records=mine,
        meetings=meets,
        predictor_mode=predictor_mode,
        gate_thresholds=gate_thresholds or {},
        snapshot=files.market_snapshot() if chosen is Role.BOOK_READER else None,
        market_role=(
            _role_card(Role.MARKET, status[Role.MARKET.value], now, run)
            if Role.MARKET.value in status
            else None
        ),
        market=market,
        market_progress=_market_progress(market, now, run.tz),
        against=against,
        against_checks=_against_checks(against, records),
        predictor_meetings=_predictor_meetings(files, now),
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


def _opt_str(st: ModuleStatus, name: str) -> str | None:
    v = getattr(st, name, None)
    return v if isinstance(v, str) and v else None


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
    # 只在开会时报状态的（总盘会）：还没到下次计划时间就不算断心跳
    planned = nxt is not None and now <= nxt + timedelta(seconds=STALE_S)
    if age > timedelta(seconds=STALE_S) and not planned:
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
            role=role, name=ROLE_NAMES.get(role, "总盘会"), state=state, status=text, headline="",
            last_beat=None, last_error=None, calls=0, passed=0, rejected=0, errors=0,
            avg_latency_ms=0.0, cost_usd=0.0,
        )  # fmt: skip
    t = st.today
    return RoleCard(
        role=role,
        name=ROLE_NAMES.get(role, "总盘会"),
        state=state,
        status=text,
        last_run_at=_opt_time(st, "last_run_at"),
        next_run_at=_opt_time(st, "next_run_at"),
        summary=_opt_str(st, "summary"),
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
        detail=st.detail,
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
        tag="反向急查" if is_against_check(r) else None,
    )


# ---------------------------------------------------------------- 历史

HistoryRange = Literal["today", "7d", "30d", "all"]
REJECT_LIMIT = 300


class TradeRow(BaseModel):
    position_id: str
    signal_id: str
    symbol: str
    category: str = ""
    """主流币 / 山寨币 …（账本里开仓时的分组）。"""
    side: str
    quantity: float
    leverage: int
    entry_price: float | None
    exit_price: float | None = None
    """币安平仓成交均价；None = 还没按成交回填。"""
    stop_loss_price: float | None = None
    take_profit_price: float | None = None
    """下单时的止损 / 止盈（按开仓成交价回填后的）。"""
    opened_at: datetime
    closed_at: datetime
    close_reason: str | None
    pnl_usdt: float
    """净盈亏（已扣开平手续费）：按币安成交回填后是币安实扣，否则是 hummingbot 报的。"""
    fees_usdt: float
    pnl_missing: bool = False
    """没回填出场价、净盈亏和手续费又都是 0：多半是执行器没报数，不是真的 0。"""
    signal_kind: str | None
    test: bool = False
    """没有哨兵 / 拍板 / 保镖记录的单：冒烟测试或手动下的，不是机器人自己的交易。"""
    unfilled: bool = False
    """开仓没成交（账本 FAILED，如保证金不够 hummingbot 没下单）：只列出来，不算进任何统计。"""


class KindStats(BaseModel):
    kind: str
    trades: int
    win_rate: float | None
    pnl_usdt: float


class GroupStats(BaseModel):
    """一组成交了的单的成绩（按币、按多空、按信号都用它）。"""

    trades: int
    wins: int
    losses: int
    win_rate: float
    pnl_usdt: float
    """净盈亏（已扣开平手续费）。"""
    fees_usdt: float
    avg_r: float | None
    """平均每笔赚几个 R（R = 这笔开仓时的止损距离 × 数量）；没有止损价的单不算，全没有就是 None。"""
    fee_share: float | None
    """手续费占毛利（毛利 = 净盈亏 + 手续费）多少；毛利 ≤ 0 时是 None（毛利本身就亏了）。"""
    avg_hold_s: float
    """平均持有多久（秒）。"""


class SymbolSplit(GroupStats):
    """一个币里再按多空 / 信号拆开：``by`` = side / kind，``key`` = long / short / 信号类型。"""

    by: Literal["side", "kind"]
    key: str


class SymbolStats(GroupStats):
    """一个币在这段时间的成绩（只算成交了的单），给以后拉黑 / 加白参考。"""

    symbol: str
    longs: int
    shorts: int
    top_kind: str | None
    """做这个币最多的信号类型（测试单算 ``test``）。"""
    splits: list[SymbolSplit] = []
    """按多空、按信号拆开的成绩（笔数多的在前）。"""


def _group(ts: list[TradeRow]) -> dict[str, Any]:
    rs = []
    for t in ts:
        if t.entry_price and t.stop_loss_price:
            risk = abs(t.entry_price - t.stop_loss_price) * t.quantity
            if risk > 0:
                rs.append(t.pnl_usdt / risk)
    pnl = sum(t.pnl_usdt for t in ts)
    fees = sum(t.fees_usdt for t in ts)
    wins = sum(1 for t in ts if t.pnl_usdt > 0)
    return {
        "trades": len(ts),
        "wins": wins,
        "losses": sum(1 for t in ts if t.pnl_usdt < 0),
        "win_rate": wins / len(ts),
        "pnl_usdt": pnl,
        "fees_usdt": fees,
        "avg_r": sum(rs) / len(rs) if rs else None,
        "fee_share": fees / (pnl + fees) if pnl + fees > 0 else None,
        "avg_hold_s": sum((t.closed_at - t.opened_at).total_seconds() for t in ts) / len(ts),
    }


def _kind(t: TradeRow) -> str:
    return "test" if t.test else t.signal_kind or "unknown"


def _symbol_stats(sym: str, ts: list[TradeRow]) -> SymbolStats:
    splits: list[SymbolSplit] = []
    for by, key_of in (("side", lambda t: t.side), ("kind", _kind)):
        groups: dict[str, list[TradeRow]] = {}
        for t in ts:
            groups.setdefault(key_of(t), []).append(t)
        splits += [
            SymbolSplit(by=by, key=k, **_group(g))
            for k, g in sorted(groups.items(), key=lambda kv: -len(kv[1]))
        ]
    return SymbolStats(
        symbol=sym,
        longs=sum(1 for t in ts if t.side == "long"),
        shorts=sum(1 for t in ts if t.side == "short"),
        top_kind=Counter(_kind(t) for t in ts).most_common(1)[0][0],
        splits=splits,
        **_group(ts),
    )


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
    by_symbol: list[SymbolStats] = []
    """按币种统计，净盈亏从高到低。"""
    rows: list[TradeRow]
    """最新在前。"""
    test_trades: int = 0
    """范围内的测试单笔数；``include_tests`` 为假时它们不在 rows 和各项统计里。"""
    rejected: list[TrailItem]
    """范围内被拍板放弃 / 保镖拒绝的信号，最新在前，最多 ``REJECT_LIMIT`` 条。"""


def _range_start(now: datetime, tz: str, rng: HistoryRange) -> datetime:
    day0 = datetime.combine(local_day(now, tz), time(), tzinfo=ZoneInfo(tz))
    days = {"today": 0, "7d": 6, "30d": 29, "all": 0}[rng]
    return (day0 - timedelta(days=days)).astimezone(UTC)


def build_history(
    files: PanelFiles,
    risk: RiskConfig,
    now: datetime,
    rng: HistoryRange,
    include_tests: bool = False,
) -> HistoryPage:
    start = _range_start(now, risk.day_reset_tz, rng)
    if rng == "all":  # 全部 = 从账本里最早一笔平仓算起
        start = min(
            (p.closed_at for p in files.ledger().positions if p.closed_at is not None),
            default=start,
        )
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
        unfilled = p.status is PositionStatus.FAILED
        pnl = 0.0 if unfilled else p.realized_pnl_usdt or 0.0
        assert p.closed_at is not None
        if not unfilled:
            cum += pnl
            peak = max(peak, cum)
            dd = max(dd, peak - cum)
            curve.append(CurvePoint(ts=p.closed_at, cum_pnl_usdt=cum))
        rows.append(
            TradeRow(
                position_id=p.position_id,
                signal_id=p.signal_id,
                symbol=p.symbol,
                category=p.category.value,
                side=p.side.value,
                quantity=p.quantity,
                leverage=p.leverage,
                entry_price=p.entry_price,
                exit_price=p.exit_price,
                stop_loss_price=p.stop_loss_price,
                take_profit_price=p.take_profit_price,
                opened_at=p.opened_at,
                closed_at=p.closed_at,
                close_reason=p.close_reason.value if p.close_reason else None,
                pnl_usdt=pnl,
                fees_usdt=p.fees_usdt,
                pnl_missing=not unfilled
                and p.exit_price is None
                and pnl == 0.0
                and p.fees_usdt == 0.0,
                signal_kind=kind_of.get(p.signal_id),
                test=test,
                unfilled=unfilled,
            )
        )
    real = [t for t in rows if not t.unfilled]
    wins = [t.pnl_usdt for t in real if t.pnl_usdt > 0]
    losses = [-t.pnl_usdt for t in real if t.pnl_usdt < 0]
    by: dict[str, list[float]] = {}
    for t in real:
        by.setdefault("test" if t.test else (t.signal_kind or "unknown"), []).append(t.pnl_usdt)
    per: dict[str, list[TradeRow]] = {}
    for t in real:
        if not t.pnl_missing:  # 没取到盈亏数的单不算，免得当成 0 拉低胜率
            per.setdefault(t.symbol, []).append(t)
    by_symbol = [_symbol_stats(sym, ts) for sym, ts in per.items()]
    by_symbol.sort(key=lambda x: -x.pnl_usdt)
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
        trades=len(real),
        win_rate=len(wins) / len(real) if real else None,
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
        by_symbol=by_symbol,
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
    commander_today = 0.0
    for r in files.decisions_since(start, now, _PAID):
        d = local_day(r.ts, tz)
        ai_by_day[d] = ai_by_day.get(d, 0.0) + r.cost_usd
        if d == today and r.role is Role.COMMANDER:
            commander_today += r.cost_usd
    # 军师团今天的花费以慢环预算文件为准：开会前预记、失败和还在开的会都算，日志要等会开完才有
    budget = files.slow_budget_cost(today)
    if budget is not None and budget > commander_today:
        ai_by_day[today] = ai_by_day.get(today, 0.0) - commander_today + budget
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
    enabled_base: bool = True
    """不算选币页的话是开还是关（slow.toml > 默认开）：「恢复默认」后的值。"""
    cap_base: int = 30
    """不算选币页的话最多进池几个（slow.toml > 每类默认上限）：「恢复默认」后的值。"""
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
                enabled_base=rule.enabled if rule else True,
                cap_base=slow_cap if slow_cap is not None else scr.default_category_cap,
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
    code_default: object = None
    """不随档位变的键：程序默认值（``resettable`` 时显示）。"""
    resettable: bool = False
    """不随档位变、现在的值和程序默认不同（配置文件写了）：可以「恢复默认」（删掉那一行）。"""
    recommended: float | None = None
    """硬上限的推荐值（按资金算，见 ``recommended_caps``）；只提示，填什么都收。"""
    recommended_why: str = ""
    multi: bool = False
    """多选：值是 ``choices`` 的子集（列表）。"""
    paper_only: bool = False
    """只有模拟盘能打开的开关；testnet / live 下面板只读。"""
    pattern: str | None = None
    """自由填写的字样要符合的格式（例如开会定点 ``08:00``）。"""
    names: bool = False
    """币名名单：自由填，空格隔开，可以一个都没有。"""


class MajorRow(BaseModel):
    name: str
    rank: int | None = None
    """市值名次；手动加入 / 固定名单里的是 None。"""
    why: str
    """「市值第 3 名」/「你手动加入」/「固定名单」。"""


class MajorsInfo(BaseModel):
    """现在算「主流币」的名单（data/majors.json + slow.toml 的手动加入 / 排除）。"""

    top_n: int
    rows: list[MajorRow]
    excluded: list[str] = []
    """你手动排除、本来会算主流的币（按山寨币算）。"""
    fetched_at: datetime | None = None
    fetched_top_n: int | None = None
    """缓存是按前几名拉的；和 ``top_n`` 不同时，下一次选币会重拉。"""
    fallback: bool = False
    """用的是写死的 7 个（major_top_n = 0，或还没拉到排名）。"""


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
    gate_cautions: list[str] = []
    """拍板门槛校准的说明（还没校准推荐时写原因）；有推荐值时直接挂在门槛项的推荐上。"""
    meaning: SettingsMeaning | None = None
    """「你的设置实际意味着」：按硬上限、最长持仓档、本金算的几行（只提示）。"""
    budget: list[SettingItem] = []
    """军师团每日花费上限（config/slow.toml 的 [budget]，只放日上限）。"""
    """节奏配置可能造成换卡空档时的提示（只提示，不拒绝）。"""
    screener: list[SettingItem] = []
    """选币规则（config/slow.toml 的 [screener]）：门槛、打分满分线、权重。"""
    tiers: list[SettingItem] = []
    """分档交易（config/slow.toml 的 [screener.tiers]）。"""
    screener_cautions: list[str] = []
    """选币规则比推荐冒险的项（只提示，不拒绝）。"""
    majors: MajorsInfo | None = None
    """现在算主流币的名单。"""
    tiers_cautions: list[str] = []
    """分档交易设置里用不上或不像设计稿的地方（只提示）。"""
    profile: ProfileInfo | None = None
    prompts: PromptsInfo | None = None
    """军师团现在用的提示词档（引擎启动时写的；换档后要重启才变）。"""
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
        if value is None and spec is not None and spec.kind is bool:
            value = False  # 开关不写 = 关
        # default_factory 的项（币名列表）也要拿到真默认值，不然永远显示「恢复默认」
        default = None if field.is_required() else field.get_default(call_default_factory=True)
        resettable = (
            spec is not None
            and not spec.profiled
            and not field.is_required()
            and _plain(value) != _plain(default)
            and not (spec.kind is bool and value is False and default is None)
        )
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
                names=bool(spec and spec.names),
                pattern=spec.pattern if spec else None,
                paper_only=section == "fast_loop" and name in PAPER_ONLY_SWITCHES,
                code_default=_plain(default) if resettable else None,
                resettable=resettable,
            )
        )
    return out


def _majors(files: PanelFiles, scr: ScreenerSection) -> MajorsInfo:
    """和选币器同一个口径：前 N 名（或固定 7 个）+ 手动加入 − 手动排除。"""
    inc = [x.strip().upper() for x in scr.major_include if x.strip()]
    exc = {x.strip().upper() for x in scr.major_exclude}
    raw = files.majors() if scr.major_top_n > 0 else None
    ranks: dict[str, int] = {}
    at: datetime | None = None
    got_n: int | None = None
    if isinstance(raw, dict) and isinstance(raw.get("ranks"), dict):
        try:
            # 选币器每轮写的「实际生效的前 N 个」（已按币安有没有永续过滤）；
            # 还没写过就先拿候选表的前 N 个
            eff = raw.get("effective")
            if isinstance(eff, dict) and eff:
                ranks = {str(k): int(v) for k, v in eff.items()}
                got_n = int(raw.get("effective_top_n", 0))
            else:
                ranks = dict(
                    list({str(k): int(v) for k, v in raw["ranks"].items()}.items())[
                        : scr.major_top_n
                    ]
                )
                got_n = None
            at = datetime.fromisoformat(str(raw["fetched_at"]))
        except (KeyError, TypeError, ValueError):
            ranks, at, got_n = {}, None, None
    rows: list[MajorRow] = []
    if ranks:
        rows = [
            MajorRow(name=k, rank=v, why=f"市值第 {v} 名")
            for k, v in sorted(ranks.items(), key=lambda kv: kv[1])
        ]
    else:
        rows = [MajorRow(name=k, why="固定名单") for k in sorted(FALLBACK_MAJORS)]
    seen = {r.name for r in rows}
    rows += [MajorRow(name=k, why="你手动加入") for k in inc if k not in seen]
    return MajorsInfo(
        top_n=scr.major_top_n,
        rows=[r for r in rows if r.name not in exc],
        excluded=[r.name for r in rows if r.name in exc],
        fetched_at=at,
        fetched_top_n=got_n,
        fallback=not ranks,
    )


def _plain(v: object) -> object:
    if isinstance(v, tuple):
        return [_plain(x) for x in v]
    return v.value if isinstance(v, StrEnum) else v


_GATE_CALIBRATED = {
    "min_p_take": "p_take",
    "min_p_genuine": "p_genuine",
    "max_p_toxic": "max_p_toxic",
}


def _gate_calibration(page: SettingsPage, files: PanelFiles, hold: str) -> None:
    """拍板门槛旁边显示校准推荐值 + 样本数 + 日期；不自动套用。"""
    cal = files.gate_calibration(hold)
    if cal is None:
        page.gate_cautions = [f"{hold} 档还没有拍板门槛校准结果（跑过 jiaoyi gate-calibrate 才有）"]
        return
    rec = cal.get("recommended")
    samples = cal.get("samples")
    when = cal.get("computed_at")
    day = str(when)[:10] if when else "日期不明"
    if not isinstance(rec, dict):
        page.gate_cautions = [f"{hold} 档拍板门槛还没有校准推荐：{cal.get('note') or '没写原因'}"]
        return
    why = f"校准推荐（{hold} 档，{samples} 个样本，{day}）"
    for item in page.fast_loop:
        v = rec.get(_GATE_CALIBRATED.get(item.key, ""))
        if isinstance(v, int | float) and not isinstance(v, bool):
            item.recommended, item.recommended_why = float(v), why


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
        meaning=build_meaning(
            risk, app, app.profile, slow.budget.daily_cost_limit_usd, equity_usdt
        ),
    )
    recs = recommended_caps(equity_usdt)
    for item in page.risk:
        if rec := recs.get(item.key):
            item.recommended, item.recommended_why = rec.value, rec.why
        elif item.key == "max_slippage_bps" and app.hold in profiles.SLIPPAGE_BPS:
            item.recommended = profiles.SLIPPAGE_BPS[app.hold]
            item.recommended_why = (
                f"按止损的 1/4（只拦不利方向）；{app.hold} 档 BTC 典型约 {item.recommended:g}bp"
            )
    _gate_calibration(page, files, app.hold)
    page.majors = _majors(files, slow.screener)
    for item in page.screener:
        if srec := SCREENER_RECOMMENDED.get(item.key):
            item.recommended, item.recommended_why = srec
    if profile:
        for section, hit in profile.pinned.items():
            for item in getattr(page, section):
                if item.key in hit:
                    item.pinned, item.profile_default = True, hit[item.key]
    return page
