"""团队页「刚刚：…」：快环角色在 ModuleStatus 里带最近一次动作和一句话，有周期的带下次运行时间。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from jiaoyi.contracts import Health, Outcome, Role
from jiaoyi.core.recorder import SUMMARY_MAX, RoleRecorder
from jiaoyi.core.store import JsonlDecisionLog

T0 = datetime(2026, 10, 23, 14, 0, tzinfo=UTC)


def test_recorder_heartbeat_carries_last_action_and_next_run(tmp_path: Path) -> None:
    log = JsonlDecisionLog(tmp_path)
    rec = RoleRecorder(Role.SENTINEL, log)
    st = rec.heartbeat(T0, Health.OK)
    assert st.last_run_at is None and st.summary is None and st.next_run_at is None
    rec.record(T0, Outcome.ACTION, "SOLUSDT 放量突破（空）：跌破 180")
    rec.set_next_run(T0 + timedelta(seconds=30))
    st = rec.heartbeat(T0 + timedelta(seconds=5), Health.OK)
    assert st.last_run_at == T0 and st.summary == "SOLUSDT 放量突破（空）：跌破 180"
    assert st.next_run_at == T0 + timedelta(seconds=30)
    # 写进模块状态文件，面板另一个进程能读到
    saved = log.read_status()[Role.SENTINEL.value]
    assert saved.summary == st.summary and saved.last_run_at == T0
    # 只更新「刚刚」不写记录；太长截断
    rec.note(T0 + timedelta(minutes=1), "很长" * 200)
    st = rec.heartbeat(T0 + timedelta(minutes=1), Health.OK)
    assert len(st.summary or "") == SUMMARY_MAX
    assert len(log.read_day("2026-10-23")) == 1
    # extra 里显式给的优先（选币自己算 last_run_at / next_run_at）
    st = rec.heartbeat(T0, Health.OK, extra={"next_run_at": None})
    assert st.next_run_at is None
