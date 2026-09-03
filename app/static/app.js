// rawTrace=false: tool payloads are masked by default (they may contain order
// ids, addresses and free-text reasons). Opt in locally to inspect raw values.
const state = { sessionId: null, token: null, customerId: null, busy: false, rawTrace: false };
const CUSTOMER_ORDERS = {
  'CUST-001': '当前演示账号为「陈先生」，名下订单：<b>AT-10086</b>（已发货）、<b>AT-10092</b>（待发货）。',
  'CUST-002': '当前演示账号为「李女士」，名下订单：<b>AT-10077</b>（已签收，定制商品）。',
  'CUST-003': '当前演示账号为「赵先生」，名下订单：<b>AT-10099</b>（已签收）。',
  'CUST-004': '当前演示账号为「王先生」，名下订单：<b>AT-10050</b>（已签收，退货处理中）。'
};
const msgs = document.getElementById('messages');
const input = document.getElementById('input');
const send = document.getElementById('send');

function scrollBottom() { msgs.scrollTop = msgs.scrollHeight; }

function addBubble(role, text) {
  const row = document.createElement('div');
  row.className = 'row ' + role;
  if (role === 'bot') {
    const av = document.createElement('div'); av.className = 'avatar bot'; av.textContent = '极';
    row.appendChild(av);
  }
  const b = document.createElement('div'); b.className = 'bubble'; b.textContent = text;
  row.appendChild(b);
  msgs.appendChild(row); scrollBottom();
  return b.parentElement;
}

function addMetaSources(rowEl, sources) {
  if (!sources || !sources.length) return;
  const meta = document.createElement('div'); meta.className = 'meta';
  meta.style.justifyContent = 'flex-end'; meta.style.maxWidth = '78%';
  const label = document.createElement('span'); label.textContent = '依据来源：';
  meta.appendChild(label);
  sources.forEach(s => {
    const chip = document.createElement('span'); chip.className = 'src-chip';
    chip.textContent = `${s.id} ${s.topic}`; meta.appendChild(chip);
  });
  rowEl.appendChild(meta); scrollBottom();
}

function showTyping() {
  const row = document.createElement('div'); row.className = 'row bot'; row.id = 'typingRow';
  // Decorative animation — keep it out of the screen-reader announcement;
  // the real reply lands in the aria-live log and gets announced there.
  row.setAttribute('aria-hidden', 'true');
  const av = document.createElement('div'); av.className = 'avatar bot'; av.textContent = '极';
  const b = document.createElement('div'); b.className = 'bubble';
  b.innerHTML = '<span class="typing"><i></i><i></i><i></i></span>';
  row.append(av, b); msgs.appendChild(row); scrollBottom();
}
function hideTyping() { const t = document.getElementById('typingRow'); if (t) t.remove(); }

// ---- sanitization -------------------------------------------------------
// Nothing that comes from the model, the user or the backend is ever handed to
// innerHTML. Values become text nodes; structure is built with DOM nodes.
function maskSensitive(text) {
  return String(text == null ? '' : text)
    // order ids: keep the prefix and last 2 chars
    .replace(/AT-\d+/g, m => 'AT-***' + m.slice(-2))
    // phone numbers
    .replace(/1[3-9]\d{9}/g, '1**********')
    // emails
    .replace(/[\w.+-]+@[\w-]+\.[\w.]+/g, '***@***.***')
    // inline event handlers / script payloads, neutralised for display
    .replace(/</g, '‹');
}

function renderPending(rowEl, pending) {
  const card = document.createElement('div');
  card.className = 'action-card';

  const h = document.createElement('h4');
  h.textContent = '⚠️ 退货申请待确认';
  card.appendChild(h);

  const detail = document.createElement('div');
  detail.className = 'detail';
  const line = (label, value, strong) => {
    const p = document.createElement('div');
    p.appendChild(document.createTextNode(label));
    const v = document.createElement(strong ? 'b' : 'span');
    v.textContent = value;
    p.appendChild(v);
    detail.appendChild(p);
  };
  line('订单：', pending.order_id, true);
  line('商品：', (pending.items || []).join('、'));
  line('退款金额：', '¥' + pending.refund_amount, true);
  if (pending.policy) {
    const p = document.createElement('div');
    p.className = 'policy';
    p.textContent = pending.policy;
    detail.appendChild(p);
  }
  card.appendChild(detail);

  const btns = document.createElement('div');
  btns.className = 'btns';
  ['confirm', 'cancel'].forEach(act => {
    const btn = document.createElement('button');
    btn.className = 'btn ' + (act === 'confirm' ? 'primary' : 'ghost');
    btn.textContent = act === 'confirm' ? '确认退货' : '取消';
    btn.onclick = () => {
      // Disable immediately: a double click must not fire two requests.
      btns.querySelectorAll('button').forEach(b => b.disabled = true);
      resolveAction(act, pending.action_id);
    };
    btns.appendChild(btn);
  });
  card.appendChild(btns);

  rowEl.appendChild(card); scrollBottom();
}

