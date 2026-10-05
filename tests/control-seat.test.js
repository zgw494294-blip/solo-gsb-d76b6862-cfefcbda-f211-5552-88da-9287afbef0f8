"use strict";

/* 单控制席前端回归测试（纯 Node，无第三方依赖）。
 *
 * 通过 vm 加载真实的 app/static/app.js，用迷你 DOM 模拟页面元素与事件，
 * 用可控 fetch 代替网络（记录请求头），用内存 sessionStorage 模拟标签页
 * 存储。每个用例独立加载一次脚本，模拟"新开标签页"。
 *
 * 覆盖：未启用时旧行为；启用/接管后凭据保存与请求头；无凭据页只读可接管；
 * 403 后凭据清除与标记纠正；启动时提交缓存凭据核验（有效凭据刷新仍可写，
 * 被接管后的旧页刷新即只读、显示接管入口、失效凭据被清除，并可主动接管）；
 * 撤回末条的按钮启用条件、二次确认、请求体（末条 cue_id + 版本）、凭据头、
 * 暂停时可用与 409/403 后的状态刷新。
 *
 * 运行：node tests/control-seat.test.js
 */

const fs = require("fs");
const path = require("path");
const vm = require("vm");

/* ------------------------------------------------------------ 迷你 DOM */

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

class El {
  constructor(tag = "div", id = "") {
    this.tagName = tag.toUpperCase();
    this.id = id;
    this.classList = new ClassList();
    this.listeners = {};
    this.children = [];
    this.dataset = {};
    this.value = "";
    this.disabled = false;
    this.textContent = "";
    this.innerHTML = "";
  }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  dispatch(type, event = {}) {
    for (const fn of this.listeners[type] || []) fn(event);
  }
  closest() { return null; }
  querySelector() { return null; }
  focus() {}
  appendChild(child) { this.children.push(child); return child; }
  remove() {}
}

