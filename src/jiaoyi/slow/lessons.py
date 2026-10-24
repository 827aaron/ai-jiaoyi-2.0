"""加密版记忆：给作战卡打分，每 5 天让模型汇总一批教训，自动替换上一批（tier-prompts-spec §4.7）。

和 TradingAgents 原版记忆的区别：原版按天结算、跟 SPY 比，这里按持仓期（{H}）用币安永续 5 分钟 K 线
打分，山寨币同时看同期 BTC。打分不花 AI 的钱；每 5 天一次的汇总才调一次模型。

只读复盘程序写的 ``card_review/cards.jsonl``，复盘程序本身不改。教训交给组合经理
（``PerpGraph._memory_step``）；评级兜底（``rating_guard``）保证教训最多让卡变弱，
不能让卡比研究经理更强。
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from jiaoyi.contracts import Bias
from jiaoyi.slow import binance_gate
from jiaoyi.slow.review import Bar, CardJournal, JournalRow

logger = logging.getLogger(__name__)

EVERY_DAYS = 5
MIN_CARDS = 30
MAX_LESSONS = 5
MAX_CARDS = 300  # 卡太多就均匀抽样，K 线请求和总盘会被挡住的时间都有上限
_PAGE = 1000
_BAR = timedelta(minutes=5)
BTC = "BTCUSDT"
_DIRECTION = {Bias.LONG.value: 1.0, Bias.SHORT.value: -1.0}


RangeFetcher = Callable[[str, datetime, datetime], list[Bar]]
"""(币, 起, 止) → 这段时间的 5 分钟 K 线（升序）。限流抛 ``binance_gate.RateLimited``。"""


def gate_bars(
    symbol: str, start: datetime, end: datetime, get: Callable[..., Any] = binance_gate.get
) -> list[Bar]:
    """走全局限流闸的 5 分钟 K 线，只要这一段需要的根数（每页最多 1000 根）。"""
    out: list[Bar] = []
    cur = start
    while cur < end:
        limit = min(_PAGE, int((end - cur) / _BAR) + 1)
        r = get(
            "https://fapi.binance.com/fapi/v1/klines",
            params={
                "symbol": symbol,
                "interval": "5m",
                "startTime": int(cur.timestamp() * 1000),
                "endTime": int(end.timestamp() * 1000),
                "limit": limit,
            },
        )
        r.raise_for_status()
        page = [
            Bar(datetime.fromtimestamp(int(k[0]) / 1000, UTC), *(float(x) for x in k[1:5]))
            for k in r.json()
        ]
        if not page:
            break
        out.extend(page)
        if len(page) < limit:
            break
        cur = page[-1].open_time + _BAR
    return out


@dataclass(frozen=True)
class Scored:
    row: JournalRow
    dir_net_pct: float | None  # 按卡方向算、扣往返手续费；观望为 None
    vs_btc_pct: float | None  # 山寨币：比同期 BTC 按同方向多走了多少；BTC 自己 / 观望为 None
    missed_pct: float  # 持仓期内最大单边波动（观望卡看错过了什么）


def score_card(
    row: JournalRow, bars: Sequence[Bar], hold: timedelta, fee_pct: float, btc: Sequence[Bar] = ()
) -> Scored | None:
    """一张卡在会后一个持仓期里的结果；K 线拿不到返回 None。"""
    end = row.created_at + hold
    inside = [b for b in bars if row.created_at <= b.open_time < end]
    if not inside or inside[0].open <= 0:
        return None
    entry = inside[0].open
    ret = (inside[-1].close / entry - 1) * 100
    up = (max(b.high for b in inside) / entry - 1) * 100
    down = (min(b.low for b in inside) / entry - 1) * 100
    sign = _DIRECTION.get(row.bias)
    net = None if sign is None else ret * sign - fee_pct
    rel = None
    if sign is not None and row.symbol != BTC:
        b_in = [b for b in btc if row.created_at <= b.open_time < end]
        if b_in and b_in[0].open > 0:
            rel = (ret - (b_in[-1].close / b_in[0].open - 1) * 100) * sign
    return Scored(row, net, rel, max(up, -down))


@dataclass(frozen=True)
class Group:
    key: str
    n: int
    hit_rate: float | None
    mean_net_pct: float | None
    mean_vs_btc_pct: float | None
    mean_missed_pct: float


def group_stats(scored: Sequence[Scored]) -> list[Group]:
    """按 方向 × 山寨 / 主流 分组。"""
    groups: dict[str, list[Scored]] = defaultdict(list)
    for s in scored:
        side = {"long": "做多", "short": "做空"}.get(s.row.bias, "观望")
        kind = "主流币" if s.row.category == "major" else "其他币"
        groups[f"{side}·{kind}"].append(s)
    out: list[Group] = []
    for key, g in sorted(groups.items()):
        nets = [x.dir_net_pct for x in g if x.dir_net_pct is not None]
        rels = [x.vs_btc_pct for x in g if x.vs_btc_pct is not None]
        out.append(
            Group(
                key,
                len(g),
                sum(1 for v in nets if v > 0) / len(nets) if nets else None,
                sum(nets) / len(nets) if nets else None,
                sum(rels) / len(rels) if rels else None,
                sum(x.missed_pct for x in g) / len(g),
            )
        )
    return out


def render_stats(groups: Sequence[Group]) -> str:
    lines = []
    for g in groups:
        if g.hit_rate is None:
            lines.append(
                f"- {g.key}：{g.n} 张，之后一个持仓期内最大单边波动平均 {g.mean_missed_pct:.2f}%"
            )
            continue
        rel = (
            f"，比同期 BTC 多走 {g.mean_vs_btc_pct:+.2f}%" if g.mean_vs_btc_pct is not None else ""
        )
        lines.append(
            f"- {g.key}：{g.n} 张，方向对 {g.hit_rate:.0%}，扣手续费后平均 "
            f"{g.mean_net_pct:+.2f}%{rel}"
        )
    return "\n".join(lines)


def lessons_prompt(stats: str, n: int, days: int, horizon_zh: str) -> str:
    return (
        f"下面是军师团最近 {days} 天 {n} 张作战卡，各自在会后一个持仓期（{horizon_zh}）里的结果"
        "（币安永续 5 分钟 K 线打分，做多 / 做空已扣往返手续费，“比同期 BTC”只算山寨币）：\n"
        f"{stats}\n\n"
        f"请汇总成最多 {MAX_LESSONS} 条教训，给以后开会的组合经理参考。要求：\n"
        "1. 每条都要写依据：哪一类卡、多少张、平均结果是多少，只能用上面的数字，不要编。\n"
        "2. 样本只有几张、结果接近 0 的不要写成教训。\n"
        "3. 教训写成“遇到什么情况要更谨慎 / 要求更多证据”，不要写成“多做某个方向”；"
        "大盘一天的涨跌会让同方向的卡一起对或一起错，样本全在同一天要说明。\n"
        "4. 中文，每条一两句话。没有站得住的教训就返回空列表。"
    )


@dataclass(frozen=True)
class Lesson:
    text: str
    evidence: str


@dataclass(frozen=True)
class Batch:
    written_at: datetime
    since: datetime
    hold: str
    n_cards: int
    stats: str
    lessons: tuple[Lesson, ...]

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["written_at"] = self.written_at.isoformat()
        d["since"] = self.since.isoformat()
        d["effective_from"] = self.written_at.isoformat()
        d["lessons"] = [asdict(x) for x in self.lessons]
        return d

    @staticmethod
    def from_json(d: dict[str, Any]) -> Batch:
        return Batch(
            written_at=datetime.fromisoformat(d["written_at"]),
            since=datetime.fromisoformat(d["since"]),
            hold=d.get("hold", ""),
            n_cards=int(d.get("n_cards", 0)),
            stats=d.get("stats", ""),
            lessons=tuple(Lesson(x["text"], x["evidence"]) for x in d.get("lessons", [])),
        )


def lessons_text(b: Batch | None) -> str:
    """交给组合经理的那段文字；没有教训就是空（原版的 past_context 也是空）。"""
    if b is None or not b.lessons:
        return ""
    rows = "\n".join(f"{i}. {x.text}（依据：{x.evidence}）" for i, x in enumerate(b.lessons, 1))
    return (
        "过去几天军师团作战卡的复盘教训（只作参考；不能因此把结论放大，最终以这场会的证据为准）：\n"
        + rows
    )


SummarizeFn = Callable[[str], list[Lesson]]
"""提示词 → 教训列表（引擎里接模型；测试里接桩）。"""


class LessonBook:
    """教训本：读卡流水 → 打分 → 每 5 天写一批 ``data/memory/lessons/<日期>.json``。"""

    def __init__(
        self,
        data_dir: str | Path,
        *,
        tz: str,
        hold_minutes: int,
        horizon_zh: str,
        fee_pct: float,
        fetch: RangeFetcher | None = None,
    ) -> None:
        self._dir = Path(data_dir) / "memory"
        self._journal = CardJournal(data_dir)
        self._tz = ZoneInfo(tz)
        self._hold = timedelta(minutes=hold_minutes)
        self._horizon_zh = horizon_zh
        self._hold_label = f"{hold_minutes}m"
        self._fee = fee_pct
        self._fetch: RangeFetcher = fetch or gate_bars

    # ---- 读
    @property
    def lessons_dir(self) -> Path:
        return self._dir / "lessons"

    def current(self) -> Batch | None:
        files = sorted(self.lessons_dir.glob("*.json")) if self.lessons_dir.is_dir() else []
        for p in reversed(files):
            try:
                return Batch.from_json(json.loads(p.read_text(encoding="utf-8")))
            except (OSError, ValueError, KeyError, TypeError):
                logger.warning("读不懂的教训文件，跳过：%s", p)
        return None

    def text(self) -> str:
        return lessons_text(self.current())

    # ---- 状态：从新提示词开始攒
    def _state_path(self) -> Path:
        return self._dir / "state.json"

    def _state(self) -> dict[str, str]:
        try:
            return dict(json.loads(self._state_path().read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return {}

    def _save_state(self, st: dict[str, str]) -> None:
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            self._state_path().write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        except OSError as e:
            logger.warning("教训本状态写不进去：%s", e)

    def since(self, now: datetime) -> datetime:
        """只用这个时间之后出的卡。第一次调用时记下（= 新提示词上线的时刻）。"""
        st = self._state()
        if "since" in st:
            return datetime.fromisoformat(st["since"])
        self._save_state({**st, "since": now.isoformat()})
        return now

    def due(self, now: datetime) -> bool:
        """曼谷日切之后，距上一批（没有就距上线）满 5 天，且今天还没试过。"""
        today = now.astimezone(self._tz).date()
        st = self._state()
        if st.get("attempt") == today.isoformat():
            return False
        b = self.current()
        anchor = (b.written_at if b else self.since(now)).astimezone(self._tz).date()
        return (today - anchor).days >= EVERY_DAYS

    # ---- 写
    def _scored(self, now: datetime, since: datetime) -> list[Scored]:
        """每个币只拉一段覆盖全部卡的 5 分钟 K 线（BTC 也只拉一次），在本地按卡切片。
        限流（RateLimited）不吞，直接抛出让这次汇总作罢；其他失败只丢掉那个币。"""
        rows = sorted(
            (
                r
                for r in self._journal.read()
                if r.created_at >= since and r.created_at + self._hold <= now
            ),
            key=lambda r: r.created_at,
        )
        if len(rows) > MAX_CARDS:  # 均匀抽样，封顶
            step = len(rows) / MAX_CARDS
            rows = [rows[int(i * step)] for i in range(MAX_CARDS)]
        by_sym: dict[str, list[JournalRow]] = defaultdict(list)
        for r in rows:
            by_sym[r.symbol].append(r)
        need_btc = any(r.symbol != BTC and r.bias in _DIRECTION for r in rows)
        if need_btc and BTC not in by_sym:
            by_sym[BTC] = []

        def span(rs: Sequence[JournalRow]) -> tuple[datetime, datetime]:
            return min(r.created_at for r in rs), max(r.created_at for r in rs) + self._hold

        cache: dict[str, list[Bar]] = {}
        spans = {sym: span(rs) for sym, rs in by_sym.items() if rs}
        if need_btc:
            lo = min(a for a, _ in spans.values())
            hi = max(b for _, b in spans.values())
            spans[BTC] = (lo, hi)
        for sym, (lo, hi) in spans.items():
            try:
                cache[sym] = self._fetch(sym, lo, hi)
            except binance_gate.RateLimited:
                raise
            except (httpx.HTTPError, ValueError, KeyError, IndexError) as e:
                logger.warning("%s 的 K 线拉不到：%s", sym, e)
        out: list[Scored] = []
        for r in rows:
            own = cache.get(r.symbol)
            if own is None:
                continue
            sc = score_card(r, own, self._hold, self._fee, cache.get(BTC, ()))
            if sc is not None:
                out.append(sc)
        return out

    def refresh(self, now: datetime, ask: SummarizeFn) -> Batch | None:
        """到期就打分、汇总、写新的一批；不到 30 张或没有站得住的教训就不换（上一批照用）。
        同步调用（引擎的工作线程里）。"""
        if not self.due(now):
            return None
        st = self._state()
        st["attempt"] = now.astimezone(self._tz).date().isoformat()
        self._save_state(st)
        since = self.since(now)
        scored = self._scored(now, since)
        if len(scored) < MIN_CARDS:
            logger.info(
                "教训本：打完分的卡只有 %d 张（要 %d 张），这次不写", len(scored), MIN_CARDS
            )
            return None
        stats = render_stats(group_stats(scored))
        days = max(1, (now - since).days)
        lessons = ask(lessons_prompt(stats, len(scored), days, self._horizon_zh))
        lessons = [x for x in lessons if x.text.strip() and x.evidence.strip()][:MAX_LESSONS]
        if not lessons:
            return None
        batch = Batch(now, since, self._hold_label, len(scored), stats, tuple(lessons))
        self._write(batch)
        return batch

    def _write(self, b: Batch) -> None:
        self.lessons_dir.mkdir(parents=True, exist_ok=True)
        day = b.written_at.astimezone(self._tz).date().isoformat()
        path = self.lessons_dir / f"{day}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(b.to_json(), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
