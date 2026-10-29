"""管家观测只补证据：冻结改动前请求，并独立验证诊断不能改变交易判断。"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from jiaoyi import contracts as c
from jiaoyi.gate.questions import hold_questions, template_dump
from jiaoyi.keeper.keeper import JevKeeper
from tests.fastloop_helpers import FAST, card, features
from tests.test_keeper_patrol import position

BASELINE = Path(__file__).parent / "fixtures" / "keeper_observation_before.json"
NO_GIVEBACK = FAST.model_copy(update={"giveback_on": False})


class _Model:
    """只在内存返回固定回答，记录真正送到模型边界的请求。"""

    def __init__(
        self, probabilities: list[float | str], on_response: Callable[[], None] | None = None
    ) -> None:
        self.probabilities = iter(probabilities)
        self.requests: list[c.JevRequest] = []
        self.responses: list[c.JevResponse] = []
        self.on_response = on_response

    async def ask(self, request: c.JevRequest) -> c.JevResponse:
        self.requests.append(request)
        result = next(self.probabilities)
        if isinstance(result, str):
            response = c.JevResponse(
                request_id=request.request_id,
                ok=False,
                error=result,
                model_version="observation-test",
                latency_ms=0,
            )
        else:
            response = c.JevResponse(
                request_id=request.request_id,
                ok=True,
                model_version="observation-test",
                latency_ms=0,
                answers=[
                    c.JevAnswer(id="thesis_intact", probs={"yes": 1 - result, "no": result}),
                    c.JevAnswer(id="adverse_risk", probs={"low": 1 - result, "high": result}),
                    c.JevAnswer(id="action", probs={"hold": 1 - result, "close": result}),
                ],
            )
        self.responses.append(response)
        if self.on_response is not None:
            self.on_response()
        return response


async def test_observation_preserves_frozen_before_request_and_verdict(now: datetime) -> None:
    """基线从原始 44ca954 keeper 捕获，逐字段、逐字比较，而非重算预期快照。"""
    baseline: dict[str, Any] = json.loads(BASELINE.read_text())
    model = _Model([0.9])
    keeper = JevKeeper(
        model,
        NO_GIVEBACK,
        lambda symbol: features(now, price=68068),
        lambda at: card(now),
    )
    with patch("jiaoyi.keeper.keeper.uuid.uuid4") as fixed_uuid:
        fixed_uuid.return_value.hex = "a1b2c3"
        verdict = await keeper.review(position(now), now)
    assert model.requests[0].model_dump(mode="json") == baseline["request"]
    assert verdict.model_dump(mode="json") == baseline["verdict"]
    template = template_dump()
    assert {"version": template["version"], "fingerprint": template["fingerprint"]} == baseline[
        "template"
    ]


async def test_response_observation_uses_value_visible_after_model(now: datetime) -> None:
    """模型等待期间报价会变；响应后必须再读，不能复用请求前的对象。"""
    events: list[str] = []
    state: dict[str, Any] = {"phase": "before", "wall": now + timedelta(seconds=1)}
    records: list[dict[str, Any]] = []

    def snapshot(symbol: str) -> c.FeatureSnapshot:
        events.append("features")
        return features(now, price=68068)

    def source(symbol: str, observed_at: datetime) -> dict[str, Any]:
        events.append(f"source:{state['phase']}")
        return {"phase": state["phase"], "observed_at": observed_at.isoformat()}

    def response_complete() -> None:
        events.append("model_complete")
        state.update(phase="after", wall=now + timedelta(seconds=2))

    def sink(record: dict[str, Any]) -> None:
        events.append("sink")
        records.append(record)

    model = _Model([0.9], on_response=response_complete)
    keeper = JevKeeper(
        model,
        NO_GIVEBACK,
        snapshot,
        lambda at: card(now),
        observation_source=source,
        observation_sink=sink,
        observation_wall=lambda: state["wall"],
    )
    verdict = await keeper.review(position(now), now)
    assert verdict.action == "close"
    assert events == ["features", "source:before", "model_complete", "source:after", "sink"]
    assert len(records) == 1
    record = records[0]
    assert record["request_view"]["phase"] == "before"
    assert record["post_response_view"]["phase"] == "after"
    assert record["post_response_view"]["observed_at"] == state["wall"].isoformat()
    assert record["response_completed_at"] == state["wall"].isoformat()
    assert record["attempt_count"] == 1 and record["model_ok"] is True


async def _run_case(
    now: datetime, case: str, fault: str | None
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    scenarios: dict[str, tuple[list[float | str], list[float | None]]] = {
        "hold": ([0.2], [68068]),
        "close": ([0.9], [68068]),
        "confirm": ([0.7, 0.7], [68068, 68068]),
        "retry": (["HTTP 529 system_overloaded", 0.9], [68068]),
        "failure": (["invalid response", 0.9], [68068]),
        "retry_failure": (["HTTP 529 system_overloaded"] * 2, [68068]),
        "guard": ([0.2], [68204, 68109]),
        "missing": ([], [None]),
    }
    probabilities, prices = scenarios[case]
    model = _Model(probabilities)
    records: list[dict[str, Any]] = []
    waits: list[float] = []
    snapshots = iter(prices)

    def snapshot(symbol: str) -> c.FeatureSnapshot | None:
        price = next(snapshots)
        return features(now, price=price) if price is not None else None

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    def source(symbol: str, observed_at: datetime) -> dict[str, Any]:
        if fault == "source":
            raise RuntimeError("诊断源故障")
        if fault == "malformed":
            return {"feature_ts": now.isoformat(), "price": None}
        return {"price": 99999, "event_ts": None, "observed_at": observed_at.isoformat()}

    def sink(record: dict[str, Any]) -> None:
        records.append(record)
        if fault == "sink":
            raise RuntimeError("诊断写入故障")

    def wall() -> datetime:
        if fault == "wall":
            raise RuntimeError("诊断时钟故障")
        return now

    diagnostics: dict[str, Any] = {}
    if fault is not None:
        diagnostics = {
            "observation_source": source,
            "observation_sink": sink,
            "observation_wall": wall,
        }
    keeper = JevKeeper(
        model,
        FAST if case == "guard" else NO_GIVEBACK,
        snapshot,
        lambda at: card(now),
        sleep=sleep,
        **diagnostics,
    )
    verdicts = []
    with patch("jiaoyi.keeper.keeper.uuid.uuid4") as fixed_uuid:
        fixed_uuid.return_value.hex = "a1b2c3"
        for _ in prices:
            verdicts.append((await keeper.review(position(now), now)).model_dump(mode="json"))
    return {
        "requests": [request.model_dump(mode="json") for request in model.requests],
        "responses": [response.model_dump(mode="json") for response in model.responses],
        "verdicts": verdicts,
        "waits": waits,
        "history": keeper.history("pos-1"),
    }, records


@pytest.mark.parametrize(
    "case", ["hold", "close", "confirm", "retry", "failure", "retry_failure", "guard", "missing"]
)
@pytest.mark.parametrize("fault", ["healthy", "source", "sink", "wall", "malformed"])
async def test_observation_and_diagnostic_faults_cannot_change_decisions(
    now: datetime, case: str, fault: str
) -> None:
    """包含回吐和故障分支：诊断失败不能增加模型请求、重试、等待或平仓。"""
    before, _ = await _run_case(now, case, None)
    after, records = await _run_case(now, case, fault)
    assert after == before
    if fault == "wall":
        assert len(records) <= len(after["verdicts"])
        return
    assert len(records) == len(after["verdicts"])
    if case == "retry":
        assert after["waits"] == [1.0]
        assert records[-1]["attempt_count"] == 2
    if case in {"guard", "missing"}:
        assert records[-1]["response_completed_at"] is None


@pytest.mark.parametrize(
    "case", ["hold", "close", "confirm", "retry", "failure", "retry_failure", "guard", "missing"]
)
async def test_every_observed_path_matches_original_keeper(now: datetime, case: str) -> None:
    """故障注入对照之外，路径本身也必须等于原始 keeper，而非仅等于新代码。"""
    baseline: dict[str, Any] = json.loads(BASELINE.read_text())
    expected = baseline["cases"][case]
    for request in expected["requests"]:
        seeded = hold_questions("direction", request["snapshot"])  # choice order follows it
        request["questions"] = [q.model_dump(mode="json") for q in seeded]
    current, _ = await _run_case(now, case, "healthy")
    assert current == expected


def test_mixed_kline_and_liquidation_clocks_do_not_freshen_price_or_stale_book(
    now: datetime,
) -> None:
    from jiaoyi.market.features import RollingFeatureEngine

    observed = now + timedelta(minutes=2)
    engine = RollingFeatureEngine(atr_period_min=1, observation_wall=lambda: observed)
    for i in range(15):
        start = now - timedelta(minutes=20 - i)
        engine.update(
            c.Kline(
                symbol="BTCUSDT",
                interval=c.Interval.M1,
                open_time=start,
                close_time=start + timedelta(seconds=59.999),
                open=100,
                high=101,
                low=99,
                close=100,
                volume=1,
            )
        )
    trade_at = now + timedelta(milliseconds=125)
    book_at = now + timedelta(seconds=1)
    engine.update(c.Trade(symbol="BTCUSDT", ts=trade_at, price=102, qty=1, taker_side=c.Side.LONG))
    engine.update(c.BookTop(symbol="BTCUSDT", ts=book_at, bids=[(101, 2)], asks=[(103, 3)]))
    engine.update(
        c.Liquidation(
            symbol="BTCUSDT", ts=now + timedelta(seconds=20), side=c.Side.LONG, price=104, qty=1
        )
    )
    kline_close = now + timedelta(seconds=59.999)
    engine.update(
        c.Kline(
            symbol="BTCUSDT",
            interval=c.Interval.M1,
            open_time=now,
            close_time=kline_close,
            open=105,
            high=106,
            low=104,
            close=105,
            volume=1,
        )
    )
    original = engine.snapshot("BTCUSDT")
    assert original is not None and original.ts == kline_close
    assert original.last_price == 102 and original.book is None
    evidence = engine.diagnostic_observation("BTCUSDT", observed)
    assert evidence["feature_ts"] == kline_close.isoformat()
    assert evidence["price"]["source"] == "trade"
    assert evidence["price"]["source_ts"] == trade_at.isoformat()
    assert evidence["price"]["value"] == 102
    assert evidence["price"]["age_s"] == pytest.approx(119.875)
    assert evidence["book"]["source_ts"] == book_at.isoformat()
    assert evidence["book"]["age_s"] == 119
    assert evidence["book"]["bid_qty"] == 2 and evidence["book"]["ask_qty"] == 3
    assert evidence["mark"] is None
    assert engine.snapshot("BTCUSDT") == original
