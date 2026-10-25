"""各接口的假实现。真实现没做好之前联调用，做好之后做对照用。

约定：假实现和桩一样“故障即关闭”，除非构造时明确要求别的行为。
"""

from __future__ import annotations

import json
import random
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path

from jiaoyi.contracts import (
    AccountState,
    Alert,
    BattleCard,
    CandidateSignal,
    DecisionRecord,
    JevRequest,
    JevResponse,
    LifeboatOrder,
    ModuleStatus,
    OrderIntent,
    PositionRecord,
)
from jiaoyi.contracts.position import CloseReason, LifeboatStatus, PositionStatus
from tests.support import builders as b


# ---------------------------------------------------------------- 决策模型（Jev）
class ScriptedDecisionModel:
    """由测试给定的函数决定回复，并记录收到的所有请求。"""

    def __init__(self, responder: Callable[[JevRequest], JevResponse] | None = None) -> None:
        self._responder = responder or (lambda req: b.jev_ok(request_id=req.request_id))
        self.requests: list[JevRequest] = []

    async def ask(self, request: JevRequest) -> JevResponse:
        self.requests.append(request)
        return self._responder(request)


class FailingDecisionModel:
    """永远 ok=False，模拟 Jev 挂了。"""

    def __init__(self, error: str = "模拟故障") -> None:
        self._error = error
        self.calls = 0

    async def ask(self, request: JevRequest) -> JevResponse:
        self.calls += 1
        return b.jev_failed(self._error, request.request_id)


class RuleDecisionModel:
    """回放用的 mock：按固定概率回答“做”，延迟和故障率可调，随机种子固定。

    不是真模型：它不看行情，所以回放结果只反映“门槛 + 手续费 + 延迟”的影响，
    不反映 Jev 的判断力。要评估判断力，用 ``RecordedDecisionModel`` 放真实录制答复。
    """

    def __init__(
        self,
        take_prob: float = 0.5,
        latency_ms: float = 350.0,
        fail_prob: float = 0.0,
        confidence: float = 0.8,
        seed: int = 3,
    ) -> None:
        self._p = take_prob
        self._lat = latency_ms
        self._fail = fail_prob
        self._conf = confidence
        self._rng = random.Random(seed)

    async def ask(self, request: JevRequest) -> JevResponse:
        if self._rng.random() < self._fail:
            return b.jev_failed("模拟超时", request.request_id)
        take = self._rng.random() < self._p
        # 方向跟着信号走（snapshot 里写了 long / short），这样“方向不一致”不会成为主要拒因
        direction = "down" if " short " in request.snapshot else "up"
        return b.jev_ok(
            {"take": "take" if take else "skip", "direction": direction},
            confidence=self._conf,
            latency_ms=self._lat,
            request_id=request.request_id,
        )


class RecordedDecisionModel:
    """读录制好的答复（JSONL，每行一个 JevResponse，``request_id`` 对应请求）。

    找不到对应录制的请求一律返回 ok=False（故障即关闭），并计入 ``misses``，
    这样“录制不全”会在回放汇总里表现为 Jev 失败，而不是悄悄放行。
    """

    def __init__(self, path: Path) -> None:
        self._by_id: dict[str, JevResponse] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = JevResponse.model_validate(json.loads(line))
                self._by_id[r.request_id] = r
        self.misses = 0

    async def ask(self, request: JevRequest) -> JevResponse:
        hit = self._by_id.get(request.request_id)
        if hit is None:
            self.misses += 1
            return b.jev_failed("录制中没有这条请求", request.request_id)
        return hit


# ---------------------------------------------------------------- 拍板人
class FakeEntryGate:
    """把信号打包成标准 6 问，问决策模型。异常也按 ok=False 返回（不外抛）。"""

    def __init__(self, model: object) -> None:
        self._model = model

    async def evaluate(
        self, signal: CandidateSignal, card: BattleCard, now: datetime
    ) -> JevResponse:
        req = b.entry_request(signal, now)
        ask: Callable[[JevRequest], Awaitable[JevResponse]] = self._model.ask  # type: ignore[attr-defined]
        try:
            return await ask(req)
        except Exception as e:
            return b.jev_failed(f"决策模型异常：{e}", req.request_id)


# ---------------------------------------------------------------- 记录与告警
class InMemoryDecisionLog:
    def __init__(self) -> None:
        self.records: list[DecisionRecord] = []
        self.statuses: list[ModuleStatus] = []

    def write(self, record: DecisionRecord) -> None:
        self.records.append(record)

    def heartbeat(self, status: ModuleStatus) -> None:
        self.statuses.append(status)


class RecordingAlerter:
    def __init__(self) -> None:
        self.alerts: list[Alert] = []

    async def send(self, alert: Alert) -> None:
        self.alerts.append(alert)


# ---------------------------------------------------------------- 执行
class FakeExecutorAdapter:
    """内存里的执行器：只接受风控员签发的 ``OrderIntent``，开仓后记录持仓。"""

    def __init__(self, equity: float = 10_000.0, now: datetime = b.T0) -> None:
        self.opened: list[OrderIntent] = []
        self.closed: list[tuple[str, CloseReason]] = []
        self._positions: dict[str, PositionRecord] = {}
        self._equity = equity
        self._now = now

    async def open(self, intent: OrderIntent) -> PositionRecord:
        if not isinstance(intent, OrderIntent) or intent.approved_by != "risk_guard":
            raise TypeError("执行器只接受风控员签发的 OrderIntent")
        self.opened.append(intent)
        rec = PositionRecord(
            position_id=f"pos-{intent.intent_id}",
            group_id=f"grp-{intent.intent_id}",
            intent_id=intent.intent_id,
            signal_id=intent.signal_id,
            card_id=intent.card_id,
            symbol=intent.symbol,
            category=intent.category,
            side=intent.side,
            status=PositionStatus.OPEN,
            quantity=intent.quantity,
            leverage=intent.leverage,
            time_limit_s=intent.time_limit_s,
            opened_at=self._now,
        )
        self._positions[rec.position_id] = rec
        return rec

    async def close(self, position_id: str, reason: CloseReason) -> None:
        self.closed.append((position_id, reason))
        self._positions.pop(position_id, None)

    async def positions(self) -> list[PositionRecord]:
        return list(self._positions.values())

    async def account(self) -> AccountState:
        return b.account(equity=self._equity, now=self._now, positions=len(self._positions))


class FakeLifeboatStop:
    """内存里的保命止损：place 后 fetch 能查到，cancel 后查不到。"""

    def __init__(self) -> None:
        self.orders: dict[str, LifeboatOrder] = {}

    async def place(self, position: PositionRecord, stop_price: float) -> LifeboatOrder:
        order = LifeboatOrder(
            exchange_order_id=f"ex-{position.position_id}",
            client_order_id=f"lb-{position.position_id}",
            stop_price=stop_price,
            status=LifeboatStatus.ACTIVE,
            updated_at=b.T0,
        )
        self.orders[position.position_id] = order
        return order

    async def cancel(self, position: PositionRecord) -> None:
        self.orders.pop(position.position_id, None)

    async def fetch(self, position: PositionRecord) -> LifeboatOrder | None:
        return self.orders.get(position.position_id)
