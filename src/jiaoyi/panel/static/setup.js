/* 首次设置向导（/setup）。只在 Mac 本机（127.0.0.1 / localhost）能填；密钥只写不读。
   面板密码只留在这个页面的内存里，不进浏览器存储。 */
const SU = { st: null, pw: "", checks: null, msg: {} };
const BASIC = {
  profile: ["持仓档位", "单笔最长持仓时间。30m / 15m 只能跑模拟盘"],
  jev_provider: ["拍板人走哪个通道", "openrouter = 和军师团共用一把 OpenRouter key"],
  llm_provider: ["军师团走哪个通道", "openrouter = 用 OPENROUTER_API_KEY；deepseek = 直连 DeepSeek"],
  daily_cost_limit_usd: ["军师团每天花费上限（美元）", "到了就停开会，第二天恢复"],
};
const BASIC_CHOICE = { "2h": "2 小时（默认）", "60m": "1 小时", "30m": "30 分钟（只能模拟盘）", "15m": "15 分钟（只能模拟盘）", typesafe: "typesafe（Jev 官方）", openrouter: "OpenRouter", deepseek: "DeepSeek 直连" };
const KEY_LABEL = {
  OPENROUTER_API_KEY: "OpenRouter（拍板 + 军师团）",
  BINANCE_TESTNET_API_KEY: "币安 Demo · Key",
  BINANCE_TESTNET_API_SECRET: "币安 Demo · Secret",
  TELEGRAM_BOT_TOKEN: "Telegram 机器人 token",
  TELEGRAM_CHAT_ID: "Telegram chat id",
  HUMMINGBOT_API_PASSWORD: "hummingbot-api 密码",
  BINANCE_API_KEY: "币安主网 · Key",
  BINANCE_API_SECRET: "币安主网 · Secret",
  JEV_API_KEY: "Jev（拍板人直连）",
  DEEPSEEK_API_KEY: "DeepSeek（军师团直连）",
  FRED_API_KEY: "FRED 宏观数据",
};
const KEY_HELP = {
  OPENROUTER_API_KEY: "sk-or-v1-…，openrouter.ai/keys",
  BINANCE_TESTNET_API_KEY: "币安 Demo Trading 的 API Key（模拟盘）",
  BINANCE_TESTNET_API_SECRET: "同上，Secret",
  TELEGRAM_BOT_TOKEN: "@BotFather 给的 123456:ABC…",
  TELEGRAM_CHAT_ID: "一串数字；群组以 - 开头",
  HUMMINGBOT_API_PASSWORD: "hummingbot-api 的登录密码",
  BINANCE_API_KEY: "币安主网（M4 小额实盘前再填）",
  BINANCE_API_SECRET: "同上，Secret",
  JEV_API_KEY: "只在拍板人走 typesafe 时要",
  DEEPSEEK_API_KEY: "只在军师团直连 DeepSeek 时要",
  FRED_API_KEY: "宏观数据，可不填",
};

function suMsg(step) {
  const m = SU.msg[step];
  return m ? `<div class="msg small ${m[1]}">${m[0]}</div>` : "";
}
const tick2 = (ok) => (ok ? `<span class="pill p-long">已完成</span>` : `<span class="pill">待做</span>`);

function suStep1(st) {
  const body = st.password_set
    ? `<p class="dim">已设置。要改请在终端运行 <code>jiaoyi set-panel-password</code>（要旧密码）。</p>`
    : `<p>以后恢复开新仓、改配置、切实盘都要它。至少 8 位，连错 5 次锁 15 分钟。</p>
      <div class="su-row"><label class="pw">面板密码<input type="password" id="su-pw1" autocomplete="new-password"></label>
      <label class="pw">再输一次<input type="password" id="su-pw2" autocomplete="new-password"></label>
      <button class="btn b-go" data-su="password">设置</button></div>`;
  return `<section class="card"><div class="ch"><h2>1. 面板密码</h2><div class="r">${tick2(st.password_set)}</div></div>${body}${suMsg(1)}</section>`;
}

