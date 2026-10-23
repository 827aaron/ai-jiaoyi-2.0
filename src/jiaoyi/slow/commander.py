"""军师团：从候选池挑 10–20 个，逐个开会，出一张作战卡（设计文档 §四、§六）。

流程：总盘会（便宜）挑标的 → 对每个标的开会（4–5 并发）→ 把结果夹进硬上限 → 汇成一张卡。
会议本身由 ``MeetingEngine`` 做（生产是 TradingAgents + DeepSeek，见 ``tradingagents_engine``），
这里只管调度、夹紧、落记录，所以没有 key 也能测。

故障即关闭：全部标的都开会失败就抛 ``CardRefreshError``，旧卡到期后快环自然不再开新仓；
单个标的失败只是这一张卡里没有它（没有指令 = 不开仓）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import time
import uuid
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from zoneinfo import ZoneInfo

from jiaoyi.contracts import (
    BattleCard,
    Bias,
    CardSymbol,
    Category,
    DecisionRecord,
    Health,
    KeyLevel,
    LevelKind,
    ModuleStatus,
    Outcome,
    Range,
    RiskConfig,
    Role,
    Side,
    SizeTier,
    SymbolPool,
    SymbolPoolEntry,
    TodayStats,
)
from jiaoyi.interfaces import DecisionLog
from jiaoyi.slow import binance_gate
from jiaoyi.slow.brief import MarketBriefService
from jiaoyi.slow.meeting_live import MeetingLive
from jiaoyi.slow.review import CardJournal

logger = logging.getLogger(__name__)


class MeetingError(RuntimeError):
    """会议中途失败。``cost_usd`` 是已烧掉的真实花费；未知为 None，由指挥官按估算计。"""

    def __init__(self, message: str, cost_usd: float | None = None) -> None:
        super().__init__(message)
        self.cost_usd = cost_usd


class CardRefreshError(RuntimeError):
    """这一轮开会没能出卡。"""


@dataclass(frozen=True)
class Pick:
    symbol: str
    reason: str


@dataclass(frozen=True)
class PickResult:
    regime: str
    picks: list[Pick]
    cost_usd: float = 0.0
    rejections: list[Pick] = field(default_factory=list)  # 没挑中的币：一句理由


@dataclass(frozen=True)
class Analysis:
    card_symbol: CardSymbol
    cost_usd: float = 0.0
    usage: dict[str, int] | None = None  # input_tokens / output_tokens / reasoning_tokens


class MeetingEngine(Protocol):
    async def pick(
        self, candidates: Sequence[SymbolPoolEntry], max_n: int, previous: BattleCard | None
    ) -> PickResult:
        """总盘会：看大盘和池子，挑最多 ``max_n`` 个值得开会的，并写一句大盘判断。"""
        ...

    async def analyze(self, entry: SymbolPoolEntry, regime: str) -> Analysis:
        """单标的会议：多空辩论 + 风控讨论，输出这个标的的作战指令。"""
        ...


@runtime_checkable
class _CostReporting(Protocol):
    """可选：引擎能报告「进行中的会议已花了多少」并叫停它（超时取消时用）。"""

    def partial_cost(self, key: str) -> float | None: ...

    def abort(self, key: str) -> None: ...


def _clamp_range(r: Range, lo: float, hi: float) -> Range:
    top = max(lo, min(r.max, hi))
    return Range(min=min(max(r.min, lo), top), max=top)


@dataclass(frozen=True)
class StopScale:
    """止损 / 止盈倍数的尺度：ATR 周期（分钟）和保镖认的两个倍数区间，来自 ``fast_loop``。

    默认值与 ``FastLoopConfig`` 的默认一致；``app.py`` 用真实配置覆盖。
    """

    period_min: int = 15
    min_stop_atr: float = 0.5
    max_stop_atr: float = 4.0
    min_tp_r: float = 1.0
    max_tp_r: float = 6.0
    long_period_min: int = 0
    """长周期 ATR（分钟）；> 0 时止损用的 stop_atr = √(短周期 ATR × 长周期 ATR)。"""
    stop_hint: tuple[float, float] | None = None
    """提示词建议的止损倍数范围（窄, 宽）；硬范围仍是 min_stop_atr–max_stop_atr。"""

    @property
    def eff_period_min(self) -> float:
        """stop_atr 等效的 K 线周期（几何平均），算「持仓时长 / 周期」用。"""
        return (
            math.sqrt(self.period_min * self.long_period_min)
            if self.long_period_min
            else self.period_min
        )


DEFAULT_STOP_SCALE = StopScale()


def clamp_to_risk(
    cs: CardSymbol, risk: RiskConfig, stop: StopScale = DEFAULT_STOP_SCALE
) -> CardSymbol:
    """卡里的范围只能比硬上限更紧：超出的夹回去；分组敞口上限为 0 的类别改成只看不做。"""
    if risk.category_exposure_x.get(cs.category, 0.0) <= 0.0 or cs.bias is Bias.NO_TRADE:
        return cs.model_copy(update={"bias": Bias.NO_TRADE, "allowed_signals": []})
    return cs.model_copy(
        update={
            # 慢环不知道实时权益：只按绝对硬顶夹（没写就不夹），权益倍数由保镖下单时封顶
            "notional_usdt": _clamp_range(cs.notional_usdt, 0.0, risk.notional_cap(None)),
            "leverage": _clamp_range(cs.leverage, 1.0, float(risk.max_leverage)),
            # 止损倍数落在保镖认的区间内，免得整张卡这个标的被 min/max_stop_atr 拒掉
            "stop_atr": _clamp_range(cs.stop_atr, stop.min_stop_atr, stop.max_stop_atr),
            # 止盈倍数同理：低于 min_tp_r 整张卡这个标的会被保镖拒掉
            "take_profit_r": _clamp_range(cs.take_profit_r, stop.min_tp_r, stop.max_tp_r),
        }
    )


MARKET_LIVE = "_MARKET"
"""总盘会实况的固定名字：写进 ``meeting_live/_MARKET.json``。"""
_SIZE_ORDER = {SizeTier.S: 0, SizeTier.M: 1, SizeTier.L: 2}


@dataclass(frozen=True)
class TierRules:
    """分档交易（设计文档 §六「头部档先开放，中间档由军师每天挑，尾部只看不做」）怎么落到开会。

    - 头部（head）：每轮都开会，和以前一样。
    - 中间（mid）：每天（曼谷日）第一轮由总盘会从中间档里挑，最多 ``mid_daily_pick`` 个，挑中的
      当天才继续开会；其余中间档当天不开会。挑中的币仓位档位最大封到 ``mid_size_max``。
    - 尾部（tail）：不开会、卡上没有、不开仓，只留在池里让快环录数据。
    - 有持仓的币不论哪一档都照常开会（不然持仓管家看不到它）。
    """

    enabled: bool = False
    mid_daily_pick: int = 5
    mid_size_max: SizeTier = SizeTier.S


@dataclass(frozen=True)
class EventRules:
    """临时会：卡有效期里出了事件，只给那一个币补开一场（不重开整轮）。

    ``triggers`` 之外的事件不开会。
    """

    enabled: bool = False
    triggers: frozenset[str] = frozenset()
    symbol_gap: timedelta = timedelta(minutes=60)
    max_per_day: int = 12
    move_atr: float = 3.0
    entry_max_age: timedelta = timedelta(0)
    """开仓前卡龄上限（该币上次开会距今）；0 = 不查。"""


PENDING_EXPIRY = timedelta(minutes=30)
SLOWDOWN_AT = 0.8
"""日花费到上限的这个比例就放慢开会（over_budget = slow）。"""


class BattleCardCommander:
    """满足 ``interfaces.BattleCardProvider``。``current`` 同步、不等开会。"""

    def __init__(
        self,
        engine: MeetingEngine,
        risk: RiskConfig,
        log: DecisionLog,
        *,
        pick_max: int = 20,
        concurrency: int = 4,
        ttl: timedelta = timedelta(hours=2),
        symbol_min_interval: timedelta = timedelta(hours=2),
        symbol_timeout_s: float = 900.0,
        min_refresh_gap: timedelta = timedelta(minutes=5),
        expiry_lead: timedelta = timedelta(minutes=10),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        brief: MarketBriefService | None = None,
        journal: CardJournal | None = None,
        stop_scale: StopScale = DEFAULT_STOP_SCALE,
        fail_backoff: Sequence[timedelta] = (
            timedelta(minutes=5),
            timedelta(minutes=10),
            timedelta(minutes=30),
        ),
        daily_cost_limit_usd: float | None = None,
        failed_meeting_cost_usd: float = 0.05,
        budget_path: Path | None = None,
        card_path: Path | None = None,
        tiers: TierRules | None = None,
        tier_path: Path | None = None,
        live_dir: Path | None = None,
        events: EventRules | None = None,
        schedule_at: str | None = None,
        slowdown: tuple[timedelta, timedelta] | None = None,
    ) -> None:
        if ttl <= timedelta(0):
            raise ValueError("作战卡有效期必须大于 0")
        self._clock = clock
        self._brief = brief
        self._journal = journal
        self._stop = stop_scale
        if not fail_backoff:
            raise ValueError("fail_backoff 至少要有一档")
        self._backoff = tuple(fail_backoff)
        self._cost_limit = daily_cost_limit_usd
        self._failed_cost = failed_meeting_cost_usd
        self._budget_path = budget_path
        self._card_path = card_path
        self._held: dict[str, Side] = {}
        self._on_picks: Callable[[list[str]], None] | None = None
        self._progress = ""
        self._last_error: str | None = None
        self._meeting_costs: deque[float] = deque(maxlen=8)
        self._pick_costs: deque[float] = deque(maxlen=4)
        self._restored = False
        self._beat_s = 30.0
        self._fails = 0
        self._retry_at: datetime | None = None
        self._rl_until: datetime | None = None  # 币安限流：整轮暂停到这时，解封后自动重开
        self._rl_reason = ""
        self._notices: list[str] = []
        self._budget_noticed_day = ""
        self._engine = engine
        self._risk = risk
        self._log = log
        self._pick_max = pick_max
        self._sem = asyncio.Semaphore(concurrency)
        self._ttl = ttl
        self._symbol_min_interval = symbol_min_interval
        self._last_meeting: dict[str, datetime] = {}
        self._level_broken: set[str] = set()
        self._blocked: dict[str, str] = {}
        self._symbol_timeout_s = symbol_timeout_s
        self._min_gap = min_refresh_gap
        self._lead = expiry_lead
        self._card: BattleCard | None = None
        self._last_refresh: datetime | None = None
        self._seen: set[str] = set()
        self._news: set[str] = set()
        self._live_dir = live_dir
        self._loop: asyncio.AbstractEventLoop | None = None
        bind = getattr(engine, "bind_late_cost", None)
        if callable(bind):
            bind(self._late_cost)
        self._tiers = tiers or TierRules()
        self._tier_path = tier_path
        self._tier_of: dict[str, str] = {}
        self._mid_day = ""
        self._mid_unlocked: set[str] = set()
        self._expand_mid = False
        self._events = events or EventRules()
        self._schedule_at = schedule_at
        self._slowdown = slowdown  # （放慢后的卡有效期，放慢后的开会间隔）
        self._slow_noticed_day = ""
        self._pending: dict[str, tuple[str, datetime]] = {}
        self._event_day = ""
        self._event_count = 0
        self._event_last: dict[str, datetime] = {}
        self._ref: dict[str, tuple[datetime, float]] = {}
        self._restore_tiers()
        self._stats = TodayStats()
        self._stats_day = ""
        self._pick_cost = 0.0
        self._restore_budget()
        self._market_last: tuple[datetime, str, int | None] | None = None
        self._market_state: tuple[Health, str, str | None] | None = None
        self._restore_card()

    # ------------------------------------------------------------ 快环读
    def current(self, now: datetime) -> BattleCard | None:
        c = self._card
        if c is None:
            return None
        # 整张卡到期了：持仓币的指令还留着，只是不开新仓
        c = self._pool_view(c, now) if c.is_valid(now) else self._held_view(c, now)
        if c is None:
            return None
        c = self._mask_stale(c, now)
        if not self._blocked:
            return c
        # 被封的币在读取时改成“只看不做”：立刻生效，不等下一次开会；
        # 已有持仓会被持仓管家按“改成只看不做”平掉
        return c.model_copy(
            update={
                "symbols": [
                    s.model_copy(update={"bias": Bias.NO_TRADE, "allowed_signals": []})
                    if s.symbol in self._blocked and s.bias is not Bias.NO_TRADE
                    else s
                    for s in c.symbols
                ]
            }
        )

    def set_held(self, held: Mapping[str, Side]) -> None:
        """有持仓的币（币 → 持仓方向）：必须强制入选本轮会议，并一直留在卡上继续盯。

        不然持仓管家看到卡上没有它，会判“不再盯”而平仓。
        """
        self._held = dict(held)

    def set_on_picks(self, callback: Callable[[list[str]], None]) -> None:
        """总盘会一挑完币就回调（本轮要开会的币，含置顶和持仓）：主循环马上订阅它们的行情、
        让预测员先算，不用等十几分钟后作战卡出来。"""
        self._on_picks = callback

    def _pool_view(self, card: BattleCard, now: datetime) -> BattleCard | None:
        """指令池：每个币的指令按自己的有效期到点消失；持仓币的指令不消失，只是不再开新仓，
        直到被新结论替换。池空了返回 None。"""
        kept: list[CardSymbol] = []
        for s in card.symbols:
            if now < (s.expires_at or card.expires_at):
                kept.append(s)
            elif s.symbol in self._held:
                kept.append(self._watch_only(s))
        if not kept:
            return None
        if kept == card.symbols:
            return card
        return card.model_copy(update={"symbols": kept})

    def _held_view(self, card: BattleCard, now: datetime) -> BattleCard | None:
        """整张卡到期后只剩持仓币：指令不按时间过期，等该币有新结论才换；信号清空，不开新仓。
        有效期只往后顶一分钟，``refresh_reason`` 照样会判「到点了」去开新一轮。"""
        kept = [self._watch_only(s) for s in card.symbols if s.symbol in self._held]
        if not kept or now < card.created_at:
            return None
        return card.model_copy(
            update={"symbols": kept, "expires_at": max(card.expires_at, now + timedelta(minutes=1))}
        )

    @staticmethod
    def _stamp(cs: CardSymbol, issued: datetime, expires: datetime) -> CardSymbol:
        """给一条指令盖上「哪一刻开的会、到几点到期」。"""
        return cs.model_copy(
            update={
                "issued_at": issued,
                "expires_at": expires,
                "meeting_id": f"{cs.symbol}-{issued:%Y%m%dT%H%M%S}",
            }
        )

    def _fit(self, syms: list[CardSymbol]) -> list[CardSymbol]:
        """池子装不下（卡最多 50 个币）时先丢最早到期的，持仓币不丢。"""
        extra = len(syms) - 50
        if extra <= 0:
            return syms
        order = sorted(
            (s for s in syms if s.symbol not in self._held),
            key=lambda s: s.expires_at or datetime.min.replace(tzinfo=UTC),
        )
        drop = {s.symbol for s in order[:extra]}
        return [s for s in syms if s.symbol not in drop]

    def _hold_only(self, symbol: str, side: Side, category: Category) -> CardSymbol:
        """旧卡里也没有这个持仓币时合成一条：按持仓方向只盯不开（信号清空，不开新仓）。"""
        return CardSymbol(
            symbol=symbol,
            category=category,
            bias=Bias(side.value),
            allowed_signals=[],
            size_tier_max=SizeTier.S,
            notional_usdt=Range(min=0.0, max=0.0),
            leverage=Range(min=1.0, max=1.0),
            stop_atr=Range(min=self._stop.min_stop_atr, max=self._stop.max_stop_atr),
            take_profit_r=Range(min=self._stop.min_tp_r, max=self._stop.max_tp_r),
            reason="持仓中，本轮没有新的会议结论：继续盯，不开新仓",
        )

    @staticmethod
    def _watch_only(cs: CardSymbol) -> CardSymbol:
        """留在卡上继续盯、不开新仓：方向不动（NO_TRADE 会被持仓管家当平仓理由），只清空信号。"""
        return cs.model_copy(update={"allowed_signals": []})

    def block_symbol(self, symbol: str, reason: str) -> None:
        """封掉一个币的新开仓（例如币安公告要下架它）。保镖只认作战卡，所以这里改卡。

        封币后卡里该币读成 NO_TRADE：不再开仓，已有持仓会被持仓管家按卡规则平掉
        （要下架的币留仓更危险）。
        进程重启后由新闻源重新补上。
        """
        if symbol in self._blocked:
            return
        self._blocked[symbol] = reason
        self._write(
            symbol=symbol,
            outcome=Outcome.ACTION,
            summary=f"{symbol} 禁止新开仓：{reason}"[:200],
            output={"blocked": True, "reason": reason},
        )

    def unblock_symbol(self, symbol: str) -> None:
        self._blocked.pop(symbol, None)

    @property
    def blocked_symbols(self) -> dict[str, str]:
        return dict(self._blocked)

    # ------------------------------------------------------------ 什么时候开会
    def notify_news(self, symbol: str) -> None:
        """哨兵 / 新闻源发现大消息时调用，下一次 ``refresh_reason`` 返回 ``news``。"""
        self._news.add(symbol)

    def _rate_limited(self, reason: str) -> bool:
        """币安还在封禁期：记下解封时间和这一轮的开会原因；不在封禁期返回 False。"""
        until = binance_gate.banned_until()
        if not until:
            return False
        self._rl_until = datetime.fromtimestamp(until + 5, UTC)  # 留 5 秒余量
        self._rl_reason = self._rl_reason or reason
        return True

    def rate_limit_note(self, now: datetime) -> str | None:
        """限流暂停中的提示，例如「币安限流，3 分钟后重开」；没在暂停返回 None。"""
        if self._rl_until is None or now >= self._rl_until:
            return None
        mins = max(1, math.ceil((self._rl_until - now).total_seconds() / 60))
        return f"币安限流，{mins} 分钟后重开"

    def may_refresh(self, now: datetime) -> bool:
        """开会前的总闸，所有触发原因（含定时兜底）都要过：失败退避中、或当日花费超限就不开。"""
        if self._budget_exhausted(now):
            return False
        if self._rl_until is not None and now < self._rl_until:
            return False
        return self._retry_at is None or now >= self._retry_at

    def take_notice(self) -> str | None:
        """取走一条待推送的提示（例如“当日花费超限，停止开会”），没有返回 None。"""
        return self._notices.pop(0) if self._notices else None

    def _ttl_now(self) -> timedelta:
        """作战卡有效期；日花费到 80% 且设了放慢，新卡按放慢后的间隔出。"""
        return self._slowdown[0] if self._slowdown is not None and self._slowed() else self._ttl

    def _day(self, now: datetime) -> str:
        return now.astimezone(ZoneInfo(self._risk.day_reset_tz)).date().isoformat()

    def _budget_exhausted(self, now: datetime) -> bool:
        """今天（按 day_reset_tz，曼谷）开会花费 ≥ 上限。每天只提示一次。"""
        if self._cost_limit is None or self._stats_day != self._day(now):
            return False
        if self._stats.cost_usd < self._cost_limit:
            return False
        day = self._day(now)
        if self._budget_noticed_day != day:
            self._budget_noticed_day = day
            self._notices.append(
                f"军师团今天已花 ${self._stats.cost_usd:.2f}，达到日上限 ${self._cost_limit:.2f}，"
                "今天不再开会（沿用现有作战卡，到期后不开新仓）"
            )
        return True

    def refresh_reason(
        self,
        now: datetime,
        pool: SymbolPool,
        prices: Mapping[str, float],
        atrs: Mapping[str, float] | None = None,
    ) -> str | None:
        """要不要开整轮会、为什么。距上次开会不足 ``min_refresh_gap`` 一律不开，避免抖动。

        开了临时会的档位：关键位突破 / 大消息 / 新币 / 走了 N 倍 ATR 不重开整轮，只排进临时会队列
        （``pop_event``），整轮只在到点（卡快到期 / 每天固定时间）才开。
        """
        if not self.may_refresh(now):
            return None
        if self._rl_until is not None:  # 解封了：不等下一个间隔，用原来的原因立刻重开这一轮
            reason, self._rl_reason, self._rl_until = self._rl_reason or "scheduled", "", None
            return reason
        card = self.current(now)
        if card is None:
            return "scheduled"
        if self._events.enabled:
            self._detect(now, pool, card, prices, atrs or {})
            return self._due_reason(now, card)
        if self._last_refresh is not None and now - self._last_refresh < self._min_gap:
            return None
        for cs in card.symbols:
            px = prices.get(cs.symbol)
            if px is None:
                continue
            for lv in cs.key_levels:
                if self._broken(lv, px):
                    self._level_broken.add(cs.symbol)
        if self._level_broken & {c.symbol for c in card.symbols}:
            return "key_level_break"
        if self._news & {c.symbol for c in card.symbols}:
            return "news"
        if {e.symbol for e in self._eligible(pool, now, expand=False)} - self._seen:
            return "new_entrant"
        return self._due_reason(now, card)

    def _due_reason(self, now: datetime, card: BattleCard) -> str | None:
        """定时整轮：每天固定时间（``schedule_at``，曼谷）或卡快到期。"""
        if self._schedule_at is not None:
            return "scheduled" if self._schedule_due(now) else None
        return "scheduled" if card.expires_at - now <= self._lead else None

    def _schedule_at_today(self, now: datetime) -> datetime:
        hh, mm = (self._schedule_at or "00:00").split(":")
        local = now.astimezone(ZoneInfo(self._risk.day_reset_tz))
        return local.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)

    def _schedule_due(self, now: datetime) -> bool:
        """今天的固定时间到了，而且上一轮整轮会是在它之前开的（重启后看存档的卡，不会补开）。"""
        at = self._schedule_at_today(now)
        return now >= at and (self._last_refresh is None or self._last_refresh < at)

    @property
    def fixed_time(self) -> bool:
        """每天固定时间开整轮：主循环不要再按开会间隔兜底。"""
        return self._schedule_at is not None

    def schedule_interval(self, base: timedelta) -> timedelta:
        """主循环的定时兜底间隔：日花费到 80% 且设了放慢，就换成放慢后的间隔。"""
        return self._slowdown[1] if self._slowdown is not None and self._slowed() else base

    def _slowed(self) -> bool:
        """日花费到上限的 80% 了（还没到 100%）。"""
        if self._slowdown is None or self._cost_limit is None:
            return False
        now = self._clock()
        if self._stats_day != self._day(now):
            return False
        if self._stats.cost_usd < self._cost_limit * SLOWDOWN_AT:
            return False
        day = self._day(now)
        if self._slow_noticed_day != day:
            self._slow_noticed_day = day
            self._notices.append(
                f"军师团今天已花 ${self._stats.cost_usd:.2f}，到日上限 ${self._cost_limit:.2f} 的 "
                f"{SLOWDOWN_AT:.0%}，开会放慢到每 "
                f"{self._slowdown[1].total_seconds() / 60:g} 分钟一轮"
            )
        return True

    # ------------------------------------------------------------ 临时会
    def _roll_events(self, now: datetime) -> None:
        day = self._day(now)
        if day != self._event_day:
            self._event_day, self._event_count = day, 0

    def _queue(self, symbol: str, trigger: str, now: datetime) -> None:
        if trigger in self._events.triggers and symbol not in self._pending:
            self._pending[symbol] = (trigger, now)

    def request_event(self, symbol: str, trigger: str) -> None:
        """外部事件（预测员连续看反作战卡等）：排进临时会队列，没开临时会 / 不在名单里就忽略。"""
        if self._events.enabled:
            self._queue(symbol, trigger, self._clock())

    def _detect(
        self,
        now: datetime,
        pool: SymbolPool,
        card: BattleCard,
        prices: Mapping[str, float],
        atrs: Mapping[str, float],
    ) -> None:
        for cs in card.symbols:
            sym, px = cs.symbol, prices.get(cs.symbol)
            if px is None:
                continue
            for lv in cs.key_levels:
                if self._broken(lv, px):
                    self._level_broken.add(sym)
            if sym in self._level_broken:
                self._queue(sym, "key_level_break", now)
            if sym in self._news:
                self._queue(sym, "news_high_impact", now)
            lm = self._last_meeting.get(sym)
            ref = self._ref.get(sym)
            if ref is None or (lm is not None and ref[0] < lm):
                ref = self._ref[sym] = (lm or now, px)  # 这个币上次开会（或第一次看到）时的价格
            atr = atrs.get(sym)
            if atr and abs(px - ref[1]) >= self._events.move_atr * atr:
                self._queue(sym, "move_since_card", now)
        if "new_head_symbol" in self._events.triggers:
            for e in self._eligible(pool, now, expand=False):
                if e.symbol not in self._seen and e.tier in (None, "head"):
                    self._queue(e.symbol, "new_head_symbol", now)

    def _mask_stale(self, card: BattleCard, now: datetime) -> BattleCard:
        """开仓前卡龄检查：该币上次开会距今超过 ``entry_max_age``，这次信号不开仓（只清空信号，
        方向不动、持仓照常盯），并排一场补会；新结论出来后的信号再开。"""
        limit = self._events.entry_max_age
        if not self._events.enabled or limit <= timedelta(0):
            return card
        stale: set[str] = set()
        for s in card.symbols:
            if not s.allowed_signals or s.bias is Bias.NO_TRADE:
                continue
            if now - self._last_meeting.get(s.symbol, card.created_at) > limit:
                stale.add(s.symbol)
                self._queue(s.symbol, "stale_card_on_entry", now)
        if not stale:
            return card
        return card.model_copy(
            update={
                "symbols": [self._watch_only(s) if s.symbol in stale else s for s in card.symbols]
            }
        )

    def pop_event(self, now: datetime, pool: SymbolPool) -> tuple[str, str] | None:
        """下一场该开的临时会（币，触发原因）；持仓币优先。没有或还不能开（同币间隔 / 当天次数 /
        失败退避 / 日花费）返回 None。过期的排队项（超过 30 分钟没轮到）丢掉，事件还在会再排。"""
        if not self._events.enabled or not self._pending or not self.may_refresh(now):
            return None
        self._roll_events(now)
        if self._event_count >= self._events.max_per_day:
            return None
        by_symbol = {e.symbol: e for e in pool.in_pool}
        for sym in sorted(self._pending, key=lambda x: x not in self._held):
            trigger, at = self._pending[sym]
            if now - at > PENDING_EXPIRY or sym not in by_symbol:
                del self._pending[sym]
                continue
            last = max(
                (t for t in (self._event_last.get(sym), self._last_meeting.get(sym)) if t),
                default=None,
            )
            if last is not None and now - last < self._events.symbol_gap:
                continue
            return sym, trigger
        return None

    async def refresh_symbol(
        self, pool: SymbolPool, symbol: str, trigger: str
    ) -> BattleCard | None:
        """临时会：只给一个币补开一场，结论并进现有作战卡（有效期不延长）。开不成返回 None，
        旧卡照旧；同一个币的间隔照常计，免得失败后每分钟重试。"""
        now = self._clock()
        self._loop = asyncio.get_running_loop()
        self._pending.pop(symbol, None)
        card = self._card
        entry = next((e for e in pool.in_pool if e.symbol == symbol), None)
        if card is None or not card.is_valid(now) or entry is None:
            return None
        self._roll_events(now)
        self._event_count += 1
        self._event_last[symbol] = now
        self._progress = f"{symbol} 临时会"
        beat = asyncio.create_task(self._beat_while_meeting())
        started = time.monotonic()
        try:
            res = await self._analyze_one(entry, card.regime)
        except Exception:
            return None  # 失败原因和花费已在各标的记录里
        finally:
            beat.cancel()
            self._progress = ""
        self._pick_cost = 0.0  # 没有总盘会这一份，别重复记上一轮的
        new = self._stamp(
            self._limit_size(clamp_to_risk(res.card_symbol, self._risk, self._stop)),
            now,
            card.expires_at,  # 临时会不改整轮的节奏：到期时间跟这一轮走
        )
        symbols = [new if s.symbol == symbol else s for s in card.symbols]
        if all(s.symbol != symbol for s in card.symbols):
            symbols.append(new)
        card = card.model_copy(
            update={
                "version": card.version + 1,
                "symbols": symbols,
                "refresh_reason": f"event:{trigger}",
                "llm_cost_usd": round(card.llm_cost_usd + res.cost_usd, 6),
            }
        )
        self._card = card
        self._last_meeting[symbol] = now
        self._seen.add(symbol)
        self._level_broken.discard(symbol)
        self._news.discard(symbol)
        self._save_card()
        if self._journal is not None:
            self._journal.record(card, [symbol])
        self._record_card(card, started)
        return card

    @staticmethod
    def _broken(lv: KeyLevel, px: float) -> bool:
        if lv.kind is LevelKind.RESISTANCE:
            return px > lv.price
        if lv.kind is LevelKind.SUPPORT:
            return px < lv.price
        return False

    # ------------------------------------------------------------ 开会
    async def refresh(self, pool: SymbolPool, reason: str) -> BattleCard:
        now = self._clock()
        started = time.monotonic()
        self._loop = asyncio.get_running_loop()
        self._rl_until, self._rl_reason = None, ""  # 重开时清掉上一次的限流暂停
        self._pick_cost = 0.0  # 本轮总盘会花费；出卡失败时记在错误记录上，面板净盈亏才不漏 AI 费
        self._tier_of = {e.symbol: e.tier for e in pool.in_pool if e.tier}
        self._expand_mid = self._tiers.enabled and self._mid_day != self._day(now)
        candidates = self._eligible(pool, now, expand=self._expand_mid)
        if not candidates:
            raise CardRefreshError("候选池为空，不出卡")
        if self._budget_exhausted(now):
            raise CardRefreshError("军师团当日花费已达上限，今天不再开会")
        if self._brief is not None:
            try:
                await self._brief.refresh()  # 每轮算一次，所有会议共用；失败不影响开会
            except Exception as exc:
                self._write(outcome=Outcome.ERROR, summary=f"市场简报失败，会议继续：{exc}"[:200])
        self._progress = "选币中"
        beat = asyncio.create_task(self._beat_while_meeting())
        try:
            card = await self._meet(pool, candidates, reason, now)
        except Exception as exc:
            self._record_error(reason, exc, started)
            if self._rate_limited(reason):
                raise  # 币安限流不是会议本身的错：不退避，等解封后原样重开
            wait = self._backoff[min(self._fails, len(self._backoff) - 1)]
            self._fails += 1
            self._retry_at = self._clock() + wait  # 连续失败退避，免得每分钟重开一轮会
            raise
        finally:
            beat.cancel()
            self._progress = ""
        self._fails, self._retry_at, self._last_error = 0, None, None
        self._restored = False
        self._card, self._last_refresh = card, now
        self._seen = {e.symbol for e in candidates}
        self._news.clear()
        self._level_broken.clear()
        self._record_card(card, started)
        return card

    def _eligible(self, pool: SymbolPool, now: datetime, *, expand: bool) -> list[SymbolPoolEntry]:
        """这一轮能开会的币：没开分档 = 整个池；开了 = 头部 + 有持仓的 + 中间档里今天挑中的
        （``expand``：今天还没挑，中间档全部参与挑选）。尾部不开会。"""
        if not self._tiers.enabled:
            return pool.in_pool
        out: list[SymbolPoolEntry] = []
        for e in pool.in_pool:
            mid_ok = e.symbol in self._mid_unlocked and self._mid_day == self._day(now)
            if (
                e.tier in (None, "head")
                or e.symbol in self._held
                or (e.tier == "mid" and (expand or mid_ok))
            ):
                out.append(e)
        return out

    def _unlock_mid(self, chosen: list[SymbolPoolEntry], now: datetime) -> list[SymbolPoolEntry]:
        """今天第一轮：中间档的挑中的币最多留 ``mid_daily_pick`` 个并记下来（落盘，重启不重挑）。"""
        keep: list[SymbolPoolEntry] = []
        mids: list[str] = []
        for e in chosen:
            if e.tier == "mid" and e.symbol not in self._held:
                if len(mids) >= self._tiers.mid_daily_pick:
                    continue
                mids.append(e.symbol)
            keep.append(e)
        self._mid_day, self._mid_unlocked = self._day(now), set(mids)
        self._save_tiers()
        return keep

    def _limit_size(self, cs: CardSymbol) -> CardSymbol:
        """中间档的币仓位档位封顶（只往小夹）。"""
        cap = self._tiers.mid_size_max
        if (
            self._tiers.enabled
            and self._tier_of.get(cs.symbol) == "mid"
            and _SIZE_ORDER[cs.size_tier_max] > _SIZE_ORDER[cap]
        ):
            return cs.model_copy(update={"size_tier_max": cap})
        return cs

    def _save_tiers(self) -> None:
        if self._tier_path is None:
            return
        data = {"day": self._mid_day, "mid": sorted(self._mid_unlocked)}
        try:
            tmp = self._tier_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self._tier_path)
        except OSError:
            logger.exception("写 %s 失败：重启后今天的中间档会重新挑", self._tier_path)

    def _restore_tiers(self) -> None:
        if self._tier_path is None or not self._tier_path.is_file():
            return
        try:
            data = json.loads(self._tier_path.read_text(encoding="utf-8"))
            self._mid_day, self._mid_unlocked = str(data["day"]), {str(s) for s in data["mid"]}
        except (OSError, ValueError, KeyError, TypeError):
            logger.warning("%s 读不出，今天的中间档会重新挑", self._tier_path)

    def _market_live(
        self,
        candidates: list[SymbolPoolEntry],
        forced: list[SymbolPoolEntry],
        free: list[SymbolPoolEntry],
        room: int,
    ) -> MeetingLive | None:
        """总盘会（大盘基调 + 挑币）的实况，写进 meeting_live/_MARKET.json，格式同单币会议。"""
        if self._live_dir is None:
            return None
        live = MeetingLive(self._live_dir, MARKET_LIVE)
        table = "\n".join(
            f"{e.symbol}（{e.category.value}，分数 {e.score:.2f}"
            + (f"，档位 {e.tier}" if e.tier else "")
            + (f"，{'、'.join(e.reasons)}" if e.reasons else "")
            + "）"
            for e in free
        )
        prev = f"上一张卡的大盘判断：{self._card.regime}\n" if self._card is not None else ""
        live.step(
            "input",
            "input",
            f"{prev}候选池 {len(candidates)} 个；固定 / 持仓强制开会 {len(forced)} 个"
            f"（不占名额）；军师最多再挑 {room} 个。\n{table}",
        )
        return live

    def _market_done(
        self,
        live: MeetingLive,
        picked: PickResult,
        chosen: list[SymbolPoolEntry],
        candidates: list[SymbolPoolEntry],
        forced: list[SymbolPoolEntry],
        reused: Mapping[str, CardSymbol],
        now: datetime,
    ) -> None:
        by_pick = {p.symbol: p.reason for p in picked.picks}
        out = [f"大盘判断：{picked.regime}", "挑中："]
        out += [f"{e.symbol}：{by_pick.get(e.symbol, '')}" for e in chosen if e not in forced]
        live.step("pick", "market", "\n".join(out))
        keep = {e.symbol for e in chosen}
        why: list[str] = []
        why += [f"{e.symbol}：固定 / 持仓强制开会" for e in forced]
        rank = {e.symbol: i + 1 for i, e in enumerate(sorted(candidates, key=lambda e: -e.score))}
        for e in candidates:
            if e.symbol in keep:
                continue
            if e.symbol in by_pick:
                why.append(f"{e.symbol}：总盘会挑了，但这轮上会名额已满（或中间档当天名额已满）")
            else:
                why.append(
                    f"{e.symbol}：总盘会这轮没挑中（候选池第 {rank[e.symbol]} 名，"
                    f"{e.score:.0f} 分；没写单币理由）"
                )
        for sym in reused:
            last = self._last_meeting.get(sym)
            ago = f"{(now - last).total_seconds() / 60:.0f} 分钟前" if last else "不久前"
            why.append(f"{sym}：{ago}开过会、没有大消息 / 关键位突破，沿用上一张卡的指令")
        live.step("filter", "filter", "\n".join(why))
        rejections, summary = self._market_summary(picked, chosen, candidates, by_pick)
        live.set_rejections(rejections)
        live.set_summary(summary)
        live.finish("market", picked.regime, "", picked.cost_usd)

    def _market_summary(
        self,
        picked: PickResult,
        chosen: list[SymbolPoolEntry],
        candidates: list[SymbolPoolEntry],
        by_pick: Mapping[str, str],
    ) -> tuple[list[dict[str, str]], str]:
        """总盘会这一场的落选理由和一句话摘要；同时写总盘会自己的模块状态（团队页用）。"""
        rejections = self._rejections(picked, chosen, candidates, by_pick)
        summary = f"挑了 {len(chosen)} 个 / 落选 {len(rejections)} 个"
        self._market_status(Health.OK, summary, len(chosen), None)
        return rejections, summary

    @staticmethod
    def _rejections(
        picked: PickResult,
        chosen: list[SymbolPoolEntry],
        candidates: list[SymbolPoolEntry],
        by_pick: Mapping[str, str],
    ) -> list[dict[str, str]]:
        """没上会的候选币各一句理由：军师给了用军师的，没给就写明原因（不留空白、不猜）。"""
        keep = {e.symbol for e in chosen}
        said = {r.symbol: r.reason for r in picked.rejections}
        rows: list[dict[str, str]] = []
        for e in candidates:
            if e.symbol in keep:
                continue
            if e.symbol in by_pick:
                reason = "军师挑了，但超出名额或中间档当天名额已满"
            else:
                reason = said.get(e.symbol) or "军师没挑，也没给理由"
            rows.append({"symbol": e.symbol, "reason": reason})
        return rows

    def _next_meeting(self, now: datetime) -> datetime | None:
        """下一次会议最早什么时候开（按现在的状态估：卡到期前 ``expiry_lead``、退避、最小间隔）。"""
        at: datetime | None = None
        card = self.current(now)
        if self._schedule_at is not None:
            at = self._schedule_at_today(now)
            if now >= at and self._last_refresh is not None and self._last_refresh >= at:
                at += timedelta(days=1)  # 今天的已经开过，等明天
        elif card is not None:
            at = card.expires_at - self._lead
            if self._last_refresh is not None:
                at = max(at, self._last_refresh + self._min_gap)
        if self._retry_at is not None:
            at = max(at, self._retry_at) if at is not None else self._retry_at
        if self._rl_until is not None:
            at = self._rl_until
        return at

    def heartbeat(self, now: datetime) -> None:
        """应用每 30 秒调一次：空闲期间也持续上报，面板才不会在两轮会议之间把慢环标红。"""
        if self._progress:
            self._heartbeat(Health.OK, f"开会中：{self._progress}", None)
            return
        tz = ZoneInfo(self._risk.day_reset_tz)
        nxt = self._next_meeting(now)
        when = f"，下次会议 {nxt.astimezone(tz):%H:%M}" if nxt is not None else ""
        card = self.current(now)
        if (note := self.rate_limit_note(now)) is not None:
            self._heartbeat(Health.DEGRADED, note, self._last_error)
        elif self._fails and self._retry_at is not None:
            self._heartbeat(
                Health.DEGRADED,
                f"上轮开会失败，{self._retry_at.astimezone(tz):%H:%M} 后重试",
                self._last_error,
            )
        elif card is not None:
            tradable = sum(1 for s in card.symbols if s.bias is not Bias.NO_TRADE)
            head = "已恢复卡" if self._restored else "当前卡"
            self._heartbeat(Health.OK, f"{head} v{card.version}，{tradable} 个可做{when}", None)
        elif self._card is not None:
            self._heartbeat(Health.OK, "作战卡已过期，等下一轮会议", None)
        else:
            self._heartbeat(Health.OK, "还没有作战卡，等第一轮会议", None)

    async def _beat_while_meeting(self) -> None:
        """一轮会议要十几分钟：期间每 30 秒报一次心跳，面板才不会把慢环标红。"""
        while True:
            self._heartbeat(Health.OK, f"开会中：{self._progress}", None)
            await asyncio.sleep(self._beat_s)

    async def _meet(
        self, pool: SymbolPool, candidates: list[SymbolPoolEntry], reason: str, now: datetime
    ) -> BattleCard:
        by_symbol = {e.symbol: e for e in candidates}
        pinned = [e for e in candidates if e.pinned][: self._pick_max]
        # 有持仓的币强制入选（和置顶一样不占军师的挑选名额），保证它们本轮有新结论、卡上不缺
        held_in = [e for e in candidates if e.symbol in self._held and e not in pinned]
        pinned = [*pinned, *held_in]
        room = max(self._pick_max - len(pinned), 0)
        free = [e for e in candidates if e not in pinned]
        est = self._estimate(self._pick_costs)
        self._charge(est)  # 开会前先按估算预记并落盘：开到一半被强退，这笔钱也不会漏
        live = self._market_live(candidates, pinned, free, max(room, 0))
        try:
            picked = await self._engine.pick(free, max(room, 0), self._card)
        except Exception as exc:
            # 总盘会失败也烧了 token：按真实 usage，拿不到就按估算上限计
            fail_cost = self._failure_cost(exc, "pick")
            self._charge(fail_cost - est)
            self._pick_cost = fail_cost
            if live is not None:
                live.fail(self._why(exc), fail_cost)
            self._market_status(
                Health.DEGRADED, f"总盘会失败：{self._why(exc)}", None, self._why(exc)
            )
            raise
        self._charge(
            picked.cost_usd - est
        )  # 选币的花费一出结果就改成实际值，哪怕后面所有标的都失败
        self._pick_costs.append(picked.cost_usd)
        chosen = list(pinned)
        for p in picked.picks:
            e = by_symbol.get(p.symbol)
            if e is None or e.pinned or len(chosen) >= self._pick_max:
                continue  # 军师只能从池里挑，池外的丢掉
            chosen.append(e)
        if self._expand_mid:
            chosen = self._unlock_mid(chosen, now)
        self._pick_cost = picked.cost_usd
        if not chosen:
            if live is not None:
                live.fail("总盘会没有挑出任何标的", picked.cost_usd)
            raise CardRefreshError("总盘会没有挑出任何标的")
        if self._on_picks is not None:
            try:
                self._on_picks([e.symbol for e in chosen])
            except Exception:
                logger.exception("通知总盘会名单失败，等作战卡出来再订阅")

        # 同一个币两次会议至少隔 symbol_min_interval；没有大消息 / 关键位突破就沿用上一张卡的指令
        forced = self._news | self._level_broken
        if reason == "manual":  # 面板「重新开会」：整条链真跑，不沿用上一张卡、不受同币最小间隔限制
            forced = forced | {e.symbol for e in chosen}
        reused: dict[str, CardSymbol] = {}
        prev_card = self._card
        for e in chosen:
            last = self._last_meeting.get(e.symbol)
            old = prev_card.for_symbol(e.symbol) if prev_card is not None else None
            if (
                prev_card is not None
                and old is not None
                and last is not None
                and now - last < self._symbol_min_interval
                and e.symbol not in forced
                and now < (old.expires_at or prev_card.expires_at)
            ):
                reused[e.symbol] = old
        to_meet = [e for e in chosen if e.symbol not in reused]
        if live is not None:
            self._market_done(live, picked, chosen, candidates, pinned, reused, now)
        else:
            self._market_summary(
                picked, chosen, candidates, {p.symbol: p.reason for p in picked.picks}
            )
        prev = self._card
        same_day = prev is not None and prev.card_id == self._card_id(now)
        base_version = prev.version if prev is not None and same_day else 0
        # 指令池：上一张卡里还没到期的币原样留着（这轮没开会的币不动），开完哪个换哪个；
        # 持仓币的指令到期也不消失，只清空信号。池外（已被筛掉）的币不留
        in_pool = {e.symbol for e in pool.in_pool}
        symbols: dict[str, CardSymbol] = {}
        for old in prev.symbols if prev is not None else []:
            exp = old.expires_at or (prev.expires_at if prev is not None else now)
            if old.symbol not in in_pool and old.symbol not in self._held:
                continue
            if now < exp:
                symbols[old.symbol] = clamp_to_risk(old, self._risk, self._stop)
            elif old.symbol in self._held:
                symbols[old.symbol] = self._watch_only(old)
        symbols.update(reused)
        # 有持仓的币卡上必须有：没有新结论就沿用旧指令（只清空信号），旧卡里也没有就按持仓方向合成
        for sym, side in self._held.items():
            if sym in symbols:
                continue
            old = prev.for_symbol(sym) if prev is not None else None
            if old is not None:
                symbols[sym] = self._watch_only(old)
            else:
                cat = by_symbol[sym].category if sym in by_symbol else Category.ALT
                symbols[sym] = self._hold_only(sym, side, cat)
        cost = picked.cost_usd
        published = 0

        round_expiry = now + self._ttl_now()

        def make_card() -> BattleCard:
            return BattleCard(
                card_id=self._card_id(now),
                version=base_version + max(published, 1),
                created_at=now,
                expires_at=round_expiry,
                source="tradingagents",
                regime=picked.regime,
                symbols=self._fit(list(symbols.values())),
                refresh_reason=reason,
                llm_cost_usd=round(cost, 6),
            )

        async def meet(e: SymbolPoolEntry) -> Analysis:
            nonlocal cost, published
            res = await self._analyze_one(e, picked.regime)
            # 开完一场就出卡并落盘：中途重启 / 失败不会丢掉已经开完的会
            symbols[e.symbol] = self._stamp(
                self._limit_size(clamp_to_risk(res.card_symbol, self._risk, self._stop)),
                now,
                round_expiry,
            )
            cost += res.cost_usd
            published += 1
            self._last_meeting[e.symbol] = now
            card = make_card()
            self._card = card
            self._save_card()
            if self._journal is not None:
                self._journal.record(card, [e.symbol])
            self._record_card(card, time.monotonic(), partial=True)  # 面板马上显示这个币
            return res

        finished = 0
        self._progress = f"0/{len(to_meet)}"

        async def counted(e: SymbolPoolEntry) -> Analysis:
            nonlocal finished
            try:
                return await meet(e)
            finally:
                finished += 1  # 成功失败都算开完一场
                self._progress = f"{finished}/{len(to_meet)}"
                self._heartbeat(Health.OK, f"开会中：{self._progress}", None)

        results = await asyncio.gather(*(counted(e) for e in to_meet), return_exceptions=True)
        failed = [
            f"{e.symbol}: {r!r}"
            for e, r in zip(to_meet, results, strict=True)
            if not isinstance(r, Analysis)
        ]
        for e, r in zip(to_meet, results, strict=True):
            if isinstance(r, Analysis):
                continue
            if e.symbol in self._held and e.symbol in symbols:
                symbols[e.symbol] = self._watch_only(symbols[e.symbol])  # 开会失败：持仓照常盯
            # 没开成会的币：池里原来的指令照旧，到它自己的点才消失
        if failed:
            self._rate_limited(reason)  # 有币因为限流没开成：整轮暂停，解封后重开
        if to_meet and published == 0:  # 旧指令还在池里，但这一轮一场都没开成：记失败、退避重试
            raise CardRefreshError("所有标的开会都失败：" + "；".join(failed))
        card = make_card()
        self._card = card
        self._save_card()
        if published == 0 and self._journal is not None:
            self._journal.record(card, [])
        return card

    def _card_id(self, now: datetime) -> str:
        return f"card-{now:%Y%m%d}"

    def _why(self, exc: BaseException) -> str:
        """异常原因文字；超时的 TimeoutError 没有文字，补上类型名和秒数。"""
        if isinstance(exc, TimeoutError) and not str(exc):
            return f"超过 {self._symbol_timeout_s:.0f} 秒未完成（TimeoutError）"
        return str(exc) or type(exc).__name__

    async def _analyze_one(self, entry: SymbolPoolEntry, regime: str) -> Analysis:
        async with self._sem:
            started = time.monotonic()
            est = self._estimate(self._meeting_costs)
            self._charge(est)  # 预记：被强退打断的会议（重启）也算花过钱；正常结束时在下面退回估算
            try:
                res = await asyncio.wait_for(
                    self._engine.analyze(entry, regime), self._symbol_timeout_s
                )
            except Exception as exc:
                self._charge(-est)
                fail_cost = self._failure_cost(exc, entry.symbol)
                guess = self._cost_estimated(entry.symbol)
                self._log_symbol(
                    entry,
                    Outcome.ERROR,
                    f"{entry.symbol} 开会失败：{self._why(exc)}"
                    + ("（花费含在途请求估算）" if guess else ""),
                    started,
                    fail_cost,
                    estimated=guess,
                )
                raise
            self._charge(-est)
            self._meeting_costs.append(res.cost_usd)
            try:
                cs = self._validate(entry, res)
            except Exception as exc:
                # 会议已经烧了 token，没有可用结论也要计入花费
                self._log_symbol(
                    entry,
                    Outcome.ERROR,
                    f"{entry.symbol} 结论不合格：{exc}",
                    started,
                    res.cost_usd,
                    usage=res.usage,
                )
                raise
            self._log_symbol(
                entry,
                Outcome.ACTION,
                f"{entry.symbol} 开会结论：{cs.bias.value}，{cs.reason}",
                started,
                res.cost_usd,
                cs,
                usage=res.usage,
            )
            return res

    def _validate(self, entry: SymbolPoolEntry, res: Analysis) -> CardSymbol:
        cs = res.card_symbol
        if cs.symbol != entry.symbol:
            raise ValueError(f"会议返回了别的标的 {cs.symbol}，应为 {entry.symbol}")
        if cs.category != entry.category:
            # 分组决定吃哪一档敞口上限，以候选池为准，不信引擎
            raise ValueError(f"{entry.symbol} 分组应为 {entry.category}，会议给了 {cs.category}")
        r = self._risk
        if cs.notional_usdt.max > r.notional_cap(None) or cs.leverage.max > r.max_leverage:
            # 会议输出越界：这个币没有结论，不是悄悄夹紧后照用
            raise ValueError(f"{entry.symbol} 会议给出的名义价值或杠杆超出硬上限")
        return cs

    # ------------------------------------------------------------ 记录
    def _roll(self, now: datetime) -> None:
        """跨过交易日（day_reset_tz，曼谷 0 点）就把当天统计清零。"""
        day = self._day(now)
        if day != self._stats_day:
            self._stats, self._stats_day = TodayStats(), day

    def _bump(self, *, ok: bool, cost: float, ms: float) -> None:
        self._roll(self._clock())
        s = self._stats
        calls = s.calls + 1
        self._stats = s.model_copy(
            update={
                "calls": calls,
                "passed": s.passed + int(ok),
                "errors": s.errors + int(not ok),
                "cost_usd": round(s.cost_usd + cost, 6),
                "avg_latency_ms": (s.avg_latency_ms * s.calls + ms) / calls,
            }
        )
        self._save_budget()

    def _charge(self, cost: float) -> None:
        """只记花费、不算一次调用（选币步骤，或不属于某个标的的花费）。"""
        self._roll(self._clock())
        self._stats = self._stats.model_copy(
            update={"cost_usd": max(round(self._stats.cost_usd + cost, 6), 0.0)}
        )
        self._save_budget()

    def _estimate(self, recent: deque[float]) -> float:
        """开会前预记的钱：最近几场的平均实际花费，没有就用 ``failed_meeting_cost_usd``。"""
        return round(sum(recent) / len(recent), 6) if recent else self._failed_cost

    def _cost_estimated(self, key: str) -> bool:
        fn = getattr(self._engine, "cost_estimated", None)
        return bool(callable(fn) and fn(key))

    def _late_cost(self, key: str, delta: float) -> None:
        """超时取消的会议，在途请求的真实花费晚到：补正今天的花费（从工作线程回到事件循环里记）。"""
        loop = self._loop
        if loop is not None and delta:
            loop.call_soon_threadsafe(self._charge, delta)

    def _failure_cost(self, exc: BaseException, key: str = "") -> float:
        """失败会议的花费：引擎带出了真实花费就用它；超时取消的会议，引擎还在跑，就叫停它并取
        已经发生的真实花费；都拿不到（断网等）才按估算上限计。"""
        known = exc.cost_usd if isinstance(exc, MeetingError) else None
        if known is None and key and isinstance(self._engine, _CostReporting):
            self._engine.abort(key)  # 超时后后台线程还在烧钱：不再发新的模型请求
            known = self._engine.partial_cost(key)
        return self._failed_cost if known is None else known

    def _restore_budget(self) -> None:
        """重启后恢复当天已花的钱，免得重启就把日上限清零。"""
        if self._budget_path is None:
            return
        try:
            data = json.loads(self._budget_path.read_text(encoding="utf-8"))
            day, cost = str(data["day"]), float(data["cost_usd"])
        except (OSError, ValueError, KeyError, TypeError):
            return
        if day == self._day(self._clock()) and cost >= 0:
            try:  # 调用次数等一并恢复，面板重启后不清零；旧文件只有花费
                stats = TodayStats.model_validate(data["stats"]).model_copy(
                    update={"cost_usd": cost}
                )
            except (KeyError, ValueError):
                stats = TodayStats(cost_usd=cost)
            self._stats, self._stats_day = stats, day

    def _save_budget(self) -> None:
        if self._budget_path is None:
            return
        tmp = self._budget_path.with_suffix(".tmp")
        try:
            self._budget_path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(
                json.dumps(
                    {
                        "day": self._stats_day,
                        "cost_usd": self._stats.cost_usd,
                        "stats": self._stats.model_dump(mode="json"),
                    }
                ),
                encoding="utf-8",
            )
            os.replace(tmp, self._budget_path)
        except OSError:
            with contextlib.suppress(OSError):
                tmp.unlink()
            # 落盘失败不影响出卡；内存里的统计照常生效

    def _restore_card(self) -> None:
        """重启后捡回还没过期的作战卡（含各币上次开会时间），不用等下一轮 2 小时的会。"""
        if self._card_path is None:
            return
        logger.info("作战卡落盘位置：%s", self._card_path.resolve())
        try:
            data = json.loads(self._card_path.read_text(encoding="utf-8"))
            card = BattleCard.model_validate(data["card"])
            meets = {str(k): datetime.fromisoformat(v) for k, v in data["last_meeting"].items()}
            saved_stop = data["stop"]
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            logger.info("没有可恢复的作战卡（%s）", exc.__class__.__name__)
            return
        # 存盘是 JSON：tuple（stop_hint）读回来是 list，两边都过一遍 JSON 再比
        if saved_stop != json.loads(json.dumps(asdict(self._stop))):
            logger.info("存档的作战卡档位 / ATR 周期与现在不一致，不恢复")
            return  # 出卡时的档位 / ATR 周期和现在不一致：卡里的止损倍数不能直接用，等下一轮会
        now = self._clock()
        if not card.is_valid(now):
            logger.info("存档的作战卡已过期，不恢复")
            return
        # 重启前后可能改过硬上限或切了档位：按现在的配置再夹一遍，免得保镖整个拒掉
        card = card.model_copy(
            update={"symbols": [clamp_to_risk(sym, self._risk, self._stop) for sym in card.symbols]}
        )
        self._card, self._last_refresh = card, card.created_at
        self._seen = {s.symbol for s in card.symbols}
        self._last_meeting = meets
        self._restored = True
        logger.info(
            "已恢复作战卡 %s v%d（%d 个标的）", card.card_id, card.version, len(card.symbols)
        )

    def _save_card(self) -> None:
        if self._card_path is None or self._card is None:
            return
        tmp = self._card_path.with_suffix(".tmp")
        try:
            self._card_path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(
                json.dumps(
                    {
                        "card": self._card.model_dump(mode="json"),
                        "last_meeting": {k: v.isoformat() for k, v in self._last_meeting.items()},
                        "stop": asdict(self._stop),
                    }
                ),
                encoding="utf-8",
            )
            os.replace(tmp, self._card_path)
        except Exception:  # 落盘失败（磁盘、权限、序列化）不能让一场开完的会算失败；但要看得见
            logger.exception("作战卡写不进 %s：重启后会丢卡", self._card_path)
            with contextlib.suppress(OSError):
                tmp.unlink()
            # 内存里的卡照常生效

    def _write(self, **kw: Any) -> None:
        self._log.write(
            DecisionRecord(record_id=uuid.uuid4().hex, ts=self._clock(), role=Role.COMMANDER, **kw)
        )

    def _log_symbol(
        self,
        entry: SymbolPoolEntry,
        outcome: Outcome,
        summary: str,
        started: float,
        cost: float,
        cs: CardSymbol | None = None,
        estimated: bool = False,
        usage: dict[str, int] | None = None,
    ) -> None:
        ms = (time.monotonic() - started) * 1000
        self._bump(ok=outcome is not Outcome.ERROR, cost=cost, ms=ms)
        self._write(
            symbol=entry.symbol,
            outcome=outcome,
            summary=summary[:200],
            input={"score": entry.score, "reasons": entry.reasons},
            output=self._symbol_output(cs, estimated, usage),
            latency_ms=ms,
            cost_usd=cost,
        )

    @staticmethod
    def _symbol_output(
        cs: CardSymbol | None, estimated: bool, usage: dict[str, int] | None
    ) -> dict[str, Any]:
        out: dict[str, Any] = (
            cs.model_dump(mode="json") if cs is not None else {"cost_estimated": estimated}
        )
        if usage is not None:  # 面板「AI 费用」按模型 token 展示
            out["usage"] = usage
        return out

    def _record_card(self, card: BattleCard, started: float, *, partial: bool = False) -> None:
        """写一条出卡记录（面板读最新一条显示作战卡）。``partial`` = 一轮会议中途、某个币刚开完：
        只为了面板马上显示这个币，不记花费、不算调用次数（整轮结束那条照旧记总盘会花费）。"""
        tradable = sum(1 for s in card.symbols if s.bias is not Bias.NO_TRADE)
        self._write(
            card_id=card.card_id,
            outcome=Outcome.ACTION,
            summary=(
                f"作战卡更新 v{card.version}：{len(card.symbols)} 个标的（这一轮还在开会）"
                if partial
                else f"出作战卡 v{card.version}：{len(card.symbols)} 个标的，{tradable} 个可做"
            ),
            input={"refresh_reason": card.refresh_reason},
            output={
                "regime": card.regime,
                "version": card.version,
                "expires_at": card.expires_at.isoformat(),  # 面板据此显示“还有多久有效”
                "symbols": [s.symbol for s in card.symbols],
                # 指令池：每个币自己的出会时间和到期时间，面板据此显示 / 到期隐藏
                "pool": {
                    s.symbol: {
                        "issued_at": (s.issued_at or card.created_at).isoformat(),
                        "expires_at": (s.expires_at or card.expires_at).isoformat(),
                        "held": s.symbol in self._held,
                    }
                    for s in card.symbols
                },
                "total_cost_usd": card.llm_cost_usd,
            },
            latency_ms=(time.monotonic() - started) * 1000,
            cost_usd=0.0
            if partial
            else self._pick_cost,  # 只记总盘会这一份；各标的的花费在各自的记录里
        )
        if partial:
            return
        self._bump(
            ok=True, cost=0.0, ms=(time.monotonic() - started) * 1000
        )  # 选币花费已在开会时计入
        self._heartbeat(
            Health.OK,
            f"当前卡 v{card.version}，{tradable} 个可做（每轮最多 {self._pick_max} 张）",
            None,
        )

    def _record_error(self, reason: str, exc: Exception, started: float) -> None:
        self._write(
            outcome=Outcome.ERROR,
            summary=f"开会失败（{reason}）：{exc}"[:200],
            latency_ms=(time.monotonic() - started) * 1000,
            cost_usd=self._pick_cost,  # 各标的的花费已在各自记录里
        )
        self._last_error = str(exc)[:200]
        self._heartbeat(Health.DEGRADED, "开会失败，沿用旧卡直到过期", self._last_error)

    def _market_status(
        self, health: Health, headline: str, count: int | None, err: str | None
    ) -> None:
        """总盘会没有决策记录，团队页靠它自己的模块状态显示「上次 / 下次 / 挑了几个」。"""
        if health is Health.OK:
            self._market_last = (self._clock(), f"挑了 {count} 个币上会", count)
            headline = self._market_last[1]
        self._market_state = (health, headline, err)
        self._market_beat()

    def _market_beat(self) -> None:
        """写总盘会的模块状态。每次心跳都带新的 ts 和下一轮会议时间，不然两轮之间会被标成无心跳。"""
        if self._market_state is None:
            return  # 还没开过总盘会：不写，团队页显示「等待首次运行」
        health, headline, err = self._market_state
        now = self._clock()
        last, _, count = self._market_last or (None, "", None)
        self._log.heartbeat(
            ModuleStatus(
                role=Role.MARKET,
                health=health,
                ts=now,
                headline=headline,
                last_error=err,
                error=err,
                version="slow-0.1",
                last_run_at=last,
                next_run_at=self._next_meeting(now),
                symbol_count=count,
            )
        )

    def _heartbeat(self, health: Health, headline: str, err: str | None) -> None:
        self._market_beat()
        now = self._clock()
        self._log.heartbeat(
            ModuleStatus(
                role=Role.COMMANDER,
                health=health,
                ts=now,
                headline=headline,
                today=self._stats,
                last_error=err,
                version="slow-0.1",
                last_run_at=self._last_refresh,
                next_run_at=self._next_meeting(now),
            )
        )
