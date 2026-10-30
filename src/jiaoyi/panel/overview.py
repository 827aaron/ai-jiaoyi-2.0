"""总览页的数据：把决策日志、模块状态、账本、熔断状态和 hummingbot-api 拼成一份 JSON。

页面只管画，口径都在这里定（设计文档 §十 总览）：
- 状态灯：每个角色最新心跳；超过 ``STALE_S`` 没更新视为掉线（主循环没在跑时全红）。
- 总资产 = hummingbot 钱包余额 + 未实现盈亏；拿不到就用账本里上一次记下的权益。
- 今日盈亏和离熔断：日初权益优先取熔断器记下的（和风控员同一口径），其次执行层账本。
- 信号与决策流：拍板、风控员、执行、管家 / 巡逻兵平仓；哨兵量大，只计数不进流。
"""

from __future__ import annotations

import json
import math
import statistics
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from jiaoyi.contracts import (
    AppConfig,
    DecisionRecord,
    Health,
    ModuleStatus,
    Outcome,
    PositionRecord,
    PositionStatus,
    RiskConfig,
    Role,
    Side,
)
from jiaoyi.contracts.battle_card import CardSymbol, MarketView
from jiaoyi.core.recorder import local_day
from jiaoyi.panel.files import Ledger, PanelFiles
from jiaoyi.panel.hb import HbSnapshot
from jiaoyi.panel.meetings import STAGES, read_meeting
from jiaoyi.panel.meetings import STALE_S as MEETING_STALE_S
from jiaoyi.panel.protection import ProtectionView, lifeboat_summary, parse_protection
from jiaoyi.panel.stop_diagnostics import StopDiagnostics
from jiaoyi.panel.trailing import TrailEstimate
from jiaoyi.slow.card_outlook import card_from_output
from jiaoyi.slow.desk_intra import compose_intra

DESK_SOURCE = "market_desk"
DESK_STATE_FILE = "market_desk_state.json"

STALE_S = 120.0
"""主循环每 30 秒心跳一次；4 个周期没更新就当掉线。"""
FEED_LIMIT = 100
ALERT_LIMIT = 12

LampState = Literal["ok", "slow", "bad", "off"]
PriceSrc = Literal["push", "rest", "mark"]
"""持仓现价哪来的：push = 币安 WebSocket 推送；rest = 推送断了，退回一次全量 REST 行情；
mark = 都拿不到，按 hummingbot 的浮盈（币安标记价）倒推。"""
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
    as_of: datetime | None = None
    """总资产 / 今日盈亏这份数是几点拉到的（hummingbot）；用账本兜底时 None。"""
    day_closed_usdt: float | None = None
    """今天平掉的单的净盈亏（已扣手续费，同历史页「交易净盈亏」）。"""
    day_floating_usdt: float | None = None
    """还没平的单现在的浮盈浮亏（hummingbot）；拿不到 None。"""
    day_closes_usdt: float | None = None
    """今日平仓：今天平掉的单，0 点前开的从当日参考价算起、今天开的从开仓价，扣今天收的手续费。"""
    day_holding_usdt: float | None = None
    """持仓今日变动：没平的单，0 点前开的从当日参考价算起、今天开的从开仓价（按标记价）。"""
    day_costs_usdt: float | None = None
    """手续费·资金费：今天收的手续费里没算进「今日平仓」的那部分 + 资金费。"""
    day_funding_known: bool = False
    """``day_costs_usdt`` 里有没有资金费（币安收入流水拿到了才有）。"""
    day_residual_usdt: float | None = None
    """对账差：今日盈亏 −（上面三项）。标记价和参考价的来源不同、流水还没拉到等，只在提示里显示。"""
    day_refs_missing: list[str] = []
    """0 点前开的、没有当日参考价的币（0 点时大脑没在跑）：这些按开仓价算，今日数不准。"""
    day_trading_usdt: float | None = None
    """币安收入流水今天的平仓盈亏（从开仓价算，手续费前）；没有流水 None。资金页的主口径从它起。"""
    day_fees_usdt: float | None = None
    """币安收入流水今天的手续费（负数）；没有流水 None。"""
    day_funding_usdt: float | None = None
    """币安收入流水今天的资金费；没有流水 None。"""
    day_overnight_usdt: float | None = None
    """今天平掉的隔夜仓在 0 点前已有的盈亏：流水按开仓价算进了今天，但它已经在日初权益里。"""
    day_recon_residual_usdt: float | None = None
    """账户今日变动 −（流水已实现净额 − 隔夜部分 + 持仓今日变动）。

    来源：流水没拉到、标记价口径、转账等。"""
    income_updated_at: datetime | None = None
    """币安收入流水最后一次汇总的时间（主程序每 5 分钟写一次）。"""


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
    pending_label: str | None = None
    """没有会议结论（bias = unknown）时方向列的短词：开会中 / 开会失败 / 会中断 / 没开会。"""
    pending: str | None = None
    """同上，理由列的一句明确说明（从 meeting_live/<币>.json 读），不写问号。"""
    issued_at: datetime | None = None
    expires_at: datetime | None = None
    held: bool = False
    flip_pending: str | None = None
    """待确认翻转（``[schedule] card_flip_confirm``）：会议改判了，卡上仍按原方向只看不做。"""
    source_note: str | None = None
    source_regime: str | None = None
    source_market_view_id: str | None = None
    """「来自 hh:mm 的会 · hh:mm 到期」；持仓币到期后写「已到期，持仓中，不再开新仓」。"""
    market_dir: str | None = None
    rel_dir: str | None = None
    board_market_dir: str | None = None
    """只记录的判断（CardOutlook）：会议看未来 4 小时大盘 / 相对大盘，及总盘会方向；旧卡没有。"""
    fading: bool = False
    """离到期不到 10 分钟（或已到期的持仓币）：显示成灰色。"""


