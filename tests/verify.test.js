"use strict";

/* 本场核验前端回归测试（纯 Node，无第三方依赖）。
 *
 * 通过 vm 加载真实的 app/static/app.js，用迷你 DOM 模拟页面元素与事件，
 * 用可控 fetch 代替网络（记录请求体与请求头）。
 *
 * 覆盖：设置页启用入口（未启用显示按钮、已启用显示标记、只读页禁用）；
 * 演出页核验卡片（未启用/旧状态缺字段时隐藏，启用后显示当前轮次与两工种
 * 状态）；确认按钮的就绪门槛（未全部就绪禁用并提示待就绪工种，全部就绪
 * 解禁）；就绪回报请求体（工种、cue_id、轮次、就绪布尔值）与凭据处理
 * （无凭据页不带请求头；当前席携带 X-Control-Token，成功与 409 后资格与
 * sessionStorage 凭据保留、页面不转只读、两工种就绪可直接确认；被接管旧页
 * 的成功与 409 响应回报 control_granted=false，失效缓存被清除、页面转只读）；
 * 已就绪再点报未就绪（取消）。
 *
 * 交错回归（T16–T17）：回报与待发重排同时在途时，任一请求先结束（成功 /
 * 409 / HTTP 错误 / 网络失败）都只释放自身的在途占用——另一请求未结束前
 * 两工种回报按钮保持禁用、重复点击不发第二个回报；全部结束后按最新状态
 * 恢复，不提前解禁也不永久锁住。
 *
 * 运行：node tests/verify.test.js
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
  set value(v) { this._value = v; }
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

function verifyView(over) {
  return Object.assign({
    enabled: true,
    round: 2,
    cue_id: 1,
    lighting: null,
    sound: null,
    all_ready: false,
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

/* 模拟点击委托按钮（迷你 DOM 不解析 innerHTML，直接构造事件目标）。 */
function clickAction(ctx, dataset) {
  const fakeBtn = { dataset, disabled: false };
  ctx.document.dispatch("click", {
    target: { closest: (sel) => (sel === "[data-action]" ? fakeBtn : null) },
  });
}

/* 取核验卡片里真实渲染出来的某工种回报按钮（disabled 即渲染时的属性）。 */
function reportButton(ctx, dept) {
  return ctx.document.querySelectorAll(
    `#verify-card button[data-action="verify-report"][data-dept="${dept}"]`
  )[0] || null;
}

function allReportButtons(ctx) {
  return ctx.document.querySelectorAll('#verify-card button[data-action="verify-report"]');
}

/* 通过真实按钮元素派发委托点击：与浏览器一致，按钮 disabled 时处理函数早退。 */
function clickButton(ctx, btn) {
  ctx.document.dispatch("click", { target: btn });
}

/* 取待发列表里真实渲染出来的重排按钮（↑ 上移 / ↓ 下移）。 */
function reorderButton(ctx, action, idx) {
  return ctx.document.querySelectorAll(
    `#pending-list button[data-action="${action}"][data-idx="${idx}"]`
  )[0] || null;
}

/* 已发出的回报请求数量：交错场景下检查是否重复提交相同回报。 */
function reportRequestCount(ctx) {
  return ctx.pending.filter((r) => r.path === "/api/verify/report").length;
}

/* 让在途请求以网络错误（fetch reject）结束。 */
async function rejectRequest(req, message = "network down") {
  req.reject(new Error(message));
  await flush();
}

