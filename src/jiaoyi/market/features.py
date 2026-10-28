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
ExtraValues = Callable[[str, datetime], Mapping[str, float]]
"""快照的额外键（持仓量、多空比等，来自 REST 轮询）：(币, 现在) → 键值。"""


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
    # (时间戳, OFI 名义价值)
    ofi: deque[tuple[float, float]] = field(default_factory=deque)
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
    last_price: float | None = None
    last_ts: float = 0.0
    last_price_source: str | None = None
    last_price_source_ts: float | None = None
    last_price_observed_ts: float | None = None
    book_observed_ts: float | None = None
    mark_observed_ts: float | None = None
    book: BookTop | None = None
    mark: MarkPrice | None = None


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


def wilder_atr(bars: Iterable[tuple[float, float, float]]) -> float | None:
    """完整 N 分钟线 ``(高, 低, 收)``（升序）→ 同实时引擎算法的 ATR；不够 15 根为 None。"""
    tr = _AtrTrack()
    for high, low, close in bars:
        _on_bar(tr, high, low, close)
    return tr.atr


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
        if isinstance(event, Trade):
            self._on_trade(st, event)
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
        elif isinstance(event, Kline):
            had_price = st.last_price is not None
            self._on_kline(st, event)
            if not had_price and st.last_price is not None:
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

    def _on_trade(self, st: _SymbolState, ev: Trade) -> None:
        t = _ts(ev.ts)
        sec = math.floor(t)
        if st.prices and st.prices[-1][0] == sec:
            st.prices[-1] = (sec, ev.price)
        else:
            st.prices.append((sec, ev.price))
        usdt, is_buy = ev.price * ev.qty, ev.taker_side == Side.LONG
        st.trades.append((t, usdt, is_buy))
        if not st.flow or st.flow[-1][0] != sec:
            st.flow.append([float(sec), 0.0, 0.0, 0.0, 0.0, 0.0])
        b = st.flow[-1]
        b[1 if is_buy else 2] += usdt
        if usdt >= BIG_TRADE_USDT:
            b[3 if is_buy else 4] += 1.0
            b[5] += usdt
        st.last_price = ev.price
        st.last_ts = max(st.last_ts, t)

    def _on_book(self, st: _SymbolState, ev: BookTop) -> None:
        t = _ts(ev.ts)
        if st.book is not None:
            st.ofi.append((t, bk.ofi_event(st.book, ev) * bk.mid(ev)))
        st.book = ev
        sec = math.floor(t)
        if not st.imbs or st.imbs[-1][0] != sec:
            st.imbs.append((float(sec), bk.imbalance(ev, self._levels)))
        st.last_ts = max(st.last_ts, t)

    def _on_kline(self, st: _SymbolState, k: Kline) -> None:
        if k.interval != Interval.M1:
            return
        if st.klines and k.open_time <= st.klines[-1].open_time:
            return  # 重复或乱序（例如回填和推送重叠）
        st.klines.append(k)
        minute = int(k.open_time.timestamp() // 60)
        st.closes.append((minute, k.close))
        st.bars.append((minute, k.open, k.high, k.low, k.close, k.volume))
        st.vol_dirty = True
        end = self._aggregate(st.short, k, self._atr_min)
        if end is not None and st.short.atr is not None:
            st.atr_hist.append((end, st.short.atr))
        if self._long_min:
            self._aggregate(st.long, k, self._long_min)
        if st.last_price is None:
            st.last_price = k.close
        st.last_ts = max(st.last_ts, _ts(k.close_time))

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
        return len(series)

    def atr_median_7d(self, symbol: str) -> float | None:
        """最近 7 天 ATR 的中位数；不够 1 天的点数返回 None（不拿半天的数据当下限）。"""
        st = self._s.get(symbol)
        if st is None or len(st.atr_hist) < 1440 // self._atr_min:
            return None
        return statistics.median(a for _, a in st.atr_hist)

    def _prune(self, st: _SymbolState) -> None:
        now = st.last_ts
        for dq, window in (
            (st.prices, TRADE_WINDOW_S + 1),
            (st.trades, TRADE_WINDOW_S),
            (st.liqs, LIQ_WINDOW_S),
            (st.ofi, FLOW_WINDOW_S),
            (st.flow, FLOW_LONG_S),
            (st.imbs, IMB_WINDOW_S),
        ):
            while dq and dq[0][0] < now - window:
                dq.popleft()

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

        buy = sell = 0.0
        buy_10 = sell_10 = 0.0
        cvd_30 = 0.0
        n = 0
        for t, usdt, is_buy in st.trades:
            if t < now - FLOW_WINDOW_S:
                continue
            n += 1
            if is_buy:
                buy += usdt
            else:
                sell += usdt
            if t >= now - 30.0:
                cvd_30 += usdt if is_buy else -usdt
            if t >= now - BURST_WINDOW_S:
                if is_buy:
                    buy_10 += usdt
                else:
                    sell_10 += usdt
        v["vol_1m_usdt"] = buy + sell
        v["trades_1m"] = float(n)
        v["taker_buy_ratio_1m"] = buy / (buy + sell) if buy + sell > 0 else 0.5
        v["cvd_1m_usdt"] = buy - sell
        v["cvd_30s_usdt"] = cvd_30

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
        t = now - window
        while t <= now:
            p = self._price_at(st, t)
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
        buy = sell = 0.0
        for t, usdt, is_buy in st.trades:
            if t >= now - FLOW_WINDOW_S:
                if is_buy:
                    buy += usdt
                else:
                    sell += usdt
        return BookMetrics(
            mid=bk.mid(b),
            microprice=bk.microprice(b),
            spread_bps=bk.spread_bps(b),
            depth_usdt=bid + ask,
            bid_depth_usdt=bid,
            ask_depth_usdt=ask,
            imbalance=bk.imbalance(b, self._levels),
            ofi=sum(e for _, e in st.ofi),
            cvd=buy - sell,
            taker_buy_ratio=buy / (buy + sell) if buy + sell > 0 else 0.5,
        )
