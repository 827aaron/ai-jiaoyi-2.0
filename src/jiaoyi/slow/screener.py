"""选币：每小时用代码筛全部币安 U 本位永续，打分后取前 N 个进候选池（设计文档 §六）。

不用 AI。优先级：拉黑 > 固定 / 手动加入 > 打分。固定的币不受类别上限限制，但也要过硬条件。
行情来源是 ``MarketStatsSource``，出错直接抛出，由调用方沿用上一份池子（故障即关闭：
池子过期后军师不再出新卡）。
"""

from __future__ import annotations

import copy
import logging
import math
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol
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
from jiaoyi.core.us_hours import us_session
from jiaoyi.slow.majors import MajorsSource, base_of
from jiaoyi.slow.market_log import MarketLog
from jiaoyi.slow.us_market import EarningsCalendar

logger = logging.getLogger(__name__)

SCREEN_EVERY = timedelta(hours=1)
"""主循环每隔多久重新选币一次；选币器据此报告「下次选币时间」。"""

FALLBACK_MAJORS = frozenset({"BTC", "ETH", "BNB", "SOL", "XRP", "DOGE", "ADA"})
"""拿不到市值排名（没缓存、接口失败、major_top_n = 0）时的主流币名单。"""
_CRYPTO_STOCKS = frozenset({"COIN", "MSTR", "HOOD", "MARA", "RIOT", "CRCL"})
_US_TECH = frozenset({"TSLA", "NVDA", "AAPL", "MSFT", "GOOGL", "AMZN", "META", "AMD", "QQQ", "SPY"})
_METALS = frozenset(
    {"XAU", "XAG", "XPT", "XPD", "PAXG", "XAUT"}
)  # PAXG / XAUT 是链上黄金，也按金属算
_METAL_HINTS = ("commodit", "metal", "gold", "silver", "precious")
_EQUITY_HINTS = ("equity", "stock")
_FOREIGN_HINTS = ("hk_", "kr_", "cn_", "jp_", "eu_", "premarket", "fx", "forex", "currency")
"""非美股的传统资产（港股 / 韩股 / A 股 / 盘前 / 外汇…）：没有对应的交易时段，先一律不放行。"""


def classify(
    symbol: str,
    kind: str = "",
    majors: Mapping[str, int] | None = None,
    include: frozenset[str] = frozenset(),
    exclude: frozenset[str] = frozenset(),
) -> Category:
    """按代码给相关性分组：先看名单，名单外的再看交易所给的标的类型（``kind``，见 binance_stats）。

    币安把美股和金属永续标成 TRADIFI_PERPETUAL；名单外的传统资产合约按 kind 自动归类，
    标明是股票的当美股科技；什么都认不出才算山寨币（传统资产认不出时不放行，见 tradfi_block）。"""
    base = symbol.removesuffix("USDT")
    mbase = base_of(symbol)  # 1000PEPEUSDT → PEPE，按市值排名对
    if mbase not in exclude and (mbase in include or mbase in (majors or FALLBACK_MAJORS)):
        return Category.MAJOR
    if base in _CRYPTO_STOCKS:
        return Category.CRYPTO_STOCK
    if base in _US_TECH:
        return Category.US_TECH
    if base in _METALS:
        return Category.PRECIOUS_METAL
    if any(h in kind for h in _METAL_HINTS):
        return Category.PRECIOUS_METAL
    if any(h in kind for h in _FOREIGN_HINTS):
        return Category.ALT  # 不套美股时段；选币也不放行（见 tradfi_block）
    if any(h in kind for h in _EQUITY_HINTS):
        return Category.US_TECH
    return Category.ALT


