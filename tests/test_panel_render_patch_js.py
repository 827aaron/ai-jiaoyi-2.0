# ruff: noqa: E501
"""面板重画不整页重写：内容没变跳过、变了按节点补；K 线只补最后一两根；时间 / 金额格式化器缓存后输出不变（Node 回归）。"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from tests.conftest import ROOT

# 够 morph 用的最小 DOM：子节点、属性、textContent、outerHTML
FAKE_DOM = r"""
const assert = require('node:assert/strict');
class N {
  constructor(nodeType, nodeName, nodeValue = null) { this.nodeType = nodeType; this.nodeName = nodeName; this.nodeValue = nodeValue; this.childNodes = []; this.parentNode = null; this.attrs = new Map(); }
  get id() { return this.attrs.get('id') || ''; }
  get firstChild() { return this.childNodes[0] || null; }
  get firstElementChild() { return this.childNodes.find((c) => c.nodeType === 1) || null; }
  get attributes() { return [...this.attrs].map(([name, value]) => ({ name, value })); }
  get textContent() { return this.nodeType === 1 ? this.childNodes.map((c) => c.textContent).join('') : this.nodeValue; }
  get outerHTML() { return this.nodeType === 3 ? this.nodeValue : `<${this.nodeName}${[...this.attrs].map(([k, v]) => ` ${k}="${v}"`).join('')}>${this.childNodes.map((c) => c.outerHTML).join('')}</${this.nodeName}>`; }
  getAttribute(k) { return this.attrs.has(k) ? this.attrs.get(k) : null; }
  setAttribute(k, v) { this.attrs.set(k, String(v)); }
  hasAttribute(k) { return this.attrs.has(k); }
  removeAttribute(k) { this.attrs.delete(k); }
  detach() { if (this.parentNode) this.parentNode.childNodes.splice(this.parentNode.childNodes.indexOf(this), 1); this.parentNode = null; }
  appendChild(n) { n.detach(); this.childNodes.push(n); n.parentNode = this; return n; }
  replaceChild(n, o) { n.detach(); const i = this.childNodes.indexOf(o); this.childNodes[i] = n; n.parentNode = this; o.parentNode = null; return o; }
  removeChild(o) { o.detach(); return o; }
}
const E = (name, attrs = {}, ...kids) => { const n = new N(1, name.toUpperCase()); for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v); kids.forEach((k) => n.appendChild(typeof k === 'string' ? new N(3, '#text', k) : k)); return n; };
// template.innerHTML 的解析：测试里按字符串查预先搭好的树
const PARSE = {};
const document = { createElement: () => { const t = { content: null }; Object.defineProperty(t, 'innerHTML', { set(h) { t.content = PARSE[h](); } }); return t; } };
let TZ = 'Asia/Bangkok';
"""

CASES = r"""
// 只有文字变：按钮还是原来那个节点（按下时赶上重画也不丢点击），文字换新
{
  const btn = E('button', { class: 'btn', 'data-act': 'close1' }, '平仓');
  const from = E('div', {}, btn, E('span', {}, '1 秒前'));
  morph(from, E('div', {}, E('button', { class: 'btn', 'data-act': 'close1' }, '平仓'), E('span', {}, '2 秒前')));
  assert.equal(from.childNodes[0], btn);
  assert.equal(from.textContent, '平仓2 秒前');
}
// 换了标签整个换新；属性按新标记补齐、多余的去掉；多出来的旧节点删掉，新的接在后面
{
  const keep = E('p', { class: 'a', title: 'x' }, 'p');
  const from = E('div', {}, E('span', {}, 's'), keep, E('i', {}, 'gone'));
  morph(from, E('div', {}, E('b', {}, 'b'), E('p', { class: 'b' }, 'p'), E('i', {}, 'i'), E('u', {}, 'new')));
  assert.equal(from.childNodes.map((c) => c.nodeName).join(), 'B,P,I,U');
  assert.equal(from.childNodes[1], keep);
  assert.equal(keep.getAttribute('class'), 'b');
  assert.equal(keep.hasAttribute('title'), false);
  assert.equal(from.textContent, 'bpinew');
}
// 走势图格子：标记里是空的，代码挂进去的画布不能被清掉
{
  const canvas = E('div', { class: 'chart-el' });
  const host = E('div', { id: 'kchart-host', class: 'chart' }, canvas);
  const from = E('div', {}, host);
  morph(from, E('div', {}, E('div', { id: 'kchart-host', class: 'chart' })));
  assert.equal(from.childNodes[0], host);
  assert.equal(host.childNodes[0], canvas);
  // 别的空格子照常清空
  const box = E('div', { id: 'other' }, E('i'));
  const f2 = E('div', {}, box);
  morph(f2, E('div', {}, E('div', { id: 'other' })));
  assert.equal(box.childNodes.length, 0);
}
// 输入框标记没变就留着（打到一半的字在）；标记变了换新的
{
  const inp = E('input', { id: 'pool-add', class: 'inp' });
  inp.value = 'SU';
  const from = E('div', {}, inp);
  morph(from, E('div', {}, E('input', { id: 'pool-add', class: 'inp' })));
  assert.equal(from.childNodes[0], inp);
  assert.equal(inp.value, 'SU');
  morph(from, E('div', {}, E('input', { id: 'pool-add', class: 'inp', disabled: '' })));
  assert.notEqual(from.childNodes[0], inp);
  assert.equal(from.childNodes[0].hasAttribute('disabled'), true);
}
// 展开的折叠块：同一条记录留着展开；标题换了是另一条，换新的（收起）
{
  const d = E('details', { open: '' }, E('summary', {}, '原始数据'), 'a');
  const from = E('div', {}, d);
  morph(from, E('div', {}, E('details', {}, E('summary', {}, '原始数据'), 'b')));
  assert.equal(from.childNodes[0], d);
  assert.equal(d.hasAttribute('open'), true);
  assert.equal(d.textContent, '原始数据b');
  morph(from, E('div', {}, E('details', {}, E('summary', {}, '看到的和判断'), 'c')));
  assert.notEqual(from.childNodes[0], d);
  assert.equal(from.childNodes[0].hasAttribute('open'), false);
}
// 历史页「没有平仓」↔ 资金曲线格子：整个换，格子 id 不补到别的块上，画布格子换回空块也不残留
{
  const empty = E('div', { class: 'empty' }, E('b', {}, '这段时间没有平仓'));
  const from = E('section', {}, empty);
  morph(from, E('section', {}, E('div', { class: 'chart sm', id: 'hchart-host' })));
  const host = from.childNodes[0];
  assert.notEqual(host, empty);
  assert.equal(host.id, 'hchart-host');
  assert.equal(host.textContent, '');
  host.appendChild(E('div', { class: 'curve' }));
  morph(from, E('section', {}, E('div', { class: 'empty' }, E('b', {}, '这段时间没有平仓'))));
  assert.notEqual(from.childNodes[0], host);
  assert.equal(from.childNodes[0].id, '');
  assert.equal(from.textContent, '这段时间没有平仓');
  assert.equal(from.childNodes[0].childNodes.length, 1);
}
// setHtml：同一串标记、页面没被别处整块换掉 → 不动；第一次 / 被别处写过 → 照写
{
  let parses = 0;
  PARSE['<b>2</b>'] = () => { parses++; return E('div', {}, E('b', {}, '2')); };
  const el = E('div');
  let writes = 0;
  Object.defineProperty(el, 'innerHTML', { set(h) { writes++; el.childNodes = []; el.appendChild(E('b', {}, h === '<b>1</b>' ? '1' : '?')); } });
  assert.equal(setHtml(el, '<b>1</b>'), true);
  assert.equal(writes, 1);
  const b = el.firstChild;
  assert.equal(setHtml(el, '<b>1</b>'), false);
  assert.equal(writes, 1);
  assert.equal(setHtml(el, '<b>2</b>'), true);
  assert.equal(parses, 1);
  assert.equal(writes, 1);
  assert.equal(el.firstChild, b);
  assert.equal(el.textContent, '2');
  // 出错 / 读取中的提示是直接写 innerHTML 的：同一串标记也要重新补上
  el.innerHTML = '<div>读不出</div>';
  assert.equal(setHtml(el, '<b>2</b>'), true);
  assert.equal(parses, 2);
  // 不补节点（换页）：直接整块写
  assert.equal(setHtml(el, '<b>1</b>', false), true);
  assert.equal(writes, 3);
}
// K 线：同一批往后延续（最后一根在变 / 多一根）才只补尾巴；第一根变了、中间改了、少了都整批重灌
{
  const bar = (t, c) => ({ time: t, open: 1, high: 2, low: 0.5, close: c });
  const prev = [bar(60, 1), bar(120, 1.2), bar(180, 1.3)];
  assert.equal(barsExtend(prev, [bar(60, 1), bar(120, 1.2), bar(180, 1.4)]), true);
  assert.equal(barsExtend(prev, [bar(60, 1), bar(120, 1.2), bar(180, 1.4), bar(240, 1.4)]), true);
  assert.equal(barsExtend(prev, [bar(120, 1.2), bar(180, 1.4), bar(240, 1.4)]), false);
  assert.equal(barsExtend(prev, [bar(60, 1), bar(120, 9), bar(180, 1.4)]), false);
  assert.equal(barsExtend(prev, [bar(60, 1), bar(120, 1.2)]), false);
  assert.equal(barsExtend(prev, [bar(60, 1), bar(120, 1.2), bar(180, 1), bar(240, 1), bar(300, 1)]), false);
  assert.equal(barsExtend([], [bar(60, 1)]), false);
}
// 缓存的格式化器和每次新建的输出一样；换了时区就按新时区
{
  const t = Date.UTC(2026, 9, 28, 17, 5, 9);
  assert.equal(hm(t), '00:05');
  assert.equal(hms(t), '00:05:09');
  assert.equal(md(t), '10/29');
  TZ = 'UTC';
  assert.equal(hm(t), '17:05');
  assert.equal(md(t), '10/28');
  assert.equal(money(1234.5), '1,234.5');
  assert.equal(money(1234.5, 2), '1,234.50');
  assert.equal(money(-0.004, 2), '-0.00');
  assert.equal(money(null), '—');
  assert.equal(money('12'), '12');
}
"""


def test_render_patches_in_place_and_formatters_are_cached() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("JavaScript 回归需要 Node")
    source = (ROOT / "src/jiaoyi/panel/static/app.js").read_text()
    fmt = source[source.index("const DTF = new Map();") : source.index("// U 本位合约的数量按币计")]
    patch = source[source.index("const sameHtml = ") : source.index("function render() {")]
    bars = source[source.index("function barsExtend(") : source.index("function entryMarkers(")]
    done = subprocess.run(
        [node, "-e", FAKE_DOM + fmt + patch + bars + CASES], capture_output=True, text=True
    )
    assert done.returncode == 0, done.stderr[-2000:]


# loadKlines 从入口跑：图表、接口、样式都是记账的桩
KLINE_STUBS = r"""
const assert = require('node:assert/strict');
let pageData = null, pageKey = '', hash = 'positions';
const S = { tf: '1m' };
const route = () => { const [page, arg] = hash.split('/'); return { page, arg }; };
const dataId = (page) => page + ' /api/' + page;
let log = [];
const series = {
  setData: (b) => log.push('setData ' + b.length), update: (b) => log.push('update ' + b.time),
  setMarkers: () => {}, applyOptions: () => {},
  removePriceLine: (l) => log.push('remove ' + l.title), createPriceLine: (o) => { log.push('create ' + o.title + ' ' + o.price); return o; },
};
const chart = { priceScale: () => ({ applyOptions() {} }) };
const mountChart = () => {}, updateEdges = () => {}, fitView = () => {};
let chartKey = '', priceLines = [], linePrices = [], klineSeq = 0, needFit = false, barCount = 0, entryIdx = null, lineDefs = [], lastBars = [], limitAt = '', lineSig = '', markSig = '', pxSig = '';
const document = { documentElement: {} };
const getComputedStyle = () => ({ getPropertyValue: (v) => v });
const LightweightCharts = { LineStyle: { Solid: 0, Dashed: 1, Dotted: 2, LargeDashed: 3 } };
const trailLines = () => null, entryBarIndex = () => null, pxDigits = () => 2, limitNote = () => '', entryMarkers = () => [], hm = String;
let BARS = [];
const fetch = async () => ({ ok: true, json: async () => BARS.map((b) => ({ ...b })) });
const bar = (t, c = 1) => ({ time: t, open: 1, high: 2, low: 0.5, close: c });
const pos = (id, sl) => ({ position_id: id, symbol: 'BTCUSDT', entry_price: 100, stop_loss_price: sl, take_profit_price: 110 });
const take = () => { const l = log; log = []; return l; };
"""

KLINE_CASES = r"""
(async () => {
  // 选币页的拉数换页后才回来：pageData 是选币页的，没有 positions，不能抛错
  pageData = { rows: [] }; pageKey = 'pool /api/pool';
  await loadKlines(false);
  pageData = { positions: [pos('p1', 90)] };
  await loadKlines(false);
  assert.deepEqual(take(), []);
  pageKey = 'positions /api/positions';

  BARS = [bar(60), bar(120), bar(180)];
  await loadKlines(false);
  assert.deepEqual(take(), ['setData 0', 'setData 3', 'create 止盈 110', 'create 开仓 100', 'create 固定止损计划 90']);
  // 多出一根：补上一轮最后那根和新的一根；价位没变不重建线
  BARS = [bar(60), bar(120), bar(180, 1.5), bar(240)];
  await loadKlines(false);
  assert.deepEqual(take(), ['update 180', 'update 240']);
  BARS = [bar(60), bar(120), bar(180, 1.5), bar(240, 1.2)];
  await loadKlines(false);
  assert.deepEqual(take(), ['update 240']);
  // 止损价变了：线全部重建
  pageData = { positions: [pos('p1', 95)] };
  await loadKlines(false);
  assert.deepEqual(take(), ['update 240', 'remove 止盈', 'remove 开仓', 'remove 固定止损计划', 'create 止盈 110', 'create 开仓 100', 'create 固定止损计划 95']);
  // 换周期：清空后整批重灌、线重建（价位一样也要重建）
  S.tf = '5m';
  await loadKlines(false);
  assert.deepEqual(take(), ['setData 0', 'remove 止盈', 'remove 开仓', 'remove 固定止损计划', 'setData 4', 'create 止盈 110', 'create 开仓 100', 'create 固定止损计划 95']);
  // 换一笔（价位一样）：同样清空重建
  pageData = { positions: [pos('p1', 95), pos('p2', 95)] }; hash = 'positions/p2';
  await loadKlines(false);
  assert.deepEqual(take(), ['setData 0', 'remove 止盈', 'remove 开仓', 'remove 固定止损计划', 'setData 4', 'create 止盈 110', 'create 开仓 100', 'create 固定止损计划 95']);
  // 后备止盈单在挂：按交易所触发价画它；移动止损激活后不画
  const tpLeg = (state, price, extra = {}) => ({ role: 'take_profit', state, trigger_price: price, ...extra });
  pageData = { positions: [pos('p1', 95), { ...pos('p2', 95), protection: { legs: [tpLeg('live', 109.5)] } }] };
  await loadKlines(false);
  assert.deepEqual(take(), ['update 240', 'remove 止盈', 'remove 开仓', 'remove 固定止损计划', 'create 交易所止盈单（后备） 109.5', 'create 开仓 100', 'create 固定止损计划 95']);
  pageData = { positions: [pos('p1', 95), { ...pos('p2', 95), protection: { legs: [tpLeg('pending', null, { previous: tpLeg('live', 109.5) })] } }] };
  await loadKlines(false);
  assert.deepEqual(take(), ['update 240']);
  pageData = { positions: [pos('p1', 95), { ...pos('p2', 95), protection: { legs: [tpLeg('cancelled', 109.5)] } }] };
  await loadKlines(false);
  assert.deepEqual(take(), ['update 240', 'remove 交易所止盈单（后备）', 'remove 开仓', 'remove 固定止损计划', 'create 止盈 110', 'create 开仓 100', 'create 固定止损计划 95']);
  pageData = { positions: [pos('p1', 95), { ...pos('p2', 95), protection: { trailing_activated: true, legs: [tpLeg('live', 109.5)] } }] };
  await loadKlines(false);
  assert.deepEqual(take(), ['update 240', 'remove 止盈', 'remove 开仓', 'remove 固定止损计划', 'create 开仓 100', 'create 固定止损计划 95']);
})().catch((e) => { console.error(e); process.exit(1); });
"""


def test_load_klines_patches_tail_and_rebuilds_lines_only_on_change() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("JavaScript 回归需要 Node")
    source = (ROOT / "src/jiaoyi/panel/static/app.js").read_text()
    code = source[
        source.index("async function loadKlines(") : source.index("function entryMarkers(")
    ]
    done = subprocess.run(
        [node, "-e", KLINE_STUBS + code + KLINE_CASES], capture_output=True, text=True
    )
    assert done.returncode == 0, done.stderr[-2000:]
