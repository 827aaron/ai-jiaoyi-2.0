"""新信号（研究 2026-10-24）：持仓量变化、大户散户分歧（反着大户）、BTC 先动、放量提前发现。"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from typing import Any

from jiaoyi import contracts as c
from jiaoyi.gate.questions import MAX_SNAPSHOT_CHARS, entry_snapshot, market_block
from jiaoyi.market.derivs import DerivsFeed
from jiaoyi.market.features import RollingFeatureEngine
from jiaoyi.sentinel.rules import RuleTrigger
from tests.fastloop_helpers import FAST, card, features, signal

T0 = datetime(2026, 10, 24, 0, 0, tzinfo=UTC)
ALL = [k.value for k in c.SignalKind]


def _kline(sym: str, i: int, close: float, qv: float = 1_000_000.0) -> c.Kline:
    ot = T0 + timedelta(minutes=i)
    return c.Kline(
        symbol=sym,
        interval=c.Interval.M1,
        open_time=ot,
        close_time=ot + timedelta(seconds=59.999),
        open=close,
        high=close + 5,
        low=close - 5,
        close=close,
        volume=qv / close,
        quote_volume=qv,
    )


def _trade(sym: str, at: datetime, usdt: float, buy: bool, price: float) -> c.Trade:
    side = c.Side.LONG if buy else c.Side.SHORT
    return c.Trade(symbol=sym, ts=at, price=price, qty=usdt / price, taker_side=side)


def _warm(eng: RollingFeatureEngine, sym: str, n: int, base: float, last: float) -> None:
    """``n`` 根 1 分钟线：前面小幅来回抖，最后一根收在 ``last``。"""
    for i in range(n - 1):
        eng.update(_kline(sym, i, base * (1 + 0.001 * (1 if i % 2 else -1))))
    eng.update(_kline(sym, n - 1, last))


# ---------------------------------------------------------------- 特征


def test_ten_second_rate_and_slow_values() -> None:
    eng = RollingFeatureEngine(atr_period_min=1, oi_window_min=60)
    _warm(eng, "ETHUSDT", 300, 2000.0, 2000.0)
    end = T0 + timedelta(minutes=300)
    eng.update(_trade("ETHUSDT", end - timedelta(seconds=50), 100_000.0, False, 2000.0))
    eng.update(_trade("ETHUSDT", end - timedelta(seconds=5), 900_000.0, True, 2020.0))
    eng.update(_trade("ETHUSDT", end, 100_000.0, True, 2020.0))
    snap = eng.snapshot("ETHUSDT")
    assert snap is not None
    v = snap.values
    # 均量 100 万；最近 10 秒 100 万 × 6 = 6 倍；1 分钟 110 万 = 1.1 倍
    assert math.isclose(v["vol_ratio_10s"], 6.0)
    assert math.isclose(v["vol_ratio_1m"], 1.1)
    assert v["taker_buy_ratio_10s"] == 1.0
    assert math.isclose(v["ret_60m"], 2020.0 / (2000.0 * 1.001) - 1, rel_tol=1e-6) or math.isclose(
        v["ret_60m"], 2020.0 / (2000.0 * 0.999) - 1, rel_tol=1e-6
    )
    assert "ret_oiw" in v and "ret_240m" in v and v["vol_1m_24h"] > 0
    assert "btc5z" not in v  # 没订 BTC


def test_btc5z_and_extra_merged_into_every_snapshot() -> None:
    eng = RollingFeatureEngine(atr_period_min=1)
    _warm(eng, "BTCUSDT", 300, 68000.0, 68000.0)
    _warm(eng, "ETHUSDT", 300, 2000.0, 2000.0)
    end = T0 + timedelta(minutes=300)
    eng.update(_trade("BTCUSDT", end, 1000.0, True, 68000.0 * 1.02))  # BTC 突然涨 2%
    eng.update(_trade("ETHUSDT", end, 1000.0, True, 2000.0))
    eng.set_extra(lambda sym, now: {"oi15": 0.8} if sym == "ETHUSDT" else {})
    snap = eng.snapshot("ETHUSDT")
    assert snap is not None
    assert snap.values["btc5z"] > 3 and snap.values["oi15"] == 0.8

    def boom(sym: str, now: datetime) -> dict[str, float]:
        raise RuntimeError("REST 挂了")

    eng.set_extra(boom)
    assert eng.snapshot("ETHUSDT") is not None  # 额外数据出错不拖垮快照


# ---------------------------------------------------------------- 持仓量 / 多空比轮询


class FakeRest:
    def __init__(self) -> None:
        self.oi = 1000.0
        self.top: list[dict[str, Any]] = []
        self.crowd: list[dict[str, Any]] = []
        self.hist: list[dict[str, Any]] = []
        self.fail = False
        self.t = T0.timestamp()
        self.calls: list[str] = []

    async def __call__(self, path: str, params: dict[str, Any], weight: int) -> Any:
        self.calls.append(path)
        if self.fail:
            raise OSError("451")
        if path.endswith("openInterestHist"):
            return self.hist
        if path.endswith("openInterest"):
            return {"openInterest": str(self.oi), "time": int((self.t + 30) * 1000)}
        return self.top if "top" in path else self.crowd


def _rows(start: float, vals: list[float], key: str) -> list[dict[str, Any]]:
    return [{"timestamp": int((start + 300 * i) * 1000), key: str(v)} for i, v in enumerate(vals)]


async def test_derivs_values_seed_and_ratios() -> None:
    rest = FakeRest()
    start = rest.t - 288 * 300
    rest.hist = _rows(start, [900.0] * 289, "sumOpenInterest")
    rest.top = _rows(rest.t - 240 * 60, [1.0] * 48 + [1.06], "longShortRatio")
    rest.crowd = _rows(rest.t - 240 * 60, [2.0] * 48 + [1.88], "longShortRatio")
    feed = DerivsFeed(oi_window_min=240, fetch=rest, clock=lambda: rest.t)
    await feed.poll_oi(["ETHUSDT"])
    await feed.poll_ratios(["ETHUSDT"])
    v = feed.values("ETHUSDT", T0)
    assert math.isclose(v["oi4h"], 11.111, abs_tol=0.01) and v["oi_w_pct"] == v["oi4h"]
    assert v["top_ls"] == 1.06 and v["crowd_ls"] == 1.88
    assert math.isclose(v["top_w_pct"], math.log(1.06) * 100, abs_tol=0.01)
    assert math.isclose(v["crowd_w_pct"], math.log(0.94) * 100, abs_tol=0.01)
    assert v["div4h"] > 10
    assert rest.calls.count("/futures/data/openInterestHist") == 1
    await feed.poll_oi(["ETHUSDT"])
    assert rest.calls.count("/futures/data/openInterestHist") == 1  # 只补一次历史


async def test_derivs_failure_skips_quietly(caplog: Any) -> None:
    rest = FakeRest()
    rest.fail = True
    feed = DerivsFeed(fetch=rest, clock=lambda: rest.t)
    for _ in range(3):
        await feed.poll_oi(["ETHUSDT"])
    assert feed.values("ETHUSDT", T0) == {}
    assert sum("ETHUSDT" in r.getMessage() for r in caplog.records) == 1  # 同一个币只记一行


# ---------------------------------------------------------------- 规则


def _cfg(**kw: Any) -> c.FastLoopConfig:
    return FAST.model_copy(update=kw)


def _quiet(now: datetime, **values: float) -> c.FeatureSnapshot:
    """老信号都不出的快照（量比 1、没爆仓、没资金费率）。"""
    base = {"vol_ratio_1m": 1.0, "taker_buy_ratio_1m": 0.5, "ret_5m": 0.0, "ret_1m": 0.0}
    return features(now, **{**base, **values})


def test_surge_fires_on_ten_second_rate(now: datetime) -> None:
    cd = card(now, allowed_signals=ALL)
    f = _quiet(now, vol_ratio_1m=2.2, vol_ratio_10s=5.5, taker_buy_ratio_10s=0.9)
    [s] = RuleTrigger(FAST).check(f, cd, now)
    assert s.kind is c.SignalKind.VOLUME_SURGE and s.side_hint is c.Side.LONG
    assert "最近 10 秒折算每分钟" in s.trigger_desc and "1 分钟量 2.2 倍" in s.trigger_desc
    assert RuleTrigger(_cfg(burst_10s_on=False)).check(f, cd, now) == []
    # 1 分钟够了照旧按 1 分钟写
    f = _quiet(now, vol_ratio_1m=5.0, taker_buy_ratio_1m=0.8, vol_ratio_10s=9.0)
    [s] = RuleTrigger(FAST).check(f, cd, now)
    assert s.trigger_desc.startswith("1 分钟量")


def test_breakout_fires_on_ten_second_rate(now: datetime) -> None:
    t = RuleTrigger(FAST)
    cd = card(now)
    t.check(_quiet(now, price=67990.0), cd, now)
    [s] = t.check(features(now, price=68010.0, vol_ratio_1m=2.1, vol_ratio_10s=5.5), cd, now)
    assert s.kind is c.SignalKind.VOLUME_BREAKOUT and "10 秒" in s.trigger_desc
    assert "1 分钟 2.1 倍" in s.trigger_desc


def test_ten_second_path_needs_one_minute_floor(now: datetime) -> None:
    """10 秒提前触发还要 1 分钟量 ≥ burst_min_1m_ratio（默认 2 倍）；只看 1 分钟那条不受影响。"""
    assert FAST.burst_min_1m_ratio == 2.0
    cd = card(now, allowed_signals=ALL)
    weak = _quiet(now, vol_ratio_1m=1.6, vol_ratio_10s=6.0, taker_buy_ratio_10s=0.9)
    assert RuleTrigger(FAST).check(weak, cd, now) == []
    assert len(RuleTrigger(_cfg(burst_min_1m_ratio=0.0)).check(weak, cd, now)) == 1  # 0 = 不看
    t = RuleTrigger(FAST)
    t.check(_quiet(now, price=67990.0), card(now), now)
    weak_bo = features(now, price=68010.0, vol_ratio_1m=1.6, vol_ratio_10s=6.0)
    assert t.check(weak_bo, card(now), now) == []


def test_ten_second_path_has_its_own_ratio(now: datetime) -> None:
    """10 秒那条用 burst_10s_ratio（默认 5），不再借量能突增的 1 分钟倍数。"""
    assert FAST.burst_10s_ratio == 5.0
    cd = card(now, allowed_signals=ALL)
    mid = _quiet(now, vol_ratio_1m=2.5, vol_ratio_10s=4.8, taker_buy_ratio_10s=0.9)
    assert RuleTrigger(FAST).check(mid, cd, now) == []  # 4.8 < 5
    assert len(RuleTrigger(_cfg(burst_10s_ratio=4.5)).check(mid, cd, now)) == 1


def test_oi_follow(now: datetime) -> None:
    cfg = _cfg(oi_follow_on=True, oi_window_min=30, oi_min_pct=1.0, oi_price_z=1.0)
    cd = card(now, allowed_signals=ALL)
    vol = 0.0005  # 1 分钟波动；√30 ≈ 5.5 → 1 倍 ≈ 0.27%
    up = _quiet(now, oi_w_pct=1.5, ret_oiw=0.012, vol_1m_24h=vol)
    t = RuleTrigger(cfg)
    [s] = t.check(up, cd, now)
    assert s.kind is c.SignalKind.OI_FOLLOW and s.side_hint is c.Side.LONG
    assert "新多头" in s.trigger_desc
    # 窗口内只出一次（比 signal_dedup_s 长）
    assert t.check(up, cd, now + timedelta(minutes=29)) == []
    assert len(t.check(up, cd, now + timedelta(minutes=31))) == 1
    # 持仓量没涨够 / 价格没走够 / 档位关了 / 实盘没开
    assert (
        RuleTrigger(cfg).check(_quiet(now, oi_w_pct=0.5, ret_oiw=0.012, vol_1m_24h=vol), cd, now)
        == []
    )
    assert (
        RuleTrigger(cfg).check(_quiet(now, oi_w_pct=1.5, ret_oiw=0.001, vol_1m_24h=vol), cd, now)
        == []
    )
    assert RuleTrigger(_cfg(oi_follow_on=False)).check(up, cd, now) == []
    assert RuleTrigger(cfg, live=True).check(up, cd, now) == []
    assert (
        len(
            RuleTrigger(cfg.model_copy(update={"oi_follow_live": True}), live=True).check(
                up, cd, now
            )
        )
        == 1
    )


def test_top_vs_crowd_fades_top_traders(now: datetime) -> None:
    cfg = _cfg(top_crowd_on=True, top_crowd_min_pct=5.0)
    both = card(now, bias="both", allowed_signals=ALL)
    # 大户加多、散户减多 → 做空（反着大户）
    [s] = RuleTrigger(cfg).check(_quiet(now, top_w_pct=6.0, crowd_w_pct=-5.5), both, now)
    assert s.kind is c.SignalKind.TOP_VS_CROWD and s.side_hint is c.Side.SHORT
    assert "反着大户" in s.trigger_desc
    [s] = RuleTrigger(cfg).check(_quiet(now, top_w_pct=-6.0, crowd_w_pct=5.5), both, now)
    assert s.side_hint is c.Side.LONG
    assert RuleTrigger(cfg).check(_quiet(now, top_w_pct=6.0, crowd_w_pct=-2.0), both, now) == []


def test_slow_kind_against_card_only_noted(now: datetime) -> None:
    """新信号和卡方向相反：只记一行，不发反向信号事件（不叫管家、不开临时会）。"""
    cfg = _cfg(top_crowd_on=True)
    t = RuleTrigger(cfg)
    long_card = card(now, allowed_signals=ALL)  # 卡只做多
    assert t.check(_quiet(now, top_w_pct=6.0, crowd_w_pct=-6.0), long_card, now) == []
    assert t.pop_against() == []
    assert any("方向和作战卡相反" in line for line in t.detail_lines(now))


def test_btc_lead(now: datetime) -> None:
    cfg = _cfg(btc_lead_on=True, btc_lead_z=3.0)
    cd = card(now, symbol="ETHUSDT", allowed_signals=ALL)
    eth = _quiet(now, btc5z=3.5, ret_5m=-0.001).model_copy(update={"symbol": "ETHUSDT"})
    [s] = RuleTrigger(cfg).check(eth, cd, now)
    assert s.kind is c.SignalKind.BTC_LEAD and s.side_hint is c.Side.LONG
    moved = _quiet(now, btc5z=3.5, ret_5m=0.004).model_copy(update={"symbol": "ETHUSDT"})
    assert RuleTrigger(cfg).check(moved, cd, now) == []  # 已经跟了
    btc = _quiet(now, btc5z=3.5, ret_5m=-0.001)
    assert RuleTrigger(cfg).check(btc, card(now, allowed_signals=ALL), now) == []  # BTC 自己不出


# ---------------------------------------------------------------- 拍板人快照


def test_jev_snapshot_carries_new_fields_only_when_present(now: datetime) -> None:
    f = features(now)
    assert not {"oi15", "top_ls", "div4h", "btc5z", "vol10_x"} & set(market_block(f))
    extra = {
        "oi15": 0.84, "oi60": 1.9, "oi4h": 3.2, "top_ls": 1.42, "crowd_ls": 2.05,
        "div4h": 4.1, "btc5z": 3.4, "vol_ratio_10s": 6.0,
    }  # fmt: skip
    d = market_block(features(now, **extra))
    assert d["oi4h"] == 3.2 and d["div4h"] == 4.1 and d["btc5z"] == 3.4 and d["vol10_x"] == 6.0
    sig = signal(now).model_copy(
        update={"features": features(now, **extra), "trigger_desc": "x" * 3000}
    )
    s = entry_snapshot(
        sig,
        card(
            now,
        ),
    )
    assert len(s) <= MAX_SNAPSHOT_CHARS
    json.loads(s)  # 太长时去掉可有可无的块，不硬截成坏 JSON
