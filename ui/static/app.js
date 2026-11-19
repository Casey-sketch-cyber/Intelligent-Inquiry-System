"use strict";
/* 天芯智能询价系统 · 界面逻辑（原生 JS，无外部依赖） */

const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const ICON = {
  home: '<path d="M3 10.5 12 3l9 7.5"/><path d="M5 9.5V21h14V9.5"/><path d="M9.5 21v-6h5v6"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  list: '<path d="M9 6h11M9 12h11M9 18h11"/><circle cx="4.5" cy="6" r="1.2"/><circle cx="4.5" cy="12" r="1.2"/><circle cx="4.5" cy="18" r="1.2"/>',
  terminal: '<rect x="3" y="4" width="18" height="16" rx="2.5"/><path d="m7 9 3 3-3 3M12.5 15H17"/>',
  file: '<path d="M14 3H6.5A2.5 2.5 0 0 0 4 5.5v13A2.5 2.5 0 0 0 6.5 21h11a2.5 2.5 0 0 0 2.5-2.5V9z"/><path d="M14 3v6h6M8 13h8M8 17h5"/>',
  settings: '<path d="M4 6h9M17 6h3M4 12h3M11 12h9M4 18h11M19 18h1"/><circle cx="15" cy="6" r="2"/><circle cx="9" cy="12" r="2"/><circle cx="17" cy="18" r="2"/>',
  chat: '<path d="M20.5 12a8.5 8.5 0 0 1-12.4 7.5L3.5 21l1.4-4.4A8.5 8.5 0 1 1 20.5 12Z"/><path d="M8.5 12h.01M12 12h.01M15.5 12h.01"/>',
  globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c2.5 2.7 3.8 5.7 3.8 9s-1.3 6.3-3.8 9c-2.5-2.7-3.8-5.7-3.8-9S9.5 5.7 12 3Z"/>',
  spark: '<path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z"/><path d="M19 15.5l.8 1.7 1.7.8-1.7.8-.8 1.7-.8-1.7-1.7-.8 1.7-.8z"/>',
  card: '<rect x="3" y="5" width="18" height="14" rx="2.5"/><circle cx="9" cy="11" r="2.2"/><path d="M5.8 16.5c.8-1.5 1.9-2.2 3.2-2.2s2.4.7 3.2 2.2M14.5 10h4M14.5 13.5h3"/>',
  play: '<path d="M8 5.5v13l10.5-6.5z"/>',
  stop: '<rect x="6.5" y="6.5" width="11" height="11" rx="2"/>',
  refresh: '<path d="M20 11a8 8 0 0 0-14.6-4.5M4 4v4h4M4 13a8 8 0 0 0 14.6 4.5M20 20v-4h-4"/>',
  download: '<path d="M12 4v11M7 10.5l5 5 5-5M5 20h14"/>',
  folder: '<path d="M3 7.5A2.5 2.5 0 0 1 5.5 5H9l2 2h7.5A2.5 2.5 0 0 1 21 9.5v7a2.5 2.5 0 0 1-2.5 2.5h-13A2.5 2.5 0 0 1 3 16.5z"/>',
  check: '<path d="M5 12.5 10 17l9-10"/>',
  x: '<path d="M6 6l12 12M18 6 6 18"/>',
  alert: '<path d="M12 3.5 2.5 20h19z"/><path d="M12 10v4.5M12 17.2v.01"/>',
  back: '<path d="M15 5.5 8.5 12l6.5 6.5"/>',
  trash: '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>',
  paste: '<rect x="6" y="4" width="12" height="17" rx="2.5"/><path d="M9.5 4h5v3h-5z"/><path d="M9 12h6M9 16h4"/>',
  upload: '<path d="M12 20V9M7 13.5l5-5 5 5M5 4h14"/>',
  arrow: '<path d="M9 5.5 15.5 12 9 18.5"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  link: '<path d="M10 14a4 4 0 0 0 5.7 0l3.3-3.3a4 4 0 0 0-5.7-5.7L12 6.3"/><path d="M14 10a4 4 0 0 0-5.7 0L5 13.3A4 4 0 0 0 10.7 19L12 17.7"/>',
};
const icon = (n, s = 18) => `<svg width="${s}" height="${s}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${ICON[n] || ""}</svg>`;

const NAV = [
  ["#/", "工作台", "home"],
  ["#/new", "新建询价", "plus"],
  ["#/tasks", "询价任务", "list"],
  ["#/reports", "报告文件", "file"],
  ["#/log", "运行日志", "terminal"],
  ["#/incidents", "运行问题", "alert"],
  ["#/settings", "设置", "settings"],
];

const S = { ov: null, settings: null, parts: [{ part: "", qty: "" }], dryRun: true, maxN: "3", submitting: false, submission: null, log: { file: null, offset: -1, auto: true }, showDry: false, dashKey: "" };

// ---------- 工具 ----------
async function api(path, opts = {}) {
  const o = { headers: {}, ...opts };
  if (o.json !== undefined) {
    o.method = o.method || "POST";
    o.headers["Content-Type"] = "application/json";
    o.body = JSON.stringify(o.json);
    delete o.json;
  }
  const r = await fetch(path, o);
  let data = null;
  try { data = await r.json(); } catch (_) { /* 非 JSON */ }
  if (!r.ok) throw new Error((data && data.error) || `请求失败（${r.status}）`);
  return data;
}

function toast(msg, kind = "ok") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.innerHTML = `<span style="color:${kind === "ok" ? "#22c55e" : "#ef4444"}">${icon(kind === "ok" ? "check" : "alert")}</span><div>${esc(msg)}</div>`;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), kind === "ok" ? 3200 : 6000);
}

const fmtTime = (ts) => (ts ? new Date(ts * 1000).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }) : "—");
const fmtQty = (q) => (q >= 1000 && q % 100 === 0 ? `${q / 1000}k` : String(q));
const money = (v) => (v == null ? "—" : `¥${Number(Number(v).toFixed(3))}`);
const parseQty = (s) => {
  const m = String(s ?? "").trim().toLowerCase().replace(/,/g, "").match(/^(\d+(?:\.\d+)?)\s*(k|千|w|万)?$/);
  if (!m) return NaN;
  const value = parseFloat(m[1]) * ({ k: 1000, 千: 1000, w: 10000, 万: 10000 }[m[2]] || 1);
  return Number.isSafeInteger(value) && value > 0 ? value : NaN;
};
const renderMsg = (tpl, part, qty) => (tpl || "").replace(/\{part\}/g, part).replace(/\{qtyk\}/g, fmtQty(qty)).replace(/\{qty\}/g, qty);

