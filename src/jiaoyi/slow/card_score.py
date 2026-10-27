"""作战卡方向记分：用发布时刻作起点、扣掉大盘，给方向卡一个不随大盘摆的命中率。

只读、只记分：不改卡、不改阈值、不调模型，也不喂给军师团。``slow/review.py`` 的老口径用整轮起点、
只看原始涨跌，命中率会跟着「大盘恰好顺着卡的多空倾斜走」在 78% 和 44% 之间摆；这里的主指标是
**超额**：方向 ×（币收益 − 同期币池等权收益）。口径与 2026-10-28 方向准确性复盘一致：

- t0：第一条带这一版本的「出作战卡 / 作战卡更新」记录时间；没有就用会议结论记录时间；都没有才用
  整轮起点 ``created_at``（它比实际发布早，中位 4.5 分钟、最多 22 分钟）。
- 价格：t 时刻最后一根已收盘 1 分钟 K 线的收盘价（主网，和哨兵一个口径）。
- 期限：1h、4h（会议提示词把结论定义为未来 4 小时）、到卡片过期。
- 币池：t0 时生效的那一份（``market_meeting`` 每轮记的池）；等权、只算两端都有价的币。
- 基线（同一批样本）：一律做空、一律做多、BTC 过去 4h 方向、自身过去 4h 动量、币池过去 4h 方向。
- 置信区间：按 4 小时 UTC 时段聚类 bootstrap（同一时段的卡高度相关，独立样本数 ≈ 时段数）。

逐卡结果追加到 ``data/card_score/scored.jsonl``（只加不改，按 卡 / 版本 / 币 去重，重跑幂等），
汇总写 ``data/card_score/summary.json`` 给面板读。
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from bisect import bisect_right
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, ValidationError

from jiaoyi.slow.review import CardJournal, JournalRow

logger = logging.getLogger(__name__)

DIR_NAME = "card_score"
SCORED_FILE = "scored.jsonl"
SUMMARY_FILE = "summary.json"

BTC = "BTCUSDT"
MINUTE = timedelta(minutes=1)
HORIZONS: tuple[tuple[str, timedelta], ...] = (
    ("1h", timedelta(hours=1)),
    ("4h", timedelta(hours=4)),
)
PRIMARY = "4h"
PRIOR = timedelta(hours=4)
"""基线看的「过去 4 小时」。"""
EXP_MAX = timedelta(hours=24)
"""有效期超过这么久的卡不算「到过期」那一档（免得一张卡等一整天）。"""
SETTLE = timedelta(minutes=2)
"""窗口走完后再等这么久，最后一根 1 分钟线一定收盘了。"""
PUBLISH_WINDOW_S = (-5.0, 1800.0)
CONCLUSION_WINDOW_S = (-120.0, 1800.0)
BLOCK_HOURS = 4
MIN_BLOCKS = 44
"""复盘的功效估计：超额命中要分辨到 ±2.5 个百分点约需 44 个 4 小时时段（7–8 天）。"""
BOOT_B = 2000
BOOT_SEED = 7

PERIOD_MARKS: tuple[tuple[str, datetime], ...] = (
    ("#213", datetime(2026, 10, 24, 12, 29, 43, tzinfo=UTC)),
    ("#304", datetime(2026, 10, 26, 6, 1, 23, tzinfo=UTC)),
    ("#306/#307", datetime(2026, 10, 26, 21, 28, 55, tzinfo=UTC)),
    ("#309", datetime(2026, 10, 27, 13, 12, 20, tzinfo=UTC)),
)
"""分期的界线（合并时间，部署可能更晚）：#213 评级兜底和弱档位，#304、#306/#307、#309 改了开会。
新的界线往后加（名字, UTC 时间）。"""

T0Source = Literal["card_publish", "conclusion", "created_at"]
_SIGN = {"long": 1, "short": -1}


class _Bar(Protocol):
    @property
    def open_time(self) -> datetime: ...

    @property
    def close(self) -> float: ...


KlineFetch = Callable[[str, datetime, datetime], Awaitable[Sequence[_Bar]]]
"""(币, 起, 止) → 开盘时间在 [起, 止] 的已收盘 1 分钟 K 线；``gate.bars.OutcomeBars`` 就是一个。"""


# ---------------------------------------------------------------- t0：卡什么时候真的发出去


@dataclass(frozen=True)
class Conclusion:
    ts: datetime
    symbol: str
    bias: str
    issued_at: datetime | None
    record_id: str


@dataclass
class LogIndex:
    """决策记录里定 t0 要用的两样：每个币的会议结论、每个（卡, 版本）的发布时间。"""

    conclusions: dict[str, list[Conclusion]]
    publishes: dict[tuple[str | None, int], list[datetime]]


def _ts(v: object) -> datetime | None:
    if not isinstance(v, str) or not v:
        return None
    return datetime.fromisoformat(v).astimezone(UTC)


def read_log_index(log_dir: Path, days: Iterable[date] | None = None) -> LogIndex:
    """只解析带「开会结论」或「作战卡」的行（一天的决策记录几十 MB，大部分是盯仓）。"""
    if days is None:
        files = sorted(log_dir.glob("????-??-??.jsonl"))
    else:
        files = [log_dir / f"{d.isoformat()}.jsonl" for d in sorted(set(days))]
    concl: dict[str, list[Conclusion]] = {}
    pub: dict[tuple[str | None, int], list[datetime]] = {}
    for path in files:
        try:
            f = path.open(encoding="utf-8")
        except OSError:
            continue
        with f:
            for line in f:
                is_concl = "开会结论" in line
                if not is_concl and "作战卡" not in line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("role") != "commander":
                    continue
                out = r.get("output") or {}
                ts = _ts(r.get("ts"))
                if ts is None or not isinstance(out, dict):
                    continue
                sym = r.get("symbol")
                if is_concl and sym:
                    concl.setdefault(sym, []).append(
                        Conclusion(
                            ts, sym, str(out.get("bias")), _ts(out.get("issued_at")), r["record_id"]
                        )
                    )
                elif sym is None and "symbols" in out and isinstance(out.get("version"), int):
                    pub.setdefault((r.get("card_id"), out["version"]), []).append(ts)
    return LogIndex(concl, pub)


def resolve_t0(
    row: JournalRow, index: LogIndex, used: set[str]
) -> tuple[datetime, T0Source, str | None]:
    """(t0, 来源, 会议结论 record_id)。结论按 币 + 方向 对：``issued_at`` 与整轮起点差 1 秒内的
    直接用，否则取起点前 2 分钟到后 30 分钟里最早的一条；一条结论只配一张卡（``used``）。"""
    ca = row.created_at
    best: Conclusion | None = None
    for c in index.conclusions.get(row.symbol, []):
        if c.bias != row.bias or c.record_id in used:
            continue
        if c.issued_at is not None and abs((c.issued_at - ca).total_seconds()) < 1:
            best = c
            break
        dt = (c.ts - ca).total_seconds()
        lo, hi = CONCLUSION_WINDOW_S
        if lo <= dt <= hi and (best is None or dt < (best.ts - ca).total_seconds()):
            best = c
    if best is not None:
        used.add(best.record_id)
    t_conc = max(ca, best.ts) if best is not None else None
    ref = t_conc or ca
    lo, hi = PUBLISH_WINDOW_S
    t_card = min(
        (
            t
            for t in index.publishes.get((row.card_id, row.version), [])
            if lo <= (t - ref).total_seconds() <= hi
        ),
        default=None,
    )
    rid = best.record_id if best is not None else None
    if t_card is not None:
        return t_card, "card_publish", rid
    if t_conc is not None:
        return t_conc, "conclusion", rid
    return ca, "created_at", rid


def dedup(rows: Iterable[JournalRow]) -> list[JournalRow]:
    """同一 卡 / 版本 / 币 写了几行的留最后一行（位置按第一次出现）。"""
    seen: dict[tuple[str, int, str], JournalRow] = {}
    for r in rows:
        seen[(r.card_id, r.version, r.symbol)] = r
    return list(seen.values())


# ---------------------------------------------------------------- 币池


@dataclass(frozen=True)
class Pool:
    pool_id: str | None
    symbols: tuple[str, ...]


class PoolTimeline:
    """t 时刻生效的币池：``market_meeting`` 每轮记下的池，取 t 之前最近一轮。
    最早一轮之前的卡用最早那轮。"""

    def __init__(self, entries: Sequence[tuple[datetime, Pool]]) -> None:
        self._entries = sorted(entries, key=lambda e: e[0])
        self._keys = [t for t, _ in self._entries]

    @classmethod
    def load(cls, data_dir: str | Path) -> PoolTimeline:
        out: list[tuple[datetime, Pool]] = []
        for path in sorted((Path(data_dir) / "market_meeting").glob("????-??-??.jsonl")):
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                try:
                    r = json.loads(line)
                    t = _ts(r.get("ts"))
                    syms = tuple(p["symbol"] for p in r.get("pool") or [])
                except (ValueError, KeyError, TypeError):
                    continue
                if t is not None and syms:
                    out.append((t, Pool(r.get("pool_id"), syms)))
        return cls(out)

    def at(self, t: datetime) -> Pool | None:
        if not self._entries:
            return None
        i = bisect_right(self._keys, t) - 1
        return self._entries[max(i, 0)][1]


# ---------------------------------------------------------------- 价格


def bar_open(t: datetime) -> datetime:
    """t 时刻最后一根已收盘 1 分钟线的开盘时间（= 向上取整到分钟再减 1 分钟）。"""
    t = t.astimezone(UTC)
    floor = t.replace(second=0, microsecond=0)
    return (floor if floor == t else floor + MINUTE) - MINUTE


class CloseBook:
    """币 → {1 分钟线开盘时间: 收盘价}，只留用得到的那几分钟。"""

    def __init__(self) -> None:
        self._px: dict[str, dict[datetime, float]] = {}

    def put(self, symbol: str, open_time: datetime, close: float) -> None:
        self._px.setdefault(symbol, {})[open_time.astimezone(UTC)] = close

    def px(self, symbol: str, t: datetime) -> float | None:
        return self._px.get(symbol, {}).get(bar_open(t))

    def ret(self, symbol: str, a: datetime, b: datetime) -> float | None:
        pa, pb = self.px(symbol, a), self.px(symbol, b)
        if pa is None or pb is None or pa <= 0:
            return None
        return pb / pa - 1


async def load_closes(
    fetch: KlineFetch, needs: dict[str, set[datetime]]
) -> tuple[CloseBook, set[str]]:
    """按 (币, UTC 日) 合并成一段拉一次（过去的整天走日包缓存，今天才打 API），只留要的分钟。
    返回 (价格, 拉失败的币)；被限流（``market.rest.RateLimited``）不接，直接抛出，这一轮作罢。"""
    book = CloseBook()
    failed: set[str] = set()
    for sym in sorted(needs):
        by_day: dict[date, list[datetime]] = {}
        for m in needs[sym]:
            by_day.setdefault(m.date(), []).append(m)
        for ms in by_day.values():
            want = set(ms)
            try:
                bars = await fetch(sym, min(ms), max(ms))
            except (OSError, ValueError, KeyError, IndexError) as e:  # 跳过用到它的卡，下次再试
                logger.warning("方向记分：%s 的 K 线拉不到：%s", sym, e)
                failed.add(sym)
                break
            for b in bars:
                ot = b.open_time.astimezone(UTC)
                if ot in want:
                    book.put(sym, ot, b.close)
    return book, failed


# ---------------------------------------------------------------- 逐卡结果


class Leg(BaseModel):
    """一个标的在各段的收益（小数，0.01 = 1%）；None = 没价格。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prior_4h: float | None = None
    h1: float | None = None
    h4: float | None = None
    exp: float | None = None

    def at(self, horizon: str) -> float | None:
        return {"1h": self.h1, "4h": self.h4, "exp": self.exp}[horizon]