class DeskLatest(BaseModel):
    """大盘台最近一轮（不是上次发布的那版）：「最新大盘判断」下面那一行，只在
    ``[market_desk] publish_intra`` 开着、当前观点是大盘台发的时候有。"""

    run_at: datetime
    probs_4h: dict[str, float] | None = None
    probs_1h: dict[str, float] | None = None
    intra: str
    """「未来 1 小时概率 + 短线现状（截至 HH:MM UTC）」，和各币会议看到的是同一段字；过旧写缺失。"""


class CardView(BaseModel):
    card_id: str
    version: int | None
    created_at: datetime
    expires_at: datetime | None
    valid: bool
    regime: str
    current_market: MarketView | None = None
    desk_latest: DeskLatest | None = None
    symbols: list[CardRow]
    stop_atr: tuple[float, float] | None
    leverage_max: float | None
    notional_usdt: tuple[float, float] | None
    size_fractions: dict[str, float] = {}
    """仓位档 → 每笔风险打几折（风控员按这个算：S / M 来自 app.toml [fast_loop]，L 固定 1）。"""
    leverage_used: int | None = None
    """风控员下单实际用的杠杆：作战卡不约束（默认）时就是硬上限；约束时为 None（按各币卡上的）。"""
    risk_per_trade_usdt: float | None = None
    """L 档每笔风险 = 总资产 × 每笔风险%；S / M 再乘 ``size_fractions``。拿不到总资产时 None。"""


class FeedItem(BaseModel):
    ts: datetime
    kind: FeedKind
    role: Role
    symbol: str | None
    text: str
    signal_id: str | None
    """点开时按它取这个信号从哨兵到执行的整条链。"""


EXIT_FILLED_NOTE = "已成交，收尾中"


