"""总览页的数据：把决策日志、模块状态、账本、熔断状态和 hummingbot-api 拼成一份 JSON。

页面只管画，口径都在这里定（设计文档 §十 总览）：
- 状态灯：每个角色最新心跳；超过 ``STALE_S`` 没更新视为掉线（主循环没在跑时全红）。
- 总资产 = hummingbot 钱包余额 + 未实现盈亏；拿不到就用账本里上一次记下的权益。
- 今日盈亏和离熔断：日初权益优先取熔断器记下的（和保镖同一口径），其次执行层账本。
- 信号与决策流：拍板、保镖、执行、管家 / 巡逻兵平仓；哨兵量大，只计数不进流。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel

from jiaoyi.contracts import (
    AppConfig,
    DecisionRecord,
    Health,
    ModuleStatus,
    Outcome,
    PositionRecord,
    RiskConfig,
    Role,
)
from jiaoyi.contracts.battle_card import CardSymbol
from jiaoyi.core.recorder import local_day
from jiaoyi.panel.files import Ledger, PanelFiles
from jiaoyi.panel.hb import HbSnapshot

STALE_S = 120.0
"""主循环每 30 秒心跳一次；4 个周期没更新就当掉线。"""
FEED_LIMIT = 30
ALERT_LIMIT = 12

LampState = Literal["ok", "slow", "bad", "off"]
FeedKind = Literal["go", "skip", "block", "close", "info"]


class Lamp(BaseModel):
    key: str
    label: str
    state: LampState
    detail: str


class Kpis(BaseModel):
    equity_usdt: float | None
    equity_source: Literal["hummingbot", "ledger", "none"]
    day_start_equity_usdt: float | None
    day_pnl_usdt: float | None
    day_pnl_pct: float | None
    halt_pct: float
    to_halt_usdt: float | None
    """再亏多少 USDT 触发日亏熔断；已触发为 0。"""
    halted: bool
    exposure_x: float | None
    max_exposure_x: float


class LevelView(BaseModel):
    price: float
    kind: str
    """support / resistance / pivot；页面按现价上下重新分支撑、压力（同慢环落卡的口径）。"""


class CardRow(BaseModel):
    """作战卡里一个标的；点开作战卡时整张表显示。旧记录读不到会议结论时只有币名。"""

    symbol: str
    bias: str
    category: str
    reason: str = ""
    size_tier_max: str | None = None
    stop_atr: tuple[float, float] | None = None
    take_profit_r: tuple[float, float] | None = None
    leverage: tuple[float, float] | None = None
    notional_usdt: tuple[float, float] | None = None
    allowed_signals: list[str] = []
    key_levels: list[LevelView] = []


class CardView(BaseModel):
    card_id: str
    version: int | None
    created_at: datetime
    expires_at: datetime | None
    valid: bool
    regime: str
    symbols: list[CardRow]
    stop_atr: tuple[float, float] | None
    leverage_max: float | None
    notional_usdt: tuple[float, float] | None


class FeedItem(BaseModel):
    ts: datetime
    kind: FeedKind
    role: Role
    symbol: str | None
    text: str
    signal_id: str | None
    """点开时按它取这个信号从哨兵到执行的整条链。"""


class PositionView(BaseModel):
    position_id: str
    symbol: str
    category: str
    side: str
    status: str
    quantity: float
    leverage: int
    entry_price: float | None
    stop_loss_price: float | None
    take_profit_price: float | None
    lifeboat_status: str | None
    lifeboat_price: float | None
    opened_at: datetime
    time_limit_s: int
    signal_id: str
    unrealized_usdt: float | None
    keeper: str | None
    """管家对这笔的最新判断（拿住 / 平仓）。"""


class Tiles(BaseModel):
    signals: int
    opened: int
    closed: int
    win_rate: float | None
    fees_usdt: float
    jev_cost_usd: float
    llm_cost_usd: float
    avg_gate_latency_ms: float | None


class AlertItem(BaseModel):
    ts: datetime
    level: Literal["warn", "critical"]
    role: Role
    symbol: str | None
    text: str


class Control(BaseModel):
    """暂停状态、配置是否待重启、面板密码设了没有（按钮要不要问密码）。"""

    paused: bool = False
    paused_corrupt: bool = False
    """paused.json 读不出：主循环按“已暂停”处理。"""
    paused_since: str | None = None
    paused_reason: str | None = None
    config_pending: bool | None = None
    """True = 配置文件改过、主循环还在用旧的（重启后生效）；None = 不知道。"""
    pw_set: bool = False
    """面板密码设了没有。"""
    panel_stale: bool = False
    """磁盘上的代码比面板进程启动时新（git pull 了没重启面板）：接口和页面可能对不上。"""
    writable: bool = False
    """面板这次启动有没有拿到配置文件路径（没有就只读）。"""
    started_at: str | None = None
    """主程序这次启动的时间（runtime.json）；面板“重启生效”看它变了才算真的重启了。"""


class Overview(BaseModel):
    now: datetime
    mode: str
    display_tz: str
    running: bool
    """主循环最近 ``STALE_S`` 秒内有心跳。"""
    last_heartbeat: datetime | None
    gate_label: str
    hb_ok: bool
    hb_error: str | None
    lamps: list[Lamp]
    kpis: Kpis
    card: CardView | None
    feed: list[FeedItem]
    positions: list[PositionView]
    tiles: Tiles
    alerts: list[AlertItem]
    blocked: dict[str, str]
    control: Control = Control()


_HEALTH_LAMP: dict[Health, LampState] = {
    Health.OK: "ok",
    Health.DEGRADED: "slow",
    Health.STARTING: "slow",
    Health.DOWN: "bad",
    Health.DISABLED: "off",
}
_RANK: dict[LampState, int] = {"off": 0, "ok": 1, "slow": 2, "bad": 3}


def build_overview(
    files: PanelFiles,
    hb: HbSnapshot,
    risk: RiskConfig,
    app: AppConfig,
    now: datetime,
) -> Overview:
    status = files.module_status()
    records = files.decisions_today(now)
    ledger = files.ledger()
    today = local_day(now, risk.day_reset_tz).isoformat()

    beats = [s.ts for s in status.values()]
    last_beat = max(beats) if beats else None
    running = last_beat is not None and now - last_beat <= timedelta(seconds=STALE_S)

    kpis = compute_kpis(files, ledger, hb, risk, now)
    halted, halt_pct = kpis.halted, kpis.halt_pct
    positions = position_views(ledger.live(), records, hb)

    # ---- 今日统计
    tz = risk.day_reset_tz
    opened_today = [
        p
        for p in ledger.positions
        if local_day(p.opened_at, tz).isoformat() == today and p.entry_price is not None
    ]
    closed_today = [
        p
        for p in ledger.positions
        if p.closed_at is not None
        and local_day(p.closed_at, tz).isoformat() == today
        and p.realized_pnl_usdt is not None
    ]
    wins = sum(1 for p in closed_today if (p.realized_pnl_usdt or 0.0) > 0)
    gate_recs = [r for r in records if r.role is Role.GATE]
    tiles = Tiles(
        signals=sum(1 for r in records if r.role is Role.SENTINEL and r.outcome is Outcome.ACTION),
        opened=len(opened_today),
        closed=len(closed_today),
        win_rate=wins / len(closed_today) if closed_today else None,
        fees_usdt=sum(p.fees_usdt for p in closed_today),
        jev_cost_usd=sum(r.cost_usd for r in records if r.role in (Role.GATE, Role.KEEPER)),
        llm_cost_usd=sum(r.cost_usd for r in records if r.role is Role.COMMANDER),
        avg_gate_latency_ms=(
            sum(r.latency_ms for r in gate_recs) / len(gate_recs) if gate_recs else None
        ),
    )

    return Overview(
        now=now,
        mode=app.mode.value,
        display_tz=tz,
        running=running,
        last_heartbeat=last_beat,
        gate_label=_gate_label(status.get(Role.GATE.value)),
        hb_ok=hb.ok,
        hb_error=hb.error,
        lamps=_lamps(status, hb, now, running),
        kpis=kpis,
        card=_card(records, now),
        feed=_feed(records),
        positions=positions,
        tiles=tiles,
        alerts=_alerts(records, halted, halt_pct),
        blocked=files.blocked(),
    )


def compute_kpis(
    files: PanelFiles, ledger: Ledger, hb: HbSnapshot, risk: RiskConfig, now: datetime
) -> Kpis:
    """总资产、今日盈亏、离熔断、敞口。各页顶部的状态条共用这一份口径。"""
    today = local_day(now, risk.day_reset_tz).isoformat()
    equity: float | None
    source: Literal["hummingbot", "ledger", "none"]
    if hb.ok and hb.equity_usdt is not None:
        equity, source = hb.equity_usdt, "hummingbot"
    elif ledger.last_equity is not None:
        equity, source = ledger.last_equity, "ledger"
    else:
        equity, source = None, "none"
    breaker = files.breaker()
    start = None
    if breaker is not None and breaker.day == today:
        start = breaker.start_equity
    if start is None and ledger.day_start_day == today:
        start = ledger.day_start_equity
    halted = bool(breaker and breaker.day == today and breaker.tripped)
    day_pnl = equity - start if equity is not None and start else None
    halt_pct = risk.daily_loss_halt_pct
    to_halt = None
    if equity is not None and start:
        to_halt = 0.0 if halted else max(equity - start * (1 - halt_pct / 100.0), 0.0)
    live = ledger.live()
    exposure = (
        sum(ledger.notional(p) for p in live) / equity
        if equity is not None and equity > 0
        else None
    )
    return Kpis(
        equity_usdt=equity,
        equity_source=source,
        day_start_equity_usdt=start,
        day_pnl_usdt=day_pnl,
        day_pnl_pct=day_pnl / start * 100.0 if day_pnl is not None and start else None,
        halt_pct=halt_pct,
        to_halt_usdt=to_halt,
        halted=halted,
        exposure_x=exposure,
        max_exposure_x=risk.max_total_exposure_x,
    )


def position_views(
    live: list[PositionRecord], records: list[DecisionRecord], hb: HbSnapshot
) -> list[PositionView]:
    """未结束的持仓，最新开的在前；附上 hummingbot 的浮盈浮亏和管家最新判断。"""
    keeper_last: dict[str, str] = {}
    for r in records:
        if r.role is Role.KEEPER and r.position_id:
            keeper_last[r.position_id] = {"close": "平仓", "hold": "拿住"}.get(r.outcome, "出错")
    return [
        PositionView(
            position_id=p.position_id,
            symbol=p.symbol,
            category=p.category.value,
            side=p.side.value,
            status=p.status.value,
            quantity=p.quantity,
            leverage=p.leverage,
            entry_price=p.entry_price,
            stop_loss_price=p.stop_loss_price,
            take_profit_price=p.take_profit_price,
            lifeboat_status=p.lifeboat.status.value if p.lifeboat else None,
            lifeboat_price=p.lifeboat.stop_price if p.lifeboat else None,
            opened_at=p.opened_at,
            time_limit_s=p.time_limit_s,
            signal_id=p.signal_id,
            unrealized_usdt=hb.unrealized_of(p.symbol) if hb.ok else None,
            keeper=keeper_last.get(p.position_id),
        )
        for p in sorted(live, key=lambda p: p.opened_at, reverse=True)
    ]


def _gate_label(st: ModuleStatus | None) -> str:
    if st is None:
        return "未接拍板模型"
    return "规则拍板" if st.headline.startswith("【规则拍板】") or "规则" in st.headline else "Jev"


def _lamps(
    status: dict[str, ModuleStatus], hb: HbSnapshot, now: datetime, running: bool
) -> list[Lamp]:
    def one(role: Role) -> tuple[LampState, str]:
        st = status.get(role.value)
        if st is None:
            return "off", "等待第一次上报" if running else "主程序没在运行"
        if now - st.ts > timedelta(seconds=STALE_S):
            return "bad", f"最后心跳 {int((now - st.ts).total_seconds() // 60)} 分钟前"
        state = _HEALTH_LAMP[st.health]
        return state, st.last_error if state in ("slow", "bad") and st.last_error else st.headline

    def group(key: str, label: str, roles: tuple[Role, ...]) -> Lamp:
        parts = [one(r) for r in roles]
        live = [p for p in parts if p[0] != "off"]
        if not live:
            return Lamp(key=key, label=label, state="off", detail=parts[0][1])
        worst = max(live, key=lambda p: _RANK[p[0]])
        return Lamp(key=key, label=label, state=worst[0], detail=worst[1])

    lamps = [
        group("fast", "快环", (Role.SENTINEL, Role.RISK_GUARD, Role.KEEPER, Role.PATROL)),
        group("slow", "慢环", (Role.COMMANDER, Role.SCREENER)),
        group("gate", "拍板", (Role.GATE,)),
        group("exec", "执行器", (Role.EXECUTOR, Role.LIFEBOAT)),
        group("recon", "对账", (Role.RECONCILER,)),
    ]
    hb_state: LampState = "ok" if hb.ok else "bad"
    lamps.insert(4, Lamp(key="hb", label="hummingbot", state=hb_state, detail=hb.error or "已连接"))
    if not running:
        lamps.insert(0, Lamp(key="main", label="主程序", state="bad", detail="没在运行"))
    return lamps


def _card(records: list[DecisionRecord], now: datetime) -> CardView | None:
    """今天最新一张作战卡：军师出卡记录 + 每个标的最新的会议结论（output 就是 CardSymbol）。"""
    cmd = [r for r in records if r.role is Role.COMMANDER]
    issued = [r for r in cmd if r.card_id and r.outcome is Outcome.ACTION]
    if not issued:
        return None
    last = issued[-1]
    names = [str(s) for s in last.output.get("symbols", [])]
    per: dict[str, CardSymbol] = {}
    for r in cmd:
        if r.symbol and r.output and r.ts <= last.ts:
            try:
                per[r.symbol] = CardSymbol.model_validate(r.output)
            except ValueError:
                continue
    rows: list[CardRow] = []
    syms = [per[n] for n in names if n in per]
    for n in names:
        cs = per.get(n)
        rows.append(
            CardRow(
                symbol=n,
                bias=cs.bias.value,
                category=cs.category.value,
                reason=cs.reason,
                size_tier_max=cs.size_tier_max.value,
                stop_atr=(cs.stop_atr.min, cs.stop_atr.max),
                take_profit_r=(cs.take_profit_r.min, cs.take_profit_r.max),
                leverage=(cs.leverage.min, cs.leverage.max),
                notional_usdt=(cs.notional_usdt.min, cs.notional_usdt.max),
                allowed_signals=[k.value for k in cs.allowed_signals],
                key_levels=[LevelView(price=lv.price, kind=lv.kind.value) for lv in cs.key_levels],
            )
            if cs
            else CardRow(symbol=n, bias="unknown", category="other")
        )
    tradable = [s for s in syms if s.bias.value != "no_trade"]
    version = last.output.get("version")
    expires = _parse_ts(last.output.get("expires_at"))
    return CardView(
        card_id=last.card_id or "",
        version=version if isinstance(version, int) else None,
        created_at=last.ts,
        expires_at=expires,
        valid=expires is None or now < expires,
        regime=str(last.output.get("regime", "")),
        symbols=rows,
        stop_atr=(
            (min(s.stop_atr.min for s in tradable), max(s.stop_atr.max for s in tradable))
            if tradable
            else None
        ),
        leverage_max=max(s.leverage.max for s in tradable) if tradable else None,
        notional_usdt=(
            (min(s.notional_usdt.min for s in tradable), max(s.notional_usdt.max for s in tradable))
            if tradable
            else None
        ),
    )


def _parse_ts(raw: object) -> datetime | None:
    """出卡记录里的到期时间（旧记录没有这一项）。"""
    if not isinstance(raw, str):
        return None
    try:
        ts = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return ts if ts.tzinfo is not None else None


_FEED_ROLES = {Role.GATE, Role.RISK_GUARD, Role.EXECUTOR, Role.KEEPER, Role.PATROL}


def _feed(records: list[DecisionRecord]) -> list[FeedItem]:
    out: list[FeedItem] = []
    for r in reversed(records):
        if r.role not in _FEED_ROLES:
            continue
        kind = _kind(r)
        if kind is None:
            continue
        out.append(
            FeedItem(
                ts=r.ts,
                kind=kind,
                role=r.role,
                symbol=r.symbol,
                text=r.summary,
                signal_id=r.signal_id,
            )
        )
        if len(out) >= FEED_LIMIT:
            break
    return out


def _kind(r: DecisionRecord) -> FeedKind | None:
    o = r.outcome
    if r.role is Role.GATE:
        return "skip" if o in (Outcome.REJECT, Outcome.ERROR) else "info"
    if r.role is Role.RISK_GUARD:
        return "go" if o is Outcome.PASS else "block"
    if r.role is Role.EXECUTOR:
        exec_kinds: dict[Outcome, FeedKind] = {
            Outcome.ACTION: "go",
            Outcome.CLOSE: "close",
            Outcome.ERROR: "block",
        }
        return exec_kinds.get(o)
    # 管家 / 巡逻兵：“拿住”太多，只放平仓
    return "close" if o is Outcome.CLOSE else None


def _alerts(records: list[DecisionRecord], halted: bool, halt_pct: float) -> list[AlertItem]:
    """告警本身不落盘（直接推 Telegram），这里从决策日志里还原：出错、对账补救、下架封禁。"""
    out: list[AlertItem] = []
    for r in reversed(records):
        if r.outcome is Outcome.ERROR:
            # Jev 超时这类拍板 / 管家出错只是少开一笔或维持现状，不算严重
            soft = r.role in (Role.GATE, Role.KEEPER)
            level: Literal["warn", "critical"] = "warn" if soft else "critical"
        elif (r.role is Role.RECONCILER and r.outcome is Outcome.ACTION) or (
            r.role is Role.COMMANDER and r.output.get("blocked")
        ):
            level = "warn"
        else:
            continue
        out.append(AlertItem(ts=r.ts, level=level, role=r.role, symbol=r.symbol, text=r.summary))
        if len(out) >= ALERT_LIMIT:
            break
    return out
