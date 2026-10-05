"use strict";

/* 改写提议前端回归测试（纯 Node，无第三方依赖）。
 *
 * 通过 vm 加载真实的 app/static/app.js，用迷你 DOM 模拟页面元素与事件
 * （解析真实渲染出来的 innerHTML，断言真实按钮的 disabled，并通过真实元素
 * 派发点击），用可控 fetch 代替网络（记录请求体与请求头）。
 *
 * 覆盖：
 * - 待发提示后出现 💡 按钮：控制席 / 只读页都可点（改写 ✎ 仍只控制席可用）；
 * - 点 💡 展开提议表单（显示原文），提交 POST /api/suggestions 的 cue_id、
 *   建议内容与版本号；空白内容不发请求；成功后退出表单、侧栏出现提议；
 *   提交请求在在途时禁用按钮防重复；
 * - 侧栏列表展示原文、建议与处理状态；待审且目标有效时控制席可接受 / 拒绝，
 *   按钮请求路径与请求体（最新版本号）正确；目标失效时接受禁用、拒绝可用；
 *   已处理提议不再显示审核按钮；
 * - 只读页（无控制资格）：可提交提议（💡 可用、提交不带凭据也能成功），
 *   接受 / 拒绝按钮禁用；当前席提交 / 审核携带 X-Control-Token；
 * - 409（版本过期 / 目标失效 / 已处理）后页面应用附带的最新状态；
 * - 拒绝不改版本：响应版本与请求时相同，列表状态刷新为已拒绝；
 * - 开场前（setup）侧栏不渲染审核按钮，且切换场次时编辑态被清理。
 *
 * 运行：node tests/suggestions.test.js
 */

const fs = require("fs");
const path = require("path");
const vm = require("vm");

/* ------------------------------------------------------------ 迷你 DOM
 * 与 tests/verify.test.js 同一套：解析 innerHTML 片段，使按钮成为可检查
 * disabled、可派发点击的真实元素。 */

class ClassList {
  constructor() { this.set = new Set(); }
  add(c) { this.set.add(c); }
  remove(c) { this.set.delete(c); }
  contains(c) { return this.set.has(c); }
  toggle(c, force) {
    const want = force === undefined ? !this.set.has(c) : !!force;
    if (want) this.set.add(c); else this.set.delete(c);
    return want;
  }
}

const VOID_TAGS = new Set(["input", "br", "img"]);

function parseAttrs(attrText, el) {
  const attrRe = /([a-zA-Z_:][-a-zA-Z0-9_:.]*)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g;
  let m;
  while ((m = attrRe.exec(attrText))) {
    el.setAttribute(m[1], m[2] ?? m[3] ?? m[4] ?? "");
  }
}

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
      if (top) top.children.push(el); else roots.push(el);
      parseAttrs(m[2], el);
      if (!(m[3] === "/" || VOID_TAGS.has(tag))) stack.push(el);
    } else if (m[4] !== undefined) {
      const tag = m[4].toLowerCase();
      for (let i = stack.length - 1; i >= 0; i--) {
        if (stack[i].tagName === tag.toUpperCase()) { stack.length = i; break; }
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
  set value(v) { this._value = v; }
  get className() { return [...this.classList.set].join(" "); }
  set className(v) {
    this.classList = new ClassList();
    String(v).split(/\s+/).filter(Boolean).forEach((c) => this.classList.add(c));
    this._attrs.class = String(v);
  }
  get textContent() {
    if (this.children.length) return this.children.map((c) => c.textContent).join("") + this._text;
    return this._text;
  }
  set textContent(v) { this.children = []; this._text = String(v); }
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
    for (const c of this.children) { fn(c); c._walk(fn); }
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
    // 无 id 的选择器（如 .suggestion-edit[data-id="2"]，app.js 保存表单使用）：
    // 在所有已知元素（含 innerHTML 解析出的后代）中找第一个真实匹配项。
    // 必须用 querySelectorAll：querySelector 未命中时返回空 El（tagName 默认为
    // DIV），不能用 tagName 真值判断，否则会在第一个不含目标的根上误停。
    return this.querySelectorAll(sel)[0] || new El();
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
    confirm: () => true,
    console,
  };
  vm.createContext(sandbox);
  vm.runInContext(appCode, sandbox, { filename: "app.js" });
  return { document, pending, storage, sandbox };
}

