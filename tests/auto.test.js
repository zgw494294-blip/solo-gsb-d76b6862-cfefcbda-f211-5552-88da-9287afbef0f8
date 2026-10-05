"use strict";

/* 自动发出前端回归测试（纯 Node，无第三方依赖）。
 *
 * 通过 vm 加载真实的 app/static/app.js，用迷你 DOM 模拟页面元素与事件，
 * 用可控 fetch 代替网络（记录请求体与请求头），用可控 setInterval 手动
 * 驱动每秒轮询。
 *
 * 覆盖：设置页隐藏自动卡片、演出页显示开关与状态行；开启 / 关闭 / 设置
 * 间隔的请求体（版本号、开关、1～60 整数间隔，非法输入不发请求）；状态行
 * 随 remaining_seconds / waiting_ready / paused 切换；轮询同步——同版本
 * 只更新倒计时且不打断正在输入的间隔，版本变化整体刷新（已发记录、版本
 * 角标），编辑中轮询不重建表单，交错轮询的更旧响应被丢弃不回退；只读页
 * 与请求在途时按钮禁用。
 *
 * 运行：node tests/auto.test.js
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
      if (!(m[3] === "/" || VOID_TAGS.has(tag))) stack.push(el);
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
    const dfs = (els, depth) => {
      for (const el of els) {
        if (el._matchesCompound(parts[depth])) {
          if (depth === parts.length - 1) {
            if (!seen.has(el)) { seen.add(el); out.push(el); }
          } else {
            dfs(el.children, depth + 1);
          }
        }
        dfs(el.children, depth);
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
    // 无 id 的全局选择器：在已知元素后代里找（测试用不到，返回空元素兜底）
    for (const root of this.byId.values()) {
      const f = root.querySelector(sel);
      if (f.tagName) return f;
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
  const intervals = []; // 可控 setInterval：测试手动驱动轮询
  const storage = {
    _m: new Map(Object.entries(storageData || {})),
    getItem(k) { return this._m.has(k) ? this._m.get(k) : null; },
    setItem(k, v) { this._m.set(k, String(v)); },
    removeItem(k) { this._m.delete(k); },
  };
  const sandbox = {
    document,
    fetch: (path, options) =>
      new Promise((resolve, reject) => pending.push({ path, options, resolve, reject })),
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

/* 手动驱动一次每秒轮询：返回发出的 GET /api/state 请求（未发起返回 null）。 */
async function pollOnce(ctx) {
  const before = ctx.pending.length;
  ctx.intervals[0].fn();
  await flush();
  return ctx.pending.length > before ? lastRequest(ctx) : null;
}

/* ------------------------------------------------------------ 测试数据 */

function autoView(over) {
  return Object.assign({
    enabled: false,
    interval: 10,
    remaining_seconds: null,
    waiting_ready: false,
  }, over || {});
}

function verifyOff() {
  return { enabled: false, round: 0, cue_id: null, lighting: null, sound: null, all_ready: false };
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
      { id: 1, number: "A1", content: "灯光渐亮" },
      { id: 2, number: "A2", content: "音乐起" },
    ],
    issued: [],
    issues: [],
    verify: verifyOff(),
    auto: autoView(),
    total: 2,
    remaining: 2,
    min_cues: 2,
    max_cues: 12,
  }, over || {});
}

function setupState(over) {
  return liveState(Object.assign({
    status: "setup",
    current: null,
    next: null,
    pending: [],
    total: 0,
    remaining: 0,
  }, over || {}));
}

/* ------------------------------------------------------------ 元素助手 */

function autoCard(ctx) { return ctx.document.el("auto-card"); }
function autoStatusEl(ctx) {
  return autoCard(ctx).querySelectorAll("#auto-status")[0] || null;
}
function autoToggle(ctx) {
  return autoCard(ctx).querySelectorAll("#auto-toggle")[0] || null;
}
function autoApply(ctx) {
  return autoCard(ctx).querySelectorAll("#auto-apply")[0] || null;
}
function autoInput(ctx) {
  return autoCard(ctx).querySelectorAll("#auto-interval")[0] || null;
}

/* 通过真实按钮元素派发委托点击：与浏览器一致，按钮 disabled 时处理函数早退。 */
function clickButton(ctx, btn) {
  ctx.document.dispatch("click", { target: btn });
}

/* 设置页录入两条提示并点「开始演出」：返回在途的 /api/setup 请求。 */
function startShowWithTwoCues(ctx) {
  const rows = ctx.document.querySelectorAll("#cue-rows .cue-row");
  rows[0].querySelector(".num").value = "A1";
  rows[0].querySelector(".cnt").value = "灯光渐亮";
  rows[1].querySelector(".num").value = "A2";
  rows[1].querySelector(".cnt").value = "音乐起";
  const before = ctx.pending.length;
  ctx.document.el("start-show").dispatch("click");
  if (ctx.pending.length === before) throw new Error("开场请求未发出（录入校验未通过）");
  return lastRequest(ctx);
}

