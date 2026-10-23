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
const BIAS = { long: ["偏多", "p-long"], short: ["偏空", "p-short"], both: ["双向", "p-accent"], no_trade: ["观望", ""], unknown: ["没结论", ""] };
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
const md = (ts) => new Intl.DateTimeFormat("zh-CN", { timeZone: TZ, month: "numeric", day: "numeric" }).format(new Date(ts));
const money = (x, d = 1) => (x == null ? "—" : x.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d }));
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
    <div class="kpi click" ${clickable("daypnl")} title="点开看明细：平仓毛利、手续费、资金费、持仓浮盈变化、AI 费"><small>今日盈亏 · 账户 <span class="dim">ⓘ 明细</span></small><b class="${cls(k.day_pnl_usdt)}">${signed(k.day_pnl_usdt)} U <span class="p">${k.day_pnl_pct == null ? "" : signed(k.day_pnl_pct, 2) + "%"}</span></b>${dayParts(k)}${k.as_of ? `<small class="dim">数据截至 ${hms(k.as_of)}</small>` : ""}</div>
    <div class="kpi"><small>离熔断（−${k.halt_pct}%）</small><b class="${k.halted ? "down" : ""}" style="font-size:15px">${k.halted ? "已熔断" : k.to_halt_usdt == null ? "—" : "还差 " + money(k.to_halt_usdt, 0) + " U"}</b><div class="meter ${k.halted ? "full" : ""}"><span style="width:${pct}%"></span></div></div>
    <div class="kpi"><small title="所有持仓名义价值（含杠杆）÷ 本金">敞口（含杠杆）</small><b style="font-size:15px">${k.exposure_x == null ? "—" : k.exposure_x.toFixed(2)} / ${k.max_exposure_x} 倍</b></div>
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
  set_category: { title: "类别开关", text: "关掉后这一类不再靠打分进候选池，你固定、手动加的币仍然进池；打开就恢复按打分进池。选币页的设置优先于 slow.toml，主程序马上重新选币。", ok: "确定", pw: true },
  set_category_cap: { title: "这一类最多进池几个", text: "0 = 这一类一个都不要。选币页的设置优先于 slow.toml，主程序马上重新选币。", ok: "保存", pw: true, cap: true },
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
  const capField = k.cap ? `<label class="pw">最多几个（0–100 的整数）<input type="number" id="dlg-cap" min="0" max="100" step="1" inputmode="numeric" value="${esc(String(params.cap))}"></label>` : "";
  ask(`${title}${what ? " · " + esc(what) : ""}`, `<p>${k.text}</p>${warn}${capField}${k.pw ? pwField(c) : ""}`, k.ok, k.danger, async () => {
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
const feeTip = (p) => (p.fees_est_usdt == null || p.unrealized_usdt == null ? "" : `浮动 ${signed(p.unrealized_usdt, 2)} U − 手续费 ${money(p.fees_est_usdt, 2)} U（开仓 + 平仓各一次，${feeSrc(p)}）`);
// 今日盈亏直接拆一行：已平（净，同历史页交易净盈亏）+ 浮动（没平的单现在的浮盈）+ 其它（资金费、0 点前已有持仓的浮盈等）
function dayParts(k) {
  if (k.day_pnl_usdt == null || k.day_closed_usdt == null || k.day_floating_usdt == null) return "";
  const rest = k.day_pnl_usdt - k.day_closed_usdt - k.day_floating_usdt;
  return `<small class="dim num">已平 ${signed(k.day_closed_usdt, 1)} · 浮动 ${signed(k.day_floating_usdt, 1)} · 其它 ${signed(rest, 1)}</small>`;
}
async function showDayPnl() {
  dlg(`<h3>今日盈亏明细</h3><div class="dim">读取中…</div>`);
  let f;
  try { f = await getJson("/api/funds"); } catch (e) { return dlg(`<h3>今日盈亏明细</h3><div class="down">${esc(e.message)}</div>`); }
  const t = (f.pnl || []).find((x) => x.key === "today");
  const k = data.kpis;
  const day = k.day_pnl_usdt;
  if (!t || day == null) return dlg(`<h3>今日盈亏明细</h3><p>还没有日初权益或账户数据，算不出明细。</p>`);
  const known = t.trading_usdt + t.fees_usdt + (t.funding_usdt || 0);
  const rest = day - known;
  const row = (label, v, note = "") => `<tr><td>${label}${note ? `<div class="dim small">${note}</div>` : ""}</td><td class="r num ${cls(v)}">${v == null ? "—" : signed(v, 2) + " U"}</td></tr>`;
  const src = f.pnl_source === "exchange" ? "币安收入流水（主程序定时拉）" : "账本里今天平掉的单";
  dlg(`<h3>今日盈亏明细</h3>
    <div class="tw"><table><tbody>
      ${row("平仓毛利", t.trading_usdt, "今天平掉的单，扣手续费前")}
      ${row("手续费", t.fees_usdt)}
      ${row("资金费", t.funding_usdt, t.funding_usdt == null ? "没有币安收入流水，拿不到" : "")}
      ${row("持仓浮盈变化及其他", rest, "还没平的单从 0 点到现在的浮盈变化；也含转入转出、流水还没拉到的部分")}
      <tr class="hi"><td><b>账户今日盈亏</b><div class="dim small">= 总资产 − 0 点的总资产，和顶部一致</div></td><td class="r num ${cls(day)}"><b>${signed(day, 2)} U</b></td></tr>
      ${row("AI 费", -t.ai_usd, "拍板人、管家、预测员的调用费加军师团今天已花（读慢环预算文件，含正在开的会），美元按 1 U 算，不从交易账户扣")}
      <tr class="hi"><td><b>扣 AI 费后</b></td><td class="r num ${cls(day - t.ai_usd)}"><b>${signed(day - t.ai_usd, 2)} U</b></td></tr>
    </tbody></table></div>
    <p class="dim small">平仓、手续费、资金费来自${src}${k.as_of ? `；总资产截至 ${hms(k.as_of)}` : ""}。平仓赚了但账户今日盈亏还是负的，通常是手续费、别的持仓浮亏，或者流水还没拉到（这时差额先算在「持仓浮盈变化及其他」里）。</p>`);
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
    <label class="pw">确认词（原样输入「${esc(m.confirm_word)}」）<input type="text" id="dlg-word" autocomplete="off"></label>${pwField(c)}`;
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
  else if (act === "meeting") showMeeting(arg);
  else if (act === "record") showRecord(+arg);
  else if (act === "tf") { S.tf = arg; loadKlines(true); render(); }
  else if (act === "hist") { S.hist = arg; render(); poll(); }
  else if (act === "ai") { S.ai = arg; render(); poll(); }
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
  else if (act === "daypnl") showDayPnl();
  else if (act === "mode") switchMode(arg);
  else if (act === "hold") switchHold(arg);
  else if (act === "clearpins") askClearPins();
  else if (act === "cat") { const [kind, cat, v] = arg.split(":"); askCommand(kind, kind === "set_category" ? { category: cat, enabled: v } : { category: cat, cap: v }, CAT[cat] || cat); }
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

// 作战卡：每个币默认折叠成一行（方向 / 止损 / 止盈 / 杠杆 / 仓位 / 到期），点开看计划、价格刻度、军师原话和开会过程。
// 止损 / 止盈是保镖真会用的口径：距离 = 卡上倍数 × ATR（/api/card-atr，√(短×长) × 时段系数），止盈 = 止损距离 × 止盈倍数下限；
// 信号来了按那一刻的价进，卡上按现价估，所以价前面带「≈」。军师原话里的价位不解析，只在折叠里标「参考」。
S.atr = {};
const TIER = { S: "小", M: "中", L: "大" };
const tierTip = (c) => {
  const f = c.size_fractions || {};
  const pct = (k) => (f[k] == null ? "" : `${TIER[k]} = 每笔风险的 ${Math.round(f[k] * 100)}%`);
  return "军师团给的仓位上限档，拍板人选的档不超过它，保镖按档打折" + (f.S != null ? `：${["S", "M", "L"].map(pct).join("，")}` : "");
};
const DIR = { long: ["做多", "p-long"], short: ["做空", "p-short"], both: ["双向", "p-accent"] };
const signedPct = (a, b) => { const p = (a - b) / b * 100; return `${p >= 0 ? "+" : "−"}${Math.abs(p).toFixed(2)}%`; };
// 保镖算的距离（不随成交价回填变）：止损 / 保命在亏钱一侧，止盈在赚钱一侧
const guardPct = (pct, side, loss) => `${(side === "long") === loss ? "−" : "+"}${pct.toFixed(2)}%`;
const lbPct = p => p.lifeboat_stop_pct != null ? ` <span class="dim">${guardPct(p.lifeboat_stop_pct, p.side, true)}</span>` : "";
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
  // 保镖开着「夹到推荐范围」时，越界的那截不要（没交集时保镖会拒单，这里照原样显示）
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

function cardBody(s, L) {
  const sym = s.symbol, p = L && L.held;
  if (s.pending_label) return `<div class="txt">${esc(s.pending || s.pending_label)}</div><div class="small"><a ${clickable("meeting", sym)}>开会过程 ›</a></div>`;
  const [dt] = DIR[s.bias] || [""];
  const sigs = s.allowed_signals.length && s.allowed_signals.length < 5 ? s.allowed_signals.map((x) => `「${esc(SIGNAL[x] || x)}」`).join("或") : "任一信号";
  const plan = p
    ? `已在 <span class="num">${price(p.entry_price, sym)}</span> ${(DIR[p.side] || [p.side])[0]}，拿着等止盈${p.stop_loss_price ? `；${p.side === "long" ? "跌破" : "涨过"} <span class="num">${price(p.stop_loss_price, sym)}</span> 止损` : ""}。`
    : `等哨兵发现${sigs}，按当时价格${dt}。`;
  const from = s.source_note ? `<div class="dim small">${esc(s.source_note)}</div>` : "";
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
      <dl class="bc-kv small"><dt>军师建议止损</dt><dd>${range(s.stop_atr, "", 1)} 倍 ATR（拍板人选窄 / 中 / 宽）</dd><dt>军师建议止盈</dt><dd>${range(s.take_profit_r, "", 1)} 倍止损距离（保镖用下限）</dd><dt>军师建议杠杆</dt><dd>${range(s.leverage, "", 0)} 倍（只是建议，实际用设置里的杠杆）</dd><dt>军师建议仓位</dt><dd>${range(s.notional_usdt, "", 0)} U（只是建议，实际由保镖按风险算）</dd></dl>
    </div></details>`;
  return `<div class="plan">${plan}</div>${from}${calc}${lad}<div class="bc-foot">${raw}<a class="small" ${clickable("meeting", sym)}>开会过程 ›</a></div>`;
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

// 历史卡：过去每条指令（一个币一场会的结论）后来怎么样了；按日期翻、按币筛
const CARD_END = { active: ["还在用", "p-accent"], expired: ["到期", ""], replaced: ["被新结论替换", "p-warn"] };
async function showCardHistory(arg = "") {
  const [day, sym] = arg.split("|");
  const q = new URLSearchParams();
  if (day) q.set("day", day);
  if (sym) q.set("symbol", sym);
  dlg(`<h3>历史卡</h3><div class="dim">读取中…</div>`);
  $("#layer .dlg").classList.add("wide");
  let h;
  try { h = await getJson(`/api/card-history?${q}`); } catch (e) {
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
    <div class="row-pick">${pick}</div>
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

// 预测员一条记录 → 大白话：未来多久、价格范围、几条模拟向上 / 向下；原始 JSON 折叠
const pxFmt = (v, sym) => price(v, sym);
const spanOf = (min) => (min % 60 ? `${min} 分钟` : `${min / 60} 小时`);
function predictorView(s) {
  const o = s.output || {};
  const inp = s.input || {};
  const n = Number.isInteger(inp.paths) ? inp.paths : null;
  const up = n ? Math.round(o.p_up * n) : null;
  const lean = o.p_up > 0.5 ? "偏多" : o.p_up < 0.5 ? "偏空" : "分歧";
  const votes = n
    ? (up === 0 ? `${n} 条模拟全部向下` : up === n ? `${n} 条模拟全部向上` : `${n} 条模拟里 ${up} 条向上、${n - up} 条向下`)
    : (o.p_up === 0 ? "模拟全部向下" : o.p_up === 1 ? "模拟全部向上" : `${Math.round(o.p_up * 100)}% 的模拟向上`);
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
  kronos: ["预测员"], h_min: ["预测多久", "min"], p_up: ["向上的模拟占比", "p"], lo_bps: ["预测下沿离现价", "bps"], hi_bps: ["预测上沿离现价", "bps"],
  approved: ["结论"], reasons: ["原因"], detail: ["细节"], day_pnl_pct: ["今天盈亏", "pct"], slippage_bps: ["预计滑点", "bps"],
  quantity: ["数量"], notional_usdt: ["名义仓位（含杠杆）", "usdt"], leverage: ["杠杆", "x"], risk_usdt: ["这笔最多亏", "usdt"], stop_loss_pct: ["止损距离", "pct"], take_profit_pct: ["止盈距离", "pct"],
  trailing_stop_pct: ["移动止损", "pct"], trailing_activation_pct: ["移动止损启动", "pct"], lifeboat_stop_pct: ["保命止损距离", "pct"], order_type: ["下单方式"], category: ["分组"],
  intent_id: ["下单编号"], executor_id: ["执行器编号"], signal_id: ["信号编号"], card_id: ["作战卡编号"], position_id: ["持仓编号"], dedup_key: ["去重键"], request_id: ["请求编号"],
  action: ["动作"], p_close: ["该平仓的概率", "p"], consecutive: ["连续几次"], reason: ["理由"], fees: ["手续费", "usdt"], fees_usdt: ["手续费", "usdt"], net: ["净盈亏", "usdt"],
  hi_1m: ["1 分钟最高", "px"], lo_1m: ["1 分钟最低", "px"], positions: ["持仓"], symbols: ["币"], checked_positions: ["核对了几个仓位"], issues: ["发现的问题"], fixed: ["已处理"],
  estimated: ["估算"], direct: ["直接"], values: ["行情特征"], features: ["行情特征"], forecast: ["预测员"], ts: ["时间", "ts"], stale_after: ["作废时间", "ts"],
  model: ["模型"], latency_ms: ["耗时", "ms"], cost_usd: ["花费", "usd"], ok: ["调用成功"], error: ["出错"], horizon_min: ["预测多久", "min"], price_low: ["预测下沿", "px"], price_high: ["预测上沿", "px"],
};
const VALUE = { ...SIGNAL, long: "多", short: "空", hold: "拿住", close: "平仓", market: "市价", limit: "限价", none: "没有作战卡", symbol_not_in_card: "作战卡里没这个币", both: "双向", no_trade: "观望", ...TIER };
const fmtNum = (v) => (Math.abs(v) >= 1000 ? money(v, 0) : Math.abs(v) >= 1 ? money(v, 2) : String(+v.toFixed(4)));
function fieldVal(k, v) {
  const unit = (FIELD[k] || [])[1];
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
    const [name, unit] = FIELD[k] || [k];
    const lab = `<span class="k" title="${esc(k)}">${esc(name)}</span>`;
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
];
const JEV_CHOICE = { up: "向上", down: "向下", flat: "横盘", narrow: "偏窄", mid: "适中", wide: "偏宽", S: "小", M: "中", L: "大" };
function jevView(s, th) {
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
// 保镖：通过 / 拒绝 + 原因列表 + 细节
function guardView(s) {
  const o = s.output || {};
  const rest = Object.fromEntries(Object.entries(o).filter(([k]) => !["approved", "reasons"].includes(k)));
  const head = o.approved != null ? `<div class="note ${o.approved ? "" : "warn"}"><b>${o.approved ? "通过" : "拒绝"}</b>${(o.reasons || []).length ? `：${esc(o.reasons.join("；"))}` : ""}</div>` : "";
  return `<div class="small dim">看到的</div>${kvOrEmpty(s.input)}<div class="small dim mt">判断</div>${head}${kvRows(rest)}`;
}
function recordBody(s) {
  const th = (pageData && pageData.gate_thresholds) || null;
  const isJev = (s.input && s.input.jev) || (s.output && s.output.jev);
  const body = isJev ? jevView(s, th) : s.output && "approved" in s.output ? guardView(s) : `<div class="small dim">看到的</div>${kvOrEmpty(s.input)}<div class="small dim mt">判断</div>${kvOrEmpty(s.output)}`;
  return body + rawData(s);
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
const MEET_STATUS = { running: ["正在开", "p-accent"], done: ["开完了", "p-long"], failed: ["没开成", "p-short"], stale: ["中断了", "p-warn"] };
const meetPill = (s) => { const [t, k] = MEET_STATUS[s] || [s, ""]; return `<span class="pill ${k}">${t}</span>`; };
const stageNo = (s) => Object.keys(STAGE).indexOf(s) + 1;
const meetTime = (m) => (m.status === "running" && m.started_at ? `已开 ${dur(Date.now() - Date.parse(m.started_at))}` : m.started_at && (m.finished_at || m.updated_at) ? `${hm(m.started_at)} 开，用时 ${dur(Date.parse(m.finished_at || m.updated_at) - Date.parse(m.started_at))}` : "");
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
  return `<p class="dim">${meetPill(m.status)} ${meetBias(m)} ${meetTime(m)}${m.cost_usd ? ` · 花费 $${m.cost_usd.toFixed(2)}` : ""}${m.status === "running" ? " · 自动刷新" : ""}</p>
    ${isMkt ? "" : `<div class="stgs">${chips}</div>`}
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
  const pills = shown.map((s) => { const [t, k] = s.pending_label ? [s.pending_label, ""] : BIAS[s.bias] || [s.bias, ""]; return `<span class="pill ${k}">${coin(s.symbol)} ${t}</span>`; }).join("");
  const until = c.expires_at ? `有效到 ${hm(c.expires_at)}` : "有效期未知";
  return `<section class="card click" ${clickable("card")}>
    <div class="ch"><h2>作战卡</h2><span class="pill">军师团${c.version ? " v" + c.version : ""}</span>
      <span class="r">${hm(c.created_at)} 更新 · ${c.valid ? until : '<span class="down">已过期，不开新仓</span>'} <button class="btn b-sm" ${clickable("cmd", "refresh_cards")}>重新开会</button></span></div>
    <div class="row">${pills || '<span class="dim">这一轮没有要做的币</span>'}${more > 0 ? `<span class="dim small">+${more} 个</span>` : ""}</div>
    ${idle.length ? `<div class="dim small">${idleLine(idle)}（不开仓）</div>` : ""}
    ${cardEffective(c)}
    ${c.regime ? `<div class="note"><b>军师纪要</b>　${esc(c.regime)}</div>` : ""}
    <div class="foot">点开看每个币的方向、止损、杠杆和理由 ›</div>
  </section>`;
}

// 作战卡摘要：只写保镖下单真会用的数（杠杆、每笔风险金额）；军师团给的范围只是建议，点开每个币才看得到
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
  return `<section class="card"><div class="ch"><h2>信号与决策流</h2><span class="r">今天 · 最新在上</span></div>${body}
    <div class="foot">绿 = 放行 / 开仓 · 黄 = 拍板放弃 · 红 = 保镖拒绝 · 蓝 = 平仓。点一条看整条链和快照。</div></section>`;
}

function posSection(list) {
  const body = list.length
    ? list.map((p) => {
        const [s, sc] = SIDE[p.side] || [p.side, ""];
        const lb = LIFEBOAT[p.lifeboat_status] || "⚠ 没有保命单";
        const lbBad = p.lifeboat_status !== "active";
        return `<a class="pos click" href="#positions/${encodeURIComponent(p.position_id)}"><div class="top"><b>${coin(p.symbol)} <span class="${sc}">${s}</span> ${qty(p.quantity, p.mark_price || p.entry_price)}</b>
          <span class="pnl ${cls(netPnl(p))}" title="${esc(feeTip(p))}">${netPnl(p) == null ? "—" : signed(netPnl(p), 2) + "U"}</span></div>
          <div class="meta"><span>开 ${price(p.entry_price, p.symbol)} · 损 ${price(p.stop_loss_price, p.symbol)} · 盈 ${price(p.take_profit_price, p.symbol)}</span></div>
          <div class="meta"><span class="${lbBad ? "warn" : ""}">${lb}${p.lifeboat_price ? " " + price(p.lifeboat_price, p.symbol) : ""}</span><span>管家：${esc(p.keeper || "还没看")}</span>${p.status !== "open" ? `<span class="pill p-warn">${esc(p.status)}</span>` : ""}</div></a>`;
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
    const go = `onclick="if (!event.target.closest('button')) location.hash='positions/${encodeURIComponent(p.position_id)}'"`;
    const side = `<span class="pill ${p.side === "long" ? "p-long" : "p-short"}">${s}</span>`;
    const split = p.fees_est_usdt != null && p.unrealized_usdt != null ? `<div class="dim small">浮动 ${signed(p.unrealized_usdt, 2)} · 费 −${money(p.fees_est_usdt, 2)}${p.fee_from_fills ? "" : "（估）"}</div>` : "";
    const pnl = netPnl(p) == null ? "—" : `${signed(netPnl(p), 2)} U${netPct(p) == null ? "" : ` <span class="small">${signed(netPct(p), 2)}%</span>`}${split}`;
    const lb = `<span class="${p.lifeboat_status === "active" ? "" : "warn"}">${LIFEBOAT[p.lifeboat_status] || "⚠ 没有保命单"} ${p.lifeboat_price ? price(p.lifeboat_price, p.symbol) + lbPct(p) : ""}</span>`;
    const hold = `${dur(held)} / ${dur(p.time_limit_s * 1000)}`;
    const closeBtn = `<button class="btn b-sm b-danger" ${clickable("close1", p.position_id)}>平仓</button>`;
    return `<tr class="click ${on ? "sel" : ""}" ${go}>
      <td class="hi">${coin(p.symbol)}</td><td>${side}</td><td class="r num">${qtyFmt(p.quantity)}</td>
      <td class="r num">${p.notional_usdt == null ? "—" : money(p.notional_usdt, 0)}</td><td class="r num">${p.leverage}x</td>
      <td class="r num">${p.margin_usdt == null ? "—" : money(p.margin_usdt, 0)}</td>
      <td class="r num">${price(p.entry_price, p.symbol)}</td><td class="r num">${price(p.mark_price, p.symbol)}</td>
      <td class="r num ${cls(netPnl(p))}" title="${esc(feeTip(p))}">${pnl}</td>
      <td class="r num">${price(p.stop_loss_price, p.symbol)}</td><td class="r num">${price(p.take_profit_price, p.symbol)}</td>
      <td class="r num">${hold}</td><td>${lb}</td><td>${esc(p.keeper || "—")}</td><td class="r">${closeBtn}</td></tr>`;
  }).join("");
  // 手机宽度：每笔两行，第一行 币种 方向 浮盈，第二行其余
  const cards = d.positions.map((p) => {
    const [s] = SIDE[p.side] || [p.side, ""];
    const on = p.position_id === sel.position_id;
    const held = now - new Date(p.opened_at).getTime();
    const lbOk = p.lifeboat_status === "active";
    return `<div class="pm ${on ? "sel" : ""}"><a href="#positions/${encodeURIComponent(p.position_id)}">
      <div class="l1"><b>${coin(p.symbol)}</b> <span class="pill ${p.side === "long" ? "p-long" : "p-short"}">${s}</span>
        <span class="r num ${cls(netPnl(p))}">${netPnl(p) == null ? "—" : `${signed(netPnl(p), 2)} U`}${netPct(p) == null ? "" : ` · ${signed(netPct(p), 2)}%`} <span class="dim">扣费后</span></span></div>
      <div class="l2 small dim">数量 ${qtyFmt(p.quantity)} · 名义 ${p.notional_usdt == null ? "—" : money(p.notional_usdt, 0)} U · ${p.leverage}x · 占用 ${p.margin_usdt == null ? "—" : money(p.margin_usdt, 0)} U<br>
        开 ${price(p.entry_price, p.symbol)} · 现 ${price(p.mark_price, p.symbol)} · 损 ${price(p.stop_loss_price, p.symbol)} · 盈 ${price(p.take_profit_price, p.symbol)} · 拿了 ${dur(held)} / ${dur(p.time_limit_s * 1000)}</div></a>
      <div class="pm-act small"><span class="${lbOk ? "dim" : "warn"}">${LIFEBOAT[p.lifeboat_status] || "⚠ 没有保命单"} ${p.lifeboat_price ? price(p.lifeboat_price, p.symbol) + lbPct(p) : ""}</span><button class="btn b-sm b-danger" ${clickable("close1", p.position_id)}>平仓</button></div></div>`;
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
      <thead><tr><th>币种</th><th>方向</th><th class="r">数量</th><th class="r">名义（U）</th><th class="r">杠杆</th><th class="r" title="名义 ÷ 杠杆">本金占用（U）</th><th class="r">开仓价</th><th class="r">现价</th><th class="r" title="浮动盈亏 − 开平两次手续费（按吃单费率估）；百分比按名义算，不乘杠杆">浮盈·扣费后（U / %）</th><th class="r">止损价</th><th class="r">止盈价</th><th class="r">拿了多久 / 最长</th><th>保命单</th><th>管家</th><th></th></tr></thead>
      <tbody>${rows}</tbody></table></div><div class="pos-narrow">${cards}</div></section>
    <section class="card mt">
      <div class="ch"><h2>${coin(sel.symbol)} <span class="${sc}">${s}</span> ${qty(sel.quantity, sel.mark_price || sel.entry_price)} · 详情</h2>
        <span class="r"><span class="seg">${["1m", "5m", "15m", "1h"].map((t) => `<button class="${S.tf === t ? "on" : ""}" ${clickable("tf", t)}>${t}</button>`).join("")}</span></span></div>
      <div class="chart" id="kchart-host"></div>
      <div class="legend small dim"><span><i style="border-color:var(--up)"></i>止盈</span><span><i style="border-color:var(--faint);border-style:solid"></i>开仓</span><span><i style="border-color:var(--sl)"></i>止损</span><span><i style="border-color:var(--down);border-style:dotted"></i>保命</span></div>
      <div><div class="lbl">从信号到现在的每一步（今天）· 点一条看快照</div><div class="log trail">${trail || '<div class="dim">今天的日志里没有这笔的记录。</div>'}</div></div>
    </section>`;
};

/* K 线图：图表 DOM 常驻，页面重画时搬回新的容器，免得每 5 秒闪一次 */
let chart = null, series = null, chartEl = null, chartKey = "", priceLines = [], linePrices = [], klineSeq = 0, needFit = false;
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
  const switched = key !== chartKey;
  if (switched) {
    // 换了一笔 / 换了周期：先清掉上一个币的 K 线和价格线，价格轴重新自动缩放（不然会停在上一个币的价格区间）
    chartKey = key;
    series.setData([]);
    priceLines.forEach((l) => series.removePriceLine(l));
    priceLines = [];
    linePrices = [];
    chart.priceScale("right").applyOptions({ autoScale: true });
    needFit = true;
  }
  const seq = ++klineSeq;
  try {
    const r = await fetch(`/api/klines?symbol=${encodeURIComponent(p.symbol)}&interval=${S.tf}`, { cache: "no-store" });
    if (!r.ok) throw new Error(String(r.status));
    const bars = await r.json();
    if (seq !== klineSeq || key !== chartKey) return; // 等的时候又换了：这份是旧币的，不画
    series.setData(bars);
    const dg = pxDigits(p.entry_price || 1, p.symbol);
    series.applyOptions({ priceFormat: { type: "price", precision: dg, minMove: 10 ** -dg } });
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
  if (force || needFit) chart.timeScale().fitContent();
  needFit = false;
}

/* ---------- 团队 */
/* 流程图（设计稿 /mnt/project-files/design/team-flow/handoff.md）：三条泳道，节点固定坐标，连线 SVG；
   宽度不够整体缩放，手机上改成按泳道竖排。点 = 进度 + 健康：出错 > 正在做 > 这一轮做完 > 等待 */
const TF_W = 1180, TF_H = 570, NODE_W = 168, NODE_H = 92;
const TF_POS = {
  screener: [10, 34], market: [250, 34], commander: [490, 34], card: [730, 34],
  book_reader: [10, 214], predictor: [250, 290], sentinel: [490, 214], gate: [730, 214], risk_guard: [970, 186], executor: [970, 300],
  alerting: [10, 446], reconciler: [250, 446], patrol: [490, 446], keeper: [730, 446], lifeboat: [970, 446],
};
const TF_LANES = [
  ["① 准备作战卡", "每轮开会出一张", 0, 150, ["screener", "market", "commander", "card"]],
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
  [tfR("market"), tfL("commander"), "commander", "上会名单"],
  [tfB("market"), tfT("book_reader"), "book_reader", "要盯的币", "gap"],
  [tfR("commander"), tfL("card"), "card", "写卡"],
  [tfB("card"), tfT("sentinel"), "sentinel", "关键价位", "gap"],
  [tfR("card"), tfT("risk_guard"), "risk_guard", "止损范围", "elbow", "side"],
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
function tfEdge([a, b, to, label, how, extra], prog) {
  const [x1, y1] = a, [x2, y2] = b;
  const st = prog[to] === "doing" ? "doing" : prog[to] === "done" ? "done" : "";
  const cls = `e ${st} ${extra || ""}`;
  let d, lx, ly, dir;
  if (how === "elbow") { d = `M${x1},${y1} H${x2 - 12} Q${x2},${y1} ${x2},${y1 + 12} V${y2 - 1}`; lx = x2 - 70; ly = y1 - 6; dir = "d"; }
  else if (how === "down-right") { d = `M${x1},${y1} V${y2 - 10} Q${x1},${y2} ${x1 + 10},${y2} H${x2 - 1}`; lx = x1 + 14; ly = y2 - 6; dir = "r"; }
  else if (how === "tree") { const yy = 428; d = `M${x1},${y1} H${x1 - 22} V${yy} H${x2} V${y2 - 1}`; lx = x1 - 120; ly = yy - 6; dir = "d"; }
  // 从上一条泳道的节点底下，沿泳道之间的空隙横过去，再落到下一条泳道的节点顶上
  else if (how === "gap") { const ym = 158, k = x2 < x1 ? -10 : 10; d = `M${x1},${y1} V${ym - 10} Q${x1},${ym} ${x1 + k},${ym} H${x2 - k} Q${x2},${ym} ${x2},${ym + 10} V${y2 - 1}`; lx = Math.min(x1, x2) + 16; ly = ym - 4; dir = "d"; }
  else if (how === "s") { const mx = (x1 + x2) / 2; d = `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2 - 1},${y2}`; lx = x1 + 8; ly = y1 - 6; dir = "r"; }
  else if (x1 === x2) { d = `M${x1},${y1} V${y2 - 1}`; lx = x1 + 8; ly = how === "v-top" ? y1 + 18 : (y1 + y2) / 2 + 4; dir = "d"; }
  else { d = `M${x1},${y1} H${x2 - 1}`; lx = (x1 + x2) / 2 - label.length * 5.5; ly = y1 - 6; dir = "r"; }
  const ah = dir === "r" ? `M${x2},${y2} l-7,-4 v8 z` : `M${x2},${y2} l-4,-7 h8 z`;
  return `<path class="${cls}" d="${d}"/><path class="ah ${st}" d="${ah}"/>${label ? `<text x="${lx}" y="${ly}">${esc(label)}</text>` : ""}`;
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
  const st = line ? line : !m ? "还没有记录" : m.status === "running" ? "" : m.status === "failed" ? `失败：${m.error || "没写原因"}` : m.status === "stale" ? "中断了（超过 15 分钟没更新）" : `上次 ${hm(m.finished_at || m.updated_at || m.started_at)}`;
  const now = m && m.status === "running" ? `开会中：${MARKET_DOING[m.stage] || m.stage || "开始"}` : "";
  return `<span class="nm"><i class="dot ${p}" title="${PROG_LABEL[p]}"></i>总盘会</span>${now ? `<small class="now">${esc(now)}</small>` : ""}${st ? `<small>${esc(st)}</small>` : ""}<small class="cnt">挑上会的币、要盯的币</small>`;
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
function tfNode(r) {
  const line = r.state === "ok" ? runLine(r) : "";
  const status = r.role === "alerting" && r.progress !== "err" ? "任何一步出事都推给你" : line || (r.role === "predictor" ? predictorText(r) : r.status);
  return `<span class="nm"><i class="dot ${r.progress}" title="${PROG_LABEL[r.progress]}"></i>${esc(r.name)}${hbBadge(r)}</span>
    ${r.doing ? `<small class="now">${esc(r.doing)}</small>` : ""}<small title="${esc(r.headline || r.status)}">${esc(status)}</small>
    <small class="cnt">今日 ${r.calls} 次${r.rejected ? ` · 拒 ${r.rejected}` : ""}${r.errors ? ` · <span class="down">错 ${r.errors}</span>` : ""}${r.cost_usd ? ` · $${r.cost_usd.toFixed(3)}` : ""}</small>`;
}
function teamFlow(d) {
  const by = Object.fromEntries(d.roles.map((r) => [r.role, r]));
  const prog = Object.fromEntries(d.roles.map((r) => [r.role, r.progress]));
  prog.card = data && data.card && data.card.valid ? "done" : "wait";
  prog.market = d.market_progress;
  const node = (k, inCanvas) => {
    if (k === "market") return `<div class="tf-node ${d.market_progress}" ${clickable("meeting", MARKET)} style="${inCanvas ? `left:${TF_POS.market[0]}px;top:${TF_POS.market[1]}px` : ""}">${tfMarketNode(d.market, d.market_progress, d.market_role)}</div>`;
    if (k === "card") { const n = tfCardNode(); return `<div class="tf-node doc" ${n.act} style="${inCanvas ? `left:${TF_POS.card[0]}px;top:${TF_POS.card[1]}px` : ""}">${n.html}</div>`; }
    const r = by[k];
    if (!r) return "";
    return `<a class="tf-node ${r.progress} ${r.role === d.selected ? "on" : ""}" href="#team/${r.role}" style="${inCanvas ? `left:${TF_POS[k][0]}px;top:${TF_POS[k][1]}px` : ""}">${tfNode(r)}</a>`;
  };
  // 盯持仓是常态，放最后说；其余按流程顺序最多说两处
  const busy = FLOW_ORDER.map((k) => by[k]).filter((r) => r && r.doing);
  const main = busy.filter((r) => !r.doing.startsWith("盯着"));
  const watching = busy.find((r) => r.doing.endsWith("笔持仓"));
  const now = main.length
    ? `现在走到：${main.slice(0, 2).map((r) => `${esc(r.name)} ${esc(r.doing)}`).join("；")}${main.length > 2 ? ` 等 ${main.length} 处` : ""}`
    : watching ? `现在走到：看着持仓（${esc(watching.doing)}）` : "现在走到：哨兵盯着信号，等下一个机会";
  const legend = `<div class="tf-legend" aria-label="图例">${["done", "doing", "wait", "err"].map((p) => `<span class="lg"><i class="dot ${p}"></i>${{ done: "这一轮已完成", doing: "正在做", wait: "等待（还没轮到 / 没活）", err: "出错或没心跳" }[p]}</span>`).join("")}<span class="lg"><em class="hb ok">心跳正常</em>右上角 = 模块心跳</span><span class="tf-now">${now}</span></div>`;
  const known = new Set([...Object.keys(TF_POS)]);
  const extra = d.roles.filter((r) => !known.has(r.role));
  const lanes = TF_LANES.map(([t, sub, y, h]) => `<div class="tf-band" style="top:${y}px;height:${h}px"><b>${t}<span>${sub}</span></b></div>`).join("");
  const edges = `<svg class="tf-edges" width="${TF_W}" height="${TF_H}" viewBox="0 0 ${TF_W} ${TF_H}" aria-hidden="true">${TF_EDGES.map((e) => tfEdge(e, prog)).join("")}</svg>`;
  const canvas = `<div class="tf-fit"><div class="tf-canvas" style="width:${TF_W}px;height:${TF_H}px">${lanes}${edges}${Object.keys(TF_POS).map((k) => node(k, true)).join("")}
    <div class="tf-watch"><span>看门狗在外面守着：主程序卡住或退出就自动拉起</span></div></div></div>`;
  // 手机：按泳道竖排，不画线
  const stack = `<div class="tf-stack">${TF_LANES.map(([t, sub, , , ks]) => `<div class="tf-lane"><b>${t}<span>${sub}</span></b>${ks.map((k) => node(k, false)).join("")}</div>`).join("")}</div>`;
  const rest = extra.length ? `<div class="tf-stack tf-extra">${extra.map((r) => node(r.role, false)).join("")}</div>` : "";
  return `${legend}<section class="card tf">${canvas}${stack}${rest}</section>`;
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
P.team = () => {
  const d = pageData;
  const cards = teamFlow(d);
  const sel = d.roles.find((r) => r.role === d.selected);
  window.__recs = d.records;
  const recs = d.records.map((s, i) => `<div class="click" ${clickable("record", i)}><span class="n">${hms(s.ts)}</span><span>${pill(s.outcome)} ${s.symbol ? "" : ""}${esc(s.text)}</span></div>`).join("");
  return `${cards}
    ${sel.role === "commander" ? meetingsCard(d.meetings || []) : ""}
    ${sel.role === "book_reader" ? bookReaderCard(sel, d.snapshot) : ""}
    <section class="card mt"><div class="ch"><h2>${esc(sel.name)} · 今天做了什么</h2>
      <span class="r">${sel.last_beat ? "最后心跳 " + hms(sel.last_beat) : "没有心跳"}${sel.avg_latency_ms ? ` · 平均 ${Math.round(sel.avg_latency_ms)} ms` : ""}</span></div>
      ${sel.last_error ? `<div class="note warn">最近出错：${esc(sel.last_error)}</div>` : ""}
      <div class="log trail scroll tall">${recs || `<div class="dim">${sel.role === "book_reader" ? "盘口员不写逐条记录，状态见上面。" : "今天还没有记录。"}</div>`}</div>
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
    [signed(d.net_pnl_usdt, 2) + "U", "交易净盈亏（已扣手续费，未扣 AI 费）", cls(d.net_pnl_usdt)],
    [d.trades + " 笔", "交易"],
    [d.win_rate == null ? "—" : Math.round(d.win_rate * 100) + "%", "胜率"],
    [d.avg_win_loss == null ? "—" : d.avg_win_loss.toFixed(2), "平均盈亏比"],
    [d.max_drawdown_usdt ? "−" + money(d.max_drawdown_usdt, 2) + "U" : "0", "最大回撤", d.max_drawdown_usdt ? "down" : ""],
  ];
  const kinds = d.by_kind.map((k) => `<tr><td class="hi">${esc(SIGNAL[k.kind] || ({ unknown: "未知", test: "测试单" })[k.kind] || k.kind)}</td><td class="r num">${k.trades}</td><td class="r num">${k.win_rate == null ? "—" : Math.round(k.win_rate * 100) + "%"}</td><td class="r num ${cls(k.pnl_usdt)}">${signed(k.pnl_usdt, 2)}</td></tr>`).join("");
  const rows = d.rows.map((t) => {
    const [s, sc] = SIDE[t.side] || [t.side, ""];
    return `<tr class="click" ${clickable("signal", t.signal_id)}><td class="num">${hm(t.closed_at)}</td><td class="hi">${coin(t.symbol)} <span class="${sc}">${s}</span> ${qty(t.quantity, t.entry_price)}</td>
      <td>${t.test ? '<span class="pill p-warn" title="没有经过哨兵 / 拍板 / 保镖：冒烟测试或手动下的单">测试</span>' : esc(SIGNAL[t.signal_kind] || "—")}</td><td class="num">${price(t.entry_price, t.symbol)}</td><td>${esc(REASON[t.close_reason] || t.close_reason || "—")}</td>
      <td class="num">${dur(new Date(t.closed_at) - new Date(t.opened_at))}</td><td class="r num ${cls(t.pnl_usdt + t.fees_usdt)}">${signed(t.pnl_usdt + t.fees_usdt, 2)}</td><td class="r num dim">−${money(t.fees_usdt, 2)}</td><td class="r num ${cls(t.pnl_usdt)}"><b>${signed(t.pnl_usdt, 2)}</b></td></tr>`;
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
        ${d.rows.length ? `<div class="tw scroll tall"><table><thead><tr><th>平仓</th><th>币种</th><th>信号</th><th>开仓价</th><th>怎么平的</th><th>持有</th><th class="r">毛利</th><th class="r">手续费</th><th class="r">净利</th></tr></thead><tbody>${rows}</tbody></table></div><div class="foot">净利 = 毛利 − 手续费（已扣手续费，不含 AI 费；AI 费在「资金」页）。</div>` : '<div class="dim">没有。</div>'}</section>
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
const CAT_FROM = { pool: "选币页设的", slow: "slow.toml", default: "默认" };
function categoryCard(d) {
  if (!d.categories || !d.categories.length) return "";
  const can = !d.overrides_error;
  const b = (arg, text, on) => `<button class="${on ? "on" : ""}" ${can && !on ? clickable("cat", arg) : "disabled"}>${text}</button>`;
  const rows = d.categories.map((c) => {
    const k = c.category;
    const back = (arg) => (can ? ` <button class="lnk small" ${clickable("cat", arg)}>改回 slow.toml</button>` : "");
    return `<tr class="${c.enabled ? "" : "dimrow"}"><td class="hi">${esc(CAT[k] || k)}</td>
      <td><span class="seg">${b(`set_category:${k}:true`, "开", c.enabled)}${b(`set_category:${k}:false`, "关", !c.enabled)}</span><div class="dim small">${CAT_FROM[c.enabled_from]}${c.enabled_from === "pool" ? back(`set_category:${k}:default`) : ""}</div></td>
      <td class="r num">${c.cap} 个<div class="dim small">${CAT_FROM[c.cap_from]}${c.cap_from === "pool" ? back(`set_category_cap:${k}:default`) : ""}</div></td>
      <td class="r num">${c.in_pool} / ${c.total}</td>
      <td class="r nowrap">${can ? `<button class="btn b-sm" ${clickable("cat", `set_category_cap:${k}:${c.cap}`)}>改上限</button>` : ""}</td></tr>`;
  }).join("");
  const off = d.categories.filter((c) => !c.enabled).length;
  return `<section class="card mt"><div class="ch"><h2>按类别选币</h2><span class="r">${off ? `关了 ${off} 类` : "六类都开着"} · 每类默认最多 ${d.default_category_cap} 个</span></div>
    ${d.slow_error ? `<div class="note warn">slow.toml 读不出：下面“slow.toml”一栏按默认显示。${esc(d.slow_error)}</div>` : ""}
    <div class="tw"><table><thead><tr><th>类别</th><th>开关</th><th class="r">最多进池</th><th class="r">上次进池 / 看了</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>
    <div class="foot">关掉的类别不再靠打分进池，你固定、手动加的币仍进池。开关和上限要面板密码，改完主程序马上重新选币（“上次进池”等这次选完才变）。“改回 slow.toml”清掉选币页的这一项，改用 slow.toml 里写的（没写就是默认）。</div></section>`;
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
// 临时会触发事件、花到日上限 80% 怎么办（slow.toml [schedule]）
const TRIGGER = { key_level_break: "突破关键价位", move_since_card: "离上次开会价格走了好几个波动幅度", new_head_symbol: "头部新币进池", predictor_against_card: "预测员连续看反作战卡", news_high_impact: "重大消息", stale_card_on_entry: "开仓时卡太旧" };
const CHOICE = { ...TRIGGER, slow: "放慢一档", stop: "不放慢", on_demand: "按需（作战卡和持仓的币）", all_symbols: "候选池全算", fixed: "固定", adaptive: "按排队自适应", mini: "小模型（最快）", small: "中模型", base: "大模型（最准）", demo: "Demo Trading", legacy_testnet: "旧测试网", typesafe: "Jev 官方直连", openrouter: "OpenRouter", "Asia/Bangkok": "曼谷时间", "Asia/Shanghai": "北京时间", UTC: "世界标准时间（UTC）", "24h": "24 小时", "12h": "12 小时", "6h": "6 小时", "8h": "8 小时", "4h": "4 小时", "2h": "2 小时", "1h": "1 小时", "60m": "1 小时", "30m": "30 分钟", "15m": "15 分钟" };
const MODEL = { base: "最大（更准，每币约 3.5 秒）", small: "标准（每币约 1 秒）", mini: "最快（最粗）" };
const choiceLabel = (k, v) => (k === "atr_period_min" ? `${v} 分钟` : k === "model_size" ? MODEL[v] || String(v) : k === "mid_size_max" ? { S: "小", M: "中", L: "大" }[v] || String(v) : CHOICE[v] || String(v));
// 描述太长的项给个短名字（完整说明仍在契约里）
const SHORT = { mode: "运行方式", size_policy: "模型尺寸怎么选" };
// 设置项的大白话标题；原来的专业说明和英文键名降为灰色小字
const PLAIN = {
  "screener.min_quote_volume_usdt": "24 小时成交额至少多少 U 才进池",
  "screener.max_spread_bps": "买卖价差超过万分之几就不进池",
  "screener.min_depth_usdt": "盘口深度至少多少 U",
  "screener.min_listed_days": "上线至少几天",
  "screener.min_range_24h_pct": "24 小时波动至少百分之几",
  "screener.max_range_24h_pct": "24 小时波动最多百分之几",
  "screener.default_category_cap": "每一类币默认最多进池几个",
  "screener.vol_full_multiple": "波动是来回手续费的几倍算满分",
  "screener.surge_full_multiple": "1 小时成交额是平时的几倍算满分",
  "screener.funding_full": "资金费率到多少算满分",
  "screener.oi_full_pct": "持仓量 1 小时变百分之几算满分",
  "screener.liq_full_usdt": "1 小时强平多少 U 算满分",
  "screener.us_events": "美股类用开盘时段和财报日历",
  "screener.us_open_bonus": "美股开盘期间，美股类的币总分加多少",
  "tiers.enabled": "分档交易（头部做、中部挑着做、尾部只看）",
  "tiers.head": "头部几个币（开放交易）",
  "tiers.mid": "中部几个币",
  "tiers.mid_daily_pick": "中部每天挑几个来做",
  "tiers.mid_size_max": "中部的币仓位最大到哪一档",
  "screener.weight_volatility": "波动的权重",
  "screener.weight_volume_surge": "量能突增的权重",
  "screener.weight_funding": "资金费率的权重",
  "screener.weight_oi": "持仓量变化的权重",
  "screener.weight_liquidations": "强平的权重",
  risk_per_trade_pct: "每笔最多亏本金的百分之几",
  max_leverage: "最多开几倍杠杆",
  max_total_exposure_x: "所有仓位加起来（含杠杆）最多是本金的几倍",
  daily_loss_halt_pct: "今天亏到本金的百分之几就停止开新仓",
  max_notional_per_trade_x: "单笔仓位（含杠杆）最大是本金的几倍",
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
  keeper_interval_s: "管家每隔几秒看一次持仓",
  reconcile_interval_s: "每隔几秒和交易所对一次账",
  slow_loop_interval_min: "军师团每隔几分钟开一次会（档位 = 开会间隔）",
  screener_interval_min: "选币每隔几分钟跑一次（默认跟开会间隔档位走，最少 15 分钟）",
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
  model_size: "预测模型",
  adaptive_base_max_queue: "排队不超过几个时用最准的模型",
  adaptive_mini_min_queue: "排队到几个时换最快的模型",
  backfill_bars: "启动时先拉多少根 5 分钟 K 线",
  max_cards_per_round: "每轮最多给几个币开会",
  concurrency: "同时开几个会",
  card_ttl_min: "作战卡管用几分钟（开会间隔 + 提前量）",
  symbol_min_interval_min: "同一个币至少隔几分钟才再开会",
  refresh_lead_min: "作战卡到期前几分钟开会换新卡",
  symbol_timeout_s: "一个币的会最多开几秒",
  min_refresh_gap_min: "两轮会至少隔几分钟",
  schedule_at: "每天固定几点开整轮会（曼谷时间）",
  event_meeting_enabled: "出事时给那一个币开临时会",
  event_triggers: "哪些事算“出事”",
  event_symbol_gap_min: "同一个币两场会至少隔几分钟",
  event_max_per_day: "一天最多开几场临时会",
  move_since_card_atr: "价格离上次开会走了几个波动幅度算“走远了”",
  predictor_against_runs: "预测员连续几次看反才算",
  entry_max_card_age_min: "开仓前卡最多旧几分钟（超了先补会再开）",
  daily_cost_limit_usd: "每日 AI 费用上限",
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
  jev_provider: "官方直连要 Jev 的 key；OpenRouter 和军师团共用一把 key",
  jev_model: "锁死一个版本，门槛按它调过；不在网页上改",
  jev_usd_per_mtok: "按用量算钱，用来统计花费",
  sim_environment: "Demo Trading 是币安现在的模拟盘；旧测试网已停用",
  mode_predictor: "按需 = 只算作战卡里和有持仓的币；全算 = 候选池每个币都算（费电脑）",
  size_policy: "固定 = 一直用下面选的模型；自适应 = 排队多时自动换小模型",
  model_size: "标准 / 最大：最大更准，但每个币算得慢 3–4 倍（Mac mini 每币约 3.5 秒，标准约 1 秒）；加载失败会自动改用标准（选“固定”时用这个）",
  adaptive_base_max_queue: "选“自适应”时生效",
  adaptive_mini_min_queue: "选“自适应”时生效；介于两者之间用中模型",
  profile: "在「运行」卡片的开会间隔里切换",
  meeting_interval: "在「运行」卡片里一键切换",
  hold: "在「运行」卡片里一键切换；不写 = 跟开会档位走",
  data_dir: "决策记录、状态文件都存在这里",
  schedule_at: "填了就每天这个时间开，不按启动时间漂；留空 = 按开会间隔",
  event_meeting_enabled: "只给出事的那个币补开一场，不重开整轮；关掉时突破、大消息、新进池照旧重开整轮",
  event_triggers: "不在名单里的事不开会，等下一轮定时会",
  entry_max_card_age_min: "要在“哪些事算出事”里勾上“开仓时卡太旧”才会补会，否则只拦不补",
  daily_cost_limit_usd: "只算军师团开会（含总盘会）的 AI 费；没写死就跟开会档位走，切档自动换",
  over_budget: "放慢一档 = 开会间隔换成下面那个；不放慢 = 照常开。花到 100% 都停开会，持仓照常管",
};
// 灰色小字只放中文说明；英文键名只在鼠标悬停时显示（排查问题用）
const hintKey = (i) => (i.key === "mode" && typeof i.value === "string" && i.value in CHOICE ? "mode_predictor" : i.key);
const techHint = (i) => [HINT[hintKey(i)] ? esc(HINT[hintKey(i)]) : plainOf(i) && i.label && i.label !== i.key && !/^[\x00-\x7f]+$/.test(i.label) ? esc(i.label) : "", i.recommended != null ? `推荐 ${valOf(i, i.recommended)}（${esc(i.recommended_why)}）` : i.bounds ? "范围 " + esc(i.bounds) : ""].filter(Boolean).join(" · ");
const NULL_AS = { schedule_at: "不定点，按开会间隔", slowdown_interval_min: "下一档", max_cards_per_round: "跟“军师团每次最多挑几个币”一样", "screener.max_spread_bps": "跟硬上限里的“最大价差”走", "risk.max_spread_bps": "不写：按这笔止损的 1/10", "risk.max_slippage_bps": "不写：按这笔止损的 1/4" };
// 这几项填 0 = 不设限
const ZERO_AS = { min_range_24h_pct: "不设", max_range_24h_pct: "不设", entry_max_card_age_min: "不查" };
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
const fmtVal = (v, i) => Array.isArray(v) ? esc(v.map((c) => choiceLabel(i ? i.key : "", c)).join("、") || "一个都不选") : (i && !(i.choices && i.choices.length) && durOf(i.key, v)) || (typeof v === "boolean" ? (v ? "开" : "关") : v == null ? (i && i.nullable ? NULL_AS[`${i.sec}.${i.key}`] || NULL_AS[i.key] || "不写（用默认）" : "—") : (i && i.choices && i.choices.length) || (typeof v === "string" && CHOICE[v]) ? esc(choiceLabel(i ? i.key : "", v)) : esc(v));
// 档位 = 军师团开会间隔（Aaron 2026-10-23），不是持仓时长；单笔最长持仓另设
const profileSpan = (p) => { const m = /^(\d+)(h|m)$/.exec(p); return m ? (m[2] === "h" ? `${m[1]} 小时` : `${m[1]} 分钟`) : p; };
const profileNote = (p) => `军师团每 ${profileSpan(p)}开一次会${p === "1h" ? "（默认）" : ""}`;
// 止损 ATR 用几分钟 K 线，适合多长的单笔最长持仓（只提醒，不拦）
const ATR_FIT = { 5: ["≤30 分钟", 0, 1800], 15: ["1–2 小时（推荐）", 1800, 7200], 30: ["2–4 小时", 7200, 14400], 60: ["4 小时以上", 14400, Infinity] };
const atrFor = (limit) => +Object.keys(ATR_FIT).find((k) => limit > ATR_FIT[k][1] && limit <= ATR_FIT[k][2]);
const atrMismatch = (atr, limit) => (ATR_FIT[atr] && atrFor(limit) !== +atr ? `${atr} 分钟 K 线适合最长持仓 ${ATR_FIT[atr][0].replace("（推荐）", "")}，现在最长持仓 ${humanDur(limit)}；建议 ${atrFor(limit)} 分钟。只提醒，不拦。` : "");
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
    ? `<div class="note warn">下面这些项写死在配置文件里，切到 ${esc(p)} 后仍按文件里的值，不跟默认走：</div>${pinTable(list, p)}
      <label class="small"><input type="checkbox" id="dlg-clear" checked> ${CLEAR_TEXT}（改前自动备份）</label>`
    : "";
  const nextHold = pf.hold_recommended[p] || pf.hold;
  const holdNote = pf.hold_custom
    ? `最长持仓是你定的 ${profileSpan(pf.hold)}，不跟着换${nextHold !== pf.hold ? `（${profileSpan(p)}开会推荐 ${profileSpan(nextHold)}，只提醒）` : ""}。`
    : `最长持仓跟开会档位走，${nextHold === pf.hold ? `还是 ${profileSpan(nextHold)}` : `从 ${profileSpan(pf.hold)} 换成 ${profileSpan(nextHold)}`}。`;
  ask(`切换开会间隔：${esc(profileSpan(pf.current))} → ${esc(profileSpan(p))}`, `<p>${esc(profileNote(p))}，作战卡有效期 = 间隔 + 20 分钟，选币也按这个间隔跑。${esc(holdNote)}</p>${warn}${pwField(data.control)}`, "切换", false, async () => {
    const pw = $("#dlg-pw");
    const clear = $("#dlg-clear");
    const pass = pw ? pw.value : "";
    // 先清写死项（按当前档位校验），再切档位
    const cleared = clear && clear.checked ? (await clearPins(pass)).changed.length : 0;
    await post("/api/settings", { section: "app", changes: { meeting_interval: p }, password: pass });
    onOk = null;
    $("#layer [data-ok]").remove();
    $("#layer [data-close].btn").textContent = "关闭";
    dlgMsg(`<b class="up">已切到每 ${esc(profileSpan(p))}开会</b>${cleared ? `，清掉 ${cleared} 个写死项` : ""}。点「重启生效」后按新档位运行。`);
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
  ask(`单笔最长持仓：${what}`, `<p>到 ${esc(profileSpan(target))}还没走就平；止损按 ${esc(spec.atr_period_min)} 分钟 K 线 ATR，止盈、拍板门槛、管家频率跟着这一档。只对之后新开的仓生效。</p>${tip}${dropTl ? `<p class="small">配置文件里写死的「一笔最多拿 ${esc(humanDur(tl.value))}」会一起删掉。</p>` : ""}${pwField(data.control)}`, "确定", false, async () => {
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

// 设置页 v2：首屏只放“运行”和三个核心风险，其余收进“高级”的五个标签；每项一行，点开再改，一次改一项
const CORE = ["risk.risk_per_trade_pct", "risk.daily_loss_halt_pct", "risk.max_leverage"];
const ADV = [
  { id: "risk", t: "风控细项", note: "首屏三项之外的仓位上限。都是你定的上限，AI 改不了。", groups: [
    { g: "仓位上限", keys: ["risk.max_total_exposure_x", "risk.max_notional_per_trade_x", "risk.max_notional_per_trade_usdt", "risk.max_open_positions"] },
    { g: "每类币仓位上限（含杠杆，本金的几倍）", cat: true, hint: "同一类币常常一起涨跌，这样一类出事不会亏太多。每类不能超过上面的总仓位倍数。" },
  ] },
  { id: "exec", t: "执行", note: "下单时的门槛、止损止盈、管家和拍板人。带“跟最长持仓走”的改单笔最长持仓时自动换，带“跟档位走”的切档位自动换。", groups: [
    { g: "下单门槛", keys: ["risk.max_spread_bps", "risk.max_slippage_bps", "risk.taker_fee_bps", "risk.min_tp_fee_multiple"] },
    { g: "止损和止盈", keys: ["risk.lifeboat_stop_multiplier", "fast_loop.atr_period_min", "fast_loop.min_stop_atr", "fast_loop.max_stop_atr", "fast_loop.stop_level_buffer_atr", "fast_loop.min_tp_r", "fast_loop.max_tp_r", "fast_loop.time_limit_s"] },
    { g: "管家、巡逻兵和对账", keys: ["app.keeper_interval_s", "fast_loop.keeper_close_p", "fast_loop.keeper_confirmations", "fast_loop.keeper_close_p_strong", "fast_loop.patrol_alert_pct_1m", "fast_loop.patrol_shock_pct_1m", "app.reconcile_interval_s", "app.reconciler_close_unknown_positions"] },
    { g: "拍板人（每个信号做不做）", warn: "gate_cautions", keys: ["fast_loop.min_p_genuine", "fast_loop.min_p_take", "fast_loop.min_p_direction", "fast_loop.max_p_toxic", "fast_loop.size_fraction_s", "fast_loop.size_fraction_m", "app.jev_timeout_ms", "fast_loop.jev_max_consecutive_errors", "fast_loop.jev_error_cooldown_s", "fast_loop.rule_gate_without_jev", "fast_loop.jev_usd_per_mtok", "fast_loop.jev_provider", "fast_loop.jev_model"] },
    { g: "信号识别", keys: ["app.signal_dedup_s", "fast_loop.signal_max_age_s", "fast_loop.breakout_vol_ratio", "fast_loop.surge_vol_ratio", "fast_loop.surge_taker_ratio", "fast_loop.liq_min_usdt_1m", "fast_loop.liq_ratio", "fast_loop.liq_drop_pct_5m", "fast_loop.crowded_funding_rate", "fast_loop.session_window_min", "fast_loop.session_vol_ratio"] },
  ] },
  { id: "slow", t: "军师团", note: "军师团多久开一次会、挑多少个币，还有 Kronos 预测员。", groups: [
    { g: "开会节奏", keys: ["app.slow_loop_interval_min", "schedule.schedule_at", "schedule.*"] },
    { g: "临时会", hint: "作战卡有效期里出了事，只给那一个币补开一场。开会间隔 2 小时起默认开。", keys: ["schedule.event_meeting_enabled", "schedule.event_triggers", "schedule.event_symbol_gap_min", "schedule.event_max_per_day", "schedule.move_since_card_atr", "schedule.predictor_against_runs", "schedule.entry_max_card_age_min"] },
    { g: "AI 花费上限", hint: "军师团每天最多花多少（曼谷 0 点清零）；花到 80% 按下面放慢或停，花到 100% 当天停开会，持仓照常管。", keys: ["budget.daily_cost_limit_usd", "schedule.over_budget", "schedule.slowdown_interval_min"] },
    { g: "选币", keys: ["app.pool_size", "app.card_pick_max"] },
    { g: "选币规则：门槛", warn: "screener_cautions", hint: "每小时用代码把全部币安 U 本位永续筛一遍，过不了门槛的不进候选池。", keys: ["screener.min_quote_volume_usdt", "screener.max_spread_bps", "screener.min_depth_usdt", "screener.min_listed_days", "screener.min_range_24h_pct", "screener.max_range_24h_pct", "screener.default_category_cap"] },
    { g: "选币规则：打分", hint: "过了门槛的币按五项打分，取前面的进池。下面是每项到多少算满分。", keys: ["screener.vol_full_multiple", "screener.surge_full_multiple", "screener.funding_full", "screener.oi_full_pct", "screener.liq_full_usdt"] },
    { g: "分档交易", warn: "tiers_cautions", hint: "候选池按分数从高到低分三档：头部开放交易；中部每天挑几个来做、仓位封顶；其余尾部只看不做。你固定、手动加的币算头部。", keys: ["tiers.enabled", "tiers.head", "tiers.mid", "tiers.mid_daily_pick", "tiers.mid_size_max"] },
    { g: "美股类别", keys: ["screener.us_events", "screener.us_open_bonus"] },
    { g: "选币规则：权重", hint: "五项各占多少分量。不用加起来等于 1，只看相对大小；不能全是 0。", keys: ["screener.weight_volatility", "screener.weight_volume_surge", "screener.weight_funding", "screener.weight_oi", "screener.weight_liquidations"] },
    { g: "预测员 Kronos", keys: ["app.predictor_enabled", "predictor.*"] },
  ] },
  { id: "keys", t: "密钥" },
  { id: "show", t: "显示" },
];
// 别处已经显示的键（运行卡、显示页的系统信息），不再进“其他”
const SHOWN_ELSEWHERE = ["app.profile", "app.meeting_interval", "app.hold", "app.data_dir", "app.sim_environment", "risk.day_reset_tz"];
// 没列到的新键按所在段落到哪个标签的“其他”组
const SECTION_TAB = { risk: "risk", category: "risk", fast_loop: "exec", app: "exec", schedule: "slow", predictor: "slow", screener: "slow", tiers: "slow" };
const UNIT = {
  risk_per_trade_pct: "%", daily_loss_halt_pct: "%", max_leverage: " 倍", max_total_exposure_x: " 倍", max_notional_per_trade_x: " 倍",
  max_notional_per_trade_usdt: " U", max_open_positions: " 个", lifeboat_stop_multiplier: " 倍", min_tp_fee_multiple: " 倍",
  min_tp_r: " 倍", max_tp_r: " 倍", keeper_confirmations: " 次", patrol_alert_pct_1m: "%", patrol_shock_pct_1m: "%",
  jev_max_consecutive_errors: " 次", breakout_vol_ratio: " 倍", surge_vol_ratio: " 倍", liq_min_usdt_1m: " U", liq_ratio: " 倍",
  liq_drop_pct_5m: "%", session_vol_ratio: " 倍", pool_size: " 个", card_pick_max: " 个", max_cards_per_round: " 个", concurrency: " 个",
  adaptive_base_max_queue: " 个", adaptive_mini_min_queue: " 个", backfill_bars: " 根",
  min_quote_volume_usdt: " U", min_depth_usdt: " U", min_listed_days: " 天", min_range_24h_pct: "%", max_range_24h_pct: "%",
  head: " 个", mid: " 个", mid_daily_pick: " 个", daily_cost_limit_usd: " 美元",
  default_category_cap: " 个", vol_full_multiple: " 倍", surge_full_multiple: " 倍", oi_full_pct: "%", liq_full_usdt: " U",
};
// 推荐值里只有这几项是“越小越冒险”
const LOWER_RISKIER = ["min_tp_fee_multiple", "min_quote_volume_usdt", "min_depth_usdt", "min_listed_days", "min_range_24h_pct"];
const valOf = (i, v = i.value) => (v === 0 && ZERO_AS[i.key] && (i.sec === "screener" || i.sec === "schedule") ? ZERO_AS[i.key] : null) || fmtVal(v >= 1e5 && v % 1e4 === 0 && i.key.endsWith("_usdt") ? `${v / 1e4} 万` : v, i) + (typeof v === "number" && !durOf(i.key, v) && !(i.choices && i.choices.length) ? UNIT[i.cat ? "max_total_exposure_x" : i.key] || "" : "");
const hot = (i) => i.recommended != null && typeof i.value === "number" && (LOWER_RISKIER.includes(i.key) ? i.value < i.recommended : i.value > i.recommended || (i.value === 0 && i.key in ZERO_AS));

// 段.键 → 设置项（每类币上限拼成 category 段）
function settingIndex(d) {
  const ix = {};
  for (const sec of ["risk", "app", "fast_loop", "predictor", "schedule", "budget", "screener", "tiers"]) for (const i of d[sec] || []) ix[`${sec}.${i.key}`] = { ...i, sec };
  const tot = (d.risk.find((i) => i.key === "max_total_exposure_x") || {}).value;
  for (const [k, v] of Object.entries(d.category_caps)) ix[`category.${k}`] = { key: k, sec: "category", cat: true, label: CAT[k] || k, value: v, bounds: tot == null ? "" : `0–${tot}（不超过总仓位倍数）`, editable: true, choices: [] };
  return ix;
}
// 把五个标签展开成 [标签, [组名, 提示, 项…]]；没列到的键收进对应标签的“其他”
function advGroups(d, ix) {
  const used = new Set([...CORE, ...SHOWN_ELSEWHERE]);
  // 「段.*」只收别处没点名的键，点名的归到点名的那一组
  const named = new Set(ADV.flatMap((t) => (t.groups || []).flatMap((g) => g.keys || [])).filter((k) => !k.endsWith(".*")));
  const expand = (k) => (k.endsWith(".*") ? Object.keys(ix).filter((x) => x.startsWith(k.slice(0, -1)) && !named.has(x)) : ix[k] ? [k] : []);
  const out = {};
  for (const tab of ADV) {
    if (!tab.groups) continue;
    out[tab.id] = tab.groups.map((g) => {
      const keys = g.cat ? Object.keys(ix).filter((x) => x.startsWith("category.")) : g.keys.flatMap(expand);
      keys.forEach((k) => used.add(k));
      return { g: g.g, hint: g.hint, warn: g.warn ? d[g.warn] || [] : [], items: keys.map((k) => ix[k]) };
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
  const by = i.sec === "fast_loop" ? "最长持仓" : "档位";
  if (i.pinned) return `<span class="warn">已写死（${by}默认是 ${valOf(i, i.profile_default)}）</span>`;
  return i.profiled ? `跟${by}走` : "";
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
  if (i.multi) input = `<span id="dlg-v" class="multi">${i.choices.map((c) => `<label class="small"><input type="checkbox" value="${esc(c)}" ${(i.value || []).includes(c) ? "checked" : ""}> ${esc(choiceLabel(i.key, c))}</label>`).join("")}</span>`;
  else if (typeof i.value === "boolean") input = `<select id="dlg-v"><option value="true" ${i.value ? "selected" : ""}>开</option><option value="false" ${i.value ? "" : "selected"}>关</option></select>`;
  else if (i.choices && i.choices.length) {
    const fit = (c) => (i.key === "atr_period_min" && ATR_FIT[c] ? ` · 适合最长持仓 ${ATR_FIT[c][0]}` : "");
    input = `<select id="dlg-v">${i.choices.map((c) => `<option value="${esc(JSON.stringify(c))}" ${c === i.value ? "selected" : ""}>${esc(choiceLabel(i.key, c) + fit(c))}</option>`).join("")}</select>`;
  } else if (i.pattern) input = `<input id="dlg-v" type="text" value="${esc(i.pinned || !i.profiled ? i.value || "" : "")}" placeholder="${esc(i.key === "schedule_at" ? "如 08:00；留空 = " + (i.profiled ? `跟档位（${fmtVal(i.pinned ? i.profile_default : i.value, i)}）` : "不定点") : "")}">`;
  else if (i.key === "time_limit_s") input = `<input id="dlg-v" type="number" step="0.25" min="0.25" inputmode="decimal" value="${esc(useRec ? 2 : i.value / 3600)}"> 小时`;
  else {
    // 档位给默认的键：没写死时留空（= 跟档位走），写死了才填文件里的值
    const shown = useRec ? i.recommended : i.value == null || (i.profiled && !i.pinned) ? "" : i.value;
    const ph = i.profiled ? `留空 = 跟${i.sec === "fast_loop" ? "最长持仓" : "档位"}（${fmtVal(i.pinned ? i.profile_default : i.value, i)}）` : i.nullable ? "留空 = 用默认" : "";
    input = `<input id="dlg-v" type="number" step="any" inputmode="decimal" value="${esc(shown)}" placeholder="${esc(ph)}">`;
  }
  const now = [`现在 ${valOf(i)}`, i.recommended != null ? `推荐 ${valOf(i, i.recommended)}（${esc(i.recommended_why)}）` : "", i.pinned ? `${i.sec === "fast_loop" ? "最长持仓" : "档位"}默认 ${valOf(i, i.profile_default)}` : i.profiled ? `现在跟${i.sec === "fast_loop" ? "最长持仓" : "档位"}走` : "", i.bounds ? `范围 ${esc(i.bounds)}` : ""].filter(Boolean).join("，");
  ask(esc(i.cat ? i.label : nameOf(i)), `<p class="dim">${now}。</p>${i.multi ? `<div class="pw">勾选${input}</div>` : `<label class="pw">改成${input}</label>`}${pwField(data.control)}
    ${i.key === "atr_period_min" || i.key === "time_limit_s" ? '<div class="small warn" id="dlg-fit"></div>' : ""}
    <div class="dim small">保存前整份检查，不合理会说原因；写前留备份。改完点「重启生效」才用上。</div>`, "保存", false, async () => {
    const el = $("#dlg-v");
    let v;
    if (i.multi) {
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
      if (i.key === "time_limit_s") v = Math.round(v * 3600);
    }
    // 档位键填了数就算写死（哪怕和档位一样），其余和原值一样算没改
    if (!i.multi && v === i.value && !(i.profiled && !i.pinned && v !== null)) throw new Error("没有改动");
    const pw = $("#dlg-pw");
    const res = await post("/api/settings", { section: i.sec, changes: { [i.key]: v }, password: pw ? pw.value : "" });
    onOk = null;
    $("#layer [data-ok]").remove();
    $("#layer [data-close].btn").textContent = "关闭";
    const x = res.changed[0];
    dlgMsg(`<b class="up">已保存</b>${x ? `：${valOf(i, x.old)} → ${valOf(i, x.new)}` : ""}。点「重启生效」后用上。`);
    poll();
  });
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
  let pfRow = `<div class="st-row"><div class="k">开会间隔<small>这次启动的面板没拿到配置文件路径，看不到档位</small></div></div>`;
  if (pf) {
    const paperOnly = (p) => pf.paper_only.includes(p);
    const btns = pf.choices.map((p) => {
      const off = !d.writable || (paperOnly(p) && d.mode !== "paper");
      const tip = paperOnly(p) && d.mode !== "paper" ? "只在模拟盘可用" : profileNote(p);
      return `<button class="${p === pf.current ? "on" : ""}" ${off || p === pf.current ? "disabled" : clickable("profile", p)} title="${esc(tip)}">${esc(profileSpan(p))}</button>`;
    }).join("");
    const pins = pinnedList(pf);
    pfRow = `<div class="st-row"><div class="k">开会间隔<small>${esc(profileNote(pf.current))}；作战卡有效期 = 间隔 + 20 分钟</small></div><span class="seg">${btns}</span>
      ${pins.length ? `<div class="st-pins" title="${esc(pins.join("、"))}"><span class="warn">${pins.length} 项写死在配置文件里，不跟默认值变。</span>${d.writable ? ` <button class="btn b-sm" ${clickable("clearpins")}>看是哪几项、清掉</button>` : ` ${esc(pins.join("、"))}`}</div>` : ""}</div>`;
  }
  return `<section class="card st"><div class="ch"><h2>运行</h2></div>
    <div class="st-row"><div class="k">交易模式<small>切回模拟盘随时可以；测试网要面板密码；实盘只能在 Mac 本机切，要逐条确认提示、确认词和面板密码。切完自动重启</small></div><span class="seg">${["paper", "testnet", "live"].map((m) => `<button class="${m === d.mode ? "on" : ""}" ${m === d.mode && (m !== "live" || d.live_armed) || !d.writable ? "disabled" : clickable("mode", m)}>${MODE[m]}</button>`).join("")}</span><span class="pill ${d.live_armed ? "p-short" : ""}">${d.live_armed ? "实盘已解锁" : "实盘未解锁"}</span></div>
    <div class="st-row"><div class="k">主程序<small>${up || "还没有启动记录"}</small></div><span class="lamp ${data.running ? "" : "bad"}"><i></i>${data.running ? "运行中" : "没在运行"}</span>${restartBtn()}</div>
    <div class="st-row"><div class="k">开新仓<small>暂停后只管已有持仓，不开新的</small></div><span class="pill ${c.paused ? "p-short" : ""}">${c.paused ? "已暂停" : "正常开仓"}</span>${c.paused ? `<button class="btn b-sm" ${clickable("cmd", "resume")}>恢复开仓</button>` : `<button class="btn b-sm" ${clickable("cmd", "pause")}>暂停开新仓</button>`}</div>
    ${pfRow}${holdRow(d)}</section>`;
}

// 单笔最长持仓：默认跟开会档位的推荐搭配，可改成固定一档（自定义）；止损 K 线周期另外点开改
function holdRow(d) {
  const ix = settingIndex(d);
  const pf = d.profile;
  const tl = ix["fast_loop.time_limit_s"];
  const atr = ix["fast_loop.atr_period_min"];
  if (!pf || !tl || !atr) return "";
  const rec = pf.hold_recommended[pf.current];
  const btn = (arg, label, on, off, tip) => `<button class="${on ? "on" : ""}" ${off || on ? "disabled" : clickable("hold", arg)} title="${esc(tip)}">${esc(label)}</button>`;
  const btns = [btn("auto", `跟开会档位（${profileSpan(rec || pf.hold)}）`, !pf.hold_custom, !d.writable, `每 ${profileSpan(pf.current)}开会推荐 ${profileSpan(rec || pf.hold)}`)]
    .concat(pf.hold_choices.map((h) => {
      const paper = pf.hold_paper_only.includes(h) && d.mode !== "paper";
      return btn(h, profileSpan(h), pf.hold_custom && pf.hold === h, !d.writable || paper, paper ? "只在模拟盘可用" : `止损按 ${(pf.hold_specs[h] || {}).atr_period_min} 分钟 K 线 ATR`);
    })).join("");
  const tips = [
    pf.hold_custom && rec && rec !== pf.hold ? `每 ${profileSpan(pf.current)}开会推荐最长持仓 ${profileSpan(rec)}，现在是自定义 ${profileSpan(pf.hold)}。只提醒，不拦。` : "",
    atrMismatch(atr.value, tl.value),
  ].filter(Boolean);
  return `<div class="st-row"><div class="k">单笔最长持仓 ${humanDur(tl.value)}${pf.hold_custom ? "（自定义）" : "（跟开会档位）"} · 止损按 ${atr.value} 分钟 K 线 ATR
      <small>到时间还没走就平；止损、止盈、拍板门槛跟着最长持仓走。30 分钟只在模拟盘用</small>${tips.map((t) => `<small class="warn">${esc(t)}</small>`).join("")}</div>
    <span class="seg">${btns}</span>
    ${d.writable ? `<button class="btn b-sm" data-act="edit1" data-arg="fast_loop.atr_period_min">改 K 线周期</button>` : ""}</div>`;
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
    : `<div class="st-note">${esc(t.note)}</div>` + (tab === "slow" && d.schedule_caution ? `<div class="note warn">${esc(d.schedule_caution)}（只提示，不拦）</div>` : "") + groups[tab].filter((g) => g.items.length).map((g) => `<div class="st-sub">${esc(g.g)}</div>${g.hint ? `<div class="st-note">${esc(g.hint)}</div>` : ""}${(g.warn || []).map((c) => `<div class="note warn">${esc(c)}（只提示，不拦）</div>`).join("")}${g.items.map((i) => itemRow(i)).join("")}`).join("");
  return `<section class="card st"><div class="ch"><h2>高级</h2><span class="r">平时不用动</span></div><div class="st-tabs" role="tablist">${tabs}</div>${body}</section>`;
}

// 「你的设置实际意味着」：后端按硬上限、最长持仓档、本金算好的几行，只提示
function meaningCard(d) {
  const m = d.meaning;
  if (!m || !m.lines.length) return "";
  return `<section class="card st"><div class="ch"><h2>你的设置实际意味着</h2><span class="r">只提示，不拦</span></div>
    ${m.lines.map((x) => `<div class="st-row"><div class="k">${esc(x.label)}<small>${x.warn ? `<span class="warn">${esc(x.text)}</span>` : esc(x.text)}</small></div></div>`).join("")}</section>`;
}

P.settings = () => {
  const d = pageData;
  const ix = settingIndex(d);
  const note = d.file_error ? `<span class="down">配置文件现在读不出：${esc(d.file_error)}。先在终端修好再改。</span>` : d.writable ? "改任何一项都要面板密码；模式、数据目录、拍板人通道和模型版本不在网页上改。" : "这次启动的面板只能看：没拿到配置文件路径。";
  return `<div class="st-col">${pendingBanner(d)}${runCard(d)}${riskCard(d, ix)}${meaningCard(d)}${advCard(d, ix)}
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
  funds: () => `/api/funds?ai=${S.ai || "today"}`,
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
  // 页面里能自己滚的块（竖着的日志、横着的宽表格）：重画前记住位置，画完按顺序放回去
  const boxes = () => [...document.querySelectorAll("#page .scroll, #page .tw")];
  const scrolls = boxes().map((e) => [e.scrollTop, e.scrollLeft]);
  // 页面上的输入框（选币页“手动加币”）：定时刷新重画时保住正在输入的内容和焦点
  const typing = document.activeElement && document.activeElement.id === "pool-add";
  const typed = $("#pool-add") ? $("#pool-add").value : "";
  // 定时刷新整页重画：先用旧高度撑住页面，免得重画中途变矮把滚动位置夹回顶部（团队页尤其明显）
  const pg = $("#page"), y = window.scrollY, same = pg.dataset.key === pageId(page, arg), samePage = pg.dataset.page === page;
  pg.style.minHeight = same ? pg.offsetHeight + "px" : "";
  pg.dataset.key = pageId(page, arg);
  pg.dataset.page = page;
  pg.innerHTML = (P[page] || placeholder)(arg);
  if (samePage) boxes().forEach((e, i) => { if (scrolls[i]) [e.scrollTop, e.scrollLeft] = scrolls[i]; });
  const box = $("#pool-add");
  if (box && typed) box.value = typed;
  if (box && typing) box.focus();
  if (page === "positions") mountChart();
  if (page === "history") mountHistoryChart();
  if (page === "team") fitTeamFlow();
  if (same) {
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
  loadPrecision();
  setInterval(poll, 5000);
}
