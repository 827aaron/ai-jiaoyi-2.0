"""独立回归：只用合成输入，不提交本地历史行情或真实模型请求。"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from jiaoyi.slow import market_audit, market_table
from jiaoyi.slow.commander import CardRefreshError
from jiaoyi.slow.market_audit import CURRENT_AUDIT, MarketAudit, invoke_market
from jiaoyi.slow.market_log import MarketLog
from jiaoyi.slow.tradingagents_engine import _MarketDraft
from tests.test_market_meeting import _draft, _engine, _FakeLLM, _Meter
from tests.test_market_refresh_independent import research
from tests.test_regime_flip import bar
from tests.test_regime_flip_loop import finish, setup
from tests.test_slow_commander import entry


def _counts(text: str) -> dict[str, int]:
    """读取用户可见的四类计数，避免只测试中间辅助函数。"""
    found = dict(re.findall(r"\b(up|down|flat|missing)\s+(\d+)", text))
    return {key: int(value) for key, value in found.items()}


@pytest.mark.parametrize(
    "values,expected",
    [
        ([0.0, -0.0, 0.0], {"up": 0, "down": 0, "flat": 3, "missing": 0}),
        (
            [None, float("nan"), float("inf"), float("-inf")],
            {"up": 0, "down": 0, "flat": 0, "missing": 4},
        ),
        ([1.5, -2.0, 0.0, None], {"up": 1, "down": 1, "flat": 1, "missing": 1}),
        ([1e-12, -1e-12, -0.0, None], {"up": 1, "down": 1, "flat": 1, "missing": 1}),
    ],
)
def test_breadth_preserves_flat_and_missing_denominator(
    values: list[float | None], expected: dict[str, int]
) -> None:
    # 极小正负变化虽然显示时可能舍入成 0.0，分类仍使用原始数值。
    actual = _counts(market_table._breadth(values))
    assert actual == expected
    assert sum(actual.values()) == len(values)


def test_full_breadth_render_preserves_metrics_and_window_specific_missing() -> None:
    pool = [
        entry("AAUSDT").model_copy(
            update={"metrics": {"chg_1h": 0.0, "chg_4h": 1.25, "chg_24h": -2.5}}
        ),
        entry("BBUSDT").model_copy(update={"metrics": {"chg_1h": -0.5, "chg_4h": 0.0}}),
        entry("CCUSDT").model_copy(update={"metrics": {"chg_1h": 0.75, "chg_24h": 0.0}}),
    ]
    original = copy.deepcopy([p.model_dump() for p in pool])
    text = market_table.breadth_line(pool)
    windows = text.split("; ")
    assert len(windows) == 3
    assert [_counts(window) for window in windows] == [
        {"up": 1, "down": 1, "flat": 1, "missing": 0},
        {"up": 1, "down": 0, "flat": 1, "missing": 1},
        {"up": 0, "down": 1, "flat": 1, "missing": 1},
    ]
    assert [p.model_dump() for p in pool] == original


def test_nonfinite_row_numbers_are_unknown_without_mutating_source_metrics() -> None:
    metrics = {
        "chg_1h": float("nan"),
        "chg_4h": float("inf"),
        "taker_buy_1h": float("-inf"),
        "vol_ratio_1h": float("nan"),
        "move_hold": float("nan"),
        "liq_1h": float("inf"),
        "liq_share": float("nan"),
        "fee_x": float("inf"),
        "rank": float("nan"),
        "pool_n": float("inf"),
    }
    source = entry("AAUSDT").model_copy(update={"metrics": metrics})
    before = json.dumps(source.metrics, sort_keys=True)
    row = market_table.format_row(source, "4h")
    assert re.search(r"\b(?:nan|inf)\b", row, re.IGNORECASE) is None
    assert "n/a" in row
    assert json.dumps(source.metrics, sort_keys=True) == before


def test_unknown_fee_multiple_is_not_relabelled_as_measured_zero() -> None:
    source = entry("AAUSDT").model_copy(
        update={"metrics": {"move_hold": 1.2, "fee_x": float("inf")}}
    )
    row = market_table.format_row(source, "4h")
    assert "0x fee" not in row and "n/a" in row


class _Collect:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def write_diagnostic(self, row: dict[str, Any]) -> None:
        self.rows.append(copy.deepcopy(row))


def test_scheduled_audit_captures_actual_free_candidates_and_full_breadth() -> None:
    free, held, pinned = entry("AAUSDT"), entry("BBUSDT"), entry("CCUSDT", pinned=True)
    draft = _draft("1h 1 涨 / 2 跌", ("AAUSDT", True, "量能 2 倍，4h 涨 1%"))
    llm, sink = _FakeLLM([draft]), _Collect()
    audit = MarketAudit(sink, {"pool_n": 3, "pool_requested_at": "synthetic request clock"})
    token = CURRENT_AUDIT.set(audit)
    try:
        result = _engine(llm)._pick_run(
            [free], 1, None, None, _Meter(), [free, held, pinned], "synthetic brief unchanged"
        )
    finally:
        CURRENT_AUDIT.reset(token)
    requests = [row for row in sink.rows if row["kind"] == "model_request"]
    assert len(llm.prompts) == len(requests) == 1 and not result.retried
    request = requests[0]
    assert request["prompt"] == llm.prompts[0]
    assert request["prompt_original_sha256"] == hashlib.sha256(llm.prompts[0].encode()).hexdigest()
    assert request["prompt_stored_sha256"] == request["prompt_original_sha256"]
    assert request["prompt_replay_exact"] is True
    assert request["table_symbols"] == ["AAUSDT"]
    assert request["breadth_symbols"] == ["AAUSDT", "BBUSDT", "CCUSDT"]
    assert "AAUSDT tier=" in request["prompt"]
    assert "BBUSDT tier=" not in request["prompt"] and "CCUSDT tier=" not in request["prompt"]
    assert "synthetic brief unchanged" in request["prompt"]
    assert '"status": "unknown"' in request["prompt"]
    response = next(row for row in sink.rows if row["kind"] == "model_response")
    assert response["trace_id"] == request["trace_id"] == audit.trace_id
    assert response["attempt"] == request["attempt"] == 1
    assert response["response_completed_at"] >= request["request_started_at"]


def test_market_only_audit_records_every_supplied_pool_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = [entry("AAUSDT"), entry("BBUSDT"), entry("CCUSDT", pinned=True)]
    draft = _MarketDraft(regime="1h 1 涨 / 2 跌", regime_direction="中性")
    llm, sink = _FakeLLM([draft]), _Collect()  # type: ignore[list-item]
    engine = _engine(llm)
    monkeypatch.setattr(engine, "_meter", lambda key: _Meter())
    monkeypatch.setattr(engine, "_done", lambda key, meter: None)
    monkeypatch.setattr("jiaoyi.slow.tradingagents_engine.cost_handler", lambda meter: None)
    token = CURRENT_AUDIT.set(MarketAudit(sink, {"pool_n": 3}))
    try:
        result = engine._refresh_market_sync(pool, None, "same synthetic brief")
    finally:
        CURRENT_AUDIT.reset(token)
    request = next(row for row in sink.rows if row["kind"] == "model_request")
    assert request["scope"] == "market_only"
    assert request["table_symbols"] == request["breadth_symbols"] == [p.symbol for p in pool]
    assert all(f"{p.symbol} tier=" in request["prompt"] for p in pool)
    assert request["prompt"] == llm.prompts[0] and len(llm.prompts) == 1
    assert result.picks == [] and result.cost_usd == _Meter.cost


def test_failing_diagnostics_preserve_one_original_model_call_and_return() -> None:
    class Broken:
        def write_diagnostic(self, row: dict[str, Any]) -> None:
            raise OSError("synthetic disk unavailable")

    draft = _draft("1h 1 涨 / 2 跌", ("AAUSDT", True, "量能 2 倍，4h 涨 1%"))
    llm = _FakeLLM([draft])
    token = CURRENT_AUDIT.set(MarketAudit(Broken(), {}))
    try:
        assert invoke_market(llm, "exact synthetic input", None) is draft
    finally:
        CURRENT_AUDIT.reset(token)
    assert llm.prompts == ["exact synthetic input"]


def test_failed_model_call_records_failure_without_retry_or_exception_replacement() -> None:
    failure = RuntimeError("synthetic model failure")
    calls = []

    class Failing:
        def invoke(self, prompt: str, config: Any = None) -> None:
            calls.append(prompt)
            raise failure

    sink = _Collect()
    token = CURRENT_AUDIT.set(MarketAudit(sink, {}))
    try:
        with pytest.raises(RuntimeError) as captured:
            invoke_market(Failing(), "exact failed input", None)
    finally:
        CURRENT_AUDIT.reset(token)
    assert captured.value is failure and calls == ["exact failed input"]
    assert [row["kind"] for row in sink.rows] == ["model_request", "model_response"]
    assert sink.rows[-1]["status"] == "failed"
    assert sink.rows[-1]["error_type"] == "RuntimeError"


async def test_stale_snapshot_logs_failed_workflow_without_model_call(
    tmp_path: Path, now: datetime, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, cards, engine, clock, _ = await setup(tmp_path, now)
    sink = _Collect()
    monkeypatch.setattr(cards._market_log, "write_diagnostic", sink.write_diagnostic)

    async def stale_snapshot(pool, at):
        return pool

    cards.set_market_snapshot(stale_snapshot)
    old_view = cards.latest_market_view
    clock[0] += timedelta(minutes=5)
    with pytest.raises(CardRefreshError, match="2 分钟"):
        await cards.refresh_market(runner.app.pool)
    assert engine.market_calls == 0 and cards.latest_market_view == old_view
    assert [row["kind"] for row in sink.rows] == ["request_accepted", "workflow_failed"]
    assert sink.rows[0]["trace_id"] == sink.rows[1]["trace_id"]
    assert sink.rows[1]["model_call_observed"] is False


async def test_superseded_scheduled_result_does_not_invent_publication_time(
    tmp_path: Path, now: datetime, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = MarketLog(tmp_path / "market_rounds")
    commander, engine, clock, candidates, _ = await research(tmp_path, now, market_log=log)
    sink = _Collect()
    monkeypatch.setattr(log, "write_diagnostic", sink.write_diagnostic)
    engine.pick_started.clear()
    engine.pick_release.clear()
    clock[0] += timedelta(minutes=5)
    earlier = asyncio.create_task(commander.refresh(candidates, "manual"))
    try:
        await asyncio.wait_for(engine.pick_started.wait(), 1)
        clock[0] += timedelta(minutes=5)
        newest = await commander.refresh_market(candidates)
    finally:
        engine.pick_release.set()
        await earlier
    assert commander.latest_market_view == newest
    rounds = [
        json.loads(line)
        for path in (tmp_path / "market_rounds").glob("*.jsonl")
        for line in path.read_text().splitlines()
    ]
    delayed = next(row for row in rounds if row["refresh_reason"] == "manual")
    superseded = next(row for row in sink.rows if row["kind"] == "superseded")
    assert delayed["trace_id"] == superseded["trace_id"]
    assert delayed["published_at"] is None and delayed["market_view"] is None
    assert delayed["publish_accepted"] is False and delayed["publish_status"] == "superseded"
    assert superseded["published_at"] is None


def test_slow_diagnostic_disk_does_not_block_submission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    market_audit.enqueue_diagnostic(tmp_path / "initial", {"kind": "start_worker"})
    assert market_audit.flush_diagnostics()
    entered, release = threading.Event(), threading.Event()

    def slow(root: Path, row: dict[str, Any]) -> None:
        entered.set()
        assert release.wait(2)

    monkeypatch.setattr(market_audit, "write_diagnostic", slow)
    log = MarketLog(tmp_path / "market")
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        call = executor.submit(log.write_diagnostic, {"kind": "synthetic"})
        assert entered.wait(1)
        # 磁盘仍被故意阻塞，调用线程应已返回。
        assert call.result(timeout=1) is None
    finally:
        release.set()
        executor.shutdown(wait=True)
        assert market_audit.flush_diagnostics()


@pytest.mark.parametrize("diagnostic_failure", [False, True])
@pytest.mark.parametrize("reset_kind", ["quiet", "missing"])
async def test_quiet_audit_preserves_confirmation_reset_and_one_model_call(
    tmp_path: Path,
    now: datetime,
    monkeypatch: pytest.MonkeyPatch,
    diagnostic_failure: bool,
    reset_kind: str,
) -> None:
    runner, cards, engine, clock, _ = await setup(tmp_path, now)
    deadline = cards._scheduled_due
    phase = ["pass"]
    checks: list[dict[str, Any]] = []

    async def fetch(symbol: str, start: datetime, end: datetime):
        if phase[0] == "missing":
            return []
        if phase[0] == "quiet":
            px = 100.2 if symbol == "BTCUSDT" else (101 if symbol[1] <= "E" else 99)
        else:
            px = 102
        return [bar(symbol, end - timedelta(hours=1), 100), bar(symbol, end, px)]

    def observe(row: dict[str, Any]) -> None:
        checks.append(copy.deepcopy(row))
        if diagnostic_failure:
            raise OSError("synthetic diagnostic failure")

    monkeypatch.setattr(runner, "_fetch_regime", fetch)
    monkeypatch.setattr(runner._regime_journal, "audit_check", observe)
    counts = []
    for step in ["pass", reset_kind, "pass", "pass"]:
        phase[0] = step
        clock[0] += timedelta(minutes=5)
        await runner.regime_flip_tick()
        counts.append(runner._regime.count)
    await finish(runner)
    assert counts == [1, 0, 1, 2]
    assert len(checks) == 4
    assert checks[1]["result_kind"] == ("quiet" if reset_kind == "quiet" else "blocked")
    if reset_kind == "missing":
        assert checks[1]["blocked_reason"] == "数据不足"
    assert checks[1]["confirm_count_before"] == 1 and checks[1]["confirm_count_after"] == 0
    assert engine.pick_calls == 1 and engine.market_calls == 1
    assert cards._scheduled_due == deadline
