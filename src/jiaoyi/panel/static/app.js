// 面板前端：每 5 秒（持仓页 2 秒）拉一次 /api/overview（状态条 + 总览），当前页另有数据就再拉自己的接口。
// 数据里的文字一律经 esc() 再进 HTML。路由：#页面 或 #页面/参数（例如 #positions/pos-1、#team/gate）。
"use strict";

const $ = (s) => document.querySelector(s);
const esc = (v) =>
  String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

const PAGES = [
  ["overview", "总览", "一屏看清 AI 在做什么"],
  ["positions", "持仓", "每笔的走势图、止损止盈和保命单"],
  ["history", "历史", "每笔交易为什么开、怎么平、赚亏多少"],
  ["pool", "选币", "代码筛池子，军师从里挑"],
  ["team", "团队", "每个角色今天做了什么"],
  ["funds", "资金", "账户有多少钱、盈亏怎么来的"],
  ["settings", "设置", "上限和开关；key 只显示状态"],
];
const ICON = {
  overview: '<path d="M3 10.5 12 3l9 7.5V21H3z"/><path d="M9 21v-6h6v6"/>',
  positions: '<path d="M3 7h18v13H3z"/><path d="M8 7V4h8v3"/>',
  history: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 3"/>',
  pool: '<path d="M3 4h18l-7 9v6l-4 2v-8z"/>',
  team: '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20c.8-3.5 3.4-5.5 6.5-5.5s5.7 2 6.5 5.5"/><path d="M16 4.6a3.5 3.5 0 0 1 0 6.8M18 14.8c1.8.7 3 2.5 3.5 5.2"/>',
  funds: '<path d="M3 6h18v14H3z"/><path d="M3 10h18M16 15h2"/>',
  settings: '<path d="M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12"/><circle cx="16" cy="6" r="2"/><circle cx="10" cy="12" r="2"/><circle cx="18" cy="18" r="2"/>',
};
const MODE = { paper: "模拟盘", testnet: "测试网", live: "实盘" };
const BIAS = { long: ["偏多", "p-long"], short: ["偏空", "p-short"], both: ["双向", "p-accent"], no_trade: ["观望", ""], unknown: ["没结论", ""] };
const SIDE = { long: ["多", "up"], short: ["空", "down"] };
const LIFEBOAT = { active: "保命 ✓", pending: "⚠ 保命挂单中", missing: "⚠ 保命缺失", triggered: "保命已触发", cancelled: "保命已撤", stale: "⚠ 保命已挂·数据过期", unknown: "⚠ 保命状态未知" };
// 本页加载的 app.js 版本号（首页给 app.js 带的 ?v=）
const UI_VER = (() => {
  try {
    const s = document.querySelector('script[src*="/static/app.js"]');
    return new URL(s.src).searchParams.get("v") || "";
  } catch (e) {
    return "";
  }
})();
const ROLE = { executor: "执行器", reconciler: "对账员", lifeboat: "保命", gate: "拍板", risk_guard: "风控员", commander: "军师团", keeper: "管家", patrol: "巡逻兵", sentinel: "哨兵", screener: "选币", predictor: "预测员", alerting: "告警", book_reader: "盘口员" };
const OUTCOME = { pass: ["放行", "p-long"], reject: ["拒绝", "p-short"], hold: ["拿住", ""], close: ["平仓", "p-accent"], action: ["动作", "p-accent"], error: ["出错", "p-warn"], info: ["信息", ""] };
const CAT = { major: "主流币", alt: "山寨币", crypto_stock: "加密概念股", us_tech: "美股科技", precious_metal: "贵金属", other: "其他" };
// 币名旁的类型小字（主流币 / 山寨币 …），和作战卡同写法
const catTag = (c) => (c ? ` <span class="dim small">${esc(CAT[c] || c)}</span>` : "");
const SIGNAL = { volume_breakout: "放量突破", volume_surge: "量能突增", liquidation_rebound: "爆仓反弹", crowded_reversal: "拥挤反向", session_event: "时段与事件", oi_follow: "持仓量变化", top_vs_crowd: "大户散户分歧", btc_lead: "BTC 先动" };

let TZ = "Asia/Bangkok";
try { if (localStorage.getItem("updown") === "red-up") document.documentElement.dataset.updown = "red-up"; } catch (err) { /* 隐私模式等读不了就用默认 */ }
let data = null; // /api/overview
let pageData = null; // 当前页自己的接口
let pageKey = "";
const pageCache = new Map(); // 数据 id → { d, at }：切回来先用它画，后台再刷新，不先出「正在读取」
let pageErr = null; // [pageId, 出错说明]：读不出就显示错误，不能一直“正在读取…”
const STALE_HINT = "多半是代码更新了、面板进程还是旧的：在 Mac 终端按 Ctrl+C 停掉面板，再运行 <code>jiaoyi panel</code>。";
let lastOk = 0;
const S = { tf: "1m" };

// 建一个 Intl.DateTimeFormat 要零点几毫秒，K 线拖动时刻度每帧要格式化几十次：按时区缓存，时区变了才重建
const DTF = new Map();
const dtf = (kind, opts) => {
  const k = kind + "|" + TZ;
  let f = DTF.get(k);
  if (!f) DTF.set(k, (f = new Intl.DateTimeFormat("zh-CN", { timeZone: TZ, ...opts })));
  return f;
};
const hm = (ts) => dtf("hm", { hour: "2-digit", minute: "2-digit", hour12: false }).format(new Date(ts));
const hms = (ts) => dtf("hms", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(new Date(ts));
const md = (ts) => dtf("md", { month: "numeric", day: "numeric" }).format(new Date(ts));
const NF = new Map();
const nf = (d) => NF.get(d) || NF.set(d, new Intl.NumberFormat("en-US", { minimumFractionDigits: d, maximumFractionDigits: d })).get(d);
const money = (x, d = 1) => (x == null ? "—" : typeof x === "number" ? nf(d).format(x) : x.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d }));
// U 本位合约的数量按币计；旁边折算成 USDT 名义金额（数量 × 开仓价），一眼看出这单多大
const qtyFmt = (q) => (q == null ? "—" : q.toLocaleString("en-US", { maximumFractionDigits: 6 }));
const qty = (q, px) => `<span class="dim small">数量</span> ${qtyFmt(q)}${px ? ` <span class="dim small">· 名义 ${money(q * px, 0)} U</span>` : ""}`;
const signed = (x, d = 1) => (x == null ? "—" : (x < 0 ? "−" : "+") + money(Math.abs(x), d));
const cls = (x) => (x == null || x === 0 ? "" : x > 0 ? "up" : "down");
const coin = (s) => esc(String(s || "").replace(/USDT$/, ""));
// 价格按币安该合约的 tickSize 小数位显示（/api/price-decimals）；拿不到就按量级：≥1000 两位、1–1000 四位、<1 六位
const PREC = {};
const pxDigits = (p, sym) => (sym && PREC[sym] != null ? PREC[sym] : Math.abs(p) >= 1000 ? 2 : Math.abs(p) >= 1 ? 4 : 6);
const price = (p, sym) => (p == null ? "—" : money(p, pxDigits(p, sym)));
async function loadPrecision() {
  try {
    Object.assign(PREC, await getJson("/api/price-decimals"));
    if (Object.keys(PREC).length) render();
  } catch (e) { setTimeout(loadPrecision, 60000); } // 币安连不上：先按量级显示，一分钟后再试
}
const range = (r, unit = "", d) => (r ? `${money(r[0], d ?? (r[0] < 10 ? 1 : 0))}–${money(r[1], d ?? (r[1] < 10 ? 1 : 0))}${unit}` : "—");
const dur = (ms) => { const m = Math.max(0, Math.round(ms / 60000)); return m < 60 ? `${m} 分` : `${Math.floor(m / 60)} 时 ${m % 60} 分`; };
// 图表坐标轴按曼谷时间显示：跨天的刻度显示日期，其余显示时分
const tick = (t) => { const d = new Date(t * 1000); const s = hm(d); return s === "00:00" ? md(d) : s; };
const pill = (o) => { const [t, k] = OUTCOME[o] || [o, ""]; return `<span class="pill ${k}">${t}</span>`; };

function route() {
  const [page, arg] = (location.hash.slice(1) || "overview").split("/");
  return { page: PAGES.some((p) => p[0] === page) ? page : "overview", arg: arg ? decodeURIComponent(arg) : null };
}

/* ---------- 外壳 */
function chrome() {
  const r = route().page;
  setHtml($("#tabs"), PAGES.map(
    ([k, n]) => `<a href="#${k}" class="${k === r ? "on" : ""}"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">${ICON[k]}</svg>${n}</a>`,
  ).join(""));
  const pg = PAGES.find((p) => p[0] === r);
  const mode = data ? MODE[data.mode] || data.mode : "…";
  const now = data ? hm(data.now) : "--:--";
  const c = data ? data.control : null;
  const pauseBtn = c && c.paused
    ? `<button class="btn b-pause" ${clickable("cmd", "resume")}>恢复开新仓</button>`
    : `<button class="btn b-pause" ${clickable("cmd", "pause")}>暂停开新仓</button>`;
  setHtml($("#pghead"), `<h1>${pg[1]}</h1><span class="sub">${esc(mode)} · 曼谷 ${now} · ${pg[2]}</span>
    <div class="acts">${data && r === "team" ? `<button class="btn b-pause" ${clickable("cmd", "refresh_cards")} title="从选币开始重跑：选币 → 总盘会 → 军师团 → 写卡">重新开会</button>` : ""}${data ? pauseBtn : ""}${data ? `<button class="btn b-kill" ${clickable("cmd", "close_all")}>全部平仓</button>` : ""}</div>`);

  let b = "";
  if (data && data.mode !== "live") {
    b += `<div class="banner bn-paper"><span class="tag">${esc(mode)}</span><span>不会动真钱，资金来自币安 Demo Trading（经 hummingbot-api）</span></div>`;
  }
  if (data && !data.running) {
    const last = data.last_heartbeat ? `最后心跳 ${hm(data.last_heartbeat)}` : "还没有心跳记录";
    b += `<div class="banner bn-bad"><b>主程序没在运行</b><span>${last}。下面是落盘的最后状态；在 Mac mini 上运行 <code>jiaoyi run</code>。</span></div>`;
  }
  if (c && c.paused) {
    const why = c.paused_corrupt ? "暂停（状态文件损坏）" : "已暂停开新仓";
    const since = c.paused_since ? `${hm(c.paused_since)} 起，` : "";
    b += `<div class="banner bn-bad"><b>⏸ ${why}</b><span>${since}${esc(c.paused_reason || "")}${c.paused_reason ? "。" : ""}持仓照常盯；恢复要点右上角“恢复开新仓”并输入面板密码。</span></div>`;
  }
  if (c && c.paper_only_ignored && c.paper_only_ignored.length) {
    b += `<div class="banner bn-paper"><b>${esc(c.paper_only_ignored.join("、"))} 只在模拟盘生效，已忽略</b><span>配置里还开着，但现在不是模拟盘，主程序按“关”跑。到 设置 → 高级 → 执行 把它关掉，这条就不显示了。</span></div>`;
  }
  if (c && c.llm_credit) {
    b += `<div class="banner bn-bad"><b>💳 ${esc(c.llm_credit)}</b></div>`;
  }
  // 页面开着没刷新，服务器上的 app.js 已经换了：提示刷新（make update 后手机上常见）
  if (c && c.ui_version && UI_VER && c.ui_version !== UI_VER) {
    b += `<div class="banner bn-paper"><b>面板有新版本</b><span>你开着的页面还是旧的。</span><button class="btn b-sm b-go" onclick="location.reload()">刷新</button></div>`;
  }
  if (c && c.panel_stale) {
    b += `<div class="banner bn-bad"><b>面板版本与代码不一致</b><span>代码已更新、面板进程还是旧的，有的页面会读不出。在 Mac 终端按 Ctrl+C 停掉面板，再运行 <code>jiaoyi panel</code>。</span></div>`;
  }
  // 设置页自己有一条更详细的待生效横幅
  if (c && c.config_pending && route().page !== "settings") {
    b += `<div class="banner bn-paper"><b>配置已改、未生效</b><span>重启主程序后生效。<a href="#settings">看改了什么 ›</a></span>${data.running ? `<button class="btn b-sm b-go" ${clickable("cmd", "restart")}>重启生效</button>` : ""}</div>`;
  }
  if (data && data.kpis.halted) {
    b += `<div class="banner bn-bad"><b>⚠ 已熔断</b><span>今日亏损达到 ${data.kpis.halt_pct}%，停开新仓，曼谷 00:00 自动恢复。持仓照常盯。</span></div>`;
  }
  setHtml($("#banners"), b);
}

function statusBar() {
  if (!data) return "";
  const k = data.kpis;
  const lamps = data.lamps.map((l) => `<span class="lamp ${l.state}" title="${esc(l.detail)}"><i></i>${esc(l.label)}</span>`).join("");
  const src = k.equity_source === "ledger" ? "（账本，hummingbot 未连上）" : "";
  const used = k.to_halt_usdt != null && k.equity_usdt ? 1 - k.to_halt_usdt / (k.equity_usdt * (k.halt_pct / 100)) : null;
  const pct = k.halted ? 100 : used == null ? 0 : Math.max(0, Math.min(100, used * 100));
  return `<div class="status">
    <div class="lamps">${lamps}</div>
    <div class="kpi"><small>总资产 · 含浮盈浮亏${src}</small><b>${money(k.equity_usdt)} <span class="u">U</span></b></div>
    <div class="kpi click" ${clickable("daypnl")} title="点开看明细：平仓毛利、手续费、资金费、持仓浮盈变化、AI 费"><small>今日盈亏 · 账户 <span class="dim">ⓘ 明细</span></small><b class="${cls(k.day_pnl_usdt)}">${signed(k.day_pnl_usdt)} U <span class="p">${k.day_pnl_pct == null ? "" : signed(k.day_pnl_pct, 2) + "%"}</span></b>${dayParts(k)}${k.as_of ? `<small class="dim">数据截至 ${hms(k.as_of)}</small>` : ""}</div>
    <div class="kpi"><small>离熔断（−${k.halt_pct}%）</small><b class="${k.halted ? "down" : ""}" style="font-size:15px">${k.halted ? "已熔断" : k.to_halt_usdt == null ? "—" : "还差 " + money(k.to_halt_usdt, 0) + " U"}</b><div class="meter ${k.halted ? "full" : ""}"><span style="width:${pct}%"></span></div></div>
    <div class="kpi"><small title="所有持仓名义价值（含杠杆）加起来，和上限比；不是杠杆">总仓位（含杠杆）</small><b style="font-size:15px">${k.equity_usdt && k.exposure_x != null ? `${money(k.exposure_x * k.equity_usdt, 0)} / ${money(k.max_exposure_x * k.equity_usdt, 0)} U` : `${k.exposure_x == null ? "—" : k.exposure_x.toFixed(2)} / ${k.max_exposure_x} 倍本金`}</b>${k.equity_usdt && k.exposure_x != null ? `<div class="dim small">本金的 ${k.exposure_x.toFixed(2)} / ${k.max_exposure_x} 倍</div>` : ""}</div>
  </div>`;
}

/* ---------- 写操作：发指令、等主循环回执 */
const CMD = {
  pause: { title: "暂停开新仓", text: "不再开新仓，已有持仓照常盯、照常止损止盈。随时可以恢复。", ok: "暂停" },
  resume: { title: "恢复开新仓", text: "恢复后，有作战卡、拍板放行就会开新仓。", ok: "恢复", pw: true },
  close_all: { title: "全部平仓", text: "市价平掉所有持仓、撤掉保命单。会先自动暂停开新仓；平完以后要开新仓，得另外点“恢复开新仓”。", ok: "全部平仓", danger: true },
  close_position: { title: "平仓", text: "市价平掉这一笔，撤掉它的保命单。", ok: "平仓", danger: true },
  unblock_symbol: { title: "解封", text: "这个币因为币安公告被禁开新仓。确认公告已经不影响交易再解封。", ok: "解封", pw: true },
  restart: { title: "重启生效", text: "主程序先停开新仓、等在途的下单和平仓做完，再退出由 launchd 自动拉起，按新配置运行（约 30–60 秒）。持仓的保命止损一直挂在交易所。", ok: "重启", pw: true },
  pin_symbol: { title: "固定这个币", text: "固定后每次选币都把它放进候选池（仍要过成交量、价差这些硬条件），不受分组名额限制。会让系统多做这个币。", ok: "固定", pw: true },
  unpin_symbol: { title: "取消固定", text: "以后按打分决定它进不进候选池。", ok: "取消固定" },
  blacklist_symbol: { title: "拉黑这个币", text: "永远不进候选池，立刻不再开新仓。已有持仓照常看管、照常止损止盈。", ok: "拉黑", danger: true },
  unblacklist_symbol: { title: "取消拉黑", text: "取消后它重新参加选币，分数够就可能进池、被开仓。", ok: "取消拉黑", pw: true },
  add_symbol: { title: "手动加币", text: "把这个币加进候选池（仍要过成交量、价差这些硬条件，不够就进不去）。主程序马上重新选一次币。", ok: "加入", pw: true },
  remove_symbol: { title: "移除手动加的币", text: "撤掉手动加入，以后按打分决定它进不进候选池。", ok: "移除" },
  set_category: { title: "类别开关", text: "关掉后这一类不再靠打分进候选池，你固定、手动加的币仍然进池；打开就恢复按打分进池。主程序马上重新选币。", ok: "确定", pw: true },
  set_category_cap: { title: "这一类最多进池几个", text: "0 = 这一类一个都不要。主程序马上重新选币。", ok: "保存", pw: true, cap: true },
  reset_category: { title: "恢复默认开关", text: "去掉你在这里改过的开关，恢复默认。主程序马上重新选币。", ok: "恢复默认", pw: true },
  reset_category_cap: { title: "恢复默认上限", text: "去掉你在这里改过的上限，恢复默认。主程序马上重新选币。", ok: "恢复默认", pw: true },
  recheck_signal: { title: "重新检查", text: "按现在的价格把这个信号的开仓检查重跑一遍（拍板人 + 风控员），只看结果、不下单；结果会写进这条信号的记录里。拍板人那一步会花一点点模型费用。", ok: "重新检查" },
  release_signal: { title: "人工放行", text: "按现在的价格、作战卡上的仓位和止损止盈，跳过拒绝它的那条过滤，立刻开仓；币安的硬限制（最小下单量、杠杆上限等）照旧。开仓会写进记录并推 Telegram，记为「人工放行」。", ok: "放行开仓", danger: true, pw: true },
  refresh_cards: { title: "重新开会出卡", text: "从选币开始把整条链重跑一遍：选币 → 总盘会 → 军师团 → 写新作战卡，不沿用上一轮结论（会花一次大模型费用，要几分钟）。", ok: "开会" },
};
const STATUS = { done: ["已执行", "up"], queued: ["已排队", "up"], rejected: ["主程序拒绝了", "down"], expired: ["过期没执行（主程序没在跑？）", "down"], failed: ["执行失败", "down"] };
let onOk = null;

// 面板密码只记在这个变量里：刷新页面就没了，不进 localStorage / sessionStorage
let panelPw = "";
const isPwFailure = (status, detail) => status === 401 || status === 403 || /密码|锁定/.test(typeof detail === "string" ? detail : "");
function rememberPw(body, ok, status, detail) {
  if (!body || !body.password) return;
  if (ok) panelPw = body.password;
  else if (isPwFailure(status, detail)) panelPw = "";
}

async function post(url, body) {
  const r = await fetch(url, { method: "POST", cache: "no-store", headers: { "Content-Type": "application/json", "X-Jiaoyi-Panel": "1" }, body: JSON.stringify(body) });
  let j = {};
  try { j = await r.json(); } catch (e) { /* 非 JSON 也当失败 */ }
  rememberPw(body, r.ok, r.status, j.detail);
  if (!r.ok) throw new Error(typeof j.detail === "string" ? j.detail : `出错了（${r.status}）`);
  return j;
}

// 带确认按钮的弹窗：body 里有 input 就一起交给 submit；submit 抛错会显示在弹窗里
function ask(title, body, ok, danger, submit) {
  $("#layer").innerHTML = `<div class="scrim" data-close="1"><div class="dlg" role="dialog" aria-modal="true"><h3>${title}</h3>${body}
    <div class="msg small" id="dlg-msg"></div>
    <div class="acts"><button class="btn" data-close="1">取消</button><button class="btn ${danger ? "b-kill" : "b-go"}" data-ok="1">${ok}</button></div></div></div>`;
  onOk = submit;
  const first = $("#layer input:not(.vh)") || $("#layer [data-ok]");
  first.focus();
}
function dlgMsg(html, k = "") { const m = $("#dlg-msg"); if (m) { m.className = "msg small " + k; m.innerHTML = html; } }

function pwField(c, { prefill = true } = {}) {
  if (!c.pw_set) return `<div class="note warn">还没有设置面板密码：先在 Mac mini 终端运行 <code>jiaoyi set-panel-password</code>。</div>`;
  return `<form id="dlg-pw-form" autocomplete="on"><input type="text" name="username" autocomplete="username" value="jiaoyi-panel" class="vh" tabindex="-1" aria-hidden="true" readonly><label class="pw">面板密码<input type="password" id="dlg-pw" name="password" autocomplete="current-password" value="${prefill ? esc(panelPw) : ""}"></label></form><div class="dim small">连错 5 次锁 15 分钟。</div>`;
}

// 设置页常显：有改动没生效时高亮「重启生效」，否则是普通的「重启主程序」（验收时随时要重启）
function restartBtn() {
  const pending = data.control.config_pending === true;
  return `<button class="btn b-sm ${pending ? "b-go" : ""}" ${clickable("cmd", "restart")} ${data.running ? "" : 'disabled title="主程序没在运行"'}>${pending ? "重启生效" : "重启主程序"}</button>`;
}

// ui：弹窗用哪套文案（「恢复默认」走 reset_*，发出去的指令还是 kind）
function askCommand(kind, params = {}, what = "", ui = kind) {
  const k = CMD[ui];
  const c = data.control;
  const startedBefore = c.started_at || null;
  const warn = data.running ? "" : `<div class="note warn">主程序没在运行，指令 60 秒内没人执行就作废。</div>`;
  const title = kind === "restart" && c.config_pending !== true ? "重启主程序" : k.title;
  const extra = params.base != null ? `<p>恢复后：<b>${esc(params.base)}</b></p>` : "";
  if (params.base != null) params = Object.fromEntries(Object.entries(params).filter(([key]) => key !== "base"));
  const capField = k.cap ? `<label class="pw">最多几个（0–100 的整数）<input type="number" id="dlg-cap" min="0" max="100" step="1" inputmode="numeric" value="${esc(String(params.cap))}"></label>` : "";
  ask(`${title}${what ? " · " + esc(what) : ""}`, `<p>${k.text}</p>${extra}${warn}${capField}${k.pw ? pwField(c) : ""}`, k.ok, k.danger, async () => {
    const pw = $("#dlg-pw");
    if (k.cap) {
      const v = $("#dlg-cap").value.trim();
      if (!/^\d+$/.test(v) || +v > 100) throw new Error("要填 0–100 的整数");
      params = { ...params, cap: String(+v) };
    }
    const res = await post("/api/command", { kind, params, password: pw ? pw.value : null });
    await watchCommand(res.id, kind, startedBefore);
  });
}

// 指令发出后：弹窗只留“关闭”，等主程序回执；重启的再等新进程起来
async function watchCommand(id, kind, startedBefore, lead = "") {
  onOk = null;
  $("#layer [data-ok]").remove();
  $("#layer [data-close].btn").textContent = "关闭";
  dlgMsg(`${lead}已发给主程序，等它执行…`);
  for (let i = 0; i < 70; i++) {
    await new Promise((r) => setTimeout(r, 1000));
    if (!$("#dlg-msg")) return; // 弹窗关了
    let st;
    try { st = await getJson(`/api/command/${encodeURIComponent(id)}`); } catch (e) { continue; }
    if (st.status !== "pending") {
      const [t, cl] = STATUS[st.status] || [st.status, ""];
      dlgMsg(`${lead}<b class="${cl}">${t}</b>${st.detail ? "　" + esc(st.detail) : ""}`);
      if (kind === "restart" && st.status === "done") return waitRestart(startedBefore);
      poll();
      return;
    }
  }
  dlgMsg(`${lead}一直没有回执：主程序可能没在运行，下次启动按新配置。`, "down");
}

// 今日盈亏明细：账户今日盈亏 = 平仓毛利 + 手续费 + 资金费 + 持仓浮盈变化（含转账等）；AI 费不在账户里，另外扣
// 持仓浮盈一律显示扣掉开平两次手续费后的数（现在平掉大概能落袋多少）；拿不到费率估算就退回原始浮盈
const netPnl = (p) => (p.net_unrealized_usdt != null ? p.net_unrealized_usdt : p.unrealized_usdt);
const netPct = (p) => (p.net_pnl_pct != null ? p.net_pnl_pct : p.pnl_pct);
const feeSrc = (p) => (p.fee_from_fills ? `按最近成交实收费率 ${money(p.fee_bps / 100, 3)}%` : `估：按配置费率 ${money(p.fee_bps / 100, 3)}%，还没有平过的单可以对`);
// 现价平时是币安推送的最新成交价（和 K 线图最后一根同一个数）；推送断了退回全量行情，再不行按标记价倒推，标一下
const PX_SRC = {
  rest: ["轮询", "币安推送这会儿断了，先用一次全量行情（2 秒一次），自动重连后恢复推送"],
  mark: ["标记", "币安行情这会儿拿不到，按 hummingbot 的浮盈倒推，是标记价，和图上会差一点"],
};
const PX_ENV = { live: "币安主网", demo: "币安 Demo（模拟盘实际撮合的盘子）" };
const pxHeadTip = (d) => d.price_env
  ? `${PX_ENV[d.price_env]}实时推送的最新成交价，和下面 K 线图最后一根是同一个数；浮盈按它算。总览「今日盈亏」里的浮动和币安 App 持仓浮盈按标记价算，会差一点`
  : "这个模式没有实时行情源，按 hummingbot 的浮盈（标记价）倒推";
const pxSrc = (p) => (PX_SRC[p.price_src] ? ` <span class="dim small" title="${PX_SRC[p.price_src][1]}">${PX_SRC[p.price_src][0]}</span>` : "");
const feeTip = (p) => (p.fees_est_usdt == null || p.unrealized_usdt == null ? "" : `浮动 ${signed(p.unrealized_usdt, 2)} U − 手续费 ${money(p.fees_est_usdt, 2)} U（开仓 + 平仓各一次，${feeSrc(p)}）`);
// 今日盈亏直接拆一行：已平（净，同历史页交易净盈亏）+ 浮动（没平的单现在的浮盈）+ 其它（资金费、0 点前已有持仓的浮盈等）
function dayParts(k) {
  if (k.day_pnl_usdt == null || k.day_closed_usdt == null || k.day_floating_usdt == null) return "";
  const rest = k.day_pnl_usdt - k.day_closed_usdt - k.day_floating_usdt;
  return `<small class="dim num" title="浮动按币安标记价算（和账户总资产同一口径），持仓页按最新成交价算，会差一点">已平 ${signed(k.day_closed_usdt, 1)} · 浮动 ${signed(k.day_floating_usdt, 1)} · 其它 ${signed(rest, 1)}</small>`;
}
// 今日盈亏曲线（面板每分钟记一个点，按现在的 0 点基准算）：标最高、最低和现在
function pnlChart(c) {
  const pts = (c && c.points) || [];
  if (pts.length < 2) return `<div class="pnl-curve"><p class="dim small">曲线从面板更新后开始记，每分钟一个点，过几分钟再来看。</p></div>`;
  const W = 600, H = 220, L = 8, R = 8, T = 10, B = 10;
  const t0 = Date.parse(pts[0].ts), t1 = Date.parse(pts[pts.length - 1].ts);
  // 纵轴按数据范围（上下各留 10%），不从 0 起，小波动也看得出；0 线在范围里才画
  const vs = pts.map((p) => p.pnl_usdt);
  let lo = Math.min(...vs), hi = Math.max(...vs);
  const pad = Math.max((hi - lo) * 0.1, 1);
  lo -= pad; hi += pad;
  const x = (p) => L + ((Date.parse(p.ts) - t0) / Math.max(t1 - t0, 1)) * (W - L - R);
  const y = (v) => T + ((hi - v) / (hi - lo)) * (H - T - B);
  const path = (ps) => ps.map((p, i) => `${i ? "L" : "M"}${x(p).toFixed(1)},${y(p.pnl_usdt).toFixed(1)}`).join("");
  const line = path(pts);
  const nEst = pts.filter((p) => p.est).length;
  const estLine = nEst ? path(pts.slice(0, nEst + 1)) : "";
  const realLine = nEst ? path(pts.slice(nEst)) : line;
  const zero = lo <= 0 && 0 <= hi;
  const z = Math.min(Math.max(y(0), T), H - B).toFixed(1);
  const dot = (p, k) => (p ? `<circle class="${k}" cx="${x(p).toFixed(1)}" cy="${y(p.pnl_usdt).toFixed(1)}" r="4"/>` : "");
  const stat = (name, p) => (p ? `<span>${name} <b class="num ${cls(p.pnl_usdt)}">${signed(p.pnl_usdt, 2)} U</b> <small class="dim">${hm(p.ts)}</small></span>` : "");
  return `<div class="pnl-curve">
    <div class="pc-stats">${stat("最高", c.high)}${stat("最低", c.low)}${stat("现在", c.now)}</div>
    <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="今日盈亏曲线">
      <clipPath id="pc-up"><rect x="0" y="0" width="${W}" height="${z}"/></clipPath><clipPath id="pc-dn"><rect x="0" y="${z}" width="${W}" height="${H}"/></clipPath>
      <path class="pc-fill up" clip-path="url(#pc-up)" d="${line}L${x(pts[pts.length - 1]).toFixed(1)},${z}L${x(pts[0]).toFixed(1)},${z}Z"/>
      <path class="pc-fill dn" clip-path="url(#pc-dn)" d="${line}L${x(pts[pts.length - 1]).toFixed(1)},${z}L${x(pts[0]).toFixed(1)},${z}Z"/>
      ${zero ? `<line class="pc-zero" x1="0" x2="${W}" y1="${z}" y2="${z}"/>` : ""}
      ${estLine ? `<path class="pc-line est" d="${estLine}"/>` : ""}<path class="pc-line" d="${realLine}"/>${dot(c.high, "hi")}${dot(c.low, "lo")}${dot(c.now, "now")}
      <line class="pc-cur" x1="-9" x2="-9" y1="0" y2="${H}"/>
    </svg>
    <div class="pc-axis dim small"><span>${hm(pts[0].ts)}</span><span class="pc-read"></span><span>${hm(pts[pts.length - 1].ts)}</span></div>
    ${nEst ? `<div class="dim small">虚线（${hm(pts[0].ts)}–${hm(pts[nEst].ts)}）是面板开始记录之前的估算：按今天的成交和 5 分钟 K 线算，不含资金费，和实线接头处可能差一点。</div>` : ""}
    ${zero ? "" : `<div class="dim small">今天一直在 ${lo > 0 ? "盈利" : "亏损"}，纵轴按今天的范围画，没画 0 线。</div>`}
  </div>`;
}
// 手指 / 鼠标划过曲线：读出那一分钟的盈亏
function bindPnlChart(c) {
  const svg = $("#layer .pnl-curve svg");
  if (!svg || !c || c.points.length < 2) return;
  const pts = c.points, t0 = Date.parse(pts[0].ts), t1 = Date.parse(pts[pts.length - 1].ts);
  const read = $("#layer .pc-read"), cur = svg.querySelector(".pc-cur");
  const move = (e) => {
    const r = svg.getBoundingClientRect();
    const t = t0 + Math.min(Math.max((e.clientX - r.left) / r.width, 0), 1) * (t1 - t0);
    let best = pts[0];
    for (const p of pts) if (Math.abs(Date.parse(p.ts) - t) < Math.abs(Date.parse(best.ts) - t)) best = p;
    const bx = 8 + ((Date.parse(best.ts) - t0) / Math.max(t1 - t0, 1)) * 584;
    cur.setAttribute("x1", bx); cur.setAttribute("x2", bx);
    read.innerHTML = `${hm(best.ts)} <b class="num ${cls(best.pnl_usdt)}">${signed(best.pnl_usdt, 2)} U</b>`;
  };
  svg.addEventListener("pointermove", move);
  svg.addEventListener("pointerdown", move);
}
async function showDayPnl() {
  dlg(`<h3>今日盈亏明细</h3><div class="dim">读取中…</div>`);
  let f, curve = null;
  try {
    [f, curve] = await Promise.all([getJson("/api/funds"), getJson("/api/pnl-curve").catch(() => null)]);
  } catch (e) { return dlg(`<h3>今日盈亏明细</h3><div class="down">${esc(e.message)}</div>`); }
  const t = (f.pnl || []).find((x) => x.key === "today");
  const k = data.kpis;
  const day = k.day_pnl_usdt;
  if (!t || day == null) return dlg(`<h3>今日盈亏明细</h3>${pnlChart(curve)}<p>还没有日初权益或账户数据，算不出明细。</p>`);
  const known = t.trading_usdt + t.fees_usdt + (t.funding_usdt || 0);
  const rest = day - known;
  const row = (label, v, note = "") => `<tr><td>${label}${note ? `<div class="dim small">${note}</div>` : ""}</td><td class="r num ${cls(v)}">${v == null ? "—" : signed(v, 2) + " U"}</td></tr>`;
  const src = f.pnl_source === "exchange" ? "币安收入流水（主程序定时拉）" : "账本里今天平掉的单";
  dlg(`<h3>今日盈亏明细</h3>${pnlChart(curve)}
    <div class="tw"><table class="wrap1"><tbody>
      ${row("平仓毛利", t.trading_usdt, "今天平掉的单，扣手续费前")}
      ${row("手续费", t.fees_usdt)}
      ${row("资金费", t.funding_usdt, t.funding_usdt == null ? "没有币安收入流水，拿不到" : "")}
      ${row("持仓浮盈变化及其他", rest, "还没平的单从 0 点到现在的浮盈变化；也含转入转出、流水还没拉到的部分")}
      <tr class="hi"><td><b>账户今日盈亏</b><div class="dim small">= 总资产 − 0 点的总资产，和顶部一致</div></td><td class="r num ${cls(day)}"><b>${signed(day, 2)} U</b></td></tr>
      ${row("AI 费", -t.ai_usd, "拍板人、管家、预测员的调用费加军师团今天已花（读慢环预算文件，含正在开的会），美元按 1 U 算，不从交易账户扣")}
      <tr class="hi"><td><b>扣 AI 费后</b></td><td class="r num ${cls(day - t.ai_usd)}"><b>${signed(day - t.ai_usd, 2)} U</b></td></tr>
    </tbody></table></div>
    <p class="dim small">平仓、手续费、资金费来自${src}${k.as_of ? `；总资产截至 ${hms(k.as_of)}` : ""}。平仓赚了但账户今日盈亏还是负的，通常是手续费、别的持仓浮亏，或者流水还没拉到（这时差额先算在「持仓浮盈变化及其他」里）。曲线每分钟一个点（含浮动，按现在的 0 点总资产算），面板没开或连不上 hummingbot-api 的时段没有点。</p>`);
  bindPnlChart(curve);
}

// 交易模式：模拟盘随时切回；测试网要面板密码；实盘只在 Mac 本机，逐条勾提示 + 确认词 + 面板密码（和终端 go-live 同一套）
async function switchMode(target) {
  let m;
  try { m = await getJson("/api/mode"); } catch (e) { return dlg(`<h3>切换交易模式</h3><div class="down">${esc(e.message)}</div>`); }
  const c = data.control;
  const startedBefore = c.started_at || null;
  const name = MODE[target];
  const notes = m.cautions[target] || [];
  const warn = data.running ? "<p>切完主程序自动重启，按新模式跑。</p>" : '<div class="note warn">主程序现在没在运行：写进配置，下次启动按新模式跑。</div>';
  if (target === "live" && !m.local) {
    return dlg(`<h3>切到实盘</h3><p>切实盘只能在 Mac mini 本机操作：在 Mac 上打开 <code>http://127.0.0.1:8090</code> 再点这里（局域网打开的面板不能切）。</p>`);
  }
  const list = notes.length ? `<div class="note warn"><b>切之前看一下：</b><ul class="cautions${target === "live" ? " acks" : ""}">${notes.map((x, i) => target === "live" ? `<li><label><input type="checkbox" class="ack" data-i="${i}"> ${esc(x)}</label></li>` : `<li>${esc(x)}</li>`).join("")}</ul></div>` : "";
  let body = "", ok = `切到${name}`;
  if (target === "paper") body = `<p>切回模拟盘：不再真下单${m.armed ? "，实盘同时上锁" : ""}。</p>${warn}`;
  else if (target === "testnet") body = `<p>切到测试网${m.armed ? "（实盘同时上锁）" : ""}。</p>${list}${warn}${pwField(c)}`;
  else body = `<p>切到实盘：用真钱在币安主网下单。每条提示都勾上，再原样输入确认词和面板密码。</p>${list}${warn}
    <label class="pw">确认词（原样输入「${esc(m.confirm_word)}」）<input type="text" id="dlg-word" autocomplete="off"></label>${pwField(c, { prefill: false })}`;
  ask(`切换交易模式 · ${name}`, body, ok, target === "live", async () => {
    const pw = $("#dlg-pw");
    const payload = { mode: target, password: pw ? pw.value : null };
    if (target === "live") {
      if ([...document.querySelectorAll("#layer .ack")].some((x) => !x.checked)) throw new Error("每条提示都要勾上");
      payload.confirm_word = $("#dlg-word").value;
      payload.acknowledged = true;
    }
    const res = await post("/api/mode", payload);
    poll();
    await watchCommand(res.command_id, "restart", startedBefore, `<b class="up">已切到${name}</b>${res.armed_at ? "，实盘已解锁" : ""}${res.notified ? "，已推 Telegram" : "（Telegram 没推出去：没配或发送失败）"}。重启指令`);
  });
}

// 重启：等新进程起来（runtime.json 的启动时间变了、心跳恢复），再看配置指纹对不对得上
async function waitRestart(startedBefore) {
  const t0 = Date.now();
  while (Date.now() - t0 < 180000) {
    if (!$("#dlg-msg")) return;
    dlgMsg(`<b>重启中…</b>　已等 ${Math.round((Date.now() - t0) / 1000)} 秒`);
    await new Promise((r) => setTimeout(r, 2000));
    let ov;
    try { ov = await getJson("/api/overview"); } catch (e) { continue; }
    const c = ov.control || {};
    if (ov.running && c.started_at && c.started_at !== startedBefore) {
      if (c.config_pending === true) dlgMsg("主程序已重启，但配置和文件还是对不上：再点一次「重启生效」，或看 data/logs/。", "down");
      else dlgMsg(`<b class="up">已按新配置重启</b>　${hm(c.started_at)} 启动`);
      poll();
      return;
    }
  }
  dlgMsg("3 分钟还没起来：在 Mac 上看 <code>launchctl list | grep jiaoyi</code> 和 data/logs/。", "down");
}

/* ---------- 弹窗 */
function dlg(html) {
  $("#layer").innerHTML = `<div class="scrim" data-close="1"><div class="dlg" role="dialog" aria-modal="true">${html}<div class="acts"><button class="btn" data-close="1">关闭</button></div></div></div>`;
  $("#layer .acts .btn").focus({ preventScroll: true });
}
function closeDlg() { $("#layer").innerHTML = ""; onOk = null; clearTimeout(meetTimer); }
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeDlg();
  if (e.key === "Enter" && e.target.closest && e.target.closest("#layer") && e.target.tagName === "INPUT") { e.preventDefault(); const b = $("#layer [data-ok]"); if (b) b.click(); }
});
document.addEventListener("submit", (e) => { if (e.target.id === "dlg-pw-form") { e.preventDefault(); const b = $("#layer [data-ok]"); if (b) b.click(); } });
document.addEventListener("click", async (e) => {
  if (e.target.dataset && e.target.dataset.close) return closeDlg();
  if (e.target.dataset && e.target.dataset.ok && onOk) {
    const btn = e.target;
    btn.disabled = true;
    dlgMsg("处理中…");
    try { await onOk(); } catch (err) { dlgMsg(esc(err.message), "down"); } finally { btn.disabled = false; }
    return;
  }
  const el = e.target.closest("[data-act]");
  if (!el) return;
  const { act, arg } = el.dataset;
  if (act === "card") showCard();
  else if (act === "cardhist") showCardHistory(arg);
  else if (act === "signal") showSignal(arg);
  else if (act === "sigcmd") {
    const [kind, symbol, ...rest] = arg.split(":");
    askCommand(kind, { signal_id: rest.join(":"), symbol }, symbol.replace(/USDT$/, ""));
  }
  else if (act === "meeting") showMeeting(arg);
  else if (act === "record") showRecord(+arg);
  else if (act === "tf") { S.tf = arg; loadKlines(true); render(); }
  else if (act === "kscale") {
    S.scaleAll = !S.scaleAll;
    try { localStorage.setItem("kline-scale", S.scaleAll ? "all" : "fit"); } catch (err) { /* 记不住就只对这次生效 */ }
    render();
    if (chart) { chart.priceScale("right").applyOptions({ autoScale: true }); updateEdges(); }
  }
  else if (act === "chartfull") setChartFull(!S.chartFull);
  else if (act === "hist") { S.hist = arg; render(); poll(); }
  else if (act === "ai") { S.ai = arg; render(); poll(); }
  else if (act === "rej") { S.rej = !S.rej; render(); }
  else if (act === "symsort") { S.symSort = arg; render(); }
  else if (act === "symopen") { if (S.symOpen.has(arg)) S.symOpen.delete(arg); else S.symOpen.add(arg); render(); }
  else if (act === "tests") { S.tests = !S.tests; render(); poll(); }
  else if (act === "csv") exportCsv();
  else if (act === "cmd") askCommand(arg);
  else if (act === "update") askUpdate();
  else if (act === "close1") { const p = (pageData && pageData.positions || []).find((x) => x.position_id === arg); askCommand("close_position", { position_id: arg }, p ? `${p.symbol.replace(/USDT$/, "")} ${(SIDE[p.side] || [p.side])[0]} ${p.quantity}` : arg); }
  else if (act === "unblock") askCommand("unblock_symbol", { symbol: arg }, arg);
  else if (act === "edit1") editOne(arg, false);
  else if (act === "rec1") editOne(arg, true);
  else if (act === "st-item") toggleItem(el, arg);
  else if (act === "st-role") toggleRole(arg);
  else if (act === "st-changes") showChanges();
  else if (act === "profile") switchProfile(arg);
  else if (act === "tier") { S.tierSel = arg; render(); }
  else if (act === "tier-cancel") { S.tierSel = null; render(); }
  else if (act === "tier-go") switchProfile(arg);
  else if (act === "tier-filter") { S.tierOnlyCustom = arg === "1"; render(); }
  else if (act === "tier-role") toggleTierRole(arg);
  else if (act === "st-more") openRole("risk_guard");
  else if (act === "daypnl") showDayPnl();
  else if (act === "prompts") showPrompts();
  else if (act === "mode") switchMode(arg);
  else if (act === "hold") switchHold(arg);
  else if (act === "clearpins") askClearPins();
  else if (act === "cat") { const [kind, cat, v, base] = arg.split(":"); askCommand(kind, { category: cat, [kind === "set_category" ? "enabled" : "cap"]: v, ...(v === "default" ? { base } : {}) }, CAT[cat] || cat, v === "default" ? `reset_${kind.slice(4)}` : kind); }
  else if (act === "pool") { const [kind, sym] = arg.split(":"); askCommand(kind, { symbol: sym }, sym.replace(/USDT$/, "")); }
  else if (act === "pooladd") addSymbol();
  else if (act === "unpin") unpin(arg);
  else if (act === "sound") { setSoundOn(arg === "on"); render(); }
  else if (act === "sound-test") playTone(arg);
  else if (act === "updown") {
    try { localStorage.setItem("updown", arg); } catch (err) { /* 存不了就只对这次生效 */ }
    location.reload();
  }
});
document.addEventListener("keydown", (e) => {
  if ((e.key === "Enter" || e.key === " ") && e.target.dataset && e.target.dataset.act) { e.preventDefault(); e.target.click(); }
});
const clickable = (act, arg = "") => `data-act="${act}" data-arg="${esc(arg)}" tabindex="0" role="button"`;
const json = (o) => (o && Object.keys(o).length ? `<pre class="json">${esc(JSON.stringify(o, null, 2))}</pre>` : `<span class="dim">（空）</span>`);

const splitIdle = (list) => [list.filter((s) => s.bias !== "no_trade"), list.filter((s) => s.bias === "no_trade")];
const idleLine = (idle) => `观望：${idle.slice(0, 6).map((s) => coin(s.symbol)).join("、")}${idle.length > 6 ? " 等" : ""} 共 ${idle.length} 个`;

// 作战卡：每个币默认折叠成一行（方向 / 止损 / 止盈 / 杠杆 / 仓位 / 到期），点开看计划、价格刻度、军师原话和开会过程。
// 止损 / 止盈是风控员真会用的口径：距离 = 卡上倍数 × ATR（/api/card-atr，√(短×长) × 时段系数），止盈 = 止损距离 × 止盈倍数下限；
// 信号来了按那一刻的价进，卡上按现价估，所以价前面带「≈」。军师原话里的价位不解析，只在折叠里标「参考」。
S.atr = {};
const TIER = { S: "小", M: "中", L: "大", low: "低", high: "高" };
const tierTip = (c) => {
  const f = c.size_fractions || {};
  const pct = (k) => (f[k] == null ? "" : `${TIER[k]} = 每笔风险的 ${Math.round(f[k] * 100)}%`);
  return "军师团给的仓位上限档，拍板人选的档不超过它，风控员按档打折" + (f.S != null ? `：${["S", "M", "L"].map(pct).join("，")}` : "");
};
const DIR = { long: ["做多", "p-long"], short: ["做空", "p-short"], both: ["双向", "p-accent"] };
const signedPct = (a, b) => { const p = (a - b) / b * 100; return `${p >= 0 ? "+" : "−"}${Math.abs(p).toFixed(2)}%`; };
// 风控员算的距离（不随成交价回填变）：止损 / 保命在亏钱一侧，止盈在赚钱一侧
const guardPct = (pct, side, loss) => `${(side === "long") === loss ? "−" : "+"}${pct.toFixed(2)}%`;
const lbPct = p => p.lifeboat_stop_pct != null ? ` <span class="dim">${guardPct(p.lifeboat_stop_pct, p.side, true)}</span>` : "";
// 只显示执行器实际观测；K 线和现价不能证明移动止损已启动。
const TRAIL_TIP = "状态来自执行器实际止损检查，超过 15 秒显示未知；K 线高低不用于判断。执行器内部按扣实际费用后的净收益阈值检查，未提供精确价格时不画移动止损价线。";
function trailNote(p) {
  const t = p.stop_diagnostics;
  const head = `移动止损配置：净收益赚到 ${(+p.trailing_activation_pct).toFixed(2)}% 开始跟，回撤距离 ${(+p.trailing_stop_pct).toFixed(2)}%。`;
  if (p.protection) {
    const tl = trailLines(p), bk = p.side === "long" ? "买一" : "卖一";
    if (!p.protection.trailing_activated) return head + (tl ? `程序记录未激活：${bk}越过 ${price(tl.start, p.symbol)} 开始跟（按实际成交额和手续费算，和程序判断同一公式）。` : "程序记录未激活；启动价要等程序第一次看盘口后才算得出。");
    const h = tl && tl.held;
    const tail = tl && tl.confirmed ? `；交易所已挂移动止损单 ${price(tl.stop, p.symbol)}` : h ? `；比在挂的${h.name} ${price(h.price, p.symbol)} ${h.looser ? "松" : "紧但收紧不够一步或标记价不可用"}，未换挂` : "；交易所还没确认挂上这一价（以下方保护单状态为准）";
    return head + `程序记录已激活${p.protection.trailing_stop_price == null ? "" : `，移动止损价 ${price(p.protection.trailing_stop_price, p.symbol)}`}${tail}。`;
  }
  if (!t || t.status !== "current" || t.trailing_active == null) return head + `实际状态未知：${esc(t?.reason || "执行器尚未提供观测")}。`;
  return head + (t.trailing_active ? `执行器确认已启动；当前净收益触发阈值 ${t.actual_trailing_trigger_net_pnl_pct == null ? "未知" : signed(t.actual_trailing_trigger_net_pnl_pct, 4) + "%"}。` : (t.coverage_complete ? "执行器本轮检查未激活。" : "执行器本轮检查未激活，此前记录不完整。")) + " 未提供精确动态触发价，不绘制动态价格线。";
}
function trailTag(p, brief) {
  if (p.trailing_stop_pct == null) return `<div class="small dim" title="开仓那会儿移动止损没开（设置 → 持仓管家），这笔不会跟着收紧">没带移动止损</div>`;
  if (p.protection) {
    const on = p.protection.trailing_activated, tl = trailLines(p), back = `${(+p.trailing_stop_pct).toFixed(2)}%`;
    const more = brief ? "" : on ? (tl ? ` ${price(tl.stop, p.symbol)}${tl.confirmed ? "" : tl.held ? ` <span class="dim">未换挂（在挂${tl.held.name} ${price(tl.held.price, p.symbol)}）</span>` : ' <span class="dim">程序记录（未确认）</span>'}` : "") : ` ${tl ? `到 ${price(tl.start, p.symbol)} 开始跟，` : ""}回撤距离 ${back}`;
    return `<div class="small ${on ? "" : "dim"}"><span class="pill ${on ? "p-accent" : ""}">${on ? (tl && tl.confirmed ? "移动止损跟踪中 · 交易所已挂" : "移动止损跟踪中 · 程序记录") : "移动止损未激活"}</span>${more}</div>`;
  }
  const t = p.stop_diagnostics, tip = esc(TRAIL_TIP);
  if (!t || t.status !== "current" || t.trailing_active == null) return `<div class="small warn" title="${tip}">移动止损 · 状态未知${t?.status === "stale" ? "（观测过期）" : ""}</div>`;
  if (t.trailing_active) return `<div class="small" title="${tip}"><span class="pill p-accent">跟踪中 · 执行器确认</span>${brief || t.actual_trailing_trigger_net_pnl_pct == null ? "" : ` 净收益阈值 ${signed(t.actual_trailing_trigger_net_pnl_pct, 4)}%`}</div>`;
  return `<div class="small dim" title="${tip}"><span class="pill">本轮未激活${t.coverage_complete ? "" : "（此前记录不完整）"}</span></div>`;
}
function stopDiagnostics(p) {
  const t = p.stop_diagnostics, b = p.book_quote;
  const stamp = ts => ts ? esc(new Date(ts).toLocaleString("zh-CN", { timeZone: TZ, hour12: false })) : "未知";
  const book = b ? `盘口参考：买一 ${price(b.bid, p.symbol)} / 卖一 ${price(b.ask, p.symbol)} · ${b.environment === "demo" ? "Demo" : "主网"} REST · 获取 ${stamp(b.observed_at)}（不是执行器检查输入）` : "盘口参考：未取得买一 / 卖一";
  const basis = { mark: "标记价", mark_price: "标记价", bid: "买一", ask: "卖一", best_bid: "买一", best_ask: "卖一", mid: "中间价", mid_price: "中间价", last: "最新成交价", last_price: "最新成交价" };
  const state = t?.status === "current" ? `检查价格 ${price(t.decision_price, p.symbol)} · 口径 ${basis[t.price_basis] || "未知"} · 检查时买一 ${price(t.bid, p.symbol)} / 卖一 ${price(t.ask, p.symbol)} · 净收益 ${t.net_pnl_pct == null ? "未知" : signed(t.net_pnl_pct, 4) + "%"} · 固定止损净收益阈值 ${t.stop_loss_net_pnl_pct == null ? "未知" : signed(t.stop_loss_net_pnl_pct, 4) + "%"}` : `实际检查状态未知：${esc(t?.reason || "未提供执行器观测")}`;
  const coverage = t ? `覆盖从 ${stamp(t.coverage_since)} · ${t.coverage_complete && !t.gap_detected && !t.dropped && !t.write_errors && !t.oversize && !t.observer_errors ? "采样器报告覆盖完整" : "覆盖不完整或未知"} · 丢弃 ${t.dropped || 0} / 写入错误 ${t.write_errors || 0} / 超限 ${t.oversize || 0} / 观测错误 ${t.observer_errors || 0} / 事件窗口淘汰 ${t.event_overflow || 0}` : "覆盖未知";
  const stage = { check: "触发检查", close_request: "平仓流程请求", close_return: "平仓流程返回（不代表下单或成交）", close_error: "平仓流程出错", order_request: "订单提交申请", order_return: "订单提交返回（不代表交易所接受或成交）", order_error: "订单提交出错", fill: "成交回报（原生回调）", order_completed: "订单完成回报（原生回调）" };
  const kind = { stop_loss: "固定止损", trailing_stop: "移动止损", take_profit: "止盈", time_limit: "到时", stop_check: "止损检查", native_order: "执行器订单", native_order_filled: "原生订单成交", native_order_completed: "原生订单完成" };
  const events = (t?.events || []).slice().reverse().map(e => `<div><span class="n">${stamp(e.ts)}</span><span>${e.stage === "check" ? (e.check_action === "activation" ? "移动止损启动条件检查" : e.check_action === "close" ? "止损平仓条件检查" : "止损条件检查（动作未知）") : stage[e.stage] || "未知阶段"}${e.stage === "check" ? (e.condition_met === true ? " · 条件满足" : e.condition_met === false ? " · 条件未满足" : " · 条件结果未知") + (e.close_requested === true ? " · 已请求平仓流程" : e.close_requested === false ? " · 本次未请求平仓流程" : " · 平仓请求状态未知") : ""} · ${kind[e.kind] || "执行器事件"} · 价 ${price(e.price, p.symbol)}${e.fill_amount == null ? "" : ` · 成交数量 ${qtyFmt(e.fill_amount)}`}${e.threshold == null ? "" : ` · 净收益 ${esc(e.comparison || "阈值")} ${signed(e.threshold, 4)}%`}${e.check_seq ? ` · 检查序号 ${e.check_seq}` : ""}</span></div>`).join("");
  return `<div class="note small"><div>${book}</div><div>${state}</div><div>源 ${esc(t?.source || "未知")} · 采样 ${stamp(t?.observed_at)} · 实际检查 ${stamp(t?.check_at)} · 序号 ${t?.check_seq || "未知"}</div><div>${coverage}。仅凭这些记录不能断言没有漏触发。</div><div>固定止损线来自账本计划价；执行器按实际费用后的净收益检查，成交还取决于订单回报。</div></div><div class="lbl">执行器止损事件（最近 ${t?.events?.length || 0} 条）</div><div class="log trail scroll tall">${events || '<div class="dim">没有可用止损事件，无法据此判断是否漏触发。</div>'}</div><div class="small dim">触发判定、下单申请和成交是不同阶段；真实成交见交易流水。</div>`;
}
const trailLegend = (p) => {
  const tl = trailLines(p);
  if (!tl) return "";
  if (tl.start != null) return `<span><i style="border-color:var(--accent);border-style:dashed;opacity:.45"></i>移动止损启动线（越过开始跟）</span>`;
  return tl.confirmed ? `<span><i style="border-color:var(--accent);border-style:solid"></i>移动止损（交易所已挂）</span>` : `<span><i style="border-color:var(--accent);border-style:dashed;opacity:.45"></i>移动止损（程序记录，${tl.held ? "未换挂" : "未确认"}）</span>`;
};
// 托管保护仓位的移动止损价线：启动价由服务端按执行层同一公式算；激活后优先用交易所确认在挂的移动止损腿
function trailLines(p) {
  const q = p.protection;
  if (!q || p.trailing_stop_pct == null) return null;
  if (!q.trailing_activated) return q.trailing_activation_price == null ? null : { start: q.trailing_activation_price };
  const l = protLeg(p, "stop_loss"), s = l && legShown(l);
  if (s && s.purpose === "trailing" && s.state === "live" && s.trigger_price != null) return { stop: s.trigger_price, confirmed: true };
  if (q.trailing_stop_price == null) return null;
  const tsp = q.trailing_stop_price;
  // 执行层只在移动止损比在挂的止损紧够一步、且标记价可用时才换挂；在挂的是别的止损就是没换
  if (s && s === l && s.state === "live" && s.trigger_price != null) return { stop: tsp, confirmed: false, held: { name: legName(s), price: s.trigger_price, looser: p.side === "long" ? tsp <= s.trigger_price : tsp >= s.trigger_price } };
  return { stop: tsp, confirmed: false };
}
// 交易所托管保护单（positions.json 的 server_protection，只读）：每条腿的真实状态，价格只用交易所确认过的
const LEG_ST = { live: "已挂", triggered: "已触发", cancelled: "已撤", rejected: "被拒", pending: "提交中·未确认", unknown: "状态未知", none: "无记录" };
const PROT_STAGE = { entry: "入场中", provisional: "临时保护", armed: "已武装", exiting: "平仓中", cleanup: "清理中", done: "已结束" };
const protLeg = (p, role) => p.protection && p.protection.legs.find((l) => l.role === role);
const STOP_PURPOSE = { fixed: "固定止损", trailing: "移动止损", tightened: "收紧止损" };
const legName = (l) => l.role === "lifeboat" ? "保命" : l.role === "take_profit" ? "止盈" : STOP_PURPOSE[l.purpose] || "其他止损";
const legBad = (l) => l.state !== "live";
// 触发价格源：每条腿建单时记下，旧腿和新腿可能不同（改设置只影响之后新挂的）
const WT_CN = { MARK_PRICE: "标记价", CONTRACT_PRICE: "成交价" };
const wtTag = (l) => WT_CN[l.working_type] ? ` <span class="dim small" title="交易所按${WT_CN[l.working_type]}触发这张单">${WT_CN[l.working_type]}</span>` : "";
// 最新一代还没挂上（提交中 / 被拒 / 不明）而上一代仍在挂：真正在保护的是上一代，主显示它
const legShown = (l) => (l.previous && l.state !== "live" ? l.previous : l);
const legNewer = (p, l) => l.previous && l.state !== "live" ? `<div class="small ${l.state === "pending" ? "dim" : "warn"}">新一代 ${legPrice(p, l)} ${LEG_ST[l.state]}${l.detail ? ` · ${l.detail}` : ""}，旧一代仍挂着</div>` : "";
const secText = (s) => s == null ? "未知" : s < 60 ? `${Math.round(s)} 秒前` : `${dur(s * 1000)}前`;
function legPrice(p, l) {
  if (l.trigger_price != null) return price(l.trigger_price, p.symbol) + wtTag(l);
  return l.planned_price != null ? `<span class="dim">计划 ${price(l.planned_price, p.symbol)}（未确认）</span>${wtTag(l)}` : "—";
}
function protBrief(p) {
  const q = p.protection;
  if (!q) return "";
  const legs = q.legs.filter((l) => l.role !== "lifeboat").map((l) => { const s = legShown(l); return `<span class="${legBad(s) ? "warn" : ""}">${legName(s)}${LEG_ST[s.state]}${s !== l ? `（新一代${LEG_ST[l.state]}）` : ""}</span>`; }).join(" · ");
  const tight = q.tightened_stop_price != null ? ` · <span class="pill p-accent" title="${esc(q.tightened_reason || "")}">反向卡收紧止损 ${price(q.tightened_stop_price, p.symbol)}</span>` : q.opposite_anchor_price != null ? ` · <span class="pill p-accent" title="${esc(q.tightened_reason || "")}">反向卡锚定移动止损 ${price(q.opposite_anchor_price, p.symbol)}</span>` : "";
  return `<div class="small dim">${PROT_STAGE[q.stage] || esc(q.stage)} · ${legs}${tight}${q.stale ? ` · <span class="warn">数据过期（${secText(q.age_s)}）</span>` : ""}${q.last_error ? ` · <span class="warn" title="${esc(q.last_error)}">有错误</span>` : ""}${q.trailing_error ? ` · <span class="warn" title="${esc(q.trailing_error)}">移动止损本轮没更新</span>` : ""}</div>`;
}
function protBlock(p) {
  const q = p.protection;
  const row = (l, tag) => `<div><span class="n">${legName(l)}${tag}</span><span class="${legBad(l) ? "warn" : ""}"><b>${LEG_ST[l.state]}</b>${l.detail ? ` · ${l.detail}` : ""} · 触发价 ${legPrice(p, l)}${l.quantity != null ? ` · 数量 ${qtyFmt(l.quantity)}` : ""}${l.algo_status ? ` · 交易所状态 ${esc(l.algo_status)}` : ""}${l.child_state ? ` · 子单 ${{ absent: "确认没有", unknown: "未确认", identified: "已出现" }[l.child_state] || esc(l.child_state)}` : ""}</span></div>`;
  const rows = q.legs.map((l) => l.previous && l.state !== "live" ? row(l, "·新一代") + row(l.previous, "·旧一代仍挂") : row(l, "")).join("");
  const trail = q.trailing_stop_price != null ? ` · 移动止损价 ${price(q.trailing_stop_price, p.symbol)}${q.trailing_activated ? "（已激活）" : ""}` : "";
  const oppWhen = q.tightened_at ? `（${new Date(q.tightened_at).toLocaleString("zh-CN", { timeZone: TZ, hour12: false })}）` : "";
  const oppWhy = q.tightened_reason ? ` · ${esc(q.tightened_reason)}` : "";
  const tight = q.opposite_anchor_price != null && q.tightened_stop_price == null ? `<div>反向作战卡锚定移动止损：锚定价 ${price(q.opposite_anchor_price, p.symbol)}${q.trailing_distance_pct != null ? ` · 回撤距离 ${(+q.trailing_distance_pct).toFixed(2)}%` : ""}${oppWhen}${oppWhy}（程序记录，之后移动止损只收紧；以下方止损腿的交易所确认状态为准）</div>` : q.tightened_stop_price != null ? `<div>反向作战卡收紧止损：目标 ${price(q.tightened_stop_price, p.symbol)}${q.tightened_at ? `（${new Date(q.tightened_at).toLocaleString("zh-CN", { timeZone: TZ, hour12: false })}）` : ""}${q.tightened_reason ? ` · ${esc(q.tightened_reason)}` : ""}（程序记录，以下方止损腿的交易所确认状态为准；卡转回不会自动放松）</div>` : "";
  const age = q.observed_at ? `${secText(q.age_s)}（${new Date(q.observed_at).toLocaleString("zh-CN", { timeZone: TZ, hour12: false })}）` : "没有观测时间";
  return `<div class="note small"><div>交易所托管保护 · 阶段 ${PROT_STAGE[q.stage] || esc(q.stage)}${q.ever_armed ? " · 曾完整武装" : " · 还没完整武装"}${trail}</div>${tight}<div class="${q.stale ? "warn" : ""}">数据 ${age}${q.stale ? " · 已过期，下面状态可能不是最新" : ""}</div>${q.last_error ? `<div class="warn">最近错误：${esc(q.last_error)}</div>` : ""}${q.trailing_error ? `<div class="warn">移动止损本轮没更新：${esc(q.trailing_error)}${q.trailing_error_book ? "（已挂的保护不受影响）" : ""}</div>` : ""}<div class="dim">状态来自执行层落盘的账本，面板不连交易所；只有“交易所已确认 + 子单确认没有”才算已挂。</div></div><div class="log trail">${rows}</div>`;
}
// 到这个价成交的预估净盈亏（U）：数量 ×（价 − 开仓价），再扣开仓 + 平仓各一次吃单费；没有费率 / 价就不估
function lvlPnl(p, lvl) {
  if (lvl == null || p.entry_price == null || !p.quantity) return null;
  const gross = p.quantity * (lvl - p.entry_price) * (p.side === "long" ? 1 : -1);
  const fee = p.fee_bps == null ? 0 : p.quantity * (p.entry_price + lvl) * p.fee_bps / 10000;
  return gross - fee;
}
const lvlEst = (p, lvl) => { const v = lvlPnl(p, lvl); return v == null ? "" : `<span class="num ${cls(v)}">${signed(v, 0)} U</span>`; };
const LVL_TIP = "到这个价成交时预估的净盈亏：数量 ×（价 − 开仓价），再扣开仓 + 平仓各一次的吃单手续费。滑点、资金费没算。";
// 止损格只显示账本固定计划价；没有可信动态触发价就不计算其预估收益。
function slCell(p) {
  const l = protLeg(p, "stop_loss");
  if (l) { const s = legShown(l); return `${legPrice(p, s)} <span class="${legBad(s) ? "warn" : "dim"} small">${legName(s)}·${LEG_ST[s.state]}</span>${legNewer(p, l)}<div class="small" title="${LVL_TIP}">${lvlEst(p, s.trigger_price)}</div>`; }
  const lvl = p.stop_loss_price;
  return `${price(lvl, p.symbol)} <span class="dim small">固定计划</span><div class="small" title="${LVL_TIP}">${lvlEst(p, lvl)}</div>`;
}
function tpCell(p) {
  const l = protLeg(p, "take_profit");
  if (!l) return `${price(p.take_profit_price, p.symbol)}<div class="small" title="${LVL_TIP}">${lvlEst(p, p.take_profit_price)}</div>`;
  const s = legShown(l);
  return `${legPrice(p, s)} <span class="${legBad(s) ? "warn" : "dim"} small">${LEG_ST[s.state]}</span>${legNewer(p, l)}<div class="small" title="${LVL_TIP}">${lvlEst(p, s.trigger_price)}</div>`;
}
const cardPos = (sym) => ((data && data.positions) || []).find((p) => p.symbol === sym && p.entry_price);

// 系统值：持仓用持仓自己的开仓 / 止损 / 止盈；没持仓按现价 + ATR 估（双向只给距离）
function cardLevels(s) {
  const p = cardPos(s.symbol);
  if (p) {
    const r = p.stop_loss_price && p.take_profit_price ? Math.abs(p.take_profit_price - p.entry_price) / Math.abs(p.entry_price - p.stop_loss_price) : null;
    return { held: p, entry: p.entry_price, px: p.mark_price || null, stop: p.stop_loss_price ? [p.stop_loss_price, p.stop_loss_price] : null, stopMid: p.stop_loss_price, tp: p.take_profit_price ? [p.take_profit_price, p.take_profit_price] : null, tpMid: p.take_profit_price, r };
  }
  const a = S.atr[s.symbol];
  if (!a || !s.stop_atr || !s.take_profit_r) return null;
  // 风控员开着「夹到推荐范围」时，越界的那截不要（没交集时风控员会拒单，这里照原样显示）
  const clamp = (r, c) => (c && Math.max(r[0], c[0]) <= Math.min(r[1], c[1]) ? [Math.max(r[0], c[0]), Math.min(r[1], c[1])] : r);
  const sa = clamp(s.stop_atr, a.stop_atr_clamp), tr = clamp(s.take_profit_r, a.tp_r_clamp);
  const R = tr[0], d = sa.map((m) => m * a.atr), dm = (d[0] + d[1]) / 2;
  const L = { entry: a.price, px: a.price, r: R, distPct: dm / a.price * 100, tpPct: dm * R / a.price * 100 };
  if (s.bias !== "long" && s.bias !== "short") return L;
  const sg = s.bias === "short" ? 1 : -1;
  return { ...L, stop: d.map((x) => a.price + sg * x), stopMid: a.price + sg * dm, tp: d.map((x) => a.price - sg * x * R), tpMid: a.price - sg * dm * R };
}

// 价格刻度线：支撑 / 压力刻度、现价、止损区间、止盈区间（窄—宽），持仓多一条开仓价；标签排版见 layoutLadders
function ladder(s, L) {
  const sym = s.symbol, px = L.px || L.entry;
  const lv = (s.key_levels || []).map((l) => l.price).filter((v) => v > 0);
  const pts = [px, L.entry, ...L.stop, ...L.tp, ...lv];
  let lo = Math.min(...pts), hi = Math.max(...pts);
  const pad = (hi - lo) * 0.07 || px * 0.001; lo -= pad; hi += pad;
  const x = (v) => ((v - lo) / (hi - lo)) * 100;
  const zone = (r, k) => (r[0] === r[1]
    ? `<div class="ln ${k}" style="left:${x(r[0])}%"></div>`
    : `<div class="zone ${k}" style="left:${x(Math.min(...r))}%;width:${Math.abs(x(r[1]) - x(r[0]))}%"></div>`);
  let h = '<div class="base"></div>';
  (s.key_levels || []).forEach((l) => {
    const sup = l.price < px;
    h += `<div class="tick ${sup ? "s" : "r"}" style="left:${x(l.price)}%" title="${sup ? "支撑" : "压力"}（军师参考）${esc(price(l.price, sym))}"></div>`;
  });
  h += zone(L.stop, "sl") + zone(L.tp, "tp");
  const top = [[x(L.stopMid), `止损 ${L.held ? "" : "≈"}${price(L.stopMid, sym)}`, "sl"], [x(L.tpMid), `止盈 ${L.held ? "" : "≈"}${price(L.tpMid, sym)}`, "tp"]];
  const bot = [[x(px), `现价 ${price(px, sym)}`, "pxl"]];
  if (L.held) { h += `<div class="open" style="left:${x(L.entry)}%"></div>`; bot.push([x(L.entry), `开仓 ${price(L.entry, sym)}`, "en"]); }
  h += `<div class="px" style="left:${x(px)}%"></div>`;
  top.forEach(([pos, t, k]) => (h += `<div class="lb ${k}" data-pos="${pos}" data-row="t">${t}</div>`));
  bot.forEach(([pos, t, k]) => (h += `<div class="lb ${k}" data-pos="${pos}" data-row="b">${t}</div>`));
  return `<div class="lad" role="img" aria-label="价格刻度：现价 ${price(px, sym)}，止损约 ${price(L.stopMid, sym)}，止盈约 ${price(L.tpMid, sym)}">${h}</div>
    <div class="bc-legend"><span><i style="background:var(--hi)"></i>现价</span>${L.held ? '<span><i style="background:var(--accent)"></i>开仓价</span>' : ""}<span><i style="background:var(--sl)"></i>止损</span><span><i style="background:var(--up)"></i>止盈</span>${lv.length ? '<span><i style="background:var(--up);opacity:.55"></i>支撑</span><span><i style="background:var(--down);opacity:.55"></i>压力</span>' : ""}${L.r ? `<span class="num">盈亏比 ${L.r.toFixed(1)}</span>` : ""}</div>`;
}

// 刻度线标签按实际像素摆：同一排挤了就换到外面一层，靠边的往里收
function layoutLadders(root = document) {
  root.querySelectorAll(".lad").forEach((lad) => {
    const W = lad.clientWidth;
    if (!W) return;
    ["t", "b"].forEach((row) => {
      const right = [-1e9, -1e9];
      [...lad.querySelectorAll(`.lb[data-row="${row}"]`)].sort((a, b) => a.dataset.pos - b.dataset.pos).forEach((el) => {
        const w = el.offsetWidth, xx = Math.min(Math.max(el.dataset.pos * W / 100, w / 2), W - w / 2);
        const lvl = xx - w / 2 >= right[0] + 6 ? 0 : xx - w / 2 >= right[1] + 6 ? 1 : 0;
        right[lvl] = xx + w / 2;
        el.style.left = xx + "px";
        el.classList.remove("t0", "t1", "b0", "b1");
        el.classList.add(row + lvl);
      });
    });
  });
}

function cardWhen(s, now) {
  if (!s.expires_at) return '<span class="dim">—</span>';
  const left = Math.round((new Date(s.expires_at) - now) / 60000);
  if (left <= 0) return s.held ? '<span class="warn">已到期·持仓中</span>' : '<span class="dim">已到期</span>';
  return `<span class="${s.fading ? "warn" : ""}">${hm(s.expires_at)}</span> <span class="dim">剩 ${left} 分</span>`;
}

function cardRow(s, now) {
  const sym = s.symbol, L = cardLevels(s), p = L && L.held;
  const [dt, dk] = s.pending_label ? [s.pending_label, s.pending_label === "开会中" ? "p-accent" : ""] : DIR[s.bias] || BIAS[s.bias] || [s.bias, ""];
  const pnl = p && p.unrealized_usdt != null ? `<span class="pill p-accent">持仓中 <span class="num ${p.unrealized_usdt >= 0 ? "up" : "down"}">${p.unrealized_usdt >= 0 ? "+" : ""}${money(p.unrealized_usdt, 2)} U</span></span>` : p ? '<span class="pill p-accent">持仓中</span>' : "";
  const fade = s.fading && !p ? '<span class="pill p-warn">快到期</span>' : "";
  let stop = "—", tp = "—";
  if (p) {
    const sp = p.stop_loss_pct != null ? guardPct(p.stop_loss_pct, p.side, true) : signedPct(p.stop_loss_price, p.entry_price);
    stop = p.stop_loss_price ? `${price(p.stop_loss_price, sym)} <span class="dim">${sp}</span>` : "—";
    tp = p.take_profit_price ? `${price(p.take_profit_price, sym)} <span class="dim">${signedPct(p.take_profit_price, p.entry_price)}</span>` : "—";
  } else if (L && L.stop) {
    stop = `≈${signedPct(L.stopMid, L.entry)}`;
    tp = `≈${signedPct(L.tpMid, L.entry)}`;
  } else if (L) {
    stop = `±${L.distPct.toFixed(2)}%`;
    tp = `±${L.tpPct.toFixed(2)}%`;
  } else if (s.stop_atr) {
    stop = `${range(s.stop_atr, "", 1)} 倍波动`;
    tp = s.take_profit_r ? `${s.take_profit_r[0]} 倍止损` : "—";
  }
  const used = data && data.card && data.card.leverage_used;
  const lev = p ? `${p.leverage} 倍` : used ? `${used} 倍` : s.leverage ? `${range(s.leverage, "", 0)} 倍` : "—";
  const size = s.size_tier_max ? `${esc(TIER[s.size_tier_max] || s.size_tier_max)}档` : "—";
  const sum = `<summary class="bc-row">
    <span class="bc-sym"><b>${coin(sym)}</b> <span class="dim small">${esc(CAT[s.category] || s.category)}</span></span>
    <span class="bc-dir"><span class="pill ${dk}">${esc(dt)}</span>${pnl}${fade}</span>
    <span class="bc-c sl" data-k="止损">${stop}</span><span class="bc-c tp" data-k="止盈">${tp}</span>
    <span class="bc-c" data-k="杠杆">${lev}</span><span class="bc-c" data-k="仓位">${size}</span>
    <span class="bc-c bc-when" data-k="到期">${cardWhen(s, now)}</span><span class="bc-chev" aria-hidden="true">›</span></summary>`;
  return `<details class="bc ${s.fading && !p ? "fade" : ""}" data-sym="${esc(sym)}">${sum}<div class="bc-body">${cardBody(s, L)}</div></details>`;
}

// 只记录的判断（CardOutlook）：大盘 / 相对大盘分开记，给复盘打分，不影响卡；旧卡没有就不显示
const OUT_M = { up: "涨", down: "跌", flat: "平" }, OUT_R = { outperform: "跑赢", underperform: "跑输", inline: "同步" };
function outlookLine(s) {
  if (!s.market_dir && !s.rel_dir) return "";
  const m = s.market_dir ? OUT_M[s.market_dir] : "没写", r = s.rel_dir ? OUT_R[s.rel_dir] : "没写";
  const b = s.board_market_dir ? `（总盘会：${OUT_M[s.board_market_dir]}）` : "";
  return `<div class="dim small" title="只记录，不影响卡">会议看未来 4 小时：大盘${m}${b}，这个币相对大盘${r}</div>`;
}

function cardBody(s, L) {
  const sym = s.symbol, p = L && L.held;
  if (s.pending_label) return `<div class="txt">${esc(s.pending || s.pending_label)}</div><div class="small"><a ${clickable("meeting", sym)}>开会过程 ›</a></div>`;
  const [dt] = DIR[s.bias] || [""];
  const sigs = s.allowed_signals.length && s.allowed_signals.length < Object.keys(SIGNAL).length ? s.allowed_signals.map((x) => `「${esc(SIGNAL[x] || x)}」`).join("或") : "任一信号";
  const plan = p
    ? `已在 <span class="num">${price(p.entry_price, sym)}</span> ${(DIR[p.side] || [p.side])[0]}，拿着等止盈${p.stop_loss_price ? `；${p.side === "long" ? "跌破" : "涨过"} <span class="num">${price(p.stop_loss_price, sym)}</span> 止损` : ""}。`
    : `等哨兵发现${sigs}，按当时价格${dt}。`;
  const from = (s.source_note ? `<div class="dim small">${esc(s.source_note)}</div>` : "") + (s.source_regime ? `<div class="dim small" title="${esc(s.source_market_view_id || "旧卡来源")}">研究时大盘：${esc(s.source_regime)}</div>` : "");
  let calc = "";
  if (!p && L && L.stop) {
    const lo = Math.min(...L.stop), hi = Math.max(...L.stop), tlo = Math.min(...L.tp), thi = Math.max(...L.tp);
    calc = `<div class="small dim">按现价 ${price(L.px, sym)} 估：止损 ${price(lo, sym)}–${price(hi, sym)}（拍板人选窄 / 中 / 宽），止盈 ${price(tlo, sym)}–${price(thi, sym)}（${L.r} 倍止损）</div>`;
  } else if (!p && L) {
    calc = `<div class="small dim">按现价 ${price(L.px, sym)} 估：止损距离约 ${L.distPct.toFixed(2)}%，止盈约 ${L.tpPct.toFixed(2)}%（${L.r} 倍止损），方向看信号</div>`;
  } else if (!p && s.stop_atr) {
    calc = `<div class="small dim">${S.atr[sym] === null ? "拿不到这个币的 K 线，" : "正在取波动（ATR）…"}只能按倍数显示</div>`;
  }
  const lad = L && L.stop && L.tp ? ladder(s, L) : "";
  const raw = `<details class="raw"><summary>军师原话和参考价位 ›</summary><div class="rawbox">
      <div><span class="tag-ref">参考</span><span class="dim small">原话里的价是军师说的，系统不会等这个价，只按信号和上面的止损 / 止盈下单。</span></div>
      <div class="txt">${esc(s.reason || "（这场会没写理由）")}</div>
      ${levelsLine(s)}
      <dl class="bc-kv small"><dt>军师建议止损</dt><dd>${range(s.stop_atr, "", 1)} 倍 ATR（拍板人选窄 / 中 / 宽）</dd><dt>军师建议止盈</dt><dd>${range(s.take_profit_r, "", 1)} 倍止损距离（风控员用下限）</dd><dt>军师建议杠杆</dt><dd>${range(s.leverage, "", 0)} 倍（只是建议，实际用设置里的杠杆）</dd><dt>军师建议仓位</dt><dd>${range(s.notional_usdt, "", 0)} U（只是建议，实际由风控员按风险算）</dd></dl>
    </div></details>`;
  return `<div class="plan">${plan}</div>${from}${outlookLine(s)}${calc}${lad}<div class="bc-foot">${raw}<a class="small" ${clickable("meeting", sym)}>开会过程 ›</a></div>`;
}

// 军师给的关键位：按在现价下方 / 上方分支撑 / 压力，各自由近到远
function levelsLine(s) {
  const lv = s.key_levels || [];
  if (!lv.length) return "";
  const px = (S.atr[s.symbol] || {}).price;
  const below = (l) => (px ? l.price < px : l.kind === "support");
  const near = (a, b) => (px ? Math.abs(a.price - px) - Math.abs(b.price - px) : 0);
  const g = (name, list) => (list.length ? `<div class="small"><span class="dim">${name}</span> ${list.sort(near).map((l) => price(l.price, s.symbol)).join(" · ")}</div>` : "");
  return g("支撑", lv.filter(below)) + g("压力", lv.filter((l) => !below(l)));
}

// 打开时给每个有方向的币取一次 ATR 和现价，拿到后重画（保留已展开的行）
async function loadCardAtr(c) {
  const syms = c.symbols.filter((s) => s.bias !== "no_trade" && s.stop_atr && !(s.symbol in S.atr) && !cardPos(s.symbol)).map((s) => s.symbol);
  if (!syms.length) return;
  await Promise.all(syms.map(async (sym) => {
    try { S.atr[sym] = await getJson(`/api/card-atr?symbol=${encodeURIComponent(sym)}`); } catch (e) { S.atr[sym] = null; }
  }));
  if ($("#layer .dlg.bc-dlg")) showCard(false);
}

function showCard(fetch = true) {
  const c = data && data.card;
  if (!c) return;
  if (fetch) { S.atr = {}; loadCardAtr(c); }
  const open = new Set([...document.querySelectorAll("#layer details.bc[open]")].map((d) => d.dataset.sym));
  const now = data.now ? new Date(data.now) : new Date();
  const [act, idle] = splitIdle(c.symbols);
  const exp = (s) => (s.expires_at ? new Date(s.expires_at).getTime() : Infinity);
  const held = act.filter((s) => cardPos(s.symbol)), wait = act.filter((s) => !cardPos(s.symbol)).sort((a, b) => exp(a) - exp(b));
  const cut = (t) => { const i = t.indexOf("，今天"); const head = i > 0 ? t.slice(0, i) : t; return head.length > 40 ? head.slice(0, 40) + "…" : head; };
  const regime = c.regime ? `<details class="bc-regime"><summary><b>军师纪要</b> <span>${esc(cut(c.regime))}</span>${cut(c.regime) !== c.regime ? '<span class="more">展开</span>' : ""}</summary><div class="dim small">${esc(c.regime)}</div></details>` : "";
  const sum = `<div class="bc-sum"><span><b>${wait.length}</b> 个币等信号</span><span><b>${held.length}</b> 个持仓中</span><span><b>${idle.length}</b> 个观望</span><span class="dim">点一行展开</span></div>`;
  const head = `<div class="bc-row bc-head" aria-hidden="true"><span>币种</span><span>方向</span><span>止损</span><span>止盈</span><span>杠杆</span><span title="${esc(tierTip(c))}">仓位</span><span>到期</span><span></span></div>`;
  const list = [...held, ...wait];
  const idleHtml = idle.length ? `<details class="bc-idle"><summary class="dim">${idleLine(idle)}（这一轮不开仓）· 点开看理由</summary><ul>${idle.map((s) => `<li><b>${coin(s.symbol)}</b>${esc(s.reason || s.pending || "")} <a class="small" ${clickable("meeting", s.symbol)}>开会过程 ›</a></li>`).join("")}</ul></details>` : "";
  dlg(`<div class="bc-top"><h3>作战卡</h3><span class="dim small">${hm(c.created_at)} 更新 · 每个币按自己那场会到期${c.valid ? "" : " · <span class='down'>已过期</span>"}</span><a class="small" ${clickable("cardhist")}>看历史卡 ›</a></div>
    ${regime}${sum}
    ${list.length ? `<div class="bc-list">${head}${list.map((s) => cardRow(s, now)).join("")}</div>` : '<div class="dim">这一轮没有要做的币，全部观望。</div>'}
    ${idleHtml}`);
  const d = $("#layer .dlg");
  d.classList.add("wide", "bc-dlg");
  d.querySelectorAll("details.bc").forEach((el) => {
    if (open.has(el.dataset.sym)) el.open = true;
    el.addEventListener("toggle", () => el.open && layoutLadders(el));
  });
  layoutLadders(d);
}

// 方向记分（data/card_score/summary.json）：起点 = 卡真正发出的时刻，主指标 = 4h 超额命中（扣掉同期币池等权），
// 原始命中只和同一批样本的「一律做空 / 一律做多」放一起看；区间按 4 小时时段聚类
const scPct = (v) => (v == null ? "-" : `${(v * 100).toFixed(1)}%`);
const scSg = (v) => `${v >= 0 ? "+" : ""}${v.toFixed(1)}`;
const scBp = (v) => (v == null ? "-" : `${scSg(v)}bp`);
const scCiP = (c) => (c ? ` <span class="dim">[${(c[0] * 100).toFixed(1)}–${(c[1] * 100).toFixed(1)}]</span>` : "");
const scCiB = (c) => (c ? ` <span class="dim">[${scSg(c[0])}, ${scSg(c[1])}]</span>` : "");
const SC_H = { "1h": "1 小时", "4h": "4 小时", exp: "到过期" };
function cardScoreBlock(r) {
  if (!r) return `<div class="note">方向记分：还没算过（面板启动几分钟后自动算，之后每小时补一次）。</div>`;
  const h = (r.horizons || {})["4h"];
  if (!h || !h.excess.n) return `<div class="note">方向记分：还没有 4 小时窗口走完的方向卡。</div>`;
  const base = Object.fromEntries(((r.baselines || {})["4h"] || []).map((b) => [b.key, b]));
  const x = h.excess, raw = h.raw;
  const sh = base.always_short ? base.always_short.raw : {}, lg = base.always_long ? base.always_long.raw : {};
  const few = x.blocks < r.min_blocks ? `<div class="warn small">只有 ${x.blocks} 个 4 小时时段；超额命中要分辨到 ±2.5 个百分点约需 ${r.min_blocks} 个，现在别当结论。</div>` : "";
  const stat = (s) => `${scPct(s.hit)}${scCiP(s.hit_ci)}</td><td class="num">${scBp(s.mean_bp)}${scCiB(s.mean_bp_ci)}`;
  const days = r.since ? Math.round((new Date(r.generated_at) - new Date(r.since)) / 86400000) : null;
  const noPool = r.n_no_pool ? ` ${r.n_no_pool} 张方向卡出在有币池记录之前，只算原始、不进超额。` : "";
  const hz = Object.entries(r.horizons).map(([k, v]) => `<tr><td>${SC_H[k] || esc(k)}</td><td class="num">${stat(v.excess)}</td><td class="num">${stat(v.raw)}</td><td class="num">${scBp(v.market.mean_bp)}</td><td class="num">${v.excess.n} / ${v.excess.blocks}</td></tr>`).join("");
  const bl = ((r.baselines || {})["4h"] || []).map((b) => `<tr><td>${esc(b.label)}</td><td class="num">${stat(b.raw)}</td><td class="num">${scPct(b.excess.hit)}</td><td class="num">${b.raw.n}</td></tr>`).join("");
  const grp = (title, gs) => (gs || []).length ? `<div class="st-sub">${title}</div><div class="tw"><table><thead><tr><th></th><th>卡数（观望）</th><th>超额命中</th><th>平均超额</th><th>原始命中</th><th>一律做空 / 做多</th><th>时段</th></tr></thead><tbody>${gs.map((g) => `<tr><td>${esc(g.key)}</td><td class="num">${g.n_cards}（${g.n_neutral}）</td><td class="num">${scPct(g.excess.hit)}${scCiP(g.excess.hit_ci)}</td><td class="num">${scBp(g.excess.mean_bp)}</td><td class="num">${scPct(g.raw.hit)}</td><td class="num">${scPct(g.always_short.hit)} / ${scPct(g.always_long.hit)}</td><td class="num">${g.excess.blocks}</td></tr>`).join("")}</tbody></table></div>` : "";
  return `<div class="note">
    <div><b>方向记分</b>（4 小时，扣掉同期币池等权${days ? `，最近 ${days} 天出的卡` : ""}）：超额命中 <b>${scPct(x.hit)}</b>${scCiP(x.hit_ci)} · 平均超额 <b>${scBp(x.mean_bp)}</b>${scCiB(x.mean_bp_ci)} · ${x.n} 张方向卡 / ${x.blocks} 个时段</div>
    <div>原始命中（没扣大盘）${scPct(raw.hit)}${scCiP(raw.hit_ci)}，同一批卡：一律做空 ${scPct(sh.hit)}、一律做多 ${scPct(lg.hit)}。观望 ${r.n_neutral} 张只计数。${noPool}</div>
    ${few}
    <details><summary class="dim">各期限、基线、分期 / 方向 / 出卡原因</summary>
      <div class="tw mt"><table><thead><tr><th>期限</th><th>超额命中</th><th>平均超额</th><th>原始命中</th><th>平均原始</th><th>大盘分量</th><th>张 / 时段</th></tr></thead><tbody>${hz}</tbody></table></div>
      <div class="st-sub">同一批样本的基线（4 小时）</div>
      <div class="tw"><table><thead><tr><th></th><th>原始命中</th><th>平均原始</th><th>超额命中</th><th>张</th></tr></thead><tbody>${bl}</tbody></table></div>
      ${grp("分期（按合并时间）", r.periods)}${grp("方向", r.by_direction)}${grp("出卡原因", r.by_refresh_reason)}
      <div class="dim small mt">口径：起点 = 这一版卡第一次发布的时刻；价格 = 主网最后一根已收盘 1 分钟线；超额 = 方向 ×（币 − 卡发出时那一份币池的等权，和复盘用的 59 币池不同，数字不会完全一样）；区间 = 按 4 小时 UTC 时段聚类 bootstrap 的 95%。
      老口径（未扣大盘，整轮起点 → 到期的原始涨跌）：命中 ${scPct(r.legacy.hit)}（${r.legacy.n} 张），只作对照。${md(r.generated_at)} ${hm(r.generated_at)} 算的。</div>
    </details>
  </div>`;
}

// 历史卡：过去每条指令（一个币一场会的结论）后来怎么样了；按日期翻、按币筛
const CARD_END = { active: ["还在用", "p-accent"], expired: ["到期", ""], replaced: ["被新结论替换", "p-warn"] };
async function showCardHistory(arg = "") {
  const [day, sym] = arg.split("|");
  const q = new URLSearchParams();
  if (day) q.set("day", day);
  if (sym) q.set("symbol", sym);
  dlg(`<h3>历史卡</h3><div class="dim">读取中…</div>`);
  $("#layer .dlg").classList.add("wide");
  let h, sc;
  try {
    [h, sc] = await Promise.all([getJson(`/api/card-history?${q}`), getJson("/api/card-score").catch(() => null)]);
  } catch (e) {
    dlg(`<h3>历史卡</h3><div class="down">读取失败：${esc(e.message)}</div>`);
    return;
  }
  const days = h.days.length ? h.days : [h.day];
  const pick = `<select id="ch-day">${days.map((d) => `<option ${d === h.day ? "selected" : ""}>${esc(d)}</option>`).join("")}</select>
    <select id="ch-sym"><option value="">全部币</option>${h.symbols.map((x) => `<option value="${esc(x)}" ${x === sym ? "selected" : ""}>${coin(x)}</option>`).join("")}</select>`;
  const rows = h.rows.map((r) => {
    const [bt, bk] = BIAS[r.bias] || [r.bias, ""];
    const [et, ek] = CARD_END[r.end] || [r.end, ""];
    const trade = r.trades ? `${r.trades} 笔${r.pnl_usdt != null ? ` · <span class="${r.pnl_usdt >= 0 ? "up" : "down"}">${r.pnl_usdt >= 0 ? "+" : ""}${money(r.pnl_usdt, 2)} U</span>` : " · 还没平"}` : '<span class="dim">没开仓</span>';
    return `<tr><td class="num">${hm(r.issued_at)} → ${hm(r.ended_at)}</td><td class="hi">${coin(r.symbol)}</td><td><span class="pill ${bk}">${bt}</span></td>
      <td>${range(r.stop_atr, "", 1)}</td><td>${range(r.take_profit_r, "", 1)}</td><td>${range(r.leverage, "", 0)}</td><td>${range(r.notional_usdt, "", 0)}</td>
      <td><span class="pill ${ek}">${et}</span></td><td>${trade}</td><td class="txt dim small">${esc(r.reason || "（没找到这场会的结论记录）")}</td></tr>`;
  }).join("");
  dlg(`<h3>历史卡</h3><p class="dim">每行是一个币一场会的结论：出会时间 → 失效时间（到期或被同一个币的新结论替换）。<a ${clickable("card")}>‹ 回到现在的作战卡</a></p>
    ${cardScoreBlock(sc)}
    <div class="row-pick mt">${pick}</div>
    ${rows ? `<div class="tw mt"><table><thead><tr><th>时间</th><th>币种</th><th>方向</th><th>止损 ATR</th><th>止盈 R</th><th>杠杆</th><th>单笔仓位 U</th><th>结局</th><th>开过仓</th><th>理由</th></tr></thead><tbody>${rows}</tbody></table></div>` : `<div class="dim mt">${esc(h.day)} 没有出过指令。</div>`}`);
  $("#layer .dlg").classList.add("wide");
  const go = () => showCardHistory(`${$("#ch-day").value}|${$("#ch-sym").value}`);
  $("#ch-day").addEventListener("change", () => { $("#ch-sym").value = ""; go(); });
  $("#ch-sym").addEventListener("change", go);
}

async function showSignal(id) {
  if (!id) return;
  dlg(`<h3>信号详情</h3><div class="dim">读取中…</div>`);
  try {
    const r = await fetch(`/api/signal/${encodeURIComponent(id)}`, { cache: "no-store" });
    const chain = await r.json();
    window.__recs = chain;
    const steps = chain.map((s, i) => `<div class="step"><div class="t"><span class="n">${hms(s.ts)}</span>${esc(ROLE[s.role] || s.role)} ${pill(s.outcome)}${s.latency_ms ? `<span class="dim small">${Math.round(s.latency_ms)} ms</span>` : ""}</div>
      <div>${esc(s.text)}</div>${keyFacts(s)}<details><summary>看到的和判断</summary>${recordBody(s)}</details></div>`).join("");
    dlg(`<h3>信号详情</h3><p class="dim small">${esc(id)}</p><div class="steps">${steps || '<div class="dim">今天的日志里找不到这个信号。</div>'}</div>`);
  } catch (e) {
    dlg(`<h3>信号详情</h3><div class="down">读取失败</div>`);
  }
}

// 拍板人的概率、风控员的理由：挑出来直接显示，其余放“完整快照”
function keyFacts(s) {
  const o = s.output || {};
  const facts = [];
  for (const [k, v] of Object.entries(o)) {
    if (typeof v === "number" && /^p_|prob/.test(k)) facts.push(`${esc(k)} ${v.toFixed(2)}`);
  }
  if (Array.isArray(o.reasons)) facts.push(...o.reasons.map(esc));
  return facts.length ? `<div class="facts">${facts.map((f) => `<span class="pill">${f}</span>`).join(" ")}</div>` : "";
}

// 预测员一条记录 → 大白话：未来多久、价格范围、平均路径终点在现价哪一侧；原始 JSON 折叠
// p_up is only the side of the averaged path's endpoint (0 / 0.5 / 1), not a share of paths
const pxFmt = (v, sym) => price(v, sym);
const spanOf = (min) => (min % 60 ? `${min} 分钟` : `${min / 60} 小时`);
function predictorView(s) {
  const o = s.output || {};
  const inp = s.input || {};
  const n = Number.isInteger(inp.paths) ? inp.paths : null;
  const lean = o.p_up > 0.5 ? "偏多" : o.p_up < 0.5 ? "偏空" : "分歧";
  const votes = o.p_up === 0 ? "平均路径终点在现价下方" : o.p_up === 1 ? "平均路径终点在现价上方" : o.p_up === 0.5 ? "平均路径终点和现价持平" : `${Math.round(o.p_up * 100)}% 的模拟向上`;
  const title = `${coin(s.symbol || o.symbol)} 未来 ${spanOf(o.horizon_min)}：预计 ${pxFmt(o.price_low, s.symbol || o.symbol)}–${pxFmt(o.price_high, s.symbol || o.symbol)}，${votes}，${lean}`;
  const bar = o.ts ? `${hm(o.ts)} 那根 K 线收盘` : "最新一根 K 线";
  const seen = [
    inp.last_close != null ? `现价 ${pxFmt(inp.last_close, s.symbol || o.symbol)}（${bar}）` : `按 ${bar}算`,
    inp.bars ? `用了 ${inp.bars} 根${inp.interval ? ` ${esc(inp.interval)}` : ""} K 线` : "",
    n ? `跑了 ${n} 条模拟` : "",
  ].filter(Boolean).join("，");
  return `<p><b>${esc(title)}</b></p>
    <div class="small dim">看到的</div><p>${esc(seen)}${inp.last_close == null || !inp.bars ? '<span class="dim small">（现价、用了几根 K 线这条记录里没写）</span>' : ""}</p>
    <div class="small dim">判断</div><p>${o.stale_after ? `这条预测 ${hm(o.stale_after)} 作废` : ""}${o.model ? ` · 模型 ${esc(o.model)}` : ""}</p>
    <p class="dim small">方向只作参考：Kronos 主要估价格范围，方向接近抛硬币，不能单独当开仓理由。</p>
    <details><summary class="dim small">原始数据</summary><div class="small dim">看到的</div>${json(s.input)}<div class="small dim">判断</div>${json(s.output)}</details>`;
}

// 记录详情：「看到的 / 判断」统一渲染成中文键值行；没有中文名的键才显示英文；原始 JSON 折叠
// [中文名, 单位或格式]：bps = 万分之几，pct = %，usdt = U，k = 千 U，ms，x = 倍，p = 概率转 %
const FIELD = {
  sym: ["币"], symbol: ["币"], side: ["方向"], side_hint: ["方向"], sig: ["信号"], kind: ["类型"], why: ["触发原因"], trigger_desc: ["触发原因"],
  trigger_price: ["触发价", "px"], price: ["价格", "px"], last_price: ["现价", "px"], px: ["现价", "px"], entry_price: ["开仓价", "px"], limit_price: ["限价", "px"],
  hold_min: ["最长持仓", "min"], time_limit_s: ["最长持仓", "s"], atr_bps: ["波动幅度 ATR", "bps"], atr: ["波动幅度 ATR", "px"], atr_period_min: ["ATR 用几分钟 K 线", "min"],
  ret1m_bps: ["1 分钟涨跌", "bps"], ret5m_bps: ["5 分钟涨跌", "bps"], ret_1m: ["1 分钟涨跌", "frac"], ret_5m: ["5 分钟涨跌", "frac"],
  vol_x: ["1 分钟量 / 平时", "x"], vol_ratio_1m: ["1 分钟量 / 平时", "x"], buy: ["主动买占比", "ratio"], taker_buy_ratio: ["主动买占比", "ratio"], taker_buy_ratio_1m: ["主动买占比", "ratio"],
  cvd30_k: ["30 秒净主动买", "k"], cvd: ["净主动买"], rv5m_bps: ["5 分钟实际波动", "bps"], realized_vol_5m: ["5 分钟实际波动", "frac"],
  liq_long_k: ["1 分钟多头强平", "k"], liq_short_k: ["1 分钟空头强平", "k"], fund_bps: ["资金费率", "bps"], funding_rate: ["资金费率", "frac"],
  book: ["盘口"], spread_bps: ["买卖价差", "bps"], imb: ["买卖挂单失衡", "ratio"], imbalance: ["买卖挂单失衡", "ratio"], micro_bps: ["微观价偏离", "bps"], microprice: ["微观价", "px"], mid: ["中间价", "px"],
  ofi_k: ["订单流失衡", "k"], ofi: ["订单流失衡"], depth_k: ["盘口深度", "k"], depth_usdt: ["盘口深度", "usdt"],
  card: ["作战卡"], status: ["状态"], bias: ["方向"], levels_bps: ["关键价位离现价", "bpslist"], stop_atr: ["止损（ATR 倍数）", "range"], tp_r: ["止盈（止损的倍数）", "range"], size_max: ["最大仓位档"], regime: ["大盘判断"],
  pos: ["持仓"], pnl_bps: ["浮盈", "bps"], age_min: ["已持仓", "min"], to_stop_bps: ["离止损", "bps"], to_tp_bps: ["离止盈", "bps"], time_left_min: ["离最长持仓还剩", "min"],
  entry_px: ["进场价", "px"], peak_bps: ["开仓以来最高浮盈", "bps"], giveback_bps: ["从最高回吐", "bps"], pnl_r: ["浮盈（止损的倍数）"], peak_r: ["最高浮盈（止损的倍数）"], be_bps: ["扣来回手续费后浮盈", "bps"], path_bps: ["之前几次复查的浮盈", "bpslist"],
  entry: ["进场理由"], p_real: ["进场时「是真信号」", "p"], p_take: ["进场时「值得做」", "p"], trail: ["移动止损"], on: ["这笔带了移动止损"], active: ["已经启动"], stop_bps: ["现价离移动止损", "bps"], agree: ["平均路径终点是否在持仓方向"],
  kronos: ["预测员"], h_min: ["预测多久", "min"], p_up: ["平均路径终点在现价哪一侧（0 / 0.5 / 1）"], lo_bps: ["预测下沿离现价", "bps"], hi_bps: ["预测上沿离现价", "bps"],
  approved: ["结论"], reasons: ["原因"], detail: ["细节"], day_pnl_pct: ["今天盈亏", "pct"], slippage_bps: ["预计滑点", "bps"], predictor: ["预测员"], predictor_p_up: ["预测员看涨概率", "p"], predictor_nudge: ["预测员把“做”挪了"], take_raw: ["“做”概率（拍板人原话）"], take_adj: ["“做”概率（加上预测员）"],
  quantity: ["数量"], notional_usdt: ["名义仓位（含杠杆）", "usdt"], leverage: ["杠杆", "x"], risk_usdt: ["这笔最多亏", "usdt"], stop_loss_pct: ["止损距离", "pct"], take_profit_pct: ["止盈距离", "pct"],
  trailing_stop_pct: ["移动止损", "pct"], stop_widened: ["止损拉宽"], trailing_activation_pct: ["移动止损启动", "pct"], lifeboat_stop_pct: ["保命止损距离", "pct"], order_type: ["下单方式"], category: ["分组"],
  intent_id: ["下单编号"], executor_id: ["执行器编号"], signal_id: ["信号编号"], card_id: ["作战卡编号"], position_id: ["持仓编号"], dedup_key: ["去重键"], request_id: ["请求编号"],
  action: ["动作"], p_close: ["该平仓的概率", "p"], consecutive: ["连续几次"], reason: ["理由"], fees: ["手续费", "usdt"], fees_usdt: ["手续费", "usdt"], net: ["净盈亏", "usdt"],
  hi_1m: ["1 分钟最高", "px"], lo_1m: ["1 分钟最低", "px"], positions: ["持仓"], symbols: ["币"], checked_positions: ["核对了几个仓位"], issues: ["发现的问题"], fixed: ["已处理"],
  estimated: ["估算"], direct: ["直接"], values: ["行情特征"], features: ["行情特征"], forecast: ["预测员"], ts: ["时间", "ts"], stale_after: ["作废时间", "ts"],
  model: ["模型"], latency_ms: ["耗时", "ms"], cost_usd: ["花费", "usd"], ok: ["调用成功"], error: ["出错"], horizon_min: ["预测多久", "min"], price_low: ["预测下沿", "px"], price_high: ["预测上沿", "px"],
  // 拍板人一道题问法的快照（技术面、成交流、衍生品、24 小时行情、完整作战卡）
  tech: ["技术面"], range_hours: ["区间用了几小时"], pos_in_range_pct: ["在区间里的位置（0 最低 100 最高）"], vwap_dist_bps: ["离今天均价 VWAP", "bps"],
  level_above_atr: ["离上方最近关键位（几个 ATR）"], level_below_atr: ["离下方最近关键位（几个 ATR）"],
  flow: ["成交流"], taker_ratio_1h: ["1 小时主动买卖量比（币安统计）"], taker_ratio_4h: ["4 小时主动买卖量比（币安统计）"], big_15m: ["15 分钟大单（≥1 万 U）"], pct: ["占成交额", "pct"],
  liq_15m_long_k: ["15 分钟多头强平", "k"], liq_15m_short_k: ["15 分钟空头强平", "k"], imb_1m: ["1 分钟平均挂单失衡", "ratio"], imb_5m: ["5 分钟平均挂单失衡", "ratio"],
  deriv: ["衍生品"], premium_bps: ["标记价比指数价贵", "bps"], next_funding_min: ["离下次资金费结算", "min"], funding_prev: ["最近几次资金费率", "bpslist"], top_acct_ls: ["大户人数多空比"], adl: ["自动减仓风险"],
  day: ["24 小时"], chg_pct: ["24 小时涨跌", "pct"], qv_m: ["24 小时成交额（百万 U）"],
  reason: ["理由"], entry_zone_bps: ["进场区间离现价", "bpslist"], in_zone: ["现价在进场区间里"], levels: ["关键价位（离现价 bp，类型）", "lvlist"], signals: ["允许的信号"], left_min: ["离卡到期", "min"],
};
// 技术面、成交流的键带周期（chg_1h_bps、rsi14_5m……），按模式认
const FIELD_DYN = [
  [/^chg_(\w+)_bps$/, (p) => [`${p} 涨跌`, "bps"]], [/^trend_(\w+)$/, (p) => [`${p} 趋势`]], [/^rsi14_(\w+)$/, (p) => [`${p} RSI(14)`]],
  [/^btc_(\w+)_bps$/, (p) => [`BTC ${p} 涨跌`, "bps"]], [/^cvd_(\w+)_k$/, (p) => [`${p} 净主动买`, "k"]], [/^buy_(\w+)$/, (p) => [`${p} 主动买占比`, "ratio"]],
];
function fieldOf(k) {
  if (FIELD[k]) return FIELD[k];
  for (const [re, f] of FIELD_DYN) { const m = re.exec(k); if (m) return f(m[1]); }
  return [k];
}
const VALUE = { ...SIGNAL, up: "涨", down: "跌", flat: "横", support: "支撑", resistance: "阻力", pivot: "枢轴", low: "低", medium: "中", high: "高", long: "多", short: "空", hold: "拿住", close: "平仓", market: "市价", limit: "限价", none: "没有作战卡", symbol_not_in_card: "作战卡里没这个币", both: "双向", no_trade: "观望", ...TIER };
const fmtNum = (v) => (Math.abs(v) >= 1000 ? money(v, 0) : Math.abs(v) >= 1 ? money(v, 2) : String(+v.toFixed(4)));
function fieldVal(k, v) {
  const unit = fieldOf(k)[1];
  if (typeof v === "boolean") return k === "approved" ? (v ? "通过" : "拒绝") : v ? "是" : "否";
  if (typeof v === "string") return unit === "ts" ? hms(v) : esc(VALUE[v] || v);
  if (typeof v !== "number") return esc(String(v));
  switch (unit) {
    case "px": return pxFmt(v);
    case "bps": return `${fmtNum(v)} bp`;
    case "pct": return `${fmtNum(v)}%`;
    case "frac": return `${(v * 100).toFixed(2)}%`;
    case "ratio": case "p": return `${Math.round(v * 100)}%`;
    case "usdt": return `${fmtNum(v)} U`;
    case "usd": return `$${v.toFixed(4)}`;
    case "k": return `${fmtNum(v)} 千 U`;
    case "x": return `${fmtNum(v)} 倍`;
    case "ms": return `${Math.round(v)} ms`;
    case "min": return spanOf(v);
    case "s": return humanDur(v);
    default: return fmtNum(v);
  }
}
function kvRows(o, depth = 0) {
  if (!o || typeof o !== "object") return "";
  return Object.entries(o).filter(([, v]) => v != null && !(Array.isArray(v) && !v.length)).map(([k, v]) => {
    const [name, unit] = fieldOf(k);
    const lab = `<span class="k" title="${esc(k)}">${esc(name)}</span>`;
    if (Array.isArray(v) && unit === "lvlist") {
      const items = v.map((x) => (Array.isArray(x) ? `${fmtNum(x[0])} bp ${esc(VALUE[x[1]] || x[1])}` : fieldVal(k, x))).join("、");
      return `<div class="kv" style="--d:${depth}">${lab}<span>${items}</span></div>`;
    }
    if (Array.isArray(v)) {
      if (v.every((x) => typeof x !== "object")) {
        const items = unit === "range" && v.length === 2 ? `${fmtNum(v[0])}–${fmtNum(v[1])}` : unit === "bpslist" ? v.map((x) => `${fmtNum(x)} bp`).join("、") : v.map((x) => fieldVal(k, x)).join("、");
        return `<div class="kv" style="--d:${depth}">${lab}<span>${items}</span></div>`;
      }
      return `<div class="kv" style="--d:${depth}">${lab}</div>${v.map((x) => kvRows(x, depth + 1)).join("")}`;
    }
    if (typeof v === "object") return `<div class="kv" style="--d:${depth}">${lab}</div>${kvRows(v, depth + 1)}`;
    return `<div class="kv" style="--d:${depth}">${lab}<span>${fieldVal(k, v)}</span></div>`;
  }).join("");
}
const rawData = (s) => `<details class="mt"><summary class="dim small">原始数据</summary><div class="small dim">看到的</div>${json(s.input)}<div class="small dim">判断</div>${json(s.output)}</details>`;
const kvOrEmpty = (o) => kvRows(o) || '<div class="dim small">（这条记录没写）</div>';

// 拍板人：六问各一行；有门槛的标出过没过
const JEV_Q = [
  ["genuine", "真信号", (p) => p.real, "min_p_genuine", ">="],
  ["direction", "方向", null, "min_p_direction", ">="],
  ["toxicity", "订单流有毒", (p) => p.high, "max_p_toxic", "<="],
  ["take", "做这笔", (p) => p.take, "min_p_take", ">="],
  ["stop_width", "止损", null],
  ["size", "仓位", null],
  // 管家（持仓复核）问的三题
  ["thesis_intact", "论点是否成立", (p) => p.yes],
  ["adverse_risk", "逆风风险", null],
  ["action", "平仓概率", (p) => p.close],
];
const JEV_CHOICE = { up: "向上", down: "向下", flat: "横盘", narrow: "偏窄", mid: "适中", wide: "偏宽", S: "小", M: "中", L: "大" };
function jevView(s, th) {
  const byKind = (pageData && pageData.gate_take_by_kind) || {};
  const kind = (s.input || {}).signal_kind;
  // a floor written into the record wins over today's config
  const own = (s.input || {}).take_floor ?? ((s.output || {}).detail || {}).take_floor;
  if (th && own != null) th = { ...th, min_p_take: own };
  else if (th && kind in byKind) th = { ...th, min_p_take: byKind[kind] };
  const inp = (s.input || {}).jev || {};
  const out = (s.output || {}).jev || {};
  let snap = inp.snapshot;
  try { if (typeof snap === "string") snap = JSON.parse(snap); } catch (e) { /* 旧格式是一句话，原样显示 */ }
  const seen = snap && typeof snap === "object" ? kvRows(snap) : snap ? `<div>${esc(snap)}</div>` : inp.called === false ? '<div class="dim small">没有调用模型（例如没有有效作战卡）</div>' : "";
  const ans = out.answers || {};
  const fails = [];
  const rows = JEV_Q.filter(([id]) => ans[id]).map(([id, name, pick, thKey, op]) => {
    const p = ans[id];
    const [top, topP] = Object.entries(p).sort((a, b) => b[1] - a[1])[0];
    const val = pick ? pick(p) : topP;
    const shown = pick ? `${Math.round(val * 100)}%` : `${JEV_CHOICE[top] || top} ${Math.round(topP * 100)}%`;
    let limit = "";
    let bad = false;
    if (thKey && th && th[thKey] != null) {
      limit = `（${op === ">=" ? "门槛" : "上限"} ${Math.round(th[thKey] * 100)}%）`;
      bad = op === ">=" ? val < th[thKey] : val > th[thKey];
      if (bad) fails.push(`${name} ${shown} ${op === ">=" ? "低于门槛" : "高于上限"} ${Math.round(th[thKey] * 100)}%`);
    }
    return `<div class="kv"><span class="k" title="${esc(id)}">${esc(name)}</span><span class="${bad ? "down" : ""}">${esc(shown)}${esc(limit)}</span></div>`;
  }).join("");
  const verdict = !out.ok ? `<div class="note warn">${esc(out.error || "没有拿到回答")}</div>` : fails.length ? `<div class="note warn">按门槛会被拒绝：${esc(fails.join("；"))}</div>` : "";
  return `<div class="small dim">看到的</div>${seen || '<div class="dim small">（这条记录没写）</div>'}
    <div class="small dim mt">判断</div>${verdict}${rows || '<div class="dim small">（没有回答）</div>'}
    <p class="dim small">${[out.model ? `模型 ${esc(out.model)}` : "", out.latency_ms != null ? `${Math.round(out.latency_ms)} ms` : "", out.cost_usd != null ? `$${(+out.cost_usd).toFixed(4)}` : ""].filter(Boolean).join(" · ")}</p>`;
}
// 风控员：通过 / 拒绝 + 原因列表 + 细节
function guardView(s) {
  const o = s.output || {};
  const rest = Object.fromEntries(Object.entries(o).filter(([k]) => !["approved", "reasons"].includes(k)));
  const head = o.approved != null ? `<div class="note ${o.approved ? "" : "warn"}"><b>${o.approved ? "通过" : "拒绝"}</b>${(o.reasons || []).length ? `：${esc(o.reasons.join("；"))}` : ""}</div>` : "";
  return `<div class="small dim">看到的</div>${kvOrEmpty(s.input)}<div class="small dim mt">判断</div>${head}${kvRows(rest)}`;
}
// 风控员拒绝的信号：重新检查（按现价重跑，不下单）/ 人工放行（按现价立刻开）。
// 这个币的卡过期了或已有持仓就灰掉，写明原因；主程序重启过、信号记录没了，主程序会回「记录已过期」
function guardActions(s) {
  const o = s.output || {};
  // 拒绝记录，或「重新检查」的结果记录（现在放行 / 现在仍拒绝）：都带这两个按钮，原来那条拒绝翻到别天了也能点
  const by = o.detail && o.detail.by;
  if ((o.approved !== false && by !== "recheck") || !s.signal_id || !s.symbol || !data || !data.control) return "";
  const row = ((data.card && data.card.symbols) || []).find((r) => r.symbol === s.symbol);
  const exp = row && row.expires_at ? new Date(row.expires_at).getTime() : null;
  const why = !data.running ? "主程序没在运行"
    : !row || (exp != null && exp <= Date.now()) ? "这个币的作战卡已过期（或已不在卡上），要等下一轮会议出新卡"
    : (data.positions || []).some((p) => p.symbol === s.symbol) ? "这个币已经有持仓，不开第二笔"
    : "";
  const btn = (kind, label, cls) => `<button class="btn b-sm ${cls}" ${why ? `disabled title="${esc(why)}"` : clickable("sigcmd", `${kind}:${s.symbol}:${s.signal_id}`)}>${label}</button>`;
  return `<div class="row mt" style="gap:8px">${btn("recheck_signal", "重新检查", "")}${btn("release_signal", "放行", "b-kill")}</div>
    <div class="dim small">${why ? esc(why) : "重新检查：按现价重跑一遍，只看结果、不下单。放行：按现价立刻开，跳过拒绝它的过滤（要面板密码）。"}</div>`;
}
function recordBody(s) {
  const th = (pageData && pageData.gate_thresholds) || null;
  const isJev = (s.input && s.input.jev) || (s.output && s.output.jev);
  const body = isJev ? jevView(s, th) : s.output && "approved" in s.output ? guardView(s) : `<div class="small dim">看到的</div>${kvOrEmpty(s.input)}<div class="small dim mt">判断</div>${kvOrEmpty(s.output)}`;
  return body + guardActions(s) + rawData(s);
}

function showRecord(i) {
  const s = (window.__recs || [])[i];
  if (!s) return;
  const o = s.output || {};
  if (s.role === "predictor" && o.price_low != null && o.price_high != null && o.horizon_min) {
    dlg(`<h3>预测员 · ${hms(s.ts)} ${pill(s.outcome)}</h3>${predictorView(s)}
      <p class="dim small">耗时 ${Math.round(s.latency_ms)} ms · 本地计算，不花钱</p>`);
    return;
  }
  const isJev = (s.input && s.input.jev) || (s.output && s.output.jev);
  dlg(`<h3>${esc(ROLE[s.role] || s.role)} · ${hms(s.ts)} ${pill(s.outcome)}</h3><p>${esc(s.text)}</p>
    ${recordBody(s)}
    ${isJev ? "" : `<p class="dim small">${s.latency_ms ? `耗时 ${Math.round(s.latency_ms)} ms · ` : ""}${s.cost_usd ? `花费 $${s.cost_usd.toFixed(4)}` : "不花钱"}</p>`}`);
}

/* ---------- 开会过程（慢环按阶段写 data/meeting_live/<币>.json；正在开的每 4 秒刷新） */
// 总盘会不走五步流程：看数据 → 挑币 → 排除，发言人另起名字
const MARKET_STAGE = { input: "看到的市场数据", pick: "挑中上会的币", filter: "没挑中的币和原因" };
const MARKET_DOING = { input: "看市场数据", pick: "挑上会的币", filter: "整理名单" };
const MARKET_WHO = { input: "候选池", market: "总盘会", filter: "名单整理" };
const STAGE = { analysts: "分析师报告", debate: "多空辩论", trader: "交易员计划", risk: "风控辩论", decision: "结论" };
const WHO = {
  market: "市场分析师", social: "情绪分析师", news: "新闻分析师", fundamentals: "基本面分析师",
  bull: "多头研究员", bear: "空头研究员", research_manager: "研究主管", trader: "交易员",
  aggressive: "激进派风控", conservative: "保守派风控", neutral: "中立派风控", risk_manager: "风控主管", convert: "出作战卡",
};
const MEET_STATUS = { running: ["正在开", "p-accent"], done: ["开完了", "p-long"], failed: ["没开成", "p-short"], interrupted: ["中断了", "p-warn"], stale: ["中断了", "p-warn"] };
const meetPill = (s) => { const [t, k] = MEET_STATUS[s] || [s, ""]; return `<span class="pill ${k}">${t}</span>`; };
const stageNo = (s) => Object.keys(STAGE).indexOf(s) + 1;
// 开会用时：不到 1 分钟写秒，1 小时内写「X 分 Y 秒」，再长同 dur；花费不到 1 分钱写到 4 位小数
const meetDur = (ms) => { const t = Math.max(0, Math.round(ms / 1000)); return t < 60 ? `${t} 秒` : t < 3600 ? `${Math.floor(t / 60)} 分 ${t % 60} 秒` : dur(ms); };
const meetCost = (v) => `$${v.toFixed(v < 0.01 ? 4 : 2)}`;
const meetTime = (m) => (m.status === "running" && m.started_at ? `已开 ${meetDur(Date.now() - Date.parse(m.started_at))}` : m.started_at && (m.finished_at || m.updated_at) ? `${hm(m.started_at)} 开，用时 ${meetDur(Date.parse(m.finished_at || m.updated_at) - Date.parse(m.started_at))}` : "");
const meetBias = (m) => (m.bias ? (() => { const [t, k] = BIAS[m.bias] || [m.bias, ""]; return `<span class="pill ${k}">${t}</span>`; })() : "");

// 军师团：只列各币的会；总盘会在团队图上单独点开
function meetingsCard(all) {
  const list = all.filter((m) => m.symbol !== MARKET);
  const rows = list.map((m) => `<div class="click" ${clickable("meeting", m.symbol)}><span class="n">${m.updated_at ? hm(m.updated_at) : ""}</span><span><b>${meetName(m.symbol)}</b> ${meetPill(m.status)} ${meetBias(m)}
    <span class="dim small">${m.status === "running" && m.stage ? `${stageNo(m.stage) ? `第 ${stageNo(m.stage)} / 5 步 · ` : ""}${esc((m.symbol === MARKET ? MARKET_STAGE : STAGE)[m.stage] || m.stage)} · ` : ""}${meetTime(m)}</span></span></div>`).join("");
  return `<section class="card mt"><div class="ch"><h2>开会过程</h2><span class="r">每个币最近一场会 · 点开看每个人说了什么</span></div>
    <div class="log">${rows || '<div class="dim">还没有开过会。</div>'}</div></section>`;
}

// 总盘会：先看大盘、挑哪些币上会，文件名 _MARKET，团队页固定在最上面
const MARKET = "_MARKET";
const meetName = (s) => (s === MARKET ? "总盘会" : coin(s));
let meetTimer = null;
S.meetOpen = new Set();
function showMeeting(sym) {
  dlg(`<h3>${meetName(sym)} · 开会过程</h3><div id="meet"><div class="dim">读取中…</div></div>`);
  $("#layer .dlg").classList.add("mid");
  $("#meet").dataset.sym = sym;
  S.meetOpen = new Set();
  loadMeeting(sym);
}
async function loadMeeting(sym) {
  clearTimeout(meetTimer);
  const box = () => { const b = $("#meet"); return b && b.dataset.sym === sym ? b : null; };
  let m;
  try { m = await getJson(`/api/meeting/${encodeURIComponent(sym)}`); } catch (e) {
    if (box()) box().innerHTML = `<div class="dim">${/ 404/.test(e.message) ? (sym === MARKET ? "还没有总盘会记录。慢环下一次开总盘会就会记下来。" : "这个币还没有开会记录。慢环升级以后，每场会的过程都会记下来。") : esc(e.message)}</div>`;
    return;
  }
  if (!box()) return; // 弹窗关了或换了币
  const dl = $("#layer .dlg");
  const follow = dl.scrollTop + dl.clientHeight >= dl.scrollHeight - 40; // 在底部就跟着新发言往下走
  if (!S.meetOpen.size && m.steps.length) S.meetOpen.add(m.steps.length - 1);
  box().innerHTML = meetingHtml(m);
  if (follow && m.status === "running") dl.scrollTop = dl.scrollHeight;
  if (m.status === "running") meetTimer = setTimeout(() => loadMeeting(sym), 4000);
}
function meetingHtml(m) {
  const cur = m.status === "done" ? 6 : stageNo(m.stage);
  const chips = Object.entries(STAGE).map(([k, t], i) => {
    const st = i + 1 < cur ? "done" : i + 1 === cur ? (m.status === "running" ? "now" : "stop") : "";
    return `<span class="stg ${st}">${st === "done" ? "✓ " : ""}${t}</span>`;
  }).join('<span class="dim">›</span>');
  const isMkt = m.symbol === MARKET;
  const stageName = isMkt ? MARKET_STAGE : STAGE;
  const whoName = isMkt ? MARKET_WHO : WHO;
  const known = Object.keys(stageName);
  const stages = [...known, ...new Set(m.steps.map((s) => s.stage).filter((s) => !known.includes(s)))];
  const groups = stages.map((st) => {
    const items = m.steps.map((s, i) => [s, i]).filter(([s]) => s.stage === st);
    if (!items.length) return "";
    return `<div class="lbl">${esc(stageName[st] || st || "其他")}</div>${items.map(([s, i]) => `<details class="say" data-step="${i}" ${S.meetOpen.has(i) ? "open" : ""}>
      <summary><b>${esc(whoName[s.who] || s.who || "—")}</b><span class="dim small">${s.ts ? hms(s.ts) : ""}</span><span class="dim small one">${esc(s.text.slice(0, 80))}</span></summary><div class="txt">${esc(s.text)}</div></details>`).join("")}`;
  }).join("");
  return `<p class="dim">${meetPill(m.status)} ${meetBias(m)} ${meetTime(m)}${m.cost_usd ? ` · 花费 ${meetCost(m.cost_usd)}` : ""}${m.status === "running" ? " · 自动刷新" : ""}</p>
    ${isMkt ? "" : `<div class="stgs">${chips}</div>`}
    ${m.status === "stale" ? '<div class="note warn">标着“正在开”，但 15 分钟没有新进展：多半是主程序重启把会打断了，下一轮会重开。</div>' : ""}
    ${m.status === "interrupted" ? `<div class="note warn">${esc(m.error || "会议中断")}，下一轮会重开。</div>` : m.error ? `<div class="note warn">出错：${esc(m.error)}</div>` : ""}
    ${m.reason ? `<div class="note"><b>结论</b>　${esc(m.reason)}</div>` : ""}
    ${groups || '<div class="dim">还没有人发言。</div>'}`;
}
document.addEventListener("toggle", (e) => {
  const i = e.target.dataset && e.target.dataset.step;
  if (i == null) return;
  if (e.target.open) S.meetOpen.add(+i);
  else S.meetOpen.delete(+i);
}, true);

/* ---------- 总览 */
const P = {};

P.overview = () => {
  const d = data;
  return `<div class="grid g3">${cardSection(d.card)}${feedSection(d.feed, d.tiles.signals)}${posSection(d.positions)}</div>
  ${tilesSection(d.tiles, d.gate_label)}
  <div class="grid g2 mt">${alertsSection(d.alerts)}${blockedSection(d.blocked)}</div>`;
};

function cardSection(c) {
  if (!c) {
    return `<section class="card"><div class="ch"><h2>作战卡</h2><span class="pill">军师团</span></div>
      <div class="empty"><b>今天还没有作战卡</b>没有有效作战卡就不开新仓；已有持仓照常盯。<div class="mt"><button class="btn b-sm" ${clickable("cmd", "refresh_cards")}>重新开会</button></div></div></section>`;
  }
  const [act, idle] = splitIdle(c.symbols);
  const shown = act.slice(0, 8);
  const more = act.length - shown.length;
  const pills = shown.map((s) => { const [t, k] = s.pending_label ? [s.pending_label, ""] : BIAS[s.bias] || [s.bias, ""]; return `<span class="pill ${k}">${coin(s.symbol)} ${t}</span>`; }).join("");
  const until = c.expires_at ? `有效到 ${hm(c.expires_at)}` : "有效期未知";
  return `<section class="card click" ${clickable("card")}>
    <div class="ch"><h2>作战卡</h2><span class="pill">军师团${c.version ? " v" + c.version : ""}</span>
      <span class="r">${hm(c.created_at)} 更新 · ${c.valid ? until : '<span class="down">已过期，不开新仓</span>'} <button class="btn b-sm" ${clickable("cmd", "refresh_cards")}>重新开会</button></span></div>
    <div class="row">${pills || '<span class="dim">这一轮没有要做的币</span>'}${more > 0 ? `<span class="dim small">+${more} 个</span>` : ""}</div>
    ${idle.length ? `<div class="dim small">${idleLine(idle)}（不开仓）</div>` : ""}
    ${cardEffective(c)}
    ${c.current_market ? `<div class="note"><b>最新大盘判断</b>　${esc(c.current_market.regime)}${deskLatestLine(c.desk_latest)}<div class="dim small">${hms(c.current_market.observed_at)} 更新${c.current_market.source_ts ? ` · 行情 ${hms(c.current_market.source_ts)}` : ""} · 大盘 v${c.current_market.version}；各币研究按原有效期执行</div></div>` : ""}
    ${c.regime ? `<div class="note"><b>出卡时大盘</b>　${esc(c.regime)}</div>` : ""}
    <div class="foot">点开看每个币的方向、止损、杠杆和理由 ›</div>
  </section>`;
}

// 作战卡摘要：只写风控员下单真会用的数（杠杆、每笔风险金额）；军师团给的范围只是建议，点开每个币才看得到
function cardEffective(c) {
  const f = c.size_fractions || {};
  const r = c.risk_per_trade_usdt;
  const risk = r == null ? "拿不到总资产，算不出每笔风险" : `每笔最多亏 ${["S", "M", "L"].filter((k) => f[k] != null).map((k) => `${TIER[k]}档 ${money(r * f[k], 0)}`).join(" / ")} U`;
  const lev = c.leverage_used == null ? "杠杆按各币卡上的" : `杠杆 ${c.leverage_used}x`;
  return `<div class="small card-eff"><span class="dim">实际下单：</span>${lev} · ${risk} · 止损按每个币自己的波动算，名义 = 风险 ÷ 止损距离</div>`;
}

function feedSection(feed, signals) {
  const body = feed.length
    ? `<div class="feed scroll">${feed.map((e) => `<div class="ev ${e.kind} ${e.signal_id ? "click" : ""}" ${e.signal_id ? clickable("signal", e.signal_id) : ""}><div class="t"><span class="n">${hm(e.ts)}</span>${esc(ROLE[e.role] || e.role)}${e.symbol ? " · " + coin(e.symbol) : ""}</div><div class="d">${esc(e.text)}</div></div>`).join("")}</div>`
    : `<div class="empty"><b>今天还没有决策</b>哨兵已触发 ${signals} 个信号。送拍板后会在这里出现。</div>`;
  return `<section class="card"><div class="ch"><h2>信号与决策流</h2><span class="r">最新在上 · 框里往下滚看更早的</span></div>${body}
    <div class="foot">绿 = 放行 / 开仓 · 黄 = 拍板放弃 · 红 = 风控员拒绝 · 蓝 = 平仓。点一条看整条链和快照。</div></section>`;
}

// 管家快照里的最高浮盈 / 回吐 / 移动止损（方案 E 之后的记录才有）
function keeperPeak(p) {
  if (p.keeper_peak_usdt == null) return "";
  const r = p.keeper_peak_r == null ? "" : `（${signed(p.keeper_peak_r, 2)}R）`;
  const give = p.keeper_giveback_usdt == null ? "" : ` · 已回吐 ${money(p.keeper_giveback_usdt, 2)}U`;
  return `最高浮盈 ${signed(p.keeper_peak_usdt, 2)}U${r}${give}`;
}

function posSection(list) {
  const body = list.length
    ? list.map((p) => {
        const [s, sc] = SIDE[p.side] || [p.side, ""];
        const lb = LIFEBOAT[p.lifeboat_status] || "⚠ 没有保命单";
        const lbBad = p.lifeboat_status !== "active";
        return `<a class="pos click" href="#positions/${encodeURIComponent(p.position_id)}"><div class="top"><b>${coin(p.symbol)}${catTag(p.category)} <span class="${sc}">${s}</span> ${qty(p.quantity, p.mark_price || p.entry_price)}</b>
          <span class="pnl ${cls(netPnl(p))}" title="${esc(feeTip(p))}">${netPnl(p) == null ? "—" : signed(netPnl(p), 2) + "U"}</span></div>
          <div class="meta"><span>开 ${price(p.entry_price, p.symbol)} · 损 ${price(p.stop_loss_price, p.symbol)} · 盈 ${price(p.take_profit_price, p.symbol)}</span></div>
          <div class="meta"><span class="${lbBad ? "warn" : ""}">${lb}${p.lifeboat_price ? " " + price(p.lifeboat_price, p.symbol) : ""}</span><span>管家：${esc(p.keeper || "还没看")}</span>${keeperPeak(p) ? `<span>${esc(keeperPeak(p))}</span>` : ""}${p.status !== "open" ? `<span class="pill p-warn">${esc(p.status)}</span>` : ""}</div></a>`;
      }).join("")
    : `<div class="empty"><b>没有持仓</b>开仓后这里显示盈亏、保命单和管家判断。</div>`;
  return `<section class="card"><div class="ch"><h2>当前持仓</h2><span class="r">${list.length} 笔 · <a href="#positions">全部 ›</a></span></div><div class="scroll">${body}</div></section>`;
}

function tilesSection(t, gateLabel) {
  const items = [
    [t.signals, "信号"],
    [t.opened, "开仓"],
    [t.win_rate == null ? "—" : Math.round(t.win_rate * 100) + "%", `胜率（平 ${t.closed} 笔）`],
    [money(t.fees_usdt, 2) + "U", "手续费"],
    ["$" + money(t.jev_cost_usd + t.llm_cost_usd, 2), `AI 花费（${esc(gateLabel)} + 军师）`],
    [t.avg_gate_latency_ms == null ? "—" : (t.avg_gate_latency_ms / 1000).toFixed(2) + "s", "拍板平均延迟"],
  ];
  return `<div class="tiles mt">${items.map(([v, l]) => `<div class="tile"><small>${l}</small><b>${v}</b></div>`).join("")}</div>`;
}

function alertsSection(list) {
  const crit = list.some((a) => a.level === "critical");
  const body = list.length
    ? `<div class="log scroll">${list.map((a) => `<div><span class="n">${hm(a.ts)}</span><span><span class="pill ${a.level === "critical" ? "p-short" : "p-warn"}">${esc(ROLE[a.role] || a.role)}</span>　${esc(a.text)}</span></div>`).join("")}</div>`
    : `<div class="dim">今天没有告警。</div>`;
  return `<section class="card ${crit ? "alert" : ""}"><div class="ch"><h2>告警</h2><span class="r">同时推到 Telegram</span></div>${body}</section>`;
}

function blockedSection(map) {
  const entries = Object.entries(map);
  const body = entries.length
    ? `<div class="log">${entries.map(([s, r]) => `<div><span class="pill p-short">${coin(s)}</span><span>${esc(r)}</span><button class="btn b-sm" ${clickable("unblock", s)}>解封</button></div>`).join("")}</div>`
    : `<div class="dim">没有被封的币。币安发下架公告时，相关币会自动停止新开仓并列在这里。</div>`;
  return `<section class="card"><div class="ch"><h2>禁开新仓</h2><span class="r">来自币安公告</span></div>${body}</section>`;
}

/* ---------- 持仓 */
P.positions = (arg) => {
  const d = pageData;
  if (!d.positions.length) {
    return `<div class="card"><div class="empty"><b>现在没有持仓</b>开仓后这里每笔一行，点开能看走势图、止损止盈和每一步判断。<a href="#overview">看信号与决策流 ›</a></div></div>`;
  }
  const sel = d.positions.find((p) => p.position_id === arg) || d.positions[0];
  const groups = d.groups.filter((g) => g.cap_x > 0 || g.exposure_x > 0).map((g) => {
    const r = g.cap_x ? g.exposure_x / g.cap_x : 1;
    return `<div class="bar ${r >= 1 ? "full" : r >= 0.7 ? "hot" : ""}"><div class="lab"><span>${esc(CAT[g.category] || g.category)}</span><span class="num" title="这一类合计 / 上限（本金 ${g.cap_x} 倍，不是杠杆）">${g.cap_usdt != null ? `${money(g.notional_usdt, 0)} / ${money(g.cap_usdt, 0)} U` : `${g.exposure_x.toFixed(2)} / ${g.cap_x} 倍本金`}</span></div><div class="tr"><span style="width:${Math.min(100, r * 100)}%"></span></div></div>`;
  }).join("");
  const now = new Date(data.now).getTime();
  const rows = d.positions.map((p) => {
    const [s, sc] = SIDE[p.side] || [p.side, ""];
    const on = p.position_id === sel.position_id;
    const held = now - new Date(p.opened_at).getTime();
    const go = `onclick="if (!event.target.closest('button')) location.hash='positions/${encodeURIComponent(p.position_id)}'"`;
    const side = `<span class="pill ${p.side === "long" ? "p-long" : "p-short"}">${s}</span>`;
    const split = p.fees_est_usdt != null && p.unrealized_usdt != null ? `<div class="dim small">浮动 ${signed(p.unrealized_usdt, 2)} · 费 −${money(p.fees_est_usdt, 2)}${p.fee_from_fills ? "" : "（估）"}</div>` : "";
    const pnl = netPnl(p) == null ? "—" : `${signed(netPnl(p), 2)} U${netPct(p) == null ? "" : ` <span class="small">${signed(netPct(p), 2)}%</span>`}${split}`;
    const lb = `<span class="${p.lifeboat_status === "active" ? "" : "warn"}">${LIFEBOAT[p.lifeboat_status] || "⚠ 没有保命单"} ${p.lifeboat_price ? price(p.lifeboat_price, p.symbol) + lbPct(p) : ""}</span>${p.lifeboat_price ? `<div class="small" title="${LVL_TIP}">${lvlEst(p, p.lifeboat_price)}</div>` : ""}${protBrief(p)}`;
    const hold = `${dur(held)} / ${dur(p.time_limit_s * 1000)}`;
    const closeBtn = `<button class="btn b-sm b-danger" ${clickable("close1", p.position_id)}>平仓</button>`;
    return `<tr class="click ${on ? "sel" : ""}" ${go}>
      <td class="hi nowrap">${coin(p.symbol)}${catTag(p.category)}</td><td>${side}</td><td class="r num">${qtyFmt(p.quantity)}</td>
      <td class="r num">${p.notional_usdt == null ? "—" : money(p.notional_usdt, 0)}</td><td class="r num">${p.leverage}x</td>
      <td class="r num">${p.margin_usdt == null ? "—" : money(p.margin_usdt, 0)}</td>
      <td class="r num">${price(p.entry_price, p.symbol)}</td><td class="r num">${price(p.mark_price, p.symbol)}${pxSrc(p)}</td>
      <td class="r num ${cls(netPnl(p))}" title="${esc(feeTip(p))}">${pnl}</td>
      <td class="r num">${slCell(p)}${trailTag(p)}</td><td class="r num">${tpCell(p)}</td>
      <td class="r num">${hold}</td><td>${lb}</td><td>${esc(p.keeper || "还没看")}${keeperPeak(p) ? `<div class="small dim">${esc(keeperPeak(p))}</div>` : ""}</td><td class="r">${closeBtn}</td></tr>`;
  }).join("");
  // 手机宽度：每笔两行，第一行 币种 方向 浮盈，第二行其余
  const cards = d.positions.map((p) => {
    const [s] = SIDE[p.side] || [p.side, ""];
    const on = p.position_id === sel.position_id;
    const held = now - new Date(p.opened_at).getTime();
    const lbOk = p.lifeboat_status === "active";
    return `<div class="pm ${on ? "sel" : ""}"><a href="#positions/${encodeURIComponent(p.position_id)}">
      <div class="l1"><b>${coin(p.symbol)}</b>${catTag(p.category)} <span class="pill ${p.side === "long" ? "p-long" : "p-short"}">${s}</span>
        <span class="r num ${cls(netPnl(p))}">${netPnl(p) == null ? "—" : `${signed(netPnl(p), 2)} U`}${netPct(p) == null ? "" : ` · ${signed(netPct(p), 2)}%`} <span class="dim">扣费后</span></span></div>
      <div class="l2 small dim">数量 ${qtyFmt(p.quantity)} · 名义 ${p.notional_usdt == null ? "—" : money(p.notional_usdt, 0)} U · ${p.leverage}x · 占用 ${p.margin_usdt == null ? "—" : money(p.margin_usdt, 0)} U<br>
        开 ${price(p.entry_price, p.symbol)} · 现 ${price(p.mark_price, p.symbol)}${pxSrc(p)} · 损 ${slCell(p).replace(/<div[^>]*>/, " ").replace("</div>", "")} · 盈 ${tpCell(p).replace(/<div[^>]*>/, " ").replace("</div>", "")} · 拿了 ${dur(held)} / ${dur(p.time_limit_s * 1000)}${trailTag(p)}</div></a>
      <div class="pm-act small"><span class="${lbOk ? "dim" : "warn"}">${LIFEBOAT[p.lifeboat_status] || "⚠ 没有保命单"} ${p.lifeboat_price ? price(p.lifeboat_price, p.symbol) + lbPct(p) + " " + lvlEst(p, p.lifeboat_price) : ""}${protBrief(p)}</span><button class="btn b-sm b-danger" ${clickable("close1", p.position_id)}>平仓</button></div></div>`;
  }).join("");
  window.__recs = d.trails[sel.position_id] || [];
  const trail = window.__recs.map((s, i) => `<div class="click" ${clickable("record", i)}><span class="n">${hms(s.ts)}</span><span>${esc(ROLE[s.role] || s.role)} ${pill(s.outcome)} ${esc(s.text)}</span></div>`).join("");
  const [s, sc] = SIDE[sel.side] || [sel.side, ""];
  return `<div class="card strip">
      <span class="hi"><b>持仓 ${d.positions.length} 笔</b></span>
      <span>未实现 <b class="num ${cls(d.unrealized_usdt)}">${d.unrealized_usdt == null ? "—" : signed(d.unrealized_usdt, 2) + "U"}</b></span>
      <span>保命止损 <b class="num ${d.lifeboats_ok === d.positions.length ? "up" : "warn"}">${d.lifeboats_ok} / ${d.positions.length} 在位</b></span>
      <div class="bars">${groups}</div>
    </div>
    <section class="card mt"><div class="tw pos-wide"><table>
      <thead><tr><th>币种</th><th>方向</th><th class="r">数量</th><th class="r">名义（U）</th><th class="r">杠杆</th><th class="r" title="名义 ÷ 杠杆">本金占用（U）</th><th class="r">开仓价</th><th class="r" title="${esc(pxHeadTip(d))}">现价</th><th class="r" title="浮动盈亏 − 开平两次手续费（按吃单费率估）；百分比按名义算，不乘杠杆">浮盈·扣费后（U / %）</th><th class="r">止损价</th><th class="r">止盈价</th><th class="r">拿了多久 / 最长</th><th>保命单</th><th>管家</th><th></th></tr></thead>
      <tbody>${rows}</tbody></table></div><div class="pos-narrow">${cards}</div></section>
    <section class="card mt ${S.chartFull ? "chart-full" : ""}">
      <div class="ch"><h2>${coin(sel.symbol)} <span class="${sc}">${s}</span> ${qty(sel.quantity, sel.mark_price || sel.entry_price)} · 详情</h2>
        <span class="r"><span class="seg">${["1m", "5m", "15m", "1h"].map((t) => `<button class="${S.tf === t ? "on" : ""}" ${clickable("tf", t)}>${t}</button>`).join("")}</span>
          <button class="btn b-sm" ${clickable("kscale")} title="${S.scaleAll ? "价格轴只装 K 线和开仓 / 止损 / 止盈线，保命、启动线超出时贴在边上" : "价格轴把所有价位线都装进来"}">${S.scaleAll ? "只看止损止盈" : "显示全部价位"}</button>
          <button class="btn b-sm chart-fs-btn" ${clickable("chartfull")} title="${S.chartFull ? "退出全屏（Esc）" : "全屏看图"}" aria-label="${S.chartFull ? "退出全屏" : "全屏看图"}">${S.chartFull ? "✕" : "⤢"}</button></span></div>
      <div class="chart" id="kchart-host"></div>
      <div class="legend small dim"><span><i style="border-color:var(--up)"></i>止盈</span><span><i style="border-color:var(--faint);border-style:solid"></i>开仓</span><span><i style="border-color:var(--sl)"></i>固定止损计划价（账本）</span><span><i style="border-color:var(--down);border-style:dotted"></i>保命</span>${trailLegend(sel)}<span class="chart-hint"><span class="h-pc">⌘/Ctrl+滚轮缩放 · 拖动平移 · 双击复位</span><span class="h-touch">双指捏合缩放 · 单指上下滑动页面</span></span></div>
      ${sel.trailing_stop_pct != null ? `<div class="small dim trail-note">${trailNote(sel)}</div>` : ""}
      ${sel.protection ? protBlock(sel) : stopDiagnostics(sel)}
      <div><div class="lbl">从信号到现在的每一步（今天）· 点一条看快照</div><div class="log trail">${trail || '<div class="dim">今天的日志里没有这笔的记录。</div>'}</div></div>
    </section>`;
};

/* K 线图：图表 DOM 常驻，页面重画时搬回新的容器，免得每 5 秒闪一次 */
let chart = null, series = null, chartEl = null, chartKey = "", priceLines = [], linePrices = [], klineSeq = 0, needFit = false, barCount = 0;
let entryIdx = null, lineDefs = [], lastBars = [], edgeEl = null, limitAt = "", lineSig = "", markSig = "", pxSig = "";
try { S.scaleAll = localStorage.getItem("kline-scale") === "all"; } catch (err) { S.scaleAll = false; /* 读不了就用默认：贴合 K 线 */ }
// KLINE-SCALE-BEGIN
const DEFAULT_BARS = 120, RIGHT_BARS = 5, ENTRY_LEAD_BARS = 10, ENTRY_LEAD_PX = 110, MAX_SPAN_BARS = 600;
// 默认视图：从开仓那根前 10 根看到最新一根后 5 根的空位，这段超过 600 根或没有开仓点就退回最近 120 根
// 开仓前留白：至少 10 根，窄屏再多留，让「开仓 价格」整句放得下（留白像素 = lead × 宽 / (lead + 开仓后的根数)）
function leadBars(width, barsAfter) {
  if (!(width > 2 * ENTRY_LEAD_PX)) return ENTRY_LEAD_BARS;
  return Math.max(ENTRY_LEAD_BARS, Math.ceil((ENTRY_LEAD_PX * barsAfter) / (width - ENTRY_LEAD_PX)));
}
// 至少铺多少根：宽屏 120，窄屏按每根约 7px 算（宽度未知按 120）
function minBars(width) {
  return width > 0 ? Math.min(DEFAULT_BARS, Math.max(40, Math.round(width / 7))) : DEFAULT_BARS;
}
function defaultRange(n, entryIdx, width) {
  const to = Math.max(n - 1, 0) + RIGHT_BARS;
  if (entryIdx != null && entryIdx >= 0) {
    const min = minBars(width);
    // 开仓很新也至少铺 min 根（不然几根蜡烛撑满全图）；K 线不够 min 根时 from 取负，左边留空而不是拉宽
    const from = Math.max(Math.min(0, n - min), Math.min(entryIdx - leadBars(width, to - entryIdx), n - min));
    if (to - from <= MAX_SPAN_BARS) return { from, to };
  }
  return { from: Math.max(0, n - DEFAULT_BARS), to };
}
// 价格轴认看得见的 K 线 + 开仓 / 止盈 / 止损（含移动止损）线；保命、启动线超出就贴边；开了「显示全部价位」才把所有线算进去。
// core 是那几条线的价格数组。返回 {min, max} 或 null
function scaleRange(bars, range, lines, core, all) {
  const lo = Math.max(0, Math.floor(range.from)), hi = Math.min(bars.length - 1, Math.ceil(range.to));
  let min = Infinity, max = -Infinity;
  for (let i = lo; i <= hi; i++) { min = Math.min(min, bars[i].low); max = Math.max(max, bars[i].high); }
  if (!(min <= max)) return null;
  for (const v of all ? lines : core) if (v != null) { min = Math.min(min, v); max = Math.max(max, v); }
  return { min, max };
}
// scaleMargins 是占图高的比例，换成占价格范围的比例：数据只占 1 - 2·pad 的高度
const padFrac = (pad) => pad / (1 - 2 * pad);
// 超出价格轴（含上下 pad 边距）的线：贴在上沿 / 下沿，留名字和价格
function edgeLabels(defs, r, pad) {
  const h = r.max - r.min;
  const lo = r.min - h * pad, hi = r.max + h * pad;
  return defs.filter((d) => d.price != null && (d.price > hi || d.price < lo)).map((d) => ({ ...d, edge: d.price > hi ? "top" : "bottom" }))
    .sort((a, b) => (a.edge !== b.edge ? (a.edge === "top" ? -1 : 1) : a.edge === "top" ? b.price - a.price : a.price - b.price));
}
// 到时点在最后一根 K 线之后：不撑大时间轴，在图边上写一句
function limitNote(openedMs, limitS, bars, fmt) {
  if (openedMs == null || !limitS || !bars.length) return "";
  const end = openedMs + limitS * 1000;
  return end > bars[bars.length - 1].time * 1000 ? `到时 ${fmt(end)} →` : "";
}
// 开仓那根 K 线的下标；开仓早于第一根 / 没有开仓时间 → null
function entryBarIndex(p, bars) {
  if (!bars.length || !p.opened_at) return null;
  const at = Math.floor(new Date(p.opened_at).getTime() / 1000);
  if (at < bars[0].time) return null;
  let k = 0;
  for (let i = 0; i < bars.length && bars[i].time <= at; i++) k = i;
  return k;
}
// KLINE-SCALE-END
const SCALE_PAD = 0.09;
function fitView(c) {
  if (c === chart && barCount > 1) {
    c.timeScale().setVisibleLogicalRange(defaultRange(barCount, entryIdx, c.timeScale().width()));
    c.priceScale("right").applyOptions({ autoScale: true });
  } else {
    c.timeScale().fitContent();
  }
}
// 超出价格轴的线以及「到时」说明：盖在图上的小标签，不挡鼠标
function updateEdges() {
  if (!chartEl || !series) return;
  if (!edgeEl) {
    edgeEl = document.createElement("div");
    edgeEl.className = "kedge";
    chartEl.appendChild(edgeEl);
  }
  const vr = chart.timeScale().getVisibleLogicalRange();
  const r = vr && lastBars.length ? scaleRange(lastBars, vr, lineDefs.map((d) => d.price), lineDefs.filter((d) => d.core).map((d) => d.price), S.scaleAll) : null;
  const out = r && !S.scaleAll ? edgeLabels(lineDefs, r, padFrac(SCALE_PAD)) : [];
  let nTop = 0, nBot = 0;
  const html = out.map((d) => {
    const n = d.edge === "top" ? nTop++ : nBot++;
    return `<span class="ke" style="${d.edge}:${n * 18 + (d.edge === "bottom" ? 28 : 4)}px;color:${d.color}">${d.edge === "top" ? "▲" : "▼"} ${esc(d.title)} ${esc(price(d.price, d.symbol))}</span>`;
  }).join("");
  const next = html + (limitAt ? `<span class="ke kl">${esc(limitAt)}</span>` : "");
  if (edgeEl.__html !== next) edgeEl.innerHTML = edgeEl.__html = next;
}
// 拖动 / 缩放时可见范围一帧变好几次：边上标签一帧只算一次
let edgesFrame = 0;
function updateEdgesSoon() {
  if (!edgesFrame) edgesFrame = requestAnimationFrame(() => { edgesFrame = 0; updateEdges(); });
}
// 图和页面抢手势：照地图控件（Google Maps / Mapbox 的 cooperative gestures）的通行做法——
// 电脑：普通滚轮留给页面滚动，按住 Ctrl / ⌘ 再滚轮才缩放，鼠标拖动平移，双击复位；
// 手机：单指滑动留给页面滚动（不接管触摸拖动），双指捏合才缩放 / 平移图。
const CHART_COOP = {
  handleScroll: { mouseWheel: false, pressedMouseMove: true, horzTouchDrag: false, vertTouchDrag: false },
  handleScale: { mouseWheel: false, pinch: true, axisPressedMouseMove: true, axisDoubleClickReset: true },
};
// 全屏看图：同一块（走势图 + 周期按钮 + 图例）铺满屏幕，线和开仓点都在；Esc / ✕ / 切走页面退出
function setChartFull(on) {
  S.chartFull = on;
  document.body.classList.toggle("chart-fs", on);
  render();
  setTimeout(() => { if (chart) fitView(chart); }, 80); // 容器尺寸变了，等重排完再铺满
}
window.addEventListener("keydown", (e) => { if (e.key === "Escape" && S.chartFull) setChartFull(false); });
// 列表框（.scroll）：滚轮放在框里面不滚框、照常滚整页；只有鼠标在它的滚动条上滚 / 拖，框才动。
// 手机上没有滚轮：手指在框里滑照样滚框。弹窗里的框不管（弹窗里本来就是要滚框）
window.addEventListener(
  "wheel",
  (e) => {
    const box = e.target instanceof Element ? e.target.closest(".scroll") : null;
    if (!box || box.closest("#layer") || e.ctrlKey) return;
    const r = box.getBoundingClientRect();
    const bar = box.offsetWidth - box.clientWidth;
    if (bar > 0 && e.clientX >= r.right - bar - 1) return; // 在滚动条上：让框自己滚
    e.preventDefault();
    const k = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? window.innerHeight : 1;
    window.scrollBy(e.deltaX * k, e.deltaY * k);
  },
  { passive: false, capture: true },
);

window.addEventListener("hashchange", () => { if (S.chartFull) setChartFull(false); });
const chartsList = [];
function lockChart(c, el) {
  chartsList.push(c);
  el.addEventListener("dblclick", () => fitView(c)); // 双击图：回到默认范围
  return c;
}
function chartWheel(on) {
  const o = { handleScroll: { ...CHART_COOP.handleScroll, mouseWheel: on }, handleScale: { ...CHART_COOP.handleScale, mouseWheel: on } };
  chartsList.forEach((c) => c.applyOptions(o));
}
window.addEventListener("keydown", (e) => { if (e.key === "Control" || e.key === "Meta") chartWheel(true); });
window.addEventListener("keyup", (e) => { if (e.key === "Control" || e.key === "Meta") chartWheel(false); });
window.addEventListener("blur", () => chartWheel(false));
function mountChart() {
  const host = $("#kchart-host");
  if (!host || !window.LightweightCharts) return;
  if (!chartEl) {
    chartEl = document.createElement("div");
    chartEl.style.height = "100%";
    chartEl.style.position = "relative";
    host.appendChild(chartEl);
    chart = lockChart(LightweightCharts.createChart(chartEl, {
      ...CHART_COOP,
      autoSize: true,
      layout: { background: { color: "transparent" }, textColor: getComputedStyle(document.body).getPropertyValue("--dim"), fontFamily: getComputedStyle(document.body).fontFamily },
      grid: { vertLines: { visible: false }, horzLines: { color: "rgba(0,0,0,0.05)" } },
      timeScale: { timeVisible: true, secondsVisible: false, borderVisible: false, tickMarkFormatter: (t) => tick(t) },
      rightPriceScale: { borderVisible: false, scaleMargins: { top: SCALE_PAD, bottom: SCALE_PAD } },
      localization: { timeFormatter: (t) => hm(t * 1000) },
    }), chartEl);
    chart.timeScale().subscribeVisibleLogicalRangeChange(updateEdgesSoon);
    const css = getComputedStyle(document.documentElement);
    const up = css.getPropertyValue("--up").trim(), down = css.getPropertyValue("--down").trim();
    series = chart.addCandlestickSeries({
      upColor: up, downColor: down, borderVisible: false, wickUpColor: up, wickDownColor: down,
      // 默认只贴合看得见的 K 线 + 开仓价；「显示全部价位」时把止盈、保命这些线也包进来
      autoscaleInfoProvider: (orig) => {
        const r = orig();
        if (!r) return r;
        const extra = S.scaleAll ? linePrices : lineDefs.filter((d) => d.core).map((d) => d.price);
        const vals = extra.filter((v) => v != null);
        if (!vals.length) return r;
        return { ...r, priceRange: { minValue: Math.min(r.priceRange.minValue, ...vals), maxValue: Math.max(r.priceRange.maxValue, ...vals) } };
      },
    });
  } else if (chartEl.parentNode !== host) {
    host.appendChild(chartEl);
  }
}

async function loadKlines(force) {
  const { page, arg } = route();
  // 别的页的拉数刚好在换到持仓页之后才回来：pageData 还是那一页的，没有 positions
  if (page !== "positions" || !pageData || pageKey !== dataId("positions", arg) || !(pageData.positions || []).length) return;
  mountChart();
  if (!series) return;
  const p = pageData.positions.find((x) => x.position_id === arg) || pageData.positions[0];
  const key = `${p.position_id}|${S.tf}`;
  const switched = key !== chartKey;
  if (switched) {
    // 换了一笔 / 换了周期：先清掉上一个币的 K 线和价格线，价格轴重新自动缩放（不然会停在上一个币的价格区间）
    chartKey = key;
    series.setData([]);
    barCount = 0;
    series.setMarkers([]);
    priceLines.forEach((l) => series.removePriceLine(l));
    priceLines = [];
    lineSig = markSig = "";
    linePrices = [];
    lineDefs = [];
    lastBars = [];
    entryIdx = null;
    limitAt = "";
    updateEdges();
    chart.priceScale("right").applyOptions({ autoScale: true });
    needFit = true;
  }
  const seq = ++klineSeq;
  let bars = [];
  try {
    const r = await fetch(`/api/klines?symbol=${encodeURIComponent(p.symbol)}&interval=${S.tf}`, { cache: "no-store" });
    if (!r.ok) throw new Error(String(r.status));
    bars = await r.json();
    if (seq !== klineSeq || key !== chartKey) return; // 等的时候又换了：这份是旧币的，不画
    // 每 2 秒一轮多半只是最后一根在动 / 多出一根：只补这一两根，不整批重灌
    if (barsExtend(lastBars, bars)) bars.slice(lastBars.length - 1).forEach((b) => series.update(b));
    else series.setData(bars);
    barCount = bars.length;
    lastBars = bars;
    entryIdx = entryBarIndex(p, bars);
    const dg = pxDigits(p.entry_price || 1, p.symbol);
    if (pxSig !== `${key}|${dg}`) series.applyOptions({ priceFormat: { type: "price", precision: dg, minMove: 10 ** -dg } });
    pxSig = `${key}|${dg}`;
  } catch (e) {
    return;
  }
  const tl = trailLines(p) || {};
  linePrices = [p.take_profit_price, p.entry_price, p.stop_loss_price, p.lifeboat_price, tl.start, tl.stop];
  const css = getComputedStyle(document.documentElement);
  const c = (v) => css.getPropertyValue(v).trim();
  const L = LightweightCharts.LineStyle;
  const defs = [
    [p.take_profit_price, "止盈", c("--up"), L.Dashed],
    [p.entry_price, "开仓", c("--faint"), L.Solid],
    [p.stop_loss_price, "固定止损计划", c("--sl"), L.Dashed],
    [p.lifeboat_price, "保命", c("--down"), L.Dotted],
    [tl.start, `赚${(+p.trailing_activation_pct).toFixed(2)}%开始跟`, c("--accent") + "73", L.LargeDashed],
    tl.confirmed ? [tl.stop, "移动止损", c("--accent"), L.Solid, 2] : [tl.stop, tl.held ? "移动止损·未换挂" : "移动止损·程序记录（未确认）", c("--accent") + "73", L.Dashed],
  ].filter(([v]) => v != null);
  const sig = JSON.stringify(defs);
  if (sig !== lineSig) {
    priceLines.forEach((l) => series.removePriceLine(l));
    priceLines = defs.map(([v, title, color, lineStyle, lineWidth = 1]) => series.createPriceLine({ price: v, title, color, lineStyle, lineWidth, axisLabelVisible: true }));
    lineSig = sig;
  }
  lineDefs = defs.map(([v, title, color]) => ({ price: v, title, color, symbol: p.symbol, core: title !== "保命" && !title.startsWith("赚") }));
  limitAt = limitNote(p.opened_at ? new Date(p.opened_at).getTime() : null, p.time_limit_s, bars, hm);
  const marks = entryMarkers(p, bars);
  if (JSON.stringify(marks) !== markSig) series.setMarkers(marks);
  markSig = JSON.stringify(marks);
  if (force || needFit) fitView(chart);
  needFit = false;
  updateEdges();
}
// 新一批 K 线是上一批的延续：第一根同一时刻，根数一样或多一根，倒数第二根往前都没变
function barsExtend(prev, bars) {
  const n = prev.length;
  if (n < 2 || bars.length < n || bars.length > n + 1 || bars[0].time !== prev[0].time) return false;
  const a = prev[n - 2], b = bars[n - 2];
  return a.time === b.time && a.open === b.open && a.high === b.high && a.low === b.low && a.close === b.close;
}
// 开仓点：标在开仓那根 K 线上（多 = 绿箭头在下方，空 = 红箭头在上方），带开仓价；
// 开仓时间早于这批 K 线的第一根（周期太小、看不到那么久以前）就不标，价格线还在
function entryMarkers(p, bars) {
  if (p.entry_price == null) return [];
  const k = entryBarIndex(p, bars);
  if (k == null) return [];
  const bar = bars[k];
  const long = p.side === "long";
  const css = getComputedStyle(document.documentElement);
  return [{
    time: bar.time, position: long ? "belowBar" : "aboveBar", shape: long ? "arrowUp" : "arrowDown",
    color: css.getPropertyValue(long ? "--up" : "--down").trim(), text: `开仓 ${price(p.entry_price, p.symbol)}`,
  }];
}

/* ---------- 团队 */
/* 流程图（设计稿 /mnt/project-files/design/team-flow/handoff.md）：三条泳道，节点固定坐标，连线 SVG；
   宽度不够整体缩放，手机上改成按泳道竖排。点 = 进度 + 健康：出错 > 正在做 > 这一轮做完 > 等待 */
const TF_W = 1180, TF_H = 570, NODE_W = 168, NODE_H = 92;
const TF_POS = {
  screener: [10, 34], market: [250, 34], desk: [490, 34], commander: [730, 34], card: [970, 34],
  book_reader: [10, 214], predictor: [250, 290], sentinel: [490, 214], gate: [730, 214], risk_guard: [970, 186], executor: [970, 300],
  alerting: [10, 446], reconciler: [250, 446], patrol: [490, 446], keeper: [730, 446], lifeboat: [970, 446],
};
const TF_LANES = [
  ["① 准备作战卡", "每轮开会出一张", 0, 150, ["screener", "market", "desk", "commander", "card"]],
  ["② 发现信号 → 下单", "几秒内走完", 166, 230, ["book_reader", "predictor", "sentinel", "gate", "risk_guard", "executor"]],
  ["③ 看着持仓", "有仓位就一直盯", 412, 150, ["lifeboat", "keeper", "patrol", "reconciler", "alerting"]],
];
const tfR = (k) => [TF_POS[k][0] + NODE_W, TF_POS[k][1] + NODE_H / 2];
const tfL = (k) => [TF_POS[k][0], TF_POS[k][1] + NODE_H / 2];
const tfT = (k) => [TF_POS[k][0] + NODE_W / 2, TF_POS[k][1]];
const tfB = (k) => [TF_POS[k][0] + NODE_W / 2, TF_POS[k][1] + NODE_H];
// [起点, 终点, 终点角色（连线颜色跟它）, 标签, 弯法]
const TF_EDGES = [
  [tfR("screener"), tfL("market"), "market", "候选池"],
  [tfT("market"), tfT("commander"), "commander", "上会名单", "over"],
  [tfR("desk"), tfL("commander"), "commander", "大盘方向"],
  [[TF_POS.desk[0] + 40, TF_POS.desk[1] + NODE_H], [TF_POS.sentinel[0] + 40, TF_POS.sentinel[1]], "sentinel", "大盘方向"],
  [tfB("market"), tfT("book_reader"), "book_reader", "要盯的币", "gap"],
  [tfR("commander"), tfL("card"), "card", "写卡"],
  [tfB("card"), tfT("sentinel"), "sentinel", "关键价位", "gap"],
  [[TF_POS.card[0] + 120, TF_POS.card[1] + NODE_H], [TF_POS.risk_guard[0] + 120, TF_POS.risk_guard[1]], "risk_guard", "止损范围", "v-top", "side"],
  [tfR("book_reader"), tfL("sentinel"), "sentinel", "盘口 · 成交"],
  [tfB("book_reader"), tfL("predictor"), "predictor", "K 线", "down-right"],
  [tfR("predictor"), [TF_POS.sentinel[0], TF_POS.sentinel[1] + NODE_H - 16], "sentinel", "预测", "s"],
  [tfR("sentinel"), tfL("gate"), "gate", "信号"],
  [tfR("gate"), tfL("risk_guard"), "risk_guard", "做不做"],
  [tfB("risk_guard"), tfT("executor"), "executor", "仓位"],
  [tfB("executor"), tfT("lifeboat"), "lifeboat", "同时挂"],
  [tfL("executor"), tfT("keeper"), "keeper", "开出的仓位", "tree"],
  [tfL("executor"), tfT("patrol"), "patrol", "", "tree"],
  [tfL("executor"), tfT("reconciler"), "reconciler", "", "tree"],
];
// 和卡相反的信号（主程序按有没有仓分流）：平时不画，最近 30 分钟真交出去过才亮，标最近一次的币、时间、结果
// 事件虚线随时间变淡：10 分钟内不透明，到 30 分钟线性降到 0.35；标签尾巴带相对时间
const EDGE_FADE_FROM = 10, EDGE_SHOW_MIN = 30, EDGE_FADE_TO = 0.35;
const edgeAgeMin = (ts) => Math.max(0, (Date.now() - new Date(ts).getTime()) / 60000);
const edgeOpacity = (ts) => {
  const m = edgeAgeMin(ts);
  if (m <= EDGE_FADE_FROM) return 1;
  return Math.max(EDGE_FADE_TO, 1 - ((1 - EDGE_FADE_TO) * (m - EDGE_FADE_FROM)) / (EDGE_SHOW_MIN - EDGE_FADE_FROM));
};
const edgeAgeTxt = (ts) => { const m = Math.floor(edgeAgeMin(ts)); return m < 1 ? "· 刚刚" : `· ${m} 分钟前`; };
const VERDICT = { close: "管家判平", hold: "管家判继续拿" };
function againstEdges(list) {
  const out = [];
  const pick = (act) => (list || []).filter((e) => e.actions.includes(act));
  const tag = (hits, tail) => `${hits[0].symbol.replace(/USDT$/, "")} ${hm(hits[0].ts)} ${tail}${hits.length > 1 ? `（30 分钟内 ${hits.length} 次）` : ""} ${edgeAgeTxt(hits[0].ts)}`;
  const k = pick("keeper_recheck");
  if (k.length) out.push([[TF_POS.sentinel[0] + 130, TF_POS.sentinel[1] + NODE_H], [TF_POS.keeper[0] + 40, TF_POS.keeper[1]], "keeper", tag(k, `反向 → ${VERDICT[k[0].keeper_verdict] || "管家急查中"}`), "drop", "against", k[0].ts]);
  const m = pick("meeting");
  if (m.length) out.push([[TF_POS.sentinel[0] + 40, TF_POS.sentinel[1]], [TF_POS.commander[0] + 40, TF_POS.commander[1] + NODE_H], "commander", tag(m, "反向 → 临时会"), "up", "against", m[0].ts]);
  return out;
}
// 预测员连续看反作战卡 → 单币临时会：同样平时不画，最近 30 分钟开过才亮
const BIAS_WORD = { long: "改做多", short: "改做空", both: "两边都可", no_trade: "改不做" };
function predictorMeetingEdges(list) {
  if (!list || !list.length) return [];
  const p = list[0];
  const label = `${p.symbol.replace(/USDT$/, "")} ${hm(p.ts)} 预测看反 → 临时会${BIAS_WORD[p.bias] ? "：" + BIAS_WORD[p.bias] : ""}${list.length > 1 ? `（30 分钟内 ${list.length} 次）` : ""} ${edgeAgeTxt(p.ts)}`;
  return [[[TF_POS.predictor[0] + 150, TF_POS.predictor[1]], [TF_POS.commander[0] + 14, TF_POS.commander[1] + NODE_H], "commander", label, "climb", "against", p.ts]];
}
// 只在真正启动大盘独立更新后画虚线，疑似 / 被拦仍进哨兵日志，不冒充已触发。
function regimeFlipEdges(event) {
  if (!event) return [];
  const flip = (event.output && event.output.regime_flip) || {};
  const desk = flip.scope === "market_desk";
  const target = desk ? "大盘台" : "独立更新";
  const to = desk ? "desk" : "market";
  return [[[TF_POS.sentinel[0] + 100, TF_POS.sentinel[1]], [TF_POS[to][0] + 140, TF_POS[to][1] + NODE_H], to, `${hm(event.ts)} 大盘变化 → ${target} ${edgeAgeTxt(event.ts)}`, "flip", "against", event.ts]];
}
function tfEdge([a, b, to, label, how, extra, ts], prog) {
  const [x1, y1] = a, [x2, y2] = b;
  const st = prog[to] === "doing" ? "doing" : prog[to] === "done" ? "done" : "";
  const cls = `e ${st} ${extra || ""}`;
  let d, lx, ly, dir;
  if (how === "elbow") { d = `M${x1},${y1} H${x2 - 12} Q${x2},${y1} ${x2},${y1 + 12} V${y2 - 1}`; lx = x2 - 70; ly = y1 - 6; dir = "d"; }
  else if (how === "down-right") { d = `M${x1},${y1} V${y2 - 10} Q${x1},${y2} ${x1 + 10},${y2} H${x2 - 1}`; lx = x1 + 14; ly = y2 - 6; dir = "r"; }
  else if (how === "tree") { const yy = 428; d = `M${x1},${y1} H${x1 - 22} V${yy} H${x2} V${y2 - 1}`; lx = x1 - 120; ly = yy - 6; dir = "d"; }
  // 从上一条泳道的节点底下，沿泳道之间的空隙横过去，再落到下一条泳道的节点顶上
  else if (how === "gap") { const ym = 158, k = x2 < x1 ? -10 : 10; d = `M${x1},${y1} V${ym - 10} Q${x1},${ym} ${x1 + k},${ym} H${x2 - k} Q${x2},${ym} ${x2},${ym + 10} V${y2 - 1}`; lx = Math.min(x1, x2) + 16; ly = ym - 4; dir = "d"; }
  // 绕过中间的节点：从起点顶上抬到泳道上沿，横过去，再落到终点顶上
  else if (how === "over") { const yy = 14; d = `M${x1},${y1} V${yy + 10} Q${x1},${yy} ${x1 + 10},${yy} H${x2 - 10} Q${x2},${yy} ${x2},${yy + 10} V${y2 - 1}`; lx = Math.min(x1, x2) + 16; ly = yy - 3; dir = "d"; }
  else if (how === "drop") { const yy = 402; d = `M${x1},${y1} V${yy} H${x2} V${y2 - 1}`; lx = x1 + 8; ly = yy - 5; dir = "d"; }
  // 从下面的节点顶上往上，在第一、二泳道之间横过去，再往上进目标节点底
  else if (how === "climb") { const yy = 182; d = `M${x1},${y1} V${yy + 10} Q${x1},${yy} ${x1 + 10},${yy} H${x2 - 10} Q${x2},${yy} ${x2},${yy - 10} V${y2 + 1}`; lx = x1 - 140; ly = yy - 5; dir = "u"; }
  else if (how === "up") { d = `M${x1},${y1} V${y2 + 1}`; lx = x1 - 8; ly = (y1 + y2) / 2 + 22; dir = "u"; }
  else if (how === "flip") { const yy = 172; d = `M${x1},${y1} V${yy + 10} Q${x1},${yy} ${x1 - 10},${yy} H${x2 + 10} Q${x2},${yy} ${x2},${yy - 10} V${y2 + 1}`; lx = x2 - 10; ly = yy - 6; dir = "u"; }
  else if (how === "s") { const mx = (x1 + x2) / 2; d = `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2 - 1},${y2}`; lx = x1 + 8; ly = y1 - 6; dir = "r"; }
  else if (x1 === x2) { d = `M${x1},${y1} V${y2 - 1}`; lx = x1 + 8; ly = how === "v-top" ? y1 + 18 : (y1 + y2) / 2 + 4; dir = "d"; }
  else { d = `M${x1},${y1} H${x2 - 1}`; lx = (x1 + x2) / 2 - label.length * 5.5; ly = y1 - 6; dir = "r"; }
  const ah = dir === "r" ? `M${x2},${y2} l-7,-4 v8 z` : dir === "u" ? `M${x2},${y2} l-4,7 h8 z` : `M${x2},${y2} l-4,-7 h8 z`;
  const body = `<path class="${cls}" d="${d}"/><path class="ah ${st} ${extra || ""}" d="${ah}"/>${label ? `<text class="${extra || ""}" x="${lx}" y="${ly}"${how === "up" ? ' text-anchor="end"' : ""}>${esc(label)}</text>` : ""}`;
  return ts ? `<g class="aged" style="opacity:${edgeOpacity(ts).toFixed(2)}">${body}</g>` : body;
}
const PROG_LABEL = { err: "出错", doing: "正在做", done: "这一轮做完", wait: "等待" };
function tfCardNode() {
  const c = data && data.card;
  const tradable = c ? c.symbols.filter((x) => x.bias === "long" || x.bias === "short").length : 0;
  const a = !c ? "还没有作战卡" : c.valid ? `${tradable} 个可做 · ${c.expires_at ? "有效到 " + hm(c.expires_at) : "有效期未知"}` : `已过期（${c.expires_at ? hm(c.expires_at) : "—"}）`;
  return { key: "card", html: `<span class="nm">作战卡${c && c.version ? " v" + c.version : ""}</span><small>${esc(a)}</small><small class="cnt">关键价位、止损范围</small>`, cls: "doc", act: c ? clickable("card") : "" };
}
// 总盘会：军师团先开的那场，挑哪些币上会（给军师团）、哪些币要盯（给盘口员）
function tfMarketNode(m, p, mr) {
  if (mr && mr.state !== "ok") return `<span class="nm"><i class="dot ${p}" title="${PROG_LABEL[p]}"></i>总盘会${hbBadge(mr)}</span><small>${esc(mr.status)}</small><small class="cnt">挑上会的币、要盯的币</small>`;
  const line = runLine(mr);
  const st = line ? line : !m ? "还没有记录" : m.status === "running" ? "" : m.status === "failed" ? `失败：${m.error || "没写原因"}` : m.status === "interrupted" ? `中断了：${m.error || "没写原因"}` : m.status === "stale" ? "中断了（超过 15 分钟没更新）" : `上次 ${hm(m.finished_at || m.updated_at || m.started_at)}`;
  const now = m && m.status === "running" ? `开会中：${MARKET_DOING[m.stage] || m.stage || "开始"}` : "";
  return `<span class="nm"><i class="dot ${p}" title="${PROG_LABEL[p]}"></i>总盘会</span>${now ? `<small class="now">${esc(now)}</small>` : ""}${st ? `<small>${esc(st)}</small>` : ""}<small class="cnt">挑上会的币、要盯的币</small>`;
}
// 大盘台最近一轮（不是上次发布的那版）：4h / 1h 概率和「短线现状」，和各币会议看到的同一段字
function deskLatestLine(d) {
  if (!d) return "";
  const pct = (v) => (v == null ? "—" : Math.round(v * 100) + "%");
  const tri = (t, q) => (q ? `${t} 涨 ${pct(q.up)} / 平 ${pct(q.flat)} / 跌 ${pct(q.down)}` : "");
  const probs = [tri("4 小时", d.probs_4h), tri("1 小时", d.probs_1h)].filter(Boolean).join(" · ");
  return `<div class="small" style="margin-top:4px"><b>大盘台 ${hms(d.run_at)} 一轮</b>　${esc(probs)}<br>${esc(d.intra)}</div>`;
}
// 团队页点大盘台：最近两轮并排（窄屏上下叠），标出和上一轮比变了什么
const DESK_REASON = { scheduled: "定时", move: "急动加跑", regime_flip: "翻转加跑" };
function deskRunCard(title, r, prev) {
  const pct = (v) => (v == null ? "—" : Math.round(v * 100) + "%");
  const delta = (q, p, k) => {
    if (!prev || !q || !p || q[k] == null || p[k] == null) return "";
    const pp = Math.round((q[k] - p[k]) * 100);
    return pp ? ` <mark class="chg">${pp > 0 ? "+" : ""}${pp}pp</mark>` : "";
  };
  const tri = (q, p) => (q ? ["up", "flat", "down"].map((k) => `${{ up: "涨", flat: "平", down: "跌" }[k]} ${pct(q[k])}${delta(q, p, k)}`).join(" / ") : "—");
  const ago = Math.max(0, Math.round((Date.now() - new Date(r.run_at).getTime()) / 60000));
  const dirChg = prev && prev.direction && r.direction !== prev.direction ? ` <mark class="chg">上一轮 ${esc(prev.direction)}</mark>` : "";
  const pub = r.published_version != null ? `已发布 · 大盘 v${r.published_version}` : "没发布（方向没变、在等确认或没有先验）";
  return `<div class="desk-run"><h3>${title}</h3>
    <div class="dim small">${hm(r.run_at)} · ${ago} 分钟前 · ${esc(DESK_REASON[r.reason] || r.reason)}</div>
    <div>4 小时 <b>${esc(r.direction || "—")}</b>${dirChg}：${tri(r.probs_4h, prev && prev.probs_4h)}</div>
    <div>1 小时：${tri(r.probs_1h, prev && prev.probs_1h)}</div>
    ${r.intra ? `<div class="small">${esc(r.intra)}</div>` : ""}
    <div class="small">${pub}</div>
    ${r.regime ? `<details class="small"><summary>模型的判断原文</summary>${esc(r.regime)}</details>` : ""}</div>`;
}
function deskRunsCard(runs) {
  if (!runs || !runs.length) return "";
  const [a, b] = runs;
  return `<section class="card mt"><div class="desk-runs" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,300px),1fr));gap:12px">${deskRunCard("最新大盘情况", a, b)}${b ? deskRunCard("上一次大盘情况", b, null) : ""}</div></section>`;
}
// 大盘台：独立判断候选池未来 4 小时的涨 / 平 / 跌，管着大盘方向（接管）或退回 Flash
function deskProgress(k) {
  if (!k || k.state === "off") return "wait";
  if (k.state === "bad" || k.error) return "err";
  return k.runs_today > 0 ? "done" : "wait";
}
function tfDeskNode(k) {
  if (!k) return `<span class="nm"><i class="dot wait"></i>大盘台</span><small>还没有数据</small>`;
  const p = deskProgress(k);
  const pct = (v) => (v == null ? "—" : Math.round(v * 100) + "%");
  const owner = k.owner === "desk" ? "大盘台接管" : k.owner === "flash" ? "回退 Flash" : "";
  const dir = k.direction ? `${k.direction}${k.published_version != null ? " v" + k.published_version : ""}` : "";
  const tri = (t, q) => (q ? `${t} 涨 ${pct(q.up)} 平 ${pct(q.flat)} 跌 ${pct(q.down)}` : "");
  const probs = tri("4h", k.probs);
  const probs1h = tri("1h", k.probs_1h);
  const when = k.state === "off" ? "" : [k.last_run_at ? `上次 ${hm(k.last_run_at)}` : "", k.next_run_at ? `下次 ${hm(k.next_run_at)}` : ""].filter(Boolean).join(" · ");
  const head = [owner, dir].filter(Boolean).join(" · ");
  const line = (t, cls) => (t ? `<small${cls ? ` class="${cls}"` : ""} title="${esc(t)}">${esc(t)}</small>` : "");
  return `<span class="nm"><i class="dot ${p}" title="${PROG_LABEL[p]}"></i>大盘台${hbBadge(k)}</span>${k.state === "off" ? line("没开（slow.toml [market_desk]）") : line(head, "now") + line(probs) + line(probs1h) + line(when) + `<small class="cnt">今日 ${k.runs_today} 次 · $${k.cost_today_usd.toFixed(3)}</small>`}`;
}
// 统一的「上次 hh:mm · 下次 hh:mm · 刚刚：…」；模块没报的字段就不写（不猜）
function runLine(r) {
  if (!r) return "";
  return [r.last_run_at ? `上次 ${hm(r.last_run_at)}` : "", r.next_run_at ? `下次 ${hm(r.next_run_at)}` : "", r.summary ? `刚刚：${r.summary}` : ""].filter(Boolean).join(" · ");
}
// 心跳好坏单独用角标（点只表示流程走到哪）
function hbBadge(r) {
  const label = {
    ok: "心跳正常",
    slow: r.status.startsWith("等待首次") ? "等待首次运行" : r.status.startsWith("准备中") ? "准备中" : "降级运行",
    bad: r.status.startsWith("无心跳") ? "无心跳" : "失败",
    off: "未启用",
  }[r.state];
  return label ? `<em class="hb ${r.state}" title="${esc(r.status)}">${label}</em>` : "";
}
// 预测员：正常时用它自己报的「已算 N/M 个币，上次 hh:mm」，M = 盯着的币（总盘会挑中 + 作战卡 + 持仓）
function predictorText(r) {
  if (r.state !== "ok" || !r.headline) return r.status;
  const m = /^已算 (\d+)\/(\d+) 个币(.*?)(（今天预测 \d+ 次）)?$/.exec(r.headline);
  return m ? `盯着 ${m[2]} 个币，已算 ${m[1]} 个${m[3]}` : r.headline;
}
// 管家卡固定一行：最近 30 分钟的反向急查（不被例行复查的「刚刚」冲掉）
function againstLine(c) {
  if (!c) return "";
  const v = c.failed ? "没问成" : c.verdict ? (c.verdict === "close" ? "平仓" : "拿住") : "查着";
  return `急查 ${coin(c.symbol)} ${hm(c.ts)} → ${v}`;
}
function tfNode(r, checks) {
  const line = r.state === "ok" ? runLine(r) : "";
  const ag = r.role === "keeper" && checks && checks.length ? checks[0] : null;
  const status = r.role === "alerting" && r.progress !== "err" ? "任何一步出事都推给你" : line || (r.role === "predictor" ? predictorText(r) : r.status);
  return `<span class="nm"><i class="dot ${r.progress}" title="${PROG_LABEL[r.progress]}"></i>${esc(r.name)}${hbBadge(r)}</span>
    ${ag ? `<small class="now against" title="${esc(ag.reason || ag.desc || "")}">${esc(againstLine(ag))}</small>` : r.doing ? `<small class="now">${esc(r.doing)}</small>` : ""}<small title="${esc(r.headline || r.status)}">${esc(status)}</small>
    <small class="cnt">今日 ${r.calls} 次${r.rejected ? ` · 拒 ${r.rejected}` : ""}${r.errors ? ` · <span class="down">错 ${r.errors}</span>` : ""}${r.cost_usd ? ` · $${r.cost_usd.toFixed(3)}` : ""}</small>`;
}
/* 「现在走到」轮播：每处一条，4 秒换一条；鼠标放上去停住，点一下看下一条。
   页面每 5 秒重画不打断（第几条记在 tfNowI） */
let tfNowItems = [], tfNowI = 0, tfNowHold = false;
function tfNowHtml() {
  const n = tfNowItems.length;
  if (!n) return "";
  const i = tfNowI % n;
  return `<b>现在走到：</b><span class="t">${esc(tfNowItems[i])}</span>${n > 1 ? `<span class="k">${i + 1}/${n}</span>` : ""}`;
}
function tfNowStep() {
  const el = $(".tf-now");
  if (!el || tfNowItems.length < 2) return;
  tfNowI = (tfNowI + 1) % tfNowItems.length;
  el.innerHTML = tfNowHtml();
}
setInterval(() => { if (!tfNowHold) tfNowStep(); }, 4000);
document.addEventListener("mouseover", (e) => { tfNowHold = !!(e.target.closest && e.target.closest(".tf-now")); });
document.addEventListener("click", (e) => { if (e.target.closest && e.target.closest(".tf-now")) tfNowStep(); });
function teamFlow(d) {
  const by = Object.fromEntries(d.roles.map((r) => [r.role, r]));
  const prog = Object.fromEntries(d.roles.map((r) => [r.role, r.progress]));
  prog.card = data && data.card && data.card.valid ? "done" : "wait";
  prog.market = d.market_progress;
  prog.desk = deskProgress(d.desk);
  const node = (k, inCanvas) => {
    if (k === "market") return `<a class="tf-node ${d.market_progress} ${d.selected === "market" ? "on" : ""}" href="#team/market" style="${inCanvas ? `left:${TF_POS.market[0]}px;top:${TF_POS.market[1]}px` : ""}">${tfMarketNode(d.market, d.market_progress, d.market_role)}</a>`;
    if (k === "desk") return `<a class="tf-node desk ${deskProgress(d.desk)} ${d.selected_desk ? "on" : ""}" href="#team/desk" style="${inCanvas ? `left:${TF_POS.desk[0]}px;top:${TF_POS.desk[1]}px` : ""}">${tfDeskNode(d.desk)}</a>`;
    if (k === "card") { const n = tfCardNode(); return `<div class="tf-node doc" ${n.act} style="${inCanvas ? `left:${TF_POS.card[0]}px;top:${TF_POS.card[1]}px` : ""}">${n.html}</div>`; }
    const r = by[k];
    if (!r) return "";
    return `<a class="tf-node ${r.progress} ${r.role === d.selected && !d.selected_desk ? "on" : ""}" href="#team/${r.role}" style="${inCanvas ? `left:${TF_POS[k][0]}px;top:${TF_POS[k][1]}px` : ""}">${tfNode(r, d.against_checks)}</a>`;
  };
  // 盯持仓是常态，放最后说；其余按流程顺序，每处一条，轮播着看全
  const busy = FLOW_ORDER.map((k) => by[k]).filter((r) => r && r.doing);
  const main = busy.filter((r) => !r.doing.startsWith("盯着"));
  const watching = busy.find((r) => r.doing.endsWith("笔持仓"));
  tfNowItems = main.length
    ? main.map((r) => `${r.name} ${r.doing}`)
    : [watching ? `看着持仓（${watching.doing}）` : "哨兵盯着信号，等下一个机会"];
  const legend = `<div class="tf-legend" aria-label="图例">${["done", "doing", "wait", "err"].map((p) => `<span class="lg"><i class="dot ${p}"></i>${{ done: "这一轮已完成", doing: "正在做", wait: "等待（还没轮到 / 没活）", err: "出错或没心跳" }[p]}</span>`).join("")}<span class="lg"><em class="hb ok">心跳正常</em>右上角 = 模块心跳</span><div class="tf-now" title="鼠标放上去停住，点一下看下一条" aria-live="polite">${tfNowHtml()}</div></div>`;
  const known = new Set([...Object.keys(TF_POS)]);
  const extra = d.roles.filter((r) => !known.has(r.role));
  const lanes = TF_LANES.map(([t, sub, y, h]) => `<div class="tf-band" style="top:${y}px;height:${h}px"><b>${t}<span>${sub}</span></b></div>`).join("");
  const edges = `<svg class="tf-edges" width="${TF_W}" height="${TF_H}" viewBox="0 0 ${TF_W} ${TF_H}" aria-hidden="true">${[...TF_EDGES, ...againstEdges(d.against), ...predictorMeetingEdges(d.predictor_meetings), ...regimeFlipEdges(d.regime_flip)].map((e) => tfEdge(e, prog)).join("")}</svg>`;
  const canvas = `<div class="tf-fit"><div class="tf-canvas" style="width:${TF_W}px;height:${TF_H}px">${lanes}${edges}${Object.keys(TF_POS).map((k) => node(k, true)).join("")}
    <div class="tf-watch"><span>看门狗在外面守着：主程序卡住或退出就自动拉起</span></div></div></div>`;
  // 手机：按泳道竖排，不画线
  const stack = `<div class="tf-stack">${TF_LANES.map(([t, sub, , , ks]) => `<div class="tf-lane"><b>${t}<span>${sub}</span></b>${ks.map((k) => node(k, false)).join("")}</div>`).join("")}</div>`;
  const rest = extra.length ? `<div class="tf-stack tf-extra">${extra.map((r) => node(r.role, false)).join("")}</div>` : "";
  const flipLog = d.regime_flip ? `<div class="note mt">最近一次：${hms(d.regime_flip.ts)} ${esc(d.regime_flip.text)} · <a href="#team/sentinel">查看哨兵日志 ›</a></div>` : "";
  return `${legend}<section class="card tf">${canvas}${stack}${rest}</section>${flipLog}`;
}
const FLOW_ORDER = ["screener", "commander", "book_reader", "predictor", "sentinel", "gate", "risk_guard", "executor", "lifeboat", "keeper", "patrol", "reconciler", "alerting"];
function fitTeamFlow() {
  const f = $(".tf-fit");
  if (!f) return;
  const c = f.firstElementChild;
  const k = Math.min(1, f.clientWidth / TF_W);
  c.style.transform = `scale(${k})`;
  f.style.height = `${(TF_H + 6) * k}px`;
}
addEventListener("resize", fitTeamFlow);
addEventListener("resize", () => layoutLadders($("#layer")));
// 盘口员只计数、不逐条写决策日志：心跳摘要拆成几行；逐币行情和各路推送读 data/market_snapshot.json（每 5 秒）
const STREAM = { trade: "逐笔成交", book: "盘口", kline: "K 线", mark: "标记价 / 资金费率", liquidation: "强平", aggTrade: "逐笔成交", depth: "盘口深度", bookTicker: "最优买卖价", markPrice: "标记价 / 资金费率", kline_1m: "1 分钟 K 线", forceOrder: "强平" };
const secAgo = (ts) => Math.max(0, Math.round((Date.now() - new Date(ts)) / 1000));
const agoTxt = (sec) => (sec < 60 ? `${sec} 秒前` : sec < 3600 ? `${Math.round(sec / 60)} 分钟前` : `${Math.round(sec / 3600)} 小时前`);
const chg = (v, d, unit) => (v == null ? '<span class="dim">—</span>' : `<span class="${v > 0 ? "up" : v < 0 ? "down" : ""}">${v > 0 ? "+" : ""}${v.toFixed(d)}${unit}</span>`);
const kU = (v) => (v == null ? "—" : v >= 1000 ? `${money(v / 1000, 1)} 千 U` : `${Math.round(v)} U`);
function tapeTables(snap) {
  if (!snap) return '<div class="foot">还没有逐币行情快照（data/market_snapshot.json）：主程序更新到最新版并在跑后，每 5 秒写一次。</div>';
  const age = secAgo(snap.ts);
  const stale = age > 30 ? `<div class="note warn mt">行情快照 ${agoTxt(age)}写的，之后没再更新：主程序可能没在跑。</div>` : "";
  const syms = Object.entries(snap.symbols || {}).sort(([a], [b]) => a.localeCompare(b));
  const rows = syms.map(([sym, v]) => `<tr><td>${coin(sym)}</td><td class="r num">${v.price == null ? "—" : pxFmt(v.price, sym)}</td><td class="r num">${chg(v.chg_1m_pct, 2, "%")}</td><td class="r num">${chg(v.chg_5m_pct, 2, "%")}</td>
    <td class="r num">${v.taker_buy_ratio_1m == null ? "—" : Math.round(v.taker_buy_ratio_1m * 100) + "%"}</td><td class="r num">${v.spread_bps == null ? '<span class="warn">盘口没更新</span>' : v.spread_bps.toFixed(2) + " bp"}</td>
    <td class="r num">${kU(v.liq_long_usdt_1m)}</td><td class="r num">${kU(v.liq_short_usdt_1m)}</td><td class="r num ${v.book_age_s > 10 ? "warn" : ""}">${v.book_age_s == null ? "—" : v.book_age_s + " 秒"}</td></tr>`).join("");
  const coins = syms.length ? `<div class="tw mt"><table><thead><tr><th>币</th><th class="r">现价</th><th class="r">1 分钟</th><th class="r">5 分钟</th><th class="r">主动买占比</th><th class="r">买卖价差</th><th class="r">1 分钟多头强平</th><th class="r">1 分钟空头强平</th><th class="r">盘口多久没更新</th></tr></thead><tbody>${rows}</tbody></table></div>` : '<div class="dim mt">现在没有在盯的币。</div>';
  const st = Object.entries(snap.streams || {});
  const streams = st.length ? `<div class="tw mt"><table><thead><tr><th>数据流</th><th class="r">今天收到</th><th class="r">最后一条</th></tr></thead><tbody>${st.map(([k, x]) => `<tr><td title="${esc(k)}">${esc(STREAM[k] || k)}</td><td class="r num">${esc(Number(x.count_today).toLocaleString())} 条</td><td class="r num">${x.last_at ? `${hms(x.last_at)}（${agoTxt(secAgo(x.last_at))}）` : "—"}</td></tr>`).join("")}</tbody></table></div>` : "";
  return `${stale}<div class="small dim mt">此刻的行情（${hms(snap.ts)}）</div>${coins}${streams ? `<div class="small dim mt">各路数据（今天 0 点起）</div>${streams}` : ""}`;
}
function bookReaderCard(r, snap) {
  const h = r.headline || "";
  const parts = h.split("；");
  const head = parts[0] ? parts[0].split("，") : [];
  const rest = parts.slice(1).filter((x) => !x.startsWith("累计 "));
  const rows = [...head, ...rest].map((x) => `<div>${esc(x)}</div>`).join("");
  return `<section class="card mt"><div class="ch"><h2>盘口员 · 现在的状态</h2><span class="r">${r.last_beat ? "心跳 " + hms(r.last_beat) : "没有心跳"}</span></div>
    <div class="note">盘口员只计数，不逐条记录（一天上万条行情，记下来没法看）。有信号会交给哨兵，哨兵那里能看到每一条。</div>
    ${rows ? `<div class="mt">${rows}</div>` : '<div class="dim mt">还没有报过状态。</div>'}${tapeTables(snap)}</section>`;
}
// 哨兵每币一行（module_status.detail）拆成表：币 | 现在 | 离出信号还差 | 1 小时内最高 | 没出的原因
function sentinelCard(r) {
  const rows = (r.detail || []).map((line) => {
    const sp = line.indexOf(" ");
    const sym = sp > 0 ? line.slice(0, sp) : line;
    const segs = (sp > 0 ? line.slice(sp + 1) : "").split("；");
    let now = segs[0] || "", need = "";
    const k = now.lastIndexOf("，");
    if (k > 0 && /还差|够/.test(now.slice(k + 1))) [now, need] = [now.slice(0, k), now.slice(k + 1)];
    const peak = (segs.find((x) => x.startsWith("1 小时内最高")) || "").replace(/^1 小时内最高\s*/, "");
    const why = segs.filter((x) => x.startsWith("上次跳过") || x.startsWith("不在作战卡上")).join("；");
    const ok = need === "够量能突增";
    return `<tr><td class="hi nowrap">${coin(sym)}</td><td>${esc(now)}</td><td class="${ok ? "up" : ""}">${esc(need) || '<span class="dim">—</span>'}</td><td class="num">${esc(peak) || '<span class="dim">—</span>'}</td><td>${esc(why) || '<span class="dim">—</span>'}</td></tr>`;
  }).join("");
  return `<section class="card mt"><div class="ch"><h2>哨兵 · 每个币为什么还没出信号</h2><span class="r">${r.last_beat ? "心跳 " + hms(r.last_beat) : "没有心跳"}</span></div>
    ${r.headline ? `<div class="note">${esc(r.headline)}</div>` : ""}
    ${rows ? `<div class="tw scroll"><table><thead><tr><th>币</th><th>现在（成交量是平时几倍 · 主动买卖哪边多）</th><th>离出信号还差</th><th>1 小时内最高</th><th>上次没出的原因</th></tr></thead><tbody>${rows}</tbody></table></div>
    <div class="foot">只列现在作战卡上的币和持仓币${r.detail_hidden ? `（另有 ${r.detail_hidden} 个已掉出作战卡的币没列）` : ""}。阈值在「设置 › 执行 › 信号识别」：量能突增要量够倍数、而且主动买或卖占比够高；放量突破还要价格突破关键位。没出信号不一定是坏事，可能只是没行情。</div>` : `<div class="dim">${r.detail_hidden ? `现在作战卡上没有币、也没持仓（另有 ${r.detail_hidden} 个已掉出作战卡的币没列）。` : "哨兵还没报每个币的情况（主程序更新后才有）。"}</div>`}</section>`;
}
// 管家：最近 30 分钟每次反向急查（哪个信号、判了什么、理由）
function againstCard(list) {
  if (!list.length) return "";
  const rows = list.map((c) => `<div><span class="n">${hm(c.ts)}</span><span><b>${coin(c.symbol)}</b> ${esc(c.desc || "反向信号")}${c.strong ? "（强）" : ""} → <b class="${c.verdict === "close" ? "down" : ""}">${esc(c.failed ? "没问成，维持现状" : c.verdict ? (c.verdict === "close" ? "判平仓" : "判继续拿") : "还在查")}</b>${c.p_close != null ? ` · 平仓概率 ${c.p_close.toFixed(2)}` : ""}${c.checked_at ? ` · ${hms(c.checked_at)} 查完` : ""}${c.reason ? `<br><span class="dim small">${esc(c.reason)}</span>` : ""}</span></div>`).join("");
  return `<section class="card mt"><div class="ch"><h2>反向急查 · 最近 30 分钟</h2></div><div class="log trail">${rows}</div>
    <div class="foot">哨兵看到和作战卡相反的信号、手里又有这个币的仓，就叫管家马上复查一次；下面「今天做了什么」里这几条标了「反向急查」。</div></section>`;
}
// 平时只记次数、不写逐条记录的角色（「今日 N 次」是查了几遍，列表只记出事的那几条）
const QUIET_ROLE = {
  patrol: "巡逻兵每 10 秒把持仓币过一遍 1 分钟波动；没碰到报警线、全平线只记次数，不写逐条记录。碰到全平线直接全平才记一条；碰到报警线是叫管家马上复查，记在管家那边。",
  reconciler: "对账员每轮核对交易所、执行器、账本；三方一致只记次数，对不上或自动处理了才记一条。",
  lifeboat: "保命止损每轮跟着对账员核对一遍；挂好了只记次数，挂 / 撤 / 补挂才记一条。",
};
function quietHead(sel) {
  const why = QUIET_ROLE[sel.role];
  if (!why) return "";
  const last = sel.summary ? `<div><span class="n">${sel.last_run_at ? hms(sel.last_run_at) : ""}</span><span><span class="pill">最近一轮</span> ${esc(sel.summary)}</span></div>` : "";
  return `<div class="note">${esc(why)}今日查了 ${sel.calls || 0} 次。</div>${last ? `<div class="log trail">${last}</div>` : ""}`;
}
// 军师团现在用的提示词（data/prompts_active.json，引擎启动时写；按最长持仓档分短线 / 中线 / 长线写法）
const PROMPT_STYLE = { short: "短线写法", mid: "中线写法", long: "长线写法" };
const PROMPT_PART = [
  ["shared_reporter", "公共说明 · 新闻和情绪分析师"],
  ["shared_analyst", "公共说明 · 行情分析师和多空"],
  ["shared_risk", "公共说明 · 风控三人"],
  ["shared", "公共说明 · 三个经理"],
  ["pick", "总盘会挑币"],
  ["meeting_note", "每场会的说明（花括号里的开会时填）"],
  ["position_note", "手上有这个币的仓时加的说明"],
  ["convert", "会议结论转成作战卡"],
];
// roles：四个原版角色换成的加密版提示词，按角色拆开显示
const ROLE_PART = [
  ["market_analyst", "市场分析师"],
  ["research_manager", "研究经理"],
  ["trader_system", "交易员（系统说明）"],
  ["trader_user", "交易员（每场会的问题）"],
  ["portfolio_manager", "组合经理"],
  ["lessons_line", "带上过往教训的那一句"],
];
function promptLine(pr, holdNow) {
  if (!pr) return `<div class="prompt-line dim small">现在用的提示词：还没有记录（军师团引擎启动后才写）</div>`;
  const later = holdNow && holdNow !== pr.hold ? `<span class="warn">（设置已改成 ${esc(profileSpan(holdNow))}，重启后换）</span>` : "";
  return `<div class="prompt-line small">现在用的提示词：<b>${esc(profileSpan(pr.hold))}档</b>（${esc(PROMPT_STYLE[pr.style] || pr.style)}）<span class="dim"> · ${md(pr.written_at)} ${hm(pr.written_at)} 起用</span>${later} <button class="btn b-sm" data-act="prompts">看原文</button></div>`;
}
async function showPrompts() {
  dlg(`<h3>军师团现在用的提示词</h3><div class="dim">读取中…</div>`);
  let p;
  try { p = await getJson("/api/prompts"); } catch (e) { return dlg(`<h3>军师团现在用的提示词</h3><div class="down">${esc(e.message)}</div>`); }
  const block = (t, v) => `<div class="st-sub">${esc(t)}</div><pre class="json prompt">${esc(typeof v === "string" ? v : JSON.stringify(v, null, 2))}</pre>`;
  const known = new Set(["hold", "style", "brief_fields", "indicator_interval", "analyst_klines", "roles", ...PROMPT_PART.map(([k]) => k)]);
  const roles = p.roles && typeof p.roles === "object" ? p.roles : {};
  const roleKnown = new Set(ROLE_PART.map(([k]) => k));
  const bf = p.brief_fields || {};
  const facts = [
    p.analyst_klines ? `分析师看的 K 线：${p.analyst_klines.join("、")}` : "",
    p.indicator_interval ? `行情分析师算指标用 ${profileSpan(p.indicator_interval)} K 线` : "",
    bf.main ? `大盘简报主要看：${bf.main.join("、")}` : "",
    bf.background && bf.background.length ? `只当背景：${bf.background.join("、")}` : "",
  ].filter(Boolean);
  dlg(`<h3>军师团现在用的提示词 · ${esc(profileSpan(p.hold || ""))}档（${esc(PROMPT_STYLE[p.style] || p.style || "")}）</h3>
    <p class="dim small">只读。按最长持仓档写，换档后重启主程序才换。</p>
    ${facts.length ? `<ul class="small">${facts.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}
    ${PROMPT_PART.filter(([k]) => p[k]).map(([k, t]) => block(t, p[k])).join("")}
    ${ROLE_PART.filter(([k]) => roles[k]).map(([k, t]) => block(t, roles[k])).join("")}
    ${Object.keys(roles).filter((k) => !roleKnown.has(k)).map((k) => block(k, roles[k])).join("")}
    ${Object.keys(p).filter((k) => !known.has(k)).map((k) => block(k, p[k])).join("")}`);
  $("#layer .dlg").classList.add("wide");
}
P.team = () => {
  const d = pageData;
  const cards = teamFlow(d);
  const k = d.desk;
  const sel = d.selected_desk && k
    ? { role: "desk", name: "大盘台", last_beat: k.last_run_at, last_error: k.error, avg_latency_ms: 0 }
    : d.roles.find((r) => r.role === d.selected) || d.market_role;
  window.__recs = d.records;
  const recs = d.records.map((s, i) => `<div class="click" ${clickable("record", i)}><span class="n">${hms(s.ts)}</span><span>${pill(s.outcome)} ${s.tag ? `<span class="pill against">${esc(s.tag)}</span> ` : ""}${esc(s.text)}</span></div>`).join("");
  return `${cards}
    ${sel.role === "keeper" ? againstCard(d.against_checks || []) : ""}
    ${sel.role === "commander" || sel.role === "market" ? `<section class="card mt">${promptLine(d.prompts)}</section>` : ""}
    ${sel.role === "commander" ? meetingsCard(d.meetings || []) : ""}
    ${sel.role === "book_reader" ? bookReaderCard(sel, d.snapshot) : ""}
    ${sel.role === "sentinel" ? sentinelCard(sel) : ""}
    ${sel.role === "desk" ? deskRunsCard(d.desk_runs) : ""}
    ${sel.role === "desk" && k ? `<section class="card mt"><div class="small">准确度：${esc(k.accuracy || "还没算过（jiaoyi market-score，面板每 30 分钟算一次）")}</div></section>` : ""}
    <section class="card mt"><div class="ch"><h2>${esc(sel.name)} · 最近做了什么</h2>
      <span class="r">${sel.role === "market" ? `<a ${clickable("meeting", MARKET)}>看开会过程 ›</a> · ` : ""}${ROLES.some((r) => r.id === sel.role && r.groups && r.groups.length) ? `<a href="#settings/${esc(sel.role)}">它的设置 ›</a> · ` : ""}${sel.last_beat ? "最后心跳 " + hms(sel.last_beat) : "没有心跳"}${sel.avg_latency_ms ? ` · 平均 ${Math.round(sel.avg_latency_ms)} ms` : ""}</span></div>
      ${sel.last_error ? `<div class="note warn">最近出错：${esc(sel.last_error)}</div>` : ""}${quietHead(sel)}
      <div class="log trail scroll tall">${recs || `<div class="dim">${sel.role === "desk" ? "大盘台还没有记录（每一轮的方向、发布、失败都记在这里）。" : sel.role === "market" ? "最近没有总盘会的记录（出作战卡、大盘更新、开会失败都记在这里）。" : sel.role === "book_reader" ? "盘口员不写逐条记录，状态见上面。" : QUIET_ROLE[sel.role] ? "今天还没出过要单独记的事。" : "还没有记录。"}</div>`}</div>
      <div class="foot">最新 200 条（跨天），框里往下滚看更早的 · 点一条看它看到了什么、怎么判断的</div></section>`;
};

/* ---------- 历史 */
const RANGE = { today: "今天", "7d": "7 天", "30d": "30 天", all: "全部" };
const REASON = { stop_loss: "止损", take_profit: "止盈", trailing_stop: "保本 / 移动止损", tightened_stop: "反向卡收紧止损", time_limit: "到时", keeper: "管家平", patrol: "巡逻兵平", lifeboat: "保命触发", manual: "手动", reconciler: "对账平" };
S.hist = "today";
S.rej = false;
S.tests = false;
S.symSort = "pnl";
S.symOpen = new Set();
const SYM_MIN = 10;  // 少于这么多笔不下结论
// 按币种统计：每个币一行，点开按多空、按信号拆；只提示，拉黑 / 加白由人自己定
function symbolCard(d) {
  const key = { pnl: (x) => x.pnl_usdt, n: (x) => x.trades, win: (x) => x.win_rate };
  const list = [...(d.by_symbol || [])].sort((a, b) => key[S.symSort](b) - key[S.symSort](a) || b.pnl_usdt - a.pnl_usdt);
  const kindName = (k) => (k === "test" ? "测试单" : k === "unknown" ? "未知" : SIGNAL[k] || k);
  const r = (v) => (v == null ? "—" : `${v >= 0 ? "+" : "−"}${Math.abs(v).toFixed(2)} R`);
  const feeShare = (x) => (x.fee_share == null ? '<span class="dim" title="毛利本身就是亏的">毛利亏</span>' : `${Math.round(x.fee_share * 100)}%`);
  const cells = (x) => `<td class="r num">${x.trades}<span class="dim small">（${x.wins} 胜 ${x.losses} 负）</span></td><td class="r num">${Math.round(x.win_rate * 100)}%</td><td class="r num ${cls(x.pnl_usdt)}"><b>${signed(x.pnl_usdt, 2)}</b></td><td class="r num ${x.avg_r == null ? "" : cls(x.avg_r)}">${r(x.avg_r)}</td><td class="r num dim">${feeShare(x)}</td><td class="r num nowrap">${dur(x.avg_hold_s * 1000)}</td>`;
  const verdict = (x) => (x.trades < SYM_MIN ? `<span class="dim small" title="少于 ${SYM_MIN} 笔，输赢多半是运气">笔数太少，先别下结论</span>`
    : (x.avg_r ?? x.pnl_usdt) < 0 ? '<span class="pill p-warn">一直在亏</span>' : '<span class="up small">在赚</span>');
  const rows = list.map((x) => {
    const open = S.symOpen.has(x.symbol);
    const main = `<tr class="click ${x.trades < SYM_MIN ? "dimrow" : ""}" ${clickable("symopen", x.symbol)} aria-expanded="${open}"><td class="hi nowrap"><span class="chev-s">${open ? "▾" : "▸"}</span> ${coin(x.symbol)}</td>${cells(x)}<td class="nowrap">${verdict(x)}</td><td class="dim small">${esc(kindName(x.top_kind))} · 多 ${x.longs} / 空 ${x.shorts}</td></tr>`;
    if (!open) return main;
    const sub = x.splits.map((g) => `<tr class="sub"><td class="nowrap">　${g.by === "side" ? `<span class="${(SIDE[g.key] || ["", ""])[1]}">${esc((SIDE[g.key] || [g.key])[0])}</span>` : esc(kindName(g.key))}</td>${cells(g)}<td></td><td class="dim small">${g.by === "side" ? "按多空" : "按信号"}</td></tr>`).join("");
    return main + sub;
  }).join("");
  const sortBtn = (k, t) => `<button class="${S.symSort === k ? "on" : ""}" ${clickable("symsort", k)}>${t}</button>`;
  return `<section class="card mt"><div class="ch"><h2>按币种</h2><span class="r">${list.length} 个币 · 点一行按多空、按信号拆开 · <span class="seg">${sortBtn("pnl", "净盈亏")}${sortBtn("n", "笔数")}${sortBtn("win", "胜率")}</span></span></div>
    ${rows ? `<div class="tw scroll tall"><table class="symtab"><thead><tr><th>币种</th><th class="r">笔数</th><th class="r">胜率</th><th class="r">净盈亏</th><th class="r" title="平均每笔赚几个止损距离：+1 R = 赚了一个止损那么多">平均每笔</th><th class="r" title="手续费 ÷ 毛利">手续费占毛利</th><th class="r">平均拿多久</th><th>看法</th><th>主要信号 · 多空</th></tr></thead><tbody>${rows}</tbody></table></div>
    <div class="foot">只算成交了、有盈亏数的单，时间范围跟上面一样${S.tests ? "，含测试单" : "，不含测试单"}。净盈亏已扣开平手续费，不含资金费和 AI 费。R = 开仓时的止损距离，按 R 比只看金额公平（仓位大小不同）。不满 ${SYM_MIN} 笔不下结论；满 ${SYM_MIN} 笔平均每笔是亏的标「一直在亏」。只提示，拉黑 / 加白你自己定。</div>` : '<div class="dim">没有。</div>'}</section>`;
}
P.history = () => {
  const d = pageData;
  const tiles = [
    [signed(d.net_pnl_usdt, 2) + "U", "交易净盈亏（已扣手续费，未扣 AI 费）", cls(d.net_pnl_usdt)],
    [d.trades + " 笔", "交易"],
    [d.win_rate == null ? "—" : Math.round(d.win_rate * 100) + "%", "胜率"],
    [d.avg_win_loss == null ? "—" : d.avg_win_loss.toFixed(2), "平均盈亏比"],
    [d.max_drawdown_usdt ? "−" + money(d.max_drawdown_usdt, 2) + "U" : "0", "最大回撤", d.max_drawdown_usdt ? "down" : ""],
  ];
  const kinds = d.by_kind.map((k) => `<tr><td class="hi">${esc(SIGNAL[k.kind] || ({ unknown: "未知", test: "测试单" })[k.kind] || k.kind)}</td><td class="r num">${k.trades}</td><td class="r num">${k.win_rate == null ? "—" : Math.round(k.win_rate * 100) + "%"}</td><td class="r num ${cls(k.pnl_usdt)}">${signed(k.pnl_usdt, 2)}</td></tr>`).join("");
  const today = md(data.now);
  const when = (ts) => (md(ts) === today ? hm(ts) : `<span class="dim small">${md(ts)}</span> ${hm(ts)}`);
  const rows = d.rows.map((t) => {
    const [s, sc] = SIDE[t.side] || [t.side, ""];
    const gross = t.pnl_usdt + t.fees_usdt;
    const miss = '<span class="warn" title="平仓价还没按币安成交回填，执行器也没报盈亏和手续费：多半是没取到数，不是真的 0">没取到</span>';
    if (t.unfilled) {
      return `<tr class="click dim" ${clickable("signal", t.signal_id)}><td class="num nowrap">${when(t.opened_at)}</td><td class="num nowrap">${when(t.closed_at)}</td><td class="hi nowrap">${coin(t.symbol)}${catTag(t.category)} <span class="${sc}">${s}</span> ${qty(t.quantity, t.entry_price)}</td>
      <td>${t.test ? '<span class="pill p-warn">测试</span>' : esc(SIGNAL[t.signal_kind] || "—")}</td><td class="r num">${t.leverage}x</td><td class="num" title="计划价，没成交">${price(t.entry_price, t.symbol)}</td><td class="num">—</td><td class="num">—</td><td><span class="pill p-warn" title="币安上没有成交（比如保证金不够、hummingbot 没下单），不算进任何统计">未成交</span></td>
      <td class="num">—</td><td class="r num dim">—</td><td class="r num dim">—</td><td class="r num dim">—</td><td class="r num dim">—</td></tr>`;
    }
    const exit = t.exit_price != null ? price(t.exit_price, t.symbol) : '<span class="dim" title="还没按币安成交回填平仓价">未回填</span>';
    const sltp = `${t.stop_loss_price != null ? price(t.stop_loss_price, t.symbol) : "—"}<div class="dim small">${t.take_profit_price != null ? price(t.take_profit_price, t.symbol) : "—"}</div>`;
    return `<tr class="click" ${clickable("signal", t.signal_id)}><td class="num nowrap">${when(t.opened_at)}</td><td class="num nowrap">${when(t.closed_at)}</td><td class="hi nowrap">${coin(t.symbol)}${catTag(t.category)} <span class="${sc}">${s}</span> ${qty(t.quantity, t.entry_price)}</td>
      <td>${t.test ? '<span class="pill p-warn" title="没有经过哨兵 / 拍板 / 风控员：冒烟测试或手动下的单">测试</span>' : esc(SIGNAL[t.signal_kind] || "—")}</td><td class="r num">${t.leverage}x</td><td class="num">${price(t.entry_price, t.symbol)}</td><td class="num">${exit}</td><td class="num">${sltp}</td><td>${esc(REASON[t.close_reason] || t.close_reason || "—")}</td>
      <td class="num nowrap">${dur(new Date(t.closed_at) - new Date(t.opened_at))}</td><td class="r num ${t.keeper_peak_usdt == null ? "dim" : cls(t.keeper_peak_usdt)}" title="管家采样的收益率按最终账本仓位估算，未扣手续费；采样不连续，可能遗漏最高点">${t.keeper_peak_usdt == null ? "—" : signed(t.keeper_peak_usdt, 2)}</td>${t.pnl_missing ? `<td class="r num">${miss}</td><td class="r num dim">—</td><td class="r num">${miss}</td>` : `<td class="r num ${cls(gross)}">${signed(gross, 2)}</td><td class="r num dim">−${money(t.fees_usdt, 2)}</td><td class="r num ${cls(t.pnl_usdt)}"><b>${signed(t.pnl_usdt, 2)}</b></td>`}</tr>`;
  }).join("");
  window.__recs = d.rejected;
  const rej = S.rej
    ? `<section class="card mt"><div class="ch"><h2>被拒的信号</h2><span class="r">${d.rejected.length} 条 · 拍板放弃和风控员拒绝 · 点一条看快照</span></div>
      <div class="log trail scroll tall">${d.rejected.map((r, i) => `<div class="click" ${clickable("record", i)}><span class="n">${hm(r.ts)}</span><span>${esc(ROLE[r.role] || r.role)} ${pill(r.outcome)} ${esc(r.text)}</span></div>`).join("") || '<div class="dim">没有。</div>'}</div></section>`
    : "";
  return `<div class="toolbar"><span class="seg">${Object.entries(RANGE).map(([k, v]) => `<button class="${S.hist === k ? "on" : ""}" ${clickable("hist", k)}>${v}</button>`).join("")}</span>
      <button class="chip ${S.rej ? "on" : ""}" ${clickable("rej")}>${S.rej ? "☑" : "☐"} 含被拒的信号</button>
      ${d.test_trades ? `<button class="chip ${S.tests ? "on" : ""}" ${clickable("tests")} title="冒烟测试、手动下的单，不是机器人自己的交易">${S.tests ? "☑" : "☐"} 含测试单（${d.test_trades} 笔）</button>` : ""}
      <button class="btn b-sm" ${clickable("csv")} ${d.rows.length ? "" : "disabled"}>导出 CSV</button></div>
    <div class="tiles t5">${tiles.map(([v, l, c]) => `<div class="tile"><small>${l}</small><b class="${c || ""}">${v}</b></div>`).join("")}</div>
    <section class="card mt"><div class="ch"><h2>资金曲线</h2><span class="r">累计净盈亏，每个点是一笔平仓</span></div>
      ${d.curve.length ? '<div class="chart sm" id="hchart-host"></div>' : '<div class="empty"><b>这段时间没有平仓</b></div>'}</section>
    ${symbolCard(d)}
    <section class="card mt"><div class="ch"><h2>交易记录</h2><span class="r">最新在上 · 点一笔看整条链 · 窄屏左右滑</span></div>
      ${d.rows.length ? `<div class="tw scroll tall"><table><thead><tr><th>开仓</th><th>平仓</th><th>币种</th><th>信号</th><th class="r">杠杆</th><th>开仓价</th><th>平仓价</th><th>止损 / 止盈</th><th>怎么平的</th><th>持有</th><th class="r">最高浮盈</th><th class="r">毛利</th><th class="r">手续费</th><th class="r">净利</th></tr></thead><tbody>${rows}</tbody></table></div><div class="foot">最高浮盈按管家采样收益率和最终账本仓位估算，未扣手续费，可能遗漏最高点；缺少可用记录显示 —。止损 / 止盈是下单时挂的价。手续费 = 开 + 平，按币安成交回填后是币安实扣。净利 = 毛利 − 手续费；资金费币安不分到单笔，按天算在「资金」页，AI 费也在那里。</div>` : '<div class="dim">没有。</div>'}</section>
    <section class="card mt"><div class="ch"><h2>按信号类型</h2><span class="r">哪类赚钱、哪类该关</span></div>
      ${kinds ? `<div class="tw"><table><thead><tr><th>信号</th><th class="r">笔数</th><th class="r">胜率</th><th class="r">净盈亏</th></tr></thead><tbody>${kinds}</tbody></table></div>` : '<div class="dim">没有。</div>'}</section>${rej}`;
};

function exportCsv() {
  const d = pageData;
  if (!d || !d.rows) return;
  const head = ["平仓时间", "开仓时间", "币种", "类型", "方向", "数量", "名义金额U", "杠杆", "开仓价", "平仓价", "止损价", "止盈价", "信号", "平仓原因", "最高浮盈采样估算U（未扣费）", "毛利U", "手续费U", "净盈亏U", "position_id"];
  // 文字字段以 = + - @ 开头时加 ' 前缀，免得表格软件当公式执行（数字照常）
  const q = (v) => { let t = String(v ?? ""); if (typeof v === "string" && /^[=+\-@]/.test(t)) t = "'" + t; return `"${t.replace(/"/g, '""')}"`; };
  const lines = d.rows.map((t) => [t.closed_at, t.opened_at, t.symbol, CAT[t.category] || t.category, t.side, t.quantity, t.entry_price ? +(t.quantity * t.entry_price).toFixed(2) : "", t.leverage, t.entry_price, t.exit_price, t.stop_loss_price, t.take_profit_price, t.test ? "测试" : t.signal_kind, t.unfilled ? "未成交" : t.close_reason, t.keeper_peak_usdt ?? "", t.pnl_missing || t.unfilled ? "" : +(t.pnl_usdt + t.fees_usdt).toFixed(4), t.pnl_missing || t.unfilled ? "" : t.fees_usdt, t.pnl_missing || t.unfilled ? "" : t.pnl_usdt, t.position_id].map(q).join(","));
  const blob = new Blob(["﻿" + [head.map(q).join(","), ...lines].join("\n")], { type: "text/csv" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `交易记录-${d.range}.csv`;
  a.click();
  URL.revokeObjectURL(a.href);
}

/* ---------- 资金 */
// 净赚多少：交易盈亏 + 手续费 + 资金费 − AI 费，按 今天 / 近 7 天 / 累计
function pnlCard(d) {
  if (!d.pnl || !d.pnl.length) return "";
  const cell = (v) => (v == null ? '<td class="r num dim">—</td>' : `<td class="r num ${cls(v)}">${signed(v, 2)}</td>`);
  const rows = [
    ["交易盈亏", "平仓赚亏，手续费前", (p) => p.trading_usdt],
    ["手续费", "币安收的，开平各一次", (p) => p.fees_usdt],
    ["资金费", "持仓过结算点时多空互付", (p) => p.funding_usdt],
    ["AI 费", "拍板人、军师团等调用大模型，按 1 美元 ≈ 1 U", (p) => -p.ai_usd],
  ].map(([n, h, f]) => `<tr><td class="hi">${n}<div class="dim small">${h}</div></td>${d.pnl.map((p) => cell(f(p))).join("")}</tr>`).join("");
  const net = `<tr class="total"><td class="hi">净盈亏</td>${d.pnl.map((p) => `<td class="r num ${cls(p.net_usdt)}"><b>${signed(p.net_usdt, 2)}</b></td>`).join("")}</tr>`;
  const src = d.pnl_source === "exchange"
    ? `交易盈亏、手续费、资金费来自币安收入流水（主程序每 5 分钟拉一次）。累计从 ${esc(d.pnl_since)} 算起。`
    : `主程序还没开始记币安收入流水，交易盈亏和手续费先按账本里平掉的单算，资金费暂时看不到。${d.pnl_since ? `累计从 ${esc(d.pnl_since)} 算起。` : ""}`;
  return `<section class="card mt"><div class="ch"><h2>扣掉所有成本，净赚多少</h2><span class="r">单位 U · 负数是亏或花出去的</span></div>
    <div class="tw"><table><thead><tr><th></th>${d.pnl.map((p) => `<th class="r">${esc(p.label)}</th>`).join("")}</tr></thead><tbody>${rows}${net}</tbody></table></div>
    <div class="foot">净盈亏 = 交易盈亏 + 手续费 + 资金费 − AI 费。不含浮盈浮亏（还没平的仓）。${src}</div></section>`;
}

// AI 费用明细：每一次调大模型花了多少（OpenRouter 报的 usage.cost），按类别小计
const AI_RANGE = { today: "今天", "7d": "近 7 天", all: "累计" };
const AI_RESULT = { ok: ["成功", "p-long"], failed: ["失败", "p-short"], timeout: ["超时", "p-warn"] };
function aiCard(a) {
  if (!a) return "";
  const rng = `<span class="seg">${Object.entries(AI_RANGE).map(([k, v]) => `<button class="${a.range === k ? "on" : ""}" ${clickable("ai", k)}>${v}</button>`).join("")}</span>`;
  const groups = a.groups.map((g) => `<div class="tile"><small>${esc(g.name)} · ${g.calls} 次</small><b>$${g.cost_usd.toFixed(3)}</b></div>`).join("");
  const rows = a.rows.map((c) => {
    const [t, k] = AI_RESULT[c.result];
    return `<tr><td class="num">${a.range === "today" ? hm(c.ts) : `${md(c.ts)} ${hm(c.ts)}`}</td><td class="hi">${esc(c.who)}</td><td>${c.symbol ? coin(c.symbol) : "—"}</td>
      <td class="r num" ${c.estimated ? 'title="没拿到实际费用，按 token 估算"' : ""}>$${c.cost_usd.toFixed(4)}${c.estimated ? '<span class="dim small">（估）</span>' : ""}</td><td class="r num ${c.tokens == null ? "dim" : ""}" ${c.tokens == null ? 'title="这条记录没写 tokens"' : ""}>${c.tokens == null ? "没记" : c.tokens.toLocaleString()}</td>
      <td><span class="pill ${k}">${t}</span></td><td class="txt dim small">${esc(c.detail)}</td></tr>`;
  }).join("");
  const cache = a.cache_hit_pct == null ? "缓存命中：还没有记录（旧记录没带缓存字段）。" : `缓存命中 ${a.cache_hit_pct}%（${a.cached_tokens.toLocaleString()} / ${a.input_tokens.toLocaleString()} 输入 tokens）。`;
  const more = a.row_count > a.rows.length ? `只列最近 ${a.rows.length} 次，共 ${a.row_count} 次；小计按全部算。` : "";
  return `<section class="card mt"><div class="ch"><h2>AI 费用详情</h2><span class="r">${rng}</span></div>
    <div class="tiles t${a.groups.length + 1}"><div class="tile"><small>合计 · ${a.row_count} 次</small><b>$${a.total_usd.toFixed(3)}</b></div>${groups}</div>
    ${a.rows.length ? `<div class="tw mt"><table><thead><tr><th>时间</th><th>谁</th><th>币</th><th class="r">花费</th><th class="r">tokens</th><th>结果</th><th>说明</th></tr></thead><tbody>${rows}</tbody></table></div>` : `<div class="dim mt">${AI_RANGE[a.range]}还没有 AI 调用。</div>`}
    <div class="foot">花费是 OpenRouter 每次调用报的实际费用（usage.cost），标「估」的是失败时拿不到实际费用、按 token 估的；按 1 美元 ≈ 1 U。${cache}${a.since ? `从 ${esc(a.since)} 算起。` : ""}${more}</div></section>`;
}

P.funds = () => {
  const d = pageData;
  const k = data.kpis;
  const flow = [
    ["日初权益（曼谷 00:00）", d.day_start_equity_usdt, ""],
    ["＋ 今天平仓的净盈亏", d.realized_today_usdt, "signed"],
    ["＋ 持仓浮盈浮亏", d.unrealized_usdt, "signed"],
    ["＋ 其他（资金费、转账、口径差）", d.other_today_usdt, "signed"],
    ["＝ 现在", d.equity_usdt, "total"],
  ].map(([l, v, t]) => `<div class="fr ${t === "total" ? "total" : ""}"><span>${l}</span><b class="num ${t === "signed" ? cls(v) : ""}">${t === "signed" ? signed(v, 2) : money(v, 2)} U</b></div>`).join("");
  const costs = d.costs.map((c) => `<tr><td class="hi">${esc(c.name)}</td><td class="r num">$${c.today_usd.toFixed(3)}</td><td class="r num">$${c.week_usd.toFixed(3)}</td></tr>`).join("");
  const tiles = [
    [money(d.equity_usdt, 2) + " U", "总资产" + (d.equity_source === "ledger" ? "（账本）" : "")],
    [money(d.wallet_usdt, 2) + " U", "钱包余额"],
    [money(d.available_usdt, 2) + " U", "可用保证金"],
    [signed(d.unrealized_usdt, 2) + " U", "浮盈浮亏", cls(d.unrealized_usdt)],
    [d.halt_line_usdt == null ? "—" : money(d.halt_line_usdt, 0) + " U", d.halted ? "熔断线（已触发）" : "熔断线", d.halted ? "down" : ""],
  ];
  return `<div class="tiles t5">${tiles.map(([v, l, c]) => `<div class="tile"><small>${l}</small><b class="${c || ""}">${v}</b></div>`).join("")}</div>
    ${pnlCard(d)}
    <div class="grid g2 mt">
      <section class="card"><div class="ch"><h2>今天的钱是怎么变的</h2><span class="r">曼谷 00:00 起算</span></div><div class="form">${flow}</div>
        <div class="foot">${d.equity_source === "hummingbot" ? `账户数字都从币安${data.mode === "live" ? "实盘" : " Demo"}账户实时拉（经 hummingbot-api），不能手填。` : "hummingbot-api 没连上，总资产暂用账本里最后一次记录。"}今天手续费 ${money(d.fees_today_usdt, 2)} U。今日盈亏 ${signed(k.day_pnl_usdt, 2)} U（${k.day_pnl_pct == null ? "—" : signed(k.day_pnl_pct, 2) + "%"}），跌到熔断线就停开新仓到第二天。</div></section>
      <section class="card"><div class="ch"><h2>运行成本</h2><span class="r">AI 调用花费，不在币安账户里</span></div>
        <div class="tw"><table><thead><tr><th>角色</th><th class="r">今天</th><th class="r">近 7 天</th></tr></thead><tbody>${costs}
        <tr class="total"><td class="hi">合计</td><td class="r num hi">$${d.cost_today_usd.toFixed(3)}</td><td class="r num hi">$${d.cost_week_usd.toFixed(3)}</td></tr></tbody></table></div>
        <div class="foot">机器人只读账户，不能转账。转入转出请在币安上看。</div></section>
    </div>
    ${aiCard(d.ai)}`;
};

/* ---------- 选币 */
// 每类币的开关和进池上限：选币页设的 > slow.toml [screener.categories] > 默认（开、每类默认上限）
// 界面上不出现文件名：slow.toml 写的也算「默认」，选币页改过的显示「你改过（默认 X）· 恢复默认」
const capText = (n) => (n == null ? "不限" : `${n} 个`);  // null = 每类默认上限关了（写 0）
function categoryCard(d) {
  if (!d.categories || !d.categories.length) return "";
  const can = !d.overrides_error;
  const b = (arg, text, on) => `<button class="${on ? "on" : ""}" ${can && !on ? clickable("cat", arg) : "disabled"}>${text}</button>`;
  const rows = d.categories.map((c) => {
    const k = c.category;
    const from = (src, arg, base) => (src === "pool" ? `你改过（默认 ${base}）${can ? ` · <button class="lnk small" ${clickable("cat", `${arg}:default:${base}`)}>恢复默认</button>` : ""}` : "默认");
    return `<tr class="${c.enabled ? "" : "dimrow"}"><td class="hi">${esc(CAT[k] || k)}</td>
      <td><span class="seg">${b(`set_category:${k}:true`, "开", c.enabled)}${b(`set_category:${k}:false`, "关", !c.enabled)}</span><div class="dim small">${from(c.enabled_from, `set_category:${k}`, c.enabled_base ? "开" : "关")}</div></td>
      <td class="r num">${capText(c.cap)}<div class="dim small">${from(c.cap_from, `set_category_cap:${k}`, capText(c.cap_base))}</div></td>
      <td class="r num">${c.in_pool} / ${c.total}</td>
      <td class="r nowrap">${can ? `<button class="btn b-sm" ${clickable("cat", `set_category_cap:${k}:${c.cap ?? 30}`)}>改上限</button>` : ""}</td></tr>`;
  }).join("");
  const off = d.categories.filter((c) => !c.enabled).length;
  return `<section class="card mt"><div class="ch"><h2>按类别选币</h2><span class="r">${off ? `关了 ${off} 类` : "六类都开着"} · 每类默认最多 ${capText(d.default_category_cap || null)}</span></div>
    ${d.slow_error ? `<div class="note warn">类别配置读不出，下面的“默认”先按出厂值显示。${esc(d.slow_error)}</div>` : ""}
    <div class="tw"><table><thead><tr><th>类别</th><th>开关</th><th class="r">最多进池</th><th class="r" title="上次选币：这一类进池几个 / 这一类一共看了几个">进池 / 候选</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>
    <div class="foot">关掉的类别不再靠打分进池，你固定、手动加的币仍进池。开关和上限要面板密码，改完主程序马上重新选币（“进池 / 候选”是上次选币的结果，等这次选完才变；候选 = 这一类上次一共看了几个币）。“恢复默认”去掉你在这里改过的那一项，回到括号里的默认值。</div></section>`;
}

P.pool = () => {
  const d = pageData;
  if (!d.entries.length && !d.pinned.length && !d.manual.length && !d.blacklist.length) {
    return `<div class="card"><div class="empty"><b>还没有选币结果</b>主循环每次选完币会写一份候选池，这里就能看到。</div></div>${categoryCard(d)}`;
  }
  // 标签和按钮按 pool_overrides.json（你的设置，立刻生效）；候选池文件要等下一次选币才更新
  const has = (list, sym) => list.includes(sym);
  const flag = (sym) => [has(d.pinned, sym) && '<span class="pill p-accent">固定</span>', has(d.manual, sym) && '<span class="pill p-accent">手动加的</span>', has(d.blacklist, sym) && '<span class="pill p-short">拉黑</span>', d.blocked[sym] && '<span class="pill p-short">禁开新仓</span>'].filter(Boolean).join(" ");
  const canEdit = !d.overrides_error;
  const btn = (kind, sym, text, danger) => `<button class="btn b-sm ${danger ? "b-danger" : ""}" ${clickable("pool", kind + ":" + sym)}>${text}</button>`;
  const ops = (sym) => (canEdit ? [
    has(d.pinned, sym) ? btn("unpin_symbol", sym, "取消固定") : has(d.blacklist, sym) ? "" : btn("pin_symbol", sym, "固定"),
    has(d.blacklist, sym) ? btn("unblacklist_symbol", sym, "取消拉黑") : btn("blacklist_symbol", sym, "拉黑", true),
    has(d.manual, sym) ? btn("remove_symbol", sym, "移除") : "",
  ].filter(Boolean).join(" ") : "");
  const listed = new Set(d.entries.map((e) => e.symbol));
  // 你设置过、但候选池文件里还没有的币（刚加的、早就拉黑的）：单列出来，也能撤销
  const extra = [...new Set([...d.pinned, ...d.manual, ...d.blacklist])].filter((s) => !listed.has(s)).sort();
  const extraRows = extra.map((sym) => `<tr class="dimrow"><td class="hi">${coin(sym)} ${flag(sym)}</td><td colspan="2" class="dim">${has(d.blacklist, sym) ? "你拉黑的" : "等下一次选币"}</td><td class="r">${ops(sym)}</td></tr>`).join("");
  const rows = d.entries.map((e) => `<tr class="${e.in_pool ? "" : "dimrow"}">
      <td class="hi">${coin(e.symbol)} ${flag(e.symbol)}</td><td>${esc(CAT[e.category] || e.category)}</td><td class="r num">${e.score.toFixed(2)}</td>
      <td>${e.in_pool ? (e.picked_by_card ? '<span class="pill p-long">军师选中</span>' : '<span class="pill">在池里</span>') : '<span class="pill">没进池</span>'}</td>
      <td class="txt">${e.in_pool ? esc(e.reasons.join("；")) + (e.pick_reason ? `<div class="dim small">军师：${esc(e.pick_reason)}</div>` : "") : esc(e.filters.failed_reasons.join("；") || (e.blacklisted ? "你拉黑的" : ""))}</td>
      <td class="r num dim">${money(e.filters.quote_volume_24h_usdt / 1e6, 0)}M</td><td class="r num dim">${e.filters.spread_bps.toFixed(2)}</td><td class="r nowrap">${ops(e.symbol)}</td></tr>`).join("");
  const addBox = canEdit
    ? `<div class="toolbar"><input id="pool-add" class="inp" placeholder="币种，例如 SUI 或 SUIUSDT" autocomplete="off" spellcheck="false" style="max-width:240px"><button class="btn b-sm" ${clickable("pooladd")}>手动加币</button>
        <span class="dim small">固定、手动加币、取消拉黑要面板密码；拉黑、取消固定、移除只要确认。改完主程序马上重新选币。</span></div>`
    : '<div class="note warn">data/pool_overrides.json 读不出来：主程序沿用上一份设置。先在 Mac 上修好这个文件再在这里改。</div>';
  return `<div class="tiles t4"><div class="tile"><small>候选池</small><b>${d.in_pool}</b></div><div class="tile"><small>军师选中</small><b>${d.picked}</b></div>
      <div class="tile"><small>没进池</small><b>${d.rejected}</b></div><div class="tile"><small>上次选币</small><b>${d.ts ? hm(d.ts) : "—"}</b></div></div>
    ${categoryCard(d)}
    ${venueExcluded(d.venue_excluded)}
    <section class="card mt"><div class="ch"><h2>候选池</h2><span class="r">代码每小时筛一次，军师只能从池里挑；固定、拉黑、手动加入优先</span></div>
      ${addBox}
      <div class="tw"><table><thead><tr><th>币种</th><th>分组</th><th class="r">分数</th><th>状态</th><th>理由</th><th class="r">24h 成交</th><th class="r">价差 bp</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>
      ${extraRows ? `<div class="ch mt"><h2>你设置过、还不在候选池里的币</h2></div><div class="tw"><table><tbody>${extraRows}</tbody></table></div>` : ""}
      <div class="foot">固定：每次都进池（仍要过硬条件）；拉黑：永不进池、立刻不开新仓；手动加的：加进候选（仍要过硬条件），“移除”就撤掉。</div></section>
    <div class="mt">${blockedSection(d.blocked)}</div>`;
};

function venueExcluded(m) {
  const items = Object.entries(m || {});
  if (!items.length) return "";
  return `<div class="note warn mt">交易所上不能交易，已从候选池剔除：${items.map(([s, st]) => `${esc(s.replace(/USDT$/, ""))}（${esc(st)}）`).join("、")}</div>`;
}

function addSymbol() {
  const el = $("#pool-add");
  let sym = (el && el.value || "").trim().toUpperCase().replace(/[^A-Z0-9]/g, "");
  if (!sym) return el && el.focus();
  if (!sym.endsWith("USDT")) sym += "USDT";
  el.value = "";
  askCommand("add_symbol", { symbol: sym }, sym.replace(/USDT$/, ""));
}
document.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && e.target && e.target.id === "pool-add") { e.preventDefault(); addSymbol(); }
});
// 设置页搜索（全部设置、档位参数各一个框）：边打边筛（中文输入法拼字时先不筛，选定字再筛）
const SEARCH_BOX = { "st-q": "stQ", "tier-q": "tierQ" };
document.addEventListener("input", (e) => {
  if (!e.target || !(e.target.id in SEARCH_BOX) || e.isComposing) return;
  S[SEARCH_BOX[e.target.id]] = e.target.value;
  render();
});
document.addEventListener("compositionstart", (e) => { if (e.target && e.target.id in SEARCH_BOX) S.stComposing = true; });
document.addEventListener("compositionend", (e) => {
  if (!e.target || !(e.target.id in SEARCH_BOX)) return;
  S.stComposing = false;
  S[SEARCH_BOX[e.target.id]] = e.target.value;
  render();
});

/* ---------- 设置 */
const KEY_NOTE = { BINANCE_API_KEY: "币安实盘", BINANCE_API_SECRET: "币安实盘", BINANCE_TESTNET_API_KEY: "币安 Demo", BINANCE_TESTNET_API_SECRET: "币安 Demo", JEV_API_KEY: "拍板人", DEEPSEEK_API_KEY: "军师团", TELEGRAM_BOT_TOKEN: "告警", TELEGRAM_CHAT_ID: "告警", FRED_API_KEY: "宏观数据（可选）", HUMMINGBOT_API_PASSWORD: "hummingbot-api" };
// 临时会触发事件、花到日上限 80% 怎么办（slow.toml [schedule]）
const TRIGGER = { key_level_break: "突破关键价位", move_since_card: "离上次开会价格走了好几个波动幅度", new_head_symbol: "头部新币进池", predictor_against_card: "预测员连续看反作战卡", signal_against_card: "哨兵反复看到和卡相反的信号", news_high_impact: "重大消息", stale_card_on_entry: "开仓时卡太旧" };
const CHOICE = { ...TRIGGER, outside: "价格在关键位外面就算", cross: "价格穿过关键位那一下才算", slow: "放慢一档", stop: "不放慢", card_agree: "只有 100% 看涨算看涨，其余算看跌", symmetric: "做多要 100% 看涨、做空要 100% 看跌", sync: "每次复查当场读（复查要等它）", lifeboat_first: "保命单 → 止损 → 止盈（原行为）", stop_first: "止损 → 止盈 → 保命单", hb_first: "先停 HB 再直连平仓（原行为）", direct: "先直连只减仓平仓，再只停不平 HB", background: "后台按仓读，复查只用新鲜结果", one: "一道题（只问做不做）", six: "6 道题（旧问法，回放对照）", on_demand: "按需（作战卡和持仓的币）", all_symbols: "候选池全算", fixed: "固定", adaptive: "按排队自适应", mini: "小模型（最快）", small: "中模型", base: "大模型（最准）", demo: "Demo Trading", legacy_testnet: "旧测试网", typesafe: "Jev 官方直连", openrouter: "OpenRouter", "Asia/Bangkok": "曼谷时间", "Asia/Shanghai": "北京时间", UTC: "世界标准时间（UTC）", "24h": "24 小时", "12h": "12 小时", "6h": "6 小时", "8h": "8 小时", "4h": "4 小时", "2h": "2 小时", "1h": "1 小时", "60m": "1 小时", "30m": "30 分钟", "15m": "15 分钟" };
const MODEL = { base: "最大（更准，每币约 3.5 秒）", small: "标准（每币约 1 秒）", mini: "最快（最粗）" };
// [meeting] 里同一个词（legacy / neutral …）在不同键下意思不同，按键查
const MEETING_CHOICE = {
  lessons_scoring: { raw: "原始涨跌（会后一个持仓期，山寨币另比 BTC）", excess: "超额（发布后 4 小时，扣掉同期币池）" },
  debate_first: { bull: "多头先说（现状）", bear: "空头先说", alternate: "按会议交替" },
  funding_label: { legacy: "写“多头拥挤”（现状）", neutral: "只写谁付谁和相对基准" },
  bear_bull_symmetry: { legacy: "现状（多空写法不对称）", mirror: "多空逐句对称" },
  crowd_ratio_mode: { raw: "只给原值（现状）", relative: "再给相对自身 30 天中位数的偏离" },
  snapshot_interval: { "5m": "5 分钟", "15m": "15 分钟", "30m": "30 分钟", "1h": "1 小时", "4h": "4 小时" },
  snapshot_label: { legacy: "叫“权威”（现状）", neutral: "写明最后收盘时间和距现在几分钟" },
  stale_on_arrival_action: { watch: "只盯不开", no_trade: "改成观望" },
  input_time_labels: { legacy: "不标（现状）", explicit: "写明已收盘小时 / 上次结算时间" },
  keeper_snapshot: { direction: "方向 + 名次（原行为）", rank: "只带名次（多空对称）", off: "不带预测员" },
};
const choiceLabel = (k, v) => (MEETING_CHOICE[k] && MEETING_CHOICE[k][v]) || (k === "atr_period_min" ? `${v} 分钟` : k === "model_size" ? MODEL[v] || String(v) : k === "mid_size_max" ? { S: "小", M: "中", L: "大" }[v] || String(v) : CHOICE[v] || String(v));
// 描述太长的项给个短名字（完整说明仍在契约里）
const SHORT = { mode: "运行方式", size_policy: "模型尺寸怎么选" };
// 设置项的大白话标题；原来的专业说明和英文键名降为灰色小字
const PLAIN = {
  notify_open_filled: "开仓成交",
  notify_close_done: "平仓完成",
  notify_lifeboat_hit: "保命止损被打到",
  notify_lifeboat_missing: "保命止损缺失 / 挂不上",
  notify_order_failed: "开仓 / 平仓失败",
  notify_reconcile: "对账不一致",
  notify_news_block: "新闻禁止开仓",
  notify_model_credit: "模型额度不足",
  credit_low_floor_usd: "模型额度最低几美元就提醒",
  credit_resend_hours: "额度提醒多久重复一次（小时）",
  card_score_every_min: "方向记分多久补算一次（分钟）",
  card_score_window_days: "方向记分汇总最近几天",
  notify_meeting_paused: "军师团暂停开会",
  notify_spend_alert: "AI 花费过线提醒（日上限不限时）",
  notify_control: "面板操作",
  notify_live_started: "实盘已启动",
  notify_system: "系统异常",
  "screener.min_quote_volume_usdt": "24 小时成交额至少多少 U 才进池",
  "screener.max_spread_bps": "买卖价差超过万分之几就不进池",
  "screener.min_depth_usdt": "盘口深度至少多少 U",
  "screener.min_listed_days": "上线至少几天",
  "screener.min_range_24h_pct": "24 小时波动至少百分之几",
  "screener.max_range_24h_pct": "24 小时波动最多百分之几",
  "screener.default_category_cap": "每一类币默认最多进池几个（0 = 不限）",
  "screener.vol_full_multiple": "每小时折算的波动是来回手续费的几倍算满分",
  "screener.surge_full_multiple": "1 小时成交额是平时的几倍算满分",
  "screener.funding_full": "资金费率到多少算满分",
  "screener.oi_full_pct": "持仓量 1 小时变百分之几算满分",
  "screener.liq_full_usdt": "1 小时强平多少 U 算满分",
  "screener.us_events": "美股类用开盘时段和财报日历",
  "screener.us_open_bonus": "美股开盘期间，美股类的币总分加多少",
  "screener.major_top_n": "主流币按市值前几名算",
  "screener.major_include": "手动算主流的币",
  "screener.major_exclude": "手动排除出主流的币",
  "screener.allow_tradfi": "美股、金属这类传统资产永续也进池",
  "screener.exclude_untradeable": "剔掉交易所上不在交易的币（如结算中）",
  "predictor.interval": "喂给模型几分钟的 K 线",
  against_signal_on: "看到和作战卡反向的信号时叫管家急查、反复出就开临时会",
  against_dedup_s: "同一个币同一种反向信号几秒内只算 1 次",
  against_grace_s: "开仓后几秒内的反向信号不算（刚进场的噪音）",
  against_recheck_cooldown_s: "同一笔仓几秒内最多因反向信号急查 1 次",
  against_strong_breakout_vol_ratio: "成交量是平时几倍才算强反向信号（管家判 1 次就平）",
  against_loss_stop_frac: "已亏到止损距离的几成时，反向信号也按强的算",
  against_meeting_count: "反向信号出几次就申请这个币的临时会",
  against_meeting_window_s: "上面「几次」在几秒内算",
  against_meeting_gap_s: "同一个币因反向信号最多几秒申请 1 次临时会",
  margin_safety_pct: "可用保证金留出百分之几不用（不够就缩单）",
  book_depth_levels: "看吃单那一侧前几档挂单（1–20）",
  max_notional_book_frac: "单笔最多等于这几档总额的几倍（超了缩单，0 = 不看）",
  card_caps_binding: "作战卡上的仓位、杠杆范围当硬约束",
  leverage_clamp_to_bracket: "杠杆按币安这个币的上限封顶（关 = 超过上限这笔开不了，原行为）",
  max_slippage_stop_frac: "滑点最多是止损距离的几成",
  max_spread_stop_frac: "价差最多是止损距离的几成",
  min_stop_fee_multiple: "止损至少是来回手续费的几倍（0 = 不查）",
  stop_floor_widen: "止损太窄时自动拉宽",
  funding_against_max_bp: "开仓方向要付的资金费超过万分之几就不开（0 = 不查）",
  us_hours_gate: "美股类合约只在美股开盘时段开新仓",
  us_open_delay_min: "美股开盘后几分钟才开新仓",
  us_close_lead_min: "美股收盘前几分钟停开新仓",
  atr_long_period_min: "再参考长周期的波动（K 线分钟数）",
  stop_level_snap: "止损挪到作战卡关键价位外侧",
  session_factor: "止损按曼谷时段放宽或收窄",
  session_factor_weekday: "工作日每个钟点的止损系数",
  session_factor_weekend: "周末每个钟点的止损系数",
  card_range_clamp: "作战卡的止损止盈范围超出档位时取重叠部分（关 = 整个币不做）",
  limit_take_profit: "模拟盘用限价单止盈",
  limit_take_profit_live: "测试网 / 实盘也用限价单止盈",
  tp_limit_fallback_s: "价格穿过止盈价几秒还没平就改市价平",
  tp_limit_fallback_through_bps: "价格越过止盈价万分之几才算「没成交」",
  patrol_alert_stop_mult: "报警线是止损距离的几倍",
  patrol_shock_stop_mult: "全平线是止损距离的几倍",
  "predictor.pred_len": "往后预测几根 K 线",
  "predictor.lookback": "回看几根 K 线",
  "predictor.paths": "模拟几条路径（模型平均成一条）",
  "predictor.temperature": "采样温度",
  "predictor.rank_band": "排名前 / 后多少算看好 / 看坏",
  "predictor.rank_min_peers": "至少几个币一起预测才排名",
  "predictor.against_card_meeting": "连续几次看反作战卡就申请临时会",
  "predictor.keeper_snapshot": "管家复查时怎么带预测员",
  "tiers.enabled": "分档交易（头部做、中部挑着做、尾部只看）",
  "tiers.head": "头部几个币（开放交易）",
  "tiers.mid": "中部几个币",
  "tiers.mid_daily_pick": "中部每天挑几个来做",
  "tiers.mid_size_max": "中部的币仓位最大到哪一档",
  "tiers.always_candidates": "固定进总盘会的币",
  "tiers.always_candidates_meet": "固定进总盘会的币每轮直接开会",
  "meeting.lessons_scoring": "教训怎么给作战卡打分",
  "meeting.debate_first": "多空辩论谁先说",
  "meeting.funding_label": "资金费率怎么写进会议材料",
  "meeting.bear_bull_symmetry": "多空提示词是否逐句对称",
  "meeting.crowd_ratio_mode": "多空人数比怎么给",
  "meeting.bias_fix_ab": "偏空整改对照：多大比例的会用新写法（0 = 都用）",
  "meeting.snapshot_interval": "行情快照和指标用几分钟 K 线",
  "meeting.snapshot_label": "快照怎么标时间",
  "meeting.live_price_line": "会议说明里加实时价",
  "meeting.stale_on_arrival": "转卡后现价已越过失效价就作废",
  "meeting.stale_on_arrival_action": "作废后怎么处理",
  "meeting.input_time_labels": "表格里的字段写明是哪一段时间",
  "meeting.market_table_rolling": "总盘会加一行近 60 分钟涨跌家数",
  "meeting.latest_market_regime": "每个币的会用开会那一刻最新的大盘判断",
  "meeting.event_brief_refresh": "临时会开会前先刷新大盘简报",
  "screener.weight_volatility": "波动的权重",
  "screener.weight_volume_surge": "量能突增的权重",
  "screener.weight_funding": "资金费率的权重",
  "screener.weight_oi": "持仓量变化的权重",
  "screener.weight_liquidations": "强平的权重",
  risk_per_trade_pct: "每笔最多亏本金的百分之几",
  max_leverage: "最多开几倍杠杆",
  max_total_exposure_x: "所有仓位合计最多（含杠杆，不是杠杆倍数）",
  daily_loss_halt_pct: "今天亏到本金的百分之几就停止开新仓",
  max_notional_per_trade_x: "单笔仓位最大（含杠杆，不是杠杆倍数）",
  max_notional_per_trade_usdt: "单笔仓位绝对上限（含杠杆）USDT（可不填）",
  max_open_positions: "同时最多持几个币",
  lifeboat_stop_multiplier: "交易所兜底止损比正常止损远几倍",
  max_spread_bps: "买卖价差超过万分之几就不开仓",
  max_slippage_bps: "成交价比预期差太多就不下单（万分之几）",
  taker_fee_bps: "交易所手续费（万分之几，单边）",
  min_tp_fee_multiple: "止盈至少能赚回几倍手续费，不够就不做",
  day_reset_tz: "“今天”按哪个时区算",
  mode: "运行方式",
  profile: "开会间隔（旧写法）",
  meeting_interval: "开会间隔（军师团多久开一轮会，选币同节奏）",
  hold: "单笔最长持仓",
  predictor_enabled: "用不用 Kronos 预测价格",
  signal_dedup_s: "同一个信号多少秒内只处理一次",
  jev_timeout_ms: "拍板人多少毫秒不回答就放弃这笔",
  jev_max_concurrent: "拍板人同时最多判几个",
  keeper_interval_s: "管家每隔几秒看一次持仓",
  reconcile_interval_s: "每隔几秒和交易所对一次账",
  reconcile_opening_grace_s: "新仓建账后几秒内，托管还没到位、运行列表暂时没返回执行器只记「信息」不记「出错」（只改标注；0 = 一律记出错）",
  reconcile_closing_grace_s: "平仓进行中几秒内，平仓结果待确认、托管在退出、运行列表没返回执行器只记「信息」不记「出错」（只改标注；0 = 一律记出错）",
  protection_stale_s: "交易所保护单的观测超过几秒就标“数据过期”（0 = 不标）",
  slow_loop_interval_min: "军师团每隔几分钟开一次会（档位 = 开会间隔）",
  screener_interval_min: "选币每隔几分钟跑一次（默认跟开会间隔档位走，最少 15 分钟）",
  pool_size: "每次选多少个币进候选池",
  card_pick_max: "军师团每次最多挑几个币",
  reconciler_close_unknown_positions: "交易所上有不认识的仓位时直接平掉",
  protection_readback_grace_s: "条件单回执后查不到，最多等几秒再算没挂上（0 = 不等）",
  position_mode_cache_s: "持仓模式查一次后几秒内不再查（0 = 每次都查）",
  protection_prearm_grace_s: "新仓还没完整托管时，同步失败最多忍几秒再平仓（0 = 立即平）",
  protection_unknown_submit_grace_s: "条件单提交结果未知时，按原单号只查不重发最多几秒（0 = 立即平）",
  protection_stop_working_type: "止损（含移动止损）按什么价触发：CONTRACT_PRICE 最新成交价 / MARK_PRICE 标记价",
  protection_take_profit_working_type: "止盈按什么价触发：CONTRACT_PRICE 最新成交价 / MARK_PRICE 标记价",
  protection_leg_order: "新仓三条腿先挂哪条：lifeboat_first 保命单在前（原行为）/ stop_first 止损在前，止损确认后保命单挂失败在未托管宽限内重试、不立即平仓",
  hosted_close_mode: "托管仓平仓顺序：hb_first 先停 HB、等它结束再直连平仓（原行为）/ direct 先按持久化单号下只减仓市价单再只停不平 HB，止损挂到确认零仓；状态不明照旧 hb_first",
  protection_trailing_book_mark_max_dev_bps: "移动止损：买一高过（空仓：卖一低过）标记价几基点当插针，不更新峰值（0 = 不比对，不建议）",
  protection_trailing_stop_mark_buffer_bps: "移动止损：新止损至少离标记价几基点（在正确一侧）才挂，否则这轮不换",
  protection_trailing_min_step_bps: "移动止损：至少收紧几基点才撤旧挂新（0 = 一变就换）",
  protection_trailing_book_critical_after_s: "移动止损：已激活却连续几秒没有有效盘口，告警升为紧急（0 = 不升级）",
  rule_gate_without_jev: "没有拍板人时用规则代替",
  gate_record_only: "拍板只记不拦（门槛没过也放行，只记下来）",
  jev_usd_per_mtok: "拍板人价格（每百万字美元）",
  jev_max_consecutive_errors: "拍板人连错几次就先歇一会",
  jev_error_cooldown_s: "拍板人连错后歇几秒",
  jev_shared_cooldown: "开仓拍板和持仓管家共用一份连错计数和冷却（关 = 各算各的）",
  account_read_retries: "开仓前读账户失败再读几次（隔 1 秒；0 = 不重读）",
  min_p_genuine: "信号至少多大把握是真的（0–1）",
  min_p_take: "拍板人：做的把握至少（0–1）",
  gate_questions: "拍板人怎么问",
  predictor_nudge_mode: "预测员怎样算和信号一致",
  entry_zone_only: "只在卡的进场区间内开仓",
  min_p_direction: "方向至少多大把握（0–1）",
  predictor_weight: "预测员意见占几成：排名和卡方向一致时加分，相反不扣",
  max_p_toxic: "被大户收割的可能最多多大（0–1）",
  signal_max_age_s: "信号超过几秒还没处理完就作废",
  size_fraction_s: "小仓位用正常仓位的几成",
  size_fraction_m: "中仓位用正常仓位的几成",
  atr_period_min: "按几分钟 K 线算波动幅度（定止损用）",
  atr_floor_7d: "波动幅度至少按 7 天中位数的几成算（防周末止损太窄）",
  min_stop_atr: "止损最近几个波动幅度（太近会被扫）",
  max_stop_atr: "止损最远几个波动幅度（太远不做）",
  stop_level_buffer_atr: "止损放在关键价位外面多远（波动幅度的几倍）",
  min_tp_r: "止盈至少是止损的几倍",
  max_tp_r: "止盈最多是止损的几倍",
  time_limit_s: "一笔最多拿几秒，到时就平",
  keeper_close_p: "管家觉得该平的把握过多少就平（0–1）",
  keeper_close_p_strong: "把握过多少不用再确认直接平（0–1）",
  keeper_confirmations: "管家连续几次都说平才平",
  keeper_price_max_age_s: "管家复查时行情最多旧几秒",
  keeper_model_mode: "Jev 的平仓概率：act 按它平 / record 只记录不平",
  keeper_price_source: "管家算浮盈用哪个价：mainnet 主网成交价 / venue 执行所标记价",
  keeper_thesis_take_on: "Jev 盈利门控：赚着且 Jev 说进场理由不成立就平",
  keeper_thesis_take_min_r: "盈利门控：浮盈至少几倍止损距离",
  keeper_thesis_take_p: "盈利门控：Jev 说理由不成立的把握至少多少（0–1）",
  keeper_thesis_take_max_age_s: "盈利门控：Jev 那次回答最多几秒前",
  keeper_close_guard: "平仓前检查：交易所保护腿在途 / 本轮已发过就不再发",
  keeper_close_guard_replacing_max_s: "平仓前检查：保护腿换代最多等几秒",
  keeper_close_guard_exiting_max_s: "平仓前检查：保护腿已触发 / 退出中最多等几秒",
  keeper_close_guard_total_max_s: "平仓前检查：保护腿连续在途合计最多等几秒",
  keeper_venue_alert_reviews: "执行所标记价连续几次读不到就告警（0 = 不告警）",
  frozen_peak_exit_on: "冻结盈利价回访（仅之后新开的 Demo 仓）",
  frozen_peak_exit_after_s: "从真实首笔成交起几分钟后冻结盈利价",
  frozen_peak_evidence_mode: "冻结盈利价的交易所证据怎么读",
  frozen_peak_evidence_interval_s: "后台读：每笔仓隔几秒读一次证据",
  frozen_peak_evidence_max_age_s: "后台读：证据超过几秒就不算（0 = 不限）",
  giveback_on: "回吐保护开关（代码规则，不问模型）",
  giveback_close_pct: "回吐保护：最高浮盈吐掉百分之几就全平",
  giveback_arm_r: "回吐保护：最高浮盈到过几倍止损距离才开始看",
  opposite_card_action: "新作战卡方向和持仓相反时：close 立刻全平 / confirm_or_tighten 价格确认才平，否则收紧止损到保本附近",
  opposite_card_ab: "反向卡 A/B：这个比例的事件走新规则，其余照旧全平（按持仓号 + 币 + 指令发布时间 + 方向抽签，整卡换版本不重抽；0 = 不分组）",
  opposite_anchor_retry_s: "trail 锚定时盘口疑似插针：这么多秒内每轮再试，过了按原规则平（0 = 立刻平）",
  opposite_confirm_loss_r: "反向卡价格确认①：已亏到几倍原始止损距离就平（0 = 不用）",
  opposite_confirm_atr: "反向卡价格确认②：看到卡后逆向走了几倍 ATR 就平（0 = 不用）",
  opposite_confirm_key_level: "反向卡价格确认③：越过反向卡在开仓价之外最近的关键位就平",
  opposite_tighten_buffer_bps: "反向卡收紧止损：保本（扣来回手续费）之外再多留几个基点",
  opposite_tighten_mark_gap_bps: "反向卡收紧止损：离标记价不到这么多基点就不挂、直接平（0 = 只要在安全一侧）",
  opposite_profit_mode: "反向卡没确认、赚得够时：trail 在现价强制启动移动止损 / breakeven 止损挪到扣费保本",
  opposite_trail_distance_mult: "trail 模式：锚定的移动止损距离 = 开仓带的移动止损距离 × 几倍",
  card_neutral_action: "开仓后新作战卡把这个币改成中性时：hold 不管 / tighten 亏的平、赚的锚定移动止损 / close 直接平",
  card_neutral_on_absent: "新作战卡里没有这个币也算中性",
  card_neutral_trail_distance_mult: "中性卡锚定的移动止损距离 = 开仓带的移动止损距离 × 几倍",
  patrol_shock_pct_1m: "1 分钟内往亏的方向走了百分之几就全平",
  patrol_alert_pct_1m: "1 分钟内往亏的方向走了百分之几就叫管家马上看",
  breakout_vol_ratio: "成交量是平时几倍算放量突破",
  surge_vol_ratio: "成交量是平时几倍算量能突增",
  surge_taker_ratio: "主动买（卖）占比多少算突增",
  volume_surge_live: "量能突增信号（测试网 / 实盘）",
  session_cme_open: "关键时段也看 CME 周日开盘",
  session_cme_open_live: "CME 周日开盘（测试网 / 实盘）",
  new_listing_block_h: "新上线的币多少小时内不做",
  entry_limit_pullback: "进场用挂单等回踩（默认关，关 = 市价进）",
  entry_limit_pullback_live: "实盘也用挂单等回踩",
  entry_limit_offset_stop: "挂单比触发价让多少（止损距离的几成）",
  entry_limit_wait_min: "挂单最多等几分钟",
  entry_limit_chase: "挂单没成交就市价追进",
  chase_price_max_age_s: "追进前行情最多旧几秒",
  trailing_on: "移动止损",
  trail_start_r: "赚到几个止损距离后开始跟",
  trail_distance_r: "止损跟在最高价后面几个止损距离",
  breakeven_on: "保本",
  breakeven_trigger_r: "赚到几个止损距离后保本",
  adl_high_block: "自动减仓风险高时不开新仓",
  oi_follow_on: "持仓量变化：出这个信号",
  oi_window_min: "持仓量变化：看多长时间（分钟）",
  oi_min_pct: "持仓量变化：持仓量至少涨（%）",
  oi_price_z: "持仓量变化：价格至少走（倍平时波动）",
  oi_follow_live: "实盘也出持仓量变化信号",
  top_crowd_on: "大户散户分歧：出这个信号",
  top_crowd_min_pct: "大户散户分歧：两边各至少变（%）",
  top_crowd_window_min: "大户散户分歧：看多长时间（分钟）",
  top_crowd_live: "实盘也出大户散户分歧信号",
  btc_lead_on: "BTC 先动：出这个信号",
  btc_lead_z: "BTC 先动：BTC 5 分钟至少走（倍平时波动）",
  btc_lead_live: "实盘也出 BTC 先动信号",
  market_review_on: "大盘更新后提名相关币复核",
  regime_flip_on: "大盘方向变化：独立更新大盘",
  regime_flip_live: "测试网 / 实盘也启用大盘翻转",
  regime_flip_breadth: "按 40 个币算，反向的至少几个",
  regime_flip_btc_pct: "BTC 至少反向走多少（%）",
  regime_flip_confirm: "连续满足几次才更新大盘",
  regime_flip_min_gap_min: "两次大盘更新至少隔多久（分钟）",
  regime_flip_min_symbols: "至少有几个有效币才判断",
  regime_flip_check_min: "每隔多久检查（分钟）",
  regime_flip_suspect_log_min: "同类疑似隔多久记文件（分钟）",
  regime_flip_price_max_age_min: "BTC 参照行情最多旧多久（分钟）",
  liq_min_usdt_1m: "1 分钟爆仓至少多少 USDT 才算",
  liq_ratio: "爆仓额是平时几倍才算",
  liq_drop_pct_5m: "爆仓砸出的 5 分钟跌幅至少百分之几",
  crowded_funding_rate: "资金费率多高算一边倒（0.0005 = 万分之五）",
  session_window_min: "开盘、结算前后几分钟算关键时段",
  session_vol_ratio: "关键时段成交量是平时几倍才算",
  mode_predictor: "预测哪些币",
  size_policy: "模型大小怎么选",
  model_size: "预测模型",
  adaptive_base_max_queue: "排队不超过几个时用最准的模型",
  adaptive_mini_min_queue: "排队到几个时换最快的模型",
  backfill_bars: "启动时先拉多少根 5 分钟 K 线",
  max_cards_per_round: "每轮最多开几个币的会（每天 AI 费用上限照样管着；0 = 不限）",
  concurrency: "同时开几个会",
  card_ttl_min: "作战卡管用几分钟（开会间隔 + 提前量）",
  symbol_min_interval_min: "同一个币至少隔几分钟才再开会",
  refresh_lead_min: "作战卡到期前几分钟开会换新卡",
  symbol_timeout_s: "一个币的会最多开几秒",
  min_refresh_gap_min: "两轮会至少隔几分钟",
  schedule_at: "每天固定几点开整轮会（曼谷时间）",
  event_meeting_enabled: "出事时给那一个币开临时会",
  event_triggers: "哪些事算“出事”",
  event_symbol_gap_min: "同一个币两场会至少隔几分钟（0 = 不限）",
  event_max_per_day: "一天最多开几场临时会（0 = 不限）",
  event_concurrency: "同时最多开几场临时会（不同币；1 = 排队一场一场开，0 = 跟「同时开几个会」一样）",
  key_level_trigger: "怎样算“突破关键价位”",
  move_since_card_atr: "价格离上次开会走了几个波动幅度算“走远了”",
  predictor_against_runs: "预测员连续几次看反才算",
  entry_max_card_age_min: "开仓前卡最多旧几分钟（超了先补会再开）",
  daily_cost_limit_usd: "每日 AI 费用上限（0 = 不限）",
  over_budget: "花到日上限 80% 时",
  slowdown_interval_min: "放慢后几分钟开一次会",
  sim_environment: "模拟盘连币安哪个环境",
  data_dir: "数据保存在哪个文件夹",
  jev_provider: "拍板人走哪个通道",
  jev_model: "拍板人用哪个模型版本",
};
const plainOf = (i) => PLAIN[`${i.sec}.${i.key}`] || PLAIN[i.key === "mode" && typeof i.value === "string" && i.value in CHOICE ? "mode_predictor" : i.key];
const label = (i) => plainOf(i) || (SHORT[i.key] && i.label.length > 30 ? SHORT[i.key] : i.label);
// 合约里的说明带英文选项名的，换一句中文
const HINT = {
  notify_open_filled: "每笔开仓成交时推一条（含入场价、止损止盈）。保存后几秒内生效",
  notify_close_done: "每笔平仓完成时推一条（含止盈 / 止损 / 到时 / 管家平的原因和盈亏）。保存后几秒内生效",
  notify_lifeboat_hit: "币安上的保命止损被触发时推。　建议保持开：关掉就收不到这类提醒。保存后几秒内生效",
  notify_lifeboat_missing: "保命止损没挂上、重挂或撤销失败时推——这时仓位可能没有最后一道保护。　建议保持开：关掉就收不到这类提醒。保存后几秒内生效",
  notify_order_failed: "开仓或平仓失败、结果不明、没成交就结束时推。　建议保持开：关掉就收不到这类提醒。保存后几秒内生效",
  notify_reconcile: "对账发现无人管的仓位、数量或方向对不上、手动平仓、连不上币安时推。　建议保持开：关掉就收不到这类提醒。保存后几秒内生效",
  notify_news_block: "某个币因新闻 / 事件被禁止新开仓时推。保存后几秒内生效",
  notify_model_credit: "OpenRouter 额度快用完或被拒（402）时推，没额度就没有新作战卡。　建议保持开：关掉就收不到这类提醒。保存后几秒内生效",
  credit_low_floor_usd: "OpenRouter 余额低于这个数（美元）就推一条。余额低于「近 7 天日均花费 × 2」时按那个更高的数算，所以实际提醒线可能比这里高。默认 5。保存后几秒内生效",
  credit_resend_hours: "同一条额度提醒（快用完 / 被拒）每隔多少小时重复推一次，充值后自动停。默认 6。保存后几秒内生效",
  card_score_every_min: "面板每隔多少分钟给 4 小时窗口走完的作战卡补算一次方向记分（只读币安主网公开 K 线，过去的整天用历史日包）。0 = 不算。默认 60。只有面板用，不用重启主循环：面板等完这一轮（最多一个间隔）后按新值算",
  card_score_window_days: "「历史卡」顶上的方向记分只汇总最近多少天出的卡（逐卡结果照样全留）。0 = 全部。默认 30。下一轮记分时生效",
  notify_meeting_paused: "开会连续失败或当日花费超限，暂停开会时推。保存后几秒内生效",
  notify_spend_alert: "每日 AI 费用上限设为不限时，当天花费超过本档默认上限推一条（只提醒，不停会）。保存后几秒内生效",
  notify_control: "你在面板暂停 / 恢复开新仓、全部平仓、重新加载配置时推。保存后几秒内生效",
  notify_live_started: "实盘启动时推。　建议保持开：关掉就收不到这类提醒。保存后几秒内生效",
  notify_system: "拒绝启动、状态文件损坏、巡逻兵全平等系统级问题时推。　建议保持开：关掉就收不到这类提醒。保存后几秒内生效",
  vol_full_multiple: "把这个币平时的波动按每小时折算，是来回手续费（0.10%）的几倍就算这一项满分。默认 30，也就是每小时 3% 算满分；改小会有更多币挤在满分上、分不出高低",
  always_candidates: "这些币只要过了硬门槛、没被拉黑，每轮都进候选池（算在池子名额里），由总盘会按数字决定挑不挑。不算头部，也不占中部每天挑几个的名额。想强制开会用选币页的「固定」。填币名，空格隔开，如 BTC ETH",
  always_candidates_meet: "打开 = 上面这些币每轮不经总盘会挑选，直接开单币会议，由会议自己定做多、做空还是不做；不占每轮挑币的名额，军师照样挑满原来的个数。仍要过硬门槛，交易所上不在交易的币不开；日花费上限、会议并发照旧。关掉 = 由总盘会决定挑不挑（原来的做法）",
  gate_record_only: "开 = 拍板人照常问、照常记，概率没过门槛也放行（盘口有毒 ≥ 0.8 仍然拦），用来在模拟盘攒样本校准门槛。关 = 拍板一票否决。只有模拟盘能开",
  volume_surge_live: "开 = 成交量突然放大、而且买卖一边倒时顺着那一边做。历史回测不稳定，觉得不对可以关；模拟盘不管开关都照出（攒样本）",
  session_cme_open: "开 = 芝加哥 CME 比特币期货周日开盘（曼谷周一早上 5 点或 6 点）前后几分钟，放量又大涨大跌时顺着做。回测只有 30 分钟到 2 小时这三档比随便进好，档位默认只在这三档开",
  session_cme_open_live: "开 = 测试网 / 实盘也出 CME 周日开盘信号。证据还不够（主要靠 4 个周日），默认关，只在模拟盘攒样本",
  jev_max_concurrent: "不同的币可以同时判，最多这么多个；同一个币一次只判一个，新信号排队等上一个判完，排队的只留最新一条，等太久（超过信号有效期）就作废",
  entry_limit_pullback: "开 = 信号来了不马上市价买，往便宜的方向让一段挂只挂单的限价单，等回踩成交（手续费也低）。等够时间没成交就撤单不开（8 小时以上档改市价追）。成交后才挂交易所兜底止损。关 = 照旧市价进",
  entry_limit_pullback_live: "开 = 测试网 / 实盘也用挂单等回踩。默认关，先在模拟盘看",
  entry_limit_offset_stop: "例如止损离进场价 1%，填 0.25 就挂在比触发价便宜 0.25% 的地方。档位默认 30 分钟 0.6、1 小时 0.4、2 / 4 小时 0.25、8 小时以上 0.1",
  entry_limit_wait_min: "从下单开始算；最长持仓按成交那一刻算，不会因为等挂单少拿",
  entry_limit_chase: "开 = 等到时没成交就撤单改市价进；关 = 撤单不开。档位默认 8 小时以上开",
  chase_price_max_age_s: "改市价追进前按现价复查进场区间，这个币的行情超过这么多秒没更新就这次不追（和开仓检查同一个时钟）。0 = 不查（现状）",
  keeper_model_mode: "act = 现状：Jev 的平仓概率过线（连续确认 / 明显过线 / 反向急查过线即平）就平。record = 每次照常问 Jev、照常记下，但不按平仓概率平；回吐保护、作战卡、冻结盈利价、盈利门控照常，被封的币直接平。决策记录里每条规则的票都写着",
  keeper_price_source: "mainnet = 现状：行情快照的主网最新成交价。venue = 执行所的标记价（Demo 上是 Demo 标记价，实盘就是主网）：浮盈、最高浮盈、回吐保护、盈利门控和给 Jev 的快照都用它；「行情最多旧几秒」按这个价查（填 0 时按 5 秒）；读不到或过旧这轮不按价判、维持现状，不拿主网价凑。最高浮盈两种价分开记，中途切换不会混着比。两种价都写进决策记录",
  keeper_close_guard: "开 = 管家每次要平之前先看：交易所的保护腿已经触发或正在退出（交给保护轮询收尾）、正在换代，或这笔仓半个复查间隔内已经发过一次平仓，就这次不发（作战卡硬规则也一样，也不收紧）；等过下面的秒数照常平并记下。关 = 现状（不看）",
  keeper_thesis_take_on: "三条同时成立就全平：浮盈（未扣手续费，和回吐保护同口径）至少下面那么多倍止损距离；Jev 最近一次答「进场理由还成立吗」是 no 的把握至少下面那个数；那次回答不超过下面那么多秒。排在回吐保护之后、平仓概率之前，只平不放宽。默认关",
  keeper_price_max_age_s: "管家复查时这个币的行情超过这么多秒没更新：这一轮跳过、维持现状，不平仓也不问模型，连续计数不变。0 = 不查（现状）",
  entry_zone_only: "开 = 卡上这个币写了进场区间（例如反抽做空区、回调做多区），触发价在区间外就不开，连拍板人都不问，团队页风控员那一行写「没到区间」；往交易方向最多放宽 0.1R（做多可以比区间上沿高一点，做空可以比下沿低一点）。卡上没写区间照旧不限。关 = 不看区间。默认关（10-24 的数据显示开着会挡掉好信号）",
  stop_floor_widen: "开 = 止损离手续费太近（不到上面那个倍数）时，把止损拉宽到这条线再下单：每笔最多亏的钱不变，所以仓位会小一些，止盈也按新止损重算。关 = 这一单不做。默认开",
  predictor_weight: "预测员（Kronos，照官方用法）：每个币算一个分数（平均走势终点比现价高多少），同一时刻所有币一起排名，排在前段算看好、后段算看坏（段多大看预测员那组的「排名前 / 后多少」）。做多信号碰上看好、做空信号碰上看坏，拍板人的「做」先加 这个数 × 0.5 再和门槛比，0.2 时加 0.10；其余情况不加也不扣。单个币的涨跌方向不看（官方也只看名次）。填 0 = 不听；预测员关掉、预测过期或一起预测的币太少时不加。档位默认都是 0.2",
  min_p_take: "拍板人只答一道题：现在按信号方向开这一单，接下来一段时间（2 小时档看 30 分钟）扣完手续费能不能赚。它给出「做」的把握（0–1），加上预测员的分以后至少要到这个数才放行。默认 0.55；想多做一些，可以放到 0.50",
  gate_questions: "一道题 = 只问「做不做」（真假、方向、盘口有没有毒都并进这一道，再看技术面和完整作战卡），只看上面「做的把握至少」。6 道题 = 以前的问法，真信号 / 方向 / 盘口有毒三个门槛也生效，留着回放对照用。默认一道题",
  trailing_on: "开 = 赚到下面的线以后，止损跟着这笔最好时的盈利往上挪，只收紧不放松，保住最高利润的一大半；关 = 只有原来的止损止盈。默认开，改了只对之后新开的仓生效，止损由执行器（hummingbot）实时盯着，不是只改面板上的数",
  trail_start_r: "单位是止损距离：1 = 一个止损距离。例如这笔止损离进场价 1%，填 0.7 就是赚到 0.7% 才开始跟（同时至少要够把止损挪到保本价）；刚开的仓不碰。档位默认都是 0.7",
  trail_distance_r: "单位是止损距离：1 = 一个止损距离。例如止损离进场价 1%，填 1.2 就是止损一直跟在这笔最高价往回 1.2% 的地方；填大一点平时不碍事，只在大回撤时锁住利润。档位默认 30 分钟–2 小时 0.7、4 小时以上 1.0",
  breakeven_on: "开 = 赚到下面的线以后，最坏按保本价平（开平两次手续费扣完不亏）。和移动止损可以一起开：一起开时从保本线就开始跟。默认关，只对之后新开的仓生效",
  breakeven_trigger_r: "单位是止损距离：1 = 一个止损距离。例如止损离进场价 1%，填 0.8 就是赚到 0.8% 后止损挪到保本价。默认 0.8",
  new_listing_block_h: "按币安上线时间算，刚上线的币价格还不稳。填 0 = 不挡",
  adl_high_block: "币安每 30 分钟给每个币标一次自动减仓风险（高 / 中 / 低）。是「高」时赚钱的仓可能被强制减掉，开 = 不开新仓；已有持仓不管；查不到就照常开",
  oi_follow_on: "持仓量在涨、价格也在涨 = 新多头进场，跟着做多；价格在跌就是新空头，跟着做空。持仓量在跌不出信号。回测 2 小时到 24 小时这几档不比现有信号差，档位默认在这几档开",
  oi_window_min: "同一个币同一个方向这么久只出一次。档位默认 2 小时档 60、4 小时以上 240",
  oi_min_pct: "这段时间持仓量至少涨这么多才算新钱进场",
  oi_price_z: "平时波动 = 过去 24 小时每分钟涨跌的标准差，按时间长短放大。填 0 = 不看价格走多远，只看方向",
  oi_follow_live: "开 = 测试网 / 实盘也出持仓量变化信号。默认关，先在模拟盘攒样本",
  top_crowd_on: "大户（保证金最多的那批账户）一边倒加仓、散户往另一边走时，反着大户做：大户加多散户减多就做空，反过来做多。过去半年 8–24 小时持有扣费后不亏，档位默认 4 小时以上开",
  top_crowd_min_pct: "大户多空比和全市场人数多空比要往相反方向各变这么多。默认 5；想多出信号可以调到 2",
  top_crowd_window_min: "同一个币同一个方向这么久只出一次",
  top_crowd_live: "开 = 测试网 / 实盘也出大户散户分歧信号。默认关，先在模拟盘攒样本",
  btc_lead_on: "BTC 突然大动、这个币还没跟着动时，按 BTC 的方向做这个币，赌它补涨补跌。回测 30 分钟到 2 小时这三档最好，档位默认在这三档开",
  btc_lead_z: "BTC 最近 5 分钟走了几倍平时波动才算「突然大动」",
  btc_lead_live: "开 = 测试网 / 实盘也出 BTC 先动信号。默认关，先在模拟盘攒样本",
  market_review_on: "大盘有新判断后，只提名有具体行情证据且仍在原池和现卡内的币，复用同币间隔、日次数、预算和持仓优先；还需打开军师团的总临时会开关。不依赖旧事件名单，不直接改方向或延长有效期。没有新增每十分钟总盘调用",
  regime_flip_on: "默认每 5 分钟检查：有效币中至少四分之三同向、BTC 相对参照至少同向 1%，连续 2 次后只更新大盘。中性同时对称检查向上和向下；不会重跑各币会议或延长卡有效期。疑似、确认、被拦原因在哨兵日志里",
  regime_flip_live: "开 = 测试网 / 实盘也启用。默认关，仅模拟盘开启；总开关关闭时所有模式都停用",
  regime_flip_breadth: "默认 30：40 个有效币里至少 30 个，也就是四分之三，过去一小时往旧判断的反方向走。不是 40 个时按比例算；持平和缺数据的币不计入。填 0 不要求池面条件",
  regime_flip_btc_pct: "BTC 比市场参照价至少反向走百分之几；中性检查上下两个方向。同方向刷新保留参照，只有观点方向改变或定期整轮成功才推进参照。默认 1 表示 1%；填 0 不要求 BTC 幅度条件",
  regime_flip_confirm: "默认连续 2 次同方向检查都满足才更新大盘。填 1 是满足一次即更新，填 0 不等连续确认；次数不设上限，改门槛后重新累计",
  regime_flip_min_gap_min: "默认 60 分钟内不因方向变化再次更新大盘，减少来回震荡时的 AI 花费。填 0 不限制；仍遵守已有日预算、限流和失败退避",
  regime_flip_min_symbols: "默认至少 10 个币有可用的一小时涨跌。持平和缺数据的币不算；填 0 不限制个数，但启用池面条件时仍需有数据才能计算比例",
  regime_flip_check_min: "默认 5 分钟检查一次，可填任意正数或小数；填 0 停止定时检查。改后按新周期重新计时，无需重启",
  regime_flip_suspect_log_min: "连续同类疑似在独立观测文件里默认每 30 分钟再记一次，填 0 每次都记。面板和决策日志始终记录每次检查发现的疑似",
  regime_flip_price_max_age_min: "取上次总盘会 BTC 参照价时，行情最多允许旧几分钟。默认 5，填 0 不限制新鲜程度；没有价格仍不能建立参照",
  jev_provider: "官方直连要 Jev 的 key；OpenRouter 和军师团共用一把 key",
  jev_model: "锁死一个版本，门槛按它调过；不在网页上改",
  jev_usd_per_mtok: "按用量算钱，用来统计花费",
  sim_environment: "Demo Trading 是币安现在的模拟盘；旧测试网已停用",
  mode_predictor: "按需 = 只算作战卡里和有持仓的币；全算 = 候选池每个币都算（费电脑）",
  size_policy: "固定 = 一直用下面选的模型；自适应 = 排队多时自动换小模型",
  lookback: "每次预测喂给模型多少根历史 K 线。官方论文的做法是回看 ≈ 往后预测的 5 倍（5 分钟线 480/96、15 分钟线 160/32）；至少 64 根才开算。回看 + 往后预测不能超过 512。档位默认 2 小时档 120、4 小时档 80（15 分钟线）",
  paths: "官方用法：一次模拟这么多条走势，模型自己平均成一条，用平均走势的终点打分。多 = 更稳，但更慢。官方和档位默认都是 10",
  temperature: "模拟走势时的随机程度：小 = 每条更像，大 = 更分散。官方回测用 0.6，档位默认 0.6",
  rank_band: "所有一起预测的币按分数排队：排在最前这么多算看好（做多加分），最后这么多算看坏（做空加分），中间不加。1/6 ≈ 0.17 = 前六分之一 / 后六分之一。看坏这一头是我们为做空加的，官方只做多",
  keeper_snapshot: "方向（原行为）= 按平均路径终点在现价哪一侧告诉管家同向 / 反向，再带名次；预测员的方向只有涨 / 跌两档、实测多数是跌，做多的仓多半被说成反向、做空的多半同向。名次 = 只带全池排名（和拍板加分同一个排名，多空对称）；不带 = 管家看不到预测员。改后重启",
  rank_min_peers: "一起预测的币少于这个数就不排名、不加分（按需模式只算卡上和持仓的币，币少时名次没意义）。想币多一点，可把预测范围改成「全算」",
  model_size: "标准 / 最大：最大更准，但每个币算得慢 3–4 倍（Mac mini 每币约 3.5 秒，标准约 1 秒）；加载失败会自动改用标准（选“固定”时用这个）",
  adaptive_base_max_queue: "选“自适应”时生效",
  adaptive_mini_min_queue: "选“自适应”时生效；介于两者之间用中模型",
  profile: "在上面的档位卡片里换",
  meeting_interval: "在上面的档位卡片里换",
  hold: "在档位参数表第一行改；不写 = 跟开会档位走",
  data_dir: "决策记录、状态文件都存在这里",
  schedule_at: "填了就每天这个时间开，不按启动时间漂；留空 = 按开会间隔",
  event_meeting_enabled: "只给出事的那个币补开一场，不重开整轮；关掉时突破、大消息、新进池照旧重开整轮",
  event_triggers: "不在名单里的事不开会，等下一轮定时会",
  key_level_trigger: "在外面就算 = 价格一直在关键位外面，隔够间隔就又开会；穿过才算 = 出卡后从里面穿出去那一下才开，出卡时已在外面的位要等回来再穿",
  entry_max_card_age_min: "要在“哪些事算出事”里勾上“开仓时卡太旧”才会补会，否则只拦不补",
  daily_cost_limit_usd: "只算军师团开会（含总盘会）的 AI 费；没手动固定就跟开会档位走，切档自动换",
  over_budget: "放慢一档 = 开会间隔换成下面那个；不放慢 = 照常开。花到 100% 都停开会，持仓照常管",
};
// 灰色小字只放中文说明；英文键名只在鼠标悬停时显示（排查问题用）
const hintKey = (i) => (i.key === "mode" && typeof i.value === "string" && i.value in CHOICE ? "mode_predictor" : i.key);
const techHint = (i) => [HINT[hintKey(i)] ? esc(HINT[hintKey(i)]) : plainOf(i) && i.label && i.label !== i.key && !/^[\x00-\x7f]+$/.test(i.label) ? esc(i.label) : "", i.recommended != null ? `推荐 ${valOf(i, i.recommended)}（${esc(i.recommended_why)}）` : i.key === "frozen_peak_exit_after_s" ? "范围 1–10080 分钟" : i.bounds ? "范围 " + esc(i.bounds) : ""].filter(Boolean).join(" · ");
const NULL_AS = { schedule_at: "不定点，按开会间隔", slowdown_interval_min: "下一档", max_cards_per_round: "跟“军师团每次最多挑几个币”一样", "screener.max_spread_bps": "跟硬上限里的“最大价差”走", "risk.max_spread_bps": "不写：按这笔止损的 1/10", "risk.max_slippage_bps": "不写：按这笔止损的 1/4", "take_by_kind.*": "跟总门槛", "open_by_kind.*": "不单独限" };
// ---- 填 0 = 关闭 / 不限 的设置：哪些项有、开关上写什么，都来自后端每项的 off（panel/pages.py 的 OFF_SWITCHES）
const offShown = (i, v = i.value) => (v === 0 && i.off ? i.off.shown : null);
// 编辑框旁边的开关；现值是 0 就是勾着的（点「用推荐值」时填的是推荐值，不勾）
const offSwitchHtml = (i, useRec) => (i.off ? ` <label class="small"><input type="checkbox" id="dlg-off" ${i.value === 0 && !useRec ? "checked" : ""}> ${esc(i.off.label)}</label>` : "");
// 保存时：勾着就写 0，不看输入框
const offPicked = (i) => !!i.off && !!$("#dlg-off") && $("#dlg-off").checked;
// 勾上：输入框清空并禁用；取消：回到现值，现值是 0 就用档位默认，再没有就用表里的 back
function offBind(i) {
  const box = $("#dlg-off");
  if (!box) return;
  const num = $("#dlg-v");
  const sync = () => {
    num.disabled = box.checked;
    if (box.checked) num.value = "";
    else if (!Number(num.value) && !i.nullable) num.value = i.value || i.profile_default || i.off.back;
  };
  box.addEventListener("change", sync);
  num.disabled = box.checked;
}
// 上面那项关了，这一项就用不上：键 → [控制项, 说明]
const MOOT = {
  "schedule.over_budget": ["budget.daily_cost_limit_usd", "日上限已关闭，用不上"],
  "schedule.slowdown_interval_min": ["budget.daily_cost_limit_usd", "日上限已关闭，用不上"],
  ...Object.fromEntries(["on", "live", "breadth", "btc_pct", "confirm", "min_gap_min", "min_symbols", "suspect_log_min", "price_max_age_min"].map((k) => [`fast_loop.regime_flip_${k}`, ["fast_loop.regime_flip_check_min", "定时检查已关闭，用不上"]])),
};
const mootOf = (i, ix) => {
  const m = MOOT[`${i.sec}.${i.key}`];
  const c = m && ix[m[0]];
  return c && c.off && c.value === 0 ? m[1] : "";
};
// ---- 填 0 = 关闭 / 不限 结束
// 时间类设置按单位后缀换成人话：1.67 小时 → 1 小时 40 分
const UNIT_S = [["_hours", 3600], ["_min", 60], ["_ms", 0.001], ["_s", 1]];
function humanDur(sec) {
  if (sec < 1) return `${Math.round(sec * 1000)} 毫秒`;
  if (sec < 60) return `${+sec.toFixed(1)} 秒`;
  const t = sec >= 3600 ? Math.round(sec / 60) * 60 : Math.round(sec); // 一小时以上精确到分
  const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), x = t % 60;
  return [h ? `${h} 小时` : "", m ? `${m} 分` : "", x ? `${x} 秒` : ""].filter(Boolean).join(" ") || "0 秒";
}
function durOf(key, v) {
  // size_fraction_s/m 的 s/m 是仓位档（小 / 中），不是秒和分
  if (typeof v !== "number" || key === "atr_period_min" || key.startsWith("size_fraction")) return null;
  const u = UNIT_S.find(([suf]) => key.endsWith(suf));
  return u ? humanDur(v * u[1]) : null;
}
const fmtVal = (v, i) => i && i.names ? esc((v || []).join(" ") || "没有") : Array.isArray(v) ? esc(v.map((c) => choiceLabel(i ? i.key : "", c)).join("、") || "一个都不选") : (i && !(i.choices && i.choices.length) && durOf(i.key, v)) || (typeof v === "boolean" ? (v ? "开" : "关") : v == null ? (i && i.nullable ? NULL_AS[`${i.sec}.${i.key}`] || NULL_AS[`${i.sec}.*`] || NULL_AS[i.key] || "不写（用默认）" : "—") : (i && i.choices && i.choices.length) || (typeof v === "string" && CHOICE[v]) ? esc(choiceLabel(i ? i.key : "", v)) : esc(v));
// 档位 = 军师团开会间隔（Aaron 2026-10-23），不是持仓时长；单笔最长持仓另设
const profileSpan = (p) => { const m = /^(\d+)(h|m)$/.exec(p); return m ? (m[2] === "h" ? `${m[1]} 小时` : `${m[1]} 分钟`) : p; };
const profileNote = (p) => `军师团每 ${profileSpan(p)}开一次会${p === TIER_REC ? "（推荐）" : ""}`;
// 止损 ATR 用几分钟 K 线，适合多长的单笔最长持仓（只提醒，不拦）
const ATR_FIT = { 5: ["≤30 分钟", 0, 1800], 15: ["1–2 小时（推荐）", 1800, 7200], 30: ["2–4 小时", 7200, 14400], 60: ["4 小时以上", 14400, Infinity] };
const atrFor = (limit) => +Object.keys(ATR_FIT).find((k) => limit > ATR_FIT[k][1] && limit <= ATR_FIT[k][2]);
const atrMismatch = (atr, limit) => (ATR_FIT[atr] && atrFor(limit) !== +atr ? `${atr} 分钟 K 线适合最长持仓 ${ATR_FIT[atr][0].replace("（推荐）", "")}，现在最长持仓 ${humanDur(limit)}；建议 ${atrFor(limit)} 分钟。只提醒，不拦。` : "");
// 写死项表格：文件值 → 某档位的默认值（一样的标出来）
function pinTable(list, p) {
  return `<div class="tw"><table><thead><tr><th>项</th><th class="r">手动固定</th><th class="r">${esc(p)} 档默认</th></tr></thead><tbody>${list.map((x) => {
    const i = { key: x.key };
    const same = JSON.stringify(x.value) === JSON.stringify(x.profile_default);
    return `<tr><td title="${esc(x.key)}">${esc(PLAIN[x.key] || x.key)}</td><td class="r num ${same ? "dim" : "warn"}">${fmtVal(x.value, i)}</td><td class="r num">${fmtVal(x.profile_default, i)}${same ? ' <span class="dim small">相同</span>' : ""}</td></tr>`;
  }).join("")}</tbody></table></div>`;
}
const CLEAR_TEXT = "清掉这些手动固定的项，完全按档位走";
async function clearPins(pw) {
  return post("/api/settings/clear-profile-pins", { password: pw });
}
function askClearPins() {
  const pf = pageData && pageData.profile;
  const list = (pf && pf.pins && pf.pins[pf.current]) || [];
  if (!list.length) return;
  ask(CLEAR_TEXT, `<p>删掉配置文件里这些行，之后切档位它们跟着档位变。改前自动备份，重启主程序后生效。</p>${pinTable(list, pf.current)}${pwField(data.control)}`, "清掉", false, async () => {
    const pw = $("#dlg-pw");
    const res = await clearPins(pw ? pw.value : "");
    onOk = null;
    $("#layer [data-ok]").remove();
    $("#layer [data-close].btn").textContent = "关闭";
    dlgMsg(`<b class="up">已清掉 ${res.changed.length} 项</b>，点「重启生效」后按档位走。`);
    poll();
  });
}
function unpin(arg) {
  const [sec, key] = arg.split(":");
  const i = ((pageData && pageData[sec]) || []).find((x) => x.key === key);
  if (!i) return;
  const body = i.pinned
    ? `<p>去掉你改过的值：${fmtVal(i.value, i)} → 档位默认 ${fmtVal(i.profile_default, i)}，以后切档位也跟着变。</p>`
    : `<p>去掉你改过的值：${fmtVal(i.value, i)} → 程序默认 ${fmtVal(i.code_default, i)}。</p>`;
  ask(`${i.pinned ? "恢复档位默认" : "恢复默认"} · ${esc(key)}`, `${body}${pwField(data.control)}`, "恢复", false, async () => {
    const pw = $("#dlg-pw");
    await post("/api/settings", { section: sec, changes: { [key]: null }, password: pw ? pw.value : "" });
    onOk = null;
    $("#layer [data-ok]").remove();
    $("#layer [data-close].btn").textContent = "关闭";
    dlgMsg(`<b class="up">已恢复</b>，重启 <code>jiaoyi run</code> 后生效。`);
    poll();
  });
}

// 换档：自定义的项一律保留（不再清写死项），会变什么已经在预览条里列过
function switchProfile(p) {
  const pf = pageData && pageData.profile;
  if (!pf) return;
  const { changed, kept } = tierDiffs(pageData, pf.current, p);
  ask(`换到 ${esc(profileSpan(p))}档`, `<p>${esc(profileNote(p))}。会变 ${changed.length} 项，${kept.length ? `你自定义的 ${kept.length} 项不动。` : "没有自定义的项。"}</p>${pwField(data.control)}`, "换档", false, async () => {
    const pw = $("#dlg-pw");
    await post("/api/settings", { section: "app", changes: { meeting_interval: p }, password: pw ? pw.value : "" });
    onOk = null;
    S.tierSel = null;
    $("#layer [data-ok]").remove();
    $("#layer [data-close].btn").textContent = "关闭";
    dlgMsg(`<b class="up">已换到 ${esc(profileSpan(p))}档</b>。点「重启生效」后按新档位运行。`);
    poll();
  });
}

// 单笔最长持仓：「auto」= 删掉 hold 行，跟开会档位的推荐搭配；其余 = 写死成这一档（自定义）
function switchHold(h) {
  const d = pageData;
  const pf = d && d.profile;
  if (!pf) return;
  const auto = h === "auto";
  const target = auto ? pf.hold_recommended[pf.current] || pf.hold : h;
  const spec = pf.hold_specs[target] || {};
  const tl = settingIndex(d)["fast_loop.time_limit_s"];
  const dropTl = tl && tl.pinned;  // 文件里写死的秒数压过 hold，一起删掉才真正按这一档
  const rec = pf.hold_recommended[pf.current];
  const tip = !auto && rec && rec !== h ? `<p class="small warn">每 ${esc(profileSpan(pf.current))}开会推荐最长持仓 ${esc(profileSpan(rec))}。只提醒，不拦。</p>` : "";
  const what = auto ? `跟开会档位走（现在 ${esc(profileSpan(target))}）` : `固定 ${esc(profileSpan(h))}（自定义）`;
  ask(`单笔最长持仓：${what}`, `<p>到 ${esc(profileSpan(target))}还没走就平；止损按 ${esc(spec.atr_period_min)} 分钟 K 线 ATR，止盈、拍板门槛、管家频率跟着这一档。只对之后新开的仓生效。</p>${tip}${dropTl ? `<p class="small">手动固定的「一笔最多拿 ${esc(humanDur(tl.value))}」会一起删掉。</p>` : ""}${pwField(data.control)}`, "确定", false, async () => {
    const pw = $("#dlg-pw");
    const pass = pw ? pw.value : "";
    await post("/api/settings", { section: "app", changes: { hold: auto ? null : h }, password: pass });
    if (dropTl) await post("/api/settings", { section: "fast_loop", changes: { time_limit_s: null }, password: pass });
    onOk = null;
    $("#layer [data-ok]").remove();
    $("#layer [data-close].btn").textContent = "关闭";
    dlgMsg(`<b class="up">已改成${what}</b>。点「重启生效」后按新的最长持仓开仓。`);
    poll();
  });
}

// 密钥只能在本机改（服务器那边也只认本机），局域网打开时按钮置灰并说明
const IS_LOCAL = ["localhost", "127.0.0.1", "[::1]"].includes(location.hostname);
const keyBtn = () => (IS_LOCAL ? `<a class="btn b-sm b-go" href="/setup">修改密钥</a>` : `<button class="btn b-sm" disabled title="只能在 Mac 本机打开 http://127.0.0.1:${esc(location.port || "8090")}/setup">修改密钥</button>`);
// 密钥：按当前模式和通道分“必须 / 可选 / 现在用不到”，只有必须的没配才标红
const NEED_ORDER = { required: 0, optional: 1, unused: 2 };
function keyRows(d) {
  const need = d.key_needs || {};
  return Object.entries(d.keys)
    .sort(([a], [b]) => (NEED_ORDER[need[a]] ?? 1) - (NEED_ORDER[need[b]] ?? 1))
    .map(([k, ok]) => {
      const n = need[k] || "optional";
      const st = n === "unused" ? `<span class="dim">现在用不到</span>`
        : ok ? `<span class="lamp ok"><i></i>已配置</span>`
        : n === "required" ? `<span class="pill p-short">缺，必须配</span>`
        : `<span class="dim">可选，不配也行</span>`;
      const why = n === "unused" ? (k.includes("TESTNET") ? "实盘模式下不用模拟盘的 key" : k.startsWith("BINANCE") ? "实盘才用" : "现在走的通道用不到它") : KEY_HELP[k] || "";
      return `<div class="fr ${n === "unused" ? "dimrow" : ""}"><label title="${esc(k)}">${esc(KEY_LABEL[k] || KEY_NOTE[k] || k)}<span class="hint">${esc(why)}</span></label>${st}</div>`;
    }).join("");
}

// 设置页 v3：首屏放档位、风控硬上限和运行；其余按角色分组，顺序跟交易流程走，每个角色一块、默认收起，点开就是它的全部设置。
// 角色 id 和团队页一样（#team/gate ↔ #settings/gate）。strip = 行名开头是本角色名字的去掉（在哪一块已经说明了是谁）
const CORE = ["risk.risk_per_trade_pct", "risk.daily_loss_halt_pct", "risk.max_leverage"];
const ROLES = [
  { id: "screener", t: "选币员", what: "用代码把全部币安永续筛一遍，挑出候选池", groups: [
    { g: "多久选一次、池子多大", keys: ["app.screener_interval_min", "app.pool_size"] },
    { g: "门槛", warn: "screener_cautions", hint: "过不了门槛的币不进候选池。", keys: ["screener.min_quote_volume_usdt", "screener.max_spread_bps", "screener.min_depth_usdt", "screener.min_listed_days", "screener.min_range_24h_pct", "screener.max_range_24h_pct", "screener.default_category_cap", "screener.allow_tradfi", "screener.exclude_untradeable"] },
    { g: "打分", hint: "过了门槛的币按五项打分，取前面的进池。下面是每项到多少算满分。", keys: ["screener.vol_full_multiple", "screener.surge_full_multiple", "screener.funding_full", "screener.oi_full_pct", "screener.liq_full_usdt"] },
    { g: "权重", hint: "五项各占多少分量。不用加起来等于 1，只看相对大小；不能全是 0。", keys: ["screener.weight_volatility", "screener.weight_volume_surge", "screener.weight_funding", "screener.weight_oi", "screener.weight_liquidations"] },
    { g: "主流币名单", extra: "majors", hint: "「主流币」这一类按市值前几名算，每天自动更新一次（去掉稳定币和包装币）。填 0 = 只用固定 7 个（BTC ETH BNB SOL XRP DOGE ADA）。手动算主流 / 排除的填币名，空格隔开。", keys: ["screener.major_top_n", "screener.major_include", "screener.major_exclude"] },
    { g: "分档交易", warn: "tiers_cautions", hint: "候选池按分数从高到低分三档：头部开放交易；中部每天挑几个来做、仓位封顶；其余尾部只看不做。你固定、手动加的币算头部；每轮进总盘会的币不算头部。", keys: ["tiers.enabled", "tiers.head", "tiers.mid", "tiers.mid_daily_pick", "tiers.mid_size_max", "tiers.always_candidates", "tiers.always_candidates_meet"] },
    { g: "美股类别", keys: ["screener.us_events", "screener.us_open_bonus"] },
  ] },
  { id: "market", t: "总盘会", what: "整轮开会的节奏：多久一轮、挑几个币、作战卡管多久", caution: "schedule_caution", groups: [
    { g: "", hint: "开会间隔和单笔最长持仓在页面上面「选档位」里换。", keys: ["app.slow_loop_interval_min", "app.card_pick_max", "schedule.max_cards_per_round", "schedule.schedule_at", "schedule.card_ttl_min", "schedule.refresh_lead_min", "schedule.min_refresh_gap_min"] },
  ] },
  { id: "commander", t: "军师团", strip: ["军师团"], what: "给上会的币一个个开会，写作战卡", prompts: true, groups: [
    { g: "每个币的会", keys: ["schedule.concurrency", "schedule.symbol_min_interval_min", "schedule.symbol_timeout_s"] },
    { g: "方向记分", hint: "只看不改：面板定时给窗口走完的作战卡记分（卡真正发出的时刻起算、扣掉同期币池），结果在「历史卡」顶上。不影响开会和交易。", keys: ["app.card_score_every_min", "app.card_score_window_days"] },
    { g: "会议输入和偏空整改（改了要重启主程序）", hint: "默认都是现状，提示词和原来一字不差。偏空整改四项可以用“对照比例”让一部分会议用新写法，另一部分照旧，对比效果（先把下面四项改掉至少一项，再开对照，否则会被拒绝）；输入新鲜度几项让会议材料写明时间、加实时价。教训口径只影响之后的汇总。", keys: ["meeting.lessons_scoring", "meeting.debate_first", "meeting.funding_label", "meeting.bear_bull_symmetry", "meeting.crowd_ratio_mode", "meeting.bias_fix_ab", "meeting.snapshot_interval", "meeting.snapshot_label", "meeting.live_price_line", "meeting.stale_on_arrival", "meeting.stale_on_arrival_action", "meeting.input_time_labels", "meeting.market_table_rolling", "meeting.latest_market_regime", "meeting.event_brief_refresh"] },
    { g: "临时会", hint: "作战卡有效期里出了事，只给那一个币补开一场。开会间隔 2 小时起默认开。", keys: ["schedule.event_meeting_enabled", "fast_loop.market_review_on", "schedule.event_triggers", "schedule.event_symbol_gap_min", "schedule.event_max_per_day", "schedule.event_concurrency", "schedule.key_level_trigger", "schedule.move_since_card_atr", "schedule.entry_max_card_age_min"] },
  ] },
  { id: "ai_cost", t: "AI 费用", what: "军师团每天最多花多少；花到 80% 放慢或停", groups: [
    { g: "", hint: "曼谷 0 点清零；花到 80% 按下面放慢或停，花到 100% 当天停开会，持仓照常管。", keys: ["budget.daily_cost_limit_usd", "schedule.over_budget", "schedule.slowdown_interval_min", "budget.*"] },
  ] },
  { id: "book_reader", t: "盘口员", what: "盯着上会币的盘口和成交", groups: [] },
  { id: "predictor", t: "预测员 Kronos", strip: ["预测员"], what: "用 Kronos 模型预测接下来的价格", groups: [
    { g: "", keys: ["app.predictor_enabled", "predictor.*", "schedule.predictor_against_runs"] },
  ] },
  { id: "sentinel", t: "哨兵", strip: ["哨兵"], what: "盯行情，发现信号就交给拍板人", groups: [
    { g: "大盘翻转", hint: "默认值是观察起点，尚无数据支持。满足条件时只调用一次市场模型，更新当前大盘背景；定时整轮和单币临时会仍按原节奏执行。门槛和间隔填 0 可关闭对应限制。", keys: ["fast_loop.regime_flip_on", "fast_loop.regime_flip_live", "fast_loop.regime_flip_breadth", "fast_loop.regime_flip_btc_pct", "fast_loop.regime_flip_confirm", "fast_loop.regime_flip_min_gap_min", "fast_loop.regime_flip_min_symbols", "fast_loop.regime_flip_check_min", "fast_loop.regime_flip_suspect_log_min", "fast_loop.regime_flip_price_max_age_min"] },
    { g: "信号识别", keys: ["fast_loop.volume_surge_live", "fast_loop.breakout_vol_ratio", "fast_loop.surge_vol_ratio", "fast_loop.surge_taker_ratio", "fast_loop.liq_min_usdt_1m", "fast_loop.liq_ratio", "fast_loop.liq_drop_pct_5m", "fast_loop.crowded_funding_rate", "fast_loop.session_window_min", "fast_loop.session_vol_ratio", "fast_loop.session_cme_open", "fast_loop.session_cme_open_live"] },
    { g: "持仓量变化", keys: ["fast_loop.oi_follow_on", "fast_loop.oi_follow_live", "fast_loop.oi_window_min", "fast_loop.oi_min_pct", "fast_loop.oi_price_z", "fast_loop.oi_*"] },
    { g: "大户散户分歧（反着大户）", keys: ["fast_loop.top_crowd_on", "fast_loop.top_crowd_live", "fast_loop.top_crowd_window_min", "fast_loop.top_crowd_min_pct", "fast_loop.top_crowd_*"] },
    { g: "BTC 先动，小币跟", keys: ["fast_loop.btc_lead_on", "fast_loop.btc_lead_live", "fast_loop.btc_lead_z", "fast_loop.btc_lead_*"] },
    { g: "去重和过期", keys: ["app.signal_dedup_s", "fast_loop.signal_max_age_s"] },
    { g: "看到和作战卡反向的信号", keys: ["fast_loop.against_*"] },
  ] },
  { id: "gate", t: "拍板人 Jev", strip: ["拍板人"], what: "每个信号问一遍：做不做", groups: [
    { g: "门槛", warn: "gate_cautions", keys: ["fast_loop.min_p_take", "fast_loop.predictor_weight", "fast_loop.gate_record_only", "fast_loop.gate_questions", "fast_loop.entry_zone_only"] },
    { g: "按信号类型单独定门槛", prefix: "take_by_kind.", hint: "某类信号单独用一个「做」的门槛，没写的类型跟上面的总门槛。填 0 = 这类信号拍板人照常问、照常记，「做」的概率不再拦；持仓笔数、同币持仓、敞口上限、每笔风险、止损、信号过期、作战卡、日亏熔断照旧。同一次 BTC 大动会让多个小币同时出「BTC 先动」，这些仓由持仓笔数上限、总敞口和下面「按信号类型限制同时持仓」封顶。" },
    { g: "按信号类型限制同时持仓", prefix: "open_by_kind.", hint: "某类信号开的在管持仓（含开仓中、平仓中）到了这个数，同类新信号就拒绝；0 = 这类信号不开仓；留空 = 不单独限（仍受持仓笔数和敞口上限约束）。升级前开的仓记不得类型，不计入。" },
    { g: "超时、出错和同时判几个", keys: ["app.jev_timeout_ms", "app.jev_max_concurrent", "fast_loop.jev_max_consecutive_errors", "fast_loop.jev_error_cooldown_s", "app.jev_shared_cooldown", "fast_loop.rule_gate_without_jev"] },
    { g: "通道和价格", keys: ["fast_loop.jev_usd_per_mtok", "fast_loop.jev_provider", "fast_loop.jev_model"] },
  ] },
  { id: "risk_guard", t: "风控员", strip: ["风控员", "保镖"], what: "下单前最后一道关：仓位多大、门槛、止损止盈放哪。上限都是你定的，AI 改不了", groups: [
    { g: "最要紧的三项（和页面上面「风控硬上限」是同一份）", keys: CORE },
    { g: "仓位上限", keys: ["risk.max_total_exposure_x", "risk.max_notional_per_trade_x", "risk.max_notional_per_trade_usdt", "risk.max_open_positions"] },
    { g: "小仓、中仓用几成", hint: "拍板人给的仓位档：小仓、中仓按正常仓位打折，大仓用满。", keys: ["fast_loop.size_fraction_s", "fast_loop.size_fraction_m"] },
    { g: "每类币合计仓位上限（这一类所有仓位的名义价值加起来，含杠杆）", cat: true, hint: "不是杠杆：每笔杠杆统一用「最多开几倍杠杆」那一项。金额按现在的本金实时折算，设置里填的是本金的几倍，本金变了金额跟着变。同一类币常常一起涨跌，这样一类出事不会亏太多。每类不能超过上面的所有仓位合计上限。" },
    { g: "下单门槛", keys: ["risk.max_spread_bps", "risk.max_slippage_bps", "risk.taker_fee_bps", "risk.min_tp_fee_multiple", "fast_loop.max_spread_stop_frac", "fast_loop.max_slippage_stop_frac", "fast_loop.min_stop_fee_multiple", "fast_loop.stop_floor_widen", "fast_loop.funding_against_max_bp", "fast_loop.new_listing_block_h", "fast_loop.adl_high_block"] },
    { g: "进场方式", keys: ["fast_loop.entry_limit_pullback", "fast_loop.entry_limit_pullback_live", "fast_loop.entry_limit_offset_stop", "fast_loop.entry_limit_wait_min", "fast_loop.entry_limit_chase", "fast_loop.chase_price_max_age_s"] },
    { g: "仓位太大时缩单", keys: ["fast_loop.book_depth_levels", "fast_loop.max_notional_book_frac", "fast_loop.margin_safety_pct", "fast_loop.card_caps_binding", "fast_loop.leverage_clamp_to_bracket"] },
    { g: "哪些币先不做", keys: ["fast_loop.us_hours_gate", "fast_loop.us_open_delay_min", "fast_loop.us_close_lead_min", "fast_loop.new_listing_*", "fast_loop.adl_*"] },
    { g: "止损放多远", hint: "带「跟最长持仓走」的改单笔最长持仓时自动换。", keys: ["fast_loop.atr_period_min", "fast_loop.atr_long_period_min", "fast_loop.atr_floor_7d", "fast_loop.session_factor", "fast_loop.session_factor_weekday", "fast_loop.session_factor_weekend", "fast_loop.min_stop_atr", "fast_loop.max_stop_atr", "fast_loop.stop_level_snap", "fast_loop.stop_level_buffer_atr"] },
    { g: "止盈放多远", keys: ["fast_loop.min_tp_r", "fast_loop.max_tp_r", "fast_loop.card_range_clamp"] },
    { g: "一笔最多拿多久", keys: ["fast_loop.time_limit_s"] },
  ] },
  { id: "executor", t: "执行", what: "下单、挂单、限价止盈", groups: [
    { g: "限价止盈", keys: ["fast_loop.limit_take_profit", "fast_loop.limit_take_profit_live", "fast_loop.tp_limit_fallback_s", "fast_loop.tp_limit_fallback_through_bps"] },
    { g: "进场挂单", keys: ["fast_loop.entry_limit_*"] },
    { g: "开仓前读账户", hint: "拍板通过后读 hummingbot 账户失败：再读几次都不行就放弃这个信号，风控员记一条拒绝。热生效。", keys: ["app.account_read_retries"] },
    { g: "托管保护", hint: "Demo 新仓在币安挂的保命单和止盈止损。除挂单顺序和平仓顺序外只在配置文件改；都要重启才生效；秒数 0、触发价 CONTRACT_PRICE、顺序 lifeboat_first、平仓 hb_first = 原行为。平仓顺序 direct：决定平仓时先直连只减仓市价单、再只停不平 HB，交易所止损挂到确认零仓后才撤。挂单顺序 stop_first：先挂交易所止损和止盈、最后挂保命单，止损确认后保命单挂失败不立即平仓，在未托管宽限内告警并重试（宽限 0 就照旧平仓）。保命单固定按标记价触发；改触发价只影响之后新挂的止损 / 止盈，已挂的单不动。", keys: ["app.protection_*", "app.hosted_close_mode", "app.position_mode_cache_s"] },
  ] },
  { id: "keeper", t: "持仓管家", strip: ["管家"], what: "隔一会儿看一次持仓，该平就平", groups: [
    { g: "", keys: ["app.keeper_interval_s", "fast_loop.keeper_close_p", "fast_loop.keeper_confirmations", "fast_loop.keeper_close_p_strong", "fast_loop.keeper_price_max_age_s", "fast_loop.keeper_model_mode", "fast_loop.keeper_price_source", "fast_loop.keeper_close_guard", "fast_loop.keeper_close_guard_replacing_max_s", "fast_loop.keeper_close_guard_exiting_max_s", "fast_loop.keeper_close_guard_total_max_s", "fast_loop.keeper_venue_alert_reviews"] },
    { g: "Jev 盈利门控", hint: "代码规则 + Jev 的一道题：赚到一定倍数止损距离、Jev 又说进场理由不成立（回答够新），就全平。只平不放宽。", keys: ["fast_loop.keeper_thesis_take_on", "fast_loop.keeper_thesis_take_min_r", "fast_loop.keeper_thesis_take_p", "fast_loop.keeper_thesis_take_max_age_s"] },
    { g: "冻结盈利价回访", hint: "默认开启，满 60 分钟冻结此前同 Demo 可成交报价中已观察到的最高盈利价；之后再次到价或穿过时管家请求全平。仅指管家实际采样看到的最高盈利价；断线或采样间隔可能漏掉更高点。冻结时间需短于该仓最长持仓，否则通常在冻结前已退出。扣实际开仓费和预计平仓费后须盈利；证据缺失不触发。开关和时间只影响之后新开的 Demo 仓，已有仓保留原设置，实盘和旧测试网不启用。证据读法默认「每次复查当场读」：复查要等交易所（最多 12 秒），一轮复查也要等最慢那笔。改成「后台按仓读」后，每笔仓按间隔在后台读，复查只用不超过「不算」秒数的结果，太旧这次就不看这条规则；一轮复查也不再等最慢那笔。", keys: ["fast_loop.frozen_peak_exit_on", "fast_loop.frozen_peak_exit_after_s", "fast_loop.frozen_peak_evidence_mode", "fast_loop.frozen_peak_evidence_interval_s", "fast_loop.frozen_peak_evidence_max_age_s"] },
    { g: "回吐保护", hint: "代码规则，不问模型：最高浮盈到过「开始看」那么多之后，从最高点吐掉这个比例就全平。", keys: ["fast_loop.giveback_on", "fast_loop.giveback_close_pct", "fast_loop.giveback_arm_r"] },
    { g: "新作战卡方向反了", hint: "代码规则，不问模型（#314）。close = 立刻全平（原规则）。confirm_or_tighten = 价格确认（三条任一）才平；没确认时亏着或赚得不够（扣费保本价离标记价不够远，约要赚 25bp 以上）照旧全平，赚得够的按「怎么收紧」：trail 在现价强制启动移动止损（之后只收紧，可能低于开仓价）、breakeven 止损挪到扣费保本；都不会比正在挂的止损松。卡还有效时之后确认了再平；旧通道的仓、从没完整托管的仓照旧全平。收紧后卡转回来不会自动放松。", keys: ["fast_loop.opposite_card_action", "fast_loop.opposite_card_ab", "fast_loop.opposite_confirm_loss_r", "fast_loop.opposite_confirm_atr", "fast_loop.opposite_confirm_key_level", "fast_loop.opposite_tighten_buffer_bps", "fast_loop.opposite_tighten_mark_gap_bps", "fast_loop.opposite_profit_mode", "fast_loop.opposite_trail_distance_mult", "fast_loop.opposite_anchor_retry_s"] },
    { g: "新作战卡把这个币改成中性", hint: "代码规则，不问模型，不做价格确认（中性不是反向）。只看开仓之后发布的新卡：这个币改成 no_trade，或到手即作废（stale_on_arrival）成只看不做，都算中性；别的原因的只看不做不算；卡里没有这个币只在打开「没有也算」时算。hold = 不管（现状）。tighten = 和「新作战卡方向反了」没确认时同一套：亏着或赚得不够照旧全平，赚得够的在第一次看到时的现价强制启动移动止损（只锚一次，重启、换卡都不重锚，不会比正在挂的止损松），保本缓冲、离标记价多远、插针重试秒数都用上面那组。close = 直接全平。管家行情没有或过旧（「管家复查时行情最多旧几秒」）时一律不平：tighten 只收紧，close 等新行情。之后卡转成反向，交给上面那条规则。", keys: ["fast_loop.card_neutral_action", "fast_loop.card_neutral_on_absent", "fast_loop.card_neutral_trail_distance_mult"] },
    { g: "赚了之后别变亏", hint: "移动止损默认开，保本默认关。数字的单位是止损距离（1 = 一个止损距离），持仓页「止损价」离开仓价多远就是一个止损距离。", keys: ["fast_loop.trailing_on", "fast_loop.trail_start_r", "fast_loop.trail_distance_r", "fast_loop.breakeven_on", "fast_loop.breakeven_trigger_r"] },
  ] },
  { id: "patrol", t: "巡逻兵", strip: ["巡逻兵"], what: "每分钟看一眼，持仓被急拉急砸时报警或全平", groups: [
    { g: "", keys: ["fast_loop.patrol_alert_pct_1m", "fast_loop.patrol_shock_pct_1m", "fast_loop.patrol_alert_stop_mult", "fast_loop.patrol_shock_stop_mult"] },
  ] },
  { id: "reconciler", t: "对账员", strip: ["对账员"], what: "定时和交易所对账，账本和币安对不上时处理", groups: [
    { g: "", keys: ["app.reconcile_interval_s", "app.reconcile_opening_grace_s", "app.reconcile_closing_grace_s", "app.protection_stale_s", "app.reconciler_close_unknown_positions"] },
  ] },
  { id: "lifeboat", t: "保命止损", what: "在交易所挂的兜底止损，程序挂了也能止损", groups: [
    { g: "", keys: ["risk.lifeboat_stop_multiplier"] },
  ] },
  { id: "alerting", t: "告警推送", what: "往 Telegram 推哪些消息（每一类单独开关，默认全开）", find: "告警 通知 提醒 推送 消息 Telegram 电报 开仓成交 平仓完成 静音 关闭提醒 保命止损 对账 额度 notify", groups: [
    { g: "", hint: "Telegram 的密钥在「密钥」里配。关掉只是不往 Telegram 推，面板「历史」和告警列表照样有记录；写「建议保持开」的是出了问题的安全告警。面板切模式、测试消息、看门狗这几条不走这里，一直推。", keys: ["app.notify_open_filled", "app.notify_close_done", "app.notify_lifeboat_hit", "app.notify_lifeboat_missing", "app.notify_order_failed", "app.notify_reconcile", "app.notify_news_block", "app.notify_model_credit", "app.credit_low_floor_usd", "app.credit_resend_hours", "app.notify_meeting_paused", "app.notify_spend_alert", "app.notify_control", "app.notify_live_started", "app.notify_system"] },
  ] },
  { id: "keys", t: "密钥", what: "币安、OpenRouter、Telegram 等的 key，只显示配没配", find: "密钥 key api 币安 binance openrouter telegram jev typesafe 测试网" },
  { id: "show", t: "显示", what: "成交提示音、涨跌颜色、面板密码、时区等", find: "显示 币安风格成交提示音 成交提示音 声音 音量 下单声音 平单声音 提示音 涨跌颜色 绿涨红跌 红涨绿跌 面板密码 时区 今天 模拟盘环境 数据文件夹 day_reset_tz sim_environment data_dir" },
];
// 别处已经显示的键（运行卡、显示里的系统信息），不再进“其他”
// 真 / 毒 / 方向三个门槛只在旧的 6 道题问法下生效；burst_* 10 秒提前触发已取消（Aaron 10-24）：都不显示
const SHOWN_ELSEWHERE = ["app.trade_notices", "app.profile", "app.meeting_interval", "app.hold", "app.data_dir", "app.sim_environment", "risk.day_reset_tz", "fast_loop.min_p_genuine", "fast_loop.min_p_direction", "fast_loop.max_p_toxic", "fast_loop.burst_10s_on", "fast_loop.burst_10s_ratio", "fast_loop.burst_min_1m_ratio", "fast_loop.predictor_nudge_mode"];
// 没点名的新键先按键名前缀归角色（新加的门槛、信号开关不用改这张表也能落对地方），再按所在段兜底
const PREFIX_ROLE = [
  ["fast_loop.market_review_", "commander"],
  ["fast_loop.regime_flip_", "sentinel"],
  ["fast_loop.against_", "sentinel"], ["fast_loop.signal_", "sentinel"], ["fast_loop.surge_", "sentinel"], ["fast_loop.breakout_", "sentinel"], ["fast_loop.burst_", "sentinel"],
  ["fast_loop.liq_", "sentinel"], ["fast_loop.crowded_", "sentinel"], ["fast_loop.session_cme", "sentinel"], ["fast_loop.session_vol", "sentinel"], ["fast_loop.session_window", "sentinel"],
  ["fast_loop.oi_", "sentinel"], ["fast_loop.top_crowd_", "sentinel"], ["fast_loop.btc_lead_", "sentinel"], ["fast_loop.volume_", "sentinel"],
  ["fast_loop.min_p_", "gate"], ["fast_loop.max_p_", "gate"], ["fast_loop.jev_", "gate"], ["fast_loop.gate_", "gate"], ["app.jev_", "gate"],
  ["fast_loop.keeper_", "keeper"], ["app.keeper_", "keeper"], ["fast_loop.patrol_", "patrol"], ["app.reconcil", "reconciler"],
  ["fast_loop.limit_take_", "executor"], ["fast_loop.tp_limit_", "executor"], ["fast_loop.entry_limit_", "executor"],
  ["app.predictor_", "predictor"], ["app.screener_", "screener"],
];
const SECTION_ROLE = { risk: "risk_guard", category: "risk_guard", take_by_kind: "gate", open_by_kind: "gate", fast_loop: "risk_guard", app: "executor", schedule: "commander", budget: "ai_cost", predictor: "predictor", screener: "screener", tiers: "screener", meeting: "commander" };
const UNIT = {
  risk_per_trade_pct: "%", daily_loss_halt_pct: "%", max_leverage: " 倍", max_total_exposure_x: " 倍", max_notional_per_trade_x: " 倍",
  max_notional_per_trade_usdt: " U", max_open_positions: " 个", lifeboat_stop_multiplier: " 倍", min_tp_fee_multiple: " 倍",
  min_tp_r: " 倍", max_tp_r: " 倍", keeper_confirmations: " 次", keeper_thesis_take_min_r: " R", keeper_thesis_take_max_age_s: " 秒", keeper_close_guard_replacing_max_s: " 秒", keeper_close_guard_exiting_max_s: " 秒", keeper_close_guard_total_max_s: " 秒", keeper_venue_alert_reviews: " 次", book_depth_levels: " 档", giveback_close_pct: "%", giveback_arm_r: " 倍", opposite_confirm_loss_r: " R", opposite_confirm_atr: " 倍 ATR", opposite_tighten_buffer_bps: " bp", opposite_tighten_mark_gap_bps: " bp", opposite_trail_distance_mult: " 倍", card_neutral_trail_distance_mult: " 倍", opposite_anchor_retry_s: " 秒", patrol_alert_pct_1m: "%", patrol_shock_pct_1m: "%",
  jev_max_consecutive_errors: " 次", account_read_retries: " 次", breakout_vol_ratio: " 倍", surge_vol_ratio: " 倍", liq_min_usdt_1m: " U", liq_ratio: " 倍",
  liq_drop_pct_5m: "%", session_vol_ratio: " 倍", pool_size: " 个", card_pick_max: " 个", max_cards_per_round: " 个", concurrency: " 个", event_concurrency: " 场",
  adaptive_base_max_queue: " 个", adaptive_mini_min_queue: " 个", backfill_bars: " 根", lookback: " 根", paths: " 条",
  min_quote_volume_usdt: " U", min_depth_usdt: " U", min_listed_days: " 天", min_range_24h_pct: "%", max_range_24h_pct: "%",
  head: " 个", mid: " 个", mid_daily_pick: " 个", daily_cost_limit_usd: " 美元",
  default_category_cap: " 个", vol_full_multiple: " 倍", surge_full_multiple: " 倍", oi_full_pct: "%", liq_full_usdt: " U",
};
// 推荐值里只有这几项是“越小越冒险”
const LOWER_RISKIER = ["min_tp_fee_multiple", "min_quote_volume_usdt", "min_depth_usdt", "min_listed_days", "min_range_24h_pct"];
// 按本金几倍设的上限：金额为主、倍数为辅（金额按现在的本金折算），免得和杠杆倍数混
const EQ_X = ["max_total_exposure_x", "max_notional_per_trade_x"];
const eqBasis = () => (pageData && pageData.recommend_basis_usdt) || null;
const eqX = (i) => i.cat || (i.sec === "risk" && EQ_X.includes(i.key));
const eqAmt = (x) => (eqBasis() ? `≈${money(x * eqBasis(), 0)} U` : "");
const valOf = (i, v = i.value) => (eqX(i) && typeof v === "number" && eqBasis() ? `${eqAmt(v)}（本金 ${+v.toFixed(2)} 倍）` : valRaw(i, v));
const valRaw = (i, v = i.value) => offShown(i, v) || fmtVal(v >= 1e5 && v % 1e4 === 0 && i.key.endsWith("_usdt") ? `${v / 1e4} 万` : v, i) + (typeof v === "number" && !durOf(i.key, v) && !(i.choices && i.choices.length) ? UNIT[i.cat ? "max_total_exposure_x" : i.key] || "" : "");
const hot = (i) => i.recommended != null && typeof i.value === "number" && (LOWER_RISKIER.includes(i.key) ? i.value < i.recommended : i.value > i.recommended || (i.value === 0 && !!i.off));

// 段.键 → 设置项（每类币上限拼成 category 段）
function settingIndex(d) {
  const ix = {};
  for (const sec of ["risk", "app", "fast_loop", "predictor", "schedule", "budget", "screener", "tiers", "meeting"]) for (const i of d[sec] || []) ix[`${sec}.${i.key}`] = { ...i, sec };
  const tot = (d.risk.find((i) => i.key === "max_total_exposure_x") || {}).value;
  for (const [k, v] of Object.entries(d.category_caps)) ix[`category.${k}`] = { key: k, sec: "category", cat: true, label: `${CAT[k] || k}合计最多`, value: v, bounds: tot == null ? "" : `本金的 0–${tot} 倍（不超过所有仓位合计上限）`, editable: true, choices: [] };
  const take = (d.fast_loop.find((i) => i.key === "min_p_take") || {}).value;
  for (const [k, v] of Object.entries(d.take_by_kind || {})) ix[`take_by_kind.${k}`] = { key: k, sec: "take_by_kind", label: `${SIGNAL[k] || k}：做的把握至少`, value: v, bounds: `0–1；留空 = 跟总门槛${take == null ? "" : `（现在 ${take}）`}`, editable: true, nullable: true, choices: [] };
  for (const [k, v] of Object.entries(d.open_by_kind || {})) ix[`open_by_kind.${k}`] = { key: k, sec: "open_by_kind", label: `${SIGNAL[k] || k}：最多同时几笔`, value: v, bounds: "0–100；0 = 这类信号不开仓；留空 = 不单独限", editable: true, nullable: true, choices: [] };
  return ix;
}
// 把每个角色展开成 {角色 id: [{组名, 提示, 项…}]}；没列到的键收进对应角色的“其他”
function roleGroups(d, ix) {
  const used = new Set(SHOWN_ELSEWHERE);
  // 「…*」只收别处没点名的键，点名的归到点名的那一组
  const named = new Set(ROLES.flatMap((r) => (r.groups || []).flatMap((g) => g.keys || [])).filter((k) => !k.endsWith("*")));
  const expand = (k) => (k.endsWith("*") ? Object.keys(ix).filter((x) => x.startsWith(k.slice(0, -1)) && !named.has(x) && !used.has(x)) : ix[k] ? [k] : []);
  const out = {};
  for (const role of ROLES) {
    if (!role.groups) continue;
    out[role.id] = role.groups.map((g) => {
      const keys = g.cat ? Object.keys(ix).filter((x) => x.startsWith("category.")) : g.prefix ? Object.keys(ix).filter((x) => x.startsWith(g.prefix)) : g.keys.flatMap(expand);
      keys.forEach((k) => used.add(k));
      return { g: g.g, hint: g.hint, extra: g.extra, warn: g.warn ? d[g.warn] || [] : [], items: keys.map((k) => ix[k]) };
    });
  }
  const rest = Object.keys(ix).filter((k) => !used.has(k));
  const roleOf = (k) => ((PREFIX_ROLE.find(([pre]) => k.startsWith(pre)) || [])[1]) || SECTION_ROLE[ix[k].sec] || "risk_guard";
  for (const id of new Set(rest.map(roleOf))) out[id].push({ g: "其他", items: rest.filter((k) => roleOf(k) === id).map((k) => ix[k]) });
  return out;
}

S.stOpen = new Set();
S.stQ = "";
try { S.roleOpen = new Set(JSON.parse(localStorage.getItem("settings-roles") || "[]")); } catch (err) { S.roleOpen = new Set(); }
const saveRoles = () => { try { localStorage.setItem("settings-roles", JSON.stringify([...S.roleOpen])); } catch (err) { /* 记不住就每次都收起 */ } };
function toggleRole(id) {
  if (S.roleOpen.has(id)) S.roleOpen.delete(id);
  else S.roleOpen.add(id);
  saveRoles();
  const sec = $(`#st-r-${id}`);
  if (!sec || S.stQ) return render();
  sec.classList.toggle("open", S.roleOpen.has(id));
  sec.querySelector(".role-h").setAttribute("aria-expanded", S.roleOpen.has(id));
}
// 别处点进来（首屏按钮、团队页、#settings/角色）：展开那一块并滚过去
function openRole(id) {
  S.roleOpen.add(id);
  saveRoles();
  S.stQ = "";
  render();
  const sec = $(`#st-r-${id}`);
  if (sec) sec.scrollIntoView({ behavior: "smooth", block: "start" });
}

const lockedPaper = (i) => i.paper_only && pageData.mode !== "paper";
function auxOf(i) {
  const moot = mootOf(i, settingIndex(pageData));
  if (moot) return `<span class="dim">${moot}</span>`;
  if (lockedPaper(i)) return i.value === true ? '<span class="warn">只在模拟盘生效，已忽略</span>' : "只有模拟盘能开";
  if (!i.editable) return "网页不改";
  if (i.recommended != null) return `推荐 <b>${valOf(i, i.recommended)}</b>`;
  const by = i.sec === "fast_loop" ? "最长持仓" : "档位";
  if (i.pinned) return `<span class="warn">手动固定（${by}默认是 ${valOf(i, i.profile_default)}）</span>`;
  return i.profiled ? `跟${by}走` : "";
}
// 时间类的值已经换成“1 小时 40 分”，标题里的“几秒 / 几分钟”就改说“多久”
const nameOf = (i) => (durOf(i.key, i.value) ? label(i).replace(/多少毫秒|几毫秒|几秒|几分钟|几小时/, "多久") : label(i));
// 数字数组（时段系数 24 格、退避分钟等）：摘要行只写概况，数值在下面排成小格子，不挤坏标题
const numArr = (v) => Array.isArray(v) && v.length > 0 && v.every((x) => typeof x === "number");
function arrSummary(v) {
  if (v.length !== 24) return `${v.length} 个`;
  const hi = v.indexOf(Math.max(...v)), lo = v.indexOf(Math.min(...v));
  return `最高 ${v[hi]}（${hi} 点）· 最低 ${v[lo]}（${lo} 点）`;
}
function arrCells(v) {
  if (v.length !== 24) return `<div class="arr-chips">${v.map((x) => `<span>${esc(x)}</span>`).join("")}</div>`;
  return `<div class="hgrid">${v.map((x, h) => `<div class="${x > 1 ? "hot" : ""}"><small>${h} 点</small>${esc(x)}</div>`).join("")}</div><div class="dim small">曼谷时间每小时一个系数；高于 1 标浅色 = 这个钟点止损放宽。</div>`;
}
// 行名开头是所在角色的名字就去掉（「拍板人多久不回答就放弃这笔」在拍板人那块里显示「多久不回答就放弃这笔」）
const stripRole = (name, strip) => {
  const p = (strip || []).find((x) => name.startsWith(x) && name.length > x.length);
  return p ? name.slice(p.length) : name;
};
function itemRow(i, big, strip) {
  const id = `${i.sec}.${i.key}`;
  const open = S.stOpen.has(id);
  const w = pageData.writable && i.editable && !(lockedPaper(i) && i.value !== true);  // 非模拟盘：开着的还能关
  const desc = techHint(i);
  const acts = [
    w ? `<button class="btn b-sm" data-act="edit1" data-arg="${esc(id)}">修改</button>` : "",
    w && i.recommended != null && i.value !== i.recommended ? `<button class="btn b-sm" data-act="rec1" data-arg="${esc(id)}">用推荐值</button>` : "",
    w && i.pinned ? `<button class="btn b-sm" ${clickable("unpin", i.sec + ":" + i.key)}>恢复档位默认</button>` : "",
    w && i.resettable ? `<button class="btn b-sm" ${clickable("unpin", i.sec + ":" + i.key)}>恢复默认</button>` : "",
    `<span class="st-key">${esc(i.key)}</span>`,
  ].join("");
  return `<div class="st-item ${big ? "big" : ""} ${open ? "open" : ""} ${i.editable ? "" : "ro"}">
    <button class="st-sum${numArr(i.value) ? " arr" : ""}" data-act="st-item" data-arg="${esc(id)}" aria-expanded="${open}" title="${esc(i.key)}"><span class="nm">${esc(i.cat ? i.label : stripRole(nameOf(i), strip))}</span><span class="cur num ${hot(i) ? "warn" : ""}">${numArr(i.value) ? arrSummary(i.value) : valOf(i)}</span><span class="aux">${auxOf(i)}</span><span class="chev">›</span></button>
    ${numArr(i.value) ? `<div class="st-arr">${arrCells(i.value)}</div>` : ""}<div class="st-body">${desc ? `<div>${desc}</div>` : ""}<div class="st-acts">${acts}</div></div></div>`;
}
function toggleItem(el, id) {
  const it = el.closest(".st-item");
  if (S.stOpen.has(id)) S.stOpen.delete(id);
  else S.stOpen.add(id);
  it.classList.toggle("open", S.stOpen.has(id));
  el.setAttribute("aria-expanded", S.stOpen.has(id));
}

// 现在算主流币的名单（data/majors.json + 手动加入 / 排除）
function majorsBox(m) {
  if (!m) return "";
  const when = m.fetched_at ? `${md(m.fetched_at)} ${hm(m.fetched_at)} 更新` : "";
  const head = m.fallback
    ? (m.top_n > 0 ? "还没拉到市值排名，先用固定 7 个" : "只用固定 7 个")
    : `市值前 ${m.fetched_top_n ?? m.top_n} 名 · ${when}${m.fetched_top_n != null && m.fetched_top_n !== m.top_n ? `（改成前 ${m.top_n} 名后，下一次选币重新拉）` : ""}`;
  const chip = (r) => `<span class="mj" title="${esc(r.why)}">${esc(r.name)}<small>${r.rank != null ? `#${r.rank}` : r.why === "你手动加入" ? "手动" : ""}</small></span>`;
  return `<div class="st-majors"><div class="dim small">现在算主流的 ${m.rows.length} 个：${esc(head)}</div><div class="mj-list">${m.rows.map(chip).join("")}</div>${m.excluded.length ? `<div class="dim small">你排除的（按山寨币算）：${esc(m.excluded.join(" "))}</div>` : ""}</div>`;
}

// 改一项：一个输入框 + 面板密码；段按键自动带
function editOne(id, useRec) {
  const d = pageData;
  const i = d && settingIndex(d)[id];
  if (!i || !d.writable || !i.editable) return;
  let input;
  if (i.names) input = `<input id="dlg-v" type="text" autocapitalize="characters" value="${esc((i.value || []).join(" "))}" placeholder="币名空格隔开，如 LINK AVAX；留空 = 没有">`;
  else if (i.multi) input = `<span id="dlg-v" class="multi">${i.choices.map((c) => `<label class="small"><input type="checkbox" value="${esc(c)}" ${(i.value || []).includes(c) ? "checked" : ""}> ${esc(choiceLabel(i.key, c))}</label>`).join("")}</span>`;
  else if (typeof i.value === "boolean") input = `<select id="dlg-v"><option value="true" ${i.value ? "selected" : ""}>开</option><option value="false" ${i.value ? "" : "selected"}>关</option></select>`;
  else if (i.choices && i.choices.length) {
    const fit = (c) => (i.key === "atr_period_min" && ATR_FIT[c] ? ` · 适合最长持仓 ${ATR_FIT[c][0]}` : "");
    input = `<select id="dlg-v">${i.choices.map((c) => `<option value="${esc(JSON.stringify(c))}" ${c === i.value ? "selected" : ""}>${esc(choiceLabel(i.key, c) + fit(c))}</option>`).join("")}</select>`;
  } else if (i.pattern) input = `<input id="dlg-v" type="text" value="${esc(i.pinned || !i.profiled ? i.value || "" : "")}" placeholder="${esc(i.key === "schedule_at" ? "如 08:00；留空 = " + (i.profiled ? `跟档位（${fmtVal(i.pinned ? i.profile_default : i.value, i)}）` : "不定点") : "")}">`;
  else if (i.key === "frozen_peak_exit_after_s") input = `<input id="dlg-v" type="number" step="1" min="1" max="10080" inputmode="numeric" value="${esc(i.value / 60)}"> 分钟`;
  else if (i.key === "time_limit_s") input = `<input id="dlg-v" type="number" step="0.25" min="0.25" inputmode="decimal" value="${esc(useRec ? 2 : i.value / 3600)}"> 小时`;
  else {
    // 档位给默认的键：没写死时留空（= 跟档位走），写死了才填文件里的值
    const shown = useRec ? i.recommended : i.value == null || (i.profiled && !i.pinned) ? "" : i.value;
    const ph = i.profiled ? `留空 = 跟${i.sec === "fast_loop" ? "最长持仓" : "档位"}（${fmtVal(i.pinned ? i.profile_default : i.value, i)}）` : i.nullable ? "留空 = 用默认" : "";
    input = `<input id="dlg-v" type="number" step="any" inputmode="decimal" value="${esc(shown)}" placeholder="${esc(ph)}">`
    input += offSwitchHtml(i, useRec);  // 能填 0 = 关闭 / 不限的项，旁边给开关
  }
  const now = [`现在 ${valOf(i)}`, i.recommended != null ? `推荐 ${valOf(i, i.recommended)}（${esc(i.recommended_why)}）` : "", i.pinned ? `${i.sec === "fast_loop" ? "最长持仓" : "档位"}默认 ${valOf(i, i.profile_default)}` : i.profiled ? `现在跟${i.sec === "fast_loop" ? "最长持仓" : "档位"}走` : "", i.key === "frozen_peak_exit_after_s" ? "范围 1–10080 分钟" : i.bounds ? `范围 ${esc(i.bounds)}` : ""].filter(Boolean).join("，");
  ask(esc(i.cat ? i.label : nameOf(i)), `<p class="dim">${now}。</p>${i.multi ? `<div class="pw">勾选${input}</div>` : `<label class="pw">改成${eqX(i) ? "（填本金的几倍）" : ""}${input}</label>`}${pwField(data.control)}
    ${i.key === "atr_period_min" || i.key === "time_limit_s" ? '<div class="small warn" id="dlg-fit"></div>' : ""}
    ${eqX(i) && eqBasis() ? `<div class="small" id="dlg-amt"></div>` : ""}
    <div class="dim small">保存前整份检查，不合理会说原因；写前留备份。${isHot(i.sec, i.key) ? "保存后几秒内生效，不用重启。" : "改完点「重启生效」才用上。"}</div>`, "保存", false, async () => {
    const el = $("#dlg-v");
    let v;
    if (offPicked(i)) v = 0;
    else if (i.names) {
      v = [...new Set(el.value.toUpperCase().split(/[\s,，、]+/).filter(Boolean))];
      if (JSON.stringify(v) === JSON.stringify(i.value || [])) throw new Error("没有改动");
    } else if (i.multi) {
      v = [...el.querySelectorAll("input:checked")].map((x) => x.value);
      if (JSON.stringify(v) === JSON.stringify(i.value) && !(i.profiled && !i.pinned)) throw new Error("没有改动");
    } else if (el.tagName === "SELECT") v = JSON.parse(el.value);
    else if (i.pattern) {
      v = el.value.trim() || null;
      if (v === null && !i.nullable && !i.profiled) throw new Error("不能空着");
      if (v === null && i.profiled && !i.pinned) throw new Error("没有改动");
      if (v !== null && !new RegExp(i.pattern).test(v)) throw new Error("格式不对，例如 08:00");
    }
    else if (el.value.trim() === "") {
      if (!i.nullable && !i.profiled) throw new Error("不能空着");
      if (i.profiled && !i.pinned) throw new Error("没有改动");
      v = null;
    } else {
      v = Number(el.value);
      if (!Number.isFinite(v)) throw new Error("要填数字");
      if (i.key === "frozen_peak_exit_after_s") {
        if (!Number.isInteger(v) || v < 1 || v > 10080) throw new Error("请输入 1–10080 之间的整分钟数");
        v *= 60;
      }
      if (i.key === "time_limit_s") v = Math.round(v * 3600);
    }
    // 跟档的项填了和档位一样的值：不用自定义（写进文件反而以后换档不跟）
    const holdMine = i.key === "hold" && d.profile && d.profile.hold_custom;
    if (i.profiled && !i.pinned && !holdMine && v !== null && sameV(v, i.value)) throw new Error(`和档位的值一样（${valOf(i)}），不用自定义`);
    // 档位键填了数就算写死（哪怕和档位一样），其余和原值一样算没改
    if (!i.multi && !i.names && v === i.value && !(i.profiled && !i.pinned && v !== null)) throw new Error("没有改动");
    const pw = $("#dlg-pw");
    const res = await post("/api/settings", { section: i.sec, changes: { [i.key]: v }, password: pw ? pw.value : "" });
    onOk = null;
    $("#layer [data-ok]").remove();
    $("#layer [data-close].btn").textContent = "关闭";
    const x = res.changed[0];
    dlgMsg(`<b class="up">已保存</b>${x ? `：${valOf(i, x.old)} → ${valOf(i, x.new)}` : ""}。${isHot(i.sec, i.key) ? "几秒内生效，不用重启。" : "点「重启生效」后用上。"}`);
    poll();
  });
  offBind(i);
  // 填的是本金几倍，旁边实时写折合多少 U
  const amtEl = $("#dlg-amt");
  if (amtEl) {
    const show = () => {
      const raw = $("#dlg-v").value.trim();
      const x = raw === "" ? i.value : Number(raw);
      amtEl.textContent = Number.isFinite(x) ? `填的是本金的几倍：${+x.toFixed(2)} 倍 = 合计最多 ${eqAmt(x)}（按现在本金 ${money(eqBasis(), 0)} U 算，不是杠杆）` : "";
    };
    $("#dlg-v").addEventListener("input", show);
    show();
  }
  // 最长持仓和 K 线周期对不上时，边选边提醒（不拦）
  const fitEl = $("#dlg-fit");
  if (fitEl) {
    const ix = settingIndex(d);
    const upd = () => {
      const raw = $("#dlg-v").value;
      const atr = i.key === "atr_period_min" ? JSON.parse(raw) : ix["fast_loop.atr_period_min"].value;
      const limit = i.key === "time_limit_s" ? Number(raw) * 3600 : ix["fast_loop.time_limit_s"].value;
      fitEl.textContent = limit > 0 ? atrMismatch(atr, limit) : "";
    };
    $("#dlg-v").addEventListener("input", upd);
    upd();
  }
}

// 主程序这次启动以后的改动 = 还没生效的
// 这项设置改了不用重启（主程序几秒内自己换上）？名单来自后端 core/hotconfig.py
function isHot(sec, key) {
  const hot = new Set(data.control.hot_keys || []);
  return hot.has(`${sec}.${key}`) || hot.has(`${sec}.*`);
}
// 热生效的项（control.hot_keys）主程序换上以后就不算待生效了；没换上（主程序没在跑 / 还没轮到）仍算
function pendingChanges(d) {
  const c = data.control;
  const since = c.started_at ? Date.parse(c.started_at) : null;
  if (since == null) return [];
  const applied = c.hot_applied_at ? Date.parse(c.hot_applied_at) : null;
  return d.changes.filter((x) => x.ts && Date.parse(x.ts) > since && !(isHot(x.section, x.key) && applied != null && Date.parse(x.ts) <= applied));
}
function changeText(x, ix) {
  const i = ix[`${x.section}.${x.key}`] || { key: x.key, label: x.key, nullable: true, choices: [] };
  const name = i.cat ? i.label : PLAIN[x.key] || i.label || x.key;
  return `${esc(name)}：${valOf({ ...i, nullable: true }, x.old)} → <b>${valOf({ ...i, nullable: true }, x.new)}</b>${x.note ? `<span class="dim small">（${esc(x.note)}）</span>` : ""}`;
}
function showChanges() {
  const d = pageData;
  if (!d) return;
  const ix = settingIndex(d);
  const pend = data.control.config_pending === true ? new Set(pendingChanges(d)) : new Set();
  dlg(`<h3>最近的修改</h3>${d.changes.length ? `<div class="log scroll">${d.changes.map((x) => `<div><span class="n">${x.ts ? hm(x.ts) : ""}</span><span>${changeText(x, ix)}${pend.has(x) ? ' <span class="pill p-warn">待生效</span>' : ""}</span></div>`).join("")}</div>` : '<div class="dim">还没有在网页上改过。</div>'}
    <p class="dim small">每次修改前都留了备份；记录在 data/config_changes.jsonl。</p>`);
}

// 设置页最上面常显：主程序状态 + 重启按钮（不用往下翻到「运行」卡片）
function restartBar() {
  const c = data.control;
  const up = c.started_at ? `${hm(c.started_at)} 启动，已跑 ${dur(Date.now() - Date.parse(c.started_at))}` : "还没有启动记录";
  loadUpdate(false);
  return `<div class="st-restart"><span class="lamp ${data.running ? "" : "bad"}"><i></i>${data.running ? "主程序运行中" : "主程序没在运行"}</span><span class="dim small">${up}</span>${updateNote()}${updateBtn()}${restartBtn()}</div>`;
}

// 「更新到最新版」：面板里跑 make update（拉代码 + 装依赖 + 检查配置 + 重启两个服务）。接口缓存 60 秒
let upd = null;
let updAt = 0;
async function loadUpdate(force) {
  if (!force && Date.now() - updAt < 60000) return;
  updAt = Date.now();
  try { upd = await getJson("/api/update"); } catch (e) { return; }
  if (route().page === "settings" && !$("#dlg-msg")) render();
}
function updateNote() {
  if (!upd || !upd.supported) return "";
  if (upd.state === "running") return `<span class="dim small">更新中…</span>`;
  if (upd.state === "failed") return `<span class="warn small">上次更新没成功</span>`;
  const v = upd.head ? `版本 ${esc(upd.head)}` : "";
  if (upd.behind > 0) return `<span class="small">${v}，落后最新版 ${upd.behind} 个提交</span>`;
  if (upd.behind === 0) return `<span class="dim small">${v}，已是最新</span>`;
  return `<span class="dim small">${v}</span>`;
}
function updateBtn() {
  if (!upd || !upd.supported) return "";
  const busy = upd.state === "running";
  return `<button class="btn b-sm ${upd.behind > 0 ? "b-go" : ""}" ${clickable("update")} ${busy ? 'disabled title="正在更新"' : ""}>更新到最新版</button>`;
}
function askUpdate() {
  const c = data.control;
  const ver = upd && upd.head ? `现在是 <b>${esc(upd.head)}</b>${upd.behind > 0 ? `，最新版比它多 ${upd.behind} 个提交` : upd.behind === 0 ? "，已经是最新" : "，查不到最新版（网络？）"}。` : "";
  const dirty = upd && upd.dirty ? `<div class="note warn">代码目录里有没提交的改动，拉代码可能失败；失败会把原因显示在这里，不会动现有服务。</div>` : "";
  ask("更新到最新版", `<p>${ver}会拉最新代码、装依赖、检查配置，然后<b>重启主程序和面板</b>（和终端里 <code>make update</code> 一样）。持仓不动；信号缓存和哨兵去重会清空。</p>${dirty}${pwField(c)}`, "更新并重启", true, async () => {
    const pw = $("#dlg-pw");
    const startedBefore = c.started_at || null;
    await post("/api/update", { password: pw ? pw.value : null });
    onOk = null;
    $("#layer [data-ok]").remove();
    $("#layer [data-close].btn").textContent = "关闭";
    return waitUpdate(startedBefore);
  });
}
// 更新跑起来后：面板会在最后一步被重启，期间拉不到接口就继续等；等到回执出结果、主程序启动时间变了
async function waitUpdate(startedBefore) {
  const t0 = Date.now();
  let lastLog = "";
  while (Date.now() - t0 < 25 * 60000) {
    if (!$("#dlg-msg")) return;
    await new Promise((r) => setTimeout(r, 2000));
    let u;
    try { u = await getJson("/api/update"); } catch (e) { dlgMsg(`<b>正在更新…</b>　面板在重启，等它回来（已等 ${Math.round((Date.now() - t0) / 1000)} 秒）${lastLog ? `<pre class="json">${esc(lastLog)}</pre>` : ""}`); continue; }
    upd = u;
    lastLog = u.log_tail || lastLog;
    if (u.state === "failed") {
      dlgMsg(`<b class="down">更新没成功</b>　现有服务没有被动，可以再试或在终端跑 <code>make update</code>。<pre class="json">${esc(u.log_tail || "（没有输出）")}</pre>`, "down");
      return;
    }
    if (u.state === "done") {
      let ov = null;
      try { ov = await getJson("/api/overview"); } catch (e) { /* 主程序还在起 */ }
      const c = (ov && ov.control) || {};
      if (ov && ov.running && c.started_at && c.started_at !== startedBefore) {
        dlgMsg(`<b class="up">已更新到 ${esc(u.to_hash || u.head || "")}</b>　主程序 ${hm(c.started_at)} 重启。页面会提示刷新。`);
        updAt = 0;
        poll();
        return;
      }
      dlgMsg(`<b>代码已更新到 ${esc(u.to_hash || u.head || "")}</b>　等主程序起来…`);
      continue;
    }
    dlgMsg(`<b>正在更新…</b>　已等 ${Math.round((Date.now() - t0) / 1000)} 秒${u.log_tail ? `<pre class="json">${esc(u.log_tail)}</pre>` : ""}`);
  }
  dlgMsg("25 分钟还没更新完：在 Mac 上看 data/panel_update.log。", "down");
}

// 待更新配置（config/*.pending.toml）：只读列出，点「更新到最新版」时和代码一起生效
function pendingConfigBanner(d) {
  const p = d.pending_config;
  if (!p || (!p.items.length && !p.error)) return "";
  const val = (v) => esc(typeof v === "string" ? v : JSON.stringify(v));
  const list = p.items.map((x) => `<div><code>${esc(x.label)}</code> = <b>${val(x.value)}</b></div>`).join("");
  const bad = p.failed ? `<div class="down">上次更新时没生效：${esc((p.failed.offending || []).join("、"))}。原配置没动，代码照常更新了；改好待更新文件后再点一次更新。<pre class="json">${esc(String(p.failed.error || "").slice(0, 600))}</pre></div>` : "";
  const err = p.error ? `<div class="down">待更新文件读不出：${esc(p.error)}</div>` : "";
  return `<div class="st-pending"><div class="t"><b>${p.items.length ? `有 ${p.items.length} 项待更新时生效` : "待更新配置有问题"}</b><div>点「更新到最新版」时和新代码一起写进配置、只重启一次（config/*.pending.toml）。</div>${list}${err}${bad}</div></div>`;
}

function pendingBanner(d) {
  if (data.control.config_pending !== true) return "";
  const list = pendingChanges(d);
  const ix = settingIndex(d);
  const what = list.length
    ? list.slice(0, 3).map((x) => `${changeText(x, ix)}（${hm(x.ts)} 改的）`).join("；") + (list.length > 3 ? ` 等 ${list.length} 项` : "")
    : "配置文件改过（可能是在终端改的）";
  return `<div class="st-pending"><div class="t"><b>${list.length ? `有 ${list.length} 项改动还没生效` : "配置改了还没生效"}</b><div>${what}。重启主程序后用上，约 1 分钟。</div></div>
    <button class="btn b-sm" data-act="st-changes">看改了什么</button></div>`;
}

function runCard(d) {
  const c = data.control;
  const up = c.started_at ? `${hm(c.started_at)} 启动，已跑 ${dur(Date.now() - Date.parse(c.started_at))}` : "";
  return `<section class="card st"><div class="ch"><h2>运行</h2></div>
    <div class="st-row"><div class="k">交易模式<small>切回模拟盘随时可以；测试网要面板密码；实盘只能在 Mac 本机切，要逐条确认提示、确认词和面板密码。切完自动重启</small></div><span class="seg">${["paper", "testnet", "live"].map((m) => `<button class="${m === d.mode ? "on" : ""}" ${m === d.mode && (m !== "live" || d.live_armed) || !d.writable ? "disabled" : clickable("mode", m)}>${MODE[m]}</button>`).join("")}</span><span class="pill ${d.live_armed ? "p-short" : ""}">${d.live_armed ? "实盘已解锁" : "实盘未解锁"}</span></div>
    <div class="st-row"><div class="k">主程序<small>${up || "还没有启动记录"}</small></div><span class="lamp ${data.running ? "" : "bad"}"><i></i>${data.running ? "运行中" : "没在运行"}</span></div>
    <div class="st-row"><div class="k">开新仓<small>暂停后只管已有持仓，不开新的</small></div><span class="pill ${c.paused ? "p-short" : ""}">${c.paused ? "已暂停" : "正常开仓"}</span>${c.paused ? `<button class="btn b-sm" ${clickable("cmd", "resume")}>恢复开仓</button>` : `<button class="btn b-sm" ${clickable("cmd", "pause")}>暂停开新仓</button>`}</div>
    </section>`;
}

function riskCard(d, ix) {
  const basis = d.recommend_basis_usdt ? `推荐按 ${money(d.recommend_basis_usdt, 0)} U 算` : "推荐按 1,000 U 算（没拿到账户资金）";
  const cautions = (d.risk_cautions || []).map((c) => esc(c.replace(/^[a-z_]+/, (k) => PLAIN[k] || k)));
  return `<section class="card st"><div class="ch"><h2>风控硬上限</h2><span class="r">不跟档位 · 你定的上限，AI 改不了 · ${basis}</span></div>
    ${CORE.filter((k) => ix[k]).map((k) => itemRow(ix[k], true)).join("")}
    <div class="st-foot">${cautions.length ? `<span class="warn">${cautions.length} 项比推荐冒险：</span>${cautions.join("；")}。只提示，不拦。` : "都在推荐范围内。"}
      <button class="btn b-sm" data-act="st-more">风控员的全部设置 ›</button></div></section>`;
}

function keysTab(d) {
  const port = esc(location.port || "8090");
  return `<div class="st-note">只显示配没配，看不到内容。${IS_LOCAL ? "" : `要换得在 Mac mini 本机打开 <code>http://127.0.0.1:${port}/setup</code>（或终端 <code>jiaoyi setup-keys</code>）。`}</div>
    <div class="st-pad"><div class="form">${keyRows(d) || '<div class="dim">启动面板时没有查密钥状态。</div>'}</div></div>
    <div class="st-foot">${keyBtn()}　${IS_LOCAL ? "点进首次设置页，只填要换的那几项，其余留空不变。" : "局域网打开时按钮是灰的，在 Mac 本机打开才能点。"}</div>`;
}

function showTab(d, ix) {
  const updown = document.documentElement.dataset.updown === "red-up" ? "red-up" : "green-up";
  const info = (k, name) => (ix[k] ? `<div class="st-row"><div class="k">${name}</div><span class="pill">${valOf(ix[k])}</span></div>` : "");
  return `<div class="st-note">只影响你现在用的这台设备，不用密码。</div>
    <div class="st-row"><div class="k">币安风格成交提示音<small>开仓成交响一声往上的双音铃，平仓响一声往下的（测试单也响）。仿币安的声音现合成，不是币安的原声。浏览器要先在页面上点一下才允许出声。</small></div><span class="seg"><button class="${soundOn() ? "on" : ""}" ${clickable("sound", "on")}>开</button><button class="${soundOn() ? "" : "on"}" ${clickable("sound", "off")}>关</button></span></div>
    <div class="st-row"><div class="k">提示音音量<small>拖完会响一声开仓的试听</small></div><input id="sound-vol" class="vol" type="range" min="0" max="100" step="5" value="${soundVol()}" aria-label="提示音音量"><button class="btn b-sm" ${clickable("sound-test", "open")}>听开仓</button><button class="btn b-sm" ${clickable("sound-test", "close")}>听平仓</button></div>
    <div class="st-row"><div class="k">涨跌颜色</div><span class="seg"><button class="${updown === "green-up" ? "on" : ""}" ${clickable("updown", "green-up")}>绿涨红跌</button><button class="${updown === "red-up" ? "on" : ""}" ${clickable("updown", "red-up")}>红涨绿跌</button></span></div>
    <div class="st-sub">系统信息（只看）</div>
    <div class="st-row"><div class="k">面板密码</div>${d.password_set ? '<span class="pill">已设置</span>' : '<span class="pill p-warn">未设置（终端 jiaoyi set-panel-password）</span>'}</div>
    ${info("risk.day_reset_tz", "“今天”按哪个时区算")}${info("app.sim_environment", "模拟盘连币安哪个环境")}${info("app.data_dir", "数据保存在哪个文件夹")}`;
}

// 搜索：每个词都要在行名、原始说明、键名、组名或角色名里出现（不分大小写）
const qTerms = (q = S.stQ) => q.trim().toLowerCase().split(/\s+/).filter(Boolean);
const hitAll = (text, terms) => { const t = text.toLowerCase(); return terms.every((w) => t.includes(w)); };
function roleBody(role, d, ix, groups, terms) {
  if (role.id === "keys") return keysTab(d);
  if (role.id === "show") return showTab(d, ix);
  const roleText = role.t;  // 只按角色名整块命中；说明里的词（比如「止损」）不算，免得一搜出来整块
  const html = groups.map((g) => {
    const items = terms.length && !hitAll(roleText, terms) && !hitAll(g.g || "", terms)
      ? g.items.filter((i) => hitAll(`${i.cat ? i.label : nameOf(i)} ${i.label} ${i.key} ${i.sec}.${i.key} ${g.g || ""} ${roleText}`, terms))
      : g.items;
    if (!items.length) return "";
    return `${g.g ? `<div class="st-sub">${esc(g.g)}</div>` : ""}${g.hint ? `<div class="st-note">${esc(g.hint)}</div>` : ""}${(g.warn || []).map((c) => `<div class="note warn">${esc(c)}（只提示，不拦）</div>`).join("")}${items.map((i) => itemRow(i, false, role.strip)).join("")}${g.extra === "majors" && !terms.length ? majorsBox(d.majors) : ""}`;
  }).join("");
  if (!html) return "";
  const caution = role.caution && d[role.caution] ? `<div class="note warn">${esc(d[role.caution])}（只提示，不拦）</div>` : "";
  const prompts = role.prompts && !terms.length ? `<div class="st-row"><div class="k">现在用的提示词<small>只读，按最长持仓档写</small></div><button class="btn b-sm" ${clickable("prompts")}>看提示词</button></div>` : "";
  return caution + html + prompts;
}
function rolesCard(d, ix) {
  const groups = roleGroups(d, ix);
  const terms = qTerms();
  const sections = ROLES.map((role) => {
    const g = groups[role.id] || [];
    const all = g.flatMap((x) => x.items);
    if (role.groups && !all.length) return "";  // 这个角色现在没有可调的设置
    const matchName = terms.length && hitAll(`${role.t} ${role.find || ""}`, terms);
    const body = terms.length && !role.groups && !matchName ? "" : roleBody(role, d, ix, g, terms);
    if (terms.length && !body) return "";
    const open = terms.length > 0 || S.roleOpen.has(role.id);
    const hotN = all.filter(hot).length;
    const found = terms.length && role.groups ? (body.match(/class="st-item /g) || []).length : 0;
    const meta = !role.groups ? "" : terms.length ? `找到 ${found} 项` : `${all.length} 项${hotN ? ` · <span class="warn">${hotN} 项比推荐冒险</span>` : ""}`;
    return `<section class="card st role ${open ? "open" : ""}" id="st-r-${role.id}">
      <button class="role-h" ${terms.length ? "disabled" : `data-act="st-role" data-arg="${role.id}"`} aria-expanded="${open}"><span class="rn">${esc(role.t)}</span><span class="rw">${esc(role.what)}</span><span class="rm">${meta}</span><span class="chev">${terms.length ? "" : "›"}</span></button>
      <div class="role-b">${body}</div></section>`;
  }).join("");
  const empty = terms.length && !sections ? `<div class="card st"><div class="st-pad dim">没找到「${esc(S.stQ.trim())}」。换个词试试，比如「超时」「止损」「杠杆」，或者直接打键名。</div></div>` : "";
  return `<div class="roles-head"><h2>按角色的全部设置</h2><span class="dim small">顺序跟交易流程走：选币 → 开会 → 发现信号 → 拍板 → 风控 → 下单 → 看着持仓。点一个角色展开它的全部设置。</span>
      <div class="st-search"><input id="st-q" class="inp" type="search" placeholder="搜设置：比如「超时」「放弃」「jev_timeout」" autocomplete="off" spellcheck="false" value="${esc(S.stQ)}"></div></div>
    ${sections}${empty}`;
}

// 「你的设置实际意味着」：后端按硬上限、最长持仓档、本金算好的几行，只提示
function meaningCard(d) {
  const m = d.meaning;
  if (!m || !m.lines.length) return "";
  return `<section class="card st"><div class="ch"><h2>你的设置实际意味着</h2><span class="r">只提示，不拦</span></div>
    ${m.lines.map((x) => `<div class="st-row"><div class="k">${esc(x.label)}<small>${x.warn ? `<span class="warn">${esc(x.text)}</span>` : esc(x.text)}</small></div></div>`).join("")}</section>`;
}

/* 档位设置页（设计稿 design/tier-settings）：7 张档位卡 → 换档预览条 → 这一档的全部参数。
   档位数值全从后端 profile.defaults 拿；「自定义」= 配置文件里写了这个键，「跟档」= 没写。 */
const TIER_REC = "2h";
const TIER_NICK = { "15m": "贴身快打", "30m": "短线", "1h": "勤快", "2h": "标准", "6h": "稳一点", "12h": "慢慢来", "24h": "一天一看" };
const TIER_WHO = {
  "15m": "想看系统高频干活、压测用。费用最高，只建议模拟盘。",
  "30m": "做短线，愿意多花 AI 费换机会。",
  "1h": "一天想多几次机会，费用中等。",
  "2h": "大多数人。机会和费用比较平衡。",
  "6h": "上班族，一天看两三次就够。",
  "12h": "不常看盘，拿得住单子。",
  "24h": "一天开一次会，做趋势单，最省钱。",
};
const EV_SHORT = { key_level_break: "破关键位", move_since_card: "出卡后价格走远", new_head_symbol: "冒出新热门币", signal_against_card: "哨兵看到反向信号", predictor_against_card: "预测员唱反调", news_high_impact: "重大新闻", stale_card_on_entry: "入场时卡太旧" };
const minDur = (m) => humanDur(m * 60);
const TF = {
  min: (v) => (v == null ? "—" : minDur(v)), sec: (v) => humanDur(v), usd: (v) => `$${v}`, x: (v) => `${v} 倍`, r: (v) => `${v} R`,
  p: (v) => `${v}`, pct: (v) => `${v}%`, bp: (v) => (v ? `${v} bp` : "不查"), n: (v) => `${v}`, bool: (v) => (v ? "开" : "关"),
  hold: (v) => profileSpan(v), list: (v) => `${(v || []).join(" / ")} 分钟`, over: (v) => (v === "slow" ? "放慢" : "不放慢"),
  ev: (v) => (v && v.length ? v.map((k) => EV_SHORT[k] || k).join("、") : "—"), atrl: (v) => (v ? minDur(v) : "不用"),
  age: (v) => (v ? `超过 ${minDur(v)} 不入场` : "不限"), at: (v) => v || "不定点",
  kl: (v) => (/^\d+[mh]$/.test(v || "") ? `${profileSpan(v)} K 线` : esc(v)),
};
// [键, 段, 名字, 说明, 格式]；hold 组 = 跟最长持仓走，其余跟开会档位走
const TIER_GROUPS = [
  { g: "开会节奏", rows: [
    ["hold", "app", "最长持仓", "一笔单子最多拿多久，到点平掉。不设就用这一档的推荐搭配", "hold"],
    ["card_ttl_min", "schedule", "作战卡有效期", "开会间隔 + 20 分钟", "min"],
    ["pool_size", "app", "每次选多少个币进候选池", "选币每轮按分数从高到低挑这么多", "n"],
    ["card_pick_max", "app", "军师团每次最多挑几个币", "从候选池里挑；实际开会的数还不能超过下面这一项", "n"],
    ["max_cards_per_round", "schedule", "每轮最多开几个币的会", "每天 AI 费用上限照样管着，花满当天不再开会", "n"],
    ["symbol_min_interval_min", "schedule", "同一个币最短隔多久再开会", "", "min"],
    ["min_refresh_gap_min", "schedule", "两轮会最短间隔", "临时会多了也不会比这更密", "min"],
    ["concurrency", "schedule", "同时开会几个币", "", "n"],
  ] },
  { g: "AI 花费", rows: [
    ["daily_cost_limit_usd", "budget", "每天 AI 费用上限", "花到 80% 按下面放慢或不放慢，花满当天不再开会", "usd"],
    ["over_budget", "schedule", "花到 80% 时", "放慢 = 换成更慢的开会间隔；不放慢 = 照常开到花满", "over"],
    ["slowdown_interval_min", "schedule", "放慢成多久开一次", "", "min"],
    ["fail_backoff_minutes", "budget", "开会失败后等多久重试", "第 1 / 2 / 3 次", "list"],
  ] },
  { g: "临时会（只重开一个币）", rows: [
    ["event_meeting_enabled", "schedule", "出事时临时开会", "2 小时及更慢的档位才开", "bool"],
    ["event_triggers", "schedule", "哪些事算出事", "", "ev"],
    ["event_symbol_gap_min", "schedule", "同一个币临时会最短间隔", "", "min"],
    ["event_max_per_day", "schedule", "一天最多几次临时会", "0 = 不限", "n"],
    ["entry_max_card_age_min", "schedule", "卡太旧就不入场", "", "age"],
    ["schedule_at", "schedule", "每天定点开会", "只有 24 小时档用", "at"],
  ] },
  { g: "止损和止盈", hold: true, rows: [
    ["atr_period_min", "fast_loop", "止损按几分钟的波动算（短）", "ATR 周期", "min"],
    ["atr_long_period_min", "fast_loop", "再参考几分钟的波动（长）", "止损 = √(短×长) × 时段系数", "atrl"],
    ["session_factor", "fast_loop", "按曼谷时段调宽窄", "美股开盘那几个小时止损自动放宽", "bool"],
    ["min_stop_atr", "fast_loop", "止损最窄", "几倍波动", "x"],
    ["max_stop_atr", "fast_loop", "止损最宽", "几倍波动", "x"],
    ["min_tp_r", "fast_loop", "止盈最近", "R = 止损距离", "r"],
    ["max_tp_r", "fast_loop", "止盈最远", "", "r"],
    ["min_stop_fee_multiple", "fast_loop", "止损至少是手续费的几倍", "太窄的单子手续费吃掉利润，不做", "x"],
    ["funding_against_max_bp", "fast_loop", "资金费太贵不开", "只查长持仓", "bp"],
  ] },
  { g: "拍板人门槛", hold: true, rows: [
    ["min_p_take", "fast_loop", "做不做 至少多少", "想多做一些可以放到 0.50", "p"],
  ] },
  { g: "盯着持仓", hold: true, rows: [
    ["keeper_interval_s", "app", "持仓管家多久看一次", "", "sec"],
    ["patrol_alert_pct_1m", "fast_loop", "1 分钟动这么多就报警", "", "pct"],
    ["patrol_shock_pct_1m", "fast_loop", "1 分钟动这么多就全平", "", "pct"],
  ] },
  { g: "预测员", hold: true, rows: [
    ["interval", "predictor", "看几分钟的 K 线", "", "kl"],
    ["pred_len", "predictor", "往后预测几根", "正好盖住最长持仓", "n"],
    ["lookback", "predictor", "回看几根 K 线", "官方 ≈ 往后预测的 5 倍，至少 64", "n"],
    ["paths", "predictor", "模拟几条路径", "模型平均成一条，官方 10", "n"],
    ["temperature", "predictor", "采样温度", "官方 0.6", "n"],
    ["predictor_weight", "fast_loop", "预测员意见占几成", "排名和卡方向一致时加分（0.2 加 0.10），相反不扣", "p"],
  ] },
];
// 档位给了、上面没列的键进哪一组的「其他」；不出行的：开会间隔本身、24 格时段系数、最长持仓秒数（= 第一行）
const TIER_OTHER = { refresh_lead_min: "开会节奏", screener_interval_min: "开会节奏", signal_dedup_s: "拍板人门槛", stop_level_buffer_atr: "止损和止盈", predictor_enabled: "预测员" };
const TIER_OTHER_SEC = { schedule: "开会节奏", app: "开会节奏", budget: "AI 花费", fast_loop: "止损和止盈", predictor: "预测员" };
const TIER_HIDE = ["app.slow_loop_interval_min", "fast_loop.session_factor_weekday", "fast_loop.session_factor_weekend", "fast_loop.time_limit_s", "fast_loop.min_p_genuine", "fast_loop.min_p_direction", "fast_loop.max_p_toxic"]; // 后三个只在旧 6 道题问法生效
S.tierSel = null;
S.tierOnlyCustom = false;

// 每组的行（含「其他」），每行 {key, sec, label, hint, fmt, group, hold}
function tierRows(d) {
  const pf = d.profile;
  const listed = new Set(TIER_GROUPS.flatMap((g) => g.rows.map(([k, sec]) => `${sec}.${k}`)));
  const ix = settingIndex(d);
  const groups = TIER_GROUPS.map((g) => ({ ...g, rows: g.rows.map(([key, sec, label, hint, fmt]) => ({ key, sec, label, hint, fmt, hold: !!g.hold })) }));
  for (const [sec, keys] of Object.entries(pf.defaults[pf.current] || {})) {
    for (const key of Object.keys(keys)) {
      const id = `${sec}.${key}`;
      if (listed.has(id) || TIER_HIDE.includes(id)) continue;
      const g = groups.find((x) => x.g === (TIER_OTHER[key] || TIER_OTHER_SEC[sec])) || groups[0];
      const i = ix[id];
      g.rows.push({ key, sec, label: i ? nameOf(i) : PLAIN[key] || key, hint: "其他", fmt: null, hold: !!g.hold });
    }
  }
  return groups;
}
const tierHold = (pf, t) => (pf.hold_custom ? pf.hold : pf.hold_recommended[t] || pf.hold);
const isCustom = (pf, r) => (r.key === "hold" ? pf.hold_custom : !!(pf.pinned[r.sec] && r.key in pf.pinned[r.sec]));
const tierVal = (pf, r, t) => (r.key === "hold" ? pf.hold_recommended[t] || pf.hold : ((pf.defaults[t] || {})[r.sec] || {})[r.key]);
function effVal(d, r, t) {
  const pf = d.profile;
  if (!isCustom(pf, r)) return tierVal(pf, r, t);
  if (r.key === "hold") return pf.hold;
  const i = settingIndex(d)[`${r.sec}.${r.key}`];
  return i ? i.value : tierVal(pf, r, t);
}
function showVal(d, r, v) {
  const i = settingIndex(d)[`${r.sec}.${r.key}`] || { key: r.key, choices: [] };
  const off = offShown(i, v);
  if (off) return esc(off);
  if (r.fmt) return TF[r.fmt](v);
  return valOf({ ...i, sec: r.sec, nullable: true }, v);
}
const sameV = (a, b) => JSON.stringify(a) === JSON.stringify(b);
// 从 a 档换到 b 档：会变的（跟档的项）和不动的（自定义的项，新档的值不一样才列）
function tierDiffs(d, from, to) {
  const pf = d.profile;
  const changed = [], kept = [];
  for (const g of tierRows(d)) for (const r of g.rows) {
    const a = tierVal(pf, r, from), b = tierVal(pf, r, to);
    if (sameV(a, b)) continue;
    if (isCustom(pf, r)) kept.push({ r, b });
    else changed.push({ r, a, b });
  }
  return { changed, kept };
}

const bars = (n, cls = "") => `<div class="tb ${cls}">${Array.from({ length: 7 }, (_, i) => `<i class="${i < n ? "on" : ""}"></i>`).join("")}</div>`;
function tierCards(d) {
  const pf = d.profile;
  const sel = S.tierSel || pf.current;
  const usds = pf.choices.map((t) => ((pf.defaults[t] || {}).budget || {}).daily_cost_limit_usd).filter((x) => x > 0);
  const lo = Math.min(...usds), hi = Math.max(...usds);
  const costBars = (u) => (hi > lo ? Math.max(1, Math.min(7, Math.round(1 + (6 * Math.log(u / lo)) / Math.log(hi / lo)))) : 4);
  const budget = settingIndex(d)["budget.daily_cost_limit_usd"];
  const mineU = isCustom(pf, { key: "daily_cost_limit_usd", sec: "budget" }) && budget;
  return pf.choices.map((t, n) => {
    const cur = t === pf.current, on = t === sel;
    const hold = cur ? pf.hold : tierHold(pf, t);
    const usd = cur && mineU ? budget.value : ((pf.defaults[t] || {}).budget || {}).daily_cost_limit_usd;
    const off = pf.paper_only.includes(t) && d.mode !== "paper";
    const stop = pf.stop_pct[hold];
    return `<button class="tier ${cur ? "cur" : ""} ${on ? "sel" : ""}" role="radio" aria-checked="${on}" ${off ? 'disabled title="只在模拟盘可用"' : `data-act="tier" data-arg="${esc(t)}"`}>
      ${cur ? '<span class="tbadge">当前</span>' : on ? '<span class="tbadge next">换到这档？</span>' : ""}
      <div><div class="tname">${esc(profileSpan(t))}</div><div class="tnick">${esc(TIER_NICK[t] || "")}${t === TIER_REC ? ' · <span class="trec">推荐</span>' : ""}</div></div>
      <div class="tm"><span>节奏</span>${bars(7 - n)}</div>
      <div class="tm"><span>费用</span>${bars(usd === 0 ? 7 : usd > 0 ? costBars(usd) : 0, "cost")}</div>
      <div class="tkv num">
        <div><span>多久开会</span><b>${esc(profileSpan(t))}</b></div>
        <div><span>拿多久</span><b class="${pf.hold_custom ? "mine" : ""}">${esc(profileSpan(hold))}</b></div>
        <div><span>止损约</span><b>${stop != null ? `${stop}%` : "—"}</b></div>
        <div><span>AI 每天</span><b class="${cur && mineU ? "mine" : ""}">${usd === 0 ? "不限" : `≤ $${esc(usd)}`}</b></div>
      </div>
      <div class="twho">${esc(TIER_WHO[t] || "")}</div></button>`;
  }).join("");
}
function tierPreview(d) {
  const pf = d.profile;
  const to = S.tierSel;
  if (!to || to === pf.current) return "";
  const { changed, kept } = tierDiffs(d, pf.current, to);
  const go = d.writable ? `<button class="btn b-go" data-act="tier-go" data-arg="${esc(to)}">换到 ${esc(profileSpan(to))}档</button>` : `<button class="btn" disabled title="这次启动的面板只能看">换到 ${esc(profileSpan(to))}档</button>`;
  return `<div class="tier-preview"><div class="ttop">
      <div class="ttitle">从 ${esc(profileSpan(pf.current))}档换到 <em>${esc(profileSpan(to))}档</em>：会变 ${changed.length} 项${kept.length ? `，你自定义的 ${kept.length} 项不动` : ""}</div>
      <div class="tacts"><button class="btn" data-act="tier-cancel">算了</button>${go}</div></div>
    <div class="tchips">${changed.slice(0, 10).map(({ r, a, b }) => `<span class="tchip">${esc(r.label)} <s>${showVal(d, r, a)}</s> → <b>${showVal(d, r, b)}</b></span>`).join("")}${changed.length > 10 ? `<span class="tchip">还有 ${changed.length - 10} 项，看下面表里蓝色的行</span>` : ""}</div>
    ${kept.length ? `<div class="tchips">${kept.map(({ r, b }) => `<span class="tchip keep">${esc(r.label)}：保持你的 ${showVal(d, r, effVal(d, r, pf.current))}（新档是 ${showVal(d, r, b)}）</span>`).join("")}</div>` : ""}
    <div class="dim small">点「换到」要面板密码，重启主程序后按新档位运行。</div></div>`;
}
// 档位参数按角色分块（和下面「按角色的全部设置」同一张 ROLES / PREFIX_ROLE 表，两边不会分歧），默认收起
const TIER_ROLE = { "app.hold": "market" };  // 别处显示、ROLES 里没点名的
function tierRoleOf(d, ix) {
  const where = {};
  for (const [id, gs] of Object.entries(roleGroups(d, ix))) for (const g of gs) for (const i of g.items) where[`${i.sec}.${i.key}`] = { role: id, g: g.g };
  return (id) => where[id] || { role: TIER_ROLE[id] || (PREFIX_ROLE.find(([pre]) => id.startsWith(pre)) || [])[1] || SECTION_ROLE[id.split(".")[0]] || "market", g: "" };
}
try { S.tierRoleOpen = new Set(JSON.parse(localStorage.getItem("tier-roles") || "[]")); } catch (err) { S.tierRoleOpen = new Set(); }
S.tierQ = "";
function toggleTierRole(id) {
  if (S.tierRoleOpen.has(id)) S.tierRoleOpen.delete(id);
  else S.tierRoleOpen.add(id);
  try { localStorage.setItem("tier-roles", JSON.stringify([...S.tierRoleOpen])); } catch (err) { /* 记不住就每次都收起 */ }
  render();
}
function tierRowHtml(d, r, ix, t, previewing) {
  const pf = d.profile;
  const mine = isCustom(pf, r);
  const curV = effVal(d, r, pf.current), newV = effVal(d, r, t);
  const changing = previewing && !mine && !sameV(curV, newV);
  const val = changing ? `<s>${showVal(d, r, curV)}</s><span class="arrow">→</span> ${showVal(d, r, newV)}` : showVal(d, r, newV);
  const id = `${r.sec}.${r.key}`;
  const i = ix[id];
  const w = d.writable && i && i.editable;
  const restore = r.key === "hold" ? clickable("hold", "auto") : clickable("unpin", `${r.sec}:${r.key}`);
  const act = !w ? '<span class="dim small">网页不改</span>'
    : mine ? `<button class="btn b-sm" data-act="edit1" data-arg="${esc(id)}">改</button><button class="btn b-sm b-ghost" ${restore}>恢复跟档</button>`
    : `<button class="btn b-sm" data-act="edit1" data-arg="${esc(id)}">自定义</button>`;
  const follow = r.hold ? `跟最长持仓 ${profileSpan(tierHold(pf, t))}` : "跟开会档位";
  return `<div class="tier-row ${mine ? "custom" : ""} ${changing ? "changing" : ""}">
    <div class="tl"><b>${esc(r.label)}</b>${r.hint && r.hint !== "其他" ? `<small>${esc(r.hint)}</small>` : ""}${mootOf(r, ix) ? `<small class="warn">${mootOf(r, ix)}</small>` : ""}<code>${esc(r.key)}</code></div>
    <div class="tv num">${val}</div>
    <div class="ts">${mine ? `<span class="ttag c">自定义</span><small>档位是 ${showVal(d, r, tierVal(pf, r, pf.current))}</small>` : `<span class="ttag f" title="${esc(follow)}走">跟档</span><small>${esc(follow)}</small>`}</div>
    <div class="ta">${act}</div></div>`;
}
function tierTable(d) {
  const pf = d.profile;
  const t = S.tierSel || pf.current;
  const previewing = t !== pf.current;
  const ix = settingIndex(d);
  const groups = tierRows(d);
  const all = groups.flatMap((g) => g.rows.map((r) => ({ ...r, paper: g.paper })));
  const n = all.filter((r) => isCustom(pf, r)).length;
  const where = tierRoleOf(d, ix);
  const terms = qTerms(S.tierQ);
  let html = "";
  for (const role of ROLES) {
    const mine = all.filter((r) => where(`${r.sec}.${r.key}`).role === role.id);
    if (!mine.length) continue;
    const rows = mine.filter((r) => (!S.tierOnlyCustom || isCustom(pf, r)) && (!terms.length || hitAll(`${r.label} ${r.hint} ${r.key} ${r.sec}.${r.key} ${where(`${r.sec}.${r.key}`).g} ${role.t}`, terms)));
    if (!rows.length) continue;
    const nCustom = mine.filter((r) => isCustom(pf, r)).length;
    const nChange = previewing ? mine.filter((r) => !isCustom(pf, r) && !sameV(effVal(d, r, pf.current), effVal(d, r, t))).length : 0;
    const auto = terms.length > 0 || S.tierOnlyCustom;
    const open = auto || S.tierRoleOpen.has(role.id);
    const meta = [terms.length ? `找到 ${rows.length} 项` : `${mine.length} 项`, nCustom ? `<span class="warn">${nCustom} 项自定义</span>` : "", nChange ? `<span class="accent">${nChange} 项会变</span>` : ""].filter(Boolean).join(" · ");
    const subs = [...new Set(rows.map((r) => where(`${r.sec}.${r.key}`).g))];  // 同一小组的行排在一起
    rows.sort((a, b) => subs.indexOf(where(`${a.sec}.${a.key}`).g) - subs.indexOf(where(`${b.sec}.${b.key}`).g));
    let body = "", last = null;
    const paper = rows.find((r) => r.paper);
    if (paper && d.mode === "paper") body += `<div class="st-note">${esc(paper.paper)}</div>`;
    for (const r of rows) {
      const g = where(`${r.sec}.${r.key}`).g;
      if (subs.length > 1 && g !== last) body += `<div class="tier-gh"><span>${esc(g || "其他")}</span></div>`;
      last = g;
      body += tierRowHtml(d, r, ix, t, previewing);
    }
    html += `<div class="role tier-role ${open ? "open" : ""}" id="tier-r-${role.id}">
      <button class="role-h" ${auto ? "disabled" : `data-act="tier-role" data-arg="${role.id}"`} aria-expanded="${open}"><span class="rn">${esc(role.t)}</span><span class="rw">${esc(role.what || "")}</span><span class="rm">${meta}</span><span class="chev">${auto ? "" : "›"}</span></button>
      <div class="role-b">${body}</div></div>`;
  }
  const head = previewing ? `换到 ${profileSpan(t)}档后的全部参数（预览）` : `${profileSpan(t)}档的全部参数`;
  const sub = previewing ? "蓝色 = 换档后会变的；橙色 = 你自定义的，换档也不动。按角色分，点一个角色展开。" : "「跟档」= 档位给的值，换档跟着变；橙色「自定义」= 你改过的，换档不动。按角色分，点一个角色展开。";
  const filt = (v, txt) => `<button aria-pressed="${S.tierOnlyCustom === v}" data-act="tier-filter" data-arg="${v ? 1 : 0}">${txt}</button>`;
  const empty = terms.length ? `没找到「${esc(S.tierQ.trim())}」。换个词试试，比如「止损」「预算」「门槛」。` : "没有自定义的项，全部跟着档位走。";
  return `<section class="card st tier-table"><div class="tier-th"><div><h2>${esc(head)}</h2><span class="dim small">${sub}</span></div>
      <div class="tcustom"><span class="n ${n ? "" : "zero"}">${n ? `有 ${n} 项自定义` : "全部跟档"}</span>${n && d.writable ? `<button class="btn b-sm b-ghost" ${clickable("clearpins")}>恢复全部跟档</button>` : ""}
        <div class="tfilter" role="group" aria-label="显示">${filt(false, "全部")}${filt(true, "只看自定义")}</div></div>
      <div class="st-search tier-search"><input id="tier-q" class="inp" type="search" placeholder="搜这一档的参数：比如「止损」「预算」「门槛」" autocomplete="off" spellcheck="false" value="${esc(S.tierQ)}"></div></div>
    ${html || `<div class="tempty">${empty}</div>`}</section>`;
}
function tierSection(d) {
  if (!d.profile || !d.profile.defaults || !Object.keys(d.profile.defaults).length) return `<section class="card st"><div class="ch"><h2>选档位</h2></div><div class="st-row"><div class="k">这次启动的面板没拿到配置文件路径，看不到档位</div></div></section>`;
  return `<section class="tier-sec"><div class="tier-head"><h2>选档位</h2><span class="dim small">档位 = 军师团多久开一次会。持仓时间、止损宽度、AI 费用都跟着走；点卡片先看会变什么，确认了才换。</span></div>
    ${promptLine(d.prompts, d.profile.hold)}
    <div class="tiers-scroll"><div class="tiers" role="radiogroup" aria-label="开会档位">${tierCards(d)}</div></div>${tierPreview(d)}</section>${tierTable(d)}`;
}

P.settings = (arg) => {
  const d = pageData;
  // #settings/gate：第一次带着这个角色进来时展开它（渲染完由 render 滚过去）
  if (arg && S.stArg !== arg) { S.roleOpen.add(arg); saveRoles(); S.stQ = ""; S.stScroll = arg; }
  S.stArg = arg;
  const ix = settingIndex(d);
  const note = d.file_error ? `<span class="down">配置文件现在读不出：${esc(d.file_error)}。先在终端修好再改。</span>` : d.writable ? "改任何一项都要面板密码；模式、数据目录、拍板人通道和模型版本不在网页上改。" : "这次启动的面板只能看：没拿到配置文件路径。";
  return `<div class="st-col st-wide">${restartBar()}${pendingBanner(d)}${pendingConfigBanner(d)}${tierSection(d)}${riskCard(d, ix)}${runCard(d)}${meaningCard(d)}${rolesCard(d, ix)}
    <div class="st-links"><button data-act="st-changes">最近的修改记录</button><span class="dim small">${note}</span></div></div>`;
};

/* 资金曲线（历史页） */
let hchart = null, hseries = null, hEl = null, hKey = "";
function mountHistoryChart() {
  const host = $("#hchart-host");
  if (!host || !window.LightweightCharts) return;
  if (!hEl) {
    hEl = document.createElement("div");
    hEl.style.height = "100%";
    host.appendChild(hEl); // 先挂到页面上再建图，否则初始尺寸是 0
    hchart = lockChart(LightweightCharts.createChart(hEl, {
      ...CHART_COOP,
      autoSize: true,
      layout: { background: { color: "transparent" }, textColor: getComputedStyle(document.documentElement).getPropertyValue("--dim").trim(), fontFamily: getComputedStyle(document.body).fontFamily },
      grid: { vertLines: { visible: false }, horzLines: { color: "rgba(0,0,0,0.05)" } },
      timeScale: { timeVisible: true, borderVisible: false, tickMarkFormatter: (t) => tick(t) },
      rightPriceScale: { borderVisible: false },
      localization: { timeFormatter: (t) => hm(t * 1000) },
    }), hEl);
    hseries = hchart.addBaselineSeries({ baseValue: { type: "price", price: 0 } });
  }
  if (hEl.parentNode !== host) host.appendChild(hEl);
  const css = getComputedStyle(document.documentElement);
  hseries.applyOptions({ topLineColor: css.getPropertyValue("--up").trim(), bottomLineColor: css.getPropertyValue("--down").trim(), topFillColor1: "rgba(26,127,55,0.15)", topFillColor2: "rgba(26,127,55,0.02)", bottomFillColor1: "rgba(215,0,21,0.02)", bottomFillColor2: "rgba(215,0,21,0.15)" });
  // 时间要严格递增：同一秒平了几笔就往后挪一秒
  let last = 0;
  const pts = pageData.curve.map((c) => { let t = Math.floor(new Date(c.ts).getTime() / 1000); if (t <= last) t = last + 1; last = t; return { time: t, value: c.cum_pnl_usdt }; });
  hseries.setData(pts);
  const key = pageData.range + pts.length;
  if (key !== hKey) hchart.timeScale().fitContent();
  hKey = key;
}

function placeholder() {
  return `<div class="card"><div class="empty"><b>这一页还在做</b>按总览的样式接着做。<a href="#overview">回总览 ›</a></div></div>`;
}

/* ---------- 渲染和拉数 */
const PAGE_API = {
  positions: () => "/api/positions",
  team: (arg) => `/api/team${arg ? "?role=" + encodeURIComponent(arg) : ""}`,
  history: () => `/api/history?range=${S.hist}${S.tests ? "&tests=true" : ""}`,
  funds: () => `/api/funds?ai=${S.ai || "today"}`,
  pool: () => "/api/pool",
  settings: () => "/api/settings",
};
const pageId = (page, arg) => page + "/" + (arg || "") + (page === "history" ? "/" + S.hist + (S.tests ? "/t" : "") : "");
// 同一个接口请求就是同一份数据：持仓页选中哪一笔不改请求，不能按选中项各存一份旧快照
const dataId = (page, arg) => page + " " + PAGE_API[page](arg);
// 持仓是活数据：存了几分钟的快照里有早就平掉的仓位，先画出来一秒后又被新数据抹掉，看着像仓位闪了一下
const CACHE_MAX_AGE_MS = { positions: 10000 };
function cachedPage(page, id, now) {
  const hit = pageCache.get(id);
  if (!hit) return null;
  const max = CACHE_MAX_AGE_MS[page];
  return max != null && now - hit.at > max ? null : hit.d;
}
// 换页时会多发一轮拉数，和定时那轮同时在路上：只认后发出的那轮，先发的晚到就丢掉，不拿旧数据盖新数据
let pollSent = 0, pollShown = 0;
function pollIsLatest(seq) {
  if (seq < pollShown) return false;
  pollShown = seq;
  return true;
}

// 每轮拉数（持仓页 2 秒、别的页 5 秒）整块重写 innerHTML：几十 KB 重新解析、CSS 动画从头放、:hover 丢掉，
// 按下鼠标时正好赶上重画，按下的那个按钮被换掉，这一下点击就没了。多数轮次内容一字不差，直接跳过；
// 有变化就把新标记逐个节点补到现有节点上，没变的节点原样留着
const sameHtml = (el, html) => el.__html === html && el.__first === el.firstChild;
function setHtml(el, html, patch = true) {
  if (sameHtml(el, html)) return false;
  if (patch && el.firstChild) {
    const t = document.createElement("template");
    t.innerHTML = html;
    morph(el, t.content);
  } else {
    el.innerHTML = html;
  }
  el.__html = html;
  el.__first = el.firstChild;
  return true;
}
// 走势图的画布是代码挂进去的，标记里那一格是空的：不能按标记把它清掉
const MORPH_KEEP = new Set(["kchart-host", "hchart-host"]);
const MORPH_FORM = new Set(["INPUT", "SELECT", "TEXTAREA"]);
function morph(from, to) {
  const a = [...from.childNodes], b = [...to.childNodes];
  for (let i = 0; i < b.length; i++) {
    const o = a[i], n = b[i];
    if (!o) from.appendChild(n);
    else if (o.nodeType !== n.nodeType || o.nodeName !== n.nodeName) from.replaceChild(n, o);
    // 图表格子和别的块互换（历史页「没有平仓」↔ 资金曲线）：整个换，不能把格子 id 补到别的块上
    else if ((MORPH_KEEP.has(o.id) || MORPH_KEEP.has(n.id)) && o.id !== n.id) from.replaceChild(n, o);
    else if (o.nodeType !== 1) { if (o.nodeValue !== n.nodeValue) o.nodeValue = n.nodeValue; }
    // 输入框标记没变就整个留着（打到一半的字、焦点、光标都在）；变了照旧换新的
    else if (MORPH_FORM.has(o.nodeName)) { if (o.outerHTML !== n.outerHTML) from.replaceChild(n, o); }
    // 展开的折叠块留着展开；标题换了说明已经是另一条记录，换新的（收起）
    else if (o.nodeName === "DETAILS" && summaryText(o) !== summaryText(n)) from.replaceChild(n, o);
    else {
      morphAttrs(o, n);
      if (!MORPH_KEEP.has(o.id)) morph(o, n);
    }
  }
  for (let i = b.length; i < a.length; i++) from.removeChild(a[i]);
}
const summaryText = (d) => { const s = d.firstElementChild; return s && s.nodeName === "SUMMARY" ? s.textContent : ""; };
function morphAttrs(o, n) {
  for (const { name, value } of n.attributes) if (o.getAttribute(name) !== value) o.setAttribute(name, value);
  for (const { name } of [...o.attributes]) if (!n.hasAttribute(name) && !(name === "open" && o.nodeName === "DETAILS")) o.removeAttribute(name);
}

function render() {
  chrome();
  setHtml($("#status"), statusBar());
  const { page, arg } = route();
  // 搜索框里正在用输入法拼字：这次定时刷新先不重画，免得把拼到一半的字打断
  if (page === "settings" && S.stComposing) return;
  const needs = PAGE_API[page];
  const did = needs ? dataId(page, arg) : null;
  if (needs && pageErr && pageErr[0] === did) {
    $("#page").innerHTML = `<div class="card"><div class="empty"><b>这一页读不出来</b><span>${esc(pageErr[1])}</span><span>${STALE_HINT}</span></div></div>`;
    return;
  }
  const hit = needs && pageKey !== did ? cachedPage(page, did, Date.now()) : null;
  if (hit) {
    pageData = hit;
    pageKey = did;
  }
  if (!data || (needs && (!pageData || pageKey !== did))) {
    $("#page").innerHTML = `<div class="card"><div class="empty"><b>正在读取…</b></div></div>`;
    return;
  }
  // 页面里能自己滚的块（竖着的日志、横着的宽表格）：重画前记住位置，画完按顺序放回去
  const boxes = () => [...document.querySelectorAll("#page .scroll, #page .tw, #page .tiers-scroll")];
  const scrolls = boxes().map((e) => [e.scrollTop, e.scrollLeft]);
  // 页面上的输入框（选币页“手动加币”）：定时刷新重画时保住正在输入的内容和焦点
  const typing = document.activeElement && document.activeElement.id === "pool-add";
  const typed = $("#pool-add") ? $("#pool-add").value : "";
  // 设置页搜索框：每打一个字整页重画，焦点和光标要放回去
  const qid = document.activeElement && document.activeElement.id in SEARCH_BOX ? document.activeElement.id : null;
  const q = qid ? [$(`#${qid}`).selectionStart, $(`#${qid}`).selectionEnd] : null;
  // 定时刷新整页重画：先用旧高度撑住页面，免得重画中途变矮把滚动位置夹回顶部（团队页尤其明显）
  const pg = $("#page"), y = window.scrollY, same = pg.dataset.key === pageId(page, arg), samePage = pg.dataset.page === page;
  const html = (P[page] || placeholder)(arg);
  const fresh = !sameHtml(pg, html);
  if (fresh || !same) pg.style.minHeight = same ? pg.offsetHeight + "px" : "";
  pg.dataset.key = pageId(page, arg);
  pg.dataset.page = page;
  setHtml(pg, html, samePage);
  if (samePage) boxes().forEach((e, i) => { if (scrolls[i]) [e.scrollTop, e.scrollLeft] = scrolls[i]; });
  const box = $("#pool-add");
  if (box && typed) box.value = typed;
  if (box && typing) box.focus();
  const qbox = qid && $(`#${qid}`);
  if (qbox && q) { qbox.focus(); qbox.setSelectionRange(...q); }
  if (page === "settings" && S.stScroll) {
    const sec = $(`#st-r-${S.stScroll}`);
    S.stScroll = null;
    if (sec) requestAnimationFrame(() => sec.scrollIntoView({ block: "start" }));
  }
  if (page === "positions") mountChart();
  if (page === "history") mountHistoryChart();
  if (page === "team") fitTeamFlow();
  if (page === "settings" && !samePage) {
    // 窄屏档位卡横着滑：一进来先把当前档滑到看得见
    const sc = $("#page .tiers-scroll"), cur = $("#page .tier.cur");
    if (sc && cur && cur.offsetLeft + cur.offsetWidth > sc.clientWidth) sc.scrollLeft = cur.offsetLeft - 16;
  }
  if (same && fresh) {
    if (window.scrollY !== y) window.scrollTo(0, y);
    // 新内容够高就放开；不够高（比如列表变短了）留着撑住，换页时再放开
    requestAnimationFrame(() => {
      const last = pg.lastElementChild;
      const bottom = last ? last.getBoundingClientRect().bottom + window.scrollY : 0;
      if (bottom >= window.scrollY + innerHeight) pg.style.minHeight = "";
    });
  }
}

// 5 秒没回就算失败；出错带上状态码和接口给的说明
async function getJson(url) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), 5000);
  try {
    let r;
    try { r = await fetch(url, { cache: "no-store", signal: ctl.signal }); } catch (e) { throw new Error(e.name === "AbortError" ? "5 秒没有回应" : "连不上面板服务"); }
    if (!r.ok) {
      let detail = "";
      try { const j = await r.json(); detail = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch (e) { /* 不是 JSON */ }
      throw new Error(`${url.split("?")[0]} 返回 ${r.status}${detail ? "：" + detail : ""}`);
    }
    return await r.json();
  } finally {
    clearTimeout(timer);
  }
}

/* 币安风格成交提示音：开仓成交（持仓变成「持仓中」）响一声上扬的双音铃，平掉（持仓从列表里消失）响一声下沉的。
   声音是用 Web Audio 现合成的仿币安提示音（不带币安的音频文件）；只在这台设备、这个浏览器里响，
   浏览器要先在页面上点过一下才允许出声。测试单一样响。 */
const soundOn = () => { try { return localStorage.getItem("trade-sound") !== "off"; } catch (err) { return true; } };
function setSoundOn(on) { try { localStorage.setItem("trade-sound", on ? "on" : "off"); } catch (err) { /* 存不了就只对这次生效 */ } }
const soundVol = () => { try { const v = +localStorage.getItem("trade-sound-vol"); return localStorage.getItem("trade-sound-vol") == null || !Number.isFinite(v) ? 60 : Math.max(0, Math.min(100, v)); } catch (err) { return 60; } };
let audioCtx = null;
function audio() {
  if (!audioCtx) { try { audioCtx = new (window.AudioContext || window.webkitAudioContext)(); } catch (err) { return null; } }
  if (audioCtx.state === "suspended") audioCtx.resume().catch(() => {});
  return audioCtx;
}
// 浏览器要求先有一次点击 / 按键才放声音：第一次操作时把声音通道准备好
["pointerdown", "keydown"].forEach((ev) => document.addEventListener(ev, () => { if (soundOn()) audio(); }, { once: true, capture: true }));
// 两个音、隔 0.09 秒、每个带一点八度泛音、0.25 秒衰减完，整段约 0.3 秒：开仓往上走（E6 → A6），平仓往下走（A5 → E5）
const TONES = { open: [1318.5, 1760], close: [880, 659.3] };
function playTone(kind) {
  const ctx = audio();
  const vol = soundVol() / 100;
  if (!ctx || !TONES[kind] || vol <= 0) return;
  const t0 = ctx.currentTime + 0.02;
  TONES[kind].forEach((f, n) => {
    const t = t0 + n * 0.09;
    [[f, 1], [f * 2, 0.18]].forEach(([hz, part]) => {
      const o = ctx.createOscillator(), g = ctx.createGain();
      o.type = "sine";
      o.frequency.value = hz;
      g.gain.setValueAtTime(0.0001, t);
      g.gain.exponentialRampToValueAtTime(Math.max(0.0002, 0.35 * vol * part), t + 0.008);
      g.gain.exponentialRampToValueAtTime(0.0001, t + 0.25);
      o.connect(g).connect(ctx.destination);
      o.start(t);
      o.stop(t + 0.26);
    });
  });
}
document.addEventListener("change", (e) => {
  if (!e.target || e.target.id !== "sound-vol") return;
  try { localStorage.setItem("trade-sound-vol", String(+e.target.value)); } catch (err) { /* 存不了就只对这次生效 */ }
  playTone("open");
});
let seenPos = null;  // position_id → status；第一次拉到只记下来不响
function tradeSounds(list) {
  const now = new Map(list.map((p) => [p.position_id, p.status]));
  if (seenPos && soundOn()) {
    const opened = [...now].some(([id, st]) => st === "open" && seenPos.get(id) !== "open" && seenPos.get(id) !== "closing");
    // 成交过的（持仓中 / 平仓中）才算平单；还在挂单就没了的是没成交，不响
    const closed = [...seenPos].some(([id, st]) => (st === "open" || st === "closing") && !now.has(id));
    if (opened) playTone("open");
    if (closed && opened) setTimeout(() => playTone("close"), 400);
    else if (closed) playTone("close");
  }
  seenPos = now;
}

async function poll() {
  const { page, arg } = route();
  const seq = ++pollSent;
  try {
    // 总览和这一页自己的接口一起拉（原来是先等总览、再拉这一页，慢一点就多等一轮）
    const api = PAGE_API[page];
    const id = api ? dataId(page, arg) : null;
    const pageReq = api ? getJson(api(arg)).then((d) => [d, null], (e) => [null, e]) : null;
    const ov = await getJson("/api/overview");
    const [d, e] = pageReq ? await pageReq : [null, null];
    if (!pollIsLatest(seq)) return;
    data = ov;
    TZ = data.display_tz || TZ;
    tradeSounds(data.positions || []);
    if (e) pageErr = [id, e.message];
    else if (pageReq) {
      pageData = d;
      pageKey = id;
      pageCache.set(id, { d, at: Date.now() });
      pageErr = null;
    }
    lastOk = Date.now();
    $("#stale").innerHTML = "";
    // 标签页在后台：照常拉数（成交提示音靠它），不重画，切回来时补画一次
    if (document.hidden) return;
    render();
    loadKlines(false);
  } catch (e) {
    if (seq < pollShown) return;
    const ago = lastOk ? Math.round((Date.now() - lastOk) / 1000) + " 秒前" : "从未";
    $("#stale").innerHTML = `<div class="stale">面板服务读不出（${esc(e.message)}；上次成功：${ago}）</div>`;
    if (!data) $("#page").innerHTML = `<div class="card"><div class="empty"><b>面板读不出数据</b><span>${esc(e.message)}</span><span>${STALE_HINT}</span></div></div>`;
  }
}

if (location.pathname !== "/setup") { // 首次设置向导在 setup.js
  window.addEventListener("hashchange", () => { closeDlg(); render(); poll(); });
  document.addEventListener("visibilitychange", () => { if (!document.hidden && data) { render(); loadKlines(false); } });
  render();
  loadPrecision();
  // 持仓页 2 秒一轮（现价、浮盈、K 线要跟得上），其它页 5 秒
  const loop = async () => { await poll(); setTimeout(loop, route().page === "positions" ? 2000 : 5000); };
  loop();
}
