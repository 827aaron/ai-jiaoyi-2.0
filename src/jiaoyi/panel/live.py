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
LIVE_BEHIND_MAX = 2000
"""游标落后这么多条以上（电脑睡醒、标签页放了很久）就不补了，重新从最近几条开始。"""
LIVE_STALE_S = 600
"""游标是这么多秒以前发的，同上：几小时前的动作不当新动作画。"""
LIVE_ROLLOVER_S = 600
"""UTC 换日后这么久里，游标继续带昨天那份文件的位置：写日志按记录自己的 ts 选文件，
ts 可能是换日前取的，晚到的几行会落进昨天的文件。"""

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
    "regime_market",
    "regime_watch",
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

ROUTINE: frozenset[str] = frozenset(
    {"key_level", "predict", "keeper_check", "against_log", "regime_watch", "book"}
)

CLOSE_BY: dict[str, tuple[str | None, str]] = {
    "keeper": ("keeper", "管家平"),
    "patrol": ("patrol", "巡逻兵平"),
    "stop_loss": ("lifeboat", "止损（交易所）"),
    "take_profit": ("lifeboat", "止盈（交易所）"),
    "trailing_stop": ("lifeboat", "移动止损（交易所）"),
    "tightened_stop": ("lifeboat", "收紧止损（交易所）"),
    "time_limit": ("lifeboat", "到时（交易所）"),
    "lifeboat": ("lifeboat", "保命止损"),
    "reconciler": ("reconciler", "对账平"),
    "manual": (None, "手动平"),
}
"""执行器平仓记录的 ``output.reason`` → (光点从哪个角色出发, 标签)。交易所托管的止损 / 止盈 /
到时记在保命止损那边，不算管家平的。None = 从仓位直接撤离（手动）。"""

_STOP_KINDS = frozenset({"stop_loss", "trailing_stop", "tightened_stop", "lifeboat"})


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
    via: str | None = None
    """平仓是谁促成的（角色 id，见 ``CLOSE_BY``）；光点从它出发打到仓位再撤离。"""


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
    """True = 没带游标、游标对不上（文件被换短、日期不对）或落后太多（``LIVE_BEHIND_MAX`` /
    ``LIVE_STALE_S``）：``events`` 是最近几条，页面不补画动画。"""
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


_REFUSED = re.compile(r"^(多|空) (?:拒绝|拍板放弃)：(.+)$", re.S)
_TRAIL = re.compile(
    r"^大盘移动止损：(?P<held>多|空)单锁住中（(?P<ref>\S+) (?:反弹|回落)过 (?P<k>[\d.]+) ATR 门槛，"
    r"现在离(?P<ext>低|高)点 (?P<atr>[\d.]+) ATR，已锁 (?P<mins>\d+) 分钟"
)
# 10-30 改措辞前的拒绝理由 / 锁住记录：只有当前离极值几倍 ATR，没有门槛
_TRAIL_OLD = re.compile(
    r"^大盘移动止损：(?P<ref>\S+) 从.段(?P<ext>低|高)点(?:反弹|回落) (?P<atr>[\d.]+) ATR，"
    r"暂停新(?P<held>多|空)单(?:（锁住 (?P<mins>\d+) 分钟）)?"
)
_HOLD = re.compile(r"^拿住：平仓概率 ([\d.]+)(?:，(.*))?$", re.S)


def _lead(r: DecisionRecord, text: str) -> str:
    """把最有信息的部分放前面：拒绝写理由在前，管家拿住写平仓概率在前。"""
    side = ""
    if m := _REFUSED.match(text):
        side, text = m.group(1), m.group(2)
        if r.symbol and text.startswith(r.symbol):
            text = text[len(r.symbol) :].lstrip(" ：:")
    if m := _TRAIL.match(text) or _TRAIL_OLD.match(text):
        g = m.groupdict()
        head = f"已锁 {g['mins']} 分钟 · " if g["mins"] else ""
        k = f" / 门槛 {g['k']}" if g.get("k") else ""
        now = f"{g['ref']} 现在离{g['ext']}点 {g['atr']}{k} ATR"
        return f"大盘移动止损，{g['held']}单锁住（{head}{now}）"
    if m := _HOLD.match(text):
        p, rest = m.groups()
        rest = "只记录" if rest and rest.startswith("只记录") else rest
        return f"拿住 · 平仓概率 {p}" + (f"（{rest}）" if rest else "")
    return f"{text}（{side}）" if side else text


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


