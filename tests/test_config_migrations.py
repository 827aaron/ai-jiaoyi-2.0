"""一次性配置迁移：只删手写的量能突增倍数，只跑一次、留备份、记修改记录、校验不过不写。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from jiaoyi.core.config import load_app_config
from jiaoyi.panel import migrations as mig
from jiaoyi.panel.actions import CHANGES_FILE, set_toml_value
from tests.test_panel_actions import _setup

NOW = datetime(2026, 10, 25, 1, 0, tzinfo=UTC)


def _hand_set(path: Path) -> None:
    """模拟 Mac：fast_loop 手写量能突增 3.5 / 0.65，predictor 手写 128 / 4。"""
    t = path.read_text(encoding="utf-8")
    t = t.replace("surge_vol_ratio = 5.0", "surge_vol_ratio = 3.5", 1)
    t = t.replace("surge_taker_ratio = 0.7", "surge_taker_ratio = 0.65", 1)
    t = t.replace("[predictor]\n", "[predictor]\nlookback = 128\npaths = 4\n", 1)
    path.write_text(t, encoding="utf-8")


def test_clears_hand_set_keys_once_and_logs(tmp_path: Path) -> None:
    paths, data = _setup(tmp_path)
    _hand_set(paths.app)
    before = load_app_config(paths.app)
    assert before.fast_loop.surge_vol_ratio == 3.5
    removed = mig.apply_pending(paths, data, NOW)
    assert [(r["section"], r["key"], r["old"]) for r in removed] == [
        ("fast_loop", "surge_vol_ratio", 3.5)
    ]
    after = load_app_config(paths.app)
    assert after.fast_loop.surge_vol_ratio == 5.0
    # 其余手写项按约定不动
    assert after.fast_loop.surge_taker_ratio == 0.65
    assert (after.predictor.lookback, after.predictor.paths) == (128, 4)
    assert list(paths.app.parent.glob("app.toml.bak-*"))
    rows = [json.loads(x) for x in (data / CHANGES_FILE).read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1 and all(r["new"] is None and "一次性迁移" in r["note"] for r in rows)
    assert json.loads((data / mig.DONE_FILE).read_text(encoding="utf-8"))["done"] == [
        "2026-10-25-surge-vol-ratio"
    ]
    # 跑过就不再跑：之后用户自己再写的值留着
    t = set_toml_value(paths.app.read_text(encoding="utf-8"), "fast_loop", "surge_vol_ratio", 3.5)
    paths.app.write_text(t, encoding="utf-8")
    assert mig.apply_pending(paths, data, NOW) == []
    assert load_app_config(paths.app).fast_loop.surge_vol_ratio == 3.5


def test_nothing_hand_set_just_marks_done(tmp_path: Path) -> None:
    paths, data = _setup(tmp_path)
    t = paths.app.read_text(encoding="utf-8")
    t = "\n".join(x for x in t.splitlines() if not x.startswith("surge_"))
    paths.app.write_text(t + "\n", encoding="utf-8")
    text = paths.app.read_text(encoding="utf-8")
    assert mig.apply_pending(paths, data, NOW) == []
    assert paths.app.read_text(encoding="utf-8") == text
    assert not (data / CHANGES_FILE).exists() and (data / mig.DONE_FILE).exists()


def test_invalid_result_is_not_written_or_marked(tmp_path: Path) -> None:
    paths, data = _setup(tmp_path)
    bad = mig.Migration(id="x", note="测试", clears=(("", "reconcile_interval_s"),))  # 必填
    text = paths.app.read_text(encoding="utf-8")
    assert mig.apply_pending(paths, data, NOW, (bad,)) == []
    assert paths.app.read_text(encoding="utf-8") == text
    assert not (data / mig.DONE_FILE).exists()
