"use strict";

/* 观众字幕（执行台页）前端回归测试（纯 Node，无第三方依赖）。
 *
 * 通过 vm 加载真实的 app/static/app.js，用迷你 DOM（与 auto.test.js 同一套）
 * 模拟页面，用可控 fetch 代替网络。
 *
 * 覆盖：待发提示出现「字幕」入口（控制席可用、只读禁用）；展开设置表单并
 * 提交 POST /api/subtitles（cue_id、version、文本，空内容为 null 清除；
 * 超长拦截不发请求）；成功后待发行显示 字幕 ✓ / 字幕已失效 标记；只读页
 * 不能打开表单；请求在途禁用；画面状态条随 state.screen 显示字幕 / 清屏与
 * 跨场画面序号，开场前隐藏；轮询同步；409 刷新；403 转只读；清除设置请求
 * 体为 null。
 *
 * 运行：node tests/subtitles.test.js
 */

const fs = require("fs");
const path = require("path");
const vm = require("vm");

/* ------------------------------------------------------------ 迷你 DOM */

/* 迷你 DOM：不做布局，但会解析被测代码关心的 innerHTML 片段——
 * verify-card 的回报按钮（<button data-action data-dept>）与设置页的
 * .cue-row 录入行，使测试能检查**真实渲染出来的 disabled 状态**、并通过
 * 真实元素派发点击（不绕过按钮禁用）。 */

class ClassList {
  constructor() {
    this.set = new Set();
  }
  add(c) { this.set.add(c); }
  remove(c) { this.set.delete(c); }
  contains(c) { return this.set.has(c); }
  toggle(c, force) {
    const want = force === undefined ? !this.set.has(c) : !!force;
    if (want) this.set.add(c);
    else this.set.delete(c);
    return want;
  }
}

/* 仅解析测试需要的少量标签/属性：button、input、textarea、div、span 等。 */
const VOID_TAGS = new Set(["input", "br", "img"]);

function parseAttrs(attrText, el) {
  // 属性名允许连字符（class="subtitle-edit"、data-action 等）：旧正则把
  // 连字符当成属性名结束，导致含连字符的 class / data-* 值被截断。
  const attrRe = /([a-zA-Z_:][-a-zA-Z0-9_:.]*)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g;
  let m;
  while ((m = attrRe.exec(attrText))) {
    el.setAttribute(m[1], m[2] ?? m[3] ?? m[4] ?? "");
  }
}

/* 小型 HTML 解析：开标签压栈、闭标签弹栈（容忍嵌套）、文本挂到栈顶。
 * 不做完整 HTML 语义，只为让 verify-card 的按钮与设置页录入行成为可检查
 * disabled、可派发点击的真实元素。 */
function parseHtml(ownerDoc, html, parent = null) {
  const roots = [];
  const stack = [];
  const tokenRe =
    /<!--[\s\S]*?-->|<([a-zA-Z][-a-zA-Z0-9]*)([^>]*?)(\/?)>|<\/([a-zA-Z][-a-zA-Z0-9]*)\s*>|([^<]+)/g;
  let m;
  while ((m = tokenRe.exec(html))) {
    if (m[1] !== undefined) {
      const tag = m[1].toLowerCase();
      const el = new El(tag);
      el.ownerDocument = ownerDoc;
      const top = stack[stack.length - 1];
      el.parentElement = top || parent;
      if (top) top.children.push(el);
      else roots.push(el);
      parseAttrs(m[2], el);
      // textarea 的内容是原始文本（含中文、空白与未转义字符），直接作为
      // value，不继续按嵌套标签解析。
      if (tag === "textarea") {
        const close = html.toLowerCase().indexOf("</textarea>", tokenRe.lastIndex);
        const raw = close === -1 ? html.slice(tokenRe.lastIndex)
          : html.slice(tokenRe.lastIndex, close);
        el._value = raw;
        tokenRe.lastIndex = close === -1 ? html.length : close + "</textarea>".length;
      } else if (!(m[3] === "/" || VOID_TAGS.has(tag))) {
        stack.push(el);
      }
    } else if (m[4] !== undefined) {
      const tag = m[4].toLowerCase();
      for (let i = stack.length - 1; i >= 0; i--) {
        if (stack[i].tagName === tag.toUpperCase()) {
          stack.length = i; // 弹出匹配层及其内未闭合标签（容错）
          break;
        }
      }
    } else if (m[5] !== undefined) {
      const top = stack[stack.length - 1];
      if (top) top._text += m[5];
    }
  }
  return roots;
}

