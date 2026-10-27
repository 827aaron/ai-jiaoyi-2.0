# ruff: noqa: E501
"""面板密码框：真表单 + 固定用户名（浏览器只存一条凭据），密码只在内存里记住，密码错就清掉。"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from tests.conftest import ROOT

SCRIPT = r"""
const assert = require('node:assert/strict');
const esc = (v) => String(v).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');
"""

CASES = r"""
// 表单：隐藏的固定用户名 + 带 name 的密码框
let h = pwField({ pw_set: true });
assert(h.startsWith('<form id="dlg-pw-form"'), h);
assert(h.includes('<input type="text" name="username" autocomplete="username" value="jiaoyi-panel" class="vh" tabindex="-1" aria-hidden="true" readonly>'), h);
assert(h.includes('type="password" id="dlg-pw" name="password" autocomplete="current-password"'), h);
assert(h.includes('value=""'));
assert(h.includes('连错 5 次锁 15 分钟'));
assert(pwField({ pw_set: false }).includes('set-panel-password') && !pwField({ pw_set: false }).includes('<form'));

// 成功之后记住，下一个弹窗预填
rememberPw({ password: 'Abc123!x' }, true, 200, undefined);
assert.equal(panelPw, 'Abc123!x');
assert(pwField({ pw_set: true }).includes('value="Abc123!x"'));
// 没带密码的请求（不需要密码的操作）不动它
rememberPw({ kind: 'pause', password: null }, true, 200);
rememberPw({ kind: 'pause', password: null }, false, 400, '密码不对');
assert.equal(panelPw, 'Abc123!x');
// 别的失败（参数校验）不清
rememberPw({ password: 'Abc123!x' }, false, 400, '要填 0–100 的整数');
assert.equal(panelPw, 'Abc123!x');
// 密码错 / 被锁 / 401 / 403：清掉
for (const [st, d] of [[400, '密码不对（还能试 4 次）'], [400, '连错 5 次，锁定 15 分钟'], [400, '密码错误次数过多，已锁定，约 9 分钟后再试'], [401, undefined], [403, undefined]]) {
  panelPw = 'Abc123!x';
  rememberPw({ password: 'Abc123!x' }, false, st, d);
  assert.equal(panelPw, '', `${st} ${d}`);
}
assert(pwField({ pw_set: true }).includes('value=""'));
// 引号不能破坏属性
panelPw = '"><x'; assert(!pwField({ pw_set: true }).includes('value=""><x'));
"""


def test_panel_password_form_and_memory() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("JavaScript 渲染回归需要 Node")
    source = (ROOT / "src/jiaoyi/panel/static/app.js").read_text()
    memo = source[source.index("let panelPw") : source.index("async function post(")]
    field = source[source.index("function pwField(") : source.index("// 设置页常显")]
    done = subprocess.run(
        [node, "-e", SCRIPT + memo + field + CASES], capture_output=True, text=True
    )
    assert done.returncode == 0, done.stderr[-2000:]


def test_post_reports_every_response_to_the_password_memory() -> None:
    source = (ROOT / "src/jiaoyi/panel/static/app.js").read_text()
    post = source[source.index("async function post(") : source.index("// 带确认按钮的弹窗")]
    assert "rememberPw(body, r.ok, r.status, j.detail)" in post
    assert "localStorage" not in source.split("let panelPw")[1].split("// 带确认按钮的弹窗")[0]
    assert 'document.addEventListener("submit"' in source
