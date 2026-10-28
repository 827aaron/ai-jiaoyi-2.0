# ruff: noqa: E501
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


# 团队页整段（泳道、卡、事件小标、P.team）真跑；页面外的工具函数用最小的桩
TEAM_STUBS = r"""
const assert = require('node:assert/strict');
const esc = (s) => String(s).replace(/</g, '&lt;').replace(/"/g, '&quot;');
const hm = (ts) => String(ts).slice(11, 16), hms = () => '', md = () => '', coin = (s) => String(s).replace(/USDT$/, '');
const clickable = (a, g = '') => `data-act="${a}" data-arg="${g}"`;
const MARKET = '_MARKET', MARKET_DOING = { pick: '挑上会的币' }, P = {}, ROLES = [];
const setInterval = () => 0, document = { addEventListener() {} }, addEventListener = () => {}, $ = () => null;
let data = null, pageData = null, window = {};
const meetingsCard = () => '', pill = () => '', profileSpan = String, money = String, pxFmt = String;
"""


def _team_js() -> str:
    source = (ROOT / "src/jiaoyi/panel/static/app.js").read_text()
    return (
        TEAM_STUBS
        + source[source.index("/* ---------- 团队 */") : source.index("/* ---------- 历史 */")]
    )


def _role(role: str, name: str, **kw: object) -> dict[str, object]:
    return {
        "role": role, "name": name, "state": "ok", "status": "正常（上次 06:31）", "headline": "",
        "last_beat": None, "last_error": None, "calls": 4, "passed": 0, "rejected": 0, "errors": 0,
        "avg_latency_ms": 0, "cost_usd": 0, "doing": None, "progress": "wait", "last_run_at": None,
        "next_run_at": None, "summary": None, "detail": [], "detail_hidden": 0, **kw,
    }  # fmt: skip


def test_js_market_node_selects_market_role() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("JavaScript 渲染回归需要 Node")
    script = (
        _team_js()
        + r"""
pageData = { roles: [], selected: 'market', market_progress: 'done', against: [],
  predictor_meetings: [], regime_flip: null, prompts: {},
  records: [{ ts: 0, outcome: 'action', text: '出作战卡 v3' }],
  market_role: { role: 'market', name: '总盘会', state: 'ok', status: '正常', last_beat: 1, avg_latency_ms: 1200, calls: 2, errors: 0, cost_usd: 0 } };
const html = P.team();
assert(/<a class="tc on" href="#team\/market"/.test(html), html);
assert(html.includes('总盘会 · 最近做了什么') && html.includes('出作战卡 v3'), html);
assert(html.includes('平均 1200 ms'), html);
assert(html.includes('data-act="meeting" data-arg="_MARKET"'), html);  // 开会过程入口还在
pageData = { ...pageData, selected: 'commander', selected_desk: true,
  desk: { state: 'ok', status: '正常', last_run_at: '2026-10-26T05:02:00+00:00', error: null, runs_today: 3, cost_today_usd: 0.0123,
    owner: 'desk', direction: '看跌', published_version: 21, probs: { up: 0.2, flat: 0.25, down: 0.55 } },
  desk_runs: [{}], records: [{ ts: 0, outcome: 'action', text: '大盘台：看跌' }] };
const dh = P.team();
assert(/<a class="tc on" href="#team\/desk"/.test(dh), dh);
assert(dh.includes('大盘台接管 · 看跌 v21') && dh.includes('今日 3 次 · $0.012'), dh);
assert(dh.includes('4 小时 涨 20% 平 25% 跌 55%'), dh);  // 概率在悬停提示里
assert(dh.includes('大盘台 · 最近做了什么') && dh.includes('大盘台：看跌'), dh);
assert(dh.indexOf('desk-runs') < dh.indexOf('大盘台 · 最近做了什么'), dh);  // 两轮对比在记录上面
"""
    )
    done = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr[-2000:]


