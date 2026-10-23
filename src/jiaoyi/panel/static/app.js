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
let data = null; // /api/overview
let pageData = null; // 当前页自己的接口
let pageKey = "";
let lastOk = 0;
const S = { tf: "1m" };

const hm = (ts) => new Intl.DateTimeFormat("zh-CN", { timeZone: TZ, hour: "2-digit", minute: "2-digit", hour12: false }).format(new Date(ts));
const hms = (ts) => new Intl.DateTimeFormat("zh-CN", { timeZone: TZ, hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(new Date(ts));
const money = (x, d = 1) => (x == null ? "—" : x.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d }));
const signed = (x, d = 1) => (x == null ? "—" : (x < 0 ? "−" : "+") + money(Math.abs(x), d));
const cls = (x) => (x == null || x === 0 ? "" : x > 0 ? "up" : "down");
const coin = (s) => esc(String(s || "").replace(/USDT$/, ""));
const price = (p) => (p == null ? "—" : p >= 100 ? money(p, 1) : p >= 1 ? money(p, 3) : String(+p.toPrecision(4)));
const range = (r, unit = "", d) => (r ? `${money(r[0], d ?? (r[0] < 10 ? 1 : 0))}–${money(r[1], d ?? (r[1] < 10 ? 1 : 0))}${unit}` : "—");
const dur = (ms) => { const m = Math.max(0, Math.round(ms / 60000)); return m < 60 ? `${m} 分` : `${Math.floor(m / 60)} 时 ${m % 60} 分`; };
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
  // 暂停 / 全部平仓要主循环配合，接上指令通道前先灰掉，免得以为按了有用
  $("#pghead").innerHTML = `<h1>${pg[1]}</h1><span class="sub">${esc(mode)} · 曼谷 ${now} · ${pg[2]}</span>
    <div class="acts"><button class="btn b-pause" disabled title="下一版接入">暂停开新仓</button><button class="btn b-kill" disabled title="下一版接入">全部平仓</button></div>`;

  let b = "";
  if (data && data.mode !== "live") {
    b += `<div class="banner bn-paper"><span class="tag">${esc(mode)}</span><span>不会动真钱，资金来自币安 Demo Trading（经 hummingbot-api）</span></div>`;
  }
  if (data && !data.running) {
    const last = data.last_heartbeat ? `最后心跳 ${hm(data.last_heartbeat)}` : "还没有心跳记录";
    b += `<div class="banner bn-bad"><b>主程序没在运行</b><span>${last}。下面是落盘的最后状态；在 Mac mini 上运行 <code>jiaoyi run</code>。</span></div>`;
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

/* ---------- 弹窗 */
function dlg(html) {
  $("#layer").innerHTML = `<div class="scrim" data-close="1"><div class="dlg" role="dialog" aria-modal="true">${html}<div class="acts"><button class="btn" data-close="1">关闭</button></div></div></div>`;
  $("#layer .acts .btn").focus();
}
function closeDlg() { $("#layer").innerHTML = ""; }
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDlg(); });
document.addEventListener("click", (e) => {
  if (e.target.dataset && e.target.dataset.close) return closeDlg();
  const el = e.target.closest("[data-act]");
  if (!el) return;
  const { act, arg } = el.dataset;
  if (act === "card") showCard();
  else if (act === "signal") showSignal(arg);
  else if (act === "record") showRecord(+arg);
  else if (act === "tf") { S.tf = arg; loadKlines(true); render(); }
});
document.addEventListener("keydown", (e) => {
  if ((e.key === "Enter" || e.key === " ") && e.target.dataset && e.target.dataset.act) { e.preventDefault(); e.target.click(); }
});
const clickable = (act, arg = "") => `data-act="${act}" data-arg="${esc(arg)}" tabindex="0" role="button"`;
const json = (o) => (o && Object.keys(o).length ? `<pre class="json">${esc(JSON.stringify(o, null, 2))}</pre>` : `<span class="dim">（空）</span>`);

