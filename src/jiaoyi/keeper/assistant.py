"""管家助手（``keeper_assistant``）：持仓后的事归快环。快环看到对持仓不利的事件时，替管家同时问
两个互相独立的打分模型同一道是非题，两个都答「是」才动手。

触发（只看有持仓的币，赚钱亏钱都算；同一个币隔 ``keeper_assistant_gap_s`` 秒、在问时不重复问）：

- 哨兵看到和持仓方向相反的信号（反向信号）；
- （单独开关）价格朝持仓不利的方向穿过卡上的关键位。

题目：持空问「<币> 接下来 <horizon> 会上涨吗？」，持多问「……会下跌吗？」。A 是 Jev（和管家同一条
通道，自己的超时和错误计数），B 是 ``keeper_assistant_b_model``；B 的选项倒过来列（否 / 是），题目
不变。题目和快照都只有行情：不提有没有仓、仓位方向、开仓价、盈亏和为什么问。两个都答「是」且 p(是)
≥ ``keeper_assistant_min_p``、平仓开着、B 不是只记录，就经主循环正常平仓路径平掉；其余不动、只记录。
B 没配置、调不通或超时都算「不一致」，从不只凭 A 平仓。

另一件事：军师团的会给出待确认翻转（1/N）时，那场会是提议，同样问两个模型「会上涨 / 下跌吗」，
都答「是」就确认，卡上换成那场会的结论，管家的反向卡 / 中性卡规则照常起作用；否则候选照旧等
下一场会。

出任何错都只记录，不往管家复查和主循环里抛。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable, Coroutine, Sequence
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
from jiaoyi.core.recorder import RoleRecorder
from jiaoyi.gate.jev import OPENROUTER_BASE_URL, JevTransport, TypeSafeTransport
from jiaoyi.gate.questions import direction_snapshot

logger = logging.getLogger(__name__)

JOURNAL_DIR = "keeper_assistant"
YES, NO = "是", "否"
OPTIONS: tuple[str, ...] = (YES, NO)
MOVE = {"long": "上涨", "short": "下跌"}
QUESTION_ID = "move"
REASONS = {
    "reverse_signal": "管家助手：反向信号，两个模型一致",
    "key_level": "管家助手：价格穿过关键位，两个模型一致",
}
TRIGGER_TEXT = {"reverse_signal": "反向信号", "key_level": "价格穿过关键位"}
DIR_TEXT = {"long": "做多", "short": "做空", "neutral": "中性"}


def move_question(symbol: str, want: str, horizon: str, options: Sequence[str]) -> str:
    """两个模型同一道是非题：只问行情会不会往 ``want`` 走，不提仓位和为什么问。"""
    return f"{symbol} 接下来 {horizon}会{MOVE[want]}吗？（{' / '.join(options)}）"


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
    """打分模型裁判：经 ``JevTransport``（TypeSafe SystemOne 接口）问一道是非题。自己的超时和
    连续错误计数（到上限冷却一段），和管家、拍板人用同一条通道也互不连累。"""

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


class FlipCards(Protocol):
    """军师团（``BattleCardCommander``）里翻转确认用到的部分。"""

    def flip_candidate(self, symbol: str) -> str | None: ...

    def confirm_flip(
        self, symbol: str, direction: str, why: str, detail: dict[str, Any]
    ) -> bool: ...


class Slots:
    """自己的名额：上限每次现读（可热更新），和军师团开会的名额无关。"""

    def __init__(self, limit: Callable[[], int]) -> None:
        self._limit = limit
        self.used = 0
        self._cond = asyncio.Condition()

    async def acquire(self) -> None:
        async with self._cond:
            await self._cond.wait_for(lambda: self.used < max(1, self._limit()))
            self.used += 1

    async def release(self) -> None:
        async with self._cond:
            self.used -= 1
            self._cond.notify_all()


class KeeperAssistant:
    def __init__(
        self,
        cfg: Callable[[], FastLoopConfig],
        recorder: RoleRecorder,
        *,
        features: Callable[[str], FeatureSnapshot | None],
        close: Callable[[PositionRecord, str], bool],
        clock: Callable[[], datetime],
        data_dir: Path | None,
        jev: Scorer | None = None,
        b_scorer: Callable[[str], Scorer | None] | None = None,
        cards: FlipCards | None = None,
    ) -> None:
        """``close(仓, 原因)``：同步登记平仓（主循环正常路径，同一笔已在平返回 False）。
        ``jev``：A；``b_scorer(模型 id)``：按设置里的模型 id 给 B，建不了返回 None。"""
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
        self._slots = Slots(lambda: self._cfg().keeper_assistant_concurrency)
        self._busy: set[str] = set()
        self._last: dict[str, datetime] = {}
        self._last_px: dict[str, float] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._error: str | None = None

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

    def forget(self, position_id: str) -> None:
        self._last_px.pop(position_id, None)

    def on_flip_pending(self, symbol: str, candidate: str) -> None:
        """军师团刚出了待确认翻转：马上问两个模型。这个币正在问就不另问。"""
        try:
            cfg = self._cfg()
            if not (cfg.keeper_assistant_on and cfg.keeper_assistant_flip_confirm):
                return
            if candidate not in MOVE:
                self._rec.record(
                    self._clock(),
                    Outcome.INFO,
                    f"{symbol} 待确认翻转转中性：是非题只问涨跌，不判，候选照旧等下一场会",
                    symbol=symbol,
                    output={"keeper_assistant": {"kind": "flip_confirm", "skipped": "neutral"}},
                )
                return
            if symbol in self._busy:
                self._rec.record(
                    self._clock(),
                    Outcome.INFO,
                    f"{symbol} 待确认翻转：这个币正在问，候选照旧等下一场会",
                    symbol=symbol,
                    output={"keeper_assistant": {"kind": "flip_confirm", "skipped": "busy"}},
                )
                return
            self._busy.add(symbol)
            self._spawn(symbol, self._flip_confirm(symbol, candidate, self._clock()))
        except Exception:
            logger.exception("管家助手接翻转确认失败，候选照旧等下一场会")

    def _trigger(self, pos: PositionRecord, trigger: str, detail: str) -> None:
        try:
            cfg = self._cfg()
            if not cfg.keeper_assistant_on or pos.status is not PositionStatus.OPEN:
                return
            now, sym = self._clock(), pos.symbol
            last = self._last.get(sym)
            gap = timedelta(seconds=cfg.keeper_assistant_gap_s)
            if sym in self._busy or (last is not None and now - last < gap):
                return
            self._busy.add(sym)
            self._last[sym] = now
            self._spawn(sym, self._check(pos, trigger, detail, now))
        except Exception:
            logger.exception("管家助手接事件失败，管家照常")

    # ------------------------------------------------------------ 生命周期 / 状态
    def _spawn(self, symbol: str, coro: Coroutine[Any, Any, None]) -> None:
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
                self._busy.discard(symbol)

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

    @property
    def busy(self) -> frozenset[str]:
        return frozenset(self._busy)

    def heartbeat(self, now: datetime) -> None:
        cfg = self._cfg()
        if not cfg.keeper_assistant_on:
            self._rec.heartbeat(now, Health.OK, "已关闭（keeper_assistant_on = false）")
            return
        b = cfg.keeper_assistant_b_model or "没配置（只记录，不平仓不确认）"
        head = (
            f"A Jev / B {b}；正在问 {len(self._busy)} 个币，占 {self._slots.used}/"
            f"{cfg.keeper_assistant_concurrency} 个名额"
        )
        if self._jev is None:
            head = f"Jev 没接上，只记录；{head}"
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
        options: tuple[str, ...], why_missing: str,
    ) -> Verdict:  # fmt: skip
        if scorer is None:
            return Verdict(judge, None, None, options, why_missing, 0.0)
        cfg = self._cfg()
        question = move_question(symbol, want, cfg.keeper_assistant_horizon, options)
        await self._slots.acquire()
        try:
            return await scorer.ask(
                judge, symbol, state, question, options, cfg.keeper_assistant_timeout_s
            )
        finally:
            await self._slots.release()

    async def _judges(self, symbol: str, want: str) -> tuple[Verdict, Verdict] | str:
        """A、B 同时问「会不会往 ``want`` 走」。返回字符串 = 问不了的原因。"""
        f = self._features(symbol)
        if f is None:
            return "没有这个币的行情快照"
        state = direction_snapshot(symbol, f)
        b_model = self._cfg().keeper_assistant_b_model
        a, b = await asyncio.gather(
            self._one(self._jev, "A", symbol, state, want, OPTIONS, "Jev 没接上"),
            self._one(
                self._b_scorer(b_model),
                "B",
                symbol,
                state,
                want,
                tuple(reversed(OPTIONS)),
                "keeper_assistant_b_model 没配置" if not b_model else f"{b_model} 建不起来",
            ),
        )
        return a, b

    # ------------------------------------------------------------ 两件事
    async def _check(self, pos: PositionRecord, trigger: str, detail: str, now: datetime) -> None:
        sym = pos.symbol
        started = time.monotonic()
        want = "short" if pos.side is Side.LONG else "long"
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
        both = a.yes(cfg.keeper_assistant_min_p) and b.yes(cfg.keeper_assistant_min_p)
        act = both and cfg.keeper_assistant_close and not cfg.keeper_assistant_b_record_only
        reason = REASONS[trigger]
        closed = act and self._close(pos, reason)
        why = (
            reason
            if closed
            else "两个模型都答是，但这笔已在平或已平"
            if act
            else "两个模型都答是，但 B 只记录（keeper_assistant_b_record_only）"
            if both and cfg.keeper_assistant_b_record_only
            else "两个模型都答是，但 keeper_assistant_close 关着"
            if both
            else "两个模型没有都答是（或 p(是) 不够 / 有一个没答上），不动"
        )
        self._log(
            now,
            started,
            sym,
            Outcome.ACTION if closed else Outcome.INFO,
            f"{sym} {TRIGGER_TEXT[trigger]}，问会不会{MOVE[want]}：A {a.text} / B {b.text}，{why}",
            {
                "trigger": trigger,
                "trigger_detail": detail,
                "position_id": pos.position_id,
                "asked": MOVE[want],
                "both_yes": both,
                "closed": closed,
                "why": why,
            },
            (a, b),
            position_id=pos.position_id,
        )

    async def _flip_confirm(self, symbol: str, candidate: str, now: datetime) -> None:
        started = time.monotonic()
        got = await self._judges(symbol, candidate)
        text = DIR_TEXT[candidate]
        if isinstance(got, str):
            self._rec.record(
                now,
                Outcome.INFO,
                f"{symbol} 待确认翻转（{text}）没问成：{got}，照旧等下一场会",
                symbol=symbol,
                output={"keeper_assistant": {"trigger": "flip_confirm", "skipped": got}},
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
                candidate,
                f"管家助手两个模型都答会{MOVE[candidate]}",
                {"judges": [a.row(), b.row()]},
            )
            result = "confirmed" if ok else "stale"
        why = {
            "confirmed": "确认，卡上换成那场会的结论",
            "stale": "候选已变或那条指令已换掉，不动",
            "not_agreed": "两个模型没有都答是（或 p(是) 不够 / 有一个没答上），候选照旧等下一场会",
            "b_record_only": "B 只记录，候选照旧等下一场会",
        }[result]
        self._log(
            now,
            started,
            symbol,
            Outcome.ACTION if result == "confirmed" else Outcome.INFO,
            f"{symbol} 待确认翻转（{text}），问会不会{MOVE[candidate]}："
            f"A {a.text} / B {b.text}，{why}",
            {
                "trigger": "flip_confirm",
                "candidate": candidate,
                "both_yes": both,
                "result": result,
                "why": why,
            },
            (a, b),
        )

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
        if self._dir is not None:
            self._journal(row)

    def _journal(self, row: dict[str, Any]) -> None:
        """``data/keeper_assistant/YYYY-MM-DD.jsonl``：两个模型的 p(是)、模型、延迟和花费，
        之后按它算两个模型的校准和一致率。写不进去只记日志。"""
        assert self._dir is not None
        path = self._dir / f"{str(row['ts'])[:10]}.jsonl"
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            logger.exception("管家助手流水写入失败")


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
