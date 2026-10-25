"""回放引擎：把历史行情按时间顺序灌进 market / sentinel / gate / risk 接口。

流程和实盘一致：行情 → 特征 → 哨兵出信号 → 拍板人问决策模型 → （等延迟）→ 风控员审 →
（等下单延迟）→ 纸面成交 → 每秒检查止损止盈。引擎本身不含任何业务判断，
全部通过注入的接口对象完成，所以真实现一做好就能替换假实现。

时间是模拟的：延迟只推进模拟时钟，不会真的 sleep。
"""

from __future__ import annotations

import heapq
import itertools
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime

from jiaoyi.backtest.report import ReplayReport, summarize
from jiaoyi.backtest.sim import ClosedTrade, LatencyModel, PaperAccount, event_ts
from jiaoyi.contracts import (
    BattleCard,
    CandidateSignal,
    DecisionRecord,
    JevResponse,
    MarketEvent,
    OrderIntent,
    RiskConfig,
    Role,
    Side,
    Trade,
)
from jiaoyi.contracts.decision_log import Outcome
from jiaoyi.interfaces import DecisionLog, EntryGate, FeatureEngine, RiskGuard, Trigger

CardSource = Callable[[datetime], BattleCard | None]


def static_card(card: BattleCard) -> CardSource:
    """整段回放用同一张作战卡（过期后返回 None，和实盘一样不再开新仓）。"""
    return lambda now: card if card.is_valid(now) else None


@dataclass(frozen=True)
class ReplayConfig:
    initial_equity: float = 10_000.0
    slippage_bps: float = 1.0
    """单边滑点。回放里偏保守：开仓和平仓都吃。"""
    exec_check_interval_s: float = 1.0
    """止损 / 止盈检查间隔，和 hummingbot 开仓执行器一致（每秒）。"""
    eval_interval_ms: int = 250
    """每个标的最多隔这么久评估一次哨兵，避免逐笔成交都跑一遍。"""
    max_fill_wait_s: float = 5.0
    """下单后超过这个时间还没有成交价（成交稀疏）就放弃，计入“未成交”。"""
    fee_bps: float | None = None
    """None = 用硬上限里的 taker_fee_bps。"""


@dataclass(order=True)
class _Pending:
    due: datetime
    seq: int
    signal: CandidateSignal = field(compare=False)
    jev: JevResponse | None = field(compare=False, default=None)
    intent: OrderIntent | None = field(compare=False, default=None)


