"""拍板人和决策模型客户端：故障即关闭、不兜底、错误计数会清零、只在有卡时问。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx2
import pytest

from jiaoyi import contracts as c
from jiaoyi import interfaces as i
from jiaoyi.core.recorder import RoleRecorder
from jiaoyi.core.store import JsonlDecisionLog
from jiaoyi.gate.entry import JevEntryGate
from jiaoyi.gate.jev import (
    OPENROUTER_BASE_URL,
    JevDecisionModel,
    MockJevTransport,
    TypeSafeTransport,
    decision_model_from_config,
    jev_from_secrets,
)
from jiaoyi.gate.questions import (
    MAX_SNAPSHOT_CHARS,
    entry_questions,
    entry_snapshot,
    jev_log_fields,
)
from jiaoyi.gate.rule_model import RuleDecisionModel
from tests.fastloop_helpers import FAST, GOOD_LONG, answer_with, card, signal


def entry_request(now: datetime) -> c.JevRequest:
    sig = signal(now)
    return c.JevRequest(
        request_id="r1",
        kind="entry",
        symbol="BTCUSDT",
        ts=now,
        signal_id=sig.signal_id,
        snapshot=entry_snapshot(sig, card(now)),
        questions=entry_questions(),
    )


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def model(t: MockJevTransport, **kw: object) -> JevDecisionModel:
    return JevDecisionModel(t, FAST, timeout_ms=200, **kw)  # type: ignore[arg-type]


def test_interfaces() -> None:
    assert isinstance(model(MockJevTransport()), i.DecisionModel)
    assert isinstance(RuleDecisionModel(c.RunMode.PAPER), i.DecisionModel)
    assert isinstance(JevEntryGate(RuleDecisionModel(c.RunMode.PAPER)), i.EntryGate)


async def test_success_returns_probs_and_cost(now: datetime) -> None:
    t = MockJevTransport(answer=answer_with(GOOD_LONG))
    r = await model(t).ask(entry_request(now))
    assert r.ok and r.error is None
    assert r.answer("take") is not None and r.answer("take").top() == ("take", 0.75)  # type: ignore[union-attr]
    assert r.cost_usd > 0
    assert r.model_version == "mock-jev"


async def test_timeout_fails_closed(now: datetime) -> None:
    t = MockJevTransport(answer=answer_with(GOOD_LONG), delay_s=0.5)
    r = await model(t).ask(entry_request(now))
    assert not r.ok and r.answers == [] and "超时" in (r.error or "")


async def test_exception_fails_closed_without_fallback(now: datetime) -> None:
    t = MockJevTransport(fail=ConnectionError("boom"))
    r = await model(t).ask(entry_request(now))
    assert not r.ok and r.answers == []
    assert "ConnectionError" in (r.error or "")


@pytest.mark.parametrize(
    "bad",
    [
        {k: v for k, v in GOOD_LONG.items() if k != "size"},
        {**GOOD_LONG, "size": {"S": 0.5, "XL": 0.5}},
        {**GOOD_LONG, "take": {"take": 0.9, "skip": 0.5}},
    ],
)
async def test_bad_answers_fail_closed(
    now: datetime, bad: Mapping[str, Mapping[str, float]]
) -> None:
    def ans(state: str, qs: Sequence[c.JevQuestion]) -> Mapping[str, Mapping[str, float]]:
        return bad

    r = await model(MockJevTransport(answer=ans)).ask(entry_request(now))
    assert not r.ok and r.answers == []


async def test_error_counter_cools_down_and_resets_on_success(now: datetime) -> None:
    clock = Clock()
    t = MockJevTransport(fail=RuntimeError("x"))
    m = model(t, clock=clock, wall=lambda: now)
    for _ in range(FAST.jev_max_consecutive_errors):
        assert not (await m.ask(entry_request(now))).ok
    assert m.health == c.Health.DOWN
    calls = len(t.calls)
    r = await m.ask(entry_request(now))
    assert not r.ok and "冷却" in (r.error or "") and len(t.calls) == calls  # 冷却中不再调用

    clock.t += FAST.jev_error_cooldown_s + 1
    t.fail = None
    t.answer = answer_with(GOOD_LONG)
    assert (await m.ask(entry_request(now))).ok
    assert m.consecutive_errors == 0 and m.health == c.Health.OK


async def test_error_counter_resets_at_bangkok_midnight() -> None:
    wall = [datetime(2026, 10, 22, 16, 0, tzinfo=UTC)]  # 曼谷 23:00
    t = MockJevTransport(fail=RuntimeError("x"))
    m = model(t, clock=Clock(), wall=lambda: wall[0])
    for _ in range(FAST.jev_max_consecutive_errors - 1):
        await m.ask(entry_request(wall[0]))
    assert m.consecutive_errors == FAST.jev_max_consecutive_errors - 1
    wall[0] = datetime(2026, 10, 22, 17, 0, 1, tzinfo=UTC)  # 曼谷 10-23 00:00
    await m.ask(entry_request(wall[0]))
    assert m.consecutive_errors == 1  # 跨天清零后只算今天这一次，没有触发冷却
    assert m.health == c.Health.DEGRADED


async def test_entry_gate_without_valid_card_does_not_call_model(now: datetime) -> None:
    t = MockJevTransport(answer=answer_with(GOOD_LONG))
    gate = JevEntryGate(model(t))
    expired = card(now - timedelta(hours=3))
    r = await gate.evaluate(signal(now), expired, now)
    assert not r.ok and "作战卡" in (r.error or "") and t.calls == []

    other = signal(now).model_copy(update={"symbol": "ETHUSDT"})
    r = await gate.evaluate(other, card(now), now)
    assert not r.ok and t.calls == []


async def test_entry_gate_busy_skip(now: datetime) -> None:
    t = MockJevTransport(answer=answer_with(GOOD_LONG), delay_s=0.05)
    gate = JevEntryGate(model(t))
    a, b = await asyncio.gather(
        gate.evaluate(signal(now), card(now), now), gate.evaluate(signal(now), card(now), now)
    )
    assert a.ok and not b.ok and "作废" in (b.error or "")
    assert len(t.calls) == 1


async def test_entry_gate_logs(tmp_path: Path, now: datetime) -> None:
    log = JsonlDecisionLog(tmp_path)
    t = MockJevTransport(answer=answer_with(GOOD_LONG))
    gate = JevEntryGate(model(t), RoleRecorder(c.Role.GATE, log))
    await gate.evaluate(signal(now), card(now), now)
    t.fail = RuntimeError("down")
    await gate.evaluate(signal(now), card(now), now)
    recs = log.read_day(now.date().isoformat())
    assert [r.outcome for r in recs] == [c.Outcome.PASS, c.Outcome.ERROR]
    assert recs[0].cost_usd > 0 and recs[0].input["jev"]["snapshot"]
    assert recs[1].output["jev"]["answers"] == {}


def test_snapshot_is_compact_json(now: datetime) -> None:
    s = entry_snapshot(signal(now), card(now))
    assert len(s) <= MAX_SNAPSHOT_CHARS
    d = json.loads(s)
    assert d["side"] == "long" and d["card"]["bias"] == "long" and "book" in d
    assert len(s) / 4 < 400  # 粗估 token 数


async def test_rule_model_answers_every_question(now: datetime) -> None:
    r = await RuleDecisionModel(c.RunMode.PAPER).ask(entry_request(now))
    assert r.ok and {a.id for a in r.answers} == {q.value for q in c.EntryQuestion}
    bad = entry_request(now).model_copy(update={"snapshot": "not json"})
    assert not (await RuleDecisionModel(c.RunMode.PAPER).ask(bad)).ok


def test_rule_model_stop_width_follows_atr_period() -> None:
    """noise 按 ATR 周期换算：15 分钟 ATR 下正常波动选 mid，不会一律偏 narrow。"""

    def stop(period: int, rv: float, atr: float) -> str:
        m = RuleDecisionModel(c.RunMode.PAPER, atr_period_min=period)
        probs = m._entry({"side": "long", "rv5m_bps": rv, "atr_bps": atr})["stop_width"]
        return max(probs, key=lambda k: probs[k])

    # BTC 常见：10 秒波动约 1.8bp，15 分钟 ATR 约 22bp
    assert stop(15, 1.8, 22.0) == "mid"
    assert stop(15, 0.5, 22.0) == "narrow"
    assert stop(15, 5.0, 22.0) == "wide"
    assert stop(60, 1.8, 22.0) == "wide"  # 同样的 ATR 数值放在 60 分钟口径下算偏窄的 ATR


async def test_typesafe_transport_against_mock_http(now: datetime) -> None:
    seen: dict[str, object] = {}

    def handler(req: httpx2.Request) -> httpx2.Response:
        body = json.loads(req.content)
        seen["body"] = body
        seen["auth"] = req.headers.get("authorization")
        answers = {
            q: {
                "type": "choice",
                "choice": max(GOOD_LONG[q], key=lambda k: GOOD_LONG[q][k]),
                "confidence": 0.8,
                "probabilities": GOOD_LONG[q],
            }
            for q in body["questions"]
        }
        return httpx2.Response(
            200,
            headers={"x-typesafe-request-id": "req-123"},
            json={
                "model": "jev-2026-10",
                "answers": answers,
                "usage": {"input_tokens": 380, "output_tokens": 6},
            },
        )

    tr = TypeSafeTransport(
        "test-key-not-real", "jev-2026-10-01", http_transport=httpx2.MockTransport(handler)
    )
    m = JevDecisionModel(tr, FAST, timeout_ms=2000)
    r = await m.ask(entry_request(now))
    assert r.ok, r.error
    assert r.model_version == "jev-2026-10"
    assert r.cost_usd == pytest.approx(380 * FAST.jev_usd_per_mtok / 1e6)
    assert r.usage == c.JevUsage(input_tokens=380, output_tokens=6)  # 没报推理 token 记 0
    assert seen["auth"] == "Bearer test-key-not-real"
    await tr.aclose()


class _Secrets:
    def __init__(self, v: str | None, name: str = "JEV_API_KEY") -> None:
        self.v = v
        self.name = name

    def get(self, name: str) -> str | None:
        return self.v if name == self.name else None


PINNED = FAST.model_copy(update={"jev_model": "jev-2026-10-01"})


def test_no_key_or_unpinned_means_no_jev() -> None:
    assert jev_from_secrets(_Secrets(None), PINNED, timeout_ms=1000, day_reset_tz="UTC") is None
    assert jev_from_secrets(_Secrets("abc"), FAST, timeout_ms=1000, day_reset_tz="UTC") is None
    m = jev_from_secrets(_Secrets("abc"), PINNED, timeout_ms=1000, day_reset_tz="UTC")
    assert isinstance(m, JevDecisionModel)


def test_rule_gate_only_when_switched_on_in_paper() -> None:
    paper, live, testnet = c.RunMode.PAPER, c.RunMode.LIVE, c.RunMode.TESTNET
    kw = {"timeout_ms": 1000, "day_reset_tz": "UTC"}
    assert decision_model_from_config(_Secrets(None), FAST, paper, **kw) is None  # type: ignore[arg-type]
    on = FAST.model_copy(update={"rule_gate_without_jev": True})
    m = decision_model_from_config(_Secrets(None), on, paper, **kw)  # type: ignore[arg-type]
    assert isinstance(m, RuleDecisionModel)
    for mode in (testnet, live):
        assert decision_model_from_config(_Secrets(None), on, mode, **kw) is None  # type: ignore[arg-type]
    # 有 Jev 时开关不起作用
    pinned = on.model_copy(update={"jev_model": "jev-x"})
    m = decision_model_from_config(_Secrets("abc"), pinned, live, **kw)  # type: ignore[arg-type]
    assert isinstance(m, JevDecisionModel)


@pytest.mark.parametrize("mode", [c.RunMode.TESTNET, c.RunMode.LIVE])
def test_rule_model_refuses_real_money_modes(mode: c.RunMode) -> None:
    with pytest.raises(ValueError, match="模拟盘"):
        RuleDecisionModel(mode)


def _openrouter_handler(seen: dict[str, object]) -> object:
    def handler(req: httpx2.Request) -> httpx2.Response:
        body = json.loads(req.content)
        seen["url"] = str(req.url)
        seen["auth"] = req.headers.get("authorization")
        seen["encoding"] = req.headers.get("content-encoding")
        seen["model"] = body.get("model")
        answers = {
            q: {"type": "choice", "choice": "x", "confidence": 0.8, "probabilities": GOOD_LONG[q]}
            for q in body["questions"]
        }
        return httpx2.Response(  # OpenRouter 的样子：正文带 id、provider、usage.cost，没有请求号头
            200,
            json={
                "id": "gen-dec-abc",
                "provider": "TypeSafe",
                "model": "typesafe/jev-1.14-20261001",
                "answers": answers,
                "usage": {
                    "input_tokens": 380,
                    "output_tokens": 6,
                    "cost": 0.0000321,
                    "completion_tokens_details": {"reasoning_tokens": 2},
                },
            },
        )

    return handler


async def test_openrouter_transport(now: datetime, caplog: pytest.LogCaptureFixture) -> None:
    seen: dict[str, object] = {}
    tr = TypeSafeTransport(
        "test-key-not-real",
        "typesafe/jev-1.14",
        base_url=OPENROUTER_BASE_URL,
        http_transport=httpx2.MockTransport(_openrouter_handler(seen)),  # type: ignore[arg-type]
    )
    raw = await tr.evaluate(entry_request(now).snapshot, entry_request(now).questions, 2.0)
    assert seen["url"] == "https://openrouter.ai/api/v1/systemone"
    assert seen["auth"] == "Bearer test-key-not-real"
    assert seen["encoding"] is None and seen["model"] == "typesafe/jev-1.14"
    assert raw.request_id == "gen-dec-abc" and raw.cost_usd == pytest.approx(0.0000321)

    cfg = FAST.model_copy(update={"jev_model": "typesafe/jev-1.14", "jev_provider": "openrouter"})
    r = await JevDecisionModel(tr, cfg, timeout_ms=2000).ask(entry_request(now))
    assert r.ok and r.cost_usd == pytest.approx(0.0000321)  # 用通道报的花费，不按单价估
    assert r.usage == c.JevUsage(input_tokens=380, output_tokens=6, reasoning_tokens=2)
    _, out = jev_log_fields(entry_request(now), r)  # 面板 AI 费用详情读 output.usage
    assert out["usage"] == {"input_tokens": 380, "output_tokens": 6, "reasoning_tokens": 2}
    assert r.model_version == "typesafe/jev-1.14-20261001"
    assert "版本变了" not in caplog.text  # 带日期的全名算同一版本
    # 日期快照滚动了要提醒一次
    tr2 = TypeSafeTransport(
        "test-key-not-real",
        "typesafe/jev-1.14",
        base_url=OPENROUTER_BASE_URL,
        http_transport=httpx2.MockTransport(_openrouter_handler({})),  # type: ignore[arg-type]
    )
    m = JevDecisionModel(tr2, cfg, timeout_ms=2000)
    m._served_model = "typesafe/jev-1.14-20260917"
    assert (await m.ask(entry_request(now))).ok
    assert "重新校准" in caplog.text
    await tr2.aclose()
    await tr.aclose()


def test_provider_picks_its_own_key() -> None:
    kw = {"timeout_ms": 1000, "day_reset_tz": "UTC"}
    orc = PINNED.model_copy(update={"jev_provider": "openrouter"})
    # 只有 JEV_API_KEY 时不接 OpenRouter，反过来也一样
    assert jev_from_secrets(_Secrets("abc"), orc, **kw) is None  # type: ignore[arg-type]
    assert jev_from_secrets(_Secrets("abc", "OPENROUTER_API_KEY"), PINNED, **kw) is None  # type: ignore[arg-type]
    m = jev_from_secrets(_Secrets("abc", "OPENROUTER_API_KEY"), orc, **kw)  # type: ignore[arg-type]
    assert isinstance(m, JevDecisionModel)


def test_provider_config() -> None:
    with pytest.raises(ValueError):
        c.FastLoopConfig(jev_provider="other")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="latest"):
        c.FastLoopConfig(jev_provider="openrouter", jev_model="~typesafe/jev-latest")
    assert c.FastLoopConfig().jev_provider == "typesafe"


async def test_warmup_does_not_touch_error_counter() -> None:
    t = MockJevTransport()
    m = model(t)
    assert await m.warmup() is not None
    assert t.calls == ["warmup"]
    t.fail = ConnectionError("down")
    assert await m.warmup() is None  # 失败不抛、不影响启动
    assert m.consecutive_errors == 0 and m.health == c.Health.OK