class El {
  constructor(tag = "div", id = "") {
    this.tagName = tag.toUpperCase();
    this.id = id;
    this.classList = new ClassList();
    this.listeners = {};
    this.children = [];
    this.dataset = {};
    this._attrs = {};
    this._value = "";
    this.disabled = false;
    this._text = "";
    this.parentElement = null;
    this.ownerDocument = null;
    if (id) this._attrs.id = id;
  }
  get value() { return this._value; }
  /* 与真实 DOM 一致：input.value 读出总是字符串（数值赋值会被字符串化）。 */
  set value(v) { this._value = String(v); }
  get className() { return [...this.classList.set].join(" "); }
  set className(v) {
    this.classList = new ClassList();
    String(v).split(/\s+/).filter(Boolean).forEach((c) => this.classList.add(c));
    this._attrs.class = String(v);
  }
  get textContent() {
    if (this.children.length) {
      return this.children.map((c) => c.textContent).join("") + this._text;
    }
    return this._text;
  }
  set textContent(v) {
    this.children = [];
    this._text = String(v);
    if (this.tagName === "BUTTON" || this.tagName === "SPAN" || this.tagName === "DIV") {
      // 被测代码用 textContent 改按钮文案：保持在本元素上
    }
  }
  get innerHTML() { return this._innerHTML || ""; }
  set innerHTML(html) {
    this._innerHTML = String(html);
    this.children = parseHtml(this.ownerDocument, String(html), this);
  }
  setAttribute(name, val) {
    this._attrs[name] = String(val);
    if (name === "id") this.id = String(val);
    if (name === "class") {
      this.classList = new ClassList();
      String(val).split(/\s+/).filter(Boolean).forEach((c) => this.classList.add(c));
    }
    if (name.startsWith("data-")) {
      this.dataset[name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = String(val);
    }
    if (name === "disabled") this.disabled = true;
    if (name === "value") this._value = String(val);
  }
  getAttribute(name) {
    return Object.prototype.hasOwnProperty.call(this._attrs, name) ? this._attrs[name] : null;
  }
  hasAttribute(name) {
    return Object.prototype.hasOwnProperty.call(this._attrs, name) ||
      (name === "disabled" ? this.disabled : false);
  }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  dispatch(type, event = {}) {
    for (const fn of this.listeners[type] || []) fn(event);
  }
  /* 单个复合选择器（无后代组合符），如 button.small[data-action="x"]。 */
  _matchesCompound(part) {
    const tokenRe = /([.#]?[a-zA-Z][-a-zA-Z0-9_]*)|(\[[^\]]+\])/g;
    let t;
    while ((t = tokenRe.exec(part))) {
      if (t[1]) {
        const tok = t[1];
        if (tok.startsWith("#")) { if (this.id !== tok.slice(1)) return false; }
        else if (tok.startsWith(".")) { if (!this.classList.contains(tok.slice(1))) return false; }
        else if (this.tagName !== tok.toUpperCase()) return false;
      } else {
        const am = /^\[([a-zA-Z0-9-]+)(?:([~|^$*]?=)\s*"([^"]*)"|=\s*'([^']*)'|=\s*([^\]]*))?\]$/.exec(t[2]);
        if (!am) return false;
        const av = this.getAttribute(am[1]);
        if (av === null) return false;
        if (am[2]) {
          const want = am[3] ?? am[4] ?? am[5] ?? "";
          if (am[2] === "=" && av !== want) return false;
        }
      }
    }
    return true;
  }
  /* 后代组合选择器：每个空白分隔的复合段表示一层后代。 */
  matches(sel) {
    const parts = sel.split(/\s+/).filter(Boolean);
    if (!parts.length || !this._matchesCompound(parts[parts.length - 1])) return false;
    let el = this.parentElement;
    for (let i = parts.length - 2; i >= 0; i--) {
      let found = null;
      while (el) {
        if (el._matchesCompound(parts[i])) { found = el; break; }
        el = el.parentElement;
      }
      if (!found) return false;
      el = found.parentElement;
    }
    return true;
  }
  _walk(fn) {
    for (const c of this.children) {
      fn(c);
      c._walk(fn);
    }
  }
  querySelectorAll(sel) {
    const parts = sel.split(/\s+/).filter(Boolean);
    const out = [];
    const seen = new Set();
    // 标准后代选择器递归：匹配当前层后既深入找下一层，也在兄弟分支继续找
    // 本层；进入下一层后不再用本层规则重复匹配其后代（避免深度错位）。
    const dfs = (els, depth) => {
      if (depth >= parts.length) return;
      for (const el of els) {
        if (el._matchesCompound(parts[depth])) {
          if (depth === parts.length - 1) {
            if (!seen.has(el)) { seen.add(el); out.push(el); }
            // 单段选择器（无后代组合）：继续在后代里找同层匹配
            if (parts.length === 1) dfs(el.children, depth);
          } else {
            dfs(el.children, depth + 1);
          }
        } else {
          dfs(el.children, depth);
        }
      }
    };
    dfs(this.children, 0);
    return out;
  }
  querySelector(sel) {
    const found = this.querySelectorAll(sel);
    return found.length ? found[0] : new El();
  }
  closest(sel) {
    let el = this;
    while (el) {
      if (el.matches && el.matches(sel)) return el;
      el = el.parentElement;
    }
    return null;
  }
  focus() {}
  appendChild(child) {
    child.parentElement = this;
    child.ownerDocument = this.ownerDocument;
    this.children.push(child);
    return child;
  }
  remove() {
    if (this.parentElement) {
      const arr = this.parentElement.children;
      const i = arr.indexOf(this);
      if (i !== -1) arr.splice(i, 1);
    }
  }
}

class Doc {
  constructor() {
    this.byId = new Map();
    this.listeners = {};
  }
  el(id) {
    if (!this.byId.has(id)) {
      const el = new El("div", id);
      el.ownerDocument = this;
      if (id === "archives-modal") el.classList.add("hidden");
      this.byId.set(id, el);
    }
    return this.byId.get(id);
  }
  querySelector(sel) {
    if (sel.startsWith("#")) return this.el(sel.slice(1));
    // 非 id 选择器：模拟 document.querySelector —— 在各根元素的**后代**
    // （不含根本身，根是测试容器而非真实 <body> 元素）中按创建顺序 DFS。
    for (const root of this.byId.values()) {
      const found = root.querySelectorAll(sel);
      if (found.length) return found[0];
    }
    return new El();
  }
  querySelectorAll(sel) {
    if (sel.startsWith("#")) {
      const idSel = sel.slice(1).split(/\s+/);
      const root = this.el(idSel[0]);
      return idSel.length > 1 ? root.querySelectorAll(idSel.slice(1).join(" ")) : [root];
    }
    const out = [];
    for (const root of this.byId.values()) {
      for (const f of root.querySelectorAll(sel)) {
        if (!out.includes(f)) out.push(f);
      }
    }
    return out;
  }
  createElement(tag) {
    const el = new El(tag);
    el.ownerDocument = this;
    return el;
  }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  dispatch(type, event = {}) {
    for (const fn of this.listeners[type] || []) fn(event);
  }
}


/* -------------------------------------------------------- 加载被测脚本 */

const appCode = fs.readFileSync(
  path.join(__dirname, "..", "app", "static", "app.js"),
  "utf8"
);

function loadApp(storageData) {
  const document = new Doc();
  const pending = [];
  const intervals = [];
  const storage = {
    _m: new Map(Object.entries(storageData || {})),
    getItem(k) { return this._m.has(k) ? this._m.get(k) : null; },
    setItem(k, v) { this._m.set(k, String(v)); },
    removeItem(k) { this._m.delete(k); },
  };
  const sandbox = {
    document,
    fetch: (p, options) =>
      new Promise((resolve, reject) => pending.push({ path: p, options, resolve, reject })),
    sessionStorage: storage,
    setTimeout,
    clearTimeout,
    setInterval: (fn, ms) => { intervals.push({ fn, ms }); return intervals.length; },
    clearInterval: () => {},
    confirm: () => true,
    console,
  };
  vm.createContext(sandbox);
  vm.runInContext(appCode, sandbox, { filename: "app.js" });
  return { document, pending, storage, sandbox, intervals };
}

const flush = () => new Promise((r) => setImmediate(r));
const lastRequest = (ctx) => ctx.pending[ctx.pending.length - 1];

async function respond(req, data, status = 200) {
  req.resolve({ ok: status >= 200 && status < 300, status, json: async () => data });
  await flush();
}
async function pollOnce(ctx) {
  const before = ctx.pending.length;
  ctx.intervals[0].fn();
  await flush();
  return ctx.pending.length > before ? lastRequest(ctx) : null;
}
function clickButton(ctx, btn) { ctx.document.dispatch("click", { target: btn }); }

/* ------------------------------------------------------------ 测试数据 */

function verifyOff() {
  return { enabled: false, round: 0, cue_id: null, lighting: null, sound: null, all_ready: false };
}
function autoView(over) {
  return Object.assign({ enabled: false, interval: 10, remaining_seconds: null, waiting_ready: false }, over || {});
}
function liveState(over) {
  return Object.assign({
    version: 3,
    status: "live",
    paused: false,
    control_enabled: false,
    control_granted: true,
    current: { id: 1, number: "A1", content: "灯光渐亮" },
    next: { id: 2, number: "A2", content: "音乐起" },
    pending: [
      { id: 1, number: "A1", content: "灯光渐亮", subtitle: null, subtitle_valid: false },
      { id: 2, number: "A2", content: "音乐起", subtitle: null, subtitle_valid: false },
    ],
    issued: [],
    issues: [],
    suggestions: [],
    verify: verifyOff(),
    auto: autoView(),
    screen: { subtitle: null, seq: 0 },
    total: 2,
    remaining: 2,
    min_cues: 2,
    max_cues: 12,
  }, over || {});
}
function setupState(over) {
  return liveState(Object.assign({
    status: "setup", current: null, next: null, pending: [],
    issued: [], total: 0, remaining: 0, screen: { subtitle: null, seq: 0 },
  }, over || {}));
}

let failures = 0;
function check(cond, msg) {
  if (cond) console.log("  ✓ " + msg);
  else { failures++; console.error("  ✗ " + msg); }
}

const subBtn = (ctx, id) =>
  ctx.document.querySelectorAll('#pending-list button[data-action="add-subtitle"][data-id="' + id + '"]')[0] || null;


/* ------------------------------------------------------------ 用例 */

async function t1_entryAndPermissions() {
  console.log("F1 字幕入口：控制席可用、只读页禁用；开场前无待发列表");
  const ctx = loadApp();
  await respond(lastRequest(ctx), liveState());
  const b1 = subBtn(ctx, 1);
  check(!!b1 && b1.disabled === false, "可写页字幕入口可用");

  const ro = loadApp();
  await respond(lastRequest(ro), liveState({ control_enabled: true, control_granted: false }));
  const rb1 = subBtn(ro, 1);
  check(!!rb1 && rb1.disabled === true, "只读页字幕入口禁用");
  const before = ro.pending.length;
  clickButton(ro, rb1);
  check(ro.pending.length === before, "只读页点击不展开表单、不发请求");
}

async function t2_setSubtitle() {
  console.log("F2 设置字幕：表单、请求体、成功标记");
  const ctx = loadApp();
  await respond(lastRequest(ctx), liveState());
  clickButton(ctx, subBtn(ctx, 1));
  const form = ctx.document.querySelectorAll('.subtitle-edit[data-id="1"]')[0];
  check(!!form, "点入口展开字幕表单");
  const ta = form.querySelectorAll(".subtitle-input")[0];
  check(ta.value === "", "无设置时输入框为空");
  ta.value = "  观众看到的字幕  ";
  clickButton(ctx, form.querySelectorAll('button[data-action="save-subtitle"]')[0]);
  const req = lastRequest(ctx);
  check(req.path === "/api/subtitles", "提交到 /api/subtitles");
  const body = JSON.parse(req.options.body);
  check(body.cue_id === 1 && body.version === 3 && body.subtitle === "观众看到的字幕",
    "请求携带 cue_id、版本号与去除首尾空白后的字幕文本");
  await respond(req, liveState({
    version: 4,
    current: { id: 1, number: "A1", content: "灯光渐亮", subtitle: "观众看到的字幕", subtitle_valid: true },
    pending: [
      { id: 1, number: "A1", content: "灯光渐亮", subtitle: "观众看到的字幕", subtitle_valid: true },
      { id: 2, number: "A2", content: "音乐起", subtitle: null, subtitle_valid: false },
    ],
  }));
  const tags = ctx.document.querySelectorAll('#pending-list .tag.sub-on');
  check(tags.length === 1 && tags[0].textContent.includes("字幕"), "成功后待发行显示 字幕 ✓ 标记");
  check(ctx.document.el("version-badge").textContent === "v4", "版本角标 +1");
}

async function t3_clearWithEmpty() {
  console.log("F3 空内容提交为清除（subtitle: null）");
  const ctx = loadApp();
  await respond(lastRequest(ctx), liveState({
    pending: [
      { id: 1, number: "A1", content: "灯光渐亮", subtitle: "旧字幕", subtitle_valid: true },
      { id: 2, number: "A2", content: "音乐起", subtitle: null, subtitle_valid: false },
    ],
    current: { id: 1, number: "A1", content: "灯光渐亮", subtitle: "旧字幕", subtitle_valid: true },
  }));
  clickButton(ctx, subBtn(ctx, 1));
  const form = ctx.document.querySelectorAll('.subtitle-edit[data-id="1"]')[0];
  check(form.querySelectorAll(".subtitle-input")[0].value === "旧字幕", "表单预填现有字幕");
  // 清空后保存
  form.querySelectorAll(".subtitle-input")[0].value = "   ";
  clickButton(ctx, form.querySelectorAll('button[data-action="save-subtitle"]')[0]);
  const body = JSON.parse(lastRequest(ctx).options.body);
  check(body.subtitle === null, "空白内容作为 null 清除提交");
}

async function t4_tooLongBlocked() {
  console.log("F4 超长（>200）前端拦截不发请求");
  const ctx = loadApp();
  await respond(lastRequest(ctx), liveState());
  clickButton(ctx, subBtn(ctx, 1));
  const form = ctx.document.querySelectorAll('.subtitle-edit[data-id="1"]')[0];
  form.querySelectorAll(".subtitle-input")[0].value = "字".repeat(201);
  const before = ctx.pending.length;
  clickButton(ctx, form.querySelectorAll('button[data-action="save-subtitle"]')[0]);
  check(ctx.pending.length === before, "超长不发请求");
  check(ctx.document.el("toast").textContent.includes("200"), "超长提示 200 字符上限");
}

async function t5_invalidTag() {
  console.log("F5 失效字幕显示红色标记");
  const ctx = loadApp();
  await respond(lastRequest(ctx), liveState({
    pending: [
      { id: 1, number: "A1", content: "灯光渐亮（改）", subtitle: "旧字幕", subtitle_valid: false },
      { id: 2, number: "A2", content: "音乐起", subtitle: null, subtitle_valid: false },
    ],
    current: { id: 1, number: "A1", content: "灯光渐亮（改）", subtitle: "旧字幕", subtitle_valid: false },
  }));
  const off = ctx.document.querySelectorAll('#pending-list .tag.sub-off');
  check(off.length === 1 && off[0].textContent.includes("字幕已失效"), "失效标记显示");
}

async function t6_screenBar() {
  console.log("F6 画面状态条：字幕 / 清屏 / 序号；开场前隐藏；轮询同步");
  const ctx = loadApp();
  await respond(lastRequest(ctx), setupState());
  check(ctx.document.el("screen-bar").classList.contains("hidden"), "开场前画面状态条隐藏");

  let req = await pollOnce(ctx);
  await respond(req, liveState());
  check(!ctx.document.el("screen-bar").classList.contains("hidden"), "演出中显示状态条");
  check(ctx.document.el("screen-bar-text").textContent === "清屏中（无字幕）", "无字幕显示清屏中");
  check(ctx.document.el("screen-bar-seq").textContent === "画面 #0", "序号显示 #0");

  req = await pollOnce(ctx);
  await respond(req, liveState({ screen: { subtitle: "欢迎观看", seq: 1 } }));
  check(ctx.document.el("screen-bar-text").textContent === "欢迎观看", "轮询同步当前字幕");
  check(ctx.document.el("screen-bar-seq").textContent === "画面 #1", "序号同步 #1");

  req = await pollOnce(ctx);
  await respond(req, liveState({ version: 4, screen: { subtitle: null, seq: 2 } }));
  check(ctx.document.el("screen-bar-text").textContent === "清屏中（无字幕）", "清屏同步");
  check(ctx.document.el("screen-bar-seq").textContent === "画面 #2", "清屏序号 #2");
  check(ctx.document.el("version-badge").textContent === "v4", "清屏随版本变化整体刷新");
}

async function t7_busyAnd409() {
  console.log("F7 在途禁用防重复；409 应用最新状态；403 转只读");
  const ctx = loadApp();
  await respond(lastRequest(ctx), liveState());
  clickButton(ctx, subBtn(ctx, 1));
  const form = ctx.document.querySelectorAll('.subtitle-edit[data-id="1"]')[0];
  form.querySelectorAll(".subtitle-input")[0].value = "在途字幕";
  clickButton(ctx, form.querySelectorAll('button[data-action="save-subtitle"]')[0]);
  const saveBtn = form.querySelectorAll('button[data-action="save-subtitle"]')[0];
  check(saveBtn.disabled === true, "请求在途保存按钮禁用");
  // 其他待发行的字幕入口也随 busy 禁用
  check(subBtn(ctx, 2).disabled === true, "在途时其他字幕入口禁用");
  await respond(lastRequest(ctx), liveState({
    version: 4,
    pending: [
      { id: 1, number: "A1", content: "灯光渐亮", subtitle: "在途字幕", subtitle_valid: true },
      { id: 2, number: "A2", content: "音乐起", subtitle: null, subtitle_valid: false },
    ],
    current: { id: 1, number: "A1", content: "灯光渐亮", subtitle: "在途字幕", subtitle_valid: true },
  }));
  check(subBtn(ctx, 2).disabled === false, "请求结束入口恢复");

  // 409：版本过期，应用附带状态（字幕已被另一页清除）
  clickButton(ctx, subBtn(ctx, 1));
  const f2 = ctx.document.querySelectorAll('.subtitle-edit[data-id="1"]')[0];
  f2.querySelectorAll(".subtitle-input")[0].value = "过期提交";
  clickButton(ctx, f2.querySelectorAll('button[data-action="save-subtitle"]')[0]);
  await respond(lastRequest(ctx), {
    error: "conflict", message: "操作已过期",
    state: liveState({
      version: 9,
      pending: [
        { id: 1, number: "A1", content: "灯光渐亮", subtitle: null, subtitle_valid: false },
        { id: 2, number: "A2", content: "音乐起", subtitle: null, subtitle_valid: false },
      ],
      current: { id: 1, number: "A1", content: "灯光渐亮", subtitle: null, subtitle_valid: false },
      screen: { subtitle: null, seq: 5 },
    }),
  }, 409);
  check(ctx.document.el("version-badge").textContent === "v9", "409 后版本刷新为最新");
  check(ctx.document.el("screen-bar-seq").textContent === "画面 #5", "409 附带画面状态一并应用");

  // 403：控制席被接管 -> 字幕入口禁用
  const ctx2 = loadApp();
  await respond(lastRequest(ctx2), liveState({
    control_enabled: true, control_granted: true,
    pending: [
      { id: 1, number: "A1", content: "灯光渐亮", subtitle: "x", subtitle_valid: true },
      { id: 2, number: "A2", content: "音乐起", subtitle: null, subtitle_valid: false },
    ],
    current: { id: 1, number: "A1", content: "灯光渐亮", subtitle: "x", subtitle_valid: true },
  }));
  check(subBtn(ctx2, 1).disabled === false, "当前控制席入口可用");
  clickButton(ctx2, subBtn(ctx2, 1));
  const f3 = ctx2.document.querySelectorAll('.subtitle-edit[data-id="1"]')[0];
  f3.querySelectorAll(".subtitle-input")[0].value = "旧席提交";
  clickButton(ctx2, f3.querySelectorAll('button[data-action="save-subtitle"]')[0]);
  await respond(lastRequest(ctx2), {
    error: "forbidden", message: "本页面不是当前控制席",
    state: liveState({
      control_enabled: true, control_granted: false,
      pending: [
        { id: 1, number: "A1", content: "灯光渐亮", subtitle: "x", subtitle_valid: true },
        { id: 2, number: "A2", content: "音乐起", subtitle: null, subtitle_valid: false },
      ],
      current: { id: 1, number: "A1", content: "灯光渐亮", subtitle: "x", subtitle_valid: true },
    }),
  }, 403);
  // 403 后回到列表视图（字幕表单关闭）：字幕入口按只读禁用
  const afterBtn = subBtn(ctx2, 1);
  check(!!afterBtn && afterBtn.disabled === true, "403 后字幕入口转禁用（只读）");
}

async function t8_cancelLeavesState() {
  console.log("F8 取消表单不发请求、不丢已有标记");
  const ctx = loadApp();
  await respond(lastRequest(ctx), liveState({
    pending: [
      { id: 1, number: "A1", content: "灯光渐亮", subtitle: "已有", subtitle_valid: true },
      { id: 2, number: "A2", content: "音乐起", subtitle: null, subtitle_valid: false },
    ],
    current: { id: 1, number: "A1", content: "灯光渐亮", subtitle: "已有", subtitle_valid: true },
  }));
  clickButton(ctx, subBtn(ctx, 1));
  const form = ctx.document.querySelectorAll('.subtitle-edit[data-id="1"]')[0];
  form.querySelectorAll(".subtitle-input")[0].value = "改了又取消";
  clickButton(ctx, form.querySelectorAll('button[data-action="cancel-subtitle"]')[0]);
  check(!ctx.document.querySelectorAll('.subtitle-edit[data-id="1"]').length, "取消后表单关闭");
  check(ctx.document.querySelectorAll('#pending-list .tag.sub-on').length === 1, "取消不丢失已有字幕标记");
  check(ctx.pending.filter((r) => r.path === "/api/subtitles").length === 0, "取消不发请求");
}

(async () => {
  await t1_entryAndPermissions();
  await t2_setSubtitle();
  await t3_clearWithEmpty();
  await t4_tooLongBlocked();
  await t5_invalidTag();
  await t6_screenBar();
  await t7_busyAnd409();
  await t8_cancelLeavesState();
  console.log(failures === 0 ? "\n全部通过（8 个用例）" : "\n失败 " + failures + " 处断言");
  process.exit(failures === 0 ? 0 : 1);
})().catch((err) => { console.error(err); process.exit(1); });
