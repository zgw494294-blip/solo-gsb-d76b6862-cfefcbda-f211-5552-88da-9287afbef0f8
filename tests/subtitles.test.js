"use strict";

/* 观众字幕前端回归测试（纯 Node，无第三方依赖）。
 *
 * 通过 vm 加载真实的 app/static/app.js（执行台）与 app/static/screen.js
 * （观众字幕屏），用迷你 DOM 模拟页面元素与事件（解析真实渲染出来的
 * innerHTML，断言真实按钮的 disabled，并通过真实元素派发点击），用可控
 * fetch 代替网络（记录请求体与请求头）。
 *
 * 覆盖（执行台）：
 * - 待发提示后有 💬 按钮：控制席可点、只读页禁用；
 * - 字幕行显示设置及有效性（有效 / 已失效）；点 💬 展开表单并预填现有字幕；
 * - 保存请求体（version、cue_id、去空白后的 text）；空白不发请求；
 *   清除请求体 text 为 null；请求在途禁用保存 / 清除；成功后退出表单并显示字幕；
 * - 409 后应用最新状态（目标已不在待发时表单关闭）。
 *
 * 覆盖（字幕屏）：
 * - 轮询 GET /api/screen：字幕上屏、null 清屏；
 * - 画面序号防倒退：迟到的旧序号响应不覆盖画面；
 * - 网络失败 / 非 200 保持当前画面。
 *
 * 运行：node tests/subtitles.test.js
 */

const fs = require("fs");
const path = require("path");
const vm = require("vm");

/* ------------------------------------------------------------ 迷你 DOM
 * 与 tests/suggestions.test.js 同一套：解析 innerHTML 片段，使按钮成为可检查
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
      if (top) {
        top._text += m[5];
        // textarea 的标签内容即其默认值（与浏览器一致），字幕表单预填依赖于此
        if (top.tagName === "TEXTAREA") top._value += m[5];
      }
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
      this.byId.set(id, el);
    }
    return this.byId.get(id);
  }
  querySelector(sel) {
    if (sel.startsWith("#")) return this.el(sel.slice(1));
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
const screenCode = fs.readFileSync(
  path.join(__dirname, "..", "app", "static", "screen.js"),
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

function loadScreen() {
  const document = new Doc();
  const pending = [];
  const intervals = [];
  const sandbox = {
    document,
    fetch: (p, options) =>
      new Promise((resolve, reject) => pending.push({ path: p, options, resolve, reject })),
    setInterval: (fn, ms) => { intervals.push({ fn, ms }); return intervals.length; },
    console,
  };
  vm.createContext(sandbox);
  vm.runInContext(screenCode, sandbox, { filename: "screen.js" });
  return { document, pending, intervals, sandbox };
}

const flush = () => new Promise((r) => setImmediate(r));
const lastRequest = (ctx) => ctx.pending[ctx.pending.length - 1];

async function respond(req, data, status = 200) {
  req.resolve({ ok: status >= 200 && status < 300, status, json: async () => data });
  await flush();
}

/* ------------------------------------------------------------ 测试数据 */

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
    subtitles: [],
    verify: { enabled: false, round: 0, cue_id: null, lighting: null, sound: null, all_ready: false },
    auto: { enabled: false, interval: 10, remaining_seconds: null, waiting_ready: false },
    total: 2,
    remaining: 2,
    min_cues: 2,
    max_cues: 12,
  }, over || {});
}

/* 通过真实元素派发文档级委托点击（与浏览器一致，disabled 按钮在委托处被挡）。 */
function clickButton(ctx, btn) {
  ctx.document.dispatch("click", { target: btn });
}

const subButton = (ctx, cueId) =>
  ctx.document.querySelectorAll(
    `#pending-list button[data-action="add-subtitle"][data-id="${cueId}"]`
  )[0] || null;

let failures = 0;
function check(cond, msg) {
  if (cond) console.log("  ✓ " + msg);
  else { failures++; console.error("  ✗ " + msg); }
}

/* ------------------------------------------------------------ 用例：执行台 */

async function t1_entryAndPermission() {
  console.log("P1 待发提示后有 💬：控制席可点、只读页禁用");
  const ctx = loadApp();
  await respond(lastRequest(ctx), liveState());
  check(!!subButton(ctx, 1), "渲染出字幕按钮 💬");
  check(subButton(ctx, 1).disabled === false, "控制席页面 💬 可点");

  const ro = loadApp();
  await respond(lastRequest(ro), liveState({ control_enabled: true, control_granted: false }));
  check(subButton(ro, 1).disabled === true, "只读页 💬 禁用（受控制席保护）");
}