function suUnlock(st) {
  if (!st.password_set) return "";
  if (SU.pw) return `<div class="note">已输入面板密码（只留在这个页面里）。<a href="#" data-su="lock">清掉</a></div>`;
  return `<section class="card"><div class="su-row"><label class="pw">输入面板密码，才能继续下面几步<input type="password" id="su-unlock" autocomplete="current-password"></label><button class="btn b-go" data-su="unlock">继续</button></div>${suMsg("u")}</section>`;
}

function keyRow(name, st) {
  const set = st.keys[name];
  return `<div class="fr"><label>${esc(KEY_LABEL[name] || KEY_NOTE[name] || name)}<span class="hint">${esc(name)} · ${esc(KEY_HELP[name] || "")}</span></label>
    <b>${set ? `<span class="pill p-long">已配</span>` : `<span class="pill p-warn">未配</span>`}
    <input type="password" class="su-key" data-key="${esc(name)}" autocomplete="off" spellcheck="false" placeholder="${set ? "留空 = 不改" : "粘贴到这里"}"></b></div>`;
}

function suStep2(st) {
  const ready = st.required.every((k) => st.keys[k]);
  const off = !st.password_set || !SU.pw;
  let body;
  if (!st.keychain) {
    body = `<div class="note warn">这台机器写不了 macOS 钥匙串。请在终端运行 <code>jiaoyi setup-keys</code>。</div>`;
  } else {
    body = `<p class="dim">写进这台 Mac 的钥匙串。网页只显示“配没配”，不会再显示内容；要换就重新粘贴一次。</p>
      <div class="form edit su-keys">${st.required.map((k) => keyRow(k, st)).join("")}</div>
      <details><summary>可选的密钥（${st.optional.filter((k) => st.keys[k]).length}/${st.optional.length} 已配）</summary>
      <div class="form edit su-keys">${st.optional.map((k) => keyRow(k, st)).join("")}</div></details>
      <div class="su-row"><button class="btn b-go" data-su="keys" ${off ? "disabled" : ""}>保存密钥</button>${off ? `<span class="dim small">先完成第 1 步并输入面板密码</span>` : ""}</div>`;
  }
  return `<section class="card"><div class="ch"><h2>2. 密钥</h2><div class="r">${tick2(ready)}</div></div>${body}${suMsg(2)}</section>`;
}

function suStep3(st) {
  if (!st.basic || !Object.keys(st.basic).length) {
    return `<section class="card"><div class="ch"><h2>3. 基本配置</h2></div><div class="note warn">读不了配置文件，先运行 <code>jiaoyi check-config</code> 看哪里不对。</div>${suMsg(3)}</section>`;
  }
  const off = !SU.pw;
  const rows = Object.entries(BASIC).map(([k, [lab, hint]]) => {
    const v = st.basic[k];
    const ch = st.basic_choices[k] || [];
    const input = ch.length
      ? `<select data-basic="${k}">${ch.map((c) => `<option value="${esc(c)}" ${c === v ? "selected" : ""}>${esc(BASIC_CHOICE[c] || c)}</option>`).join("")}</select>`
      : `<input type="number" step="any" data-basic="${k}" value="${esc(v)}">`;
    return `<div class="fr"><label>${esc(lab)}<span class="hint">${esc(hint)}</span></label><b>${input}</b></div>`;
  }).join("");
  return `<section class="card"><div class="ch"><h2>3. 基本配置</h2><div class="r">运行方式：${esc(MODE[st.mode] || st.mode)}（实盘走 <code>jiaoyi go-live</code>）</div></div>
    <div class="form edit">${rows}</div>
    <div class="su-row"><button class="btn b-go" data-su="basic" ${off ? "disabled" : ""}>保存配置</button><span class="dim small">写进 config/*.toml，主程序重启后生效。其余设置在面板“设置”页改。</span></div>${suMsg(3)}</section>`;
}

function suStep4() {
  const off = !SU.pw;
  const list = SU.checks
    ? `<div class="form">${SU.checks.map((c) => `<div class="fr"><label>${esc(c.name)}</label><b class="${c.ok ? "up" : "down"}">${c.ok ? "✓" : "✗"} ${esc(c.detail)}</b></div>`).join("")}</div>`
    : "";
  return `<section class="card"><div class="ch"><h2>4. 自检</h2><div class="r">${SU.checks ? tick2(SU.checks.every((c) => c.ok)) : ""}</div></div>
    <p class="dim">检查配置文件、连 hummingbot-api、给 Telegram 发一条测试消息。</p>${list}
    <div class="su-row"><button class="btn b-go" data-su="check" ${off ? "disabled" : ""}>开始自检</button></div>${suMsg(4)}</section>`;
}

