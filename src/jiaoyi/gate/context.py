"""拍板人一道题问法的额外快照块：按持仓档的技术面、长一点的成交流、衍生品、24 小时行情。

Aaron 10-24：「我们这个是做两个小时的单，那你只给他 30 秒，他怎么判断？」「币安所有的技术类数据
给他」。规格见 /mnt/project-files/research/jev-snapshot/spec.md 拍板人一节（§2、§3）。

- ``tech``：技术面，周期跟着最长持仓档走（``horizon_for``）。只用信号时刻之前已收盘的 K 线；
  最后一根 1 分钟线收盘超过 10 分钟就整块不写；单个键算不出就不写。
- ``flow`` / ``book_imb``：特征引擎内存里已有的成交流和盘口失衡平均，不发请求。
- 信号来时并发补拉的 REST（大户人数多空比、主动买卖量比、近 3 次资金费率、24 小时行情，
  4 小时及以上档再拉 4 小时 / 1 天 K 线）：和拍板人排队同时开始，总共最多等 1 秒，没回来的
  项不写；按币缓存，同一个币短时间多个信号不重复拉。

这里只出原始值（``EntryExtras``），怎么压进快照在 ``gate.questions.entry_snapshot``。所有值都会
原样写进决策日志 ``input.jev.extra``（包括快照超长被删掉的），上线几天后逐项验证。
"""

from __future__ import annotations

import asyncio
import bisect
import contextlib
import itertools
import logging
import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from jiaoyi.market.derivs import Fetch
from jiaoyi.market.features import EntryContext

log = logging.getLogger(__name__)

Bar = tuple[int, float, float, float, float, float]
"""(开盘分钟, 开, 高, 低, 收, 成交量)。"""

TREND_BPS = 15.0
"""涨跌超过 +15 基点算 up，低于 −15 算 down，其余 flat。"""
STALE_MIN = 10
"""最后一根 1 分钟线收盘离现在超过这么多分钟（5 分钟线 × 2），技术面整块不写。"""
REST_WAIT_S = 1.0
"""信号来时补拉的 REST 总共最多等几秒，没回来的不写。"""

