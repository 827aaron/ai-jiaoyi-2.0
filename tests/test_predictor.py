"""预测员：开关、故障即无预测、过期、缺口、状态上报。用假后端，不需要 GPU 和权重。"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from jiaoyi import interfaces as i
from jiaoyi.contracts import Health, Kline, Outcome, PredictorConfig, Role
from jiaoyi.contracts.market import Interval
from jiaoyi.core.store import JsonlDecisionLog
from jiaoyi.predictor import KronosForecaster, KronosSettings
from jiaoyi.predictor import kronos as kmod
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
    # 面板「预测员」卡片读它看到了什么
    assert set(rec.input) == {"last_close", "bars", "interval", "paths", "temperature"}
    assert rec.input["bars"] >= 3 and rec.input["interval"] == SET.interval.value


async def test_listeners_get_each_new_forecast() -> None:
    f = make(True, SET, FakeBackend(), clock=lambda: T0 + timedelta(hours=1))
    got: list[float] = []

    def boom(fc: object) -> None:
        raise RuntimeError("订阅方坏了")

    f.add_listener(boom)  # 出错不影响别的订阅方和预测员
    f.add_listener(lambda fc: got.append(fc.p_up))
    await feed(f, 3)
    assert got == [1.0]


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


async def test_gap_is_reported_and_refill_merges_with_newer_bars() -> None:
    """断档后不用再干等 min_bars 根：报给主循环，REST 补回的旧线和新线合起来马上能算。"""
    be = FakeBackend()
    f = make(True, SET, be)
    await feed(f, 3)
    assert f.take_gaps() == set()
    await f.on_kline(bar(5))  # 3、4 两根没收到
    assert f.take_gaps() == {"BTCUSDT"} and f.take_gaps() == set()
    calls = be.calls
    await f.backfill("BTCUSDT", [bar(k) for k in range(5)])  # REST 拉到 0–4（不含刚收盘的 5）
    await f.wait_idle()
    assert be.calls == calls + 1
    assert f.latest("BTCUSDT", T0 + timedelta(minutes=30)) is not None
    await f.on_kline(bar(6))  # 接着实时线照常往后拼
    await f.wait_idle()
    assert be.calls == calls + 2


async def test_backfill_keeps_only_the_last_continuous_run() -> None:
    be = FakeBackend()
    f = make(True, SET, be)
    await f.backfill("BTCUSDT", [bar(0), bar(1), bar(5), bar(6)])  # 中间缺 2–4
    await f.wait_idle()
    assert be.calls == 0  # 只剩 5、6 两根，不够 3 根
    await f.backfill("BTCUSDT", [bar(k) for k in range(2, 6)])
    await f.wait_idle()
    assert be.calls == 1


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


def test_auto_device_prefers_mps(monkeypatch: pytest.MonkeyPatch) -> None:
    """切 eval 后 MPS 可用且更快：auto 优先 MPS，没有 MPS 用 CPU；显式写的设备照用。"""
    import types

    from jiaoyi.predictor import kronos as kmod

    fake = types.SimpleNamespace(
        cuda=types.SimpleNamespace(is_available=lambda: False),
        backends=types.SimpleNamespace(mps=types.SimpleNamespace(is_available=lambda: True)),
    )
    monkeypatch.setattr(kmod.importlib, "import_module", lambda name: fake)
    assert kmod.OfficialKronosBackend(KronosSettings())._pick_device() == "mps"
    assert kmod.OfficialKronosBackend(KronosSettings(device="cpu"))._pick_device() == "cpu"
    fake.backends.mps.is_available = lambda: False
    assert kmod.OfficialKronosBackend(KronosSettings())._pick_device() == "cpu"


def test_mps_failure_falls_back_to_cpu_and_stays(monkeypatch: pytest.MonkeyPatch) -> None:
    from jiaoyi.predictor import kronos as kmod

    be = kmod.OfficialKronosBackend(KronosSettings())
    loads: list[str] = []
    runs: list[str] = []

    def load(device: str) -> None:
        loads.append(device)
        be._predictor, be._device = object(), device

    def run(bars: list[Kline], pred_len: int, paths: int) -> list[Path_]:
        runs.append(be._device)
        if be._device == "mps":
            raise RuntimeError("MPS 不支持某个算子")
        return [[(1.0, 1.0, 1.0)]]

    monkeypatch.setattr(be, "_pick_device", lambda: "mps")
    monkeypatch.setattr(be, "_load", load)
    monkeypatch.setattr(be, "_run", run)
    assert be.predict([], 1, 1) == [[(1.0, 1.0, 1.0)]]
    be.predict([], 1, 1)
    assert loads == ["mps", "cpu"] and runs == ["mps", "cpu", "cpu"]


def test_official_backend_uses_eval_mode_and_open_time_grid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """加载后切 eval（不然推理时 dropout 开着）；时间戳用开盘时间，落在 5 分钟格点上。"""
    import types

    from jiaoyi.predictor import kronos as kmod

    evals: list[str] = []
    calls: list[dict[str, Any]] = []

    class Loaded:
        def __init__(self, name: str) -> None:
            self.name = name

        def eval(self) -> Loaded:
            evals.append(self.name)
            return self

    class Predictor:
        def __init__(self, model: Loaded, tokenizer: Loaded, **kw: Any) -> None:
            assert evals == ["tokenizer", "model"]  # 构造前就已经切好

        def predict(self, **kw: Any) -> dict[str, list[float]]:
            calls.append(kw)
            n = kw["pred_len"]
            return {"high": [101.0] * n, "low": [99.0] * n, "close": [100.0] * n}

    class Series(list[Any]):
        @property
        def iloc(self) -> Series:
            return self

    class Stamps(list[Any]):
        def tz_localize(self, tz: None) -> list[Any]:
            return [t.replace(tzinfo=tz) for t in self]

    model_mod = types.SimpleNamespace(
        KronosTokenizer=types.SimpleNamespace(from_pretrained=lambda *a, **k: Loaded("tokenizer")),
        Kronos=types.SimpleNamespace(from_pretrained=lambda *a, **k: Loaded("model")),
        KronosPredictor=Predictor,
    )
    pd_mod = types.SimpleNamespace(
        DataFrame=dict,
        Series=Series,
        to_datetime=lambda xs, utc: Stamps(xs),
        Timedelta=timedelta,
    )
    mods = {"model": model_mod, "pandas": pd_mod}
    monkeypatch.setattr(kmod.importlib, "import_module", lambda name: mods[name])
    be = kmod.OfficialKronosBackend(KronosSettings(device="cpu"), kmod.MODEL_SPECS["small"])
    out = be.predict([bar(k) for k in range(3)], pred_len=2, paths=10)
    assert out == [[(101.0, 99.0, 100.0)] * 2]
    kw = calls[0]
    # 官方用法：一次调用采样 10 条、模型内部平均；温度 0.6、top_p 0.9
    assert len(calls) == 1 and kw["sample_count"] == 10
    assert (kw["T"], kw["top_p"]) == (0.6, 0.9)
    stamps = [*kw["x_timestamp"], *kw["y_timestamp"]]
    assert all(t.minute % 5 == 0 and t.second == 0 and t.microsecond == 0 for t in stamps)
    assert kw["x_timestamp"][0] == T0.replace(tzinfo=None)
    assert kw["y_timestamp"][0] == (T0 + timedelta(minutes=15)).replace(tzinfo=None)


class ByCoin(FakeBackend):
    """每个币平均路径终点 = 现价 × (1 + 涨幅)，看排名用。"""

    def __init__(self, ups: dict[str, float]) -> None:
        super().__init__()
        self.ups = ups

    def predict(self, bars: list[Kline], pred_len: int, paths: int) -> list[Path_]:
        end = bars[-1].close * (1 + self.ups[bars[-1].symbol])
        return [[(end + 1, end - 1, end)] * pred_len]


async def _feed_coins(f: KronosForecaster, coins: list[str]) -> None:
    for k in range(3):
        for sym in coins:
            await f.on_kline(bar(k, symbol=sym))
    await f.wait_idle()


async def test_forecasts_are_ranked_across_coins(tmp_path: Path) -> None:
    """官方用法：同一时刻所有币按预测涨幅排名；前 1/6 看好、后 1/6 看坏，中间不表态。"""
    ups = {"A": 0.03, "B": 0.01, "C": 0.004, "D": 0.0, "E": -0.002, "F": -0.02}
    coins = [f"{k}{k}USDT" for k in ups]
    be = ByCoin({f"{k}{k}USDT": v for k, v in ups.items()})
    f = make(True, SET, be, JsonlDecisionLog(tmp_path), clock=lambda: T0 + timedelta(hours=1))
    await _feed_coins(f, coins)
    now = T0 + timedelta(minutes=15)
    got = {s: f.latest(s, now) for s in coins}
    assert all(fc is not None and fc.rank_n == 6 for fc in got.values())
    a, d, fz = got["AAUSDT"], got["DDUSDT"], got["FFUSDT"]
    assert a is not None and a.rank_view == "top" and a.rank_pct == 1.0
    assert a.score == pytest.approx(0.03) and a.p_up == 1.0
    assert fz is not None and fz.rank_view == "bottom" and fz.rank_pct == 0.0 and fz.p_up == 0.0
    assert d is not None and d.rank_view == "mid" and d.p_up == 0.5
    assert got["BBUSDT"].rank_view == "mid"  # type: ignore[union-attr]


async def test_too_few_coins_are_not_ranked(tmp_path: Path) -> None:
    be = ByCoin({"AAUSDT": 0.03, "BBUSDT": -0.03})
    f = make(True, SET, be, JsonlDecisionLog(tmp_path), clock=lambda: T0 + timedelta(hours=1))
    await _feed_coins(f, ["AAUSDT", "BBUSDT"])
    fc = f.latest("AAUSDT", T0 + timedelta(minutes=15))
    assert fc is not None and fc.score is not None and fc.rank_view is None
    loose = make(
        True, SET, be, JsonlDecisionLog(tmp_path), clock=lambda: T0 + timedelta(hours=1),
        cfg=PredictorConfig(mode="all_symbols", rank_min_peers=2),
    )  # fmt: skip
    await _feed_coins(loose, ["AAUSDT", "BBUSDT"])
    fc = loose.latest("AAUSDT", T0 + timedelta(minutes=15))
    assert fc is not None and fc.rank_view == "top" and fc.rank_n == 2


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
    f = make(True, SET, be, cfg=PredictorConfig(mode="on_demand"))
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
             cfg=PredictorConfig(mode="on_demand"), clock=lambda: now)  # fmt: skip
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
        kmod.MODEL_SPECS[n].version for n in ("mini", "small", "base")
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
    f = make(
        True,
        SET,
        be,
        cfg=PredictorConfig(mode="on_demand"),
        clock=lambda: T0 + timedelta(minutes=16),
    )
    f.set_wanted({"BTCUSDT"})
    await f.backfill("BTCUSDT", [bar(k) for k in range(3)])  # 补完马上算，不等收盘
    await f.wait_idle()
    assert be.calls == 1 and f.latest("BTCUSDT", T0 + timedelta(minutes=15)) is not None
    await f.backfill("BTCUSDT", [bar(k) for k in range(3)])  # 重叠的丢掉；已有新鲜预测不重算
    await f.wait_idle()
    assert be.calls == 1
    for minute in range(15, 20):  # 实时 1 分钟线拼出第 4 根 5 分钟线
        await f.on_kline(m1(minute))
    await f.wait_idle()
    assert be.calls == 2
    assert f.latest("BTCUSDT", T0 + timedelta(minutes=20)) is not None


async def test_backfill_mid_bar_needs_the_closed_minutes() -> None:
    """启动在 5 分钟线中间（第 17 分钟）：只有 5 分钟回填会差一根被当缺口；
    把当前这根已收盘的 15、16 分钟也喂进去，第 20 分钟就接上了。"""
    be = FakeBackend()
    f = make(True, SET, be)
    await f.backfill("BTCUSDT", [bar(k) for k in range(3)])  # 0/5/10 分
    for minute in range(17, 20):  # 只有实时线
        await f.on_kline(m1(minute))
    await f.on_kline(m1(20))
    await f.wait_idle()
    assert be.calls == 1  # 只有回填后那一次；15–20 这根拼不完整，回填被当缺口清掉

    be2 = FakeBackend()
    g = make(True, SET, be2)
    await g.backfill("BTCUSDT", [bar(k) for k in range(3)])
    for minute in range(0, 17):  # Runner 回填时拉到的 1 分钟线（早的拼出来是重复的）
        await g.on_kline(m1(minute))
    await g.wait_idle()
    assert be2.calls == 1  # 回填后那一次
    for minute in range(17, 20):
        await g.on_kline(m1(minute))
    await g.wait_idle()
    assert be2.calls == 2 and g.latest("BTCUSDT", T0 + timedelta(minutes=20)) is not None


async def test_log_and_status_carry_the_model_actually_used(tmp_path: Path) -> None:
    sink = JsonlDecisionLog(tmp_path)
    cfg = PredictorConfig(mode="all_symbols", model_size="base")
    f = KronosForecaster(True, SET, FakeBackend(), sink, clock=lambda: T0, cfg=cfg)
    await feed(f, 3)
    rec = sink.read_day("2026-10-22")[-1]
    assert rec.model_version == "Kronos-base@2b55474"  # 模型名 @ 钉死的权重提交号
    assert sink.read_status()[Role.PREDICTOR.value].version == "Kronos-base@2b55474"


async def test_newly_wanted_symbol_with_history_predicts_right_away() -> None:
    """重启后作战卡一到（或首根 K 线触发 set_wanted），历史够的币马上算，不等 5 分钟收盘。"""
    be = FakeBackend()
    f = make(True, SET, be, cfg=PredictorConfig(mode="on_demand"))
    await f.backfill("BTCUSDT", [bar(k) for k in range(3)])
    await f.backfill("ETHUSDT", [bar(k, symbol="ETHUSDT") for k in range(3)])
    await f.wait_idle()
    assert be.calls == 0  # 按需模式，还没有要算的币
    f.set_wanted({"BTCUSDT"})
    await f.wait_idle()
    assert be.calls == 1
    f.set_wanted({"BTCUSDT"})  # 没有新增，不重复算
    f.set_wanted({"BTCUSDT", "SOLUSDT"})  # SOL 没有历史，跳过
    await f.wait_idle()
    assert be.calls == 1


def test_set_wanted_outside_event_loop_is_safe() -> None:
    f = make(True, SET, FakeBackend(), cfg=PredictorConfig(mode="on_demand"))
    f.set_wanted({"BTCUSDT"})


async def test_starting_headline_says_what_it_waits_for(tmp_path: Path) -> None:
    sink = JsonlDecisionLog(tmp_path)
    be = FakeBackend()
    be.delay = 0.2
    f = KronosForecaster(
        True, KronosSettings(**{**SET.__dict__, "infer_timeout_s": 2.0}), be, sink,
        clock=lambda: T0, cfg=PredictorConfig(mode="on_demand"),
    )  # fmt: skip

    def headline() -> str:
        f.heartbeat()
        return sink.read_status()[Role.PREDICTOR.value].headline

    assert "回填" in headline()
    await f.backfill("BTCUSDT", [bar(k) for k in range(3)])
    assert "按需模式" in headline() and "1 个币" in headline()
    f.set_wanted({"BTCUSDT"})
    assert "正在算第一轮" in headline()
    await f.wait_idle()
    assert "今天预测 1 次" in headline()


class OrderBackend(FakeBackend):
    def __init__(self) -> None:
        super().__init__()
        self.order: list[str] = []
        self.delay = 0.05

    def predict(self, bars: list[Kline], pred_len: int, paths: int) -> list[Path_]:
        self.order.append(bars[-1].symbol)
        return super().predict(bars, pred_len, paths)


async def test_held_card_and_meeting_symbols_jump_the_queue() -> None:
    """同一根 5 分钟线收盘、几个币排队时，作战卡/持仓（wanted）和总盘会（priority）的币先算。"""
    be = OrderBackend()
    f = make(True, KronosSettings(**{**SET.__dict__, "infer_timeout_s": 2.0}), be,
             clock=lambda: T0)  # fmt: skip
    syms = ["AAAUSDT", "BBBUSDT", "CCCUSDT", "DDDUSDT", "EEEUSDT"]
    for s in syms:
        f._bars[s] = deque([bar(k, symbol=s) for k in range(2)], maxlen=SET.lookback)
    f.set_wanted({"DDDUSDT"})
    f.set_priority({"EEEUSDT"})  # 历史还不够：不算
    await f.wait_idle()
    assert be.order == []
    for s in syms:  # 按到达顺序 A..E
        await f.on_kline(bar(2, symbol=s))
    await f.wait_idle()
    # A 先到直接开始；排队的 B..E 里 D（持仓/卡）和 E（总盘会）插到前面
    assert be.order == ["AAAUSDT", "DDDUSDT", "EEEUSDT", "BBBUSDT", "CCCUSDT"]


async def test_set_priority_fills_missing_forecasts_right_away() -> None:
    be = FakeBackend()
    f = make(True, SET, be, cfg=PredictorConfig(mode="on_demand"), clock=lambda: T0)
    await f.backfill("BTCUSDT", [bar(k) for k in range(3)])
    await f.backfill("ETHUSDT", [bar(k, symbol="ETHUSDT") for k in range(3)])
    await f.wait_idle()
    assert be.calls == 0
    f.set_priority({"ETHUSDT"})  # 总盘会挑中 → 按需模式下也马上算
    await f.wait_idle()
    assert be.calls == 1 and f.latest("ETHUSDT", T0) is not None
    f.set_priority({"ETHUSDT"})  # 已有新鲜预测，不重算
    await f.wait_idle()
    assert be.calls == 1


async def test_ok_headline_counts_fresh_symbols_and_last_time(tmp_path: Path) -> None:
    sink = JsonlDecisionLog(tmp_path)
    now = T0 + timedelta(minutes=16)
    f = KronosForecaster(True, SET, FakeBackend(), sink, clock=lambda: now,
                         cfg=ALL, display_tz="Asia/Bangkok")  # fmt: skip
    await f.backfill("BTCUSDT", [bar(k) for k in range(3)])
    await f.backfill("ETHUSDT", [bar(k, symbol="ETHUSDT") for k in range(2)])  # 不够，不算数
    await f.wait_idle()
    f.heartbeat()
    st = sink.read_status()[Role.PREDICTOR.value]
    # 最后一根 5 分钟线 00:10–00:15 UTC → 曼谷 07:15
    assert st.headline == "已算 1/1 个币，上次 07:15（今天预测 1 次）"


async def test_hourly_interval_for_long_hold() -> None:
    """24h 档：1 分钟线拼成 1 小时线，预测 pred_len 根 1h，过两根（120 分钟）算过期。"""
    be = SpyBackend()
    s = KronosSettings(**{**SET.__dict__, "interval": Interval.H1, "pred_len": 2})
    f = make(True, s, be)
    for minute in range(3 * 60):  # 3 根完整 1 小时线
        await f.on_kline(m1(minute))
    await f.wait_idle()
    assert be.calls == 1 and [b.interval for b in be.seen[0]] == [Interval.H1] * 3
    fc = f.latest("BTCUSDT", T0 + timedelta(hours=3))
    assert fc is not None and fc.horizon_min == 120
    assert fc.stale_after == T0 + timedelta(hours=5)


def test_predictor_interval_follows_hold() -> None:
    from jiaoyi.core import profiles

    # 官方论文：回看 ≈ 5 × 往后预测；凑不够 64 根（攒够才开算）就用 64
    want = {
        "30m": ("5m", 6, 64), "1h": ("5m", 12, 64), "2h": ("5m", 24, 120),
        "4h": ("15m", 16, 80), "8h": ("15m", 32, 160), "12h": ("15m", 48, 240),
        "24h": ("1h", 24, 120),
    }  # fmt: skip
    for hold, (interval, n, back) in want.items():
        cfg = PredictorConfig.model_validate(profiles.fill({}, "1h", "predictor", hold))
        assert (cfg.interval, cfg.pred_len, cfg.lookback) == (interval, n, back)
        assert (cfg.paths, cfg.temperature) == (10, 0.6)
    # 用户写了就按用户的
    mine = profiles.fill({"interval": "5m"}, "1h", "predictor", "24h")
    assert PredictorConfig.model_validate(mine).interval == "5m"


def test_default_model_is_the_largest() -> None:
    assert PredictorConfig().model_size == "base"


async def test_largest_model_load_failure_falls_back_to_standard(tmp_path: Path) -> None:
    """所选（最大）模型加载失败：这次就改用标准模型出预测，之后一直用标准，状态里写原因。"""
    built: dict[str, FakeBackend] = {}

    class Broken(FakeBackend):
        def predict(self, bars: list[Kline], pred_len: int, paths: int) -> list[Path_]:
            self.calls += 1
            raise kmod.ModelLoadError("Kronos-base 加载失败：OSError: 没有权重")

    def factory(size: str) -> FakeBackend:
        built[size] = Broken() if size == "base" else FakeBackend()
        return built[size]

    sink = JsonlDecisionLog(tmp_path)
    f = KronosForecaster(True, SET, log_sink=sink, cfg=ALL, backend_for=factory,
                         clock=lambda: T0 + timedelta(minutes=15))  # fmt: skip
    await feed(f, 4)
    fc = f.latest("BTCUSDT", T0 + timedelta(minutes=15))
    assert fc is not None and fc.model == kmod.MODEL_SPECS["small"].version
    assert built["base"].calls == 1  # 失败一次后不再试
    st = sink.read_status()[Role.PREDICTOR.value]
    assert st.health == Health.OK and "已改用标准模型" in st.headline and "没有权重" in st.headline


async def test_standard_model_load_failure_is_an_error() -> None:
    class Broken(FakeBackend):
        def predict(self, bars: list[Kline], pred_len: int, paths: int) -> list[Path_]:
            raise kmod.ModelLoadError("Kronos-small 加载失败")

    f = make(True, SET, cfg=PredictorConfig(mode="all_symbols", model_size="small"),
             backend_for=lambda size: Broken())  # fmt: skip
    await feed(f, 3)
    assert f.latest("BTCUSDT", T0 + timedelta(minutes=15)) is None


def test_fetch_weights_pins_revisions(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    import types

    from jiaoyi.predictor import fetch_weights

    got: list[tuple[str, str]] = []
    hub = types.SimpleNamespace(
        snapshot_download=lambda repo_id, revision: got.append((repo_id, revision))
    )
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)
    assert fetch_weights.main([]) == 0
    base, small = kmod.MODEL_SPECS["base"], kmod.MODEL_SPECS["small"]
    assert got == [
        (base.model_id, base.model_revision),
        (base.tokenizer_id, base.tokenizer_revision),
        (small.model_id, small.model_revision),
    ]  # 两个模型共用一个分词器，只拉一次
    assert fetch_weights.main(["huge"]) == 2