class PositionView(BaseModel):
    position_id: str
    executor_id: str | None = None
    symbol: str
    category: str
    side: str
    status: str
    close_note: str | None = None
    """「已成交，收尾中」 once the exit order has filled while the ledger still says closing."""
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
    keeper_peak_usdt: float | None = None
    """管家最近一次复查时记的开仓以来最高浮盈（U，按管家快照的 ``pos.peak_bps`` 换算，未扣费）。"""
    keeper_peak_r: float | None = None
    """同上，止损距离的倍数。"""
    keeper_giveback_usdt: float | None = None
    """从最高浮盈回吐了多少（U）。"""
    keeper_trail_active: bool | None = None
    """管家快照里移动止损是否已启动；None = 这笔没带移动止损或还没复查过。"""
    mark_price: float | None = None
    """现价，和浮盈同一口径：``price_src`` 为 push / rest 是币安最新成交价（和 K 线图同源），
    为 mark 是由 hummingbot 的浮盈倒推（开仓价 ± 浮盈 ÷ 数量，币安按标记价算）。"""
    price_src: PriceSrc | None = None
    notional_usdt: float | None = None
    """名义 = 数量 × 现价（没有现价用开仓价）。"""
    margin_usdt: float | None = None
    """本金占用 = 名义 ÷ 杠杆。"""
    stop_loss_pct: float | None = None
    """风控员算的止损距离（%），不用回填后的价格倒推；旧记录为 None。"""
    lifeboat_stop_pct: float | None = None
    """风控员算的保命止损距离（%）。"""
    trailing_activation_pct: float | None = None
    """移动止损启动线（%，扣过开仓手续费的盈亏）；None = 这笔没带移动止损。"""
    trailing_stop_pct: float | None = None
    """移动止损回撤距离（%）。"""
    entry_fee_pct: float | None = None
    """开仓手续费占名义的百分比（账本实收，没有按吃单费率估），算启动价用。"""
    trail: TrailEstimate | None = None
    """兼容旧客户端；不再用 K 线估算执行器状态。"""
    stop_diagnostics: StopDiagnostics | None = None
    protection: ProtectionView | None = None
    """交易所托管保护单的落盘状态（新仓才有）；有它时 ``lifeboat_*`` 按它算。"""
    book_quote: dict[str, object] | None = None
    """独立的公开 REST 盘口参考（含获取时间和环境），不是执行器检查输入。"""
    pnl_pct: float | None = None
    """浮盈占名义的百分比（不乘杠杆）。"""
    fees_est_usdt: float | None = None
    """开平两次手续费：开仓按开仓名义、平仓按现在的名义，都按吃单费率估（币安只在平仓后给实收）。"""
    net_unrealized_usdt: float | None = None
    """扣掉 ``fees_est_usdt`` 后的浮盈：现在平掉大概能落袋多少。"""
    today_usdt: float | None = None
    """今日变动（未扣费）：0 点前开的从当日参考价算起，今天开的同浮盈。"""
    today_from: Literal["ref", "entry", "missing"] | None = None
    """``today_usdt`` 从哪算起：当日参考价 / 开仓价（今天开的）/ 隔夜仓缺参考价（按开仓价）。"""
    net_pnl_pct: float | None = None
    fee_bps: float | None = None
    """估手续费用的单边费率（bp）。"""
    fee_from_fills: bool = False
    """费率是从最近平掉的单实收倒推的（True），还是配置里的 ``taker_fee_bps``（False，只是估）。"""


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
    ui_version: str = ""
    """磁盘上 app.js 当前的版本号（同首页 ``app.js?v=``）；开着的页面不是它就提示刷新。"""
    writable: bool = False
    """面板这次启动有没有拿到配置文件路径（没有就只读）。"""
    started_at: str | None = None
    """主程序这次启动的时间（runtime.json）；面板“重启生效”看它变了才算真的重启了。"""
    hot_applied_at: str | None = None
    """主程序最近一次把设置热换上（不用重启）的时间（runtime.json）。"""
    hot_keys: list[str] = []
    """不用重启就生效的设置键（``段.键``，``category.*`` = 整段）；其余改了要重启。"""
    paper_only_ignored: list[str] = []
    """配置里开着、但现在不是模拟盘所以被忽略的开关（中文名）。"""
    llm_credit: str | None = None
    """模型额度（OpenRouter 余额）快用完或刚撞 402 时的提示语，总览顶部显示横幅。"""


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
    prices: dict[str, tuple[float, PriceSrc]] | None = None,
) -> Overview:
    status = files.module_status()
    records = files.decisions_today(now)
    recent = files.decisions_days(now)  # 列表类（信号流、持仓走势）不按曼谷日截断
    ledger = files.ledger()
    today = local_day(now, risk.day_reset_tz).isoformat()

    beats = [s.ts for s in status.values()]
    last_beat = max(beats) if beats else None
    running = last_beat is not None and now - last_beat <= timedelta(seconds=STALE_S)

    kpis = compute_kpis(files, ledger, hb, risk, now)
    halted, halt_pct = kpis.halted, kpis.halt_pct
    positions = position_views(
        ledger.live(),
        recent,
        hb,
        risk.taker_fee_bps,
        fee_rate_bps(ledger.positions),
        prices,
        ledger.protection,
        now,
        app.protection_stale_s,
        *day_refs_at(ledger, risk.day_reset_tz, now),
        exit_filled=ledger.exit_filled,
    )

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
        and p.status is not PositionStatus.FAILED  # 没成交的不算平仓
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
        card=_with_fractions(
            _with_pool_notes(
                _with_pending(
                    _with_current_market(
                        _card(records, now, {p.symbol for p in positions}), files.root, now
                    ),
                    files.root,
                    now,
                    files.tz,
                ),
                now,
                files.tz,
            ),
            app,
            risk,
            kpis.equity_usdt,
        ),
        feed=_feed(recent),
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
        as_of=hb.fetched_at if source == "hummingbot" else None,
        # Demo's fills: this sits next to equity-based day P&L, which is Demo's money.
        day_closed_usdt=sum(
            p.realized_pnl_usdt or 0.0
            for p in map(ledger.demo, ledger.positions)
            if p.closed_at is not None
            and local_day(p.closed_at, risk.day_reset_tz).isoformat() == today
        ),
        day_floating_usdt=hb.unrealized_usdt if hb.ok else None,
    ).model_copy(update=_day_breakdown(files, ledger, hb, risk.day_reset_tz, now, day_pnl))


def day_refs_at(ledger: Ledger, tz: str, now: datetime) -> tuple[dict[str, float], datetime]:
    """(当日参考价, 当地 0 点)：给 ``position_views`` 算每笔的「今日」。"""
    local = now.astimezone(ZoneInfo(tz))
    refs = ledger.day_refs if ledger.day_start_day == local.date().isoformat() else {}
    return refs, local.replace(hour=0, minute=0, second=0, microsecond=0)


def _sign(p: PositionRecord) -> float:
    return 1.0 if p.side is Side.LONG else -1.0


