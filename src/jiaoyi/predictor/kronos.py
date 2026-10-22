"""预测员：官方 Kronos（shiyu-coder/Kronos，MIT）的适配，带开关。

只用来估价格范围（波动区间），方向近似抛硬币，``p_up`` 不能当方向信号用。
设计要点：

- 开关关闭、数据不够、推理失败、结果过期，``latest`` 一律返回 ``None``，快环无感运行。
- 推理放在单线程池里，不占事件循环；同一标的上一次没算完就丢弃新的，不排队。
- Mac mini 上 MPS 报错自动退回 CPU；任何异常都只记状态，不向外抛。
- 每次推理写一条 ``DecisionRecord`` 并上报 ``ModuleStatus``（耗时、成功率、最近错误）。
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import math
import os
import sys
import time
import uuid
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from jiaoyi.contracts import (
    DecisionRecord,
    Forecast,
    Health,
    Kline,
    ModuleStatus,
    Outcome,
    Role,
    TodayStats,
)
from jiaoyi.contracts.market import Interval
from jiaoyi.interfaces import DecisionLog

log = logging.getLogger(__name__)

_INTERVAL_MIN = {Interval.M1: 1, Interval.M5: 5, Interval.M15: 15, Interval.H1: 60}

# 一条预测路径：每一步的 (最高, 最低, 收盘)。
Path_ = list[tuple[float, float, float]]


@dataclass(frozen=True)
class KronosSettings:
    """预测员自己的参数。开关不在这里，用 ``AppConfig.predictor_enabled``。"""

    interval: Interval = Interval.M5
    lookback: int = 400
    """喂给模型的历史 K 线根数（官方 Kronos-small 上下文上限 512）。"""
    min_bars: int = 64
    """攒够这么多根才开始预测。"""
    pred_len: int = 12
    """往后预测几根（5 分钟线 × 12 = 1 小时）。"""
    paths: int = 4
    """独立采样几条路径；范围取所有路径的包络，p_up 取收在现价之上的比例。"""
    infer_timeout_s: float = 30.0
    model_id: str = "NeoQuasar/Kronos-small"
    tokenizer_id: str = "NeoQuasar/Kronos-Tokenizer-base"
    repo_dir: str = "vendor/Kronos"
    """官方 Kronos 源码目录（它不是 pip 包），见 predictor/README.md。"""
    device: str = "auto"
    """``auto``：有 MPS/CUDA 用，否则 CPU；也可写 ``cpu``。"""
    model_name: str = "Kronos-small"


class KronosBackend(Protocol):
    """真正跑模型的一层，测试里用假的替换。同步接口，由预测员丢进线程池。"""

    def predict(self, bars: list[Kline], pred_len: int, paths: int) -> list[Path_]: ...


class OfficialKronosBackend:
    """官方 ``KronosPredictor``。torch / pandas 只在第一次推理时导入。"""

    def __init__(self, settings: KronosSettings) -> None:
        self._s = settings
        self._predictor: Any = None
        self._device = ""

    def predict(self, bars: list[Kline], pred_len: int, paths: int) -> list[Path_]:
        if self._predictor is None:
            self._load(self._pick_device())
        try:
            return self._run(bars, pred_len, paths)
        except Exception:
            if self._device in ("mps", "cuda"):
                log.warning("Kronos 在 %s 上推理失败，退回 CPU 重试", self._device, exc_info=True)
                self._load("cpu")
                return self._run(bars, pred_len, paths)
            raise

    def _pick_device(self) -> str:
        if self._s.device != "auto":
            return self._s.device
        torch = importlib.import_module("torch")
        if torch.cuda.is_available():
            return "cuda"
        if torch.backends.mps.is_available():
            return "mps"
        return "cpu"

    def _load(self, device: str) -> None:
        os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
        repo = str(Path(self._s.repo_dir).resolve())
        if repo not in sys.path:
            sys.path.insert(0, repo)
        mod = importlib.import_module("model")
        tokenizer = mod.KronosTokenizer.from_pretrained(self._s.tokenizer_id)
        model = mod.Kronos.from_pretrained(self._s.model_id)
        self._predictor = mod.KronosPredictor(model, tokenizer, device=device, max_context=512)
        self._device = device
        log.info("Kronos 已加载：%s，设备 %s", self._s.model_id, device)

    def _run(self, bars: list[Kline], pred_len: int, paths: int) -> list[Path_]:
        pd = importlib.import_module("pandas")
        df = pd.DataFrame(
            {
                "open": [b.open for b in bars],
                "high": [b.high for b in bars],
                "low": [b.low for b in bars],
                "close": [b.close for b in bars],
                "volume": [b.volume for b in bars],
                "amount": [b.quote_volume for b in bars],
            }
        )
        x_ts = pd.Series(pd.to_datetime([b.close_time for b in bars], utc=True).tz_localize(None))
        step = pd.Timedelta(minutes=_INTERVAL_MIN[self._s.interval])
        y_ts = pd.Series([x_ts.iloc[-1] + step * (i + 1) for i in range(pred_len)])
        out: list[Path_] = []
        for _ in range(paths):
            pred = self._predictor.predict(
                df=df,
                x_timestamp=x_ts,
                y_timestamp=y_ts,
                pred_len=pred_len,
                T=1.0,
                top_p=0.9,
                sample_count=1,
                verbose=False,
            )
            out.append(
                [
                    (float(h), float(lo), float(c))
                    for h, lo, c in zip(pred["high"], pred["low"], pred["close"], strict=True)
                ]
            )
        return out


class KronosForecaster:
    """满足 ``interfaces.Predictor``。"""

    def __init__(
        self,
        enabled: bool,
        settings: KronosSettings | None = None,
        backend: KronosBackend | None = None,
        log_sink: DecisionLog | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._enabled = enabled
        self._s = settings or KronosSettings()
        self._backend: KronosBackend = backend or OfficialKronosBackend(self._s)
        self._sink = log_sink
        self._clock = clock
        self._bars: dict[str, deque[Kline]] = {}
        self._forecasts: dict[str, Forecast] = {}
        self._busy: set[str] = set()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="kronos")
        self._calls = 0
        self._errors = 0
        self._consec_errors = 0
        self._latency_sum = 0.0
        self._last_error: str | None = None
        self._day = self._clock().date()

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        """运行中切换开关。关闭时清掉已有预测，立即上报状态。"""
        self._enabled = enabled
        if not enabled:
            self._forecasts.clear()
            self._busy.clear()
        self.heartbeat()

    def latest(self, symbol: str, now: datetime) -> Forecast | None:
        if not self._enabled:
            return None
        fc = self._forecasts.get(symbol)
        if fc is None or now >= fc.stale_after:
            return None
        return fc

    async def on_kline(self, kline: Kline) -> None:
        if not self._enabled or kline.interval != self._s.interval:
            return
        sym = kline.symbol
        buf = self._bars.setdefault(sym, deque(maxlen=self._s.lookback))
        step = timedelta(minutes=_INTERVAL_MIN[self._s.interval])
        if buf:
            last = buf[-1]
            if kline.open_time <= last.open_time:
                return  # 重复或乱序
            if kline.open_time - last.open_time > step:
                buf.clear()  # 有缺口，旧数据不能和新数据拼成一段连续序列
                self._forecasts.pop(sym, None)
        buf.append(kline)
        if len(buf) < self._s.min_bars or sym in self._busy:
            return
        self._busy.add(sym)
        try:
            await self._infer(sym, list(buf), kline)
        finally:
            self._busy.discard(sym)

    async def _infer(self, sym: str, bars: list[Kline], last: Kline) -> None:
        t0 = time.perf_counter()
        loop = asyncio.get_running_loop()
        try:
            paths = await asyncio.wait_for(
                loop.run_in_executor(
                    self._pool, self._backend.predict, bars, self._s.pred_len, self._s.paths
                ),
                timeout=self._s.infer_timeout_s,
            )
            fc = self._summarize(sym, paths, last)
        except Exception as exc:  # 预测员任何故障都不能影响快环
            self._record(sym, (time.perf_counter() - t0) * 1000, None, exc)
            return
        if not self._enabled:  # 推理期间被关掉
            return
        self._forecasts[sym] = fc
        self._record(sym, (time.perf_counter() - t0) * 1000, fc, None)

    def _summarize(self, sym: str, paths: list[Path_], last: Kline) -> Forecast:
        if not paths or any(len(p) != self._s.pred_len for p in paths):
            raise ValueError("模型返回的路径数量或长度不对")
        flat = [v for p in paths for step in p for v in step]
        if any(not math.isfinite(v) or v <= 0 for v in flat):
            raise ValueError("模型返回了非正数或非有限数")
        low = min(min(s[1], s[2]) for p in paths for s in p)
        high = max(max(s[0], s[2]) for p in paths for s in p)
        p_up = sum(1 for p in paths if p[-1][2] > last.close) / len(paths)
        step_min = _INTERVAL_MIN[self._s.interval]
        return Forecast(
            symbol=sym,
            ts=last.close_time,
            horizon_min=self._s.pred_len * step_min,
            price_low=low,
            price_high=high,
            p_up=p_up,
            model=self._s.model_name,
            stale_after=last.close_time + timedelta(minutes=2 * step_min),
        )

    def _record(
        self, sym: str, latency_ms: float, fc: Forecast | None, exc: Exception | None
    ) -> None:
        now = self._clock()
        if now.date() != self._day:
            self._day = now.date()
            self._calls = self._errors = 0
            self._latency_sum = 0.0
        self._calls += 1
        self._latency_sum += latency_ms
        if exc is None:
            self._consec_errors = 0
        else:
            self._errors += 1
            self._consec_errors += 1
            self._last_error = f"{type(exc).__name__}: {exc}"[:300]
            log.warning("Kronos 推理失败 %s：%s", sym, self._last_error)
        if self._sink is not None:
            if fc is not None:
                summary = f"{sym} 未来 {fc.horizon_min} 分钟范围 {fc.price_low:g}–{fc.price_high:g}"
                out: dict[str, Any] = fc.model_dump(mode="json")
            else:
                summary = f"{sym} 预测失败，本轮无预测"
                out = {"error": self._last_error}
            try:
                self._sink.write(
                    DecisionRecord(
                        record_id=uuid.uuid4().hex,
                        ts=now,
                        role=Role.PREDICTOR,
                        symbol=sym,
                        outcome=Outcome.INFO if fc is not None else Outcome.ERROR,
                        summary=summary,
                        output=out,
                        latency_ms=latency_ms,
                        model_version=self._s.model_name,
                    )
                )
            except OSError:
                log.exception("预测员写决策日志失败")
        self.heartbeat()

    def heartbeat(self) -> None:
        """上报模块状态；外部也可以定时调用，让面板看到它还活着。"""
        if self._sink is None:
            return
        if not self._enabled:
            health, headline = Health.DISABLED, "预测员已关闭，快环不使用预测"
        elif self._consec_errors >= 3:
            health, headline = Health.DOWN, "连续推理失败，已停止提供预测"
        elif self._consec_errors > 0:
            health, headline = Health.DEGRADED, "最近一次推理失败"
        elif self._calls == 0:
            health, headline = Health.STARTING, "等待 K 线攒够再开始预测"
        else:
            health, headline = Health.OK, f"今天预测 {self._calls} 次"
        avg = self._latency_sum / self._calls if self._calls else 0.0
        try:
            self._sink.heartbeat(
                ModuleStatus(
                    role=Role.PREDICTOR,
                    health=health,
                    ts=self._clock(),
                    headline=headline,
                    today=TodayStats(
                        calls=self._calls,
                        passed=self._calls - self._errors,
                        errors=self._errors,
                        avg_latency_ms=avg,
                    ),
                    last_error=self._last_error,
                    version=self._s.model_name,
                )
            )
        except OSError:
            log.exception("预测员上报状态失败")

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