def _source_copy(metadata: Any) -> dict[str, Any]:
    try:
        return copy.deepcopy(metadata) if isinstance(metadata, dict) else {}
    except Exception:
        return {}  # 旁路未知不得改变原筛选成功路径。


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
    kind: str = ""
    """交易所给的标的类型（小写，含 contractType / underlyingType 等），给 ``classify`` 用。"""
    range_hold_pct: float | None = None
    """最近 3 个完整的「持仓时长」窗口，每个窗口的（最高 − 最低）÷ 现价（%），取平均。
    ``None`` = 数据源没算（按 24h 波动折成每小时）。"""
    hold_hours: float = 1.0
    """持仓时长（小时，不足 1 小时按 1 算），打分时按 √时长折成每小时。"""
    volume_surge_old: float | None = None
    """旧算法的量能比值（最后一根半截小时 ÷ 前 24 小时均值），只用来算 ``score_old`` 对照。"""
    metrics: dict[str, float] = field(default_factory=dict)
    """给总盘会表格用的本地行情数字（见 ``binance_stats``）；缺的不写。"""
    bars: tuple[tuple[int, float, float, float, float], ...] = ()
    source_metadata: dict[str, Any] = field(default_factory=dict)
    """1 小时 K 线 (开盘毫秒, 开, 高, 低, 收)，最后一根是还没走完的小时；事后回填用。"""


class MarketStatsSource(Protocol):
    async def fetch(self, now: datetime) -> list[SymbolStats]: ...


@dataclass(frozen=True)
class UserOverrides:
    """Aaron 在选币页设的覆盖项，AI 改不了。"""

    pinned: frozenset[str] = frozenset()
    blacklist: frozenset[str] = frozenset()
    manual: frozenset[str] = frozenset()
    category_caps: Mapping[Category, int] = field(default_factory=dict)
    """每个类别最多进池几个，缺省用 slow.toml 的设置，
    再缺省用 ``ScreenerConfig.default_category_cap``。"""
    category_enabled: Mapping[Category, bool] = field(default_factory=dict)
    """类别开关，缺省用 slow.toml 的设置（默认开）。"""

    @classmethod
    def from_file(cls, ov: PoolOverrides) -> UserOverrides:
        """``data/pool_overrides.json``（core/pool_overrides.py）→ 选币用的覆盖项。"""
        return cls(
            ov.pinned, ov.blacklist, ov.manual, dict(ov.category_caps), dict(ov.category_enabled)
        )


@dataclass(frozen=True)
class ScreenerConfig:
    pool_size: int = 40
    min_quote_volume_usdt: float = 20_000_000.0
    max_spread_bps: float = 5.0
    min_depth_usdt: float = 100_000.0
    min_listed_days: int = 7
    round_trip_fee_bps: float = 10.0
    default_category_cap: int = 30
    """0 = 不限。"""
    tiers: bool = True
    """是否分档（头部 / 中间 / 尾部）。"""
    tier_head: int = 10
    """头部档几个：池里分数最高的前几个（固定 / 手动加入的币一律算头部）。"""
    tier_mid: int = 15
    """中间档几个：头部之后的几个；再往后是尾部。"""
    major_top_n: int = 20
    """主流币 = 市值前几名（每天自动更新，去掉稳定币和包装币）；0 = 用写死的 7 个。"""
    major_include: frozenset[str] = frozenset()
    """手动加入主流币的币名（如 LINK），不管市值排名。"""
    major_exclude: frozenset[str] = frozenset()
    """手动排除出主流币的币名；被排除的按山寨币算。"""
    always_candidates: frozenset[str] = frozenset()
    """每轮都进总盘会候选的币（不带 USDT 的币名）：过了硬门槛、没被拉黑就进池（算在池子大小里），
    不算头部、不占中间档每天的名额。"""
    allow_tradfi: bool = False
    """是否让币安 TRADIFI_PERPETUAL（美股 / 金属等传统资产永续）进池。默认关：等 Demo 上把开平仓、
    保命止损、行情流整条链验证完再开。"""
    us_events: bool = True
    """美股类别（crypto_stock / us_tech）是否用开盘时段和财报日历。"""
    us_open_bonus: float = 0.08
    """美股常规盘开盘期间，美股类别的币总分加这么多（0 = 不加）。"""
    category_caps: Mapping[Category, int] = field(default_factory=dict)
    """slow.toml ``[screener.categories]`` 里各类的上限；选币页的设置优先。"""
    category_enabled: Mapping[Category, bool] = field(default_factory=dict)
    """slow.toml 里各类的开关；选币页的设置优先。"""
    min_range_24h_pct: float = 0.0
    """24 小时波动下限（%）；0 = 不设。波动太小的币赚不回手续费。"""
    max_range_24h_pct: float = 0.0
    """24 小时波动上限（%）；0 = 不设。波动过大的币止损容易被一根针扫掉。"""
    # 打分时各项「到多少算满分」
    vol_full_multiple: float = 30.0
    """按每小时折算的正常波动是来回手续费的几倍算满分（30 = 每小时 3%）。"""
    surge_full_multiple: float = 8.0
    """最近 1 小时成交额是平时的几倍算满分。"""
    funding_full: float = 0.001
    """资金费率绝对值到多少算满分（0.001 = 0.1%）。"""
    oi_full_pct: float = 10.0
    """持仓量变化绝对值到多少 % 算满分。"""
    liq_full_usdt: float = 10_000_000.0
    """近 1 小时强平额到多少美元算满分。"""
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


