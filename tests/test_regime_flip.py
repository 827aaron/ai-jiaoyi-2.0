from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from jiaoyi.contracts import FastLoopConfig, Interval, Kline, Role
from jiaoyi.core.hotconfig import patch_inplace
from jiaoyi.core.recorder import RoleRecorder
from jiaoyi.core.store import JsonlDecisionLog
from jiaoyi.market.rest import RateLimited
from jiaoyi.sentinel.regime_flip import (
    Direction,
    FlipJournal,
    RegimeFlip,
    RegimeReference,
    close_at,
    snapshot,
)
from jiaoyi.slow.tradingagents_engine import _PickDraft, pick_result


def reference(now: datetime, direction: Direction = "看跌") -> RegimeReference:
    return RegimeReference(
        ts=now, direction=direction, btc_px=100, regime_text="原始文字不用于解析"
    )


def rule(now: datetime, direction: Direction = "看跌", **kw: object) -> RegimeFlip:
    cfg = FastLoopConfig.model_validate(kw)
    r = RegimeFlip(cfg)
    r.arm(reference(now, direction))
    return r


@pytest.mark.parametrize("direction,up,down,px", [("看跌", 30, 10, 101), ("看涨", 10, 30, 99)])
def test_symmetric_confirmation_and_duplicate_tick(
    now: datetime, direction: Direction, up: int, down: int, px: float
) -> None:
    r = rule(now, direction)
    assert r.step(now, up, down, 40, px).count == 1
    assert r.step(now, up, down, 40, px).kind == "blocked"
    assert r.step(now + timedelta(minutes=5), up, down, 40, px).kind == "confirmed"


@pytest.mark.parametrize(
    "up,down,px,kind",
    [
        (30, 10, 100.3, "suspect_breadth"),
        (20, 20, 101, "suspect_btc"),
        (20, 20, 100.3, "quiet"),
        (5, 4, 102, "blocked"),
    ],
)
def test_missing_condition_resets_streak(
    now: datetime, up: int, down: int, px: float, kind: str
) -> None:
    r = rule(now)
    r.step(now, 40, 0, 40, 102)
    assert r.step(now + timedelta(minutes=5), up, down, up + down, px).kind == kind
    assert r.step(now + timedelta(minutes=10), 40, 0, 40, 102).count == 1


def test_neutral_missing_reference_and_missing_prices(now: datetime) -> None:
    r = rule(now, "中性", regime_flip_confirm=1)
    assert r.step(now, 40, 0, 40, 102).kind == "confirmed"
    r.arm(None)
    assert "参照" in r.step(now, 40, 0, 40, 102).reason
    r.arm(reference(now))
    for px in (None, 0, float("nan"), float("inf")):
        assert r.step(now, 40, 0, 40, px).reason == "数据不足"


def test_pool_proportion_and_no_implicit_maximum(now: datetime) -> None:
    r = rule(now, regime_flip_confirm=1, regime_flip_min_gap_min=10000.25)
    assert r.step(now, 15, 5, 20, 102).kind == "confirmed"
    r.last_rerun = now
    assert "间隔内" in r.step(now + timedelta(minutes=10000), 15, 5, 20, 102).reason
    assert r.step(now + timedelta(minutes=10000.25), 15, 5, 20, 102).kind == "confirmed"
    assert FastLoopConfig(regime_flip_confirm=1000, regime_flip_min_symbols=1000)


def test_zero_disables_each_threshold_and_hot_edit_resets(now: datetime) -> None:
    r = rule(now, regime_flip_confirm=0, regime_flip_min_gap_min=0)
    r.last_rerun = now
    assert r.step(now, 30, 10, 40, 102).kind == "confirmed"
    new = r.cfg.model_copy(
        update={
            "regime_flip_breadth": 0,
            "regime_flip_min_symbols": 0,
            "regime_flip_btc_pct": 0,
            "regime_flip_confirm": 2,
        }
    )
    patch_inplace(r.cfg, new, set(r.thresholds()))
    assert r.step(now + timedelta(minutes=1), 0, 0, 0, 100).count == 1
    assert r.step(now + timedelta(minutes=2), 0, 0, 0, 100).kind == "confirmed"
    r.arm(reference(now + timedelta(minutes=2), "看涨"))
    assert r.count == 0


def bar(symbol: str, at: datetime, px: float) -> Kline:
    return Kline(
        symbol=symbol,
        interval=Interval.M1,
        open_time=at - timedelta(minutes=1),
        close_time=at - timedelta(milliseconds=1),
        open=px,
        high=px,
        low=px,
        close=px,
        volume=1,
        quote_volume=px,
    )


async def test_rolling_hour_uses_exact_closed_endpoints(now: datetime) -> None:
    async def fetch(sym: str, start: datetime, end: datetime) -> list[Kline]:
        assert start == now - timedelta(minutes=61) and end == now
        last = {"AAUSDT": 102, "BBUSDT": 98, "CCUSDT": 100, "BTCUSDT": 101}[sym]
        rows = [bar(sym, now - timedelta(hours=1), 100), bar(sym, now, last)]
        if sym == "BBUSDT":
            rows[0] = bar(sym, now - timedelta(minutes=61), 100)  # 缺正确的端点不能拿旧线替代
        return [*rows, bar(sym, now + timedelta(minutes=1), 900)]  # 未收盘的不能算

    s = await snapshot(["AAUSDT", "BBUSDT", "CCUSDT"], now, fetch)
    assert (s.up, s.down, s.n) == (1, 0, 1)  # 持平不进分母
    assert s.median_pct == pytest.approx(1)
    assert s.prices == {"AAUSDT": 102, "CCUSDT": 100}
    assert s.btc_px == 101
    assert close_at([bar("AAUSDT", now + timedelta(minutes=1), 900)], now) is None
    # 大盘台刷新短线现状用的同一批线算出的涨跌（含 BTC）；没有 15 分钟前那根就不给
    assert s.rets_1h == pytest.approx({"AAUSDT": 2, "CCUSDT": 0, "BTCUSDT": 1})
    assert s.rets_15m == {}


