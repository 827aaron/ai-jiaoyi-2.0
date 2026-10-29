"""团队页的实时动作：决策日志里新写的几行，翻成页面上一个个能画出来的动作。

页面每 1–2 秒带着游标来问一次（``/api/team/live?cursor=``），只拿游标之后的记录。游标是
``<UTC 日期>:<这一天第几条>``：日志按 UTC 日期分文件、只追加，所以这个位置不会变。

每条记录归成一种 ``kind``，页面按 kind 决定动画走哪条路（哨兵 → 拍板人、执行器 → 持仓圈……）。
持仓管家的例行复查、预测员每轮预测这类「常规」动作一天上万条，标 ``routine``，页面合并着画。
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from jiaoyi.contracts import DecisionRecord, Outcome, Role
from jiaoyi.panel.files import PanelFiles

LIVE_TAIL = 30
"""没带游标（刚打开页面）时给最近几条，只填「实时动作」列表，不画动画。"""
LIVE_MAX = 60
"""一次最多给几条；更多的按角色和种类只给条数（``coalesced``），免得一阵爆发把页面淹了。"""

Tone = Literal["open", "close", "reject", "error", "info"]

KINDS: tuple[str, ...] = (
    "screen",
    "meeting",
    "card",
    "desk",
    "key_level",
    "commander",
    "signal",
    "against_keeper",
    "against_meeting",
    "against_log",
    "regime",
    "sentinel",
    "predict",
    "verdict",
    "gate",
    "risk_pass",
    "risk_reject",
    "risk",
    "fill",
    "close",
    "backfill",
    "executor",
    "keeper_check",
    "keeper_close",
    "keeper",
    "assistant",
    "patrol",
    "patrol_close",
    "paper_stop",
    "lifeboat",
    "reconcile",
    "alert",
    "book",
)
"""页面认识的全部动作种类（JS 那边每一种都有动画路线，测试对着这个表查）。"""

ROUTINE: frozenset[str] = frozenset({"key_level", "predict", "keeper_check", "against_log", "book"})


class LiveEvent(BaseModel):
    id: str
    ts: datetime
    role: Role
    kind: str
    tone: Tone
    routine: bool = False
    symbol: str | None = None
    side: str | None = None
    position_id: str | None = None
    signal_id: str | None = None
    text: str
    """决策日志的一句话，去掉开头的币名（币名单独在 ``symbol``）。"""
    badge: str | None = None
    """动画上挂的短标签：拍板人「take 0.65」、风控员「拒：…」、作战卡「v399」、管家助手两个 p。"""
    signal_kind: str | None = None
    pnl_usdt: float | None = None
    """平仓回填的净盈亏（U），撤离箭头上标。"""


class LiveCount(BaseModel):
    role: Role
    kind: str
    count: int


class RingPosition(BaseModel):
    position_id: str
    symbol: str
    side: str
    status: str
    entry_price: float | None
    stop_loss_price: float | None
    quantity: float
    opened_at: datetime


class LiveFeed(BaseModel):
    cursor: str
    reset: bool
    """True = 没带游标或游标对不上（换日太久、文件被换）：``events`` 是最近几条，页面不补画动画。"""
    events: list[LiveEvent]
    """时间先后（旧的在前）。"""
    coalesced: list[LiveCount] = []
    ring: list[RingPosition]
    """在管的持仓（没平完的），持仓圈中间画它们。"""
    book_calls: int | None = None
    """盘口员今天收到的行情条数（模块状态），页面按两次的差写「+N 条」，不逐条画。"""


def _num(v: object) -> float | None:
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None


def _short(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def _strip_symbol(r: DecisionRecord) -> str:
    s = r.summary
    if r.symbol and s.startswith(r.symbol):
        s = s[len(r.symbol) :].lstrip(" ：:")
    return s or r.summary


def _verdict(out: dict[str, Any]) -> str | None:
    jev = out.get("jev")
    take = jev.get("answers", {}).get("take") if isinstance(jev, dict) else None
    if not isinstance(take, dict):
        return None
    t, s = _num(take.get("take")), _num(take.get("skip"))
    if t is None or s is None:
        return None
    return f"take {t:.2f}" if t >= s else f"skip {s:.2f}"


def _judges(out: dict[str, Any]) -> str | None:
    ka = out.get("keeper_assistant")
    judges = ka.get("judges") if isinstance(ka, dict) else None
    if not isinstance(judges, list):
        return None
    parts = []
    for j in judges:
        if isinstance(j, dict):
            p = _num(j.get("p_yes"))
            parts.append(f"{j.get('judge', '?')} {'—' if p is None else f'{p:.2f}'}")
    return " / ".join(parts) or None


_CARD_V = re.compile(r"作战卡 v(\d+)")


def classify(r: DecisionRecord) -> tuple[str, Tone, str | None]:
    """记录 → (kind, tone, badge)：看角色、结果和 output 里有哪些键；认不出的按角色给通用种类。"""
    out = r.output
    tone: Tone = "error" if r.outcome is Outcome.ERROR else "info"
    match r.role:
        case Role.SCREENER:
            return "screen", tone, None
        case Role.MARKET:
            return "meeting", tone, None
        case Role.COMMANDER:
            if "market_desk" in out:
                return "desk", tone, None
            if "key_level" in out:
                return "key_level", tone, None
            if "bias" in out and r.symbol:
                return "meeting", tone, str(out.get("bias"))
            if m := _CARD_V.search(r.summary):
                return "card", tone, f"v{m.group(1)}"
            return "commander", tone, None
        case Role.SENTINEL:
            ag = out.get("against_signal")
            if isinstance(ag, dict):
                acts = ag.get("actions") or []
                if "keeper_recheck" in acts or "assistant" in acts or ag.get("position_id"):
                    return "against_keeper", "info", "反向"
                if "meeting" in acts:
                    return "against_meeting", "info", "反向 → 临时会"
                return "against_log", "info", None
            if "regime_flip" in out:
                return "regime", tone, None
            if r.outcome is Outcome.ACTION and "kind" in out:
                return "signal", "info", None
            return "sentinel", "reject" if r.outcome is Outcome.REJECT else tone, None
        case Role.PREDICTOR:
            if r.outcome is Outcome.ERROR:
                return "predict", "error", "失败"
            return "predict", "info", None
        case Role.GATE:
            v = _verdict(out)
            if v and r.outcome is not Outcome.ERROR:
                return "verdict", "info", v
            return "gate", tone, "超时" if r.outcome is Outcome.ERROR else None
        case Role.RISK_GUARD:
            if r.outcome is Outcome.PASS:
                return "risk_pass", "open", "放行"
            if r.outcome is Outcome.REJECT:
                reasons = out.get("reasons")
                why = reasons[0] if isinstance(reasons, list) and reasons else _strip_symbol(r)
                return "risk_reject", "reject", "拒：" + _short(str(why), 16)
            return "risk", tone, None
        case Role.EXECUTOR:
            if r.outcome is Outcome.ACTION and "entry_price" in out:
                return "fill", "open", "成交"
            if r.outcome is Outcome.CLOSE:
                return "close", "close", "平仓"
            bf = out.get("backfill")
            if isinstance(bf, dict) and _num(bf.get("realized_pnl_usdt")) is not None:
                return "backfill", "close", None
            return "executor", tone, None
        case Role.KEEPER:
            if r.outcome is Outcome.CLOSE:
                return "keeper_close", "close", "判平"
            if r.outcome in (Outcome.HOLD, Outcome.INFO) and "against" not in out:
                return "keeper_check", tone, None
            return "keeper", tone, None
        case Role.KEEPER_ASSISTANT:
            return "assistant", tone, _judges(out)
        case Role.PATROL:
            if "paper_stop" in out:
                return "paper_stop", "close", "模拟止损"
            if r.outcome is Outcome.CLOSE:
                return "patrol_close", "close", "全平"
            return "patrol", tone, None
        case Role.LIFEBOAT:
            return "lifeboat", tone, None
        case Role.RECONCILER:
            return "reconcile", tone, None
        case Role.ALERTING:
            return "alert", tone, None
        case Role.BOOK_READER:
            return "book", tone, None
    return "commander", tone, None  # pragma: no cover - every Role is matched above


def to_event(r: DecisionRecord) -> LiveEvent:
    kind, tone, badge = classify(r)
    out = r.output
    pnl = None
    if kind == "backfill":
        bf = out.get("backfill")
        pnl = _num(bf.get("realized_pnl_usdt")) if isinstance(bf, dict) else None
    side = out.get("side") if isinstance(out.get("side"), str) else None
    sig = out.get("kind") if kind == "signal" and isinstance(out.get("kind"), str) else None
    routine = kind in ROUTINE or (
        kind == "reconcile" and r.outcome is Outcome.INFO and bool(out.get("informational"))
    )
    return LiveEvent(
        id=r.record_id,
        ts=r.ts,
        role=r.role,
        kind=kind,
        tone=tone,
        routine=routine,
        symbol=r.symbol,
        side=side,
        position_id=r.position_id,
        signal_id=r.signal_id,
        text=_short(_strip_symbol(r), 120),
        badge=badge,
        signal_kind=sig,
        pnl_usdt=pnl,
    )


def parse_cursor(cursor: str | None) -> tuple[date, int] | None:
    if not cursor or ":" not in cursor:
        return None
    day, _, n = cursor.partition(":")
    try:
        return date.fromisoformat(day), int(n)
    except ValueError:
        return None


def coalesce(
    events: list[LiveEvent], limit: int = LIVE_MAX
) -> tuple[list[LiveEvent], list[LiveCount]]:
    """超过 ``limit`` 条时：先丢常规动作，再丢最旧的；丢掉的按（角色，种类）计数。顺序不变。"""
    if len(events) <= limit:
        return events, []
    order = sorted(range(len(events)), key=lambda i: (events[i].routine, -i))
    keep = set(order[:limit])  # 先留真动作（新的在前），名额还有就留最新的常规动作
    kept = [e for i, e in enumerate(events) if i in keep]
    dropped = Counter((e.role, e.kind) for i, e in enumerate(events) if i not in keep)
    return kept, [LiveCount(role=ro, kind=k, count=n) for (ro, k), n in dropped.most_common()]


class LiveSource:
    """持仓圈要的在管仓位：positions.json 有 1 MB，按文件大小和修改时间缓存，文件不变就不重读。"""

    def __init__(self, files: PanelFiles) -> None:
        self.files = files
        self._ring_key: tuple[int, int] | None = None
        self._ring: list[RingPosition] = []

    def ring(self) -> list[RingPosition]:
        path = Path(self.files.root) / "positions.json"
        try:
            st = path.stat()
            key = (st.st_mtime_ns, st.st_size)
        except OSError:
            self._ring_key, self._ring = None, []
            return []
        if key != self._ring_key:
            self._ring = [
                RingPosition(
                    position_id=p.position_id,
                    symbol=p.symbol,
                    side=p.side.value,
                    status=p.status.value,
                    entry_price=p.entry_price,
                    stop_loss_price=p.stop_loss_price,
                    quantity=p.quantity,
                    opened_at=p.opened_at,
                )
                for p in sorted(self.files.ledger().live(), key=lambda p: p.opened_at)
            ]
            self._ring_key = key
        return self._ring

    def feed(self, now: datetime, cursor: str | None) -> LiveFeed:
        today = now.date()
        pos = parse_cursor(cursor)
        records: list[DecisionRecord] = []
        total_today = 0
        reset = pos is None or pos[0] not in (today, today - timedelta(days=1))
        if pos is not None and not reset:
            day, n = pos
            got, total = self.files.decisions_from(day, n)
            if total < n:  # 文件变短了（被换掉）：当作没游标
                reset = True
            elif day == today:
                records, total_today = got, total
            else:
                rest, total_today = self.files.decisions_from(today, 0)
                records = got + rest
        if reset:
            _, total = self.files.decisions_from(today, 1 << 62)
            records, total_today = self.files.decisions_from(today, max(0, total - LIVE_TAIL))
            if len(records) < LIVE_TAIL:
                y, ny = self.files.decisions_from(today - timedelta(days=1), 0)
                records = y[max(0, ny - (LIVE_TAIL - len(records))) :] + records
        events, counts = coalesce([to_event(r) for r in records])
        st = self.files.module_status().get(Role.BOOK_READER.value)
        return LiveFeed(
            cursor=f"{today.isoformat()}:{total_today}",
            reset=reset,
            events=events,
            coalesced=counts,
            ring=self.ring(),
            book_calls=st.today.calls if st else None,
        )
