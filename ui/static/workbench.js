// 型号聚合和会话面板。刷新只更新消息区，保留用户正在输入的草稿。
const replyDrafts = new Map();
let activeConversation = null;
let conversationTimer = null;
let conversationLoading = false;
const OUTBOX_TEXT = {
  queued: "等待发送", deferred: "已暂停，尚未发送", sending: "正在发送，请勿重复提交",
  sent: "已发送到 QQ", failed: "未发送，请检查", uncertain: "结果待核实，请查看 QQ，勿重复发送", cancelled: "已取消",
};

async function renderModels() {
  const view = $("#view");
  view.dataset.page = "models";
  view.innerHTML = `<div class="card fade">
    <div class="card-h wrap"><h3>按型号查看</h3><span class="small muted">同一型号合并展示，保留每次询价记录</span><div class="spacer"></div>
      <label class="switch small"><input type="checkbox" id="showDry" ${S.showDry ? "checked" : ""}><i></i>显示试运行</label></div>
    <div class="card-b"><input class="input mono" id="modelSearch" placeholder="搜索型号，例如 STM32F405" value="${esc(S.modelSearch || "")}" aria-label="搜索型号"></div>
    <div id="modelsTable"></div></div>`;
  let version = 0;
  const load = async () => {
    const v = ++version;
    try {
      const list = await api(`/api/models?dry=${S.showDry ? 1 : 0}&q=${encodeURIComponent(S.modelSearch || "")}`);
      if (v !== version || !$("#modelsTable")) return;
      $("#modelsTable").innerHTML = list.length ? `<div class="table-wrap"><table class="t">
        <thead><tr><th>型号</th><th>最近数量</th><th>进度</th><th>供应商</th><th>已回复</th><th>有效报价</th><th>询价记录</th><th>最近创建</th><th></th></tr></thead>
        <tbody>${list.map(t => `<tr class="click" data-href="#/models/${encodeURIComponent(t.part_no)}">
          <td class="mono"><b>${esc(t.part_no)}</b></td><td>${fmtQty(t.qty)}</td><td>${taskChip(t)}</td>
          <td>${t.sent} 家</td><td>${t.replied} 家</td><td>${t.valid} 条</td><td>${t.runs} 次</td>
          <td class="small">${fmtTime(t.created_at)}</td><td>${icon("arrow")}</td></tr>`).join("")}</tbody></table></div>`
        : empty(S.modelSearch ? "没有匹配的型号" : "还没有询价记录");
      bindCommon();
    } catch (e) { if ($("#modelsTable")) $("#modelsTable").innerHTML = empty(e.message, "alert"); }
  };
  let debounce;
  $("#modelSearch").oninput = e => {
    S.modelSearch = e.target.value;
    clearTimeout(debounce);
    debounce = setTimeout(load, 180);
  };
  $("#showDry").onchange = e => { S.showDry = e.target.checked; load(); };
  await load();
}

function conversationButton(s) {
  return `<button class="btn btn-sm mt" data-conversation="${s.id}">${icon("chat", 15)}查看会话 / 回复</button>`;
}

function bindConversations() {
  document.querySelectorAll("[data-conversation]").forEach(button => {
    button.onclick = () => openConversation(+button.dataset.conversation);
  });
}

function closeConversation() {
  clearInterval(conversationTimer);
  activeConversation = null;
  $("#conversationOverlay")?.remove();
}

async function openConversation(sid) {
  closeConversation();
  activeConversation = sid;
  const draft = replyDrafts.get(sid) || { text: "", requestId: crypto.randomUUID(), sending: false };
  replyDrafts.set(sid, draft);
  const overlay = document.createElement("div");
  overlay.id = "conversationOverlay";
  overlay.className = "conversation-overlay";
  overlay.innerHTML = `<section class="conversation-panel" role="dialog" aria-modal="true" aria-label="供应商会话">
    <div class="card-h"><div><h3 id="conversationTitle">正在加载会话…</h3><div class="small muted" id="conversationBinding"></div></div>
      <div class="spacer"></div><button class="btn btn-icon" id="closeConversation" aria-label="关闭">${icon("x")}</button></div>
    <div class="conversation-scroll">
      <div id="conversationMessages"></div>
      <div id="unassignedMessages"></div>
      <div class="row between mt"><b>DeepSeek 回复建议</b><button class="btn btn-sm" id="refreshSuggestions">生成 / 更新建议</button></div>
      <div id="replySuggestions" class="suggestions"></div>
      <div id="replyOutbox"></div>
    </div>
    <div class="reply-composer"><label for="replyText"><b>回复内容</b><span class="small muted"> · 选择建议可编辑，也可直接输入</span></label>
      <textarea class="input mt" id="replyText" maxlength="2000" placeholder="输入给这家供应商的回复…">${esc(draft.text)}</textarea>
      <div class="row between mt"><span class="small muted">仅发送回复正文，不自动添加型号；收件人为上方绑定的 QQ 会话</span>
        <button class="btn btn-primary" id="sendReply">发送到 QQ</button></div><div class="small" id="replyNotice"></div></div>
    </section>`;
  document.body.appendChild(overlay);
  $("#closeConversation").onclick = closeConversation;
  overlay.addEventListener("click", e => { if (e.target === overlay) closeConversation(); });
  $("#replyText").oninput = e => {
    draft.text = e.target.value;
    if (!draft.sending) draft.requestId = crypto.randomUUID();
  };
  $("#refreshSuggestions").onclick = async () => {
    try { await api(`/api/suppliers/${sid}/suggestions`, { json: {} }); await refreshConversation(); }
    catch (e) { toast(e.message, "bad"); }
  };
  $("#sendReply").onclick = async () => {
    if (draft.sending) return;
    const text = $("#replyText").value.trim();
    if (!text) { toast("请先填写回复", "bad"); return; }
    draft.sending = true;
    $("#sendReply").disabled = true;
    $("#replyText").disabled = true;
    try {
      const item = await api(`/api/suppliers/${sid}/reply`, { json: { text, request_id: draft.requestId, confirm_send: true } });
      draft.text = "";
      draft.requestId = crypto.randomUUID();
      if (activeConversation === sid) $("#replyText").value = "";
      toast(OUTBOX_TEXT[item.status] || "已加入发送队列");
    } catch (e) {
      // 网络响应丢失时复用同一 requestId，服务器不会重复发送。
      toast(e.message, "bad");
    } finally {
      draft.sending = false;
      if (activeConversation === sid) { $("#replyText").disabled = false; await refreshConversation(); }
    }
  };
  await refreshConversation();
  conversationTimer = setInterval(refreshConversation, 4000);
}

