"""决策日志和模块状态落盘，格式就是契约本身的 JSON，以后的面板直接读这些文件。

目录结构（``data_dir`` 默认 ``data/``）::

    data/decision_log/2026-10-22.jsonl   每行一个 DecisionRecord，按 UTC 日期分文件
    data/module_status.json              {role: ModuleStatus}，每个角色最新一条

M0–M1 用 JSONL 足够；以后量大再换 SQLite，读写接口（``DecisionLog``）不变。
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from jiaoyi.contracts import DecisionRecord, ModuleStatus


class JsonlDecisionLog:
    """``DecisionLog`` 的文件实现。线程安全；写失败直接抛出，由调用方决定是否告警。"""

    def __init__(self, data_dir: str | Path) -> None:
        self._root = Path(data_dir)
        self._log_dir = self._root / "decision_log"
        self._status_path = self._root / "module_status.json"
        self._lock = threading.Lock()
        self._log_dir.mkdir(parents=True, exist_ok=True)

    def write(self, record: DecisionRecord) -> None:
        path = self._log_dir / f"{record.ts.date().isoformat()}.jsonl"
        line = record.model_dump_json() + "\n"
        with self._lock, path.open("a", encoding="utf-8") as f:
            f.write(line)

    def heartbeat(self, status: ModuleStatus) -> None:
        with self._lock:
            current = self._read_status_raw()
            current[status.role.value] = status.model_dump(mode="json")
            tmp = self._status_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self._status_path)

    def read_day(self, day: str) -> list[DecisionRecord]:
        """读某一天（``YYYY-MM-DD``，UTC）的全部记录。"""
        path = self._log_dir / f"{day}.jsonl"
        if not path.is_file():
            return []
        with path.open(encoding="utf-8") as f:
            return [DecisionRecord.model_validate_json(line) for line in f if line.strip()]

    def read_status(self) -> dict[str, ModuleStatus]:
        with self._lock:
            raw = self._read_status_raw()
        return {k: ModuleStatus.model_validate(v) for k, v in raw.items()}

    def _read_status_raw(self) -> dict[str, object]:
        if not self._status_path.is_file():
            return {}
        data: dict[str, object] = json.loads(self._status_path.read_text(encoding="utf-8"))
        return data
