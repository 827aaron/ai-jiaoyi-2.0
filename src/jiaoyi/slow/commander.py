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
import os
import time
import uuid
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
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


@dataclass(frozen=True)
class Analysis:
    card_symbol: CardSymbol
    cost_usd: float = 0.0


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
        self._progress = ""
        self._last_error: str | None = None
        self._meeting_costs: deque[float] = deque(maxlen=8)
        self._pick_costs: deque[float] = deque(maxlen=4)
        self._restored = False
        self._beat_s = 30.0
        self._fails = 0
        self._retry_at: datetime | None = None
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
        self._restore_tiers()
        self._stats = TodayStats()
        self._stats_day = ""
        self._pick_cost = 0.0
        self._restore_budget()
        self._restore_card()

    # ------------------------------------------------------------ 快环读
    def current(self, now: datetime) -> BattleCard | None:
        c = self._card
        if c is None or not c.is_valid(now):
            return None
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

    def may_refresh(self, now: datetime) -> bool:
        """开会前的总闸，所有触发原因（含定时兜底）都要过：失败退避中、或当日花费超限就不开。"""
        if self._budget_exhausted(now):
            return False
        return self._retry_at is None or now >= self._retry_at

    def take_notice(self) -> str | None:
        """取走一条待推送的提示（例如“当日花费超限，停止开会”），没有返回 None。"""
        return self._notices.pop(0) if self._notices else None

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
        self, now: datetime, pool: SymbolPool, prices: Mapping[str, float]
    ) -> str | None:
        """要不要开会、为什么。距上次开会不足 ``min_refresh_gap`` 一律不开，避免抖动。"""
        if not self.may_refresh(now):
            return None
        card = self.current(now)
        if card is None:
            return "scheduled"
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
        if card.expires_at - now <= self._lead:
            return "scheduled"
        return None

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
        for e in candidates:
            if e.symbol in keep:
                continue
            if e.symbol in by_pick:
                why.append(f"{e.symbol}：军师挑了，但超出名额或中间档当天名额已满")
            else:
                why.append(f"{e.symbol}：军师没挑")
        for sym in reused:
            last = self._last_meeting.get(sym)
            ago = f"{(now - last).total_seconds() / 60:.0f} 分钟前" if last else "不久前"
            why.append(f"{sym}：{ago}开过会、没有大消息 / 关键位突破，沿用上一张卡的指令")
        live.step("filter", "filter", "\n".join(why))
        live.finish("market", picked.regime, "", picked.cost_usd)

    def _next_meeting(self, now: datetime) -> datetime | None:
        """下一次会议最早什么时候开（按现在的状态估：卡到期前 ``expiry_lead``、退避、最小间隔）。"""
        at: datetime | None = None
        card = self.current(now)
        if card is not None:
            at = card.expires_at - self._lead
            if self._last_refresh is not None:
                at = max(at, self._last_refresh + self._min_gap)
        if self._retry_at is not None:
            at = max(at, self._retry_at) if at is not None else self._retry_at
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
        if self._fails and self._retry_at is not None:
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
            if live is not None:
                live.fail(self._why(exc), fail_cost)
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

        # 同一个币两次会议至少隔 symbol_min_interval；没有大消息 / 关键位突破就沿用上一张卡的指令
        forced = self._news | self._level_broken
        reused: dict[str, CardSymbol] = {}
        prev_card = self._card
        for e in chosen:
            last = self._last_meeting.get(e.symbol)
            old = prev_card.for_symbol(e.symbol) if prev_card is not None else None
            if (
                old is not None
                and last is not None
                and now - last < self._symbol_min_interval
                and e.symbol not in forced
            ):
                reused[e.symbol] = old
        to_meet = [e for e in chosen if e.symbol not in reused]
        if live is not None:
            self._market_done(live, picked, chosen, candidates, pinned, reused, now)
        prev = self._card
        same_day = prev is not None and prev.card_id == self._card_id(now)
        base_version = prev.version if prev is not None and same_day else 0
        symbols: dict[str, CardSymbol] = dict(reused)
        # 本轮还没轮到 / 开会失败的币：上一张卡的指令先留着，开完哪个换哪个，不然持仓管家会看到
        # 卡上暂时没有这个币而判“不再盯”。上一张卡已过期就不沿用旧指令，只给有持仓的币留“不开新仓”
        old_ok = prev is not None and prev.is_valid(now)
        for e in to_meet:
            old = prev.for_symbol(e.symbol) if prev is not None else None
            if old is None:
                continue
            if old_ok:
                symbols[e.symbol] = clamp_to_risk(old, self._risk, self._stop)
            elif e.symbol in self._held:
                symbols[e.symbol] = self._watch_only(old)
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

        def make_card() -> BattleCard:
            return BattleCard(
                card_id=self._card_id(now),
                version=base_version + max(published, 1),
                created_at=now,
                expires_at=now + self._ttl,
                source="tradingagents",
                regime=picked.regime,
                symbols=list(symbols.values()),
                refresh_reason=reason,
                llm_cost_usd=round(cost, 6),
            )

        async def meet(e: SymbolPoolEntry) -> Analysis:
            nonlocal cost, published
            res = await self._analyze_one(e, picked.regime)
            # 开完一场就出卡并落盘：中途重启 / 失败不会丢掉已经开完的会
            symbols[e.symbol] = self._limit_size(
                clamp_to_risk(res.card_symbol, self._risk, self._stop)
            )
            cost += res.cost_usd
            published += 1
            self._last_meeting[e.symbol] = now
            card = make_card()
            self._card = card
            self._save_card()
            if self._journal is not None:
                self._journal.record(card, [e.symbol])
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
            else:
                symbols.pop(e.symbol, None)  # 没有新结论的币不留旧指令
        if not symbols:
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
                    entry, Outcome.ERROR, f"{entry.symbol} 结论不合格：{exc}", started, res.cost_usd
                )
                raise
            self._log_symbol(
                entry,
                Outcome.ACTION,
                f"{entry.symbol} 开会结论：{cs.bias.value}，{cs.reason}",
                started,
                res.cost_usd,
                cs,
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
        if saved_stop != asdict(self._stop):
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
    ) -> None:
        ms = (time.monotonic() - started) * 1000
        self._bump(ok=outcome is not Outcome.ERROR, cost=cost, ms=ms)
        self._write(
            symbol=entry.symbol,
            outcome=outcome,
            summary=summary[:200],
            input={"score": entry.score, "reasons": entry.reasons},
            output=cs.model_dump(mode="json") if cs is not None else {"cost_estimated": estimated},
            latency_ms=ms,
            cost_usd=cost,
        )

    def _record_card(self, card: BattleCard, started: float) -> None:
        tradable = sum(1 for s in card.symbols if s.bias is not Bias.NO_TRADE)
        self._write(
            card_id=card.card_id,
            outcome=Outcome.ACTION,
            summary=f"出作战卡 v{card.version}：{len(card.symbols)} 个标的，{tradable} 个可做",
            input={"refresh_reason": card.refresh_reason},
            output={
                "regime": card.regime,
                "version": card.version,
                "expires_at": card.expires_at.isoformat(),  # 面板据此显示“还有多久有效”
                "symbols": [s.symbol for s in card.symbols],
                "total_cost_usd": card.llm_cost_usd,
            },
            latency_ms=(time.monotonic() - started) * 1000,
            cost_usd=self._pick_cost,  # 只记总盘会这一份；各标的的花费在各自的记录里，避免重复统计
        )
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
        )
        self._last_error = str(exc)[:200]
        self._heartbeat(Health.DEGRADED, "开会失败，沿用旧卡直到过期", self._last_error)

    def _heartbeat(self, health: Health, headline: str, err: str | None) -> None:
        self._log.heartbeat(
            ModuleStatus(
                role=Role.COMMANDER,
                health=health,
                ts=self._clock(),
                headline=headline,
                today=self._stats,
                last_error=err,
                version="slow-0.1",
            )
        )
