"""预测员：开关、故障即无预测、过期、缺口、状态上报。用假后端，不需要 GPU 和权重。"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from jiaoyi import interfaces as i
from jiaoyi.contracts import Health, Kline, Outcome, PredictorConfig, Role
from jiaoyi.contracts.market import Interval
from jiaoyi.core.store import JsonlDecisionLog
from jiaoyi.predictor import KronosForecaster, KronosSettings
from jiaoyi.predictor.kronos import Path_

T0 = datetime(2026, 10, 22, 0, 0, tzinfo=UTC)
SET = KronosSettings(min_bars=3, lookback=10, pred_len=2, paths=2, infer_timeout_s=0.5)
ALL = PredictorConfig(mode="all_symbols")


def make(*args: Any, **kw: Any) -> KronosForecaster:
    """原有用例按“全部标的”跑；按需模式的用例自己传 cfg。"""
    kw.setdefault("cfg", ALL)
    return KronosForecaster(*args, **kw)


def bar(n: int, close: float = 100.0, symbol: str = "BTCUSDT") -> Kline:
    o = T0 + timedelta(minutes=5 * n)
    return Kline(
        symbol=symbol, interval=Interval.M5, open_time=o, close_time=o + timedelta(minutes=5),
        open=close, high=close + 1, low=close - 1, close=close, volume=1.0,
    )  # fmt: skip


class FakeBackend:
    def __init__(self, paths: list[Path_] | None = None, error: Exception | None = None) -> None:
        self.paths = paths or [[(103.0, 98.0, 101.0), (104.0, 97.0, 102.0)]] * 2
        self.error = error
        self.calls = 0
        self.delay = 0.0

    def predict(self, bars: list[Kline], pred_len: int, paths: int) -> list[Path_]:
        self.calls += 1
        time.sleep(self.delay)
        if self.error:
            raise self.error
        return self.paths


async def feed(f: KronosForecaster, n: int, **kw: float) -> None:
    for k in range(n):
        await f.on_kline(bar(k, **kw))
    await f.wait_idle()


async def test_forecast_after_enough_bars(tmp_path: Path) -> None:
    sink = JsonlDecisionLog(tmp_path)
    f = make(True, SET, FakeBackend(), sink, clock=lambda: T0 + timedelta(hours=1))
    assert isinstance(f, i.Predictor)
    await feed(f, 2)
    assert f.latest("BTCUSDT", T0) is None  # 不够 3 根
    await feed(f, 3)
    fc = f.latest("BTCUSDT", T0 + timedelta(minutes=15))
    assert fc is not None
    assert (fc.price_low, fc.price_high, fc.horizon_min) == (97.0, 104.0, 10)
    assert fc.p_up == 1.0
    st = sink.read_status()[Role.PREDICTOR.value]
    assert st.health == Health.OK and st.today.calls >= 1
    rec = sink.read_day("2026-10-22")[0]
    assert rec.role == Role.PREDICTOR and rec.latency_ms >= 0


async def test_disabled_returns_none_and_never_calls_model(tmp_path: Path) -> None:
    be = FakeBackend()
    sink = JsonlDecisionLog(tmp_path)
    f = make(False, SET, be, sink)
    await feed(f, 5)
    assert be.calls == 0 and f.latest("BTCUSDT", T0) is None
    f.heartbeat()
    assert sink.read_status()["predictor"].health == Health.DISABLED


async def test_runtime_switch_clears_forecast() -> None:
    f = make(True, SET, FakeBackend())
    await feed(f, 4)
    now = T0 + timedelta(minutes=20)
    assert f.latest("BTCUSDT", now) is not None
    f.set_enabled(False)
    assert f.latest("BTCUSDT", now) is None


async def test_backend_error_means_no_forecast_and_down_after_three(tmp_path: Path) -> None:
    sink = JsonlDecisionLog(tmp_path)
    f = make(True, SET, FakeBackend(error=RuntimeError("MPS 炸了")), sink, clock=lambda: T0)
    await feed(f, 3)
    assert f.latest("BTCUSDT", T0) is None
    assert sink.read_status()["predictor"].health == Health.DEGRADED
    await f.on_kline(bar(3))
    await f.wait_idle()
    await f.on_kline(bar(4))
    await f.wait_idle()
    st = sink.read_status()["predictor"]
    assert st.health == Health.DOWN and "MPS" in (st.last_error or "")
    assert sink.read_day("2026-10-22")[-1].outcome == Outcome.ERROR


async def test_timeout_means_no_forecast() -> None:
    be = FakeBackend()
    be.delay = 0.8
    f = make(True, SET, be)
    await feed(f, 3)
    assert f.latest("BTCUSDT", T0) is None


async def test_bad_model_output_rejected() -> None:
    nan = [[(float("nan"), 98.0, 101.0), (104.0, 97.0, 102.0)]] * 2
    short = [[(103.0, 98.0, 101.0)]]
    for paths in (nan, short):
        f = make(True, SET, FakeBackend(paths=paths))
        await feed(f, 3)
        assert f.latest("BTCUSDT", T0) is None


async def test_forecast_expires() -> None:
    f = make(True, SET, FakeBackend())
    await feed(f, 3)  # 最后一根收盘 T0+15m，有效到 +25m
    assert f.latest("BTCUSDT", T0 + timedelta(minutes=24)) is not None
    assert f.latest("BTCUSDT", T0 + timedelta(minutes=25)) is None


async def test_gap_resets_history_and_other_interval_ignored() -> None:
    be = FakeBackend()
    f = make(True, SET, be)
    await feed(f, 3)
    calls = be.calls
    await f.on_kline(bar(10))  # 缺口：旧序列作废，只剩 1 根
    assert be.calls == calls and f.latest("BTCUSDT", T0) is None
    await f.on_kline(bar(10))  # 重复
    odd = bar(11).model_copy(update={"interval": Interval.M1})
    await f.on_kline(odd)
    await f.wait_idle()
    assert be.calls == calls


async def test_busy_symbol_drops_new_kline() -> None:
    be = FakeBackend()
    be.delay = 0.2
    f = make(True, KronosSettings(**{**SET.__dict__, "infer_timeout_s": 2.0}), be)
    await feed(f, 2)
    await asyncio.gather(f.on_kline(bar(2)), f.on_kline(bar(3)))
    await f.wait_idle()
    assert be.calls == 1


async def test_on_kline_returns_immediately_while_model_runs() -> None:
    be = FakeBackend()
    be.delay = 1.0
    f = make(True, KronosSettings(**{**SET.__dict__, "infer_timeout_s": 5.0}), be)
    await feed(f, 2)
    t0 = time.perf_counter()
    await f.on_kline(bar(2))
    assert time.perf_counter() - t0 < 0.05
    assert f.latest("BTCUSDT", T0) is None  # 还没算完
    await f.wait_idle()
    assert f.latest("BTCUSDT", T0 + timedelta(minutes=15)) is not None


async def test_close_cancels_background_inference() -> None:
    be = FakeBackend()
    be.delay = 0.5
    f = make(True, SET, be)
    await feed(f, 2)
    await f.on_kline(bar(2))
    f.close()
    await f.wait_idle()


def test_auto_device_skips_mps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mac mini 上 MPS 每次报 dropout 不支持，auto 直接用 CPU；显式写 mps 仍然尊重。"""
    import types

    from jiaoyi.predictor import kronos as kmod

    fake = types.SimpleNamespace(
        cuda=types.SimpleNamespace(is_available=lambda: False),
        backends=types.SimpleNamespace(mps=types.SimpleNamespace(is_available=lambda: True)),
    )
    monkeypatch.setattr(kmod.importlib, "import_module", lambda name: fake)
    assert kmod.OfficialKronosBackend(KronosSettings())._pick_device() == "cpu"
    assert kmod.OfficialKronosBackend(KronosSettings(device="mps"))._pick_device() == "mps"


