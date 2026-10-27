"""总盘旁路并发/重问隔离与窗口缺失，全部合成离线边界。"""

import asyncio
import threading
from datetime import UTC, datetime

import pytest

from jiaoyi.slow import market_audit
from jiaoyi.slow.binance_stats import _source_metadata
from jiaoyi.slow.market_audit import CURRENT_AUDIT, MarketAudit
from jiaoyi.slow.tradingagents_engine import _MarketDraft
from tests.test_market_observability_regression import _Collect, _draft, _engine, _Meter
from tests.test_slow_commander import entry


async def test_same_engine_concurrent_market_calls_keep_attempts_and_actual_retry_prompts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    barrier = threading.Barrier(2)
    counts: dict[str, int] = {}

    class LLM:
        def with_structured_output(self, schema):
            return self

        def bind(self, **kwargs):
            return self

        def invoke(self, prompt, config):
            key = "market_only" if "Judge the CURRENT" in prompt else "scheduled_market"
            counts[key] = counts.get(key, 0) + 1
            if counts[key] == 1:
                barrier.wait(timeout=2)
                return (
                    _MarketDraft(regime="无数字", regime_direction="中性")
                    if key == "market_only"
                    else _draft("无数字")
                )
            return (
                _MarketDraft(regime="1h 1涨2跌", regime_direction="中性")
                if key == "market_only"
                else _draft("1h 1涨2跌", ("AAUSDT", True, "量2倍"))
            )

    llm, sink = LLM(), _Collect()
    engine = _engine(llm)  # 同一真实engine实例，两个worker中的最后prompt不共享latest状态。
    monkeypatch.setattr(engine, "_meter", lambda key: _Meter())
    monkeypatch.setattr(engine, "_done", lambda key, meter: None)
    monkeypatch.setattr("jiaoyi.slow.tradingagents_engine.cost_handler", lambda meter: None)

    async def run(scope):
        token = CURRENT_AUDIT.set(MarketAudit(sink, {"scope": scope}))
        try:
            if scope == "market_only":
                return await asyncio.to_thread(
                    engine._refresh_market_sync, [entry("AAUSDT")], None, "brief-only"
                )
            return await asyncio.to_thread(
                engine._pick_run,
                [entry("AAUSDT")],
                1,
                None,
                None,
                _Meter(),
                [entry("AAUSDT"), entry("BBUSDT")],
                "brief-pick",
            )
        finally:
            CURRENT_AUDIT.reset(token)

    await asyncio.gather(run("scheduled_market"), run("market_only"))
    requests = [r for r in sink.rows if r["kind"] == "model_request"]
    assert len(requests) == 4 and counts == {"scheduled_market": 2, "market_only": 2}
    for scope in counts:
        rows = [r for r in requests if r["scope"] == scope]
        assert len({r["trace_id"] for r in rows}) == 1
        assert [r["attempt"] for r in rows] == [1, 2]
        assert rows[1]["prompt"].startswith(rows[0]["prompt"])
        assert rows[1]["prompt"] != rows[0]["prompt"]
    assert len({r["trace_id"] for r in requests}) == 2


def test_source_unknown_and_zero_volume_do_not_fake_a_price_clock() -> None:
    now = datetime(2026, 1, 1, 12, 30, tzinfo=UTC)
    row = [0, 100, 100, 100, 100, 0, 3_599_999, 0, 0, 0, 0]
    meta = _source_metadata({"closeTime": 1_000}, [row, row], row, now)
    assert meta["price_event_at"] is None
    assert meta["volume_ratio"]["branch"] == "default1.0_insufficient_baseline"
    assert _source_metadata({}, [[0]], None, now) == {}


def test_audit_clock_failure_still_calls_original_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(market_audit, "_clock", lambda: None)
    result = object()

    class LLM:
        def invoke(self, prompt, config):
            assert prompt == "unchanged"
            return result

    token = CURRENT_AUDIT.set(MarketAudit(_Collect(), {}))
    try:
        assert market_audit.invoke_market(LLM(), "unchanged", None) is result
    finally:
        CURRENT_AUDIT.reset(token)
