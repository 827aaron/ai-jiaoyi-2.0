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
    functions = js[js.index("const EDGE_FADE_FROM") : js.index("const PROG_LABEL =")]
    script = (
        'const assert = require("node:assert/strict");\n'
        "const hm = (ts) => ts.slice(11, 16); const esc = (s) => s;\n"
        + layout
        + functions
        + "\n"
        + """
assert.deepEqual(regimeFlipEdges(null), []);
const edges = regimeFlipEdges({ts: "2026-10-26T06:35:00+00:00", output: {}});
assert.equal(edges.length, 1);
const edge = edges[0];
assert.equal(edge[0][1], TF_POS.sentinel[1]);
assert.equal(edge[1][1], TF_POS.market[1] + NODE_H);
assert.equal(edge[2], "market");
const svg = tfEdge(edge, {market: "doing"});
assert.match(svg, /class="e doing against"/);
assert.match(svg, /06:35 大盘变化 → 独立更新/);
assert.match(svg, /分钟前/);
assert.match(svg, /class="ah doing against"/);
assert.ok(!svg.includes("undefined") && !svg.includes("NaN"));
"""
    )
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    css = (root / "app.css").read_text()
    rule = css.split(".tf-edges .e.against {", 1)[1].split("}", 1)[0]
    assert "stroke-dasharray:" in rule


@pytest.mark.skipif(shutil.which("node") is None, reason="前端 SVG 验证需要 Node.js")
def test_event_edges_fade_with_age_and_show_relative_time() -> None:
    root = Path(__file__).resolve().parents[1] / "src/jiaoyi/panel/static"
    js = (root / "app.js").read_text()
    layout = js[js.index("const TF_W =") : js.index("const TF_LANES =")]
    functions = js[js.index("const EDGE_FADE_FROM") : js.index("const PROG_LABEL =")]
    script = (
        'const assert = require("node:assert/strict");\n'
        "const hm = (ts) => ts.slice(11, 16); const esc = (s) => s;\n"
        + layout
        + functions
        + """
const NOW = Date.parse("2026-10-26T06:40:00+00:00");
Date.now = () => NOW;
const ago = (m) => new Date(NOW - m * 60000).toISOString();
const op = (svg) => Number(/style="opacity:([0-9.]+)"/.exec(svg)[1]);
const flip = (m, scope) => regimeFlipEdges({ts: ago(m), output: {regime_flip: {scope}}});

assert.equal(op(tfEdge(flip(3)[0], {})), 1);
assert.equal(op(tfEdge(flip(10)[0], {})), 1);
assert.equal(op(tfEdge(flip(20)[0], {})), 0.68);
assert.equal(op(tfEdge(flip(30)[0], {})), 0.35);
assert.match(tfEdge(flip(3)[0], {}), /· 3 分钟前/);
assert.match(tfEdge(flip(0.2)[0], {}), /· 刚刚/);
assert.match(tfEdge(flip(3, "market_desk")[0], {}), /大盘变化 → 大盘台 · 3 分钟前/);
assert.match(tfEdge(flip(3, "market_only")[0], {}), /→ 独立更新/);

const ag = againstEdges([{symbol: "BTCUSDT", ts: ago(20), actions: ["meeting"]}]);
assert.equal(ag.length, 1);
assert.match(tfEdge(ag[0], {}), /反向 → 临时会.*· 20 分钟前/);
assert.equal(op(tfEdge(ag[0], {})), 0.68);

const pm = predictorMeetingEdges([{symbol: "ETHUSDT", ts: ago(30), bias: "long"}]);
assert.equal(op(tfEdge(pm[0], {})), 0.35);
assert.match(tfEdge(pm[0], {}), /· 30 分钟前/);

assert.ok(!tfEdge([[0, 0], [10, 0], "market", "固定", ""], {}).includes("opacity:"));
"""
    )
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="前端 SVG 验证需要 Node.js")
def test_desk_node_renders_and_flip_edge_points_at_it() -> None:
    root = Path(__file__).resolve().parents[1] / "src/jiaoyi/panel/static"
    js = (root / "app.js").read_text()
    layout = js[js.index("const TF_W =") : js.index("const TF_LANES =")]
    lanes = js[js.index("const TF_LANES =") : js.index("const tfR =")]
    edges = js[js.index("const tfR =") : js.index("const PROG_LABEL =")]
    node = js[js.index("// 大盘台：独立判断") : js.index("// 统一的「上次")]
    hb = js[js.index("function hbBadge(") : js.index("// 预测员：正常时")]
    script = (
        'const assert = require("node:assert/strict");\n'
        "const hm = (ts) => ts.slice(11, 16); const esc = (s) => s;\n"
        'const PROG_LABEL = { err: "出错", doing: "正在做", done: "这一轮做完", wait: "等待" };\n'
        + layout
        + lanes
        + edges
        + node
        + hb
        + """
const desk = {state: "ok", status: "正常", owner: "desk", direction: "看跌", published_version: 21,
  probs: {up: 0.2, flat: 0.25, down: 0.55}, last_run_at: "2026-10-26T05:02:00+00:00",
  next_run_at: "2026-10-26T06:02:00+00:00", runs_today: 3, cost_today_usd: 0.0123, error: null};
const html = tfDeskNode(desk);
const want = ["大盘台接管 · 看跌 v21", "涨 20% 平 25% 跌 55%"];
want.push("上次 05:02 · 下次 06:02", "今日 3 次 · $0.012", "心跳正常");
for (const t of want) assert.ok(html.includes(t), t);
assert.match(html, /class="dot done"/);
const fb = tfDeskNode({...desk, owner: "flash"});
assert.ok(fb.includes("回退 Flash · 看跌 v21"));
assert.match(tfDeskNode({...desk, state: "bad", error: "超时"}), /class="dot err"/);
const offNode = tfDeskNode({state: "off", status: "未启用", runs_today: 0, cost_today_usd: 0});
assert.ok(offNode.includes("没开"));

assert.equal(TF_LANES[0][4].join(), "screener,market,desk,commander,card");
assert.equal(TF_POS.desk[1], TF_POS.market[1]);
assert.ok(TF_POS.market[0] < TF_POS.desk[0] && TF_POS.desk[0] < TF_POS.commander[0]);
const feeds = TF_EDGES.filter((e) => e[3] === "大盘方向");
assert.deepEqual(feeds.map((e) => e[2]).sort(), ["commander", "sentinel"]);
for (const e of TF_EDGES) {
  const svg = tfEdge(e, {});
  assert.ok(!svg.includes("NaN") && !svg.includes("undefined"), e[3]);
}
const ev = (scope) => ({ts: "2026-10-26T06:35:00+00:00", output: {regime_flip: {scope}}});
const flip = regimeFlipEdges(ev("market_desk"))[0];
assert.equal(flip[2], "desk");
assert.equal(flip[1][0], TF_POS.desk[0] + 140);
assert.equal(flip[1][1], TF_POS.desk[1] + NODE_H);
assert.match(tfEdge(flip, {desk: "doing"}), /大盘台/);
const old = regimeFlipEdges(ev("market_only"))[0];
assert.equal(old[2], "market");
"""
    )
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