# ---------------------------------------------------------------- 1m→5m、按需、尺寸策略


def m1(minute: int, close: float = 100.0, symbol: str = "BTCUSDT") -> Kline:
    o = T0 + timedelta(minutes=minute)
    return Kline(
        symbol=symbol, interval=Interval.M1, open_time=o, close_time=o + timedelta(minutes=1),
        open=close, high=close + minute, low=close - 1, close=close + 0.5, volume=2.0,
        quote_volume=200.0,
    )  # fmt: skip


class SpyBackend(FakeBackend):
    def __init__(self) -> None:
        super().__init__()
        self.seen: list[list[Kline]] = []

    def predict(self, bars: list[Kline], pred_len: int, paths: int) -> list[Path_]:
        self.seen.append(bars)
        return super().predict(bars, pred_len, paths)


async def test_one_minute_klines_aggregate_to_five() -> None:
    be = SpyBackend()
    f = make(True, SET, be)
    for minute in range(15):  # 3 根完整 5 分钟线
        await f.on_kline(m1(minute))
    await f.wait_idle()
    assert be.calls == 1
    bars = be.seen[0]
    assert [b.interval for b in bars] == [Interval.M5] * 3
    first = bars[0]
    assert first.open_time == T0 and first.close_time == T0 + timedelta(minutes=5)
    assert (first.open, first.high, first.low, first.close) == (100.0, 104.0, 99.0, 100.5)
    assert (first.volume, first.quote_volume) == (10.0, 1000.0)
    assert f.latest("BTCUSDT", T0 + timedelta(minutes=15)) is not None


async def test_missing_minute_drops_the_whole_bar() -> None:
    be = FakeBackend()
    f = make(True, SET, be)
    for minute in [*range(0, 7), *range(8, 20)]:  # 缺第 7 分钟：5–9 这根丢掉，序列断开
        await f.on_kline(m1(minute))
    await f.wait_idle()
    assert be.calls == 0  # 缺口后只剩 10–14、15–19 两根，不够 3 根
    for minute in range(20, 25):
        await f.on_kline(m1(minute))
    await f.wait_idle()
    assert be.calls == 1


