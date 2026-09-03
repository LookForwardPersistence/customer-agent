// Behaviour-level XSS harness: executes the real app/static/app.js against a
// minimal DOM shim, feeds it a malicious server response and inspects the
// resulting DOM. Usage: node dom_harness.js <path-to-app.js>
// Prints a JSON verdict; exit code 1 on violations.

const fs = require('fs');
const vm = require('vm');

const appJs = fs.readFileSync(process.argv[2], 'utf8');

// -- records collected while the app runs -----------------------------------
const innerHTMLAssignments = []; // every innerHTML= the app performs
const textContentValues = [];    // every textContent= the app performs

class ClassList {
  constructor() { this.s = new Set(); }
  toggle(c) { this.s.has(c) ? this.s.delete(c) : this.s.add(c); return this.s.has(c); }
  add(c) { this.s.add(c); }
}

class El {
  constructor(tag) {
    this.tagName = String(tag || 'div').toUpperCase();
    this.children = [];
    this.attributes = {};      // setAttribute-style props (style etc.)
    this.style = {};
    this.classList = new ClassList();
    this.disabled = false;
    this.value = '';
    this.parentElement = null;
    this.onclick = null;       // must stay a function, never a string
  }
  get textContent() { return this._textContent || ''; }
  set textContent(v) { this._textContent = String(v); textContentValues.push(String(v)); this.children = []; }
  get innerHTML() { return this._innerHTML || ''; }
  set innerHTML(v) { this._innerHTML = String(v); innerHTMLAssignments.push(String(v)); }
  appendChild(c) { this.children.push(c); c.parentElement = this; return c; }
  append(...cs) { cs.forEach(c => this.appendChild(c)); }
  setAttribute(k, v) { this.attributes[k] = String(v); }
  getAttribute(k) { return this.attributes[k]; }
  addEventListener() {}
  querySelectorAll(sel) { // only used with 'button' by app.js
    const out = [];
    const want = sel.toUpperCase();
    const walk = e => (e.children || []).forEach(c => { if (c.tagName === want) out.push(c); walk(c); });
    walk(this);
    return out;
  }
  remove() {}
  focus() {}
  get scrollTop() { return 0; }
  set scrollTop(_) {}
  get scrollHeight() { return 0; }
}

const byId = {};
const documentShim = {
  getElementById(id) { if (!byId[id]) byId[id] = new El('div'); return byId[id]; },
  createElement(t) { return new El(t); },
  createTextNode(t) { const n = new El('#text'); n.textContent = t; return n; },
};

// newSession() at load time calls fetch; stub it benignly.
const fetchShim = () => Promise.resolve({
  ok: true,
  json: async () => ({ session_id: 'S1', token: 'T', customer_id: 'CUST-001' }),
});

const ctx = {
  document: documentShim,
  fetch: fetchShim,
  console,
  setTimeout, clearTimeout,
};
vm.createContext(ctx);
vm.runInContext(appJs, ctx, { filename: 'app.js' });

// -- malicious server payload ----------------------------------------------
// Everything the server might return is treated as hostile: script tags, img
// onerror handlers, javascript: URLs, event-handler-looking strings.
const EVIL = {
  reply: 'hello <img src=x onerror="alert(1)"> <script>alert(2)</script>',
  sources: [{ id: 'KB-001<img src=x onerror=alert(3)>', topic: '<script>alert(4)</script>' }],
  pending_action: {
    action_id: 'A1<script>',
    order_id: 'AT-10092" onmouseover="alert(5)',
    items: ['<b>bold</b><img src=x onerror=alert(6)>'],
    refund_amount: 499,
    policy: '<script>alert(7)</script> javascript:void(0)',
  },
  handoff: {
    id: 'HO-<img onerror=alert(8)>',
    status: '<script>alert(9)</script>',
    reason: '<img src=x onerror=alert(10)>',
    summary: '<script>alert(11)</script>',
    payload: {
      intent: '<script>alert(12)</script>',
      order_ids: ['<img onerror=alert(13)>'],
      customer_sentiment: '<script>alert(14)</script>',
      attempts: [{ event: 'return_proposed' }],
      last_error: { code: '<img onerror=alert(15)>' },
    },
  },
  trace: [
    { type: 'tool_call', name: '<script>alert(16)</script>', detail: 'a<b onmouseover=alert(17)>' },
    { type: 'tool_result', name: '<img src=x onerror=alert(18)>', detail: 'x<img src=y onerror=alert(19)>' },
  ],
};

ctx.handleResponse(EVIL).then(() => {
  const violations = [];

  for (const html of innerHTMLAssignments) {
    // Static allow-list: the typing indicator and the customer welcome map.
    const isStaticTyping = html.includes('class="typing"');
    const isStaticWelcome = html.includes('当前演示账号为');
    if (isStaticTyping || isStaticWelcome) continue;
    violations.push(`dynamic innerHTML: ${html.slice(0, 120)}`);
  }

  // Anything that parses markup could turn these into executable nodes; as
  // text they are inert. If they appear only in textContent records, the
  // payload was neutralised.
  const sawEvilAsText = textContentValues.some(v => v.includes('<img src=x onerror'));
  if (!sawEvilAsText) violations.push('evil payload never rendered as text (render path changed?)');

  // Event handlers must be functions, never attacker-controlled strings.
  const walk = e => {
    (e.children || []).forEach(c => {
      if (typeof c.onclick === 'string') violations.push(`string onclick: ${c.onclick.slice(0, 80)}`);
      walk(c);
    });
  };
  for (const el of Object.values(byId)) walk(el);

  console.log(JSON.stringify({
    violations,
    innerHTMLAssignments,
    sawEvilAsText,
    textContentCount: textContentValues.length,
  }));
  process.exit(violations.length ? 1 : 0);
}).catch(e => {
  console.error('harness crashed:', e);
  process.exit(2);
});
