"""分析师 K 线表、关键位候选周期、止盈提示（研究 v3）。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from jiaoyi.core import profiles
from jiaoyi.slow import tradingagents_engine as eng
from jiaoyi.slow.commander import StopScale
from jiaoyi.slow.levels import Candidate, LevelContext, describe_candidates
from jiaoyi.slow.settings import MeetingConfig

END = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
STEP = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}


def fake_klines(calls: list[tuple[str, int]] | None = None):  # type: ignore[no-untyped-def]
    end_ms = int(END.timestamp() * 1000)

    def fake(symbol: str, interval: str, limit: int, end: object = None) -> list[list[object]]:
        if calls is not None:
            calls.append((interval, limit))
        step = STEP[interval]
        rows = []
        for i in range(limit):
            t = end_ms - (limit - i) * step + step // 2  # 最后一根还没收盘
            rows.append([t, "1", "2", "0.5", "1.5", "10", t + step - 1, "15"])
        return rows

    return fake


def test_spec_parsing() -> None:
    assert eng.parse_kline_spec("15m x 96") == ("15m", 96)
    assert eng.parse_kline_spec("1d×60") == ("1d", 60)
    for bad in ("15 x 96", "3m x 5", "1h", "1h x 0"):
        with pytest.raises(ValueError):
            eng.parse_kline_spec(bad)
    with pytest.raises(ValueError):
        MeetingConfig(analyst_klines=("bogus",))


def test_hold_tables() -> None:
    for hold in ("30m", "1h", "2h"):
        assert profiles.analyst_klines(hold) == ("15m x 96", "1h x 72", "4h x 60")
        assert profiles.level_interval(hold) == "15m"
    for hold in ("4h", "8h", "12h", "24h"):
        assert profiles.analyst_klines(hold) == ("1h x 72", "4h x 90", "1d x 60")
    assert [profiles.level_interval(h) for h in ("4h", "8h", "12h", "24h")] == [
        "1h",
        "1h",
        "4h",
        "4h",
    ]


def test_csv_has_one_closed_section_per_period_finest_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(eng, "_klines", fake_klines())
    monkeypatch.setattr(eng, "derivatives_context", lambda *a, **k: "DERIV")
    eng.set_analyst_klines(profiles.analyst_klines("2h"), 120)
    out = eng.get_binance_stock_data("BTC", "2026-10-20", END.isoformat())
    heads = [ln for ln in out.splitlines() if ln.startswith("# ")]
    assert [("15m" in h, "1h" in h, "4h" in h) for h in heads] == [
        (True, False, False),
        (False, True, False),
        (False, False, True),
    ]
    assert "已收盘 96 根" in heads[0] and "已收盘 72 根" in heads[1] and "已收盘 60 根" in heads[2]
    assert out.count("Date,Open") == 3 and out.rstrip().endswith("DERIV")


def test_short_meeting_interval_adds_15m_section(monkeypatch: pytest.MonkeyPatch) -> None:
    long = profiles.analyst_klines("4h")
    eng.set_analyst_klines(long, 30)  # 30 分钟开一次会，最细一段是 1h
    assert eng._ANALYST_KLINES[0] == ("15m", 48) and len(eng._ANALYST_KLINES) == 4
    eng.set_analyst_klines(long, 120)
    assert eng._ANALYST_KLINES[0] == ("1h", 72) and len(eng._ANALYST_KLINES) == 3
    eng.set_analyst_klines(profiles.analyst_klines("2h"), 15)  # 本来就有 15m 段：不重复加
    assert [p[0] for p in eng._ANALYST_KLINES] == ["15m", "1h", "4h"]


def test_level_candidates_use_the_candidate_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(eng, "_klines", fake_klines(calls))
    eng.level_context("ETHUSDT", END, StopScale(period_min=15), 240, 1.5, "1h")
    assert ("1h", 300) in calls and ("15m", 300) in calls and ("1d", 12) in calls
    calls.clear()
    eng.level_context("ETHUSDT", END, StopScale(period_min=15), 240, 1.5, None)
    assert [c for c in calls if c[0] == "1h"] == []


def test_prompts_carry_tp_hint_breakout_note_and_oscillator_note() -> None:
    text = eng.describe_atr(10.0, 1000.0, StopScale(15, 0.5, 3.0, 1.0, 3.0))
    assert "1–3" in text and "建议 1.2–2" in text
    narrow = eng.describe_atr(10.0, 1000.0, StopScale(15, 0.5, 3.0, 1.0, 1.1))
    assert "建议" not in narrow  # 硬上限比建议区间还低：不给矛盾的建议
    ctx = LevelContext(100.0, 1.0, 5.0, (Candidate(101.0, "昨日高点"),), "basis")
    assert "突破位" in describe_candidates(ctx)
    assert "never treat them alone as reversal signals" in eng.perp_context(120)
