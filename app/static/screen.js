"use strict";

/* 观众字幕屏：只显示执行台另行设置的字幕，不显示后台提示正文。
 *
 * 每秒轮询只读接口 GET /api/screen——响应仅含字幕正文（null 为清屏）与
 * 画面序号 seq（每次上屏 / 清屏 +1，跨场单调递增不归零），不含提示正文、
 * 版本号等任何后台信息。画面只接受**更新**的序号：乱序到达、重试或缓存
 * 造成的迟到旧响应直接丢弃，画面不会倒退；网络失败时保持当前画面。
 */

let currentSeq = -1; // 已上屏的画面序号；初始 -1，接受任何首个响应

/* 应用一帧画面：序号必须更新才生效；text 为 null 表示清屏。 */
function applyScreen(seq, text) {
  if (typeof seq !== "number" || seq <= currentSeq) return; // 迟到 / 重复：不倒退
  currentSeq = seq;
  const el = document.querySelector("#screen-text");
  if (!el) return;
  const blank = text === null || text === undefined || text === "";
  el.textContent = blank ? "" : String(text);
  el.classList.toggle("blank", blank);
}

async function pollScreen() {
  let data;
  try {
    const res = await fetch("/api/screen", { cache: "no-store" });
    if (!res.ok) return; // 非 200：保持当前画面，下次轮询再试
    data = await res.json();
  } catch (err) {
    return; // 网络抖动：保持当前画面，下次轮询再试
  }
  applyScreen(data.seq, data.text);
}

/* 页面打开期间每秒轮询（纯查询）；测试环境无 setInterval 时不启动，
 * 由测试直接调用 pollScreen 驱动。 */
if (typeof setInterval === "function") {
  setInterval(pollScreen, 1000);
}
pollScreen();