function renderSetup() {
  const st = SU.st;
  $("#tabs").innerHTML = `<a href="/" class=""><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">${ICON.overview}</svg>回面板</a>`;
  $("#sidefoot").textContent = "首次设置 · 只在本机可用";
  $("#pghead").innerHTML = `<h1>首次设置</h1><span class="sub">按顺序走完 4 步，以后换机器也照这个来</span>`;
  if (!st) { $("#page").innerHTML = `<div class="card"><div class="empty"><b>正在读取…</b></div></div>`; return; }
  if (!st.local) {
    const url = `http://127.0.0.1:${location.port || 8090}/setup`;
    $("#page").innerHTML = `<section class="card su-narrow"><div class="empty"><b>请在 Mac 本机打开</b>
      <span>密钥只能在运行面板的那台 Mac 上填：在 Mac 上用浏览器打开 <code>${esc(url)}</code></span><a href="/">回面板 ›</a></div></section>`;
    return;
  }
  const done = st.done ? `<div class="note">必填项都配齐了。<a href="/">去总览 ›</a>　启动主程序：<code>jiaoyi run</code>（或 launchd）</div>` : "";
  $("#page").innerHTML = `<div class="su-narrow grid">${done}${suStep1(st)}${suUnlock(st)}${suStep2(st)}${suStep3(st)}${suStep4()}</div>`;
}

async function loadSetup() {
  try { SU.st = await getJson("/api/setup"); } catch (e) { $("#stale").innerHTML = `<div class="stale">面板服务连不上</div>`; }
  renderSetup();
}

async function suRun(step, fn) {
  SU.msg[step] = ["处理中…", ""];
  renderSetup();
  try {
    SU.msg[step] = [await fn(), "up"];
  } catch (err) {
    SU.msg[step] = [esc(err.message), "down"];
  }
  renderSetup();
}

async function suAction(act) {
  if (act === "password") {
    const a = $("#su-pw1").value, b = $("#su-pw2").value;
    await suRun(1, async () => { SU.st = await post("/api/setup/password", { password: a, confirm: b }); SU.pw = a; return "面板密码已设置"; });
  } else if (act === "unlock") {
    SU.pw = $("#su-unlock").value;
    SU.msg.u = null;
    renderSetup();
  } else if (act === "lock") {
    SU.pw = "";
    renderSetup();
  } else if (act === "keys") {
    const values = {};
    document.querySelectorAll(".su-key").forEach((i) => { if (i.value.trim()) values[i.dataset.key] = i.value; });
    await suRun(2, async () => {
      const r = await post("/api/setup/keys", { values, password: SU.pw });
      SU.st = r.state;
      return `已写入：${r.written.map(esc).join("、")}`;
    });
  } else if (act === "basic") {
    const values = {};
    document.querySelectorAll("[data-basic]").forEach((i) => {
      const k = i.dataset.basic;
      const v = i.tagName === "SELECT" ? i.value : Number(i.value);
      if (v !== SU.st.basic[k]) values[k] = v;
    });
    if (!Object.keys(values).length) { SU.msg[3] = ["没有改动", ""]; renderSetup(); return; }
    await suRun(3, async () => {
      const r = await post("/api/setup/basic", { values, password: SU.pw });
      SU.st = r.state;
      return r.changed.length ? `已保存 ${r.changed.length} 项，主程序重启后生效` : "没有改动";
    });
  } else if (act === "check") {
    await suRun(4, async () => { SU.checks = await post("/api/setup/check", { password: SU.pw }); return ""; });
  }
}

if (location.pathname === "/setup") {
  document.addEventListener("click", (e) => {
    const el = e.target.closest("[data-su]");
    if (!el || el.disabled) return;
    e.preventDefault();
    suAction(el.dataset.su);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" || e.target.tagName !== "INPUT") return;
    const btn = e.target.closest(".su-row")?.querySelector("[data-su]");
    if (btn) { e.preventDefault(); btn.click(); }
  });
  renderSetup();
  loadSetup();
}
