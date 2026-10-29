# ruff: noqa: E501
"""团队页大盘台：节点卡的内容，以及大盘翻转 / 大盘台更新在实时动作里落到大盘台上。"""

from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from jiaoyi import contracts as c
from jiaoyi.panel.live import classify

STATIC = Path(__file__).resolve().parents[1] / "src/jiaoyi/panel/static"


def _rec(role: c.Role, outcome: c.Outcome, output: dict[str, object]) -> c.DecisionRecord:
    return c.DecisionRecord(
        record_id="r1",
        ts=datetime(2026, 10, 26, 6, 35, tzinfo=UTC),
        role=role,
        outcome=outcome,
        summary="大盘变化",
        output=output,
    )


def test_regime_flip_and_desk_records_pulse_the_desk() -> None:
    flip = {"regime_flip": {"kind": "confirmed"}}
    assert classify(_rec(c.Role.SENTINEL, c.Outcome.ACTION, flip))[0] == "regime"
    assert classify(_rec(c.Role.SENTINEL, c.Outcome.REJECT, flip))[0] == "regime"
    desk = {"market_desk": {"direction": "看跌"}}
    assert classify(_rec(c.Role.COMMANDER, c.Outcome.ACTION, desk))[0] == "desk"
    assert classify(_rec(c.Role.COMMANDER, c.Outcome.ERROR, desk))[1] == "error"


@pytest.mark.skipif(shutil.which("node") is None, reason="前端验证需要 Node.js")
def test_desk_node_renders_state_and_regime_path_ends_on_it() -> None:
    js = (STATIC / "app.js").read_text()
    live = js[js.index("const TL_W =") : js.index("// 进队：")]
    nodes = js[js.index("// 统一的「上次") : js.index("// 作战卡票根")]
    script = (
        'const assert = require("node:assert/strict");\n'
        "const hm = (ts) => ts.slice(11, 16); const esc = (s) => s;\n"
        "const MARKET_DOING = {}; const deskProgress = (k) => (k.error ? 'err' : 'done');\n"
        + live
        + nodes
        + """
const d = { selected_desk: true, against_checks: [] };
const desk = {state: "ok", status: "正常", owner: "desk", direction: "看跌", published_version: 21,
  last_run_at: "2026-10-26T05:02:00+00:00", runs_today: 3, cost_today_usd: 0.0123, error: null};
const html = tlNodeHtml("desk", { ...d, desk }, {});
for (const t of ["看跌 v21", "今日 3 · $0.012", 'class="hs ok"', "→ 管家 · 拍板快照"]) assert.ok(html.includes(t), t);
assert.match(html, /class="tl-node done on" data-k="desk"/);
assert.ok(tlNodeHtml("desk", { ...d, desk: { ...desk, owner: "flash" } }, {}).includes("Flash"));
const bad = tlNodeHtml("desk", { ...d, desk: { ...desk, state: "bad", error: "超时" } }, {});
assert.ok(bad.includes('class="hs err"') && bad.includes("出错：超时"));
assert.ok(tlNodeHtml("desk", { ...d, desk: { ...desk, state: "off", status: "未启用" } }, {}).includes("没开"));
assert.ok(tlNodeHtml("desk", { ...d, desk: null }, {}).includes("还没有数据"));
assert.deepEqual(livePath({ kind: "regime" }, []).pts, [TL_POS.desk]);
assert.deepEqual(livePath({ kind: "desk" }, []).pts, [TL_POS.desk]);
assert.equal(liveRole({ kind: "regime", role: "sentinel" }), "desk");
// 大盘台在慢环上，排在总盘会和军师团之间（给军师团当背景）
assert.equal(TL_LANES[0][2].join(), "screener,market,desk,commander,card");
assert.equal(TL_POS.desk[1], TL_POS.market[1]);
assert.ok(TL_POS.market[0] < TL_POS.desk[0] && TL_POS.desk[0] < TL_POS.commander[0]);
"""
    )
    done = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr[-2000:]
