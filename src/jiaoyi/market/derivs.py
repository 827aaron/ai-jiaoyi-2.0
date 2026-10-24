"""持仓量和多空比（币安没有推送，按分钟 / 5 分钟轮询 REST），给哨兵新信号和拍板人快照用。

新信号研究（2026-10-24，/mnt/project-files/research/new-signals/spec.md）：

- 持仓量：作战卡上的币每 60 秒查一次 ``/fapi/v1/openInterest``（权重 1），内存里留 24 小时；
  第一次见到的币用 ``/futures/data/openInterestHist``（5 分钟，最多 288 根）补历史。
- 大户持仓多空比 / 全市场人数多空比（至少 4 小时）：每 5 分钟查
  ``/futures/data/topLongShortPositionRatio``、``globalLongShortAccountRatio``
  （权重 0，各 5 分钟限 1000 次）。
- 自动减仓风险：每 30 分钟查一次 ``/fapi/v1/symbolAdlRisk``（全市场，权重 1），保镖遇到「高」
  不开新仓（``adl_risk``）；查不到 / 超过 2 小时没更新当没有，不挡。
- 查失败就跳过这一轮：不出信号、不报错刷屏（同一个币连着失败只记一行日志）。

``values()`` 的键（登记在 docs/fast_loop.md）：

- ``oi15`` / ``oi60`` / ``oi4h`` / ``oi_w_pct``：持仓量变化 %；
- ``top_ls`` / ``crowd_ls``：最新比值；``top_w_pct`` / ``crowd_w_pct``：窗口内对数变化 %；
- ``div4h``：大户 4 小时变化 % − 散户 4 小时变化 %（正 = 大户比散户更看多）。

拿不到的键不写。
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import deque
from collections.abc import Awaitable, Callable, Collection
from datetime import datetime
from typing import Any

from jiaoyi.market.rest import fetch_json

log = logging.getLogger(__name__)

OI_EVERY_S = 60.0
RATIO_EVERY_S = 300.0
ADL_EVERY_S = 1800.0
ADL_STALE_S = 7200.0
KEEP_S = 24 * 3600.0 + 600.0
STALE_GAP_S = 600.0
"""找「W 分钟前」的点时允许差多少秒（5 分钟一根的历史 + 轮询抖动）。"""
CONCURRENCY = 4
DIV_MIN = 240
"""拍板人快照 ``div4h`` 的窗口（分钟）。"""

Fetch = Callable[[str, dict[str, Any], int], Awaitable[Any]]
"""(路径, 参数, 权重) → JSON。测试换成假的。"""


async def _rest(path: str, params: dict[str, Any], weight: int) -> Any:
    return await fetch_json(path, params, weight=weight)


class _Series:
    """(秒级时间戳, 值)，按时间排，只留 24 小时。"""

    def __init__(self) -> None:
        self.pts: deque[tuple[float, float]] = deque()

    def add(self, t: float, v: float) -> None:
        if v <= 0 or (self.pts and t <= self.pts[-1][0]):
            return
        self.pts.append((t, v))
        while self.pts and self.pts[0][0] < t - KEEP_S:
            self.pts.popleft()

    def merge(self, rows: list[tuple[float, float]]) -> None:
        """补历史：比已有最早的点还早的才插进去（实时轮询的点优先）。"""
        first = self.pts[0][0] if self.pts else math.inf
        old = sorted((t, v) for t, v in rows if t < first and v > 0)
        self.pts.extendleft(reversed(old))

    def latest(self) -> tuple[float, float] | None:
        return self.pts[-1] if self.pts else None

    def at(self, t: float) -> float | None:
        """``t`` 之前最近的一个点（差 ``STALE_GAP_S`` 以上算没有）。"""
        best: tuple[float, float] | None = None
        for p in self.pts:
            if p[0] > t:
                break
            best = p
        if best is None or t - best[0] > STALE_GAP_S:
            return None
        return best[1]


class DerivsFeed:
    def __init__(
        self,
        *,
        oi_window_min: int = 240,
        ratio_window_min: int = 240,
        fetch: Fetch = _rest,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._oi_w = oi_window_min
        self._ratio_w = ratio_window_min
        self._fetch = fetch
        self._clock = clock
        self._oi: dict[str, _Series] = {}
        self._top: dict[str, _Series] = {}
        self._crowd: dict[str, _Series] = {}
        self._seeded: set[str] = set()
        self._failing: set[str] = set()
        self._adl: dict[str, str] = {}
        self._adl_at: float | None = None

    # ------------------------------------------------------------------ 读

    def values(self, symbol: str, now: datetime) -> dict[str, float]:
        t = now.timestamp()
        out: dict[str, float] = {}
        oi = self._oi.get(symbol)
        if oi is not None:
            for key, minutes in (
                ("oi15", 15),
                ("oi60", 60),
                ("oi4h", 240),
                ("oi_w_pct", self._oi_w),
            ):
                pct = _pct(oi, t, minutes)
                if pct is not None:
                    out[key] = pct
        top, crowd = self._top.get(symbol), self._crowd.get(symbol)
        for name, s in (("top", top), ("crowd", crowd)):
            last = s.latest() if s is not None else None
            if s is None or last is None or t - last[0] > RATIO_EVERY_S * 3:
                continue
            out[f"{name}_ls"] = round(last[1], 4)
            chg = _log_pct(s, t, self._ratio_w)
            if chg is not None:
                out[f"{name}_w_pct"] = chg
        if top is not None and crowd is not None:
            a, b = _log_pct(top, t, DIV_MIN), _log_pct(crowd, t, DIV_MIN)
            if a is not None and b is not None:
                out["div4h"] = round(a - b, 3)
        return out

    def adl_risk(self, symbol: str) -> str | None:
        """币安自动减仓风险 ``low`` / ``medium`` / ``high``；没查到或太旧返回 None（不挡）。"""
        if self._adl_at is None or self._clock() - self._adl_at > ADL_STALE_S:
            return None
        return self._adl.get(symbol)

    # ------------------------------------------------------------------ 轮询

    async def run(self, symbols: Callable[[], Collection[str]]) -> None:
        """一直跑：每 60 秒持仓量、每 5 分钟多空比。``symbols`` 每轮现取（作战卡上的币）。"""
        last_ratio = last_adl = -math.inf
        while True:
            try:
                syms = sorted(symbols())
                await self.poll_oi(syms)
                if self._clock() - last_ratio >= RATIO_EVERY_S:
                    last_ratio = self._clock()
                    await self.poll_ratios(syms)
                if self._clock() - last_adl >= ADL_EVERY_S:
                    last_adl = self._clock()
                    await self.poll_adl()
            except asyncio.CancelledError:
                raise
            except Exception:  # 只是参考数据：出错不能让主循环跟着退出
                log.exception("持仓量 / 多空比这一轮出错，下一轮再查")
            await asyncio.sleep(OI_EVERY_S)

    async def poll_oi(self, symbols: Collection[str]) -> None:
        sem = asyncio.Semaphore(CONCURRENCY)

        async def one(sym: str) -> None:
            async with sem:
                if sym not in self._seeded:
                    self._seeded.add(sym)
                    await self._seed(sym)
                data = await self._get(sym, "/fapi/v1/openInterest", {"symbol": sym}, 1)
                if isinstance(data, dict) and data.get("openInterest") is not None:
                    t = float(data.get("time") or self._clock() * 1000) / 1000.0
                    self._oi.setdefault(sym, _Series()).add(t, float(data["openInterest"]))

        await asyncio.gather(*(one(s) for s in symbols))

    async def poll_ratios(self, symbols: Collection[str]) -> None:
        limit = max(self._ratio_w, DIV_MIN) // 5 + 2
        sem = asyncio.Semaphore(CONCURRENCY)

        async def one(sym: str) -> None:
            async with sem:
                for path, store in (
                    ("/futures/data/topLongShortPositionRatio", self._top),
                    ("/futures/data/globalLongShortAccountRatio", self._crowd),
                ):
                    params = {"symbol": sym, "period": "5m", "limit": limit}
                    rows = _rows(await self._get(sym, path, params, 0), "longShortRatio")
                    s = store.setdefault(sym, _Series())
                    s.merge(rows)
                    for t, v in rows:
                        s.add(t, v)

        await asyncio.gather(*(one(s) for s in symbols))

    async def poll_adl(self) -> None:
        data = await self._get("*", "/fapi/v1/symbolAdlRisk", {}, 1)
        if not isinstance(data, list):
            return
        out = {
            str(r["symbol"]): str(r["adlRisk"]).lower()
            for r in data
            if isinstance(r, dict) and r.get("symbol") and r.get("adlRisk")
        }
        if out:
            self._adl, self._adl_at = out, self._clock()

    async def _seed(self, sym: str) -> None:
        params = {"symbol": sym, "period": "5m", "limit": 288}
        rows = _rows(
            await self._get(sym, "/futures/data/openInterestHist", params, 0), "sumOpenInterest"
        )
        if rows:
            self._oi.setdefault(sym, _Series()).merge(rows)

    async def _get(self, sym: str, path: str, params: dict[str, Any], weight: int) -> Any:
        try:
            data = await self._fetch(path, params, weight)
        except Exception as e:
            if sym not in self._failing:
                self._failing.add(sym)
                log.warning("%s 查 %s 失败，这一轮跳过：%r", sym, path, e)
            return None
        self._failing.discard(sym)
        return data


def _rows(data: Any, key: str) -> list[tuple[float, float]]:
    if not isinstance(data, list):
        return []
    out: list[tuple[float, float]] = []
    for r in data:
        try:
            out.append((float(r["timestamp"]) / 1000.0, float(r[key])))
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(out)


def _pct(s: _Series, t: float, minutes: int) -> float | None:
    last = s.latest()
    if last is None or t - last[0] > STALE_GAP_S:
        return None
    then = s.at(last[0] - minutes * 60.0)
    if not then:
        return None
    return round((last[1] / then - 1.0) * 100.0, 3)


def _log_pct(s: _Series, t: float, minutes: int) -> float | None:
    last = s.latest()
    if last is None:
        return None
    then = s.at(last[0] - minutes * 60.0)
    if not then:
        return None
    return round(math.log(last[1] / then) * 100.0, 3)
