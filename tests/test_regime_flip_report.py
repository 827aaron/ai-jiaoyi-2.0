from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

from scripts.regime_flip_report import read_rows, report


def test_report_counts_denominators_cost_and_window(tmp_path: Path, now: datetime) -> None:
    def event(eid: str, kind: str, **kw: object) -> dict:
        return {"ts": now.isoformat(), "event_id": eid, "kind": kind, **kw}

    rows = [
        event("suspect", "suspect_breadth"),
        event("btc", "suspect_btc"),
        event("blocked", "blocked", blocked_reason="日预算"),
        event("a", "confirmed", regime_dir="看跌"),
        event("b", "confirmed", regime_dir="看涨"),
        event("c", "confirmed", regime_dir="看跌"),  # 失败不当成方向不变
        event("d", "confirmed", regime_dir="看跌"),  # 尚无结果不当成零收益
        event("a", "rerun_result", regime_changed=True, cost_usd=1.25),
        event("b", "rerun_result", regime_changed=False, cost_usd=2.25),
        event("c", "rerun_result", result="flip_failed", cost_usd=0.5),
        event("a", "outcome", horizon_h=1, fwd_1h_pool_median_pct=2, fwd_1h_btc_pct=1),
        event("b", "outcome", horizon_h=1, fwd_1h_pool_median_pct=4, fwd_1h_btc_pct=-1),
        event("old", "confirmed", ts=(now - timedelta(days=4)).isoformat()),
        event("future", "confirmed", ts=(now + timedelta(days=1)).isoformat()),
    ]
    path = tmp_path / f"{now:%Y-%m-%d}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\nnot-json\n[]\n")
    before = path.read_bytes()
    output = report(read_rows(tmp_path), now)
    assert f"| {now:%Y-%m-%d} | 1 | 1 | 4 | 1 |" in output
    assert "- 日预算：1 次" in output
    assert "| 1h 池面 | 2 | +3.000% | +3.000% | 50.0%（1/2） | 50.0%（1/2） | 0 |" in output
    assert "| 1h BTC | 2 | +0.000% | +0.000% | 100.0%（2/2） | 0.0%（0/2） | 0 |" in output
    assert "| 2h 池面 | 0 | — | — |" in output
    assert "重跑后方向改变：50.0%（1/2）；失败 1 次，尚无结果 1 次" in output
    assert "$4.000000" in output and "默认值无数据支持" in output
    assert path.read_bytes() == before


def test_cli_no_records_does_not_create_data(tmp_path: Path) -> None:
    missing = tmp_path / "regime_flip"
    script = Path(__file__).resolve().parents[1] / "scripts/regime_flip_report.py"
    result = subprocess.run(
        [sys.executable, str(script), "--data-dir", str(missing)],
        check=True,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={},
    )
    assert "最近 3 天汇总" in result.stdout
    assert "| 无记录 | 0 | 0 | 0 | 0 |" in result.stdout
    assert "无有效样本" in result.stdout
    assert not list(tmp_path.iterdir())
