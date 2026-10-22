"""预测员：开关、故障即无预测、过期、缺口、状态上报。用假后端，不需要 GPU 和权重。"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from jiaoyi import interfaces as i
from jiaoyi.contracts import Health, Kline, Outcome, Role
from jiaoyi.contracts.market import Interval
from jiaoyi.core.store import JsonlDecisionLog
from jiaoyi.predictor import KronosForecaster, KronosSettings
from jiaoyi.predictor.kronos import Path_

T0 = datetime(2026, 10, 22, 0, 0, tzinfo=UTC)
SET = KronosSettings(min_bars=3, lookback=10, pred_len=2, paths=2, infer_timeout_s=0.5)


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


async def test_forecast_after_enough_bars(tmp_path: Path) -> None:
    sink = JsonlDecisionLog(tmp_path)
    f = KronosForecaster(True, SET, FakeBackend(), sink, clock=lambda: T0 + timedelta(hours=1))
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
    f = KronosForecaster(False, SET, be, sink)
    await feed(f, 5)
    assert be.calls == 0 and f.latest("BTCUSDT", T0) is None
    f.heartbeat()
    assert sink.read_status()["predictor"].health == Health.DISABLED


async def test_runtime_switch_clears_forecast() -> None:
    f = KronosForecaster(True, SET, FakeBackend())
    await feed(f, 4)
    now = T0 + timedelta(minutes=20)
    assert f.latest("BTCUSDT", now) is not None
    f.set_enabled(False)
    assert f.latest("BTCUSDT", now) is None


async def test_backend_error_means_no_forecast_and_down_after_three(tmp_path: Path) -> None:
    sink = JsonlDecisionLog(tmp_path)
    f = KronosForecaster(True, SET, FakeBackend(error=RuntimeError("MPS 炸了")), sink)
    await feed(f, 3)
    assert f.latest("BTCUSDT", T0) is None
    assert sink.read_status()["predictor"].health == Health.DEGRADED
    await f.on_kline(bar(3))
    await f.on_kline(bar(4))
    st = sink.read_status()["predictor"]
    assert st.health == Health.DOWN and "MPS" in (st.last_error or "")
    assert sink.read_day("2026-10-22")[-1].outcome == Outcome.ERROR


async def test_timeout_means_no_forecast() -> None:
    be = FakeBackend()
    be.delay = 0.8
    f = KronosForecaster(True, SET, be)
    await feed(f, 3)
    assert f.latest("BTCUSDT", T0) is None


async def test_bad_model_output_rejected() -> None:
    nan = [[(float("nan"), 98.0, 101.0), (104.0, 97.0, 102.0)]] * 2
    short = [[(103.0, 98.0, 101.0)]]
    for paths in (nan, short):
        f = KronosForecaster(True, SET, FakeBackend(paths=paths))
        await feed(f, 3)
        assert f.latest("BTCUSDT", T0) is None


async def test_forecast_expires() -> None:
    f = KronosForecaster(True, SET, FakeBackend())
    await feed(f, 3)  # 最后一根收盘 T0+15m，有效到 +25m
    assert f.latest("BTCUSDT", T0 + timedelta(minutes=24)) is not None
    assert f.latest("BTCUSDT", T0 + timedelta(minutes=25)) is None


async def test_gap_resets_history_and_other_interval_ignored() -> None:
    be = FakeBackend()
    f = KronosForecaster(True, SET, be)
    await feed(f, 3)
    calls = be.calls
    await f.on_kline(bar(10))  # 缺口：旧序列作废，只剩 1 根
    assert be.calls == calls and f.latest("BTCUSDT", T0) is None
    await f.on_kline(bar(10))  # 重复
    odd = bar(11).model_copy(update={"interval": Interval.M1})
    await f.on_kline(odd)
    assert be.calls == calls


async def test_busy_symbol_drops_new_kline() -> None:
    be = FakeBackend()
    be.delay = 0.2
    f = KronosForecaster(True, KronosSettings(**{**SET.__dict__, "infer_timeout_s": 2.0}), be)
    await feed(f, 2)
    await asyncio.gather(f.on_kline(bar(2)), f.on_kline(bar(3)))
    assert be.calls == 1
