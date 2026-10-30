# ruff: noqa: E501
"""团队页点「总盘会」：选中总盘会自己的视图，显示总盘会的记录，不再停在默认的拍板人。"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from jiaoyi import contracts as c
from jiaoyi.core.store import JsonlDecisionLog
from jiaoyi.panel import live
from jiaoyi.panel.files import PanelFiles
from jiaoyi.panel.live import KINDS, LIVE_MAX, LIVE_STALE_S, LiveSource, classify, to_event
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


TEAM_STUBS = """
const assert = require('node:assert/strict');
const esc = String, tx = String, clickable = (a, g = '') => `data-act="${a}" data-arg="${g}"`;
const MARKET = '_MARKET', hms = () => '', hm = () => '07:30', P = {};
const coin = (s) => String(s || '').replace(/USDT$/, ''), cls = () => '', signed = (x) => String(x);
const SIDE = { long: ['多', 'up'], short: ['空', 'down'] }, SIGNAL = {}, BIAS = {};
const MARKET_DOING = {}, ROLES = [], QUIET_ROLE = {};
const deskProgress = (k) => (k ? 'done' : 'wait');
let data = { card: null, positions: [] }, pageData = null, window = {};
const promptLine = () => '', meetingsCard = () => '', quietHead = () => '', pill = () => '';
const againstCard = () => '', bookReaderCard = () => '', sentinelCard = () => '';
const deskRunsCard = (runs) => (runs && runs.length ? 'RUNS' : '');
const $ = () => null, document = { hidden: false }, addEventListener = () => {};
"""


def _team_js() -> str:
    source = (ROOT / "src/jiaoyi/panel/static/app.js").read_text()
    live = source[source.index("const TL_W =") : source.index("function liveStart()")]
    nodes = source[source.index("// 统一的「上次") : source.index("// 画布按容器宽度整体缩放")]
    team = source[source.index("P.team = () =>") : source.index("/* ---------- 历史 */")]
    return TEAM_STUBS + live + nodes + team


def _role(role: str, name: str, **kw: object) -> dict[str, object]:
    base: dict[str, object] = {
        "role": role, "name": name, "state": "ok", "status": "正常", "headline": "",
        "calls": 3, "rejected": 0, "errors": 0, "cost_usd": 0, "progress": "done",
        "last_beat": 1, "avg_latency_ms": 0,
    }  # fmt: skip
    return base | kw


def test_js_market_node_selects_market_role() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("JavaScript 渲染回归需要 Node")
    script = (
        _team_js()
        + """
pageData = { roles: [], selected: 'market', market_progress: 'done', against: [],
  predictor_meetings: [], regime_flip: null, prompts: {},
  records: [{ ts: 0, outcome: 'action', text: '出作战卡 v3' }],
  market_role: { role: 'market', name: '总盘会', state: 'ok', last_beat: 1, avg_latency_ms: 1200 } };
const html = P.team();
assert(/class="tl-node done on" data-k="market" href="#team\\/market"/.test(html), html);
assert(html.includes('总盘会 · 最近做了什么') && html.includes('出作战卡 v3'), html);
assert(html.includes('平均 1200 ms'), html);
assert(html.includes('data-act="meeting" data-arg="_MARKET"'), html);  // 开会过程入口还在
pageData = { ...pageData, selected: 'commander', selected_desk: true,
  desk: { state: 'ok', status: '正常', direction: '看跌', published_version: 21, runs_today: 3,
    cost_today_usd: 0.0123, last_run_at: 5, error: null }, desk_runs: [{}],
  records: [{ ts: 0, outcome: 'action', text: '大盘台：看跌' }] };