async def test_on_demand_only_predicts_wanted_symbols() -> None:
    be = FakeBackend()
    f = make(True, SET, be, cfg=PredictorConfig())
    assert f.mode == "on_demand"
    await feed(f, 4)
    assert be.calls == 0 and f.latest("BTCUSDT", T0) is None
    f.set_wanted({"BTCUSDT"})
    await f.on_kline(bar(4))
    await f.on_kline(bar(4, symbol="ETHUSDT"))
    await f.wait_idle()
    assert be.calls == 1
    assert f.latest("BTCUSDT", T0 + timedelta(minutes=25)) is not None


async def test_request_fills_in_once_without_blocking() -> None:
    be = FakeBackend()
    be.delay = 0.2
    now = T0 + timedelta(minutes=20)
    f = make(True, KronosSettings(**{**SET.__dict__, "infer_timeout_s": 2.0}), be,
             cfg=PredictorConfig(), clock=lambda: now)  # fmt: skip
    await feed(f, 4)  # 不在 wanted 里，没算
    assert be.calls == 0
    t0 = time.perf_counter()
    f.request("BTCUSDT")
    f.request("BTCUSDT")  # 正在算，不重复派
    assert time.perf_counter() - t0 < 0.05
    await f.wait_idle()
    assert be.calls == 1 and f.latest("BTCUSDT", now) is not None
    f.request("BTCUSDT")  # 已有新鲜预测
    f.request("SOLUSDT")  # 没有数据
    await f.wait_idle()
    assert be.calls == 1


def test_size_policy_picks_by_queue_depth() -> None:
    fixed = make(True, SET, FakeBackend(), cfg=PredictorConfig(model_size="base"))
    assert [fixed._pick_size(n) for n in (0, 5)] == ["base", "base"]
    cfg = PredictorConfig(size_policy="adaptive", adaptive_base_max_queue=0,
                          adaptive_mini_min_queue=3)  # fmt: skip
    ad = make(True, SET, FakeBackend(), cfg=cfg)
    assert [ad._pick_size(n) for n in (0, 1, 2, 3, 9)] == ["base", "small", "small", "mini", "mini"]


def test_predictor_config_validation() -> None:
    with pytest.raises(ValueError):
        PredictorConfig(adaptive_base_max_queue=3, adaptive_mini_min_queue=3)
    with pytest.raises(ValueError):
        PredictorConfig(backfill_bars=10)


async def test_adaptive_loads_each_size_on_first_use() -> None:
    built: dict[str, FakeBackend] = {}

    def factory(size: str) -> FakeBackend:
        built[size] = FakeBackend()
        built[size].delay = 0.1
        return built[size]

    cfg = PredictorConfig(mode="all_symbols", size_policy="adaptive",
                          adaptive_base_max_queue=0, adaptive_mini_min_queue=2)  # fmt: skip
    f = KronosForecaster(True, KronosSettings(**{**SET.__dict__, "infer_timeout_s": 2.0}),
                         cfg=cfg, backend_for=factory, clock=lambda: T0)  # fmt: skip
    syms = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]
    for s in syms:
        await f.backfill(s, [bar(k, symbol=s) for k in range(2)])
    for s in syms:  # 一起收盘：第一个开始算时后面还排着 3 个 → mini
        await f.on_kline(bar(2, symbol=s))
    await f.wait_idle()
    assert set(built) == {"mini", "small", "base"}
    models = {s: f.latest(s, T0 + timedelta(minutes=15)) for s in syms}
    assert {fc.model for fc in models.values() if fc} == {
        "Kronos-mini",
        "Kronos-small",
        "Kronos-base",
    }


async def test_five_symbols_infer_in_background_without_blocking_on_kline() -> None:
    be = FakeBackend()
    be.delay = 0.2
    f = make(True, KronosSettings(**{**SET.__dict__, "infer_timeout_s": 5.0}), be)
    syms = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "DOGEUSDT"]
    for s in syms:
        await f.backfill(s, [bar(k, symbol=s) for k in range(2)])
    t0 = time.perf_counter()
    for s in syms:
        await f.on_kline(bar(2, symbol=s))
    assert time.perf_counter() - t0 < 0.05  # 5 个币一起收盘，行情循环不等推理
    await f.wait_idle()
    assert be.calls == 5
    assert all(f.latest(s, T0 + timedelta(minutes=15)) for s in syms)


async def test_backfill_then_first_five_minute_close_forecasts() -> None:
    be = FakeBackend()
    f = make(True, SET, be, cfg=PredictorConfig())
    f.set_wanted({"BTCUSDT"})
    await f.backfill("BTCUSDT", [bar(k) for k in range(3)])  # 补历史不推理
    await f.wait_idle()
    assert be.calls == 0
    await f.backfill("BTCUSDT", [bar(k) for k in range(3)])  # 重叠的丢掉
    for minute in range(15, 20):  # 实时 1 分钟线拼出第 4 根 5 分钟线
        await f.on_kline(m1(minute))
    await f.wait_idle()
    assert be.calls == 1
    assert f.latest("BTCUSDT", T0 + timedelta(minutes=20)) is not None
