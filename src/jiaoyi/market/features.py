"""特征引擎：逐个行情事件增量更新，每个标的一份状态，随时出 ``FeatureSnapshot``。

写法参考 buberlo/jev-trader 的 ``state/features.py``（MIT，提交 15d089e）：滚动窗口 +
按时间裁剪；改成多币种，补上 ATR、OFI、CVD、成交量放大、爆仓和资金费率。

“现在”用该标的最后一个事件的时间，不读系统时钟，所以实盘和回放算出来一样。
``values`` 里的键登记在 docs/fast_loop.md，别的模块只认那里列出的键。
"""

from __future__ import annotations

import bisect
import contextlib
import itertools
import math
import statistics
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from jiaoyi.contracts import (
    BookMetrics,
    BookTop,
    FeatureSnapshot,
    Interval,
    Kline,
    Liquidation,
    MarketEvent,
    MarkPrice,
    Side,
    Trade,
)
from jiaoyi.market import book as bk

ATR_PERIOD = 14
HISTORY_DAYS = 7
DEFAULT_ATR_MINUTES = 15
VOL_AVG_KLINES = 20
TRADE_WINDOW_S = 300.0
LIQ_WINDOW_S = 1800.0
STREAM_GAP_S = 300.0
"""这么久没收到这个币的成交 / 盘口 / 标记价就算断过（重启、重新订阅、断线），``live_s`` 从下一条
重新计时：断档前的成交已被 5 分钟窗口裁掉，``ret_5m`` 只剩断档后的几笔，读出来是 0。"""
FLOW_WINDOW_S = 60.0
BURST_WINDOW_S = 10.0
"""放量提前发现：最近这么多秒的成交额折算成每分钟，和 1 分钟均量比。"""
CLOSES_KEEP = 1500
"""留多少根 1 分钟收盘价：24 小时波动（1440）+ 4 小时涨跌。"""
VOL_24H_MINUTES = 1441
"""24 小时 1 分钟波动要的收盘价个数（启动回填至少这么多根）。"""
VOL_24H_MIN_POINTS = 60
"""1 分钟收益不够这么多个就不算 24 小时波动（刚上线的币 / 回填失败）。"""
BTC = "BTCUSDT"
FLOW_LONG_S = 3600.0
"""拍板人快照的成交流（5 分钟到 1 小时的主动买卖、大单）按秒汇总，留这么多秒。"""
BIG_TRADE_USDT = 10_000.0
"""「大单」：单笔成交额至少这么多 USDT。"""
IMB_WINDOW_S = 300.0
"""盘口失衡每秒采样一次，留 5 分钟（拍板人快照看 1 / 5 分钟平均，不看一瞬间）。"""
_TRADE_SOURCES = ("trade", "rest_trade")
SEED_JOIN_S = 10.0
"""A REST trade seed and the websocket segment count as one coverage when they are at most this
far apart: trades in that gap are in neither, so a wider join would let ``trade_s`` vouch for
a window with holes in it."""
ExtraValues = Callable[[str, datetime], Mapping[str, float]]
"""快照的额外键（持仓量、多空比等，来自 REST 轮询）：(币, 现在) → 键值。"""


class _ExactSum:
    """可加可减的精确和（Shewchuk 部分和，``math.fsum`` 同一算法）：读出的是窗口里各数真实和
    舍入一次的结果，跟加减的先后无关，进出多少次也不累积误差，窗口清空时正好是 0.0。
    非有限数单独计数，读出时按 IEEE 加法的规矩给 inf / nan。"""

    __slots__ = ("_bad", "_p")

    def __init__(self) -> None:
        self._p: list[float] = []
        self._bad: list[int] = [0, 0, 0]  # +inf、-inf、nan 的个数

    def add(self, x: float, sign: int = 1) -> None:
        if not math.isfinite(x):
            self._bad[0 if x > 0 else 1 if x < 0 else 2] += sign
            return
        if sign < 0:
            x = -x
        p = self._p
        i = 0
        for y in p:
            if abs(x) < abs(y):
                x, y = y, x
            hi = x + y
            lo = y - (hi - x)
            if lo:
                p[i] = lo
                i += 1
            x = hi
        p[i:] = [x]

    def value(self) -> float:
        pinf, ninf, nan = self._bad
        if nan or (pinf and ninf):
            return math.nan
        if pinf or ninf:
            return math.inf if pinf else -math.inf
        return math.fsum(self._p)


class _FlowWindow:
    """最近 ``span`` 秒的主动买 / 卖成交额（精确和）和笔数。

    行按成交时间排着进来，``now`` 只增不减（``last_ts`` 取的是最大值），所以过期的总在左头。"""

    __slots__ = ("buy", "rows", "sell", "span")

    def __init__(self, span: float) -> None:
        self.span = span
        self.rows: deque[tuple[float, float, bool]] = deque()
        self.buy = _ExactSum()
        self.sell = _ExactSum()

    def push(self, row: tuple[float, float, bool]) -> None:
        self.rows.append(row)
        (self.buy if row[2] else self.sell).add(row[1])

    def expire(self, now: float) -> None:
        rows, cut = self.rows, now - self.span
        while rows and rows[0][0] < cut:
            _, usdt, is_buy = rows.popleft()
            (self.buy if is_buy else self.sell).add(usdt, -1)


@dataclass
class _Flows:
    """``st.trades`` 的 60 / 30 / 10 秒窗口，增量维护。成交乱序到达、或 ``st.trades`` 被
    REST 补数改过时作废（置 None），下次读从 ``st.trades`` 重建。"""

    m1: _FlowWindow = field(default_factory=lambda: _FlowWindow(FLOW_WINDOW_S))
    s30: _FlowWindow = field(default_factory=lambda: _FlowWindow(30.0))
    s10: _FlowWindow = field(default_factory=lambda: _FlowWindow(BURST_WINDOW_S))
    last_t: float = -math.inf

    def push(self, row: tuple[float, float, bool]) -> bool:
        """接一笔；比上一笔早（乱序）返回 False，调用方作废整个窗口。"""
        if row[0] < self.last_t:
            return False
        self.last_t = row[0]
        for w in (self.m1, self.s30, self.s10):
            w.push(row)
        return True

    def expire(self, now: float) -> None:
        for w in (self.m1, self.s30, self.s10):
            w.expire(now)


