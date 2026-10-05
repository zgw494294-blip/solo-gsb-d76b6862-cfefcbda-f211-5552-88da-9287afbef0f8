"use strict";

/* 观众字幕屏（/screen -> screen.js）前端回归测试（纯 Node，无第三方依赖）。
 *
 * 用迷你 DOM 模拟 screen.html 的两个元素与可控 fetch（响应顺序完全由测试
 * 决定，模拟乱序 / 迟到 / 失败），通过 vm 加载真实的 app/static/screen.js。
 *
 * 覆盖：
 * - 首次响应应用字幕；subtitle 为 null 时清屏（空文本，不显示后台内容）；
 * - 画面序号单调：只接受更大的 seq；相同 seq 不重复写入、更小 seq（迟到的
 *   旧帧，含清屏前最后一屏字幕）一律丢弃，画面不倒退；
 * - 非法响应（缺 seq / seq 非整数 / 非对象）不改画面；
 * - 网络失败显示断线提示但保留最后一帧字幕，恢复后继续更新；
 * - 上一轮询未结束时不重叠发请求；
 * - 接口固定为 GET /api/screen（只读、无请求头、不发送任何凭据）。
 *
 * 运行：node tests/screen.test.js
 */

const fs = require("fs");
const path = require("path");
const vm = require("vm");

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

class FakeEl {
  constructor(id) {
    this.id = id;
    this.tagName = "DIV";
    this.classList = new ClassList();
    this._text = "";
  }
  get textContent() { return this._text; }
  set textContent(v) { this._text = String(v); }
}

function loadScreen() {
  const elements = {
    "screen-text": new FakeEl("screen-text"),
    "screen-conn": new FakeEl("screen-conn"),
  };
  const document = {
    getElementById: (id) => elements[id] || null,
  };
  const pending = [];
  let timerFn = null;
  const sandboxGlobal = {};
  const sandbox = {
    document,
    console,
    fetch: (p, options) =>
      new Promise((resolve, reject) => pending.push({ path: p, options, resolve, reject })),
    setInterval: (fn) => { timerFn = fn; return 1; },
  };
  // vm 中 screen.js 的 globalThis 即沙箱全局对象：预置共享钩子容器
  sandbox.globalThis = sandboxGlobal;
  vm.createContext(sandbox);
  const code = fs.readFileSync(
    path.join(__dirname, "..", "app", "static", "screen.js"),
    "utf8"
  );
  vm.runInContext(code, sandbox, { filename: "screen.js" });
  const tick = () => timerFn && timerFn();
  return { elements, pending, sandbox: sandboxGlobal, tick };
}

const flush = () => new Promise((r) => setImmediate(r));
async function answer(req, data, status = 200) {
  req.resolve({ ok: status >= 200 && status < 300, status, json: async () => data });
  await flush();
}
async function fail(req) {
  req.reject(new Error("network down"));
  await flush();
}

let failures = 0;
function check(cond, msg) {
  if (cond) console.log("  ✓ " + msg);
  else { failures++; console.error("  ✗ " + msg); }
}

async function t1_initialPollAndDisplay() {
  console.log("P1 首次轮询：GET /api/screen 应用字幕；null 清屏")
  const ctx = loadScreen();
  check(ctx.pending.length === 1, "加载即发起首次轮询");
  const req = ctx.pending[0];
  check(req.path === "/api/screen", "轮询固定请求 GET /api/screen");
  check(!req.options || (!req.options.method && !req.options.headers),
        "只读轮询：无 POST 方法、无凭据等请求头（fetch 默认 GET）");
  await answer(req, { subtitle: "第一屏字幕", seq: 1 });
  check(ctx.elements["screen-text"].textContent === "第一屏字幕", "字幕上屏");
  check(!ctx.elements["screen-conn"].classList.contains("offline"), "连接正常不提示");

  // 清屏帧
  ctx.tick();
  await answer(ctx.pending.at(-1), { subtitle: null, seq: 2 });
  check(ctx.elements["screen-text"].textContent === "", "subtitle 为 null：清屏（空文本）");
}

async function t2_monotonicSeq() {
  console.log("P2 防倒退：相同 / 更小 seq 丢弃，只接受更大的序号")
  const ctx = loadScreen();
  await answer(ctx.pending[0], { subtitle: "当前帧", seq: 5 });
  check(ctx.elements["screen-text"].textContent === "当前帧", "应用 seq=5");

  // 迟到的旧帧（清屏前更早的画面）：不能覆盖
  await ctx.sandbox.__screen.applyScreen({ subtitle: "迟到的旧帧", seq: 3 });
  check(ctx.elements["screen-text"].textContent === "当前帧", "更小 seq 的旧字幕被丢弃");
  await ctx.sandbox.__screen.applyScreen({ subtitle: null, seq: 4 });
  check(ctx.elements["screen-text"].textContent === "当前帧", "更小 seq 的旧清屏帧被丢弃");
  // 相同 seq：不回退也不重复改动
  await ctx.sandbox.__screen.applyScreen({ subtitle: "相同序号的乱序响应", seq: 5 });
  check(ctx.elements["screen-text"].textContent === "当前帧", "相同 seq 的乱序响应不覆盖");
  // 更大 seq：接受
  await ctx.sandbox.__screen.applyScreen({ subtitle: null, seq: 6 });
  check(ctx.elements["screen-text"].textContent === "", "更大 seq 的清屏帧生效");
  check(ctx.sandbox.__screen.appliedSeq === 6, "已应用序号更新为 6");
}

