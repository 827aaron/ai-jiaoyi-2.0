"""大盘台的数据包（设计 2026-10-28 §2）：只放池级 / 大盘级的数字，每项带来源和源时间。

原则（§6.5 中立性硬要求）：
- 只给数字和单位，不给「拥挤」「恐慌」这类定性标签；资金费给相对基准（币安默认每 8 小时 0.01%，
  按各币结算间隔折算）的偏离，多空比给相对自身 7 天历史的 z 值；恐慌贪婪只给数值。
- 先验用基础率：过去 30 天等权池 4h / 1h 涨 / 平 / 跌频率（「平」= |收益| ≤ band × σ，
  σ 用每个时点之前的滚动窗口算，只用已收盘数据）。
- 每个有方向的字段都能「镜像」：涨跌全部取反（收益取负、上涨家数和下跌家数对调、主动买卖占比对调、
  多空账户占比对调……），镜像测试用它检查模型有没有方向偏置。
- 可选来源拿不到就跳过并记下原因，不挡这一轮。

请求都走 ``binance_gate``（和单币会议共用封禁与权重闸）。1 小时 K 线按币缓存，每轮只补新收盘的几根。
池成员用当前池（幸存者口径，日志里写明）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import statistics
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import httpx

from jiaoyi.contracts import SymbolPool
from jiaoyi.slow import binance_gate

logger = logging.getLogger(__name__)

FAPI = "https://fapi.binance.com"
FNG_URL = "https://api.alternative.me/fng/"
BTC, ETH = "BTCUSDT", "ETHUSDT"
HOUR_MS = 3_600_000
FUNDING_BASE_PER_8H = 0.0001
"""币安默认资金费基准：利率部分每 8 小时 0.01%，其他结算间隔按小时数折算。"""
POOL_METRICS_MAX_AGE = timedelta(minutes=90)
FUNDING_INFO_TTL = timedelta(hours=24)
FNG_TTL = timedelta(hours=1)
MAX_BARS = 1000
Q_MS = 900_000
BARS_15M = 16
"""15 分钟线留最近 16 根（4 小时）；首次 limit 16、之后只补新收盘的，权重都是 1。"""
SHALLOW_BARS = 200
"""1h 线首次只拉 200 根（权重 2），够算 σ（168 个窗口）和各段收益。"""
DEEP_PER_RUN = 8
"""每轮最多给几个币补全 30 天以上的 1h 历史（limit≈900，权重 5）：启动时分几轮补完，
不一口气打几十个大请求（防 418）。"""
DEEP_MIN_SHARE = 0.8
"""等权池里补全历史的币占到这个比例才给基础率（先验），之前写「回填中」。"""

Mirror = Literal["neg", "comp100", "inv", "same", "pair"]
Getter = Callable[..., Any]
"""``get(url, params) -> httpx.Response``；默认 ``binance_gate.get``（共用限流），测试换替身。"""


@dataclass(frozen=True)
class DossierField:
    """数据包里的一个数。``mirror`` 是镜像时怎么变：neg 取负、comp100 变成 100 − x、
    inv 取倒数、same 不变、pair 和 ``partner`` 互换值（上涨家数 ↔ 下跌家数这种）。"""

    name: str
    value: float
    unit: str
    note: str
    source: str
    source_ts: datetime
    mirror: Mirror = "same"
    partner: str | None = None
    digits: int = 2

    def shown(self) -> str:
        return f"{self.value:.{self.digits}f}"

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "shown": self.shown(),
            "unit": self.unit,
            "source": self.source,
            "source_ts": self.source_ts.isoformat(),
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
    high: float
    low: float
    close: float
    quote_vol: float
    taker_quote_vol: float


def parse_bars(rows: Sequence[Sequence[Any]], now_ms: int) -> list[Bar]:
    """``/fapi/v1/klines`` 原始行 → 已收盘的 1 小时线（收盘时间晚于现在的丢掉）。"""
    out: list[Bar] = []
    for r in rows:
        try:
            if int(r[6]) >= now_ms:
                continue
            out.append(
                Bar(int(r[0]), float(r[2]), float(r[3]), float(r[4]), float(r[7]), float(r[10]))
            )
        except (IndexError, TypeError, ValueError):
            continue
    return out


def price_at(bars: Mapping[int, Bar], t_ms: int, step: int = HOUR_MS) -> float | None:
    """时刻 t 的价 = 开盘在 t − step 的那根已收盘线的收盘价；没有就是 None，不拿邻近的补。"""
    b = bars.get(t_ms - step)
    return b.close if b is not None and b.close > 0 else None


def pool_return(
    series: Mapping[str, Mapping[int, Bar]], start_ms: int, end_ms: int, step: int = HOUR_MS
) -> tuple[float | None, list[float]]:
    """等权池在 [start, end] 的简单收益（%）= 两端都有价的币收益的平均；同时返回各币收益。"""
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
    # σ 序列先按小时算一遍，起点 t 的 σ 取 sigma(t)；避免对每个起点重复算 168 个窗口
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
        get: Getter | None = None,
        fng_get: Getter | None = None,
    ) -> None:
        self._band = band_sigma
        self._lookback = sigma_lookback_h
        self._days = base_rate_days
        self._exclude = frozenset(exclude_symbols)
        self._get: Getter = get or binance_gate.get
        self._fng_get: Getter = fng_get or _plain_get
        self._bars: dict[str, dict[int, Bar]] = {}
        self._bars15: dict[str, dict[int, Bar]] = {}
        self._deep: set[str] = set()
        self._funding_hours: dict[str, float] = {}
        self._funding_info_at: datetime | None = None
        self._fng: tuple[datetime, float, datetime] | None = None  # (取到时间, 值, 源时间)

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
        t0_ms = int(now.timestamp() * 1000) // HOUR_MS * HOUR_MS  # 最后一根已收盘 1h 线的收盘整点
        t0_bar = datetime.fromtimestamp(t0_ms / 1000, UTC)
        self._refresh_bars([*syms, BTC, ETH], now, missing)
        basket = {s: self._bars[s] for s in syms if self._bars.get(s)}
        sigma4: float | None = None
        if len(basket) < 5:
            missing["klines_1h"] = f"等权池能用的币只有 {len(basket)} 个（要 ≥5）"
        else:
            sigma4 = self._kline_fields(basket, t0_ms, t0_bar, fields, missing)
        self._refresh_15m([*syms, BTC], now, missing)
        self._quarter_fields(syms, now, fields, missing)
        self._major_fields(t0_ms, t0_bar, fields, missing)
        self._pool_metric_fields(pool, syms, now, fields, missing)
        self._funding_fields(syms, now, t0_ms, fields, missing)
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
        missing.setdefault("news", "第一阶段不接新闻（不可复现、有注入风险）")
        return Dossier(
            t0=now,
            fields=tuple(fields),
            missing=missing,
            symbols=tuple(sorted(basket)),
            pool_id=pool.pool_id,
            pool_ts=pool.ts,
            sigma_4h_pct=sigma4,
            method={
                "basket": "equal-weight simple return of current pool members (survivor basis)",
                "price_at_t": "close of the 1h bar opening at t-1h",
                "sigma": f"RMS of {self._lookback} rolling windows ending at or before t",
                "band_sigma": self._band,
                "base_rate_days": self._days,
                "base_rate_smoothing": "laplace +1",
                "funding_baseline": "0.01% per 8h scaled to each symbol's interval",
                "excluded": sorted(self._exclude),
            },
        )

    # ---------------------------------------------------------- K 线
    def _fetch_into(
        self,
        store: dict[str, dict[int, Bar]],
        sym: str,
        interval: str,
        limit: int,
        now_ms: int,
        keep: int,
    ) -> int:
        """拉一个币的 K 线并合进缓存，返回缓存里的根数。限流原样往外抛。"""
        r = self._get(
            f"{FAPI}/fapi/v1/klines", {"symbol": sym, "interval": interval, "limit": limit}
        )
        r.raise_for_status()
        merged = dict(store.get(sym, {}))
        for b in parse_bars(r.json(), now_ms):
            merged[b.open_ms] = b
        kept = sorted(merged)[-keep:]
        store[sym] = {k: merged[k] for k in kept}
        return len(kept)

    def _refresh_bars(self, symbols: Sequence[str], now: datetime, missing: dict[str, str]) -> None:
        """1h 线：没缓存的先拉 ``SHALLOW_BARS`` 根（够 σ 和各段收益），有缓存的只补新收盘的；
        再给最多 ``DEEP_PER_RUN`` 个还没补全的币拉满 30 天以上历史（基础率要），分几轮补完，
        启动时不一口气打几十个大请求。"""
        now_ms = int(now.timestamp() * 1000)
        failed: list[str] = []
        for sym in dict.fromkeys(symbols):
            have = self._bars.get(sym, {})
            if have:
                want = (now_ms - max(have)) // HOUR_MS + 2
                limit = int(min(max(want, 2), self.need_bars))
            else:
                limit = min(SHALLOW_BARS, self.need_bars)
            try:
                n = self._fetch_into(self._bars, sym, "1h", limit, now_ms, MAX_BARS)
            except binance_gate.RateLimited:
                raise
            except Exception as exc:
                failed.append(f"{sym}:{type(exc).__name__}")
                continue
            if n >= self.need_bars - 2:
                self._deep.add(sym)
        todo = [s for s in dict.fromkeys(symbols) if s in self._bars and s not in self._deep]
        for sym in todo[:DEEP_PER_RUN]:
            try:
                self._fetch_into(self._bars, sym, "1h", self.need_bars, now_ms, MAX_BARS)
            except binance_gate.RateLimited:
                raise
            except Exception as exc:
                failed.append(f"{sym}:deep:{type(exc).__name__}")
                continue
            self._deep.add(sym)  # 新上线不满 30 天的币：拉过一次全量就算补全
        if failed:
            missing["klines_1h_partial"] = "部分币 K 线没取到：" + ", ".join(failed[:10])

    def deep_basket(
        self, basket: Mapping[str, Mapping[int, Bar]]
    ) -> dict[str, Mapping[int, Bar]] | None:
        """补全了长历史的那部分等权池；占比不到 ``DEEP_MIN_SHARE`` 返回 None（先验还在回填）。"""
        deep = {s: b for s, b in basket.items() if s in self._deep}
        return deep if basket and len(deep) >= DEEP_MIN_SHARE * len(basket) else None

    def _refresh_15m(self, symbols: Sequence[str], now: datetime, missing: dict[str, str]) -> None:
        now_ms = int(now.timestamp() * 1000)
        failed: list[str] = []
        for sym in dict.fromkeys(symbols):
            have = self._bars15.get(sym, {})
            want = (now_ms - max(have)) // Q_MS + 2 if have else BARS_15M
            limit = int(min(max(want, 2), BARS_15M))
            try:
                self._fetch_into(self._bars15, sym, "15m", limit, now_ms, BARS_15M)
            except binance_gate.RateLimited:
                raise
            except Exception as exc:
                failed.append(f"{sym}:{type(exc).__name__}")
        if failed:
            missing["klines_15m_partial"] = "部分币 15 分钟线没取到：" + ", ".join(failed[:10])

    def _quarter_fields(
        self, syms: Sequence[str], now: datetime, out: list[DossierField], missing: dict[str, str]
    ) -> None:
        """15 分钟线：最近 15 / 30 / 60 分钟的等权收益、上涨下跌家数、主动买卖占比、BTC。"""
        t_ms = int(now.timestamp() * 1000) // Q_MS * Q_MS  # 最后一根已收盘 15m 线的收盘时刻
        ts = datetime.fromtimestamp(t_ms / 1000, UTC)
        basket = {s: self._bars15[s] for s in syms if self._bars15.get(s)}
        if len(basket) < 5:
            missing["klines_15m"] = f"15 分钟线能用的币只有 {len(basket)} 个（要 ≥5）"
            return

        def add(name: str, value: float | None, unit: str, note: str, **kw: Any) -> None:
            if value is None or not math.isfinite(value):
                missing[name] = "数据不足"
                return
            out.append(DossierField(name, value, unit, note, "klines_15m", ts, **kw))

        for m in (15, 30, 60):
            r, rets = pool_return(basket, t_ms - m * 60_000, t_ms, Q_MS)
            add(
                f"pool_ret_{m}m_pct",
                r,
                "%",
                f"Equal-weight basket return over the last {m} minutes (15m bars).",
                mirror="neg",
                digits=3,
            )
            if m == 15 and rets:
                add(
                    "pool_up_count_15m",
                    float(sum(x > 0 for x in rets)),
                    "count",
                    "Basket symbols with a 15m return above 0.",
                    mirror="pair",
                    partner="pool_down_count_15m",
                    digits=0,
                )
                add(
                    "pool_down_count_15m",
                    float(sum(x < 0 for x in rets)),
                    "count",
                    "Basket symbols with a 15m return below 0.",
                    mirror="pair",
                    partner="pool_up_count_15m",
                    digits=0,
                )
        btc = self._bars15.get(BTC, {})
        a, b = price_at(btc, t_ms - Q_MS, Q_MS), price_at(btc, t_ms, Q_MS)
        add(
            "btc_ret_15m_pct",
            (b / a - 1.0) * 100.0 if a and b else None,
            "%",
            "BTC return over the last 15 minutes.",
            mirror="neg",
            digits=3,
        )
        qv = tb = 0.0
        for bars in basket.values():
            last = bars.get(t_ms - Q_MS)
            if last is not None and last.quote_vol > 0:
                qv, tb = qv + last.quote_vol, tb + last.taker_quote_vol
        if qv <= 0:
            missing["taker_15m"] = "数据不足"
            return
        add(
            "pool_taker_buy_share_15m",
            tb / qv * 100.0,
            "%",
            "Taker buy share of basket quote volume over the last 15 minutes.",
            mirror="pair",
            partner="pool_taker_sell_share_15m",
        )
        add(
            "pool_taker_sell_share_15m",
            100.0 - tb / qv * 100.0,
            "%",
            "Taker sell share of basket quote volume over the last 15 minutes.",
            mirror="pair",
            partner="pool_taker_buy_share_15m",
        )

    def _kline_fields(
        self,
        basket: Mapping[str, Mapping[int, Bar]],
        t0_ms: int,
        t0: datetime,
        out: list[DossierField],
        missing: dict[str, str],
    ) -> float | None:
        def add(name: str, value: float | None, unit: str, note: str, **kw: Any) -> None:
            if value is None or not math.isfinite(value):
                missing[name] = "数据不足"
                return
            out.append(DossierField(name, value, unit, note, "klines_1h", t0, **kw))

        for h in (1, 4, 12, 24, 72):
            r, _ = pool_return(basket, t0_ms - h * HOUR_MS, t0_ms)
            add(
                f"pool_ret_{h}h_pct",
                r,
                "%",
                f"Equal-weight basket return over the last {h}h.",
                mirror="neg",
                digits=3,
            )
        for h in (1, 4, 24):
            _, rets = pool_return(basket, t0_ms - h * HOUR_MS, t0_ms)
            if not rets:
                missing[f"pool_count_{h}h"] = "数据不足"
                continue
            up, down = sum(x > 0 for x in rets), sum(x < 0 for x in rets)
            add(
                f"pool_up_count_{h}h",
                float(up),
                "count",
                f"Basket symbols with a {h}h return above 0.",
                mirror="pair",
                partner=f"pool_down_count_{h}h",
                digits=0,
            )
            add(
                f"pool_down_count_{h}h",
                float(down),
                "count",
                f"Basket symbols with a {h}h return below 0.",
                mirror="pair",
                partner=f"pool_up_count_{h}h",
                digits=0,
            )
            if h == 4 and len(rets) >= 2:
                add(
                    "pool_dispersion_4h_pct",
                    statistics.pstdev(rets),
                    "%",
                    "Cross-sectional standard deviation of 4h returns across the basket.",
                    digits=3,
                )
        sigma4 = sigma_at(basket, t0_ms, 4, self._lookback)
        sigma1 = sigma_at(basket, t0_ms, 1, self._lookback)
        add("sigma_4h_pct", sigma4, "%", "Sigma of the 4h basket return (RMS).", digits=3)
        add("sigma_1h_pct", sigma1, "%", "Sigma of the 1h basket return (RMS).", digits=3)
        r4, _ = pool_return(basket, t0_ms - 4 * HOUR_MS, t0_ms)
        if r4 is not None and sigma4:
            add(
                "pool_ret_4h_in_sigma",
                r4 / sigma4,
                "sigma",
                "Last 4h basket return divided by sigma_4h_pct.",
                mirror="neg",
            )
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
        # 主动买卖（K 线第 10 列：主动买入成交额），按成交额加权
        for h in (1, 4):
            qv = tb = 0.0
            for bars in basket.values():
                for k in range(h):
                    b = bars.get(t0_ms - (k + 1) * HOUR_MS)
                    if b is not None and b.quote_vol > 0:
                        qv += b.quote_vol
                        tb += b.taker_quote_vol
            if qv <= 0:
                missing[f"taker_{h}h"] = "数据不足"
                continue
            buy = tb / qv * 100.0
            add(
                f"pool_taker_buy_share_{h}h",
                buy,
                "%",
                f"Taker buy share of basket quote volume over the last {h}h.",
                mirror="pair",
                partner=f"pool_taker_sell_share_{h}h",
            )
            add(
                f"pool_taker_sell_share_{h}h",
                100.0 - buy,
                "%",
                f"Taker sell share of basket quote volume over the last {h}h.",
                mirror="pair",
                partner=f"pool_taker_buy_share_{h}h",
            )
        pos: list[float] = []
        for bars in basket.values():
            win = [bars.get(t0_ms - (k + 1) * HOUR_MS) for k in range(24)]
            got = [b for b in win if b is not None]
            if len(got) < 20:
                continue
            hi, lo = max(b.high for b in got), min(b.low for b in got)
            last = bars.get(t0_ms - HOUR_MS)
            if last is not None and hi > lo:
                pos.append((last.close - lo) / (hi - lo) * 100.0)
        add(
            "pool_range_position_24h_median_pct",
            statistics.median(pos) if pos else None,
            "%",
            "Median position of the last close inside each symbol's 24h low-high range "
            "(0 = at the low, 100 = at the high).",
            mirror="comp100",
            digits=1,
        )
        return sigma4

    def _major_fields(
        self, t0_ms: int, t0: datetime, out: list[DossierField], missing: dict[str, str]
    ) -> None:
        btc, eth = self._bars.get(BTC, {}), self._bars.get(ETH, {})

        def ret(bars: Mapping[int, Bar], h: int) -> float | None:
            a, b = price_at(bars, t0_ms - h * HOUR_MS), price_at(bars, t0_ms)
            return (b / a - 1.0) * 100.0 if a and b else None

        rows: list[tuple[str, float | None, str]] = [
            ("btc_ret_1h_pct", ret(btc, 1), "BTC return over the last 1h."),
            ("btc_ret_4h_pct", ret(btc, 4), "BTC return over the last 4h."),
            ("btc_ret_24h_pct", ret(btc, 24), "BTC return over the last 24h."),
            ("eth_ret_4h_pct", ret(eth, 4), "ETH return over the last 4h."),
            ("eth_ret_24h_pct", ret(eth, 24), "ETH return over the last 24h."),
        ]
        e24, b24 = ret(eth, 24), ret(btc, 24)
        if e24 is not None and b24 is not None:
            ratio = ((1 + e24 / 100) / (1 + b24 / 100) - 1) * 100
            rows.append(("eth_btc_ratio_chg_24h_pct", ratio, "Change of the ETH/BTC price ratio."))
        basket = {s: b for s, b in self._bars.items() if s not in (BTC, ETH)}
        p24, _ = pool_return(basket, t0_ms - 24 * HOUR_MS, t0_ms) if basket else (None, [])
        if b24 is not None and p24 is not None:
            rows.append(("btc_minus_pool_ret_24h_pct", b24 - p24, "BTC 24h return minus basket."))
        for name, value, note in rows:
            if value is None:
                missing[name] = "BTC / ETH K 线不足"
                continue
            out.append(DossierField(name, value, "%", note, "klines_1h", t0, "neg", digits=3))

    # ---------------------------------------------------------- 池统计（选币时已算好）
    def _pool_metric_fields(
        self,
        pool: SymbolPool,
        syms: Sequence[str],
        now: datetime,
        out: list[DossierField],
        missing: dict[str, str],
    ) -> None:
        age = now - pool.ts
        if age > POOL_METRICS_MAX_AGE or age < timedelta(0):
            missing["pool_metrics"] = f"候选池统计 {age.total_seconds() / 60:.0f} 分钟前，太旧不用"
            return
        keep = set(syms)
        entries = [e for e in pool.in_pool if e.symbol in keep]

        def med(key: str) -> float | None:
            vs = [e.metrics[key] for e in entries if math.isfinite(e.metrics.get(key, math.nan))]
            return statistics.median(vs) if vs else None

        liq = [
            e.metrics["liq_1h"] for e in entries if math.isfinite(e.metrics.get("liq_1h", math.nan))
        ]
        rows: list[tuple[str, float | None, str, str]] = [
            (
                "pool_oi_chg_1h_median_pct",
                med("oi_chg_1h"),
                "%",
                "Median 1h open-interest change across the basket.",
            ),
            (
                "pool_vol_ratio_1h_median",
                med("vol_ratio_1h"),
                "x",
                "Median of last-hour volume divided by the usual hourly volume.",
            ),
            (
                "pool_liquidations_1h_musd",
                sum(liq) / 1e6 if liq else None,
                "M USDT",
                "Total liquidations (both sides) across the basket over the last hour.",
            ),
        ]
        for name, value, unit, note in rows:
            if value is None or not math.isfinite(value):
                missing[name] = "候选池统计里没有"
                continue
            out.append(DossierField(name, value, unit, note, "pool_metrics", pool.ts))

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

    def _intrahour_fields(
        self,
        rows: Mapping[str, Mapping[str, Any]],
        syms: Sequence[str],
        t0_ms: int,
        ts: datetime,
        out: list[DossierField],
        missing: dict[str, str],
    ) -> None:
        """整点之后到现在（标记价）的涨跌：15 分钟一轮时，整点 K 线之外最新的一段。"""

        def since_close(sym: str) -> float | None:
            last = price_at(self._bars.get(sym, {}), t0_ms)
            try:
                mark = float(rows[sym]["markPrice"])
            except (KeyError, TypeError, ValueError):
                return None
            return (mark / last - 1.0) * 100.0 if last and mark > 0 else None

        pool = [r for r in (since_close(s) for s in syms) if r is not None]
        btc = since_close(BTC)
        minutes = (ts - datetime.fromtimestamp(t0_ms / 1000, UTC)).total_seconds() / 60
        if len(pool) < 5 or btc is None:
            missing["intrahour"] = "整点后涨跌算不出（K 线或标记价缺）"
            return
        note = "since the last hourly close, using the current mark price."
        out.append(
            DossierField(
                "pool_ret_since_hour_close_pct",
                statistics.fmean(pool),
                "%",
                f"Equal-weight basket return {note}",
                "premiumIndex",
                ts,
                "neg",
                digits=3,
            )
        )
        out.append(
            DossierField(
                "btc_ret_since_hour_close_pct",
                btc,
                "%",
                f"BTC return {note}",
                "premiumIndex",
                ts,
                "neg",
                digits=3,
            )
        )
        out.append(
            DossierField(
                "minutes_since_hour_close",
                minutes,
                "min",
                "Minutes between the last hourly close and the mark-price snapshot.",
                "premiumIndex",
                ts,
                digits=0,
            )
        )

    def _funding_fields(
        self,
        syms: Sequence[str],
        now: datetime,
        t0_ms: int,
        out: list[DossierField],
        missing: dict[str, str],
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
        self._intrahour_fields(rows, syms, t0_ms, ts, out, missing)
        pool_ex = [excess[s] for s in syms if s in excess]
        if not pool_ex:
            missing["funding"] = "池内没有资金费数据"
            return
        n = len(pool_ex)
        above = sum(x > 0.005 for x in pool_ex) / n * 100.0
        below = sum(x < -0.005 for x in pool_ex) / n * 100.0
        rows_out: list[DossierField] = [
            DossierField(
                "funding_excess_median_bp8h",
                statistics.median(pool_ex),
                "bp/8h",
                "Median funding minus the 0.01%/8h baseline, in basis points per 8h; "
                "positive = longs pay shorts more than baseline, negative = shorts pay longs "
                "more than baseline.",
                "premiumIndex",
                ts,
                "neg",
                digits=3,
            ),
            DossierField(
                "funding_share_above_base_pct",
                above,
                "%",
                "Share of basket symbols with funding above the baseline.",
                "premiumIndex",
                ts,
                "pair",
                "funding_share_below_base_pct",
                digits=1,
            ),
            DossierField(
                "funding_share_below_base_pct",
                below,
                "%",
                "Share of basket symbols with funding below the baseline.",
                "premiumIndex",
                ts,
                "pair",
                "funding_share_above_base_pct",
                digits=1,
            ),
        ]
        if basis:
            rows_out.append(
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
                rows_out.append(
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
        out.extend(rows_out)

    # ---------------------------------------------------------- 多空账户 / 持仓量（BTC、ETH）
    def _crowd_fields(
        self, sym: str, tag: str, out: list[DossierField], missing: dict[str, str]
    ) -> None:
        name = f"{tag}_long_short_account"
        try:
            r = self._get(
                f"{FAPI}/futures/data/globalLongShortAccountRatio",
                {"symbol": sym, "period": "1h", "limit": 168},
            )
            r.raise_for_status()
            rows = r.json()
            longs = [float(x["longAccount"]) * 100 for x in rows]
            shorts = [float(x["shortAccount"]) * 100 for x in rows]
            ratios = [math.log(float(x["longShortRatio"])) for x in rows]
            ts = datetime.fromtimestamp(int(rows[-1]["timestamp"]) / 1000, UTC)
        except binance_gate.RateLimited:
            raise
        except Exception as exc:
            missing[name] = f"多空账户比没取到：{type(exc).__name__}"
            return
        up = tag.upper()
        out.append(
            DossierField(
                f"{tag}_long_account_pct",
                longs[-1],
                "%",
                f"{up} share of accounts net long.",
                "globalLongShortAccountRatio",
                ts,
                "pair",
                f"{tag}_short_account_pct",
                digits=1,
            )
        )
        out.append(
            DossierField(
                f"{tag}_short_account_pct",
                shorts[-1],
                "%",
                f"{up} share of accounts net short.",
                "globalLongShortAccountRatio",
                ts,
                "pair",
                f"{tag}_long_account_pct",
                digits=1,
            )
        )
        z = zscore(ratios)
        if z is None:
            missing[f"{tag}_long_short_z_7d"] = "历史不足"
        else:
            out.append(
                DossierField(
                    f"{tag}_long_short_log_ratio_z_7d",
                    z,
                    "z",
                    f"{up} log(long/short account ratio) versus its own last 7 days, in standard "
                    "deviations; positive = more long than usual, "
                    "negative = more short than usual.",
                    "globalLongShortAccountRatio",
                    ts,
                    "neg",
                )
            )

    def _oi_fields(
        self, sym: str, tag: str, out: list[DossierField], missing: dict[str, str]
    ) -> None:
        try:
            r = self._get(
                f"{FAPI}/futures/data/openInterestHist",
                {"symbol": sym, "period": "1h", "limit": 25},
            )
            r.raise_for_status()
            rows = r.json()
            vals = [float(x["sumOpenInterestValue"]) for x in rows]
            ts = datetime.fromtimestamp(int(rows[-1]["timestamp"]) / 1000, UTC)
        except binance_gate.RateLimited:
            raise
        except Exception as exc:
            missing[f"{tag}_oi"] = f"持仓量历史没取到：{type(exc).__name__}"
            return
        for h in (4, 24):
            if len(vals) > h and vals[-1 - h] > 0:
                out.append(
                    DossierField(
                        f"{tag}_oi_chg_{h}h_pct",
                        (vals[-1] / vals[-1 - h] - 1) * 100,
                        "%",
                        f"{tag.upper()} open-interest value change over {h}h.",
                        "openInterestHist",
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
                "alternative.me index value only (0 to 100); the text label is withheld.",
                "alternative.me",
                ts,
                "comp100",
                digits=0,
            )
        )


def _plain_get(url: str, params: Mapping[str, Any] | None = None) -> httpx.Response:
    started = time.monotonic()
    r = httpx.get(url, params=dict(params or {}), timeout=10.0)
    logger.debug("GET %s %.0fms", url, (time.monotonic() - started) * 1000)
    return r


def _next_settlement(now: datetime) -> datetime:
    base = now.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    for k in range(1, 10):
        t = base + timedelta(hours=k)
        if t.hour % 8 == 0 and t > now:
            return t
    return base + timedelta(hours=8)