_US_CATEGORIES = frozenset({Category.CRYPTO_STOCK, Category.US_TECH})


def _assign_tiers(entries: list[SymbolPoolEntry], cfg: ScreenerConfig) -> list[SymbolPoolEntry]:
    """进池的币分档：固定 / 手动加入的币和分数最高的凑满 ``tier_head`` 个算头部，接着
    ``tier_mid`` 个算中间，其余尾部。``entries`` 已按（进池优先、分数从高到低）排好。
    「每轮进总盘会」的币不抢名额：别的币按没有它们时排名，它们自己按分数落在哪档就是哪档。"""
    pooled = [e for e in entries if e.in_pool]
    user = [e for e in pooled if e.pinned or e.manual]
    always = [e for e in pooled if e.always_candidate and not (e.pinned or e.manual)]
    rest = [e for e in pooled if not (e.pinned or e.manual or e.always_candidate)]
    head_room = max(cfg.tier_head - len(user), 0)

    def tier_at(i: int) -> str:
        return "head" if i < head_room else "mid" if i < head_room + cfg.tier_mid else "tail"

    tiers: dict[str, str] = {e.symbol: "head" for e in user}
    for i, e in enumerate(rest):
        tiers[e.symbol] = tier_at(i)
    for e in always:
        tiers[e.symbol] = tier_at(sum(1 for r in rest if r.score > e.score))
    return [e.model_copy(update={"tier": tiers.get(e.symbol)}) for e in entries]


def _is_always(symbol: str, cfg: ScreenerConfig) -> bool:
    return bool(cfg.always_candidates) and (
        symbol.removesuffix("USDT") in cfg.always_candidates
        or base_of(symbol) in cfg.always_candidates
    )


def _with_ranks(entries: list[SymbolPoolEntry]) -> list[SymbolPoolEntry]:
    """池里的币按「每小时波动」排名，写进 metrics（rank = 第几，pool_n = 池子几个币）。"""
    pooled = [e for e in entries if e.in_pool and "move_hold" in e.metrics]
    order = {
        e.symbol: i + 1
        for i, e in enumerate(sorted(pooled, key=lambda e: (-e.metrics["move_hold"], e.symbol)))
    }
    return [
        e.model_copy(
            update={
                "metrics": {
                    **e.metrics,
                    "rank": float(order[e.symbol]),
                    "pool_n": float(len(order)),
                }
            }
        )
        if e.symbol in order
        else e
        for e in entries
    ]


def _enabled(category: Category, cfg: ScreenerConfig, ov: UserOverrides) -> bool:
    return ov.category_enabled.get(category, cfg.category_enabled.get(category, True))


def _cap(category: Category, cfg: ScreenerConfig, ov: UserOverrides) -> int | None:
    """这一类最多进池几个；None = 不限。只有「每类默认上限」写 0 才不限，单类写 0 是一个都不要。"""
    specific = ov.category_caps.get(category, cfg.category_caps.get(category))
    return specific if specific is not None else cfg.default_category_cap or None


def _major_note(symbol: str, ranks: Mapping[str, int], cfg: ScreenerConfig) -> str:
    base = base_of(symbol)
    if base in ranks:
        return f"主流币 · 市值第 {ranks[base]} 名"
    if base in cfg.major_include:
        return "主流币（你手动加入）"
    return "主流币（固定名单，没拿到市值排名）"