/* ------------------------------------------------------------ 断言 */

let failures = 0;
function check(cond, msg) {
  if (cond) console.log("  ✓ " + msg);
  else { failures++; console.error("  ✗ " + msg); }
}

/* ------------------------------------------------------------ 用例 */

async function t1_cardVisibility() {
  console.log("J1 设置页隐藏自动卡片，演出页显示开关与关闭态");
  const ctx = loadApp();
  await respond(lastRequest(ctx), setupState());
  check(autoCard(ctx).classList.contains("hidden"), "开场前自动卡片隐藏");

  const req = startShowWithTwoCues(ctx);
  await respond(req, liveState({ version: 4 }));
  check(!autoCard(ctx).classList.contains("hidden"), "演出中显示自动卡片");
  check(autoStatusEl(ctx).textContent === "已关闭：手工确认发出", "关闭态状态行");
  check(autoToggle(ctx).textContent === "开启自动 ▶", "开关按钮显示开启");
  check(autoToggle(ctx).disabled === false, "可写时开关可用");
  check(autoInput(ctx).value === "10", "间隔输入框显示当前间隔");
}

async function t2_enableFlow() {
  console.log("J2 开启自动：请求携带版本号、开关与间隔，成功后显示倒计时");
  const ctx = loadApp();
  await respond(lastRequest(ctx), liveState());

  clickButton(ctx, autoToggle(ctx));
  const req = lastRequest(ctx);
  check(req.path === "/api/auto", "点击开启请求自动设置接口");
  const body = JSON.parse(req.options.body);
  check(body.version === 3 && body.enabled === true && body.interval === 10,
    "开启请求携带版本号、开关与间隔");
  await respond(req, liveState({
    version: 4,
    auto: autoView({ enabled: true, remaining_seconds: 10 }),
  }));
  check(autoStatusEl(ctx).textContent === "约 10 秒后自动发出当前提示", "开启后显示倒计时");
  check(autoToggle(ctx).textContent === "关闭自动 ⏹", "开关变为关闭态");
  check(ctx.document.el("version-badge").textContent === "v4", "版本号同步 +1");
}

async function t3_statusLineVariants() {
  console.log("J3 状态行：剩余秒数 / 即将发出 / 等待就绪 / 暂停冻结");
  const ctx = loadApp();
  await respond(lastRequest(ctx), liveState({
    auto: autoView({ enabled: true, remaining_seconds: 42.3 }),
  }));
  check(autoStatusEl(ctx).textContent === "约 43 秒后自动发出当前提示",
    "剩余秒数向上取整显示");

  let req = await pollOnce(ctx);
  check(req && req.path === "/api/state", "轮询发出状态查询");
  await respond(req, liveState({ auto: autoView({ enabled: true, remaining_seconds: 0.0 }) }));
  check(autoStatusEl(ctx).textContent === "即将自动发出…", "到期显示即将发出");

  req = await pollOnce(ctx);
  await respond(req, liveState({
    auto: autoView({ enabled: true, remaining_seconds: 0.0, waiting_ready: true }),
  }));
  check(autoStatusEl(ctx).textContent === "已到间隔 · 等待灯光、音响就绪…",
    "到期未就绪显示等待就绪");

  req = await pollOnce(ctx);
  await respond(req, liveState({
    paused: true,
    auto: autoView({ enabled: true, remaining_seconds: 6.0 }),
  }));
  check(autoStatusEl(ctx).textContent === "已暂停 · 恢复后续计（剩余约 6 秒）",
    "暂停显示冻结剩余时间");

  req = await pollOnce(ctx);
  await respond(req, liveState({ auto: autoView() }));
  check(autoStatusEl(ctx).textContent === "已关闭：手工确认发出", "关闭后回到关闭态");
}

async function t4_disableFlow() {
  console.log("J4 关闭自动：不携带间隔，成功后回到关闭态");
  const ctx = loadApp();
  await respond(lastRequest(ctx), liveState({
    auto: autoView({ enabled: true, interval: 15, remaining_seconds: 12.0 }),
  }));

  clickButton(ctx, autoToggle(ctx));
  const req = lastRequest(ctx);
  const body = JSON.parse(req.options.body);
  check(req.path === "/api/auto" && body.enabled === false, "关闭请求携带开关");
  check(!("interval" in body), "关闭不携带间隔（保留已设间隔）");
  check(body.version === 3, "关闭携带版本号");
  await respond(req, liveState({ version: 4, auto: autoView({ interval: 15 }) }));
  check(autoStatusEl(ctx).textContent === "已关闭：手工确认发出", "关闭后状态行更新");
  check(autoInput(ctx).value === "15", "关闭后输入框保留已设间隔");
}