class CardScore(BaseModel):
    """``scored.jsonl`` 一行：一张卡（一个币一场会的结论）的方向记分原料，指标在汇总时算。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    card_id: str
    version: int
    symbol: str
    category: str
    bias: str
    refresh_reason: str
    created_at: datetime
    expires_at: datetime
    t0: datetime
    t0_src: T0Source
    conclusion_id: str | None = None
    pool_id: str | None = None
    pool_size: int = 0
    pool_n_4h: int = 0
    """4h 这段两端都有价、真正进了等权的币数。"""
    coin: Leg
    pool: Leg
    btc: Leg
    legacy_ret: float | None = None
    """老口径：整轮起点 → 到期的原始涨跌（``review.py`` 的算法，换成 1 分钟收盘价）。"""
    scored_at: datetime

    @property
    def key(self) -> tuple[str, int, str]:
        return (self.card_id, self.version, self.symbol)

    @property
    def direction(self) -> int:
        return _SIGN.get(self.bias, 0)

    @property
    def block(self) -> str:
        t = self.t0.astimezone(UTC)
        return f"{t:%Y-%m-%d}T{t.hour // BLOCK_HOURS * BLOCK_HOURS:02d}"


def exp_end(t0: datetime, expires_at: datetime) -> datetime | None:
    return expires_at if t0 < expires_at <= t0 + EXP_MAX else None


def window_end(t0: datetime, expires_at: datetime) -> datetime:
    """所有期限都走完的时刻。"""
    ends = [t0 + d for _, d in HORIZONS]
    e = exp_end(t0, expires_at)
    return max([*ends, *([e] if e else [])])


def needed_times(row: JournalRow, t0: datetime) -> list[datetime]:
    return [t0 - PRIOR, t0, *(t0 + d for _, d in HORIZONS), row.created_at, row.expires_at]


def _mean(xs: Sequence[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def score_card(
    row: JournalRow,
    t0: datetime,
    src: T0Source,
    conclusion_id: str | None,
    book: CloseBook,
    pool: Pool | None,
    now: datetime,
) -> CardScore:
    syms = pool.symbols if pool is not None else ()
    e = exp_end(t0, row.expires_at)
    prior = (t0 - PRIOR, t0)
    h1, h4 = ((t0, t0 + d) for _, d in HORIZONS)
    ex = (t0, e) if e is not None else None

    def leg(sym: str) -> Leg:
        return Leg(
            prior_4h=book.ret(sym, *prior),
            h1=book.ret(sym, *h1),
            h4=book.ret(sym, *h4),
            exp=book.ret(sym, *ex) if ex else None,
        )

    def ew(ab: tuple[datetime, datetime] | None) -> tuple[float | None, int]:
        if ab is None:
            return None, 0
        rs = [r for s in syms if (r := book.ret(s, *ab)) is not None]
        return _mean(rs), len(rs)

    p4, n4 = ew(h4)
    return CardScore(
        card_id=row.card_id,
        version=row.version,
        symbol=row.symbol,
        category=row.category,
        bias=row.bias,
        refresh_reason=row.refresh_reason.removeprefix("event:"),
        created_at=row.created_at,
        expires_at=row.expires_at,
        t0=t0,
        t0_src=src,
        conclusion_id=conclusion_id,
        pool_id=pool.pool_id if pool is not None else None,
        pool_size=len(syms),
        pool_n_4h=n4,
        coin=leg(row.symbol),
        pool=Leg(prior_4h=ew(prior)[0], h1=ew(h1)[0], h4=p4, exp=ew(ex)[0]),
        btc=leg(BTC),
        legacy_ret=book.ret(row.symbol, row.created_at, row.expires_at),
        scored_at=now,
    )


class ScoreStore:
    """``data/card_score/``：逐卡结果只追加；同一张卡读到两行以第一行为准。"""

    def __init__(self, data_dir: str | Path) -> None:
        self.dir = Path(data_dir) / DIR_NAME

    @property
    def scored_path(self) -> Path:
        return self.dir / SCORED_FILE

    @property
    def summary_path(self) -> Path:
        return self.dir / SUMMARY_FILE

    def read(self) -> list[CardScore]:
        try:
            lines = self.scored_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out: dict[tuple[str, int, str], CardScore] = {}
        for ln in lines:
            if not ln.strip():
                continue
            try:
                s = CardScore.model_validate_json(ln)
            except ValidationError:
                logger.warning("方向记分：跳过读不懂的一行：%.80s", ln)
                continue
            out.setdefault(s.key, s)
        return list(out.values())

    def append(self, rows: Sequence[CardScore]) -> int:
        """只写还没有的卡，返回写了几行。"""
        have = {s.key for s in self.read()}
        new = []
        for r in rows:
            if r.key not in have:
                have.add(r.key)
                new.append(r)
        if not new:
            return 0
        self.dir.mkdir(parents=True, exist_ok=True)
        with self.scored_path.open("a", encoding="utf-8") as f:
            for r in new:
                f.write(r.model_dump_json() + "\n")
        return len(new)

    def write_summary(self, report: Report) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.summary_path.with_suffix(".tmp")
        tmp.write_text(report.model_dump_json(indent=1), encoding="utf-8")
        tmp.replace(self.summary_path)

    def read_summary(self) -> Report | None:
        try:
            return Report.model_validate_json(self.summary_path.read_text(encoding="utf-8"))
        except (OSError, ValidationError):
            return None


# ---------------------------------------------------------------- 汇总


@lru_cache(maxsize=8)
def _boot_draws(k: int, b: int, seed: int) -> tuple[tuple[int, ...], ...]:
    """同样的时段数、次数、种子，抽到的时段下标完全一样（命中和均值共用一套抽样）。"""
    rng = random.Random(seed)
    return tuple(tuple(rng.randrange(k) for _ in range(k)) for _ in range(b))


def cluster_ci(
    values: Sequence[float], blocks: Sequence[str], *, b: int = BOOT_B, seed: int = BOOT_SEED
) -> tuple[float | None, float | None, int]:
    """按时段整块有放回重抽 ``b`` 次，取均值的 2.5% / 97.5% 分位。时段少于 3 个不给区间。
    返回 (下限, 上限, 时段数)。"""
    sums: dict[str, float] = {}
    cnts: dict[str, int] = {}
    for v, k in zip(values, blocks, strict=True):
        sums[k] = sums.get(k, 0.0) + v
        cnts[k] = cnts.get(k, 0) + 1
    keys = list(sums)
    if len(keys) < 3:
        return None, None, len(keys)
    s = [sums[k] for k in keys]
    c = [cnts[k] for k in keys]
    sims = sorted(
        sum(s[i] for i in draw) / sum(c[i] for i in draw)
        for draw in _boot_draws(len(keys), b, seed)
    )
    return sims[int(0.025 * b)], sims[int(0.975 * b)], len(keys)


class Stat(BaseModel):
    """一组带方向的收益：命中率（> 0 才算对）、平均 bp，各带时段聚类 95% 区间。"""

    model_config = ConfigDict(frozen=True)

    n: int = 0
    blocks: int = 0
    hit: float | None = None
    hit_ci: tuple[float, float] | None = None
    mean_bp: float | None = None
    mean_bp_ci: tuple[float, float] | None = None


def stat(pairs: Sequence[tuple[float, str]]) -> Stat:
    """(带方向的收益, 时段) → ``Stat``。"""
    if not pairs:
        return Stat()
    vals = [v for v, _ in pairs]
    blocks = [k for _, k in pairs]
    hits = [1.0 if v > 0 else 0.0 for v in vals]
    hlo, hhi, nb = cluster_ci(hits, blocks)
    mlo, mhi, _ = cluster_ci([v * 1e4 for v in vals], blocks)
    return Stat(
        n=len(vals),
        blocks=nb,
        hit=sum(hits) / len(hits),
        hit_ci=(hlo, hhi) if hlo is not None and hhi is not None else None,
        mean_bp=sum(vals) / len(vals) * 1e4,
        mean_bp_ci=(mlo, mhi) if mlo is not None and mhi is not None else None,
    )


Signer = Callable[[CardScore], int | None]


def _sign(x: float | None) -> int | None:
    if x is None or x == 0:
        return None
    return 1 if x > 0 else -1


BASELINES: tuple[tuple[str, str, Signer], ...] = (
    ("card", "作战卡", lambda s: s.direction),
    ("always_short", "一律做空", lambda s: -1),
    ("always_long", "一律做多", lambda s: 1),
    ("btc_4h", "BTC 过去 4h 方向", lambda s: _sign(s.btc.prior_4h)),
    ("mom_4h", "自身过去 4h 动量", lambda s: _sign(s.coin.prior_4h)),
    ("pool_4h", "币池过去 4h 方向", lambda s: _sign(s.pool.prior_4h)),
)


def _raw(rows: Sequence[CardScore], h: str, signer: Signer) -> Stat:
    out = []
    for s in rows:
        d, r = signer(s), s.coin.at(h)
        if d and r is not None:
            out.append((d * r, s.block))
    return stat(out)


def _excess(rows: Sequence[CardScore], h: str, signer: Signer) -> Stat:
    out = []
    for s in rows:
        d, r, p = signer(s), s.coin.at(h), s.pool.at(h)
        if d and r is not None and p is not None:
            out.append((d * (r - p), s.block))
    return stat(out)


def _market(rows: Sequence[CardScore], h: str) -> Stat:
    out = []
    for s in rows:
        p = s.pool.at(h)
        if s.direction and p is not None and s.coin.at(h) is not None:
            out.append((s.direction * p, s.block))
    return stat(out)


class HorizonStats(BaseModel):
    model_config = ConfigDict(frozen=True)

    raw: Stat
    """原始：方向 × 币收益。"""
    excess: Stat
    """超额（个币分量）：方向 ×（币 − 币池等权）。"""
    market: Stat
    """大盘分量：方向 × 币池等权。原始 = 大盘 + 超额。"""


class Baseline(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str
    label: str
    raw: Stat
    excess: Stat


class Group(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str
    n_cards: int
    n_neutral: int
    n_long: int
    n_short: int
    raw: Stat
    excess: Stat
    always_short: Stat
    always_long: Stat


class Report(BaseModel):
    """``summary.json``：面板和命令行读的汇总。"""

    model_config = ConfigDict(frozen=True)

    generated_at: datetime
    first_t0: datetime | None = None
    last_t0: datetime | None = None
    n_cards: int = 0
    n_long: int = 0
    n_short: int = 0
    n_neutral: int = 0
    """观望 / 双向：不计命中率，只计数。"""
    n_unpriced: int = 0
    """方向卡里 4h 拿不到价格的（下架、没有主网合约）。"""
    t0_src: dict[str, int] = {}
    min_blocks: int = MIN_BLOCKS
    primary: str = PRIMARY
    horizons: dict[str, HorizonStats] = {}
    baselines: dict[str, list[Baseline]] = {}
    legacy: Stat = Stat()
    """老口径（未扣大盘）：整轮起点 → 到期的原始涨跌命中。"""
    periods: list[Group] = []
    by_direction: list[Group] = []
    by_refresh_reason: list[Group] = []

    @property
    def enough_blocks(self) -> bool:
        return self.horizons.get(PRIMARY, _EMPTY_H).excess.blocks >= self.min_blocks


_EMPTY_H = HorizonStats(raw=Stat(), excess=Stat(), market=Stat())


def period_of(t: datetime, marks: Sequence[tuple[str, datetime]] = PERIOD_MARKS) -> str:
    prev: str | None = None
    for name, at in marks:
        if t < at:
            return f"{prev} – {name}" if prev else f"{name} 之前"
        prev = name
    return f"{prev} 之后" if prev else "全部"


def _group(key: str, rows: Sequence[CardScore]) -> Group:
    d = [s for s in rows if s.direction]
    return Group(
        key=key,
        n_cards=len(rows),
        n_neutral=len(rows) - len(d),
        n_long=sum(1 for s in d if s.direction > 0),
        n_short=sum(1 for s in d if s.direction < 0),
        raw=_raw(d, PRIMARY, BASELINES[0][2]),
        excess=_excess(d, PRIMARY, BASELINES[0][2]),
        always_short=_raw(d, PRIMARY, BASELINES[1][2]),
        always_long=_raw(d, PRIMARY, BASELINES[2][2]),
    )


def _groups(rows: Sequence[CardScore], key: Callable[[CardScore], str]) -> list[Group]:
    by: dict[str, list[CardScore]] = {}
    for s in rows:
        by.setdefault(key(s), []).append(s)
    return [_group(k, v) for k, v in by.items()]


def summarize(
    rows: Iterable[CardScore],
    now: datetime,
    marks: Sequence[tuple[str, datetime]] = PERIOD_MARKS,
) -> Report:
    rows = sorted(rows, key=lambda s: s.t0)  # 稳定排序：同一时刻保持写入顺序
    d = [s for s in rows if s.direction]
    card = BASELINES[0][2]
    horizons = {
        h: HorizonStats(raw=_raw(d, h, card), excess=_excess(d, h, card), market=_market(d, h))
        for h in (*(k for k, _ in HORIZONS), "exp")
    }
    baselines: dict[str, list[Baseline]] = {}
    for h, _ in HORIZONS:
        same = [s for s in d if s.coin.at(h) is not None]  # 同一批样本
        baselines[h] = [
            Baseline(key=k, label=lab, raw=_raw(same, h, f), excess=_excess(same, h, f))
            for k, lab, f in BASELINES
        ]
    legacy = stat([(s.direction * s.legacy_ret, s.block) for s in d if s.legacy_ret is not None])
    direction_name = {1: "做多", -1: "做空"}
    return Report(
        generated_at=now,
        first_t0=rows[0].t0 if rows else None,
        last_t0=rows[-1].t0 if rows else None,
        n_cards=len(rows),
        n_long=sum(1 for s in d if s.direction > 0),
        n_short=sum(1 for s in d if s.direction < 0),
        n_neutral=len(rows) - len(d),
        n_unpriced=sum(1 for s in d if s.coin.h4 is None),
        t0_src=dict(Counter(s.t0_src for s in rows)),
        horizons=horizons,
        baselines=baselines,
        legacy=legacy,
        periods=_groups(rows, lambda s: period_of(s.t0, marks)),
        by_direction=[
            g.model_copy(update={"key": direction_name.get(int(g.key), g.key)})
            for g in _groups(d, lambda s: str(s.direction))
        ],
        by_refresh_reason=sorted(
            _groups(rows, lambda s: s.refresh_reason), key=lambda g: -g.n_cards
        ),
    )


# ---------------------------------------------------------------- 一轮：补算 + 汇总


@dataclass(frozen=True)
class RunResult:
    new: int
    waiting: int
    failed: int
    report: Report


async def update(
    data_dir: str | Path,
    now: datetime,
    fetch: KlineFetch,
    *,
    since: datetime | None = None,
    pools: PoolTimeline | None = None,
    marks: Sequence[tuple[str, datetime]] = PERIOD_MARKS,
) -> RunResult:
    """把窗口已走完、还没记过分的卡补上，再重写汇总。重跑只补新的（幂等）。
    同步的读文件 / 统计放线程里，不卡事件循环。"""
    store = ScoreStore(data_dir)

    def prepare() -> tuple[list[tuple[JournalRow, datetime, T0Source, str | None]], int]:
        done = store.read()
        have = {s.key for s in done}
        used = {s.conclusion_id for s in done if s.conclusion_id}
        pending = [
            r
            for r in dedup(CardJournal(data_dir).read())  # 流水顺序：配结论的先后和复盘一致
            if (r.card_id, r.version, r.symbol) not in have
            and (since is None or r.created_at >= since)
        ]
        todo = [r for r in pending if r.created_at + PRIOR + SETTLE <= now]  # t0 不早于整轮起点
        waiting = len(pending) - len(todo)
        if not todo:
            return [], waiting
        days = {r.created_at.date() + timedelta(days=i) for r in todo for i in (0, 1)}
        index = read_log_index(Path(data_dir) / "decision_log", days)
        out = []
        for r in todo:
            t0, src, rid = resolve_t0(r, index, used)
            if window_end(t0, r.expires_at) + SETTLE > now:
                waiting += 1  # 结论仍算配过：和一次性从头算的配对顺序一致
                continue
            out.append((r, t0, src, rid))
        return out, waiting

    ready, waiting = await asyncio.to_thread(prepare)
    new = failed_n = 0
    if ready:
        timeline = (
            pools if pools is not None else await asyncio.to_thread(PoolTimeline.load, data_dir)
        )
        needs: dict[str, set[datetime]] = {}
        plan = []
        for r, t0, src, rid in ready:
            pool = timeline.at(t0)
            syms = {r.symbol, BTC, *(pool.symbols if pool else ())}
            times = [bar_open(t) for t in needed_times(r, t0)]
            for s in syms:
                needs.setdefault(s, set()).update(times)
            plan.append((r, t0, src, rid, pool, syms))
        book, failed = await load_closes(fetch, needs)
        rows = []
        for r, t0, src, rid, pool, syms in plan:
            if syms & failed:
                failed_n += 1
                continue
            rows.append(score_card(r, t0, src, rid, book, pool, now))
        new = await asyncio.to_thread(store.append, rows)
    report = await asyncio.to_thread(lambda: summarize(store.read(), now, marks))
    await asyncio.to_thread(store.write_summary, report)
    return RunResult(new, waiting, failed_n, report)


# ---------------------------------------------------------------- 文字版（命令行 / 日志）


def _pct(v: float | None) -> str:
    return "-" if v is None else f"{v * 100:.1f}%"


def _ci_pct(ci: tuple[float, float] | None) -> str:
    return "" if ci is None else f" [{ci[0] * 100:.1f}, {ci[1] * 100:.1f}]"


def _bp(v: float | None) -> str:
    return "-" if v is None else f"{v:+.1f}bp"


def _ci_bp(ci: tuple[float, float] | None) -> str:
    return "" if ci is None else f" [{ci[0]:+.1f}, {ci[1]:+.1f}]"


def stat_line(s: Stat) -> str:
    if not s.n:
        return "n=0"
    return (
        f"命中 {_pct(s.hit)}{_ci_pct(s.hit_ci)}，平均 {_bp(s.mean_bp)}{_ci_bp(s.mean_bp_ci)}"
        f"（{s.n} 张 / {s.blocks} 个时段）"
    )


def headline(r: Report) -> str:
    """一句话：主指标（4h 超额）+ 原始命中和两条朴素基线。"""
    h = r.horizons.get(PRIMARY)
    if h is None or not h.excess.n:
        return "作战卡方向记分：还没有窗口走完的方向卡"
    base = {b.key: b for b in r.baselines.get(PRIMARY, [])}
    short, long_ = base.get("always_short"), base.get("always_long")
    few = "" if r.enough_blocks else f"（时段不足 {r.min_blocks} 个，别当结论）"
    return (
        f"作战卡方向记分：4h 超额命中 {_pct(h.excess.hit)}{_ci_pct(h.excess.hit_ci)}、"
        f"平均超额 {_bp(h.excess.mean_bp)}{_ci_bp(h.excess.mean_bp_ci)}，"
        f"{h.excess.n} 张 / {h.excess.blocks} 个 4h 时段{few}；原始命中 {_pct(h.raw.hit)}，"
        f"同样本一律做空 {_pct(short.raw.hit if short else None)}、"
        f"一律做多 {_pct(long_.raw.hit if long_ else None)}"
    )


def render(r: Report) -> list[str]:
    lines = [headline(r)]
    if not r.n_cards:
        return lines
    lines.append(
        f"样本：{r.n_cards} 张卡（做多 {r.n_long} / 做空 {r.n_short} / 观望 {r.n_neutral}，"
        f"观望不计命中）；t0 来源 {r.t0_src}"
    )
    for h, label in (("1h", "1h"), ("4h", "4h"), ("exp", "到过期")):
        hs = r.horizons.get(h)
        if hs is None:
            continue
        lines.append(f"## {label}")
        lines.append(f"  超额（主指标） {stat_line(hs.excess)}")
        lines.append(f"  原始          {stat_line(hs.raw)}")
        lines.append(
            f"  其中大盘分量  平均 {_bp(hs.market.mean_bp)}，个币分量 {_bp(hs.excess.mean_bp)}"
        )
        for b in r.baselines.get(h, []):
            lines.append(f"  基线 {b.label}：原始 {stat_line(b.raw)}；超额 {_pct(b.excess.hit)}")
    lines.append(f"## 老口径（整轮起点 → 到期，原始涨跌，未扣大盘）  {stat_line(r.legacy)}")
    for title, groups in (
        ("分期（4h）", r.periods),
        ("方向（4h）", r.by_direction),
        ("出卡原因（4h）", r.by_refresh_reason),
    ):
        lines.append(f"## {title}")
        for g in groups:
            lines.append(
                f"  {g.key}：{g.n_cards} 张（观望 {g.n_neutral}）超额 {stat_line(g.excess)}；"
                f"原始 {_pct(g.raw.hit)}，一律做空 {_pct(g.always_short.hit)}，"
                f"一律做多 {_pct(g.always_long.hit)}"
            )
    lines.append(
        f"说明：区间按 4 小时 UTC 时段聚类 bootstrap（{BOOT_B} 次）；超额命中要分辨到 ±2.5 个百分点"
        f"约需 {MIN_BLOCKS} 个时段。"
    )
    return lines
