"""团队页点「总盘会」：选中总盘会自己的视图，显示总盘会的记录，不再停在默认的拍板人。"""

from __future__ import annotations

import shutil
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest

from jiaoyi import contracts as c
from jiaoyi.core.store import JsonlDecisionLog
from jiaoyi.panel.files import PanelFiles
from jiaoyi.panel.pages import build_team
from tests.conftest import ROOT
from tests.test_panel import NOW, RISK, _client, _rec, _seed


def test_market_role_shows_meeting_records_not_symbol_ones(tmp_path: Path) -> None:
    _seed(tmp_path)
    log = JsonlDecisionLog(tmp_path)
    t = NOW - timedelta(minutes=5)
    log.write(_rec(c.Role.COMMANDER, c.Outcome.ACTION, "出作战卡 v3：2 个标的，1 个可做", t))
    log.write(_rec(c.Role.COMMANDER, c.Outcome.ACTION, "BTCUSDT 开会完", t, symbol="BTCUSDT"))
    log.write(_rec(c.Role.GATE, c.Outcome.PASS, "BTC 又放行", t, symbol="BTCUSDT"))
    log.heartbeat(c.ModuleStatus(role=c.Role.MARKET, health=c.Health.OK, ts=NOW, headline=""))
    team = build_team(PanelFiles(tmp_path, RISK.day_reset_tz), NOW, c.Role.MARKET)
    assert team.selected is c.Role.MARKET
    texts = [r.text for r in team.records]
    assert "出作战卡 v3：2 个标的，1 个可做" in texts  # _seed 里还有一条出卡记录
    assert not any("开会完" in x or "放行" in x for x in texts)  # 单个币的会、拍板人的记录不混进来
    assert team.market_role is not None and team.market_role.last_beat is not None
    # 没报过状态也要能选：页面靠 market_role 画标题
    bare = build_team(PanelFiles(tmp_path / "nope", RISK.day_reset_tz), NOW, c.Role.MARKET)
    assert bare.market_role is not None and bare.market_role.name == "总盘会"


def test_team_api_accepts_market_role(tmp_path: Path) -> None:
    _seed(tmp_path)
    JsonlDecisionLog(tmp_path).write(
        _rec(c.Role.COMMANDER, c.Outcome.ERROR, "开会失败（定时）：超时", NOW)
    )
    body = _client(tmp_path, "127.0.0.1").get("/api/team", params={"role": "market"}).json()
    assert body["selected"] == "market"
    assert body["records"][0]["text"] == "开会失败（定时）：超时"  # 最新在前
    assert {r["role"] for r in body["records"]} == {"commander"}


def test_js_market_node_selects_market_role() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("JavaScript 渲染回归需要 Node")
    source = (ROOT / "src/jiaoyi/panel/static/app.js").read_text()
    flow = source[source.index("function teamFlow(d)") : source.index("const FLOW_ORDER")]
    team = source[source.index("P.team = () =>") : source.index("/* ---------- 历史 */")]
    script = (
        """
const assert = require('node:assert/strict');
const esc = String, clickable = (a, g = '') => `data-act="${a}" data-arg="${g}"`;
const MARKET = '_MARKET', hms = () => '', hm = () => '', PROG_LABEL = {}, P = {};
const TF_POS = { market: [1, 2] }, TF_LANES = [], TF_EDGES = [], TF_W = 1, TF_H = 1;
const FLOW_ORDER = [], ROLES = [], QUIET_ROLE = {};
let tfNowItems = [], data = null, pageData = null, window = {};
const tfMarketNode = () => 'M', tfNowHtml = () => '', tfEdge = () => '';
const againstEdges = () => [], predictorMeetingEdges = () => [], regimeFlipEdges = () => [];
const promptLine = () => '', meetingsCard = () => '', quietHead = () => '', pill = () => '';
const againstCard = () => '', bookReaderCard = () => '', sentinelCard = () => '';
"""
        + flow
        + team
        + """
pageData = { roles: [], selected: 'market', market_progress: 'done', against: [],
  predictor_meetings: [], regime_flip: null, prompts: {},
  records: [{ ts: 0, outcome: 'action', text: '出作战卡 v3' }],
  market_role: { role: 'market', name: '总盘会', last_beat: 1, avg_latency_ms: 1200 } };
const html = P.team();
assert(/<a class="tf-node done on" href="#team\\/market"/.test(html), html);
assert(html.includes('总盘会 · 最近做了什么') && html.includes('出作战卡 v3'), html);
assert(html.includes('平均 1200 ms'), html);
assert(html.includes('data-act="meeting" data-arg="_MARKET"'), html);  // 开会过程入口还在
"""
    )
    subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)