async function t2_markerShowsValidity() {
  console.log("P2 字幕行显示设置及有效性（有效 / 已失效）");
  const ctx = loadApp();
  await respond(lastRequest(ctx), liveState({
    subtitles: [
      { cue_id: 1, text: "第一幕字幕", valid: true },
      { cue_id: 2, text: "失效的字幕", valid: false },
    ],
  }));
  const html = ctx.document.el("pending-list").innerHTML;
  check(html.includes("💬 字幕：第一幕字幕"), "有效字幕行显示正文");
  check(html.includes("字幕已失效"), "失效字幕显示失效标记");
  check(html.includes("失效的字幕"), "失效字幕仍显示原设置正文");
  check(subButton(ctx, 1).classList.contains("has-sub"), "有效字幕的 💬 按钮高亮");
  check(!subButton(ctx, 2).classList.contains("has-sub"), "失效字幕的 💬 按钮不高亮");
}

async function t3_formPrefillAndSave() {
  console.log("P3 点 💬 展开表单（预填现有字幕），保存请求体正确");
  const ctx = loadApp();
  await respond(lastRequest(ctx), liveState({
    subtitles: [{ cue_id: 2, text: "已有字幕", valid: true }],
  }));
  clickButton(ctx, subButton(ctx, 2));
  const li = ctx.document.querySelectorAll('#pending-list .subtitle-edit')[0];
  check(!!li, "待发列表内展开字幕表单");
  const input = ctx.document.querySelectorAll('#pending-list .subtitle-input')[0];
  check(input.value === "已有字幕", "表单预填现有字幕");
  check(!!ctx.document.querySelectorAll(
    '#pending-list button[data-action="clear-subtitle"]'
  )[0], "已有设置时显示清除按钮");

  input.value = "  改写后的字幕  ";
  clickButton(ctx, ctx.document.querySelectorAll(
    '#pending-list button[data-action="save-subtitle"]'
  )[0]);
  const req = lastRequest(ctx);
  check(req.path === "/api/subtitles", "保存请求提交到 /api/subtitles");
  const body = JSON.parse(req.options.body);
  check(body.cue_id === 2 && body.version === 3 && body.text === "改写后的字幕",
    "请求体携带 cue_id、所见版本号与去空白后的字幕");

  // 在途时保存 / 清除按钮禁用
  check(ctx.document.querySelectorAll(
    '#pending-list button[data-action="save-subtitle"]'
  )[0].disabled === true, "请求在途时保存按钮禁用防重复");

  // 成功响应：版本 +1，字幕更新
  await respond(req, liveState({
    version: 4,
    subtitles: [{ cue_id: 2, text: "改写后的字幕", valid: true }],
  }));
  check(ctx.document.querySelectorAll('#pending-list .subtitle-edit').length === 0,
    "成功后退出字幕表单");
  check(ctx.document.el("pending-list").innerHTML.includes("💬 字幕：改写后的字幕"),
    "列表显示新字幕");
  check(ctx.document.el("version-badge").textContent === "v4", "版本角标 +1");
}

async function t4_emptyNoRequestAndClear() {
  console.log("P4 空白不发请求；清除提交 text: null");
  const ctx = loadApp();
  await respond(lastRequest(ctx), liveState());
  clickButton(ctx, subButton(ctx, 1));
  const input = ctx.document.querySelectorAll('#pending-list .subtitle-input')[0];
  input.value = "   ";
  const before = ctx.pending.length;
  clickButton(ctx, ctx.document.querySelectorAll(
    '#pending-list button[data-action="save-subtitle"]'
  )[0]);
  check(ctx.pending.length === before, "空白字幕不发请求（前端拦截）");
  check(ctx.document.querySelectorAll('#pending-list .subtitle-edit').length === 1,
    "表单保留，可继续输入");
  check(ctx.document.querySelectorAll(
    '#pending-list button[data-action="clear-subtitle"]'
  ).length === 0, "无设置时不显示清除按钮");

  // 有设置时清除：text 为 null
  const ctx2 = loadApp();
  await respond(lastRequest(ctx2), liveState({
    subtitles: [{ cue_id: 1, text: "待清除", valid: true }],
  }));
  clickButton(ctx2, subButton(ctx2, 1));
  clickButton(ctx2, ctx2.document.querySelectorAll(
    '#pending-list button[data-action="clear-subtitle"]'
  )[0]);
  const req = lastRequest(ctx2);
  check(JSON.parse(req.options.body).text === null, "清除请求体 text 为 null");
  check(JSON.parse(req.options.body).cue_id === 1, "清除请求携带 cue_id");
  await respond(req, liveState({ version: 4, subtitles: [] }));
  check(ctx2.document.el("pending-list").innerHTML.includes("💬 字幕：") === false,
    "清除成功后列表不再显示字幕行");
}