def test_js_team_lanes_place_every_role_and_show_errors_only_when_nonzero() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("JavaScript 渲染回归需要 Node")
    roles = [
        _role("screener", "选币"),
        _role("commander", "军师团", errors=2, cost_usd=1.284),
        _role("book_reader", "盘口员", doing="盯着行情", progress="doing"),
        _role("predictor", "预测员 Kronos"),
        _role("sentinel", "哨兵", summary="BTC 放量突破"),
        _role("gate", "拍板人", rejected=4),
        _role("risk_guard", "风控员"),
        _role("executor", "执行器"),
        _role("lifeboat", "保命止损"),
        _role("keeper", "持仓管家"),
        _role("keeper_assistant", "管家助手", state="bad", status="失败：模型超时", errors=1),
        _role("patrol", "巡逻兵"),
        _role("reconciler", "对账员"),
        _role("alerting", "告警"),
    ]
    script = (
        _team_js()
        + "const ROLES_IN = "
        + json.dumps(roles, ensure_ascii=False)
        + r""";
data = { card: { version: 397, valid: true, expires_at: '2026-10-26T07:51:00+00:00', symbols: [{ bias: 'long' }, { bias: 'short' }, { bias: 'no_trade' }] } };
const d = { roles: ROLES_IN, selected: 'gate', market_progress: 'wait', market: null, desk: null,
  market_role: { role: 'market', name: '总盘会', state: 'ok', status: '正常', calls: 1, errors: 0, cost_usd: 0 },
  against: [], predictor_meetings: [], regime_flip: null, against_checks: [] };
const html = teamFlow(d);
const lane = (id) => {
  const a = html.indexOf(`data-lane="${id}"`);
  const b = html.indexOf('data-lane=', a + 1);
  return html.slice(a, b < 0 ? undefined : b);
};
const ids = (part) => [...part.matchAll(/data-role="([a-z_]+)"/g)].map((m) => m[1]);
assert.deepEqual(ids(lane('slow')), ['screener', 'market', 'desk', 'commander', 'card']);
assert.deepEqual(ids(lane('entry')), ['book_reader', 'sentinel', 'gate', 'risk_guard', 'executor', 'predictor']);
assert.deepEqual(ids(lane('fast')), ['keeper', 'keeper_assistant', 'patrol', 'lifeboat', 'reconciler']);
assert.deepEqual(ids(lane('foot')), ['alerting']);
// 箭头只在 ① ② 的主链里，四个一条；③ 不画
assert.equal((lane('slow').match(/class="tc-ar"/g) || []).length, 4);
assert.equal((lane('entry').match(/class="tc-ar"/g) || []).length, 4);
assert.equal((lane('fast').match(/class="tc-ar"/g) || []).length, 0);
assert(!html.includes('<svg'), 'no connector lines');
// 错只在 > 0 时出现，红色
const cardOf = (role) => { const i = html.indexOf(`data-role="${role}"`); return html.slice(html.lastIndexOf('<', i), html.indexOf('data-role=', i + 1)); };
assert(cardOf('commander').includes('<span class="s-err">错 2</span>'), cardOf('commander'));
assert(!cardOf('screener').includes('错'), cardOf('screener'));
assert(!cardOf('gate').includes('错') && cardOf('gate').includes('拒 4'), cardOf('gate'));
assert(cardOf('keeper_assistant').includes('class="hd bad"') && cardOf('keeper_assistant').includes('失败：模型超时'));
// 正在做 = 蓝框；「刚刚」只在悬停提示里，不占卡上的行
assert(/class="tc doing" href="#team\/book_reader"/.test(html), cardOf('book_reader'));
assert(cardOf('sentinel').includes('title="正常（上次 06:31）\n刚刚：BTC 放量突破"'), cardOf('sentinel'));
assert(!/<span class="tc-k[^"]*">刚刚/.test(html), html);
assert(cardOf('sentinel').includes('用：作战卡 v397 关键位'));
assert(cardOf('commander').includes('用：大盘台 方向'), cardOf('commander'));
assert(cardOf('risk_guard').includes('<span class="tc-k">上次 06:31</span>'), cardOf('risk_guard'));
assert(cardOf('card').includes('<span class="tc-v">v397</span>') && cardOf('card').includes('2 个可做'));
// 没归到泳道的新角色进底栏，不丢
const extra = teamFlow({ ...d, roles: [...ROLES_IN, { ...ROLES_IN[0], role: 'newbie', name: '新角色' }] });
assert(ids(extra.slice(extra.indexOf('data-lane="foot"'))).includes('newbie'));
"""
    )
    done = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr[-2000:]


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
