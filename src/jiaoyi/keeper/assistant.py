"""管家助手（``keeper_assistant``）：持仓后的事归快环。快环看到对持仓不利的事件时，替管家同时问
两个互相独立的打分模型同一道是非题，两个都答「是」才动手。

触发（只看有持仓的币，赚钱亏钱都算；同一个币隔 ``keeper_assistant_gap_s`` 秒、在问时不重复问）：

- 哨兵看到和持仓方向相反的信号（反向信号）；
- （单独开关）价格朝持仓不利的方向穿过卡上的关键位。

题目：持空问「<币> 接下来 <horizon> 会上涨吗？」，持多问「……会下跌吗？」。A 是 Jev（同一条通道，
管家助手自己的超时和错误计数），B 是 ``keeper_assistant_b_model``；B 的选项倒过来列（否 / 是），
题目不变。题目和快照都只有行情：不提有没有仓、仓位方向、开仓价、盈亏和为什么问。两个都答「是」且
p(是) ≥ ``keeper_assistant_min_p``，就是「本来会平」；平仓开着、B 不是只记录、过了管家的平仓前
检查，才真的经主循环正常平仓路径平掉。B 没配置、调不通或两个合起来超时都算「不一致」，从不只凭
A 平仓。

另一件事（``keeper_assistant_flip_confirm``）：军师团的会给出待确认翻转（1/N）时，出卡路径照旧
同步记下候选、不等任何东西，只把候选交过来；这里另起一件事问两个模型「会上涨 / 下跌吗」，都答
「是」就请军师团一步同步确认（候选和卡上那条指令都没变才换，变了就作罢）；否则候选照旧等下一场会。

同一个币按「币 + 哪件事」去重：持仓的事在问时，这个币的翻转确认推后（问完再问，不丢）；名额上
持仓的事排在翻转确认前面。

每次判断记下信号时的价格和这笔仓的 R，仓位结束时再记一行实际出场：只记录时拿「本来会平」和实际
出场比；真平了的仓接着按它原来的止损 / 止盈 / 到时规则在价格上往后跟（影子出场），拿实际平仓和
影子出场比。每天、以及每平满 10 笔，决策日志和 Telegram 各一条汇总（不会自动关）。
``jiaoyi keeper-assistant-report`` 出同样的对比。

出任何错都只记录，不往管家复查和主循环里抛。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable, Coroutine, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from jiaoyi.contracts import (
    FastLoopConfig,
    FeatureSnapshot,
    Health,
    JevQuestion,
    KeyLevel,
    Outcome,
    PositionRecord,
    PositionStatus,
    Side,
)
from jiaoyi.core.recorder import RoleRecorder, local_day
from jiaoyi.gate.jev import OPENROUTER_BASE_URL, JevTransport, TypeSafeTransport
from jiaoyi.gate.questions import direction_snapshot
from jiaoyi.slow.card_flip import FlipCandidate

logger = logging.getLogger(__name__)

JOURNAL_DIR = "keeper_assistant"
CLOSE_SOURCE = "keeper_assistant"
YES, NO = "是", "否"
OPTIONS: tuple[str, ...] = (YES, NO)
MOVE = {"long": "上涨", "short": "下跌"}
QUESTION_ID = "move"
REASONS = {
    "reverse_signal": "管家助手：反向信号，两个模型一致",
    "key_level": "管家助手：价格穿过关键位，两个模型一致",
}
TRIGGER_TEXT = {"reverse_signal": "反向信号", "key_level": "价格穿过关键位"}
REOPEN_DAYS = 7
SUMMARY_EVERY = 10
"""每平满这么多笔发一次汇总。"""
"""重启时往回读几天的流水，找还没记出场的仓（仓位最长持有远短于这个）。"""


def move_question(symbol: str, want: str, horizon: str, options: Sequence[str]) -> str:
    """两个模型同一道是非题：只问行情会不会往 ``want`` 走，不提仓位和为什么问。"""
    return f"{symbol} 接下来 {horizon}会{MOVE[want]}吗？（{' / '.join(options)}）"


def position_r(pos: PositionRecord, px: float | None) -> float | None:
    """价格 ``px`` 下这笔仓的 R：账本开仓价到账本止损价为 1R。算不出为 None。"""
    entry, stop = pos.entry_price, pos.stop_loss_price
    if px is None or not entry or not stop or entry == stop:
        return None
    sign = 1.0 if pos.side is Side.LONG else -1.0
    return round(sign * (px - entry) / abs(entry - stop), 4)


@dataclass(frozen=True)
class Verdict:
    """一个模型的回答。``p_yes`` None = 没答上（``error`` 写原因）。"""

    judge: str
    model: str | None
    p_yes: float | None
    options: tuple[str, ...]
    error: str | None
    latency_ms: float
    cost_usd: float = 0.0
    usage: dict[str, int] | None = None

    def yes(self, min_p: float) -> bool:
        return self.p_yes is not None and self.p_yes > 0.5 and self.p_yes >= min_p

    @property
    def text(self) -> str:
        if self.p_yes is None:
            return f"没答上（{self.error}）"
        return f"{YES if self.p_yes > 0.5 else NO}（p(是) {self.p_yes:.2f}）"

    def row(self) -> dict[str, Any]:
        return {
            "judge": self.judge,
            "model": self.model,
            "p_yes": self.p_yes,
            "options": list(self.options),
            "error": self.error,
            "latency_ms": round(self.latency_ms, 1),
            "cost_usd": self.cost_usd,
            "usage": self.usage,
        }


class Scorer(Protocol):
    async def ask(
        self,
        judge: str,
        symbol: str,
        state: str,
        question: str,
        options: Sequence[str],
        timeout_s: float,
    ) -> Verdict: ...


class TransportScorer:
    """打分模型裁判：经 ``JevTransport``（TypeSafe SystemOne 接口）问一道是非题。

    这是管家助手自己的一份：自己的超时、连续错误计数和冷却，只借底层通道。拍板人 / 管家的
    ``JevDecisionModel`` 只收合同里的 entry / hold 题，问不了这道是非题；它的错误计数也只在它
    自己的 ``ask`` 里动，所以这里出错永远不会让拍板人或管家的 Jev 进冷却。"""

    def __init__(
        self,
        transport: JevTransport,
        *,
        usd_per_mtok: float,
        max_errors: int,
        cooldown_s: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._t = transport
        self._usd = usd_per_mtok
        self._max_errors = max(1, max_errors)
        self._cooldown_s = cooldown_s
        self._clock = clock
        self.consecutive_errors = 0
        self._cooldown_until: float | None = None

    async def ask(
        self,
        judge: str,
        symbol: str,
        state: str,
        question: str,
        options: Sequence[str],
        timeout_s: float,
    ) -> Verdict:
        opts = tuple(options)
        now = self._clock()
        if self._cooldown_until is not None and now < self._cooldown_until:
            left = self._cooldown_until - now
            return Verdict(judge, None, None, opts, f"连续出错，冷却中（还剩 {left:.0f} 秒）", 0.0)
        q = JevQuestion(id=QUESTION_ID, prompt=question, choices=opts)
        started = time.perf_counter()
        try:
            raw = await asyncio.wait_for(self._t.evaluate(state, [q], timeout_s), timeout_s)
            probs = {k: float(v) for k, v in dict(raw.probs[QUESTION_ID]).items()}
            if set(probs) != set(opts) or abs(sum(probs.values()) - 1.0) > 0.02:
                raise ValueError(f"回答的选项或概率和不对：{probs}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.consecutive_errors += 1
            if self.consecutive_errors >= self._max_errors:
                self._cooldown_until = self._clock() + self._cooldown_s
                self.consecutive_errors = 0
            why = "超时" if isinstance(exc, TimeoutError) else f"{type(exc).__name__}: {exc}"
            ms = (time.perf_counter() - started) * 1000
            return Verdict(judge, None, None, opts, why[:200], ms)
        self.consecutive_errors = 0
        self._cooldown_until = None
        cost = raw.cost_usd if raw.cost_usd is not None else raw.input_tokens * self._usd / 1e6
        return Verdict(
            judge,
            raw.model,
            probs[YES],
            opts,
            None,
            (time.perf_counter() - started) * 1000,
            cost,
            {"input_tokens": raw.input_tokens, "output_tokens": raw.output_tokens},
        )


class Slots:
    """自己的名额：上限每次现读（可热更新），和军师团开会的名额无关。``first`` 的等待者
    （持仓的事）先拿到，其余的（翻转确认）等它们都拿到了再拿。"""

    def __init__(self, limit: Callable[[], int]) -> None:
        self._limit = limit
        self.used = 0
        self._first_waiting = 0
        self._cond = asyncio.Condition()

    async def acquire(self, first: bool = True) -> None:
        async with self._cond:
            if first:
                self._first_waiting += 1
                try:
                    await self._cond.wait_for(lambda: self.used < max(1, self._limit()))
                finally:
                    self._first_waiting -= 1
            else:
                await self._cond.wait_for(
                    lambda: self.used < max(1, self._limit()) and not self._first_waiting
                )
            self.used += 1

    async def release(self) -> None:
        async with self._cond:
            self.used -= 1
            self._cond.notify_all()


class FlipCards(Protocol):
    """军师团（``BattleCardCommander``）里翻转确认用到的部分。"""

    def confirm_flip(
        self, symbol: str, expected: FlipCandidate, why: str, detail: dict[str, Any]
    ) -> bool: ...


@dataclass
class _Shadow:
    """管家助手平掉的仓：按它原来的止损 / 止盈 / 到时规则往后跟，看不平会在哪出场。"""

    position_id: str
    symbol: str
    side: str
    entry: float
    stop: float
    take_profit: float | None
    deadline: datetime
    quantity: float
    close_ts: str


class KeeperAssistant:
    def __init__(
        self,
        cfg: Callable[[], FastLoopConfig],
        recorder: RoleRecorder,
        *,
        features: Callable[[str], FeatureSnapshot | None],
        close: Callable[[PositionRecord, str], str | None],
        clock: Callable[[], datetime],
        data_dir: Path | None,
        jev: Scorer | None = None,
        b_scorer: Callable[[str], Scorer | None] | None = None,
        cards: FlipCards | None = None,
        alert: Callable[[str, str], None] | None = None,
        day_reset_tz: str = "Asia/Bangkok",
    ) -> None:
        """``close(仓, 原因)``：过平仓前检查、登记平仓任务（主循环正常路径）；返回 None = 已交出去，
        否则是没平的原因（已在平、平仓前检查拦下）。``jev``：A；``b_scorer(模型 id)``：给 B；
        ``alert(标题, 正文)``：汇总的 Telegram。"""
        self._cfg = cfg
        self._rec = recorder
        self._features = features
        self._close = close
        self._clock = clock
        self._dir = data_dir / JOURNAL_DIR if data_dir is not None else None
        self._jev = jev
        self._b_factory = b_scorer
        self._b: tuple[str, Scorer | None] | None = None
        self._cards = cards
        self._alert = alert
        self._tz = day_reset_tz
        self._slots = Slots(lambda: self._cfg().keeper_assistant_concurrency)
        self._busy: set[tuple[str, str]] = set()
        self._deferred: dict[str, FlipCandidate] = {}
        self._last: dict[str, datetime] = {}
        self._last_px: dict[str, float] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._error: str | None = None
        self._checked: set[str] = set()
        self._shadows: dict[str, _Shadow] = {}
        self._closes = 0
        self._summary_day = local_day(self._clock(), self._tz)
        self._restore()

    # ------------------------------------------------------------ 入口（同步，不抛）
    def on_reverse_signal(self, pos: PositionRecord, signal_side: Side, detail: str) -> None:
        """哨兵看到 ``signal_side`` 方向的信号；和这笔仓方向相反才问。"""
        if signal_side is not pos.side:
            self._trigger(pos, "reverse_signal", detail)

    def on_price(self, pos: PositionRecord, levels: Sequence[KeyLevel], px: float) -> None:
        """成交价朝持仓不利的方向穿过卡上某个关键位（持空往上穿、持多往下穿）就问。
        每笔仓记上一次看到的价，第一次只记不判。"""
        try:
            if not self._cfg().keeper_assistant_key_level:
                self._last_px.pop(pos.position_id, None)
                return
            prev = self._last_px.get(pos.position_id)
            self._last_px[pos.position_id] = px
            if prev is None or prev == px:
                return
            up = pos.side is Side.SHORT
            crossed = [
                lv.price
                for lv in levels
                if (prev < lv.price <= px if up else px <= lv.price < prev)
            ]
            if crossed:
                way = "往上" if up else "往下"
                self._trigger(pos, "key_level", f"价格 {prev:g} → {px:g} {way}穿过 {crossed}")
        except Exception:
            logger.exception("管家助手看关键位失败，管家照常")

    def on_closed(self, pos: PositionRecord, last_px: float | None) -> None:
        """仓位结束：问过它的话记一行实际出场（账本还没回填出场价就先用 ``last_px``）。"""
        self._last_px.pop(pos.position_id, None)
        if pos.position_id not in self._checked:
            return
        self._checked.discard(pos.position_id)
        try:
            exit_px = pos.exit_price or last_px
            self._write_row(
                {
                    "ts": self._clock().isoformat(),
                    "kind": "closed",
                    "symbol": pos.symbol,
                    "position_id": pos.position_id,
                    "side": pos.side.value,
                    "exit_price": exit_px,
                    "exit_price_source": "ledger" if pos.exit_price else "last_price",
                    "r_exit": position_r(pos, exit_px),
                    "close_reason": pos.close_reason.value if pos.close_reason else None,
                    "realized_pnl_usdt": pos.realized_pnl_usdt,
                }
            )
        except Exception:
            logger.exception("管家助手记出场失败")

    def on_flip_pending(self, symbol: str, cand: FlipCandidate) -> None:
        """军师团刚记下待确认翻转（出卡路径里同步调，这里只登记、另起任务，不等）。"""
        try:
            cfg = self._cfg()
            if not (cfg.keeper_assistant_on and cfg.keeper_assistant_flip_confirm):
                return
            if cand.candidate not in MOVE:
                self._rec.record(
                    self._clock(),
                    Outcome.INFO,
                    f"{symbol} 待确认翻转转中性：是非题只问涨跌，不判，候选照旧等下一场会",
                    symbol=symbol,
                    output={"keeper_assistant": {"kind": "flip_confirm", "skipped": "neutral"}},
                )
                return
            if any(sym == symbol for sym, _ in self._busy):
                self._deferred[symbol] = cand  # 这个币正在问：问完再问，不丢
                return
            self._start_flip(symbol, cand)
        except Exception:
            logger.exception("管家助手接翻转确认失败，候选照旧等下一场会")

    def _start_flip(self, symbol: str, cand: FlipCandidate) -> None:
        self._busy.add((symbol, "flip"))
        self._spawn((symbol, "flip"), self._flip_confirm(symbol, cand, self._clock()))

    def _trigger(self, pos: PositionRecord, trigger: str, detail: str) -> None:
        try:
            cfg = self._cfg()
            if not cfg.keeper_assistant_on or pos.status is not PositionStatus.OPEN:
                return
            now, sym = self._clock(), pos.symbol
            last = self._last.get(sym)
            gap = timedelta(seconds=cfg.keeper_assistant_gap_s)
            key = (sym, "position")
            if key in self._busy or (last is not None and now - last < gap):
                return
            self._busy.add(key)
            self._last[sym] = now
            self._spawn(key, self._check(pos, trigger, detail, now))
        except Exception:
            logger.exception("管家助手接事件失败，管家照常")

    # ------------------------------------------------------------ 生命周期 / 状态
    def _spawn(self, key: tuple[str, str], coro: Coroutine[Any, Any, None]) -> None:
        symbol = key[0]

        async def guarded() -> None:
            try:
                await coro
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._error = f"{symbol}：{exc}"[:200]
                logger.exception("管家助手处理 %s 出错，不影响管家和主循环", symbol)
                self._rec.record(
                    self._clock(),
                    Outcome.ERROR,
                    f"{symbol} 管家助手出错：{exc}"[:200],
                    symbol=symbol,
                )
            finally:
                self._busy.discard(key)
                self._resume(symbol)

        task = asyncio.ensure_future(guarded())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def aclose(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()
        self._busy.clear()  # 还没开始跑就被取消的任务进不了自己的 finally
        self._deferred.clear()

    def _resume(self, symbol: str) -> None:
        """这个币的事都问完了：推后的翻转确认现在问（候选变没变由军师团确认时再查）。"""
        if any(sym == symbol for sym, _ in self._busy):
            return
        cand = self._deferred.pop(symbol, None)
        if cand is not None:
            self._start_flip(symbol, cand)

    @property
    def busy(self) -> frozenset[str]:
        return frozenset(sym for sym, _ in self._busy)

    def heartbeat(self, now: datetime) -> None:
        cfg = self._cfg()
        if not cfg.keeper_assistant_on:
            self._rec.heartbeat(now, Health.OK, "已关闭（keeper_assistant_on = false）")
            return
        b = cfg.keeper_assistant_b_model or "没配置"
        acting = (
            "两个都答是就平仓"
            if cfg.keeper_assistant_close and not cfg.keeper_assistant_b_record_only
            else "只记录"
        )
        head = (
            f"{acting}；A Jev / B {b}；正在问 {len(self._busy)} 个币，占 {self._slots.used}/"
            f"{cfg.keeper_assistant_concurrency} 个名额"
        )
        if self._jev is None:
            head = f"Jev 没接上；{head}"
        health = Health.DEGRADED if self._error or self._jev is None else Health.OK
        self._rec.heartbeat(now, health, head)
        self._error = None

    # ------------------------------------------------------------ 两个模型
    def _b_scorer(self, model: str | None) -> Scorer | None:
        if not model or self._b_factory is None:
            return None
        if self._b is None or self._b[0] != model:
            try:
                self._b = (model, self._b_factory(model))
            except Exception:
                logger.exception("管家助手建模型 B（%s）失败，按没配置处理", model)
                self._b = (model, None)
        return self._b[1]

    async def _one(
        self, scorer: Scorer | None, judge: str, symbol: str, state: str, want: str,
        options: tuple[str, ...], why_missing: str, first: bool,
    ) -> Verdict:  # fmt: skip
        if scorer is None:
            return Verdict(judge, None, None, options, why_missing, 0.0)
        cfg = self._cfg()
        question = move_question(symbol, want, cfg.keeper_assistant_horizon, options)
        await self._slots.acquire(first)
        try:
            return await scorer.ask(
                judge, symbol, state, question, options, cfg.keeper_assistant_timeout_s
            )
        finally:
            await self._slots.release()

    async def _judges(
        self, symbol: str, want: str, first: bool = True
    ) -> tuple[Verdict, Verdict] | str:
        """A、B 同时问「会不会往 ``want`` 走」，两个合起来最多等 ``keeper_assistant_timeout_s``；
        超时两个都算没答上。返回字符串 = 问不了的原因。"""
        f = self._features(symbol)
        if f is None:
            return "没有这个币的行情快照"
        state = direction_snapshot(symbol, f)
        cfg = self._cfg()
        b_model = cfg.keeper_assistant_b_model
        mirrored = tuple(reversed(OPTIONS))
        started = time.perf_counter()
        try:
            a, b = await asyncio.wait_for(
                asyncio.gather(
                    self._one(self._jev, "A", symbol, state, want, OPTIONS, "Jev 没接上", first),
                    self._one(
                        self._b_scorer(b_model),
                        "B",
                        symbol,
                        state,
                        want,
                        mirrored,
                        "keeper_assistant_b_model 没配置" if not b_model else f"{b_model} 建不起来",
                        first,
                    ),
                ),
                cfg.keeper_assistant_timeout_s,
            )
        except TimeoutError:
            ms = (time.perf_counter() - started) * 1000
            why = f"两个合起来超过 {cfg.keeper_assistant_timeout_s:g} 秒"
            return (
                Verdict("A", None, None, OPTIONS, why, ms),
                Verdict("B", None, None, mirrored, why, ms),
            )
        return a, b

    # ------------------------------------------------------------ 判断
    async def _check(self, pos: PositionRecord, trigger: str, detail: str, now: datetime) -> None:
        sym = pos.symbol
        started = time.monotonic()
        want = "short" if pos.side is Side.LONG else "long"
        snap = self._features(sym)
        px = snap.last_price if snap is not None else None
        got = await self._judges(sym, want)
        if isinstance(got, str):
            self._rec.record(
                now,
                Outcome.INFO,
                f"{sym} {TRIGGER_TEXT[trigger]}，但{got}，不动",
                symbol=sym,
                position_id=pos.position_id,
                output={"keeper_assistant": {"trigger": trigger, "skipped": got}},
            )
            return
        a, b = got
        cfg = self._cfg()
        would = a.yes(cfg.keeper_assistant_min_p) and b.yes(cfg.keeper_assistant_min_p)
        act = would and cfg.keeper_assistant_close and not cfg.keeper_assistant_b_record_only
        reason = REASONS[trigger]
        # 名额在问完那一刻已放开；平仓由主循环另起任务去发，这里不等它
        refused = self._close(pos, reason) if act else None
        closed = act and refused is None
        why = (
            reason
            if closed
            else f"两个模型都答是，但没平：{refused}"
            if act
            else "两个模型都答是，但 B 只记录（keeper_assistant_b_record_only）"
            if would and cfg.keeper_assistant_b_record_only
            else "两个模型都答是，但 keeper_assistant_close 关着（只记录）"
            if would
            else "两个模型没有都答是（或 p(是) 不够 / 有一个没答上），不动"
        )
        self._checked.add(pos.position_id)
        if closed:
            self._closes += 1
            self._start_shadow(pos, now)
        self._log(
            now,
            started,
            sym,
            Outcome.ACTION if closed else Outcome.INFO,
            f"{sym} {TRIGGER_TEXT[trigger]}，问会不会{MOVE[want]}：A {a.text} / B {b.text}，{why}",
            {
                "kind": "check",
                "trigger": trigger,
                "trigger_detail": detail,
                "position_id": pos.position_id,
                "side": pos.side.value,
                "entry_price": pos.entry_price,
                "stop_loss_price": pos.stop_loss_price,
                "quantity": pos.quantity,
                "take_profit_price": pos.take_profit_price,
                "opened_at": pos.opened_at.isoformat(),
                "time_limit_s": pos.time_limit_s,
                "price_at_signal": px,
                "r_at_signal": position_r(pos, px),
                "asked": MOVE[want],
                "would_close": would,
                "closed": closed,
                "close_source": CLOSE_SOURCE if closed else None,
                "why": why,
            },
            (a, b),
            position_id=pos.position_id,
        )
        if closed and self._closes % SUMMARY_EVERY == 0:
            self._summary(f"管家助手已平 {self._closes} 笔", None)

    async def _flip_confirm(self, symbol: str, cand: FlipCandidate, now: datetime) -> None:
        started = time.monotonic()
        want = cand.candidate
        got = await self._judges(symbol, want, first=False)
        if isinstance(got, str):
            self._rec.record(
                now,
                Outcome.INFO,
                f"{symbol} 待确认翻转（{MOVE[want]}）没问成：{got}，照旧等下一场会",
                symbol=symbol,
                output={"keeper_assistant": {"kind": "flip_confirm", "skipped": got}},
            )
            return
        a, b = got
        cfg = self._cfg()
        both = a.yes(cfg.keeper_assistant_min_p) and b.yes(cfg.keeper_assistant_min_p)
        result = "not_agreed"
        if both and cfg.keeper_assistant_b_record_only:
            result = "b_record_only"
        elif both and self._cards is not None:
            ok = self._cards.confirm_flip(
                symbol,
                cand,
                f"管家助手两个模型都答会{MOVE[want]}",
                {"judges": [a.row(), b.row()]},
            )
            result = "confirmed" if ok else "stale"
        why = {
            "confirmed": "确认，卡上换成那场会的结论",
            "stale": "候选已变或卡上那条指令已换掉，作罢",
            "not_agreed": "两个模型没有都答是（或 p(是) 不够 / 有一个没答上），候选照旧等下一场会",
            "b_record_only": "B 只记录，候选照旧等下一场会",
        }[result]
        self._log(
            now,
            started,
            symbol,
            Outcome.ACTION if result == "confirmed" else Outcome.INFO,
            f"{symbol} 待确认翻转，问会不会{MOVE[want]}：A {a.text} / B {b.text}，{why}",
            {
                "kind": "flip_confirm",
                "position_id": f"flip:{symbol}:{cand.first_seen.isoformat()}",
                "candidate": want,
                "count": cand.count,
                "both_yes": both,
                "result": result,
                "why": why,
            },
            (a, b),
        )

    # ------------------------------------------------------------ 平掉之后：影子出场、汇总
    def _start_shadow(self, pos: PositionRecord, now: datetime) -> None:
        if not pos.entry_price or not pos.stop_loss_price:
            return
        self._shadows[pos.position_id] = _Shadow(
            pos.position_id,
            pos.symbol,
            pos.side.value,
            pos.entry_price,
            pos.stop_loss_price,
            pos.take_profit_price,
            pos.opened_at + timedelta(seconds=pos.time_limit_s),
            pos.quantity,
            now.isoformat(),
        )

    def tick(self, now: datetime) -> None:
        """心跳时调：影子仓按最新价看止损 / 止盈 / 到时有没有到；跨天发前一天的汇总。不抛。"""
        try:
            for pid, sh in list(self._shadows.items()):
                f = self._features(sh.symbol)
                px = f.last_price if f is not None else None
                exit_px, how = shadow_exit(sh, px, now)
                if exit_px is None and how is None:
                    continue
                self._shadows.pop(pid, None)
                self._write_row(
                    {
                        "ts": now.isoformat(),
                        "kind": "shadow_exit",
                        "symbol": sh.symbol,
                        "position_id": pid,
                        "side": sh.side,
                        "shadow_exit_price": exit_px,
                        "shadow_rule": how,
                        "r_shadow": None
                        if exit_px is None
                        else round(
                            (1 if sh.side == Side.LONG.value else -1)
                            * (exit_px - sh.entry)
                            / abs(sh.entry - sh.stop),
                            4,
                        ),
                    }
                )
            day = local_day(now, self._tz)
            if day != self._summary_day:
                prev = self._summary_day
                self._summary_day = day
                self._summary(f"管家助手 {prev} 汇总", prev.isoformat())
        except Exception:
            logger.exception("管家助手跟影子出场 / 汇总失败")

    def _summary(self, title: str, day: str | None) -> None:
        if self._dir is None:
            return
        pairs, pending = live_pairs(read_rows(self._dir), day)
        n = len(pairs)
        text = (
            f"平仓 {n + pending} 笔（{day or '累计'}），已知不平会在哪出场的 {n} 笔："
            + (
                f"平均 ΔR {sum(p.delta_r for p in pairs) / n:+.3f}，"
                f"合计 ΔU {sum(p.delta_usdt for p in pairs):+.2f} U"
                if n
                else "还没有"
            )
            + "（ΔR / ΔU 正 = 平掉比按原规则拿着更好；不含手续费差，不会自动关）"
        )
        self._rec.record(
            self._clock(),
            Outcome.INFO,
            f"{title}：{text}"[:200],
            output={"keeper_assistant": {"kind": "summary", "day": day, "n": n,
                                          "pending": pending, "text": text}},
        )  # fmt: skip
        if self._alert is not None:
            try:
                self._alert(title, text)
            except Exception:
                logger.exception("管家助手汇总告警失败")

    def _log(
        self,
        now: datetime,
        started: float,
        symbol: str,
        outcome: Outcome,
        summary: str,
        detail: dict[str, Any],
        judges: tuple[Verdict, Verdict],
        position_id: str | None = None,
    ) -> None:
        cfg = self._cfg()
        cost = sum(v.cost_usd for v in judges)
        row = {
            "ts": now.isoformat(),
            "symbol": symbol,
            **detail,
            "horizon": cfg.keeper_assistant_horizon,
            "min_p": cfg.keeper_assistant_min_p,
            "judges": [v.row() for v in judges],
            "cost_usd": cost,
        }
        self._rec.record(
            now,
            outcome,
            summary[:200],
            symbol=symbol,
            position_id=position_id,
            output={"keeper_assistant": row},
            latency_ms=(time.monotonic() - started) * 1000,
            cost_usd=cost,
        )
        self._write_row(row)

    # ------------------------------------------------------------ 流水
    def _write_row(self, row: dict[str, Any]) -> None:
        """``data/keeper_assistant/YYYY-MM-DD.jsonl``：每次判断一行（两个模型的 p(是)、模型、
        延迟、信号时的价格和 R），仓位结束再一行（实际出场）。写不进去只记日志。"""
        if self._dir is None:
            return
        path = self._dir / f"{str(row['ts'])[:10]}.jsonl"
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            logger.exception("管家助手流水写入失败")

    def _restore(self) -> None:
        """重启后接着等：最近几天问过、还没记出场的仓；平掉了、影子还没出场的仓；累计平了几笔。"""
        if self._dir is None:
            return
        rows = read_rows(self._dir, self._clock() - timedelta(days=REOPEN_DAYS))
        checks = [r for r in rows if r.get("kind") == "check"]
        done = {str(r["position_id"]) for r in rows if r.get("kind") == "closed"}
        shadowed = {str(r["position_id"]) for r in rows if r.get("kind") == "shadow_exit"}
        self._checked = {str(r["position_id"]) for r in checks} - done
        self._closes = sum(
            1 for r in read_rows(self._dir) if r.get("kind") == "check" and r.get("closed")
        )
        for r in checks:
            pid = str(r["position_id"])
            if not r.get("closed") or pid in shadowed:
                continue
            try:
                self._shadows[pid] = _Shadow(
                    pid,
                    str(r["symbol"]),
                    str(r["side"]),
                    float(r["entry_price"]),
                    float(r["stop_loss_price"]),
                    float(r["take_profit_price"]) if r.get("take_profit_price") else None,
                    datetime.fromisoformat(str(r["opened_at"]))
                    + timedelta(seconds=float(r["time_limit_s"])),
                    float(r.get("quantity") or 0.0),
                    str(r["ts"]),
                )
            except (KeyError, TypeError, ValueError):
                continue


def read_rows(directory: Path, since: datetime | None = None) -> list[dict[str, Any]]:
    """读流水（按文件名的日期过滤），坏行跳过。"""
    out: list[dict[str, Any]] = []
    if not directory.is_dir():
        return out
    floor = since.date().isoformat() if since is not None else ""
    for path in sorted(directory.glob("*.jsonl")):
        if path.stem < floor:
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("position_id"):
                out.append(row)
    return out


@dataclass(frozen=True)
class Pair:
    position_id: str
    day: str
    symbol: str
    r_signal: float
    r_exit: float
    delta_r: float
    delta_usdt: float


def counterfactual(
    rows: Iterable[Mapping[str, Any]], ledger: Mapping[str, PositionRecord]
) -> tuple[list[Pair], int]:
    """每笔仓第一次「本来会平」（两个都答是、p(是) 都 ≥ 当时的 min_p）和它的实际出场配对。
    ΔR = 信号时平的 R − 实际出场的 R（正 = 早平更好）；ΔU 按数量算，不含手续费差。出场价先用账本
    回填的成交价，没有才用流水里出场那一行的价格。返回（配上的，还没出场的个数）。"""
    first: dict[str, Mapping[str, Any]] = {}
    exits: dict[str, Mapping[str, Any]] = {}
    for r in sorted(rows, key=lambda x: str(x.get("ts", ""))):
        pid = str(r.get("position_id"))
        if r.get("kind") == "check" and r.get("would_close") and pid not in first:
            first[pid] = r
        elif r.get("kind") == "closed":
            exits[pid] = r
    pairs: list[Pair] = []
    open_n = 0
    for pid, r in first.items():
        px, r_sig = r.get("price_at_signal"), r.get("r_at_signal")
        rec = ledger.get(pid)
        exit_px = rec.exit_price if rec is not None and rec.exit_price else None
        if exit_px is None and pid in exits:
            exit_px = exits[pid].get("exit_price")
        closed = (rec is not None and rec.status is PositionStatus.CLOSED) or pid in exits
        if not closed or exit_px is None or px is None or r_sig is None:
            open_n += 1
            continue
        sign = 1.0 if r.get("side") == Side.LONG.value else -1.0
        entry, stop = r.get("entry_price"), r.get("stop_loss_price")
        if not entry or not stop or entry == stop:
            open_n += 1
            continue
        r_exit = sign * (float(exit_px) - float(entry)) / abs(float(entry) - float(stop))
        qty = float(r.get("quantity") or 0.0)
        pairs.append(
            Pair(
                pid,
                str(r["ts"])[:10],
                str(r.get("symbol")),
                float(r_sig),
                round(r_exit, 4),
                round(float(r_sig) - r_exit, 4),
                round(sign * (float(px) - float(exit_px)) * qty, 6),
            )
        )
    return pairs, open_n


def shadow_exit(sh: _Shadow, px: float | None, now: datetime) -> tuple[float | None, str | None]:
    """影子仓这一刻出没出场：（出场价, 哪条规则）；没出场（或没价格还没到时）是 (None, None)。
    到时了却没有价格：(None, "deadline_no_price")，不再跟。"""
    long = sh.side == Side.LONG.value
    if px is not None:
        if (px <= sh.stop) if long else (px >= sh.stop):
            return sh.stop, "stop_loss"
        tp = sh.take_profit
        if tp is not None and ((px >= tp) if long else (px <= tp)):
            return tp, "take_profit"
    if now >= sh.deadline:
        return (px, "time_limit") if px is not None else (None, "deadline_no_price")
    return None, None


def live_pairs(rows: Iterable[Mapping[str, Any]], day: str | None = None) -> tuple[list[Pair], int]:
    """管家助手真平掉的仓：实际平仓（出场那一行的价格）对比影子出场。ΔR = 实际 R − 影子 R
    （正 = 平掉更好）。``day`` 只看那天（按平仓判断那一行的 UTC 日期）平的。返回（配上的，
    还没影子出场或还没记出场的个数）。"""
    checks: dict[str, Mapping[str, Any]] = {}
    exits: dict[str, Mapping[str, Any]] = {}
    shadows: dict[str, Mapping[str, Any]] = {}
    for r in rows:
        pid = str(r.get("position_id"))
        kind = r.get("kind")
        if kind == "check" and r.get("closed") and (day is None or str(r["ts"])[:10] == day):
            checks.setdefault(pid, r)
        elif kind == "closed":
            exits[pid] = r
        elif kind == "shadow_exit":
            shadows[pid] = r
    pairs: list[Pair] = []
    pending = 0
    for pid, r in checks.items():
        ex, sh = exits.get(pid), shadows.get(pid)
        if (
            ex is None
            or sh is None
            or ex.get("r_exit") is None
            or sh.get("r_shadow") is None
            or ex.get("exit_price") is None
            or sh.get("shadow_exit_price") is None
        ):
            pending += 1
            continue
        sign = 1.0 if r.get("side") == Side.LONG.value else -1.0
        qty = float(r.get("quantity") or 0.0)
        pairs.append(
            Pair(
                pid,
                str(r["ts"])[:10],
                str(r.get("symbol")),
                float(ex["r_exit"]),
                float(sh["r_shadow"]),
                round(float(ex["r_exit"]) - float(sh["r_shadow"]), 4),
                round(sign * (float(ex["exit_price"]) - float(sh["shadow_exit_price"])) * qty, 6),
            )
        )
    return pairs, pending


def report_text(pairs: Sequence[Pair], open_n: int, *, live: bool = False) -> str:
    """``live``：真平了的仓对比影子出场；否则只记录的「本来会平」对比实际出场。"""
    lines = [
        "管家助手真平了的仓对比按原规则拿着（影子出场）（ΔR、ΔU 正 = 平掉更好；不含手续费差）"
        if live
        else "管家助手「本来会平」对比实际出场（ΔR、ΔU 正 = 信号时平更好；不含手续费差）"
    ]
    if not pairs:
        lines.append(f"还没有配上的仓（还在等出场的 {open_n} 笔）")
        return "\n".join(lines)
    mean = sum(p.delta_r for p in pairs) / len(pairs)
    total = sum(p.delta_usdt for p in pairs)
    lines.append(
        f"n = {len(pairs)}，平均 ΔR = {mean:+.3f}，合计 ΔU = {total:+.2f} U"
        f"（还在等出场的 {open_n} 笔不算）"
    )
    days: dict[str, list[Pair]] = {}
    for p in pairs:
        days.setdefault(p.day, []).append(p)
    for day in sorted(days):
        ps = days[day]
        lines.append(
            f"{day}：n = {len(ps)}，平均 ΔR = {sum(p.delta_r for p in ps) / len(ps):+.3f}，"
            f"ΔU = {sum(p.delta_usdt for p in ps):+.2f} U"
        )
    return "\n".join(lines)


def openrouter_scorer(
    api_key: str, cfg: Callable[[], FastLoopConfig]
) -> Callable[[str], Scorer | None]:
    """模型 B 的通道：OpenRouter 的 SystemOne 接口（TypeSafe SDK 换 base URL，和 Jev 走 OpenRouter
    时同一种请求），模型 id 由设置给。
    TODO: Microsoft-Decision-1 的 OpenRouter 调用方式还没在文档里核实（今天的 /models 目录里
    也查不到它）；如果它不走 SystemOne 接口，只换这一个工厂。调不通时每次都算「B 没答上」，
    不平仓。"""

    def make(model: str) -> Scorer | None:
        fl = cfg()
        return TransportScorer(
            TypeSafeTransport(api_key, model, base_url=OPENROUTER_BASE_URL),
            usd_per_mtok=fl.jev_usd_per_mtok,
            max_errors=fl.jev_max_consecutive_errors,
            cooldown_s=fl.jev_error_cooldown_s,
        )

    return make
