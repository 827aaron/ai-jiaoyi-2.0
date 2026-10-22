"""决策模型客户端：Jev（TypeSafe AI 的 System One 接口），以后可换本地模型。

结构参考 buberlo/jev-trader 的 ``judgment/client.py`` + ``judgment/battery.py``
（https://github.com/buberlo/jev-trader ，提交 15d089e，MIT License，Copyright (c) 2026 buberlo）：
一次调用问完全部问题、锁定模型版本、记录延迟和请求号、不在 SDK 里重试。
和它不同的地方（设计文档附录 A 列的坑）：
- 出错不换公式兜底：返回 ``ok=False``，开仓一律不做、盯仓维持现状（故障即关闭）。
- 错误计数会清零：成功一次就归零、跨天（``day_reset_tz`` 0 点）归零；连续出错到上限后
  暂停一段时间（冷却），冷却结束计数清零再试。
- 只在哨兵触发或管家定时复查时才问，不轮询。

网络部分抽成 ``JevTransport``，测试用 ``MockJevTransport``，不需要真 key。
key 只从 ``SecretStore``（钥匙串 / 环境变量 ``JEV_API_KEY``）读，不进日志。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Protocol

from jiaoyi.contracts import (
    FastLoopConfig,
    Health,
    JevAnswer,
    JevQuestion,
    JevRequest,
    JevResponse,
    RunMode,
)
from jiaoyi.core.recorder import local_day
from jiaoyi.gate.questions import CRITERIA
from jiaoyi.interfaces import DecisionModel, SecretStore

logger = logging.getLogger(__name__)

JEV_KEY_NAME = "JEV_API_KEY"


@dataclass(frozen=True)
class RawJevResult:
    """传输层的原始结果：每个问题每个选项的概率。"""

    model: str
    probs: Mapping[str, Mapping[str, float]]
    input_tokens: int = 0
    request_id: str | None = None


class JevTransport(Protocol):
    async def evaluate(
        self, state: str, questions: Sequence[JevQuestion], timeout_s: float
    ) -> RawJevResult: ...


class TypeSafeTransport:
    """用 TypeSafe 官方 SDK（``typesafe-sdk``，MIT）调 Jev。SDK 内不重试，超时由外层控制。"""

    def __init__(self, api_key: str, model: str, *, http_transport: Any = None) -> None:
        # 延迟导入：只用规则版或 mock 时不加载网络库
        from typesafe_sdk import AsyncTypeSafeClient, RetryPolicy

        self._model = model
        self._client = AsyncTypeSafeClient(
            api_key=api_key,
            model=model,
            retry=RetryPolicy(max_retries=0),
            transport=http_transport,  # 测试时传 httpx2.MockTransport
        )

    async def evaluate(
        self, state: str, questions: Sequence[JevQuestion], timeout_s: float
    ) -> RawJevResult:
        from typesafe_sdk import Choice

        qs = {
            q.id: Choice(
                instructions=q.prompt,
                criteria={c: CRITERIA.get(q.id, {}).get(c) for c in q.choices},
            )
            for q in questions
        }
        resp = await self._client.system_one(state=state, questions=qs, timeout=timeout_s)
        probs = {name: dict(a.probabilities) for name, a in resp.choices.items()}
        try:
            rid: Any = resp.request_id
        except Exception:  # 响应没带请求号时 SDK 会抛错；请求号只用于排查，不影响判断
            rid = None
        return RawJevResult(
            model=resp.model,
            probs=probs,
            input_tokens=resp.usage.input_tokens or 0,
            request_id=rid if isinstance(rid, str) else None,
        )

    async def aclose(self) -> None:
        await self._client.aclose()


@dataclass
class MockJevTransport:
    """测试和模拟盘联调用：按给定概率作答，可以设延迟、设报错。"""

    answer: Callable[[str, Sequence[JevQuestion]], Mapping[str, Mapping[str, float]]] | None = None
    delay_s: float = 0.0
    fail: BaseException | None = None
    model: str = "mock-jev"
    calls: list[str] = field(default_factory=list)

    async def evaluate(
        self, state: str, questions: Sequence[JevQuestion], timeout_s: float
    ) -> RawJevResult:
        self.calls.append(state)
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.fail is not None:
            raise self.fail
        if self.answer is not None:
            probs = self.answer(state, questions)
        else:  # 默认平均分布
            probs = {q.id: {c: 1.0 / len(q.choices) for c in q.choices} for q in questions}
        return RawJevResult(model=self.model, probs=probs, input_tokens=len(state) // 4)


class JevDecisionModel:
    """``DecisionModel`` 的实现。从不抛异常：出任何错都返回 ``ok=False``。"""

    def __init__(
        self,
        transport: JevTransport,
        cfg: FastLoopConfig,
        *,
        timeout_ms: int,
        day_reset_tz: str = "Asia/Bangkok",
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._t = transport
        self._cfg = cfg
        self._timeout_s = timeout_ms / 1000.0
        self._tz = day_reset_tz
        self._clock = clock
        self._wall = wall
        self._day: date | None = None
        self.consecutive_errors = 0
        self._cooldown_until: float | None = None

    @property
    def health(self) -> Health:
        if self._cooldown_until is not None and self._clock() < self._cooldown_until:
            return Health.DOWN
        return Health.DEGRADED if self.consecutive_errors else Health.OK

    async def ask(self, request: JevRequest) -> JevResponse:
        day = local_day(self._wall(), self._tz)
        if day != self._day:  # 跨天：错误计数和冷却一起清零
            self._day = day
            self.consecutive_errors = 0
            self._cooldown_until = None
        now = self._clock()
        if self._cooldown_until is not None:
            if now < self._cooldown_until:
                left = self._cooldown_until - now
                return self._fail(request, f"Jev 连续出错，冷却中（还剩 {left:.0f} 秒）", 0.0)
            # 冷却结束：计数清零，重新试
            self._cooldown_until = None
            self.consecutive_errors = 0

        started = time.perf_counter()
        try:
            raw = await asyncio.wait_for(
                self._t.evaluate(request.snapshot, request.questions, self._timeout_s),
                timeout=self._timeout_s,
            )
        except TimeoutError:
            return self._error(request, f"Jev 超时（>{self._timeout_s * 1000:.0f}ms）", started)
        except Exception as e:  # 网络、鉴权、限流……一律故障即关闭
            return self._error(request, f"Jev 调用失败：{type(e).__name__}: {e}", started)
        latency = (time.perf_counter() - started) * 1000.0

        try:
            answers = _parse(request, raw)
        except ValueError as e:
            return self._error(request, f"Jev 回复解析失败：{e}", started)

        self.consecutive_errors = 0
        if self._cfg.jev_model is not None and raw.model != self._cfg.jev_model:
            logger.warning("Jev 模型版本变了：锁定 %s，实际 %s", self._cfg.jev_model, raw.model)
        return JevResponse(
            request_id=request.request_id,
            ok=True,
            model_version=raw.model or self._cfg.jev_model or "unpinned",
            latency_ms=latency,
            cost_usd=raw.input_tokens * self._cfg.jev_usd_per_mtok / 1e6,
            answers=answers,
        )

    def _error(self, request: JevRequest, msg: str, started: float) -> JevResponse:
        self.consecutive_errors += 1
        if self.consecutive_errors >= self._cfg.jev_max_consecutive_errors:
            self._cooldown_until = self._clock() + self._cfg.jev_error_cooldown_s
            msg += (
                f"；连续出错 {self.consecutive_errors} 次，暂停 {self._cfg.jev_error_cooldown_s} 秒"
            )
        logger.warning(msg)
        return self._fail(request, msg, (time.perf_counter() - started) * 1000.0)

    def _fail(self, request: JevRequest, msg: str, latency_ms: float) -> JevResponse:
        return JevResponse(
            request_id=request.request_id,
            ok=False,
            error=msg,
            model_version=self._cfg.jev_model or "unpinned",
            latency_ms=max(latency_ms, 0.0),
        )


def _parse(request: JevRequest, raw: RawJevResult) -> list[JevAnswer]:
    """每个问题都要有，且选项必须和问的完全一致；概率和偏差 2% 以内的归一化。"""
    out: list[JevAnswer] = []
    for q in request.questions:
        probs = raw.probs.get(q.id)
        if probs is None:
            raise ValueError(f"缺少问题 {q.id}")
        if set(probs) != set(q.choices):
            raise ValueError(
                f"问题 {q.id} 的选项 {sorted(probs)} 和请求 {sorted(q.choices)} 不一致"
            )
        total = sum(probs.values())
        if total <= 0 or any(p < 0 for p in probs.values()):
            raise ValueError(f"问题 {q.id} 的概率不合法")
        if abs(total - 1.0) > 0.02:
            raise ValueError(f"问题 {q.id} 的概率和为 {total:.3f}")
        out.append(JevAnswer(id=q.id, probs={c: probs[c] / total for c in q.choices}))
    return out


def jev_from_secrets(
    secrets: SecretStore, cfg: FastLoopConfig, *, timeout_ms: int, day_reset_tz: str
) -> JevDecisionModel | None:
    """有 ``JEV_API_KEY`` 且锁定了 ``jev_model`` 才建真 Jev，否则返回 None。"""
    key = secrets.get(JEV_KEY_NAME)
    if not key:
        return None
    if cfg.jev_model is None:
        logger.error("有 JEV_API_KEY 但没在 [fast_loop] 锁定 jev_model，不接 Jev")
        return None
    return JevDecisionModel(
        TypeSafeTransport(key, cfg.jev_model), cfg, timeout_ms=timeout_ms, day_reset_tz=day_reset_tz
    )


def decision_model_from_config(
    secrets: SecretStore,
    cfg: FastLoopConfig,
    mode: RunMode,
    *,
    timeout_ms: int,
    day_reset_tz: str,
) -> DecisionModel | None:
    """接线用：有 Jev 用 Jev；没有时模拟盘且 ``rule_gate_without_jev`` 打开才用规则版；
    否则 None（用桩，不开仓）。testnet / live 永远不会拿到规则版。

    这是启动时的一次性选择。Jev 运行中出错只会返回 ``ok=False``，不会切到规则版。
    """
    jev = jev_from_secrets(secrets, cfg, timeout_ms=timeout_ms, day_reset_tz=day_reset_tz)
    if jev is not None:
        return jev
    if cfg.rule_gate_without_jev and mode == RunMode.PAPER:
        from jiaoyi.gate.rule_model import RuleDecisionModel

        return RuleDecisionModel(mode)
    return None