async function t5_applyInterval() {
  console.log("J5 设置间隔：合法提交、非法输入不发请求");
  const ctx = loadApp();
  await respond(lastRequest(ctx), liveState({
    auto: autoView({ enabled: true, remaining_seconds: 10 }),
  }));

  autoInput(ctx).value = "25";
  clickButton(ctx, autoApply(ctx));
  let req = lastRequest(ctx);
  let body = JSON.parse(req.options.body);
  check(req.path === "/api/auto" && body.interval === 25 && body.enabled === true,
    "设置间隔携带新间隔与当前开关");
  await respond(req, liveState({
    version: 4,
    auto: autoView({ enabled: true, interval: 25, remaining_seconds: 25 }),
  }));
  check(autoStatusEl(ctx).textContent === "约 25 秒后自动发出当前提示",
    "改间隔后按新间隔倒计时");

  for (const bad of ["0", "61", "1.5", ""]) {
    autoInput(ctx).value = bad;
    const before = ctx.pending.length;
    clickButton(ctx, autoApply(ctx));
    check(ctx.pending.length === before, `非法间隔 "${bad}" 不发请求`);
    check(ctx.document.el("toast").textContent.includes("1～60"),
      `非法间隔 "${bad}" 提示合法范围`);
  }
  // 开启时非法间隔同样拦截
  autoInput(ctx).value = "99";
  const beforeToggle = ctx.pending.length;
  clickButton(ctx, autoToggle(ctx)); // 当前已开启 → 关闭不带间隔，合法
  check(ctx.pending.length === beforeToggle + 1, "开启态点开关仍正常关闭");
}

async function t6_pollSync() {
  console.log("J6 轮询同步：同版本只更新倒计时且不打断输入，版本变化整体刷新");
  const ctx = loadApp();
  await respond(lastRequest(ctx), liveState({
    version: 5,
    auto: autoView({ enabled: true, remaining_seconds: 42.3 }),
  }));

  // 正在输入间隔（聚焦）：同版本轮询不覆盖输入框
  autoInput(ctx).value = "25";
  ctx.document.activeElement = autoInput(ctx);
  let req = await pollOnce(ctx);
  await respond(req, liveState({
    version: 5,
    auto: autoView({ enabled: true, remaining_seconds: 41.3 }),
  }));
  check(autoStatusEl(ctx).textContent === "约 42 秒后自动发出当前提示",
    "同版本轮询更新倒计时");
  check(autoInput(ctx).value === "25", "聚焦输入时轮询不覆盖输入框");
  check(ctx.document.el("version-badge").textContent === "v5", "同版本轮询版本不变");

  // 未聚焦：输入框跟随服务端间隔
  ctx.document.activeElement = null;
  req = await pollOnce(ctx);
  await respond(req, liveState({
    version: 5,
    auto: autoView({ enabled: true, remaining_seconds: 40.0 }),
  }));
  check(autoInput(ctx).value === "10", "未聚焦时输入框跟随服务端间隔");

  // 版本变化（后台自动发出了一条）：整体刷新已发记录与版本角标
  req = await pollOnce(ctx);
  await respond(req, liveState({
    version: 6,
    current: { id: 2, number: "A2", content: "音乐起" },
    next: null,
    pending: [{ id: 2, number: "A2", content: "音乐起" }],
    issued: [{ id: 1, number: "A1", content: "灯光渐亮", issued_at: "2026-10-05T10:00:00+00:00" }],
    remaining: 1,
    auto: autoView({ enabled: true, remaining_seconds: 10 }),
  }));
  check(ctx.document.el("version-badge").textContent === "v6", "版本变化刷新版本角标");
  check(ctx.document.el("issued-list").innerHTML.includes("A1"),
    "版本变化刷新已发记录（后台发出的提示出现）");
  check(autoStatusEl(ctx).textContent === "约 10 秒后自动发出当前提示",
    "版本变化后倒计时按新状态显示");
}

