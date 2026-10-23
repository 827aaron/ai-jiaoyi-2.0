"""回放用的简化特征引擎和哨兵（测试替身）。

真实现是快环工程师的 ``jiaoyi.market`` / ``jiaoyi.sentinel``。这两个只吃成交流，
够把回放跑通：1 分钟区间的 EMA 当 ATR，10 秒量对 5 分钟均量当放量倍数，
突破 5 分钟高低点出信号。aggTrades 里没有盘口，价差用固定值。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from jiaoyi.contracts import (
    BattleCard,
    BookMetrics,
    CandidateSignal,
    FeatureSnapshot,
    MarketEvent,
    Side,
    SignalKind,
    Trade,
)

FAST = timedelta(seconds=10)
SLOW = timedelta(minutes=5)


@dataclass
class _State:
    """增量状态：快窗口（10 秒）和前窗口（10 秒到 5 分钟）各自滚动，避免每次重扫。"""

    fast: deque[tuple[datetime, float, float, int]] = field(default_factory=deque)
    prior: deque[tuple[datetime, float, float, int]] = field(default_factory=deque)
    hi: deque[tuple[datetime, float]] = field(default_factory=deque)  # 前窗口最高价单调队列
    lo: deque[tuple[datetime, float]] = field(default_factory=deque)
    fast_vol: float = 0.0
    fast_buy: float = 0.0
    fast_cvd: float = 0.0
    slow_vol: float = 0.0
    first: datetime | None = None
    last: float = 0.0
    atr: float = 0.0
    bucket_start: datetime | None = None
    bucket_hi: float = 0.0
    bucket_lo: float = 0.0
    now: datetime | None = None
    count: int = 0


class SimpleFeatureEngine:
    def __init__(self, spread_bps: float = 1.0, atr_alpha: float = 0.15) -> None:
        self._spread = spread_bps
        self._alpha = atr_alpha
        self._s: dict[str, _State] = {}

    def update(self, event: MarketEvent) -> None:
        if not isinstance(event, Trade):
            return
        st = self._s.setdefault(event.symbol, _State())
        ts = event.ts
        st.now, st.last, st.count = ts, event.price, st.count + 1
        st.first = st.first or ts
        sign = 1 if event.taker_side is Side.LONG else -1
        item = (ts, event.price, event.qty, sign)
        st.fast.append(item)
        st.fast_vol += event.qty
        st.fast_buy += event.qty if sign > 0 else 0.0
        st.fast_cvd += event.qty * sign
        st.slow_vol += event.qty
        # 超过 10 秒的成交从快窗口移到前窗口
        while st.fast and ts - st.fast[0][0] > FAST:
            old = st.fast.popleft()
            st.fast_vol -= old[2]
            st.fast_buy -= old[2] if old[3] > 0 else 0.0
            st.fast_cvd -= old[2] * old[3]
            st.prior.append(old)
            while st.hi and st.hi[-1][1] <= old[1]:
                st.hi.pop()
            st.hi.append((old[0], old[1]))
            while st.lo and st.lo[-1][1] >= old[1]:
                st.lo.pop()
            st.lo.append((old[0], old[1]))
        # 超过 5 分钟的丢掉
        while st.prior and ts - st.prior[0][0] > SLOW:
            old = st.prior.popleft()
            st.slow_vol -= old[2]
        while st.hi and ts - st.hi[0][0] > SLOW:
            st.hi.popleft()
        while st.lo and ts - st.lo[0][0] > SLOW:
            st.lo.popleft()
        # 1 分钟区间的 EMA 当 ATR
        minute = ts.replace(second=0, microsecond=0)
        if st.bucket_start is None or minute != st.bucket_start:
            if st.bucket_start is not None:
                rng = st.bucket_hi - st.bucket_lo
                st.atr = rng if st.atr == 0 else st.atr + self._alpha * (rng - st.atr)
            st.bucket_start, st.bucket_hi, st.bucket_lo = minute, event.price, event.price
        st.bucket_hi = max(st.bucket_hi, event.price)
        st.bucket_lo = min(st.bucket_lo, event.price)

    def snapshot(self, symbol: str) -> FeatureSnapshot | None:
        st = self._s.get(symbol)
        if st is None or st.now is None or st.first is None or st.atr <= 0 or st.count < 50:
            return None
        if st.now - st.first < SLOW * 0.8 or not st.hi:
            return None  # 数据还不够 5 分钟
        baseline = st.slow_vol / (SLOW / FAST)
        ratio = max(0.0, st.fast_vol) / baseline if baseline > 0 else 0.0
        return FeatureSnapshot(
            symbol=symbol,
            ts=st.now,
            last_price=st.last,
            atr=st.atr,
            values={
                "vol_ratio_10s": ratio,
                "prior_high": st.hi[0][1],
                "prior_low": st.lo[0][1],
            },
            book=BookMetrics(
                mid=st.last,
                microprice=st.last,
                spread_bps=self._spread,
                depth_usdt=0.0,
                imbalance=0.0,
                ofi=st.fast_cvd,
                cvd=st.fast_cvd,
                taker_buy_ratio=min(1.0, max(0.0, st.fast_buy / st.fast_vol))
                if st.fast_vol > 0
                else 0.5,
            ),
        )


class SimpleTrigger:
    """放量（10 秒量 ≥ 倍数）且突破前 5 分钟高 / 低点，冷却期内同键只发一次。"""

    def __init__(self, vol_ratio: float = 3.0, cooldown_s: int = 30) -> None:
        self._ratio = vol_ratio
        self._cool = timedelta(seconds=cooldown_s)
        self._last: dict[str, datetime] = {}
        self._n = 0

    def check(
        self, features: FeatureSnapshot, card: BattleCard | None, now: datetime
    ) -> list[CandidateSignal]:
        if card is None or not card.is_valid(now):
            return []
        cs = card.for_symbol(features.symbol)
        if cs is None or SignalKind.VOLUME_BREAKOUT not in cs.allowed_signals:
            return []
        v = features.values
        if v.get("vol_ratio_10s", 0.0) < self._ratio:
            return []
        px = features.last_price
        side = Side.LONG if px > v["prior_high"] else Side.SHORT if px < v["prior_low"] else None
        if side is None or (cs.bias.value in ("long", "short") and cs.bias.value != side.value):
            return []
        key = f"{features.symbol}:{SignalKind.VOLUME_BREAKOUT.value}:{side.value}"
        if (prev := self._last.get(key)) and now - prev < self._cool:
            return []
        self._last[key] = now
        self._n += 1
        return [
            CandidateSignal(
                signal_id=f"sig-{self._n:05d}",
                symbol=features.symbol,
                kind=SignalKind.VOLUME_BREAKOUT,
                side_hint=side,
                ts=now,
                trigger_price=px,
                trigger_desc=(
                    f"突破 5 分钟{'高' if side is Side.LONG else '低'}点，"
                    f"10 秒量 {v['vol_ratio_10s']:.1f} 倍"
                ),
                dedup_key=key,
                card_id=card.card_id,
                card_version=card.version,
                features=features,
            )
        ]
