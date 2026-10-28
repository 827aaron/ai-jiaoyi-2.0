"""大盘台记分（设计 2026-10-28 D3）：大盘台每一轮的概率准不准，门槛该怎么调。

按每轮运行记分，不按发布时刻（CONFLICTS #13）：每轮 t0 起 1h / 4h 的结局，口径和大盘台被问的一样
（``market_desk.score_targets``）——t0 时那一池（日志里的 ``basket``）各币简单收益的等权平均，
价格取主网 1 分钟线收盘价，落在 ±band·σ（日志里的 ``flat_band_pct``）
以内为平。结局起点 / 终点都取「收盘时间不晚于该时刻的最后一根」1 分钟线，不看 t0 之后的价。K 线经
``binance_gate`` 拉（和数据包同源）；面板和主程序是两个进程，只共用 IP 权重（响应头里的已用权重），
封禁状态各记各的。按 币 / UTC 日缓存在 ``data/market_desk/score_cache/``，重算只补新的分钟，
超过窗口 + 2 天的缓存删掉。

报告（``data/market_desk/score.json``）：
- 概率：Brier（三类求和）、log loss、RPS，对照先验（日志里的基础率）和「延续」（近 1 小时等权池
  涨跌的方向当成 0.55 的尖锐预测）；4 小时时段聚类的 Brier 差区间；可靠性分箱。
- 方向：代码派生方向的命中、非平判断精度和覆盖、混淆矩阵；发布方向（t0 时生效的大盘台版本）的命中。
- 延迟：近 4 小时等权池走出 ±band·σ 且持续 ≥ 30 分钟算一段「已发生的行情」，大盘台多久后发布同向。
- 旧口径：总盘会 Flash 的大盘方向（``data/market_meeting``，每轮自己的池；σ 用 1h 线按大盘台同一
  方法算）的命中和尖锐版 Brier。
- 门槛扫描：用日志里的概率重新派生方向（θ / δ），按大盘台的发布滞回规则重放发布（确认 1 / 2 轮），
  看准确度和延迟怎么换。不调模型。

只读日志和公开行情，不下单、不改设置。
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import statistics
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx

from jiaoyi.slow import binance_gate
from jiaoyi.slow.market_desk import LOG_DIR, Probs, derive_direction
from jiaoyi.slow.market_dossier import FAPI, HOUR_MS, Bar, classify, sigma_at
from jiaoyi.slow.scoring_common import cluster_ci, parse_ts

logger = logging.getLogger(__name__)

SCORE_FILE = "score.json"
CACHE_DIR = "score_cache"
MINUTE_MS = 60_000
DAY_MS = 86_400_000
SETTLE = timedelta(minutes=2)
"""结算点过后再等 2 分钟才算（最后一根 1 分钟线收盘、行情落地）。"""
HORIZONS: tuple[Literal["1h", "4h"], ...] = ("1h", "4h")
HOURS = {"1h": 1, "4h": 4}
CLASSES: tuple[str, ...] = ("up", "flat", "down")
DIR_CLASS = {"看涨": "up", "中性": "flat", "看跌": "down"}
SHARP = 0.55
"""把一个方向当成概率时：叫到的那类 0.55，另两类各 0.225（设计里基线的「尖锐版」）。"""
LOG_CLIP = 0.02
MIN_COVERAGE = 0.8
"""池里有价的币不到八成：这一轮这一段先不算（下次重拉）。"""
SUSTAIN = timedelta(minutes=30)
"""近 4 小时的等权池收益连续停在同一侧这么久，才算一段「已发生的行情」。"""
SIGMA_LOOKBACK_H = 168
THETAS = (0.35, 0.40, 0.45, 0.50, 0.55)
DELTAS = (0.05, 0.10, 0.15, 0.20)
CONFIRMS = (1, 2)
DEFAULT_BAND = 0.5
DEFAULT_EXCLUDE = ("PAXGUSDT", "XAUTUSDT")
BLOCK_HOURS = 4
RUNS_KEPT = timedelta(hours=48)
"""``score.json`` 只附最近 48 小时的逐轮明细（汇总照算整个窗口），面板读这个文件不至于太大。"""

Getter = Callable[..., httpx.Response]


# ------------------------------------------------------------------ 价格


class SymbolGone(Exception):
    """币安说没有这个币（HTTP 400 / 404）。"""


def _floor_closed(now_ms: int) -> int:
    """开盘时间小于这个值的 1 分钟线都已收盘。"""
    return (now_ms - MINUTE_MS) // MINUTE_MS * MINUTE_MS + MINUTE_MS


def _limit(span_ms: int, step_ms: int) -> int:
    """要几根就请求几根（币安按 limit 算权重：<100 根 1，<500 根 2，≤1000 根 5）。"""
    return max(1, min(1000, -(-span_ms // step_ms)))


class Stopped(Exception):
    """要求停下（面板关闭 / 重启）：这一轮作罢，不写结果。"""


class StopFlag(Protocol):
    def is_set(self) -> bool: ...


def stoppable(get: Getter, stop: StopFlag) -> Getter:
    """每次请求前看一眼 ``stop``，已设就抛 ``Stopped``（不吞进「拉不到」的分支）。"""

    def run(url: str, params: dict[str, Any] | None = None) -> httpx.Response:
        if stop.is_set():
            raise Stopped
        return get(url, params)

    return run


def paced(get: Getter, gap_s: float, sleep: Callable[[float], object] = time.sleep) -> Getter:
    """每次请求之间至少隔 ``gap_s`` 秒（面板后台跑时压低权重，大头留给交易程序）。"""
    last = [0.0]

    def run(url: str, params: dict[str, Any] | None = None) -> httpx.Response:
        wait = last[0] + gap_s - time.monotonic()
        if wait > 0:
            sleep(wait)
        last[0] = time.monotonic()
        return get(url, params)

    return run


class MinuteCloses:
    """币 → 1 分钟线收盘价，按 币 / UTC 日缓存：``{"end_ms": 已覆盖到, "c": [1440 个收盘价]}``
    （没有的分钟是 null）。要的分钟落在已覆盖范围外才请求，只补缺的那一段。拉不到的币记在
    ``failed``，这一次按没价格算。"""

    def __init__(self, cache_dir: Path, now: datetime, get: Getter | None = None) -> None:
        self._dir = cache_dir / "1m"
        self._now_ms = int(now.timestamp() * 1000)
        self._get: Getter = get or binance_gate.get
        self._days: dict[tuple[str, int], dict[str, Any]] = {}
        self.failed: set[str] = set()
        self.gone: set[str] = set()
        self.requests = 0

    def _path(self, sym: str, day0: int) -> Path:
        d = datetime.fromtimestamp(day0 / 1000, UTC).date().isoformat()
        return self._dir / sym / f"{d}.json"

    def _load(self, sym: str, day0: int) -> dict[str, Any]:
        key = (sym, day0)
        got = self._days.get(key)
        if got is not None:
            return got
        try:
            raw = json.loads(self._path(sym, day0).read_text(encoding="utf-8"))
            if not isinstance(raw.get("c"), list) or len(raw["c"]) != 1440:
                raise ValueError("缓存格式不对")
            got = {"end_ms": int(raw["end_ms"]), "c": raw["c"]}
        except (OSError, ValueError, KeyError, TypeError):
            got = {"end_ms": day0, "c": [None] * 1440}
        self._days[key] = got
        return got

    def _save(self, sym: str, day0: int, entry: Mapping[str, Any]) -> None:
        path = self._path(sym, day0)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"end_ms": entry["end_ms"], "c": entry["c"]}), "utf-8")
            tmp.replace(path)
        except OSError:
            logger.warning("大盘台记分：%s 的 K 线缓存写不了", sym, exc_info=True)

    def _fill(self, sym: str, day0: int, entry: dict[str, Any]) -> None:
        end = min(day0 + DAY_MS, _floor_closed(self._now_ms))
        start = int(entry["end_ms"])
        while start < end:
            self.requests += 1
            r = self._get(
                f"{FAPI}/fapi/v1/klines",
                {
                    "symbol": sym,
                    "interval": "1m",
                    "startTime": start,
                    "endTime": end - 1,
                    "limit": _limit(end - start, MINUTE_MS),
                },
            )
            if r.status_code in (400, 404):
                raise SymbolGone(sym)
            r.raise_for_status()
            rows = r.json()
            for row in rows:
                ot, close_ms = int(row[0]), int(row[6])
                if day0 <= ot < end and close_ms < self._now_ms:
                    entry["c"][(ot - day0) // MINUTE_MS] = float(row[4])
            if not rows or int(rows[-1][0]) + MINUTE_MS >= end:
                start = end
            else:
                start = max(start + MINUTE_MS, int(rows[-1][0]) + MINUTE_MS)
        entry["end_ms"] = max(int(entry["end_ms"]), end)
        self._save(sym, day0, entry)

    def px(self, sym: str, t: datetime) -> float | None:
        """t 时刻的价 = 收盘时间不晚于 t 的最后一根 1 分钟线的收盘价（t 正好整分钟时就是刚收盘
        那根；t 在分钟中间时是上一根，不用 t 之后才收盘的那根）。"""
        if sym in self.gone or sym in self.failed:
            return None
        t_ms = int(t.timestamp() * 1000)
        ot = (t_ms - MINUTE_MS) // MINUTE_MS * MINUTE_MS
        if ot + MINUTE_MS > self._now_ms:
            return None
        day0 = ot // DAY_MS * DAY_MS
        entry = self._load(sym, day0)
        if ot >= int(entry["end_ms"]):
            try:
                self._fill(sym, day0, entry)
            except binance_gate.RateLimited:
                raise
            except SymbolGone:
                logger.warning("大盘台记分：币安没有 %s，按没价格算", sym)
                self.gone.add(sym)
                return None
            except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
                logger.warning("大盘台记分：%s 的 1 分钟线拉不到：%s", sym, exc)
                self.failed.add(sym)
                return None
        v = entry["c"][(ot - day0) // MINUTE_MS]
        return float(v) if isinstance(v, int | float) and v > 0 else None


class HourCloses:
    """σ 用的 1h 收盘价（只给 Flash 旧口径用：大盘台的 σ 日志里有）。按币缓存连续的一段。"""

    def __init__(self, cache_dir: Path, now: datetime, get: Getter | None = None) -> None:
        self._dir = cache_dir / "1h"
        self._now_ms = int(now.timestamp() * 1000)
        self._get: Getter = get or binance_gate.get
        self._mem: dict[str, dict[str, Any]] = {}
        self.failed: set[str] = set()

    def _load(self, sym: str) -> dict[str, Any]:
        got = self._mem.get(sym)
        if got is not None:
            return got
        try:
            raw = json.loads((self._dir / f"{sym}.json").read_text(encoding="utf-8"))
            got = {
                "from_ms": int(raw["from_ms"]),
                "to_ms": int(raw["to_ms"]),
                "c": {int(k): float(v) for k, v in raw["c"].items()},
            }
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            got = {"from_ms": 0, "to_ms": 0, "c": {}}
        self._mem[sym] = got
        return got

    def series(self, sym: str, start_ms: int, end_ms: int) -> dict[int, Bar]:
        """开盘时间在 [start_ms, end_ms) 的已收盘 1h 线（只有收盘价有意义）。"""
        if sym in self.failed:
            return {}
        e = self._load(sym)
        end_ms = min(end_ms, (self._now_ms - HOUR_MS) // HOUR_MS * HOUR_MS + HOUR_MS)
        have = bool(e["c"])
        gaps = [(start_ms, e["from_ms"]), (e["to_ms"], end_ms)] if have else [(start_ms, end_ms)]
        gaps = [(a, b) for a, b in gaps if a < b]
        if gaps:
            try:
                for a, b in gaps:
                    e["c"].update(self._fetch(sym, a, b))
            except binance_gate.RateLimited:
                raise
            except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
                logger.warning("大盘台记分：%s 的 1h 线拉不到：%s", sym, exc)
                self.failed.add(sym)
                return {}
            lo = min(start_ms, e["from_ms"]) if have else start_ms
            e["from_ms"], e["to_ms"] = lo, max(end_ms, e["to_ms"])
            self._save(sym, e)
        return {
            k: Bar(k, v, v, v, v, 0.0, 0.0) for k, v in e["c"].items() if start_ms <= k < end_ms
        }

    def _fetch(self, sym: str, lo: int, hi: int) -> dict[int, float]:
        out: dict[int, float] = {}
        start = lo
        while start < hi:
            r = self._get(
                f"{FAPI}/fapi/v1/klines",
                {"symbol": sym, "interval": "1h", "startTime": start, "endTime": hi - 1,
                 "limit": _limit(hi - start, HOUR_MS)},
            )  # fmt: skip
            r.raise_for_status()
            rows = r.json()
            for row in rows:
                if int(row[6]) < self._now_ms:
                    out[int(row[0])] = float(row[4])
            if not rows or int(rows[-1][0]) + HOUR_MS >= hi:
                break
            start = int(rows[-1][0]) + HOUR_MS
        return out

    def _save(self, sym: str, e: Mapping[str, Any]) -> None:
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            body = {"from_ms": e["from_ms"], "to_ms": e["to_ms"], "c": e["c"]}
            (self._dir / f"{sym}.json").write_text(json.dumps(body), "utf-8")
        except OSError:
            logger.warning("大盘台记分：%s 的 1h 缓存写不了", sym, exc_info=True)


Price = Callable[[str, datetime], float | None]


def pool_ret(
    px: Price, basket: Sequence[str], a: datetime, b: datetime
) -> tuple[float | None, int]:
    """等权池 a → b 的简单收益（%）= 两端都有价的币收益的平均；返回 (收益, 有价币数)。"""
    rets = []
    for s in basket:
        pa, pb = px(s, a), px(s, b)
        if pa is not None and pb is not None and pa > 0:
            rets.append((pb / pa - 1.0) * 100.0)
    return (statistics.fmean(rets) if rets else None), len(rets)


# ------------------------------------------------------------------ 读日志


@dataclass(frozen=True)
class LoggedRun:
    cycle_id: str
    t0: datetime
    completed: datetime
    reason: str
    probs: Mapping[str, Probs]
    direction: str
    basket: tuple[str, ...]
    band_pct: Mapping[str, float | None]
    base: Mapping[str, Probs | None]
    prior_missing: bool
    mirror_test: bool
    mirror_probs: Probs | None
    published: str | None
    """这一轮发布了的方向（没发布是 None）。"""
    settings: Mapping[str, Any]
    prompt_version: str = ""

    @property
    def band_sigma(self) -> float:
        return _num(self.settings.get("band_sigma")) or DEFAULT_BAND

    @property
    def group(self) -> str:
        """口径：提示词版本 + 平的带宽。换了口径的轮次不和之前的混在一起算。"""
        return f"{self.prompt_version or '?'} · band {self.band_sigma:g}σ"


@dataclass
class DeskLog:
    runs: list[LoggedRun] = field(default_factory=list)
    owner_flash_at: list[datetime] = field(default_factory=list)
    """大盘台把大盘方向交回 Flash 的时刻（之后 Flash 发过，下一轮大盘台算「接管」，立即发布）。"""
    failed: int = 0
    skipped: int = 0


def _probs(raw: Any) -> Probs | None:
    if not isinstance(raw, Mapping):
        return None
    try:
        vals = [float(raw[k]) for k in CLASSES]
    except (KeyError, TypeError, ValueError):
        return None
    s = sum(vals)
    if not all(math.isfinite(v) and v >= 0 for v in vals) or s <= 0:
        return None
    return Probs(vals[0] / s, vals[1] / s, vals[2] / s)


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, int | float):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def read_desk_log(data_dir: Path, since: datetime, until: datetime) -> DeskLog:
    out = DeskLog()
    root = data_dir / LOG_DIR
    day = since.astimezone(UTC).date()
    while day <= until.astimezone(UTC).date():
        path = root / f"{day.isoformat()}.jsonl"
        day += timedelta(days=1)
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if not isinstance(r, dict):
                continue
            if r.get("event") == "owner_change":
                at = parse_ts(r.get("at"))
                if at is not None and r.get("to") == "flash" and since <= at <= until:
                    out.owner_flash_at.append(at)
                continue
            t0 = parse_ts(r.get("t0"))
            if t0 is None or not since <= t0 <= until:
                continue
            if r.get("status") == "failed":
                out.failed += 1
                continue
            if r.get("status") != "ok":
                out.skipped += 1
                continue
            run = _desk_run(r, t0)
            if run is not None:
                out.runs.append(run)
    out.runs.sort(key=lambda x: x.t0)
    out.owner_flash_at.sort()
    return out


def _desk_run(r: Mapping[str, Any], t0: datetime) -> LoggedRun | None:
    p4, p1 = _probs(r.get("probs_4h")), _probs(r.get("probs_1h"))
    targets = r.get("score_targets")
    if p4 is None or p1 is None or not isinstance(targets, Mapping):
        return None
    basket = tuple(str(s) for s in targets.get("basket") or [])
    band: dict[str, float | None] = {}
    base: dict[str, Probs | None] = {}
    for h in HORIZONS:
        got = targets.get(h)
        t: Mapping[str, Any] = got if isinstance(got, Mapping) else {}
        band[h] = _num(t.get("flat_band_pct"))
        base[h] = _probs(t.get("base_rate"))
    pub = r.get("published_view")
    published = pub.get("regime_direction") if isinstance(pub, Mapping) else None
    if r.get("republished_same_direction"):
        published = None  # same-direction refresh: not a direction publish
    mirror = r.get("mirror")
    ms = r.get("latency_ms")
    completed = t0 + timedelta(milliseconds=float(ms) if isinstance(ms, int | float) else 0.0)
    settings = r.get("settings")
    return LoggedRun(
        cycle_id=str(r.get("cycle_id", "")),
        t0=t0,
        completed=completed,
        reason=str(r.get("reason", "scheduled")),
        probs={"4h": p4, "1h": p1},
        direction=str(r.get("direction", "中性")),
        basket=basket,
        band_pct=band,
        base=base,
        prior_missing=bool(r.get("prior_missing")),
        mirror_test=bool(r.get("mirror_test")),
        mirror_probs=_probs(mirror.get("probs_4h")) if isinstance(mirror, Mapping) else None,
        published=str(published) if isinstance(published, str) else None,
        settings=settings if isinstance(settings, Mapping) else {},
        prompt_version=str(r.get("prompt_version") or ""),
    )


@dataclass(frozen=True)
class FlashRound:
    t: datetime
    direction: str
    basket: tuple[str, ...]


def read_flash(
    data_dir: Path, since: datetime, until: datetime, exclude: Iterable[str]
) -> list[FlashRound]:
    """总盘会每轮的大盘方向（发布的和只记录的都算）和那一轮的池。"""
    skip = set(exclude)
    out: list[FlashRound] = []
    for path in sorted((data_dir / "market_meeting").glob("????-??-??.jsonl")):
        try:
            d = date.fromisoformat(path.stem)
        except ValueError:
            continue
        if not since.date() - timedelta(days=1) <= d <= until.date():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                t = parse_ts(r.get("ts"))
                syms = tuple(
                    p["symbol"] for p in r.get("pool") or [] if p.get("symbol") not in skip
                )
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
            direction = r.get("regime_direction")
            if t is None or direction not in DIR_CLASS or not syms or not since <= t <= until:
                continue
            out.append(FlashRound(t, str(direction), syms))
    return sorted(out, key=lambda f: f.t)


# ------------------------------------------------------------------ 打分


def onehot(cls: str) -> tuple[float, float, float]:
    return tuple(1.0 if c == cls else 0.0 for c in CLASSES)  # type: ignore[return-value]


def as_tuple(p: Probs) -> tuple[float, float, float]:
    return (p.up, p.flat, p.down)


def sharp(cls: str) -> Probs:
    lo = (1.0 - SHARP) / 2
    return Probs(*(SHARP if c == cls else lo for c in CLASSES))


def brier(p: Probs, y: str) -> float:
    return sum((a - b) ** 2 for a, b in zip(as_tuple(p), onehot(y), strict=True))


def logloss(p: Probs, y: str) -> float:
    return -math.log(max(as_tuple(p)[CLASSES.index(y)], LOG_CLIP))


def rps(p: Probs, y: str) -> float:
    """有序三类（跌 < 平 < 涨）的 RPS。"""
    o = onehot(y)
    cp1, co1 = p.down, o[2]
    cp2, co2 = p.down + p.flat, o[2] + o[1]
    return ((cp1 - co1) ** 2 + (cp2 - co2) ** 2) / 2


def block_of(t: datetime) -> str:
    t = t.astimezone(UTC)
    return f"{t:%Y-%m-%d}T{t.hour // BLOCK_HOURS * BLOCK_HOURS:02d}"


@dataclass(frozen=True)
class Outcome:
    ret_pct: float
    band_pct: float
    cls: str
    priced: int


@dataclass
class Scored:
    run: LoggedRun
    out: dict[str, Outcome] = field(default_factory=dict)
    trail_1h: float | None = None
    trail_4h: float | None = None
    persist: str | None = None
    """近 1 小时等权池收益按 1h 的 ±band·σ 归类（「延续」基线叫的那一类）。"""
    trail_cls: str | None = None
    """近 4 小时等权池收益按 4h 的 ±band·σ 归类（延迟分析用）。"""
    in_force: str | None = None
    """t0 时生效的大盘台发布方向（大盘台没发过或已交回 Flash 是 None）。"""


def resolve_runs(
    runs: Sequence[LoggedRun], px: Price, now: datetime
) -> tuple[list[Scored], dict[str, int]]:
    counts = {"pending_1h": 0, "pending_4h": 0, "unpriced_1h": 0, "unpriced_4h": 0}
    out: list[Scored] = []
    for run in runs:
        s = Scored(run)
        n = len(run.basket)
        need = max(1, math.ceil(MIN_COVERAGE * n))
        for h in HORIZONS:
            end = run.t0 + timedelta(hours=HOURS[h])
            band = run.band_pct[h]
            if end + SETTLE > now:
                counts[f"pending_{h}"] += 1
                continue
            r, got = pool_ret(px, run.basket, run.t0, end)
            if r is None or band is None or got < need:
                counts[f"unpriced_{h}"] += 1
                continue
            s.out[h] = Outcome(r, band, classify(r, band, 1.0), got)
        for hours, attr in ((1, "trail_1h"), (4, "trail_4h")):
            r, got = pool_ret(px, run.basket, run.t0 - timedelta(hours=hours), run.t0)
            if r is not None and got >= need:
                setattr(s, attr, r)
        b1, b4 = run.band_pct["1h"], run.band_pct["4h"]
        if s.trail_1h is not None and b1 is not None:
            s.persist = classify(s.trail_1h, b1, 1.0)
        if s.trail_4h is not None and b4 is not None:
            s.trail_cls = classify(s.trail_4h, b4, 1.0)
        out.append(s)
    return out, counts


def _mean(xs: Sequence[float]) -> float | None:
    return statistics.fmean(xs) if xs else None


def _r(v: float | None, nd: int = 4) -> float | None:
    return None if v is None else round(v, nd)


def reliability(pairs: Sequence[tuple[Probs, str]], bins: int = 10) -> dict[str, list[Any]]:
    """每一类：预测概率按 0.1 分箱，箱里的平均预测和实际发生频率。"""
    out: dict[str, list[Any]] = {}
    for i, c in enumerate(CLASSES):
        acc: dict[int, list[tuple[float, float]]] = {}
        for p, y in pairs:
            v = as_tuple(p)[i]
            acc.setdefault(min(int(v * bins), bins - 1), []).append((v, 1.0 if y == c else 0.0))
        out[c] = [
            {
                "bin": f"{k / bins:.1f}-{(k + 1) / bins:.1f}",
                "n": len(v),
                "mean_p": round(statistics.fmean(a for a, _ in v), 4),
                "freq": round(statistics.fmean(b for _, b in v), 4),
            }
            for k, v in sorted(acc.items())
        ]
    return out


def confusion(pairs: Iterable[tuple[str, str]]) -> dict[str, dict[str, int]]:
    """行 = 叫的方向（up / flat / down），列 = 实际。"""
    m = {a: dict.fromkeys(CLASSES, 0) for a in CLASSES}
    for called, actual in pairs:
        m[called][actual] += 1
    return m


def call_stats(pairs: Sequence[tuple[str, str]]) -> dict[str, Any]:
    """方向命中、非平判断的覆盖（占比）和精度。"""
    n = len(pairs)
    calls = [(c, y) for c, y in pairs if c != "flat"]
    return {
        "n": n,
        "hit": _r(_mean([1.0 if c == y else 0.0 for c, y in pairs])),
        "coverage": _r(len(calls) / n if n else None),
        "precision": _r(_mean([1.0 if c == y else 0.0 for c, y in calls])),
        "n_calls": len(calls),
        "n_up_calls": sum(1 for c, _ in calls if c == "up"),
        "n_down_calls": sum(1 for c, _ in calls if c == "down"),
    }


def run_direction(run: LoggedRun, h: str) -> str:
    """这一轮在 h 上叫的方向：4h 用日志里的（发布用的就是它）；1h 按当时的 θ / δ 从 1h 概率派生。"""
    if h == "4h":
        return run.direction if run.direction in DIR_CLASS else "中性"
    theta = _num(run.settings.get("call_p_min"))
    delta = _num(run.settings.get("call_edge_min"))
    return derive_direction(run.probs[h], theta or 0.45, delta or 0.15)


def prob_metrics(rows: Sequence[Scored], h: str) -> dict[str, Any]:
    """一个期限上大盘台概率 vs 先验 vs 延续。只算先验齐全的轮次（同一批比）。"""
    use = [s for s in rows if h in s.out and s.run.base[h] is not None]
    if not use:
        return {"n": 0}
    ys = [s.out[h].cls for s in use]
    desk = [s.run.probs[h] for s in use]
    base = [bp for s in use if (bp := s.run.base[h]) is not None]
    blocks = [block_of(s.run.t0) for s in use]
    res: dict[str, Any] = {
        "n": len(use),
        "outcome_freq": {c: round(ys.count(c) / len(ys), 4) for c in CLASSES},
    }
    for name, fn in (("brier", brier), ("logloss", logloss), ("rps", rps)):
        d = [fn(p, y) for p, y in zip(desk, ys, strict=True)]
        b = [fn(p, y) for p, y in zip(base, ys, strict=True)]
        res[name] = {"desk": _r(_mean(d)), "base": _r(_mean(b))}
        diff = [x - z for x, z in zip(d, b, strict=True)]
        lo, hi, k = cluster_ci(diff, blocks)
        res[name]["desk_minus_base"] = _r(_mean(diff))
        res[name]["ci95"] = [_r(lo), _r(hi)] if lo is not None else None
        res[name]["blocks"] = k
        pers = [(fn(sharp(s.persist), y), fn(p, y)) for s, p, y in zip(use, desk, ys, strict=True)
                if s.persist is not None]  # fmt: skip
        res[name]["persist"] = _r(_mean([a for a, _ in pers]))
        res[name]["desk_on_persist_rows"] = _r(_mean([b2 for _, b2 in pers]))
        res[name]["n_persist"] = len(pers)
    bb = res["brier"]
    res["brier_skill_vs_base"] = (
        _r(1 - bb["desk"] / bb["base"]) if bb["base"] not in (None, 0) else None
    )
    res["argmax_hit"] = _r(
        _mean([1.0 if CLASSES[max(range(3), key=lambda i: as_tuple(p)[i])] == y else 0.0
               for p, y in zip(desk, ys, strict=True)])
    )  # fmt: skip
    calls = [(DIR_CLASS[run_direction(s.run, h)], y) for s, y in zip(use, ys, strict=True)]
    res["direction"] = call_stats(calls)
    res["confusion"] = confusion(calls)
    res["reliability"] = reliability(list(zip(desk, ys, strict=True)))
    return res


# ------------------------------------------------------------------ 发布：实际与重放


def published_in_force(
    runs: Sequence[LoggedRun], flash_at: Sequence[datetime]
) -> list[tuple[datetime, str | None]]:
    """发布时间线：(时刻, 生效方向)；交回 Flash 记成 None。"""
    events: list[tuple[datetime, str | None]] = [
        (r.completed, r.published) for r in runs if r.published is not None
    ]
    events += [(t, None) for t in flash_at]
    return sorted(events, key=lambda e: e[0])


def in_force_at(timeline: Sequence[tuple[datetime, str | None]], t: datetime) -> str | None:
    cur: str | None = None
    for at, d in timeline:
        if at > t:
            break
        cur = d
    return cur


def replay_publishes(
    runs: Sequence[LoggedRun],
    directions: Sequence[str],
    flash_at: Sequence[datetime],
    *,
    confirm: int | None = None,
) -> list[tuple[datetime, str | None]]:
    """按 ``MarketDesk._maybe_publish`` 的滞回规则重放发布（确认轮数、持续时间、最小间隔取每轮当时的
    设置；``confirm`` 给了就替换确认轮数）。第一轮和交回 Flash 之后的第一轮算接管，立即发布。
    没有先验的轮次不发布、不计确认（和大盘台一样）。"""
    out: list[tuple[datetime, str | None]] = []
    view: str | None = None
    takeover = True
    cand: str | None = None
    count = 0
    since: datetime | None = None
    event = False
    last_pub: datetime | None = None
    flashes = sorted(flash_at)
    fi = 0
    for run, d in zip(runs, directions, strict=True):
        while fi < len(flashes) and flashes[fi] <= run.t0:
            out.append((flashes[fi], None))
            takeover, view = True, None
            fi += 1
        if run.prior_missing:
            continue
        cfg = run.settings
        need_n = confirm if confirm is not None else int(cfg.get("publish_confirm", 2))
        cadence = float(cfg.get("cadence_min", 60))
        confirm_min = float(cfg.get("publish_confirm_min", 0))
        min_gap = float(cfg.get("publish_min_gap_min", 0))
        if not takeover and d == view:
            cand, count, since, event = None, 0, None, False
            continue
        if not takeover:
            ev = run.reason != "scheduled"
            if d == cand and since is not None:
                count += 1
                event = event or ev
            else:
                cand, count, since, event = d, 1, run.t0, ev
            if count < need_n:
                continue
            spread = timedelta(minutes=max(confirm_min / 2, cadence))
            need = spread if event else max(spread, timedelta(minutes=confirm_min))
            if run.t0 - since < need:
                continue
            if last_pub is not None and run.completed - last_pub < timedelta(minutes=min_gap):
                continue
        out.append((run.completed, d))
        view, takeover = d, False
        cand, count, since, event = None, 0, None, False
        last_pub = run.completed
    while fi < len(flashes):
        out.append((flashes[fi], None))
        fi += 1
    return out


def published_hits(
    rows: Sequence[Scored], timeline: Sequence[tuple[datetime, str | None]], h: str
) -> dict[str, Any]:
    """每轮按 t0 时生效的发布方向记命中（按轮，不按发布）。"""
    pairs = []
    for s in rows:
        d = in_force_at(timeline, s.run.t0)
        if d is None or h not in s.out:
            continue
        pairs.append((DIR_CLASS.get(d, "flat"), s.out[h].cls))
    return call_stats(pairs) | {"confusion": confusion(pairs)}


def episodes(rows: Sequence[Scored]) -> list[tuple[datetime, datetime, str]]:
    """「已发生的行情」：近 4 小时等权池收益连续停在 +band·σ 之上（或 −band·σ 之下）的一段，
    持续 ≥ ``SUSTAIN``。返回 (开始, 最后一轮, up / down)。"""
    out: list[tuple[datetime, datetime, str]] = []
    cur: tuple[datetime, datetime, str] | None = None
    for s in rows:
        c = s.trail_cls
        if cur is not None and c == cur[2]:
            cur = (cur[0], s.run.t0, c)
            continue
        if cur is not None and cur[1] - cur[0] >= SUSTAIN:
            out.append(cur)
        cur = (s.run.t0, s.run.t0, c) if c in ("up", "down") else None
    if cur is not None and cur[1] - cur[0] >= SUSTAIN:
        out.append(cur)
    return out


def latency(
    eps: Sequence[tuple[datetime, datetime, str]],
    events: Sequence[tuple[datetime, str | None]],
    last: datetime | None = None,
) -> dict[str, Any]:
    """每段行情：开始时已经同向生效记 0（already）；段内发布了同向记分钟数；到段结束都没有记
    missed；段落还没结束（最后一轮 ``last`` 还在段里）又还没跟上记 open，不算 missed。
    ``events`` = (时刻, 方向)，发布时间线或每轮叫的方向都行。"""
    lat: list[float] = []
    already = missed = still = 0
    detail = []
    for start, end, c in eps:
        want = {"up": "看涨", "down": "看跌"}[c]
        if in_force_at(events, start) == want:
            already += 1
            lat.append(0.0)
            detail.append({"start": start.isoformat(), "dir": c, "latency_min": 0.0})
            continue
        hit = next((at for at, d in events if start <= at and d == want), None)
        if hit is None or hit > end + timedelta(minutes=5):
            ongoing = last is not None and end >= last
            still += ongoing
            missed += not ongoing
            detail.append(
                {"start": start.isoformat(), "dir": c, "latency_min": None, "open": ongoing}
            )
            continue
        m = (hit - start).total_seconds() / 60
        lat.append(m)
        detail.append({"start": start.isoformat(), "dir": c, "latency_min": round(m, 1)})
    return {
        "episodes": len(eps),
        "caught": len(lat),
        "already": already,
        "missed": missed,
        "open": still,
        "median_min": _r(statistics.median(lat), 1) if lat else None,
        "mean_min": _r(_mean(lat), 1),
        "detail": detail[-20:],
    }


def call_events(rows: Sequence[Scored], dirs: Sequence[str]) -> list[tuple[datetime, str | None]]:
    """每轮叫的方向当成「发布」（看大盘台自己多快叫出来，不管滞回）。"""
    return [(s.run.completed, d) for s, d in zip(rows, dirs, strict=True)]


KEYS = ("n", "hit", "coverage", "precision")


def sweep(rows: Sequence[Scored], flash_at: Sequence[datetime]) -> list[dict[str, Any]]:
    eps = episodes(rows)
    last = rows[-1].run.t0 if rows else None
    out = []
    usable = [s for s in rows if not s.run.prior_missing]
    for theta in THETAS:
        for delta in DELTAS:
            dirs = [derive_direction(s.run.probs["4h"], theta, delta) for s in rows]
            calls = {
                h: call_stats(
                    [
                        (DIR_CLASS[derive_direction(s.run.probs[h], theta, delta)], s.out[h].cls)
                        for s in rows
                        if h in s.out and not s.run.prior_missing
                    ]
                )
                for h in HORIZONS
            }
            call_lat = latency(eps, call_events(rows, dirs), last)
            for k in CONFIRMS:
                tl = replay_publishes([s.run for s in rows], dirs, flash_at, confirm=k)
                pub = published_hits(usable, tl, "4h")
                lat = latency(eps, tl, last)
                out.append(
                    {
                        "theta": theta,
                        "delta": delta,
                        "confirm": k,
                        "call_4h": {x: calls["4h"][x] for x in KEYS},
                        "call_1h": {x: calls["1h"][x] for x in KEYS},
                        "published_4h": {x: pub[x] for x in KEYS},
                        "publishes": sum(1 for _, d in tl if d is not None),
                        "latency_median_min": lat["median_min"],
                        "missed": lat["missed"],
                        "open": lat["open"],
                        "episodes": lat["episodes"],
                        "call_latency_median_min": call_lat["median_min"],
                    }
                )  # fmt: skip
    return out


# ------------------------------------------------------------------ Flash 旧口径


def score_flash(
    rounds: Sequence[FlashRound],
    px: Price,
    hours: HourCloses,
    now: datetime,
    band_at: Callable[[datetime], float],
) -> dict[str, Any]:
    res: dict[str, Any] = {}
    span: dict[str, tuple[int, int]] = {}
    for f in rounds:
        t_ms = int(f.t.timestamp() * 1000) // HOUR_MS * HOUR_MS
        lo = t_ms - (SIGMA_LOOKBACK_H + 5) * HOUR_MS
        for s in f.basket:
            a, b = span.get(s, (lo, t_ms))
            span[s] = (min(a, lo), max(b, t_ms))
    for s, (a, b) in span.items():  # 每个币一次拉够，后面逐轮只读缓存
        hours.series(s, a, b)
    for h in HORIZONS:
        hh = HOURS[h]
        pairs: list[tuple[str, str]] = []
        sharp_b: list[float] = []
        pending = unpriced = 0
        for f in rounds:
            end = f.t + timedelta(hours=hh)
            if end + SETTLE > now:
                pending += 1
                continue
            t_ms = int(f.t.timestamp() * 1000) // HOUR_MS * HOUR_MS
            lo = t_ms - (SIGMA_LOOKBACK_H + 5) * HOUR_MS
            series = {s: hours.series(s, lo, t_ms) for s in f.basket}
            sigma = sigma_at(series, t_ms, hh, SIGMA_LOOKBACK_H)
            r, got = pool_ret(px, f.basket, f.t, end)
            if sigma is None or r is None or got < max(1, math.ceil(MIN_COVERAGE * len(f.basket))):
                unpriced += 1
                continue
            y = classify(r, sigma, band_at(f.t))
            c = DIR_CLASS[f.direction]
            pairs.append((c, y))
            sharp_b.append(brier(sharp(c), y))
        res[h] = call_stats(pairs) | {
            "confusion": confusion(pairs),
            "brier_sharp": _r(_mean(sharp_b)),
            "pending": pending,
            "unpriced": unpriced,
        }
    res["rounds"] = len(rounds)
    return res


# ------------------------------------------------------------------ 汇总


def band_in_force(runs: Sequence[LoggedRun]) -> Callable[[datetime], float]:
    """t 时刻生效的带宽：t 之前最近一轮大盘台的设置；比第一轮还早就用第一轮的（没有大盘台时
    用默认 0.5σ）。Flash 轮次按它自己那时的带宽归类，不拿最后一轮的。"""
    ordered = sorted(runs, key=lambda r: r.t0)

    def at(t: datetime) -> float:
        cur = ordered[0].band_sigma if ordered else DEFAULT_BAND
        for r in ordered:
            if r.t0 > t:
                break
            cur = r.band_sigma
        return cur

    return at


def _window(rows: Sequence[Scored], since: datetime) -> list[Scored]:
    return [s for s in rows if s.run.t0 >= since]


def panel_summary(
    rows: Sequence[Scored],
    timeline: Sequence[tuple[datetime, str | None]],
    now: datetime,
    group: str | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {"group": group}
    parts = []
    for name, span in (("24h", timedelta(hours=24)), ("7d", timedelta(days=7))):
        w = [s for s in _window(rows, now - span) if not s.run.prior_missing]
        m = prob_metrics(w, "4h")
        pub = published_hits(w, timeline, "4h")
        out[name] = {
            "n": m["n"],
            "brier": m["brier"]["desk"] if m["n"] else None,
            "brier_base": m["brier"]["base"] if m["n"] else None,
            "hit": m["direction"]["hit"] if m["n"] else None,
            "published_hit": pub["hit"],
            "published_n": pub["n"],
        }
        label = "近 24 小时" if name == "24h" else "近 7 天"
        o = out[name]
        if not o["n"]:
            parts.append(f"{label}：4h 还没有走完的轮次")
            continue
        hit = f"命中 {o['hit']:.0%}" if o["hit"] is not None else ""
        ph = f"发布方向命中 {o['published_hit']:.0%}" if o["published_hit"] is not None else ""
        parts.append(
            f"{label}：4h Brier {o['brier']:.3f} vs 先验 {o['brier_base']:.3f} · "
            + " · ".join(x for x in (hit, ph) if x)
            + f"（n {o['n']}）"
        )
    out["text"] = (f"口径 {group}：" if group else "") + "；".join(parts)
    return out


def _section(
    runs: Sequence[LoggedRun],
    rows: Sequence[Scored],
    timeline: Sequence[tuple[datetime, str | None]],
    flash_at: Sequence[datetime],
    now: datetime,
) -> dict[str, Any]:
    """一个口径内的全部指标。发布时间线用全部轮次（t0 时生效的是哪一版是事实），其余只看本口径。"""
    usable = [s for s in rows if not s.run.prior_missing]
    replay = replay_publishes(runs, [r.direction for r in runs], flash_at)
    eps = episodes(rows)
    last = rows[-1].run.t0 if rows else None
    mirror_rows = [s for s in usable if s.run.mirror_test and "4h" in s.out]
    mirror_main = [brier(s.run.probs["4h"], s.out["4h"].cls) for s in mirror_rows]
    mirror_swap = [
        brier(Probs(m.down, m.flat, m.up), s.out["4h"].cls)
        for s in mirror_rows
        if (m := s.run.mirror_probs) is not None
    ]
    own = {r.completed for r in runs}
    return {
        "desk": {h: prob_metrics(usable, h) for h in ("4h", "1h")},
        "published": {
            "publishes": sum(1 for at, d in timeline if d is not None and at in own),
            "replay_publishes": sum(1 for _, d in replay if d is not None),
            "4h": published_hits(usable, timeline, "4h"),
            "1h": published_hits(usable, timeline, "1h"),
        },
        "latency": {
            "definition": (
                "行情 = 近 4 小时等权池收益连续 ≥ 30 分钟停在 ±flat_band_pct 外；"
                "publish = 多久后发布同向，call = 多久后某一轮叫出同向"
            ),
            "publish": latency(eps, timeline, last),
            "call": latency(eps, call_events(rows, [r.run.direction for r in rows]), last),
        },
        "mirror": {
            "n": len(mirror_rows),
            "brier_main": _r(_mean(mirror_main)),
            "brier_mirror_swapped": _r(_mean(mirror_swap)),
            "n_mirror": len(mirror_swap),
        },
        "sweep": sweep(rows, flash_at),
    }


def _brief(rows: Sequence[Scored], h: str) -> dict[str, Any]:
    m = prob_metrics([s for s in rows if not s.run.prior_missing], h)
    if not m["n"]:
        return {"n": 0}
    return {
        "n": m["n"],
        "brier": m["brier"]["desk"],
        "brier_base": m["brier"]["base"],
        "hit": m["direction"]["hit"],
    }


def summarize(
    log: DeskLog,
    rows: Sequence[Scored],
    counts: Mapping[str, int],
    flash: Mapping[str, Any] | None,
    now: datetime,
    since: datetime,
) -> dict[str, Any]:
    """顶层的指标、扫描和面板一行都只算当前口径（最后一轮的提示词版本 + 带宽）；
    每个口径的简表在 ``groups``。"""
    timeline = published_in_force(log.runs, log.owner_flash_at)
    for s in rows:
        s.in_force = in_force_at(timeline, s.run.t0)
    current = log.runs[-1].group if log.runs else None
    cur_runs = [r for r in log.runs if r.group == current]
    cur_rows = [s for s in rows if s.run.group == current]
    groups = []
    for key in dict.fromkeys(r.group for r in log.runs):
        g_runs = [r for r in log.runs if r.group == key]
        g_rows = [s for s in rows if s.run.group == key]
        groups.append(
            {
                "group": key,
                "prompt_version": g_runs[0].prompt_version,
                "band_sigma": g_runs[0].band_sigma,
                "first_t0": g_runs[0].t0.isoformat(),
                "last_t0": g_runs[-1].t0.isoformat(),
                "runs": len(g_runs),
                "4h": _brief(g_rows, "4h"),
                "1h": _brief(g_rows, "1h"),
            }
        )
    usable = [s for s in cur_rows if not s.run.prior_missing]
    return {
        "generated_at": now.isoformat(),
        "since": since.isoformat(),
        "definition": (
            "每轮 t0 起 1h / 4h：t0 时那一池（basket）各币 1 分钟线收盘价简单收益的等权平均，"
            "落在 ±flat_band_pct 以内为平；按轮记分，不按发布；只汇总当前口径"
        ),
        "group": current,
        "groups": groups,
        "counts": {
            "runs_ok": len(log.runs),
            "runs_current_group": len(cur_runs),
            "runs_failed": log.failed,
            "runs_skipped": log.skipped,
            "prior_missing": sum(1 for r in cur_runs if r.prior_missing),
            "mirror_test_runs": sum(1 for r in cur_runs if r.mirror_test),
            "scored_1h": sum(1 for s in usable if "1h" in s.out),
            "scored_4h": sum(1 for s in usable if "4h" in s.out),
            **counts,
        },
        **_section(cur_runs, cur_rows, timeline, log.owner_flash_at, now),
        "flash": flash,
        "panel": panel_summary(cur_rows, timeline, now, current),
        "runs": [_row_json(s) for s in rows if s.run.t0 >= now - RUNS_KEPT],
    }


def _row_json(s: Scored) -> dict[str, Any]:
    r = s.run
    return {
        "cycle_id": r.cycle_id,
        "t0": r.t0.isoformat(),
        "reason": r.reason,
        "group": r.group,
        "direction": r.direction,
        "in_force": s.in_force,
        "published": r.published,
        "prior_missing": r.prior_missing,
        "mirror_test": r.mirror_test,
        "probs_4h": r.probs["4h"].as_dict(),
        "trail_1h_pct": _r(s.trail_1h),
        "trail_4h_pct": _r(s.trail_4h),
        "out": {
            h: {"ret_pct": round(o.ret_pct, 4), "band_pct": round(o.band_pct, 4), "cls": o.cls,
                "priced": o.priced}
            for h, o in s.out.items()
        },
    }  # fmt: skip


def prune_cache(cache: Path, now: datetime, days: float) -> int:
    """删掉窗口 + 2 天以前的缓存：1 分钟线按日文件删；1h 线删掉更早的根（σ 还要往前 175 小时）。
    返回删掉的日文件数。"""
    keep_from = now - timedelta(days=days + 2)
    cut_day = keep_from.astimezone(UTC).date().isoformat()
    n = 0
    for sym_dir in (cache / "1m").glob("*"):
        if not sym_dir.is_dir():
            continue
        for f in sym_dir.glob("????-??-??.json"):
            if f.stem < cut_day:
                f.unlink(missing_ok=True)
                n += 1
        with contextlib.suppress(OSError):
            sym_dir.rmdir()  # 空了才删得掉
    cut_ms = ms_of(keep_from) - (SIGMA_LOOKBACK_H + 5) * HOUR_MS
    for f in (cache / "1h").glob("*.json"):
        try:
            raw = json.loads(f.read_text(encoding="utf-8"))
            if int(raw["from_ms"]) >= cut_ms:
                continue
            raw["c"] = {k: v for k, v in raw["c"].items() if int(k) >= cut_ms}
            raw["from_ms"] = min(max(cut_ms, int(raw["from_ms"])), int(raw["to_ms"]))
            f.write_text(json.dumps(raw), "utf-8")
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            f.unlink(missing_ok=True)
    return n


def ms_of(t: datetime) -> int:
    return int(t.timestamp() * 1000)


def update(
    data_dir: str | Path,
    now: datetime,
    *,
    days: float = 7.0,
    out_path: Path | None = None,
    cache_dir: Path | None = None,
    get: Getter | None = None,
    flash: bool = True,
    stop: StopFlag | None = None,
) -> dict[str, Any]:
    """读日志、拉缺的 K 线、算分，写 ``score.json``（``out_path`` 给了就写那里）。同步；限流时
    抛 ``binance_gate.RateLimited``，``stop`` 设了就在下一次请求前抛 ``Stopped``，都不写结果。"""
    root = Path(data_dir)
    since = now - timedelta(days=days)
    cache = cache_dir or root / LOG_DIR / CACHE_DIR
    if stop is not None:
        get = stoppable(get or binance_gate.get, stop)
    log = read_desk_log(root, since, now)
    closes = MinuteCloses(cache, now, get)
    rows, counts = resolve_runs(log.runs, closes.px, now)
    flash_res = None
    if flash:
        latest = log.runs[-1].settings if log.runs else {}
        exclude = latest.get("exclude_symbols") or DEFAULT_EXCLUDE
        rounds = read_flash(root, since, now, exclude)
        hours = HourCloses(cache, now, get)
        flash_res = score_flash(rounds, closes.px, hours, now, band_in_force(log.runs))
        flash_res["paired"] = _paired(rows, rounds)
    report = summarize(log, rows, counts, flash_res, now, since)
    report["fetch"] = {
        "pruned_files": prune_cache(cache, now, days),
        "requests_1m": closes.requests,
        "failed": sorted(closes.failed),
        "gone": sorted(closes.gone),
    }
    path = out_path or root / LOG_DIR / SCORE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return report


def _paired(rows: Sequence[Scored], rounds: Sequence[FlashRound]) -> dict[str, Any]:
    """同一时段比：每个 Flash 轮次配它之前 20 分钟内最近的一轮大盘台，两边按大盘台的结局记命中。"""
    out: dict[str, Any] = {}
    for h in HORIZONS:
        desk_hit, flash_hit = [], []
        for f in rounds:
            cands = [s for s in rows if f.t - timedelta(minutes=20) <= s.run.t0 <= f.t]
            if not cands or h not in cands[-1].out:
                continue
            y = cands[-1].out[h].cls
            desk_hit.append(1.0 if DIR_CLASS.get(cands[-1].run.direction) == y else 0.0)
            flash_hit.append(1.0 if DIR_CLASS[f.direction] == y else 0.0)
        out[h] = {"n": len(desk_hit), "desk_hit": _r(_mean(desk_hit)),
                  "flash_hit": _r(_mean(flash_hit))}  # fmt: skip
    return out


# ------------------------------------------------------------------ 文字版


def _f(v: Any, fmt: str = ".3f") -> str:
    return "-" if v is None else format(v, fmt)


def render(rep: Mapping[str, Any]) -> list[str]:
    c = rep["counts"]
    lines = [
        f"大盘台记分（{rep['since'][:16]} 起，当前口径 {rep['group']}，"
        f"{c['runs_current_group']} 轮）：成功 {c['runs_ok']} 轮"
        f"（没先验 {c['prior_missing']}、"
        f"镜像测试 {c['mirror_test_runs']}），失败 {c['runs_failed']}，跳过 {c['runs_skipped']}；"
        f"已结算 1h {c['scored_1h']} / 4h {c['scored_4h']}，未到点 1h {c['pending_1h']} / "
        f"4h {c['pending_4h']}，缺价 1h {c['unpriced_1h']} / 4h {c['unpriced_4h']}",
    ]
    for g in rep["groups"]:
        g4, g1 = g["4h"], g["1h"]
        lines.append(
            f"  口径 {g['group']}（{g['first_t0'][:16]} – {g['last_t0'][:16]}，{g['runs']} 轮）："
            f"4h Brier {_f(g4.get('brier'))} vs 先验 {_f(g4.get('brier_base'))}（n {g4['n']}），"
            f"1h {_f(g1.get('brier'))} vs {_f(g1.get('brier_base'))}（n {g1['n']}）"
        )
    lines.append("以下只算当前口径：")
    for h in ("4h", "1h"):
        m = rep["desk"][h]
        if not m.get("n"):
            lines.append(f"{h}：还没有可算的轮次")
            continue
        b, ll = m["brier"], m["logloss"]
        ci = b["ci95"]
        lines.append(
            f"{h}（n {m['n']}，实际 涨/平/跌 {m['outcome_freq']['up']:.0%}/"
            f"{m['outcome_freq']['flat']:.0%}/{m['outcome_freq']['down']:.0%}）："
            f"Brier 大盘台 {_f(b['desk'])} / 先验 {_f(b['base'])} / 延续 {_f(b['persist'])}"
            f"（差 {_f(b['desk_minus_base'], '+.3f')}"
            + (f" [{_f(ci[0], '+.3f')}, {_f(ci[1], '+.3f')}]" if ci else "")
            + f"）；log loss {_f(ll['desk'])} / {_f(ll['base'])} / {_f(ll['persist'])}；"
            f"方向命中 {_f(m['direction']['hit'], '.0%')}，非平覆盖 "
            f"{_f(m['direction']['coverage'], '.0%')}、"
            f"精度 {_f(m['direction']['precision'], '.0%')}"
        )
    p = rep["published"]
    p4 = p["4h"]
    lines.append(
        f"发布：{p['publishes']} 次（按规则重放 {p['replay_publishes']} 次）；生效方向 4h 命中 "
        f"{_f(p4['hit'], '.0%')}（n {p4['n']}），1h 命中 {_f(p['1h']['hit'], '.0%')}"
    )
    lp, lc = rep["latency"]["publish"], rep["latency"]["call"]
    lines.append(
        f"延迟：{lp['episodes']} 段行情，发布同向 {lp['caught']}（已生效 {lp['already']}）、没跟上 "
        f"{lp['missed']}、还在走没跟上 {lp['open']}，中位 {_f(lp['median_min'], '.0f')} 分钟；"
        f"单轮叫出中位 {_f(lc['median_min'], '.0f')} 分钟"
    )
    fl = rep.get("flash")
    if fl:
        f4, f1 = fl["4h"], fl["1h"]
        lines.append(
            f"Flash 旧口径（{fl['rounds']} 轮）：4h 命中 {_f(f4['hit'], '.0%')}"
            f"（n {f4['n']}，尖锐 Brier "
            f"{_f(f4['brier_sharp'])}），1h 命中 {_f(f1['hit'], '.0%')}（n {f1['n']}）"
        )
    lines.append(
        "门槛扫描（叫方向 4h 命中 / 覆盖 / 精度 · 1h 命中 · 发布生效 4h 命中 · 发布次数 · "
        "发布延迟中位分钟 / 没跟上）："
    )
    for r in rep["sweep"]:
        cl, c1, pb = r["call_4h"], r["call_1h"], r["published_4h"]
        lines.append(
            f"  θ {r['theta']:.2f} δ {r['delta']:.2f} 确认 {r['confirm']}："
            f"{_f(cl['hit'], '.0%')} / {_f(cl['coverage'], '.0%')} / "
            f"{_f(cl['precision'], '.0%')} · {_f(c1['hit'], '.0%')} · {_f(pb['hit'], '.0%')} · "
            f"{r['publishes']} · {_f(r['latency_median_min'], '.0f')} / {r['missed']}"
        )
    lines.append(rep["panel"]["text"])
    return lines