@dataclass(frozen=True)
class _FlowSums:
    """``now`` 时刻 60 秒买 / 卖 / 笔数、30 秒 CVD（买和 − 卖和）、10 秒买 / 卖。"""

    buy: float
    sell: float
    n: int
    cvd_30: float
    buy_10: float
    sell_10: float

    @classmethod
    def of(cls, m1: _FlowWindow, s30: _FlowWindow, s10: _FlowWindow) -> _FlowSums:
        return cls(
            m1.buy.value(),
            m1.sell.value(),
            len(m1.rows),
            s30.buy.value() - s30.sell.value(),
            s10.buy.value(),
            s10.sell.value(),
        )


def _flow_sums_scan(trades: Iterable[tuple[float, float, bool]], now: float) -> _FlowSums:
    """不靠增量窗口、直接扫一遍算（成交乱序时用）；同样的成交和增量结果逐位相同。"""
    m1, s30, s10 = _FlowWindow(FLOW_WINDOW_S), _FlowWindow(30.0), _FlowWindow(BURST_WINDOW_S)
    for row in trades:
        t = row[0]
        if t < now - FLOW_WINDOW_S:
            continue
        m1.push(row)
        if t >= now - 30.0:
            s30.push(row)
        if t >= now - BURST_WINDOW_S:
            s10.push(row)
    return _FlowSums.of(m1, s30, s10)


@dataclass
class _AtrTrack:
    """一个周期的 ATR：1 分钟 K 线聚合成 N 分钟，每收完一根算一个 TR，Wilder 14。"""

    # 正在聚合的 N 分钟 K 线：(起点分钟, 开, 高, 低, 收, 已有几根 1 分钟)
    bar: tuple[int, float, float, float, float, int] | None = None
    prev_close: float | None = None  # 上一根完整 N 分钟 K 线的收盘价
    trs: list[float] = field(default_factory=list)
    atr: float | None = None


@dataclass
class _SymbolState:
    # (秒级时间戳, 该秒最后成交价)，最多 5 分钟
    prices: deque[tuple[float, float]] = field(default_factory=deque)
    # (时间戳, 名义价值 USDT, 是否主动买)
    trades: deque[tuple[float, float, bool]] = field(default_factory=deque)
    # (时间戳, 名义价值 USDT, 被强平的是多头吗)
    liqs: deque[tuple[float, float, bool]] = field(default_factory=deque)
    flows: _Flows | None = field(default_factory=_Flows)
    # (时间戳, OFI 名义价值)
    ofi: deque[tuple[float, float]] = field(default_factory=deque)
    ofi_sum: _ExactSum = field(default_factory=_ExactSum)
    klines: deque[Kline] = field(default_factory=lambda: deque(maxlen=60))
    # (开盘分钟, 开, 高, 低, 收, 成交量)：拍板人快照的技术面块（涨跌、RSI、区间、VWAP）
    bars: deque[tuple[int, float, float, float, float, float]] = field(
        default_factory=lambda: deque(maxlen=CLOSES_KEEP)
    )
    # 按秒汇总的成交：[秒, 主动买 U, 主动卖 U, 大单买笔数, 大单卖笔数, 大单 U]，留 1 小时
    flow: deque[list[float]] = field(default_factory=deque)
    # (秒, 盘口失衡)，每秒一个点，留 5 分钟
    imbs: deque[tuple[float, float]] = field(default_factory=deque)
    # (开盘分钟, 收盘价)：算 15 分钟到 4 小时涨跌、24 小时 1 分钟波动
    closes: deque[tuple[int, float]] = field(default_factory=lambda: deque(maxlen=CLOSES_KEEP))
    vol_24h: float | None = None  # 过去 24 小时 1 分钟对数收益的标准差
    vol_dirty: bool = False  # 收了新线还没重算（用到时才算：回填 1500 根时不用算 1500 遍）
    short: _AtrTrack = field(default_factory=_AtrTrack)
    long: _AtrTrack = field(default_factory=_AtrTrack)  # 没配长周期就一直空着
    # 最近 7 天每根 N 分钟线收完时的 (收盘时间戳, ATR)（求中位数给止损 ATR 当下限）
    atr_hist: deque[tuple[float, float]] = field(default_factory=deque)
    atr_med: float | None = None  # atr_hist 的中位数缓存，atr_hist 一变就清
    last_price: float | None = None
    last_ts: float = 0.0
    last_price_source: str | None = None
    last_price_source_ts: float | None = None
    last_price_observed_ts: float | None = None
    book_observed_ts: float | None = None
    mark_observed_ts: float | None = None
    book: BookTop | None = None
    mark: MarkPrice | None = None
    live_since: float | None = None  # 这一段连续实时行情从哪一秒开始
    live_last: float | None = None
    # REST 补的成交覆盖 [cov_since, cov_last]（只在 rest_warm_start 开时有）
    cov_since: float | None = None
    cov_last: float | None = None
    rest_until: float | None = None  # REST 补到的最后一笔成交时间：实时流再推来的同一笔不重复记
    rest_last: float | None = None  # REST 补进来的最新一个点（成交或标记价）的时间


@dataclass(frozen=True)
class EntryContext:
    """``RollingFeatureEngine.entry_context`` 的结果：拍板人快照用的原始数据。"""

    now: float
    """该币最后一个事件的时间戳（秒）。"""
    price: float
    bars: list[tuple[int, float, float, float, float, float]]
    """已收盘的 1 分钟线 (开盘分钟, 开, 高, 低, 收, 成交量)，最多约 25 小时，从早到晚。"""
    btc_bars: list[tuple[int, float, float, float, float, float]]
    flow: dict[str, float]
    """``cvd_5m`` / ``cvd_15m`` / ``cvd_1h``（U）、``buy_*``（主动买占比）、15 分钟大单
    ``big_buy_n`` / ``big_sell_n`` / ``big_pct``（占成交额 %）、
    ``liq_15m_long`` / ``liq_15m_short``（U）。"""
    imb: dict[str, float]
    """盘口失衡 ``1m`` / ``5m`` 平均；没盘口就空。"""


def _first(row: tuple[float, Any]) -> float:
    return row[0]


def _ts(dt: datetime) -> float:
    return dt.timestamp()


def _finite_observation(value: float | None) -> float | None:
    return value if value is not None and math.isfinite(value) else None


