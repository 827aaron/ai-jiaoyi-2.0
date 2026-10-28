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
两条通道（``FastLoopConfig.jev_provider``）：直连 TypeSafe，或经 OpenRouter 转发
（同一个 SDK 换 base URL，见 https://openrouter.ai/docs/guides/community/typesafe-sdk ）。
key 只从 ``SecretStore``（钥匙串 / 环境变量 ``JEV_API_KEY``、``OPENROUTER_API_KEY``）读，
不进日志。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Literal, Protocol

from jiaoyi.contracts import (
    FastLoopConfig,
    Health,
    JevAnswer,
    JevQuestion,
    JevRequest,
    JevResponse,
    JevUsage,
    RunMode,
)
from jiaoyi.core.recorder import local_day
from jiaoyi.gate.questions import CRITERIA
from jiaoyi.interfaces import DecisionModel, SecretStore

logger = logging.getLogger(__name__)

JEV_KEY_NAME = "JEV_API_KEY"
OPENROUTER_KEY_NAME = "OPENROUTER_API_KEY"
OPENROUTER_BASE_URL = "https://openrouter.ai/api"  # SDK 自己拼 /v1/systemone
PROVIDER_KEYS = {"typesafe": JEV_KEY_NAME, "openrouter": OPENROUTER_KEY_NAME}


@dataclass(frozen=True)
class RawJevResult:
    """传输层的原始结果：每个问题每个选项的概率。"""

    model: str
    probs: Mapping[str, Mapping[str, float]]
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    request_id: str | None = None
    cost_usd: float | None = None
    """通道直接报的花费（OpenRouter 的 ``usage.cost``）；没有时按 token 单价估。"""


class JevTransport(Protocol):
    async def evaluate(
        self, state: str, questions: Sequence[JevQuestion], timeout_s: float
    ) -> RawJevResult: ...


