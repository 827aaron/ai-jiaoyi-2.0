"""预测员：官方 Kronos（shiyu-coder/Kronos，MIT）的适配，带开关。

只用来估价格范围（波动区间），方向近似抛硬币，``p_up`` 不能当方向信号用。
设计要点：

- 开关关闭、数据不够、推理失败、结果过期，``latest`` 一律返回 ``None``，快环无感运行。
- 推理放在单线程池里，不占事件循环；同一标的上一次没算完就丢弃新的，不排队。
- 吃 5 分钟线：行情只推 1 分钟线，这里按 UTC 整 5 分钟聚合（缺分钟的整根丢掉）；
  启动和新币入池时用 REST 回填 5 分钟线（``backfill``），回填后下一根 5 分钟线收盘就出预测。
- 按需（``PredictorConfig.mode = on_demand``，默认）：只算作战卡里的币和有持仓的币
  （``set_wanted``）；哨兵出信号而这个币没有新鲜预测时 ``request`` 补算一次，也在后台。
- 模型尺寸：fixed 用 ``model_size``；adaptive 按排队深度在 base / small / mini 间切换，
  各尺寸用到才加载，权重都钉 HuggingFace 提交号（``MODEL_SPECS``）。
- 设备 auto：CUDA > MPS > CPU（Mac mini 上用 MPS）；MPS/CUDA 推理出错会退回 CPU 并一直用 CPU。
  任何异常都只记状态，不向外抛。
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
    PredictorConfig,
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
class ModelSpec:
    model_id: str
    model_revision: str
    tokenizer_id: str
    tokenizer_revision: str
    max_context: int
    name: str


# 升级权重要单独评审：提交号是钉死的
MODEL_SPECS: dict[str, ModelSpec] = {
    "mini": ModelSpec(
        "NeoQuasar/Kronos-mini",
        "f4e68697d9d5aed55cef5c96aabc3376bcad9f81",
        "NeoQuasar/Kronos-Tokenizer-2k",
        "26966d0035065a0cae0ebad7af8ece35bc1fb51c",
        2048,
        "Kronos-mini",
    ),
    "small": ModelSpec(
        "NeoQuasar/Kronos-small",
        "901c26c1332695a2a8f243eb2f37243a37bea320",
        "NeoQuasar/Kronos-Tokenizer-base",
        "0e0117387f39004a9016484a186a908917e22426",
        512,
        "Kronos-small",
    ),
    "base": ModelSpec(
        "NeoQuasar/Kronos-base",
        "2b554741eca47781b64468546e77fef3e85130e6",
        "NeoQuasar/Kronos-Tokenizer-base",
        "0e0117387f39004a9016484a186a908917e22426",
        512,
        "Kronos-base",
    ),
}


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
    model_revision: str = "901c26c1332695a2a8f243eb2f37243a37bea320"
    """HuggingFace 提交号，钉死权重版本；升级要单独评审。"""
    tokenizer_id: str = "NeoQuasar/Kronos-Tokenizer-base"
    tokenizer_revision: str = "0e0117387f39004a9016484a186a908917e22426"
    repo_dir: str = "vendor/Kronos"
    """官方 Kronos 源码目录（它不是 pip 包），见 predictor/README.md。"""
    device: str = "auto"
    """``auto``：CUDA > MPS > CPU；也可写 ``cpu`` / ``mps``。"""
    model_name: str = "Kronos-small"


class KronosBackend(Protocol):
    """真正跑模型的一层，测试里用假的替换。同步接口，由预测员丢进线程池。"""

    def predict(self, bars: list[Kline], pred_len: int, paths: int) -> list[Path_]: ...


class OfficialKronosBackend:
    """官方 ``KronosPredictor``。torch / pandas 只在第一次推理时导入。"""

    def __init__(self, settings: KronosSettings, spec: ModelSpec | None = None) -> None:
        self._s = settings
        self._spec = spec or ModelSpec(
            settings.model_id,
            settings.model_revision,
            settings.tokenizer_id,
            settings.tokenizer_revision,
            512,
            settings.model_name,
        )
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
        # Mac mini M4 实测（切 eval 之后）：small 在 MPS 上 0.8 秒/币，CPU 1.4 秒/币。
        # MPS 推理出错时 predict 会退回 CPU 并一直用 CPU
        if torch.backends.mps.is_available():
            return "mps"
        return "cpu"

    def _load(self, device: str) -> None:
        os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
        repo = str(Path(self._s.repo_dir).resolve())
        if repo not in sys.path:
            sys.path.insert(0, repo)
        mod = importlib.import_module("model")
        sp = self._spec
        # from_pretrained（safetensors）不会切到 eval：不切的话推理时 dropout 一直开着，
        # 注意力也变成训练用的因果模式（MPS 报 "does not support dropout" 就是这个原因）
        tokenizer = mod.KronosTokenizer.from_pretrained(
            sp.tokenizer_id, revision=sp.tokenizer_revision
        ).eval()
        model = mod.Kronos.from_pretrained(sp.model_id, revision=sp.model_revision).eval()
        self._predictor = mod.KronosPredictor(
            model, tokenizer, device=device, max_context=sp.max_context
        )
        self._device = device
        log.info("Kronos 已加载：%s，设备 %s", sp.model_id, device)

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
        # 时间戳用开盘时间，分钟落在 5 分钟格点上（和官方训练数据一致）；收盘时间是 xx:04:59.999
        x_ts = pd.Series(pd.to_datetime([b.open_time for b in bars], utc=True).tz_localize(None))
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
        *,
        cfg: PredictorConfig | None = None,
        backend_for: Callable[[str], KronosBackend] | None = None,
    ) -> None:
        """``backend`` 给了就所有尺寸都用它（测试）；否则按尺寸用 ``backend_for`` 现建、缓存。"""
        self._enabled = enabled
        self._s = settings or KronosSettings()
        self._cfg = cfg or PredictorConfig()
        self._fixed_backend = backend
        self._backend_for = backend_for or (
            lambda size: OfficialKronosBackend(self._s, MODEL_SPECS[size])
        )
        self._backends: dict[str, KronosBackend] = {}
        self._sink = log_sink
        self._clock = clock
        self._bars: dict[str, deque[Kline]] = {}
        # 正在聚合的 5 分钟线：标的 → (起点分钟, 开, 高, 低, 收, 量, 成交额, 已有几根 1 分钟)
        self._partial: dict[str, tuple[int, float, float, float, float, float, float, int]] = {}
        self._wanted: set[str] = set()
        self._queued = 0
        self._last_model = MODEL_SPECS[self._cfg.model_size].name  # 最近一次实际用的模型
        self._forecasts: dict[str, Forecast] = {}
        self._busy: set[str] = set()
        self._tasks: set[asyncio.Task[None]] = set()
        self._gate = asyncio.Semaphore(1)
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

    @property
    def mode(self) -> str:
        return self._cfg.mode

    def set_wanted(self, symbols: set[str] | frozenset[str]) -> None:
        """按需模式下要算的币：作战卡里的 + 有持仓的。主循环换卡、刷新持仓时调。

        新加进来的币如果历史已经够（启动回填过），马上在后台算一次，不等下一根 5 分钟线收盘。
        """
        added = set(symbols) - self._wanted
        self._wanted = set(symbols)
        for sym in sorted(added):
            self._request_now(sym)

    def _request_now(self, symbol: str) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return  # 不在事件循环里（测试或启动前）：等下一根 5 分钟线
        self.request(symbol)

    def request(self, symbol: str) -> None:
        """哨兵出信号时调：这个币没有新鲜预测就在后台补算一次。立即返回，不等结果。"""
        if not self._enabled:
            return
        if self.latest(symbol, self._clock()) is not None:
            return
        buf = self._bars.get(symbol)
        if buf is None or len(buf) < self._s.min_bars or symbol in self._busy:
            return
        self._spawn(symbol, list(buf), buf[-1])

    async def backfill(self, symbol: str, bars: list[Kline]) -> None:
        """用 REST 拉来的 5 分钟线把历史补上。这个币要算（全部模式或在 wanted 里）就马上算一次。"""
        for b in bars:
            self._append(b)
        if self._cfg.mode == "all_symbols" or symbol in self._wanted:
            self.request(symbol)

    def latest(self, symbol: str, now: datetime) -> Forecast | None:
        if not self._enabled:
            return None
        fc = self._forecasts.get(symbol)
        if fc is None or now >= fc.stale_after:
            return None
        return fc

    async def on_kline(self, kline: Kline) -> None:
        """收 1 分钟线（聚合）或模型周期的线。只做记账和派活，立即返回，推理在后台。"""
        if not self._enabled:
            return
        if kline.interval == Interval.M1 and self._s.interval != Interval.M1:
            done = self._aggregate(kline)
            if done is None:
                return
            kline = done
        elif kline.interval != self._s.interval:
            return
        if not self._append(kline):
            return
        sym = kline.symbol
        if self._cfg.mode == "on_demand" and sym not in self._wanted:
            return
        buf = self._bars[sym]
        if len(buf) < self._s.min_bars or sym in self._busy:
            return
        self._spawn(sym, list(buf), kline)

    def _aggregate(self, k: Kline) -> Kline | None:
        """1 分钟线按 UTC 整点拼成模型周期的线；拼完一根返回它，缺分钟的整根丢掉。"""
        n = _INTERVAL_MIN[self._s.interval]
        minute = int(k.open_time.timestamp() // 60)
        start = minute - minute % n
        cur = self._partial.get(k.symbol)
        if cur is not None and cur[0] != start:
            cur = None
        if cur is None:
            if minute != start:
                self._partial.pop(k.symbol, None)
                return None
            cur = (start, k.open, k.high, k.low, k.close, k.volume, k.quote_volume, 1)
        else:
            s, o, h, lo, _, v, q, cnt = cur
            cur = (
                s, o, max(h, k.high), min(lo, k.low), k.close,
                v + k.volume, q + k.quote_volume, cnt + 1,
            )  # fmt: skip
        if minute != start + n - 1:
            self._partial[k.symbol] = cur
            return None
        self._partial.pop(k.symbol, None)
        s, o, h, lo, c, v, q, cnt = cur
        if cnt != n:
            return None
        open_time = datetime.fromtimestamp(s * 60, UTC)
        return Kline(
            symbol=k.symbol,
            interval=self._s.interval,
            open_time=open_time,
            close_time=k.close_time,
            open=o, high=h, low=lo, close=c, volume=v, quote_volume=q,
        )  # fmt: skip

    def _append(self, kline: Kline) -> bool:
        """放进缓冲；重复或乱序返回 False。"""
        sym = kline.symbol
        buf = self._bars.setdefault(sym, deque(maxlen=self._s.lookback))
        step = timedelta(minutes=_INTERVAL_MIN[self._s.interval])
        if buf:
            last = buf[-1]
            if kline.open_time <= last.open_time:
                return False  # 重复或乱序（回填和实时重叠）
            if kline.open_time - last.open_time > step:
                buf.clear()  # 有缺口，旧数据不能和新数据拼成一段连续序列
                self._forecasts.pop(sym, None)
        buf.append(kline)
        return True

    def _spawn(self, sym: str, bars: list[Kline], last: Kline) -> None:
        # 推理放后台，调用方立即返回，不卡行情循环
        self._busy.add(sym)
        self._queued += 1
        task = asyncio.get_running_loop().create_task(self._run(sym, bars, last))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self, sym: str, bars: list[Kline], last: Kline) -> None:
        try:
            await self._infer(sym, bars, last)
        finally:
            self._busy.discard(sym)

    def _pick_size(self, waiting: int) -> str:
        cfg = self._cfg
        if cfg.size_policy == "fixed":
            return cfg.model_size
        if waiting >= cfg.adaptive_mini_min_queue:
            return "mini"
        if waiting <= cfg.adaptive_base_max_queue:
            return "base"
        return "small"

    def _backend(self, size: str) -> KronosBackend:
        if self._fixed_backend is not None:
            return self._fixed_backend
        be = self._backends.get(size)
        if be is None:  # 用到才建，权重第一次推理时才加载
            be = self._backends[size] = self._backend_for(size)
        return be

    async def wait_idle(self) -> None:
        """等后台推理全部结束（测试和优雅退出用）。"""
        while self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _infer(self, sym: str, bars: list[Kline], last: Kline) -> None:
        t0 = time.perf_counter()
        loop = asyncio.get_running_loop()
        size: str = self._cfg.model_size
        dequeued = False
        try:
            # 超时从真正开始推理算起，排队等模型的时间不计
            async with self._gate:
                self._queued -= 1
                dequeued = True
                size = self._pick_size(self._queued)  # 还在排队的有几个
                backend = self._backend(size)
                t0 = time.perf_counter()
                paths = await asyncio.wait_for(
                    loop.run_in_executor(
                        self._pool, backend.predict, bars, self._s.pred_len, self._s.paths
                    ),
                    timeout=self._s.infer_timeout_s,
                )
            fc = self._summarize(sym, paths, last, MODEL_SPECS[size].name)
        except Exception as exc:  # 预测员任何故障都不能影响快环
            self._record(sym, (time.perf_counter() - t0) * 1000, None, exc, MODEL_SPECS[size].name)
            return
        finally:
            if not dequeued:
                self._queued -= 1
        if not self._enabled:  # 推理期间被关掉
            return
        self._forecasts[sym] = fc
        self._record(sym, (time.perf_counter() - t0) * 1000, fc, None, fc.model)

    def _summarize(self, sym: str, paths: list[Path_], last: Kline, model: str) -> Forecast:
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
            model=model,
            stale_after=last.close_time + timedelta(minutes=2 * step_min),
        )

    def _record(
        self,
        sym: str,
        latency_ms: float,
        fc: Forecast | None,
        exc: Exception | None,
        model: str,
    ) -> None:
        now = self._clock()
        self._last_model = model
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
                        model_version=model,
                    )
                )
            except OSError:
                log.exception("预测员写决策日志失败")
        self.heartbeat()

    def _starting_headline(self) -> str:
        ready = {s for s, b in self._bars.items() if len(b) >= self._s.min_bars}
        if not ready:
            return "等待历史 K 线（启动时从币安回填；一直这样说明回填失败，看日志）"
        if self._busy:
            return f"{len(ready)} 个币历史已就绪，正在算第一轮预测"
        if self._cfg.mode == "on_demand" and not ready & self._wanted:
            return f"{len(ready)} 个币历史已就绪；按需模式，作战卡或持仓里有币才预测"
        return f"{len(ready)} 个币历史已就绪，等下一根 5 分钟线"

    def heartbeat(self, now: datetime | None = None) -> None:
        """上报模块状态；外部也可以定时调用，让面板看到它还活着。

        时间用自己的 clock，``now`` 只为和其它模块统一签名（App.heartbeat_all）。
        """
        if self._sink is None:
            return
        if not self._enabled:
            health, headline = Health.DISABLED, "预测员已关闭，快环不使用预测"
        elif self._consec_errors >= 3:
            health, headline = Health.DOWN, "连续推理失败，已停止提供预测"
        elif self._consec_errors > 0:
            health, headline = Health.DEGRADED, "最近一次推理失败"
        elif self._calls == 0:
            health, headline = Health.STARTING, self._starting_headline()
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
                    version=self._last_model,
                )
            )
        except OSError:
            log.exception("预测员上报状态失败")

    def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        self._pool.shutdown(wait=False, cancel_futures=True)