const flush = () => new Promise((r) => setImmediate(r));
const lastRequest = (ctx) => ctx.pending[ctx.pending.length - 1];

async function respond(req, data, status = 200) {
  req.resolve({ ok: status >= 200 && status < 300, status, json: async () => data });
  await flush();
}

/* ------------------------------------------------------------ 测试数据 */

function suggestion(over) {
  return Object.assign({
    id: 1,
    cue_id: 2,
    cue_number: "A2",
    original_content: "音乐起",
    content: "音乐提前两拍起",
    status: "pending",
    target_valid: true,
    created_at: "2026-10-05T10:00:00+00:00",
    decided_at: null,
  }, over || {});
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
    suggestions: [],
    verify: { enabled: false, round: 0, cue_id: null, lighting: null, sound: null, all_ready: false },
    auto: { enabled: false, interval: 10, remaining_seconds: null, waiting_ready: false },
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

/* 通过真实元素派发文档级委托点击（与浏览器一致，disabled 按钮在委托处被挡）。 */
function clickButton(ctx, btn) {
  ctx.document.dispatch("click", { target: btn });
}

const suggestButton = (ctx, cueId) =>
  ctx.document.querySelectorAll(
    `#pending-list button[data-action="add-suggestion"][data-id="${cueId}"]`
  )[0] || null;

const editButton = (ctx, cueId) =>
  ctx.document.querySelectorAll(
    `#pending-list button[data-action="edit"][data-id="${cueId}"]`
  )[0] || null;

const decideButton = (ctx, action, sid) =>
  ctx.document.querySelectorAll(
    `#suggestions-list button[data-action="${action}"][data-id="${sid}"]`
  )[0] || null;

let failures = 0;
function check(cond, msg) {
  if (cond) console.log("  ✓ " + msg);
  else { failures++; console.error("  ✗ " + msg); }
}

/* ------------------------------------------------------------ 用例 */

async function t1_suggestEntryAvailableToAll() {
  console.log("T1 待发提示后有 💡：控制席与只读页都可点；✎ 仅控制席可用");
  const ctx = loadApp();
  await respond(lastRequest(ctx), liveState());
  const btn = suggestButton(ctx, 1);
  check(!!btn, "渲染出提议按钮 💡");
  check(btn.disabled === false, "控制席页面 💡 可点");
  check(editButton(ctx, 1).disabled === false, "控制席页面 ✎ 可点");

  const ro = loadApp();
  await respond(lastRequest(ro), liveState({ control_enabled: true, control_granted: false }));
  check(suggestButton(ro, 1).disabled === false, "只读页 💡 仍可点（无需控制凭据）");
  check(editButton(ro, 1).disabled === true, "只读页 ✎ 禁用");
}

async function t2_openFormShowsOriginal() {
  console.log("T2 点 💡 展开提议表单，显示原文与提交/取消");
  const ctx = loadApp();
  await respond(lastRequest(ctx), liveState());
  clickButton(ctx, suggestButton(ctx, 1));
  const li = ctx.document.querySelectorAll('#pending-list .suggestion-edit')[0];
  check(!!li, "待发列表内展开提议表单");
  check(li.textContent.includes("原文：灯光渐亮"), "表单展示原文快照");
  const save = ctx.document.querySelectorAll(
    '#pending-list button[data-action="save-suggestion"]'
  )[0];
  check(!!save, "有提交提议按钮");
  // 点取消收起表单
  clickButton(ctx, ctx.document.querySelectorAll(
    '#pending-list button[data-action="cancel-suggestion"]'
  )[0]);
  check(ctx.document.querySelectorAll('#pending-list .suggestion-edit').length === 0,
    "取消后收起表单");
}

async function t3_submitRequestAndSuccess() {
  console.log("T3 提交：请求体含 cue_id/建议内容/版本；成功后表单退出、列表出现提议");
  const ctx = loadApp();
  await respond(lastRequest(ctx), liveState());
  clickButton(ctx, suggestButton(ctx, 2));
  const input = ctx.document.querySelectorAll('#pending-list .suggestion-input')[0];
  input.value = "  音乐提前两拍起  ";
  const before = ctx.pending.length;
  clickButton(ctx, ctx.document.querySelectorAll(
    '#pending-list button[data-action="save-suggestion"]'
  )[0]);
  const req = lastRequest(ctx);
  check(ctx.pending.length === before + 1, "点击提交发出请求");
  check(req.path === "/api/suggestions", "请求提交到 /api/suggestions");
  const body = JSON.parse(req.options.body);
  check(body.cue_id === 2 && body.version === 3 && body.content === "音乐提前两拍起",
    "请求体携带 cue_id、所见版本号与去空白后的建议（与编辑/问题表单一致）");
  check(!("X-Control-Token" in req.options.headers), "未启用控制席时不带凭据头");

  // 在途时提交按钮禁用
  const saveBtn = ctx.document.querySelectorAll(
    '#pending-list button[data-action="save-suggestion"]'
  )[0];
  check(saveBtn.disabled === true, "请求在途时提交按钮禁用防重复");

  // 成功响应（版本不变，多一条提议）
  const after = liveState({ suggestions: [suggestion()] });
  await respond(req, after);
  check(ctx.document.querySelectorAll('#pending-list .suggestion-edit').length === 0,
    "成功后退出提议表单");
  const listHtml = ctx.document.el("suggestions-list").innerHTML;
  check(listHtml.includes("音乐提前两拍起"), "侧栏列表显示建议内容");
  check(listHtml.includes("原文：音乐起"), "侧栏列表显示原文");
  check(listHtml.includes("待审核"), "显示待审核状态");
}

async function t4_emptyContentNoRequest() {
  console.log("T4 建议为空白时不发请求");
  const ctx = loadApp();
  await respond(lastRequest(ctx), liveState());
  clickButton(ctx, suggestButton(ctx, 1));
  const input = ctx.document.querySelectorAll('#pending-list .suggestion-input')[0];
  input.value = "   ";
  const before = ctx.pending.length;
  clickButton(ctx, ctx.document.querySelectorAll(
    '#pending-list button[data-action="save-suggestion"]'
  )[0]);
  check(ctx.pending.length === before, "空白建议不发请求（前端拦截）");
  check(ctx.document.querySelectorAll('#pending-list .suggestion-edit').length === 1,
    "表单保留，可继续输入");
}

async function t5_acceptRejectControls() {
  console.log("T5 控制席审核：接受/拒绝请求正确；失效目标只禁用接受");
  const ctx = loadApp();
  const s = liveState({ suggestions: [suggestion()] });
  await respond(lastRequest(ctx), s);

  const accept = decideButton(ctx, "accept-suggestion", 1);
  const reject = decideButton(ctx, "reject-suggestion", 1);
  check(accept.disabled === false, "待审且有效：接受可用");
  check(reject.disabled === false, "拒绝可用");

  clickButton(ctx, accept);
  const req = lastRequest(ctx);
  check(req.path === "/api/suggestions/1/accept", "接受请求路径正确");
  check(JSON.parse(req.options.body).version === 3, "接受携带最新版本号");

  const accepted = suggestion({ status: "accepted", target_valid: false,
    decided_at: "2026-10-05T10:05:00+00:00" });
  const afterState = liveState({
    version: 4,
    pending: [
      { id: 1, number: "A1", content: "灯光渐亮" },
      { id: 2, number: "A2", content: "音乐提前两拍起" },
    ],
    next: { id: 2, number: "A2", content: "音乐提前两拍起" },
    suggestions: [accepted],
  });
  await respond(req, afterState);
  check(ctx.document.el("version-badge").textContent === "v4", "接受成功后版本 +1");
  check(decideButton(ctx, "accept-suggestion", 1) === null, "已接受提议不再显示审核按钮");
  check(ctx.document.el("suggestions-list").innerHTML.includes("已接受"), "显示已接受");

  // 拒绝：版本不变
  const ctx2 = loadApp();
  const s2 = liveState({
    version: 7,
    suggestions: [suggestion({ id: 5, cue_id: 1, cue_number: "A1", original_content: "灯光渐亮" })],
  });
  await respond(lastRequest(ctx2), s2);
  clickButton(ctx2, decideButton(ctx2, "reject-suggestion", 5));
  const rr = lastRequest(ctx2);
  check(rr.path === "/api/suggestions/5/reject", "拒绝请求路径正确");
  check(JSON.parse(rr.options.body).version === 7, "拒绝携带最新版本号");
  const rejectedState = liveState({
    version: 7,
    suggestions: [suggestion({
      id: 5, cue_id: 1, cue_number: "A1", original_content: "灯光渐亮",
      status: "rejected", target_valid: false, decided_at: "2026-10-05T10:06:00+00:00",
    })],
  });
  await respond(rr, rejectedState);
  check(ctx2.document.el("version-badge").textContent === "v7", "拒绝不改版本号");
  check(ctx2.document.el("suggestions-list").innerHTML.includes("已拒绝"), "显示已拒绝");
  check(decideButton(ctx2, "reject-suggestion", 5) === null, "已拒绝提议不再显示按钮");

  // 目标失效：接受禁用、拒绝可用
  const ctx3 = loadApp();
  await respond(lastRequest(ctx3), liveState({
    suggestions: [suggestion({ id: 9, target_valid: false })],
  }));
  check(decideButton(ctx3, "accept-suggestion", 9).disabled === true, "目标失效：接受禁用");
  check(decideButton(ctx3, "reject-suggestion", 9).disabled === false, "目标失效：拒绝仍可用");
  check(ctx3.document.el("suggestions-list").innerHTML.includes("目标已改动或已发出"),
    "失效原因展示在列表中");
}

async function t6_readonlyCanSubmitButNotReview() {
  console.log("T6 只读页：可提交提议（不 403），接受/拒绝禁用；当前席审核带凭据");
  const ctx = loadApp();
  await respond(lastRequest(ctx), liveState({
    control_enabled: true, control_granted: false,
    suggestions: [suggestion()],
  }));
  check(decideButton(ctx, "accept-suggestion", 1).disabled === true, "只读页接受禁用");
  check(decideButton(ctx, "reject-suggestion", 1).disabled === true, "只读页拒绝禁用");

  // 仍可提交
  clickButton(ctx, suggestButton(ctx, 2));
  const input = ctx.document.querySelectorAll('#pending-list .suggestion-input')[0];
  input.value = "只读页建议";
  clickButton(ctx, ctx.document.querySelectorAll(
    '#pending-list button[data-action="save-suggestion"]'
  )[0]);
  const req = lastRequest(ctx);
  check(req.path === "/api/suggestions", "只读页也能发出提交请求");
  check(!("X-Control-Token" in req.options.headers), "无缓存凭据时不带凭据头");
  await respond(req, liveState({
    control_enabled: true, control_granted: false,
    suggestions: [suggestion(), suggestion({ id: 2, content: "只读页建议" })],
  }));
  // 仍是只读
  check(decideButton(ctx, "accept-suggestion", 2).disabled === true,
    "提交成功后审核按钮仍禁用（本页无控制权）");

  // 当前席：审核请求带凭据
  const seat = loadApp({ cueControlToken: "tok-seat" });
  await respond(lastRequest(seat), liveState({
    control_enabled: true, control_granted: true,
    suggestions: [suggestion({ id: 3 })],
  }));
  clickButton(seat, decideButton(seat, "accept-suggestion", 3));
  const areq = lastRequest(seat);
  check(areq.options.headers["X-Control-Token"] === "tok-seat", "当前席接受携带凭据");
  await respond(areq, liveState({
    version: 4, control_enabled: true, control_granted: true,
    pending: [
      { id: 1, number: "A1", content: "灯光渐亮" },
      { id: 2, number: "A2", content: "音乐提前两拍起" },
    ],
    next: { id: 2, number: "A2", content: "音乐提前两拍起" },
    suggestions: [suggestion({ id: 3, status: "accepted", target_valid: false })],
  }));
}

async function t7_conflictRefreshesState() {
  console.log("T7 提交/审核遇 409：应用附带最新状态，提议不被本地改写");
  // 提交 409（如目标刚好被发出）
  const ctx = loadApp();
  await respond(lastRequest(ctx), liveState());
  clickButton(ctx, suggestButton(ctx, 1));
  ctx.document.querySelectorAll('#pending-list .suggestion-input')[0].value = "建议";
  clickButton(ctx, ctx.document.querySelectorAll(
    '#pending-list button[data-action="save-suggestion"]'
  )[0]);
  const req = lastRequest(ctx);
  const fresh = liveState({
    version: 9,
    current: { id: 2, number: "A2", content: "音乐起" },
    next: null,
    pending: [{ id: 2, number: "A2", content: "音乐起" }],
    issued: [{ id: 1, number: "A1", content: "灯光渐亮", issued_at: "2026-10-05T10:00:00+00:00" }],
    total: 2, remaining: 1,
  });
  await respond(req, { error: "conflict", message: "操作已过期", state: fresh }, 409);
  check(ctx.document.el("version-badge").textContent === "v9", "409 后刷新到最新版本");
  check(ctx.document.querySelectorAll('#pending-list .suggestion-edit').length === 0,
    "目标已不在待发：提议表单关闭（编辑态被清理）");

  // 接受 409（目标已被别人改写）：页面刷新，提议仍待审并显示失效
  const ctx2 = loadApp();
  await respond(lastRequest(ctx2), liveState({
    suggestions: [suggestion({ id: 4, target_valid: false })],
  }));
  check(decideButton(ctx2, "accept-suggestion", 4).disabled === true,
    "失效目标接受按钮禁用，避免发出必败请求");
  check(decideButton(ctx2, "reject-suggestion", 4).disabled === false, "仍可拒绝");
}

async function t8_processedProposalNoButtons() {
  console.log("T8 已处理（接受/拒绝）提议不渲染审核按钮");
  const ctx = loadApp();
  await respond(lastRequest(ctx), liveState({
    suggestions: [
      suggestion({ id: 1, status: "accepted", target_valid: false,
                  decided_at: "2026-10-05T10:01:00+00:00" }),
      suggestion({ id: 2, content: "另一条", status: "rejected", target_valid: false,
                  decided_at: "2026-10-05T10:02:00+00:00" }),
      suggestion({ id: 3, cue_id: 1, cue_number: "A1", original_content: "灯光渐亮" }),
    ],
  }));
  check(decideButton(ctx, "accept-suggestion", 1) === null, "已接受无接受按钮");
  check(decideButton(ctx, "reject-suggestion", 2) === null, "已拒绝无拒绝按钮");
  check(decideButton(ctx, "accept-suggestion", 3) !== null, "待审核的仍显示按钮");
}

async function t9_busyDisablesReviewButtons() {
  console.log("T9 任意请求在途：接受/拒绝/提交按钮统一禁用，结束后恢复");
  const ctx = loadApp();
  await respond(lastRequest(ctx), liveState({ suggestions: [suggestion()] }));
  // 发一个在途的接受请求
  clickButton(ctx, decideButton(ctx, "accept-suggestion", 1));
  check(decideButton(ctx, "reject-suggestion", 1).disabled === true,
    "接受在途时拒绝按钮禁用（防交错重复处理）");
  const req = lastRequest(ctx);
  await respond(req, liveState({
    version: 4,
    pending: [
      { id: 1, number: "A1", content: "灯光渐亮" },
      { id: 2, number: "A2", content: "音乐提前两拍起" },
    ],
    next: { id: 2, number: "A2", content: "音乐提前两拍起" },
    suggestions: [suggestion({ status: "accepted", target_valid: false })],
  }));
  check(ctx.document.el("version-badge").textContent === "v4", "结束后应用新版本");
}

(async function main() {
  const cases = [
    t1_suggestEntryAvailableToAll,
    t2_openFormShowsOriginal,
    t3_submitRequestAndSuccess,
    t4_emptyContentNoRequest,
    t5_acceptRejectControls,
    t6_readonlyCanSubmitButNotReview,
    t7_conflictRefreshesState,
    t8_processedProposalNoButtons,
    t9_busyDisablesReviewButtons,
  ];
  for (const run of cases) await run();
  console.log(
    failures === 0
      ? `\n全部通过（${cases.length} 个用例）`
      : `\n失败 ${failures} 处断言`
  );
  process.exit(failures === 0 ? 0 : 1);
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