async function t7_pollWhileEditing() {
  console.log("J7 编辑中轮询：版本变化也不重建表单，已输入内容保留");
  const ctx = loadApp();
  await respond(lastRequest(ctx), liveState({ version: 5 }));

  const editBtn = ctx.document.querySelectorAll(
    '#pending-list button[data-action="edit"][data-id="1"]')[0];
  clickButton(ctx, editBtn);
  const textarea = ctx.document.el("pending-list").querySelectorAll(".content-input")[0];
  check(!!textarea, "进入编辑出现内容输入框");
  textarea.value = "正在输入的新内容";

  const req = await pollOnce(ctx);
  await respond(req, liveState({
    version: 6, // 后台自动发出了正在编辑的提示
    current: { id: 2, number: "A2", content: "音乐起" },
    next: null,
    pending: [{ id: 2, number: "A2", content: "音乐起" }],
    issued: [{ id: 1, number: "A1", content: "灯光渐亮", issued_at: "2026-10-05T10:00:00+00:00" }],
    remaining: 1,
    auto: autoView({ enabled: true, remaining_seconds: 8 }),
  }));
  const textareaAfter = ctx.document.el("pending-list").querySelectorAll(".content-input")[0];
  check(textareaAfter === textarea, "编辑中轮询不重建表单（元素未替换）");
  check(textareaAfter && textareaAfter.value === "正在输入的新内容",
    "已输入内容保留");
  check(ctx.document.el("version-badge").textContent === "v6", "编辑中轮询仍同步版本角标");

  const cancelBtn = ctx.document.el("pending-list").querySelectorAll(
    'button[data-action="cancel-edit"]')[0];
  clickButton(ctx, cancelBtn);
  const items = ctx.document.el("pending-list").querySelectorAll(".pending-item");
  check(items.length === 1, "取消编辑后按最新状态渲染（只剩一条待发）");
}

async function t8_readonlyAndBusy() {
  console.log("J8 只读页与请求在途：开关 / 设置 / 输入禁用");
  const readonly = loadApp();
  await respond(lastRequest(readonly), liveState({
    control_enabled: true,
    control_granted: false,
  }));
  check(autoToggle(readonly).disabled === true, "只读页开关禁用");
  check(autoApply(readonly).disabled === true, "只读页设置按钮禁用");
  check(autoInput(readonly).disabled === true, "只读页间隔输入禁用");
  const before = readonly.pending.length;
  clickButton(readonly, autoToggle(readonly));
  check(readonly.pending.length === before, "只读页点击不发请求");

  const ctx = loadApp();
  await respond(lastRequest(ctx), liveState());
  clickButton(ctx, autoToggle(ctx));
  check(autoToggle(ctx).disabled === true, "请求在途时开关禁用");
  check(autoApply(ctx).disabled === true, "请求在途时设置按钮禁用");
  await respond(lastRequest(ctx), liveState({
    version: 4,
    auto: autoView({ enabled: true, remaining_seconds: 10 }),
  }));
  check(autoToggle(ctx).disabled === false, "请求结束后开关恢复");
  check(autoApply(ctx).disabled === false, "请求结束后设置按钮恢复");
}

async function t9_stalePollDropped() {
  console.log("J9 交错轮询：更旧的响应迟到被丢弃，状态不回退");
  const ctx = loadApp();
  await respond(lastRequest(ctx), liveState({
    version: 5,
    auto: autoView({ enabled: true, remaining_seconds: 9 }),
  }));

  // 两次轮询同时在途：先回来的响应版本更新（v6），旧响应（v5）后回来
  const reqOld = await pollOnce(ctx);
  const reqNew = await pollOnce(ctx);
  await respond(reqNew, liveState({
    version: 6,
    issued: [{ id: 1, number: "A1", content: "灯光渐亮", issued_at: "2026-10-05T10:00:00+00:00" }],
    current: { id: 2, number: "A2", content: "音乐起" },
    next: null,
    pending: [{ id: 2, number: "A2", content: "音乐起" }],
    remaining: 1,
    auto: autoView({ enabled: true, remaining_seconds: 10 }),
  }));
  check(ctx.document.el("version-badge").textContent === "v6", "新响应先到达：版本推进");
  await respond(reqOld, liveState({
    version: 5,
    auto: autoView({ enabled: true, remaining_seconds: 8 }),
  }));
  check(ctx.document.el("version-badge").textContent === "v6", "旧响应迟到：版本不回退");
  check(ctx.document.el("issued-list").innerHTML.includes("A1"),
    "旧响应迟到：已发记录不回退");
  check(autoStatusEl(ctx).textContent === "约 10 秒后自动发出当前提示",
    "旧响应迟到：倒计时不回退");
}

/* ------------------------------------------------------------ 运行 */

(async () => {
  await t1_cardVisibility();
  await t2_enableFlow();
  await t3_statusLineVariants();
  await t4_disableFlow();
  await t5_applyInterval();
  await t6_pollSync();
  await t7_pollWhileEditing();
  await t8_readonlyAndBusy();
  await t9_stalePollDropped();
  console.log(failures === 0 ? "\n全部通过（9 个用例）" : `\n失败 ${failures} 处断言`);
  process.exit(failures === 0 ? 0 : 1);
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
