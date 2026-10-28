"""大盘台的数据包（设计 2026-10-28 §2）：只放池级 / 大盘级的数字，每项带来源和 as_of。

原则（§6.5 中立性硬要求 + 「不能有延后信息」）：
- 所有「近 N 分钟 / 近 N 小时」的窗口都是**截止到运行时刻的滚动窗口**：终点价 = 15 分钟线里还在走的
  那一根的现价（拉取时刻），起点价在包含起点的那根 K 线里按时间线性插值；绝不把上一根已收盘的
  1h / 4h K 线当成「近 1 小时 / 近 4 小时」。
- 每项带 as_of；「实时」类字段 as_of 比运行时刻旧过 ``max_input_age_min`` 就不给（写进缺失原因）。
  σ、基础率这类统计量按已收盘 1h 线算（as_of = 最后一根收盘），恐慌贪婪是日值，这两类不按新鲜度拦。
- 只给数字和单位，不给定性标签；资金费给相对基准（每 8 小时 0.01%，按各币结算间隔折算）的偏离，
  多空比给相对自身 7 天历史的 z 值；先验用过去 30 天涨 / 平 / 跌基础率。
- 每个有方向的字段都能「镜像」（收益取负、上涨下跌家数对调……），镜像测试用它量方向偏置。
- 可选来源拿不到就跳过并记下原因，不挡这一轮。

请求都走 ``binance_gate``（和单币会议共用封禁与权重闸）。1h K 线按币缓存并落盘（重启不用重新回填），
只在有新收盘时补；首次每币只拉 200 根，30 天长历史每轮最多补 8 个币。15 分钟线每轮重拉 18 根
（含正在走的一根，权重 1）。池成员用当前池（幸存者口径，记分器另按时点池算）。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import math
import os
import statistics
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import httpx

from jiaoyi.contracts import SymbolPool
from jiaoyi.slow import binance_gate

logger = logging.getLogger(__name__)

FAPI = "https://fapi.binance.com"
FNG_URL = "https://api.alternative.me/fng/"
BTC, ETH = "BTCUSDT", "ETHUSDT"
HOUR_MS = 3_600_000
Q_MS = 900_000
FUNDING_BASE_PER_8H = 0.0001
"""币安默认资金费基准：利率部分每 8 小时 0.01%，其他结算间隔按小时数折算。"""
FUNDING_INFO_TTL = timedelta(hours=24)
FNG_TTL = timedelta(hours=1)
MAX_BARS = 1000
BARS_15M = 18
"""15 分钟线每轮拉 18 根（含正在走的一根）：够 4 小时滚动窗口的起点，权重 1。"""
SHALLOW_BARS = 200
"""1h 线首次只拉 200 根（权重 2），够算 σ（168 个窗口）和 12h–72h 窗口的起点。"""
DEEP_PER_RUN = 8
"""每轮最多给几个币补全 30 天以上的 1h 历史（limit≈900，权重 5）：启动时分几轮补完，
不一口气打几十个大请求（防 418）。补过的落盘，重启不用重来。"""
CACHE_KEEP_DAYS = 7
"""1h 线缓存只留当前池、BTC / ETH 和最近这么多天在池里出现过的币，其余的删掉（缓存不无限长）。"""
DEEP_MIN_SHARE = 0.8
"""等权池里补全历史的币占到这个比例才给基础率（先验），之前写「回填中」。"""
PERIOD_5M = timedelta(minutes=5)
"""futures/data 的 5 分钟统计每 5 分钟出一条：新鲜度检查对它多给一段的余量。"""

Mirror = Literal["neg", "comp100", "inv", "same", "pair"]
Kind = Literal["live", "stat", "daily"]
Getter = Callable[..., Any]
"""``get(url, params) -> httpx.Response``；默认 ``binance_gate.get``（共用限流），测试换替身。"""


@dataclass(frozen=True)
class DossierField:
    """数据包里的一个数。``mirror`` 是镜像时怎么变：neg 取负、comp100 变成 100 − x、
    inv 取倒数、same 不变、pair 和 ``partner`` 互换值（上涨家数 ↔ 下跌家数这种）。
    ``source_ts`` 是这个数的 as_of；``kind``：live = 按新鲜度拦，stat = 已收盘线上的统计量，
    daily = 日值。``note_alt`` 是两半（正 / 负）顺序对调的说明，按轮次交替用。"""

    name: str
    value: float
    unit: str
    note: str
    source: str
    source_ts: datetime
    mirror: Mirror = "same"
    partner: str | None = None
    digits: int = 2
    kind: Kind = "live"
    note_alt: str | None = None

    def shown(self) -> str:
        return f"{self.value:.{self.digits}f}"

    def note_for(self, order: str) -> str:
        return self.note_alt if order == "down_first" and self.note_alt else self.note

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "shown": self.shown(),
            "unit": self.unit,
            "source": self.source,
            "as_of": self.source_ts.isoformat(),
            "kind": self.kind,
            "mirror": self.mirror,
            "partner": self.partner,
        }


@dataclass(frozen=True)
class Dossier:
    t0: datetime
    fields: tuple[DossierField, ...]
    missing: dict[str, str] = field(default_factory=dict)
    symbols: tuple[str, ...] = ()
    pool_id: str = ""
    pool_ts: datetime | None = None
    sigma_4h_pct: float | None = None
    mirrored: bool = False
    method: dict[str, Any] = field(default_factory=dict)

    def get(self, name: str) -> DossierField | None:
        return next((f for f in self.fields if f.name == name), None)

    def value(self, name: str) -> float | None:
        f = self.get(name)
        return f.value if f is not None else None

    @property
    def has_prior(self) -> bool:
        return self.value("base_rate_up_4h") is not None

    def mirror(self) -> Dossier:
        """涨跌全部取反的同一份数据（镜像测试用）。非方向性的数（波动、持仓量变化、强平总额）不变。"""
        values = {f.name: f.value for f in self.fields}
        out: list[DossierField] = []
        for f in self.fields:
            if f.mirror == "neg":
                v = -f.value
            elif f.mirror == "comp100":
                v = 100.0 - f.value
            elif f.mirror == "inv":
                v = 1.0 / f.value if f.value else f.value
            elif f.mirror == "pair" and f.partner is not None and f.partner in values:
                v = values[f.partner]
            else:
                v = f.value
            out.append(replace(f, value=v + 0.0))  # +0.0：-0.0 显示成 0.00
        return replace(self, fields=tuple(out), mirrored=not self.mirrored)

    def to_json(self) -> dict[str, Any]:
        return {
            "t0": self.t0.isoformat(),
            "mirrored": self.mirrored,
            "pool_id": self.pool_id,
            "pool_ts": self.pool_ts.isoformat() if self.pool_ts else None,
            "symbols": list(self.symbols),
            "fields": [f.to_json() for f in self.fields],
            "missing": dict(self.missing),
            "method": self.method,
        }

    def sha256(self) -> str:
        blob = json.dumps(self.to_json(), sort_keys=True, ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()


# ------------------------------------------------------------------ 纯计算


@dataclass(frozen=True)
class Bar:
    open_ms: int
    open: float
    high: float
    low: float
    close: float
    quote_vol: float
    taker_quote_vol: float

    def row(self) -> list[float]:
        return [
            self.open_ms,
            self.open,
            self.high,
            self.low,
            self.close,
            self.quote_vol,
            self.taker_quote_vol,
        ]


def _bar(r: Sequence[Any]) -> Bar:
    return Bar(
        int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[7]), float(r[10])
    )


def parse_bars(rows: Sequence[Sequence[Any]], now_ms: int) -> list[Bar]:
    """``/fapi/v1/klines`` 原始行 → 已收盘的线（收盘时间晚于现在的丢掉）。"""
    out: list[Bar] = []
    for r in rows:
        try:
            if int(r[6]) >= now_ms:
                continue
            out.append(_bar(r))
        except (IndexError, TypeError, ValueError):
            continue
    return out


@dataclass(frozen=True)
class Live:
    """一个币截止到拉取时刻的 15 分钟线：已收盘的几根 + 正在走的一根（close = 现价）。"""

    bars: tuple[Bar, ...]
    now_ms: int
    price: float

    @property
    def as_of(self) -> datetime:
        return datetime.fromtimestamp(self.now_ms / 1000, UTC)


def parse_live(rows: Sequence[Sequence[Any]], now_ms: int) -> Live | None:
    """15 分钟线（含正在走的一根）→ ``Live``；最后一根必须是还没收盘的那根，不然现价不是现在的。"""
    bars: list[Bar] = []
    for r in rows:
        with contextlib.suppress(IndexError, TypeError, ValueError):
            bars.append(_bar(r))
    if not bars:
        return None
    last = bars[-1]
    if not last.open_ms <= now_ms < last.open_ms + Q_MS or last.close <= 0:
        return None
    return Live(tuple(bars), now_ms, last.close)


def interp_price(
    bars: Sequence[Bar], t_ms: int, step: int, now_ms: int, now_px: float
) -> float | None:
    """时刻 t 的价：在包含 t 的那根线里，开盘价到收盘价按时间线性插值（正在走的那根，收盘价是
    现价、终点是拉取时刻）。t 不在任何一根里返回 None，不拿邻近的补。"""
    if t_ms >= now_ms:
        return now_px
    for b in bars:
        if b.open_ms <= t_ms < b.open_ms + step:
            end_ms, end_px = b.open_ms + step, b.close
            if end_ms > now_ms:
                end_ms, end_px = now_ms, now_px
            if end_ms <= b.open_ms or b.open <= 0:
                return None
            frac = (t_ms - b.open_ms) / (end_ms - b.open_ms)
            return b.open + (end_px - b.open) * frac
    return None


def price_at(bars: Mapping[int, Bar], t_ms: int, step: int = HOUR_MS) -> float | None:
    """整点时刻 t 的价 = 开盘在 t − step 的那根已收盘线的收盘价（只给 σ / 基础率这类统计量用）。"""
    b = bars.get(t_ms - step)
    return b.close if b is not None and b.close > 0 else None


def pool_return(
    series: Mapping[str, Mapping[int, Bar]], start_ms: int, end_ms: int, step: int = HOUR_MS
) -> tuple[float | None, list[float]]:
    """等权池在两个整点之间的简单收益（%）= 两端都有价的币收益的平均；同时返回各币收益。"""
    rets: list[float] = []
    for bars in series.values():
        a, b = price_at(bars, start_ms, step), price_at(bars, end_ms, step)
        if a is not None and b is not None:
            rets.append((b / a - 1.0) * 100.0)
    return (statistics.fmean(rets) if rets else None), rets


def sigma_at(
    series: Mapping[str, Mapping[int, Bar]], t_ms: int, horizon_h: int, lookback: int
) -> float | None:
    """σ_h(t)：结束于 t, t−1h, …, t−(lookback−1)h 的 ``lookback`` 个滚动 h 小时窗口等权收益的 RMS。
    只用 t 之前已完成的数据。有效窗口少于一半返回 None。"""
    vals: list[float] = []
    for k in range(lookback):
        end = t_ms - k * HOUR_MS
        r, _ = pool_return(series, end - horizon_h * HOUR_MS, end)
        if r is not None:
            vals.append(r)
    if len(vals) < lookback // 2:
        return None
    return math.sqrt(statistics.fmean(v * v for v in vals))


def classify(ret: float, sigma: float, band: float) -> Literal["up", "flat", "down"]:
    if ret > band * sigma:
        return "up"
    if ret < -band * sigma:
        return "down"
    return "flat"


def base_rates(
    series: Mapping[str, Mapping[int, Bar]],
    t0_ms: int,
    horizon_h: int,
    *,
    days: int,
    band: float,
    lookback: int,
) -> dict[str, float] | None:
    """过去 ``days`` 天里（结局在 t0 前已知的起点），等权池 h 小时收益落在涨 / 平 / 跌的频率，
    拉普拉斯平滑（各加 1）。σ 用每个起点自己之前的窗口算（按时点，无前视）。"""
    counts = {"up": 0, "flat": 0, "down": 0}
    starts = range(t0_ms - days * 24 * HOUR_MS, t0_ms - horizon_h * HOUR_MS + 1, HOUR_MS)
    rets: dict[int, float] = {}
    lo = starts.start - (lookback - 1) * HOUR_MS
    for end in range(lo, t0_ms + 1, HOUR_MS):
        r, _ = pool_return(series, end - horizon_h * HOUR_MS, end)
        if r is not None:
            rets[end] = r
    for t in starts:
        window = [rets[t - k * HOUR_MS] for k in range(lookback) if t - k * HOUR_MS in rets]
        outcome = rets.get(t + horizon_h * HOUR_MS)
        if len(window) < lookback // 2 or outcome is None:
            continue
        sigma = math.sqrt(statistics.fmean(v * v for v in window))
        if sigma <= 0:
            continue
        counts[classify(outcome, sigma, band)] += 1
    n = sum(counts.values())
    if n < days * 24 // 4:  # 不到四分之一的起点可用：数据太少，不给先验
        return None
    return {k: (v + 1) / (n + 3) for k, v in counts.items()} | {"n": float(n)}


def zscore(values: Sequence[float]) -> float | None:
    if len(values) < 24:
        return None
    hist, last = values[:-1], values[-1]
    sd = statistics.pstdev(hist)
    if sd <= 0:
        return None
    return (last - statistics.fmean(hist)) / sd


def _halves(intro: str, up: str, down: str) -> tuple[str, str]:
    """「正 = 一边，负 = 另一边」两种顺序的说明（按轮次交替，中立）。"""
    return f"{intro}; {up}; {down}.", f"{intro}; {down}; {up}."


# ------------------------------------------------------------------ 采集


class DossierBuilder:
    """每轮组装一次数据包。同步请求（``binance_gate.get``）放在线程里跑。"""

    def __init__(
        self,
        *,
        band_sigma: float = 0.5,
        sigma_lookback_h: int = 168,
        base_rate_days: int = 30,
        exclude_symbols: Sequence[str] = (),
        max_input_age_min: float = 5.0,
        cache_path: Path | None = None,
        get: Getter | None = None,
        fng_get: Getter | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """``monotonic``：量开跑以来过了多久（请求的 as_of = 运行时刻 + 这段时间），测试可换。"""
        self._band = band_sigma
        self._lookback = sigma_lookback_h
        self._days = base_rate_days
        self._exclude = frozenset(exclude_symbols)
        self._max_age = max_input_age_min
        self._cache_path = cache_path
        self._get: Getter = get or binance_gate.get
        self._fng_get: Getter = fng_get or _plain_get
        self._bars: dict[str, dict[int, Bar]] = {}
        self._live: dict[str, Live] = {}
        self._deep: set[str] = set()
        self._funding_hours: dict[str, float] = {}
        self._funding_info_at: datetime | None = None
        self._fng: tuple[datetime, float, datetime] | None = None  # (取到时间, 值, 源时间)
        self._seen: dict[str, int] = {}
        """币 → 最后一次在池里（或是 BTC / ETH）的时刻（毫秒），缓存修剪用。"""
        self._run_at = datetime.fromtimestamp(0, UTC)
        self._monotonic = monotonic
        self._run_mono = monotonic()
        self._load_cache()

    @property
    def need_bars(self) -> int:
        return min(self._days * 24 + self._lookback + 8, MAX_BARS)

    def symbols(self, pool: SymbolPool) -> list[str]:
        return sorted(e.symbol for e in pool.in_pool if e.symbol not in self._exclude)

    def build(self, pool: SymbolPool, now: datetime) -> Dossier:
        """组装数据包（同步，可能要几秒到十几秒；调用方放线程里）。封禁直接抛 ``RateLimited``。"""
        missing: dict[str, str] = {}
        fields: list[DossierField] = []
        syms = self.symbols(pool)
        self._run_at, self._run_mono = now, self._monotonic()
        now_ms = int(now.timestamp() * 1000)
        t0_ms = now_ms // HOUR_MS * HOUR_MS  # 最后一根已收盘 1h 线的收盘整点（统计量用）
        self._prune_cache([*syms, BTC, ETH], now_ms)
        self._refresh_bars([*syms, BTC, ETH], now, missing)
        self._refresh_live([*syms, BTC, ETH], now, missing)
        basket = {s: self._bars[s] for s in syms if self._bars.get(s)}
        live = {s: self._live[s] for s in syms if s in self._live}
        sigma4: float | None = None
        if len(basket) < 5:
            missing["klines_1h"] = f"等权池有 1h 历史的币只有 {len(basket)} 个（要 ≥5）"
        else:
            sigma4 = self._stat_fields(basket, t0_ms, fields, missing)
        if len(live) < 5:
            missing["klines_15m"] = f"等权池有现价的币只有 {len(live)} 个（要 ≥5）"
        else:
            self._window_fields(live, sigma4, fields, missing)
        self._major_fields(syms, fields, missing)
        self._funding_fields(syms, now, fields, missing)
        for sym, tag in ((BTC, "btc"), (ETH, "eth")):
            self._crowd_fields(sym, tag, fields, missing)
            self._oi_fields(sym, tag, fields, missing)
        self._fng_fields(now, fields, missing)
        nxt = _next_settlement(now)
        fields.append(
            DossierField(
                "minutes_to_next_funding_settlement",
                (nxt - now).total_seconds() / 60,
                "min",
                "Minutes until the next 00/08/16 UTC funding settlement.",
                "calendar",
                now,
                digits=0,
            )
        )
        missing.setdefault(
            "pool_metrics",
            "选币时的池统计（持仓量 1h、量比、强平）是选币时刻 / 已收盘小时的值，不用",
        )
        missing.setdefault("news", "第一阶段不接新闻（不可复现、有注入风险）")
        fields = self._drop_stale(fields, self._fetched_at(), missing)
        return Dossier(
            t0=now,
            fields=tuple(fields),
            missing=missing,
            symbols=tuple(sorted(live or basket)),
            pool_id=pool.pool_id,
            pool_ts=pool.ts,
            sigma_4h_pct=sigma4,
            method={
                "basket": "equal-weight simple return of current pool members (survivor basis)",
                "windows": "rolling, ending at the run time: end price = current price of the "
                "forming 15m bar; start price interpolated inside the bar containing the start",
                "sigma": f"RMS of {self._lookback} rolling 1h-aligned windows of closed 1h bars",
                "band_sigma": self._band,
                "base_rate_days": self._days,
                "base_rate_smoothing": "laplace +1",
                "funding_baseline": "0.01% per 8h scaled to each symbol's interval",
                "max_input_age_min": self._max_age,
                "excluded": sorted(self._exclude),
            },
        )

    def _fetched_at(self) -> datetime:
        """此刻（运行时刻 + 开跑以来真实流逝的时间）：每个请求取回来的时刻就是它的 as_of。"""
        return self._run_at + timedelta(seconds=self._monotonic() - self._run_mono)

    def _drop_stale(
        self, fields: list[DossierField], now: datetime, missing: dict[str, str]
    ) -> list[DossierField]:
        """实时类字段 as_of 比数据包组好的时刻（``now``）旧过 ``max_input_age_min`` 就不给（5 分钟
        统计多给一段）。请求慢、限流等太久时，先取到的数会在这里被拦下。"""
        if self._max_age <= 0:
            return fields
        out: list[DossierField] = []
        for f in fields:
            limit = timedelta(minutes=self._max_age)
            if f.source.startswith("futures_data"):
                limit += PERIOD_5M
            age = now - f.source_ts
            if f.kind == "live" and age > limit:
                missing[f.name] = (
                    f"as_of {f.source_ts:%H:%M:%S}Z 比运行时刻旧 "
                    f"{age.total_seconds() / 60:.1f} 分钟（上限 {limit.total_seconds() / 60:g}）"
                )
                continue
            out.append(f)
        return out

    # ---------------------------------------------------------- K 线
    def _fetch(self, sym: str, interval: str, limit: int) -> Any:
        r = self._get(
            f"{FAPI}/fapi/v1/klines", {"symbol": sym, "interval": interval, "limit": limit}
        )
        r.raise_for_status()
        return r.json()

    def _merge(self, sym: str, rows: Any, now_ms: int) -> int:
        merged = dict(self._bars.get(sym, {}))
        for b in parse_bars(rows, now_ms):
            merged[b.open_ms] = b
        kept = sorted(merged)[-MAX_BARS:]
        self._bars[sym] = {k: merged[k] for k in kept}
        return len(kept)

    def _refresh_bars(self, symbols: Sequence[str], now: datetime, missing: dict[str, str]) -> None:
        """1h 线：有新收盘才补（只补新的几根）；没缓存的先拉 ``SHALLOW_BARS`` 根；再给最多
        ``DEEP_PER_RUN`` 个还没补全的币拉满 30 天以上历史（基础率要），分几轮补完。有变化就落盘。"""
        now_ms = int(now.timestamp() * 1000)
        failed: list[str] = []
        changed = False
        for sym in dict.fromkeys(symbols):
            have = self._bars.get(sym, {})
            if have:
                last = max(have)
                if now_ms < last + 2 * HOUR_MS:  # 下一根还没收盘：不请求
                    continue
                limit = int(min((now_ms - last) // HOUR_MS + 2, self.need_bars))
            else:
                limit = min(SHALLOW_BARS, self.need_bars)
            try:
                n = self._merge(sym, self._fetch(sym, "1h", limit), now_ms)
            except binance_gate.RateLimited:
                raise
            except Exception as exc:
                failed.append(f"{sym}:{type(exc).__name__}")
                continue
            changed = True
            if n >= self.need_bars - 2:
                self._deep.add(sym)
        todo = [s for s in dict.fromkeys(symbols) if s in self._bars and s not in self._deep]
        for sym in todo[:DEEP_PER_RUN]:
            try:
                self._merge(sym, self._fetch(sym, "1h", self.need_bars), now_ms)
            except binance_gate.RateLimited:
                raise
            except Exception as exc:
                failed.append(f"{sym}:deep:{type(exc).__name__}")
                continue
            self._deep.add(sym)  # 新上线不满 30 天的币：拉过一次全量就算补全
            changed = True
        if failed:
            missing["klines_1h_partial"] = "部分币 1h 线没取到：" + ", ".join(failed[:10])
        if changed:
            self._save_cache()

    def _refresh_live(self, symbols: Sequence[str], now: datetime, missing: dict[str, str]) -> None:
        """15 分钟线每轮重拉（含正在走的一根）：现价和滚动窗口的起点都从这里来。"""
        self._live = {}
        failed: list[str] = []
        for sym in dict.fromkeys(symbols):
            try:
                rows = self._fetch(sym, "15m", BARS_15M)
            except binance_gate.RateLimited:
                raise
            except Exception as exc:
                failed.append(f"{sym}:{type(exc).__name__}")
                continue
            # 现价的 as_of = 这个请求取回来的时刻（不是开跑时刻），新鲜度检查才拦得住慢请求
            live = parse_live(rows, int(self._fetched_at().timestamp() * 1000))
            if live is None:
                failed.append(f"{sym}:no_forming_bar")
                continue
            self._live[sym] = live
        if failed:
            missing["klines_15m_partial"] = "部分币 15 分钟线没取到：" + ", ".join(failed[:10])

    def _ret(self, sym: str, minutes: int) -> float | None:
        """截止到拉取时刻的滚动 ``minutes`` 分钟收益（%）。
        4 小时内用 15 分钟线找起点，更长用 1h 线。"""
        live = self._live.get(sym)
        if live is None:
            return None
        start = live.now_ms - minutes * 60_000
        if minutes <= 240:
            px = interp_price(live.bars, start, Q_MS, live.now_ms, live.price)
        else:
            bars = sorted(self._bars.get(sym, {}).values(), key=lambda b: b.open_ms)
            px = interp_price(bars, start, HOUR_MS, live.now_ms, live.price)
        return (live.price / px - 1.0) * 100.0 if px else None

    def _window_fields(
        self,
        live: Mapping[str, Live],
        sigma4: float | None,
        out: list[DossierField],
        missing: dict[str, str],
    ) -> None:
        as_of = min(v.as_of for v in live.values())

        def add(name: str, value: float | None, unit: str, note: str, **kw: Any) -> None:
            if value is None or not math.isfinite(value):
                missing[name] = "数据不足"
                return
            out.append(DossierField(name, value, unit, note, "klines_15m_live", as_of, **kw))

        windows = ((15, "15m"), (30, "30m"), (60, "1h"), (240, "4h"), (720, "12h"))
        for minutes, tag in (*windows, (1440, "24h"), (4320, "72h")):
            rets = [r for s in live if (r := self._ret(s, minutes)) is not None]
            add(
                f"pool_ret_{tag}_pct",
                statistics.fmean(rets) if len(rets) >= 5 else None,
                "%",
                f"Equal-weight basket return over the {tag} ending at as_of (rolling).",
                mirror="neg",
                digits=3,
            )
            if tag in ("15m", "1h", "4h", "24h") and len(rets) >= 5:
                add(
                    f"pool_up_count_{tag}",
                    float(sum(x > 0 for x in rets)),
                    "count",
                    f"Basket symbols with a rolling {tag} return above 0.",
                    mirror="pair",
                    partner=f"pool_down_count_{tag}",
                    digits=0,
                )
                add(
                    f"pool_down_count_{tag}",
                    float(sum(x < 0 for x in rets)),
                    "count",
                    f"Basket symbols with a rolling {tag} return below 0.",
                    mirror="pair",
                    partner=f"pool_up_count_{tag}",
                    digits=0,
                )
            if tag == "4h" and len(rets) >= 5:
                add(
                    "pool_dispersion_4h_pct",
                    statistics.pstdev(rets),
                    "%",
                    "Cross-sectional standard deviation of rolling 4h returns across the basket.",
                    digits=3,
                )
                if sigma4:
                    add(
                        "pool_ret_4h_in_sigma",
                        statistics.fmean(rets) / sigma4,
                        "sigma",
                        "Rolling 4h basket return divided by sigma_4h_pct.",
                        mirror="neg",
                    )
        for minutes, tag in ((60, "1h"), (240, "4h")):
            qv = tb = 0.0
            for v in live.values():
                for b in v.bars:
                    if b.open_ms + Q_MS > v.now_ms - minutes * 60_000:
                        qv, tb = qv + b.quote_vol, tb + b.taker_quote_vol
            if qv <= 0:
                missing[f"taker_{tag}"] = "数据不足"
                continue
            note = "of basket quote volume in the 15m bars overlapping the {} ending at as_of."
            add(
                f"pool_taker_buy_share_{tag}",
                tb / qv * 100.0,
                "%",
                "Taker buy share " + note.format(tag),
                mirror="pair",
                partner=f"pool_taker_sell_share_{tag}",
            )
            add(
                f"pool_taker_sell_share_{tag}",
                100.0 - tb / qv * 100.0,
                "%",
                "Taker sell share " + note.format(tag),
                mirror="pair",
                partner=f"pool_taker_buy_share_{tag}",
            )
        pos: list[float] = []
        for sym, v in live.items():
            start = v.now_ms - 24 * HOUR_MS
            # 含起点所在的那根 1h 线：区间覆盖完整 24 小时（最多多出不到 1 小时）
            hist = [b for b in self._bars.get(sym, {}).values() if b.open_ms + HOUR_MS > start]
            if len(hist) < 20:
                continue
            covered = max(b.open_ms for b in hist) + HOUR_MS  # 已收盘 1h 线覆盖到哪
            got = [*hist, *(b for b in v.bars if b.open_ms + Q_MS > covered)]
            hi, lo = max(b.high for b in got), min(b.low for b in got)
            if hi > lo:
                pos.append((v.price - lo) / (hi - lo) * 100.0)
        add(
            "pool_range_position_24h_median_pct",
            statistics.median(pos) if pos else None,
            "%",
            "Median position of the current price inside each symbol's low-high range over at "
            "least the last 24h (from the 1h bar containing the start; 0 = at the low, 100 = at "
            "the high).",
            mirror="comp100",
            digits=1,
        )

    def _stat_fields(
        self,
        basket: Mapping[str, Mapping[int, Bar]],
        t0_ms: int,
        out: list[DossierField],
        missing: dict[str, str],
    ) -> float | None:
        """σ 和基础率：已收盘 1h 线上的统计量（as_of = 最后一根收盘），不按新鲜度拦。"""
        t0 = datetime.fromtimestamp(t0_ms / 1000, UTC)

        def add(name: str, value: float | None, unit: str, note: str, **kw: Any) -> None:
            if value is None or not math.isfinite(value):
                missing[name] = "数据不足"
                return
            out.append(DossierField(name, value, unit, note, "klines_1h", t0, kind="stat", **kw))

        sigma4 = sigma_at(basket, t0_ms, 4, self._lookback)
        sigma1 = sigma_at(basket, t0_ms, 1, self._lookback)
        add("sigma_4h_pct", sigma4, "%", "Sigma of the 4h basket return (RMS).", digits=3)
        add("sigma_1h_pct", sigma1, "%", "Sigma of the 1h basket return (RMS).", digits=3)
        deep = self.deep_basket(basket)
        for h, sigma in ((4, sigma4), (1, sigma1)):
            if deep is None:
                done = sum(1 for s in basket if s in self._deep)
                missing[f"base_rate_{h}h"] = (
                    f"长历史回填中 {done}/{len(basket)}（每轮补 {DEEP_PER_RUN} 个），暂无先验"
                )
                continue
            rates = base_rates(
                deep, t0_ms, h, days=self._days, band=self._band, lookback=self._lookback
            )
            if rates is None or sigma is None:
                missing[f"base_rate_{h}h"] = "历史不足，没有先验"
                continue
            add(
                f"base_rate_up_{h}h",
                rates["up"],
                "share",
                f"Share of past {h}h windows that ended UP.",
                mirror="pair",
                partner=f"base_rate_down_{h}h",
                digits=3,
            )
            add(
                f"base_rate_flat_{h}h",
                rates["flat"],
                "share",
                f"Share of past {h}h windows that ended FLAT.",
                digits=3,
            )
            add(
                f"base_rate_down_{h}h",
                rates["down"],
                "share",
                f"Share of past {h}h windows that ended DOWN.",
                mirror="pair",
                partner=f"base_rate_up_{h}h",
                digits=3,
            )
            add(
                f"base_rate_windows_{h}h",
                rates["n"],
                "count",
                f"Number of past {h}h windows behind the base rate.",
                digits=0,
            )
        return sigma4

    def btc_sigma_1h(self, now: datetime) -> float | None:
        """BTC 自己的 1h σ，和等权池的 ``sigma_1h_pct`` 同一方法、同一批已缓存的 1h 线（不发请求）。
        不进数据包，只给大盘台 event 模式判断「走了几个 σ」。"""
        bars = self._bars.get(BTC)
        if not bars:
            return None
        t0_ms = int(now.timestamp() * 1000) // HOUR_MS * HOUR_MS
        return sigma_at({BTC: bars}, t0_ms, 1, self._lookback)

    def deep_basket(
        self, basket: Mapping[str, Mapping[int, Bar]]
    ) -> dict[str, Mapping[int, Bar]] | None:
        """补全了长历史的那部分等权池；占比不到 ``DEEP_MIN_SHARE`` 返回 None（先验还在回填）。"""
        deep = {s: b for s, b in basket.items() if s in self._deep}
        return deep if basket and len(deep) >= DEEP_MIN_SHARE * len(basket) else None

    def _major_fields(
        self, syms: Sequence[str], out: list[DossierField], missing: dict[str, str]
    ) -> None:
        btc, eth = self._live.get(BTC), self._live.get(ETH)
        r = self._ret
        rows: list[tuple[str, float | None, str]] = [
            ("btc_ret_15m_pct", r(BTC, 15), "BTC return over the 15m ending at as_of."),
            ("btc_ret_1h_pct", r(BTC, 60), "BTC return over the 1h ending at as_of."),
            ("btc_ret_4h_pct", r(BTC, 240), "BTC return over the 4h ending at as_of."),
            ("btc_ret_24h_pct", r(BTC, 1440), "BTC return over the 24h ending at as_of."),
            ("eth_ret_4h_pct", r(ETH, 240), "ETH return over the 4h ending at as_of."),
            ("eth_ret_24h_pct", r(ETH, 1440), "ETH return over the 24h ending at as_of."),
        ]
        e24, b24 = r(ETH, 1440), r(BTC, 1440)
        if e24 is not None and b24 is not None:
            ratio = ((1 + e24 / 100) / (1 + b24 / 100) - 1) * 100
            rows.append(("eth_btc_ratio_chg_24h_pct", ratio, "ETH/BTC ratio change over 24h."))
        pool24 = [x for s in syms if (x := r(s, 1440)) is not None]  # 只用当前池成员
        if b24 is not None and len(pool24) >= 5:
            rows.append(
                (
                    "btc_minus_pool_ret_24h_pct",
                    b24 - statistics.fmean(pool24),
                    "BTC 24h return minus the basket 24h return (current pool members).",
                )
            )
        stamps = [v.as_of for v in (btc, eth) if v is not None]
        as_of = min(stamps) if stamps else None
        for name, value, note in rows:
            if value is None or as_of is None:
                missing[name] = "BTC / ETH 现价或历史不足"
                continue
            out.append(
                DossierField(name, value, "%", note, "klines_15m_live", as_of, "neg", digits=3)
            )

    # ---------------------------------------------------------- 资金费
    def _funding_intervals(self, now: datetime, missing: dict[str, str]) -> None:
        if self._funding_info_at is not None and now - self._funding_info_at < FUNDING_INFO_TTL:
            return
        try:
            r = self._get(f"{FAPI}/fapi/v1/fundingInfo", None)
            r.raise_for_status()
            hours = {}
            for row in r.json():
                h = row.get("fundingIntervalHours")
                if isinstance(h, int | float) and h > 0:
                    hours[str(row.get("symbol"))] = float(h)
            self._funding_hours, self._funding_info_at = hours, now
        except binance_gate.RateLimited:
            raise
        except Exception as exc:
            missing["funding_interval"] = f"结算间隔没取到（按 8 小时算）：{type(exc).__name__}"

    def _funding_fields(
        self, syms: Sequence[str], now: datetime, out: list[DossierField], missing: dict[str, str]
    ) -> None:
        self._funding_intervals(now, missing)
        try:
            r = self._get(f"{FAPI}/fapi/v1/premiumIndex", None)
            r.raise_for_status()
            rows = {str(p["symbol"]): p for p in r.json()}
        except binance_gate.RateLimited:
            raise
        except Exception as exc:
            missing["funding"] = f"premiumIndex 没取到：{type(exc).__name__}"
            return
        excess: dict[str, float] = {}
        basis: list[float] = []
        ts_ms = 0
        for sym in {*syms, BTC, ETH}:
            p = rows.get(sym)
            if p is None:
                continue
            try:
                rate = float(p["lastFundingRate"])
                hours = self._funding_hours.get(sym, 8.0)
                # 折成每 8 小时、减去按间隔折算的基准，单位基点
                excess[sym] = (rate * 8.0 / hours - FUNDING_BASE_PER_8H) * 1e4
                mark, index = float(p["markPrice"]), float(p["indexPrice"])
                if sym in syms and index > 0:
                    basis.append((mark / index - 1.0) * 1e4)
                ts_ms = max(ts_ms, int(p.get("time") or 0))
            except (KeyError, TypeError, ValueError, ZeroDivisionError):
                continue
        ts = datetime.fromtimestamp(ts_ms / 1000, UTC) if ts_ms else now
        pool_ex = [excess[s] for s in syms if s in excess]
        if not pool_ex:
            missing["funding"] = "池内没有资金费数据"
            return
        n = len(pool_ex)
        note, alt = _halves(
            "Median funding minus the 0.01%/8h baseline, in basis points per 8h",
            "positive = longs pay shorts more than baseline",
            "negative = shorts pay longs more than baseline",
        )
        out.append(
            DossierField(
                "funding_excess_median_bp8h",
                statistics.median(pool_ex),
                "bp/8h",
                note,
                "premiumIndex",
                ts,
                "neg",
                digits=3,
                note_alt=alt,
            )
        )
        out.append(
            DossierField(
                "funding_share_above_base_pct",
                sum(x > 0.005 for x in pool_ex) / n * 100.0,
                "%",
                "Share of basket symbols with funding above the baseline.",
                "premiumIndex",
                ts,
                "pair",
                "funding_share_below_base_pct",
                digits=1,
            )
        )
        out.append(
            DossierField(
                "funding_share_below_base_pct",
                sum(x < -0.005 for x in pool_ex) / n * 100.0,
                "%",
                "Share of basket symbols with funding below the baseline.",
                "premiumIndex",
                ts,
                "pair",
                "funding_share_above_base_pct",
                digits=1,
            )
        )
        if basis:
            out.append(
                DossierField(
                    "mark_index_premium_median_bp",
                    statistics.median(basis),
                    "bp",
                    "Median of mark price over index price minus 1, in basis points.",
                    "premiumIndex",
                    ts,
                    "neg",
                    digits=2,
                )
            )
        for sym, tag in ((BTC, "btc"), (ETH, "eth")):
            if sym in excess:
                out.append(
                    DossierField(
                        f"{tag}_funding_excess_bp8h",
                        excess[sym],
                        "bp/8h",
                        f"{tag.upper()} funding minus the baseline, basis points per 8h.",
                        "premiumIndex",
                        ts,
                        "neg",
                        digits=3,
                    )
                )

    # ---------------------------------------------------------- 多空账户 / 持仓量（BTC、ETH）
    def _series(self, path: str, sym: str, period: str, limit: int) -> Any:
        r = self._get(
            f"{FAPI}/futures/data/{path}", {"symbol": sym, "period": period, "limit": limit}
        )
        r.raise_for_status()
        rows = r.json()
        if not rows:
            raise ValueError("empty")
        return rows

    def _crowd_fields(
        self, sym: str, tag: str, out: list[DossierField], missing: dict[str, str]
    ) -> None:
        """最新值取 5 分钟统计（as_of = 时间戳，即段尾）；z 值相对自身 7 天（1h 统计）。"""
        name = f"{tag}_long_short_account"
        try:
            hist = self._series("globalLongShortAccountRatio", sym, "1h", 168)
            last = self._series("globalLongShortAccountRatio", sym, "5m", 1)[-1]
            longs = float(last["longAccount"]) * 100
            shorts = float(last["shortAccount"]) * 100
            ratios = [math.log(float(x["longShortRatio"])) for x in hist]
            ratios.append(math.log(float(last["longShortRatio"])))
            ts = _period_as_of(int(last["timestamp"]), self._fetched_at())
        except binance_gate.RateLimited:
            raise
        except Exception as exc:
            missing[name] = f"多空账户比没取到：{type(exc).__name__}"
            return
        up = tag.upper()
        src = "futures_data:globalLongShortAccountRatio"
        out.append(
            DossierField(
                f"{tag}_long_account_pct",
                longs,
                "%",
                f"{up} share of accounts net long.",
                src,
                ts,
                "pair",
                f"{tag}_short_account_pct",
                digits=1,
            )
        )
        out.append(
            DossierField(
                f"{tag}_short_account_pct",
                shorts,
                "%",
                f"{up} share of accounts net short.",
                src,
                ts,
                "pair",
                f"{tag}_long_account_pct",
                digits=1,
            )
        )
        z = zscore(ratios)
        if z is None:
            missing[f"{tag}_long_short_z_7d"] = "历史不足"
            return
        note, alt = _halves(
            f"{up} log(long/short account ratio) now versus its own last 7 days, in standard "
            "deviations",
            "positive = more long than usual",
            "negative = more short than usual",
        )
        out.append(
            DossierField(
                f"{tag}_long_short_log_ratio_z_7d", z, "z", note, src, ts, "neg", note_alt=alt
            )
        )

    def _oi_fields(
        self, sym: str, tag: str, out: list[DossierField], missing: dict[str, str]
    ) -> None:
        """持仓量：5 分钟统计，截止到最新一段的 4 小时 / 24 小时变化。"""
        try:
            rows = self._series("openInterestHist", sym, "5m", 289)
            vals = [float(x["sumOpenInterestValue"]) for x in rows]
            ts = _period_as_of(int(rows[-1]["timestamp"]), self._fetched_at())
        except binance_gate.RateLimited:
            raise
        except Exception as exc:
            missing[f"{tag}_oi"] = f"持仓量历史没取到：{type(exc).__name__}"
            return
        for h in (4, 24):
            k = h * 12
            if len(vals) > k and vals[-1 - k] > 0:
                out.append(
                    DossierField(
                        f"{tag}_oi_chg_{h}h_pct",
                        (vals[-1] / vals[-1 - k] - 1) * 100,
                        "%",
                        f"{tag.upper()} open-interest value change over the {h}h ending at as_of.",
                        "futures_data:openInterestHist",
                        ts,
                    )
                )

    def _fng_fields(self, now: datetime, out: list[DossierField], missing: dict[str, str]) -> None:
        if self._fng is None or now - self._fng[0] >= FNG_TTL:
            try:
                r = self._fng_get(FNG_URL, {"limit": 1})
                r.raise_for_status()
                row = r.json()["data"][0]
                self._fng = (
                    now,
                    float(row["value"]),
                    datetime.fromtimestamp(int(row["timestamp"]), UTC),
                )
            except Exception as exc:
                missing["fear_greed_index"] = f"没取到：{type(exc).__name__}"
                return
        _, value, ts = self._fng
        out.append(
            DossierField(
                "fear_greed_index",
                value,
                "0-100",
                "alternative.me daily index value only (0 to 100); the text label is withheld.",
                "alternative.me",
                ts,
                "comp100",
                digits=0,
                kind="daily",
            )
        )

    # ---------------------------------------------------------- 1h 缓存落盘
    def _load_cache(self) -> None:
        path = self._cache_path
        if path is None or not path.is_file():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            bars = {
                str(sym): {int(r[0]): Bar(int(r[0]), *map(float, r[1:7])) for r in rows}
                for sym, rows in raw["bars"].items()
            }
            deep = {str(s) for s in raw.get("deep", [])}
            seen = {str(s): int(t) for s, t in (raw.get("seen") or {}).items()}
        except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
            logger.warning("大盘台 1h K 线缓存读不了，重新回填：%s", exc)
            return
        self._bars, self._deep, self._seen = bars, deep & set(bars), seen

    def _prune_cache(self, keep: Sequence[str], now_ms: int) -> None:
        """记下这一轮要的币；最近 ``CACHE_KEEP_DAYS`` 天都没要过的币从缓存删掉（旧缓存没有记录的
        币从这一轮起算）。删了就落盘。"""
        for sym in keep:
            self._seen[sym] = now_ms
        for sym in self._bars:
            self._seen.setdefault(sym, now_ms)
        cutoff = now_ms - CACHE_KEEP_DAYS * 24 * HOUR_MS
        drop = [s for s, t in self._seen.items() if t < cutoff]
        if not drop:
            return
        for sym in drop:
            self._seen.pop(sym, None)
            self._bars.pop(sym, None)
            self._deep.discard(sym)
        self._save_cache()

    def _save_cache(self) -> None:
        path = self._cache_path
        if path is None:
            return
        tmp = path.with_suffix(".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "bars": {
                    s: [b.row() for _, b in sorted(bars.items())] for s, bars in self._bars.items()
                },
                "deep": sorted(self._deep),
                "seen": dict(sorted(self._seen.items())),
            }
            tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, path)
        except Exception:
            logger.exception("大盘台 1h K 线缓存存盘失败（重启后要重新回填）")
            with contextlib.suppress(OSError):
                tmp.unlink()


def _plain_get(url: str, params: Mapping[str, Any] | None = None) -> httpx.Response:
    started = time.monotonic()
    r = httpx.get(url, params=dict(params or {}), timeout=10.0)
    logger.debug("GET %s %.0fms", url, (time.monotonic() - started) * 1000)
    return r


def _period_as_of(ts_ms: int, fetched: datetime) -> datetime:
    """futures/data 5 分钟统计的 as_of：时间戳本身就是这一段的结束（快照时刻），不晚于取到的时刻。

    实测（2026-10-28）：02:49:01 取到的最新一条是 02:45，02:51:54 取到的是 02:50——每条在时间戳
    之后一两分钟就有了，不可能是一段的开始；以前按开始 + 5 分钟算，as_of 会落在未来。"""
    return min(datetime.fromtimestamp(ts_ms / 1000, UTC), fetched)


def _next_settlement(now: datetime) -> datetime:
    base = now.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    for k in range(1, 10):
        t = base + timedelta(hours=k)
        if t.hour % 8 == 0 and t > now:
            return t
    return base + timedelta(hours=8)
