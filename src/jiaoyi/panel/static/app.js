// 面板前端：每 5 秒拉一次 /api/overview（状态条 + 总览），当前页另有数据就再拉自己的接口。
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
const BIAS = { long: ["偏多", "p-long"], short: ["偏空", "p-short"], both: ["双向", "p-accent"], no_trade: ["观望", ""], unknown: ["?", ""] };
const SIDE = { long: ["多", "up"], short: ["空", "down"] };
const LIFEBOAT = { active: "保命 ✓", pending: "⚠ 保命挂单中", missing: "⚠ 保命缺失", triggered: "保命已触发", cancelled: "保命已撤" };
const ROLE = { executor: "执行器", reconciler: "对账员", lifeboat: "保命", gate: "拍板", risk_guard: "保镖", commander: "军师团", keeper: "管家", patrol: "巡逻兵", sentinel: "哨兵", screener: "选币", predictor: "预测员", alerting: "告警", book_reader: "盘口员" };
const OUTCOME = { pass: ["放行", "p-long"], reject: ["拒绝", "p-short"], hold: ["拿住", ""], close: ["平仓", "p-accent"], action: ["动作", "p-accent"], error: ["出错", "p-warn"], info: ["信息", ""] };
const CAT = { major: "主流币", alt: "山寨币", crypto_stock: "加密概念股", us_tech: "美股科技", precious_metal: "贵金属", other: "其他" };
const SIGNAL = { volume_breakout: "放量突破", volume_surge: "量能突增", liquidation_rebound: "爆仓反弹", crowded_reversal: "拥挤反向", session_event: "时段与事件" };

let TZ = "Asia/Bangkok";
try { if (localStorage.getItem("updown") === "red-up") document.documentElement.dataset.updown = "red-up"; } catch (err) { /* 隐私模式等读不了就用默认 */ }
let data = null; // /api/overview
let pageData = null; // 当前页自己的接口
let pageKey = "";
let pageErr = null; // [pageId, 出错说明]：读不出就显示错误，不能一直“正在读取…”
const STALE_HINT = "多半是代码更新了、面板进程还是旧的：在 Mac 终端按 Ctrl+C 停掉面板，再运行 <code>jiaoyi panel</code>。";
let lastOk = 0;
const S = { tf: "1m" };

const hm = (ts) => new Intl.DateTimeFormat("zh-CN", { timeZone: TZ, hour: "2-digit", minute: "2-digit", hour12: false }).format(new Date(ts));
const hms = (ts) => new Intl.DateTimeFormat("zh-CN", { timeZone: TZ, hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(new Date(ts));
const money = (x, d = 1) => (x == null ? "—" : x.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d }));
// U 本位合约的数量按币计；旁边折算成 USDT 名义金额（数量 × 开仓价），一眼看出这单多大
const qty = (q, px) => `${q}${px ? ` <span class="dim">≈${money(q * px, 0)}U</span>` : ""}`;
const signed = (x, d = 1) => (x == null ? "—" : (x < 0 ? "−" : "+") + money(Math.abs(x), d));
const cls = (x) => (x == null || x === 0 ? "" : x > 0 ? "up" : "down");
const coin = (s) => esc(String(s || "").replace(/USDT$/, ""));
const price = (p) => (p == null ? "—" : p >= 100 ? money(p, 1) : p >= 1 ? money(p, 3) : String(+p.toPrecision(4)));
const range = (r, unit = "", d) => (r ? `${money(r[0], d ?? (r[0] < 10 ? 1 : 0))}–${money(r[1], d ?? (r[1] < 10 ? 1 : 0))}${unit}` : "—");
const dur = (ms) => { const m = Math.max(0, Math.round(ms / 60000)); return m < 60 ? `${m} 分` : `${Math.floor(m / 60)} 时 ${m % 60} 分`; };
// 图表坐标轴按曼谷时间显示：跨天的刻度显示日期，其余显示时分
const tick = (t) => { const d = new Date(t * 1000); const s = hm(d); return s === "00:00" ? new Intl.DateTimeFormat("zh-CN", { timeZone: TZ, month: "numeric", day: "numeric" }).format(d) : s; };
const pill = (o) => { const [t, k] = OUTCOME[o] || [o, ""]; return `<span class="pill ${k}">${t}</span>`; };

function route() {
  const [page, arg] = (location.hash.slice(1) || "overview").split("/");
  return { page: PAGES.some((p) => p[0] === page) ? page : "overview", arg: arg ? decodeURIComponent(arg) : null };
}

/* ---------- 外壳 */
function chrome() {
  const r = route().page;
  $("#tabs").innerHTML = PAGES.map(
    ([k, n]) => `<a href="#${k}" class="${k === r ? "on" : ""}"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">${ICON[k]}</svg>${n}</a>`,
  ).join("");
  const pg = PAGES.find((p) => p[0] === r);
  const mode = data ? MODE[data.mode] || data.mode : "…";
  const now = data ? hm(data.now) : "--:--";
  const c = data ? data.control : null;
  const pauseBtn = c && c.paused
    ? `<button class="btn b-pause" ${clickable("cmd", "resume")}>恢复开新仓</button>`
    : `<button class="btn b-pause" ${clickable("cmd", "pause")}>暂停开新仓</button>`;
  $("#pghead").innerHTML = `<h1>${pg[1]}</h1><span class="sub">${esc(mode)} · 曼谷 ${now} · ${pg[2]}</span>
    <div class="acts">${data ? pauseBtn : ""}${data ? `<button class="btn b-kill" ${clickable("cmd", "close_all")}>全部平仓</button>` : ""}</div>`;

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
  $("#banners").innerHTML = b;
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
    <div class="kpi"><small>今日盈亏</small><b class="${cls(k.day_pnl_usdt)}">${signed(k.day_pnl_usdt)} U <span class="p">${k.day_pnl_pct == null ? "" : signed(k.day_pnl_pct, 2) + "%"}</span></b></div>
    <div class="kpi"><small>离熔断（−${k.halt_pct}%）</small><b class="${k.halted ? "down" : ""}" style="font-size:15px">${k.halted ? "已熔断" : k.to_halt_usdt == null ? "—" : "还差 " + money(k.to_halt_usdt, 0) + " U"}</b><div class="meter ${k.halted ? "full" : ""}"><span style="width:${pct}%"></span></div></div>
    <div class="kpi"><small>敞口</small><b style="font-size:15px">${k.exposure_x == null ? "—" : k.exposure_x.toFixed(2)} / ${k.max_exposure_x} 倍</b></div>
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
  refresh_cards: { title: "重新开会出卡", text: "让军师团马上开一次会、重出作战卡（会花一次大模型费用）。", ok: "开会" },
};
const STATUS = { done: ["已执行", "up"], rejected: ["主程序拒绝了", "down"], expired: ["过期没执行（主程序没在跑？）", "down"], failed: ["执行失败", "down"] };
let onOk = null;

async function post(url, body) {
  const r = await fetch(url, { method: "POST", cache: "no-store", headers: { "Content-Type": "application/json", "X-Jiaoyi-Panel": "1" }, body: JSON.stringify(body) });
  let j = {};
  try { j = await r.json(); } catch (e) { /* 非 JSON 也当失败 */ }
  if (!r.ok) throw new Error(typeof j.detail === "string" ? j.detail : `出错了（${r.status}）`);
  return j;
}

// 带确认按钮的弹窗：body 里有 input 就一起交给 submit；submit 抛错会显示在弹窗里
function ask(title, body, ok, danger, submit) {
  $("#layer").innerHTML = `<div class="scrim" data-close="1"><div class="dlg" role="dialog" aria-modal="true"><h3>${title}</h3>${body}
    <div class="msg small" id="dlg-msg"></div>
    <div class="acts"><button class="btn" data-close="1">取消</button><button class="btn ${danger ? "b-kill" : "b-go"}" data-ok="1">${ok}</button></div></div></div>`;
  onOk = submit;
  const first = $("#layer input") || $("#layer [data-ok]");
  first.focus();
}
function dlgMsg(html, k = "") { const m = $("#dlg-msg"); if (m) { m.className = "msg small " + k; m.innerHTML = html; } }

function pwField(c) {
  if (!c.pw_set) return `<div class="note warn">还没有设置面板密码：先在 Mac mini 终端运行 <code>jiaoyi set-panel-password</code>。</div>`;
  return `<label class="pw">面板密码<input type="password" id="dlg-pw" autocomplete="current-password"></label><div class="dim small">连错 5 次锁 15 分钟。</div>`;
}

// 设置页常显：有改动没生效时高亮「重启生效」，否则是普通的「重启主程序」（验收时随时要重启）
function restartBtn() {
  const pending = data.control.config_pending === true;
  return `<button class="btn b-sm ${pending ? "b-go" : ""}" ${clickable("cmd", "restart")} ${data.running ? "" : 'disabled title="主程序没在运行"'}>${pending ? "重启生效" : "重启主程序"}</button>`;
}