/* 让在途请求返回非 2xx 业务错误（如 400），无附带状态。 */
async function respondError(req, status = 400, data = { error: "bad request" }) {
  req.resolve({ ok: false, status, json: async () => data });
  await flush();
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

async function t1_setupEnableEntry() {
  console.log("T1 设置页：未启用显示启用按钮，点击携带版本号启用");
  const ctx = loadApp();
  await respond(lastRequest(ctx), setupState({ verify: verifyView({ enabled: false, round: 0 }) }));

  check(ctx.document.el("verify-setup-badge").textContent === "本场核验：未启用", "标记显示未启用");
  check(!ctx.document.el("verify-enable-btn").classList.contains("hidden"), "启用按钮可见");
  check(ctx.document.el("verify-enable-btn").disabled === false, "可写时启用按钮可用");

  ctx.document.el("verify-enable-btn").dispatch("click");
  const req = lastRequest(ctx);
  check(req.path === "/api/verify/enable", "点击后请求启用接口");
  check(JSON.parse(req.options.body).version === 3, "启用携带当前版本号");
  await respond(req, setupState({ version: 4, verify: verifyView({ enabled: true, round: 0 }) }));

  check(ctx.document.el("verify-setup-badge").textContent === "本场核验：已启用 ✓", "启用后标记为已启用");
  check(ctx.document.el("verify-enable-btn").classList.contains("hidden"), "已启用后按钮隐藏");
  check(ctx.document.el("version-badge").textContent === "v4", "启用后版本 +1");
}

async function t2_setupEnableReadonlyDisabled() {
  console.log("T2 设置页：只读页（非控制席）启用按钮禁用");
  const ctx = loadApp();
  await respond(lastRequest(ctx), setupState({
    control_enabled: true,
    control_granted: false,
    verify: verifyView({ enabled: false, round: 0 }),
  }));
  check(ctx.document.el("verify-enable-btn").disabled === true, "只读页启用按钮禁用（受控制席保护）");
}

async function t3_liveCardHiddenWhenDisabled() {
  console.log("T3 演出页：核验未启用（含旧状态无 verify 字段）时卡片隐藏");
  const ctx = loadApp();
  await respond(lastRequest(ctx), liveState({ verify: verifyView({ enabled: false }) }));
  check(ctx.document.el("verify-card").classList.contains("hidden"), "未启用时核验卡片隐藏");

  const legacy = loadApp();
  const noVerify = liveState();
  delete noVerify.verify; // 旧服务端状态没有 verify 字段
  await respond(lastRequest(legacy), noVerify);
  check(legacy.document.el("verify-card").classList.contains("hidden"), "缺 verify 字段时按未启用处理");
  check(legacy.document.el("confirm-btn").disabled === false, "旧状态下确认按钮不受核验门槛影响");
}

async function t4_liveCardShowsRoundAndDepts() {
  console.log("T4 演出页：卡片显示当前轮次与两工种状态");
  const ctx = loadApp();
  await respond(lastRequest(ctx), liveState({ verify: verifyView({ lighting: true }) }));

  const html = ctx.document.el("verify-card").innerHTML;
  check(!ctx.document.el("verify-card").classList.contains("hidden"), "启用后核验卡片显示");
  check(html.includes("第 2 轮"), "显示当前轮次");
  check(html.includes("灯光"), "显示灯光工种");
  check(html.includes("音响"), "显示音响工种");
  check(html.includes("就绪 ✓"), "已报就绪工种显示就绪");
  check(html.includes("未回报"), "未回报工种显示未回报");
  check(html.includes("待灯光、音响都报就绪后才能确认发出"), "未全部就绪时给出提示");
}

async function t5_confirmGateAndLabels() {
  console.log("T5 确认门槛：未全部就绪禁用并提示工种，全部就绪解禁");
  const ctx = loadApp();
  await respond(lastRequest(ctx), liveState({ verify: verifyView() }));
  check(ctx.document.el("confirm-btn").disabled === true, "两工种未回报：确认禁用");
  check(ctx.document.el("current-card").innerHTML.includes("待灯光、音响就绪"), "按钮提示待两工种就绪");

  const ctx2 = loadApp();
  await respond(lastRequest(ctx2), liveState({ verify: verifyView({ lighting: true }) }));
  check(ctx2.document.el("confirm-btn").disabled === true, "仅灯光就绪：确认仍禁用");
  check(ctx2.document.el("current-card").innerHTML.includes("待音响就绪"), "按钮提示待音响就绪");

  const ctx3 = loadApp();
  await respond(lastRequest(ctx3), liveState({
    verify: verifyView({ lighting: true, sound: true, all_ready: true }),
  }));
  check(ctx3.document.el("confirm-btn").disabled === false, "两工种就绪：确认解禁");
  check(ctx3.document.el("current-card").innerHTML.includes("确认发出 ✓"), "按钮恢复确认发出");
  const cardHtml = ctx3.document.el("verify-card").innerHTML;
  check(cardHtml.includes("两工种均已就绪，可确认发出"), "卡片提示可确认发出");
}

async function t6_reportRequestBodyAndNoToken() {
  console.log("T6 就绪回报：请求体含工种/cue_id/轮次/布尔值，只读页也可回报");
  // 只读页（启用控制席且本页无凭据）：回报不需要控制凭据
  const ctx = loadApp();
  await respond(lastRequest(ctx), liveState({
    control_enabled: true,
    control_granted: false,
    verify: verifyView(),
  }));
  check(ctx.document.el("confirm-btn").disabled === true, "只读页确认禁用（控制席规则不变）");

  clickAction(ctx, { action: "verify-report", dept: "lighting" });
  const req = lastRequest(ctx);
  check(req.path === "/api/verify/report", "点击报就绪请求回报接口");
  check(!("X-Control-Token" in req.options.headers), "回报不携带控制凭据（无需凭据）");
  const body = JSON.parse(req.options.body);
  check(body.department === "lighting", "提交工种 lighting");
  check(body.cue_id === 1, "提交当前提示 cue_id");
  check(body.round === 2, "提交当前轮次");
  check(body.ready === true, "未就绪时点击报就绪（true）");
  // 成功响应：版本不变，灯光就绪
  await respond(req, liveState({
    version: 3,
    control_enabled: true,
    control_granted: false,
    verify: verifyView({ lighting: true }),
  }));
  check(ctx.document.el("version-badge").textContent === "v3", "回报不改全局版本号");
  check(ctx.document.el("verify-card").innerHTML.includes("就绪 ✓"), "回报后灯光显示就绪");

  // 已就绪再点：取消就绪（ready: false）
  clickAction(ctx, { action: "verify-report", dept: "lighting" });
  const req2 = lastRequest(ctx);
  check(JSON.parse(req2.options.body).ready === false, "已就绪再点报未就绪（取消）");
  await respond(req2, liveState({
    version: 3,
    control_enabled: true,
    control_granted: false,
    verify: verifyView({ lighting: false }),
  }));
  check(ctx.document.el("verify-card").innerHTML.includes("未就绪"), "取消后显示未就绪");

  // 无凭据页遇过期 409：刷新状态且 control_granted=false（本来就无凭据可清）
  clickAction(ctx, { action: "verify-report", dept: "sound" });
  const req3 = lastRequest(ctx);
  check(!("X-Control-Token" in req3.options.headers), "无凭据页 409 场景仍不带头");
  await respond(req3, {
    error: "conflict",
    message: "操作已过期",
    state: liveState({
      version: 4,
      control_enabled: true,
      control_granted: false,
      verify: verifyView({ round: 3, cue_id: 2 }),
    }),
  }, 409);
  check(ctx.document.el("verify-card").innerHTML.includes("第 3 轮"), "无凭据页 409 后刷新为最新状态");
  check(ctx.document.el("control-badge").textContent.includes("只读"), "无凭据页仍为只读");
}

async function t9_currentSeatKeepsToken() {
  console.log("T9 当前控制席：成功与取消就绪后资格与 sessionStorage 凭据保留，可直接确认");
  const ctx = loadApp({ cueControlToken: "seat-token" });
  await respond(lastRequest(ctx), liveState({
    control_enabled: true,
    control_granted: true,
    verify: verifyView(),
  }));
  check(ctx.document.el("control-badge").textContent.includes("本页"), "初始为本页控制");
  check(ctx.storage.getItem("cueControlToken") === "seat-token", "sessionStorage 有当前凭据");

  clickAction(ctx, { action: "verify-report", dept: "lighting" });
  const req = lastRequest(ctx);
  check(req.options.headers["X-Control-Token"] === "seat-token", "当前席回报携带 X-Control-Token");
  await respond(req, liveState({
    control_enabled: true,
    control_granted: true,
    verify: verifyView({ lighting: true }),
  }));
  check(ctx.storage.getItem("cueControlToken") === "seat-token", "成功后 sessionStorage 凭据保留");
  check(ctx.document.el("control-badge").textContent.includes("本页"), "成功后仍为本页控制（未变只读）");
  check(ctx.document.el("confirm-btn").disabled === true, "单工种就绪：确认仍禁用");

  clickAction(ctx, { action: "verify-report", dept: "sound" });
  await respond(lastRequest(ctx), liveState({
    control_enabled: true,
    control_granted: true,
    verify: verifyView({ lighting: true, sound: true, all_ready: true }),
  }));
  check(ctx.storage.getItem("cueControlToken") === "seat-token", "两工种就绪后凭据仍保留");
  check(ctx.document.el("confirm-btn").disabled === false, "两工种就绪且未暂停：可直接确认");

  // 已就绪再点：取消就绪（ready=false），资格依旧保留
  clickAction(ctx, { action: "verify-report", dept: "sound" });
  const req2 = lastRequest(ctx);
  check(JSON.parse(req2.options.body).ready === false, "当前席已就绪再点为取消就绪");
  check(req2.options.headers["X-Control-Token"] === "seat-token", "取消就绪同样携带凭据");
  await respond(req2, liveState({
    control_enabled: true,
    control_granted: true,
    verify: verifyView({ lighting: true, sound: false }),
  }));
  check(ctx.storage.getItem("cueControlToken") === "seat-token", "取消就绪后凭据保留");
  check(ctx.document.el("control-badge").textContent.includes("本页"), "取消就绪后仍为本页控制");
  check(ctx.document.el("confirm-btn").disabled === true, "取消后缺就绪：确认重新禁用");
}

async function t10_currentSeatConflictKeepsSeat() {
  console.log("T10 当前控制席遇过期 409：凭据与资格保留，页面不转只读");
  const ctx = loadApp({ cueControlToken: "seat-token" });
  await respond(lastRequest(ctx), liveState({
    control_enabled: true,
    control_granted: true,
    verify: verifyView(),
  }));
  clickAction(ctx, { action: "verify-report", dept: "lighting" });
  const req = lastRequest(ctx);
  check(req.options.headers["X-Control-Token"] === "seat-token", "409 场景同样携带凭据");
  await respond(req, {
    error: "conflict",
    message: "操作已过期",
    state: liveState({
      version: 9,
      control_enabled: true,
      control_granted: true,
      verify: verifyView({ round: 4, cue_id: 2 }),
    }),
  }, 409);
  check(ctx.document.el("version-badge").textContent === "v9", "409 后刷新为最新状态");
  check(ctx.document.el("verify-card").innerHTML.includes("第 4 轮"), "409 后显示最新轮次");
  check(ctx.storage.getItem("cueControlToken") === "seat-token", "当前席 409 后凭据保留（未被清除）");
  check(ctx.document.el("control-badge").textContent.includes("本页"), "当前席 409 后仍为本页控制（非只读）");
  // 后续请求仍带凭据
  clickAction(ctx, { action: "verify-report", dept: "lighting" });
  check(lastRequest(ctx).options.headers["X-Control-Token"] === "seat-token",
    "409 后后续回报仍携带凭据");
}

async function t11_takenOverOldPageClearsCache() {
  console.log("T11 被接管旧页：成功与 409 的 control_granted=false，失效缓存清除、页面转只读");
  // 场景一：旧凭据回报成功（服务端不 403，但回报 control_granted=false）
  const ctx = loadApp({ cueControlToken: "stale-token" });
  await respond(lastRequest(ctx), liveState({
    control_enabled: true,
    control_granted: true, // 回报前页面仍自认为控制席（尚未感知被接管）
    verify: verifyView(),
  }));
  clickAction(ctx, { action: "verify-report", dept: "lighting" });
  await respond(lastRequest(ctx), liveState({
    control_enabled: true,
    control_granted: false, // 凭据已被接管替换：按真实资格回报
    verify: verifyView({ lighting: true }),
  }));
  check(ctx.storage.getItem("cueControlToken") === null, "成功响应后旧页失效缓存被清除");
  check(ctx.document.el("control-badge").textContent.includes("只读"), "成功响应后旧页转只读");
  check(ctx.document.el("confirm-btn").disabled === true, "旧页确认禁用");
  // 失效缓存清除后，后续回报不再带凭据（也不 403，仍可继续回报）
  clickAction(ctx, { action: "verify-report", dept: "sound" });
  const req2 = lastRequest(ctx);
  check(!("X-Control-Token" in req2.options.headers), "缓存清除后后续回报不再携带旧凭据");
  await respond(req2, liveState({
    control_enabled: true,
    control_granted: false,
    verify: verifyView({ lighting: true, sound: true, all_ready: true }),
  }));
  check(ctx.document.el("confirm-btn").disabled === true, "旧页两工种就绪仍只读（确认禁用）");

  // 场景二：旧凭据遇过期 409（state.control_granted=false），同样清缓存、转只读
  const ctx2 = loadApp({ cueControlToken: "stale-token" });
  await respond(lastRequest(ctx2), liveState({
    control_enabled: true,
    control_granted: true,
    verify: verifyView(),
  }));
  clickAction(ctx2, { action: "verify-report", dept: "sound" });
  await respond(lastRequest(ctx2), {
    error: "conflict",
    message: "操作已过期",
    state: liveState({
      version: 8,
      control_enabled: true,
      control_granted: false,
      verify: verifyView({ round: 5, cue_id: 3 }),
    }),
  }, 409);
  check(ctx2.storage.getItem("cueControlToken") === null, "409 后旧页失效缓存被清除");
  check(ctx2.document.el("control-badge").textContent.includes("只读"), "409 后旧页转只读");
  check(ctx2.document.el("verify-card").innerHTML.includes("第 5 轮"), "409 后仍刷新为最新轮次");
}

async function t7_reportConflictRefreshes() {
  console.log("T7 回报遇 409（轮次过期）：页面刷新为最新状态");
  const ctx = loadApp();
  await respond(lastRequest(ctx), liveState({ verify: verifyView() }));
  clickAction(ctx, { action: "verify-report", dept: "sound" });
  const req = lastRequest(ctx);
  check(JSON.parse(req.options.body).department === "sound", "提交工种 sound");
  await respond(req, {
    error: "conflict",
    message: "操作已过期",
    state: liveState({ version: 5, verify: verifyView({ round: 3, cue_id: 2 }) }),
  }, 409);
  check(ctx.document.el("version-badge").textContent === "v5", "409 后页面刷新为最新状态");
  check(ctx.document.el("verify-card").innerHTML.includes("第 3 轮"), "409 后显示最新轮次");
}

async function t8_pausedCanReport() {
  console.log("T8 暂停中：回报按钮可用，确认仍禁用");
  const ctx = loadApp();
  await respond(lastRequest(ctx), liveState({
    paused: true,
    verify: verifyView({ lighting: true }),
  }));
  check(ctx.document.el("confirm-btn").disabled === true, "暂停中确认禁用");
  const html = ctx.document.el("verify-card").innerHTML;
  check(html.includes("暂停中（可继续回报）"), "卡片提示暂停中可继续回报");
  check(!/data-dept="sound"[^>]*disabled/.test(html), "暂停中回报按钮可用");
  clickAction(ctx, { action: "verify-report", dept: "sound" });
  const req = lastRequest(ctx);
  check(req.path === "/api/verify/report", "暂停中可发出回报请求");
  await respond(req, liveState({
    paused: true,
    verify: verifyView({ lighting: true, sound: true, all_ready: true }),
  }));
  check(ctx.document.el("confirm-btn").disabled === true, "全部就绪但暂停中确认仍禁用");
}

async function t12_openShowEnablesReportButtons() {
  console.log("T12 回归：同页启用控制席+核验、录入两条提示开场后，回报按钮立即可点");
  // 这是线上故障路径：开场响应在 busy 仍为 true 时重建核验卡片，旧代码渲染出
  // disabled 后再没有任何地方恢复，导致开场成功仍无法点击「报就绪」。
  const ctx = loadApp({ cueControlToken: "seat-token" });
  await respond(lastRequest(ctx), setupState({
    version: 5,
    control_enabled: true,
    control_granted: true,
    verify: verifyView({ enabled: true, round: 0 }),
  }));
  // 开场前不渲染演出页核验卡片回报按钮
  check(allReportButtons(ctx).length === 0, "开场前不渲染回报按钮（无演出中卡片）");

  const setupReq = startShowWithTwoCues(ctx);
  check(setupReq.path === "/api/setup", "录入两条后请求 /api/setup");
  check(setupReq.options.headers["X-Control-Token"] === "seat-token", "当前席开场携带凭据");

  const live = liveState({
    version: 6,
    control_enabled: true,
    control_granted: true,
    verify: verifyView({ enabled: true, round: 1, cue_id: 1, lighting: null, sound: null }),
  });
  await respond(setupReq, live);

  check(!ctx.document.el("verify-card").classList.contains("hidden"), "开场成功后核验卡片显示");
  const light = reportButton(ctx, "lighting");
  const sound = reportButton(ctx, "sound");
  check(!!light && !!sound, "卡片中真实渲染出灯光/音响两个回报按钮");
  check(light.disabled === false, "开场成功后灯光「报就绪」可点击（不再卡死）");
  check(sound.disabled === false, "开场成功后音响「报就绪」可点击（不是始终禁用）");
  check(light.textContent.trim() === "报就绪", "未回报时按钮文案为报就绪");

  // 开场后立刻点真实按钮：请求应正常发出
  clickButton(ctx, light);
  const reportReq = lastRequest(ctx);
  check(reportReq.path === "/api/verify/report", "开场后点击真实按钮发出回报");
  const body = JSON.parse(reportReq.options.body);
  check(body.department === "lighting" && body.cue_id === 1 && body.round === 1 && body.ready === true,
    "回报内容为第 1 轮当前提示的灯光就绪");
  await respond(reportReq, liveState({
    version: 6,
    control_enabled: true,
    control_granted: true,
    verify: verifyView({ round: 1, lighting: true }),
  }));
  check(reportButton(ctx, "lighting").disabled === false, "首次回报成功后按钮仍可继续操作");
  check(reportButton(ctx, "lighting").textContent.trim() === "取消就绪", "已就绪后文案切换为取消就绪");
  check(ctx.storage.getItem("cueControlToken") === "seat-token", "当前席凭据未被误清");
}

async function t13_inFlightBlocksAndSuccessReenables() {
  console.log("T13 在途请求禁用两个回报按钮防重复；成功后按最新状态恢复，可连续回报/取消");
  const ctx = loadApp();
  await respond(lastRequest(ctx), liveState({ verify: verifyView() }));
  check(allReportButtons(ctx).every((b) => !b.disabled), "空闲时两工种回报按钮均可点");

  clickButton(ctx, reportButton(ctx, "lighting"));
  const req1 = lastRequest(ctx);
  check(req1.path === "/api/verify/report", "首次点击发出回报请求");
  check(allReportButtons(ctx).every((b) => b.disabled), "请求在途：灯光/音响按钮全部禁用");

  // 在途时再点真实按钮：委托处理函数见 disabled 早退，不产生重复请求
  const pendingAfterFirst = ctx.pending.length;
  clickButton(ctx, reportButton(ctx, "lighting"));
  clickButton(ctx, reportButton(ctx, "sound"));
  check(ctx.pending.length === pendingAfterFirst, "在途期间重复点击不发第二个请求");

  await respond(req1, liveState({ verify: verifyView({ lighting: true }) }));
  check(!reportButton(ctx, "lighting").disabled, "成功后灯光按钮恢复可点（可取消）");
  check(!reportButton(ctx, "sound").disabled, "成功后音响按钮恢复可点（可连续回报）");

  // 连续：音响报就绪
  clickButton(ctx, reportButton(ctx, "sound"));
  const req2 = lastRequest(ctx);
  check(allReportButtons(ctx).every((b) => b.disabled), "第二次回报在途：按钮再次全部禁用");
  await respond(req2, liveState({
    verify: verifyView({ lighting: true, sound: true, all_ready: true }),
  }));
  check(!reportButton(ctx, "lighting").disabled && !reportButton(ctx, "sound").disabled,
    "两工种就绪后两个按钮仍可点（可取消）");

  // 连续：灯光取消就绪
  clickButton(ctx, reportButton(ctx, "lighting"));
  const req3 = lastRequest(ctx);
  check(JSON.parse(req3.options.body).ready === false, "再点灯光为取消就绪");
  await respond(req3, liveState({ verify: verifyView({ lighting: false, sound: true }) }));
  check(!reportButton(ctx, "lighting").disabled && !reportButton(ctx, "sound").disabled,
    "取消成功后按钮同样恢复，不卡死");
  check(ctx.document.el("confirm-btn").disabled === true, "取消后缺就绪：确认重新禁用");
}

async function t14_conflictFailureAnd400Reenable() {
  console.log("T14 409 刷新、网络失败、400 业务错误后按钮都按最新状态恢复");
  const ctx = loadApp();
  await respond(lastRequest(ctx), liveState({ verify: verifyView() }));

  // 409（轮次过期）：applyState 刷新到新轮次，finally 恢复按钮
  clickButton(ctx, reportButton(ctx, "sound"));
  const req1 = lastRequest(ctx);
  await respond(req1, {
    error: "conflict",
    message: "操作已过期",
    state: liveState({
      version: 5,
      current: { id: 2, number: "A2", content: "音乐起" },
      next: null,
      pending: [{ id: 2, number: "A2", content: "音乐起" }],
      remaining: 1,
      verify: verifyView({ round: 3, cue_id: 2 }),
    }),
  }, 409);
  check(ctx.document.el("verify-card").innerHTML.includes("第 3 轮"), "409 后刷新为最新轮次");
  check(!reportButton(ctx, "lighting").disabled, "409 刷新后灯光按钮恢复可点");
  check(!reportButton(ctx, "sound").disabled, "409 刷新后音响按钮恢复可点");
  // 恢复后的请求应使用最新轮次与提示
  clickButton(ctx, reportButton(ctx, "lighting"));
  const after409 = lastRequest(ctx);
  const body = JSON.parse(after409.options.body);
  check(body.round === 3 && body.cue_id === 2, "409 恢复后的回报使用最新轮次与 cue_id");

  // 网络失败：状态不变，按钮也必须恢复
  clickButton(ctx, reportButton(ctx, "lighting"));
  const req2 = lastRequest(ctx);
  check(allReportButtons(ctx).every((b) => b.disabled), "失败请求在途：按钮禁用");
  await rejectRequest(req2);
  check(!reportButton(ctx, "lighting").disabled, "网络失败后灯光按钮恢复可点");
  check(!reportButton(ctx, "sound").disabled, "网络失败后音响按钮恢复可点");

  // 400 等非 409 错误（api 不刷新状态）：按钮同样恢复，不被错误响应卡死
  clickButton(ctx, reportButton(ctx, "sound"));
  const req3 = lastRequest(ctx);
  await respondError(req3, 400, { error: "工种须为 lighting 或 sound" });
  check(!reportButton(ctx, "lighting").disabled, "400 后灯光按钮恢复可点");
  check(!reportButton(ctx, "sound").disabled, "400 后音响按钮恢复可点");
}

async function t15_reportGatingStates() {
  console.log("T15 回报资格：暂停/无凭据/被接管旧页可回报；完成态与核验关闭不可回报");
  // 暂停中：按钮可用（确认仍禁用），回报在途禁用、结束恢复
  const paused = loadApp();
  await respond(lastRequest(paused), liveState({
    paused: true,
    control_enabled: true,
    control_granted: false,
    verify: verifyView(),
  }));
  check(!reportButton(paused, "lighting").disabled, "暂停中且无凭据页仍可回报");
  clickButton(paused, reportButton(paused, "sound"));
  check(allReportButtons(paused).every((b) => b.disabled), "暂停中回报在途同样禁用防重复");
  await respond(lastRequest(paused), liveState({
    paused: true,
    control_enabled: true,
    control_granted: false,
    verify: verifyView({ sound: true }),
  }));
  check(!reportButton(paused, "lighting").disabled && !reportButton(paused, "sound").disabled,
    "暂停中回报成功后按钮恢复");

  // 被接管旧页（缓存有失效凭据）：仍可回报；成功响应 control_granted=false
  // 清缓存转只读后，回报按钮依然可点（回报不看控制资格）
  const stale = loadApp({ cueControlToken: "stale-token" });
  await respond(lastRequest(stale), liveState({
    control_enabled: true,
    control_granted: true,
    verify: verifyView(),
  }));
  clickButton(stale, reportButton(stale, "lighting"));
  await respond(lastRequest(stale), liveState({
    control_enabled: true,
    control_granted: false,
    verify: verifyView({ lighting: true }),
  }));
  check(stale.storage.getItem("cueControlToken") === null, "被接管旧页失效缓存被清除");
  check(stale.document.el("confirm-btn").disabled === true, "旧页转只读后确认禁用");
  check(!reportButton(stale, "lighting").disabled, "旧页清缓存后灯光回报仍可点");
  check(!reportButton(stale, "sound").disabled, "旧页清缓存后音响回报仍可点");
  clickButton(stale, reportButton(stale, "sound"));
  check(lastRequest(stale).path === "/api/verify/report", "旧页只读状态下仍能继续回报");
  await respond(lastRequest(stale), liveState({
    control_enabled: true,
    control_granted: false,
    verify: verifyView({ lighting: true, sound: true, all_ready: true }),
  }));
  check(stale.document.el("confirm-btn").disabled === true, "旧页两工种就绪仍不能确认");

  // 完成态（全部已发出，无当前提示）：按钮禁用；真实点击不发请求
  const done = loadApp();
  await respond(lastRequest(done), liveState({
    status: "done",
    current: null,
    next: null,
    pending: [],
    remaining: 0,
    verify: verifyView({ cue_id: null, lighting: true, sound: true, all_ready: true }),
  }));
  check(reportButton(done, "lighting").disabled === true, "完成态灯光回报禁用");
  check(reportButton(done, "sound").disabled === true, "完成态音响回报禁用");
  const before = done.pending.length;
  clickButton(done, reportButton(done, "lighting"));
  check(done.pending.length === before, "完成态点击禁用按钮不发请求");

  // 核验关闭：卡片隐藏，无回报按钮
  const off = loadApp();
  await respond(lastRequest(off), liveState({ verify: verifyView({ enabled: false }) }));
  check(off.document.el("verify-card").classList.contains("hidden"), "核验关闭时卡片隐藏");
  check(allReportButtons(off).length === 0, "核验关闭时不渲染回报按钮");
}

async function t16_interleavedReorderFinishesFirst() {
  console.log("T16 回归：回报在途时点重排，重排先结束（网络失败 / 409）不解禁回报，全部结束后才恢复");
  // 故障路径：同页拥有控制权、核验开启且有当前提示；点灯光回报后再点待发下移，
  // 重排先失败——旧代码的单一 busy 布尔被重排的收尾复位，两工种按钮提前解禁，
  // 再点灯光会重复提交相同回报。
  const ctx = loadApp({ cueControlToken: "seat-token" });
  await respond(lastRequest(ctx), liveState({
    control_enabled: true,
    control_granted: true,
    verify: verifyView({ round: 1 }),
  }));
  check(!reorderButton(ctx, "down", 0).disabled, "当前席的待发下移按钮可点");

  // 第一轮交错：重排先网络失败
  clickButton(ctx, reportButton(ctx, "lighting"));
  const reportReq = lastRequest(ctx);
  check(reportReq.path === "/api/verify/report", "灯光回报请求已发出");
  check(allReportButtons(ctx).every((b) => b.disabled), "回报在途：两工种按钮禁用");

  clickButton(ctx, reorderButton(ctx, "down", 0));
  const reorderReq = lastRequest(ctx);
  check(reorderReq.path === "/api/reorder", "回报未结束时仍可发起待发重排");
  check(JSON.parse(reorderReq.options.body).order.join(",") === "2,1", "重排提交交换后的顺序");

  await rejectRequest(reorderReq); // 重排先失败：只能释放重排自身的在途占用
  check(allReportButtons(ctx).every((b) => b.disabled),
    "重排先网络失败：回报仍在途，两工种按钮保持禁用");
  const pendingBefore = ctx.pending.length;
  clickButton(ctx, reportButton(ctx, "lighting"));
  clickButton(ctx, reportButton(ctx, "sound"));
  check(ctx.pending.length === pendingBefore, "回报在途时点禁用按钮不发请求");
  check(reportRequestCount(ctx) === 1, "不重复提交相同回报（回报请求仍只有 1 个）");

  await respond(reportReq, liveState({ // 回报结束：全部请求结束，按最新状态恢复
    control_enabled: true,
    control_granted: true,
    verify: verifyView({ round: 1, lighting: true }),
  }));
  check(!reportButton(ctx, "lighting").disabled && !reportButton(ctx, "sound").disabled,
    "全部结束后两工种按钮按最新状态恢复");
  check(ctx.storage.getItem("cueControlToken") === "seat-token", "交错期间当前席凭据保留");

  // 第二轮交错：重排先 409（刷新最新状态），回报仍在途同样不解禁
  clickButton(ctx, reportButton(ctx, "lighting")); // 已就绪再点 = 取消就绪
  const reportReq2 = lastRequest(ctx);
  check(JSON.parse(reportReq2.options.body).ready === false, "恢复后再点灯光为取消就绪");
  clickButton(ctx, reorderButton(ctx, "down", 0));
  const reorderReq2 = lastRequest(ctx);
  await respond(reorderReq2, {
    error: "conflict",
    message: "操作已过期",
    state: liveState({
      version: 4,
      control_enabled: true,
      control_granted: true,
      verify: verifyView({ round: 1, lighting: true }),
    }),
  }, 409);
  check(ctx.document.el("version-badge").textContent === "v4", "重排 409 刷新为最新状态");
  check(allReportButtons(ctx).every((b) => b.disabled),
    "重排先 409：回报仍在途，两工种按钮保持禁用");
  clickButton(ctx, reportButton(ctx, "lighting"));
  check(reportRequestCount(ctx) === 2, "409 后回报在途时点击仍不发重复回报");

  await respond(reportReq2, liveState({
    version: 4,
    control_enabled: true,
    control_granted: true,
    verify: verifyView({ round: 1 }),
  }));
  check(!reportButton(ctx, "lighting").disabled && !reportButton(ctx, "sound").disabled,
    "第二轮全部结束后按钮恢复（不永久锁住）");
  check(reportRequestCount(ctx) === 2, "两轮交错均未重复提交回报");
}

async function t17_interleavedReportFinishesFirst() {
  console.log("T17 回归：回报先结束、重排仍在途时不解禁；重排结束后才按最新状态恢复");
  const ctx = loadApp({ cueControlToken: "seat-token" });
  await respond(lastRequest(ctx), liveState({
    control_enabled: true,
    control_granted: true,
    verify: verifyView({ round: 1 }),
  }));

  // 第一轮交错：回报先成功，重排（网络失败）后结束
  clickButton(ctx, reportButton(ctx, "lighting"));
  const reportReq = lastRequest(ctx);
  clickButton(ctx, reorderButton(ctx, "down", 0));
  const reorderReq = lastRequest(ctx);
  check(reorderReq.path === "/api/reorder", "回报在途时发起重排");

  await respond(reportReq, liveState({ // 回报先结束：只能释放回报自身的在途占用
    control_enabled: true,
    control_granted: true,
    verify: verifyView({ round: 1, lighting: true }),
  }));
  check(allReportButtons(ctx).every((b) => b.disabled),
    "回报先成功：重排仍在途，两工种按钮保持禁用");
  clickButton(ctx, reportButton(ctx, "sound"));
  check(reportRequestCount(ctx) === 1, "重排在途时点击音响不发重复回报");

  await rejectRequest(reorderReq); // 重排网络失败结束：全部结束，按最新状态恢复
  check(!reportButton(ctx, "lighting").disabled && !reportButton(ctx, "sound").disabled,
    "重排网络失败结束后按钮按最新状态恢复（不永久锁住）");
  check(reportButton(ctx, "lighting").textContent.trim() === "取消就绪",
    "恢复后状态为回报成功后的最新状态（灯光已就绪）");

  // 第二轮交错：回报先 400（HTTP 错误，不刷新状态），重排成功后结束
  clickButton(ctx, reportButton(ctx, "lighting")); // 取消就绪
  const reportReq2 = lastRequest(ctx);
  clickButton(ctx, reorderButton(ctx, "down", 0));
  const reorderReq2 = lastRequest(ctx);
  await respondError(reportReq2, 400, { error: "bad request" }); // 回报先失败：只释放自身占用
  check(allReportButtons(ctx).every((b) => b.disabled),
    "回报先 HTTP 错误：重排仍在途，两工种按钮保持禁用");
  clickButton(ctx, reportButton(ctx, "lighting"));
  check(reportRequestCount(ctx) === 2, "重排在途时点击仍不发重复回报");

  await respond(reorderReq2, liveState({ // 重排成功：换头开启新轮次并清空就绪
    version: 4,
    control_enabled: true,
    control_granted: true,
    current: { id: 2, number: "A2", content: "音乐起" },
    next: { id: 1, number: "A1", content: "灯光渐亮" },
    pending: [
      { id: 2, number: "A2", content: "音乐起" },
      { id: 1, number: "A1", content: "灯光渐亮" },
    ],
    verify: verifyView({ round: 2, cue_id: 2 }),
  }));
  check(!reportButton(ctx, "lighting").disabled && !reportButton(ctx, "sound").disabled,
    "第二轮全部结束后按钮恢复");
  check(ctx.document.el("verify-card").innerHTML.includes("第 2 轮"), "按重排后的最新状态显示新轮次");
  clickButton(ctx, reportButton(ctx, "lighting"));
  const body = JSON.parse(lastRequest(ctx).options.body);
  check(body.round === 2 && body.cue_id === 2 && body.ready === true,
    "恢复后的回报使用最新轮次与当前提示");
  check(reportRequestCount(ctx) === 3, "恢复后正常发出新回报（无重复）");
  await respond(lastRequest(ctx), liveState({
    version: 4,
    control_enabled: true,
    control_granted: true,
    current: { id: 2, number: "A2", content: "音乐起" },
    next: { id: 1, number: "A1", content: "灯光渐亮" },
    pending: [
      { id: 2, number: "A2", content: "音乐起" },
      { id: 1, number: "A1", content: "灯光渐亮" },
    ],
    verify: verifyView({ round: 2, cue_id: 2, lighting: true }),
  }));
  check(!reportButton(ctx, "lighting").disabled, "收尾后按钮仍可操作（不永久锁住）");
}

/* ------------------------------------------------------------ 主流程 */

(async function main() {
  const cases = [
    t1_setupEnableEntry,
    t2_setupEnableReadonlyDisabled,
    t3_liveCardHiddenWhenDisabled,
    t4_liveCardShowsRoundAndDepts,
    t5_confirmGateAndLabels,
    t6_reportRequestBodyAndNoToken,
    t7_reportConflictRefreshes,
    t8_pausedCanReport,
    t9_currentSeatKeepsToken,
    t10_currentSeatConflictKeepsSeat,
    t11_takenOverOldPageClearsCache,
    t12_openShowEnablesReportButtons,
    t13_inFlightBlocksAndSuccessReenables,
    t14_conflictFailureAnd400Reenable,
    t15_reportGatingStates,
    t16_interleavedReorderFinishesFirst,
    t17_interleavedReportFinishesFirst,
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
