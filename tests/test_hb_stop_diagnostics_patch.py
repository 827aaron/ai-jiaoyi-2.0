"""镜像构建补丁及有界落盘验收，只用合成数据，不运行Docker或交易。"""

from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).parents[1] / "docker/hb-demo"


def _module(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_build_patch_preserves_original_method_ast_and_is_idempotent() -> None:
    patch = _module("patch_stop_diagnostics")
    source = """class PositionExecutor:
    async def control_task(self):
        return 7
    def control_stop_loss(self):
        return 'original-stop'
    def control_trailing_stop(self):
        return 'original-trail'
    def place_close_order_and_cancel_open_orders(self):
        return 'original-close'
    def get_custom_info(self):
        return {'original': True}
"""
    result = patch.patch(source)
    assert ast.dump(ast.parse(result).body[0]) == ast.dump(ast.parse(source).body[0])
    assert patch.patch(result) == result
    assert result.count(patch.MARK) == 1
    with pytest.raises(ValueError):
        patch.patch("class ChangedUpstream: pass")


def test_log_segments_roll_instead_of_stopping_for_the_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module("stop_diagnostics")
    monkeypatch.setattr(module, "MAX_SEGMENT_BYTES", 150)
    monkeypatch.setattr(module, "SEGMENTS", 2)
    writer = module.Writer(tmp_path)
    for seq in range(12):
        writer._write(json.dumps({"check_seq": seq, "check_at": f"synthetic-{seq:02d}"}))
    files = sorted(tmp_path.glob("stop-*.jsonl"))
    assert len(files) == 2
    assert sum(p.stat().st_size for p in files) <= 300
    assert writer.retention_evictions > 0
    records = [json.loads(line) for p in files for line in p.read_text().splitlines()]
    assert max(row["check_seq"] for row in records) == 11
    assert writer.retained_since is not None


def test_queue_overflow_is_visible_and_does_not_wait(tmp_path: Path) -> None:
    module = _module("stop_diagnostics")
    writer = module.Writer(tmp_path)
    writer.started = True  # 合成阻塞的消费者；不启动背景线程。
    state = module.State("synthetic-executor")
    for seq in range(129):
        writer.submit(state, {"check_seq": seq, "kind": "executor_stop_check"})
    assert writer.pending.qsize() == 128
    assert state.dropped == 1
    assert state.record_seq == 129
    assert state.snapshot()["coverage_complete"] is False


def test_oversize_is_counted_without_writing(tmp_path: Path) -> None:
    module = _module("stop_diagnostics")
    state, writer = module.State("synthetic-executor"), module.Writer(tmp_path)
    writer.submit(state, {"check_seq": 1, "value": "x" * (module.MAX_ROW_BYTES + 1)})
    assert state.oversize == state.dropped == 1
    assert writer.pending.empty() and list(tmp_path.iterdir()) == []