def tradfi_block(s: SymbolStats, cfg: ScreenerConfig) -> str | None:
    """传统资产永续（TRADIFI）不让进池的原因；不是传统资产或放行返回 None。

    打开 ``allow_tradfi`` 也只放行美股和贵金属 / 商品；港股、韩股、A 股、盘前、外汇等没有对应的
    交易时段，等各自的时段做好再放。"""
    if "tradifi" not in s.kind:
        return None
    if not cfg.allow_tradfi:
        return "传统资产永续暂未开放"
    if classify(s.symbol, s.kind) is Category.ALT:
        return (
            f"暂不支持的传统资产类型（{s.kind.replace('tradifi_perpetual', '').strip() or '未知'}）"
        )
    return None


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
    if (why := tradfi_block(s, cfg)) is not None:
        reasons.append(why)
    if cfg.min_range_24h_pct > 0 and s.range_24h_pct < cfg.min_range_24h_pct:
        reasons.append("24 小时波动太小")
    if cfg.max_range_24h_pct > 0 and s.range_24h_pct > cfg.max_range_24h_pct:
        reasons.append("24 小时波动过大")
    return HardFilters(
        quote_volume_24h_usdt=s.quote_volume_24h_usdt,
        spread_bps=s.spread_bps,
        depth_usdt=s.depth_usdt,
        listed_days=s.listed_days,
        passed=not reasons,
        failed_reasons=reasons,
    )


OLD_VOL_FULL_MULTIPLE = 20.0
"""旧打分（24h 波动 ÷ 手续费 ÷ 20）的满分点，只用来算 ``score_old`` 对照。"""


def _vol_item(s: SymbolStats, cfg: ScreenerConfig) -> float:
    """波动分：最近 3 段持仓时长的平均高低差，按 √时长折成每小时，是手续费的几倍（30 倍满分）。"""
    fee_pct = cfg.round_trip_fee_bps / 100.0
    if s.range_hold_pct is not None:
        per_hour = s.range_hold_pct / math.sqrt(max(s.hold_hours, 1.0))
    else:
        per_hour = s.range_24h_pct / math.sqrt(24.0)  # 数据源没给分段高低差：24h 波动折成每小时
    return _clip01(per_hour / fee_pct / cfg.vol_full_multiple)