def _income_today(raw: object, today: str) -> tuple[float, float, float] | None:
    """(realized_pnl, commission, funding_fee) of the local day; None when the file lacks it."""
    days = raw.get("days") if isinstance(raw, dict) else None
    row = days.get(today) if isinstance(days, dict) else None
    try:
        return (
            float(row["realized_pnl"]),  # type: ignore[index]
            float(row["commission"]),  # type: ignore[index]
            float(row["funding_fee"]),  # type: ignore[index]
        )
    except (TypeError, ValueError, KeyError):
        return None


def _income_updated_at(raw: object) -> datetime | None:
    ts = raw.get("updated_at") if isinstance(raw, dict) else None
    try:
        return datetime.fromisoformat(ts) if isinstance(ts, str) else None
    except ValueError:
        return None


def _day_breakdown(
    files: PanelFiles,
    ledger: Ledger,
    hb: HbSnapshot,
    tz: str,
    now: datetime,
    day_pnl: float | None,
) -> dict[str, object]:
    """今日盈亏拆成 今日平仓 + 持仓今日变动 + 手续费·资金费（同券商 App 的口径）。"""
    local = now.astimezone(ZoneInfo(tz))
    today = local.date().isoformat()
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    refs = ledger.day_refs if ledger.day_start_day == today else {}
    missing: set[str] = set()
    closes = 0.0
    overnight = 0.0
    charged = 0.0  # 今天收的、已经扣在「今日平仓」里的手续费
    for p in map(ledger.demo, ledger.positions):
        if p.closed_at is None or p.closed_at < midnight or p.realized_pnl_usdt is None:
            continue
        closes += p.realized_pnl_usdt
        if p.opened_at >= midnight or not p.entry_price:
            charged += p.fees_usdt
            continue
        # Fees only come as one open+close total; split by price, both legs pay the same rate.
        exit_px = p.exit_price or p.entry_price
        open_fee = p.fees_usdt * p.entry_price / (p.entry_price + exit_px)
        charged += p.fees_usdt - open_fee
        ref = refs.get(p.symbol)
        if ref is None:
            missing.add(p.symbol)
            continue
        before = _sign(p) * p.quantity * (ref - p.entry_price)
        overnight += before
        closes += open_fee - before
    holding: float | None = None
    live = ledger.live()
    if hb.ok:
        holding = hb.unrealized_usdt
        for p in live:
            if p.opened_at >= midnight or not p.entry_price:
                continue
            ref = refs.get(p.symbol)
            if ref is None:
                missing.add(p.symbol)
            else:
                holding -= _sign(p) * p.quantity * (ref - p.entry_price)
    raw = files.income_daily()
    income = _income_today(raw, today)
    if income is not None:
        costs = income[1] + income[2] + charged
    else:
        costs = -sum(p.fees_usdt for p in live if p.opened_at >= midnight)
    residual = (
        day_pnl - closes - holding - costs if day_pnl is not None and holding is not None else None
    )
    recon = (
        day_pnl - (sum(income) - overnight + holding)
        if income is not None and day_pnl is not None and holding is not None
        else None
    )
    return {
        "day_trading_usdt": income[0] if income else None,
        "day_fees_usdt": income[1] if income else None,
        "day_funding_usdt": income[2] if income else None,
        "day_overnight_usdt": overnight,
        "day_recon_residual_usdt": recon,
        "income_updated_at": _income_updated_at(raw),
        "day_closes_usdt": closes,
        "day_holding_usdt": holding,
        "day_costs_usdt": costs,
        "day_funding_known": income is not None,
        "day_residual_usdt": residual,
        "day_refs_missing": sorted(missing),
    }


FEE_SAMPLE = 20
"""倒推实际费率用最近多少笔平掉的单。"""


def fee_rate_bps(positions: list[PositionRecord]) -> float | None:
    """最近平掉的单实收手续费 ÷ (开仓名义 + 平仓名义) 的中位数（bp）；一笔都没有就 None。

    面板不拿密钥，查不了币安的账户费率接口；拿成交回报倒推，Demo / VIP 档位都对得上。
    """
    closed = sorted(
        (
            p
            for p in positions
            if p.closed_at and p.fees_usdt > 0 and p.entry_price and p.exit_price
        ),
        key=lambda p: p.closed_at or p.opened_at,
    )[-FEE_SAMPLE:]
    rates = [
        p.fees_usdt / (p.quantity * ((p.entry_price or 0.0) + (p.exit_price or 0.0))) * 10_000
        for p in closed
    ]
    return statistics.median(rates) if rates else None