async def test_snapshot_gives_15m_returns_from_the_same_bars(now: datetime) -> None:
    async def fetch(sym: str, start: datetime, end: datetime) -> list[Kline]:
        return [bar(sym, now - timedelta(minutes=15), 100), bar(sym, now, 99)]

    s = await snapshot(["AAUSDT"], now, fetch)
    assert s.rets_15m == pytest.approx({"AAUSDT": -1, "BTCUSDT": -1})


async def test_snapshot_propagates_rate_limit(now: datetime) -> None:
    async def fetch(*args: object) -> list[Kline]:
        raise RateLimited(429, now.timestamp() + 60)

    with pytest.raises(RateLimited):
        await snapshot(["AAUSDT"], now, fetch)


def row(now: datetime, kind: str = "suspect_breadth", **kw: object) -> dict:
    return {
        "ts": now.isoformat(),
        "sample_at": now.isoformat(),
        "kind": kind,
        "thresholds": RegimeFlip(FastLoopConfig()).thresholds(),
        "prices": {"AAUSDT": 100, "BBUSDT": 200},
        "btc_px": 100,
        "regime_dir": "看跌",
        **kw,
    }


def records(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for p in sorted(path.glob("*.jsonl"))
        for line in p.read_text().splitlines()
    ]


def test_every_event_reaches_sentinel_but_suspect_file_is_throttled(
    tmp_path: Path, now: datetime
) -> None:
    log = JsonlDecisionLog(tmp_path)
    root = tmp_path / "regime_flip"
    journal = FlipJournal(root, RoleRecorder(Role.SENTINEL, log))
    for mins in (0, 5, 30):
        journal.emit(row(now + timedelta(minutes=mins)), "大盘翻转疑似")
    assert len(records(root)) == 2
    assert len(log.read_day(now.date().isoformat())) == 3
    settings = {**row(now)["thresholds"], "regime_flip_suspect_log_min": 0}
    journal.emit(row(now + timedelta(minutes=31), thresholds=settings), "疑似不节流")
    journal.emit(row(now, "confirmed", rerun=True), "大盘翻转确认")
    journal.emit(row(now, "blocked", blocked_reason="日预算"), "大盘翻转被拦：日预算")
    assert [r["kind"] for r in records(root)][-3:] == ["suspect_breadth", "confirmed", "blocked"]
    assert {r.role for r in log.read_day(now.date().isoformat())} == {Role.SENTINEL}


async def test_forward_outcomes_survive_restart_and_keep_original_pool(
    tmp_path: Path, now: datetime
) -> None:
    first = FlipJournal(tmp_path, None).emit(row(now, "confirmed", rerun=True), "翻转")
    journal = FlipJournal(tmp_path, None)
    calls = []

    async def fetch(sym: str, start: datetime, end: datetime) -> list[Kline]:
        calls.append((sym, start, end))
        # AA +10%，BB −10%：原池收益中位数为 0；BTC +2%
        return [bar(sym, end, {"AAUSDT": 110, "BBUSDT": 180, "BTCUSDT": 102}[sym])]

    await journal.backfill(now + timedelta(hours=1, minutes=5), fetch)
    outcome = records(tmp_path)[-1]
    assert outcome["kind"] == "outcome" and outcome["event_id"] == first["event_id"]
    assert outcome["outcome_at"] == (now + timedelta(hours=1)).isoformat()
    assert outcome["fwd_1h_pool_median_pct"] == pytest.approx(0)
    assert outcome["fwd_1h_btc_pct"] == pytest.approx(2)
    await journal.backfill(now + timedelta(hours=1, minutes=10), fetch)
    assert len(calls) == 3  # 已回填的不重复取
    await FlipJournal(tmp_path, None).backfill(now + timedelta(hours=2), fetch)
    assert records(tmp_path)[-1]["horizon_h"] == 2
    assert journal.pending(now + timedelta(days=2)) == []


def test_write_failure_keeps_decision_log(tmp_path: Path, now: datetime) -> None:
    root = tmp_path / "not-a-directory"
    root.write_text("占位")
    log = JsonlDecisionLog(tmp_path / "log")
    j = FlipJournal(root, RoleRecorder(Role.SENTINEL, log))
    j.emit(row(now, "blocked"), "大盘翻转被拦")
    assert log.read_day(now.date().isoformat())[0].summary == "大盘翻转被拦"


def test_structured_direction_is_required_and_text_is_not_parsed() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _PickDraft.model_validate({"regime": "强势上行", "decisions": []})
    for direction in ("看涨", "看跌", "中性"):
        draft = _PickDraft.model_validate(
            {
                "regime": "中文写了涨和跌，但不拿它猜方向",
                "regime_direction": direction,
                "decisions": [],
            }
        )
        assert pick_result(draft, [], 1, [], False, 0, "", "").regime_direction == direction
    assert "regime_direction" in _PickDraft.model_json_schema()["required"]
