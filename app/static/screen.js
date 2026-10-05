"use strict";

/* 观众字幕屏（/screen）专用脚本：只轮询 GET /api/screen 并显示字幕。
 *
 * 接口只返回 { subtitle, seq }：
 * - subtitle 为当前应显示的字幕文本，null 表示清屏（不显示提示正文等任何
 *   后台信息，本页也没有其他接口入口）；
 * - seq 是跨场递增、从不归零的画面序号，每次画面内容变化都 +1。
 *
 * 防倒退：轮询响应可能乱序 / 迟到（网络抖动、切场、重试），本页只接受
 * seq 严格大于已显示序号的响应；相同序号只同步连接标记，更小的序号一律
 * 丢弃——迟到的旧帧（含清屏前最后一屏字幕）永远不能把画面倒回去。
 */

const screenTextEl = document.getElementById("screen-text");
const screenConnEl = document.getElementById("screen-conn");

let appliedSeq = null; // 已上屏画面的序号；null 表示尚未收到任何响应
let polling = false;

function setConnected(ok) {
  screenConnEl.textContent = ok ? "" : "连接中断，正在重试…";
  screenConnEl.classList.toggle("offline", !ok);
}

function applyScreen(data) {
  if (!data || typeof data !== "object") return;
  const seq = Number(data.seq);
  if (!Number.isInteger(seq)) return;
  // 迟到的旧响应（含清屏前最后一帧）不能倒退画面：只接受更新的序号。
  if (appliedSeq !== null && seq <= appliedSeq) return;
  appliedSeq = seq;
  screenTextEl.textContent = data.subtitle == null ? "" : String(data.subtitle);
}

async function pollOnce() {
  if (polling) return; // 上一次轮询还没结束：不重叠发请求，天然避免乱序
  polling = true;
  try {
    const res = await fetch("/api/screen", { cache: "no-store" });
    if (!res.ok) throw new Error("HTTP " + res.status);
    applyScreen(await res.json());
    setConnected(true);
  } catch (err) {
    setConnected(false); // 不断线清屏：保留最后一帧字幕，下次轮询成功再更新
  } finally {
    polling = false;
  }
}

pollOnce();
if (typeof setInterval === "function") {
  setInterval(pollOnce, 1000);
}

/* 测试环境通过全局钩子驱动 / 检查，不影响浏览器运行。 */
try {
  if (typeof globalThis !== "undefined") {
    globalThis.__screen = {
      pollOnce,
      applyScreen,
      get appliedSeq() { return appliedSeq; },
    };
  }
} catch (err) { /* 某些受限环境下 globalThis 只读，忽略即可 */ }