def position_views(
    live: list[PositionRecord],
    records: list[DecisionRecord],
    hb: HbSnapshot,
    taker_fee_bps: float = 0.0,
    fills_fee_bps: float | None = None,
    prices: dict[str, tuple[float, PriceSrc]] | None = None,
    protections: dict[str, object] | None = None,
    now: datetime | None = None,
    protection_stale_s: int = 120,
    day_refs: dict[str, float] | None = None,
    midnight: datetime | None = None,
    *,
    exit_filled: set[str] | None = None,
) -> list[PositionView]:
    """未结束的持仓，最新开的在前；附上浮盈浮亏和管家最新判断。

    ``prices`` 是币安最新成交价和来源（推送 / REST，和持仓页 K 线图最后一根同一个数）：有就按它
    算现价和浮盈；拿不到退回 hummingbot 的浮盈（币安按标记价算）倒推。
    """
    keeper_last: dict[str, str] = {}
    keeper_pos: dict[str, dict[str, object]] = {}  # 最近一次管家快照的 pos + trail
    for r in records:
        if r.role is Role.KEEPER and r.position_id:
            # 市场复核的 INFO 是进度回执，不能覆盖真实判断或伪装成调用错误。
            if r.outcome in (Outcome.HOLD, Outcome.CLOSE, Outcome.ERROR):
                keeper_last[r.position_id] = {"close": "平仓", "hold": "拿住"}.get(
                    r.outcome, "出错"
                )
            snap = _keeper_snap(r.input)
            if snap is not None:
                keeper_pos[r.position_id] = snap
    # 账本加移动止损字段之前开的仓：从风控员放行记录（output 顶层是 OrderIntent）补
    trail_of: dict[str, tuple[float, float]] = {}
    for r in records:
        o = r.output
        if r.role is Role.RISK_GUARD and o.get("intent_id") and o.get("trailing_stop_pct"):
            act, delta = o.get("trailing_activation_pct"), o.get("trailing_stop_pct")
            if isinstance(act, int | float) and isinstance(delta, int | float):
                trail_of[str(o["intent_id"])] = (float(act), float(delta))
    out = []
    for p in sorted(live, key=lambda p: p.opened_at, reverse=True):
        sign = 1 if p.side.value == "long" else -1
        last = (prices or {}).get(p.symbol)
        src: PriceSrc | None = None
        mark: float | None = None
        pnl: float | None = None
        if last and p.entry_price:
            mark, src = last
            pnl = sign * p.quantity * (mark - p.entry_price)
        else:
            pnl = hb.unrealized_of(p.symbol) if hb.ok else None
            if pnl is not None and p.entry_price and p.quantity > 0:
                mark, src = p.entry_price + sign * pnl / p.quantity, "mark"
        ref = mark or p.entry_price
        notional = p.quantity * ref if ref else None
        bps = fills_fee_bps if fills_fee_bps is not None else taker_fee_bps
        fees = None
        if notional and p.entry_price:
            # 开仓那次账本里有实收就用实收，没有按费率估；平仓那次只能按费率估
            opened = p.fees_usdt or p.quantity * p.entry_price * bps / 10_000
            fees = opened + notional * bps / 10_000
        net = pnl - fees if pnl is not None and fees is not None else None
        today_pnl, today_from = pnl, None
        if midnight is not None and pnl is not None:
            ref_px = (day_refs or {}).get(p.symbol)
            if p.opened_at >= midnight or not p.entry_price:
                today_from = "entry"
            elif ref_px is None:
                today_from = "missing"
            else:
                today_pnl, today_from = pnl - sign * p.quantity * (ref_px - p.entry_price), "ref"
        trail = (
            (p.trailing_activation_pct, p.trailing_stop_pct)
            if p.trailing_activation_pct is not None and p.trailing_stop_pct is not None
            else trail_of.get(p.intent_id)
        )
        kp = keeper_pos.get(p.position_id, {})
        base = p.entry_price * p.quantity if p.entry_price else None
        peak_bps, give_bps, peak_r = kp.get("peak_bps"), kp.get("giveback_bps"), kp.get("peak_r")
        trail_on = kp.get("trail_on")
        prot = (
            parse_protection(
                protections.get(p.position_id),
                now or datetime.now(UTC),
                protection_stale_s,
                long=p.side is Side.LONG,
                activation_pct=p.trailing_activation_pct,
            )
            if protections
            else None
        )
        lb_status, lb_price = (
            lifeboat_summary(prot)
            if prot
            else (
                p.lifeboat.status.value if p.lifeboat else None,
                p.lifeboat.stop_price if p.lifeboat else None,
            )
        )
        entry_fee_pct = None
        if p.entry_price:
            paid = p.fees_usdt / (p.quantity * p.entry_price) * 100 if p.fees_usdt else None
            entry_fee_pct = paid if paid is not None else bps / 100
        out.append(
            PositionView(
                position_id=p.position_id,
                executor_id=p.executor_id,
                symbol=p.symbol,
                category=p.category.value,
                side=p.side.value,
                status=p.status.value,
                close_note=(
                    EXIT_FILLED_NOTE
                    if p.status is PositionStatus.CLOSING
                    and p.position_id in (exit_filled or set())
                    else None
                ),
                quantity=p.quantity,
                leverage=p.leverage,
                entry_price=p.entry_price,
                stop_loss_price=p.stop_loss_price,
                take_profit_price=p.take_profit_price,
                lifeboat_status=lb_status,
                lifeboat_price=lb_price,
                protection=prot,
                opened_at=p.opened_at,
                time_limit_s=p.time_limit_s,
                signal_id=p.signal_id,
                unrealized_usdt=pnl,
                keeper=keeper_last.get(p.position_id),
                keeper_peak_usdt=(
                    base * peak_bps / 1e4 if base and isinstance(peak_bps, int | float) else None
                ),
                keeper_peak_r=float(peak_r) if isinstance(peak_r, int | float) else None,
                keeper_giveback_usdt=(
                    base * give_bps / 1e4 if base and isinstance(give_bps, int | float) else None
                ),
                keeper_trail_active=bool(kp.get("trail_active")) if trail_on else None,
                mark_price=mark,
                price_src=src,
                notional_usdt=notional,
                margin_usdt=notional / p.leverage if notional and p.leverage else None,
                pnl_pct=pnl / notional * 100 if pnl is not None and notional else None,
                stop_loss_pct=p.stop_loss_pct,
                lifeboat_stop_pct=p.lifeboat_stop_pct,
                trailing_activation_pct=trail[0] if trail else None,
                trailing_stop_pct=trail[1] if trail else None,
                entry_fee_pct=entry_fee_pct,
                fees_est_usdt=fees,
                net_unrealized_usdt=net,
                today_usdt=today_pnl if today_from is not None else None,
                today_from=today_from,
                net_pnl_pct=net / notional * 100 if net is not None and notional else None,
                fee_bps=bps,
                fee_from_fills=fills_fee_bps is not None,
            )
        )
    return out


