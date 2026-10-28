# ruff: noqa: E501
"""团队页的事件小标（大盘翻转、反向信号、预测员临时会）：落在收件的那张卡上，随时间变淡。"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from tests.test_panel_team_market import _team_js

NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="前端渲染验证需要 Node.js")
def test_regime_flip_badge_lands_on_market_or_desk() -> None:
    script = (
        _team_js()
        + r"""
assert.deepEqual(regimeFlipEvents(null), []);
const ev = (scope) => ({ ts: "2026-10-26T06:35:00+00:00", output: { regime_flip: { scope } } });
const [old] = regimeFlipEvents({ ts: "2026-10-26T06:35:00+00:00", output: {} });
assert.equal(old[0], "market");
assert.match(old[1], /06:35 大盘变化 → 独立更新/);
const [desk] = regimeFlipEvents(ev("market_desk"));
assert.equal(desk[0], "desk");
assert.match(desk[1], /大盘变化 → 大盘台/);
const html = tfEvent(desk);
assert.match(html, /class="tc-ev"/);
assert.match(html, /分钟前/);
assert.ok(!html.includes("undefined") && !html.includes("NaN"));
// 页面上：大盘台卡里多一条，不画线
const d = { roles: [], selected: null, market_progress: "wait", market: null, desk: null, market_role: null,
  against: [], predictor_meetings: [], regime_flip: { ...ev("market_desk"), text: "x" }, against_checks: [] };
const page = teamFlow(d);
const deskCard = page.slice(page.indexOf('href="#team/desk"'), page.indexOf('data-role="commander"'));
assert.match(deskCard, /大盘变化 → 大盘台/);
assert.ok(!page.includes("<svg"));
"""
    )
    done = subprocess.run([NODE or "node", "-e", script], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr[-2000:]


@pytest.mark.skipif(NODE is None, reason="前端渲染验证需要 Node.js")
def test_event_badges_fade_with_age_and_show_relative_time() -> None:
    script = (
        _team_js()
        + r"""
const NOW = Date.parse("2026-10-26T06:40:00+00:00");
Date.now = () => NOW;
const ago = (m) => new Date(NOW - m * 60000).toISOString();
const op = (html) => Number(/style="opacity:([0-9.]+)"/.exec(html)[1]);
const flip = (m, scope) => regimeFlipEvents({ ts: ago(m), output: { regime_flip: { scope } } })[0];

assert.equal(op(tfEvent(flip(3))), 1);
assert.equal(op(tfEvent(flip(10))), 1);
assert.equal(op(tfEvent(flip(20))), 0.68);
assert.equal(op(tfEvent(flip(30))), 0.35);
assert.match(tfEvent(flip(3)), /· 3 分钟前/);
assert.match(tfEvent(flip(0.2)), /· 刚刚/);

const ag = againstEvents([
  { symbol: "BTCUSDT", ts: ago(20), actions: ["meeting"] },
  { symbol: "ETHUSDT", ts: ago(5), actions: ["keeper_recheck"], keeper_verdict: "close" },
]);
assert.deepEqual(ag.map((e) => e[0]), ["keeper", "commander"]);
assert.match(tfEvent(ag[0]), /ETH .* 反向 → 管家判平 · 5 分钟前/);
assert.match(tfEvent(ag[1]), /反向 → 临时会 · 20 分钟前/);
assert.equal(op(tfEvent(ag[1])), 0.68);

const [pm] = predictorMeetingEvents([{ symbol: "ETHUSDT", ts: ago(30), bias: "long" }]);
assert.equal(pm[0], "commander");
assert.match(tfEvent(pm), /预测看反 → 临时会：改做多 · 30 分钟前/);
assert.equal(op(tfEvent(pm)), 0.35);
assert.deepEqual(predictorMeetingEvents([]), []);
"""
    )
    done = subprocess.run([NODE or "node", "-e", script], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr[-2000:]
