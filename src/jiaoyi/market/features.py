"""特征引擎：逐个行情事件增量更新，每个标的一份状态，随时出 ``FeatureSnapshot``。

写法参考 buberlo/jev-trader 的 ``state/features.py``（MIT，提交 15d089e）：滚动窗口 +
按时间裁剪；改成多币种，补上 ATR、OFI、CVD、成交量放大、爆仓和资金费率。

“现在”用该标的最后一个事件的时间，不读系统时钟，所以实盘和回放算出来一样。
``values`` 里的键登记在 docs/fast_loop.md，别的模块只认那里列出的键。
"""

from __future__ import annotations

import bisect
import math
from collections import deque
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
VOL_AVG_KLINES = 20
TRADE_WINDOW_S = 300.0
LIQ_WINDOW_S = 1800.0
FLOW_WINDOW_S = 60.0


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
    trs: list[float] = field(default_factory=list)
    atr: float | None = None
    last_price: float | None = None
    last_ts: float = 0.0
    book: BookTop | None = None
    mark: MarkPrice | None = None


def _ts(dt: datetime) -> float:
    return dt.timestamp()


class RollingFeatureEngine:
    """``FeatureEngine`` 的实现。只认 1 分钟 K 线算 ATR 和均量，其它周期忽略。"""

    def __init__(self, depth_levels: int = 5) -> None:
        self._levels = depth_levels
        self._s: dict[str, _SymbolState] = {}

    def update(self, event: MarketEvent) -> None:
        st = self._s.setdefault(event.symbol, _SymbolState())
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
        prev = st.klines[-1] if st.klines else None
        st.klines.append(k)
        if prev is not None:
            tr = max(k.high - k.low, abs(k.high - prev.close), abs(k.low - prev.close))
            if st.atr is None:
                st.trs.append(tr)
                if len(st.trs) >= ATR_PERIOD:
                    st.atr = sum(st.trs[-ATR_PERIOD:]) / ATR_PERIOD
            else:
                st.atr = (st.atr * (ATR_PERIOD - 1) + tr) / ATR_PERIOD
        if st.last_price is None:
            st.last_price = k.close
        st.last_ts = max(st.last_ts, _ts(k.close_time))

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

    def snapshot(self, symbol: str) -> FeatureSnapshot | None:
        st = self._s.get(symbol)
        if st is None or st.last_price is None or st.atr is None:
            return None  # 还没攒够 15 根 1 分钟 K 线
        now = st.last_ts
        values = self._values(st, now)
        return FeatureSnapshot(
            symbol=symbol,
            ts=datetime.fromtimestamp(now, UTC),
            last_price=st.last_price,
            atr=st.atr,
            values=values,
            book=self._book_metrics(st, now),
        )

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

        long_1m = short_1m = total = 0.0
        for t, usdt, was_long in st.liqs:
            total += usdt
            if t >= now - 60.0:
                if was_long:
                    long_1m += usdt
                else:
                    short_1m += usdt
        v["liq_long_usdt_1m"] = long_1m
        v["liq_short_usdt_1m"] = short_1m
        per_min = total / (LIQ_WINDOW_S / 60.0)
        v["liq_ratio_1m"] = max(long_1m, short_1m) / per_min if per_min > 0 else 0.0

        if st.mark is not None:
            v["funding_rate"] = st.mark.funding_rate
            if st.mark.index_price:
                v["mark_premium_bps"] = (st.mark.mark_price / st.mark.index_price - 1.0) * 1e4
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
            imbalance=bk.imbalance(b, self._levels),
            ofi=sum(e for _, e in st.ofi),
            cvd=buy - sell,
            taker_buy_ratio=buy / (buy + sell) if buy + sell > 0 else 0.5,
        )