const TASK_CHIP = { running: "blue", done: "green", stopped: "amber", "dry-run": "", needs_review: "amber", failed: "red", queued:"blue", waiting_verification:"amber" };
const SUP_CHIP = { quoted: "green", sent: "blue", asking: "amber", failed: "red", no_stock: "", skipped: "", pending: "", waiting_desktop: "amber", identity_unverified: "amber", parse_pending: "amber", send_uncertain: "amber" };
const chip = (text, cls = "") => `<span class="chip ${cls}">${esc(text)}</span>`;
const taskChip = (t) => chip(t.status_cn, TASK_CHIP[t.status] ?? "");
const bar = (v, max) => `<div class="bar ${v >= max ? "done" : ""}"><i style="width:${Math.min(100, max ? (v / max) * 100 : 0)}%"></i></div>`;
const empty = (msg, ic = "list") => `<div class="empty">${icon(ic, 36)}<div>${esc(msg)}</div></div>`;

// ---------- 外框：导航、状态 ----------
function renderNav() {
  const h = location.hash || "#/";
  $("#nav").innerHTML = NAV.map(([href, label, ic]) => {
    const active = href === "#/" ? h === "#/" || h === "" : (h.startsWith(href) || (href === "#/tasks" && h.startsWith("#/models/")));
    return `<a href="${href}" class="${active ? "active" : ""}">${icon(ic)}<span>${label}</span></a>`;
  }).join("");
}

function renderChrome() {
  const ov = S.ov;
  if (!ov) return;
  const e = ov.env;
  const pill = (ok, label, text) => `<span class="pill"><span class="dot ${ok ? "ok" : "bad"}"></span>${label}：${esc(text)}</span>`;
  const run = ov.run;
  const collected=ov.collection || {};
  const collectionStatus=$("#collectionStatus"),startCollection=$("#collectReplies"),pauseCollection=$("#pauseCollectReplies");
  if(collectionStatus)collectionStatus.textContent=collected.text || '等待手动收集回复';
  if(startCollection){startCollection.disabled=!!collected.pending;startCollection.onclick=()=>collectionAction('start');}
  if(pauseCollection){pauseCollection.disabled=!collected.pending;pauseCollection.onclick=()=>collectionAction('pause');}
  $("#pills").innerHTML =
    pill(e.qq.ok, "QQ", e.qq.text) + pill(e.browser.ok, "IC交易网", e.browser.text) + pill(e.deepseek.ok, "DeepSeek", e.deepseek.text) +
    `<span class="pill"><span class="dot ${run.running ? "run" : "ok"}"></span>${ov.collection?.active ? "正在读取回复" : run.running ? (run.stopping ? "正在停止…" : run.waiting_reason ? "等待 QQ 协助" : "运行中") : esc(ov.collection?.text || "空闲")}</span>`;
  $("#sideFoot").innerHTML = run.running
    ? `<div class="run"><span class="dot run"></span>${run.stopping ? "正在安全停止" : { run: "正在询价", collect: "正在补收回复", reply: "正在发送回复", "dry-run": "试运行中" }[run.kind] || "运行中"}</div><div class="hint">${esc(run.label || "")}</div>`
    : `<div class="run"><span class="dot ok"></span>系统空闲</div><div class="hint">${esc(ov.observer?.text || `今天 ${ov.today}`)}</div>`;
}

let collectingRequest=null,collectionBusy=false;
async function collectionAction(action){
  if(collectionBusy)return;
  collectionBusy=true;
  try{
    const body={action};
    if(action==='start'){collectingRequest ||= crypto.randomUUID();body.request_id=collectingRequest;}
    await api('/api/collection',{json:body});
    if(action==='start')collectingRequest=null;
    await refreshOverview();
  }catch(e){toast(e.message,'bad');}finally{collectionBusy=false;}
}

