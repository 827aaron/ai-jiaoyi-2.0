"""拍板门槛校准：样本筛选、三维扫门槛、样本不足 / 过不了成本线的说法、结果文件给设置页用。"""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from jiaoyi.gate import calibrate

NOW = datetime(2026, 10, 24, 0, 0, tzinfo=UTC)


def _row(i: int, p_take: float, p_toxic: float, r: float, **over: Any) -> dict[str, Any]:
    return {
        "signal_id": f"s{i}",
        "ts": (NOW - timedelta(days=i % 7, minutes=i)).isoformat(),
        "template": "q3",
        "template_fp": "fp1",
        "model": "m1",
        "hold_min": 120,
        "p_take": p_take,
        "p_real": 0.5,
        "p_toxic": p_toxic,
        "r": r,
        "net_bps": r * 80.0,
        "stop_bps": 80.0,
        "exit": "tp" if r > 0 else "stop",
        "sig": "volume_breakout",
        **over,
    }


def _rows(n: int = 600) -> list[dict[str, Any]]:
    """做概率高的赚钱、低的亏钱：门槛该落在 0.4 附近。"""
    rnd = random.Random(7)
    out = []
    for i in range(n):
        p = rnd.random() * 0.8
        win = rnd.random() < (0.7 if p >= 0.4 else 0.3)
        out.append(_row(i, round(p, 3), 0.5, 1.5 if win else -1.0, source="live"))
    return out


def test_recommends_threshold_that_clears_cost_line() -> None:
    res = calibrate.calibrate(_rows(), hold="2h", now=NOW)
    assert res["samples"] == 600 and res["recommended"] is not None
    rec = res["recommended"]
    assert 0.3 <= rec["p_take"] <= 0.5
    best = res["best"]
    assert best["n"] >= calibrate.MIN_ABOVE and best["lcb90_r"] > 0
    assert res["breakdown"]["sig"]["volume_breakout"]["n"] == 600
    assert "不自动套用" in res["note"]


def test_too_few_samples_gives_table_only() -> None:
    res = calibrate.calibrate(_rows(200), hold="2h", now=NOW, min_above=50)
    assert res["recommended"] is None and "样本不足" in res["note"]


def test_losing_template_says_cannot_clear_cost_line() -> None:
    rows = [_row(i, 0.5 + (i % 9) / 20, 0.5, -0.2, source="live") for i in range(400)]
    res = calibrate.calibrate(rows, hold="2h", now=NOW)
    assert res["recommended"] is None and "过不了成本线" in res["note"]


def test_slippage_is_charged_per_stop() -> None:
    assert calibrate.net_r({"r": 1.0, "stop_bps": 50.0}) == 1.0 - 1.0 / 50.0
    assert calibrate.net_r({"r": 1.0, "net_bps": 40.0}) == 1.0 - 1.0 / 40.0  # 旧数据倒推


def test_load_rows_filters_hold_and_merges_replay(tmp_path: Path) -> None:
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    rows = [_row(1, 0.5, 0.5, 1.0), _row(2, 0.5, 0.5, 1.0, hold_min=240), _row(3, 0.5, 0.5, 1.0)]
    rows[2]["r"] = None  # 没算出 R 的不要
    (shadow / "outcomes.jsonl").write_text("\n".join(json.dumps(r) for r in rows), "utf-8")
    run = tmp_path / "replay" / "jev" / "20261023T000000"
    run.mkdir(parents=True)
    (run / "outcomes.jsonl").write_text(json.dumps(_row(1, 0.4, 0.5, -1.0)), "utf-8")
    got = calibrate.load_rows(tmp_path, 120)
    assert sorted((r["source"], r["signal_id"]) for r in got) == [("live", "s1"), ("replay", "s1")]
    assert [r["source"] for r in calibrate.load_rows(tmp_path, 120, replay=False)] == ["live"]


def test_writes_file_the_settings_page_reads(tmp_path: Path) -> None:
    from jiaoyi.panel.files import PanelFiles

    res = calibrate.calibrate(_rows(), hold="2h", now=NOW)
    path = calibrate.write(tmp_path, res)
    assert path == tmp_path / "gate_calibration" / "2h.json"
    cal = PanelFiles(tmp_path, "Asia/Bangkok").gate_calibration("2h")
    assert cal is not None and cal["samples"] == 600
    assert set(cal["recommended"]) == {"p_take", "p_genuine", "max_p_toxic"}
    assert calibrate.render(res)[-1] == res["note"]


def test_mixed_templates_are_not_pooled() -> None:
    rows = _rows(400) + [
        _row(1000 + i, 0.9, 0.3, 2.0, model="m2", source="live") for i in range(50)
    ]
    res = calibrate.calibrate(rows, hold="2h", now=NOW)
    assert res["samples"] == 400 and res["group"]["model"] == "m1"
    assert res["other_groups"] == [{"template": "q3", "template_fp": "fp1", "model": "m2", "n": 50}]