async function refreshConversation() {
  const sid = activeConversation;
  if (!sid || conversationLoading) return;
  conversationLoading = true;
  try {
    const d = await api(`/api/suppliers/${sid}/conversation`);
    if (sid !== activeConversation) return;
    $("#conversationTitle").textContent = `${d.supplier.company} · ${d.supplier.part_no}`;
    $("#conversationBinding").textContent = `QQ 会话：${d.supplier.chat_name || "尚未绑定"} · 需求 ${fmtQty(d.supplier.qty)} pcs`;
    $("#conversationMessages").innerHTML = d.messages.length ? d.messages.map(m =>
      `<div class="chat-bubble ${m.direction === "out" ? "mine" : ""}"><div class="small muted">${m.direction === "out" ? "我方" : "供应商"} · ${esc(m.message_time || fmtTime(m.seen_at))}</div><div>${esc(m.content)}</div></div>`).join("") : empty("暂无已保存的聊天记录", "chat");
    $("#unassignedMessages").innerHTML = d.unassigned.length ? `<div class="notice mt"><div><b>待确认归属的回复</b><p>此 QQ 会话涉及多个型号，确认后再关联。</p>
      ${d.unassigned.map(m => `<div class="unassigned"><p>${esc(m.content)}</p><button class="btn btn-sm" data-assign="${m.id}">关联到 ${esc(d.supplier.part_no)}</button></div>`).join("")}</div></div>` : "";
    const sug = d.suggestions;
    $("#replySuggestions").innerHTML = sug.items.length ? sug.items.map((text, i) =>
      `<button class="suggestion" data-suggestion="${i}"><span class="small muted">建议 ${i + 1}</span><span>${esc(text)}</span></button>`).join("")
      : `<div class="small muted">${sug.status === "queued" ? "DeepSeek 正在生成，原始回复已保存…" : sug.status === "failed" ? "建议生成失败，可重试或直接手动回复。" : "收到回复后自动生成；历史回复可点击上方按钮生成。"}</div>`;
    $("#replyOutbox").innerHTML = d.outbox.length ? `<div class="mt"><b>发送记录</b>${d.outbox.map(m =>
      `<div class="outbox-item"><div>${esc(m.content)}</div><div class="row wrap small"><b>${esc(OUTBOX_TEXT[m.status] || m.status)}</b><span class="muted">${fmtTime(m.updated_at)}</span>
      ${m.status === "deferred" ? `<button class="btn btn-sm" data-retry="${m.id}">继续发送</button>` : ""}
      ${["queued", "deferred"].includes(m.status) ? `<button class="btn btn-sm" data-cancel="${m.id}">取消</button>` : ""}</div>
      ${m.error ? `<div class="small err">${esc(m.error)}</div>` : ""}</div>`).join("")}</div>` : "";
    const blocked = d.outbox.some(m => ["queued", "deferred", "sending", "uncertain"].includes(m.status));
    $("#sendReply").disabled = !d.supplier.sent_at || !d.supplier.qq_uin || blocked || replyDrafts.get(sid).sending;
    $("#replyNotice").textContent = blocked ? "请先处理上方待发送或待核实记录。" : "";
    document.querySelectorAll("[data-suggestion]").forEach(b => b.onclick = () => {
      const draft = replyDrafts.get(sid);
      if (draft.sending) return;
      draft.text = sug.items[+b.dataset.suggestion];
      draft.requestId = crypto.randomUUID();
      $("#replyText").value = draft.text;
      $("#replyText").focus();
    });
    for (const action of ["assign", "retry", "cancel"]) {
      document.querySelectorAll(`[data-${action}]`).forEach(b => b.onclick = async () => {
        b.disabled = true;
        try {
          const url = action === "assign" ? `/api/inbox/${b.dataset.assign}/assign` : `/api/outbox/${b.dataset[action]}/${action}`;
          await api(url, { json: action === "assign" ? { supplier_id: sid } : {} });
          await refreshConversation();
        } catch (e) { toast(e.message, "bad"); b.disabled = false; }
      });
    }
  } catch (e) { if (sid === activeConversation) toast(e.message, "bad"); }
  finally { conversationLoading = false; }
}

window.addEventListener("keydown", e => { if (e.key === "Escape") closeConversation(); });
window.addEventListener("hashchange", closeConversation);
