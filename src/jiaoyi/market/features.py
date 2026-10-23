"""特征引擎：逐个行情事件增量更新，每个标的一份状态，随时出 ``FeatureSnapshot``。

写法参考 buberlo/jev-trader 的 ``state/features.py``（MIT，提交 15d089e）：滚动窗口 +
按时间裁剪；改成多币种，补上 ATR、OFI、CVD、成交量放大、爆仓和资金费率。

“现在”用该标的最后一个事件的时间，不读系统时钟，所以实盘和回放算出来一样。
``values`` 里的键登记在 docs/fast_loop.md，别的模块只认那里列出的键。
"""

from __future__ import annotations

import bisect
import math
import statistics
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime

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
    short: _AtrTrack = field(default_factory=_AtrTrack)
    long: _AtrTrack = field(default_factory=_AtrTrack)  # 没配长周期就一直空着
    # 最近 7 天每根 N 分钟线收完时的 (收盘时间戳, ATR)（求中位数给止损 ATR 当下限）
    atr_hist: deque[tuple[float, float]] = field(default_factory=deque)
    last_price: float | None = None
    last_ts: float = 0.0
    book: BookTop | None = None
    mark: MarkPrice | None = None


def _ts(dt: datetime) -> float:
    return dt.timestamp()


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
    ``atr_long_period_min`` 不为 0 时同样再算一个长周期 ATR（保镖止损用两者的几何平均）。
    均量 ``vol_ratio_1m`` 仍按 1 分钟 K 线算。
    """

    def __init__(
        self,
        depth_levels: int = 5,
        *,
        atr_period_min: int = DEFAULT_ATR_MINUTES,
        atr_long_period_min: int = 0,
    ) -> None:
        if atr_period_min < 1:
            raise ValueError("atr_period_min 至少 1 分钟")
        if 0 < atr_long_period_min <= atr_period_min:
            raise ValueError("atr_long_period_min 必须比 atr_period_min 长")
        self._levels = depth_levels
        self._atr_min = atr_period_min
        self._long_min = atr_long_period_min
        self._hist_len = HISTORY_DAYS * 1440 // atr_period_min
        self._s: dict[str, _SymbolState] = {}

    def _state(self, symbol: str) -> _SymbolState:
        st = self._s.get(symbol)
        if st is None:
            st = self._s[symbol] = _SymbolState(atr_hist=deque(maxlen=self._hist_len))
        return st

    def update(self, event: MarketEvent) -> None:
        st = self._state(event.symbol)
        if isinstance(event, Trade):
            self._on_trade(st, event)
        elif isinstance(event, BookTop):
            self._on_book(st, event)
        elif isinstance(event, Liquidation):
            t = _ts(event.ts)
            st.liqs.append((t, event.price * event.qty, event.side == Side.LONG))
            st.last_ts = max(st.last_ts, t)
        elif isinstance(event, MarkPrice):
            st.mark = event
            st.last_ts = max(st.last_ts, _ts(event.ts))
        elif isinstance(event, Kline):
            self._on_kline(st, event)
        self._prune(st)

    # ------------------------------------------------------------------ 输入

    def _on_trade(self, st: _SymbolState, ev: Trade) -> None:
        t = _ts(ev.ts)
        sec = math.floor(t)
        if st.prices and st.prices[-1][0] == sec:
            st.prices[-1] = (sec, ev.price)
        else:
            st.prices.append((sec, ev.price))
        st.trades.append((t, ev.price * ev.qty, ev.taker_side == Side.LONG))
        st.last_price = ev.price
        st.last_ts = max(st.last_ts, t)

    def _on_book(self, st: _SymbolState, ev: BookTop) -> None:
        t = _ts(ev.ts)
        if st.book is not None:
            st.ofi.append((t, bk.ofi_event(st.book, ev) * bk.mid(ev)))
        st.book = ev
        st.last_ts = max(st.last_ts, t)

    def _on_kline(self, st: _SymbolState, k: Kline) -> None:
        if k.interval != Interval.M1:
            return
        if st.klines and k.open_time <= st.klines[-1].open_time:
            return  # 重复或乱序（例如回填和推送重叠）
        st.klines.append(k)
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

    def book_age_s(self, symbol: str, now: datetime) -> float | None:
        """最近一次盘口更新距 ``now`` 几秒；没收到过盘口返回 None（面板看盘口员是否在线）。"""
        st = self._s.get(symbol)
        if st is None or st.book is None:
            return None
        return max(0.0, now.timestamp() - _ts(st.book.ts))

    def symbols(self) -> list[str]:
        return sorted(self._s)

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

        rv = self._realized_vol(st, now, 300.0, 10.0)
        if rv is not None:
            v["realized_vol_5m"] = rv

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
            return None  # 盘口太旧就当没有，保镖看到没有盘口会拒单
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