const dh = P.team();
assert(/class="tl-node done on" data-k="desk" href="#team\\/desk"/.test(dh), dh);
assert(dh.includes('看跌 v21') && dh.includes('今日 3 · $0.012'), dh);
assert(dh.includes('大盘台 · 最近做了什么') && dh.includes('大盘台：看跌'), dh);
assert(dh.indexOf('RUNS') < dh.indexOf('大盘台 · 最近做了什么'), dh);  // 两张卡在记录上面
"""
    )
    done = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr[-3000:]


def test_js_team_places_roles_by_data_flow_and_shows_errors_only_when_nonzero() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("JavaScript 渲染回归需要 Node")
    roles = [
        _role("screener", "选币"), _role("commander", "军师团"), _role("book_reader", "盘口员"),
        _role("sentinel", "哨兵", rejected=36), _role("predictor", "预测员"),
        _role("gate", "拍板人", errors=8, cost_usd=0.0073), _role("risk_guard", "风控员"),
        _role("executor", "执行器"), _role("keeper", "持仓管家"),
        _role("keeper_assistant", "管家助手"), _role("patrol", "巡逻兵"),
        _role("lifeboat", "保命止损", state="off", status="未启用"),
        _role("reconciler", "对账员", state="bad", status="无心跳超过 5 分钟", progress="err"),
        _role("alerting", "告警推送", errors=1),
    ]  # fmt: skip
    script = (
        _team_js()
        + f"const ROLES_IN = {json.dumps(roles, ensure_ascii=False)};\n"
        + """
pageData = { roles: ROLES_IN, selected: 'gate', market_progress: 'wait', against_checks: [],
  records: [], market_role: null, desk: null };
