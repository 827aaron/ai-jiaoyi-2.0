"""军师团：从候选池挑 10–20 个，逐个开会，出一张作战卡（设计文档 §四、§六）。

流程：总盘会（便宜）挑标的 → 对每个标的开会（4–5 并发）→ 把结果夹进硬上限 → 汇成一张卡。
会议本身由 ``MeetingEngine`` 做（生产是 TradingAgents + DeepSeek，见 ``tradingagents_engine``），
这里只管调度、夹紧、落记录，所以没有 key 也能测。

故障即关闭：全部标的都开会失败就抛 ``CardRefreshError``，旧卡到期后快环自然不再开新仓；
单个标的失败只是这一张卡里没有它（没有指令 = 不开仓）。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from jiaoyi.contracts import (
    BattleCard,
    Bias,
    CardSymbol,
    DecisionRecord,
    Health,
    KeyLevel,
    LevelKind,
    ModuleStatus,
    Outcome,
    Range,
    RiskConfig,
    Role,
    SymbolPool,
    SymbolPoolEntry,
    TodayStats,
)
from jiaoyi.interfaces import DecisionLog
from jiaoyi.slow.brief import MarketBriefService
from jiaoyi.slow.review import CardJournal


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


def _clamp_range(r: Range, lo: float, hi: float) -> Range:
    top = max(lo, min(r.max, hi))
    return Range(min=min(max(r.min, lo), top), max=top)


def clamp_to_risk(cs: CardSymbol, risk: RiskConfig) -> CardSymbol:
    """卡里的范围只能比硬上限更紧：超出的夹回去；分组敞口上限为 0 的类别改成只看不做。"""
    if risk.category_exposure_x.get(cs.category, 0.0) <= 0.0 or cs.bias is Bias.NO_TRADE:
        return cs.model_copy(update={"bias": Bias.NO_TRADE, "allowed_signals": []})
    return cs.model_copy(
        update={
            "notional_usdt": _clamp_range(cs.notional_usdt, 0.0, risk.max_notional_per_trade_usdt),
            "leverage": _clamp_range(cs.leverage, 1.0, float(risk.max_leverage)),
        }
    )


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
        symbol_timeout_s: float = 600.0,
        min_refresh_gap: timedelta = timedelta(minutes=5),
        expiry_lead: timedelta = timedelta(minutes=10),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        brief: MarketBriefService | None = None,
        journal: CardJournal | None = None,
        fail_backoff: Sequence[timedelta] = (
            timedelta(minutes=5),
            timedelta(minutes=10),
            timedelta(minutes=30),
        ),
        daily_cost_limit_usd: float | None = None,
    ) -> None:
        if not timedelta(hours=1) <= ttl <= timedelta(hours=4):
            raise ValueError("作战卡有效期应在 1–4 小时")
        self._clock = clock
        self._brief = brief
        self._journal = journal
        if not fail_backoff:
            raise ValueError("fail_backoff 至少要有一档")
        self._backoff = tuple(fail_backoff)
        self._cost_limit = daily_cost_limit_usd
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
        self._stats = TodayStats()
        self._stats_day = ""
        self._pick_cost = 0.0

    # ------------------------------------------------------------ 快环读
    def current(self, now: datetime) -> BattleCard | None:
        c = self._card
        if c is None or not c.is_valid(now):
            return None
        if not self._blocked:
            return c
        # 被封的币在读取时改成“只看不做”：立刻生效，不等下一次开会；已有持仓不受影响
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

    def block_symbol(self, symbol: str, reason: str) -> None:
        """封掉一个币的新开仓（例如币安公告要下架它）。保镖只认作战卡，所以这里改卡。

        只拦新开仓；持仓怎么处理由持仓管家和人决定。进程重启后由新闻源重新补上。
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
        if {e.symbol for e in pool.in_pool} - self._seen:
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
        candidates = pool.in_pool
        if not candidates:
            raise CardRefreshError("候选池为空，不出卡")
        if self._budget_exhausted(now):
            raise CardRefreshError("军师团当日花费已达上限，今天不再开会")
        if self._brief is not None:
            try:
                await self._brief.refresh()  # 每轮算一次，所有会议共用；失败不影响开会
            except Exception as exc:
                self._write(outcome=Outcome.ERROR, summary=f"市场简报失败，会议继续：{exc}"[:200])
        try:
            card = await self._meet(pool, candidates, reason, now)
        except Exception as exc:
            self._record_error(reason, exc, started)
            wait = self._backoff[min(self._fails, len(self._backoff) - 1)]
            self._fails += 1
            self._retry_at = self._clock() + wait  # 连续失败退避，免得每分钟重开一轮会
            raise
        self._fails, self._retry_at = 0, None
        self._card, self._last_refresh = card, now
        self._seen = {e.symbol for e in candidates}
        self._news.clear()
        self._level_broken.clear()
        self._record_card(card, started)
        return card

    async def _meet(
        self, pool: SymbolPool, candidates: list[SymbolPoolEntry], reason: str, now: datetime
    ) -> BattleCard:
        by_symbol = {e.symbol: e for e in candidates}
        pinned = [e for e in candidates if e.pinned][: self._pick_max]
        room = self._pick_max - len(pinned)
        free = [e for e in candidates if not e.pinned]
        picked = await self._engine.pick(free, max(room, 0), self._card)
        chosen = list(pinned)
        for p in picked.picks:
            e = by_symbol.get(p.symbol)
            if e is None or e.pinned or len(chosen) >= self._pick_max:
                continue  # 军师只能从池里挑，池外的丢掉
            chosen.append(e)
        self._pick_cost = picked.cost_usd
        if not chosen:
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
        results = await asyncio.gather(
            *(self._analyze_one(e, picked.regime) for e in to_meet), return_exceptions=True
        )
        symbols: list[CardSymbol] = list(reused.values())
        cost = picked.cost_usd
        failed: list[str] = []
        for e, res in zip(to_meet, results, strict=True):
            if isinstance(res, Analysis):
                symbols.append(clamp_to_risk(res.card_symbol, self._risk))
                cost += res.cost_usd
                self._last_meeting[e.symbol] = now
            else:
                failed.append(f"{e.symbol}: {res!r}")
        if not symbols:
            raise CardRefreshError("所有标的开会都失败：" + "；".join(failed))
        prev = self._card
        same_day = prev is not None and prev.card_id == self._card_id(now)
        card = BattleCard(
            card_id=self._card_id(now),
            version=prev.version + 1 if prev is not None and same_day else 1,
            created_at=now,
            expires_at=now + self._ttl,
            source="tradingagents",
            regime=picked.regime,
            symbols=symbols,
            refresh_reason=reason,
            llm_cost_usd=round(cost, 6),
        )
        if self._journal is not None:
            # 只记这次真开了会的标的；沿用旧卡的不重复记
            self._journal.record(
                card,
                [
                    e.symbol
                    for e, r in zip(to_meet, results, strict=True)
                    if isinstance(r, Analysis)
                ],
            )
        return card

    def _card_id(self, now: datetime) -> str:
        return f"card-{now:%Y%m%d}"

    async def _analyze_one(self, entry: SymbolPoolEntry, regime: str) -> Analysis:
        async with self._sem:
            started = time.monotonic()
            try:
                res = await asyncio.wait_for(
                    self._engine.analyze(entry, regime), self._symbol_timeout_s
                )
            except Exception as exc:
                self._log_symbol(
                    entry, Outcome.ERROR, f"{entry.symbol} 开会失败：{exc}", started, 0.0
                )
                raise
            cs = res.card_symbol
            if cs.symbol != entry.symbol:
                raise ValueError(f"会议返回了别的标的 {cs.symbol}，应为 {entry.symbol}")
            if cs.category != entry.category:
                # 分组决定吃哪一档敞口上限，以候选池为准，不信引擎
                raise ValueError(
                    f"{entry.symbol} 分组应为 {entry.category}，会议给了 {cs.category}"
                )
            r = self._risk
            if (
                cs.notional_usdt.max > r.max_notional_per_trade_usdt
                or cs.leverage.max > r.max_leverage
            ):
                # 会议输出越界：这个币没有结论，不是悄悄夹紧后照用
                raise ValueError(f"{entry.symbol} 会议给出的名义价值或杠杆超出硬上限")
            self._log_symbol(
                entry,
                Outcome.ACTION,
                f"{entry.symbol} 开会结论：{cs.bias.value}，{cs.reason}",
                started,
                res.cost_usd,
                cs,
            )
            return res

    # ------------------------------------------------------------ 记录
    def _bump(self, *, ok: bool, cost: float, ms: float) -> None:
        now = self._clock()
        day = now.astimezone(ZoneInfo(self._risk.day_reset_tz)).date().isoformat()
        if day != self._stats_day:
            self._stats, self._stats_day = TodayStats(), day
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
    ) -> None:
        ms = (time.monotonic() - started) * 1000
        self._bump(ok=outcome is not Outcome.ERROR, cost=cost, ms=ms)
        self._write(
            symbol=entry.symbol,
            outcome=outcome,
            summary=summary[:200],
            input={"score": entry.score, "reasons": entry.reasons},
            output=cs.model_dump(mode="json") if cs is not None else {},
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
        self._bump(ok=True, cost=self._pick_cost, ms=(time.monotonic() - started) * 1000)
        self._heartbeat(Health.OK, f"当前卡 v{card.version}，{tradable} 个可做", None)

    def _record_error(self, reason: str, exc: Exception, started: float) -> None:
        self._write(
            outcome=Outcome.ERROR,
            summary=f"开会失败（{reason}）：{exc}"[:200],
            latency_ms=(time.monotonic() - started) * 1000,
        )
        self._heartbeat(Health.DEGRADED, "开会失败，沿用旧卡直到过期", str(exc)[:200])

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
