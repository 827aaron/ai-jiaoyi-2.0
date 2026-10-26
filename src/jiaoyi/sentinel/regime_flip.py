"""大盘翻转：规则、观测记录与只供事后评估的收益回填。

规则本身不联网、不调用 AI。行情由主循环注入；方向只接受总盘会的结构化字段。
疑似事件每次都进哨兵决策日志，独立观测文件的疑似节流可热更新，默认 30 分钟。
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import statistics
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from jiaoyi.contracts import FastLoopConfig, Kline, Outcome
from jiaoyi.core.recorder import RoleRecorder
from jiaoyi.market.rest import RateLimited
from jiaoyi.slow.market_log import MarketLog

Direction = Literal["看涨", "看跌", "中性"]
KlineSource = Callable[[str, datetime, datetime], Awaitable[list[Kline]]]
BTC = "BTCUSDT"
logger = logging.getLogger(__name__)


class RegimeReference(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ts: AwareDatetime
    direction: Direction
    btc_px: float = Field(gt=0, allow_inf_nan=False)
    regime_text: str


@dataclass(frozen=True)
class FlipCheck:
    kind: str
    reason: str = ""
    count: int = 0
    btc_ret: float | None = None
    candidate_direction: Direction | None = None


class RegimeFlip:
    """连续双条件确认；只有主循环真正接受开会请求后才记最少间隔。"""

    def __init__(self, cfg: FastLoopConfig) -> None:
        self.cfg = cfg  # 和热更新共用同一个配置对象，不复制阈值
        self.reference: RegimeReference | None = None
        self.last_rerun: datetime | None = None
        self.count = 0
        self._checked_at: datetime | None = None
        self._candidate: Direction | None = None
        self._thresholds: dict[str, Any] = {}

    def arm(self, reference: RegimeReference | None) -> None:
        if reference != self.reference:
            self.reset()
            self.reference = reference

    def reset(self) -> None:
        self.count = 0
        self._checked_at = None
        self._candidate = None

    def thresholds(self) -> dict[str, Any]:
        return {k: v for k, v in self.cfg.model_dump().items() if k.startswith("regime_flip_")}

    def step(self, now: datetime, up: int, down: int, n: int, btc_px: float | None) -> FlipCheck:
        settings = self.thresholds()
        if settings != self._thresholds:
            self.reset()  # 改门槛后从新门槛下的第一次检查开始确认
            self._thresholds = settings
        ref = self.reference
        if ref is None:
            self.reset()
            return FlipCheck("blocked", "没有可恢复的总盘会参照")
        if (
            n < self.cfg.regime_flip_min_symbols
            or (n == 0 and self.cfg.regime_flip_breadth > 0)
            or n != up + down
            or min(up, down) < 0
            or btc_px is None
            or not math.isfinite(btc_px)
            or btc_px <= 0
            or now < ref.ts
        ):
            self.reset()
            return FlipCheck("blocked", "数据不足")
        ret = (btc_px / ref.btc_px - 1) * 100
        directions: tuple[Direction, ...] = (
            ("看涨", "看跌")
            if ref.direction == "中性"
            else (("看涨",) if ref.direction == "看跌" else ("看跌",))
        )
        checks = []
        for direction in directions:
            opposite = up if direction == "看涨" else down
            breadth = (
                self.cfg.regime_flip_breadth == 0
                or opposite * 40 >= self.cfg.regime_flip_breadth * n
            )
            btc = (
                self.cfg.regime_flip_btc_pct == 0
                or (ret if direction == "看涨" else -ret) >= self.cfg.regime_flip_btc_pct
            )
            checks.append((direction, breadth, btc))
        candidates = [d for d, breadth, btc in checks if breadth and btc]
        if len(candidates) != 1:
            self.reset()
            if len(candidates) > 1:
                return FlipCheck("blocked", "上下候选同时满足，方向有歧义", btc_ret=ret)
            kind = "quiet"
            if any(b for _, b, _ in checks) and self.cfg.regime_flip_breadth > 0:
                kind = "suspect_breadth"
            elif any(b for _, _, b in checks) and self.cfg.regime_flip_btc_pct > 0:
                kind = "suspect_btc"
            return FlipCheck(kind, btc_ret=ret)
        candidate = candidates[0]
        if candidate != self._candidate:
            self.reset()  # 中性向上、向下分别确认，不能把两边次数拼在一起
            self._candidate = candidate
        if self._checked_at is None or now > self._checked_at:
            self.count += 1
            self._checked_at = now
        if self.count < self.cfg.regime_flip_confirm:
            return FlipCheck(
                "blocked",
                f"连续确认 {self.count}/{self.cfg.regime_flip_confirm} 次",
                self.count,
                ret,
                candidate,
            )
        if self.last_rerun is not None and self.cfg.regime_flip_min_gap_min > 0:
            age = (now - self.last_rerun).total_seconds() / 60
            if age < self.cfg.regime_flip_min_gap_min:
                return FlipCheck(
                    "blocked",
                    f"间隔内：距上次翻转重开 {age:.1f} 分钟 < "
                    f"{self.cfg.regime_flip_min_gap_min:g} 分钟",
                    self.count,
                    ret,
                    candidate,
                )
        return FlipCheck("confirmed", count=self.count, btc_ret=ret, candidate_direction=candidate)


def minute(now: datetime) -> datetime:
    return now.astimezone(UTC).replace(second=0, microsecond=0)


def close_at(bars: Sequence[Kline], at: datetime) -> float | None:
    """px_at(t) 严格取开盘在 t−60 秒的已收盘线，不拿邻近的旧线补缺。"""
    start = at - timedelta(minutes=1)
    for b in bars:
        if b.open_time == start and start <= b.close_time <= at and b.close > 0:
            return b.close
    return None


async def fetch_bars(
    symbols: Sequence[str], start: datetime, end: datetime, source: KlineSource
) -> dict[str, list[Kline]]:
    slots = asyncio.Semaphore(5)

    async def one(sym: str) -> tuple[str, list[Kline]]:
        async with slots:
            try:
                return sym, await source(sym, start, end)
            except RateLimited:
                raise
            except Exception:
                logger.exception("%s 大盘翻转取行情失败", sym)
                return sym, []

    tasks = [asyncio.create_task(one(s)) for s in sorted(set(symbols))]
    try:
        return dict(await asyncio.gather(*tasks))
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@dataclass(frozen=True)
class Breadth:
    ts: datetime
    prices: dict[str, float]
    up: int
    down: int
    median_pct: float | None
    btc_px: float | None

    @property
    def n(self) -> int:
        return self.up + self.down


async def snapshot(symbols: Sequence[str], now: datetime, source: KlineSource) -> Breadth:
    at = minute(now)
    bars = await fetch_bars([*symbols, BTC], at - timedelta(minutes=61), at, source)
    prices: dict[str, float] = {}
    changes: list[float] = []
    for sym in set(symbols):
        rows = bars.get(sym, [])
        current, previous = close_at(rows, at), close_at(rows, at - timedelta(hours=1))
        if current is not None and previous is not None:
            prices[sym] = current
            changes.append((current / previous - 1) * 100)
    return Breadth(
        at,
        prices,
        sum(x > 0 for x in changes),
        sum(x < 0 for x in changes),
        statistics.median(changes) if changes else None,
        close_at(bars.get(BTC, []), at),
    )


class FlipJournal:
    """只观测，不参与放行；文件中的事件与结果用 event_id 关联，可跨重启回填。"""

    def __init__(self, root: Path, recorder: RoleRecorder | None) -> None:
        self.root = root
        self.recorder = recorder
        self.writer = MarketLog(root)
        self._suspect: tuple[str, datetime] | None = None

    def quiet(self) -> None:
        self._suspect = None

    def emit(self, row: dict[str, Any], summary: str) -> dict[str, Any]:
        row = {"event_id": uuid.uuid4().hex, **row}
        now = datetime.fromisoformat(row["ts"])
        kind = row["kind"]
        if self.recorder is not None:
            outcome = Outcome.REJECT if kind == "blocked" else Outcome.INFO
            if kind == "confirmed" and row.get("rerun"):
                outcome = Outcome.ACTION
            self.recorder.record(now, outcome, summary, output={"regime_flip": row})
        if kind.startswith("suspect_"):
            gap = timedelta(minutes=row["thresholds"]["regime_flip_suspect_log_min"])
            if self._suspect is not None:
                last_kind, last = self._suspect
                if kind == last_kind and now - last < gap:
                    return row
            self._suspect = kind, now
        elif kind not in ("rerun_result", "outcome"):
            self.quiet()
        self.writer.write_round(row)
        return row

    def pending(self, now: datetime) -> list[tuple[dict[str, Any], int]]:
        events: dict[str, dict[str, Any]] = {}
        done: set[tuple[str, int]] = set()
        try:
            for path in sorted(self.root.glob("????-??-??.jsonl")):
                for line in path.read_text(encoding="utf-8").splitlines():
                    try:
                        r = json.loads(line)
                        if not isinstance(r, dict):
                            continue
                        if r.get("kind") == "outcome":
                            done.add((r["event_id"], r["horizon_h"]))
                        elif r.get("kind") in ("suspect_breadth", "suspect_btc", "confirmed"):
                            events[r["event_id"]] = r
                    except (ValueError, KeyError, TypeError):
                        continue
        except OSError:
            logger.exception("读大盘翻转待回填记录失败")
            return []
        pending = []
        for eid, row in events.items():
            try:
                at = datetime.fromisoformat(row["sample_at"])
                if at.tzinfo is None or not row.get("prices"):
                    continue
                for hours in (1, 2):
                    if (eid, hours) not in done and now >= at + timedelta(hours=hours):
                        pending.append((row, hours))
            except (ValueError, KeyError, TypeError):
                continue
        return pending

    async def backfill(self, now: datetime, source: KlineSource) -> None:
        """固定原池成员与原时点，收益是向前 1/2 小时，绝不把本轮滚动 1h 涨跌当结果。"""
        cache: dict[tuple[datetime, tuple[str, ...]], dict[str, list[Kline]]] = {}
        for row, hours in self.pending(now):
            at = datetime.fromisoformat(row["sample_at"]) + timedelta(hours=hours)
            base = row["prices"]
            symbols = tuple(sorted(set(base) | {BTC}))
            key = at, symbols
            if key not in cache:
                cache[key] = await fetch_bars(symbols, at - timedelta(minutes=1), at, source)
            prices = {s: close_at(b, at) for s, b in cache[key].items()}
            changes = [
                (prices[s] / p - 1) * 100
                for s, p in base.items()
                if p > 0 and prices.get(s) is not None
            ]
            btc = prices.get(BTC)
            if not changes or btc is None or not row.get("btc_px"):
                continue  # 数据还没到，留待下次；不把缺失算成 0 收益
            self.writer.write_round(
                {
                    "ts": now.isoformat(),
                    "kind": "outcome",
                    "event_id": row["event_id"],
                    "origin_ts": row["ts"],
                    "sample_at": row["sample_at"],
                    "outcome_at": at.isoformat(),
                    "horizon_h": hours,
                    f"fwd_{hours}h_pool_median_pct": statistics.median(changes),
                    f"fwd_{hours}h_btc_pct": (btc / row["btc_px"] - 1) * 100,
                    "pool_observed": len(changes),
                    "pool_expected": len(base),
                }
            )