def _observation_iso(value: float | None) -> str | None:
    if _finite_observation(value) is None:
        return None
    try:
        assert value is not None
        return datetime.fromtimestamp(value, UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _observation_times(
    source: float | None, received: float | None, observed: float | None
) -> dict[str, Any]:
    source, received = _finite_observation(source), _finite_observation(received)
    return {
        "source_ts": _observation_iso(source),
        "observed_at": _observation_iso(received),
        "age_s": observed - source if observed is not None and source is not None else None,
        "source_after_update_observation": (
            source > received if source is not None and received is not None else None
        ),
        "source_after_capture": (
            source > observed if source is not None and observed is not None else None
        ),
    }


def _return_observation(st: _SymbolState, seconds: float) -> dict[str, Any]:
    target = st.last_ts - seconds
    result: dict[str, Any] = {
        "nominal_window_s": seconds,
        "reference_ts": _observation_iso(st.last_ts),
        "reference_clock": "latest_mixed_market_event",
        "target_ts": _observation_iso(target),
        "anchor_bucket_ts": None,
        "anchor_price": None,
        "actual_lookback_s": None,
        "anchor_gap_s": None,
        "history_span_s": None,
        "last_bucket_age_s": None,
        "max_bucket_gap_s": None,
        "warmup_complete": False,
        "used_first_available": None,
        "source_bucket_precision_s": 1,
        "sample_count": 0,
        "chronological": None,
        "coverage_status": "no_trade_price_history",
    }
    if not st.prices or not math.isfinite(st.last_ts):
        return result
    prices = list(st.prices)
    if not all(math.isfinite(t) and math.isfinite(px) for t, px in prices):
        result["coverage_status"] = "nonfinite_history"
        return result
    chronological = all(a[0] <= b[0] for a, b in itertools.pairwise(prices))
    index = bisect.bisect_right(prices, (target, math.inf)) - 1
    anchor_ts, anchor_price = prices[max(index, 0)]
    window = prices[max(index, 0) :]
    result.update(
        anchor_bucket_ts=_observation_iso(anchor_ts),
        anchor_price=anchor_price,
        actual_lookback_s=st.last_ts - anchor_ts,
        anchor_gap_s=target - anchor_ts,
        history_span_s=st.last_ts - min(t for t, _ in prices),
        last_bucket_age_s=st.last_ts - prices[-1][0],
        max_bucket_gap_s=max((b[0] - a[0] for a, b in itertools.pairwise(window)), default=None),
        warmup_complete=chronological and index >= 0,
        used_first_available=index < 0,
        sample_count=len(window),
        chronological=chronological,
        coverage_status=(
            "unordered_history"
            if not chronological
            else "target_reached_not_continuity_proof"
            if index >= 0
            else "insufficient_history"
        ),
    )
    return result


def backfill_minutes(atr_period_min: int, atr_long_period_min: int = 0) -> int:
    """攒够 ATR 要回填多少根 1 分钟 K 线（有长周期按长的算）。

    14 个 TR 要 15 根完整的 N 分钟线，再加当前没走完的一根。
    """
    return (ATR_PERIOD + 2) * max(atr_period_min, atr_long_period_min)


def _on_bar(tr: _AtrTrack, high: float, low: float, close: float) -> None:
    prev = tr.prev_close
    tr.prev_close = close
    if prev is None:
        return
    x = max(high - low, abs(high - prev), abs(low - prev))
    if tr.atr is None:
        tr.trs.append(x)
        if len(tr.trs) >= ATR_PERIOD:
            tr.atr = sum(tr.trs[-ATR_PERIOD:]) / ATR_PERIOD
            tr.trs.clear()
    else:
        tr.atr = (tr.atr * (ATR_PERIOD - 1) + x) / ATR_PERIOD


def _vol_1m(closes: deque[tuple[int, float]]) -> float | None:
    """最近 24 小时相邻 1 分钟收盘价的对数收益标准差；点太少返回 None。"""
    pts = list(closes)[-VOL_24H_MINUTES:]
    rets = [math.log(b / a) for (_, a), (_, b) in itertools.pairwise(pts) if a > 0 and b > 0]
    if len(rets) < VOL_24H_MIN_POINTS:
        return None
    mean = sum(rets) / len(rets)
    return math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1))