def _keeper_snap(inp: dict[str, object]) -> dict[str, object] | None:
    """管家决策记录里的快照 → ``peak_bps`` / ``giveback_bps`` / ``peak_r`` / ``trail_on`` /
    ``trail_active``；旧快照（方案 E 之前）没有这些键就返回 None。"""
    jev = inp.get("jev")
    raw = jev.get("snapshot") if isinstance(jev, dict) else None
    if not isinstance(raw, str):
        return None
    try:
        snap = json.loads(raw)
    except ValueError:
        return None
    pos = snap.get("pos") if isinstance(snap, dict) else None
    if not isinstance(pos, dict) or "peak_bps" not in pos:
        return None
    trail = snap.get("trail") if isinstance(snap.get("trail"), dict) else {}
    return {
        "peak_bps": pos.get("peak_bps"),
        "giveback_bps": pos.get("giveback_bps"),
        "peak_r": pos.get("peak_r"),
        "trail_on": bool(trail.get("on")),
        "trail_active": bool(trail.get("active")),
    }


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
        # 和团队页一样按数据流：慢环（选币、军师团）→ 快环 → 拍板 → 执行器 → hummingbot → 对账
        group("slow", "慢环", (Role.SCREENER, Role.COMMANDER)),
        group("fast", "快环", (Role.SENTINEL, Role.RISK_GUARD, Role.KEEPER, Role.PATROL)),
        group("gate", "拍板", (Role.GATE,)),
        group("exec", "执行器", (Role.EXECUTOR, Role.LIFEBOAT)),
        group("recon", "对账", (Role.RECONCILER,)),
    ]
    hb_state: LampState = "ok" if hb.ok else "bad"
    lamps.insert(4, Lamp(key="hb", label="hummingbot", state=hb_state, detail=hb.error or "已连接"))
    if not running:
        lamps.insert(0, Lamp(key="main", label="主程序", state="bad", detail="没在运行"))
    return lamps


def _with_fractions(
    card: CardView | None, app: AppConfig, risk: RiskConfig, equity: float | None
) -> CardView | None:
    """附上风控员真正会用的口径：仓位档折扣、实际杠杆、每笔风险金额（总览摘要行显示这些，不显示军师建议范围）。"""
    if card is None:
        return None
    fl = app.fast_loop
    fr = {"S": fl.size_fraction_s, "M": fl.size_fraction_m, "L": 1.0}
    return card.model_copy(
        update={
            "size_fractions": fr,
            "leverage_used": None
            if fl.card_caps_binding
            else max(1, math.floor(float(risk.max_leverage))),
            "risk_per_trade_usdt": equity * risk.risk_per_trade_pct / 100 if equity else None,
        }
    )


_STAGE_NAMES = {
    "analysts": "分析师报告",
    "debate": "多空辩论",
    "trader": "交易员计划",
    "risk": "风控辩论",
    "decision": "结论",
}