def score(s: SymbolStats, cfg: ScreenerConfig) -> tuple[float, dict[str, float], list[str]]:
    """总分 0–1、各分项、中文入池理由。分项都先压到 0–1。"""
    parts = {
        # 默认：波动分 = 最近 3 段持仓时长的平均高低差（折成每小时）是手续费的 30 倍及以上算满分；
        # 量能 8 倍封顶；资金费 0.1% 封顶；持仓量变化 10% 封顶；强平 1000 万美元封顶（各项满分点
        # 可配，见 ScreenerConfig）
        "vol_vs_fees": _vol_item(s, cfg),
        "volume_surge": _clip01(
            math.log2(max(s.volume_surge, 1e-9)) / math.log2(cfg.surge_full_multiple)
        ),
        "funding_extreme": _clip01(abs(s.funding_rate) / cfg.funding_full),
        "oi_change": _clip01(abs(s.oi_change_pct) / cfg.oi_full_pct),
        "liquidations": _clip01(
            math.log10(max(s.liquidations_1h_usdt, 1.0)) / math.log10(cfg.liq_full_usdt)
        ),
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


def score_old(s: SymbolStats, cfg: ScreenerConfig) -> float:
    """旧公式的总分（24h 波动 ÷ 手续费 ÷ 20、半截小时的量能），每轮和新分数一起记，验收时对照。"""
    fee_pct = cfg.round_trip_fee_bps / 100.0
    surge = s.volume_surge_old if s.volume_surge_old is not None else s.volume_surge
    parts = {
        "vol_vs_fees": _clip01(s.range_24h_pct / fee_pct / OLD_VOL_FULL_MULTIPLE),
        "volume_surge": _clip01(math.log2(max(surge, 1e-9)) / math.log2(cfg.surge_full_multiple)),
        "funding_extreme": _clip01(abs(s.funding_rate) / cfg.funding_full),
        "oi_change": _clip01(abs(s.oi_change_pct) / cfg.oi_full_pct),
        "liquidations": _clip01(
            math.log10(max(s.liquidations_1h_usdt, 1.0)) / math.log10(cfg.liq_full_usdt)
        ),
    }
    return round(sum(cfg.weights.get(k, 0.0) * v for k, v in parts.items()), 4)


class CodeSymbolScreener:
    """满足 ``interfaces.SymbolScreener``。"""

    def __init__(
        self,
        source: MarketStatsSource,
        cfg: ScreenerConfig | None = None,
        overrides: UserOverrides | None = None,
        recorder: RoleRecorder | None = None,
        earnings: EarningsCalendar | None = None,
        majors: MajorsSource | None = None,
        every: timedelta = SCREEN_EVERY,
        tz: str = "Asia/Bangkok",
        market_log: MarketLog | None = None,
    ) -> None:
        self._source = source
        self._cfg = cfg or ScreenerConfig()
        self._ov = overrides or UserOverrides()
        self._rec = recorder
        self._earnings = earnings
        self._majors = majors
        self._market_log = market_log
        self._us_tickers: frozenset[str] = frozenset()
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

    def us_tickers(self) -> frozenset[str]:
        """最近一次选出的池子里，美股类别的代码（财报新闻源按它拉）。"""
        return self._us_tickers

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
                f"候选 {self._count} 个币",
                latency_ms=(time.monotonic() - started) * 1000,
            )
        self.heartbeat(now)
        return pool

    async def market_snapshot(self, pool: SymbolPool, now: datetime) -> SymbolPool:
        """独立市场评估用：保留池成员、分档和分数，只取新的市场数字。"""
        fetch_symbols = getattr(self._source, "fetch_symbols", None)
        rows = (
            await fetch_symbols([e.symbol for e in pool.in_pool], now)
            if callable(fetch_symbols)
            else await self._source.fetch(now)
        )
        stats = {s.symbol: s for s in rows}
        entries = []
        for old in pool.in_pool:
            current = stats.get(old.symbol)
            if current is None:
                raise RuntimeError(f"{old.symbol} 缺少本轮市场统计，保留上一份观点")
            filters = _hard_filters(current, self._cfg)
            metrics = dict(current.metrics)
            if current.range_hold_pct is not None:
                metrics["move_hold"] = round(current.range_hold_pct, 4)
                metrics["fee_x"] = round(
                    current.range_hold_pct / (self._cfg.round_trip_fee_bps / 100.0), 2
                )
            entries.append(
                old.model_copy(
                    update={
                        "metrics": metrics,
                        "filters": old.filters.model_copy(
                            update={
                                "quote_volume_24h_usdt": filters.quote_volume_24h_usdt,
                                "spread_bps": filters.spread_bps,
                                "depth_usdt": filters.depth_usdt,
                            }
                        ),
                    }
                )
            )
        return pool.model_copy(
            update={
                "ts": now,
                "entries": entries,
                "source_metadata": {s.symbol: _source_copy(s.source_metadata) for s in rows},
            }
        )

    def _us_adjust(
        self,
        symbol: str,
        now: datetime,
        us_open: bool,
        total: float,
        parts: dict[str, float],
        reasons: list[str],
    ) -> tuple[float, dict[str, float], list[str]]:
        """美股类别：开盘期间加分；财报日前后在理由里标注（只标注，不改分）。"""
        bonus = self._cfg.us_open_bonus
        if us_open and bonus > 0:
            total = round(total + bonus, 4)
            parts = {**parts, "us_open": bonus}
            reasons = ["美股开盘中", *reasons]
        if self._earnings is not None:
            note = self._earnings.note(symbol.removesuffix("USDT"), now)
            if note:
                reasons = [*reasons, note]
        return total, parts, reasons

    async def _screen(self, now: datetime) -> SymbolPool:
        stats = await self._source.fetch(now)
        if not stats:
            raise RuntimeError("行情数据源返回空列表，沿用上一份候选池")
        if self._market_log is not None:
            try:  # 持仓时长走完的总盘会轮次：用这次拉到的 K 线补上池子里每个币的实际结果
                self._market_log.backfill(now, {s.symbol: s.bars for s in stats})
            except Exception:
                logger.exception("总盘会结果回填失败，不影响选币")
        cfg, ov = self._cfg, self._ov
        us_on = cfg.us_events
        if us_on and self._earnings is not None:
            await self._earnings.refresh(now)  # 拉不到沿用旧缓存，内部已记日志，不影响选币
        us_open = us_on and us_session(now) == "open"
        ranks: Mapping[str, int] = {}
        if self._majors is not None and cfg.major_top_n > 0:
            await self._majors.refresh(now)  # 一天拉一次；失败沿用上次，再不行回退写死的名单
            # 币安没有永续的（只在市值榜上）不占名额：往下补满 major_top_n 个
            ranks = self._majors.ranks(cfg.major_top_n, {base_of(s.symbol) for s in stats})
            self._majors.set_effective(ranks, cfg.major_top_n, now)
        entries: dict[str, SymbolPoolEntry] = {}
        ranked: list[SymbolPoolEntry] = []
        for s in stats:
            f = _hard_filters(s, cfg)
            total, parts, reasons = score(s, cfg)
            category = classify(
                s.symbol, s.kind, ranks or None, cfg.major_include, cfg.major_exclude
            )
            if category is Category.MAJOR:
                reasons = [*reasons, _major_note(s.symbol, ranks, cfg)]
            if us_on and category in _US_CATEGORIES:
                total, parts, reasons = self._us_adjust(
                    s.symbol, now, us_open, total, parts, reasons
                )
            black = s.symbol in ov.blacklist
            pinned = s.symbol in ov.pinned and not black
            manual = s.symbol in ov.manual and not black
            always = not black and _is_always(s.symbol, cfg)
            metrics = dict(s.metrics)
            metrics["score_old"] = score_old(s, cfg)
            if s.range_hold_pct is not None:
                metrics["move_hold"] = round(s.range_hold_pct, 4)
                metrics["fee_x"] = round(s.range_hold_pct / (cfg.round_trip_fee_bps / 100.0), 2)
            try:
                entry = SymbolPoolEntry(
                    symbol=s.symbol,
                    category=category,
                    score=total,
                    score_breakdown=parts,
                    reasons=(["你固定的币"] if pinned else [])
                    + (["每轮进总盘会"] if always else [])
                    + (["这一类已关闭"] if not _enabled(category, cfg, ov) else [])
                    + reasons,
                    filters=f,
                    pinned=pinned,
                    blacklisted=black,
                    manual=manual,
                    always_candidate=always,
                    metrics=metrics,
                    in_pool=False,
                )
            except ValidationError:
                continue  # 代码格式不是 XXXUSDT 的合约不碰
            entries[s.symbol] = entry
            if f.passed and not black:
                ranked.append(entry)

        chosen: set[str] = {e.symbol for e in ranked if e.pinned or e.manual or e.always_candidate}
        counts: dict[Category, int] = {}
        for sym in chosen:
            counts[entries[sym].category] = counts.get(entries[sym].category, 0) + 1
        for e in sorted(ranked, key=lambda x: (-x.score, x.symbol)):
            if len(chosen) >= cfg.pool_size:
                break
            if e.symbol in chosen:
                continue
            if not _enabled(e.category, cfg, ov):
                continue
            cap = _cap(e.category, cfg, ov)
            if cap is not None and counts.get(e.category, 0) >= cap:
                continue
            chosen.add(e.symbol)
            counts[e.category] = counts.get(e.category, 0) + 1

        final = [e.model_copy(update={"in_pool": e.symbol in chosen}) for e in entries.values()]
        final.sort(key=lambda e: (not e.in_pool, -e.score, e.symbol))
        if cfg.tiers:
            final = _assign_tiers(final, cfg)
        final = _with_ranks(final)
        self._us_tickers = frozenset(
            e.symbol.removesuffix("USDT")
            for e in final
            if e.in_pool and e.category in _US_CATEGORIES
        )
        return SymbolPool(
            pool_id=f"pool-{now:%Y%m%dT%H%M}",
            ts=now,
            entries=final,
            source_metadata={s.symbol: _source_copy(s.source_metadata) for s in stats},
        )
