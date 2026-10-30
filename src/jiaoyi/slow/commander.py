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
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, cast, runtime_checkable
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
    MarketReviewCandidate,
    MarketReviewCard,
    MarketView,
    ModuleStatus,
    Outcome,
    QueuedMarketReview,
    Range,
    RiskConfig,
    Role,
    Side,
    SignalKind,
    SizeTier,
    SymbolPool,
    SymbolPoolEntry,
    TodayStats,
)
from jiaoyi.core import desk_turn, held_events
from jiaoyi.core.us_hours import us_entry_window
from jiaoyi.interfaces import DecisionLog
from jiaoyi.sentinel.regime_flip import Direction, RegimeReference
from jiaoyi.slow import binance_gate, market_table
from jiaoyi.slow.brief import MarketBriefService
from jiaoyi.slow.card_flip import (
    DIR_TEXT,
    REASON_MARK,
    REASON_MAX,
    FlipBook,
    FlipCandidate,
    card_direction,
    held_key,
)
from jiaoyi.slow.card_outlook import with_board
from jiaoyi.slow.credits import is_credit_error
from jiaoyi.slow.market_audit import CURRENT_AUDIT, MarketAudit
from jiaoyi.slow.market_log import MarketLog, pool_rows, round_id
from jiaoyi.slow.meeting_live import MeetingLive
from jiaoyi.slow.review import CardJournal

logger = logging.getLogger(__name__)
_MEETING_NEAR = timedelta(minutes=10)


class MeetingError(RuntimeError):
    """会议中途失败。``cost_usd`` 是已烧掉的真实花费；未知为 None，由指挥官按估算计。"""

    def __init__(
        self, message: str, cost_usd: float | None = None, setup: dict[str, str] | None = None
    ) -> None:
        super().__init__(message)
        self.cost_usd = cost_usd
        self.setup = setup  # 这场会用的偏空整改设置（bias_fix），失败也要记


class CardRefreshError(RuntimeError):
    """这一轮开会没能出卡。"""


@dataclass(frozen=True)
class Pick:
    symbol: str
    reason: str


@dataclass(frozen=True)
class Decision:
    """总盘会对一个候选的结论：挑 / 不挑 + 一句带数字的理由。"""

    symbol: str
    pick: bool
    reason: str


@dataclass(frozen=True)
class PickResult:
    regime: str
    picks: list[Pick]
    cost_usd: float = 0.0
    rejections: list[Pick] = field(default_factory=list)  # 没挑中的币：一句理由
    decisions: list[Decision] = field(default_factory=list)  # 每个候选一条（含没挑的）
    retried: bool = False  # 第一次回答不合格，重问过一次
    errors: list[str] = field(default_factory=list)  # 校验没过的地方（只记录，不丢任何结论）
    breadth: str = ""  # 池子概况那一行（代码算的）
    table: str = ""  # 给模型看的候选表
    regime_direction: Direction = "中性"  # 旧结果没有方向时不从中文猜
    review_candidates: list[MarketReviewCandidate] = field(default_factory=list)
    reviews_valid: bool = False  # 只有显式、完整有效的候选集合才能撤销旧市场原因


@dataclass(frozen=True)
class Analysis:
    card_symbol: CardSymbol
    cost_usd: float = 0.0
    usage: dict[str, int] | None = None  # input_tokens / output_tokens / reasoning_tokens
    # 这场会实际用的偏空整改设置（bias_fix.MeetingSetup.record()）：分组、谁先发言等；旧引擎没有
    setup: dict[str, str] | None = None
    # 这场会实际用的大盘判断（latest_market_regime 开着时可能比本轮开始时新）；None = 没有
    market_view: MarketView | None = None


