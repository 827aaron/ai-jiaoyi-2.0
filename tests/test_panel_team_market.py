"""团队页点「总盘会」：选中总盘会自己的视图，显示总盘会的记录，不再停在默认的拍板人。"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timedelta
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
const TF_POS = { market: [1, 2], desk: [3, 4] }, TF_LANES = [], TF_EDGES = [], TF_W = 1, TF_H = 1;
const FLOW_ORDER = [], ROLES = [], QUIET_ROLE = {};
let tfNowItems = [], data = null, pageData = null, window = {};
const deskProgress = () => 'wait', tfDeskNode = () => 'D';
const tfMarketNode = () => 'M', tfNowHtml = () => '', tfEdge = () => '';
const againstEdges = () => [], predictorMeetingEdges = () => [], regimeFlipEdges = () => [];
const promptLine = () => '', meetingsCard = () => '', quietHead = () => '', pill = () => '';
const againstCard = () => '', bookReaderCard = () => '', sentinelCard = () => '';
const deskRunsCard = (runs) => (runs && runs.length ? 'RUNS' : '');
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
pageData = { ...pageData, selected: 'commander', selected_desk: true,
  desk: { last_run_at: 5, error: null }, desk_runs: [{}],
  records: [{ ts: 0, outcome: 'action', text: '大盘台：看跌' }] };
const dh = P.team();
assert(/<a class="tf-node desk wait on" href="#team\\/desk"/.test(dh), dh);
assert(dh.includes('大盘台 · 最近做了什么') && dh.includes('大盘台：看跌'), dh);
assert(dh.indexOf('RUNS') < dh.indexOf('大盘台 · 最近做了什么'), dh);  // 两张卡在记录上面
"""
    )
    subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)


def _desk_row(t0: datetime, **kw: object) -> dict[str, object]:
    return {
        "cycle_id": f"{t0:%Y-%m-%dT%H:%MZ}",
        "t0": t0.isoformat(),
        "reason": "scheduled",
        "status": "ok",
        "direction": "中性",
        "probs_4h": {"up": 0.3, "flat": 0.45, "down": 0.25},
        "probs_1h": {"up": 0.3, "flat": 0.5, "down": 0.2},
        "regime": "模型的话",
        "published_view": None,
        **kw,
    }


def test_desk_node_gives_latest_and_previous_runs(tmp_path: Path) -> None:
    from jiaoyi.panel.pages import desk_runs

    t1 = NOW - timedelta(minutes=20)
    t2 = NOW - timedelta(minutes=5)
    block = {
        "t0": t2.isoformat(),
        "as_of": t2.isoformat(),
        "order": "up_first",
        "probs_1h": {"up": 0.4, "flat": 0.4, "down": 0.2},
        "values": {"btc_ret_15m_pct": 0.1, "btc_ret_1h_pct": 0.2},
        "max_age_min": 5.0,
    }
    refreshed = {
        **block,
        "status": {
            "as_of": (NOW - timedelta(minutes=1)).isoformat(),
            "values": {"btc_ret_15m_pct": -0.3, "btc_ret_1h_pct": 0.5},
        },
    }
    old = _desk_row(t1 - timedelta(days=1), direction="看涨")
    prev = _desk_row(
        t1,
        direction="看涨",
        published_view={"version": 7},
        intra={**block, "t0": t1.isoformat(), "as_of": t1.isoformat()},
    )
    latest = _desk_row(t2, reason="move", intra=block)
    d = tmp_path / "market_desk"
    d.mkdir()
    (d / f"{old['t0'][:10]}-old.jsonl").write_text(json.dumps(old) + "\n")  # 更早的文件不用读
    rows = [prev, {**_desk_row(t2), "status": "failed"}, latest]
    (d / f"{NOW:%Y-%m-%d}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (tmp_path / "market_desk_state.json").write_text(
        json.dumps({"latest": {"cycle_id": latest["cycle_id"], "intra": refreshed}})
    )
    got = desk_runs(PanelFiles(tmp_path, RISK.day_reset_tz), NOW)
    assert [r.cycle_id for r in got] == [latest["cycle_id"], prev["cycle_id"]]
    assert got[0].reason == "move" and got[0].published_version is None
    assert got[0].intra is not None and "BTC 15 分钟 -0.30%" in got[0].intra  # 刷新过的那份
    assert got[1].published_version == 7 and got[1].direction == "看涨"
    assert got[1].intra is not None and "BTC 15 分钟 +0.10%" in got[1].intra  # 那一轮存下的
    assert "短线现状：缺失" not in got[1].intra  # 存下的那份按那一轮的时刻显示，不算过旧
    team = build_team(PanelFiles(tmp_path, RISK.day_reset_tz), NOW, None, desk_selected=True)
    assert len(team.desk_runs) == 2
    assert build_team(PanelFiles(tmp_path, RISK.day_reset_tz), NOW, None).desk_runs == []


def test_js_desk_runs_cards_show_changes() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("JavaScript 渲染回归需要 Node")
    source = (ROOT / "src/jiaoyi/panel/static/app.js").read_text()
    part = source[source.index("const DESK_REASON") : source.index("function deskProgress(k)")]
    part = part[: part.index("// 大盘台：独立判断")]
    script = (
        """
const assert = require('node:assert/strict');
const esc = (s) => String(s).replace(/</g, '&lt;'), hm = () => '10:02';
"""
        + part
        + """
const ago = (m) => new Date(Date.now() - m * 60000).toISOString();
const prev = { cycle_id: 'a', run_at: ago(20), reason: 'scheduled', direction: '看涨',
  probs_4h: { up: 0.5, flat: 0.3, down: 0.2 }, probs_1h: { up: 0.3, flat: 0.5, down: 0.2 },
  intra: '短线现状（截至 09:47 UTC）', published_version: 7, regime: '<b>原文</b>' };
const latest = { ...prev, cycle_id: 'b', run_at: ago(5),
  reason: 'move', direction: '中性', probs_4h: { up: 0.32, flat: 0.44, down: 0.24 },
  intra: '短线现状（截至 10:06 UTC）', published_version: null };
const html = deskRunsCard([latest, prev]);
assert(html.indexOf('最新大盘情况') < html.indexOf('上一次大盘情况'), html);
assert(html.includes('5 分钟前 · 急动加跑') && html.includes('20 分钟前 · 定时'), html);
assert(html.includes('上一轮 看涨'), html);                 // 方向变了
assert(html.includes('涨 32% <mark class="chg">-18pp</mark>'), html);
assert(html.includes('跌 24% <mark class="chg">+4pp</mark>'), html);
assert(!html.includes('平 50% <mark'), html);                 // 1h 没变不标
assert(html.includes('短线现状（截至 10:06 UTC）'), html);
assert(html.includes('短线现状（截至 09:47 UTC）'), html);
assert(html.includes('已发布 · 大盘 v7') && html.includes('没发布'), html);
assert(html.includes('<details') && html.includes('&lt;b>原文'), html);
assert(html.includes('grid-template-columns:repeat(auto-fit'), html);  // 窄屏上下叠
assert.equal(deskRunsCard([]), '');
const one = deskRunsCard([latest]);
assert(one.includes('最新大盘情况') && !one.includes('上一次') && !one.includes('<mark'), one);
"""
    )
    subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)
