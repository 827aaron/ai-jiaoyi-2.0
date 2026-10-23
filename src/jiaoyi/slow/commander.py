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
import os
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
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
            "notional_usdt": _clamp_range(cs.notional_usdt, 0.0, risk.max_notional_per_trade_usdt),
            "leverage": _clamp_range(cs.leverage, 1.0, float(risk.max_leverage)),
            # 止损倍数落在保镖认的区间内，免得整张卡这个标的被 min/max_stop_atr 拒掉
            "stop_atr": _clamp_range(cs.stop_atr, stop.min_stop_atr, stop.max_stop_atr),
            # 止盈倍数同理：低于 min_tp_r 整张卡这个标的会被保镖拒掉
            "take_profit_r": _clamp_range(cs.take_profit_r, stop.min_tp_r, stop.max_tp_r),
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
    ) -> None:
        if not timedelta(hours=1) <= ttl <= timedelta(hours=4):
            raise ValueError("作战卡有效期应在 1–4 小时")
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
        self._restore_budget()
        self._restore_card()

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
        try:
            picked = await self._engine.pick(free, max(room, 0), self._card)
        except Exception as exc:
            # 总盘会失败也烧了 token：按真实 usage，拿不到就按估算上限计
            self._charge(self._failure_cost(exc))
            raise
        self._charge(picked.cost_usd)  # 选币的花费一出结果就计，哪怕后面所有标的都失败
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
        prev = self._card
        same_day = prev is not None and prev.card_id == self._card_id(now)
        base_version = prev.version if prev is not None and same_day else 0
        symbols: dict[str, CardSymbol] = dict(reused)
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
            symbols[e.symbol] = clamp_to_risk(res.card_symbol, self._risk, self._stop)
            cost += res.cost_usd
            published += 1
            self._last_meeting[e.symbol] = now
            card = make_card()
            self._card = card
            self._save_card()
            if self._journal is not None:
                self._journal.record(card, [e.symbol])
            return res

        results = await asyncio.gather(*(meet(e) for e in to_meet), return_exceptions=True)
        failed = [
            f"{e.symbol}: {r!r}"
            for e, r in zip(to_meet, results, strict=True)
            if not isinstance(r, Analysis)
        ]
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

    async def _analyze_one(self, entry: SymbolPoolEntry, regime: str) -> Analysis:
        async with self._sem:
            started = time.monotonic()
            try:
                res = await asyncio.wait_for(
                    self._engine.analyze(entry, regime), self._symbol_timeout_s
                )
            except Exception as exc:
                self._log_symbol(
                    entry,
                    Outcome.ERROR,
                    f"{entry.symbol} 开会失败：{exc}",
                    started,
                    self._failure_cost(exc),
                )
                raise
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
        if cs.notional_usdt.max > r.max_notional_per_trade_usdt or cs.leverage.max > r.max_leverage:
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
            update={"cost_usd": round(self._stats.cost_usd + cost, 6)}
        )
        self._save_budget()

    def _failure_cost(self, exc: BaseException) -> float:
        """失败会议的花费：引擎给了真实 usage 就用它，否则（超时、断网等）按估算上限计。"""
        known = exc.cost_usd if isinstance(exc, MeetingError) else None
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
            self._stats, self._stats_day = TodayStats(cost_usd=cost), day

    def _save_budget(self) -> None:
        if self._budget_path is None:
            return
        tmp = self._budget_path.with_suffix(".tmp")
        try:
            self._budget_path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(
                json.dumps({"day": self._stats_day, "cost_usd": self._stats.cost_usd}),
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
        try:
            data = json.loads(self._card_path.read_text(encoding="utf-8"))
            card = BattleCard.model_validate(data["card"])
            meets = {str(k): datetime.fromisoformat(v) for k, v in data["last_meeting"].items()}
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return
        now = self._clock()
        if not card.is_valid(now):
            return
        # 重启前后可能改过硬上限或切了档位：按现在的配置再夹一遍，免得保镖整个拒掉
        card = card.model_copy(
            update={"symbols": [clamp_to_risk(sym, self._risk, self._stop) for sym in card.symbols]}
        )
        self._card, self._last_refresh = card, card.created_at
        self._seen = {s.symbol for s in card.symbols}
        self._last_meeting = meets

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
                    }
                ),
                encoding="utf-8",
            )
            os.replace(tmp, self._card_path)
        except OSError:
            with contextlib.suppress(OSError):
                tmp.unlink()
            # 落盘失败不影响出卡；内存里的卡照常生效

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
