"""总览「防线状态」：每条止损 / 退出防线一行——开关（面板读到的现行配置）、此刻、今天、最近一次。

只读本机文件：决策日志（今天 = ``day_reset_tz`` 的本地日）、positions.json、paper_stops.json、
data/market_trail.json。关着的规则只给「未启用」和它的配置键，不算今天的数。
"""

from __future__ import annotations

import json
import math
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from jiaoyi.contracts import AppConfig, DecisionRecord, Outcome, RiskConfig, Role
from jiaoyi.core.market_trail import LABEL, STALE_S, STATE_FILE, TrailSeries
from jiaoyi.core.paper_stop import PaperStop
from jiaoyi.core.recorder import local_day
from jiaoyi.panel.card_dir import CARD_CLOSE, action_kind, commander_steps
from jiaoyi.panel.files import Ledger

if TYPE_CHECKING:
    from jiaoyi.panel.overview import Kpis, PositionView

Level = Literal["off", "idle", "ok", "active", "warn"]
"""此刻这一格的颜色：off 未启用 / idle 没事 / ok 在保护 / active 正在起作用 / warn 要看一眼。"""


class DefenceRow(BaseModel):
    key: str
    name: str
    on: bool
    config_key: str
    """``段.键``：关着时鼠标悬停显示它。"""
    setting: str = ""
    """开着时的要点（阈值、参照）。"""
    now: str = ""
    level: Level = "idle"
    today: str = ""
    last_ts: datetime | None = None
    last_text: str | None = None
    link: str | None = None
    """点这一行去哪儿（页面 hash）；None = 没有对应页面。"""


_SIDE_CN = {-1: "空单", 1: "多单"}
_LEG_CN = {-1: "跌段", 1: "涨段"}
_EXT_CN = {-1: "低点", 1: "高点"}
_OPP = {"long": "short", "short": "long"}
_TRIGGER_CN = {"held_reverse_loss": "反向信号", "held_desk_flip": "大盘台转向"}


def _last(recs: Sequence[DecisionRecord]) -> tuple[datetime | None, str | None]:
    if not recs:
        return None, None
    r = max(recs, key=lambda x: x.ts)
    return r.ts, r.summary


def _off(key: str, name: str, config_key: str, link: str | None) -> DefenceRow:
    return DefenceRow(key=key, name=name, on=False, config_key=config_key, level="off", link=link)


def _trail_series(root: Path, ref: str) -> TrailSeries | None:
    try:
        raw = json.loads((root / STATE_FILE).read_text(encoding="utf-8"))
        d = raw["series"][ref]
        return TrailSeries.from_json(d) if d is not None else None
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


class MarketTrailView(BaseModel):
    """团队页风控员节点的大盘闸门一行、详情顶上的状态条（总览那一行用同一份）。"""

    on: bool
    ref: str = ""
    k: float = 0.0
    state: Literal["off", "none", "warmup", "no_leg", "open", "locked"] = "off"
    leg: str | None = None
    """跌段 / 涨段。"""
    guards: str | None = None
    """这一段管的方向：空单 / 多单。"""
    extreme_price: float | None = None
    """段内极值的价格；参照是 BTC+ETH 平均（指数，不是价格）时为 None。"""
    retrace_atr: float | None = None
    locked_min: int | None = None
    stale_at: datetime | None = None
    """参照的数据停在这一分钟（超过 5 分钟没更新）；正常是 None。"""
    locks_today: int = 0
    blocked_today: int = 0
    line: str = "大盘闸门：未启用"
    """节点上那一行。"""
    now: str = ""
    level: Level = "off"
    last_ts: datetime | None = None
    last_text: str | None = None


def is_trail_block(r: DecisionRecord) -> bool:
    return r.outcome is Outcome.REJECT and (
        (r.output.get("detail") or {}).get("by") == "market_trail"
        or any(str(x).startswith("大盘移动止损") for x in r.output.get("reasons") or [])
    )