function showCard() {
  const c = data && data.card;
  if (!c) return;
  const rows = c.symbols.map((s) => {
    const [t, k] = BIAS[s.bias] || [s.bias, ""];
    return `<tr><td class="hi">${coin(s.symbol)}<div class="dim small">${esc(CAT[s.category] || s.category)}</div></td><td><span class="pill ${k}">${t}</span></td>
      <td>${range(s.stop_atr, "", 1)}</td><td>${range(s.take_profit_r, "", 1)}</td><td>${range(s.leverage, "", 0)}</td><td>${range(s.notional_usdt, "", 0)}</td><td>${esc(s.size_tier_max || "—")}</td>
      <td class="wrap">${esc(s.reason)}${s.allowed_signals.length && s.allowed_signals.length < 5 ? `<div class="dim small">只做：${s.allowed_signals.map((x) => esc(SIGNAL[x] || x)).join("、")}</div>` : ""}${s.key_levels.length ? `<div class="dim small">关键位：${s.key_levels.map(price).join(" / ")}</div>` : ""}</td></tr>`;
  }).join("");
  dlg(`<h3>作战卡${c.version ? " v" + c.version : ""} · ${c.symbols.length} 个标的</h3>
    <p class="dim">${hm(c.created_at)} 出卡 · ${c.expires_at ? "有效到 " + hm(c.expires_at) : "有效期未知"}${c.valid ? "" : " · <span class='down'>已过期</span>"}</p>
    ${c.regime ? `<div class="note"><b>军师纪要</b>　${esc(c.regime)}</div>` : ""}
    <div class="tw"><table><thead><tr><th>币种</th><th>方向</th><th>止损 ATR</th><th>止盈 R</th><th>杠杆</th><th>单笔 U</th><th>最大档</th><th>理由</th></tr></thead><tbody>${rows}</tbody></table></div>`);
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
  const shown = c.symbols.slice(0, 8);
  const more = c.symbols.length - shown.length;
  const pills = shown.map((s) => { const [t, k] = BIAS[s.bias] || [s.bias, ""]; return `<span class="pill ${k}">${coin(s.symbol)} ${t}</span>`; }).join("");
  const until = c.expires_at ? `有效到 ${hm(c.expires_at)}` : "有效期未知";
  return `<section class="card click" ${clickable("card")}>
    <div class="ch"><h2>作战卡</h2><span class="pill">军师团${c.version ? " v" + c.version : ""}</span>
      <span class="r">${hm(c.created_at)} 更新 · ${c.valid ? until : '<span class="down">已过期，不开新仓</span>'}</span></div>
    <div class="row">${pills}${more > 0 ? `<span class="dim small">+${more} 个</span>` : ""}</div>
    <dl class="kv"><dt>止损</dt><dd>${range(c.stop_atr, " ATR")}</dd><dt>杠杆</dt><dd>${c.leverage_max == null ? "—" : "≤ " + c.leverage_max + " 倍"}</dd><dt>单笔</dt><dd>${range(c.notional_usdt, " U")}</dd></dl>
    ${c.regime ? `<div class="note"><b>军师纪要</b>　${esc(c.regime)}</div>` : ""}
    <div class="foot">点开看全部 ${c.symbols.length} 个标的的方向、止损、杠杆和理由 ›</div>
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
        return `<a class="pos click" href="#positions/${encodeURIComponent(p.position_id)}"><div class="top"><b>${coin(p.symbol)} <span class="${sc}">${s}</span> ${p.quantity}</b>
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
    ? `<div class="log">${entries.map(([s, r]) => `<div><span class="pill p-short">${coin(s)}</span><span>${esc(r)}</span></div>`).join("")}</div>`
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
    return `<tr class="click ${on ? "sel" : ""}" onclick="location.hash='positions/${encodeURIComponent(p.position_id)}'">
      <td class="hi">${coin(p.symbol)} <span class="${sc}">${s}</span> ${p.quantity} <span class="dim">${p.leverage}x</span></td>
      <td class="num">${price(p.entry_price)}</td><td class="r num ${cls(p.unrealized_usdt)}">${p.unrealized_usdt == null ? "—" : signed(p.unrealized_usdt, 2) + "U"}</td>
      <td class="num">${price(p.stop_loss_price)} / ${price(p.take_profit_price)}</td>
      <td class="${p.lifeboat_status === "active" ? "" : "warn"}">${LIFEBOAT[p.lifeboat_status] || "⚠ 没有保命单"} ${price(p.lifeboat_price)}</td>
      <td class="num">${dur(held)} / ${dur(p.time_limit_s * 1000)}</td><td>${esc(p.keeper || "—")}</td>
      <td class="r"><button class="btn b-sm b-danger" disabled title="下一版接入">平仓</button></td></tr>`;
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
      <div class="ch"><h2>${coin(sel.symbol)} <span class="${sc}">${s}</span> ${sel.quantity} · 详情</h2>
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
      timeScale: { timeVisible: true, secondsVisible: false, borderVisible: false },
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
    <section class="card mt"><div class="ch"><h2>${esc(sel.name)} · 今天做了什么</h2>
      <span class="r">${sel.last_beat ? "最后心跳 " + hms(sel.last_beat) : "没有心跳"}${sel.avg_latency_ms ? ` · 平均 ${Math.round(sel.avg_latency_ms)} ms` : ""}</span></div>
      ${sel.last_error ? `<div class="note warn">最近出错：${esc(sel.last_error)}</div>` : ""}
      <div class="log trail scroll tall">${recs || '<div class="dim">今天还没有记录。</div>'}</div>
      <div class="foot">最多显示最新 200 条 · 点一条看它看到了什么、怎么判断的</div></section>`;
};

function placeholder() {
  return `<div class="card"><div class="empty"><b>这一页还在做</b>按总览的样式接着做。<a href="#overview">回总览 ›</a></div></div>`;
}

/* ---------- 渲染和拉数 */
const PAGE_API = { positions: () => "/api/positions", team: (arg) => `/api/team${arg ? "?role=" + encodeURIComponent(arg) : ""}` };

function render() {
  chrome();
  $("#status").innerHTML = statusBar();
  const { page, arg } = route();
  const needs = PAGE_API[page];
  if (!data || (needs && (!pageData || pageKey !== page + "/" + (arg || "")))) {
    $("#page").innerHTML = `<div class="card"><div class="empty"><b>正在读取…</b></div></div>`;
    return;
  }
  const scrolls = [...document.querySelectorAll("#page .scroll")].map((e) => e.scrollTop);
  $("#page").innerHTML = (P[page] || placeholder)(arg);
  document.querySelectorAll("#page .scroll").forEach((e, i) => { e.scrollTop = scrolls[i] || 0; });
  if (page === "positions") mountChart();
}

async function getJson(url) {
  const r = await fetch(url, { cache: "no-store" });
  if (!r.ok) throw new Error(String(r.status));
  return r.json();
}

async function poll() {
  const { page, arg } = route();
  try {
    data = await getJson("/api/overview");
    TZ = data.display_tz || TZ;
    const api = PAGE_API[page];
    if (api) {
      pageData = await getJson(api(arg));
      pageKey = page + "/" + (arg || "");
    }
    lastOk = Date.now();
    $("#stale").innerHTML = "";
    render();
    loadKlines(false);
  } catch (e) {
    const ago = lastOk ? Math.round((Date.now() - lastOk) / 1000) + " 秒前" : "从未";
    $("#stale").innerHTML = `<div class="stale">面板服务连不上（上次成功：${ago}）</div>`;
  }
}

window.addEventListener("hashchange", () => { closeDlg(); render(); poll(); });
render();
poll();
setInterval(poll, 5000);