function askCommand(kind, params = {}, what = "") {
  const k = CMD[kind];
  const c = data.control;
  const startedBefore = c.started_at || null;
  const warn = data.running ? "" : `<div class="note warn">主程序没在运行，指令 60 秒内没人执行就作废。</div>`;
  const title = kind === "restart" && c.config_pending !== true ? "重启主程序" : k.title;
  ask(`${title}${what ? " · " + esc(what) : ""}`, `<p>${k.text}</p>${warn}${k.pw ? pwField(c) : ""}`, k.ok, k.danger, async () => {
    const pw = $("#dlg-pw");
    const res = await post("/api/command", { kind, params, password: pw ? pw.value : null });
    onOk = null;
    $("#layer [data-ok]").remove();
    $("#layer [data-close].btn").textContent = "关闭";
    dlgMsg("已发给主程序，等它执行…");
    for (let i = 0; i < 70; i++) {
      await new Promise((r) => setTimeout(r, 1000));
      if (!$("#dlg-msg")) return; // 弹窗关了
      let st;
      try { st = await getJson(`/api/command/${encodeURIComponent(res.id)}`); } catch (e) { continue; }
      if (st.status !== "pending") {
        const [t, cl] = STATUS[st.status] || [st.status, ""];
        dlgMsg(`<b class="${cl}">${t}</b>${st.detail ? "　" + esc(st.detail) : ""}`);
        if (kind === "restart" && st.status === "done") return waitRestart(startedBefore);
        poll();
        return;
      }
    }
    dlgMsg("一直没有回执：主程序可能没在运行。", "down");
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
  else if (act === "signal") showSignal(arg);
  else if (act === "meeting") showMeeting(arg);
  else if (act === "record") showRecord(+arg);
  else if (act === "tf") { S.tf = arg; loadKlines(true); render(); }
  else if (act === "hist") { S.hist = arg; render(); poll(); }
  else if (act === "rej") { S.rej = !S.rej; render(); }
  else if (act === "tests") { S.tests = !S.tests; render(); poll(); }
  else if (act === "csv") exportCsv();
  else if (act === "cmd") askCommand(arg);
  else if (act === "close1") { const p = (pageData && pageData.positions || []).find((x) => x.position_id === arg); askCommand("close_position", { position_id: arg }, p ? `${p.symbol.replace(/USDT$/, "")} ${(SIDE[p.side] || [p.side])[0]} ${p.quantity}` : arg); }
  else if (act === "unblock") askCommand("unblock_symbol", { symbol: arg }, arg);
  else if (act === "edit1") editOne(arg, false);
  else if (act === "rec1") editOne(arg, true);
  else if (act === "st-item") toggleItem(el, arg);
  else if (act === "st-tab") {
    S.stTab = arg;
    try { localStorage.setItem("settings-tab", arg); } catch (err) { /* 记不住就每次回到默认 */ }
    render();
  }
  else if (act === "st-changes") showChanges();
  else if (act === "profile") switchProfile(arg);
  else if (act === "clearpins") askClearPins();
  else if (act === "pool") { const [kind, sym] = arg.split(":"); askCommand(kind, { symbol: sym }, sym.replace(/USDT$/, "")); }
  else if (act === "pooladd") addSymbol();
  else if (act === "unpin") unpin(arg);
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

// 关键位分「支撑 / 压力」：有现价就按在现价下方 / 上方分（同慢环落卡），各自由近到远，标距现价百分比
S.px = {};
function levelsHtml(levels, px) {
  if (!levels || !levels.length) return "";
  const below = (l) => (px ? l.price < px : l.kind === "support");
  const dist = (l) => (px ? Math.abs(l.price - px) : 0);
  const fmt = (l) => `${price(l.price)}${px ? `<span class="dim">（${l.price >= px ? "+" : "−"}${(Math.abs(l.price - px) / px * 100).toFixed(1)}%）</span>` : ""}`;
  const group = (name, list) => (list.length ? `<div class="small"><span class="dim">${name}</span> ${list.sort((a, b) => dist(a) - dist(b)).map(fmt).join(" · ")}</div>` : "");
  return `${group("支撑", levels.filter(below))}${group("压力", levels.filter((l) => !below(l)))}${px ? `<div class="dim small">现价 ${price(px)}</div>` : ""}`;
}

// 弹窗打开时取一次各币现价（1 分钟线最后一根收盘），拿到后重画；取不到就只按类型分组、不标百分比
async function loadCardPrices(c) {
  const syms = c.symbols.filter((s) => s.key_levels && s.key_levels.length && !(s.symbol in S.px)).map((s) => s.symbol);
  if (!syms.length) return;
  await Promise.all(syms.map(async (sym) => {
    try {
      const bars = await getJson(`/api/klines?symbol=${encodeURIComponent(sym)}&interval=1m`);
      if (bars.length) S.px[sym] = bars[bars.length - 1].close;
    } catch (e) { /* 拿不到现价：照旧显示 */ }
  }));
  if ($("#layer .dlg.wide") && Object.keys(S.px).length) showCard(false);
}

function showCard(fetchPrices = true) {
  const c = data && data.card;
  if (!c) return;
  if (fetchPrices) { S.px = {}; loadCardPrices(c); }
  // 观望 = 军师团看过、这一轮不开仓：默认收成一行，只把有方向的币摊开
  const [act, idle] = splitIdle(c.symbols);
  const row = (s) => {
    const [t, k] = BIAS[s.bias] || [s.bias, ""];
    return `<tr><td class="hi">${coin(s.symbol)}<div class="dim small">${esc(CAT[s.category] || s.category)}</div></td><td><span class="pill ${k}">${t}</span></td>
      <td>${range(s.stop_atr, "", 1)}</td><td>${range(s.take_profit_r, "", 1)}</td><td>${range(s.leverage, "", 0)}</td><td>${range(s.notional_usdt, "", 0)}</td><td>${esc(s.size_tier_max || "—")}</td>
      <td class="txt">${esc(s.reason)}${s.allowed_signals.length && s.allowed_signals.length < 5 ? `<div class="dim small">只做：${s.allowed_signals.map((x) => esc(SIGNAL[x] || x)).join("、")}</div>` : ""}${levelsHtml(s.key_levels, S.px[s.symbol])}<div class="small"><a ${clickable("meeting", s.symbol)}>看这个币的开会过程 ›</a></div></td></tr>`;
  };
  const head = '<thead><tr><th>币种</th><th>方向</th><th>止损 ATR</th><th>止盈 R</th><th>杠杆</th><th title="军师团建议的名义仓位范围（已含杠杆），最终由保镖按风险算">单笔仓位（含杠杆）U</th><th>最大档</th><th>理由</th></tr></thead>';
  dlg(`<h3>作战卡${c.version ? " v" + c.version : ""} · ${c.symbols.length} 个标的</h3>
    <p class="dim">${hm(c.created_at)} 出卡 · ${c.expires_at ? "有效到 " + hm(c.expires_at) : "有效期未知"}${c.valid ? "" : " · <span class='down'>已过期</span>"}</p>
    ${c.regime ? `<div class="note"><b>军师纪要</b>　${esc(c.regime)}</div>` : ""}
    ${act.length ? `<div class="tw"><table>${head}<tbody>${act.map(row).join("")}</tbody></table></div>` : '<div class="dim">这一轮没有要做的币，全部观望。</div>'}
    ${idle.length ? `<details class="mt"><summary class="dim">${idleLine(idle)}（这一轮不开仓）· 点开看理由</summary><div class="tw"><table>${head}<tbody>${idle.map(row).join("")}</tbody></table></div></details>` : ""}`);
  $("#layer .dlg").classList.add("wide");
}

async function showSignal(id) {
  if (!id) return;
  dlg(`<h3>信号详情</h3><div class="dim">读取中…</div>`);
  try {
    const r = await fetch(`/api/signal/${encodeURIComponent(id)}`, { cache: "no-store" });
    const chain = await r.json();
    window.__recs = chain;
    const steps = chain.map((s, i) => `<div class="step"><div class="t"><span class="n">${hms(s.ts)}</span>${esc(ROLE[s.role] || s.role)} ${pill(s.outcome)}${s.latency_ms ? `<span class="dim small">${Math.round(s.latency_ms)} ms</span>` : ""}</div>
      <div>${esc(s.text)}</div>${keyFacts(s)}<details><summary>完整快照</summary><div class="small dim">看到的</div>${json(s.input)}<div class="small dim">判断</div>${json(s.output)}</details></div>`).join("");
    dlg(`<h3>信号详情</h3><p class="dim small">${esc(id)}</p><div class="steps">${steps || '<div class="dim">今天的日志里找不到这个信号。</div>'}</div>`);
  } catch (e) {
    dlg(`<h3>信号详情</h3><div class="down">读取失败</div>`);
  }
}

// 拍板人的概率、保镖的理由：挑出来直接显示，其余放“完整快照”
function keyFacts(s) {
  const o = s.output || {};
  const facts = [];
  for (const [k, v] of Object.entries(o)) {
    if (typeof v === "number" && /^p_|prob/.test(k)) facts.push(`${esc(k)} ${v.toFixed(2)}`);
  }
  if (Array.isArray(o.reasons)) facts.push(...o.reasons.map(esc));
  return facts.length ? `<div class="facts">${facts.map((f) => `<span class="pill">${f}</span>`).join(" ")}</div>` : "";
}

function showRecord(i) {
  const s = (window.__recs || [])[i];
  if (!s) return;
  dlg(`<h3>${esc(ROLE[s.role] || s.role)} · ${hms(s.ts)} ${pill(s.outcome)}</h3><p>${esc(s.text)}</p>${keyFacts(s)}
    <div class="small dim">看到的</div>${json(s.input)}<div class="small dim">判断</div>${json(s.output)}
    <p class="dim small">耗时 ${Math.round(s.latency_ms)} ms · 花费 $${s.cost_usd.toFixed(4)}</p>`);
}

/* ---------- 开会过程（慢环按阶段写 data/meeting_live/<币>.json；正在开的每 4 秒刷新） */
const STAGE = { analysts: "分析师报告", debate: "多空辩论", trader: "交易员计划", risk: "风控辩论", decision: "结论" };
const WHO = {
  market: "市场分析师", social: "情绪分析师", news: "新闻分析师", fundamentals: "基本面分析师",
  bull: "多头研究员", bear: "空头研究员", research_manager: "研究主管", trader: "交易员",
  aggressive: "激进派风控", conservative: "保守派风控", neutral: "中立派风控", risk_manager: "风控主管", convert: "出作战卡",
};
const MEET_STATUS = { running: ["正在开", "p-accent"], done: ["开完了", "p-long"], failed: ["没开成", "p-short"], stale: ["中断了", "p-warn"] };
const meetPill = (s) => { const [t, k] = MEET_STATUS[s] || [s, ""]; return `<span class="pill ${k}">${t}</span>`; };
const stageNo = (s) => Object.keys(STAGE).indexOf(s) + 1;
const meetTime = (m) => (m.status === "running" && m.started_at ? `已开 ${dur(Date.now() - Date.parse(m.started_at))}` : m.started_at && (m.finished_at || m.updated_at) ? `${hm(m.started_at)} 开，用时 ${dur(Date.parse(m.finished_at || m.updated_at) - Date.parse(m.started_at))}` : "");
const meetBias = (m) => (m.bias ? (() => { const [t, k] = BIAS[m.bias] || [m.bias, ""]; return `<span class="pill ${k}">${t}</span>`; })() : "");

function meetingsCard(list) {
  const rows = list.map((m) => `<div class="click" ${clickable("meeting", m.symbol)}><span class="n">${m.updated_at ? hm(m.updated_at) : ""}</span><span><b>${coin(m.symbol)}</b> ${meetPill(m.status)} ${meetBias(m)}
    <span class="dim small">${m.status === "running" && m.stage ? `第 ${stageNo(m.stage) || "?"} / 5 步 · ${esc(STAGE[m.stage] || m.stage)} · ` : ""}${meetTime(m)}</span></span></div>`).join("");
  return `<section class="card mt"><div class="ch"><h2>开会过程</h2><span class="r">每个币最近一场会 · 点开看每个人说了什么</span></div>
    <div class="log">${rows || '<div class="dim">还没有开会记录。慢环升级以后，每场会的过程都会记下来。</div>'}</div></section>`;
}

let meetTimer = null;
S.meetOpen = new Set();
function showMeeting(sym) {
  dlg(`<h3>${coin(sym)} · 开会过程</h3><div id="meet"><div class="dim">读取中…</div></div>`);
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
    if (box()) box().innerHTML = `<div class="dim">${/ 404/.test(e.message) ? "这个币还没有开会记录。慢环升级以后，每场会的过程都会记下来。" : esc(e.message)}</div>`;
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
  const known = Object.keys(STAGE);
  const stages = [...known, ...new Set(m.steps.map((s) => s.stage).filter((s) => !known.includes(s)))];
  const groups = stages.map((st) => {
    const items = m.steps.map((s, i) => [s, i]).filter(([s]) => s.stage === st);
    if (!items.length) return "";
    return `<div class="lbl">${esc(STAGE[st] || st || "其他")}</div>${items.map(([s, i]) => `<details class="say" data-step="${i}" ${S.meetOpen.has(i) ? "open" : ""}>
      <summary><b>${esc(WHO[s.who] || s.who || "—")}</b><span class="dim small">${s.ts ? hms(s.ts) : ""}</span><span class="dim small one">${esc(s.text.slice(0, 80))}</span></summary><div class="txt">${esc(s.text)}</div></details>`).join("")}`;
  }).join("");
  return `<p class="dim">${meetPill(m.status)} ${meetBias(m)} ${meetTime(m)}${m.cost_usd ? ` · 花费 $${m.cost_usd.toFixed(2)}` : ""}${m.status === "running" ? " · 自动刷新" : ""}</p>
    <div class="stgs">${chips}</div>
    ${m.status === "stale" ? '<div class="note warn">标着“正在开”，但 15 分钟没有新进展：多半是主程序重启把会打断了，下一轮会重开。</div>' : ""}
    ${m.error ? `<div class="note warn">出错：${esc(m.error)}</div>` : ""}
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
      <div class="empty"><b>今天还没有作战卡</b>没有有效作战卡就不开新仓；已有持仓照常盯。</div></section>`;
  }
  const [act, idle] = splitIdle(c.symbols);
  const shown = act.slice(0, 8);
  const more = act.length - shown.length;
  const pills = shown.map((s) => { const [t, k] = BIAS[s.bias] || [s.bias, ""]; return `<span class="pill ${k}">${coin(s.symbol)} ${t}</span>`; }).join("");
  const until = c.expires_at ? `有效到 ${hm(c.expires_at)}` : "有效期未知";
  return `<section class="card click" ${clickable("card")}>
    <div class="ch"><h2>作战卡</h2><span class="pill">军师团${c.version ? " v" + c.version : ""}</span>
      <span class="r">${hm(c.created_at)} 更新 · ${c.valid ? until : '<span class="down">已过期，不开新仓</span>'} <button class="btn b-sm" ${clickable("cmd", "refresh_cards")}>重新开会</button></span></div>
    <div class="row">${pills || '<span class="dim">这一轮没有要做的币</span>'}${more > 0 ? `<span class="dim small">+${more} 个</span>` : ""}</div>
    ${idle.length ? `<div class="dim small">${idleLine(idle)}（不开仓）</div>` : ""}
    <dl class="kv"><dt>止损</dt><dd>${range(c.stop_atr, " ATR")}</dd><dt>杠杆</dt><dd>${c.leverage_max == null ? "—" : "≤ " + c.leverage_max + " 倍"}</dd><dt title="军师团建议的名义仓位范围（已含杠杆），最终由保镖按风险算">单笔仓位（含杠杆）</dt><dd title="军师团建议的名义仓位范围（已含杠杆），最终由保镖按风险算">${range(c.notional_usdt, " U")}</dd></dl>
    ${c.regime ? `<div class="note"><b>军师纪要</b>　${esc(c.regime)}</div>` : ""}
    <div class="foot">点开看每个币的方向、止损、杠杆和理由 ›</div>
  </section>`;
}

function feedSection(feed, signals) {
  const body = feed.length
    ? `<div class="feed scroll">${feed.map((e) => `<div class="ev ${e.kind} ${e.signal_id ? "click" : ""}" ${e.signal_id ? clickable("signal", e.signal_id) : ""}><div class="t"><span class="n">${hm(e.ts)}</span>${esc(ROLE[e.role] || e.role)}${e.symbol ? " · " + coin(e.symbol) : ""}</div><div class="d">${esc(e.text)}</div></div>`).join("")}</div>`
    : `<div class="empty"><b>今天还没有决策</b>哨兵已触发 ${signals} 个信号。送拍板后会在这里出现。</div>`;
  return `<section class="card"><div class="ch"><h2>信号与决策流</h2><span class="r">今天 · 最新在上</span></div>${body}
    <div class="foot">绿 = 放行 / 开仓 · 黄 = 拍板放弃 · 红 = 保镖拒绝 · 蓝 = 平仓。点一条看整条链和快照。</div></section>`;
}

function posSection(list) {
  const body = list.length
    ? list.map((p) => {
        const [s, sc] = SIDE[p.side] || [p.side, ""];
        const lb = LIFEBOAT[p.lifeboat_status] || "⚠ 没有保命单";
        const lbBad = p.lifeboat_status !== "active";
        return `<a class="pos click" href="#positions/${encodeURIComponent(p.position_id)}"><div class="top"><b>${coin(p.symbol)} <span class="${sc}">${s}</span> ${qty(p.quantity, p.entry_price)}</b>
          <span class="pnl ${cls(p.unrealized_usdt)}">${p.unrealized_usdt == null ? "—" : signed(p.unrealized_usdt, 2) + "U"}</span></div>
          <div class="meta"><span>开 ${price(p.entry_price)} · 损 ${price(p.stop_loss_price)} · 盈 ${price(p.take_profit_price)}</span></div>
          <div class="meta"><span class="${lbBad ? "warn" : ""}">${lb}${p.lifeboat_price ? " " + price(p.lifeboat_price) : ""}</span><span>管家：${esc(p.keeper || "还没看")}</span>${p.status !== "open" ? `<span class="pill p-warn">${esc(p.status)}</span>` : ""}</div></a>`;
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
    return `<div class="bar ${r >= 1 ? "full" : r >= 0.7 ? "hot" : ""}"><div class="lab"><span>${esc(CAT[g.category] || g.category)}</span><span class="num">${g.exposure_x.toFixed(2)} / ${g.cap_x} 倍</span></div><div class="tr"><span style="width:${Math.min(100, r * 100)}%"></span></div></div>`;
  }).join("");
  const now = new Date(data.now).getTime();
  const rows = d.positions.map((p) => {
    const [s, sc] = SIDE[p.side] || [p.side, ""];
    const on = p.position_id === sel.position_id;
    const held = now - new Date(p.opened_at).getTime();
    return `<tr class="click ${on ? "sel" : ""}" onclick="if (!event.target.closest('button')) location.hash='positions/${encodeURIComponent(p.position_id)}'">
      <td class="hi">${coin(p.symbol)} <span class="${sc}">${s}</span> ${qty(p.quantity, p.entry_price)} <span class="dim">${p.leverage}x</span></td>
      <td class="num">${price(p.entry_price)}</td><td class="r num ${cls(p.unrealized_usdt)}">${p.unrealized_usdt == null ? "—" : signed(p.unrealized_usdt, 2) + "U"}</td>
      <td class="num">${price(p.stop_loss_price)} / ${price(p.take_profit_price)}</td>
      <td class="${p.lifeboat_status === "active" ? "" : "warn"}">${LIFEBOAT[p.lifeboat_status] || "⚠ 没有保命单"} ${price(p.lifeboat_price)}</td>
      <td class="num">${dur(held)} / ${dur(p.time_limit_s * 1000)}</td><td>${esc(p.keeper || "—")}</td>
      <td class="r"><button class="btn b-sm b-danger" ${clickable("close1", p.position_id)}>平仓</button></td></tr>`;
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
    <section class="card mt"><div class="tw"><table>
      <thead><tr><th>币种</th><th>开仓价</th><th class="r">浮盈亏</th><th>止损 / 止盈</th><th>保命单</th><th>已持有 / 上限</th><th>管家</th><th></th></tr></thead>
      <tbody>${rows}</tbody></table></div></section>
    <section class="card mt">
      <div class="ch"><h2>${coin(sel.symbol)} <span class="${sc}">${s}</span> ${qty(sel.quantity, sel.entry_price)} · 详情</h2>
        <span class="r"><span class="seg">${["1m", "5m", "15m", "1h"].map((t) => `<button class="${S.tf === t ? "on" : ""}" ${clickable("tf", t)}>${t}</button>`).join("")}</span></span></div>
      <div class="chart" id="kchart-host"></div>
      <div class="legend small dim"><span><i style="border-color:var(--up)"></i>止盈</span><span><i style="border-color:var(--faint);border-style:solid"></i>开仓</span><span><i style="border-color:var(--sl)"></i>止损</span><span><i style="border-color:var(--down);border-style:dotted"></i>保命</span></div>
      <div><div class="lbl">从信号到现在的每一步（今天）· 点一条看快照</div><div class="log trail">${trail || '<div class="dim">今天的日志里没有这笔的记录。</div>'}</div></div>
    </section>`;
};

/* K 线图：图表 DOM 常驻，页面重画时搬回新的容器，免得每 5 秒闪一次 */
let chart = null, series = null, chartEl = null, chartKey = "", priceLines = [], linePrices = [];
function mountChart() {
  const host = $("#kchart-host");
  if (!host || !window.LightweightCharts) return;
  if (!chartEl) {
    chartEl = document.createElement("div");
    chartEl.style.height = "100%";
    host.appendChild(chartEl);
    chart = LightweightCharts.createChart(chartEl, {
      autoSize: true,
      layout: { background: { color: "transparent" }, textColor: getComputedStyle(document.body).getPropertyValue("--dim"), fontFamily: getComputedStyle(document.body).fontFamily },
      grid: { vertLines: { visible: false }, horzLines: { color: "rgba(0,0,0,0.05)" } },
      timeScale: { timeVisible: true, secondsVisible: false, borderVisible: false, tickMarkFormatter: (t) => tick(t) },
      rightPriceScale: { borderVisible: false },
      localization: { timeFormatter: (t) => hm(t * 1000) },
    });
    const css = getComputedStyle(document.documentElement);
    const up = css.getPropertyValue("--up").trim(), down = css.getPropertyValue("--down").trim();
    series = chart.addCandlestickSeries({
      upColor: up, downColor: down, borderVisible: false, wickUpColor: up, wickDownColor: down,
      // 止盈、保命这些线可能在 K 线范围外，把它们也包进可视范围
      autoscaleInfoProvider: (orig) => {
        const r = orig();
        const lines = linePrices.filter((v) => v != null);
        if (!r || !lines.length) return r;
        return { ...r, priceRange: { minValue: Math.min(r.priceRange.minValue, ...lines), maxValue: Math.max(r.priceRange.maxValue, ...lines) } };
      },
    });
  } else {
    host.appendChild(chartEl);
  }
}

async function loadKlines(force) {
  const { page, arg } = route();
  if (page !== "positions" || !pageData || !pageData.positions.length) return;
  mountChart();
  if (!series) return;
  const p = pageData.positions.find((x) => x.position_id === arg) || pageData.positions[0];
  const key = `${p.position_id}|${S.tf}`;
  try {
    const r = await fetch(`/api/klines?symbol=${encodeURIComponent(p.symbol)}&interval=${S.tf}`, { cache: "no-store" });
    if (!r.ok) throw new Error(String(r.status));
    series.setData(await r.json());
  } catch (e) {
    return;
  }
  priceLines.forEach((l) => series.removePriceLine(l));
  linePrices = [p.take_profit_price, p.entry_price, p.stop_loss_price, p.lifeboat_price];
  const css = getComputedStyle(document.documentElement);
  const c = (v) => css.getPropertyValue(v).trim();
  const L = LightweightCharts.LineStyle;
  priceLines = [
    [p.take_profit_price, "止盈", c("--up"), L.Dashed],
    [p.entry_price, "开仓", c("--faint"), L.Solid],
    [p.stop_loss_price, "止损", c("--sl"), L.Dashed],
    [p.lifeboat_price, "保命", c("--down"), L.Dotted],
  ].filter(([v]) => v != null).map(([v, t, color, style]) => series.createPriceLine({ price: v, title: t, color, lineStyle: style, lineWidth: 1, axisLabelVisible: true }));
  if (force || key !== chartKey) chart.timeScale().fitContent();
  chartKey = key;
}

/* ---------- 团队 */
P.team = () => {
  const d = pageData;
  const cards = d.roles.map((r) => `<a class="role ${r.role === d.selected ? "on" : ""} ${r.state === "bad" ? "bad" : ""}" href="#team/${r.role}">
      <div class="nm"><span class="lamp ${r.state}"><i></i></span>${esc(r.name)}</div>
      <small>${esc(r.headline || "—")}</small>
      <small class="num">今日 ${r.calls} 次${r.rejected ? ` · 拒 ${r.rejected}` : ""}${r.errors ? ` · <span class="down">错 ${r.errors}</span>` : ""}${r.cost_usd ? ` · $${r.cost_usd.toFixed(3)}` : ""}</small></a>`).join("");
  const sel = d.roles.find((r) => r.role === d.selected);
  window.__recs = d.records;
  const recs = d.records.map((s, i) => `<div class="click" ${clickable("record", i)}><span class="n">${hms(s.ts)}</span><span>${pill(s.outcome)} ${s.symbol ? "" : ""}${esc(s.text)}</span></div>`).join("");
  return `<div class="roles">${cards}</div>
    ${sel.role === "commander" ? meetingsCard(d.meetings || []) : ""}
    <section class="card mt"><div class="ch"><h2>${esc(sel.name)} · 今天做了什么</h2>
      <span class="r">${sel.last_beat ? "最后心跳 " + hms(sel.last_beat) : "没有心跳"}${sel.avg_latency_ms ? ` · 平均 ${Math.round(sel.avg_latency_ms)} ms` : ""}</span></div>
      ${sel.last_error ? `<div class="note warn">最近出错：${esc(sel.last_error)}</div>` : ""}
      <div class="log trail scroll tall">${recs || '<div class="dim">今天还没有记录。</div>'}</div>
      <div class="foot">最多显示最新 200 条 · 点一条看它看到了什么、怎么判断的</div></section>`;
};

/* ---------- 历史 */
const RANGE = { today: "今天", "7d": "7 天", "30d": "30 天" };
const REASON = { stop_loss: "止损", take_profit: "止盈", trailing_stop: "移动止损", time_limit: "到时", keeper: "管家平", patrol: "巡逻兵平", lifeboat: "保命触发", manual: "手动", reconciler: "对账平" };
S.hist = "today";
S.rej = false;
S.tests = false;

P.history = () => {
  const d = pageData;
  const tiles = [
    [signed(d.net_pnl_usdt, 2) + "U", "净盈亏（已扣手续费）", cls(d.net_pnl_usdt)],
    [d.trades + " 笔", "交易"],
    [d.win_rate == null ? "—" : Math.round(d.win_rate * 100) + "%", "胜率"],
    [d.avg_win_loss == null ? "—" : d.avg_win_loss.toFixed(2), "平均盈亏比"],
    [d.max_drawdown_usdt ? "−" + money(d.max_drawdown_usdt, 2) + "U" : "0", "最大回撤", d.max_drawdown_usdt ? "down" : ""],
  ];
  const kinds = d.by_kind.map((k) => `<tr><td class="hi">${esc(SIGNAL[k.kind] || ({ unknown: "未知", test: "测试单" })[k.kind] || k.kind)}</td><td class="r num">${k.trades}</td><td class="r num">${k.win_rate == null ? "—" : Math.round(k.win_rate * 100) + "%"}</td><td class="r num ${cls(k.pnl_usdt)}">${signed(k.pnl_usdt, 2)}</td></tr>`).join("");
  const rows = d.rows.map((t) => {
    const [s, sc] = SIDE[t.side] || [t.side, ""];
    return `<tr class="click" ${clickable("signal", t.signal_id)}><td class="num">${hm(t.closed_at)}</td><td class="hi">${coin(t.symbol)} <span class="${sc}">${s}</span> ${qty(t.quantity, t.entry_price)}</td>
      <td>${t.test ? '<span class="pill p-warn" title="没有经过哨兵 / 拍板 / 保镖：冒烟测试或手动下的单">测试</span>' : esc(SIGNAL[t.signal_kind] || "—")}</td><td class="num">${price(t.entry_price)}</td><td>${esc(REASON[t.close_reason] || t.close_reason || "—")}</td>
      <td class="num">${dur(new Date(t.closed_at) - new Date(t.opened_at))}</td><td class="r num ${cls(t.pnl_usdt)}">${signed(t.pnl_usdt, 2)}</td><td class="r num dim">${money(t.fees_usdt, 2)}</td></tr>`;
  }).join("");
  window.__recs = d.rejected;
  const rej = S.rej
    ? `<section class="card mt"><div class="ch"><h2>被拒的信号</h2><span class="r">${d.rejected.length} 条 · 拍板放弃和保镖拒绝 · 点一条看快照</span></div>
      <div class="log trail scroll tall">${d.rejected.map((r, i) => `<div class="click" ${clickable("record", i)}><span class="n">${hm(r.ts)}</span><span>${esc(ROLE[r.role] || r.role)} ${pill(r.outcome)} ${esc(r.text)}</span></div>`).join("") || '<div class="dim">没有。</div>'}</div></section>`
    : "";
  return `<div class="toolbar"><span class="seg">${Object.entries(RANGE).map(([k, v]) => `<button class="${S.hist === k ? "on" : ""}" ${clickable("hist", k)}>${v}</button>`).join("")}</span>
      <button class="chip ${S.rej ? "on" : ""}" ${clickable("rej")}>${S.rej ? "☑" : "☐"} 含被拒的信号</button>
      ${d.test_trades ? `<button class="chip ${S.tests ? "on" : ""}" ${clickable("tests")} title="冒烟测试、手动下的单，不是机器人自己的交易">${S.tests ? "☑" : "☐"} 含测试单（${d.test_trades} 笔）</button>` : ""}
      <button class="btn b-sm" ${clickable("csv")} ${d.rows.length ? "" : "disabled"}>导出 CSV</button></div>
    <div class="tiles t5">${tiles.map(([v, l, c]) => `<div class="tile"><small>${l}</small><b class="${c || ""}">${v}</b></div>`).join("")}</div>
    <section class="card mt"><div class="ch"><h2>资金曲线</h2><span class="r">累计净盈亏，每个点是一笔平仓</span></div>
      ${d.curve.length ? '<div class="chart sm" id="hchart-host"></div>' : '<div class="empty"><b>这段时间没有平仓</b></div>'}</section>
    <div class="grid g21 mt">
      <section class="card"><div class="ch"><h2>交易记录</h2><span class="r">最新在上 · 点一笔看整条链</span></div>
        ${d.rows.length ? `<div class="tw scroll tall"><table><thead><tr><th>平仓</th><th>币种</th><th>信号</th><th>开仓价</th><th>怎么平的</th><th>持有</th><th class="r">净盈亏</th><th class="r">手续费</th></tr></thead><tbody>${rows}</tbody></table></div>` : '<div class="dim">没有。</div>'}</section>
      <section class="card"><div class="ch"><h2>按信号类型</h2><span class="r">哪类赚钱、哪类该关</span></div>
        ${kinds ? `<div class="tw"><table><thead><tr><th>信号</th><th class="r">笔数</th><th class="r">胜率</th><th class="r">净盈亏</th></tr></thead><tbody>${kinds}</tbody></table></div>` : '<div class="dim">没有。</div>'}</section>
    </div>${rej}`;
};

function exportCsv() {
  const d = pageData;
  if (!d || !d.rows) return;
  const head = ["平仓时间", "开仓时间", "币种", "方向", "数量", "名义金额U", "杠杆", "开仓价", "信号", "平仓原因", "净盈亏U", "手续费U", "position_id"];
  // 文字字段以 = + - @ 开头时加 ' 前缀，免得表格软件当公式执行（数字照常）
  const q = (v) => { let t = String(v ?? ""); if (typeof v === "string" && /^[=+\-@]/.test(t)) t = "'" + t; return `"${t.replace(/"/g, '""')}"`; };
  const lines = d.rows.map((t) => [t.closed_at, t.opened_at, t.symbol, t.side, t.quantity, t.entry_price ? +(t.quantity * t.entry_price).toFixed(2) : "", t.leverage, t.entry_price, t.test ? "测试" : t.signal_kind, t.close_reason, t.pnl_usdt, t.fees_usdt, t.position_id].map(q).join(","));
  const blob = new Blob(["﻿" + [head.map(q).join(","), ...lines].join("\n")], { type: "text/csv" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `交易记录-${d.range}.csv`;
  a.click();
  URL.revokeObjectURL(a.href);
}

/* ---------- 资金 */
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
    <div class="grid g2 mt">
      <section class="card"><div class="ch"><h2>今天的钱是怎么变的</h2><span class="r">曼谷 00:00 起算</span></div><div class="form">${flow}</div>
        <div class="foot">${d.equity_source === "hummingbot" ? `账户数字都从币安${data.mode === "live" ? "实盘" : " Demo"}账户实时拉（经 hummingbot-api），不能手填。` : "hummingbot-api 没连上，总资产暂用账本里最后一次记录。"}今天手续费 ${money(d.fees_today_usdt, 2)} U。今日盈亏 ${signed(k.day_pnl_usdt, 2)} U（${k.day_pnl_pct == null ? "—" : signed(k.day_pnl_pct, 2) + "%"}），跌到熔断线就停开新仓到第二天。</div></section>
      <section class="card"><div class="ch"><h2>运行成本</h2><span class="r">AI 调用花费，不在币安账户里</span></div>
        <div class="tw"><table><thead><tr><th>角色</th><th class="r">今天</th><th class="r">近 7 天</th></tr></thead><tbody>${costs}
        <tr class="total"><td class="hi">合计</td><td class="r num hi">$${d.cost_today_usd.toFixed(3)}</td><td class="r num hi">$${d.cost_week_usd.toFixed(3)}</td></tr></tbody></table></div>
        <div class="foot">机器人只读账户，不能转账。转入转出请在币安上看。</div></section>
    </div>`;
};

/* ---------- 选币 */
P.pool = () => {
  const d = pageData;
  if (!d.entries.length && !d.pinned.length && !d.manual.length && !d.blacklist.length) {
    return `<div class="card"><div class="empty"><b>还没有选币结果</b>主循环每次选完币会写一份候选池，这里就能看到。</div></div>`;
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
    <section class="card mt"><div class="ch"><h2>候选池</h2><span class="r">代码每小时筛一次，军师只能从池里挑；固定、拉黑、手动加入优先</span></div>
      ${addBox}
      <div class="tw"><table><thead><tr><th>币种</th><th>分组</th><th class="r">分数</th><th>状态</th><th>理由</th><th class="r">24h 成交</th><th class="r">价差 bp</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>
      ${extraRows ? `<div class="ch mt"><h2>你设置过、还不在候选池里的币</h2></div><div class="tw"><table><tbody>${extraRows}</tbody></table></div>` : ""}
      <div class="foot">固定：每次都进池（仍要过硬条件）；拉黑：永不进池、立刻不开新仓；手动加的：加进候选（仍要过硬条件），“移除”就撤掉。</div></section>
    <div class="mt">${blockedSection(d.blocked)}</div>`;
};

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

/* ---------- 设置 */
const KEY_NOTE = { BINANCE_API_KEY: "币安实盘", BINANCE_API_SECRET: "币安实盘", BINANCE_TESTNET_API_KEY: "币安 Demo", BINANCE_TESTNET_API_SECRET: "币安 Demo", JEV_API_KEY: "拍板人", DEEPSEEK_API_KEY: "军师团", TELEGRAM_BOT_TOKEN: "告警", TELEGRAM_CHAT_ID: "告警", FRED_API_KEY: "宏观数据（可选）", HUMMINGBOT_API_PASSWORD: "hummingbot-api" };
const CHOICE = { on_demand: "按需（作战卡和持仓的币）", all_symbols: "候选池全算", fixed: "固定", adaptive: "按排队自适应", mini: "小模型（最快）", small: "中模型", base: "大模型（最准）", demo: "Demo Trading", legacy_testnet: "旧测试网", typesafe: "Jev 官方直连", openrouter: "OpenRouter", "Asia/Bangkok": "曼谷时间", "Asia/Shanghai": "北京时间", UTC: "世界标准时间（UTC）", "2h": "2 小时", "60m": "1 小时", "30m": "30 分钟", "15m": "15 分钟" };
const choiceLabel = (k, v) => (k === "atr_period_min" ? `${v} 分钟` : CHOICE[v] || String(v));
// 描述太长的项给个短名字（完整说明仍在契约里）
const SHORT = { mode: "运行方式", size_policy: "模型尺寸怎么选" };
// 设置项的大白话标题；原来的专业说明和英文键名降为灰色小字
const PLAIN = {
  risk_per_trade_pct: "每笔最多亏本金的百分之几",
  max_leverage: "最多开几倍杠杆",
  max_total_exposure_x: "所有仓位加起来最多是本金的几倍",
  daily_loss_halt_pct: "今天亏到本金的百分之几就停止开新仓",
  max_notional_per_trade_x: "单笔仓位最大是本金的几倍",
  max_notional_per_trade_usdt: "单笔仓位绝对上限 USDT（可不填）",
  max_open_positions: "同时最多持几个币",
  lifeboat_stop_multiplier: "交易所兜底止损比正常止损远几倍",
  max_spread_bps: "买卖价差超过万分之几就不开仓",
  max_slippage_bps: "成交价比预期差太多就不下单（万分之几）",
  taker_fee_bps: "交易所手续费（万分之几，单边）",
  min_tp_fee_multiple: "止盈至少能赚回几倍手续费，不够就不做",
  day_reset_tz: "“今天”按哪个时区算",
  mode: "运行方式",
  profile: "持仓档位（单笔最长拿多久）",
  predictor_enabled: "用不用 Kronos 预测价格",
  signal_dedup_s: "同一个信号多少秒内只处理一次",
  jev_timeout_ms: "拍板人多少毫秒不回答就放弃这笔",
  keeper_interval_s: "管家每隔几秒看一次持仓",
  reconcile_interval_s: "每隔几秒和交易所对一次账",
  slow_loop_interval_min: "军师团每隔几分钟开一次会",
  pool_size: "候选池放多少个币",
  card_pick_max: "军师团每次最多挑几个币",
  reconciler_close_unknown_positions: "交易所上有不认识的仓位时直接平掉",
  rule_gate_without_jev: "没有拍板人时用规则代替",
  jev_usd_per_mtok: "拍板人价格（每百万字美元）",
  jev_max_consecutive_errors: "拍板人连错几次就先歇一会",
  jev_error_cooldown_s: "拍板人连错后歇几秒",
  min_p_genuine: "信号至少多大把握是真的（0–1）",
  min_p_take: "至少多大把握值得做（0–1）",
  min_p_direction: "方向至少多大把握（0–1）",
  max_p_toxic: "被大户收割的可能最多多大（0–1）",
  signal_max_age_s: "信号超过几秒还没处理完就作废",
  size_fraction_s: "小仓位用正常仓位的几成",
  size_fraction_m: "中仓位用正常仓位的几成",
  atr_period_min: "按几分钟 K 线算波动幅度（定止损用）",
  min_stop_atr: "止损最近几个波动幅度（太近会被扫）",
  max_stop_atr: "止损最远几个波动幅度（太远不做）",
  stop_level_buffer_atr: "止损放在关键价位外面多远（波动幅度的几倍）",
  min_tp_r: "止盈至少是止损的几倍",
  max_tp_r: "止盈最多是止损的几倍",
  time_limit_s: "一笔最多拿几秒，到时就平",
  keeper_close_p: "管家觉得该平的把握过多少就平（0–1）",
  keeper_close_p_strong: "把握过多少不用再确认直接平（0–1）",
  keeper_confirmations: "管家连续几次都说平才平",
  patrol_shock_pct_1m: "1 分钟内往亏的方向走了百分之几就全平",
  patrol_alert_pct_1m: "1 分钟内往亏的方向走了百分之几就叫管家马上看",
  breakout_vol_ratio: "成交量是平时几倍算放量突破",
  surge_vol_ratio: "成交量是平时几倍算量能突增",
  surge_taker_ratio: "主动买（卖）占比多少算突增",
  liq_min_usdt_1m: "1 分钟爆仓至少多少 USDT 才算",
  liq_ratio: "爆仓额是平时几倍才算",
  liq_drop_pct_5m: "爆仓砸出的 5 分钟跌幅至少百分之几",
  crowded_funding_rate: "资金费率多高算一边倒（0.0005 = 万分之五）",
  session_window_min: "开盘、结算前后几分钟算关键时段",
  session_vol_ratio: "关键时段成交量是平时几倍才算",
  mode_predictor: "预测哪些币",
  size_policy: "模型大小怎么选",
  model_size: "固定用哪个大小的模型",
  adaptive_base_max_queue: "排队不超过几个时用最准的模型",
  adaptive_mini_min_queue: "排队到几个时换最快的模型",
  backfill_bars: "启动时先拉多少根 5 分钟 K 线",
  max_cards_per_round: "每轮最多给几个币开会",
  concurrency: "同时开几个会",
  card_ttl_hours: "作战卡管用几小时",
  symbol_min_interval_hours: "同一个币至少隔几小时才再开会",
  refresh_lead_min: "作战卡到期前几分钟开会换新卡",
  symbol_timeout_s: "一个币的会最多开几秒",
  min_refresh_gap_min: "两轮会至少隔几分钟",
  sim_environment: "模拟盘连币安哪个环境",
  data_dir: "数据保存在哪个文件夹",
  jev_provider: "拍板人走哪个通道",
  jev_model: "拍板人用哪个模型版本",
};
const plainOf = (i) => PLAIN[i.key === "mode" && typeof i.value === "string" && i.value in CHOICE ? "mode_predictor" : i.key];
const label = (i) => plainOf(i) || (SHORT[i.key] && i.label.length > 30 ? SHORT[i.key] : i.label);
// 合约里的说明带英文选项名的，换一句中文
const HINT = {
  jev_provider: "官方直连要 Jev 的 key；OpenRouter 和军师团共用一把 key",
  jev_model: "锁死一个版本，门槛按它调过；不在网页上改",
  jev_usd_per_mtok: "按用量算钱，用来统计花费",
  sim_environment: "Demo Trading 是币安现在的模拟盘；旧测试网已停用",
  mode_predictor: "按需 = 只算作战卡里和有持仓的币；全算 = 候选池每个币都算（费电脑）",
  size_policy: "固定 = 一直用下面选的模型；自适应 = 排队多时自动换小模型",
  model_size: "选“固定”时用这个",
  adaptive_base_max_queue: "选“自适应”时生效",
  adaptive_mini_min_queue: "选“自适应”时生效；介于两者之间用中模型",
  profile: "在下面的“持仓档位”卡片里一键切换",
  data_dir: "决策记录、状态文件都存在这里",
};
// 灰色小字只放中文说明；英文键名只在鼠标悬停时显示（排查问题用）
const hintKey = (i) => (i.key === "mode" && typeof i.value === "string" && i.value in CHOICE ? "mode_predictor" : i.key);
const techHint = (i) => [HINT[hintKey(i)] ? esc(HINT[hintKey(i)]) : plainOf(i) && i.label && i.label !== i.key && !/^[\x00-\x7f]+$/.test(i.label) ? esc(i.label) : "", i.recommended != null ? `推荐 ${esc(String(i.recommended))}（${esc(i.recommended_why)}）` : i.bounds ? "范围 " + esc(i.bounds) : ""].filter(Boolean).join(" · ");
const NULL_AS = { max_cards_per_round: "跟“军师团每次最多挑几个币”一样" };
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
const fmtVal = (v, i) => (i && !(i.choices && i.choices.length) && durOf(i.key, v)) || (typeof v === "boolean" ? (v ? "开" : "关") : v == null ? (i && i.nullable ? NULL_AS[i.key] || "不写（用默认）" : "—") : (i && i.choices && i.choices.length) || (typeof v === "string" && CHOICE[v]) ? esc(choiceLabel(i ? i.key : "", v)) : esc(v));
const PROFILE_NOTE = { "2h": "单笔最长 2 小时（默认）", "60m": "单笔最长 1 小时", "30m": "单笔最长 30 分钟", "15m": "单笔最长 15 分钟" };
function pinnedList(pf) {
  if (pf.pins && pf.pins[pf.current]) return pf.pins[pf.current].map((x) => PLAIN[x.key] || x.key);
  return Object.entries(pf.pinned).flatMap(([sec, m]) => Object.keys(m).map((k) => PLAIN[k] || k));
}
// 写死项表格：文件值 → 某档位的默认值（一样的标出来）
function pinTable(list, p) {
  return `<div class="tw"><table><thead><tr><th>项</th><th class="r">文件里写死</th><th class="r">${esc(p)} 档默认</th></tr></thead><tbody>${list.map((x) => {
    const i = { key: x.key };
    const same = JSON.stringify(x.value) === JSON.stringify(x.profile_default);
    return `<tr><td title="${esc(x.key)}">${esc(PLAIN[x.key] || x.key)}</td><td class="r num ${same ? "dim" : "warn"}">${fmtVal(x.value, i)}</td><td class="r num">${fmtVal(x.profile_default, i)}${same ? ' <span class="dim small">相同</span>' : ""}</td></tr>`;
  }).join("")}</tbody></table></div>`;
}
const CLEAR_TEXT = "清掉这些写死项，完全按档位走";
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
  ask(`恢复档位默认 · ${esc(key)}`, `<p>删掉配置文件里的这一行：${fmtVal(i.value, i)} → 档位默认 ${fmtVal(i.profile_default, i)}，以后切档位也跟着变。</p>${pwField(data.control)}`, "恢复", false, async () => {
    const pw = $("#dlg-pw");
    await post("/api/settings", { section: sec, changes: { [key]: null }, password: pw ? pw.value : "" });
    onOk = null;
    $("#layer [data-ok]").remove();
    $("#layer [data-close].btn").textContent = "关闭";
    dlgMsg(`<b class="up">已恢复</b>，重启 <code>jiaoyi run</code> 后生效。`);
    poll();
  });
}

function switchProfile(p) {
  const d = pageData;
  const pf = d && d.profile;
  if (!pf) return;
  const list = (pf.pins && pf.pins[p]) || [];
  const warn = list.length
    ? `<div class="note warn">下面这些项写死在配置文件里，切到 ${esc(p)} 后仍按文件里的值，不跟档位走：</div>${pinTable(list, p)}
      <label class="small"><input type="checkbox" id="dlg-clear" checked> ${CLEAR_TEXT}（改前自动备份）</label>`
    : "";
  ask(`切换持仓档位：${esc(pf.current)} → ${esc(p)}`, `<p>${esc(PROFILE_NOTE[p] || "")}。作战卡有效期、止损周期、管家频率等跟着档位换。</p>${warn}${pwField(data.control)}`, "切换", false, async () => {
    const pw = $("#dlg-pw");
    const clear = $("#dlg-clear");
    const pass = pw ? pw.value : "";
    // 先清写死项（按当前档位校验），再切档位
    const cleared = clear && clear.checked ? (await clearPins(pass)).changed.length : 0;
    await post("/api/settings", { section: "app", changes: { profile: p }, password: pass });
    onOk = null;
    $("#layer [data-ok]").remove();
    $("#layer [data-close].btn").textContent = "关闭";
    dlgMsg(`<b class="up">已切到 ${esc(p)}</b>${cleared ? `，清掉 ${cleared} 个写死项` : ""}。点「重启生效」后按新档位运行。`);
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

// 设置页 v2：首屏只放“运行”和三个核心风险，其余收进“高级”的五个标签；每项一行，点开再改，一次改一项
const CORE = ["risk.risk_per_trade_pct", "risk.daily_loss_halt_pct", "risk.max_leverage"];
const ADV = [
  { id: "risk", t: "风控细项", note: "首屏三项之外的仓位上限。都是你定的上限，AI 改不了。", groups: [
    { g: "仓位上限", keys: ["risk.max_total_exposure_x", "risk.max_notional_per_trade_x", "risk.max_notional_per_trade_usdt", "risk.max_open_positions"] },
    { g: "每类币仓位上限（本金的几倍）", cat: true, hint: "同一类币常常一起涨跌，这样一类出事不会亏太多。每类不能超过上面的总仓位倍数。" },
  ] },
  { id: "exec", t: "执行", note: "下单时的门槛、止损止盈、管家和拍板人。带“跟档位走”的切档位会自动换。", groups: [
    { g: "下单门槛", keys: ["risk.max_spread_bps", "risk.max_slippage_bps", "risk.taker_fee_bps", "risk.min_tp_fee_multiple"] },
    { g: "止损和止盈", keys: ["risk.lifeboat_stop_multiplier", "fast_loop.atr_period_min", "fast_loop.min_stop_atr", "fast_loop.max_stop_atr", "fast_loop.stop_level_buffer_atr", "fast_loop.min_tp_r", "fast_loop.max_tp_r", "fast_loop.time_limit_s"] },
    { g: "管家、巡逻兵和对账", keys: ["app.keeper_interval_s", "fast_loop.keeper_close_p", "fast_loop.keeper_confirmations", "fast_loop.keeper_close_p_strong", "fast_loop.patrol_alert_pct_1m", "fast_loop.patrol_shock_pct_1m", "app.reconcile_interval_s", "app.reconciler_close_unknown_positions"] },
    { g: "拍板人（每个信号做不做）", keys: ["fast_loop.min_p_genuine", "fast_loop.min_p_take", "fast_loop.min_p_direction", "fast_loop.max_p_toxic", "fast_loop.size_fraction_s", "fast_loop.size_fraction_m", "app.jev_timeout_ms", "fast_loop.jev_max_consecutive_errors", "fast_loop.jev_error_cooldown_s", "fast_loop.rule_gate_without_jev", "fast_loop.jev_usd_per_mtok", "fast_loop.jev_provider", "fast_loop.jev_model"] },
    { g: "信号识别", keys: ["app.signal_dedup_s", "fast_loop.signal_max_age_s", "fast_loop.breakout_vol_ratio", "fast_loop.surge_vol_ratio", "fast_loop.surge_taker_ratio", "fast_loop.liq_min_usdt_1m", "fast_loop.liq_ratio", "fast_loop.liq_drop_pct_5m", "fast_loop.crowded_funding_rate", "fast_loop.session_window_min", "fast_loop.session_vol_ratio"] },
  ] },
  { id: "slow", t: "军师团", note: "军师团多久开一次会、挑多少个币，还有 Kronos 预测员。", groups: [
    { g: "开会节奏", keys: ["app.slow_loop_interval_min", "schedule.*"] },
    { g: "选币", keys: ["app.pool_size", "app.card_pick_max"] },
    { g: "预测员 Kronos", keys: ["app.predictor_enabled", "predictor.*"] },
  ] },
  { id: "keys", t: "密钥" },
  { id: "show", t: "显示" },
];
// 别处已经显示的键（运行卡、显示页的系统信息），不再进“其他”
const SHOWN_ELSEWHERE = ["app.profile", "app.data_dir", "app.sim_environment", "risk.day_reset_tz"];
// 没列到的新键按所在段落到哪个标签的“其他”组
const SECTION_TAB = { risk: "risk", category: "risk", fast_loop: "exec", app: "exec", schedule: "slow", predictor: "slow" };
const UNIT = {
  risk_per_trade_pct: "%", daily_loss_halt_pct: "%", max_leverage: " 倍", max_total_exposure_x: " 倍", max_notional_per_trade_x: " 倍",
  max_notional_per_trade_usdt: " U", max_open_positions: " 个", lifeboat_stop_multiplier: " 倍", min_tp_fee_multiple: " 倍",
  min_tp_r: " 倍", max_tp_r: " 倍", keeper_confirmations: " 次", patrol_alert_pct_1m: "%", patrol_shock_pct_1m: "%",
  jev_max_consecutive_errors: " 次", breakout_vol_ratio: " 倍", surge_vol_ratio: " 倍", liq_min_usdt_1m: " U", liq_ratio: " 倍",
  liq_drop_pct_5m: "%", session_vol_ratio: " 倍", pool_size: " 个", card_pick_max: " 个", max_cards_per_round: " 个", concurrency: " 个",
  adaptive_base_max_queue: " 个", adaptive_mini_min_queue: " 个", backfill_bars: " 根",
};
// 推荐值里只有这几项是“越小越冒险”
const LOWER_RISKIER = ["min_tp_fee_multiple"];
const valOf = (i, v = i.value) => fmtVal(v, i) + (typeof v === "number" && !durOf(i.key, v) && !(i.choices && i.choices.length) ? UNIT[i.cat ? "max_total_exposure_x" : i.key] || "" : "");
const hot = (i) => i.recommended != null && typeof i.value === "number" && (LOWER_RISKIER.includes(i.key) ? i.value < i.recommended : i.value > i.recommended);

// 段.键 → 设置项（每类币上限拼成 category 段）
function settingIndex(d) {
  const ix = {};
  for (const sec of ["risk", "app", "fast_loop", "predictor", "schedule"]) for (const i of d[sec] || []) ix[`${sec}.${i.key}`] = { ...i, sec };
  const tot = (d.risk.find((i) => i.key === "max_total_exposure_x") || {}).value;
  for (const [k, v] of Object.entries(d.category_caps)) ix[`category.${k}`] = { key: k, sec: "category", cat: true, label: CAT[k] || k, value: v, bounds: tot == null ? "" : `0–${tot}（不超过总仓位倍数）`, editable: true, choices: [] };
  return ix;
}
// 把五个标签展开成 [标签, [组名, 提示, 项…]]；没列到的键收进对应标签的“其他”
function advGroups(d, ix) {
  const used = new Set([...CORE, ...SHOWN_ELSEWHERE]);
  const expand = (k) => (k.endsWith(".*") ? Object.keys(ix).filter((x) => x.startsWith(k.slice(0, -1))) : ix[k] ? [k] : []);
  const out = {};
  for (const tab of ADV) {
    if (!tab.groups) continue;
    out[tab.id] = tab.groups.map((g) => {
      const keys = g.cat ? Object.keys(ix).filter((x) => x.startsWith("category.")) : g.keys.flatMap(expand);
      keys.forEach((k) => used.add(k));
      return { g: g.g, hint: g.hint, items: keys.map((k) => ix[k]) };
    });
  }
  const rest = Object.keys(ix).filter((k) => !used.has(k));
  for (const tab of new Set(rest.map((k) => SECTION_TAB[ix[k].sec] || "exec"))) {
    out[tab].push({ g: "其他", items: rest.filter((k) => (SECTION_TAB[ix[k].sec] || "exec") === tab).map((k) => ix[k]) });
  }
  return out;
}

S.stOpen = new Set();
try { S.stTab = localStorage.getItem("settings-tab") || "risk"; } catch (err) { S.stTab = "risk"; }

function auxOf(i) {
  if (!i.editable) return "网页不改";
  if (i.recommended != null) return `推荐 <b>${valOf(i, i.recommended)}</b>`;
  if (i.pinned) return `<span class="warn">已写死（档位是 ${valOf(i, i.profile_default)}）</span>`;
  return i.profiled ? "跟档位走" : "";
}
// 时间类的值已经换成“1 小时 40 分”，标题里的“几秒 / 几分钟”就改说“多久”
const nameOf = (i) => (durOf(i.key, i.value) ? label(i).replace(/多少毫秒|几毫秒|几秒|几分钟|几小时/, "多久") : label(i));
function itemRow(i, big) {
  const id = `${i.sec}.${i.key}`;
  const open = S.stOpen.has(id);
  const w = pageData.writable && i.editable;
  const desc = techHint(i);
  const acts = [
    w ? `<button class="btn b-sm" data-act="edit1" data-arg="${esc(id)}">修改</button>` : "",
    w && i.recommended != null && i.value !== i.recommended ? `<button class="btn b-sm" data-act="rec1" data-arg="${esc(id)}">用推荐值</button>` : "",
    w && i.pinned ? `<button class="btn b-sm" ${clickable("unpin", i.sec + ":" + i.key)}>恢复档位默认</button>` : "",
    `<span class="st-key">${esc(i.key)}</span>`,
  ].join("");
  return `<div class="st-item ${big ? "big" : ""} ${open ? "open" : ""} ${i.editable ? "" : "ro"}">
    <button class="st-sum" data-act="st-item" data-arg="${esc(id)}" aria-expanded="${open}" title="${esc(i.key)}"><span class="nm">${esc(i.cat ? i.label : nameOf(i))}</span><span class="cur num ${hot(i) ? "warn" : ""}">${valOf(i)}</span><span class="aux">${auxOf(i)}</span><span class="chev">›</span></button>
    <div class="st-body">${desc ? `<div>${desc}</div>` : ""}<div class="st-acts">${acts}</div></div></div>`;
}
function toggleItem(el, id) {
  const it = el.closest(".st-item");
  if (S.stOpen.has(id)) S.stOpen.delete(id);
  else S.stOpen.add(id);
  it.classList.toggle("open", S.stOpen.has(id));
  el.setAttribute("aria-expanded", S.stOpen.has(id));
}

// 改一项：一个输入框 + 面板密码；段按键自动带
function editOne(id, useRec) {
  const d = pageData;
  const i = d && settingIndex(d)[id];
  if (!i || !d.writable || !i.editable) return;
  let input;
  if (typeof i.value === "boolean") input = `<select id="dlg-v"><option value="true" ${i.value ? "selected" : ""}>开</option><option value="false" ${i.value ? "" : "selected"}>关</option></select>`;
  else if (i.choices && i.choices.length) input = `<select id="dlg-v">${i.choices.map((c) => `<option value="${esc(JSON.stringify(c))}" ${c === i.value ? "selected" : ""}>${esc(choiceLabel(i.key, c))}</option>`).join("")}</select>`;
  else {
    // 档位给默认的键：没写死时留空（= 跟档位走），写死了才填文件里的值
    const shown = useRec ? i.recommended : i.value == null || (i.profiled && !i.pinned) ? "" : i.value;
    const ph = i.profiled ? `留空 = 跟档位（${fmtVal(i.pinned ? i.profile_default : i.value, i)}）` : i.nullable ? "留空 = 用默认" : "";
    input = `<input id="dlg-v" type="number" step="any" inputmode="decimal" value="${esc(shown)}" placeholder="${esc(ph)}">`;
  }
  const now = [`现在 ${valOf(i)}`, i.recommended != null ? `推荐 ${valOf(i, i.recommended)}（${esc(i.recommended_why)}）` : "", i.pinned ? `档位默认 ${valOf(i, i.profile_default)}` : i.profiled ? "现在跟档位走" : "", i.bounds ? `范围 ${esc(i.bounds)}` : ""].filter(Boolean).join("，");
  ask(esc(i.cat ? i.label : nameOf(i)), `<p class="dim">${now}。</p><label class="pw">改成${input}</label>${pwField(data.control)}
    <div class="dim small">保存前整份检查，不合理会说原因；写前留备份。改完点「重启生效」才用上。</div>`, "保存", false, async () => {
    const el = $("#dlg-v");
    let v;
    if (el.tagName === "SELECT") v = JSON.parse(el.value);
    else if (el.value.trim() === "") {
      if (!i.nullable && !i.profiled) throw new Error("不能空着");
      if (i.profiled && !i.pinned) throw new Error("没有改动");
      v = null;
    } else {
      v = Number(el.value);
      if (!Number.isFinite(v)) throw new Error("要填数字");
    }
    // 档位键填了数就算写死（哪怕和档位一样），其余和原值一样算没改
    if (v === i.value && !(i.profiled && !i.pinned && v !== null)) throw new Error("没有改动");
    const pw = $("#dlg-pw");
    const res = await post("/api/settings", { section: i.sec, changes: { [i.key]: v }, password: pw ? pw.value : "" });
    onOk = null;
    $("#layer [data-ok]").remove();
    $("#layer [data-close].btn").textContent = "关闭";
    const x = res.changed[0];
    dlgMsg(`<b class="up">已保存</b>${x ? `：${valOf(i, x.old)} → ${valOf(i, x.new)}` : ""}。点「重启生效」后用上。`);
    poll();
  });
}

// 主程序这次启动以后的改动 = 还没生效的
function pendingChanges(d) {
  const since = data.control.started_at ? Date.parse(data.control.started_at) : null;
  return since == null ? [] : d.changes.filter((x) => x.ts && Date.parse(x.ts) > since);
}
function changeText(x, ix) {
  const i = ix[`${x.section}.${x.key}`] || { key: x.key, label: x.key, nullable: true, choices: [] };
  const name = i.cat ? i.label : PLAIN[x.key] || i.label || x.key;
  return `${esc(name)}：${valOf({ ...i, nullable: true }, x.old)} → <b>${valOf({ ...i, nullable: true }, x.new)}</b>`;
}
function showChanges() {
  const d = pageData;
  if (!d) return;
  const ix = settingIndex(d);
  const pend = data.control.config_pending === true ? new Set(pendingChanges(d)) : new Set();
  dlg(`<h3>最近的修改</h3>${d.changes.length ? `<div class="log scroll">${d.changes.map((x) => `<div><span class="n">${x.ts ? hm(x.ts) : ""}</span><span>${changeText(x, ix)}${pend.has(x) ? ' <span class="pill p-warn">待生效</span>' : ""}</span></div>`).join("")}</div>` : '<div class="dim">还没有在网页上改过。</div>'}
    <p class="dim small">每次修改前都留了备份；记录在 data/config_changes.jsonl。</p>`);
}

function pendingBanner(d) {
  if (data.control.config_pending !== true) return "";
  const list = pendingChanges(d);
  const ix = settingIndex(d);
  const what = list.length
    ? list.slice(0, 3).map((x) => `${changeText(x, ix)}（${hm(x.ts)} 改的）`).join("；") + (list.length > 3 ? ` 等 ${list.length} 项` : "")
    : "配置文件改过（可能是在终端改的）";
  return `<div class="st-pending"><div class="t"><b>${list.length ? `有 ${list.length} 项改动还没生效` : "配置改了还没生效"}</b><div>${what}。重启主程序后用上，约 1 分钟。</div></div>
    <button class="btn b-sm" data-act="st-changes">看改了什么</button>${data.running ? `<button class="btn b-sm b-go" ${clickable("cmd", "restart")}>重启生效</button>` : ""}</div>`;
}

function runCard(d) {
  const c = data.control;
  const pf = d.profile;
  const up = c.started_at ? `${hm(c.started_at)} 启动，已跑 ${dur(Date.now() - Date.parse(c.started_at))}` : "";
  let pfRow = `<div class="st-row"><div class="k">持仓档位<small>这次启动的面板没拿到配置文件路径，看不到档位</small></div></div>`;
  if (pf) {
    const paperOnly = (p) => pf.paper_only.includes(p);
    const btns = pf.choices.map((p) => {
      const off = !d.writable || (paperOnly(p) && d.mode !== "paper");
      const tip = paperOnly(p) && d.mode !== "paper" ? "只在模拟盘可用" : PROFILE_NOTE[p] || "";
      return `<button class="${p === pf.current ? "on" : ""}" ${off || p === pf.current ? "disabled" : clickable("profile", p)} title="${esc(tip)}">${esc(CHOICE[p] || p)}</button>`;
    }).join("");
    const pins = pinnedList(pf);
    pfRow = `<div class="st-row"><div class="k">持仓档位<small>${esc(PROFILE_NOTE[pf.current] || "")}，开会节奏、止损周期跟着换；30 分、15 分只在模拟盘用</small></div><span class="seg">${btns}</span>
      ${pins.length ? `<div class="st-pins" title="${esc(pins.join("、"))}"><span class="warn">${pins.length} 项写死在配置文件里，切档位不跟着变。</span>${d.writable ? ` <button class="btn b-sm" ${clickable("clearpins")}>看是哪几项、清掉</button>` : ` ${esc(pins.join("、"))}`}</div>` : ""}</div>`;
  }
  return `<section class="card st"><div class="ch"><h2>运行</h2></div>
    <div class="st-row"><div class="k">现在的模式<small>切实盘要在 Mac mini 终端输 <code>jiaoyi go-live</code>，要确认词和面板密码</small></div><span class="pill ${d.mode === "live" ? "p-short" : "p-warn"}">${esc(MODE[d.mode] || d.mode)}</span><span class="pill ${d.live_armed ? "p-short" : ""}">${d.live_armed ? "实盘已解锁" : "实盘未解锁"}</span></div>
    <div class="st-row"><div class="k">主程序<small>${up || "还没有启动记录"}</small></div><span class="lamp ${data.running ? "" : "bad"}"><i></i>${data.running ? "运行中" : "没在运行"}</span>${restartBtn()}</div>
    <div class="st-row"><div class="k">开新仓<small>暂停后只管已有持仓，不开新的</small></div><span class="pill ${c.paused ? "p-short" : ""}">${c.paused ? "已暂停" : "正常开仓"}</span>${c.paused ? `<button class="btn b-sm" ${clickable("cmd", "resume")}>恢复开仓</button>` : `<button class="btn b-sm" ${clickable("cmd", "pause")}>暂停开新仓</button>`}</div>
    ${pfRow}</section>`;
}

function riskCard(d, ix) {
  const basis = d.recommend_basis_usdt ? `推荐按 ${money(d.recommend_basis_usdt, 0)} U 算` : "推荐按 1,000 U 算（没拿到账户资金）";
  const cautions = (d.risk_cautions || []).map((c) => esc(c.replace(/^[a-z_]+/, (k) => PLAIN[k] || k)));
  return `<section class="card st"><div class="ch"><h2>风险</h2><span class="r">你定的上限，AI 改不了 · ${basis}</span></div>
    ${CORE.filter((k) => ix[k]).map((k) => itemRow(ix[k], true)).join("")}
    <div class="st-foot">${cautions.length ? `<span class="warn">${cautions.length} 项比推荐冒险：</span>${cautions.join("；")}。只提示，不拦。` : "都在推荐范围内。"}</div></section>`;
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
    <div class="st-row"><div class="k">涨跌颜色</div><span class="seg"><button class="${updown === "green-up" ? "on" : ""}" ${clickable("updown", "green-up")}>绿涨红跌</button><button class="${updown === "red-up" ? "on" : ""}" ${clickable("updown", "red-up")}>红涨绿跌</button></span></div>
    <div class="st-sub">系统信息（只看）</div>
    <div class="st-row"><div class="k">面板密码</div>${d.password_set ? '<span class="pill">已设置</span>' : '<span class="pill p-warn">未设置（终端 jiaoyi set-panel-password）</span>'}</div>
    ${info("risk.day_reset_tz", "“今天”按哪个时区算")}${info("app.sim_environment", "模拟盘连币安哪个环境")}${info("app.data_dir", "数据保存在哪个文件夹")}`;
}

function advCard(d, ix) {
  const groups = advGroups(d, ix);
  const tab = ADV.some((t) => t.id === S.stTab) ? S.stTab : "risk";
  const t = ADV.find((x) => x.id === tab);
  const tabs = ADV.map((x) => `<button role="tab" aria-selected="${x.id === tab}" class="${x.id === tab ? "on" : ""}" data-act="st-tab" data-arg="${x.id}">${x.t}</button>`).join("");
  const body = tab === "keys" ? keysTab(d) : tab === "show" ? showTab(d, ix)
    : `<div class="st-note">${esc(t.note)}</div>` + groups[tab].filter((g) => g.items.length).map((g) => `<div class="st-sub">${esc(g.g)}</div>${g.hint ? `<div class="st-note">${esc(g.hint)}</div>` : ""}${g.items.map((i) => itemRow(i)).join("")}`).join("");
  return `<section class="card st"><div class="ch"><h2>高级</h2><span class="r">平时不用动</span></div><div class="st-tabs" role="tablist">${tabs}</div>${body}</section>`;
}

P.settings = () => {
  const d = pageData;
  const ix = settingIndex(d);
  const note = d.file_error ? `<span class="down">配置文件现在读不出：${esc(d.file_error)}。先在终端修好再改。</span>` : d.writable ? "改任何一项都要面板密码；模式、数据目录、拍板人通道和模型版本不在网页上改。" : "这次启动的面板只能看：没拿到配置文件路径。";
  return `<div class="st-col">${pendingBanner(d)}${runCard(d)}${riskCard(d, ix)}${advCard(d, ix)}
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
    hchart = LightweightCharts.createChart(hEl, {
      autoSize: true,
      layout: { background: { color: "transparent" }, textColor: getComputedStyle(document.documentElement).getPropertyValue("--dim").trim(), fontFamily: getComputedStyle(document.body).fontFamily },
      grid: { vertLines: { visible: false }, horzLines: { color: "rgba(0,0,0,0.05)" } },
      timeScale: { timeVisible: true, borderVisible: false, tickMarkFormatter: (t) => tick(t) },
      rightPriceScale: { borderVisible: false },
      localization: { timeFormatter: (t) => hm(t * 1000) },
    });
    hseries = hchart.addBaselineSeries({ baseValue: { type: "price", price: 0 } });
  }
  host.appendChild(hEl);
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
  funds: () => "/api/funds",
  pool: () => "/api/pool",
  settings: () => "/api/settings",
};
const pageId = (page, arg) => page + "/" + (arg || "") + (page === "history" ? "/" + S.hist + (S.tests ? "/t" : "") : "");

function render() {
  chrome();
  $("#status").innerHTML = statusBar();
  const { page, arg } = route();
  const needs = PAGE_API[page];
  if (needs && pageErr && pageErr[0] === pageId(page, arg)) {
    $("#page").innerHTML = `<div class="card"><div class="empty"><b>这一页读不出来</b><span>${esc(pageErr[1])}</span><span>${STALE_HINT}</span></div></div>`;
    return;
  }
  if (!data || (needs && (!pageData || pageKey !== pageId(page, arg)))) {
    $("#page").innerHTML = `<div class="card"><div class="empty"><b>正在读取…</b></div></div>`;
    return;
  }
  const scrolls = [...document.querySelectorAll("#page .scroll")].map((e) => e.scrollTop);
  // 页面上的输入框（选币页“手动加币”）：定时刷新重画时保住正在输入的内容和焦点
  const typing = document.activeElement && document.activeElement.id === "pool-add";
  const typed = $("#pool-add") ? $("#pool-add").value : "";
  $("#page").innerHTML = (P[page] || placeholder)(arg);
  document.querySelectorAll("#page .scroll").forEach((e, i) => { e.scrollTop = scrolls[i] || 0; });
  const box = $("#pool-add");
  if (box && typed) box.value = typed;
  if (box && typing) box.focus();
  if (page === "positions") mountChart();
  if (page === "history") mountHistoryChart();
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

async function poll() {
  const { page, arg } = route();
  try {
    data = await getJson("/api/overview");
    TZ = data.display_tz || TZ;
    const api = PAGE_API[page];
    if (api) {
      const id = pageId(page, arg);
      try {
        pageData = await getJson(api(arg));
        pageKey = id;
        pageErr = null;
      } catch (e) {
        pageErr = [id, e.message];
      }
    }
    lastOk = Date.now();
    $("#stale").innerHTML = "";
    render();
    loadKlines(false);
  } catch (e) {
    const ago = lastOk ? Math.round((Date.now() - lastOk) / 1000) + " 秒前" : "从未";
    $("#stale").innerHTML = `<div class="stale">面板服务读不出（${esc(e.message)}；上次成功：${ago}）</div>`;
    if (!data) $("#page").innerHTML = `<div class="card"><div class="empty"><b>面板读不出数据</b><span>${esc(e.message)}</span><span>${STALE_HINT}</span></div></div>`;
  }
}

if (location.pathname !== "/setup") { // 首次设置向导在 setup.js
  window.addEventListener("hashchange", () => { closeDlg(); render(); poll(); });
  render();
  poll();
  setInterval(poll, 5000);
}