def _pending(sym: str, card: CardView, root: Path, now: datetime, tz: str) -> tuple[str, str]:
    """卡上有这个币却没有它的会议结论：按 meeting_live 说清楚是哪种情况（不许出现问号）。"""

    def hm(t: datetime) -> str:
        return t.astimezone(ZoneInfo(tz)).strftime("%H:%M")

    m = read_meeting(root, sym, now)
    if m is None:
        return "没开会", "这一轮没给这个币开会（没有会议记录），这张卡里它没有结论，不会开仓"
    if m.status == "running":
        no = f"第 {STAGES.index(m.stage) + 1} / {len(STAGES)} 步 · " if m.stage in STAGES else ""
        name = _STAGE_NAMES.get(m.stage or "", m.stage or "刚开始")
        return "开会中", f"还在开会（{no}{name}），开完后结论进下一张卡"
    if m.status == "failed":
        return "开会失败", f"开会失败：{m.error or '没写原因'}；这张卡里它没有结论，不会开仓"
    if m.status == "interrupted":
        return "会中断", f"开会中断：{m.error or '没写原因'}；这张卡里它没有结论，不会开仓"
    if m.status == "stale":
        mins = MEETING_STALE_S // 60
        return "会中断", f"开会中断（超过 {mins} 分钟没更新）；这张卡里它没有结论，不会开仓"
    end = m.finished_at or m.updated_at
    if end and end > card.created_at:
        return "开会中", f"这张卡出了以后才开完（{hm(end)}），结论进下一张卡"
    when = f"（{hm(end)} 开完）" if end else ""
    return "没出结论", f"会开完了{when}但没写出结论；这张卡里它没有结论，不会开仓"


FADE_BEFORE = timedelta(minutes=10)


def _with_pool_notes(card: CardView | None, now: datetime, tz: str) -> CardView | None:
    """每行写明「来自 hh:mm 的会 · hh:mm 到期」；离到期不到 10 分钟变灰。"""
    if card is None:
        return None

    def hm(t: datetime) -> str:
        return t.astimezone(ZoneInfo(tz)).strftime("%H:%M")

    rows = []
    for r in card.symbols:
        if r.issued_at is None or r.expires_at is None:
            rows.append(r)
            continue
        if now >= r.expires_at:  # 只剩持仓币：到期了，继续盯，不再开新仓
            note = f"来自 {hm(r.issued_at)} 的会 · {hm(r.expires_at)} 已过期·持仓中，不再开新仓"
            rows.append(r.model_copy(update={"source_note": note, "fading": True}))
            continue
        note = f"来自 {hm(r.issued_at)} 的会 · {hm(r.expires_at)} 到期"
        fading = r.expires_at - now <= FADE_BEFORE
        rows.append(r.model_copy(update={"source_note": note, "fading": fading}))
    return card.model_copy(update={"symbols": rows})


def _with_pending(card: CardView | None, root: Path, now: datetime, tz: str) -> CardView | None:
    if card is None:
        return None
    rows = [
        r.model_copy(
            update=dict(
                zip(
                    ("pending_label", "pending"),
                    _pending(r.symbol, card, root, now, tz),
                    strict=True,
                )
            )
        )
        if r.bias == "unknown"
        else r
        for r in card.symbols
    ]
    return card.model_copy(update={"symbols": rows})


_FLIP_DIR = {"long": "做多", "short": "做空", "neutral": "中性"}


def _flip_text(raw: object) -> str | None:
    if not isinstance(raw, dict):
        return None
    was, to = _FLIP_DIR.get(str(raw.get("confirmed"))), _FLIP_DIR.get(str(raw.get("candidate")))
    if was is None or to is None:
        return None
    progress = raw.get("progress")
    if not isinstance(progress, str):
        need = raw.get("need")
        progress = f"{min(int(raw.get('count') or 0), int(need or 0))}/{need}"
    rule = (
        "只由管家助手的两个模型确认，会回到原方向就取消"
        if raw.get("by") == "assistant"
        else "连续够场数的会都不是原方向才换成最新结论"
    )
    return (
        f"待确认翻转 {progress}：会议改判{to}，卡上仍按{was}只看不做"
        f"（不开新仓，持仓不按卡规则平）；{rule}"
    )


