"""执行实际前端连线函数，验证触发后画到总盘会且复用虚线样式。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="前端 SVG 验证需要 Node.js")
def test_trigger_renders_dashed_sentinel_to_market_edge() -> None:
    root = Path(__file__).resolve().parents[1] / "src/jiaoyi/panel/static"
    js = (root / "app.js").read_text()
    layout = js[js.index("const TF_W =") : js.index("const TF_LANES =")]
    functions = js[js.index("function regimeFlipEdges(") : js.index("const PROG_LABEL =")]
    script = (
        'const assert = require("node:assert/strict");\n'
        "const hm = (ts) => ts.slice(11, 16); const esc = (s) => s;\n"
        + layout
        + functions
        + "\n"
        + """
assert.deepEqual(regimeFlipEdges(null), []);
const edges = regimeFlipEdges({ts: "2026-10-26T06:35:00+00:00"});
assert.equal(edges.length, 1);
const edge = edges[0];
assert.equal(edge[0][1], TF_POS.sentinel[1]);
assert.equal(edge[1][1], TF_POS.market[1] + NODE_H);
assert.equal(edge[2], "market");
const svg = tfEdge(edge, {market: "doing"});
assert.match(svg, /class="e doing against"/);
assert.match(svg, /06:35 大盘变化 → 独立更新/);
assert.match(svg, /class="ah doing against"/);
assert.ok(!svg.includes("undefined") && !svg.includes("NaN"));
"""
    )
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    css = (root / "app.css").read_text()
    rule = css.split(".tf-edges .e.against {", 1)[1].split("}", 1)[0]
    assert "stroke-dasharray:" in rule