def _ret_minutes(st: _SymbolState, now: float, minutes: int) -> float | None:
    """现价相对 ``minutes`` 分钟前那根 1 分钟线收盘价的涨跌。

    那时候的线缺了（差 5 分钟以上）返回 None。"""
    if st.last_price is None or not st.closes:
        return None
    target = int(now // 60) - minutes
    i = bisect.bisect_right(st.closes, (target, math.inf)) - 1
    if i < 0:
        return None
    minute, close = st.closes[i]
    if target - minute > 5 or close <= 0:
        return None
    return st.last_price / close - 1.0


def _move_z(st: _SymbolState, now: float, minutes: int) -> float | None:
    """``minutes`` 分钟涨跌 ÷（24 小时 1 分钟波动 × √分钟数），带正负号。"""
    r = _ret_minutes(st, now, minutes)
    vol = _vol_24h(st)
    if r is None or not vol:
        return None
    return round(r / (vol * math.sqrt(minutes)), 3)


def _vol_24h(st: _SymbolState) -> float | None:
    if st.vol_dirty:
        st.vol_24h, st.vol_dirty = _vol_1m(st.closes), False
    return st.vol_24h


def _mark_live(st: _SymbolState, t: float) -> None:
    if st.live_last is None or t - st.live_last > STREAM_GAP_S:
        st.live_since = t
    st.live_last = t if st.live_last is None else max(st.live_last, t)


def wilder_atr(bars: Iterable[tuple[float, float, float]]) -> float | None:
    """完整 N 分钟线 ``(高, 低, 收)``（升序）→ 同实时引擎算法的 ATR；不够 15 根为 None。"""
    tr = _AtrTrack()
    for high, low, close in bars:
        _on_bar(tr, high, low, close)
    return tr.atr


def trade_warm_s(v: Mapping[str, float]) -> float | None:
    """成交窗口的特征（涨跌、波动、主动买卖）连续了几秒：实时流和 REST 补的覆盖取长的。

    ``trade_s`` 只在 ``rest_warm_start`` 补过 REST 时有；爆仓没有 REST 历史，强平瀑布反弹仍只看
    ``live_s``。"""
    live, rest = v.get("live_s"), v.get("trade_s")
    if live is None:
        return rest
    return live if rest is None else max(live, rest)


class RollingFeatureEngine:
    """``FeatureEngine`` 的实现。只认 1 分钟 K 线，其它周期忽略。

    ATR 用 1 分钟 K 线聚合成 ``atr_period_min`` 分钟（按 UTC 整点对齐，例如 15 分钟线从 :00、:15
    开始），每收完一根算一个 TR，Wilder 平滑 14 根。不完整的 N 分钟线（启动回填的第一段）丢掉。
    ``atr_long_period_min`` 不为 0 时同样再算一个长周期 ATR（风控员止损用两者的几何平均）。
    均量 ``vol_ratio_1m`` 仍按 1 分钟 K 线算。
    """

    def __init__(
        self,
        depth_levels: int = 5,
        *,
        atr_period_min: int = DEFAULT_ATR_MINUTES,
        atr_long_period_min: int = 0,
        oi_window_min: int = 240,
        observation_wall: Callable[[], datetime] | None = None,
    ) -> None:
        """``oi_window_min``：持仓量变化信号的窗口，按它多算一个 ``ret_oiw``（同窗口价格涨跌）。"""
        if atr_period_min < 1:
            raise ValueError("atr_period_min 至少 1 分钟")
        if 0 < atr_long_period_min <= atr_period_min:
            raise ValueError("atr_long_period_min 必须比 atr_period_min 长")
        self._levels = depth_levels
        self._atr_min = atr_period_min
        self._long_min = atr_long_period_min
        self._hist_len = HISTORY_DAYS * 1440 // atr_period_min
        self._oi_min = oi_window_min
        self._s: dict[str, _SymbolState] = {}
        self._extra: ExtraValues | None = None
        self._observation_wall = observation_wall or (lambda: datetime.now(UTC))

    def set_depth_levels(self, levels: int) -> None:
        """盘口深度档数热更新（设置 ``book_depth_levels``）：下一次算盘口就按新档数求和。"""
        if not 1 <= levels <= 20:
            raise ValueError("盘口深度档数要在 1–20 之间")
        self._levels = levels

    def set_extra(self, extra: ExtraValues | None) -> None:
        """快照里并进的额外键（持仓量、多空比，见 ``market.derivs``）。拿不到的键不写。"""
        self._extra = extra

    def _state(self, symbol: str) -> _SymbolState:
        st = self._s.get(symbol)
        if st is None:
            st = self._s[symbol] = _SymbolState(atr_hist=deque(maxlen=self._hist_len))
        return st

    def update(self, event: MarketEvent) -> None:
        st = self._state(event.symbol)
        if isinstance(event, Trade | BookTop | MarkPrice):
            _mark_live(st, _ts(event.ts))
        if isinstance(event, Trade):
            if self._on_trade(st, event):
                st.last_price_source = "trade"
                st.last_price_source_ts = _ts(event.ts)
                st.last_price_observed_ts = self._observation_update_ts()
        elif isinstance(event, BookTop):
            self._on_book(st, event)
            st.book_observed_ts = self._observation_update_ts()
        elif isinstance(event, Liquidation):
            t = _ts(event.ts)
            st.liqs.append((t, event.price * event.qty, event.side == Side.LONG))
            st.last_ts = max(st.last_ts, t)
        elif isinstance(event, MarkPrice):
            st.mark = event
            st.last_ts = max(st.last_ts, _ts(event.ts))
            st.mark_observed_ts = self._observation_update_ts()
        elif (
            isinstance(event, Kline)
            and self._on_kline(st, event)
            and st.last_price_source not in _TRADE_SOURCES
        ):
            st.last_price_source = "kline_close_bootstrap"
            st.last_price_source_ts = _ts(event.close_time)
            st.last_price_observed_ts = self._observation_update_ts()
        self._prune(st)

    def _observation_update_ts(self) -> float | None:
        try:
            observed = self._observation_wall()
            if observed.tzinfo is None or observed.utcoffset() is None:
                return None
            return _finite_observation(_ts(observed))
        except Exception:
            return None

    # ------------------------------------------------------------------ 输入

    def _on_trade(self, st: _SymbolState, ev: Trade) -> bool:
        """收下返回 True；REST 已经补过的同一笔返回 False。"""
        t = _ts(ev.ts)
        since, until = st.cov_since, st.rest_until
        if since is not None and until is not None and since <= t <= until:
            return False
        sec = math.floor(t)
        if st.prices and st.prices[-1][0] == sec:
            st.prices[-1] = (sec, ev.price)
        else:
            st.prices.append((sec, ev.price))
        usdt, is_buy = ev.price * ev.qty, ev.taker_side == Side.LONG
        row = (t, usdt, is_buy)
        st.trades.append(row)
        if st.flows is not None and not st.flows.push(row):
            st.flows = None
        if not st.flow or st.flow[-1][0] != sec:
            st.flow.append([float(sec), 0.0, 0.0, 0.0, 0.0, 0.0])
        b = st.flow[-1]
        b[1 if is_buy else 2] += usdt
        if usdt >= BIG_TRADE_USDT:
            b[3 if is_buy else 4] += 1.0
            b[5] += usdt
        st.last_price = ev.price
        st.last_ts = max(st.last_ts, t)
        return True

    def _on_book(self, st: _SymbolState, ev: BookTop) -> None:
        t = _ts(ev.ts)
        if st.book is not None:
            e = bk.ofi_event(st.book, ev) * bk.mid(ev)
            st.ofi.append((t, e))
            st.ofi_sum.add(e)
        st.book = ev
        sec = math.floor(t)
        if not st.imbs or st.imbs[-1][0] != sec:
            st.imbs.append((float(sec), bk.imbalance(ev, self._levels)))
        st.last_ts = max(st.last_ts, t)

    def _on_kline(self, st: _SymbolState, k: Kline) -> bool:
        """收下返回 True；别的周期、重复或乱序的返回 False。"""
        if k.interval != Interval.M1:
            return False
        if st.klines and k.open_time <= st.klines[-1].open_time:
            return False  # 重复或乱序（例如回填和推送重叠）
        st.klines.append(k)
        minute = int(k.open_time.timestamp() // 60)
        st.closes.append((minute, k.close))
        st.bars.append((minute, k.open, k.high, k.low, k.close, k.volume))
        st.vol_dirty = True
        end = self._aggregate(st.short, k, self._atr_min)
        if end is not None and st.short.atr is not None:
            st.atr_hist.append((end, st.short.atr))
            st.atr_med = None
        if self._long_min:
            self._aggregate(st.long, k, self._long_min)
        # 还没来过成交就跟最新收盘价：回填从 24 小时前往后喂，只取第一根会把一天前的价当现价
        if st.last_price_source not in _TRADE_SOURCES:
            st.last_price = k.close
        st.last_ts = max(st.last_ts, _ts(k.close_time))
        return True

    @staticmethod
    def _aggregate(tr: _AtrTrack, k: Kline, n: int) -> float | None:
        """喂一根 1 分钟线；收完一根完整 N 分钟线时返回它的收盘时间戳。"""
        minute = int(k.open_time.timestamp() // 60)
        start = minute - minute % n
        if tr.bar is not None and tr.bar[0] != start:
            tr.bar = None  # 上一根没收齐（中间缺数据或回填起点），丢掉
        if tr.bar is None:
            if minute != start:
                return None  # 从 N 分钟线中间开始的，等下一根整点
            tr.bar = (start, k.open, k.high, k.low, k.close, 1)
        else:
            s, o, h, lo, _, cnt = tr.bar
            tr.bar = (s, o, max(h, k.high), min(lo, k.low), k.close, cnt + 1)
        if minute != start + n - 1:
            return None
        _, _, h, lo, c, cnt = tr.bar  # 这根 N 分钟线收完了
        tr.bar = None
        if cnt != n:
            return None
        _on_bar(tr, h, lo, c)
        return _ts(k.close_time)

    def seed_atr_history(self, symbol: str, bars: list[Kline]) -> int:
        """用 REST 拉的 ``atr_period_min`` 分钟 K 线（已收盘、按时间排）补 7 天 ATR 历史。

        按同样的 TR + Wilder 14 算出每根收完时的 ATR，替换掉这段时间里已有的点（同一个币重新
        进订阅集再补一次不会重复），之后实时攒的接在后面。只用来求中位数，不动当前 ATR。
        返回补了几个点。
        """
        series: list[tuple[float, float]] = []
        trs: list[float] = []
        atr: float | None = None
        prev: float | None = None
        for k in bars:
            if prev is not None:
                tr = max(k.high - k.low, abs(k.high - prev), abs(k.low - prev))
                if atr is None:
                    trs.append(tr)
                    if len(trs) >= ATR_PERIOD:
                        atr = sum(trs[-ATR_PERIOD:]) / ATR_PERIOD
                else:
                    atr = (atr * (ATR_PERIOD - 1) + tr) / ATR_PERIOD
                if atr is not None:
                    series.append((_ts(k.close_time), atr))
            prev = k.close
        st = self._state(symbol)
        last = series[-1][0] if series else float("-inf")
        live = [p for p in st.atr_hist if p[0] > last]
        st.atr_hist.clear()
        st.atr_hist.extend(series + live)  # maxlen 自动丢掉最老的
        st.atr_med = None
        return len(series)

    def seed_rest(
        self,
        symbol: str,
        trades: list[Trade],
        mark: MarkPrice | None,
        *,
        covered_from: float,
        fetched_at: float,
    ) -> None:
        """REST 拉的逐笔归集成交（``/fapi/v1/aggTrades``）和标记价并进这个币的状态。

        ``trades`` 是 ``covered_from`` 到 ``fetched_at`` 之间的全部成交（按时间排）。只补实时流
        没有的时间段：比已有最早一笔早的、比已有最晚一笔晚的；中间那段以实时流为准。补进来的不算
        实时行情（``live_s`` 不动），只给 ``trade_s``：成交窗口从哪一秒起是全的。标记价比已有的新
        才换。爆仓没有 REST 历史，这里不碰。"""
        st = self._state(symbol)
        rows = sorted(((_ts(tr.ts), tr) for tr in trades if tr.symbol == symbol), key=_first)
        self._drop_before_segment(st)
        # 成交 5 分钟、逐秒价 301 秒、成交流 1 小时各留各的：边界按三者里最早 / 最晚的算，免得补进
        # 成交流已有的秒（重复记）或插到比它新的秒前面（乱序）。逐秒的桶只知道是哪一秒，同一秒的不补
        heads = [d[0][0] for d in (st.trades, st.prices, st.flow) if d]
        tails = [st.trades[-1][0]] if st.trades else []
        tails += [d[-1][0] + 1.0 for d in (st.prices, st.flow) if d]
        had = bool(heads)
        first = min(heads, default=math.inf)
        last = max(tails, default=-math.inf)
        before = [tr for t, tr in rows if t < first]
        after = [tr for t, tr in rows if t > last] if had else []
        self._prepend(st, before)
        for tr in after:
            self._on_trade(st, tr)
        if rows and (after or (before and not had)):
            newest_t, newest = rows[-1]
            st.last_price = newest.price
            st.last_price_source = "rest_trade"
            st.last_price_source_ts = newest_t
            st.last_price_observed_ts = self._observation_update_ts()
            st.last_ts = max(st.last_ts, newest_t)
        if rows:
            st.rest_until = max(st.rest_until or -math.inf, rows[-1][0])
            st.rest_last = max(st.rest_last or -math.inf, rows[-1][0])
        if mark is not None and mark.symbol == symbol:
            if st.mark is None or mark.ts > st.mark.ts:
                st.mark = mark
            st.last_ts = max(st.last_ts, _ts(mark.ts))
            st.rest_last = max(st.rest_last or -math.inf, _ts(mark.ts))
        self._cover(st, covered_from, fetched_at)
        self._prune(st)

    def _drop_before_segment(self, st: _SymbolState) -> None:
        """丢掉当前这段连续行情（实时流 + 接得上的 REST 覆盖）之前的成交、逐秒价和成交流。

        币退订后一小时内又订上（或断流 5 分钟以上重连）：成交流还留着上一段的秒，拿它当已有
        数据的起点，最近 5 分钟的 REST 成交会被当成「中间那段」整段丢掉，覆盖却照样接上，
        ``ret_5m`` 读 0（#390 同一类假信号）。"""
        span = self._span(st)
        start = span[0] if span is not None else st.live_since
        if start is None:
            return
        sec = math.floor(start)
        while st.trades and st.trades[0][0] < start:
            st.trades.popleft()
            st.flows = None
        for dq in (st.prices, st.flow):
            while dq and dq[0][0] < sec:
                dq.popleft()

    @staticmethod
    def _prepend(st: _SymbolState, trades: list[Trade]) -> None:
        """比已有最早一笔还早的成交按时间插到前面（同一秒以已有的为准 / 成交额并进去）。"""
        if not trades:
            return
        prices: list[tuple[float, float]] = []
        flow: list[list[float]] = []
        rows: list[tuple[float, float, bool]] = []
        for tr in trades:
            t = _ts(tr.ts)
            sec = float(math.floor(t))
            if prices and prices[-1][0] == sec:
                prices[-1] = (sec, tr.price)
            else:
                prices.append((sec, tr.price))
            usdt, is_buy = tr.price * tr.qty, tr.taker_side == Side.LONG
            rows.append((t, usdt, is_buy))
            if not flow or flow[-1][0] != sec:
                flow.append([sec, 0.0, 0.0, 0.0, 0.0, 0.0])
            b = flow[-1]
            b[1 if is_buy else 2] += usdt
            if usdt >= BIG_TRADE_USDT:
                b[3 if is_buy else 4] += 1.0
                b[5] += usdt
        if st.prices and prices[-1][0] == st.prices[0][0]:
            prices.pop()
        if st.flow and flow[-1][0] == st.flow[0][0]:
            for i in range(1, 6):
                st.flow[0][i] += flow[-1][i]
            flow.pop()
        st.prices.extendleft(reversed(prices))
        st.flow.extendleft(reversed(flow))
        st.trades.extendleft(reversed(rows))
        st.flows = None

    @staticmethod
    def _cover(st: _SymbolState, since: float, last: float) -> None:
        """[since, last] 并进成交覆盖：和已有覆盖或这段实时流挨着（``SEED_JOIN_S`` 内）就连起来。"""
        segs = [(since, last)]
        if st.cov_since is not None and st.cov_last is not None:
            segs.append((st.cov_since, st.cov_last))
        if st.live_since is not None and st.live_last is not None:
            segs.append((st.live_since, st.live_last))
        segs.sort()
        merged: list[tuple[float, float]] = []
        for a, b in segs:
            if merged and a <= merged[-1][1] + SEED_JOIN_S:
                merged[-1] = (merged[-1][0], max(merged[-1][1], b))
            else:
                merged.append((a, b))
        st.cov_since, st.cov_last = merged[-1]

    def forget_rest(self) -> None:
        """``rest_warm_start`` 热关掉时调：已补的覆盖不再算预热（``trade_s`` 消失），回到只看
        ``live_s``；已经并进来的成交本身是真的，留着。"""
        for st in self._s.values():
            st.cov_since = st.cov_last = st.rest_until = st.rest_last = None

    def trade_cover(self, symbol: str) -> tuple[float, float] | None:
        """(起, 止)：这个币的成交窗口从哪一秒到哪一秒是全的；什么都没有返回 None。"""
        st = self._s.get(symbol)
        if st is None:
            return None
        span = self._span(st)
        if span is None and st.live_since is not None and st.live_last is not None:
            return st.live_since, st.live_last
        return span

    def data_source(self, symbol: str) -> str | None:
        """最新一个点是实时流（``ws``）还是 REST 补的（``rest``）；都没有返回 None。"""
        st = self._s.get(symbol)
        if st is None:
            return None
        if st.rest_last is not None and (st.live_last is None or st.rest_last > st.live_last):
            return "rest"
        return "ws" if st.live_last is not None else None

    @staticmethod
    def _span(st: _SymbolState) -> tuple[float, float] | None:
        if st.cov_since is None or st.cov_last is None:
            return None
        start, end = st.cov_since, st.cov_last
        if st.live_since is not None and st.live_last is not None:
            if st.live_since <= end + SEED_JOIN_S:
                end = max(end, st.live_last)
            else:
                start, end = st.live_since, st.live_last
        return start, end

    def _trade_s(self, st: _SymbolState, now: float) -> float | None:
        """成交窗口（``ret_1m`` / ``ret_5m`` / 波动 / 主动买卖）从现在往回有几秒是全的。

        没补过 REST 返回 None（只有 ``live_s``）。实时流在覆盖之后断过又重来，只算新这段。"""
        span = self._span(st)
        if span is None or now - span[1] > STREAM_GAP_S:
            return None
        start = span[0]
        return max(now - start, 0.0)

    def atr_median_7d(self, symbol: str) -> float | None:
        """最近 7 天 ATR 的中位数；不够 1 天的点数返回 None（不拿半天的数据当下限）。"""
        st = self._s.get(symbol)
        if st is None or len(st.atr_hist) < 1440 // self._atr_min:
            return None
        if st.atr_med is None:
            st.atr_med = statistics.median(a for _, a in st.atr_hist)
        return st.atr_med

    def _prune(self, st: _SymbolState) -> None:
        now = st.last_ts
        for dq, window in (
            (st.prices, TRADE_WINDOW_S + 1),
            (st.trades, TRADE_WINDOW_S),
            (st.liqs, LIQ_WINDOW_S),
            (st.flow, FLOW_LONG_S),
            (st.imbs, IMB_WINDOW_S),
        ):
            while dq and dq[0][0] < now - window:
                dq.popleft()
        while st.ofi and st.ofi[0][0] < now - FLOW_WINDOW_S:
            st.ofi_sum.add(st.ofi.popleft()[1], -1)

    @staticmethod
    def _flow_sums(st: _SymbolState, now: float) -> _FlowSums:
        if st.flows is None:
            rows = [r for r in st.trades if r[0] >= now - FLOW_WINDOW_S]
            if any(a[0] > b[0] for a, b in itertools.pairwise(rows)):
                return _flow_sums_scan(rows, now)  # 窗口里还有乱序的成交：这次照扫
            st.flows = _Flows()
            for r in rows:
                st.flows.push(r)
        f = st.flows
        f.expire(now)
        return _FlowSums.of(f.m1, f.s30, f.s10)

    # ------------------------------------------------------------------ 输出

    def coverage(self, now: datetime) -> tuple[int, int]:
        """（盘口 10 秒内更新过的币数，ATR 已攒够的币数），给团队页「盘口员」卡用。"""
        t = _ts(now)
        books = sum(1 for st in self._s.values() if st.book and t - _ts(st.book.ts) <= 10.0)
        ready = sum(1 for st in self._s.values() if st.short.atr is not None)
        return books, ready

    def snapshot(self, symbol: str) -> FeatureSnapshot | None:
        st = self._s.get(symbol)
        if st is None or st.last_price is None or st.short.atr is None:
            return None  # 还没攒够 15 根 N 分钟 K 线（回填 backfill_minutes 根 1 分钟线就够）
        now = st.last_ts
        values = self._values(st, now)
        btc = self._s.get(BTC)
        if btc is not None and btc.last_price is not None:
            z = _move_z(btc, btc.last_ts, 5)
            if z is not None:
                values["btc5z"] = z
        if self._extra is not None:
            # 额外数据只是参考，出错不能让快照（和信号、管家）跟着挂
            with contextlib.suppress(Exception):
                values.update(self._extra(symbol, datetime.fromtimestamp(now, UTC)))
        return FeatureSnapshot(
            symbol=symbol,
            ts=datetime.fromtimestamp(now, UTC),
            last_price=st.last_price,
            atr=st.short.atr,
            atr_period_min=self._atr_min,
            atr_long=st.long.atr if self._long_min else None,
            atr_long_period_min=self._long_min or None,
            atr_median_7d=self.atr_median_7d(symbol),
            values=values,
            book=self._book_metrics(st, now),
        )

    def diagnostic_observation(self, symbol: str, observed_at: datetime) -> dict[str, Any]:
        """只读诊断；update 的观察时间不是 WebSocket 接收时间，不进入模型快照。"""
        observed = (
            _finite_observation(_ts(observed_at))
            if observed_at.tzinfo is not None and observed_at.utcoffset() is not None
            else None
        )
        st = self._s.get(symbol)
        result: dict[str, Any] = {
            "symbol": symbol,
            "observed_at": _observation_iso(observed),
            "feature_ts": _observation_iso(st.last_ts) if st is not None else None,
            "feature_clock": "latest_mixed_market_event",
            "source_observed_clock": "python_feature_update_not_websocket_receive",
            "price": None,
            "mark": None,
            "book": None,
            "returns": {},
        }
        if st is None:
            result["status"] = "unknown_symbol"
            return result
        result["status"] = "observed"
        if st.last_price is not None:
            result["price"] = {
                "value": _finite_observation(st.last_price),
                "source": st.last_price_source or "unknown",
                **_observation_times(st.last_price_source_ts, st.last_price_observed_ts, observed),
            }
        if st.mark is not None:
            result["mark"] = {
                "value": _finite_observation(st.mark.mark_price),
                "source": "mark",
                **_observation_times(_ts(st.mark.ts), st.mark_observed_ts, observed),
            }
        if st.book is not None:
            result["book"] = {
                "bid": _finite_observation(st.book.bids[0][0]),
                "bid_qty": _finite_observation(st.book.bids[0][1]),
                "ask": _finite_observation(st.book.asks[0][0]),
                "ask_qty": _finite_observation(st.book.asks[0][1]),
                "source": "book_top_reference_not_fill",
                **_observation_times(_ts(st.book.ts), st.book_observed_ts, observed),
            }
        result["returns"] = {
            name: _return_observation(st, seconds)
            for name, seconds in (("ret1m", 60.0), ("ret5m", 300.0))
        }
        return result

    def entry_context(self, symbol: str) -> EntryContext | None:
        """拍板人快照要的原始数据（技术面、长一点的成交流、盘口失衡平均）。没见过这个币返回 None。

        只读内存，不发请求；算法在 ``jiaoyi.gate.context``。"""
        st = self._s.get(symbol)
        if st is None or st.last_price is None:
            return None
        now = st.last_ts
        flow: dict[str, float] = {}
        for label, secs in (("5m", 300.0), ("15m", 900.0), ("1h", 3600.0)):
            buy = sell = 0.0
            for b in st.flow:
                if b[0] >= now - secs:
                    buy += b[1]
                    sell += b[2]
            flow[f"cvd_{label}"] = buy - sell
            if buy + sell > 0:
                flow[f"buy_{label}"] = buy / (buy + sell)
        big_b = big_s = big_u = tot = 0.0
        for b in st.flow:
            if b[0] >= now - 900.0:
                big_b, big_s, big_u, tot = (
                    big_b + b[3],
                    big_s + b[4],
                    big_u + b[5],
                    tot + b[1] + b[2],
                )
        flow["big_buy_n"], flow["big_sell_n"] = big_b, big_s
        if tot > 0:
            flow["big_pct"] = big_u / tot * 100.0
        liq_l = sum(u for t, u, was_long in st.liqs if t >= now - 900.0 and was_long)
        liq_s = sum(u for t, u, was_long in st.liqs if t >= now - 900.0 and not was_long)
        flow["liq_15m_long"], flow["liq_15m_short"] = liq_l, liq_s
        imb: dict[str, float] = {}
        for label, secs in (("1m", 60.0), ("5m", 300.0)):
            pts = [x for t, x in st.imbs if t >= now - secs]
            if pts:
                imb[label] = sum(pts) / len(pts)
        btc = self._s.get(BTC)
        return EntryContext(
            now=now,
            price=st.last_price,
            bars=list(st.bars),
            btc_bars=list(btc.bars) if btc is not None and symbol != BTC else list(st.bars),
            flow=flow,
            imb=imb,
        )

    def book_age_s(self, symbol: str, now: datetime) -> float | None:
        """最近一次盘口更新距 ``now`` 几秒；没收到过盘口返回 None（面板看盘口员是否在线）。"""
        st = self._s.get(symbol)
        if st is None or st.book is None:
            return None
        return max(0.0, now.timestamp() - _ts(st.book.ts))

    def symbols(self) -> list[str]:
        return sorted(self._s)

    def price_at(self, symbol: str, t: datetime) -> tuple[float, datetime, str] | None:
        """Mainnet price at ``t`` from what the stream holds: the last trade of that second
        while the five-minute trade window covers it (``ws_trade``), else open-to-close
        interpolation inside the closed 1-minute bar (``ws_kline_1m``); None if neither."""
        st = self._s.get(symbol)
        if st is None:
            return None
        ts = _ts(t)
        if st.prices and st.prices[0][0] <= ts <= st.last_ts + 1.0:
            sec, px = st.prices[max(bisect.bisect_right(st.prices, (ts, math.inf)) - 1, 0)]
            return px, datetime.fromtimestamp(sec, UTC), "ws_trade"
        minute = int(ts // 60)
        for m, o, _h, _l, c, _v in reversed(st.bars):
            if m < minute:
                break
            if m == minute:
                frac = min(max(ts / 60.0 - minute, 0.0), 1.0)
                return o + (c - o) * frac, datetime.fromtimestamp(minute * 60, UTC), "ws_kline_1m"
        return None

    def prices_between(
        self, symbol: str, start: datetime, end: datetime
    ) -> list[tuple[datetime, float]]:
        """Per-second last mainnet trade prices in [start, end] still in the trade window."""
        st = self._s.get(symbol)
        if st is None:
            return []
        a, b = _ts(start), _ts(end)
        return [
            (datetime.fromtimestamp(sec, UTC), px) for sec, px in st.prices if a - 1.0 < sec <= b
        ]

    def _price_at(self, st: _SymbolState, t: float) -> float | None:
        if not st.prices:
            return None
        i = bisect.bisect_right(st.prices, (t, math.inf)) - 1
        return st.prices[max(i, 0)][1]

    def _values(self, st: _SymbolState, now: float) -> dict[str, float]:
        assert st.last_price is not None
        v: dict[str, float] = {}
        for name, secs in (("ret_1m", 60.0), ("ret_5m", 300.0)):
            past = self._price_at(st, now - secs)
            if past:
                v[name] = st.last_price / past - 1.0

        f = self._flow_sums(st, now)
        buy, sell, buy_10, sell_10 = f.buy, f.sell, f.buy_10, f.sell_10
        v["vol_1m_usdt"] = buy + sell
        v["trades_1m"] = float(f.n)
        if st.live_since is not None:
            v["live_s"] = max(now - st.live_since, 0.0)
        trade_s = self._trade_s(st, now)
        if trade_s is not None:
            v["trade_s"] = trade_s
        v["taker_buy_ratio_1m"] = buy / (buy + sell) if buy + sell > 0 else 0.5
        v["cvd_1m_usdt"] = buy - sell
        v["cvd_30s_usdt"] = f.cvd_30

        recent = list(st.klines)[-VOL_AVG_KLINES:]
        if len(recent) >= 5:
            avg = sum(k.quote_volume for k in recent) / len(recent)
            if avg > 0:
                v["vol_ratio_1m"] = (buy + sell) / avg
                # 最近 10 秒折算成每分钟：放量开头几秒就能看出来，不用等满 1 分钟
                v["vol_ratio_10s"] = (buy_10 + sell_10) * (60.0 / BURST_WINDOW_S) / avg
        if buy_10 + sell_10 > 0:
            v["taker_buy_ratio_10s"] = buy_10 / (buy_10 + sell_10)

        rv = self._realized_vol(st, now, 300.0, 10.0)
        if rv is not None:
            v["realized_vol_5m"] = rv

        for name, minutes in (
            ("ret_15m", 15),
            ("ret_60m", 60),
            ("ret_240m", 240),
            ("ret_oiw", self._oi_min),
        ):
            r = _ret_minutes(st, now, minutes)
            if r is not None:
                v[name] = r
        vol = _vol_24h(st)
        if vol is not None:
            v["vol_1m_24h"] = vol

        long_1m = short_1m = long_5m = short_5m = total = 0.0
        for t, usdt, was_long in st.liqs:
            total += usdt
            if t >= now - 300.0:
                if was_long:
                    long_5m += usdt
                else:
                    short_5m += usdt
            if t >= now - 60.0:
                if was_long:
                    long_1m += usdt
                else:
                    short_1m += usdt
        v["liq_long_usdt_1m"] = long_1m
        v["liq_short_usdt_1m"] = short_1m
        v["liq_long_usdt_5m"] = long_5m
        v["liq_short_usdt_5m"] = short_5m
        per_min = total / (LIQ_WINDOW_S / 60.0)
        v["liq_ratio_1m"] = max(long_1m, short_1m) / per_min if per_min > 0 else 0.0

        if st.mark is not None:
            v["funding_rate"] = st.mark.funding_rate
            if st.mark.index_price:
                v["mark_premium_bps"] = (st.mark.mark_price / st.mark.index_price - 1.0) * 1e4
            if st.mark.next_funding_at is not None:
                left = (_ts(st.mark.next_funding_at) - now) / 60.0
                v["funding_in_min"] = max(left, 0.0)
        return v

    def _realized_vol(
        self, st: _SymbolState, now: float, window: float, step: float
    ) -> float | None:
        """每 ``step`` 秒取一个价格，算对数收益的标准差（不年化）。"""
        pts: list[float] = []
        # 和 _price_at 同一个查法；先拷成列表，31 次二分不用每次在 deque 里按下标找
        prices = list(st.prices)
        t = now - window
        while prices and t <= now:
            p = prices[max(bisect.bisect_right(prices, (t, math.inf)) - 1, 0)][1]
            if p:
                pts.append(p)
            t += step
        rets = [math.log(pts[i] / pts[i - 1]) for i in range(1, len(pts))]
        if len(rets) < 3:
            return None
        mean = sum(rets) / len(rets)
        return math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1))

    def _book_metrics(self, st: _SymbolState, now: float) -> BookMetrics | None:
        b = st.book
        if b is None or now - _ts(b.ts) > 10.0:
            return None  # 盘口太旧就当没有，风控员看到没有盘口会拒单
        bid, ask = bk.depth_usdt(b, self._levels)
        f = self._flow_sums(st, now)
        buy, sell = f.buy, f.sell
        return BookMetrics(
            mid=bk.mid(b),
            microprice=bk.microprice(b),
            spread_bps=bk.spread_bps(b),
            depth_usdt=bid + ask,
            bid_depth_usdt=bid,
            ask_depth_usdt=ask,
            imbalance=bk.imbalance(b, self._levels),
            ofi=st.ofi_sum.value(),
            cvd=buy - sell,
            taker_buy_ratio=buy / (buy + sell) if buy + sell > 0 else 0.5,
        )
