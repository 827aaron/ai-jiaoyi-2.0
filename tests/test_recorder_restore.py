from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from jiaoyi.contracts import Outcome, Role
from jiaoyi.core.recorder import RoleRecorder
from jiaoyi.core.store import JsonlDecisionLog


def test_today_stats_survive_restart(tmp_path: Path) -> None:
    # 曼谷 2026-10-24 00:30 = UTC 10-23 17:30：本地这一天跨两个 UTC 日
    t0 = datetime(2026, 10, 23, 17, 30, tzinfo=UTC)
    log = JsonlDecisionLog(tmp_path)
    r1 = RoleRecorder(Role.SENTINEL, log)
    r1.record(t0, Outcome.PASS, "a", latency_ms=10, cost_usd=0.01)
    r1.record(t0 + timedelta(hours=8), Outcome.ERROR, "b", latency_ms=30, cost_usd=0.02)
    RoleRecorder(Role.GATE, log).record(t0, Outcome.PASS, "别的角色不算")
    r1.record(t0 - timedelta(hours=1), Outcome.PASS, "昨天（曼谷）不算")

    r2 = RoleRecorder(Role.SENTINEL, log)  # 重启
    s = r2.today(t0 + timedelta(hours=9))
    assert (s.calls, s.passed, s.errors) == (2, 1, 1)
    assert s.cost_usd == pytest.approx(0.03) and s.avg_latency_ms == pytest.approx(20)
    r2.record(t0 + timedelta(hours=9), Outcome.PASS, "c")
    assert r2.today(t0 + timedelta(hours=9)).calls == 3  # 新记录不重复计


def test_an_earlier_timestamp_never_rolls_the_day_back() -> None:
    from datetime import UTC, datetime

    from jiaoyi.contracts import Outcome, Role
    from jiaoyi.core.recorder import RoleRecorder

    r = RoleRecorder(Role.RECONCILER, None, day_reset_tz="Asia/Bangkok")
    after = datetime(2026, 10, 28, 17, 0, 5, tzinfo=UTC)  # 曼谷 10-29 00:00:05
    before = datetime(2026, 10, 28, 16, 59, 50, tzinfo=UTC)  # 曼谷 10-28 23:59:50
    r.record(after, Outcome.INFO, "written after midnight")
    r.note(before, "tick that started before midnight", count=True)
    assert r.today(after).calls == 2
