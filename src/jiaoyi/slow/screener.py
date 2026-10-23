"""选币：每小时用代码筛全部币安 U 本位永续，打分后取前 N 个进候选池（设计文档 §六）。

不用 AI。优先级：拉黑 > 固定 / 手动加入 > 打分。固定的币不受类别上限限制，但也要过硬条件。
行情来源是 ``MarketStatsSource``，出错直接抛出，由调用方沿用上一份池子（故障即关闭：
池子过期后军师不再出新卡）。
"""

from __future__ import annotations

import math
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from jiaoyi.contracts import (
    Category,
    HardFilters,
    Health,
    Outcome,
    SymbolPool,
    SymbolPoolEntry,
)
from jiaoyi.core.pool_overrides import PoolOverrides
from jiaoyi.core.recorder import RoleRecorder

SCREEN_EVERY = timedelta(hours=1)
"""主循环每隔多久重新选币一次；选币器据此报告「下次选币时间」。"""

_MAJORS = frozenset({"BTC", "ETH", "BNB", "SOL", "XRP", "DOGE", "ADA"})
_CRYPTO_STOCKS = frozenset({"COIN", "MSTR", "HOOD", "MARA", "RIOT", "CRCL"})
_US_TECH = frozenset({"TSLA", "NVDA", "AAPL", "MSFT", "GOOGL", "AMZN", "META", "AMD", "QQQ", "SPY"})
_METALS = frozenset({"XAU", "XAG", "XPT", "XPD"})


def classify(symbol: str) -> Category:
    """按代码给相关性分组。币安新上的传统资产合约名不在表里时按山寨币算（更保守的是调小上限）。"""
    base = symbol.removesuffix("USDT")
    if base in _MAJORS:
        return Category.MAJOR
    if base in _CRYPTO_STOCKS:
        return Category.CRYPTO_STOCK
    if base in _US_TECH:
        return Category.US_TECH
    if base in _METALS:
        return Category.PRECIOUS_METAL
    return Category.ALT


@dataclass(frozen=True)
class SymbolStats:
    """一个合约此刻的原始行情统计，由数据源填。"""

    symbol: str
    quote_volume_24h_usdt: float
    spread_bps: float
    depth_usdt: float
    listed_days: int
    range_24h_pct: float
    """24 小时最高最低价差，占现价的百分比。"""
    volume_surge: float = 1.0
    """最近 1 小时成交额 / 过去 24 小时的小时均值。"""
    funding_rate: float = 0.0
    oi_change_pct: float = 0.0
    liquidations_1h_usdt: float = 0.0


class MarketStatsSource(Protocol):
    async def fetch(self, now: datetime) -> list[SymbolStats]: ...


@dataclass(frozen=True)
class UserOverrides:
    """Aaron 在选币页设的覆盖项，AI 改不了。"""

    pinned: frozenset[str] = frozenset()
    blacklist: frozenset[str] = frozenset()
    manual: frozenset[str] = frozenset()
    category_caps: Mapping[Category, int] = field(default_factory=dict)
    """每个类别最多进池几个，缺省用 ``ScreenerConfig.default_category_cap``。"""

    @classmethod
    def from_file(cls, ov: PoolOverrides) -> UserOverrides:
        """``data/pool_overrides.json``（core/pool_overrides.py）→ 选币用的覆盖项。"""
        return cls(ov.pinned, ov.blacklist, ov.manual, dict(ov.category_caps))


@dataclass(frozen=True)
class ScreenerConfig:
    pool_size: int = 40
    min_quote_volume_usdt: float = 20_000_000.0
    max_spread_bps: float = 5.0
    min_depth_usdt: float = 100_000.0
    min_listed_days: int = 7
    round_trip_fee_bps: float = 10.0
    default_category_cap: int = 30
    weights: Mapping[str, float] = field(
        default_factory=lambda: {
            "vol_vs_fees": 0.35,
            "volume_surge": 0.25,
            "funding_extreme": 0.15,
            "oi_change": 0.15,
            "liquidations": 0.10,
        }
    )


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _hard_filters(s: SymbolStats, cfg: ScreenerConfig) -> HardFilters:
    reasons: list[str] = []
    if s.quote_volume_24h_usdt < cfg.min_quote_volume_usdt:
        reasons.append("24 小时成交额不足")
    if s.spread_bps > cfg.max_spread_bps:
        reasons.append("点差过大")
    if s.depth_usdt < cfg.min_depth_usdt:
        reasons.append("盘口深度不足")
    if s.listed_days < cfg.min_listed_days:
        reasons.append(f"上线不满 {cfg.min_listed_days} 天")
    return HardFilters(
        quote_volume_24h_usdt=s.quote_volume_24h_usdt,
        spread_bps=s.spread_bps,
        depth_usdt=s.depth_usdt,
        listed_days=s.listed_days,
        passed=not reasons,
        failed_reasons=reasons,
    )


