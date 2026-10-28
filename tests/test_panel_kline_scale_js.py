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
assert.deepEqual(defaultRange(300, 229), { from: 180, to: 304 });  // 开仓 70 根前：不够 120 根就补到 120
// 开仓只有 25 根前：至少铺 120 根，开仓那根仍在里面
const fresh = defaultRange(300, 274);
assert.deepEqual(fresh, { from: 180, to: 304 });
assert(fresh.to - fresh.from >= 120 && fresh.from <= 274);
// 窄屏按约 7px 一根：宽 300 → 43 根
assert.equal(minBars(300), 43);
assert.equal(minBars(1000), 120);
assert.equal(minBars(100), 40);
assert.equal(minBars(undefined), 120);
assert.deepEqual(defaultRange(300, 274, 300), { from: 256, to: 304 });
// 一共才拉到 50 根：from 取负，左边留空，不把 50 根拉满全图
assert.deepEqual(defaultRange(50, 40), { from: -70, to: 54 });
assert.deepEqual(defaultRange(50, 2), { from: -70, to: 54 });
// 开仓比 120 根更早：从开仓前 10 根开始（不是 120）
assert.deepEqual(defaultRange(300, 100), { from: 90, to: 304 });
// 开仓很早但整段不超过 600 根：照样包进来；刚好 600 算在内
assert.deepEqual(defaultRange(500, 100), { from: 90, to: 504 });
assert.deepEqual(defaultRange(600, 15), { from: 5, to: 604 });
// 开仓太早（整段超过 600 根）：退回最近 120 根
assert.deepEqual(defaultRange(2100, 100), { from: 1980, to: 2104 });
// 开仓就在前几根：from 不小于 0
assert.deepEqual(defaultRange(300, 3), { from: 0, to: 304 });
// 窄屏：宽 300px、开仓后 75 根 → 留白至少 110px，约 ceil(110*75/190)=44 根
assert.equal(leadBars(300, 75), 44);
assert.equal(leadBars(1000, 75), 10);  // 宽屏 10 根就够
assert.equal(leadBars(0, 75), 10);  // 宽度未知 / 太窄：退回 10 根
assert.deepEqual(defaultRange(300, 229, 300), { from: 185, to: 304 });
// 留白放大后超过 600 根上限：仍退回最近 120 根
assert.deepEqual(defaultRange(2100, 1600, 300), { from: 1980, to: 2104 });
assert.deepEqual(defaultRange(50), { from: 0, to: 54 });
assert.deepEqual(defaultRange(0), { from: 0, to: 5 });

// 默认：可见 K 线 + 开仓 / 止盈 / 止损（core），保命 / 启动线再远也不撑大
const core = [194.74, 199.5, 202.75];  // 止盈、开仓、固定止损
const r = scaleRange(bars, { from: 180, to: 304 }, lines, core, false);
assert.deepEqual(r, { min: 194.74, max: 203 });
assert.deepEqual(scaleRange(bars, { from: 180, to: 304 }, [], [], false), { min: 200, max: 203 });
// 显示全部价位：所有线都进来
assert.deepEqual(scaleRange(bars, { from: 180, to: 304 }, lines, core, true), { min: 194.74, max: 203.49 });
// 只看前面一小段：可见根数少，范围跟着变
assert.deepEqual(scaleRange(bars, { from: 0, to: 2 }, lines, [], false), { min: 200, max: 203 });
assert.equal(scaleRange([], { from: 0, to: 5 }, lines, [1], false), null);

// 超出边距的线贴边：名字、价格、方向都在；上沿从高到低，下沿从低到高（越极端越靠边）
const defs = [
  { price: 194.74, title: '止盈' }, { price: 199.5, title: '开仓' },
  { price: 205, title: '保命' }, { price: 204.5, title: '启动线' }, { price: 190, title: '很远' },
];
const out = edgeLabels(defs, r, padFrac(0.09));
assert.deepEqual(out.map((d) => [d.title, d.edge]), [['保命', 'top'], ['启动线', 'top'], ['很远', 'bottom']]);
assert.equal(out[0].price, 205);
// 贴合范围内的线（止盈 / 止损 / 开仓）不上边
assert.deepEqual(edgeLabels([{ price: 194.74, title: '止盈' }, { price: 202.75, title: '止损' }, { price: 199.5, title: '开仓' }], r, padFrac(0.09)), []);
// 全显示时范围本身包含全部线，没有可贴边的
const all = scaleRange(bars, { from: 180, to: 304 }, lines, core, true);
assert.deepEqual(edgeLabels([{ price: 194.74, title: '止盈' }, { price: 203.49, title: '保命' }, { price: 195.12, title: '启动线' }], all, padFrac(0.09)), []);

// 边距占图高 9% = 占价格范围 9/82；刚好画在图里的线不上边
assert(Math.abs(padFrac(0.09) - 0.09 / 0.82) < 1e-12);
const hiEdge = 203 + 8.26 * padFrac(0.09);
assert.deepEqual(edgeLabels([{ price: hiEdge - 0.01, title: '图内' }, { price: hiEdge + 0.01, title: '图外' }], r, padFrac(0.09)).map((d) => d.title), ['图外']);

// 类似 GRASS 的空单：蜡烛 0.745-0.765，开仓 0.7616、止损 0.7859、止盈 0.7325 都进范围；保命 0.7907、启动线 0.7366 不撑大范围，在边距内就直接画
const gb = Array.from({ length: 120 }, (_, i) => ({ time: i * 60, low: 0.745 + (i % 4) * 0.001, high: 0.762 + (i % 4) * 0.001 }));
const gcore = [0.7616, 0.7859, 0.7325];
const gr = scaleRange(gb, { from: 0, to: 124 }, [...gcore, 0.7907, 0.7366], gcore, false);
assert.deepEqual(gr, { min: 0.7325, max: 0.7859 });
const gout = edgeLabels([{ price: 0.7907, title: '保命' }, { price: 0.7366, title: '赚2.00%开始跟' }, { price: 0.7859, title: '固定止损计划' }, { price: 0.7325, title: '止盈' }], gr, padFrac(0.09));
assert.deepEqual(gout, []);  // 保命 0.7907 落在上边距里，直接画在图上；更高才贴边
const gfar = edgeLabels([{ price: 0.8, title: '保命' }], gr, padFrac(0.09));
assert.deepEqual(gfar.map((d) => [d.title, d.edge]), [['保命', 'top']]);

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
    assert 'clickable("kscale")' in source and "显示全部价位" in source and "只看止损止盈" in source
    assert (
        'localStorage.getItem("kline-scale")' in source
        and 'localStorage.setItem("kline-scale"' in source
    )
    assert "defaultRange(barCount, entryIdx, c.timeScale().width())" in source  # 双击复位走默认范围