_LABEL_MIN = {"5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440, "3d": 4320}


@dataclass(frozen=True)
class Horizon:
    """一个持仓档问多久、技术面看哪三个周期。"""

    minutes: int
    """题目问「接下来多久」（分钟）。"""
    periods: tuple[str, str, str]
    """技术面的短 / 中 / 长周期（涨跌、趋势），中周期也是 BTC 涨跌的周期。"""
    rsi_short: str
    rsi_long: str
    range_hours: int
    rest: tuple[str, ...]
    """要用 REST 补拉的 K 线周期（``4h`` / ``1d``）。"""

    @property
    def text(self) -> str:
        if self.minutes % 60 == 0:
            h = self.minutes // 60
            return f"{h} hour" if h == 1 else f"{h} hours"
        return f"{self.minutes} minutes"


# 只有 2 小时档测过（30 分钟、15m/1h/4h）；其余按同样比例推的，上线前用该档本地记录重跑 ab/run_ab.py
HORIZONS: tuple[tuple[int, Horizon], ...] = (
    (30, Horizon(15, ("5m", "15m", "1h"), "5m", "1h", 24, ())),
    (120, Horizon(30, ("15m", "1h", "4h"), "5m", "1h", 24, ())),
    (240, Horizon(60, ("1h", "4h", "1d"), "15m", "4h", 24, ("4h",))),
    (10**9, Horizon(120, ("4h", "1d", "3d"), "1h", "1d", 72, ("4h", "1d"))),
)


def horizon_for(hold_min: int) -> Horizon:
    """最长持仓（分钟）→ 问多久、看哪些周期：30 分钟档问 15 分钟，1–2 小时问 30 分钟，
    4 小时问 1 小时，8 小时及以上问 2 小时。"""
    for upto, h in HORIZONS:
        if hold_min <= upto:
            return h
    return HORIZONS[-1][1]


# ---------------------------------------------------------------------- 技术面


def _r(x: float, nd: int = 1) -> float:
    return round(x, nd)


def _resample(bars: Sequence[Bar], n: int) -> list[Bar]:
    """1 分钟线按 UTC 对齐聚合成 ``n`` 分钟线；只留收完的（最后一根 1 分钟线在这一段末尾）。"""
    if n == 1:
        return list(bars)
    out: list[Bar] = []
    cur: list[float] | None = None
    start = -1
    for minute, o, h, lo, c, v in bars:
        s = minute - minute % n
        if s != start:
            cur, start = [float(s), o, h, lo, c, v], s
        elif cur is not None:
            cur[2], cur[3], cur[4], cur[5] = max(cur[2], h), min(cur[3], lo), c, cur[5] + v
        if minute == s + n - 1 and cur is not None:
            out.append((int(cur[0]), cur[1], cur[2], cur[3], cur[4], cur[5]))
            cur = None
    return out


def rsi14(closes: Sequence[float]) -> int | None:
    """最近 15 个收盘价的 RSI(14)，简单平均涨跌；不够 15 个点返回 None。"""
    if len(closes) < 15:
        return None
    pts = closes[-15:]
    gain = loss = 0.0
    for a, b in itertools.pairwise(pts):
        d = b - a
        if d > 0:
            gain += d
        else:
            loss -= d
    if gain == 0 and loss == 0:
        return 50
    if loss == 0:
        return 100
    return round(100.0 - 100.0 / (1.0 + gain / loss))


def _open_at(bars: Sequence[Bar], target: int, tf: int) -> float | None:
    """``target``（分钟）所在那根 K 线的开盘价。1 分钟线允许缺 5 分钟以内。"""
    if not bars:
        return None
    if tf == 1:
        i = bisect.bisect_left(bars, (target,))
        if i < len(bars) and bars[i][0] - target <= 5:
            return bars[i][1]
        return None
    best: Bar | None = None
    for b in bars:
        if b[0] > target:
            break
        best = b
    if best is None or target - best[0] >= tf:
        return None
    return best[1]


def _chg(
    m1: Sequence[Bar], rest: Mapping[str, Sequence[Bar]], now_min: int, minutes: int, px: float
) -> float | None:
    """现价 ÷ ``minutes`` 分钟前那根 K 线的开盘价 − 1（基点）；1 分钟线不够就用 REST 的长周期线。"""
    target = now_min - minutes
    op: float | None = None
    if m1 and m1[0][0] <= target:
        op = _open_at(m1, target, 1)
    if op is None:
        for tf in ("4h", "1d"):
            if tf in rest:
                op = _open_at(rest[tf], target, _LABEL_MIN[tf])
                if op is not None:
                    break
    if not op:
        return None
    return _r((px / op - 1.0) * 1e4)


def _series(m1: Sequence[Bar], rest: Mapping[str, Sequence[Bar]], label: str) -> list[Bar]:
    """某个周期的收完的 K 线：1 分钟线够算 RSI(14) 就自己聚合，不够用 REST 拉的。"""
    n = _LABEL_MIN[label]
    own = _resample(m1, n)
    if len(own) >= 15 or label not in rest:
        return own
    return list(rest[label])


def _trend(bps: float) -> str:
    return "up" if bps > TREND_BPS else "down" if bps < -TREND_BPS else "flat"


def tech_block(
    ctx: EntryContext,
    h: Horizon,
    *,
    atr_bps: float,
    levels_bps: Sequence[float] = (),
    rest: Mapping[str, Sequence[Bar]] | None = None,
) -> dict[str, Any] | None:
    """技术面块；1 分钟线太旧或没有返回 None（整块不写），单个键算不出就不写。

    ``levels_bps``：作战卡关键位离现价多少基点；
    ``rest``：REST 拉的 ``4h`` / ``1d`` 线（已收盘）。"""
    m1 = ctx.bars
    rest = rest or {}
    now_min = int(ctx.now // 60)
    if not m1 or now_min - (m1[-1][0] + 1) > STALE_MIN:
        return None
    px = ctx.price
    d: dict[str, Any] = {}
    for label in h.periods:
        bps = _chg(m1, rest, now_min, _LABEL_MIN[label], px)
        if bps is not None:
            d[f"chg_{label}_bps"] = bps
            d[f"trend_{label}"] = _trend(bps)
    for label in (h.rsi_short, h.rsi_long):
        v = rsi14([b[4] for b in _series(m1, rest, label)])
        if v is not None:
            d[f"rsi14_{label}"] = v
    lo_bound = now_min - h.range_hours * 60
    if h.range_hours <= 24 or "4h" not in rest:
        span = [b for b in m1 if b[0] >= lo_bound]
        first = span[0][0] if span else now_min
    else:
        span = [b for b in rest["4h"] if b[0] >= lo_bound]
        first = span[0][0] if span else now_min
    if span:
        hi, lo = max(b[2] for b in span), min(b[3] for b in span)
        d["range_hours"] = _r((now_min - first) / 60.0)
        if hi > lo:
            d["pos_in_range_pct"] = round(min(max((px - lo) / (hi - lo), 0.0), 1.0) * 100)
    day0 = now_min - now_min % 1440
    pv = vol = 0.0
    for b in m1:
        if b[0] >= day0:
            pv += (b[2] + b[3] + b[4]) / 3.0 * b[5]
            vol += b[5]
    if vol > 0:
        d["vwap_dist_bps"] = _r((px / (pv / vol) - 1.0) * 1e4)
    if atr_bps > 0:
        above = [x for x in levels_bps if x > 0]
        below = [x for x in levels_bps if x < 0]
        d["level_above_atr"] = _r(min(above) / atr_bps, 2) if above else None
        d["level_below_atr"] = _r(-max(below) / atr_bps, 2) if below else None
    btc = ctx.btc_bars
    if btc and now_min - (btc[-1][0] + 1) <= STALE_MIN:
        mid = h.periods[1]
        bpx = btc[-1][4]
        bps = _chg(btc, {}, now_min, _LABEL_MIN[mid], bpx)
        if bps is not None:
            d[f"btc_{mid}_bps"] = bps
    return d or None


# ---------------------------------------------------------------------- 成交流、盘口、REST


def flow_block(ctx: EntryContext, rest: Mapping[str, Any]) -> dict[str, Any]:
    """快照 ``flow`` 块：5 分钟 / 15 分钟 / 1 小时主动买卖、币安统计的主动买卖量比、
    大单、15 分钟爆仓。"""
    f = ctx.flow
    d: dict[str, Any] = {}
    for label in ("5m", "15m", "1h"):
        if f"cvd_{label}" in f:
            d[f"cvd_{label}_k"] = _r(f[f"cvd_{label}"] / 1e3)
        if f"buy_{label}" in f:
            d[f"buy_{label}"] = _r(f[f"buy_{label}"], 2)
    taker = rest.get("taker")
    if isinstance(taker, dict):
        for k in ("taker_ratio_1h", "taker_ratio_4h"):
            if taker.get(k) is not None:
                d[k] = _r(taker[k], 2)
    if "big_buy_n" in f:
        big: dict[str, Any] = {"buy": int(f["big_buy_n"]), "sell": int(f.get("big_sell_n", 0))}
        if "big_pct" in f:
            big["pct"] = _r(f["big_pct"])
        d["big_15m"] = big
    if "liq_15m_long" in f:
        d["liq_15m_long_k"] = _r(f["liq_15m_long"] / 1e3)
        d["liq_15m_short_k"] = _r(f.get("liq_15m_short", 0.0) / 1e3)
    return d


def deriv_block(
    values: Mapping[str, float], adl: str | None, rest: Mapping[str, Any]
) -> dict[str, Any]:
    """快照 ``deriv`` 块：基差、离下次结算几分钟、近 3 次资金费率、大户人数多空比、自动减仓风险。

    当前资金费率、持仓量、大户持仓 / 散户多空比在快照顶层已有（``fund_bps``、``oi*``、
    ``top_ls`` 等），不重复。"""
    d: dict[str, Any] = {}
    if "mark_premium_bps" in values:
        d["premium_bps"] = _r(values["mark_premium_bps"], 2)
    if "funding_in_min" in values:
        d["next_funding_min"] = round(values["funding_in_min"])
    prev = rest.get("funding")
    if isinstance(prev, list) and prev:
        d["funding_prev"] = [_r(x, 2) for x in prev]
    if rest.get("top_acct") is not None:
        d["top_acct_ls"] = _r(float(rest["top_acct"]), 3)
    if adl:
        d["adl"] = adl
    return d


def day_block(rest: Mapping[str, Any], px: float) -> dict[str, Any] | None:
    t = rest.get("day")
    if not isinstance(t, dict) or px <= 0:
        return None
    d: dict[str, Any] = {}
    with contextlib.suppress(KeyError, TypeError, ValueError):
        d["chg_pct"] = _r(float(t["priceChangePercent"]), 2)
    with contextlib.suppress(KeyError, TypeError, ValueError):
        d["qv_m"] = _r(float(t["quoteVolume"]) / 1e6)
    for key, src in (("hi_bps", "highPrice"), ("lo_bps", "lowPrice")):
        with contextlib.suppress(KeyError, TypeError, ValueError, ZeroDivisionError):
            d[key] = _r((float(t[src]) / px - 1.0) * 1e4)
    return d or None


def _klines(data: Any, now: float) -> list[Bar]:
    """REST K 线 → ``Bar``，丢掉还没收盘的那根。"""
    out: list[Bar] = []
    if not isinstance(data, list):
        return out
    for row in data:
        try:
            if float(row[6]) / 1000.0 > now:
                continue
            out.append(
                (
                    int(float(row[0]) // 60000),
                    float(row[1]),
                    float(row[2]),
                    float(row[3]),
                    float(row[4]),
                    float(row[5]),
                )
            )
        except (IndexError, TypeError, ValueError):
            continue
    return out


def _taker(data: Any) -> dict[str, float] | None:
    """``takerlongshortRatio`` 5 分钟 × 48 根 → 最近 1 小时 / 4 小时的主动买量 ÷ 主动卖量。"""
    if not isinstance(data, list) or not data:
        return None
    try:
        rows = sorted(
            (float(r["timestamp"]), float(r["buyVol"]), float(r["sellVol"])) for r in data
        )
    except (KeyError, TypeError, ValueError):
        return None
    out: dict[str, float] = {}
    for key, n in (("taker_ratio_1h", 12), ("taker_ratio_4h", 48)):
        part = rows[-n:]
        sell = sum(r[2] for r in part)
        if sell > 0 and len(part) >= n // 2:
            out[key] = sum(r[1] for r in part) / sell
    return out or None


def _top_acct(data: Any) -> float | None:
    if not isinstance(data, list) or not data:
        return None
    try:
        return float(max(data, key=lambda r: float(r["timestamp"]))["longShortRatio"])
    except (KeyError, TypeError, ValueError):
        return None


def _funding(data: Any) -> list[float] | None:
    if not isinstance(data, list) or not data:
        return None
    try:
        rows = sorted((float(r["fundingTime"]), float(r["fundingRate"]) * 1e4) for r in data)
    except (KeyError, TypeError, ValueError):
        return None
    return [r[1] for r in rows[-3:]]


@dataclass(frozen=True)
class _Item:
    path: str
    params: Callable[[str], dict[str, Any]]
    weight: int
    ttl_s: float
    parse: Callable[[Any, float], Any]


_ITEMS: dict[str, _Item] = {
    "top_acct": _Item(
        "/futures/data/topLongShortAccountRatio",
        lambda s: {"symbol": s, "period": "5m", "limit": 1},
        0,
        300.0,
        lambda d, _: _top_acct(d),
    ),
    "taker": _Item(
        "/futures/data/takerlongshortRatio",
        lambda s: {"symbol": s, "period": "5m", "limit": 48},
        0,
        300.0,
        lambda d, _: _taker(d),
    ),
    "funding": _Item(
        "/fapi/v1/fundingRate",
        lambda s: {"symbol": s, "limit": 3},
        1,
        1800.0,
        lambda d, _: _funding(d),
    ),
    "day": _Item(
        "/fapi/v1/ticker/24hr",
        lambda s: {"symbol": s},
        1,
        60.0,
        lambda d, _: d if isinstance(d, dict) else None,
    ),
    "4h": _Item(
        "/fapi/v1/klines", lambda s: {"symbol": s, "interval": "4h", "limit": 20}, 1, 300.0, _klines
    ),
    "1d": _Item(
        "/fapi/v1/klines", lambda s: {"symbol": s, "interval": "1d", "limit": 20}, 1, 300.0, _klines
    ),
}
FAIL_TTL_S = 30.0
"""查失败的项缓存这么久再重试（别每个信号都去撞）。"""


class SignalRest:
    """信号来时补拉的 REST，按币缓存；同一个币同时来几个信号只拉一次。"""

    def __init__(self, fetch: Fetch, *, clock: Callable[[], float] = time.time) -> None:
        self._fetch = fetch
        self._clock = clock
        self._cache: dict[tuple[str, str], tuple[float, asyncio.Task[Any]]] = {}
        self._failing: set[str] = set()

    def start(self, symbol: str, names: Sequence[str]) -> dict[str, asyncio.Task[Any]]:
        now = self._clock()
        out: dict[str, asyncio.Task[Any]] = {}
        for name in names:
            hit = self._cache.get((symbol, name))
            if hit is not None and (not hit[1].done() or hit[0] > now):
                out[name] = hit[1]
                continue
            task = asyncio.ensure_future(self._get(symbol, name))
            self._cache[(symbol, name)] = (math.inf, task)
            out[name] = task
        return out

    async def _get(self, symbol: str, name: str) -> Any:
        item = _ITEMS[name]
        try:
            data = await self._fetch(item.path, item.params(symbol), item.weight)
            value = item.parse(data, self._clock())
        except Exception as e:
            if name not in self._failing:
                self._failing.add(name)
                log.warning("拍板人快照补拉 %s 失败，这次不写：%r", name, e)
            value = None
        else:
            self._failing.discard(name)
        ttl = item.ttl_s if value is not None else FAIL_TTL_S
        key = (symbol, name)
        hit = self._cache.get(key)
        if hit is not None:
            self._cache[key] = (self._clock() + ttl, hit[1])
        return value


async def gather_within(tasks: Mapping[str, asyncio.Task[Any]], timeout_s: float) -> dict[str, Any]:
    """等 ``tasks`` 最多 ``timeout_s`` 秒，返回已经回来的（值为 None 的不要）。

    任务不取消（缓存着下次用）。"""
    if not tasks:
        return {}
    pending = [t for t in tasks.values() if not t.done()]
    if pending:
        await asyncio.wait([asyncio.shield(t) for t in pending], timeout=max(timeout_s, 0.0))
    out: dict[str, Any] = {}
    for name, t in tasks.items():
        if t.done() and not t.cancelled() and t.exception() is None and t.result() is not None:
            out[name] = t.result()
    return out


# ---------------------------------------------------------------------- 拼起来


@dataclass(frozen=True)
class EntryExtras:
    """拍板人快照的额外原始值。键：``tech`` / ``flow`` / ``deriv`` / ``day`` / ``book_imb``；
    ``missing``：没回来 / 算不出的项；``rest_ms``：等 REST 用了多久。"""

    blocks: dict[str, Any]
    missing: tuple[str, ...] = ()
    rest_ms: float = 0.0

    def log(self) -> dict[str, Any]:
        return {**self.blocks, "missing": list(self.missing), "rest_ms": round(self.rest_ms, 1)}


class EntryContextSource:
    """拍板人一道题问法的额外快照数据。``local``：特征引擎的 ``entry_context``；
    ``adl``：自动减仓风险；``fetch``：公开 REST（None = 不补拉，测试 / 回放用）。"""

    def __init__(
        self,
        local: Callable[[str], EntryContext | None],
        *,
        adl: Callable[[str], str | None] | None = None,
        fetch: Fetch | None = None,
        timeout_s: float = REST_WAIT_S,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._local = local
        self._adl = adl
        self._rest = SignalRest(fetch, clock=clock) if fetch is not None else None
        self._timeout_s = timeout_s

    async def __call__(
        self,
        symbol: str,
        hold_min: int,
        atr_bps: float,
        levels_bps: Sequence[float],
        values: Mapping[str, float],
    ) -> EntryExtras:
        h = horizon_for(hold_min)
        names = ("top_acct", "taker", "funding", "day", *h.rest)
        t0 = time.monotonic()
        tasks = self._rest.start(symbol, names) if self._rest is not None else {}
        rest = await gather_within(tasks, self._timeout_s)
        rest_ms = (time.monotonic() - t0) * 1000.0
        missing = [n for n in names if n not in rest] if self._rest is not None else []
        ctx = self._local(symbol)
        blocks: dict[str, Any] = {}
        if ctx is not None:
            bars = {k: rest[k] for k in h.rest if k in rest}
            tech = tech_block(ctx, h, atr_bps=atr_bps, levels_bps=levels_bps, rest=bars)
            if tech is not None:
                blocks["tech"] = tech
            else:
                missing.append("tech")
            blocks["flow"] = flow_block(ctx, rest)
            if ctx.imb:
                blocks["book_imb"] = {k: _r(v, 2) for k, v in ctx.imb.items()}
            px = ctx.price
        else:
            missing.append("local")
            px = 0.0
        adl = None
        if self._adl is not None:
            with contextlib.suppress(Exception):
                adl = self._adl(symbol)
        deriv = deriv_block(values, adl, rest)
        if deriv:
            blocks["deriv"] = deriv
        day = day_block(rest, px)
        if day is not None:
            blocks["day"] = day
        return EntryExtras(blocks, tuple(missing), rest_ms)
