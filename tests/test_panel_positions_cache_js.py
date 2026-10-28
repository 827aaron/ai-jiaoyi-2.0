# ruff: noqa: E501
"""持仓页不闪旧仓位：旧快照不先画、先发晚到的那轮拉数不盖新数据（Node 回归）。"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from tests.conftest import ROOT

PRELUDE = r"""
const assert = require('node:assert/strict');
const S = { hist: 'today', ai: 'today' };
let data = null, pageData = null, pageKey = '', pageErr = null, lastOk = 0, TZ = 'UTC';
const pageCache = new Map();
let hash = 'positions';
const route = () => { const [page, arg] = hash.split('/'); return { page, arg }; };
const esc = (v) => String(v);
const STALE_HINT = '';
const el = () => ({ innerHTML: '' });
const els = { '#stale': el(), '#page': el() };
const $ = (sel) => els[sel];
const sounds = [];
const tradeSounds = (list) => sounds.push(list.length);
const loadKlines = () => {};
const document = { hidden: false };
const shown = [];
const render = () => shown.push([data.positions.length, pageData && pageData.positions.length]);
// 每个请求一个可以手动放行的 promise
const pending = [];
const getJson = (url) => new Promise((resolve, reject) => pending.push({ url, resolve, reject }));
const flush = () => new Promise((r) => setImmediate(r));
const pos = (n) => ({ positions: Array.from({ length: n }, (_, i) => ({ position_id: 'p' + i, status: 'open' })) });
"""

CASES = r"""
(async () => {
  // 选中哪一笔不改请求，数据 id 一样；别的页参数照旧区分
  assert.equal(dataId('positions', 'p1'), dataId('positions', undefined));
  assert.notEqual(dataId('team', 'gate'), dataId('team', undefined));

  // 持仓快照超过 10 秒就不拿来先画；别的页的缓存不限年龄
  const id = dataId('positions');
  pageCache.set(id, { d: pos(4), at: 1000 });
  assert.equal(cachedPage('positions', id, 1000 + 9000).positions.length, 4);
  assert.equal(cachedPage('positions', id, 1000 + 11000), null);
  pageCache.set('pool x', { d: pos(1), at: 0 });
  assert.equal(cachedPage('pool', 'pool x', 3600000).positions.length, 1);
  assert.equal(cachedPage('positions', 'nothing', 0), null);

  // 两轮拉数同时在路上：先发的（4 笔，旧）晚到，不能盖掉后发的（2 笔）
  const a = poll(); await flush();
  const b = poll(); await flush();
  assert.equal(pending.length, 4);
  const [aPage, aOv, bPage, bOv] = pending;
  bPage.resolve(pos(2)); bOv.resolve(pos(2)); await b;
  aPage.resolve(pos(4)); aOv.resolve(pos(4)); await a;
  assert.deepEqual(shown, [[2, 2]]);
  assert.equal(pageData.positions.length, 2);
  assert.equal(data.positions.length, 2);
  assert.deepEqual(sounds, [2]);
  assert.equal(pageCache.get(id).d.positions.length, 2);

  // 按顺序到的照常显示
  pending.length = 0;
  const c = poll(); await flush();
  pending[0].resolve(pos(3)); pending[1].resolve(pos(3)); await c;
  assert.deepEqual(shown, [[2, 2], [3, 3]]);

  // 先发的那轮失败、晚到：不在新数据上面挂「读不出」
  pending.length = 0;
  const f = poll(); await flush();
  const g = poll(); await flush();
  pending[2].resolve(pos(3)); pending[3].resolve(pos(3)); await g;
  pending[1].reject(new Error('5 秒没有回应')); pending[0].resolve(pos(4)); await f;
  assert.equal(els['#stale'].innerHTML, '');
  assert.equal(pageData.positions.length, 3);

  // 标签页在后台：照常拉数、照常响提示音，不重画
  pending.length = 0;
  document.hidden = true;
  const n = shown.length, rang = sounds.length;
  const h = poll(); await flush();
  pending[0].resolve(pos(5)); pending[1].resolve(pos(5)); await h;
  assert.equal(shown.length, n);
  assert.equal(sounds.length, rang + 1);
  assert.equal(pageData.positions.length, 5);
})().catch((e) => { console.error(e); process.exit(1); });
"""


def test_positions_page_never_renders_stale_or_out_of_order_positions() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("JavaScript 回归需要 Node")
    source = (ROOT / "src/jiaoyi/panel/static/app.js").read_text()
    helpers = source[source.index("const PAGE_API = {") : source.index("function render() {")]
    poll = source[
        source.index("async function poll() {") : source.index(
            'if (location.pathname !== "/setup")'
        )
    ]
    done = subprocess.run(
        [node, "-e", PRELUDE + helpers + poll + CASES], capture_output=True, text=True
    )
    assert done.returncode == 0, done.stderr[-2000:]