def _card(
    records: list[DecisionRecord], now: datetime, held: set[str] | None = None
) -> CardView | None:
    """今天最新一张作战卡：军师出卡记录 + 每个标的最新的会议结论（output 就是 CardSymbol）。"""
    cmd = [r for r in records if r.role is Role.COMMANDER]
    issued = [r for r in cmd if r.card_id and r.outcome is Outcome.ACTION]
    if not issued:
        return None
    last = issued[-1]
    names = [str(s) for s in last.output.get("symbols", [])]
    pool = last.output.get("pool")
    stamps: dict[str, tuple[datetime | None, datetime | None, bool]] = {}
    flips: dict[str, str] = {}
    if isinstance(pool, dict):
        for n, v in pool.items():
            if isinstance(v, dict) and (note := _flip_text(v.get("flip_pending"))):
                flips[str(n)] = note
            if isinstance(v, dict):
                stamps[str(n)] = (
                    _parse_ts(v.get("issued_at")),
                    _parse_ts(v.get("expires_at")),
                    # 出卡时的快照可能过时：有实时持仓就以实时为准
                    (str(n) in held) if held is not None else bool(v.get("held")),
                )
    # 指令池：过了自己有效期的币不再显示（持仓币留着，标明不再开新仓）
    names = [
        n
        for n in names
        if n not in stamps or stamps[n][1] is None or now < (stamps[n][1] or now) or stamps[n][2]
    ]
    per: dict[str, CardSymbol] = {}
    for r in cmd:
        if r.symbol and r.output and r.outcome is Outcome.ACTION and r.ts <= last.ts:
            try:
                # 记录里还夹着 usage（token 用量）、meeting_setup（偏空整改的分组）等不属于作战指令
                # 的键：契约禁止多余字段，不去掉的话校验会整条丢掉；只记录的 outlook 单独校验，
                # 坏了不连累卡
                per[r.symbol] = card_from_output(
                    {k: v for k, v in r.output.items() if k in CardSymbol.model_fields}
                )
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
                source_regime=cs.source_regime,
                source_market_view_id=cs.source_market_view_id,
                market_dir=cs.outlook.market_dir if cs.outlook else None,
                rel_dir=cs.outlook.rel_dir if cs.outlook else None,
                board_market_dir=cs.outlook.board_market_dir if cs.outlook else None,
                size_tier_max=cs.size_tier_max.value,
                stop_atr=(cs.stop_atr.min, cs.stop_atr.max),
                take_profit_r=(cs.take_profit_r.min, cs.take_profit_r.max),
                leverage=(cs.leverage.min, cs.leverage.max),
                notional_usdt=(cs.notional_usdt.min, cs.notional_usdt.max),
                allowed_signals=[k.value for k in cs.allowed_signals],
                key_levels=[LevelView(price=lv.price, kind=lv.kind.value) for lv in cs.key_levels],
                issued_at=stamps[n][0] if n in stamps else None,
                expires_at=stamps[n][1] if n in stamps else None,
                held=stamps[n][2] if n in stamps else False,
                flip_pending=flips.get(n),
            )
            if cs
            else CardRow(
                symbol=n,
                bias="unknown",
                category="other",
                issued_at=stamps[n][0] if n in stamps else None,
                expires_at=stamps[n][1] if n in stamps else None,
                held=stamps[n][2] if n in stamps else False,
            )
        )
    latest_market = None
    for record in cmd:
        raw = record.output.get("market_view") if record.output else None
        if isinstance(raw, dict):
            try:
                view = MarketView.model_validate(raw)
                if latest_market is None or view.version > latest_market.version:
                    latest_market = view
            except ValueError:
                continue
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
        current_market=latest_market,
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


def _with_current_market(card: CardView | None, root: Path, now: datetime) -> CardView | None:
    """独立观点可能来自前一天；磁盘状态比仅查本日出卡记录更完整。"""
    if card is None:
        return None
    try:
        data = json.loads((root / "market_view.json").read_text(encoding="utf-8"))
        view = MarketView.model_validate(data["latest_market_view"])
    except (OSError, ValueError, KeyError, TypeError):
        view = None
    if view is not None and (
        card.current_market is None or view.version >= card.current_market.version
    ):
        card = card.model_copy(update={"current_market": view})
    if card.current_market is not None and card.current_market.source == DESK_SOURCE:
        card = card.model_copy(update={"desk_latest": _desk_latest(root, now)})
    return card


def _desk_latest(root: Path, now: datetime) -> DeskLatest | None:
    """``market_desk_state.json`` 里最近一轮的概率和「4 小时之内」那段字（没开
    publish_intra 就没有）。"""
    try:
        latest = json.loads((root / DESK_STATE_FILE).read_text(encoding="utf-8"))["latest"]
        block = latest["intra"]
        if not block.get("panel", True):  # 只开了 keeper_intra
            return None
        return DeskLatest(
            run_at=datetime.fromisoformat(str(latest["t0"])),
            probs_4h=latest.get("probs_4h"),
            probs_1h=latest.get("probs_1h"),
            intra=compose_intra(block, now, float(block.get("max_age_min", 0.0))),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


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


def _feed(records: list[DecisionRecord], limit: int = FEED_LIMIT) -> list[FeedItem]:
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
                role=Role.GATE if _gate_skip(r) else r.role,
                symbol=r.symbol,
                text=r.summary,
                signal_id=r.signal_id,
            )
        )
        if len(out) >= limit:
            break
    return out


def _gate_skip(r: DecisionRecord) -> bool:
    """风控员记的拒绝里，只因为拍板人概率没过门槛的：算「拍板放弃」（黄），不算风控员拒绝（红）。"""
    if r.role is not Role.RISK_GUARD or r.outcome is not Outcome.REJECT:
        return False
    detail = (r.output or {}).get("detail")
    return isinstance(detail, dict) and detail.get("by") == "gate"


def _kind(r: DecisionRecord) -> FeedKind | None:
    o = r.outcome
    if r.role is Role.GATE:
        return "skip" if o in (Outcome.REJECT, Outcome.ERROR) else "info"
    if r.role is Role.RISK_GUARD:
        if _gate_skip(r):
            return "skip"
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