const html = P.team();
const box = (k) => { const m = new RegExp(`<a [^>]*data-k="${k}"[^>]*>([^]*?)</a>`).exec(html); assert(m, k); return m[1]; };
// 每个角色都在图上，各在自己那条线
for (const r of ROLES_IN) if (r.role !== 'alerting') box(r.role);
assert.deepEqual(TL_LANES.map((l) => l[2].length), [5, 6, 6]);
assert.ok(TL_POS.book_reader[0] < TL_POS.sentinel[0] && TL_POS.sentinel[0] < TL_POS.gate[0]);
assert.ok(TL_POS.gate[0] < TL_POS.risk_guard[0] && TL_POS.risk_guard[0] < TL_POS.executor[0]);
assert.equal(TL_POS.predictor[0], TL_POS.gate[0]);         // 预测员挂在拍板人上方
assert.ok(TL_POS.predictor[1] < TL_POS.gate[1]);
assert.equal(TL_POS.keeper[1], TL_POS.keeper_assistant[1]); // 管家助手和持仓管家并排
assert(/<div class="tl-pair"[^>]*>[^]*data-k="keeper"[^]*data-k="keeper_assistant"/.test(html));
for (const k of ['patrol', 'lifeboat', 'reconciler']) {      // 守卫围着持仓圈
  const d = Math.hypot(TL_POS[k][0] - TL_RING[0], TL_POS[k][1] - TL_RING[1]);
  assert.ok(d > TL_RING_R - 30 && d < TL_RING_R + 70, k + ' ' + d);
}
assert(html.includes('盯的币 ← 总盘会') && html.includes('只接哨兵对持仓币的反向信号'));
assert(html.includes('行情总线：盘口员推送 → 哨兵统一取数'));
assert(html.includes('<b>告警</b>收所有角色的事') && html.includes('<b>看门狗</b>在主程序外面'));
// 错只在 > 0 时出现，红色；拒、花费是 0 不写
assert(box('gate').includes('<span class="err">错 8</span>'), box('gate'));
assert(box('gate').includes('$0.007'));
assert(!box('executor').includes('错') && !box('executor').includes('拒') && !box('executor').includes('$'));
assert(box('sentinel').includes('拒 36'));
assert(box('reconciler').includes('class="hs err"') && box('lifeboat').includes('class="hs off"'));
assert(html.includes('id="tl-feed"') && html.includes('id="tl-fx"') && html.includes('id="tl-ring"'));
// 持仓圈：没有仓位写空仓；一笔大圆；多笔小圆
assert(ringHtml([]).includes('空仓'));
const one = ringHtml([{ position_id: 'p', symbol: 'STRKUSDT', side: 'short', status: 'open', entry_price: 2, stop_loss_price: 2.1, quantity: 10 }]);
assert(one.includes('<b>STRK</b>') && one.includes('守着的持仓'), one);
data = { positions: [{ position_id: 'p', net_unrealized_usdt: 0.5 }] };
assert(ringHtml([{ position_id: 'p', symbol: 'STRKUSDT', side: 'short', status: 'open', entry_price: 2, stop_loss_price: 2.1, quantity: 10 }]).includes('+0.50R'));
const many = ringHtml(Array.from({ length: 3 }, (_, i) => ({ position_id: 'p' + i, symbol: 'X' + i + 'USDT', side: 'long', status: 'open', quantity: 1 })));
assert.equal((many.match(/class="tl-core/g) || []).length, 3);
assert(ringHtml(Array.from({ length: 6 }, (_, i) => ({ position_id: 'p' + i, symbol: 'X' + i + 'USDT', side: 'long', status: 'open', quantity: 1 }))).includes('守着 6 笔'));
// 实时动作列表：最新在前，常规动作合并；点一条带下标
LIVE.feed = [];
liveFeedAdd({ id: 'a', ts: 1, role: 'keeper', kind: 'keeper_check', routine: true, tone: 'info', text: '拿住' });
liveFeedAdd({ id: 'b', ts: 2, role: 'keeper', kind: 'keeper_check', routine: true, tone: 'info', text: '拿住' });
liveFeedAdd({ id: 'c', ts: 3, role: 'risk_guard', kind: 'risk_reject', tone: 'reject', symbol: 'ARBUSDT', badge: '拒：低于门槛', text: '空 拍板放弃' });
assert.equal(LIVE.feed.length, 2);
assert.equal(LIVE.feed[1].count, 2);
const feed = liveFeedHtml(LIVE.feed, 'c');
assert(feed.indexOf('风控员') < feed.indexOf('持仓管家'), feed);
assert(feed.includes('class="tl-ev reject on"') && feed.includes('data-act="liveev" data-arg="0"'), feed);
assert(feed.includes('<em class="tl-tw reject">拒</em>') && feed.includes('×2'), feed);
"""
    )
    done = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr[-3000:]


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


AT = int(NOW.timestamp())


def _live(client: Any, cursor: str | None = None) -> dict[str, Any]:
    params = {"cursor": cursor} if cursor else {}
    r = client.get("/api/team/live", params=params)
    assert r.status_code == 200, r.text
    return cast(dict[str, Any], r.json())


def test_team_live_returns_events_after_cursor_in_order(tmp_path: Path) -> None:
    _seed(tmp_path)
    log = JsonlDecisionLog(tmp_path)
    with _client(tmp_path, "127.0.0.1") as client:
        first = _live(client)
        assert first["reset"] is True
        assert first["cursor"] == f"2026-10-22:9@{AT}"
        # 没带游标：给最近几条（今天 9 条 + 昨天 1 条），旧的在前
        assert [e["text"] for e in first["events"]][:2] == ["昨天的信号", "BTC 放量突破"]
        assert [p["position_id"] for p in first["ring"]] == ["pos-0001"]

        t = NOW - timedelta(seconds=5)
        jev = {"jev": {"answers": {"take": {"take": 0.65, "skip": 0.35}}}}
        assistant = {"keeper_assistant": {"judges": [
            {"judge": "A", "p_yes": 0.61}, {"judge": "B", "p_yes": 0.377}]}}  # fmt: skip
        for r in [
            _rec(c.Role.SENTINEL, c.Outcome.ACTION, "ARBUSDT 量能突增（空）：量 5 倍", t,
                 symbol="ARBUSDT", output={"kind": "volume_surge", "side": "short"}),
            _rec(c.Role.GATE, c.Outcome.INFO, "ARBUSDT 空：做 take 0.65", t, symbol="ARBUSDT",
                 output=jev),
            _rec(c.Role.RISK_GUARD, c.Outcome.REJECT, "ARBUSDT 空 拍板放弃", t, symbol="ARBUSDT",
                 output={"reasons": ["“做”概率 0.47 低于门槛 0.55"]}),
            _rec(c.Role.EXECUTOR, c.Outcome.ACTION, "ARBUSDT short 开仓成交", t,
                 symbol="ARBUSDT", position_id="pos-9", output={"entry_price": 0.17}),
            _rec(c.Role.EXECUTOR, c.Outcome.INFO, "ARBUSDT 账本按币安成交回填：净盈亏 -18.51U", t,
                 symbol="ARBUSDT", position_id="pos-9",
                 output={"backfill": {"realized_pnl_usdt": -18.51}}),
            _rec(c.Role.KEEPER_ASSISTANT, c.Outcome.INFO, "LTCUSDT 待确认翻转", t,
                 symbol="LTCUSDT", output=assistant),
            _rec(c.Role.COMMANDER, c.Outcome.ACTION, "出作战卡 v4：2 个标的，1 个可做", t,
                 output={"version": 4, "symbols": []}),
        ]:  # fmt: skip
            log.write(r)
        nxt = _live(client, first["cursor"])
        assert nxt["reset"] is False and nxt["cursor"] == f"2026-10-22:16@{AT}"
        got = [(e["role"], e["kind"], e["tone"], e["badge"]) for e in nxt["events"]]
        assert got == [
            ("sentinel", "signal", "info", None),
            ("gate", "verdict", "info", "take 0.65"),
            ("risk_guard", "risk_reject", "reject", "拒：“做”概率 0.47 低于门槛…"),
            ("executor", "fill", "open", "成交"),
            ("executor", "backfill", "close", None),
            ("keeper_assistant", "assistant", "info", "A 0.61 / B 0.38"),
            ("commander", "card", "info", "v4"),
        ]
        sig = nxt["events"][0]
        assert (sig["symbol"], sig["side"], sig["signal_kind"]) == (
            "ARBUSDT",
            "short",
            "volume_surge",
        )
        assert sig["text"] == "量能突增（空）：量 5 倍"  # 币名单独放在 symbol 里
        assert nxt["events"][4]["pnl_usdt"] == -18.51
        assert _live(client, nxt["cursor"])["events"] == []
        # 游标对不上（文件被换短了 / 太旧）：当作没游标，给最近几条
        for bad in (
            f"2026-10-22:999@{AT}",  # 文件变短了
            f"2026-10-22:-1@{AT}",
            f"2026-10-01:3@{AT}",  # 太旧的日期
            f"2026-10-22:16@{AT - LIVE_STALE_S - 1}",  # 发出太久（电脑睡醒）
            "2026-10-22:16",  # 没有发出时间
        ):
            assert _live(client, bad)["reset"] is True, bad


def test_team_live_coalesces_bursts_keeping_real_actions(tmp_path: Path) -> None:
    _seed(tmp_path)
    log = JsonlDecisionLog(tmp_path)
    with _client(tmp_path, "127.0.0.1") as client:
        cur = _live(client)["cursor"]
        t = NOW - timedelta(seconds=30)
        for i in range(LIVE_MAX + 40):
            log.write(_rec(c.Role.KEEPER, c.Outcome.HOLD, f"BTCUSDT 拿住 {i}", t,
                           symbol="BTCUSDT", position_id="pos-0001"))  # fmt: skip
        log.write(_rec(c.Role.EXECUTOR, c.Outcome.CLOSE, "BTCUSDT 平仓", t, symbol="BTCUSDT"))
        body = _live(client, cur)
    assert len(body["events"]) == LIVE_MAX
    assert body["events"][-1]["kind"] == "close"  # 真动作一条不丢
    assert all(e["routine"] for e in body["events"][:-1])
    assert body["events"][-2]["text"] == f"拿住 {LIVE_MAX + 39}"  # 留下的是最新的那些
    assert body["coalesced"] == [{"role": "keeper", "kind": "keeper_check", "count": 41}]


def test_team_live_ring_lists_open_positions_only(tmp_path: Path) -> None:
    _seed(tmp_path)
    src = LiveSource(PanelFiles(tmp_path, RISK.day_reset_tz))
    assert [(p.position_id, p.symbol, p.side, p.status) for p in src.ring()] == [
        ("pos-0001", "BTCUSDT", "long", "open")
    ]
    raw = json.loads((tmp_path / "positions.json").read_text())
    raw["positions"]["pos-0001"]["record"]["status"] = "closed"
    (tmp_path / "positions.json").write_text(json.dumps(raw) + " ")
    assert src.ring() == []


def test_every_record_kind_is_one_the_page_knows() -> None:
    cases: list[tuple[c.Role, c.Outcome, dict[str, Any]]] = [
        (role, outcome, {}) for role in c.Role for outcome in c.Outcome
    ]
    cases += [
        (c.Role.COMMANDER, c.Outcome.ACTION, {"market_desk": {}}),
        (c.Role.COMMANDER, c.Outcome.INFO, {"key_level": 1}),
        (c.Role.SENTINEL, c.Outcome.INFO, {"against_signal": {"actions": ["keeper_recheck"]}}),
        (c.Role.SENTINEL, c.Outcome.ACTION, {"against_signal": {"actions": ["meeting"]}}),
        (c.Role.SENTINEL, c.Outcome.REJECT, {"regime_flip": {}}),
        (c.Role.PATROL, c.Outcome.CLOSE, {"paper_stop": {}}),
    ]
    for role, outcome, out in cases:
        kind, tone, _ = classify(_rec(role, outcome, "x", NOW, output=out))
        assert kind in KINDS, (role, outcome, kind)
        assert tone in ("open", "close", "reject", "error", "info")


def test_js_live_paths_cover_every_kind_and_rate_limit() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("JavaScript 回归需要 Node")
    source = (ROOT / "src/jiaoyi/panel/static/app.js").read_text()
    live = source[source.index("const TL_W =") : source.index("const LIVE = {")]
    script = (
        """
const assert = require('node:assert/strict');
const SIGNAL = { volume_surge: '量能突增' }, BIAS = { no_trade: ['观望', ''] };
const signed = (x, d) => (x < 0 ? '−' : '+') + Math.abs(x).toFixed(d);
"""
        + live
        + f"const KINDS = {json.dumps(list(KINDS))};\n"
        + """
assert.deepEqual(Object.keys(LIVE_PATHS).sort(), [...KINDS].sort());
for (const k of KINDS) {
  const p = livePath({ kind: k, role: 'keeper', position_id: 'p2' }, [{ position_id: 'p1' }, { position_id: 'p2' }]);
  assert.ok(p.pts.length >= 1 && p.pts.every((q) => Number.isFinite(q[0]) && Number.isFinite(q[1])), k);
}
// 信号从哨兵走到拍板人；被拒停在风控员；成交飞进圈里这笔仓位的位置；平仓从仓位走到撤离箭头
const ring = [{ position_id: 'a' }, { position_id: 'b' }];
assert.deepEqual(livePath({ kind: 'signal' }, ring).pts, [TL_POS.sentinel, TL_POS.gate]);
const rej = livePath({ kind: 'risk_reject' }, ring);
assert.ok(rej.stop);
assert.deepEqual(rej.pts[rej.pts.length - 1], TL_POS.risk_guard);
assert.ok(!livePath({ kind: 'risk_pass' }, ring).stop);
const fill = livePath({ kind: 'fill', position_id: 'b' }, ring).pts;
assert.deepEqual(fill[0], TL_POS.executor);
assert.deepEqual(fill[fill.length - 1], ringSlots(2)[1].slice(0, 2));
const out = livePath({ kind: 'close', position_id: 'a' }, ring).pts;
assert.deepEqual(out[0], ringSlots(2)[0].slice(0, 2));
assert.deepEqual(out[out.length - 1], TP.exitB);
// 平仓从促成它的角色出发：交易所止损从保命止损、管家判平从管家
assert.deepEqual(livePath({ kind: 'close', position_id: 'a', via: 'lifeboat' }, ring).pts[0], TP.lifeboatR);
assert.deepEqual(livePath({ kind: 'close', position_id: 'a', via: 'keeper' }, ring).pts.slice(0, 2), [TP.keeperB, ringSlots(2)[0].slice(0, 2)]);
assert.deepEqual(livePath({ kind: 'close', position_id: 'a', via: null }, ring).pts[0], ringSlots(2)[0].slice(0, 2));
assert.deepEqual(livePath({ kind: 'keeper', position_id: 'gone' }, ring).pts[1], TL_RING);
assert.equal(ringSlots(0).length, 0);
assert.equal(ringSlots(7).length, 7);
assert.equal(liveLabel({ kind: 'signal', symbol: 'ARBUSDT', side: 'short', signal_kind: 'volume_surge', text: '' }), 'ARB 空 量能突增');
assert.equal(liveLabel({ kind: 'verdict', symbol: 'ARBUSDT', badge: 'take 0.65', text: '' }), 'ARB take 0.65');
assert.equal(liveLabel({ kind: 'backfill', symbol: 'ARBUSDT', pnl_usdt: -18.51, text: '' }), 'ARB 净 −18.51U');
assert.equal(liveLabel({ kind: 'keeper_check', role: 'keeper', routine: true, count: 12, text: '' }), '持仓管家 ×12');
assert.equal(liveLabel({ kind: 'book', count: 1234, text: '' }), '+1,234 条行情');
// 合并和限速：常规动作并成一个；队列有上限，先丢常规；两个光点至少隔 LIVE_GAP_MS
const q = [];
for (let i = 0; i < 50; i++) liveEnqueue(q, { kind: 'keeper_check', routine: true });
assert.equal(q.length, 1);
assert.equal(q[0].count, 50);
for (let i = 0; i < 20; i++) liveEnqueue(q, { kind: 'signal', id: 's' + i });
assert.equal(q.length, LIVE_QUEUE_MAX);
assert.ok(q.every((e) => !e.routine));
assert.equal(q[q.length - 1].id, 's19');
const st = { queue: q, lastFire: 1000, flying: 0 };
assert.equal(liveReady(st, 1000 + LIVE_GAP_MS - 1), false);
assert.equal(liveReady(st, 1000 + LIVE_GAP_MS), true);
assert.equal(liveReady({ ...st, flying: 4 }, 99999), false);
assert.equal(liveReady({ ...st, queue: [] }, 99999), false);
assert.ok(1000 / LIVE_GAP_MS <= 3);
"""
    )
    done = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr[-2000:]


def test_team_live_far_behind_cursor_resets_instead_of_replaying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(tmp_path)
    src = LiveSource(PanelFiles(tmp_path, RISK.day_reset_tz))
    cur = src.feed(NOW, None).cursor
    log = JsonlDecisionLog(tmp_path)
    for i in range(6):
        log.write(_rec(c.Role.SENTINEL, c.Outcome.INFO, f"BTCUSDT 看 {i}", NOW, symbol="BTCUSDT"))
    monkeypatch.setattr(live, "LIVE_BEHIND_MAX", 5)
    body = src.feed(NOW, cur)
    assert body.reset is True
    assert body.events[-1].text == "看 5"  # 从最近几条重新开始
    monkeypatch.setattr(live, "LIVE_BEHIND_MAX", 6)
    assert src.feed(NOW, cur).reset is False


def test_team_live_reads_late_lines_of_yesterday_after_utc_rollover(tmp_path: Path) -> None:
    files = PanelFiles(tmp_path, RISK.day_reset_tz)
    log = JsonlDecisionLog(tmp_path)
    src = LiveSource(files)
    eve = datetime(2026, 10, 22, 23, 59, 50, tzinfo=UTC)
    log.write(_rec(c.Role.GATE, c.Outcome.INFO, "BTC 前一条", eve - timedelta(seconds=5)))
    cur = src.feed(eve, None).cursor
    assert cur.split("@")[0] == "2026-10-22:1"
    # 换日后：主循环拿着换日前取的 now 写的那行落进昨天的文件，今天的文件也开始有了
    log.write(_rec(c.Role.RISK_GUARD, c.Outcome.PASS, "BTC 晚到", eve + timedelta(seconds=5)))
    log.write(_rec(c.Role.EXECUTOR, c.Outcome.ACTION, "BTC 成交", eve + timedelta(seconds=15)))
    after = eve + timedelta(seconds=20)
    body = src.feed(after, cur)
    assert body.reset is False
    assert [e.text for e in body.events] == ["BTC 晚到", "BTC 成交"]
    assert body.cursor.split("@")[0] == "2026-10-22:2|2026-10-23:1"
    log.write(_rec(c.Role.KEEPER, c.Outcome.HOLD, "BTC 又晚到", eve + timedelta(seconds=8)))
    body = src.feed(after + timedelta(minutes=2), body.cursor)
    assert [e.text for e in body.events] == ["BTC 又晚到"]
    body = src.feed(after + timedelta(minutes=7), body.cursor)
    assert body.events == [] and "2026-10-22:3|" in body.cursor
    later = src.feed(after + timedelta(minutes=11), body.cursor)
    assert later.reset is False and later.cursor.split("@")[0] == "2026-10-23:1"


def test_live_card_version_comes_from_the_record_not_the_text() -> None:
    def card(summary: str, **out: object) -> tuple[str, object, object]:
        return classify(_rec(c.Role.COMMANDER, c.Outcome.ACTION, summary, NOW, output=out))

    stub = card("作战卡更新 v1：12 个标的（这一轮还在开会）", version=1, symbols=[])
    assert stub == ("card", "info", "v1")
    assert card("出作战卡 v399：11 个标的，7 个可做", version=399, symbols=[])[2] == "v399"
    assert card("出作战卡 v9：没有版本号字段")[0] == "commander"


def test_live_regime_only_confirmed_flips_pulse_the_market_side() -> None:
    def flip(outcome: c.Outcome, **f: object) -> tuple[str, bool]:
        ev = to_event(_rec(c.Role.SENTINEL, outcome, "大盘", NOW, output={"regime_flip": f}))
        return ev.kind, ev.routine

    assert flip(c.Outcome.ACTION, kind="confirmed", scope="market_desk") == ("regime", False)
    assert flip(c.Outcome.ACTION, kind="confirmed", scope="market_only") == ("regime_market", False)
    assert flip(c.Outcome.INFO, kind="suspect_breadth") == ("regime_watch", True)
    assert flip(c.Outcome.REJECT, kind="blocked") == ("regime_watch", True)


def test_live_executor_close_credits_whoever_caused_it() -> None:
    def close(**out: object) -> tuple[object, object]:
        ev = to_event(_rec(c.Role.EXECUTOR, c.Outcome.CLOSE, "BTCUSDT 直连平仓", NOW, output=out))
        assert ev.kind == "close"
        return ev.via, ev.badge

    assert close(reason="keeper") == ("keeper", "管家平")
    assert close(reason="stop_loss") == ("lifeboat", "止损（交易所）")
    assert close(reason="take_profit") == ("lifeboat", "止盈（交易所）")
    assert close(reason="time_limit") == ("lifeboat", "到时（交易所）")
    assert close(reason="stop_loss", detail="主网价格越过止损") == ("patrol", "模拟止损")
    assert close(reason="reconciler") == ("reconciler", "对账平")
    assert close(reason="patrol") == ("patrol", "巡逻兵平")
    assert close(reason="manual") == (None, "手动平")
    assert close() == (None, "平仓")


def test_js_human_turns_decision_log_enums_into_chinese() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("没有 node")
    source = (ROOT / "src/jiaoyi/panel/static/app.js").read_text()
    part = source[source.index("const HUMAN = [") : source.index("const tx = ")]
    script = (
        "const assert = require('node:assert/strict');\n"
        + part
        + """
assert.equal(human('ZECUSDT 空：做 take 0.67 / 止损 wide 0.67 / 仓位 S 0.84（500ms）'), 'ZEC 空：做（0.67） / 止损宽 0.67 / 仓位小 0.84（500ms）');
assert.equal(human('PENGUUSDT 多：做 skip 0.70 / 止损 narrow 0.83 / 仓位 L 0.99'), 'PENGU 多：不做（0.70） / 止损窄 0.83 / 仓位大 0.99');
assert.equal(human('HYPEUSDT 平仓（stop_loss：越过止损）'), 'HYPE 平仓（止损：越过止损）');
assert.equal(human('Jev 超时（>3000ms）'), 'Jev 超时（>3000ms）');
"""
    )
    done = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr[-3000:]
