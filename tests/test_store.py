from __future__ import annotations

from datetime import datetime
from pathlib import Path

from jiaoyi import contracts as c
from jiaoyi.core.store import JsonlDecisionLog
from jiaoyi.interfaces import DecisionLog
from tests.conftest import load_example


def test_decision_log_roundtrip(tmp_path: Path) -> None:
    log = JsonlDecisionLog(tmp_path)
    assert isinstance(log, DecisionLog)
    rec = c.DecisionRecord.model_validate(load_example("decision_record"))
    log.write(rec)
    log.write(rec.model_copy(update={"record_id": "dec-0002"}))
    day = rec.ts.date().isoformat()
    assert (tmp_path / "decision_log" / f"{day}.jsonl").is_file()
    assert [r.record_id for r in log.read_day(day)] == ["dec-0001", "dec-0002"]
    assert log.read_day("2000-01-01") == []


def test_module_status_keeps_latest_per_role(tmp_path: Path, now: datetime) -> None:
    log = JsonlDecisionLog(tmp_path)
    log.heartbeat(c.ModuleStatus(role=c.Role.GATE, health=c.Health.STARTING, ts=now))
    log.heartbeat(c.ModuleStatus(role=c.Role.GATE, health=c.Health.OK, ts=now))
    log.heartbeat(c.ModuleStatus(role=c.Role.PREDICTOR, health=c.Health.DISABLED, ts=now))
    status = log.read_status()
    assert status["gate"].health == c.Health.OK
    assert status["predictor"].health == c.Health.DISABLED
