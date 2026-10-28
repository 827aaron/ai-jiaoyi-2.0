from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest

from jiaoyi.execution.day_override import apply_day_override
from jiaoyi.risk.guard import DayLossBreaker

NOW = datetime(2026, 10, 28, 17, 30, tzinfo=UTC)  # 曼谷 10-29 00:30：UTC 日期还是 10-28
TZ = "Asia/Bangkok"


class FakeExecutor:
    def __init__(self, old: float | None) -> None:
        self.old = old
        self.calls: list[tuple[str, float, dict[str, float]]] = []

    def override_day_start(self, day: str, equity: float, refs: dict[str, float]) -> float | None:
        self.calls.append((day, equity, refs))
        return self.old


def _breaker(tmp_path: Path, start: float) -> DayLossBreaker:
    (tmp_path / "risk_state.json").write_text(
        json.dumps({"day": "2026-10-29", "start_equity": start, "tripped": False})
    )
    return DayLossBreaker(3.0, TZ, tmp_path / "risk_state.json")


def _write(tmp_path: Path, **doc: object) -> None:
    (tmp_path / "day_start_override.json").write_text(json.dumps(doc), encoding="utf-8")


def test_apply_day_override_sets_both_bases_audits_and_never_reapplies(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _write(
        tmp_path,
        day="2026-10-29",
        equity_usdt=5751.86,
        refs={"BTCUSDT": 68000.5},
        reason="0 点基准取了钱包余额",
    )
    ex, breaker = FakeExecutor(5970.98), _breaker(tmp_path, 5970.98)
    with caplog.at_level(logging.WARNING):
        assert apply_day_override(tmp_path, NOW, TZ, ex, breaker)
    assert ex.calls == [("2026-10-29", 5751.86, {"BTCUSDT": 68000.5})]
    state = json.loads((tmp_path / "risk_state.json").read_text())
    assert state == {"day": "2026-10-29", "start_equity": 5751.86, "tripped": False}
    [row] = [json.loads(x) for x in (tmp_path / "config_changes.jsonl").read_text().splitlines()]
    assert (row["section"], row["key"], row["old"], row["new"]) == (
        "day_start",
        "equity_usdt",
        5970.98,
        5751.86,
    )
    assert "熔断变松" in row["note"] and "0 点基准取了钱包余额" in row["note"]
    assert "熔断变松" in caplog.text and "5970.98" in caplog.text
    assert not (tmp_path / "day_start_override.json").exists()
    assert [p.name for p in tmp_path.glob("day_start_override.json.applied-*")] == [
        "day_start_override.json.applied-20261029T003000"
    ]
    assert not apply_day_override(tmp_path, NOW, TZ, ex, breaker)
    assert len(ex.calls) == 1


def test_apply_day_override_says_when_the_breaker_gets_stricter(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _write(tmp_path, day="2026-10-29", equity_usdt=6100.0)
    with caplog.at_level(logging.WARNING):
        assert apply_day_override(
            tmp_path, NOW, TZ, FakeExecutor(5970.98), _breaker(tmp_path, 5970.98)
        )
    assert "熔断变严" in caplog.text


@pytest.mark.parametrize(
    "doc",
    [
        {"day": "2026-10-28", "equity_usdt": 5751.86},
        {"day": "2026-10-29", "equity_usdt": 0},
        {"day": "2026-10-29", "equity_usdt": "5751.86"},
        {"day": "2026-10-29", "equity_usdt": 5751.86, "refs": {"BTCUSDT": -1}},
        [],
    ],
)
def test_apply_day_override_rejects_wrong_day_or_bad_value_untouched(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, doc: object
) -> None:
    (tmp_path / "day_start_override.json").write_text(json.dumps(doc), encoding="utf-8")
    ex, breaker = FakeExecutor(5970.98), _breaker(tmp_path, 5970.98)
    before = (tmp_path / "risk_state.json").read_text()
    with caplog.at_level(logging.WARNING):
        assert not apply_day_override(tmp_path, NOW, TZ, ex, breaker)
    assert ex.calls == []
    assert (tmp_path / "risk_state.json").read_text() == before
    assert not (tmp_path / "config_changes.jsonl").exists()
    assert [p.name for p in tmp_path.glob("day_start_override.json.*")] == [
        "day_start_override.json.rejected-20261029T003000"
    ]
    assert "没用上" in caplog.text


def test_apply_day_override_without_file_does_nothing(tmp_path: Path) -> None:
    ex = FakeExecutor(None)
    assert not apply_day_override(tmp_path, NOW, TZ, ex, None)
    assert ex.calls == [] and list(tmp_path.iterdir()) == []
