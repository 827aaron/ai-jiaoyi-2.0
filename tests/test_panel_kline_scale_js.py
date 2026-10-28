# ruff: noqa: E501
"""持仓页 K 线：价格轴默认贴合看得见的 K 线，超出的价位线贴边；默认只看最近 120 根（Node 回归）。"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from tests.conftest import ROOT

CASES = r"""
const assert = require('node:assert/strict');
const bars = Array.from({ length: 300 }, (_, i) => ({ time: 1000 + i * 60, low: 200 + (i % 3), high: 201 + (i % 3) }));
const lines = [194.74, 199.5, 202.75, 203.49, 195.12];

// 默认视图：最近 120 根 + 右侧 5 根空位；根数不够 120 就从 0 开始
assert.deepEqual(defaultRange(300), { from: 180, to: 304 });
assert.deepEqual(defaultRange(300, null), { from: 180, to: 304 });
// 开仓在 70 根前：从开仓前 10 根开始，看到最新 + 5
assert.deepEqual(defaultRange(300, 229), { from: 219, to: 304 });
// 开仓很早但整段不超过 600 根：照样包进来；刚好 600 算在内
assert.deepEqual(defaultRange(500, 100), { from: 90, to: 504 });
assert.deepEqual(defaultRange(600, 15), { from: 5, to: 604 });
// 开仓太早（整段超过 600 根）：退回最近 120 根
assert.deepEqual(defaultRange(2100, 100), { from: 1980, to: 2104 });
// 开仓就在前几根：from 不小于 0
assert.deepEqual(defaultRange(300, 3), { from: 0, to: 304 });
assert.deepEqual(defaultRange(50), { from: 0, to: 54 });
assert.deepEqual(defaultRange(0), { from: 0, to: 5 });

// 贴合：只看可见 K 线 + 开仓价，线再远也不撑大
const r = scaleRange(bars, { from: 180, to: 304 }, lines, 199.5, false);
assert.deepEqual(r, { min: 199.5, max: 203 });
// 显示全部价位：所有线都进来
assert.deepEqual(scaleRange(bars, { from: 180, to: 304 }, lines, 199.5, true), { min: 194.74, max: 203.49 });
// 只看前面一小段：可见根数少，范围跟着变
assert.deepEqual(scaleRange(bars, { from: 0, to: 2 }, lines, null, false), { min: 200, max: 203 });
assert.equal(scaleRange([], { from: 0, to: 5 }, lines, 1, false), null);

// 超出边距的线贴边：名字、价格、方向都在；上沿从高到低，下沿从低到高（越极端越靠边）
const defs = [
  { price: 194.74, title: '止盈' }, { price: 199.5, title: '开仓', entry: true },
  { price: 203.49, title: '保命' }, { price: 204.5, title: '固定止损' }, { price: 195.12, title: '启动线' },
];
const out = edgeLabels(defs, r, 0.09);
assert.deepEqual(out.map((d) => [d.title, d.edge]), [['固定止损', 'top'], ['保命', 'top'], ['止盈', 'bottom'], ['启动线', 'bottom']]);
assert.equal(out[0].price, 204.5);
// 贴合范围内的线（含 9% 边距内）不上边
assert.deepEqual(edgeLabels([{ price: 203.2, title: '近' }, { price: 199.5, title: '开仓' }], r, 0.09), []);
// 全显示时范围本身包含全部线，没有可贴边的
const all = scaleRange(bars, { from: 180, to: 304 }, lines, 199.5, true);
assert.deepEqual(edgeLabels(defs.slice(0, 3), all, 0.09), []);

// 边距占图高 9% = 占价格范围 9/82；刚好画在图里的线不上边
assert(Math.abs(padFrac(0.09) - 0.09 / 0.82) < 1e-12);
assert.deepEqual(edgeLabels([{ price: 203.38, title: '图内' }, { price: 203.42, title: '图外' }], r, padFrac(0.09)).map((d) => d.title), ['图外']);

// 开仓那根：最后一个不晚于开仓时间的 K 线；早于第一根 / 没时间 → null
const at = (i) => new Date((bars[i].time + 20) * 1000).toISOString();
assert.equal(entryBarIndex({ opened_at: at(229) }, bars), 229);
assert.equal(entryBarIndex({ opened_at: new Date(500 * 1000).toISOString() }, bars), null);
assert.equal(entryBarIndex({ opened_at: null }, bars), null);
assert.equal(entryBarIndex({ opened_at: at(10) }, []), null);

// 到时点：在最后一根之后才写，格式由调用方给
const fmt = (ms) => 'T' + (ms / 1000);
const lastMs = bars[bars.length - 1].time * 1000;
assert.equal(limitNote(lastMs - 600000, 0, bars, fmt), '');
assert.equal(limitNote(lastMs - 600000, 3600, bars, fmt), `到时 T${(lastMs + 3000000) / 1000} →`);
assert.equal(limitNote(lastMs - 7200000, 3600, bars, fmt), '');  // 早就过了最后一根：不写
assert.equal(limitNote(null, 3600, bars, fmt), '');
"""


def test_kline_scale_helpers() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("JavaScript 渲染回归需要 Node")
    source = (ROOT / "src/jiaoyi/panel/static/app.js").read_text()
    helpers = source[source.index("// KLINE-SCALE-BEGIN") : source.index("// KLINE-SCALE-END")]
    done = subprocess.run([node, "-e", helpers + CASES], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr[-2000:]


def test_toggle_is_remembered_and_wired() -> None:
    source = (ROOT / "src/jiaoyi/panel/static/app.js").read_text()
    assert 'clickable("kscale")' in source and "显示全部价位" in source and "贴合K线" in source
    assert (
        'localStorage.getItem("kline-scale")' in source
        and 'localStorage.setItem("kline-scale"' in source
    )
    assert "defaultRange(barCount, entryIdx)" in source  # 双击复位走默认范围