class TypeSafeTransport:
    """用 TypeSafe 官方 SDK（``typesafe-sdk``，MIT）调 Jev。SDK 内不重试，超时由外层控制。"""

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        base_url: str | None = None,
        http_transport: Any = None,
    ) -> None:
        # 延迟导入：只用规则版或 mock 时不加载网络库
        from typesafe_sdk import AsyncTypeSafeClient, RetryPolicy

        self._model = model
        self._client = AsyncTypeSafeClient(
            api_key=api_key,
            model=model,
            retry=RetryPolicy(max_retries=0),
            transport=http_transport,  # 测试时传 httpx2.MockTransport
            base_url=base_url,
            # 走转发时不压缩请求体：OpenRouter 文档没说收 zstd；请求本来就只有几百 token
            compression=False if base_url else None,
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
        body = _raw_body(resp)
        if not isinstance(rid, str):  # OpenRouter 把请求号放在正文 id（gen-dec-…）
            rid = body.get("id")
        usage = body.get("usage")
        cost = usage.get("cost") if isinstance(usage, dict) else None
        return RawJevResult(
            model=resp.model,
            probs=probs,
            input_tokens=resp.usage.input_tokens or 0,
            output_tokens=resp.usage.output_tokens or 0,
            reasoning_tokens=_reasoning_tokens(usage),
            request_id=rid if isinstance(rid, str) else None,
            cost_usd=float(cost) if isinstance(cost, int | float) and cost >= 0 else None,
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
        self._served_model: str | None = None

    def with_own_errors(self) -> JevDecisionModel:
        """Same transport, config and clocks, but its own error counter and cooldown, so one
        caller's failures cannot put the other into cooldown. Closing the transport stays with
        the original instance."""
        return JevDecisionModel(
            self._t,
            self._cfg,
            timeout_ms=round(self._timeout_s * 1000),
            day_reset_tz=self._tz,
            clock=self._clock,
            wall=self._wall,
        )

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
        pinned = self._cfg.jev_model
        # OpenRouter 回的是带日期的全名（typesafe/jev-1.14-20260917），算同一版本
        if pinned is not None and raw.model != pinned and not raw.model.startswith(pinned + "-"):
            logger.warning("Jev 模型版本变了：锁定 %s，实际 %s", pinned, raw.model)
        elif self._served_model is not None and raw.model != self._served_model:
            # 同一版本下带日期的快照滚动了（OpenRouter 会这样），门槛可能要重新校准
            logger.warning(
                "Jev 实际模型从 %s 换成了 %s，建议复盘后重新校准门槛", self._served_model, raw.model
            )
        self._served_model = raw.model
        return JevResponse(
            request_id=request.request_id,
            ok=True,
            model_version=raw.model or self._cfg.jev_model or "unpinned",
            latency_ms=latency,
            cost_usd=raw.cost_usd
            if raw.cost_usd is not None
            else raw.input_tokens * self._cfg.jev_usd_per_mtok / 1e6,
            usage=JevUsage(
                input_tokens=raw.input_tokens,
                output_tokens=raw.output_tokens,
                reasoning_tokens=raw.reasoning_tokens,
            ),
            answers=answers,
        )

    async def warmup(self, timeout_s: float = 5.0) -> float | None:
        """启动时发一次很小的探测请求，把 DNS、TLS 握手和连接池先建好，免得第一笔真信号吃冷启动
        （M1 实测首次 963ms，之后约 500ms）。返回延迟毫秒；失败返回 None。

        探测不算拍板：不进决策日志、不动错误计数和冷却，失败只记一行日志。
        连接空闲超过约 30 秒（SDK 连接池的保活时间）会重新握手，所以这只管启动后第一笔。
        """
        started = time.perf_counter()
        try:
            await asyncio.wait_for(
                self._t.evaluate("warmup", _WARMUP_QUESTIONS, timeout_s), timeout=timeout_s
            )
        except Exception as e:  # 包括超时；预热失败不影响启动，真信号来了照常按超时规则走
            logger.warning("Jev 预热失败：%s: %s", type(e).__name__, e)
            return None
        ms = (time.perf_counter() - started) * 1000.0
        logger.info("Jev 预热完成，%.0f ms", ms)
        return ms

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


_BUSY = re.compile(
    r"\b5\d\d\b|overload|InternalServerError|ServiceUnavailable|BadGateway|Bad Gateway",
    re.IGNORECASE,
)


def error_kind(error: str | None) -> Literal["busy", "timeout", "cooldown", "other"]:
    """Jev 失败按类型分：busy = 服务方繁忙（5xx / 529 过载），timeout = 超时，cooldown = 我们自己
    连续出错后的冷却，other = 鉴权、解析失败等。"""
    msg = error or ""
    if "冷却中" in msg:
        return "cooldown"
    if "超时" in msg or "Timeout" in msg:
        return "timeout"
    if _BUSY.search(msg):
        return "busy"
    return "other"


_WARMUP_QUESTIONS = (
    JevQuestion(id="warmup", prompt="Connection check. Pick either option.", choices=("a", "b")),
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


def _reasoning_tokens(usage: Any) -> int:
    """推理 token：OpenRouter 放在 ``completion_tokens_details.reasoning_tokens``，
    有的通道放在 ``output_tokens_details`` 或直接 ``reasoning_tokens``。都没有记 0。"""
    if not isinstance(usage, dict):
        return 0
    for key in ("completion_tokens_details", "output_tokens_details"):
        d = usage.get(key)
        if isinstance(d, dict) and isinstance(d.get("reasoning_tokens"), int):
            return max(int(d["reasoning_tokens"]), 0)
    n = usage.get("reasoning_tokens")
    return max(n, 0) if isinstance(n, int) else 0


def _raw_body(resp: Any) -> dict[str, Any]:
    """SDK 丢掉了不认识的字段（OpenRouter 加的 id、usage.cost），从原始响应里取。取不到就算了。"""
    try:
        body = resp.raw_http_response.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def jev_from_secrets(
    secrets: SecretStore, cfg: FastLoopConfig, *, timeout_ms: int, day_reset_tz: str
) -> JevDecisionModel | None:
    """所选通道的 key 有了、且锁定了 ``jev_model`` 才建真 Jev，否则返回 None。

    直连用 ``JEV_API_KEY``，OpenRouter 用 ``OPENROUTER_API_KEY``；不会拿另一条通道的 key 顶替。
    """
    key_name = PROVIDER_KEYS[cfg.jev_provider]
    key = secrets.get(key_name)
    if not key:
        return None
    if cfg.jev_model is None:
        logger.error("有 %s 但没在 [fast_loop] 锁定 jev_model，不接 Jev", key_name)
        return None
    base_url = OPENROUTER_BASE_URL if cfg.jev_provider == "openrouter" else None
    return JevDecisionModel(
        TypeSafeTransport(key, cfg.jev_model, base_url=base_url),
        cfg,
        timeout_ms=timeout_ms,
        day_reset_tz=day_reset_tz,
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

        return RuleDecisionModel(mode, atr_period_min=cfg.atr_period_min)
    return None
