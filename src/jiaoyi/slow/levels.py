"""关键位：由代码从真实 K 线算出候选，军师团只能从里面选。

大模型凭记忆报价位会报出远离现价、精度可疑的数（BTC 现价 81600 报 72859.5）。这里改成：
代码用 K 线算一批候选（昨日 / 7 日高低点、近期摆动高低点、成交密集区、整数关口），
只保留现价 ±``span_atr`` 倍 ATR 以内的，按支撑 / 压力分组、距现价从近到远排好喂给会议；
会议给出的价位再对回候选（容差 ``snap_atr`` 倍 ATR），对不上的丢掉，最终每边最多 3 个。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from jiaoyi.contracts import KeyLevel, LevelKind

Rows = Sequence[Sequence[Any]]

SPAN_ATR = 4.0
SNAP_ATR = 0.5
MERGE_ATR = 0.3
MIN_GAP_ATR = 0.2
PER_SIDE_OFFERED = 5
PER_SIDE_KEPT = 3
SWING_WING = 3
_PRIORITY = {"昨日": 0, "7日": 1, "摆动": 2, "成交": 3, "整数": 4}


@dataclass(frozen=True)
class Candidate:
    price: float
    source: str

    @property
    def rank(self) -> int:
        return _PRIORITY[self.source[:2]]


@dataclass(frozen=True)
class LevelContext:
    """某个币开会时的关键位背景：现价、参考 ATR、候选。拿不到就是空（不出关键位，也不瞎编）。"""

    price: float | None = None
    atr: float | None = None
    candidates: tuple[Candidate, ...] = ()

    def kind(self, price: float) -> LevelKind:
        assert self.price is not None
        return LevelKind.SUPPORT if price < self.price else LevelKind.RESISTANCE


def _sig(x: float, digits: int = 5) -> float:
    if x == 0:
        return 0.0
    return round(x, digits - 1 - math.floor(math.log10(abs(x))))


def nice_step(raw: float) -> float:
    """不小于 raw 的 1/2/5 × 10^k。"""
    exp = math.floor(math.log10(raw))
    for m in (1, 2, 5, 10):
        step = m * 10.0**exp
        if step >= raw:
            return step
    return 10.0 ** (exp + 1)


def swing_points(rows: Rows, wing: int = SWING_WING) -> tuple[list[float], list[float]]:
    highs, lows = [], []
    for i in range(wing, len(rows) - wing):
        win = rows[i - wing : i + wing + 1]
        wh, wl = [float(r[2]) for r in win], [float(r[3]) for r in win]
        h, lo = float(rows[i][2]), float(rows[i][3])
        if h == max(wh) and h > min(wh):  # 平的一段不算摆动点；双顶（相等的高点）算
            highs.append(h)
        if lo == min(wl) and lo < max(wl):
            lows.append(lo)
    return highs, lows


def volume_nodes(rows: Rows, width: float, top: int = 3) -> list[float]:
    """按 ``width`` 分箱累计成交量，取量最大的几箱（箱内按量加权的均价）。"""
    if width <= 0:
        return []
    bins: dict[int, list[float]] = {}
    for r in rows:
        tp = (float(r[2]) + float(r[3]) + float(r[4])) / 3
        vol = float(r[5])
        b = bins.setdefault(math.floor(tp / width), [0.0, 0.0])
        b[0] += vol
        b[1] += tp * vol
    ranked = sorted(bins.values(), key=lambda v: v[0], reverse=True)[:top]
    return [v[1] / v[0] for v in ranked if v[0] > 0]


def round_numbers(price: float, span: float) -> list[float]:
    """现价上下 ``span`` 内的整数关口：步长取 1/2/5×10^k，使窗口里只有几个。"""
    step = nice_step(span / 2)
    lo = math.ceil((price - span) / step)
    hi = math.floor((price + span) / step)
    return [k * step for k in range(lo, hi + 1) if k * step > 0]


def build_candidates(
    price: float,
    atr: float,
    hourly: Rows,
    daily: Rows,
    *,
    span_atr: float = SPAN_ATR,
) -> tuple[Candidate, ...]:
    """现价 ±span_atr×ATR 内的候选，已去重（相近的留优先级高的），按距现价从近到远。"""
    span = span_atr * atr
    raw: list[Candidate] = []
    if daily:
        last = daily[-1]
        raw += [Candidate(float(last[2]), "昨日高点"), Candidate(float(last[3]), "昨日低点")]
        raw += [
            Candidate(max(float(r[2]) for r in daily[-7:]), "7日高点"),
            Candidate(min(float(r[3]) for r in daily[-7:]), "7日低点"),
        ]
    highs, lows = swing_points(hourly)
    raw += [Candidate(h, "摆动高点") for h in highs] + [Candidate(x, "摆动低点") for x in lows]
    raw += [Candidate(_sig(v), "成交密集区") for v in volume_nodes(hourly, atr * 0.5)]
    raw += [Candidate(_sig(v), "整数关口") for v in round_numbers(price, span)]
    near = [c for c in raw if MIN_GAP_ATR * atr <= abs(c.price - price) <= span]
    kept: list[Candidate] = []
    for c in sorted(near, key=lambda c: (c.rank, abs(c.price - price))):
        if all(abs(c.price - k.price) >= MERGE_ATR * atr for k in kept):
            kept.append(c)
    return tuple(sorted(kept, key=lambda c: abs(c.price - price)))


def split_sides(
    candidates: Sequence[Candidate], price: float, limit: int
) -> tuple[list[Candidate], list[Candidate]]:
    """(支撑, 压力)，各按距现价从近到远，最多 ``limit`` 个。"""
    sup = [c for c in candidates if c.price < price][:limit]
    res = [c for c in candidates if c.price > price][:limit]
    return sup, res


def describe_candidates(ctx: LevelContext) -> str:
    """给会议提示词用的候选关键位说明。"""
    if ctx.price is None or ctx.atr is None or not ctx.candidates:
        return "候选关键位：暂无（行情数据不可用）——key_levels 请留空，不要自己编价位。"
    sup, res = split_sides(ctx.candidates, ctx.price, PER_SIDE_OFFERED)

    def fmt(cs: list[Candidate]) -> str:
        return "、".join(f"{c.price:g}（{c.source}）" for c in cs) or "无"

    return (
        f"候选关键位（代码用真实 K 线算的，现价 {ctx.price:g}，范围现价 ±{SPAN_ATR:g}×ATR 以内，"
        f"距现价从近到远）：\n支撑：{fmt(sup)}\n压力：{fmt(res)}\n"
        f"key_levels 只能从上面的候选里选，直接写候选的价格；支撑最多 {PER_SIDE_KEPT} 个、"
        f"压力最多 {PER_SIDE_KEPT} 个；候选里没有的价位不要写，更不要凭印象编价位。"
    )


def select_levels(
    wanted: Sequence[tuple[float, str]], ctx: LevelContext, *, snap_atr: float = SNAP_ATR
) -> list[KeyLevel]:
    """把会议选的价位对回候选：容差内取最近的候选（价格、类型以候选和现价为准），其余丢掉。

    返回先支撑后压力，各自距现价从近到远，每边最多 ``PER_SIDE_KEPT`` 个。
    """
    if ctx.price is None or ctx.atr is None or not ctx.candidates:
        return []
    chosen: dict[float, tuple[Candidate, str]] = {}
    for price, note in wanted:
        best = min(ctx.candidates, key=lambda c: abs(c.price - price))
        if abs(best.price - price) <= snap_atr * ctx.atr:
            chosen.setdefault(best.price, (best, note))
    sup = sorted((v for v in chosen.values() if v[0].price < ctx.price), key=lambda v: -v[0].price)
    res = sorted((v for v in chosen.values() if v[0].price > ctx.price), key=lambda v: v[0].price)
    return [
        KeyLevel(
            price=c.price,
            kind=ctx.kind(c.price),
            note=f"{c.source}；{note}" if note else c.source,
        )
        for c, note in sup[:PER_SIDE_KEPT] + res[:PER_SIDE_KEPT]
    ]
