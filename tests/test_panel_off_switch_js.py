# ruff: noqa: E501
"""面板「关闭 / 不限」开关：编辑框旁的开关、只读时 0 的显示，全部由每项的 off 驱动（Node 渲染回归）。"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from tests.conftest import ROOT

SCRIPT = r"""
const assert = require('node:assert/strict');
const esc = (v) => String(v);
const CHOICE = {};
const choiceLabel = (k, c) => String(c);
const pageData = null;
const location = { hostname: 'example.test' };
const money = (v) => String(v);
// 假页面元素：只有开关用到的那几个属性
const els = {};
const mk = (extra) => ({ checked: false, disabled: false, value: '', handlers: {}, addEventListener(t, f) { this.handlers[t] = f; }, ...extra });
const $ = (sel) => els[sel] || null;
"""

CASES = r"""
const off = (label, shown, back) => ({ label, shown, back });
const money_i = { key: 'daily_cost_limit_usd', sec: 'budget', value: 0, off: off('不限', '不限', 10) };
const cnt_i = { key: 'event_max_per_day', sec: 'schedule', value: 12, off: off('不限', '不限', 12) };
const stale_i = { key: 'protection_stale_s', sec: 'app', value: 0, off: off('关闭', '已关闭', 120), editable: false };
const plain_i = { key: 'pool_size', sec: 'app', value: 0 };

// 只读显示：0 写成「不限」/「已关闭」，别的数照旧带单位
assert.equal(valOf(money_i), '不限');
assert.equal(valOf(stale_i), '已关闭');
assert.equal(valOf(cnt_i), '12');
assert.equal(valOf({ ...stale_i, value: 120 }), '2 分');  // 时间类照旧换成人话
assert.equal(valOf({ ...cnt_i, value: 0 }), '不限');
assert.equal(valOf(plain_i), '0 个');  // 没有 off 的项 0 不改写
assert.equal(valOf(money_i, 20), '20 美元');  // 推荐值 / 旧值走原来的格式
assert.equal(offShown({ ...cnt_i, value: 3 }), null);

// 编辑框旁边的开关：现值是 0 就勾着，写的是 off.label
let h = offSwitchHtml(money_i, false);
assert(h.includes('id="dlg-off"') && h.includes('checked') && h.includes('不限'), h);
h = offSwitchHtml(cnt_i, false);
assert(h.includes('id="dlg-off"') && !h.includes('checked'), h);
assert(!offSwitchHtml(money_i, true).includes('checked'));  // 点「用推荐值」：填的是推荐值，不勾
assert(offSwitchHtml(stale_i, false).includes('关闭'));
assert.equal(offSwitchHtml(plain_i, false), '');  // 没有 off 的项不出开关

// 勾 / 取消：输入框清空禁用 ↔ 回到现值 / 档位默认 / 表里的 back
const box = mk({ checked: true }), num = mk({ value: '' });
els['#dlg-off'] = box; els['#dlg-v'] = num;
offBind(money_i);
assert.equal(num.disabled, true);  // 一打开就是勾着的：数字框不可改
assert.equal(offPicked(money_i), true);
box.checked = false; box.handlers.change();
assert.equal(num.disabled, false);
assert.equal(num.value, 10);  // 现值 0、没有档位默认：回到表里的 back
assert.equal(offPicked(money_i), false);
box.checked = true; box.handlers.change();
assert.equal(num.disabled, true); assert.equal(num.value, '');
box.checked = false; num.value = ''; box.handlers.change();
const pinned = { ...money_i, pinned: true, profile_default: 25 };
offBind(pinned); box.checked = false; num.value = ''; box.handlers.change();
assert.equal(num.value, 25);  // 档位有默认就回到档位默认
// 档位默认本身是 0（|| 而不是 ?? ）：取消开关不能回到 0，要用表里的数
const zero = { ...money_i, pinned: true, profile_default: 0 };
offBind(zero); box.checked = false; num.value = ''; box.handlers.change();
assert.equal(num.value, 10);
// 现值是 12：勾上再取消，回到 12
const box2 = mk({ checked: false }), num2 = mk({ value: '12' });
els['#dlg-off'] = box2; els['#dlg-v'] = num2;
offBind(cnt_i);
box2.checked = true; box2.handlers.change();
assert.equal(num2.value, '');
box2.checked = false; box2.handlers.change();
assert.equal(num2.value, 12);
// 留空 = 用默认的项（max_cards_per_round）：取消开关回到留空
const nul = { key: 'max_cards_per_round', sec: 'schedule', value: null, nullable: true, off: off('不限', '不限', 10) };
const box3 = mk({ checked: true }), num3 = mk({ value: '' });
els['#dlg-off'] = box3; els['#dlg-v'] = num3;
offBind(nul); box3.checked = false; box3.handlers.change();
assert.equal(num3.value, '');
// 没有开关的项：什么都不绑、不选
delete els['#dlg-off'];
offBind(plain_i);
assert.equal(offPicked(plain_i), false);