def score(s: SymbolStats, cfg: ScreenerConfig) -> tuple[float, dict[str, float], list[str]]:
    """总分 0–1、各分项、中文入池理由。分项都先压到 0–1。"""
    fee_pct = cfg.round_trip_fee_bps / 100.0
    parts = {
        # 24h 波动是来回手续费的 20 倍及以上算满分
        "vol_vs_fees": _clip01(s.range_24h_pct / fee_pct / 20.0),
        "volume_surge": _clip01(math.log2(max(s.volume_surge, 1e-9)) / 3.0),  # 8 倍封顶
        "funding_extreme": _clip01(abs(s.funding_rate) / 0.001),  # 0.1% 封顶
        "oi_change": _clip01(abs(s.oi_change_pct) / 10.0),
        "liquidations": _clip01(math.log10(max(s.liquidations_1h_usdt, 1.0)) / 7.0),
    }
    total = sum(cfg.weights.get(k, 0.0) * v for k, v in parts.items())
    notes = {
        "vol_vs_fees": "波动足以覆盖手续费",
        "volume_surge": "量能突增",
        "funding_extreme": "资金费率极端",
        "oi_change": "持仓量变化大",
        "liquidations": "近 1 小时强平集中",
    }
    reasons = [notes[k] for k, v in sorted(parts.items(), key=lambda kv: -kv[1]) if v >= 0.5][:3]
    return round(total, 4), {k: round(v, 4) for k, v in parts.items()}, reasons


class CodeSymbolScreener:
    """满足 ``interfaces.SymbolScreener``。"""

    def __init__(
        self,
        source: MarketStatsSource,
        cfg: ScreenerConfig | None = None,
        overrides: UserOverrides | None = None,
        recorder: RoleRecorder | None = None,
        every: timedelta = SCREEN_EVERY,
        tz: str = "Asia/Bangkok",
    ) -> None:
        self._source = source
        self._cfg = cfg or ScreenerConfig()
        self._ov = overrides or UserOverrides()
        self._rec = recorder
        self._every = every
        self._tz = ZoneInfo(tz)
        self._last_run: datetime | None = None
        self._count = 0
        self._error: str | None = None

    def heartbeat(self, now: datetime) -> None:
        """上报选币状态：从没跑过 / 正常（上次时间、币数、下次时间）/ 失败（原因）。"""
        if self._rec is None:
            return
        last = self._last_run
        nxt = last + self._every if last else None

        def hm(t: datetime) -> str:
            return t.astimezone(self._tz).strftime("%H:%M")

        if self._error is not None:
            health = Health.DEGRADED
            headline = f"上次选币失败，沿用旧候选池：{self._error}"
        elif last is None or nxt is None:
            health, headline = Health.STARTING, "等待首次选币"
        else:
            health = Health.OK
            headline = f"上次 {hm(last)} 选出 {self._count} 个币，下次 {hm(nxt)}"
        self._rec.heartbeat(
            now,
            health,
            headline,
            extra={
                "last_run_at": last,
                "next_run_at": nxt,
                "symbol_count": self._count if last else None,
                "error": self._error,
            },
        )

    def set_overrides(self, overrides: UserOverrides) -> None:
        """选币页改了置顶 / 黑名单后调用，下一次 ``screen`` 生效。"""
        self._ov = overrides

    async def screen(self, now: datetime) -> SymbolPool:
        started = time.monotonic()
        try:
            pool = await self._screen(now)
        except Exception as exc:
            self._error = str(exc) or type(exc).__name__
            if self._rec is not None:
                self._rec.record(
                    now,
                    Outcome.ERROR,
                    f"选币失败：{self._error}",
                    latency_ms=(time.monotonic() - started) * 1000,
                )
            self.heartbeat(now)
            raise
        self._last_run, self._error = now, None
        self._count = len(pool.in_pool)
        if self._rec is not None:
            self._rec.record(
                now,
                Outcome.PASS,
                f"选出 {self._count} 个币",
                latency_ms=(time.monotonic() - started) * 1000,
            )
        self.heartbeat(now)
        return pool

    async def _screen(self, now: datetime) -> SymbolPool:
        stats = await self._source.fetch(now)
        if not stats:
            raise RuntimeError("行情数据源返回空列表，沿用上一份候选池")
        cfg, ov = self._cfg, self._ov
        entries: dict[str, SymbolPoolEntry] = {}
        ranked: list[SymbolPoolEntry] = []
        for s in stats:
            f = _hard_filters(s, cfg)
            total, parts, reasons = score(s, cfg)
            black = s.symbol in ov.blacklist
            pinned = s.symbol in ov.pinned and not black
            manual = s.symbol in ov.manual and not black
            try:
                entry = SymbolPoolEntry(
                    symbol=s.symbol,
                    category=classify(s.symbol),
                    score=total,
                    score_breakdown=parts,
                    reasons=(["你固定的币"] if pinned else []) + reasons,
                    filters=f,
                    pinned=pinned,
                    blacklisted=black,
                    manual=manual,
                    in_pool=False,
                )
            except ValidationError:
                continue  # 代码格式不是 XXXUSDT 的合约不碰
            entries[s.symbol] = entry
            if f.passed and not black:
                ranked.append(entry)

        chosen: set[str] = {e.symbol for e in ranked if e.pinned or e.manual}
        counts: dict[Category, int] = {}
        for sym in chosen:
            counts[entries[sym].category] = counts.get(entries[sym].category, 0) + 1
        for e in sorted(ranked, key=lambda x: (-x.score, x.symbol)):
            if len(chosen) >= cfg.pool_size:
                break
            if e.symbol in chosen:
                continue
            cap = ov.category_caps.get(e.category, cfg.default_category_cap)
            if counts.get(e.category, 0) >= cap:
                continue
            chosen.add(e.symbol)
            counts[e.category] = counts.get(e.category, 0) + 1

        final = [e.model_copy(update={"in_pool": e.symbol in chosen}) for e in entries.values()]
        final.sort(key=lambda e: (not e.in_pool, -e.score, e.symbol))
        return SymbolPool(pool_id=f"pool-{now:%Y%m%dT%H%M}", ts=now, entries=final)