class MeetingEngine(Protocol):
    async def pick(
        self,
        candidates: Sequence[SymbolPoolEntry],
        max_n: int,
        previous: BattleCard | None,
        pool: Sequence[SymbolPoolEntry] = (),
    ) -> PickResult:
        """总盘会：看大盘和池子，挑最多 ``max_n`` 个值得开会的，并写一句大盘判断。
        ``pool`` 是整个候选池（含尾部），只用来算「池子概况」那一行。"""
        ...

    async def analyze(self, entry: SymbolPoolEntry, regime: str, pick_reason: str = "") -> Analysis:
        """单标的会议：多空辩论 + 风控讨论，输出这个标的的作战指令。
        ``regime`` / ``pick_reason`` 是总盘会这一轮的判断和挑这个币的理由，只作背景；
        固定币 / 持仓强制开会的币传空字符串。"""
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
    """止损 / 止盈倍数的尺度：ATR 周期（分钟）和风控员认的两个倍数区间，来自 ``fast_loop``。

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
        gated = cs.bias if cs.bias in (Bias.LONG, Bias.SHORT) else cs.no_trade_from
        return cs.model_copy(
            update={"bias": Bias.NO_TRADE, "allowed_signals": [], "no_trade_from": gated}
        )
    return cs.model_copy(
        update={
            # 慢环不知道实时权益：只按绝对硬顶夹（没写就不夹），权益倍数由风控员下单时封顶
            "notional_usdt": _clamp_range(cs.notional_usdt, 0.0, risk.notional_cap(None)),
            "leverage": _clamp_range(cs.leverage, 1.0, float(risk.max_leverage)),
            # 止损倍数落在风控员认的区间内，免得整张卡这个标的被 min/max_stop_atr 拒掉
            "stop_atr": _clamp_range(cs.stop_atr, stop.min_stop_atr, stop.max_stop_atr),
            # 止盈倍数同理：低于 min_tp_r 整张卡这个标的会被风控员拒掉
            "take_profit_r": _clamp_range(cs.take_profit_r, stop.min_tp_r, stop.max_tp_r),
        }
    )


MARKET_LIVE = "_MARKET"
"""总盘会实况的固定名字：写进 ``meeting_live/_MARKET.json``。"""
_SIZE_ORDER = {SizeTier.S: 0, SizeTier.M: 1, SizeTier.L: 2}


@runtime_checkable
class MarketDeskOwner(Protocol):
    """大盘台（``slow/market_desk.py``）：此刻大盘方向是否归它管。"""

    def owns_market(self, now: datetime) -> bool: ...


DESK_SOURCE = "market_desk"
"""大盘台发布的 ``MarketView.source``（不往契约加字段，概率等放 market_desk_state.json）。"""


DIRECT_MEET_REASON = "每轮直接开会（设置）"


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
    always_meet: bool = False
    """Always-candidate coins skip the pick and get a per-coin meeting every scheduled round,
    on top of the ``pick_max`` slots (Flash keeps its full room). Works with tiers off too."""


@dataclass(frozen=True)
class EventRules:
    """临时会：卡有效期里出了事件，只给那一个币补开一场（不重开整轮）。

    ``triggers`` 之外的事件不开会。
    """

    enabled: bool = False
    triggers: frozenset[str] = frozenset()
    symbol_gap: timedelta = timedelta(minutes=60)
    """同一个币两场会至少隔多久；0 = 不隔（配置里的 0 由 ``commander_kwargs`` 换成下限）。"""
    max_per_day: int = 12
    """每天最多几场临时会；0 = 不限（照常计数）。"""
    move_atr: float = 3.0
    entry_max_age: timedelta = timedelta(0)
    """开仓前卡龄上限（该币上次开会距今）；0 = 不查。"""
    key_level_trigger: str = "outside"
    """outside = 价格在关键位外面就算突破；cross = 出卡后价格从里侧穿到外侧那一下才算。"""
    concurrency: int = 1
    """同时最多开几场临时会（≥ 1，1 = 一场一场排队）；和整轮共用会议并发的名额。"""


HELD_TRIGGERS: frozenset[str] = held_events.TRIGGERS | {desk_turn.TRIGGER}
"""持仓币的两类临时会、大盘台转向暂停的补会不看 ``event_triggers``，开关在 app.toml
``[fast_loop] patrol_held_event_*`` / ``signal_desk_turn_pause``（热生效）。"""

PENDING_EXPIRY = timedelta(minutes=30)
LEVEL_PX_FRESH = timedelta(minutes=5)
"""开会开始时用多新的价定新关键位的两侧；更旧就等出卡后第一次看到的价。"""
SLOWDOWN_AT = 0.8
EVENT_GAP_FLOOR = timedelta(minutes=5)
"""配置里同币临时会间隔写 0（不限）时，实际仍隔这么久：关键位一直被穿时不会连着开。"""
EVENT_FAIL_BACKOFF = timedelta(minutes=15)
"""一场临时会开失败后，这个币至少隔这么久才再排（失败也算一场，但配置的间隔可能是 0 / 很短）。"""
MARKET_SNAPSHOT_FRESH = timedelta(minutes=2)
"""沿用整轮会前统计的新鲜度要求；不是方向判断阈值。"""
"""日花费到上限的这个比例就放慢开会（over_budget = slow）。"""


REGIME_STALE = "已过期，仅作参考"
VIEW_DIRS_MAX = 512
DIRECTIONS: frozenset[str] = frozenset({"看涨", "看跌", "中性"})
POOL_SCOPE = "其中的涨跌、主动买卖占比等数字是全池口径，不是这个币自己的"


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
        us_hours: tuple[int, int] | None = None,
        fail_backoff: Sequence[timedelta] = (
            timedelta(minutes=5),
            timedelta(minutes=10),
            timedelta(minutes=30),
        ),
        daily_cost_limit_usd: float | None = None,
        spend_alert_usd: float | None = None,
        failed_meeting_cost_usd: float = 0.05,
        budget_path: Path | None = None,
        card_path: Path | None = None,
        tiers: TierRules | None = None,
        tier_path: Path | None = None,
        live_dir: Path | None = None,
        events: EventRules | None = None,
        schedule_at: str | None = None,
        slowdown: tuple[timedelta, timedelta] | None = None,
        market_log: MarketLog | None = None,
        log_meta: Mapping[str, Any] | None = None,
        latest_market_regime: bool = False,
        event_brief_refresh: bool = False,
        reuse_extend_expiry: bool = False,
        regime_time_label: bool = False,
        regime_max_age: timedelta = timedelta(0),
        pool_scope_label: bool = False,
        flip_confirm: int = 1,
        flip_confirm_min: timedelta = timedelta(0),
    ) -> None:
        if ttl <= timedelta(0):
            raise ValueError("作战卡有效期必须大于 0")
        self._clock = clock
        self._brief = brief
        self._latest_regime = latest_market_regime
        self._event_brief_refresh = event_brief_refresh
        self._reuse_extend = reuse_extend_expiry
        self._regime_time_label = regime_time_label
        self._regime_max_age = regime_max_age
        self._pool_scope_label = pool_scope_label
        self._journal = journal
        self._stop = stop_scale
        if not fail_backoff:
            raise ValueError("fail_backoff 至少要有一档")
        self._backoff = tuple(fail_backoff)
        self._cost_limit = daily_cost_limit_usd or None  # None / 0 = 不限
        self._spend_alert = spend_alert_usd or None  # 日上限关了时的「只提醒不停」线
        self._spend_noticed_day = ""
        self._spend_alerts: list[str] = []
        self._event_fail_until: dict[str, datetime] = {}
        self._failed_cost = failed_meeting_cost_usd
        self._budget_path = budget_path
        self._card_path = card_path
        self.regime_reference: RegimeReference | None = None
        self.last_regime_flip_at: datetime | None = None
        self.last_regime_direction: Direction = "中性"
        self.latest_market_view: MarketView | None = None
        self._market_generation = 0
        self._market_published_generation = 0
        self._reference_generation = 0
        self._pending_reference_view: MarketView | None = None
        # 大盘台接管（[market_desk] mode = replace）：它是唯一的大盘发布者，整轮总盘会的大盘判断
        # 只记录；Flash 下一轮只带它自己上一轮的判断（不被大盘台的观点锚定）
        self._desk: MarketDeskOwner | None = None
        self._flash_regime = ""
        self._round_desk_owned = False
        self._market_lock = asyncio.Lock()
        self._market_snapshot: Callable[[SymbolPool, datetime], Awaitable[SymbolPool]] | None = None
        self.market_refresh_cost_usd = 0.0
        self.market_refresh_discarded = False
        self._market_error: str | None = None
        self._market_fails = 0
        self._market_retry_at: datetime | None = None
        self._market_path = card_path.with_name("market_view.json") if card_path else None
        self.round_cost_usd = 0.0
        self._round_reference: RegimeReference | None = None
        self._regime_price: Callable[[], float | None] | None = None
        self._scheduled_due: datetime | None = None
        self._us_hours = us_hours  # (开盘后几分钟, 收盘前几分钟)；None = 不按美股时段限制
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
        self._credit_alarm: Callable[[], None] | None = None
        self._engine = engine
        self._risk = risk
        self._log = log
        self._pick_max = pick_max
        self._sem = asyncio.Semaphore(concurrency)
        self._ttl = ttl
        self._symbol_min_interval = symbol_min_interval
        self._last_meeting: dict[str, datetime] = {}
        self._level_broken: set[str] = set()
        # cross 模式：币 → (这条结论的标识, {关键位 → 上次看到价格是否已在外侧})；结论换了才重记
        self._level_side: dict[str, tuple[str, dict[str, bool]]] = {}
        self._level_cross: dict[str, datetime] = {}  # 穿过了、还没开上会的币 → 穿过的时刻
        self._last_px: dict[str, tuple[float, datetime]] = {}
        self._meet_px: dict[str, float] = {}  # 开会开始时的价：新结论的关键位按它定在哪一侧
        self._blocked: dict[str, str] = {}
        self._untradeable: dict[str, str] = {}
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
        self._market_log = market_log
        self._log_meta: dict[str, Any] = dict(log_meta or {})
        self._always: set[str] = set()  # 设置里「每轮进总盘会」的币（本轮池子里的）
        self._direct_n = 0  # 上一轮不经挑选直接开会的币数（心跳文字用）
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
        self._market_reviews: dict[str, QueuedMarketReview] = {}
        self._symbol_inflight: set[str] = set()
        self._market_review_enabled: Callable[[], bool] = lambda: True
        self._on_market: Callable[[MarketView], None] | None = None
        self._on_card: Callable[[], None] | None = None
        self._on_flip_pending: Callable[[str, FlipCandidate], None] | None = None
        # 待确认翻转的那场会的结论（上卡前已夹好、盖好章）：管家助手确认时换上的就是它
        self._flip_fresh: dict[str, CardSymbol] = {}
        self._event_day = ""
        self._event_count = 0
        self._event_last: dict[str, datetime] = {}
        # 并发临时会的预留：派出去、还没开始计数的币（占当天场数），和还没预记花费的币（占日上限）
        self._event_claimed: set[str] = set()
        self._event_uncharged: set[str] = set()
        self._event_live: list[str] = []  # 正在开的临时会（心跳文字用，按开始先后）
        # 整轮进行中：None = 还没定本轮开哪些币（这段时间不开临时会）；定了以后这些币不开临时会
        self._round_on = False
        self._round_meeting: set[str] | None = None
        self._round_symbols: dict[str, CardSymbol] | None = None
        self._round_expiry: datetime | None = None
        # market_view_id -> direction of the last VIEW_DIRS_MAX publishes, kept in market_view.json
        self._view_dirs: dict[str, Direction] = {}
        self._round_event_merges = 0
        self._round_event_cost = 0.0
        self._ref: dict[str, tuple[datetime, float]] = {}
        self._restore_tiers()
        self._stats = TodayStats()
        self._stats_day = ""
        self._pick_cost = 0.0
        self._restore_budget()
        self._market_last: tuple[datetime, str, int | None] | None = None
        self._market_state: tuple[Health, str, str | None] | None = None
        self._flips = FlipBook(
            flip_confirm,
            flip_confirm_min,
            card_path.with_name("card_flip.json") if card_path else None,
        )
        self._restore_card()
        self._restore_market()
        for sym, lost in self._flips.restore(
            {s.symbol: s for s in self._card.symbols} if self._card is not None else {}
        ).items():
            why = (
                "重启后卡上留着的那条指令对不上"
                if self._flips.on
                else "设置改回 1（card_flip_confirm = 1），新结论立即生效"
            )
            self._flip_dropped(sym, lost, why)

    # ------------------------------------------------------------ 快环读
    def current(self, now: datetime) -> BattleCard | None:
        c = self._card
        if c is None:
            return None
        # 整张卡到期了：没到自己有效期的币（临时会翻了方向的）照常；持仓币的指令留着，只是不开新仓
        c = self._pool_view(c, now) if self._alive(c, now) else self._held_view(c, now)
        if c is None:
            return None
        c = self._mask_us_closed(self._mask_stale(c, now), now)
        c = c.model_copy(update={"current_market": self.latest_market_view})
        if self._untradeable:
            c = c.model_copy(
                update={
                    "symbols": [
                        self._watch_only(s)
                        if s.symbol in self._untradeable and s.symbol not in self._held
                        else s
                        for s in c.symbols
                    ]
                }
            )
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

    def set_untradeable(self, untradeable: Mapping[str, str]) -> None:
        """执行交易所上不在交易的币（币 → 状态）：卡上读成只看不做，持仓币不动。"""
        self._untradeable = dict(untradeable)

    def card_symbols(self) -> set[str]:
        """卡上原始的币名单；不像 ``current`` 那样做卡龄检查，所以不会顺带排补会。"""
        return {s.symbol for s in self._card.symbols} if self._card is not None else set()

    def set_held(self, held: Mapping[str, Side]) -> None:
        """有持仓的币（币 → 持仓方向）：必须强制入选本轮会议，并一直留在卡上继续盯。

        不然持仓管家看到卡上没有它，会判“不再盯”而平仓。
        """
        self._held = dict(held)

    def set_on_picks(self, callback: Callable[[list[str]], None]) -> None:
        """总盘会一挑完币就回调（本轮要开会的币，含置顶和持仓）：主循环马上订阅它们的行情、
        让预测员先算，不用等十几分钟后作战卡出来。"""
        self._on_picks = callback

    @staticmethod
    def _alive(card: BattleCard, now: datetime) -> bool:
        """卡上还有没到期的指令：翻了方向的币有效期从自己开会那一刻起算，可以比整张卡晚到期。"""
        ends = [card.expires_at, *(s.expires_at for s in card.symbols if s.expires_at is not None)]
        return card.created_at <= now < max(ends)

    def _pool_view(self, card: BattleCard, now: datetime) -> BattleCard | None:
        """指令池：每个币的指令按自己的有效期到点消失；持仓币的指令不消失，只是不再开新仓，
        直到被新结论替换。池空了返回 None。整张卡已到期、还有币没到期时，有效期和 ``_held_view``
        一样只往后顶一分钟：快环照常用这些币，整轮照样判「到点了」。"""
        kept: list[CardSymbol] = []
        for s in card.symbols:
            if now < (s.expires_at or card.expires_at):
                kept.append(s)
            elif s.symbol in self._held:
                kept.append(self._watch_only(s))
        if not kept:
            return None
        if now >= card.expires_at:
            return card.model_copy(
                update={"symbols": kept, "expires_at": now + timedelta(minutes=1)}
            )
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
    def _stamp(
        cs: CardSymbol, issued: datetime, expires: datetime, market: MarketView | None = None
    ) -> CardSymbol:
        """给一条指令盖上「哪一刻开的会、到几点到期」；只记录的 outlook 配上同一份总盘会方向。"""
        stamped = cs.model_copy(
            update={
                "issued_at": issued,
                "expires_at": expires,
                "meeting_id": f"{cs.symbol}-{issued:%Y%m%dT%H%M%S}",
                "source_market_view_id": market.market_view_id if market else None,
                "source_regime": market.regime if market else None,
            }
        )
        return with_board(stamped, market)

    def _extend_reused(self, cs: CardSymbol, round_expiry: datetime) -> CardSymbol:
        """``reuse_extend_expiry``: a carried-over conclusion lives until the new round's expiry,
        but never longer than one card TTL from its own meeting, so a stale conclusion is not
        stretched. Never shortens; without ``issued_at`` its age is unknown and it is left alone.
        Nor is a conclusion researched under a market direction other than the one in force now
        (or an unknown one): it must not outlive the regime it was argued against."""
        if not self._reuse_extend or cs.issued_at is None or cs.expires_at is None:
            return cs
        now_view = self.latest_market_view
        if now_view is None or self._source_direction(cs) != now_view.regime_direction:
            return cs
        target = min(round_expiry, cs.issued_at + self._ttl_now())
        if target <= cs.expires_at:
            return cs
        return cs.model_copy(update={"expires_at": target})

    def _flip_expiry(self, before: CardSymbol | None, after: CardSymbol) -> CardSymbol:
        """临时会 / 管家助手换上的指令按卡的到期时间走（不改整轮节奏），但两种情况例外：
        方向变了（做多 ↔ 做空、中性或卡上没有 → 做多 / 做空），从这场会的 ``issued_at`` 起给满一整轮
        有效期，不然刚翻的方向几分钟后就随整张卡到期；方向没变（同向再确认、会判观望·卡保留、
        候选期）不重置，只是不比卡上那条更早到期。转中性不重置：中性不开仓。"""
        new = card_direction(after)
        old = card_direction(before) if before is not None else "neutral"
        if new != old and new != "neutral" and after.issued_at is not None:
            target = after.issued_at + self._ttl_now()
        elif new == old and before is not None and before.expires_at is not None:
            target = before.expires_at
        else:
            return after
        if after.expires_at is not None and target <= after.expires_at:
            return after
        return after.model_copy(update={"expires_at": target})

    def _source_direction(self, cs: CardSymbol) -> Direction | None:
        vid = cs.source_market_view_id
        if vid is None:
            return None
        if vid in self._view_dirs:
            return self._view_dirs[vid]
        latest = self.latest_market_view
        if latest is not None and latest.market_view_id == vid:
            return latest.regime_direction
        o = cs.outlook
        if o is not None and o.board_market_view_id == vid:
            return o.board_regime_direction
        return None

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

    def _flip_gate(
        self, symbol: str, fresh: CardSymbol, in_force: CardSymbol | None, now: datetime
    ) -> CardSymbol:
        if not self._flips.on:
            self._flips.clear()  # switched back to 1: leftover candidates must not resurface
        out = self._flips.gate(symbol, fresh, in_force, now)
        kept = self._flips.get(symbol)
        if out.event == "ignored" and kept is not None and kept.candidate == "neutral":
            # a neutral candidate an earlier sweep could not restore: this meeting retries it
            restored = self._restore_neutral(symbol, out.card_symbol, kept)
            if restored is not None:
                out = replace(out, card_symbol=restored)
        if out.event == "pending":
            self._flip_fresh[symbol] = fresh
        elif out.event != "ignored":  # an ignored neutral meeting leaves a pending flip alone
            self._flip_fresh.pop(symbol, None)
        if out.dropped is not None:
            why = "卡上留着的那条指令已到期被拿掉" if in_force is None else "卡上那条指令已被换掉"
            self._flip_dropped(symbol, out.dropped, why)
        if out.event is not None:
            self._write(
                symbol=symbol,
                outcome=Outcome.INFO,
                summary=out.summary[:200],
                output={"card_flip": out.event, **(out.detail or {})},
            )
        cand = self._flips.get(symbol) if out.event == "pending" else None
        if cand is not None and self._on_flip_pending is not None:
            try:  # 只交出去（对方另起任务），这里不等：出卡路径保持没有 await
                self._on_flip_pending(symbol, cand)
            except Exception:
                logger.exception("%s 待确认翻转交给管家助手失败，照旧等下一场会", symbol)
        elif cand is not None and self._flips.by_assistant():
            self._write(
                symbol=symbol,
                outcome=Outcome.INFO,
                summary=f"{symbol} 待确认翻转没人确认：管家助手没接上，卡上照旧只看不做",
                output={"card_flip": "unasked", "why": "管家助手没接上"},
            )
        return out.card_symbol

    # ------------------------------------------------------------ 管家助手
    def set_on_flip_pending(self, callback: Callable[[str, FlipCandidate], None] | None) -> None:
        """翻转候选刚记下（还没确认）时同步回调（币, 那一刻的候选）；回调方不能在这里等。"""
        self._on_flip_pending = callback

    def set_neutral_flip_ignored(self, ignored: Callable[[], bool]) -> None:
        """返回 True 时会判不做不算翻转，卡上留原方向（每次调用时读，热生效）。"""
        self._flips.neutral_ignored = ignored

    def set_flip_limits(
        self, ignore_max_age: Callable[[], timedelta], fallback_after: Callable[[], timedelta]
    ) -> None:
        """``card_ignore_max_age_min`` 和 ``card_flip_assistant_fallback_min``（每次调用时读）。"""
        self._flips.ignore_max_age = ignore_max_age
        self._flips.fallback_after = fallback_after

    def set_flip_by_assistant(self, by_assistant: Callable[[], bool]) -> None:
        """返回 True 时会只提候选，不按场数确认（每次调用时读，热生效）。"""
        self._flips.by_assistant = by_assistant

    def confirm_flip(
        self, symbol: str, expected: FlipCandidate, why: str, detail: dict[str, Any]
    ) -> bool:
        """管家助手确认待确认翻转：一步同步做完（中间没有 await）。翻转簿里还是交出去时那个候选
        （方向、场数、首次出现、留在卡上的那条指令都没变），卡上生效的指令也还是它，才换上那场会
        的结论，和第 N 场会确认时一样上卡、唤醒管家；否则什么都不动，返回 False。"""
        cand, fresh, card = self._flips.get(symbol), self._flip_fresh.get(symbol), self._card
        in_force = card.for_symbol(symbol) if card is not None else None
        if (
            fresh is None
            and in_force is not None
            and cand is not None
            and cand.candidate == "neutral"
        ):
            fresh = self._neutral_body(in_force)
        if (
            cand is None
            or fresh is None
            or card is None
            or in_force is None
            or (cand.candidate, cand.count, cand.first_seen, cand.held)
            != (expected.candidate, expected.count, expected.first_seen, expected.held)
            or held_key(in_force) != cand.held
        ):
            return False
        self._flips.discard(symbol)
        self._flip_fresh.pop(symbol, None)
        self._merge_symbol(card, self._flip_expiry(in_force, fresh), "event:keeper_assistant_flip")
        self._write(
            symbol=symbol,
            outcome=Outcome.INFO,
            summary=f"{symbol} 翻转确认：{why}，卡上换成待确认的那场结论"[:200],
            output={"card_flip": "confirmed", "by": "keeper_assistant", **detail},
        )
        return True

    def _merge_symbol(self, card: BattleCard, cs: CardSymbol, reason: str) -> None:
        """一步同步换掉卡上一个币的指令（中间没有 await），和临时会出卡一样上卡、唤醒管家。"""
        symbols = [cs if s.symbol == cs.symbol else s for s in card.symbols]
        card = card.model_copy(
            update={"version": card.version + 1, "symbols": symbols, "refresh_reason": reason}
        )
        if self._round_symbols is not None:
            self._round_symbols[cs.symbol] = cs
            self._round_event_merges += 1
        self._card = card
        self._notify_card()
        self._save_card()
        self._record_card(card, time.monotonic())

    def clear_neutral_flips(self) -> None:
        """``card_neutral_flip = "ignore"`` 刚生效（启动或切过来）：转中性的候选都作废，卡上那条
        恢复成候选前的样子（原方向、原来的信号）。找不到原来的信号就留着候选，记一条。"""
        if not self._flips.neutral_ignored():
            return
        for symbol, cand in self._flips.items().items():
            card = self._card
            if cand.candidate != "neutral" or card is None:
                continue
            held = card.for_symbol(symbol)
            if held is None or held_key(held) != cand.held:
                continue  # the next meeting drops it as today
            restored = self._restore_neutral(symbol, held, cand)
            if restored is not None:
                self._merge_symbol(card, restored, "event:card_neutral_flip_ignore")

    def _restore_neutral(
        self, symbol: str, held: CardSymbol, cand: FlipCandidate
    ) -> CardSymbol | None:
        """转中性的候选作废、``held``（候选期留在卡上那条）恢复候选前的信号；返回恢复后的指令，
        候选已丢掉、记好了。找不到原来的信号就什么都不动，记一条 ``kept``，返回 None。"""
        signals = cand.signals if cand.signals is not None else self._signals_of(held)
        if signals is None:
            self._write(
                symbol=symbol,
                outcome=Outcome.INFO,
                summary=f"{symbol} 转中性的待确认翻转没清：找不到候选前的信号，等下一场会",
                output={"card_flip": "kept", "why": "找不到候选前的信号", **cand.to_json()},
            )
            return None
        base = held.reason.split(REASON_MARK, 1)[-1]
        self._flips.discard(symbol)
        self._flip_fresh.pop(symbol, None)
        self._write(
            symbol=symbol,
            outcome=Outcome.INFO,
            summary=f"{symbol} 会议判不做，按设置不翻转，保留原方向"
            f"（{DIR_TEXT[cand.confirmed]}），恢复开仓"[:200],
            output={"card_flip": "ignored", **cand.to_json()},
        )
        return held.model_copy(
            update={"allowed_signals": [SignalKind(s) for s in signals], "reason": base}
        )

    def flip_answered(self, symbol: str, cand: FlipCandidate) -> None:
        """管家助手的两个模型对这个候选真答过了（同不同意都算）：之后不再按「问不了」兜底。"""
        self._flips.mark_answered(symbol, cand)

    def stuck_held_flips(self, now: datetime, older: timedelta) -> list[tuple[str, FlipCandidate]]:
        """有持仓的币里，候选从第一次出现起已超过 ``older`` 的。"""
        return [
            (s, f)
            for s, f in self._flips.items().items()
            if s in self._held and now - f.first_seen > older
        ]

    def _signals_of(self, held: CardSymbol) -> tuple[str, ...] | None:
        """候选是旧代码记的、没存信号：从出这条指令的那场会的决策记录里找。会议记录里的结论还没
        盖 meeting_id，只能按币、方向和开会时刻（前后 10 分钟内最近的一条）认。"""
        scan = getattr(self._log, "scan_day", None)
        if scan is None or held.issued_at is None:
            return None
        issued = held.issued_at.astimezone(UTC)
        best: tuple[timedelta, tuple[str, ...]] | None = None
        for day in {
            issued.date(),
            (issued - _MEETING_NEAR).date(),
            (issued + _MEETING_NEAR).date(),
        }:
            for raw in scan(day.isoformat(), held.symbol):
                out = raw.get("output")
                try:
                    ts = datetime.fromisoformat(str(raw.get("ts")))
                except ValueError:
                    continue
                gap = abs(ts - issued)
                if (
                    raw.get("role") == Role.COMMANDER.value
                    and raw.get("symbol") == held.symbol
                    and isinstance(out, dict)
                    and out.get("bias") == held.bias.value
                    and isinstance(out.get("allowed_signals"), list)
                    and gap <= _MEETING_NEAR
                    and (best is None or gap < best[0])
                ):
                    best = (gap, tuple(str(x) for x in out["allowed_signals"]))
        return best[1] if best is not None else None

    @staticmethod
    def _neutral_body(held: CardSymbol) -> CardSymbol:
        """重启后手上没有那场转中性的会的结论：拿卡上留着的那条改成中性。不开新仓、关键位和
        止损区间都不再用来开仓，所以沿用它是安全的；做多 ↔ 做空不能这么拼（关键位是反方向的）。"""
        base = held.reason.split(REASON_MARK, 1)[-1]
        head = "管家助手确认转中性（没有那场会的卡面，沿用卡上那条）"
        return held.model_copy(
            update={
                "bias": Bias.NO_TRADE,
                "no_trade_from": None,
                "allowed_signals": [],
                "reason": f"{head}{REASON_MARK}{base}"[:REASON_MAX],
            }
        )

    def sweep_flips(self) -> None:
        """把翻转簿里现有的候选都交给管家助手（启动时、切到只由管家助手确认时）。做多 ↔ 做空的
        候选要那场会的卡面才能上卡，重启后没有就不交，记一条，等下一场会。"""
        if self._on_flip_pending is None:
            return
        for symbol, cand in self._flips.items().items():
            if cand.candidate == "neutral" and self._flips.neutral_ignored():
                continue
            if cand.candidate != "neutral" and symbol not in self._flip_fresh:
                why = "需要下一场会的卡面"
                self._write(
                    symbol=symbol,
                    outcome=Outcome.INFO,
                    summary=f"{symbol} 待确认翻转（{DIR_TEXT[cand.candidate]}）没交给管家助手："
                    f"{why}（重启后没有那场会的结论），卡上照旧只看不做"[:200],
                    output={"card_flip": "unasked", "why": why, **cand.to_json()},
                )
                continue
            try:
                self._on_flip_pending(symbol, cand)
            except Exception:
                logger.exception("%s 待确认翻转交给管家助手失败", symbol)

    def _flip_dropped(self, symbol: str, lost: FlipCandidate, why: str) -> None:
        self._write(
            symbol=symbol,
            outcome=Outcome.INFO,
            summary=f"{symbol} 待确认翻转作废：{why}"[:200],
            output={"card_flip": "dropped", "why": why, **lost.to_json()},
        )

    @staticmethod
    def _raw_view(card: BattleCard, fresh: CardSymbol) -> BattleCard:
        """The review journal scores what the meeting concluded, not the pending-flip stand-in."""
        cs = card.for_symbol(fresh.symbol)
        if cs is None or cs is fresh:
            return card
        return card.model_copy(
            update={"symbols": [fresh if s.symbol == fresh.symbol else s for s in card.symbols]}
        )

    def block_symbol(self, symbol: str, reason: str) -> None:
        """封掉一个币的新开仓（例如币安公告要下架它）。风控员只认作战卡，所以这里改卡。

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

    def set_regime_price(self, source: Callable[[], float | None]) -> None:
        """主循环提供此刻 BTC 价；pick 完成时取，整轮成功后才更新参照。"""
        self._regime_price = source

    def regime_flip_blocked(self, now: datetime) -> str | None:
        if binance_gate.banned_until():
            return "限流"
        if self._desk_owns(now):
            return "大盘台接管中，Flash 不独立更新大盘"
        if self._market_retry_at is not None and now < self._market_retry_at:
            return "市场刷新失败退避"
        if self.may_refresh(now):
            return None
        if self._budget_exhausted(now):
            return "日预算"
        if self._rl_until is not None and now < self._rl_until:
            return "限流"
        return "失败退避"

    def mark_regime_flip(self, now: datetime) -> None:
        self.last_regime_flip_at = now
        self._save_market()  # 失败也消耗钱，重启不能绕过最少间隔

    def take_notice(self) -> str | None:
        """取走一条待推送的提示（例如“当日花费超限，停止开会”），没有返回 None。"""
        return self._notices.pop(0) if self._notices else None

    def take_spend_alert(self) -> str | None:
        """取走一条「日上限不限时花费过线」的提醒（只提醒，不是暂停开会），没有返回 None。"""
        return self._spend_alerts.pop(0) if self._spend_alerts else None

    def _ttl_now(self) -> timedelta:
        """作战卡有效期；日花费到 80% 且设了放慢，新卡按放慢后的间隔出。"""
        return self._slowdown[0] if self._slowdown is not None and self._slowed() else self._ttl

    def _day(self, now: datetime) -> str:
        return now.astimezone(ZoneInfo(self._risk.day_reset_tz)).date().isoformat()

    def _budget_exhausted(self, now: datetime) -> bool:
        """今天（按 day_reset_tz，曼谷）开会花费 ≥ 上限。每天只提示一次。"""
        if self._stats_day != self._day(now):
            return False
        if self._cost_limit is None:
            self._spend_alerted(now)
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

    def _spend_alerted(self, now: datetime) -> None:
        """日上限关了（不限）：花费超过档位默认日上限时只推一条提醒（每天一次），不停会、不放慢。"""
        if self._spend_alert is None or self._stats.cost_usd < self._spend_alert:
            return
        day = self._day(now)
        if self._spend_noticed_day != day:
            self._spend_noticed_day = day
            self._spend_alerts.append(
                f"军师团今天已花 ${self._stats.cost_usd:.2f}，超过本档默认日上限 "
                f"${self._spend_alert:.2f}（日上限已设为不限，不会停会，只提醒）"
            )

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
        if self._cross_mode:
            self._last_px.update({sym: (px, now) for sym, px in prices.items()})
        if self._cross_mode and self._card is not None:
            # 每次检查都记两侧、认穿越，不受开会间隔 / 退避影响；间隔只管开不开会
            for cs in self._card.symbols:
                px = prices.get(cs.symbol)
                if px is not None and self._level_hit(cs, px, now):
                    self._level_broken.add(cs.symbol)
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
        if not self._cross_mode:
            for cs in card.symbols:
                px = prices.get(cs.symbol)
                if px is not None and any(self._broken(lv, px) for lv in cs.key_levels):
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
        due = self._scheduled_due or (card.expires_at - self._lead)
        return "scheduled" if now >= due else None

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
        if trigger not in self._events.triggers and trigger not in HELD_TRIGGERS:
            return
        if symbol not in self._pending:
            self._pending[symbol] = (trigger, now)
        queued = self._market_reviews.get(symbol)
        if queued is not None and trigger not in queued.reasons:
            self._market_reviews[symbol] = queued.model_copy(
                update={
                    "reasons": [*queued.reasons, trigger],
                    "reason_queued_at": {**queued.reason_queued_at, trigger: now},
                }
            )
            self._save_market()

    def accepts_event(self, trigger: str) -> bool:
        """开了临时会、且这类事件在名单里（不然 ``request_event`` 会被忽略）。"""
        return self._events.enabled and (
            self._market_review_enabled()
            if trigger == "market_review"
            else trigger in self._events.triggers or trigger in HELD_TRIGGERS
        )

    def request_event(self, symbol: str, trigger: str) -> None:
        """外部事件（预测员连续看反作战卡等）：排进临时会队列，没开临时会 / 不在名单里就忽略。"""
        if self._events.enabled:
            self._queue(symbol, trigger, self._clock())

    def met_since(self, symbol: str, at: datetime) -> bool:
        """这个币在 ``at`` 之后开始过会（含开失败的临时会），或此刻正在开 / 已派出。"""
        if symbol in self._symbol_inflight | self._event_claimed | (self._round_meeting or set()):
            return True
        starts = (self._last_meeting.get(symbol), self._event_last.get(symbol))
        return any(t is not None and t >= at for t in starts)

    def meeting_pending(self, symbol: str) -> bool:
        """这个币的单币会此刻在排队、已派出或在开（整轮本轮要开的也算）。"""
        return symbol in (
            self._pending.keys()
            | self._symbol_inflight
            | self._event_claimed
            | (self._round_meeting or set())
        )

    def set_market_review_enabled(self, enabled: Callable[[], bool]) -> None:
        self._market_review_enabled = enabled

    def set_on_market(self, callback: Callable[[MarketView], None]) -> None:
        self._on_market = callback

    def set_on_card(self, callback: Callable[[], None]) -> None:
        """单币结论已生效时唤醒管家；接收方必须重新读取最新卡，不能用排队旧卡。"""
        self._on_card = callback

    def _notify_card(self) -> None:
        if self._on_card is not None:
            try:
                self._on_card()
            except Exception:
                logger.exception("新卡已生效，但唤醒管家失败，留待例行巡检")

    @property
    def last_market_success_at(self) -> datetime | None:
        return self.latest_market_view.observed_at if self.latest_market_view else None

    def market_review_context(self, pool: SymbolPool) -> list[MarketReviewCard]:
        card = self._card
        now = self._clock()
        if card is None or not card.is_valid(now):
            return []
        symbols = {e.symbol for e in pool.in_pool}
        # 大盘台接管时卡上各币的来源大盘是大盘台的文字：给 Flash 看的复核摘要换成 Flash 自己上一轮
        # 的判断（没有就不给），不让大盘台的观点锚定 Flash 对照
        desk = self._desk_owns(now)
        return [
            MarketReviewCard(
                symbol=cs.symbol,
                research_token=cs.research_token(),
                bias=cs.bias,
                reason=cs.reason,
                issued_at=cs.issued_at or card.created_at,
                source_regime=(self._flash_regime or None)
                if desk
                else cs.source_regime or card.regime,
                source_market_view_id=cs.source_market_view_id,
                key_levels=cs.key_levels,
            )
            for cs in card.symbols
            if cs.symbol in symbols
        ]

    def _event_eligible(self, symbol: str, now: datetime) -> datetime:
        current = self._card.for_symbol(symbol) if self._card else None
        issued = (current.issued_at or self._card.created_at) if current and self._card else None
        last = max(
            (
                at
                for at in (self._event_last.get(symbol), self._last_meeting.get(symbol), issued)
                if at
            ),
            default=None,
        )
        at = last + self._events.symbol_gap if last else datetime.min.replace(tzinfo=UTC)
        if (failed := self._event_fail_until.get(symbol)) is not None:
            at = max(at, failed)
        self._roll_events(now)
        if self._events.max_per_day and self._event_count >= self._events.max_per_day:
            local = now.astimezone(ZoneInfo(self._risk.day_reset_tz))
            tomorrow = (local + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            at = max(at, tomorrow.astimezone(UTC))
        return at

    def _drop_market_review(self, symbol: str, why: str) -> None:
        queued = self._market_reviews.pop(symbol, None)
        if queued is None:
            return
        others = [
            r
            for r in queued.reasons
            if r != "market_review"
            and self._clock() - queued.reason_queued_at.get(r, queued.first_seen) <= PENDING_EXPIRY
        ]
        if others:
            first_reason = min(
                others, key=lambda reason: queued.reason_queued_at.get(reason, queued.first_seen)
            )
            self._pending[symbol] = (
                first_reason,
                queued.reason_queued_at.get(first_reason, queued.first_seen),
            )
        else:
            self._pending.pop(symbol, None)
        self._write(
            symbol=symbol,
            outcome=Outcome.INFO,
            summary=f"{symbol} 市场复核提名已忽略：{why}"[:200],
            output={"market_review": "ignored", "reason": why},
        )
        self._save_market()

    def enqueue_market_reviews(
        self,
        candidates: Sequence[MarketReviewCandidate],
        pool: SymbolPool,
        context: Sequence[MarketReviewCard],
        view: MarketView,
    ) -> None:
        supplied = {s.symbol: s for s in context}
        entries = {e.symbol: e for e in pool.in_pool}
        retained: set[str] = set()
        for candidate in candidates:
            sym = candidate.symbol
            old = supplied.get(sym)
            current = self._card.for_symbol(sym) if self._card else None
            entry = entries.get(sym)
            why = ""
            if not self.accepts_event("market_review"):
                why = "市场复核或总临时会开关已关闭"
            elif entry is None or old is None or current is None:
                why = "不在本次原池与现卡交集"
            elif (
                candidate.research_token != old.research_token
                or current.research_token() != old.research_token
            ):
                why = "该币研究已替换，提名过时"
            elif sym in self._symbol_inflight:
                why = "该币正在研究，合并到现有会议，不取消重开"
            else:
                values = {f"metrics.{key}": value for key, value in entry.metrics.items()}
                values.update(
                    {
                        f"filters.{key}": value
                        for key, value in entry.filters.model_dump().items()
                        if isinstance(value, int | float) and not isinstance(value, bool)
                    }
                )
                if not candidate.evidence or any(
                    e.field not in values
                    or not math.isclose(e.value, values[e.field], rel_tol=1e-9, abs_tol=1e-9)
                    for e in candidate.evidence
                ):
                    why = "缺少与本次新鲜快照匹配的具体数字证据"
            self._write(
                symbol=sym,
                outcome=Outcome.INFO,
                summary=f"{sym} 大盘提名单币复核：{why or candidate.reason}"[:200],
                output={
                    "market_review": "ignored" if why else "nominated",
                    "market_view_id": view.market_view_id,
                    "candidate": candidate.model_dump(mode="json"),
                    "reason": why,
                },
            )
            if why:
                continue
            now = self._clock()
            retained.add(sym)
            previous = self._market_reviews.get(sym)
            first = previous.first_seen if previous else self._pending.get(sym, ("", now))[1]
            eligible = (
                previous.eligible_at if previous else max(now, self._event_eligible(sym, now))
            )
            reasons = (
                previous.reasons
                if previous
                else list(
                    dict.fromkeys(
                        [
                            *([self._pending[sym][0]] if sym in self._pending else []),
                            "market_review",
                        ]
                    )
                )
            )
            queued = QueuedMarketReview(
                candidate=candidate,
                market_view_id=view.market_view_id,
                first_seen=first,
                eligible_at=eligible,
                expires_at=eligible + PENDING_EXPIRY,
                reasons=reasons,
                reason_queued_at=(
                    previous.reason_queued_at
                    if previous
                    else {
                        **(
                            {self._pending[sym][0]: self._pending[sym][1]}
                            if sym in self._pending
                            else {}
                        ),
                        "market_review": first,
                    }
                ),
            )
            self._market_reviews[sym] = queued
            self._pending.setdefault(sym, ("market_review", first))
            self._write(
                symbol=sym,
                outcome=Outcome.INFO,
                summary=f"{sym} 市场复核已排队，最早 {eligible.isoformat()}"[:200],
                output={"market_review": "queued", **queued.model_dump(mode="json")},
            )
        for symbol in set(self._market_reviews) - retained:
            self._drop_market_review(symbol, "本次新市场未再提名，撤销旧市场原因")
        self._save_market()

    def _detect(
        self,
        now: datetime,
        pool: SymbolPool,
        card: BattleCard,
        prices: Mapping[str, float],
        atrs: Mapping[str, float],
    ) -> None:
        in_pool = {e.symbol for e in pool.in_pool}
        for cs in card.symbols:
            sym, px = cs.symbol, prices.get(cs.symbol)
            if px is None:
                continue
            if self._cross_mode:
                if self._cross_pending(sym, now):
                    old = self._pending.get(sym)
                    if (
                        old is not None
                        and old[0] == "key_level_break"
                        and sym in in_pool
                        and sym not in self._market_reviews
                        and now - old[1] > PENDING_EXPIRY
                    ):
                        self._pending.pop(sym)  # 穿过时同币间隔没到、排队过期了：重排，等间隔满
                    self._queue(sym, "key_level_break", now)
            else:
                if any(self._broken(lv, px) for lv in cs.key_levels):
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

    def _mask_us_closed(self, card: BattleCard, now: datetime) -> BattleCard:
        """美股 / 加密股合约不在开仓时段：卡上标「休市不可做」，清空信号（方向不动、持仓照常盯）。
        金属不受限。风控员那边另有同样的闸。"""
        if self._us_hours is None or us_entry_window(now, *self._us_hours):
            return card
        us = (Category.US_TECH, Category.CRYPTO_STOCK)
        if not any(s.category in us and s.allowed_signals for s in card.symbols):
            return card
        return card.model_copy(
            update={
                "symbols": [
                    self._watch_only(s).model_copy(
                        update={"reason": f"休市不可做：{s.reason}"[:400]}
                    )
                    if s.category in us and s.allowed_signals
                    else s
                    for s in card.symbols
                ]
            }
        )

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
        if self._event_budget_held(now) or (self._round_on and self._round_meeting is None):
            return None
        self._roll_events(now)
        if (
            self._events.max_per_day
            and self._event_count + len(self._event_claimed) >= self._events.max_per_day
        ):
            for symbol, quota_item in self._market_reviews.items():
                eligible = max(quota_item.eligible_at, self._event_eligible(symbol, now))
                self._market_reviews[symbol] = quota_item.model_copy(
                    update={
                        "eligible_at": eligible,
                        "expires_at": eligible + PENDING_EXPIRY,
                    }
                )
            self._save_market()
            return None
        by_symbol = {e.symbol: e for e in pool.in_pool}
        busy = self._symbol_inflight | self._event_claimed | (self._round_meeting or set())
        for sym in sorted(self._pending, key=lambda x: x not in self._held):
            if sym in busy:
                continue
            queued = self._market_reviews.get(sym)
            if queued is not None:
                current = self._card.for_symbol(sym) if self._card else None
                if (
                    not self.accepts_event("market_review")
                    or current is None
                    or current.research_token() != queued.candidate.research_token
                    or sym not in by_symbol
                ):
                    self._drop_market_review(sym, "开关关闭、移出原池或该币研究已替换")
                    if sym not in self._pending:
                        continue
                    queued = None
                else:
                    eligible = max(queued.eligible_at, self._event_eligible(sym, now))
                    if eligible > queued.eligible_at:
                        queued = queued.model_copy(
                            update={
                                "eligible_at": eligible,
                                "expires_at": eligible + PENDING_EXPIRY,
                            }
                        )
                        self._market_reviews[sym] = queued
                        self._save_market()
                    if now < queued.eligible_at:
                        continue
            trigger, at = self._pending[sym]
            if queued is not None and (
                self.latest_market_view is None
                or queued.market_view_id != self.latest_market_view.market_view_id
            ):
                # 新市场未提供有效集合：保位置但不将旧证据当作新版确认。
                others = [
                    reason
                    for reason in queued.reasons
                    if reason != "market_review"
                    and now - queued.reason_queued_at.get(reason, queued.first_seen)
                    <= PENDING_EXPIRY
                ]
                if not others:
                    if now > queued.expires_at:
                        self._drop_market_review(sym, "未获新版确认且可执行窗口已过期")
                    continue
                trigger = min(
                    others,
                    key=lambda reason: queued.reason_queued_at.get(reason, queued.first_seen),
                )
                at = queued.reason_queued_at.get(trigger, queued.first_seen)
            expires = queued.expires_at if queued else at + PENDING_EXPIRY
            if now > expires or sym not in by_symbol:
                if queued is not None:
                    self._drop_market_review(sym, "可执行窗口已过期")
                else:
                    self._pending.pop(sym, None)
                continue
            last = max(
                (t for t in (self._event_last.get(sym), self._last_meeting.get(sym)) if t),
                default=None,
            )
            if last is not None and now - last < self._events.symbol_gap:
                continue
            if (failed := self._event_fail_until.get(sym)) is not None and now < failed:
                continue
            return sym, trigger
        return None

    @property
    def symbol_timeout_s(self) -> float:
        return self._symbol_timeout_s

    @property
    def event_concurrency(self) -> int:
        return max(1, self._events.concurrency)

    @property
    def round_symbols_known(self) -> bool:
        """整轮在开、且本轮开哪些币已经定了：这时才能给别的币派临时会。主循环自己的「整轮在开」
        标记比这早（开会前还要选币、拉简报），那段时间不派，免得和本轮挑中的币撞。"""
        return self._round_on and self._round_meeting is not None

    def claim_event(self, now: datetime, pool: SymbolPool) -> tuple[str, str] | None:
        """``pop_event`` 并预留：同一刻再派下一场时，这个币、它占的当天场数和预估花费都已算上，
        并发派出的几场不会同币重叠、也不会一起越过每日上限 / 日花费上限。预留由
        ``refresh_symbol`` 释放（开成了转成正式计数）。"""
        event = self.pop_event(now, pool)
        if event is not None:
            self._event_claimed.add(event[0])
            if self._cost_limit is not None:
                self._event_uncharged.add(event[0])
        return event

    def _event_budget_held(self, now: datetime) -> bool:
        """已派出、还没预记花费的临时会按估算算进今天花费后，是否已到日上限。"""
        if self._cost_limit is None or not self._event_uncharged:
            return False
        if self._stats_day != self._day(now):
            return False
        held = self._estimate(self._meeting_costs) * len(self._event_uncharged)
        return self._stats.cost_usd + held >= self._cost_limit

    def _progress_text(self) -> str:
        parts = [self._progress] if self._progress else []
        if self._event_live:
            parts.append(f"{'、'.join(self._event_live)} 临时会")
        return "；".join(parts)

    async def refresh_symbol(
        self, pool: SymbolPool, symbol: str, trigger: str
    ) -> BattleCard | None:
        """临时会：只给一个币补开一场，结论并进现有作战卡（有效期不延长）。开不成返回 None，
        旧卡照旧；同一个币的间隔照常计，免得失败后每分钟重试。"""
        try:
            return await self._refresh_symbol(pool, symbol, trigger)
        finally:
            self._event_claimed.discard(symbol)
            self._event_uncharged.discard(symbol)

    async def _refresh_symbol(
        self, pool: SymbolPool, symbol: str, trigger: str
    ) -> BattleCard | None:
        if symbol in self._symbol_inflight or symbol in (self._round_meeting or set()):
            return None  # 这个币正在开会（另一场临时会 / 本轮整轮），不叠开
        now = self._clock()
        self._loop = asyncio.get_running_loop()
        nomination = self._market_reviews.get(symbol)
        if nomination is not None and (
            self.latest_market_view is None
            or nomination.market_view_id != self.latest_market_view.market_view_id
        ):
            if trigger == "market_review":
                return None
            nomination = None  # 其他真实事件可独立执行，不冒认旧市场提名已确认。
        if nomination is not None:
            current = self._card.for_symbol(symbol) if self._card else None
            if (
                not self.accepts_event("market_review")
                or current is None
                or current.research_token() != nomination.candidate.research_token
            ):
                self._drop_market_review(symbol, "开会前该币研究已替换或开关关闭")
                if trigger == "market_review":
                    return None
                nomination = None
            elif (
                not self.may_refresh(now)
                or now < nomination.eligible_at
                or now < self._event_eligible(symbol, now)
            ):
                return None
        elif trigger == "market_review":
            return None
        self._pending.pop(symbol, None)
        self._market_reviews.pop(symbol, None)
        self._save_market()
        card = self._card
        entry = next((e for e in pool.in_pool if e.symbol == symbol), None)
        if card is None or not card.is_valid(now) or entry is None:
            return None
        self._roll_events(now)
        self._event_count += 1
        self._event_claimed.discard(symbol)  # 预留转成正式计数
        self._event_last[symbol] = now
        self._level_cross.pop(symbol, None)  # 这次穿越换到了一场会：开不成也不再排
        self._mark_meeting_px(symbol, now)
        source_symbol = card.for_symbol(symbol)
        source_token = source_symbol.research_token() if source_symbol else None
        if nomination is not None:
            self._write(
                symbol=symbol,
                outcome=Outcome.INFO,
                summary=f"{symbol} 市场提名复核开始：{nomination.candidate.reason}"[:200],
                output={
                    "market_review": "started",
                    "nomination": nomination.model_dump(mode="json"),
                },
            )
        self._save_market()
        # 紧挨着 try 登记：上面写记录 / 落盘出错时不能把这个币永远留在「正在开会」里
        self._symbol_inflight.add(symbol)
        self._event_live.append(symbol)
        beat = asyncio.create_task(self._beat_while_meeting())
        started = time.monotonic()
        try:
            market = self.latest_market_view
            regime = market.regime if market else card.regime
            brief_text = self._brief.text if self._brief is not None else ""
            if self._event_brief_refresh and self._brief is not None:
                try:
                    brief_text, _ = await self._market_brief()  # 简报自己 10 分钟内不重复拉
                except Exception as exc:
                    self._write(
                        symbol=symbol,
                        outcome=Outcome.ERROR,
                        summary=f"{symbol} 临时会刷新简报失败，用上一份：{exc}"[:200],
                    )
            res = await self._analyze_one(
                entry,
                regime,
                nomination.candidate.reason if nomination else "",
                brief_text=brief_text,
                market_view=market,
            )
        except BaseException as exc:
            if nomination is not None:
                self._write(
                    symbol=symbol,
                    outcome=Outcome.ERROR,
                    summary=f"{symbol} 市场提名复核失败：{self._why(exc)}"[:200],
                    output={"market_review": "failed", "market_view_id": nomination.market_view_id},
                )
            if isinstance(exc, asyncio.CancelledError):
                raise
            self._event_fail_until[symbol] = self._clock() + EVENT_FAIL_BACKOFF
            return None  # 失败原因和花费已在各标的记录里
        finally:
            beat.cancel()
            self._symbol_inflight.discard(symbol)
            self._event_live.remove(symbol)
        latest_card = self._card
        latest_symbol = latest_card.for_symbol(symbol) if latest_card else None
        if (
            latest_card is None
            or (latest_symbol.research_token() if latest_symbol else None) != source_token
        ):
            self._write(
                symbol=symbol,
                outcome=Outcome.INFO,
                summary=f"{symbol} 研究期间该币指令已替换，本次结论不覆盖新研究",
                output={"market_review": "superseded"},
            )
            return None
        # 从这里到落盘没有 await：并发的几场临时会、整轮的单币会在事件循环上一场一场并进
        # 最新卡，版本号连续、谁的结论都不丢
        card = latest_card
        expires = card.expires_at  # 临时会不改整轮的节奏：到期时间跟这一轮走
        if self._round_symbols is not None and self._round_expiry is not None:
            # 整轮的币已定、新卡还没出：这条结论要活到整轮新卡的到期时间，不按旧卡提前消失
            expires = max(expires, self._round_expiry)
        fresh = self._stamp(
            self._limit_size(clamp_to_risk(res.card_symbol, self._risk, self._stop)),
            now,
            expires,
            res.market_view or market,
        )
        in_force = card.for_symbol(symbol)
        if (
            in_force is not None
            and symbol not in self._held
            and now >= (in_force.expires_at or card.expires_at)
        ):
            in_force = None  # expired and unheld: _pool_view already hides it, as a round drops it
        new = self._flip_expiry(in_force, self._flip_gate(symbol, fresh, in_force, now))
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
        if self._round_symbols is not None:
            # 整轮还在开：它下一次出卡从自己的币表重建，这场结论和版本号要记进去
            self._round_symbols[symbol] = new
            self._round_event_merges += 1
            self._round_event_cost += res.cost_usd
        self._card = card
        self._notify_card()
        self._last_meeting[symbol] = now
        self._event_fail_until.pop(symbol, None)
        self._seen.add(symbol)
        self._level_broken.discard(symbol)
        self._news.discard(symbol)
        self._save_card()
        if self._journal is not None:
            self._journal.record(self._raw_view(card, fresh), [symbol], {symbol: res.setup})
        self._record_card(card, started, cost=0.0)  # 临时会没有总盘会这一份
        if nomination is not None:
            self._write(
                symbol=symbol,
                outcome=Outcome.ACTION,
                summary=f"{symbol} 市场提名复核完成，仅更新该币指令",
                output={
                    "market_review": "completed",
                    "source_market_view_id": new.source_market_view_id,
                    "nomination_market_view_id": nomination.market_view_id,
                },
            )
        self._save_market()
        return card.model_copy(update={"current_market": self.latest_market_view})

    @property
    def _cross_mode(self) -> bool:
        return self._events.key_level_trigger == "cross"

    @staticmethod
    def _level_token(cs: CardSymbol) -> str:
        """这条结论的标识：哪场会出的 + 关键位。沿用、只清信号、按风控夹过都不变，新开一场才变。"""
        levels = ",".join(f"{lv.kind.value}@{lv.price!r}" for lv in cs.key_levels)
        return f"{cs.meeting_id or cs.research_token()}|{levels}"

    def _mark_meeting_px(self, symbol: str, now: datetime) -> None:
        """开会开始时记下该币的价（最近 5 分钟内看到的），新结论的关键位按它定两侧。"""
        seen = self._last_px.get(symbol)
        if seen is not None and now - seen[1] <= LEVEL_PX_FRESH:
            self._meet_px[symbol] = seen[0]
        else:
            self._meet_px.pop(symbol, None)

    def _cross_pending(self, symbol: str, now: datetime) -> bool:
        """有没有还没开上会的穿越。等得比开会间隔（``min_refresh_gap``）和同币间隔都久就作废，
        免得被每日上限 / 定点开会压到隔夜再补开。"""
        at = self._level_cross.get(symbol)
        if at is None:
            return False
        if now - at <= max(self._min_gap, self._events.symbol_gap):
            return True
        del self._level_cross[symbol]
        queued = self._pending.get(symbol)
        if (
            queued is not None
            and queued[0] == "key_level_break"
            and symbol not in self._market_reviews
        ):
            self._pending.pop(symbol)
        self._save_market()
        self._write(
            symbol=symbol,
            outcome=Outcome.INFO,
            summary=f"{symbol} 关键位穿越等了一个开会间隔还没开上会，作废"[:200],
            output={"key_level": "expired", "crossed_at": at.isoformat()},
        )
        return False

    def _level_hit(self, cs: CardSymbol, px: float, now: datetime) -> bool:
        """cross 模式：这一刻有没有关键位从里侧穿到外侧。

        这条结论第一次看到时，按开会开始时的价（没有就用现在的价）定每个关键位在哪一侧，
        当时已在外侧的位不算，等价格回到里侧再穿过去。"""
        sym = cs.symbol
        self._last_px[sym] = (px, now)
        token = self._level_token(cs)
        rec = self._level_side.get(sym)
        old: dict[str, bool] = {}
        base_px = px
        if rec is None or rec[0] != token:
            fresh = True
            self._level_cross.pop(sym, None)  # 新结论：旧卡上的穿越作废
            base_px = self._meet_px.pop(sym, px)
        else:
            fresh, old = False, rec[1]
        new: dict[str, bool] = {}
        crossed: list[float] = []
        beyond_at_card: list[float] = []
        for lv in cs.key_levels:
            if lv.kind not in (LevelKind.RESISTANCE, LevelKind.SUPPORT):
                continue
            key = f"{lv.kind.value}@{lv.price!r}"
            out = self._broken(lv, px)
            new[key] = out
            was = self._broken(lv, base_px) if fresh else old.get(key, out)
            if fresh and was:
                beyond_at_card.append(lv.price)
            elif out and not was:
                crossed.append(lv.price)
        if fresh or new != old:
            self._level_side[sym] = (token, new)
            self._save_market()
        if beyond_at_card:
            self._write(
                symbol=sym,
                outcome=Outcome.INFO,
                summary=f"{sym} 出卡时价格已在关键位外侧，等回到里侧再穿过才算突破"[:200],
                output={
                    "key_level": "suppressed_already_beyond",
                    "price": base_px,
                    "levels": beyond_at_card,
                },
            )
        if not crossed:
            return False
        self._level_cross[sym] = now
        self._save_market()
        self._write(
            symbol=sym,
            outcome=Outcome.INFO,
            summary=f"{sym} 价格穿过关键位 {crossed}"[:200],
            output={"key_level": "crossed", "price": px, "levels": crossed},
        )
        return True

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
        self.round_cost_usd = 0.0
        generation = self._new_market_generation()
        self._round_reference = None
        self._round_desk_owned = False
        started = time.monotonic()
        self._loop = asyncio.get_running_loop()
        self._rl_until, self._rl_reason = None, ""  # 重开时清掉上一次的限流暂停
        self._pick_cost = 0.0  # 本轮总盘会花费；出卡失败时记在错误记录上，面板净盈亏才不漏 AI 费
        self._tier_of = {e.symbol: e.tier for e in pool.in_pool if e.tier}
        self._always = {e.symbol for e in pool.in_pool if e.always_candidate}
        self._expand_mid = self._tiers.enabled and self._mid_day != self._day(now)
        candidates = self._eligible(pool, now, expand=self._expand_mid)
        if not candidates:
            raise CardRefreshError("候选池为空，不出卡")
        if self._budget_exhausted(now):
            raise CardRefreshError("军师团当日花费已达上限，今天不再开会")
        brief_text = ""
        brief_provenance: dict[str, Any] = {"status": "not_configured"}
        if self._brief is not None:
            try:
                brief_text, brief_provenance = await self._market_brief()  # 同份文本/来源快照
            except Exception as exc:
                self._write(outcome=Outcome.ERROR, summary=f"市场简报失败，会议继续：{exc}"[:200])
        self._progress = "选币中"
        beat = asyncio.create_task(self._beat_while_meeting())
        self._round_on, self._round_meeting, self._round_symbols = True, None, None
        self._round_event_merges, self._round_event_cost = 0, 0.0
        try:
            card = await self._meet(
                pool, candidates, reason, now, generation, brief_text, brief_provenance
            )
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
            self._round_on, self._round_meeting, self._round_symbols = False, None, None
            self._round_expiry = None
        self._fails, self._retry_at, self._last_error = 0, None, None
        self._restored = False
        self._card = card
        self._notify_card()
        if reason != "regime_flip":
            self._last_refresh = now
            self._scheduled_due = card.expires_at - self._lead
        # 大盘台接管：翻转参照只由大盘台的发布来设（CONFLICTS #2）
        if not self._round_desk_owned and generation >= self._reference_generation:
            self.regime_reference = self._round_reference
            self._reference_generation = generation
            self._pending_reference_view = (
                self.latest_market_view if self.regime_reference is None else None
            )
        self._seen = {e.symbol for e in candidates}
        self._news.clear()
        self._level_broken.clear()
        self._save_card()
        self._save_market()
        self._record_card(card, started, cost=self._pick_cost)
        return card.model_copy(update={"current_market": self.latest_market_view})

    def set_market_snapshot(
        self, source: Callable[[SymbolPool, datetime], Awaitable[SymbolPool]]
    ) -> None:
        """只刷新原池成员统计，不重新选择或发布候选池。"""
        self._market_snapshot = source

    def _new_market_generation(self) -> int:
        self._market_generation += 1
        return self._market_generation

    def new_market_generation(self) -> int:
        """大盘台一轮开始时取代数（和总盘会 / 独立刷新共用一套），发布时据此判断是否已被取代。"""
        return self._new_market_generation()

    def set_market_desk(self, desk: MarketDeskOwner | None) -> None:
        self._desk = desk

    def market_intra(self, now: datetime, view: MarketView | None) -> str | None:
        """大盘台观点后面接的「4 小时之内」（``[market_desk] publish_intra``）：只接在大盘台发布的
        观点后面，读大盘台最近一轮。没开、观点不是大盘台的、读不到都返回 None。"""
        return self._desk_text("intra_text", now, view)

    def keeper_intra(self, now: datetime, view: MarketView | None) -> str | None:
        """同一段给管家复查快照（``[market_desk] keeper_intra``，和 publish_intra 分开）。"""
        return self._desk_text("keeper_intra_text", now, view)

    def _desk_text(self, name: str, now: datetime, view: MarketView | None) -> str | None:
        if view is None or view.source != DESK_SOURCE or self._desk is None:
            return None
        fn = getattr(self._desk, name, None)
        if not callable(fn):
            return None
        try:
            text = fn(now)
        except Exception:
            logger.exception("读大盘台短线现状失败，这次不带")
            return None
        return text if isinstance(text, str) and text else None

    def _flash_previous(self) -> BattleCard | None:
        """总盘会（Flash）看到的「上一轮」：大盘台接管时卡上的大盘是大盘台的，换回 Flash 自己上一轮
        的判断，免得 Flash 被大盘台锚定（CONFLICTS #14）。"""
        card = self._card
        if card is None or not self._desk_owns(self._clock()):
            return card
        return card.model_copy(update={"regime": self._flash_regime or "none"})

    def _desk_owns(self, now: datetime) -> bool:
        if self._desk is None:
            return False
        try:
            return bool(self._desk.owns_market(now))
        except Exception:
            logger.exception("查大盘台是否接管失败，按未接管处理")
            return False

    def publish_desk_view(
        self,
        *,
        regime: str,
        direction: Direction,
        reason: str,
        pool: SymbolPool,
        generation: int,
        desk_id: str,
        probs: Mapping[str, float],
        republish: bool = False,
    ) -> MarketView | None:
        """大盘台发布：走和总盘会 / 独立刷新同一条路（共用版本号、翻转参照、通知管家复核）。
        ``republish``：同方向按 ``republish_after_min`` 重发，只换话和数字，不唤醒管家复核。
        比它晚开始的发布已经生效时不覆盖，返回 None。同步执行（不让出事件循环），就是「只在
        发布时持锁」。仍有效的市场复核提名迁到新版本，过时的撤掉。"""
        if generation < self._market_published_generation:
            self._write(
                outcome=Outcome.INFO,
                summary="大盘台结果已被较新的大盘观点取代，不发布",
                output={DESK_SOURCE: {"desk_id": desk_id, "accepted": False}},
            )
            return None
        view = self._publish_view(
            regime,
            direction,
            DESK_SOURCE,
            reason,
            pool.pool_id,
            pool.ts,
            generation,
            transition=True,
            notify=not republish,
        )
        self._migrate_market_reviews(view)
        self._write(
            outcome=Outcome.ACTION,
            summary=f"大盘台发布大盘 v{view.version}：{view.regime}"[:200],
            input={"refresh_reason": reason, "scope": DESK_SOURCE},
            output={
                DESK_SOURCE: {"desk_id": desk_id, "accepted": True, "probs_4h": dict(probs)},
                "market_view": view.model_dump(mode="json"),
            },
        )
        return view

    def _migrate_market_reviews(self, view: MarketView) -> None:
        """大盘台发了新版本：已排队、该币研究没换、没过期的复核提名改挂到新版本，其余撤掉
        （CONFLICTS #6）。接管期间总盘会不再产生新提名。"""
        now = self._clock()
        for sym, queued in list(self._market_reviews.items()):
            current = self._card.for_symbol(sym) if self._card else None
            if (
                current is None
                or current.research_token() != queued.candidate.research_token
                or now > queued.expires_at
            ):
                self._drop_market_review(sym, "大盘台发布新版本时，该提名已过时")
                continue
            self._market_reviews[sym] = queued.model_copy(
                update={"market_view_id": view.market_view_id}
            )
            self._write(
                symbol=sym,
                outcome=Outcome.INFO,
                summary=f"{sym} 市场复核提名迁到大盘台 v{view.version}",
                output={
                    "market_review": "migrated",
                    "from_market_view_id": queued.market_view_id,
                    "market_view_id": view.market_view_id,
                },
            )
        self._save_market()

    def _reference_for(self, view: MarketView) -> RegimeReference | None:
        try:
            px = self._regime_price() if self._regime_price is not None else None
            if px is not None:
                return RegimeReference(
                    ts=self._clock(),
                    direction=view.regime_direction,
                    btc_px=px,
                    regime_text=view.regime,
                )
        except Exception:
            logger.exception("市场观点缺少可用 BTC 参照，暂停翻转检测")
        return None

    def ensure_regime_reference(self) -> None:
        """方向变化时缺少新鲜 BTC，就等下一次有效行情后原子建立新参照。"""
        if self._pending_reference_view is not None:
            ref = self._reference_for(self._pending_reference_view)
            if ref is not None:
                self.regime_reference = ref
                self._pending_reference_view = None
                self._save_market()

    async def _market_brief(self) -> tuple[str, dict[str, Any]]:
        if self._brief is None:
            return "", {"status": "not_configured"}
        richer = getattr(self._brief, "refresh_with_provenance", None)
        if callable(richer):
            text, metadata = await richer()
            return str(text), dict(metadata)
        return await self._brief.refresh(), {"status": "unknown_legacy_brief"}

    def _market_audit(
        self,
        pool: SymbolPool,
        scope: str,
        reason: str,
        now: datetime,
        brief_provenance: dict[str, Any] | None,
    ) -> MarketAudit:
        try:
            audit = MarketAudit(
                self._market_log,
                {
                    "role": "market",
                    "scope": scope,
                    "refresh_reason": reason,
                    "round_id": round_id(now),
                    "workflow_started_at": now.isoformat(),
                    "pool_id": pool.pool_id,
                    "pool_requested_at": pool.ts.isoformat(),
                    "pool_n": len(pool.in_pool),
                    "pool_source_metadata": {
                        e.symbol: pool.source_metadata.get(e.symbol) for e in pool.in_pool
                    },
                    "brief_provenance": brief_provenance or {"status": "unknown"},
                    "previous_market_view_id": self.latest_market_view.market_view_id
                    if self.latest_market_view
                    else None,
                    "reference_at": self.regime_reference.ts.isoformat()
                    if self.regime_reference
                    else None,
                },
            )
            audit.emit("request_accepted")
            return audit
        except Exception:
            return MarketAudit(None, {})  # 诊断准备失败仍走原路径。

    def _publish_market(
        self,
        picked: PickResult,
        pool: SymbolPool,
        reason: str,
        generation: int,
        *,
        transition: bool,
    ) -> MarketView:
        return self._publish_view(
            picked.regime,
            picked.regime_direction,
            "market_only" if transition else "scheduled_market",
            reason,
            pool.pool_id,
            pool.ts,
            generation,
            transition=transition,
        )

    def _publish_view(
        self,
        regime: str,
        direction: Direction,
        source: str,
        reason: str,
        pool_id: str,
        source_ts: datetime,
        generation: int,
        *,
        transition: bool,
        notify: bool = True,
    ) -> MarketView:
        view = MarketView(
            market_view_id=uuid.uuid4().hex,
            version=(self.latest_market_view.version + 1) if self.latest_market_view else 1,
            observed_at=self._clock(),
            regime=regime,
            regime_direction=direction,
            source=source,
            refresh_reason=reason,
            pool_id=pool_id,
            source_ts=source_ts,
        )
        self._view_dirs[view.market_view_id] = direction
        while len(self._view_dirs) > VIEW_DIRS_MAX:
            del self._view_dirs[next(iter(self._view_dirs))]
        if generation < self._market_published_generation:
            return view  # 仍是这一场币研究的历史来源，但不能覆盖更晚的当前观点
        self.latest_market_view = view
        self.last_regime_direction = view.regime_direction
        self._market_published_generation = generation
        if transition:
            self._reference_generation = generation
        if transition and (
            self.regime_reference is None
            or self.regime_reference.direction != view.regime_direction
        ):
            self.regime_reference = self._reference_for(view)
            self._reference_generation = generation
            self._pending_reference_view = view if self.regime_reference is None else None
        self._save_market()
        if notify and self._on_market is not None:
            try:
                self._on_market(view)
            except Exception:
                logger.exception("通知最新市场观点失败，持仓巡检会继续补查")
        return view

    async def refresh_market(
        self,
        pool: SymbolPool,
        reason: str = "regime_flip",
        *,
        valid: Callable[[], bool] | None = None,
    ) -> MarketView:
        """独立更新市场背景；不选币、不分析币、不出卡，不重置整轮节奏。"""
        waiting = self._market_lock.locked()
        async with self._market_lock:
            if waiting:
                if self._market_error is None and self.latest_market_view is not None:
                    return self.latest_market_view
                raise CardRefreshError(self._market_error or "市场刷新未完成")
            now = self._clock()
            if why := self.regime_flip_blocked(now):
                raise CardRefreshError(f"市场刷新暂停：{why}")
            contextual_refresh = getattr(self._engine, "refresh_market_with_reviews", None)
            refresh = getattr(self._engine, "refresh_market", None)
            if not callable(refresh):
                raise CardRefreshError("会议引擎不支持独立市场刷新")
            self._loop = asyncio.get_running_loop()
            generation = self._new_market_generation()
            self._market_error = None
            started = time.monotonic()
            self.market_refresh_cost_usd = 0.0
            self.market_refresh_discarded = False
            est = self._estimate(self._pick_costs)
            reserve_day = self._day(now)
            self._charge(est, round_cost=False)
            cost = est
            model_started = False
            model_cost: float | None = None
            audit = self._market_audit(pool, "market_only", reason, now, None)
            token = CURRENT_AUDIT.set(audit)
            try:
                if self._market_snapshot is not None:
                    snapshot = await asyncio.wait_for(
                        self._market_snapshot(pool, now), self._symbol_timeout_s
                    )
                    if {e.symbol for e in snapshot.in_pool} != {e.symbol for e in pool.in_pool}:
                        raise CardRefreshError("市场统计快照改变了原池成员，拒绝发布")
                    pool = snapshot
                age = self._clock() - pool.ts
                if age < timedelta(0) or age > MARKET_SNAPSHOT_FRESH:
                    raise CardRefreshError("市场统计超过 2 分钟或时间异常，保留上一份观点")
                if valid is not None and not valid():
                    raise CardRefreshError("市场刷新条件已变化，保留上一份观点")
                brief_text, brief_provenance = await self._market_brief()
                audit.context.update(
                    pool_requested_at=pool.ts.isoformat(),
                    pool_source_metadata={
                        e.symbol: pool.source_metadata.get(e.symbol) for e in pool.in_pool
                    },
                    brief_provenance=brief_provenance,
                )
                card_context = (
                    self.market_review_context(pool) if self.accepts_event("market_review") else []
                )
                model_started = True
                meeting = (
                    contextual_refresh(
                        pool.in_pool,
                        self.latest_market_view,
                        brief_text=brief_text,
                        card_context=card_context,
                    )
                    if callable(contextual_refresh)
                    else refresh(pool.in_pool, self.latest_market_view, brief_text=brief_text)
                )
                picked: PickResult = await asyncio.wait_for(
                    meeting,
                    self._symbol_timeout_s,
                )
                model_cost = picked.cost_usd
                if valid is not None and not valid():
                    raise MeetingError("市场刷新期间设置或参照已变化，结果不发布", model_cost)
                if picked.errors:
                    raise MeetingError(
                        "市场观点校验失败：" + "；".join(picked.errors), picked.cost_usd
                    )
                # 先验证，失败不得刷新时间、版本或方向。
                MarketView(
                    market_view_id="validate",
                    version=1,
                    observed_at=self._clock(),
                    regime=picked.regime,
                    regime_direction=picked.regime_direction,
                )
                cost = picked.cost_usd
            except BaseException as exc:
                audit.emit(
                    "workflow_failed",
                    error_type=type(exc).__name__,
                    model_call_observed=audit.attempt > 0,
                )
                cost = (
                    model_cost
                    if model_cost is not None
                    else self._failure_cost(exc, "market_only")
                    if model_started
                    else 0.0
                )
                self._market_error = self._why(exc)
                self._market_retry_at = (
                    self._clock() + self._backoff[min(self._market_fails, len(self._backoff) - 1)]
                )
                self._market_fails += 1
                self._write(
                    outcome=Outcome.ERROR,
                    summary=f"大盘独立更新失败，保留上一份观点：{self._market_error}"[:200],
                    input={"refresh_reason": reason, "scope": "market_only"},
                    output={"error": self._market_error},
                    cost_usd=cost,
                    latency_ms=(time.monotonic() - started) * 1000,
                )
                raise
            finally:
                CURRENT_AUDIT.reset(token)
                # 预留跨日不能从新一天退款；新一天计完整实际费用。
                charge = cost - est if self._day(self._clock()) == reserve_day else cost
                self._charge(charge, round_cost=False)
                self.market_refresh_cost_usd = cost
                self._bump(
                    ok=self._market_error is None,
                    cost=0.0,
                    ms=(time.monotonic() - started) * 1000,
                    round_cost=False,
                )
                self._save_market()
            self._market_fails, self._market_retry_at = 0, None
            self._pick_costs.append(cost)
            if generation < self._market_published_generation:
                self.market_refresh_discarded = True
                audit.emit("superseded", published_at=None, model_call_observed=audit.attempt > 0)
                self._write(
                    outcome=Outcome.INFO,
                    summary="独立市场结果已被较新观点取代，不覆盖当前大盘",
                    input={"refresh_reason": reason, "scope": "market_only"},
                    output={"discarded_regime": picked.regime, "accepted": False},
                    cost_usd=cost,
                    latency_ms=(time.monotonic() - started) * 1000,
                )
                self._save_market()
                assert self.latest_market_view is not None
                return self.latest_market_view
            view = self._publish_market(picked, pool, reason, generation, transition=True)
            audit.emit(
                "published",
                published_at=view.observed_at.isoformat(),
                market_view=view.model_dump(mode="json"),
                model_call_observed=audit.attempt > 0,
            )
            if picked.reviews_valid:
                self.enqueue_market_reviews(picked.review_candidates, pool, card_context, view)
            else:
                self._write(
                    outcome=Outcome.INFO,
                    summary="本次市场复核提名不可用：保留位置，市场原因暂停等待新版确认",
                    output={"reviews_valid": False, "market_view_id": view.market_view_id},
                )
            self._write(
                outcome=Outcome.ACTION,
                summary=f"独立大盘更新 v{view.version}：{view.regime}"[:200],
                input={"refresh_reason": reason, "scope": "market_only"},
                output={
                    "market_view": view.model_dump(mode="json"),
                    "reviews_valid": picked.reviews_valid,
                    "review_candidates": [
                        c.model_dump(mode="json") for c in picked.review_candidates
                    ],
                },
                cost_usd=cost,
                latency_ms=(time.monotonic() - started) * 1000,
            )
            self._record_market(
                pool,
                picked,
                [],
                0,
                self._card.version if self._card else 0,
                time.monotonic() - started,
                view.observed_at,
                reason,
                view,
                audit.trace_id,
                True,
            )
            self._save_market()
            return view

    def _save_market(self) -> None:
        if self._market_path is None:
            return
        data = {
            "latest_market_view": self.latest_market_view.model_dump(mode="json")
            if self.latest_market_view
            else None,
            "generation": self._market_generation,
            "published_generation": self._market_published_generation,
            "reference_generation": self._reference_generation,
            "regime_reference": self.regime_reference.model_dump(mode="json")
            if self.regime_reference
            else None,
            "pending_reference_view": self._pending_reference_view.model_dump(mode="json")
            if self._pending_reference_view
            else None,
            "last_regime_flip_at": self.last_regime_flip_at.isoformat()
            if self.last_regime_flip_at
            else None,
            "last_market_success_at": self.last_market_success_at.isoformat()
            if self.last_market_success_at
            else None,
            "market_reviews": {
                key: q.model_dump(mode="json") for key, q in self._market_reviews.items()
            },
            "event_day": self._event_day,
            "event_count": self._event_count,
            "event_last": {key: at.isoformat() for key, at in self._event_last.items()},
            "level_side": {
                sym: {"token": token, "sides": sides}
                for sym, (token, sides) in self._level_side.items()
            },
            "level_cross": {sym: at.isoformat() for sym, at in self._level_cross.items()},
            "market_error": self._market_error,
            "market_fails": self._market_fails,
            "market_retry_at": self._market_retry_at.isoformat() if self._market_retry_at else None,
            # 旧代码按键读、不认识的键忽略：回滚安全
            "flash_regime": self._flash_regime,
            "view_dirs": dict(self._view_dirs),
        }
        tmp = self._market_path.with_suffix(".tmp")
        try:
            self._market_path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self._market_path)
        except Exception:
            logger.exception("独立市场观点存盘失败")
            with contextlib.suppress(OSError):
                tmp.unlink()

    def _restore_market(self) -> None:
        if self._market_path is None:
            return
        try:
            data = json.loads(self._market_path.read_text(encoding="utf-8"))
            view = (
                MarketView.model_validate(data["latest_market_view"])
                if data.get("latest_market_view")
                else None
            )
            ref = (
                RegimeReference.model_validate(data["regime_reference"])
                if data.get("regime_reference")
                else None
            )
            pending = (
                MarketView.model_validate(data["pending_reference_view"])
                if data.get("pending_reference_view")
                else None
            )
            times: dict[str, datetime | None] = {}
            for key in ("last_regime_flip_at", "market_retry_at"):
                times[key] = datetime.fromisoformat(data[key]) if data.get(key) else None
                at = times[key]
                if at is not None and at.tzinfo is None:
                    raise ValueError("市场状态时间必须带时区")
            generation = int(data.get("generation", 0))
            published = int(data.get("published_generation", 0))
            reference_generation = int(data.get("reference_generation", 0))
            fails = int(data.get("market_fails", 0))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return
        self.latest_market_view, self.regime_reference = view, ref
        self._pending_reference_view = pending
        self._market_generation, self._market_published_generation = generation, published
        self._reference_generation = reference_generation
        self.last_regime_flip_at = times["last_regime_flip_at"]
        self._market_retry_at, self._market_fails = times["market_retry_at"], fails
        self._market_error = data.get("market_error")
        flash = data.get("flash_regime")
        self._flash_regime = flash if isinstance(flash, str) else ""
        dirs = data.get("view_dirs")
        if isinstance(dirs, dict):  # older files have none: those entries stay unknown
            known = [(k, v) for k, v in dirs.items() if isinstance(k, str) and v in DIRECTIONS]
            self._view_dirs = dict(known[-VIEW_DIRS_MAX:])
        if view is not None:
            self.last_regime_direction = view.regime_direction
        try:
            self._event_day = str(data.get("event_day", ""))
            self._event_count = int(data.get("event_count", 0))
            self._event_last = {
                key: datetime.fromisoformat(value)
                for key, value in data.get("event_last", {}).items()
            }
            for key, raw in data.get("market_reviews", {}).items():
                queued = QueuedMarketReview.model_validate(raw)
                self._market_reviews[key] = queued
                self._pending[key] = (queued.reasons[0], queued.first_seen)
        except (ValueError, TypeError, AttributeError):
            logger.warning("市场复核排队状态损坏，保留卡与市场，等待新提名")
            self._market_reviews.clear()
        try:  # 旧文件没有这两项：当作出卡后还没看过价格
            self._level_side = {
                str(sym): (
                    str(rec["token"]),
                    {str(k): bool(v) for k, v in rec["sides"].items()},
                )
                for sym, rec in data.get("level_side", {}).items()
            }
            self._level_cross = {
                str(sym): datetime.fromisoformat(at)
                for sym, at in data.get("level_cross", {}).items()
            }
            if any(at.tzinfo is None for at in self._level_cross.values()):
                raise ValueError("穿越时间必须带时区")
        except (ValueError, TypeError, AttributeError, KeyError):
            logger.warning("关键位穿越状态损坏，按出卡后第一次看到的价格重记")
            self._level_side, self._level_cross = {}, {}

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
                or e.always_candidate  # 每轮进总盘会：不管落在哪档
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
            if e.tier == "mid" and e.symbol not in self._held and e.symbol not in self._always:
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
            and cs.symbol not in self._always  # 每轮进总盘会的币仓位按头部算，不被中间档封顶
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
        pool: Sequence[SymbolPoolEntry],
        candidates: list[SymbolPoolEntry],
        forced: list[SymbolPoolEntry],
        free: list[SymbolPoolEntry],
        room: int,
        direct: Sequence[SymbolPoolEntry] = (),
    ) -> MeetingLive | None:
        """总盘会（大盘基调 + 挑币）的实况，写进 meeting_live/_MARKET.json，格式同单币会议。"""
        if self._live_dir is None:
            return None
        live = MeetingLive(self._live_dir, MARKET_LIVE)
        hold = str(self._log_meta.get("hold", "hold"))
        table = (
            market_table.breadth_line(pool or free)
            + "\n"
            + "\n".join(market_table.format_row(e, hold) for e in free)
        )
        prev = f"上一张卡的大盘判断：{self._card.regime}\n" if self._card is not None else ""
        also = f"；{DIRECT_MEET_REASON} {len(direct)} 个（不占名额）" if direct else ""
        live.step(
            "input",
            "input",
            f"{prev}候选池 {len(candidates)} 个；固定 / 持仓强制开会 {len(forced)} 个"
            f"（不占名额）{also}；军师最多再挑 {room} 个。\n{table}",
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
        direct: Sequence[SymbolPoolEntry] = (),
    ) -> None:
        by_pick = {p.symbol: p.reason for p in picked.picks}
        out = [f"大盘判断：{picked.regime}"]
        if picked.decisions:
            out.append("每个候选的结论：")
            out += [
                f"{d.symbol}：{'挑' if d.pick else '不挑'}：{d.reason}" for d in picked.decisions
            ]
        out.append("挑中：")
        out += [
            f"{e.symbol}：{DIRECT_MEET_REASON if e in direct else by_pick.get(e.symbol, '')}"
            for e in chosen
            if e not in forced
        ]
        live.step("pick", "market", "\n".join(out))
        if "rolling_60m" in picked.table or "_closedHour[" in picked.table:
            # 会前那一步是按现状格式先显示的；开了新标签 / 滚动涨跌家数时，补上模型实际收到的
            live.step("input", "input", f"模型实际收到的池子概况和候选表：\n{picked.table}")
        keep = {e.symbol for e in chosen}
        said = {r.symbol: r.reason for r in picked.rejections}
        why: list[str] = []
        why += [f"{e.symbol}：固定 / 持仓强制开会" for e in forced]
        why += [f"{e.symbol}：{DIRECT_MEET_REASON}，不经挑选" for e in direct]
        rank = {e.symbol: i + 1 for i, e in enumerate(sorted(candidates, key=lambda e: -e.score))}
        for e in candidates:
            if e.symbol in keep:
                continue
            if e.symbol in by_pick:
                why.append(f"{e.symbol}：总盘会挑了，但这轮上会名额已满（或中间档当天名额已满）")
            else:
                reason = said.get(e.symbol) or "AI 没写理由"
                why.append(
                    f"{e.symbol}：总盘会这轮没挑中（候选池第 {rank[e.symbol]} 名，"
                    f"选币分 {e.score:.2f}）：{reason}"
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

    def _record_market(
        self,
        pool: SymbolPool,
        picked: PickResult,
        chosen: list[SymbolPoolEntry],
        room: int,
        card_version: int,
        elapsed_s: float,
        now: datetime,
        reason: str = "scheduled",
        market_view: MarketView | None = None,
        trace_id: str | None = None,
        publish_accepted: bool | None = None,
        *,
        record_only: MarketView | None = None,
        direct: Sequence[SymbolPoolEntry] = (),
    ) -> None:
        """每轮总盘会写一行记录（data/market_meeting/）：池子里每个币的分数和数字、候选的结论。
        ``record_only``：大盘台接管时 Flash 自己的大盘判断（只记录、没发布）。"""
        if self._market_log is None:
            return
        decided = {d.symbol: (d.pick, d.reason) for d in picked.decisions}
        decided.update({e.symbol: (True, DIRECT_MEET_REASON) for e in direct})
        try:
            self._market_log.write_round(
                {
                    "round_id": round_id(now),
                    "ts": now.isoformat(),
                    "trace_id": trace_id,
                    "publish_accepted": publish_accepted,
                    "publish_status": "record_only"
                    if record_only is not None
                    else "published"
                    if publish_accepted is True
                    else "superseded"
                    if publish_accepted is False
                    else "unknown",
                    "record_only_view": record_only.model_dump(mode="json")
                    if record_only is not None
                    else None,
                    "published_at": market_view.observed_at.isoformat() if market_view else None,
                    "market_view": market_view.model_dump(mode="json") if market_view else None,
                    "pool_requested_at": pool.ts.isoformat(),
                    "pool_id": pool.pool_id,
                    "card_version": card_version,
                    **self._log_meta,
                    "max_n": max(room, 0),
                    "regime": picked.regime,
                    "regime_direction": picked.regime_direction,
                    "reviews_valid": picked.reviews_valid,
                    "review_candidates": [
                        c.model_dump(mode="json") for c in picked.review_candidates
                    ],
                    "refresh_reason": reason,
                    "breadth": picked.breadth,
                    "picked": [p.symbol for p in picked.picks],
                    "direct_meet": [e.symbol for e in direct],
                    "chosen": [e.symbol for e in chosen],
                    "cost_usd": picked.cost_usd,
                    "elapsed_s": round(elapsed_s, 1),
                    "retried": picked.retried,
                    "errors": picked.errors,
                    "pool": pool_rows(pool.in_pool, decided),
                }
            )
        except Exception:
            logger.exception("写总盘会记录失败，不影响开会")

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
                reason = said.get(e.symbol) or "AI 没写理由"
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
        if progress := self._progress_text():
            self._heartbeat(Health.OK, f"开会中：{progress}", None)
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
            self._heartbeat(Health.OK, f"开会中：{self._progress_text()}", None)
            await asyncio.sleep(self._beat_s)

    async def _meet(
        self,
        pool: SymbolPool,
        candidates: list[SymbolPoolEntry],
        reason: str,
        now: datetime,
        generation: int,
        brief_text: str,
        brief_provenance: dict[str, Any] | None = None,
    ) -> BattleCard:
        by_symbol = {e.symbol: e for e in candidates}
        pinned = [e for e in candidates if e.pinned][: self._pick_max]
        # 有持仓的币强制入选（和置顶一样不占军师的挑选名额），保证它们本轮有新结论、卡上不缺
        held_in = [e for e in candidates if e.symbol in self._held and e not in pinned]
        pinned = [*pinned, *held_in]
        room = max(self._pick_max - len(pinned), 0)
        # 每轮直接开会（设置）：不经挑选、不占名额；执行交易所上不在交易的币不白开
        direct = [
            e
            for e in candidates
            if self._tiers.always_meet
            and e.always_candidate
            and e not in pinned
            and e.symbol not in self._untradeable
        ]
        free = [e for e in candidates if e not in pinned and e not in direct]
        self._direct_n = len(direct)
        pick_started = time.monotonic()
        est = self._estimate(self._pick_costs)
        self._charge(est)  # 开会前先按估算预记并落盘：开到一半被强退，这笔钱也不会漏
        live = self._market_live(pool.in_pool, candidates, pinned, free, max(room, 0), direct)
        card_context = (
            self.market_review_context(pool) if self.accepts_event("market_review") else []
        )
        audit = self._market_audit(pool, "scheduled_market", reason, now, brief_provenance)
        token = CURRENT_AUDIT.set(audit)
        previous = self._flash_previous()
        try:
            review_pick = getattr(self._engine, "pick_with_reviews", None)
            contextual_pick = getattr(self._engine, "pick_with_context", None)
            if callable(review_pick):
                picked = await review_pick(
                    free,
                    max(room, 0),
                    previous,
                    pool.in_pool,
                    brief_text=brief_text,
                    card_context=card_context,
                )
            elif callable(contextual_pick):
                picked = await contextual_pick(
                    free, max(room, 0), previous, pool.in_pool, brief_text=brief_text
                )
            else:
                picked = await self._engine.pick(free, max(room, 0), previous, pool=pool.in_pool)
        except Exception as exc:
            audit.emit(
                "workflow_failed",
                error_type=type(exc).__name__,
                model_call_observed=audit.attempt > 0,
            )
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
        finally:
            CURRENT_AUDIT.reset(token)
        self._flash_regime = picked.regime
        desk_owned = self._round_desk_owned = self._desk_owns(self._clock())
        flash_view: MarketView | None = None
        if desk_owned and self.latest_market_view is not None:
            # 大盘台接管：Flash 的大盘判断只记录（不发布、不改翻转参照、提名不进队列），
            # 本轮各币会议看大盘台当前生效的观点
            accepted = False
            flash_view = MarketView(
                market_view_id=uuid.uuid4().hex,
                version=self.latest_market_view.version,
                observed_at=self._clock(),
                regime=picked.regime,
                regime_direction=picked.regime_direction,
                source="scheduled_market",
                refresh_reason=reason,
                pool_id=pool.pool_id,
                source_ts=pool.ts,
            )
            market_view = self.latest_market_view
            self._save_market()
        else:
            desk_owned = self._round_desk_owned = False
            accepted = generation >= self._market_published_generation
            market_view = self._publish_market(picked, pool, reason, generation, transition=False)
        audit.emit(
            "record_only" if desk_owned else "published" if accepted else "superseded",
            published_at=market_view.observed_at.isoformat() if accepted else None,
            market_view=(flash_view or market_view).model_dump(mode="json"),
            model_call_observed=audit.attempt > 0,
        )
        # 总盘会结束与单币会全部结束可能相差十几分钟：价和方向必须取同一时刻。
        if self._regime_price is not None and not desk_owned:
            try:
                px = self._regime_price()
                if px is not None:
                    self._round_reference = RegimeReference(
                        ts=self._clock(),
                        direction=picked.regime_direction,
                        btc_px=px,
                        regime_text=picked.regime,
                    )
            except Exception:
                logger.exception("取总盘会 BTC 参照失败；本轮不建立翻转参照")
        self._charge(
            picked.cost_usd - est
        )  # 选币的花费一出结果就改成实际值，哪怕后面所有标的都失败
        self._pick_costs.append(picked.cost_usd)
        if picked.errors:  # 格式问题只记录，挑中的币照样用
            self._write(
                outcome=Outcome.ERROR,
                summary=f"总盘会回答有问题（挑中的币照用）：{'；'.join(picked.errors)}"[:200],
            )
        chosen = [*pinned, *direct]
        cap = self._pick_max + len(direct)
        for p in picked.picks:
            e = by_symbol.get(p.symbol)
            if e is None or e.pinned or e in direct or len(chosen) >= cap:
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
        if reason in ("manual", "regime_flip"):  # 整轮重判：不沿用同币最小间隔内的旧结论
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
        # 已派出或正在开临时会的币本轮不再开：让那场开完，结论照样并进本轮的卡（见 _refresh_symbol）
        busy = self._symbol_inflight | self._event_claimed
        if busy & {e.symbol for e in to_meet}:
            self._write(
                outcome=Outcome.INFO,
                summary=f"本轮不重开正在开临时会的币：{sorted(busy & {e.symbol for e in to_meet})}"[
                    :200
                ],
            )
            to_meet = [e for e in to_meet if e.symbol not in busy]
        if desk_owned and picked.review_candidates:
            self._write(
                outcome=Outcome.INFO,
                summary="大盘台接管中：总盘会的市场复核提名只记录，不进队列",
                output={
                    "market_review": "record_only",
                    "review_candidates": [
                        c.model_dump(mode="json") for c in picked.review_candidates
                    ],
                },
            )
        if (
            not desk_owned
            and picked.reviews_valid
            and self.latest_market_view is not None
            and self.latest_market_view.market_view_id == market_view.market_view_id
        ):
            planned = {entry.symbol for entry in to_meet}
            self.enqueue_market_reviews(
                [
                    candidate
                    for candidate in picked.review_candidates
                    if candidate.symbol not in planned
                ],
                pool,
                card_context,
                market_view,
            )
        if live is not None:
            self._market_done(live, picked, chosen, candidates, pinned, reused, now, direct)
        else:
            self._market_summary(
                picked, chosen, candidates, {p.symbol: p.reason for p in picked.picks}
            )
        prev = self._card
        same_day = prev is not None and prev.card_id == self._card_id(now)
        base_version = prev.version if prev is not None and same_day else 0
        self._record_market(
            pool,
            picked,
            chosen,
            room,
            base_version + 1,
            time.monotonic() - pick_started,
            now,
            reason,
            market_view if accepted else None,
            audit.trace_id,
            None if desk_owned else accepted,
            record_only=flash_view,
            direct=direct,
        )
        # 本轮各币会议看的大盘：平时是总盘会刚发布的，大盘台接管时是大盘台当前生效的
        round_regime, round_direction = market_view.regime, market_view.regime_direction
        if not desk_owned:
            round_regime, round_direction = picked.regime, picked.regime_direction
        # 指令池：上一张卡里还没到期的币原样留着（这轮没开会的币不动），开完哪个换哪个；
        # 持仓币的指令到期也不消失，只清空信号。池外（已被筛掉）的币不留
        in_pool = {e.symbol for e in pool.in_pool}
        round_expiry = now + self._ttl_now()
        symbols: dict[str, CardSymbol] = {}
        for old in prev.symbols if prev is not None else []:
            exp = old.expires_at or (prev.expires_at if prev is not None else now)
            if old.symbol not in in_pool and old.symbol not in self._held:
                continue
            if now < exp:
                # event conclusions carry the expiry of the card in force when they merged; a
                # flipped one carries a full TTL from its own meeting (_flip_expiry)
                kept = clamp_to_risk(old, self._risk, self._stop)
                symbols[old.symbol] = self._extend_reused(kept, round_expiry)
            elif old.symbol in self._held:
                symbols[old.symbol] = self._watch_only(old)
        symbols.update({k: self._extend_reused(v, round_expiry) for k, v in reused.items()})
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

        # 从这里起本轮在开的币定了：别的币可以并发开临时会，结论记进 symbols（见 _refresh_symbol）
        self._round_symbols, self._round_expiry = symbols, round_expiry
        self._round_meeting = {e.symbol for e in to_meet}

        def make_card() -> BattleCard:
            return BattleCard(
                card_id=self._card_id(now),
                version=base_version + max(published, 1) + self._round_event_merges,
                created_at=now,
                expires_at=round_expiry,
                source="tradingagents",
                regime=round_regime,
                regime_direction=round_direction,
                symbols=self._fit(list(symbols.values())),
                refresh_reason=reason,
                llm_cost_usd=round(cost + self._round_event_cost, 6),
            )

        # 总盘会挑的币带上挑它的理由；固定 / 持仓强制开会的币不是挑的，理由留空（引擎写成
        # 「这轮不是挑的，每轮都开会」，不提持仓，免得会议被现有仓位带着走）
        reasons = {p.symbol: p.reason for p in picked.picks}

        async def meet(e: SymbolPoolEntry) -> Analysis:
            nonlocal cost, published
            self._mark_meeting_px(e.symbol, self._clock())
            res = await self._analyze_one(
                e,
                round_regime,
                reasons.get(e.symbol, ""),
                brief_text=brief_text,
                market_view=market_view,
            )
            # 开完一场就出卡并落盘：中途重启 / 失败不会丢掉已经开完的会
            fresh = self._stamp(
                self._limit_size(clamp_to_risk(res.card_symbol, self._risk, self._stop)),
                now,
                round_expiry,
                res.market_view or market_view,
            )
            symbols[e.symbol] = self._flip_gate(e.symbol, fresh, symbols.get(e.symbol), now)
            cost += res.cost_usd
            published += 1
            self._last_meeting[e.symbol] = now
            card = make_card()
            self._card = card
            self._notify_card()
            self._save_card()
            if self._journal is not None:
                self._journal.record(self._raw_view(card, fresh), [e.symbol], {e.symbol: res.setup})
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
                self._heartbeat(Health.OK, f"开会中：{self._progress_text()}", None)

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
        self._notify_card()
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

    async def _analyze_one(
        self,
        entry: SymbolPoolEntry,
        regime: str,
        pick_reason: str = "",
        *,
        brief_text: str = "",
        market_view: MarketView | None = None,
    ) -> Analysis:
        async with self._sem:
            started = time.monotonic()
            regime, market_view, regime_note = self._regime_at_start(regime, market_view)
            intra = self.market_intra(self._clock(), market_view)
            if intra and market_view is not None:
                regime_note = {**regime_note, "market_intra": intra}
            regime, intra, labels = self._regime_labels(regime, market_view, intra)
            regime_note = {**regime_note, **labels}
            est = self._estimate(self._meeting_costs)
            self._charge(est)  # 预记：被强退打断的会议（重启）也算花过钱；正常结束时在下面退回估算
            self._event_uncharged.discard(entry.symbol)
            try:
                contextual_analyze = getattr(self._engine, "analyze_with_context", None)
                extra = {"market_intra": intra} if intra else {}
                meeting = (
                    contextual_analyze(entry, regime, pick_reason, brief_text=brief_text, **extra)
                    if callable(contextual_analyze)
                    else self._engine.analyze(
                        entry, f"{regime} {intra}" if intra else regime, pick_reason
                    )
                )
                res = cast(Analysis, await asyncio.wait_for(meeting, self._symbol_timeout_s))
                cs = res.card_symbol
                if market_view is not None:
                    cs = cs.model_copy(
                        update={
                            "source_market_view_id": market_view.market_view_id,
                            "source_regime": market_view.regime,
                        }
                    )
                # 只记录的 outlook 配上这场会看到的总盘会方向，开会记录里就是完整的一份；
                # replace 只换指令，其余（花费、用量、会议设置）原样带着
                res = replace(res, card_symbol=with_board(cs, market_view), market_view=market_view)
                if regime_note:
                    res = replace(res, setup={**(res.setup or {}), **regime_note})
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
                    setup=self._failed_setup(exc, entry.symbol),
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
                    setup=res.setup,
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
                setup=res.setup,
            )
            return res

    def _regime_labels(
        self, regime: str, view: MarketView | None, intra: str | None
    ) -> tuple[str, str | None, dict[str, str]]:
        """Prefixes the regime text for one meeting. A view is only republished when its direction
        changes (unless ``republish_after_min``), so without its publish time a meeting reads an
        hours-old text as current."""
        tag, note = "", {}
        if view is not None:
            when = f"{view.observed_at.astimezone(UTC):%H:%M} UTC"
            age = self.market_age(self._clock(), view)
            if age:
                head = f"大盘台 {when} 发布的 4 小时判断" if intra else f"大盘判断 {when} 发布"
                tag = f"{head}，距此刻 {age['age_min']:.0f} 分钟"
                if "stale" in age:
                    tag += f"，{age['stale']}"
                note = {"regime_age_min": f"{age['age_min']:.0f}"}
                if "stale" in age:
                    note["regime_stale"] = "true"
            elif intra:
                tag = f"大盘台 {when} 发布的 4 小时判断"
        if self._pool_scope_label:
            tag = f"{tag}；{POOL_SCOPE}" if tag else POOL_SCOPE
            if intra:
                intra = f"全池口径：{intra}"
        return (f"（{tag}）{regime}" if tag else regime), intra, note

    def market_age(self, now: datetime, view: MarketView | None) -> dict[str, Any]:
        """How old the market view is, for meetings and the keeper / Jev snapshots
        (``current_market``): ``age_min`` (minutes since publish), plus ``stale`` past
        ``regime_max_age_min``. Empty when both settings are off or there is no view."""
        if view is None:
            return {}
        expired = self._regime_max_age > timedelta(0) and now - view.observed_at > (
            self._regime_max_age
        )
        if not (self._regime_time_label or expired):
            return {}
        out: dict[str, Any] = {
            "age_min": round(max((now - view.observed_at).total_seconds(), 0.0) / 60.0, 1)
        }
        if expired:
            out["stale"] = REGIME_STALE
        return out

    def _regime_at_start(
        self, regime: str, market_view: MarketView | None
    ) -> tuple[str, MarketView | None, dict[str, str]]:
        """单币会真正开始（拿到并发名额）时用哪一版大盘判断。``latest_market_regime`` 关着 =
        原样（本轮开始时的）；开着 = 此刻最新发布的那份，并返回记进 meeting_setup 的几项。"""
        if not self._latest_regime:
            return regime, market_view, {}
        latest = self.latest_market_view
        if latest is None or market_view is None:
            used, source = market_view, "round"
        else:
            used = latest
            source = "round" if latest.market_view_id == market_view.market_view_id else "latest"
        note = {
            "regime_source": source,
            "regime_market_view_id": used.market_view_id if used is not None else "",
            "regime_observed_at": used.observed_at.isoformat() if used is not None else "",
        }
        return (used.regime if used is not None else regime), used, note

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

    def _bump(self, *, ok: bool, cost: float, ms: float, round_cost: bool = True) -> None:
        if round_cost:
            self.round_cost_usd += cost
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

    def _charge(self, cost: float, *, round_cost: bool = True) -> None:
        """只记花费、不算一次调用（选币步骤，或不属于某个标的的花费）。"""
        if round_cost:
            self.round_cost_usd += cost
        else:
            self.market_refresh_cost_usd += cost
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
            loop.call_soon_threadsafe(lambda: self._charge(delta, round_cost=key != "market_only"))

    def bind_credit_alarm(self, fn: Callable[[], None]) -> None:
        """模型额度不足（402）时调一下：让主程序发告警、面板显示横幅。"""
        self._credit_alarm = fn

    def _failed_setup(self, exc: BaseException, key: str) -> dict[str, str] | None:
        """失败的会（报错 / 质量门槛 / 超时）用的偏空整改设置，好按组算失败率：引擎随错误带出来的
        优先；超时没有错误对象，就问引擎这个币这场会定下的设置。拿不到（还没定就失败了）为 None。"""
        if isinstance(exc, MeetingError) and exc.setup is not None:
            return exc.setup
        fn = getattr(self._engine, "meeting_setup", None)
        return fn(key) if callable(fn) else None

    def _failure_cost(self, exc: BaseException, key: str = "") -> float:
        """失败会议的花费：引擎带出了真实花费就用它；超时取消的会议，引擎还在跑，就叫停它并取
        已经发生的真实花费；都拿不到（断网等）才按估算上限计。额度不足（402）被拒的请求没有
        烧 token，按 0 计，免得日花费虚高。"""
        known = exc.cost_usd if isinstance(exc, MeetingError) else None
        if is_credit_error(exc):
            if self._credit_alarm is not None:
                self._credit_alarm()
            return 0.0 if known is None else known
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
        # 用户可把间隔设得比卡寿命长；旧卡过期也不能丢掉已花钱重开的时间。
        try:
            if data.get("last_regime_flip_at") is not None:
                at = datetime.fromisoformat(data["last_regime_flip_at"])
                if at.tzinfo is None:
                    raise ValueError("翻转时间必须带时区")
                self.last_regime_flip_at = at
        except (ValueError, TypeError):
            logger.warning("大盘翻转上次重开时间损坏，无法恢复最少间隔")
        # 存盘是 JSON：tuple（stop_hint）读回来是 list，两边都过一遍 JSON 再比
        if saved_stop != json.loads(json.dumps(asdict(self._stop))):
            logger.info("存档的作战卡档位 / ATR 周期与现在不一致，不恢复")
            return  # 出卡时的档位 / ATR 周期和现在不一致：卡里的止损倍数不能直接用，等下一轮会
        now = self._clock()
        if not self._alive(card, now):
            logger.info("存档的作战卡已过期，不恢复")
            return
        # 重启前后可能改过硬上限或切了档位：按现在的配置再夹一遍，免得风控员整个拒掉
        card = card.model_copy(
            update={"symbols": [clamp_to_risk(sym, self._risk, self._stop) for sym in card.symbols]}
        )
        self._card, self._last_refresh = card, card.created_at
        try:
            raw_ref = data.get("regime_reference")
            if raw_ref is not None:
                self.regime_reference = RegimeReference.model_validate(raw_ref)
                self.last_regime_direction = self.regime_reference.direction
            for key, attr in (
                ("scheduled_due", "_scheduled_due"),
                ("last_refresh", "_last_refresh"),
            ):
                if data.get(key) is not None:
                    at = datetime.fromisoformat(data[key])
                    if at.tzinfo is None:
                        raise ValueError("参照时间必须带时区")
                    setattr(self, attr, at)
        except (ValueError, TypeError):
            self.regime_reference = None
            logger.warning("大盘翻转参照损坏，不触发翻转；作战卡仍按原规则恢复")
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
                        # outlook 只是记录（已在开会记录和复盘流水里），不进存档：回退旧版本时
                        # 旧代码（契约禁止多余字段）还能读回这张卡
                        "card": self._card.model_dump(
                            mode="json", exclude={"symbols": {"__all__": {"outlook"}}}
                        ),
                        "last_meeting": {k: v.isoformat() for k, v in self._last_meeting.items()},
                        "stop": asdict(self._stop),
                        "regime_reference": (
                            self.regime_reference.model_dump(mode="json")
                            if self.regime_reference is not None
                            else None
                        ),
                        "last_regime_flip_at": (
                            self.last_regime_flip_at.isoformat()
                            if self.last_regime_flip_at
                            else None
                        ),
                        "scheduled_due": (
                            self._scheduled_due.isoformat() if self._scheduled_due else None
                        ),
                        "last_refresh": self._last_refresh.isoformat()
                        if self._last_refresh
                        else None,
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
        setup: dict[str, str] | None = None,
    ) -> None:
        ms = (time.monotonic() - started) * 1000
        self._bump(ok=outcome is not Outcome.ERROR, cost=cost, ms=ms)
        self._write(
            symbol=entry.symbol,
            outcome=outcome,
            summary=summary[:200],
            input={"score": entry.score, "reasons": entry.reasons},
            output=self._symbol_output(cs, estimated, usage, setup),
            latency_ms=ms,
            cost_usd=cost,
        )

    @staticmethod
    def _symbol_output(
        cs: CardSymbol | None,
        estimated: bool,
        usage: dict[str, int] | None,
        setup: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        out: dict[str, Any] = (
            cs.model_dump(mode="json") if cs is not None else {"cost_estimated": estimated}
        )
        if usage is not None:  # 面板「AI 费用」按模型 token 展示
            out["usage"] = usage
        if setup is not None:  # 偏空整改的分组和设置，复盘按组对照
            out["meeting_setup"] = setup
        return out

    def _record_card(
        self, card: BattleCard, started: float, *, partial: bool = False, cost: float = 0.0
    ) -> None:
        """写一条出卡记录（面板读最新一条显示作战卡）。``partial`` = 一轮会议中途、某个币刚开完：
        只为了面板马上显示这个币，不记花费、不算调用次数（整轮结束那条照旧记总盘会花费）。
        ``cost``：这条记录记的总盘会花费（整轮传本轮的，临时会传 0）；各标的花费在各自记录里。"""
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
                "regime_direction": card.regime_direction,
                "market_view": self.latest_market_view.model_dump(mode="json")
                if self.latest_market_view
                else None,
                "version": card.version,
                "expires_at": card.expires_at.isoformat(),  # 面板据此显示“还有多久有效”
                "symbols": [s.symbol for s in card.symbols],
                # 指令池：每个币自己的出会时间和到期时间，面板据此显示 / 到期隐藏
                "pool": {
                    s.symbol: {
                        "issued_at": (s.issued_at or card.created_at).isoformat(),
                        "expires_at": (s.expires_at or card.expires_at).isoformat(),
                        "held": s.symbol in self._held,
                        **self._flip_note(s.symbol),
                    }
                    for s in card.symbols
                },
                "total_cost_usd": card.llm_cost_usd,
            },
            latency_ms=(time.monotonic() - started) * 1000,
            cost_usd=0.0 if partial else cost,
        )
        if partial:
            return
        self._bump(
            ok=True, cost=0.0, ms=(time.monotonic() - started) * 1000
        )  # 选币花费已在开会时计入
        also = f" + {self._direct_n} 个直接开会" if self._direct_n else ""
        self._heartbeat(
            Health.OK,
            f"当前卡 v{card.version}，{tradable} 个可做（每轮最多 {self._pick_max} 张{also}）",
            None,
        )

    def _flip_note(self, symbol: str) -> dict[str, Any]:
        f = self._flips.get(symbol)
        if f is None:
            return {}
        return {
            "flip_pending": {
                "confirmed": f.confirmed,
                "candidate": f.candidate,
                "count": f.count,
                "need": self._flips.need,
                "by": "assistant" if self._flips.by_assistant() else "meeting",
                "since": f.first_seen.isoformat(),
                "progress": self._flips.progress(f.count, f.first_seen, self._clock()),
            }
        }

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