// 日上限关了，80% 放慢那两行写「用不上」；没关时不写
const ix = { 'budget.daily_cost_limit_usd': money_i };
assert.equal(mootOf({ sec: 'schedule', key: 'over_budget' }, ix), '日上限已关闭，用不上');
assert.equal(mootOf({ sec: 'schedule', key: 'slowdown_interval_min' }, ix), '日上限已关闭，用不上');
assert.equal(mootOf({ sec: 'schedule', key: 'over_budget' }, { 'budget.daily_cost_limit_usd': { ...money_i, value: 20 } }), '');
assert.equal(mootOf({ sec: 'schedule', key: 'event_max_per_day' }, ix), '');
// 大盘翻转的定时检查关了（regime_flip_check_min = 0），其余 regime_flip_* 行写「用不上」
const flip = { 'fast_loop.regime_flip_check_min': { key: 'regime_flip_check_min', sec: 'fast_loop', value: 0, off: off('关闭', '已关闭', 5) } };
for (const k of ['regime_flip_on', 'regime_flip_breadth', 'regime_flip_min_gap_min', 'regime_flip_min_symbols']) {
  assert.equal(mootOf({ sec: 'fast_loop', key: k }, flip), '定时检查已关闭，用不上', k);
}
assert.equal(mootOf({ sec: 'fast_loop', key: 'regime_flip_check_min' }, flip), '');  // 开关本身照常
assert.equal(mootOf({ sec: 'fast_loop', key: 'regime_flip_on' }, { 'fast_loop.regime_flip_check_min': { ...flip['fast_loop.regime_flip_check_min'], value: 5 } }), '');
// 开着的「0 = 关」项比推荐更冒险，仍标黄；有推荐值才算
assert.equal(hot({ ...cnt_i, value: 0, recommended: 12 }), true);
assert.equal(hot({ ...cnt_i, value: 12, recommended: 12 }), false);
"""


def test_off_switch_rendering_is_driven_by_item_off() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("JavaScript 渲染回归需要 Node")
    source = (ROOT / "src/jiaoyi/panel/static/app.js").read_text()
    helpers = source[source.index("const NULL_AS =") : source.index("// 段.键 → 设置项")]
    assert "dlg-unl" not in source  # 不再有 event_max_per_day 的专用开关
    script = SCRIPT + helpers + CASES
    done = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr[-2000:]


def test_tier_and_settings_use_the_same_off_text() -> None:
    """档位表、成本条、类别表都走同一套：showVal 先看 off，没有按 key 写死的分支。"""
    source = (ROOT / "src/jiaoyi/panel/static/app.js").read_text()
    show = source[source.index("function showVal(") : source.index("const sameV =")]
    assert "offShown(" in show
    assert 'usd === 0 ? "不限"' in source and "usd === 0 ? 7" in source  # 费用条：不限 = 满格
    assert "capText(c.cap)" in source
    assert (
        json.dumps("event_max_per_day")
        not in source.split("function editOne")[1].split("function ")[0]
    )  # 编辑弹窗里没有按键名的特判