def market_trail_view(
    root: Path, app: AppConfig, today: list[DecisionRecord], now: datetime, tz: str
) -> MarketTrailView:
    fl = app.fast_loop
    if not fl.signal_market_trail_on:
        return MarketTrailView(on=False)
    ref, k = fl.signal_market_trail_ref, fl.signal_market_trail_k
    label = LABEL[ref]
    notes = [r for r in today if r.role is Role.RISK_GUARD and "market_trail" in r.output]
    blocked = [r for r in today if is_trail_block(r)]
    v = MarketTrailView(
        on=True,
        ref=label,
        k=k,
        locks_today=sum(1 for r in notes if r.output["market_trail"].get("kind") == "lock"),
        blocked_today=len(blocked),
        level="idle",
    )
    v.last_ts, v.last_text = _last([*notes, *blocked])
    s = _trail_series(root, ref)
    if s is None:
        v.state, v.now, v.line = "none", "还没有状态（主程序没跑过这一版）", "大盘闸门：还没有状态"
        return v
    if not s.warm:
        v.state, v.line = "warmup", "大盘闸门：历史不足 24 小时"
        v.now = f"历史不足 24 小时（已有 {s.count / 60:.0f} 小时），暂不拦"
        return v
    if s.leg is None:
        v.state, v.line = "no_leg", "大盘闸门：没有跌段 / 涨段，不拦"
        v.now = f"{label} 还没有跌段 / 涨段，不拦"
        return v
    side, rt = s.leg.side, s.retrace_atr() or 0.0
    v.leg, v.guards, v.retrace_atr = _LEG_CN[side], _SIDE_CN[side], round(rt, 4)
    if ref != "btc_eth":
        v.extreme_price = math.exp(s.leg.extreme)
    if s.locked is not None:
        mins = max(0, int(now.timestamp() - (s.leg.stopped_at or now.timestamp())) // 60)
        v.state, v.locked_min, v.level = "locked", mins, "warn"
        v.now = (
            f"{_SIDE_CN[side]}锁住中 · 已锁 {mins} 分钟 · {label} 现在离{_EXT_CN[side]} "
            f"{rt:.1f} / 门槛 {k:.1f} ATR"
        )
        v.line = f"大盘闸门：{_SIDE_CN[side]}锁住 {mins} 分钟"
    else:
        v.state, v.level = "open", "ok"
        gap = f"{rt:.1f} / {k:g} ATR"
        v.now = f"{label} {_LEG_CN[side]}，{_SIDE_CN[side]}放行（离{_EXT_CN[side]} {gap}）"
        v.line = f"大盘闸门：{_SIDE_CN[side]}放行 {rt:.1f}/{k:g} ATR"
    if s.last_t is not None and now.timestamp() - s.last_t > STALE_S:
        v.stale_at = datetime.fromtimestamp(s.last_t, UTC)
        v.now += f"；数据停在 {v.stale_at.astimezone(ZoneInfo(tz)):%H:%M}"
    return v


def market_trail_row(
    root: Path, app: AppConfig, today: list[DecisionRecord], now: datetime, tz: str
) -> DefenceRow:
    fl = app.fast_loop
    key, name, link = "market_trail", "大盘移动止损", "#team/risk_guard"
    if not fl.signal_market_trail_on:
        return _off(key, name, "fast_loop.signal_market_trail_on", link)
    v = market_trail_view(root, app, today, now, tz)
    return DefenceRow(
        key=key,
        name=name,
        on=True,
        config_key="fast_loop.signal_market_trail_on",
        setting=f"参照 {v.ref} · {v.k:g} 倍 ATR",
        now=v.now,
        level=v.level,
        today=f"锁 {v.locks_today} 次 · 拦下开仓 {v.blocked_today} 笔",
        last_ts=v.last_ts,
        last_text=v.last_text,
        link=link,
    )


def native_trailing_row(
    app: AppConfig, ledger: Ledger, positions: Sequence[PositionView], now: datetime, tz: str
) -> DefenceRow:
    fl = app.fast_loop
    key, name, link = "trailing_native", "币安移动止损", "#positions"
    if not (fl.trailing_on and fl.trailing_native):
        cfg = "fast_loop.trailing_native" if fl.trailing_on else "fast_loop.trailing_on"
        return _off(key, name, cfg, link)
    prot = [p.protection for p in positions if p.protection is not None]
    native = sum(1 for p in prot if p.native_trailing)
    pending = sum(1 for p in prot if p.native_pending)
    fallback = sum(1 for p in prot if p.native_fallback)
    parts = [f"币安跟着 {native} 笔"]
    if pending:
        parts.append(f"等挂上 {pending} 笔")
    if fallback:
        parts.append(f"没用上、程序自己跟 {fallback} 笔")
    day = local_day(now, tz)
    placed = fell = 0
    for p in ledger.positions:
        if local_day(p.opened_at, tz) != day:
            continue
        raw = ledger.protection.get(p.position_id)
        if not isinstance(raw, dict):
            continue
        legs = [x for x in raw.get("legs") or [] if isinstance(x, dict)]
        if any(x.get("kind") == "trailing_stop" and x.get("algo_id") for x in legs):
            placed += 1
        if raw.get("native_fallback"):
            fell += 1
    closes = [
        p
        for p in ledger.positions
        if p.closed_at is not None
        and local_day(p.closed_at, tz) == day
        and p.close_reason is not None
        and p.close_reason.value == "trailing_stop"
    ]
    last = max(closes, key=lambda p: p.closed_at or now, default=None)
    return DefenceRow(
        key=key,
        name=name,
        on=True,
        config_key="fast_loop.trailing_native",
        setting=f"{fl.trail_start_r:g}R 启动",
        now="；".join(parts) if positions else "没有持仓",
        level="warn" if fallback else "ok" if native else "idle",
        today=f"挂上 {placed} · 没用上 {fell} · 移动止损平 {len(closes)} 笔",
        last_ts=last.closed_at if last else None,
        last_text=f"{last.symbol} 移动止损平仓" if last else None,
        link=link,
    )


def frozen_peak_row(
    app: AppConfig, ledger: Ledger, today: list[DecisionRecord], live_ids: set[str]
) -> DefenceRow:
    fl = app.fast_loop
    key, name, link = "frozen_peak", "冻结峰值止盈", "#positions"
    if not fl.frozen_peak_exit_on:
        return _off(key, name, "fast_loop.frozen_peak_exit_on", link)
    st = Counter(v for pid, v in ledger.frozen_peak.items() if pid in live_ids)
    hits = [
        r
        for r in today
        if r.role is Role.KEEPER
        and r.outcome is Outcome.CLOSE
        and (r.output.get("frozen_peak_exit") or {}).get("triggered") is True
    ]
    ts, text = _last(hits)
    return DefenceRow(
        key=key,
        name=name,
        on=True,
        config_key="fast_loop.frozen_peak_exit_on",
        setting=f"持仓满 {fl.frozen_peak_exit_after_s / 60:g} 分钟冻结",
        now=f"收集中 {st['collecting']} 笔 · 已冻结 {st['frozen']} 笔",
        level="active" if st["frozen"] else "ok" if st["collecting"] else "idle",
        today=f"到价平 {len({r.position_id for r in hits})} 笔",
        last_ts=ts,
        last_text=text,
        link=link,
    )


def opposite_card_row(
    app: AppConfig, positions: Sequence[PositionView], today: list[DecisionRecord]
) -> DefenceRow:
    mode = app.fast_loop.opposite_card_action
    kinds = [(r, action_kind(r)) for r in today if r.role is Role.KEEPER]
    closes = [r for r, k in kinds if k is not None and CARD_CLOSE.get(k) == "opposite_card"]
    closes = [r for r in closes if r.outcome is Outcome.CLOSE]
    tight = {r.position_id for r, k in kinds if k == "tighten"}
    blocked = {r.position_id for r, k in kinds if k == "blocked"}
    against = [p for p in positions if p.hold_card is not None and p.hold_card.state == "against"]
    extra = (f" · 收紧 {len(tight)}" if tight else "") + (
        f" · 拦下 {len(blocked)}" if blocked else ""
    )
    ts, text = _last([r for r, k in kinds if k is not None and k not in _NEUTRAL])
    return DefenceRow(
        key="opposite_card",
        name="反向卡平仓",
        on=True,
        config_key="fast_loop.opposite_card_action",
        setting="卡反就全平" if mode == "close" else "价格确认才平，否则收紧止损",
        now=f"卡和持仓反着的 {len(against)} 笔" if against else "没有卡反着的持仓",
        level="warn" if against else "idle",
        today=f"平 {len({r.position_id for r in closes})} 笔{extra}",
        last_ts=ts,
        last_text=text,
        link="#team/keeper",
    )


_NEUTRAL = frozenset({"neutral_close", "neutral_tighten", "neutral_hold"})


def held_event_row(
    app: AppConfig,
    ledger: Ledger,
    positions: Sequence[PositionView],
    today: list[DecisionRecord],
    recent: list[DecisionRecord],
    now: datetime,
) -> DefenceRow:
    fl = app.fast_loop
    key, name, link = "held_event", "亏损持仓币开会", "#team/patrol"
    loss_r, desk = fl.patrol_held_event_loss_r, fl.patrol_held_event_on_desk_flip
    if loss_r <= 0 and not desk:
        return _off(key, name, "fast_loop.patrol_held_event_loss_r", link)
    asks = [r for r in today if r.role is Role.PATROL and "held_event" in r.output]
    queued = [r for r in asks if r.outcome is Outcome.ACTION]
    by = Counter(str(r.output["held_event"].get("trigger")) for r in queued)
    side = {p.position_id: p.side.value for p in ledger.positions}
    steps = commander_steps(recent, now)
    flipped = 0
    for r in queued:
        held = side.get(r.position_id or "")
        nxt = next(
            (s for s in steps.get(r.symbol or "", []) if s.ts > r.ts and s.meeting is not None),
            None,
        )
        if held is not None and nxt is not None and nxt.card == _OPP.get(held):
            flipped += 1
    parts = [f"{_TRIGGER_CN.get(t, t)} {n}" for t, n in sorted(by.items())]
    if len(asks) > len(queued):
        parts.append(f"没开成 {len(asks) - len(queued)}")
    setting = [
        f"亏 ≥{loss_r:g}R + 反向信号" if loss_r > 0 else "",
        "大盘台转向 · 持仓在亏" if desk else "",
    ]
    losing = sum(1 for p in positions if (p.net_unrealized_usdt or 0.0) < 0)
    ts, text = _last(asks)
    return DefenceRow(
        key=key,
        name=name,
        on=True,
        config_key="fast_loop.patrol_held_event_loss_r",
        setting=" · ".join(x for x in setting if x),
        now=f"在亏的持仓 {losing} 笔" if positions else "没有持仓",
        level="active" if losing else "idle",
        today=f"申请 {len(queued)} 场（{' · '.join(parts)}）· 卡翻了 {flipped}"
        if asks
        else "没申请",
        last_ts=ts,
        last_text=text,
        link=link,
    )


def paper_stop_row(app: AppConfig, ledger: Ledger, now: datetime, tz: str) -> DefenceRow:
    fl = app.fast_loop
    key, name, link = "paper_stop", "固定止损 / 主网价格碰止损", "#history"
    if fl.paper_stop_on_mainnet == "off" or app.mode.value == "live":
        return _off(key, name, "fast_loop.paper_stop_on_mainnet", link)
    day = local_day(now, tz)
    booked = [b for b in ledger.paper.values() if local_day(b.booked_at, tz) == day]

    def profit(b: PaperStop) -> bool:
        long = b.side.value == "long"
        return b.stop_price > b.entry_price if long else b.stop_price < b.entry_price

    win = sum(1 for b in booked if profit(b))
    last = max(booked, key=lambda b: b.booked_at, default=None)
    text = None
    if last is not None:
        where = "盈利侧（移动止损）" if profit(last) else "亏损侧（止损）"
        text = f"{last.symbol} 主网价 {last.cross_price:g} 碰到{where} {last.stop_price:g}"
    src = "标记价" if fl.paper_stop_on_mainnet == "mark" else "成交价"
    return DefenceRow(
        key=key,
        name=name,
        on=True,
        config_key="fast_loop.paper_stop_on_mainnet",
        setting=f"按主网{src}判",
        now="固定止损 + 主网价同时盯着",
        level="ok",
        today=f"平 {len(booked)} 笔（盈利侧 {win} · 亏损侧 {len(booked) - win}）",
        last_ts=last.booked_at if last else None,
        last_text=text,
        link=link,
    )


def halt_row(risk: RiskConfig, kpis: Kpis) -> DefenceRow:
    pct = kpis.day_pnl_pct
    if kpis.halted:
        now_txt, level = "已熔断：今天只许平仓", "warn"
    elif kpis.to_halt_usdt is None:
        now_txt, level = "拿不到今天的起始资金，算不出离熔断多远", "idle"
    else:
        day = f"今日 {pct:+.2f}%，" if pct is not None else ""
        now_txt = f"{day}离熔断还差 {kpis.to_halt_usdt:,.0f} U"
        near = pct is not None and pct <= -0.5 * risk.daily_loss_halt_pct
        level = "warn" if near else "ok"
    return DefenceRow(
        key="daily_halt",
        name="单日熔断",
        on=True,
        config_key="risk.daily_loss_halt_pct",
        setting=f"日亏 {risk.daily_loss_halt_pct:g}% 停开新仓",
        now=now_txt,
        level=level,
        today="已触发" if kpis.halted else "未触发",
        link="#funds",
    )


def defence_rows(
    root: Path,
    app: AppConfig,
    risk: RiskConfig,
    ledger: Ledger,
    positions: Sequence[PositionView],
    kpis: Kpis,
    today: list[DecisionRecord],
    recent: list[DecisionRecord],
    now: datetime,
) -> list[DefenceRow]:
    tz = risk.day_reset_tz
    live_ids = {p.position_id for p in positions}
    return [
        market_trail_row(root, app, today, now, tz),
        native_trailing_row(app, ledger, positions, now, tz),
        frozen_peak_row(app, ledger, today, live_ids),
        opposite_card_row(app, positions, today),
        held_event_row(app, ledger, positions, today, recent, now),
        paper_stop_row(app, ledger, now, tz),
        halt_row(risk, kpis),
    ]