class ReplayEngine:
    def __init__(
        self,
        *,
        features: FeatureEngine,
        trigger: Trigger,
        gate: EntryGate,
        guard: RiskGuard,
        cards: CardSource,
        risk: RiskConfig,
        log: DecisionLog,
        latency: LatencyModel | None = None,
        config: ReplayConfig | None = None,
    ) -> None:
        self._features = features
        self._trigger = trigger
        self._gate = gate
        self._guard = guard
        self._cards = cards
        self._risk = risk
        self._log = log
        self._lat = latency or LatencyModel()
        self._cfg = config or ReplayConfig()
        self._account = PaperAccount(
            self._cfg.initial_equity,
            fee_bps=self._cfg.fee_bps if self._cfg.fee_bps is not None else risk.taker_fee_bps,
            slippage_bps=self._cfg.slippage_bps,
            day_reset_tz=risk.day_reset_tz,
            leverage_cap=risk.max_leverage,
        )
        self._seq = itertools.count()
        self._rec = itertools.count(1)
        self._decisions: list[_Pending] = []
        self._fills: list[_Pending] = []
        self._last_eval: dict[str, datetime] = {}
        self._kinds: dict[str, str] = {}
        self._reasons: Counter[str] = Counter()
        self._m: Counter[str] = Counter()
        self._jev_latency_sum = 0.0
        self._last_ts: datetime | None = None

    @property
    def account(self) -> PaperAccount:
        return self._account

    # ---------------------------------------------------------------- 主循环
    async def run(self, events: Iterable[MarketEvent]) -> ReplayReport:
        for ev in events:
            ts = event_ts(ev)
            self._last_ts = ts
            if self._account.roll_day(ts):
                self._m["day_resets"] += 1
            await self._drain(ts)
            self._features.update(ev)
            if isinstance(ev, Trade):
                self._on_trade(ev)
                await self._evaluate(ev)
        await self._finish()
        return self._report()

    async def _drain(self, ts: datetime) -> None:
        """处理到期的“决策模型回复到了”事件。"""
        while self._decisions and self._decisions[0].due <= ts:
            self._review(heapq.heappop(self._decisions))

    def _on_trade(self, tr: Trade) -> None:
        for closed in self._account.check_exits(
            tr.symbol, tr.price, tr.ts, self._cfg.exec_check_interval_s
        ):
            self._record_close(closed)
        # 到点要成交的单：用该标的第一笔不早于下单到达时刻的成交价
        keep: list[_Pending] = []
        for item in self._fills:
            if item.signal.symbol == tr.symbol and item.due <= tr.ts:
                if (tr.ts - item.due).total_seconds() > self._cfg.max_fill_wait_s:
                    self._m["unfilled"] += 1
                    continue
                self._fill(item, tr)
            else:
                keep.append(item)
        self._fills = keep

    async def _evaluate(self, tr: Trade) -> None:
        last = self._last_eval.get(tr.symbol)
        if last and (tr.ts - last).total_seconds() * 1000 < self._cfg.eval_interval_ms:
            return
        self._last_eval[tr.symbol] = tr.ts
        snap = self._features.snapshot(tr.symbol)
        card = self._cards(tr.ts)
        if snap is None or card is None:
            return
        for sig in self._trigger.check(snap, card, tr.ts):
            self._m["triggers"] += 1
            self._kinds[sig.signal_id] = sig.kind.value
            self._emit(Role.SENTINEL, tr.ts, Outcome.INFO, sig.trigger_desc, sig)
            resp = await self._gate.evaluate(sig, card, tr.ts)
            self._m["jev_calls"] += 1
            self._jev_latency_sum += resp.latency_ms
            if not resp.ok:
                self._m["jev_failed"] += 1
            elif (take := resp.answer("take")) and take.top()[0] == "take":
                self._m["jev_take"] += 1
            self._emit(
                Role.GATE,
                tr.ts,
                Outcome.PASS if resp.ok else Outcome.ERROR,
                "决策模型已答复" if resp.ok else f"决策模型失败：{resp.error}",
                sig,
                latency_ms=resp.latency_ms,
                cost_usd=resp.cost_usd,
                model_version=resp.model_version,
                output=resp.model_dump(mode="json", exclude={"request_id"}),
            )
            due = tr.ts + self._lat.jev_delay(resp.latency_ms)
            heapq.heappush(self._decisions, _Pending(due, next(self._seq), sig, jev=resp))

    # ---------------------------------------------------------------- 风控员、成交、收尾
    def _review(self, item: _Pending) -> None:
        sig, jev, now = item.signal, item.jev, item.due
        assert jev is not None
        card = self._cards(now)
        market = self._features.snapshot(sig.symbol)  # 拍板回来那一刻的行情
        verdict = self._guard.review(sig, jev, card, self._account.state(now), now, market=market)
        if not verdict.approved or verdict.intent is None:
            self._m["risk_rejected"] += 1
            reason = verdict.reasons[0] if verdict.reasons else "未写原因"
            self._reasons[reason] += 1
            self._emit(
                Role.RISK_GUARD, now, Outcome.REJECT, "拒绝：" + "；".join(verdict.reasons), sig
            )
            return
        self._m["risk_approved"] += 1
        self._emit(
            Role.RISK_GUARD,
            now,
            Outcome.PASS,
            f"放行 {verdict.intent.side.value} 名义 {verdict.intent.notional_usdt:.0f}U",
            sig,
            output=verdict.intent.model_dump(mode="json"),
        )
        due = now + self._lat.order_delay()
        self._fills.append(_Pending(due, next(self._seq), sig, intent=verdict.intent))

    def _fill(self, item: _Pending, tr: Trade) -> None:
        assert item.intent is not None
        pos = self._account.open_position(item.intent, tr.price, tr.ts)
        self._m["filled"] += 1
        self._emit(
            Role.EXECUTOR,
            tr.ts,
            Outcome.ACTION,
            f"开仓成交 {pos.intent.symbol} {pos.intent.side.value} @ {pos.entry_price:.4f}",
            item.signal,
            position_id=pos.position_id,
        )
        # 实盘里开仓成交后要在币安挂保命止损；回放里记一条，验收脚本按同一套规矩检查
        lb = pos.intent.lifeboat_stop_pct / 100.0
        px = pos.entry_price * (1 - lb if pos.intent.side is Side.LONG else 1 + lb)
        self._emit(
            Role.LIFEBOAT,
            tr.ts,
            Outcome.ACTION,
            f"保命止损已挂 @ {px:.4f}",
            item.signal,
            position_id=pos.position_id,
            output={"stop_price": px},
        )

    def _record_close(self, t: ClosedTrade) -> None:
        self._emit(
            Role.EXECUTOR,
            t.exit_ts,
            Outcome.CLOSE,
            f"平仓 {t.symbol}（{t.reason}）净 {t.net_pnl:+.2f}U",
            None,
            symbol=t.symbol,
            signal_id=t.signal_id,
            position_id=t.position_id,
            output={"reason": t.reason, "gross": t.gross_pnl, "fees": t.fees, "net": t.net_pnl},
        )
        self._emit(
            Role.LIFEBOAT,
            t.exit_ts,
            Outcome.INFO,
            "保命止损已撤",
            None,
            symbol=t.symbol,
            signal_id=t.signal_id,
            position_id=t.position_id,
        )

    async def _finish(self) -> None:
        """回放结束：还在途的单算未成交，还没平的仓按最后价格强平并标注。"""
        self._m["unfilled"] += len(self._fills)
        self._fills.clear()
        self._decisions.clear()
        if self._last_ts is None:
            return
        for pid, pos in list(self._account.positions.items()):
            px = self._account.last_price.get(pos.intent.symbol, pos.entry_price)
            self._record_close(self._account.close_position(pid, px, self._last_ts, "replay_end"))

    def _report(self) -> ReplayReport:
        s = summarize(
            self._account.closed,
            self._kinds,
            self._reasons,
            self._cfg.initial_equity,
            self._account.equity(),
        )
        m = self._m
        return ReplayReport(
            initial_equity=self._cfg.initial_equity,
            triggers=m["triggers"],
            jev_calls=m["jev_calls"],
            jev_failed=m["jev_failed"],
            jev_take=m["jev_take"],
            risk_approved=m["risk_approved"],
            risk_rejected=m["risk_rejected"],
            filled=m["filled"],
            unfilled=m["unfilled"],
            day_resets=m["day_resets"],
            avg_jev_latency_ms=self._jev_latency_sum / m["jev_calls"] if m["jev_calls"] else 0.0,
            **s,
        )

    # ---------------------------------------------------------------- 记录
    def _emit(
        self,
        role: Role,
        ts: datetime,
        outcome: Outcome,
        summary: str,
        sig: CandidateSignal | None,
        *,
        symbol: str | None = None,
        signal_id: str | None = None,
        position_id: str | None = None,
        output: dict[str, object] | None = None,
        latency_ms: float = 0.0,
        cost_usd: float = 0.0,
        model_version: str | None = None,
    ) -> None:
        self._log.write(
            DecisionRecord(
                record_id=f"replay-{next(self._rec):06d}",
                ts=ts,
                role=role,
                symbol=sig.symbol if sig else symbol,
                signal_id=sig.signal_id if sig else signal_id,
                position_id=position_id,
                card_id=sig.card_id if sig else None,
                outcome=outcome,
                summary=summary,
                input={"trigger": sig.trigger_desc} if sig else {},
                output=output or {},
                latency_ms=latency_ms,
                cost_usd=cost_usd,
                model_version=model_version,
            )
        )