def _close_by(out: dict[str, Any]) -> tuple[str | None, str]:
    reason = str(out.get("reason") or "")
    if reason in _STOP_KINDS and "主网价格" in str(out.get("detail") or ""):
        return "patrol", "模拟止损"  # 模拟盘按主网价格越过止损平，是巡逻兵记的
    return CLOSE_BY.get(reason, (None, "平仓"))


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
            v = out.get("version")
            if isinstance(v, int) and not isinstance(v, bool) and "symbols" in out:
                return "card", tone, f"v{v}"
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
            flip = out.get("regime_flip")
            if isinstance(flip, dict):
                if r.outcome is not Outcome.ACTION:  # 疑似 / 被拦：只是哨兵在看，不算大盘更新
                    return "regime_watch", "info", None
                if flip.get("scope") == "market_desk":
                    return "regime", tone, "大盘变化 → 大盘台"
                return "regime_market", tone, "大盘变化 → 独立更新"
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
                return "close", "close", _close_by(out)[1]
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
        text=_short(_lead(r, _strip_symbol(r)), 300),
        badge=badge,
        signal_kind=sig,
        pnl_usdt=pnl,
        via=_close_by(out)[0] if kind == "close" else None,
    )


def parse_cursor(cursor: str | None) -> tuple[dict[date, int], float] | None:
    """``<日期>:<条数>[|<日期>:<条数>]@<发出时的 unix 秒>`` → ({日期: 条数}, 发出时间)。"""
    if not cursor or "@" not in cursor:
        return None
    body, _, at = cursor.rpartition("@")
    out: dict[date, int] = {}
    try:
        for part in body.split("|"):
            day, _, n = part.partition(":")
            out[date.fromisoformat(day)] = int(n)
        return out, float(at)
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
        yday = today - timedelta(days=1)
        got = parse_cursor(cursor)
        records: list[DecisionRecord] = []
        counts: dict[date, int] = {}
        reset = (
            got is None
            or not got[0]
            or any(n < 0 or d not in (today, yday) for d, n in got[0].items())
            or now.timestamp() - got[1] > LIVE_STALE_S
        )
        if got is not None and not reset:
            pos = got[0]
            for day in (yday, today):
                start = pos.get(day, 0 if day == today else None)
                if start is None:
                    continue
                recs, total = self.files.decisions_from(day, start)
                if total < start:  # 文件变短了（被换掉）
                    reset = True
                    break
                records += recs
                counts[day] = total
            reset = reset or len(records) > LIVE_BEHIND_MAX
        if reset:
            _, total = self.files.decisions_from(today, 1 << 62)
            records, counts[today] = self.files.decisions_from(today, max(0, total - LIVE_TAIL))
            if len(records) < LIVE_TAIL:
                y, ny = self.files.decisions_from(yday, 0)
                records = y[max(0, ny - (LIVE_TAIL - len(records))) :] + records
                counts[yday] = ny
        midnight = datetime.combine(today, datetime.min.time(), tzinfo=now.tzinfo)
        rollover = (now - midnight).total_seconds() < LIVE_ROLLOVER_S
        if rollover and yday not in counts:
            counts[yday] = self.files.decisions_from(yday, 1 << 62)[1]
        days = [yday, today] if rollover else [today]
        cur = "|".join(f"{d.isoformat()}:{counts[d]}" for d in days)
        events, dropped = coalesce([to_event(r) for r in records])
        st = self.files.module_status().get(Role.BOOK_READER.value)
        return LiveFeed(
            cursor=f"{cur}@{int(now.timestamp())}",
            reset=reset,
            events=events,
            coalesced=dropped,
            ring=self.ring(),
            book_calls=st.today.calls if st else None,
        )