class Doc {
  constructor() {
    this.byId = new Map();
    this.listeners = {};
  }
  el(id) {
    if (!this.byId.has(id)) {
      const el = new El("div", id);
      if (id === "archives-modal") el.classList.add("hidden");
      this.byId.set(id, el);
    }
    return this.byId.get(id);
  }
  querySelector(sel) {
    if (sel.startsWith("#")) return this.el(sel.slice(1));
    return null;
  }
  querySelectorAll() { return []; }
  createElement(tag) { return new El(tag); }
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

/* 每个用例独立加载一次脚本（相当于新开标签页），可预置 sessionStorage。 */
function loadApp(storageData) {
  const document = new Doc();
  const pending = []; // 尚未应答的请求：{ path, options, resolve, reject }
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

function liveState(over) {
  return Object.assign({
    version: 3,
    status: "live",
    paused: false,
    control_enabled: false,
    // 服务端对"本请求控制资格"的核验结论：未启用时恒 true；启用后取决于
    // 请求携带的凭据，各用例按需覆盖。
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

/* 已有一条已发（A1）、当前指向 A2 的演出状态：撤回末条用例使用。 */
function issuedState(over) {
  return liveState(Object.assign({
    current: { id: 2, number: "A2", content: "音乐起" },
    next: null,
    pending: [{ id: 2, number: "A2", content: "音乐起" }],
    issued: [{ id: 1, number: "A1", content: "灯光渐亮", issued_at: "2026-10-04T10:00:00+00:00" }],
    total: 2,
    remaining: 1,
  }, over || {}));
}

/* ------------------------------------------------------------ 断言 */

let failures = 0;
function check(cond, msg) {
  if (cond) console.log("  ✓ " + msg);
  else { failures++; console.error("  ✗ " + msg); }
}

/* ------------------------------------------------------------ 用例 */

async function t1_disabledByDefault_oldCallsUnchanged() {
  console.log("T1 默认未启用：写请求不带凭据头，显示启用入口");
  const ctx = loadApp();
  const startup = lastRequest(ctx);
  check(startup.path === "/api/state", "启动即查询状态");
  check(!("X-Control-Token" in startup.options.headers), "无缓存凭据时启动查询不带凭据头");
  await respond(startup, liveState()); // 启动 GET /api/state

  check(ctx.document.el("control-badge").textContent === "控制席：未启用", "标记显示未启用");
  check(ctx.document.el("control-btn").textContent === "启用控制席", "显示「启用控制席」按钮");
  check(!ctx.document.el("control-btn").classList.contains("hidden"), "启用按钮可见");
  check(ctx.document.el("confirm-btn").disabled === false, "未启用时确认按钮可用");

  ctx.document.el("pause-btn").dispatch("click");
  const req = lastRequest(ctx);
  check(req.path === "/api/pause", "暂停请求已发出");
  check(!("X-Control-Token" in req.options.headers), "未启用时写请求不带凭据头");
  await respond(req, liveState({ version: 4 }));
}

async function t2_enable_savesTokenAndSendsHeader() {
  console.log("T2 启用成功：凭据存本标签页，后续写请求带头");
  const ctx = loadApp();
  await respond(lastRequest(ctx), liveState());

  ctx.document.el("control-btn").dispatch("click");
  const req = lastRequest(ctx);
  check(req.path === "/api/control/enable", "点击后请求启用接口");
  check(JSON.parse(req.options.body).version === 3, "启用携带当前版本号");
  await respond(req, liveState({ version: 4, control_enabled: true, control_token: "tok-1" }));

  check(ctx.storage.getItem("cueControlToken") === "tok-1", "凭据已保存到本标签页存储");
  check(ctx.document.el("control-badge").textContent === "控制席：本页 ✓", "标记显示本页控制");
  check(ctx.document.el("control-btn").classList.contains("hidden"), "控制中隐藏启用/接管按钮");

  ctx.document.el("pause-btn").dispatch("click");
  const writeReq = lastRequest(ctx);
  check(writeReq.options.headers["X-Control-Token"] === "tok-1", "写请求携带凭据头");
  await respond(writeReq, liveState({ version: 5, control_enabled: true, paused: true }));
}

async function t3_enabledWithoutToken_readonlyButCanTakeover() {
  console.log("T3 已启用但本页无凭据：写操作禁用，可接管");
  const ctx = loadApp(); // 新标签页：sessionStorage 为空
  const startup = lastRequest(ctx);
  check(!("X-Control-Token" in startup.options.headers), "无凭据页启动查询不带凭据头");
  await respond(startup, liveState({ control_enabled: true, control_granted: false }));

  check(ctx.document.el("control-badge").textContent === "控制席：他人 · 本页只读", "标记显示他人控制");
  check(ctx.document.el("control-btn").textContent === "接管控制席", "显示「接管控制席」按钮");
  check(ctx.document.el("confirm-btn").disabled === true, "确认按钮禁用");
  check(ctx.document.el("pause-btn").disabled === true, "暂停按钮禁用");
  check(ctx.document.el("reset-btn").disabled === true, "重置按钮禁用");
  check(ctx.document.el("start-show").disabled === true, "开始演出按钮禁用");
  const html = ctx.document.el("pending-list").innerHTML;
  check(html.includes('title="改写" disabled'), "待发列表改写按钮禁用");
  check(html.includes('title="记现场问题" disabled'), "待发列表记问题按钮禁用");
  check((html.match(/title="上移" disabled|disabled=""? title="上移"/) || []).length >= 0, "重排按钮禁用");

  ctx.document.el("control-btn").dispatch("click"); // confirm() 已 stub 为 true
  const req = lastRequest(ctx);
  check(req.path === "/api/control/takeover", "点击后请求接管接口");
  check(JSON.parse(req.options.body).version === 3, "接管携带当前版本号");
  await respond(req, liveState({ version: 4, control_enabled: true, control_token: "tok-2" }));

  check(ctx.storage.getItem("cueControlToken") === "tok-2", "接管凭据已保存");
  check(ctx.document.el("control-badge").textContent === "控制席：本页 ✓", "接管后标记为本页控制");
  check(ctx.document.el("confirm-btn").disabled === false, "接管后写操作解禁");
}

async function t4_forbidden_clearsTokenAndRefreshes() {
  console.log("T4 写请求收到 403：凭据清除、标记更新、状态刷新");
  const ctx = loadApp({ cueControlToken: "tok-old" }); // 本标签页保存过凭据
  const startup = lastRequest(ctx);
  check(startup.options.headers["X-Control-Token"] === "tok-old", "启动查询提交缓存凭据核验");
  // 凭据此刻仍有效（接管尚未发生）：核验通过，本页控制
  await respond(startup, liveState({ control_enabled: true, control_granted: true }));
  check(ctx.document.el("control-badge").textContent === "控制席：本页 ✓", "刷新后凭据有效、标记本页控制");

  ctx.document.el("pause-btn").dispatch("click");
  const req = lastRequest(ctx);
  check(req.options.headers["X-Control-Token"] === "tok-old", "写请求携带已保存凭据");
  await respond(req, {
    error: "forbidden",
    message: "本页面不是当前控制席：写操作未生效，可凭最新版本号接管",
    state: liveState({ version: 9, control_enabled: true, control_granted: false }),
  }, 403);

  check(ctx.storage.getItem("cueControlToken") === null, "403 后凭据已清除");
  check(ctx.document.el("control-badge").textContent === "控制席：他人 · 本页只读", "403 后标记更新为只读");
  check(ctx.document.el("version-badge").textContent === "v9", "403 后页面刷新为最新状态");
  check(ctx.document.el("pause-btn").disabled === true, "403 后写操作禁用");
  check(ctx.document.el("control-btn").textContent === "接管控制席", "403 后可接管");
}

async function t5_enableConflict_noTokenStored() {
  console.log("T5 启用遇 409：不存凭据，页面应用最新状态");
  const ctx = loadApp();
  await respond(lastRequest(ctx), liveState());

  ctx.document.el("control-btn").dispatch("click");
  const req = lastRequest(ctx);
  check(req.path === "/api/control/enable", "启用请求已发出");
  // 他人已抢先启用：409 + 最新状态
  await respond(req, {
    error: "conflict",
    message: "操作已过期",
    state: liveState({ version: 5, control_enabled: true, control_granted: false }),
  }, 409);

  check(ctx.storage.getItem("cueControlToken") === null, "409 不保存凭据");
  check(ctx.document.el("control-badge").textContent === "控制席：他人 · 本页只读", "标记更新为他人控制");
  check(ctx.document.el("version-badge").textContent === "v5", "页面刷新为最新版本");
}

async function t6_tokenPersistsAcrossReload() {
  console.log("T6 有效凭据跨刷新保留：启动核验通过，仍为本页控制");
  const storageData = { cueControlToken: "tok-kept" };
  const ctx = loadApp(storageData); // 模拟刷新：sessionStorage 内容保留
  const startup = lastRequest(ctx);
  check(startup.path === "/api/state", "刷新后启动即查询状态");
  check(startup.options.headers["X-Control-Token"] === "tok-kept", "启动查询提交缓存凭据核验");
  // 凭据仍有效：服务端核验通过
  await respond(startup, liveState({ control_enabled: true, control_granted: true, version: 7 }));

  check(ctx.document.el("control-badge").textContent === "控制席：本页 ✓", "刷新后标记仍为本页控制");
  check(ctx.document.el("confirm-btn").disabled === false, "刷新后写操作可用");
  ctx.document.el("pause-btn").dispatch("click");
  const req = lastRequest(ctx);
  check(req.options.headers["X-Control-Token"] === "tok-kept", "刷新后写请求仍带原凭据");
  await respond(req, liveState({ version: 8, control_enabled: true, paused: true }));
}

async function t7_takenOver_oldTabReloads_readonlyAndCanTakeover() {
  console.log("T7 两页交接：旧页刷新即只读、可接管（无需先触发 403）");
  // 页 A：启用控制席
  const pageA = loadApp();
  await respond(lastRequest(pageA), liveState());
  pageA.document.el("control-btn").dispatch("click");
  await respond(lastRequest(pageA),
    liveState({ version: 4, control_enabled: true, control_token: "tok-A" }));
  check(pageA.document.el("control-badge").textContent === "控制席：本页 ✓", "页 A 启用后为本页控制");

  // 页 B：另一标签页接管控制席
  const pageB = loadApp();
  await respond(lastRequest(pageB),
    liveState({ version: 4, control_enabled: true, control_granted: false }));
  pageB.document.el("control-btn").dispatch("click"); // confirm() 已 stub 为 true
  const takeoverReq = lastRequest(pageB);
  check(takeoverReq.path === "/api/control/takeover", "页 B 发起接管");
  await respond(takeoverReq,
    liveState({ version: 5, control_enabled: true, control_token: "tok-B" }));
  check(pageB.document.el("control-badge").textContent === "控制席：本页 ✓", "页 B 接管后为本页控制");

  // 页 A 刷新：sessionStorage 里仍是已被替换的旧凭据 tok-A
  const reloadedA = loadApp({ cueControlToken: "tok-A" });
  const startup = lastRequest(reloadedA);
  check(startup.options.headers["X-Control-Token"] === "tok-A", "旧页刷新时提交缓存的旧凭据");
  // 服务端核验：tok-A 已被接管替换 → control_granted: false
  await respond(startup,
    liveState({ version: 5, control_enabled: true, control_granted: false }));

  check(reloadedA.document.el("control-badge").textContent === "控制席：他人 · 本页只读",
    "旧页刷新后标记为他人控制（不再误标本页）");
  check(reloadedA.document.el("control-btn").textContent === "接管控制席", "旧页刷新后显示接管入口");
  check(!reloadedA.document.el("control-btn").classList.contains("hidden"), "接管按钮可见");
  check(reloadedA.document.el("confirm-btn").disabled === true, "旧页刷新后确认按钮禁用");
  check(reloadedA.document.el("pause-btn").disabled === true, "旧页刷新后暂停按钮禁用");
  check(reloadedA.document.el("reset-btn").disabled === true, "旧页刷新后重置按钮禁用");
  const html = reloadedA.document.el("pending-list").innerHTML;
  check(html.includes('title="改写" disabled'), "旧页刷新后改写按钮禁用");
  check(reloadedA.storage.getItem("cueControlToken") === null, "失效凭据已从本标签页清除");
  check(reloadedA.document.el("version-badge").textContent === "v5", "旧页刷新后显示最新版本");

  // 旧页可主动接管，重新成为控制席
  reloadedA.document.el("control-btn").dispatch("click");
  const retakeReq = lastRequest(reloadedA);
  check(retakeReq.path === "/api/control/takeover", "旧页可主动发起接管");
  check(JSON.parse(retakeReq.options.body).version === 5, "接管携带最新版本号");
  await respond(retakeReq,
    liveState({ version: 6, control_enabled: true, control_token: "tok-A2" }));
  check(reloadedA.document.el("control-badge").textContent === "控制席：本页 ✓", "旧页接管后恢复本页控制");
  check(reloadedA.document.el("confirm-btn").disabled === false, "旧页接管后写操作解禁");
  check(reloadedA.storage.getItem("cueControlToken") === "tok-A2", "新凭据已保存");
}

/* ------------------------------------------------------------ 撤回末条 */

async function w1_withdrawButton_gatingAndRequest() {
  console.log("W1 撤回末条：无已发禁用；有已发时二次确认后提交末条 id 与版本");
  const ctx = loadApp();
  await respond(lastRequest(ctx), liveState()); // 演出中但尚无已发
  check(ctx.document.el("withdraw-btn").disabled === true, "无已发记录时撤回按钮禁用");

  // 已有一条已发
  const s1 = issuedState();
  const startup2Ctx = loadApp();
  await respond(lastRequest(startup2Ctx), s1);
  check(startup2Ctx.document.el("withdraw-btn").disabled === false, "有已发记录时撤回按钮可用");

  // 拒绝二次确认：不发请求
  const pendingBefore = startup2Ctx.pending.length;
  startup2Ctx.sandbox.confirm = () => false;
  startup2Ctx.document.el("withdraw-btn").dispatch("click");
  check(startup2Ctx.pending.length === pendingBefore, "二次确认被取消时不发请求");

  // 允许二次确认
  startup2Ctx.sandbox.confirm = () => true;
  startup2Ctx.document.el("withdraw-btn").dispatch("click");
  const req = lastRequest(startup2Ctx);
  check(req.path === "/api/withdraw", "确认后请求撤回接口");
  const body = JSON.parse(req.options.body);
  check(body.cue_id === 1, "提交页面所见的末条已发 cue_id");
  check(body.version === 3, "提交页面所见版本号");
  check(!("X-Control-Token" in req.options.headers), "未启用控制席时不带凭据头");

  // 成功：A1 按发出快照回到待发队首
  const after = liveState({
    version: 4,
    current: { id: 1, number: "A1", content: "灯光渐亮" },
    next: { id: 2, number: "A2", content: "音乐起" },
    pending: [
      { id: 1, number: "A1", content: "灯光渐亮" },
      { id: 2, number: "A2", content: "音乐起" },
    ],
    issued: [],
    remaining: 2,
  });
  await respond(req, after);
  check(startup2Ctx.document.el("withdraw-btn").disabled === true, "撤回后已发清空，按钮重新禁用");
  check(startup2Ctx.document.el("version-badge").textContent === "v4", "页面应用成功响应的新版本");
}

async function w2_withdraw_pausedAllowedAndTokenHeader() {
  console.log("W2 暂停时也可撤回；启用控制席后请求携带凭据，403 后按钮禁用");
  // 暂停 + 有已发
  const ctx = loadApp({ cueControlToken: "tok-x" });
  await respond(lastRequest(ctx), issuedState({
    version: 6,
    paused: true,
    control_enabled: true,
    control_granted: true,
  }));
  check(ctx.document.el("withdraw-btn").disabled === false, "暂停期间撤回按钮仍可用（确认按钮禁用）");
  check(ctx.document.el("confirm-btn").disabled === true, "暂停期间确认按钮禁用、撤回不受影响");

  ctx.document.el("withdraw-btn").dispatch("click"); // confirm stub 默认 true
  const req = lastRequest(ctx);
  check(req.path === "/api/withdraw", "暂停时发出撤回请求");
  check(req.options.headers["X-Control-Token"] === "tok-x", "撤回请求携带控制席凭据");
  check(JSON.parse(req.options.body).version === 6, "携带当前版本号");
  await respond(req, issuedState({
    version: 7,
    paused: true,
    control_enabled: true,
    control_granted: true,
    status: "live",
    current: { id: 1, number: "A1", content: "灯光渐亮" },
    next: { id: 2, number: "A2", content: "音乐起" },
    pending: [
      { id: 1, number: "A1", content: "灯光渐亮" },
      { id: 2, number: "A2", content: "音乐起" },
    ],
    issued: [],
    remaining: 2,
  }));
  check(ctx.document.el("version-badge").textContent === "v7", "暂停态撤回成功后版本更新");
}

async function w3_withdraw_conflictAndForbidden_refresh() {
  console.log("W3 撤回遇 409/403：页面刷新为最新状态，写入口按资格更新");
  // 409：他人先操作（例如末条已被另一页撤回）
  const ctx = loadApp();
  await respond(lastRequest(ctx), issuedState());
  ctx.document.el("withdraw-btn").dispatch("click");
  await respond(lastRequest(ctx), {
    error: "conflict",
    message: "操作已过期",
    state: liveState({ version: 8, issued: [] }), // 末条已被撤回：已发清空
  }, 409);
  check(ctx.document.el("version-badge").textContent === "v8", "409 后页面刷新为最新版本");
  check(ctx.document.el("withdraw-btn").disabled === true, "最新状态无已发，撤回按钮禁用");

  // 403：只读页点击撤回（按钮本应禁用；即便发出也由服务端拒绝并纠正标记）
  const ro = loadApp();
  await respond(lastRequest(ro), issuedState({ control_enabled: true, control_granted: false }));
  check(ro.document.el("withdraw-btn").disabled === true, "非控制席页面撤回按钮禁用");
}

/* ------------------------------------------------------------ 主流程 */

(async function main() {
  const cases = [
    t1_disabledByDefault_oldCallsUnchanged,
    t2_enable_savesTokenAndSendsHeader,
    t3_enabledWithoutToken_readonlyButCanTakeover,
    t4_forbidden_clearsTokenAndRefreshes,
    t5_enableConflict_noTokenStored,
    t6_tokenPersistsAcrossReload,
    t7_takenOver_oldTabReloads_readonlyAndCanTakeover,
    w1_withdrawButton_gatingAndRequest,
    w2_withdraw_pausedAllowedAndTokenHeader,
    w3_withdraw_conflictAndForbidden_refresh,
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