async function t3_pollOutOfOrderNetwork() {
  console.log("P3 轮询乱序模拟：先发的旧响应后到，不倒退画面")
  const ctx = loadScreen();
  await answer(ctx.pending[0], { subtitle: "初始", seq: 1 });

  // 两次轮询在途（真实代码防重叠，这里直接验证 applyScreen 的乱序保护；
  // 轮询函数自身的串行保证在 P5 覆盖）
  ctx.tick();
  const older = ctx.pending.at(-1);
  // 新帧先应用
  await answer({ resolve: older.resolve }, { subtitle: "新帧", seq: 8 });
  // 旧请求（若真的存在更旧的响应）后到：直接用 applyScreen 模拟迟到
  await ctx.sandbox.__screen.applyScreen({ subtitle: "新帧", seq: 8 });
  await ctx.sandbox.__screen.applyScreen({ subtitle: "旧帧倒退", seq: 2 });
  check(ctx.elements["screen-text"].textContent === "新帧", "迟到旧帧不倒退画面");
}

async function t4_badPayloads() {
  console.log("P4 非法响应不改画面")
  const ctx = loadScreen();
  await answer(ctx.pending[0], { subtitle: "稳定帧", seq: 3 });
  for (const bad of [
    null, undefined, {}, { subtitle: "无序号" }, { subtitle: "x", seq: "3" },
    { subtitle: "x", seq: 3.5 }, "nope",
  ]) {
    await ctx.sandbox.__screen.applyScreen(bad);
  }
  check(ctx.elements["screen-text"].textContent === "稳定帧",
        "缺序号 / 非整数序号 / 非对象响应都被丢弃");
  check(ctx.sandbox.__screen.appliedSeq === 3, "已应用序号保持 3");
  // 数字字幕被转字符串（防御），序号更大才应用
  await ctx.sandbox.__screen.applyScreen({ subtitle: 42, seq: 4 });
  check(ctx.elements["screen-text"].textContent === "42", "更大序号的非字符串字幕按字符串显示");
}

async function t5_noOverlapAndReconnect() {
  console.log("P5 不重叠轮询；断线保留最后一帧、恢复后继续更新")
  const ctx = loadScreen();
  await answer(ctx.pending[0], { subtitle: "断线前最后一帧", seq: 1 });

  // 上一次轮询未结束时，定时触发不发新请求
  ctx.tick();
  const before = ctx.pending.length;
  ctx.tick();
  ctx.tick();
  check(ctx.pending.length === before, "在途轮询不重叠发请求");

  // 该请求网络失败：显示断线、保留字幕
  await fail(ctx.pending.at(-1));
  check(ctx.elements["screen-conn"].classList.contains("offline"), "断线提示");
  check(ctx.elements["screen-text"].textContent === "断线前最后一帧",
        "断线不清屏：保留最后一帧字幕");

  // 下次轮询成功：断线提示消失、画面更新
  ctx.tick();
  await answer(ctx.pending.at(-1), { subtitle: "恢复后新帧", seq: 2 });
  check(!ctx.elements["screen-conn"].classList.contains("offline"), "恢复后断线提示消失");
  check(ctx.elements["screen-text"].textContent === "恢复后新帧", "恢复后接受新帧");

  // HTTP 500 同样按断线处理
  ctx.tick();
  await answer(ctx.pending.at(-1), { error: "server" }, 500);
  check(ctx.elements["screen-conn"].classList.contains("offline"), "HTTP 500 显示断线");
  check(ctx.elements["screen-text"].textContent === "恢复后新帧", "错误响应不改画面");
}

async function t6_crossShowNoReset() {
  console.log("P6 跨场序号不归零：小序号的新场帧不倒退")
  const ctx = loadScreen();
  await answer(ctx.pending[0], { subtitle: "晚场字幕", seq: 42 });
  // 新场首条尚未发出（清屏）时若序号被错误归零，迟到的 0 不能倒退
  await ctx.sandbox.__screen.applyScreen({ subtitle: null, seq: 0 });
  check(ctx.elements["screen-text"].textContent === "晚场字幕",
        "序号归零的响应被丢弃（跨场不归零由后端保证，前端只接受更大序号）");
  await ctx.sandbox.__screen.applyScreen({ subtitle: null, seq: 43 });
  check(ctx.elements["screen-text"].textContent === "", "跨场后的更大清屏序号生效");
}

(async () => {
  await t1_initialPollAndDisplay();
  await t2_monotonicSeq();
  await t3_pollOutOfOrderNetwork();
  await t4_badPayloads();
  await t5_noOverlapAndReconnect();
  await t6_crossShowNoReset();
  console.log(failures === 0 ? "\n全部通过（6 个用例）" : `\n失败 ${failures} 处断言`);
  process.exit(failures === 0 ? 0 : 1);
})().catch((err) => { console.error(err); process.exit(1); });