async function refreshOverview() {
  const wasRunning = S.ov?.run?.running;
  try {
    S.ov = await api("/api/overview");
  } catch (e) {
    $("#pills").innerHTML = `<span class="pill"><span class="dot bad"></span>与后台服务断开，请重新打开界面</span>`;
    return;
  }
  renderChrome();
  const h = location.hash || "#/";
  if (h === "#/" || h === "") renderDashboard();
  else if (h === "#/new") updateNewPageState();
  const task = h.match(/^#\/tasks\/(\d+)$/);
  if (task && wasRunning && !S.ov.run.running) renderTaskDetail(+task[1], true);
}

// ---------- 工作台 ----------
function renderDashboard(force = false) {
  const ov = S.ov;
  const view = $("#view");
  if (!ov) { view.innerHTML = empty("正在加载…", "clock"); return; }
  const key = JSON.stringify([ov.run, ov.env, ov.tasks, ov.stats, ov.jobs, ov.collection]);
  if (!force && key === S.dashKey && view.dataset.page === "dash") return;
  S.dashKey = key;
  view.dataset.page = "dash";
  const run = {...ov.run, running:ov.run.running && !ov.collection?.active}, e = ov.env;
  const hero = run.running
    ? `<div class="hero fade">
        <div class="hero-icon">${icon("spark", 28)}</div>
        <div><h2>${run.stopping ? "正在安全停止…" : { run: "正在询价", collect: "正在补收回复", reply: "正在发送回复", "dry-run": "正在试运行" }[run.kind] || "运行中"}：${esc(run.label || "")}</h2>
        <p>${run.stopping ? "处理完手头这一家就会停下，并生成报告" : "有新回复会自动读取并交给 DeepSeek 解析。后台模式不抢焦点；需要 QQ 前台协助的步骤会暂停并保留进度。"}</p></div>
        <div class="spacer"></div>
        <a class="btn btn-light" href="#/log">${icon("terminal")}查看日志</a>
        <button class="btn btn-light" id="btnStop" ${run.stopping ? "disabled" : ""}>${icon("stop")}安全停止</button>
      </div>`
    : `<div class="hero idle fade">
        <div class="hero-icon">${icon("chat", 28)}</div>
        <div><h2>${esc(ov.collection?.text || "等待任务")}</h2><p>录入型号和数量，第二台自动询价。发信后需要读取回复时，请点顶部「收集回复」。</p></div>
        <div class="spacer"></div>
        <a class="btn btn-light btn-lg" href="#/new">${icon("plus")}新建询价</a>
      </div>`;
  const lastErr = !run.running && run.last_error
    ? `<div class="notice mt">${icon("alert")}<div><b>上一次运行异常结束：</b><div class="mono small" style="white-space:pre-wrap">${esc(run.last_error)}</div></div></div>` : "";
  const envCard = (ic, title, st, extra = "") => `
    <div class="card env">
      <div class="env-icon" style="${st.ok ? "" : "background:var(--red-soft);color:var(--red)"}">${icon(ic, 22)}</div>
      <div style="min-width:0">
        <div class="env-title">${title}</div>
        <div class="row" style="margin-top:3px">${chip(st.text, st.ok ? "green" : "red")}</div>
        ${extra}
      </div>
    </div>`;
  const browserAct = e.browser.ok ? `<div class="small muted env-act">专用 Edge 窗口，登录状态会保留</div>`
    : `<div class="env-act"><button class="btn btn-sm" id="btnBrowser">${icon("globe", 15)}打开专用浏览器${e.browser.code === "login" ? "登录" : ""}</button></div>`;
  const tasks = ov.tasks.length
    ? ov.tasks.map(taskRow).join("")
    : empty("还没有询价任务，点「新建询价」开始第一轮", "list");
  view.innerHTML = `
    ${hero}${lastErr}
    ${(ov.jobs || []).length ? `<div class="card mt" style="padding:18px"><h3>执行批次</h3>${ov.jobs.map(j=>`<div class="row wrap between" style="padding:8px 0"><span>${j.dry_run?"试运行 ":""}任务 ${j.task_ids.map(id=>'#'+id).join('、')} · ${esc(({queued:'已排队',running:'正在执行',done:'发信已完成',waiting_verification:'等待网站人工校验',stopped:'已停止',interrupted:'中断待核对',failed:'执行异常'})[j.status]||j.status)}${j.error?`<br><span class="small muted">${esc(j.error)}</span>`:''}</span><span>${['queued','waiting_verification','interrupted','failed'].includes(j.status)?`<button class="btn btn-sm" data-cancel-job="${esc(j.id)}">取消此批次</button>`:''}${['interrupted','stopped','failed'].includes(j.status)?`<button class="btn btn-sm" data-resume-job="${esc(j.id)}">继续未完成部分</button>`:''}</span></div>`).join('')}</div>`:''}
    <div class="grid g4 mt">
      ${envCard("chat", "QQ", e.qq, e.qq.ok ? `<div class="small muted env-act">请保持登录，主窗口别关</div>` : `<div class="small muted env-act">请登录 QQ 并打开主窗口</div>`)}
      ${envCard("globe", "IC交易网", e.browser, browserAct)}
      ${envCard("spark", "DeepSeek 解析", e.deepseek, `<div class="env-act"><button class="btn btn-sm" id="btnDs">${icon("link", 15)}测试连接</button></div>`)}
      ${envCard("card", "名片", e.card, `<div class="env-act"><a class="btn btn-sm" href="#/settings">${icon("settings", 15)}查看 / 更换</a></div>`)}
    </div>
    <div class="grid g3 mt">
      <div class="card stat"><div class="k">今日已询价</div><div class="v">${ov.stats.sent_today}<small>家</small></div></div>
      <div class="card stat"><div class="k">今日收到回复</div><div class="v">${ov.stats.replied_today}<small>家</small></div></div>
      <div class="card stat"><div class="k">今日有效报价</div><div class="v">${ov.stats.valid_today}<small>条</small></div></div>
    </div>
    <div class="card mt">
      <div class="card-h"><h3>最近的询价任务</h3><div class="spacer"></div><a href="#/tasks" class="small">全部任务 ›</a></div>
      <div class="card-b" style="padding:12px 0 6px">${tasks}</div>
    </div>`;
  bindCommon();
  document.querySelectorAll('[data-cancel-job],[data-resume-job]').forEach(b=>b.onclick=async()=>{try{const resume=!!b.dataset.resumeJob;await api(`/api/jobs/${b.dataset.resumeJob||b.dataset.cancelJob}/${resume?'resume':'cancel'}`,{json:{}});await refreshOverview()}catch(e){toast(e.message,'bad')}});
}

function taskRow(t) {
  return `<div class="task-row" data-href="#/tasks/${t.id}">
    <div><div class="task-title">${esc(t.part_no)} <span class="muted">× ${fmtQty(t.qty)}</span></div>
      <div class="small muted">任务 #${t.id} · ${fmtTime(t.created_at)}</div></div>
    <div>${taskChip(t)}</div>
    <div><div class="row between small"><span class="text2">有效报价</span><b>${t.valid} / ${t.target}</b></div>${bar(t.valid, t.target)}</div>
    <div class="small text2 hide-md">已询 ${t.sent} · 回复 ${t.replied}${t.best ? `<br>最低 <b style="color:var(--text)">${money(t.best)}</b>` : ""}</div>
    <div class="muted">${icon("arrow")}</div>
  </div>`;
}

function bindCommon() {
  document.querySelectorAll("[data-href]").forEach((el) => el.addEventListener("click", () => (location.hash = el.dataset.href)));
  const stop = $("#btnStop");
  if (stop) stop.onclick = async () => {
    if (!confirm("安全停止：程序会做完手头这一家再停，并用已收到的报价生成报告。确定吗？")) return;
    try { await api("/api/stop", { json: {} }); toast("已发出停止请求"); refreshOverview(); } catch (e) { toast(e.message, "bad"); }
  };
  const br = $("#btnBrowser");
  if (br) br.onclick = async () => {
    try { await api("/api/browser/open", { json: {} }); toast("已打开专用浏览器，请在里面扫码登录 IC交易网"); } catch (e) { toast(e.message, "bad"); }
  };
  const ds = $("#btnDs");
  if (ds) ds.onclick = async () => {
    ds.disabled = true;
    try { const r = await api("/api/deepseek/test", { json: {} }); toast(`DeepSeek 连接正常，可用模型：${r.models.join("、")}`); } catch (e) { toast(e.message, "bad"); }
    ds.disabled = false;
  };
}

// ---------- 新建询价 ----------
async function ensureSettings() {
  if (!S.settings) S.settings = await api("/api/settings");
  return S.settings;
}

async function renderNew() {
  if(S.ov?.runtime?.dedicated_worker && !S.newInitialized){S.dryRun=false;S.newInitialized=true;}
  const view = $("#view");
  view.dataset.page = "new";
  view.innerHTML = empty("正在加载…", "clock");
  let st;
  try { st = await ensureSettings(); } catch (e) { view.innerHTML = empty(e.message, "alert"); return; }
  const inq = st.settings.inquiry || {};
  view.innerHTML = `
    <div class="grid g3 fade">
      <div class="span2" style="display:grid;gap:18px">
        <div class="card">
          <div class="card-h"><h3>要询价的型号</h3><div class="spacer"></div>
            <button class="btn btn-sm" id="btnPaste">${icon("paste", 15)}批量粘贴型号</button>
          </div>
          <div class="card-b">
            <div id="pasteBox" style="display:none;margin-bottom:14px">
              <div class="field"><label>统一数量（每行未填数量时使用）</label><input class="input" id="batchQty" placeholder="例如 5000 或 5k"></div><textarea class="input" id="pasteText" placeholder="每行一个型号，可选填数量；也可粘贴 Excel 两列，例如：&#10;STM32F405RGT6	5000&#10;GD32F405RGT6	3k"></textarea>
              <div class="row mt" style="margin-top:10px"><button class="btn btn-sm btn-primary" id="btnParse">${icon("check", 15)}识别并加入</button><span class="small muted">数量可以写 5000、5k、5千</span></div>
            </div>
            <table class="t parts-table"><thead><tr><th></th><th>型号</th><th style="width:220px">数量</th><th style="width:60px"></th></tr></thead><tbody id="partsBody"></tbody></table>
            <button class="btn btn-sm mt" id="btnAdd" style="margin-top:12px">${icon("plus", 15)}添加一行</button>
          </div>
        </div>
        <div class="card">
          <div class="card-h"><h3>询价话术预览</h3><div class="spacer"></div><a class="small" href="#/settings">修改话术 ›</a></div>
          <div class="card-b"><div id="msgPreview" style="display:grid;gap:10px"></div>
            <div class="small muted" style="margin-top:10px">${inq.send_card && inq.card_image ? `发完这段话后会接着发送名片图片（${esc(inq.card_image)}）。` : "本次只发送文字（名片开关已关闭或未设置图片）。"}</div></div>
        </div>
      </div>
      <div style="display:grid;gap:18px;align-content:start">
        <div class="card">
          <div class="card-h"><h3>本次选项</h3></div>
          <div class="card-b" style="display:grid;gap:16px">
            <div class="field"><label>每个型号最多问几家</label>
              <div class="suffix"><input class="input" id="maxN" type="number" min="1" placeholder="默认 ${esc(inq.max_suppliers ?? 30)}" value="${esc(S.maxN)}"><span>家</span></div>
              <div class="help">${S.ov?.runtime?.dedicated_worker ? "按本次家数上限执行，发信完成后独立采集回复；默认每个型号最多联系三家。" : `先问 ${esc(inq.first_batch ?? 10)} 家，凑不够 ${esc(inq.target_quotes ?? 5)} 条有效报价再每批补 ${esc(inq.next_batch ?? 5)} 家`}</div></div>
            <label class="switch"><input type="checkbox" id="dryRun" ${S.dryRun ? "checked" : ""}><i></i><span><b>试运行</b><br><span class="small muted">只列出会询价哪些供应商，不发消息</span></span></label>
          </div>
        </div>
        <div class="card">
          <div class="card-h"><h3>开始前检查</h3></div>
          <div class="card-b"><div class="checklist" id="checklist"></div></div>
        </div>
        <button class="btn btn-primary btn-lg" id="btnStart">${icon("play")}开始询价</button>
        <div class="notice">${icon("alert")}<div>${S.ov?.runtime?.dedicated_worker ? "QQ 操作只在第二台执行，本机可照常使用。请保持第二台登录且不要手动操作它；登录或处理验证码前可在本机暂停全部操作。" : "默认后台处理，不抢鼠标。若 QQ 不支持后台切换或发送，请在设置开启「前台协助」并手动打开 QQ；切换到其他软件后暂停。"}</div></div>
      </div>
    </div>`;
  renderParts();
  updateNewPageState();
  $("#btnAdd").onclick = () => { S.parts.push({ part: "", qty: "" }); renderParts(); };
  $("#btnPaste").onclick = () => { const b = $("#pasteBox"); b.style.display = b.style.display === "none" ? "block" : "none"; if (b.style.display === "block") $("#pasteText").focus(); };
  $("#btnParse").onclick = () => {
    const uniformQty=$("#batchQty").value.trim();
    const rows = $("#pasteText").value.split(/\r?\n/).map(l=>l.trim()).filter(Boolean).map(line=>{
      const explicit=line.split(/[\t,，;；:：]+/).map(s=>s.trim());
      if(explicit.length>1)return {part:explicit[0].replace(/\s+/g,'').toUpperCase(),qty:explicit[1]||uniformQty};
      if(uniformQty)return {part:line.replace(/\s+/g,'').toUpperCase(),qty:uniformQty};
      const pair=line.match(/^(.*?)\s+(\d+(?:\.\d+)?(?:k|千|w|万)?)$/i);
      return {part:(pair?pair[1]:line).replace(/\s+/g,'').toUpperCase(),qty:pair?pair[2]:''};
    });
    if (!rows.length) { toast("没有识别到型号", "bad"); return; }
    const merged = S.parts.filter((p) => p.part.trim()).concat(rows);
    const names = merged.map(p => p.part.trim().toUpperCase());
    if (new Set(names).size !== names.length) { toast("有重复型号，请合并后再粘贴", "bad"); return; }
    if (merged.length > 100) { toast("每批最多 100 个型号", "bad"); return; }
    S.parts = merged;
    $("#pasteText").value = "";
    $("#pasteBox").style.display = "none";
    renderParts();
    toast(`已加入 ${rows.length} 个型号`);
  };
  $("#maxN").oninput = (e) => (S.maxN = e.target.value);
  $("#dryRun").onchange = (e) => { S.dryRun = e.target.checked; updateNewPageState(); };
  $("#btnStart").onclick = startRun;
}

function renderParts() {
  const body = $("#partsBody");
  if (!body) return;
  body.innerHTML = S.parts.map((p, i) => `
    <tr>
      <td class="small">${i + 1}</td>
      <td><input class="input mono" data-i="${i}" data-k="part" value="${esc(p.part)}" placeholder="例如 STM32F405RGT6"></td>
      <td><div class="suffix"><input class="input" data-i="${i}" data-k="qty" value="${esc(p.qty)}" placeholder="例如 5000 或 5k"><span>pcs</span></div></td>
      <td><button class="btn btn-sm btn-icon btn-danger" data-del="${i}" title="删除">${icon("trash", 15)}</button></td>
    </tr>`).join("");
  body.querySelectorAll("input").forEach((inp) => inp.addEventListener("input", (e) => {
    const { i, k } = e.target.dataset;
    S.parts[+i][k] = k === "part" ? e.target.value.toUpperCase() : e.target.value;
    if (k === "part") e.target.value = S.parts[+i][k];
    renderPreview();
  }));
  body.querySelectorAll("[data-del]").forEach((b) => (b.onclick = () => {
    S.parts.splice(+b.dataset.del, 1);
    if (!S.parts.length) S.parts.push({ part: "", qty: "" });
    renderParts();
  }));
  renderPreview();
}

function validParts() {
  return S.parts.map((p) => ({ part: p.part.trim().replace(/\s+/g, ""), qty: parseQty(p.qty) })).filter((p) => p.part && p.qty > 0);
}

function renderPreview() {
  const box = $("#msgPreview");
  if (!box || !S.settings) return;
  const tpl = (S.settings.settings.inquiry || {}).template || "";
  const ps = validParts();
  box.innerHTML = ps.length
    ? ps.map((p) => `<div class="preview">${esc(renderMsg(tpl, p.part, p.qty))}</div>`).join("")
    : `<div class="preview muted">${esc(renderMsg(tpl, "型号", 5000))}</div>`;
}

function updateNewPageState() {
  const ov = S.ov, list = $("#checklist"), btn = $("#btnStart");
  if (!ov || !list || !btn) return;
  const e = ov.env;
  const item = (ok, text, hint) => `<div class="row"><span style="color:${ok ? "#22c55e" : "#ef4444"}">${icon(ok ? "check" : "x")}</span><div><div>${text}</div>${!ok && hint ? `<div class="small muted">${hint}</div>` : ""}</div></div>`;
  list.innerHTML = item(e.qq.ok, "QQ 已登录、主窗口已打开", "请先登录 QQ 并打开主窗口")
    + item(e.browser.ok, "IC交易网", e.browser.code === "closed" ? "专用浏览器没开，开始询价时会自动打开；第一次需要扫码登录" : e.browser.code==='verification' ? "第二台需要人工完成网站校验" : "请在第二台的专用浏览器里扫码登录")
    + item(e.deepseek.ok, "DeepSeek 已配置", "请在项目文件夹的 .env 里填写 DEEPSEEK_API_KEY")
    + item(ov.runtime?.dedicated_worker || !ov.run.running, ov.runtime?.dedicated_worker ? "任务由第二台依次自动执行" : "没有其他询价在运行", "请等当前运行结束，或先安全停止")
    + (e.browser.code==='verification' ? '<div class="notice">第二台的网站需要人工完成验证码。提交后会保留任务并等待，不会反复搜索。</div>' : '');
  btn.disabled = S.submitting || (!ov.runtime?.dedicated_worker && ov.run.running) || (!S.dryRun && (!e.qq.ok || !ov.runtime?.allow_send));
  btn.innerHTML = S.submitting ? '正在提交…' : `${icon("play")}${S.dryRun ? "开始试运行" : ov.runtime?.dedicated_worker ? "提交并由第二台询价" : "开始询价"}`;
}

async function startRun() {
  if(S.submitting)return;
  const parts = validParts();
  if (!parts.length) { toast("请至少填写一个型号和数量（数量要大于 0）", "bad"); return; }
  const bad = S.parts.filter((p) => p.part.trim() && !(parseQty(p.qty) > 0));
  if (bad.length) { toast(`这些型号的数量不对：${bad.map((p) => p.part).join("、")}`, "bad"); return; }
  if (new Set(parts.map(p => p.part.toUpperCase())).size !== parts.length) { toast("型号重复，请合并后提交", "bad"); return; }
  const tip = parts.map((p) => `${p.part} × ${fmtQty(p.qty)}`).join("\n");
  if (!S.dryRun && !confirm(`将对下面的型号真实发起 QQ 询价：\n\n${tip}\n\n确定开始吗？`)) return;
  const payload={parts,dry_run:S.dryRun,max:S.maxN ? Number(S.maxN) : 3};
  const payloadKey=JSON.stringify(payload);
  if(S.submission?.key!==payloadKey)S.submission={key:payloadKey,id:crypto.randomUUID()};
  payload.request_id=S.submission.id;
  S.submitting=true;updateNewPageState();
  try {
    await api("/api/run", { json: payload });
    S.submission=null;
    toast(S.dryRun ? "试运行已提交" : "任务已交给第二台，系统会依次执行");
    await refreshOverview();
    location.hash = S.ov?.runtime?.dedicated_worker ? "#/" : "#/log";
  } catch (e) { toast(e.message, "bad"); } finally {S.submitting=false;updateNewPageState();}
}

// ---------- 询价任务 ----------
async function renderTasks() { return renderModels(); }

async function renderLegacyTasks() {
  const view = $("#view");
  view.dataset.page = "tasks";
  view.innerHTML = empty("正在加载…", "clock");
  let list;
  try { list = await api(`/api/tasks${S.showDry ? "?dry=1" : ""}`); } catch (e) { view.innerHTML = empty(e.message, "alert"); return; }
  view.innerHTML = `
    <div class="card fade">
      <div class="card-h"><h3>全部询价任务</h3><div class="spacer"></div>
        <label class="switch small"><input type="checkbox" id="showDry" ${S.showDry ? "checked" : ""}><i></i>显示试运行</label></div>
      <div class="card-b" style="padding:12px 0 0">
        ${list.length ? `<div class="table-wrap"><table class="t">
          <thead><tr><th>任务</th><th>型号</th><th class="num">数量</th><th>状态</th><th style="width:200px">有效报价</th><th class="num">已询价</th><th class="num">已回复</th><th class="num">无货</th><th>创建时间</th><th></th></tr></thead>
          <tbody>${list.map((t) => `<tr class="click" data-href="#/tasks/${t.id}">
            <td class="muted">#${t.id}</td><td class="mono"><b>${esc(t.part_no)}</b></td><td class="num">${fmtQty(t.qty)}</td><td>${taskChip(t)}</td>
            <td><div class="row between small"><span></span><b>${t.valid} / ${t.target}</b></div>${bar(t.valid, t.target)}</td>
            <td class="num">${t.sent}</td><td class="num">${t.replied}</td><td class="num">${t.no_stock}</td><td class="small text2 nowrap">${fmtTime(t.created_at)}</td>
            <td class="muted">${icon("arrow")}</td></tr>`).join("")}</tbody></table></div>`
          : empty("还没有询价任务")}
      </div>
    </div>`;
  $("#showDry").onchange = (e) => { S.showDry = e.target.checked; renderTasks(); };
  bindCommon();
}

async function renderTaskDetail(id, quiet = false, model = null) {
  const view = $("#view");
  if (!quiet) { view.dataset.page = `task-${id}`; view.innerHTML = empty("正在加载…", "clock"); }
  let d;
  try { d = await api(model !== null ? `/api/models/detail?part=${encodeURIComponent(model)}&dry=${S.showDry ? 1 : 0}` : `/api/tasks/${id}`); } catch (e) { view.innerHTML = empty(e.message, "alert"); return; }
  if (view.dataset.page !== `task-${id}`) return;
  const t = d.task, sups = d.suppliers;
  const history = d.history || [];
  const sent = sups.filter((s) => s.sent_at), replied = sups.filter((s) => s.replies?.length);
  const quotes = sups.filter((s) => s.valid).sort((a, b) => (a.tax_price ?? 1e9) - (b.tax_price ?? 1e9));
  const asking = sups.filter((s) => s.status === "asking");
  const taxText = (q) => ({ true: "含税", false: "未税" }[q.tax_included] ?? "未说明");
  const vsChip = (v) => (!v ? "" : v.startsWith("高") ? chip(v, "red") : v.startsWith("低") ? chip(v, "green") : chip(v));
  $("#pageSub").textContent = `${t.runs ? `共 ${t.runs} 次询价 · 最近数量 ${fmtQty(t.qty)}` : `任务 #${t.id}`} · 创建于 ${fmtTime(t.created_at)}`;
  view.innerHTML = `
    <a class="back" href="#/tasks">${icon("back", 16)}返回任务列表</a>
    <div class="card fade" style="padding:20px 22px">
      <div class="row wrap between">
        <div class="row" style="gap:14px">
          <div class="env-icon" style="width:48px;height:48px">${icon("list", 24)}</div>
          <div><div style="font-size:20px;font-weight:750" class="mono">${esc(t.part_no)} <span class="muted" style="font-weight:500">× ${fmtQty(t.qty)} pcs</span></div>
            <div class="row small text2" style="gap:8px">${taskChip(t)}<span>网站参考价 <b>${money(t.ref_price)}</b></span></div></div>
        </div>
        <div class="row">
          <button class="btn" id="btnCollect">${icon("refresh", 16)}收集本型号回复一轮</button>
          <button class="btn btn-primary" id="btnReport">${icon("download", 16)}生成并下载报告</button>
        </div>
      </div>
      ${t.message ? `<div class="preview" style="margin-top:16px"><span class="small muted">发出的询价：</span><br>${esc(t.message)}</div>` : ""}
    </div>
    <div class="grid g4 mt">
      <div class="card kpi"><div class="k">已询价</div><div class="v">${sent.length}<small class="muted small"> 家</small></div></div>
      <div class="card kpi"><div class="k">已回复</div><div class="v">${replied.length}<small class="muted small"> 家</small></div></div>
      <div class="card kpi"><div class="k">有效报价</div><div class="v">${quotes.length}<small class="muted small"> / ${t.target}</small></div>${bar(quotes.length, t.target)}</div>
      <div class="card kpi"><div class="k">需要你回复</div><div class="v" style="${asking.length ? "color:var(--amber)" : ""}">${asking.length}<small class="muted small"> 家</small></div></div>
    </div>
    <div class="card mt">
      <div class="card-h"><h3>报价对比</h3><span class="small muted">按含税单价排序；未税按报价税率估算（缺省 13%），税务不明及美元不参与比价</span></div>
      <div class="card-b" style="padding:12px 0 0">${quotes.length ? `<div class="table-wrap"><table class="t">
        <thead><tr><th></th><th>供应商</th><th class="num">含税单价</th><th>原始报价</th><th>对比参考价</th><th>交期</th><th>批次</th><th>供应商原话</th></tr></thead>
        <tbody>${quotes.map((s, i) => `<tr>
          <td><span class="medal ${i === 0 && s.tax_price != null ? "gold" : ""}">${s.tax_price != null ? i + 1 : "—"}</span></td>
          <td><b>${esc(s.company)}</b><div class="small muted">${esc(s.chat_name || "")}</div></td>
          <td class="num"><span class="price">${s.tax_price != null ? money(s.tax_price) : "—"}</span></td>
          <td class="nowrap">${esc(s.quote.unit_price)} ${esc(s.quote.currency || "")} · ${taxText(s.quote)}</td>
          <td>${vsChip(s.vs_ref)}</td><td>${esc(s.quote.lead_time || "—")}</td><td>${esc(s.quote.batch || "—")}</td>
          <td><div class="quote-text">${esc(s.replies.join("\n"))}</div></td></tr>`).join("")}</tbody></table></div>`
        : empty("还没有有效报价", "clock")}</div>
    </div>
    <div class="card mt">
      <div class="card-h"><h3>已发送供应商</h3><span class="small muted">共 ${sups.length} 家</span></div>
      <div class="card-b" style="padding:12px 0 0">${sups.length ? `<div class="table-wrap"><table class="t">
        <thead><tr><th>供应商</th><th>状态</th><th>网站库存 / 批号</th><th>回复</th><th>发送</th><th>回复时间</th></tr></thead>
        <tbody>${sups.map((s) => `<tr>
          <td><b>${esc(s.company)}</b><div class="small muted">${esc(s.chat_name || "—")}</div>${conversationButton(s)}</td>
          <td>${chip(s.status_cn, SUP_CHIP[s.status] ?? "")}${s.quote && s.quote.question ? `<div class="small" style="color:var(--amber);margin-top:4px">对方问：${esc(s.quote.question)}</div>` : ""}</td>
          <td class="small text2 nowrap">${esc(s.site.qty || "—")} · ${esc(s.site.batch || "—")}<div class="muted">${esc(s.site.warehouse || "")}</div></td>
          <td>${s.replies.length ? `<div class="quote-text">${esc(s.replies.join("\n"))}</div>` : `<span class="muted small">—</span>`}${s.error ? `<div class="err" style="margin-top:4px">${esc(s.error)}</div>` : ""}${s.read_error ? `<div class="err" style="margin-top:4px">待补收：${esc(s.read_error)}</div>` : ""}</td>
          <td class="small text2 nowrap">${fmtTime(s.sent_at)}</td><td class="small text2 nowrap">${fmtTime(s.last_reply_at)}</td></tr>`).join("")}</tbody></table></div>`
        : empty("暂无已发送成功的供应商")}</div>
    </div>`;
  if (history.length) view.insertAdjacentHTML("beforeend", `<div class="card mt"><div class="card-h"><h3>历史询价记录</h3></div><div class="card-b">${history.map(h => `<a class="btn btn-sm" style="margin:4px" href="#/tasks/${h.id}">#${h.id} · ${fmtQty(h.qty)} pcs · ${fmtTime(h.created_at)}</a>`).join("")}</div></div>`);
  bindConversations();
  $("#btnCollect").onclick = async () => {
    try { await api("/api/collect", { json: { task_ids: t.task_ids || [t.id],request_id:crypto.randomUUID() } }); toast("已提交本型号的一轮回复收集，完成后停止"); refreshOverview(); } catch (e) { toast(e.message, "bad"); }
  };
  $("#btnReport").onclick = async () => {
    try { const r = await api(`/api/tasks/${t.id}/report`, { json: {} }); location.href = `/api/reports/${encodeURIComponent(r.name)}`; toast("报告已生成"); } catch (e) { toast(e.message, "bad"); }
  };
}

// ---------- 运行日志 ----------
function logLineHtml(line) {
  const m = line.match(/^(\[\d\d:\d\d:\d\d\])\s?(.*)$/);
  const time = m ? `<span class="time">${esc(m[1])}</span> ` : "";
  const body = m ? m[2] : line;
  const cls = /✓|已凑齐|报告已生成/.test(body) ? "ok" : /✗|失败|Traceback|Error/.test(body) ? "bad" : /←/.test(body) ? "in" : /!|确认框|停止|暂停/.test(body) ? "warn" : "";
  return `${time}<span class="${cls}">${esc(body)}</span>\n`;
}

async function renderLog() {
  const view = $("#view");
  view.dataset.page = "log";
  S.log = { file: null, offset: -1, auto: S.log.auto };
  view.innerHTML = `
    <div class="card fade">
      <div class="card-h"><h3>运行日志</h3><span class="small muted">实时显示，每条询价、回复和解析结果都在这里</span><div class="spacer"></div>
        <label class="switch small"><input type="checkbox" id="autoScroll" ${S.log.auto ? "checked" : ""}><i></i>自动滚动</label>
        <button class="btn btn-sm" id="btnLogDir">${icon("folder", 15)}打开日志文件夹</button></div>
      <div class="card-b"><div class="console" id="console"></div></div>
    </div>`;
  $("#autoScroll").onchange = (e) => (S.log.auto = e.target.checked);
  $("#btnLogDir").onclick = () => api("/api/open-folder", { json: { which: "logs" } }).catch((e) => toast(e.message, "bad"));
  await pollLog();
}

async function pollLog() {
  const box = $("#console");
  if (!box) return;
  try {
    const r = await api(`/api/log?offset=${S.log.offset}&file=${encodeURIComponent(S.log.file || "")}`);
    if (r.file !== S.log.file || S.log.offset < 0) box.innerHTML = "";
    S.log.file = r.file;
    S.log.offset = r.offset;
    if (r.lines.length) {
      box.insertAdjacentHTML("beforeend", r.lines.map(logLineHtml).join(""));
      if (S.log.auto) box.scrollTop = box.scrollHeight;
    } else if (!box.innerHTML) {
      box.innerHTML = `<span class="time">今天还没有运行记录。</span>`;
    }
  } catch (_) { /* 下次再试 */ }
}

// ---------- 运行问题 ----------
async function renderIncidents() {
  const view = $("#view");
  view.dataset.page = "incidents";
  view.innerHTML = `<div class="row between wrap">
    <span class="small muted" id="incidentDays">最近 14 天</span>
    <button class="btn btn-sm" id="refreshIncidents" disabled>${icon("refresh", 15)}刷新</button>
    </div><div id="incidentSummary" class="mt">${empty("正在读取运行问题…", "clock")}</div>`;
  const summary = $("#incidentSummary", view);
  const refresh = $("#refreshIncidents", view);
  refresh.onclick = renderIncidents;
  try {
    const data = await api("/api/incidents?days=14");
    if (!summary.isConnected) return;
    $("#incidentDays", view).textContent = `最近 ${data.days} 天`;
    if (!data.by_type.length) {
      summary.innerHTML = `<div class="card fade">${empty("这段时间没有运行问题记录", "check")}</div>`;
      return;
    }
    summary.innerHTML = `<div class="grid g2 fade">
      <div class="card"><div class="card-h"><h3>按类型统计</h3></div>
        <div class="table-wrap"><table class="t" id="incidentTypes">
          <thead><tr><th scope="col">问题类型</th><th scope="col" class="num">次数</th></tr></thead>
          <tbody>${data.by_type.map(row => `<tr><td>${esc(row.label)}</td><td class="num">${esc(row.count)}</td></tr>`).join("")}</tbody>
        </table></div></div>
      <div class="card"><div class="card-h"><h3>按日期统计</h3></div>
        <div class="table-wrap"><table class="t" id="incidentDates">
          <thead><tr><th scope="col">日期</th><th scope="col">问题类型</th><th scope="col" class="num">次数</th></tr></thead>
          <tbody>${data.by_date.map(row => `<tr><td>${esc(row.date)}</td><td>${esc(row.label)}</td><td class="num">${esc(row.count)}</td></tr>`).join("")}</tbody>
        </table></div></div>
    </div>`;
  } catch (e) {
    if (summary.isConnected) summary.innerHTML = `<div class="card fade">${empty(e.message, "alert")}</div>`;
  } finally {
    refresh.disabled = false;
  }
}

// ---------- 报告文件 ----------
async function renderReports() {
  const view = $("#view");
  view.dataset.page = "reports";
  let list;
  try { list = await api("/api/reports"); } catch (e) { view.innerHTML = empty(e.message, "alert"); return; }
  const kb = (n) => (n > 1024 * 1024 ? `${(n / 1024 / 1024).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`);
  view.innerHTML = `
    <div class="card fade">
      <div class="card-h"><h3>Excel 报告</h3><span class="small muted">每次询价结束、补收回复或手动生成都会保存一份</span><div class="spacer"></div>
        <button class="btn btn-sm" id="btnRepDir">${icon("folder", 15)}打开报告文件夹</button></div>
      <div class="card-b" style="padding:12px 0 0">${list.length ? `<div class="table-wrap"><table class="t">
        <thead><tr><th>文件名</th><th>生成时间</th><th class="num">大小</th><th></th></tr></thead>
        <tbody>${list.map((f) => `<tr><td><span class="row">${icon("file", 16)}${esc(f.name)}</span></td><td class="text2">${fmtTime(f.mtime)}</td><td class="num text2">${kb(f.size)}</td>
          <td class="num"><a class="btn btn-sm" href="/api/reports/${encodeURIComponent(f.name)}">${icon("download", 15)}下载</a></td></tr>`).join("")}</tbody></table></div>`
        : empty("还没有生成过报告", "file")}</div>
    </div>`;
  $("#btnRepDir").onclick = () => api("/api/open-folder", { json: { which: "reports" } }).catch((e) => toast(e.message, "bad"));
}

// ---------- 设置 ----------
const FIELD_META = {
  "inquiry.send_card": ["询价后发送名片"],
  "desktop.foreground_assist": ["允许 QQ 前台协助（用户切走即暂停，不抢回窗口）"],
  "inquiry.target_quotes": ["每个型号要凑齐几条有效报价", "条"],
  "inquiry.first_batch": ["第一批询价几家", "家"],
  "inquiry.next_batch": ["报价不够时每批再补几家", "家"],
  "inquiry.wave_wait_minutes": ["每批发完等多久再决定补发", "分钟"],
  "inquiry.max_suppliers": ["一个型号最多询价几家", "家"],
  "inquiry.max_wait_minutes": ["一个型号最多等多久", "分钟"],
  "inquiry.skip_recent_hours": ["多少小时内问过的供应商不再重复问", "小时"],
  "pacing.min_interval": ["两条询价最短间隔", "秒"],
  "pacing.max_interval": ["两条询价最长间隔", "秒"],
  "pacing.max_per_hour": ["每小时最多发几条", "条"],
  "filter.max_pages": ["最多翻几页搜索结果", "页"],
  "filter.require_enough_stock": ["库存够需求数量的供应商优先"],
  "filter.only_today": ["只问当天更新过库存的供应商"],
  "llm.model": ["解析回复用的模型"],
};

async function renderSettings() {
  const view = $("#view");
  view.dataset.page = "settings";
  view.innerHTML = empty("正在加载…", "clock");
  try { S.settings = await api("/api/settings"); } catch (e) { view.innerHTML = empty(e.message, "alert"); return; }
  const s = S.settings.settings;
  const get = (k) => k.split(".").reduce((o, p) => (o || {})[p], s);
  const num = (k) => `<div class="field"><label>${FIELD_META[k][0]}</label><div class="suffix"><input class="input" type="number" min="0" step="any" data-k="${k}" value="${esc(get(k) ?? "")}"><span>${FIELD_META[k][1]}</span></div></div>`;
  const sw = (k) => `<label class="switch"><input type="checkbox" data-k="${k}" ${get(k) ? "checked" : ""}><i></i>${FIELD_META[k][0]}</label>`;
  view.innerHTML = `
    <div class="grid g2 fade">
      <div class="card span2">
        <div class="card-h"><h3>询价话术</h3><span class="small muted">占位符：{part} 型号，{qty} 数量（5000），{qtyk} 数量简写（5k）</span></div>
        <div class="card-b grid g2">
          <div class="field"><textarea class="input" data-k="inquiry.template" id="tplInput" style="min-height:120px">${esc(get("inquiry.template") || "")}</textarea></div>
          <div class="field"><label class="small muted">预览（型号 STM32F405RGT6，数量 5000）</label><div class="preview" id="tplPreview"></div></div>
        </div>
      </div>
      <div class="card">
        <div class="card-h"><h3>名片</h3><span class="small muted">关闭发送后仍保留图片</span></div>
        <div class="card-b">
          ${sw("inquiry.send_card")}<div style="height:16px"></div>
          ${get("inquiry.card_image") ? `<img class="card-img" src="/api/card?t=${Date.now()}" alt="名片">` : empty("还没有设置名片", "card")}
          <div class="row" style="margin-top:12px"><label class="btn btn-sm">${icon("upload", 15)}更换名片<input type="file" id="cardFile" accept="image/*" hidden></label>
            <span class="small muted">${esc(get("inquiry.card_image") || "")}</span></div>
        </div>
      </div>
      <div class="card">
        <div class="card-h"><h3>询价策略</h3></div>
        <div class="card-b grid g2">${["inquiry.target_quotes", "inquiry.first_batch", "inquiry.next_batch", "inquiry.wave_wait_minutes", "inquiry.max_suppliers", "inquiry.max_wait_minutes", "inquiry.skip_recent_hours"].map(num).join("")}</div>
      </div>
      <div class="card">
        <div class="card-h"><h3>发送节奏</h3><span class="small muted">保守限速，无法保证不触发第三方风控</span></div>
        <div class="card-b grid g2">${["pacing.min_interval", "pacing.max_interval", "pacing.max_per_hour"].map(num).join("")}</div>
      </div>
      <div class="card">
        <div class="card-h"><h3>操作方式与供应商筛选</h3></div>
        <div class="card-b" style="display:grid;gap:14px">${sw("desktop.foreground_assist")}<p class="small muted">默认后台模式只使用 QQ 提供的无障碍接口。首次唤起新会话、图片或不支持后台的控件需要前台协助；开启后仍不会抢回焦点。其他软件可正常使用，待处理步骤保留。</p>${sw("filter.require_enough_stock")}${sw("filter.only_today")}${num("filter.max_pages")}</div>
      </div>
      <div class="card">
        <div class="card-h"><h3>DeepSeek 解析</h3><span class="small muted">密钥写在项目文件夹的 .env 文件里</span></div>
        <div class="card-b" style="display:grid;gap:14px">
          <div class="field"><label>${FIELD_META["llm.model"][0]}</label><input class="input mono" data-k="llm.model" value="${esc(get("llm.model") || "")}"></div>
          <div><button class="btn btn-sm" id="btnDs">${icon("link", 15)}测试连接</button></div>
        </div>
      </div>
    </div>
    <div class="row mt" style="justify-content:flex-end;position:sticky;bottom:16px">
      <button class="btn" id="btnReset">${icon("refresh", 16)}放弃修改</button>
      <button class="btn btn-primary btn-lg" id="btnSave">${icon("check")}保存设置</button>
    </div>`;
  const tpl = $("#tplInput");
  const prev = () => ($("#tplPreview").textContent = renderMsg(tpl.value, "STM32F405RGT6", 5000));
  tpl.oninput = prev;
  prev();
  $("#btnReset").onclick = renderSettings;
  $("#btnSave").onclick = saveSettings;
  $("#cardFile").onchange = async (e) => {
    const f = e.target.files[0];
    if (!f) return;
    const fd = new FormData();
    fd.append("file", f);
    try { await api("/api/card", { method: "POST", body: fd }); toast("名片已更换"); renderSettings(); refreshOverview(); } catch (err) { toast(err.message, "bad"); }
  };
  bindCommon();
}

async function saveSettings() {
  const body = {};
  document.querySelectorAll("#view [data-k]").forEach((el) => {
    const [sec, key] = el.dataset.k.split(".");
    body[sec] = body[sec] || {};
    body[sec][key] = el.type === "checkbox" ? el.checked : el.value;
  });
  try {
    await api("/api/settings", { method: "PUT", json: body });
    S.settings = null;
    toast("设置已保存，下次开始询价时生效");
  } catch (e) { toast(e.message, "bad"); }
}

// ---------- 路由 ----------
const TITLES = {
  "#/": ["工作台", "系统状态和最近的询价"],
  "#/new": ["新建询价", "录入型号和数量，系统自动发询价；收回复请点击顶部按钮"],
  "#/tasks": ["询价任务", "每个型号的询价进度和报价"],
  "#/reports": ["报告文件", "导出的 Excel 报价报告"],
  "#/log": ["运行日志", ""],
  "#/incidents": ["运行问题", ""],
  "#/settings": ["设置", "话术、名片和询价参数"],
};

function route() {
  const h = location.hash || "#/";
  renderNav();
  const m = h.match(/^#\/tasks\/(\d+)$/);
  const modelMatch = h.match(/^#\/models\/(.+)$/);
  const [title, sub] = modelMatch ? ["型号详情", "供应商回复与历史询价"] : m ? ["任务详情", ""] : TITLES[h] || TITLES["#/"];
  $("#pageTitle").textContent = title;
  $("#pageSub").textContent = sub;
  document.title = `${title} · 天芯智能询价系统`;
  window.scrollTo(0, 0);
  if (modelMatch) { const part = decodeURIComponent(modelMatch[1]); renderTaskDetail("model:" + part, false, part); }
  else if (m) renderTaskDetail(+m[1]);
  else if (h === "#/new") renderNew();
  else if (h === "#/tasks") renderTasks();
  else if (h === "#/reports") renderReports();
  else if (h === "#/log") renderLog();
  else if (h === "#/incidents") renderIncidents();
  else if (h === "#/settings") renderSettings();
  else renderDashboard(true);
}

window.addEventListener("hashchange", route);
setInterval(refreshOverview, 3000);
setInterval(() => { if ($("#view").dataset.page === "log") pollLog(); }, 1500);
setInterval(() => {
  const m = (location.hash || "").match(/^#\/tasks\/(\d+)$/);
  if (document.querySelector("#conversationOverlay") || document.activeElement?.matches("textarea,input")) return;
  const modelMatch = (location.hash || "").match(/^#\/models\/(.+)$/);
  if (modelMatch) { const part = decodeURIComponent(modelMatch[1]); renderTaskDetail("model:" + part, true, part); }
  else if (m) renderTaskDetail(+m[1], true);
}, 8000);
refreshOverview().then(route);