async function t5_readonlyCannotSave() {
  console.log("P5 只读页：💬 禁用；当前席保存携带凭据");
  const seat = loadApp({ cueControlToken: "tok-seat" });
  await respond(lastRequest(seat), liveState({
    control_enabled: true, control_granted: true,
  }));
  clickButton(seat, subButton(seat, 1));
  seat.document.querySelectorAll('#pending-list .subtitle-input')[0].value = "带凭据的字幕";
  clickButton(seat, seat.document.querySelectorAll(
    '#pending-list button[data-action="save-subtitle"]'
  )[0]);
  const req = lastRequest(seat);
  check(req.options.headers["X-Control-Token"] === "tok-seat", "当前席保存携带凭据头");
  await respond(req, liveState({
    version: 4, control_enabled: true, control_granted: true,
    subtitles: [{ cue_id: 1, text: "带凭据的字幕", valid: true }],
  }));
}

async function t6_conflictClosesForm() {
  console.log("P6 保存遇 409：应用最新状态，目标已发出时表单关闭");
  const ctx = loadApp();
  await respond(lastRequest(ctx), liveState());
  clickButton(ctx, subButton(ctx, 1));
  ctx.document.querySelectorAll('#pending-list .subtitle-input')[0].value = "字幕";
  clickButton(ctx, ctx.document.querySelectorAll(
    '#pending-list button[data-action="save-subtitle"]'
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
  check(ctx.document.querySelectorAll('#pending-list .subtitle-edit').length === 0,
    "目标已不在待发：字幕表单关闭（编辑态被清理）");
}

/* ------------------------------------------------------------ 用例：字幕屏 */

async function t7_screenShowsAndClears() {
  console.log("S1 字幕屏：轮询上屏、null 清屏");
  const ctx = loadScreen();
  check(ctx.intervals.length === 1 && ctx.intervals[0].ms === 1000, "启动每秒轮询");
  const req0 = lastRequest(ctx);
  check(req0 && req0.path === "/api/screen", "打开即请求观众接口");
  const el = ctx.document.el("screen-text");
  await respond(req0, { seq: 1, text: "第一幕字幕" });
  check(el.textContent === "第一幕字幕", "字幕上屏");
  check(el.classList.contains("blank") === false, "有字幕时不为清屏态");

  // 下一轮轮询：清屏
  ctx.intervals[0].fn();
  const req1 = lastRequest(ctx);
  await respond(req1, { seq: 2, text: null });
  check(el.textContent === "", "null 字幕清屏");
  check(el.classList.contains("blank") === true, "清屏态标记生效");
}

async function t8_lateResponseNoRollback() {
  console.log("S2 字幕屏：迟到的旧序号响应不倒退画面");
  const ctx = loadScreen();
  const el = ctx.document.el("screen-text");
  // 两次轮询同时在途：新响应先回来，旧响应后回来
  const reqOld = ctx.pending[0];
  ctx.intervals[0].fn();
  const reqNew = lastRequest(ctx);
  await respond(reqNew, { seq: 5, text: "新画面" });
  check(el.textContent === "新画面", "新响应上屏");
  await respond(reqOld, { seq: 3, text: "旧画面" });
  check(el.textContent === "新画面", "迟到的旧响应不倒退画面");
  // 相同序号重复到达也不覆盖（含清屏倒退）
  ctx.intervals[0].fn();
  const reqDup = lastRequest(ctx);
  await respond(reqDup, { seq: 5, text: null });
  check(el.textContent === "新画面", "同序号重复响应不覆盖画面");
}

async function t9_networkFailureKeepsScreen() {
  console.log("S3 字幕屏：网络失败 / 非 200 保持当前画面");
  const ctx = loadScreen();
  const el = ctx.document.el("screen-text");
  await respond(ctx.pending[0], { seq: 2, text: "保持我" });
  check(el.textContent === "保持我", "初始上屏");
  // 网络失败
  ctx.intervals[0].fn();
  const reqFail = lastRequest(ctx);
  reqFail.reject(new Error("network down"));
  await flush();
  check(el.textContent === "保持我", "网络失败保持画面");
  // 非 200
  ctx.intervals[0].fn();
  const req500 = lastRequest(ctx);
  await respond(req500, {}, 500);
  check(el.textContent === "保持我", "非 200 保持画面");
}

(async function main() {
  const cases = [
    t1_entryAndPermission,
    t2_markerShowsValidity,
    t3_formPrefillAndSave,
    t4_emptyNoRequestAndClear,
    t5_readonlyCannotSave,
    t6_conflictClosesForm,
    t7_screenShowsAndClears,
    t8_lateResponseNoRollback,
    t9_networkFailureKeepsScreen,
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