function renderHandoff(handoff) {
  const el = document.createElement('div');
  el.className = 'handoff';
  // assertive: a handoff changes what the customer must do next — announce
  // it immediately rather than waiting for the polite queue.
  el.setAttribute('role', 'alert');

  const head = document.createElement('b');
  head.textContent = `转人工客服 · ${handoff.id}（${handoff.status}）`;
  el.appendChild(head);

  const reason = document.createElement('div');
  reason.textContent = '转接原因：' + (handoff.reason || '');
  el.appendChild(reason);

  const p = handoff.payload || {};
  if (handoff.summary || p.order_ids || p.attempts) {
    const ctx = document.createElement('div');
    ctx.className = 'ctx';
    const lines = ['📋 已转交上下文：'];
    lines.push('客户诉求：' + (p.intent || handoff.reason || '—'));
    lines.push('涉及订单：' + ((p.order_ids || []).join('、') || '—'));
    lines.push('客户情绪：' + (p.customer_sentiment || '—'));
    lines.push('已尝试处理：' + ((p.attempts || []).length ? (p.attempts.length + ' 步') : '0 步'));
    if (p.last_error) lines.push('最后错误：' + (p.last_error.code || p.last_error.event));
    if (handoff.summary) lines.push('摘要：' + handoff.summary);
    ctx.textContent = lines.join('\n');
    el.appendChild(ctx);
  }
  msgs.appendChild(el); scrollBottom();
}

function renderTrace(trace) {
  const list = document.getElementById('traceList');
  list.textContent = '';
  if (!trace || !trace.length) {
    list.textContent = '本轮无工具调用（纯对话回答）。';
    return;
  }
  trace.forEach(t => {
    const item = document.createElement('div');
    item.className = 'trace-item' + (t.type === 'tool_result' ? ' result' : '');

    const tag = document.createElement('span');
    tag.className = 't';
    tag.textContent = t.type === 'tool_call' ? '→ 调用' : '← 结果';
    const name = document.createElement('span');
    name.textContent = ' ' + t.name;
    item.append(tag, name, document.createElement('br'));

    const d = document.createElement('span');
    // Tool args/results are masked by default: they can contain order ids,
    // addresses and free-text reasons typed by the user.
    d.textContent = (state.rawTrace ? String(t.detail || '') : maskSensitive(t.detail));
    item.appendChild(d);
    list.appendChild(item);
  });
}

async function post(url, body) {
  const headers = {'Content-Type': 'application/json'};
  // Bearer token: session identity comes from the header, never the body.
  if (state.token) headers['Authorization'] = 'Bearer ' + state.token;
  const res = await fetch(url, {
    method: 'POST', headers: headers,
    body: JSON.stringify(body)
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      const d = body.detail;
      detail = (d && typeof d === 'object') ? (d.message || d.reason || JSON.stringify(d)) : (d || detail);
    } catch (_) { /* keep statusText */ }
    const err = new Error(detail);
    err.status = res.status;
    throw err;
  }
  return res.json();
}

async function handleResponse(data) {
  hideTyping();
  const rowEl = addBubble('bot', data.reply || '（无回复）');
  addMetaSources(rowEl, data.sources);
  if (data.pending_action) renderPending(rowEl, data.pending_action);
  if (data.handoff) renderHandoff(data.handoff);
  renderTrace(data.trace);
}

async function sendMsg(text) {
  if (state.busy || !text.trim()) return;
  state.busy = true; send.disabled = true;
  addBubble('user', text);
  input.value = '';
  showTyping();
  try {
    const data = await post('/api/chat', { message: text });
    await handleResponse(data);
  } catch (e) {
    hideTyping();
    if (e.status === 401) {
      addBubble('bot', '会话已过期，正在为您重新建立会话……');
      await newSession(state.customerId || 'CUST-001');
      return;
    }
    addBubble('bot', '抱歉，系统开小差了：' + e.message + '。请稍后重试，或点击右上角联系人工客服。');
  } finally {
    state.busy = false; send.disabled = false; input.focus();
  }
}

async function resolveAction(act, actionId) {
  if (state.busy) return;
  if (!actionId) {
    addBubble('bot', '该操作缺少标识，无法提交。请重新发起退货申请。');
    return;
  }
  state.busy = true; send.disabled = true;
  showTyping();
  try {
    // The action id binds the click to the exact proposal the user was shown,
    // so a stale card cannot trigger a different action.
    const data = await post(`/api/session/${act}`, { action_id: actionId });
    await handleResponse(data);
  } catch (e) {
    hideTyping();
    if (e.status === 409) {
      addBubble('bot', '这个操作已经失效或被处理过了（' + e.message + '）。请重新发起退货申请，确认前请核对订单与金额。');
    } else {
      addBubble('bot', '操作失败：' + e.message);
    }
  } finally {
    state.busy = false; send.disabled = false; input.focus();
  }
}

send.onclick = () => sendMsg(input.value);
input.addEventListener('keydown', e => { if (e.key === 'Enter') sendMsg(input.value); });
document.getElementById('quick').addEventListener('click', e => {
  if (e.target.tagName === 'BUTTON') sendMsg(e.target.textContent);
});
document.getElementById('traceToggle').addEventListener('click', e => {
  const open = document.getElementById('tracePanel').classList.toggle('open');
  e.target.classList.toggle('active');
  // aria-pressed mirrors the panel state for assistive tech.
  e.target.setAttribute('aria-pressed', open ? 'true' : 'false');
});

async function newSession(customerId) {
  const res = await fetch('/api/session/new', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ customer_id: customerId })
  });
  const data = await res.json();
  state.sessionId = data.session_id;
  state.token = data.token;
  state.customerId = data.customer_id;
  const wc = document.getElementById('welcomeCustomer');
  if (wc) wc.innerHTML = CUSTOMER_ORDERS[customerId] || '';
}

document.getElementById('customerSel').addEventListener('change', async e => {
  // Switching demo customers starts a fresh, separately-scoped session.
  await newSession(e.target.value);
  addBubble('bot', '已切换演示客户并创建新会话。当前账号：' + e.target.selectedOptions[0].textContent);
});

newSession('CUST-001');
