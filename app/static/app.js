"use strict";

/* 最近一次从服务端拿到的状态；所有变更请求都携带 state.version。
 * 服务端版本不一致会返回 409 + 最新状态，页面据此刷新。 */
let state = null;
let editingId = null; // 正在编辑的待发提示 id
let editingIssueId = null; // 正在追加现场问题记录的提示 id（已发或待发）
let suggestingId = null; // 正在为其填写改写提议的待发提示 id（无控制权也可提交）
let subtitleId = null; // 正在为其设置观众字幕的待发提示 id（受控制席保护）
/* 在途请求计数：交错发出的请求（如工种回报与待发重排同时）各自结束时只
 * 释放自身占用，计数归零才解禁——避免一个请求的收尾把另一个仍在途请求的
 * 占用提前解开（回报尚未结束就被重排的收尾解禁，从而重复提交相同回报）。 */
let busyCount = 0;
let busy = false;     // 有请求在途（busyCount > 0）时禁用写入口，防止重复点击
let prevStatus = null; // 上一帧状态，用于识别回到开场前的切换

/* ---------------------------------------------------------------- 单控制席
 * 启用后所有写操作须携带本标签页保存的凭据（请求头 X-Control-Token）。
 * 凭据存 sessionStorage：刷新保留、标签页之间各自独立；启用状态与凭据在
 * 服务端数据库中，跨场、刷新页面、重启服务都继续有效。
 *
 * 本页是否具备控制资格以服务端核验为准（control_granted）：本地存有凭据
 * 不代表它仍有效——被接管后旧标签页的 sessionStorage 里仍留着旧凭据。
 * 页面启动时把缓存凭据随 GET /api/state 提交核验，按响应的 control_granted
 * 更新资格标记与全部写入口；写请求被 403 拒绝时同样纠正。 */

const CONTROL_TOKEN_KEY = "cueControlToken";
let controlToken = loadControlToken();
let controlGranted = false; // 服务端核验的本页控制资格（仅启用控制席后有意义）

function loadControlToken() {
  try {
    if (typeof sessionStorage !== "undefined") {
      return sessionStorage.getItem(CONTROL_TOKEN_KEY) || null;
    }
  } catch (err) { /* 存储不可用时按无凭据处理 */ }
  return null;
}

function saveControlToken(token) {
  controlToken = token || null;
  try {
    if (typeof sessionStorage !== "undefined") {
      if (controlToken) sessionStorage.setItem(CONTROL_TOKEN_KEY, controlToken);
      else sessionStorage.removeItem(CONTROL_TOKEN_KEY);
    }
  } catch (err) { /* 存储不可用时仅保留在内存中 */ }
}

/* 本页是否具备控制资格：未启用控制席时任何页面可写；启用后以服务端核验的
 * control_granted 为准——本地缓存的凭据可能已被接管替换，不能自证有效。 */
function canWrite() {
  return !!state && (!state.control_enabled || controlGranted);
}

function apiHeaders() {
  const headers = { "Content-Type": "application/json" };
  if (controlToken) headers["X-Control-Token"] = controlToken;
  return headers;
}

/* 403：凭据缺失或已被接管替换——清除本页控制标记并刷新为最新状态。 */
function handleForbidden(data) {
  controlGranted = false;
  saveControlToken(null);
  // 凭据失效：受控制席保护的内联编辑表单（含字幕设置）退出，避免只读页
  // 停留在可提交的表单里（提交只会再次 403）。
  editingId = null;
  subtitleId = null;
  if (data && data.state) applyState(data.state);
  else render();
  toast((data && data.message) || "本页面不是当前控制席，写操作未生效");
}

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

let toastTimer = null;
function toast(msg) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 3200);
}

/* ---------------------------------------------------------------- 请求 */

/* 请求开始：占用一份在途计数并禁用写入口。 */
function busyEnter() {
  busyCount += 1;
  busy = true;
  updateBusy();
}

/* 请求结束：只释放本次请求自身的占用——仍有其他交错请求在途时保持禁用，
 * 全部结束（计数归零）后才按最新状态恢复；成功、409 刷新、HTTP 错误与
 * 网络失败都走这里，既不会提前解禁也不会永久锁住。 */
function busyLeave() {
  busyCount = Math.max(0, busyCount - 1);
  busy = busyCount > 0;
  updateBusy();
}

async function api(path, body) {
  busyEnter();
  try {
    const res = await fetch(path, {
      method: "POST",
      headers: apiHeaders(),
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (res.status === 403) {
      // 凭据缺失或已被接管：清除本页控制标记，刷新为最新状态
      handleForbidden(data);
      return null;
    }
    if (res.status === 409) {
      // 操作过期：同版本已有其他先生效，用最新状态刷新页面
      applyState(data.state);
      toast("操作未生效：已有人先操作，页面已刷新为最新状态");
      return null;
    }
    if (!res.ok) {
      toast(data.error || "请求失败");
      return null;
    }
    applyState(data);
    return data;
  } catch (err) {
    toast("网络错误：" + err.message);
    return null;
  } finally {
    busyLeave();
  }
}

function applyState(s) {
  state = s;
  /* 服务端随状态回报的"本请求控制资格"是权威结论：据此更新资格标记；
   * 已启用且核验不通过时，本地缓存的凭据必然已失效（被接管替换），一并
   * 清除，避免旧标签页继续自认为是控制席。 */
  if (typeof s.control_granted === "boolean") {
    controlGranted = s.control_granted;
    if (s.control_enabled && !s.control_granted && controlToken) {
      saveControlToken(null);
    }
  }
  if (editingId !== null && !s.pending.some((c) => c.id === editingId)) {
    editingId = null; // 正在编辑的提示已不在待发列表（如已被确认）
  }
  if (suggestingId !== null && !s.pending.some((c) => c.id === suggestingId)) {
    suggestingId = null; // 正在填写改写提议的提示已不在待发列表（如已被确认）
  }
  if (subtitleId !== null && !s.pending.some((c) => c.id === subtitleId)) {
    subtitleId = null; // 正在设置字幕的提示已不在待发列表（如已被确认）
  }
  if (editingIssueId !== null &&
      !s.pending.some((c) => c.id === editingIssueId) &&
      !s.issued.some((c) => c.id === editingIssueId)) {
    editingIssueId = null; // 追加问题的提示已不在本场（归档 / 重置 / 开新场）
  }
  render();
}

function updateBusy() {
  const writable = canWrite(); // 非控制席禁用一切写操作（仍可接管）
  const btn = $("#confirm-btn");
  // 暂停期间、或启用核验而两工种未全部就绪时禁用确认按钮（服务端同样拒绝并返回 409）
  if (btn) btn.disabled = busy || !writable || !state || !state.current || state.paused || verifyBlocked();
  const archiveBtn = $("#archive-btn");
  if (archiveBtn) archiveBtn.disabled = busy || !writable || archiveSubmitting || !state || state.status === "setup";
  const pauseBtn = $("#pause-btn");
  if (pauseBtn) pauseBtn.disabled = busy || !writable || !state || state.status !== "live";
  // 撤回末条：演出中（含暂停）或已完成、且存在已发记录时可用；开场前禁用
  const withdrawBtn = $("#withdraw-btn");
  if (withdrawBtn) withdrawBtn.disabled = busy || !writable || !state || !state.issued.length;
  const resetBtn = $("#reset-btn");
  if (resetBtn) resetBtn.disabled = busy || !writable;
  const startBtn = $("#start-show");
  if (startBtn) startBtn.disabled = busy || !writable;
  const controlBtn = $("#control-btn");
  if (controlBtn) controlBtn.disabled = busy;
  // 开场前的核验启用按钮：受控制席保护；演出页的回报按钮无需凭据，
  // 不随控制资格禁用，由 updateVerifyButtons 按演出状态与在途请求管理
  const verifyEnableBtn = $("#verify-enable-btn");
  if (verifyEnableBtn) verifyEnableBtn.disabled = busy || !writable;
  // 演出页的回报按钮：请求在途禁用（防重复回报），结束后按最新状态恢复；
  // 不随控制资格禁用（回报无需凭据）。开场 /api/setup 成功后 renderVerify
  // 在 busy 仍为 true 时渲染，全靠这里把按钮恢复为可点。
  updateVerifyButtons();
  // 自动发出的开关 / 设置按钮与间隔输入：随在途请求与控制资格同步
  updateAutoButtons();
  // 改写提议的提交 / 接受 / 拒绝按钮：随在途请求同步（防重复处理）；
  // 接受 / 拒绝另需本页有控制资格，提交提议在只读页也允许
  updateSuggestionButtons();
  // 观众字幕设置按钮：随在途请求与控制资格同步（只读页不可设置）
  updateSubtitleButtons();
}

/* 页头控制席标记：未启用 / 本页控制 / 他人控制（本页只读，可接管）。 */
function renderControl() {
  const badge = $("#control-badge");
  const btn = $("#control-btn");
  if (!badge || !btn) return;
  if (!state) {
    badge.textContent = "控制席：…";
    badge.className = "control-badge off";
    btn.classList.add("hidden");
    return;
  }
  if (!state.control_enabled) {
    badge.textContent = "控制席：未启用";
    badge.className = "control-badge off";
    btn.textContent = "启用控制席";
    btn.classList.remove("hidden");
  } else if (controlGranted) {
    badge.textContent = "控制席：本页 ✓";
    badge.className = "control-badge own";
    btn.classList.add("hidden");
  } else {
    badge.textContent = "控制席：他人 · 本页只读";
    badge.className = "control-badge other";
    btn.textContent = "接管控制席";
    btn.classList.remove("hidden");
  }
}

/* 启用或接管控制席：凭当前版本号，成功返回的随机凭据只保存在本标签页。 */
async function claimControl(enabling) {
  busyEnter();
  try {
    const res = await fetch(enabling ? "/api/control/enable" : "/api/control/takeover", {
      method: "POST",
      headers: apiHeaders(),
      body: JSON.stringify({ version: state.version }),
    });
    const data = await res.json();
    if (res.status === 409) {
      // 同版本已有其他操作先生效（含他人抢先启用/接管），刷新为最新状态
      applyState(data.state);
      toast("操作未生效：已有人先操作，页面已刷新为最新状态");
      return;
    }
    if (res.status === 403) { handleForbidden(data); return; }
    if (!res.ok) {
      toast(data.error || "请求失败");
      return;
    }
    saveControlToken(data.control_token); // 凭据仅保存在本标签页
    controlGranted = true;                // 本页即新控制席（响应同样回报）
    delete data.control_token;            // 不留在页面状态里
    applyState(data);
    toast(enabling
      ? "已启用控制席：本页面获得控制资格，其他页面变为只读"
      : "已接管控制席：原控制席凭据已失效");
  } catch (err) {
    toast("网络错误：" + err.message);
  } finally {
    busyLeave();
  }
}

/* ---------------------------------------------------------------- 档案库 */

let archiveSubmitting = false; // 归档请求在途，禁用提交防止重复建档案
let currentDetail = null;      // 当前查看的档案详情（"以此开新场"使用）
let startSubmitting = false;   // 以此开新场请求在途，禁用提交防重复
/* 档案弹窗视图序号：进入列表/详情/归档表单/对照任一视图、或关闭弹窗时 +1。
 * 视图内发出的请求都记下发起时的序号；响应回来时序号不符，说明弹窗已关闭
 * 或已切到别的视图——无论成功、空列表还是失败都直接丢弃，不改写当前视图。 */
let archivesViewSeq = 0;

function statusLabel(status) {
  return status === "done" ? "已完成" : "提前收场";
}

/* 档案详情中的现场问题记录区块：按保存顺序，已发/待发状态与内容均为归档快照。 */
function archiveIssuesHtml(issues) {
  return `
    <h3>现场问题记录（${issues.length}）<span class="hint-inline">按保存顺序 · 随档案快照保存</span></h3>
    <ul class="issue-list">
      ${issues.length ? issues.map((it) => {
        const tag = it.cue_status === "issued"
          ? `<span class="tag issue-issued">已发</span>`
          : `<span class="tag issue-pending">待发</span>`;
        return `<li class="issue-item">
          <div class="issue-top">
            <span class="badge">${escapeHtml(it.cue_number)}</span>
            ${tag}
            <span class="time">${fmtTime(it.created_at)}</span>
          </div>
          <div class="issue-text">${escapeHtml(it.text)}</div>
          <div class="issue-cue-content" title="记录时该提示的内容快照">↳ ${escapeHtml(it.cue_content)}</div>
        </li>`;
      }).join("") : `<li class="empty">无（本场未记录现场问题）</li>`}
    </ul>`;
}

function fmtTime(iso) {
  return iso ? new Date(iso).toLocaleString() : "";
}

function openArchivesModal() {
  $("#archives-modal").classList.remove("hidden");
  loadArchiveList();
}

function closeArchivesModal() {
  archivesViewSeq++; // 关闭弹窗：列表/详情/对照的在途请求一并失效
  $("#archives-modal").classList.add("hidden");
}

async function apiGetJson(path) {
  const res = await fetch(path);
  if (!res.ok) throw new Error("HTTP " + res.status);
  return res.json();
}

async function loadArchiveList() {
  const viewSeq = ++archivesViewSeq; // 进入列表视图：旧视图（含对照）在途请求失效
  currentDetail = null;
  $("#archives-title").textContent = "📚 档案库";
  $("#archives-body").innerHTML = `<p class="empty">加载中…</p>`;
  let data;
  try {
    data = await apiGetJson("/api/archives");
  } catch (err) {
    if (viewSeq !== archivesViewSeq) return; // 已离开列表或弹窗已关闭，过期失败不覆盖当前视图
    $("#archives-body").innerHTML = `<p class="empty">档案加载失败：${escapeHtml(err.message)}</p>`;
    return;
  }
  if (viewSeq !== archivesViewSeq) return; // 已离开列表或弹窗已关闭，丢弃过期响应
  const items = data.archives || [];
  const canArchive = state && state.status !== "setup" && canWrite();
  if (!items.length) {
    $("#archives-body").innerHTML = `
      <p class="empty">还没有归档档案。演出结束或提前收场后，可点击「归档本场」保存回看。</p>
      ${canArchive ? `<div class="actions"><button class="primary" data-action="archive-form">归档当前场次 🗂</button></div>` : ""}`;
    return;
  }
  $("#archives-body").innerHTML = `
    <div class="actions">
      ${canArchive ? `<button class="primary" data-action="archive-form">归档当前场次 🗂</button>` : ""}
      <button data-action="compare-view">场次对照 ⇄</button>
    </div>
    <ul class="archive-list">
      ${items.map((a) => `
        <li class="archive-item" data-action="archive-detail" data-id="${a.id}" tabindex="0">
          <span class="archive-name">${escapeHtml(a.name)}</span>
          <span class="archive-meta">
            <span class="tag ${a.status === "done" ? "done" : "early"}">${statusLabel(a.status)}</span>
            <span class="time">${fmtTime(a.archived_at)}</span>
          </span>
          <span class="archive-count">已发 ${a.issued_count} · 剩余 ${a.pending_count} · 问题 ${a.issue_count || 0}</span>
        </li>`).join("")}
    </ul>`;
}

async function loadArchiveDetail(id) {
  const viewSeq = ++archivesViewSeq; // 进入详情视图：旧视图在途请求失效
  $("#archives-title").textContent = "档案详情";
  $("#archives-body").innerHTML = `<p class="empty">加载中…</p>`;
  let a;
  try {
    a = await apiGetJson("/api/archives/" + encodeURIComponent(id));
  } catch (err) {
    if (viewSeq !== archivesViewSeq) return; // 已离开详情或弹窗已关闭，过期失败不覆盖当前视图
    $("#archives-body").innerHTML = `
      <p class="empty">档案加载失败：${escapeHtml(err.message)}</p>
      <div class="actions"><button data-action="archive-list">← 返回列表</button></div>`;
    return;
  }
  if (viewSeq !== archivesViewSeq) return; // 已离开详情或弹窗已关闭，丢弃过期响应
  currentDetail = a;
  const total = a.issued.length + a.pending.length;
  const canStart = state && state.status === "setup" && canWrite();
  $("#archives-body").innerHTML = `
    <div class="archive-detail-head">
      <div class="archive-detail-name">${escapeHtml(a.name)}</div>
      <div class="archive-detail-meta">
        <span class="tag ${a.status === "done" ? "done" : "early"}">${statusLabel(a.status)}</span>
        归档时间：${fmtTime(a.archived_at)}
      </div>
    </div>
    <div class="archive-columns">
      <div>
        <h3>已发记录（${a.issued.length}）<span class="hint-inline">按发出顺序</span></h3>
        <ul class="cue-list">
          ${a.issued.length ? a.issued.map((c) => `
            <li class="issued-item">
              <span class="badge done">✓ ${escapeHtml(c.number)}</span>
              <span class="content">${escapeHtml(c.content)}</span>
              <span class="time">${fmtTime(c.issued_at)}</span>
            </li>`).join("")
            : `<li class="empty">无已发记录（开场后未发出即收场）</li>`}
        </ul>
      </div>
      <div>
        <h3>剩余未发（${a.pending.length}）<span class="hint-inline">按待发顺序</span></h3>
        <ul class="cue-list">
          ${a.pending.length ? a.pending.map((c) => `
            <li class="pending-item">
              <span class="badge">${escapeHtml(c.number)}</span>
              <span class="content">${escapeHtml(c.content)}</span>
            </li>`).join("")
            : `<li class="empty">无（全部提示均已发出）</li>`}
        </ul>
      </div>
    </div>
    ${archiveIssuesHtml(a.issues || [])}
    <div class="actions">
      <button data-action="archive-list">← 返回列表</button>
      <button id="start-from-archive" class="primary" data-action="start-from-archive"
        ${canStart ? "" : "disabled"}>以此开新场 ▶</button>
    </div>
    <p class="hint">
      ${canStart
        ? `将按<b>已发顺序</b>取 ${a.issued.length} 条编号与内容快照，再接<b>待发顺序</b>的 ${a.pending.length} 条，共 ${total} 条全部作为新场待发提示；不继承发出时间或进度，原档案保持不变。`
        : !state || state.status !== "setup"
          ? "仅开场前（设置页）可以此开新场；当前场次进行中，请先收场或重新开始。"
          : "本页面不是当前控制席：接管后方可以此开新场。"}
    </p>`;
}

function openArchiveForm() {
  if (!state || state.status === "setup") {
    toast("开场前没有可归档的场次");
    return;
  }
  if (!canWrite()) {
    toast("本页面不是当前控制席：可接管后再归档");
    return;
  }
  archivesViewSeq++; // 进入归档表单视图：列表/对照的在途请求失效
  $("#archives-title").textContent = "归档本场";
  $("#archives-body").innerHTML = `
    <p class="hint">归档后本场将保存为独立档案，并清空当前提示、回到开场前设置；之后录入、重置和重启都不会影响档案。</p>
    <label class="field-label" for="archive-name">场次名称</label>
    <input id="archive-name" maxlength="60" placeholder="例如：10 月 1 日晚场" autocomplete="off">
    <p class="hint-inline">当前页面版本 v${state.version} · ${statusLabel(state.status === "done" ? "done" : "live")}</p>
    <div class="actions">
      <button id="archive-submit" class="primary" data-action="archive-submit">保存档案并收场 🗂</button>
      <button data-action="archive-cancel">取消</button>
    </div>`;
  $("#archives-modal").classList.remove("hidden");
  $("#archive-name").focus();
}

async function submitArchive() {
  if (archiveSubmitting) return;
  const input = $("#archive-name");
  const name = input.value.trim();
  if (!name) { toast("请输入场次名称"); input.focus(); return; }
  archiveSubmitting = true;
  const btn = $("#archive-submit");
  if (btn) btn.disabled = true;
  updateBusy();
  try {
    const res = await fetch("/api/archive", {
      method: "POST",
      headers: apiHeaders(),
      body: JSON.stringify({ version: state.version, name }),
    });
    const data = await res.json();
    if (res.status === 403) {
      handleForbidden(data);
      closeArchivesModal();
      return;
    }
    if (res.status === 409) {
      applyState(data.state);
      closeArchivesModal();
      toast("归档未生效：操作已过期，页面已刷新为最新状态");
      return;
    }
    if (!res.ok) {
      toast(data.error || "归档失败");
      if (btn) btn.disabled = false;
      return;
    }
    // 档案已落库且当前提示已一起清空（同一事务）
    applyState(data);
    toast(`已归档「${name}」，当前提示已清空`);
    loadArchiveList();
  } catch (err) {
    toast("网络错误：" + err.message);
    if (btn) btn.disabled = false;
  } finally {
    archiveSubmitting = false;
    updateBusy();
  }
}

async function submitStartFromArchive() {
  if (startSubmitting || !currentDetail) return;
  if (!state || state.status !== "setup") {
    toast("仅开场前可以此开新场");
    return;
  }
  const a = currentDetail;
  const total = a.issued.length + a.pending.length;
  const ok = confirm(
    `将以档案「${a.name}」开新场：\n` +
    `先按已发顺序取 ${a.issued.length} 条（编号与发出时内容快照），` +
    `再接待发顺序的 ${a.pending.length} 条，共 ${total} 条，全部作为待发提示。\n\n` +
    "不继承发出时间或进度，原档案不变。确认开始新场吗？"
  );
  if (!ok) return;
  startSubmitting = true;
  const btn = $("#start-from-archive");
  if (btn) btn.disabled = true;
  try {
    const res = await fetch("/api/archives/" + encodeURIComponent(a.id) + "/start", {
      method: "POST",
      headers: apiHeaders(),
      body: JSON.stringify({ version: state.version }),
    });
    const data = await res.json();
    if (res.status === 403) {
      handleForbidden(data);
      closeArchivesModal();
      return;
    }
    if (res.status === 409) {
      applyState(data.state);
      closeArchivesModal();
      toast("开新场未生效：非开场前或版本已过期，页面已刷新");
      return;
    }
    if (res.status === 404) {
      toast("档案不存在，可能已被删除");
      closeArchivesModal();
      return;
    }
    if (!res.ok) {
      toast(data.error || "开新场失败");
      if (btn) btn.disabled = false;
      return;
    }
    // 新场提示已写入且状态一并推进（同一事务）
    applyState(data);
    closeArchivesModal();
    toast(`已按档案「${a.name}」开始新场，共 ${total} 条待发提示`);
  } catch (err) {
    toast("网络错误：" + err.message);
    if (btn) btn.disabled = false;
  } finally {
    startSubmitting = false;
  }
}

/* ---------------------------------------------------------------- 场次对照 */

let compareSeq = 0; // 对照选择序号：切换场次即作废旧响应；离开视图/关闭弹窗由 archivesViewSeq 兜底

const COMPARE_STATUS = {
  issued: { label: "已发", cls: "issued" },
  pending: { label: "未发", cls: "pending" },
  missing: { label: "缺失", cls: "missing" },
};

function fmtSeconds(s, signed) {
  if (s === null || s === undefined) return "—";
  const v = Number(s).toFixed(3);
  return signed && s > 0 ? `+${v} s` : `${v} s`;
}

async function openCompareView() {
  const viewSeq = ++archivesViewSeq; // 进入对照视图：旧视图（含列表）在途请求失效
  $("#archives-title").textContent = "场次对照";
  $("#archives-body").innerHTML = `<p class="empty">加载中…</p>`;
  let data;
  try {
    data = await apiGetJson("/api/archives");
  } catch (err) {
    if (viewSeq !== archivesViewSeq) return; // 已离开对照或弹窗已关闭，过期失败不覆盖当前视图
    $("#archives-body").innerHTML = `
      <p class="empty">档案加载失败：${escapeHtml(err.message)}</p>
      <div class="actions"><button data-action="archive-list">← 返回列表</button></div>`;
    return;
  }
  // 已离开对照或弹窗已关闭：过期响应不得重建选择器、恢复默认组合或触发默认对照查询
  if (viewSeq !== archivesViewSeq) return;
  const items = data.archives || [];
  if (!items.length) {
    $("#archives-body").innerHTML = `
      <p class="empty">还没有归档档案，无法对照。演出结束或提前收场后，可先「归档本场」保存档案。</p>
      <div class="actions"><button data-action="archive-list">← 返回列表</button></div>`;
    return;
  }
  const optionsHtml = items.map((a) =>
    `<option value="${a.id}">#${a.id} ${escapeHtml(a.name)}（${statusLabel(a.status)} · ${fmtTime(a.archived_at)}）</option>`
  ).join("");
  $("#archives-body").innerHTML = `
    <p class="hint">按编号精确匹配两场提示，各场以<b>首条已发</b>提示的发出时间为零点比较节奏（暂停时长计入）。仅两侧均已发出且内容完全一致的编号才计算「对照 − 基准」秒数差，<b>正值表示对照场更晚</b>。</p>
    <div class="compare-selects">
      <div class="compare-field">
        <label for="compare-base">基准场次</label>
        <select id="compare-base">${optionsHtml}</select>
      </div>
      <div class="compare-field">
        <label for="compare-compare">对照场次</label>
        <select id="compare-compare">${optionsHtml}</select>
      </div>
    </div>
    <div id="compare-result"></div>
    <div class="actions"><button data-action="archive-list">← 返回列表</button></div>`;
  // 默认：基准选较早一场、对照选最新一场，便于直接看到差异
  $("#compare-compare").value = String(items[0].id);
  $("#compare-base").value = String(items.length > 1 ? items[1].id : items[0].id);
  compareSelectionChanged();
}

function compareSelectionChanged() {
  const baseId = $("#compare-base").value;
  const compareId = $("#compare-compare").value;
  if (!baseId || !compareId) {
    compareSeq++; // 作废旧响应并清空旧结果，避免残留
    $("#compare-result").innerHTML = `<p class="hint">请选择基准场次与对照场次。</p>`;
    return;
  }
  runCompare(Number(baseId), Number(compareId));
}

async function runCompare(baseId, compareId) {
  const seq = ++compareSeq; // 新选择作废旧响应；响应回来时序号不符即丢弃
  const viewSeq = archivesViewSeq; // 离开对照视图或关闭弹窗后，响应同样丢弃
  const result = $("#compare-result");
  result.innerHTML = `<p class="empty">对照中…</p>`;
  let res, data;
  try {
    res = await fetch(
      `/api/compare?base=${encodeURIComponent(baseId)}&compare=${encodeURIComponent(compareId)}`
    );
    data = await res.json();
  } catch (err) {
    if (seq !== compareSeq || viewSeq !== archivesViewSeq) return;
    result.innerHTML = `<p class="empty">对照失败：${escapeHtml(err.message)}</p>`;
    return;
  }
  // 场次已切换、已离开对照视图或弹窗已关闭：不用旧响应覆盖当前结果
  if (seq !== compareSeq || viewSeq !== archivesViewSeq) return;
  if (res.status === 404) {
    result.innerHTML = `<p class="empty">档案不存在，可能已被删除，请返回列表刷新。</p>`;
    return;
  }
  if (!res.ok) {
    result.innerHTML = `<p class="empty">对照失败：${escapeHtml(data.error || "请求失败")}</p>`;
    return;
  }
  renderCompareResult(data);
}

function compareSideHtml(side) {
  const meta = COMPARE_STATUS[side.status] || COMPARE_STATUS.missing;
  const content = side.content === null || side.content === undefined
    ? "—" : escapeHtml(side.content);
  const offset = side.offset_seconds === null || side.offset_seconds === undefined
    ? "" : `<div class="compare-offset">零点 +${fmtSeconds(side.offset_seconds)}</div>`;
  return `<span class="tag ${meta.cls}">${meta.label}</span>
    <div class="content">${content}</div>${offset}`;
}

function renderCompareResult(data) {
  const result = $("#compare-result");
  if (!result) return; // 已离开对照视图，丢弃结果
  const rows = data.rows || [];
  const comparable = rows.filter((r) => r.delta_seconds !== null).length;
  const emptyHint = comparable === 0
    ? `<p class="hint compare-no-match">⚠ 两场没有可对照的提示：需同一编号在两侧均已发出且内容完全一致，才能计算秒数差。</p>`
    : "";
  result.innerHTML = `
    ${emptyHint}
    <div class="compare-head">
      <span>编号</span>
      <span>基准 · ${escapeHtml(data.base.name)}</span>
      <span>对照 · ${escapeHtml(data.compare.name)}</span>
      <span>对照 − 基准</span>
    </div>
    ${rows.map((r) => {
      let deltaHtml;
      if (r.delta_seconds !== null) {
        const cls = r.delta_seconds > 0 ? "later" : r.delta_seconds < 0 ? "earlier" : "same";
        deltaHtml = `<span class="compare-delta ${cls}">${fmtSeconds(r.delta_seconds, true)}</span>`;
      } else {
        deltaHtml = `<span class="compare-delta na">—</span>
          <div class="compare-reason">${escapeHtml(r.delta_reason || "")}</div>`;
      }
      return `<div class="compare-row">
        <span class="badge">${escapeHtml(r.number)}</span>
        <div class="compare-side">${compareSideHtml(r.base)}</div>
        <div class="compare-side">${compareSideHtml(r.compare)}</div>
        <div class="compare-delta-cell">${deltaHtml}</div>
      </div>`;
    }).join("")}
    <p class="hint">零点为各场首条已发提示的发出时间；秒数保留毫秒，暂停时长计入。共 ${rows.length} 个编号，其中 ${comparable} 条可对照。</p>`;
}

/* ---------------------------------------------------------------- 本场核验
 * 启用后灯光（lighting）、音响（sound）须分别对"当前提示 + 当前轮次"报就绪，
 * 两工种都就绪才能确认发出。回报经 POST /api/verify/report 提交：无需控制
 * 凭据（只读页面也可回报）、不改全局版本号——回报永不返回 403，但本页若有
 * 缓存凭据会随请求头 X-Control-Token 一并提交（apiHeaders），服务端在成功与
 * 409 响应的 control_granted 中按本次凭据回报：当前席回报后凭据保留、页面不
 * 变只读，被接管的旧页回报后按真实资格清除失效缓存。确认推进 / 撤回 / 改写
 * 当前提示 / 重排换头都会开启新轮次并清空本轮就绪，旧轮次回报被服务端以
 * 409 拒绝。 */

const VERIFY_DEPTS = [
  ["lighting", "灯光"],
  ["sound", "音响"],
];

/* 服务端状态中的核验视图；旧服务端/异常响应缺该字段时按未启用处理。 */
function verifyState() {
  const v = state && state.verify;
  return v && typeof v === "object"
    ? v
    : { enabled: false, round: 0, cue_id: null, lighting: null, sound: null, all_ready: false };
}

/* 确认是否被核验门槛挡住：启用且两工种未全部就绪。 */
function verifyBlocked() {
  const v = verifyState();
  return !!(v.enabled && !v.all_ready);
}

/* 演出页核验卡片是否允许回报：核验启用、演出中、存在当前提示即可。
 * 回报是工种操作，不看本页控制资格（只读/被接管旧页同样可回报）；暂停中
 * 也允许（仅确认发出被暂停挡下）；已完成（无当前提示）或核验关闭不可回报。
 * 在途请求另由 busy 挡下，防止同一工种重复回报。 */
function canVerifyReport() {
  const v = verifyState();
  return !!state && state.status === "live" && !!state.current && v.enabled && !busy;
}

/* 按最新状态同步已渲染回报按钮的 disabled：任一请求在途（busy，含交错
 * 发出的其他请求，如待发重排）全部禁用防止重复回报；所有请求结束后必须
 * 调用本函数恢复——成功 / 409 刷新 / 请求失败都只依据当前最新状态重算，
 * 避免按钮在一次请求后永久卡死。按钮的显隐与文字仍由 renderVerify 重建，
 * 这里只衔接请求状态，不改回报资格判定本身。 */
function updateVerifyButtons() {
  if (!state || state.status === "setup") return;
  const card = $("#verify-card");
  if (!card || card.classList.contains("hidden")) return;
  const enabled = canVerifyReport();
  $$('button[data-action="verify-report"]', card).forEach((btn) => {
    btn.disabled = !enabled;
  });
}

/* 演出页的核验卡片：当前轮次 + 两工种就绪状态 + 回报按钮。
 * 回报按钮不随控制席资格禁用（回报无需凭据，缺失 / 失效凭据也不返回 403），
 * 仅随演出状态与在途请求禁用；本页缓存的凭据仍随请求提交，服务端据此在响应
 * 的 control_granted 中如实回报本页资格。 */
function renderVerify() {
  const card = $("#verify-card");
  if (!card) return;
  const v = verifyState();
  card.classList.toggle("hidden", !v.enabled);
  if (!v.enabled) return;
  const canReport = canVerifyReport();
  const deptHtml = VERIFY_DEPTS.map(([key, label]) => {
    const ready = v[key];
    const status = ready === null || ready === undefined
      ? `<span class="verify-status unknown">未回报</span>`
      : ready
        ? `<span class="verify-status ready">就绪 ✓</span>`
        : `<span class="verify-status not">未就绪</span>`;
    return `<div class="verify-dept">
      <span class="verify-name">${label}</span>${status}
      <button class="small" data-action="verify-report" data-dept="${key}"
        ${canReport ? "" : "disabled"}>${ready === true ? "取消就绪" : "报就绪"}</button>
    </div>`;
  }).join("");
  card.innerHTML = `
    <div class="card-label">本场核验 · 第 ${v.round} 轮${state.paused ? " · 暂停中（可继续回报）" : ""}</div>
    <div class="verify-row">${deptHtml}</div>
    <div class="${v.all_ready ? "verify-ok" : "verify-wait"}">${v.all_ready
      ? "两工种均已就绪，可确认发出"
      : "待灯光、音响都报就绪后才能确认发出"}</div>`;
  // innerHTML 已整体重建：立即按最新状态与在途标记同步一次真实 disabled
  // （渲染发生在请求在途时，属性为禁用；结束后由 updateBusy 再同步恢复）。
  updateVerifyButtons();
}

/* 设置页的核验入口：未启用时显示启用按钮（受控制席保护），已启用显示标记。 */
function renderVerifySetup() {
  const badge = $("#verify-setup-badge");
  const btn = $("#verify-enable-btn");
  if (!badge || !btn) return;
  const v = verifyState();
  badge.textContent = v.enabled ? "本场核验：已启用 ✓" : "本场核验：未启用";
  badge.className = "verify-badge " + (v.enabled ? "on" : "off");
  btn.classList.toggle("hidden", v.enabled);
  btn.disabled = busy || !canWrite();
}

/* 工种回报就绪/未就绪：提交工种、当前提示 cue_id、当前轮次与就绪布尔值。
 * 走统一 api()，缓存的控制凭据会自动随请求头提交；409（轮次/提示已过期等）
 * 由 api() 统一刷新为最新状态并提示，其中 control_granted 按本次凭据回报，
 * 当前席凭据保留、被接管旧页清除失效缓存。 */
function reportVerify(dept) {
  if (!canVerifyReport()) return; // 未启用/非演出中/无当前提示，或请求在途防重复
  const v = verifyState();
  const ready = v[dept] === true ? false : true; // 已就绪再点为取消就绪
  api("/api/verify/report", {
    department: dept,
    cue_id: state.current.id,
    round: v.round,
    ready,
  });
}

/* ---------------------------------------------------------------- 自动发出
 * 服务端后台按统一间隔自动发出当前提示：开启/改间隔后从完整间隔计时，暂停冻结
 * 剩余时间、恢复续计，到期未就绪（核验门槛）继续等；手工确认/撤回/改写当前/
 * 重排换头重新计时，队首不变的重排与编辑其他提示不重置。设置经 POST /api/auto
 * （携带版本号、受控制席保护，成功版本 +1）。发出在服务端进行，无需页面保持
 * 打开；页面每秒轮询（pollState）同步进度、剩余秒数或等待就绪。 */

/* 服务端状态中的自动发出视图；旧服务端/异常响应缺该字段时按关闭处理。 */
function autoState() {
  const a = state && state.auto;
  return a && typeof a === "object"
    ? a
    : { enabled: false, interval: 10, remaining_seconds: null, waiting_ready: false };
}

/* 状态行文案：关闭 / 倒计时剩余秒数 / 暂停冻结 / 等待就绪。 */
function autoStatusView() {
  const a = autoState();
  if (!a.enabled) return { cls: "off", text: "已关闭：手工确认发出" };
  const secs = a.remaining_seconds === null || a.remaining_seconds === undefined
    ? null : Math.max(0, Math.ceil(a.remaining_seconds));
  if (state.paused) {
    return { cls: "paused",
      text: `已暂停 · 恢复后续计（剩余约 ${secs === null ? "—" : secs + " 秒"}）` };
  }
  if (a.waiting_ready) return { cls: "waiting", text: "已到间隔 · 等待灯光、音响就绪…" };
  if (secs === null) return { cls: "on", text: "自动发出已开启" };
  return { cls: "on", text: secs > 0 ? `约 ${secs} 秒后自动发出当前提示` : "即将自动发出…" };
}

/* 演出页的自动发出卡片：开关、间隔设置与状态行。整体重建仅在版本变化或
 * 操作后发生；秒数流逝等轻量更新走 updateAutoDynamic，不打断正在输入的间隔。 */
function renderAuto() {
  const card = $("#auto-card");
  if (!card) return;
  if (!state || state.status === "setup") {
    card.classList.add("hidden");
    card.innerHTML = "";
    return;
  }
  card.classList.remove("hidden");
  const a = autoState();
  card.innerHTML = `
    <div class="card-label">自动发出 · 统一间隔</div>
    <div class="auto-controls">
      <label class="auto-interval-label">间隔
        <input id="auto-interval" type="number" min="1" max="60" step="1"
          value="${a.interval}" aria-label="自动间隔（秒）">
        秒
      </label>
      <button id="auto-apply" class="small" data-action="auto-apply">设置间隔</button>
      <button id="auto-toggle" class="small" data-action="auto-toggle"></button>
    </div>
    <div id="auto-status" class="auto-status"></div>`;
  updateAutoDynamic();
}

/* 轻量更新：状态行、开关按钮与间隔输入框（输入框聚焦时不覆盖页面上的值）。
 * 轮询到同版本状态、或请求结束后都会走到这里。 */
function updateAutoDynamic() {
  const card = $("#auto-card");
  if (!card || card.classList.contains("hidden")) return;
  const a = autoState();
  const view = autoStatusView();
  const statusEl = $("#auto-status", card);
  if (statusEl) {
    statusEl.textContent = view.text;
    statusEl.className = "auto-status " + view.cls;
  }
  const toggle = $("#auto-toggle", card);
  if (toggle) {
    toggle.textContent = a.enabled ? "关闭自动 ⏹" : "开启自动 ▶";
    toggle.classList.toggle("primary", !a.enabled);
  }
  const input = $("#auto-interval", card);
  if (input && document.activeElement !== input) input.value = a.interval;
  updateAutoButtons();
}

/* 开关 / 设置按钮与间隔输入的可用性：请求在途、非控制席或非演出中禁用
 * （服务端同样拒绝）；与其他写入口一样随 busy 与资格同步。 */
function updateAutoButtons() {
  const card = $("#auto-card");
  if (!card || card.classList.contains("hidden")) return;
  const usable = !busy && canWrite() && !!state && state.status === "live";
  const toggle = $("#auto-toggle", card);
  if (toggle) toggle.disabled = !usable;
  const apply = $("#auto-apply", card);
  if (apply) apply.disabled = !usable;
  const input = $("#auto-interval", card);
  if (input) input.disabled = !usable;
}

/* 读取间隔输入框：1～60 的整数才合法，否则提示并返回 null（不发请求）。 */
function readAutoInterval() {
  const card = $("#auto-card");
  const input = card ? $("#auto-interval", card) : null;
  const value = input ? Number(input.value) : NaN;
  if (!Number.isInteger(value) || value < 1 || value > 60) {
    toast("自动间隔须为 1～60 秒的整数");
    if (input) input.focus();
    return null;
  }
  return value;
}

/* 每秒轮询同步进度：自动发出在服务端后台进行，页面据此刷新已发记录、版本号
 * 与倒计时。轮询是纯查询（GET /api/state），不改变任何状态；有写请求在途时
 * 跳过本次，避免旧响应覆盖写操作带回的最新状态。 */
async function pollState() {
  if (busy || !state) return;
  let s;
  try {
    const headers = {};
    if (controlToken) headers["X-Control-Token"] = controlToken;
    const res = await fetch("/api/state", { headers });
    if (!res.ok) return;
    s = await res.json();
  } catch (err) {
    return; // 网络抖动：下次轮询再试
  }
  if (busy) return; // 轮询在途时页面发起了写操作：写响应会带回最新状态
  if (s.version < state.version) return; // 更旧的轮询响应迟到：丢弃，不回退状态
  const editing = editingId !== null || editingIssueId !== null;
  if (s.version === state.version || editing) {
    /* 版本未变（仅时间流逝或资格变化），或正在编辑/记问题：只同步数据与
     * 轻量显示（自动卡片、控制席标记、版本角标），不重建页面——避免打断
     * 正在输入的内容；编辑保存时若已过期会按 409 统一处理。 */
    state = s;
    if (typeof s.control_granted === "boolean") {
      controlGranted = s.control_granted;
      if (s.control_enabled && !s.control_granted && controlToken) {
        saveControlToken(null);
      }
    }
    renderControl();
    updateAutoDynamic();
    renderScreenBar(); // 画面状态条随每次轮询同步（正常情况下画面变化版本也 +1）
    // 改写提议不推进版本：另一标签页提交 / 审核后，同版本轮询也要刷新侧栏
    // 列表与审核按钮（只重建侧栏列表，不重建待发列表，避免打断正在填写的
    // 提议 / 编辑表单）。
    if (s.status !== "setup") {
      renderSuggestions();
      updateSuggestionButtons();
    }
    $("#version-badge").textContent = "v" + s.version;
    return;
  }
  applyState(s); // 版本变化（含后台自动发出）：整体刷新
}

/* ---------------------------------------------------------------- 渲染 */

function render() {
  if (!state) return;
  $("#version-badge").textContent = "v" + state.version;
  renderControl(); // 刷新或收到 403 后都会走到这里，标记随之更新

  const inSetup = state.status === "setup";
  $("#setup-view").classList.toggle("hidden", !inSetup);
  $("#live-view").classList.toggle("hidden", inSetup);
  renderAuto(); // 自动发出卡片：开场前隐藏，演出页显示开关与倒计时
  renderScreenBar(); // 观众画面状态条：开场前隐藏（需在 setup 提前返回前同步）
  if (inSetup) {
    // 从演出中/完成回到开场前（开始前加载、重置或归档成功）时清空旧录入
    if (prevStatus !== "setup") resetSetupRows();
    prevStatus = "setup";
    renderVerifySetup(); // 开场前可启用本场核验
    return;
  }
  prevStatus = state.status;

  $("#done-banner").classList.toggle("hidden", state.status !== "done");
  $("#paused-banner").classList.toggle("hidden", !state.paused);
  $("#live-view").classList.toggle("paused", !!state.paused);
  const pauseBtn = $("#pause-btn");
  pauseBtn.textContent = state.paused ? "恢复发出 ▶" : "暂停发出 ⏸";
  pauseBtn.classList.toggle("primary", !!state.paused);
  pauseBtn.classList.toggle("warning-outline", !state.paused);
  $("#progress").textContent =
    `已发 ${state.issued.length} / 共 ${state.total} · 待发 ${state.remaining}`;

  const cur = state.current;
  // 启用本场核验且两工种未全部就绪时，确认按钮禁用并提示待就绪工种
  const v = verifyState();
  let confirmLabel = "确认发出 ✓";
  if (state.paused) {
    confirmLabel = "已暂停 · 恢复后可发出 ⏸";
  } else if (v.enabled && !v.all_ready) {
    const waiting = VERIFY_DEPTS.filter(([key]) => v[key] !== true)
      .map(([, label]) => label);
    confirmLabel = `待${waiting.join("、")}就绪`;
  }
  $("#current-card").innerHTML = cur
    ? `<div class="card-label">当前待发</div>
       <div class="cue-number">${escapeHtml(cur.number)}</div>
       <div class="cue-content">${escapeHtml(cur.content)}</div>
       <button id="confirm-btn" class="primary big" data-action="confirm">${confirmLabel}</button>`
    : `<div class="card-label">当前待发</div><div class="empty">— 全部已发出 —</div>`;

  const nxt = state.next;
  $("#next-card").innerHTML = nxt
    ? `<div class="card-label">下一条</div>
       <div class="cue-number small">${escapeHtml(nxt.number)}</div>
       <div class="cue-content small">${escapeHtml(nxt.content)}</div>`
    : `<div class="card-label">下一条</div><div class="empty">— 无 —</div>`;

  renderPending();
  renderIssued();
  renderIssues();
  renderSuggestions(); // 改写提议：原文 / 建议 / 处理状态与控制席审核入口
  renderVerify(); // 核验卡片：当前轮次与两工种就绪状态
  renderScreenBar(); // 观众字幕屏当前画面监看（开场前隐藏）
  updateBusy();
}

function renderPending() {
  const ul = $("#pending-list");
  const items = state.pending;
  const writable = canWrite(); // 非控制席：改写 / 记问题 / 重排按钮全部禁用
  if (!items.length) {
    ul.innerHTML = `<li class="empty">全部提示已发出</li>`;
    return;
  }
  ul.innerHTML = items.map((c, i) => {
    if (c.id === editingIssueId) {
      return issueEditHtml(c, "pending");
    }
    if (c.id === suggestingId) {
      return suggestionEditHtml(c);
    }
    if (c.id === subtitleId) {
      return subtitleEditHtml(c);
    }
    if (c.id === editingId) {
      return `<li class="pending-item editing" data-id="${c.id}">
        <input class="num-input" value="${escapeHtml(c.number)}" maxlength="30" aria-label="编号">
        <span class="ops">
          <button class="primary" data-action="save-edit" data-id="${c.id}" ${writable ? "" : "disabled"}>保存</button>
          <button data-action="cancel-edit">取消</button>
        </span>
        <textarea class="content-input" rows="2" aria-label="内容">${escapeHtml(c.content)}</textarea>
      </li>`;
    }
    const tag = i === 0 ? `<span class="tag now">当前</span>`
              : i === 1 ? `<span class="tag next">下一条</span>` : "";
    return `<li class="pending-item ${i === 0 ? "is-current" : ""}" data-id="${c.id}">
      <span class="badge">${escapeHtml(c.number)}</span>
      <span class="content">${escapeHtml(c.content)}</span>
      ${tag}
      ${subtitleTagHtml(c)}
      <span class="ops">
        <button data-action="edit" data-id="${c.id}" title="改写" ${writable ? "" : "disabled"}>✎</button>
        <button data-action="add-subtitle" data-id="${c.id}"
          title="设置观众字幕（独立于提示正文；发出时有效字幕上屏，改写后失效）"
          ${writable ? "" : "disabled"}">字幕</button>
        <button data-action="add-issue" data-id="${c.id}" title="记现场问题" ${writable ? "" : "disabled"}>⚠</button>
        <button data-action="add-suggestion" data-id="${c.id}"
          title="提交改写提议（无控制权也可提交，控制席审核后才改提示）">💡</button>
        <button data-action="up" data-idx="${i}" ${i === 0 || !writable ? "disabled" : ""} title="上移">↑</button>
        <button data-action="down" data-idx="${i}" ${i === items.length - 1 || !writable ? "disabled" : ""} title="下移">↓</button>
      </span>
    </li>`;
  }).join("");
}

function renderIssued() {
  const ul = $("#issued-list");
  const writable = canWrite();
  if (!state.issued.length) {
    ul.innerHTML = `<li class="empty">尚无已发记录</li>`;
    return;
  }
  ul.innerHTML = state.issued.map((c) => {
    if (c.id === editingIssueId) return issueEditHtml(c, "issued");
    const t = c.issued_at ? new Date(c.issued_at).toLocaleTimeString() : "";
    return `<li class="issued-item">
      <span class="badge done">✓ ${escapeHtml(c.number)}</span>
      <span class="content">${escapeHtml(c.content)}</span>
      <span class="time">${t}</span>
      <span class="ops">
        <button data-action="add-issue" data-id="${c.id}" title="记现场问题" ${writable ? "" : "disabled"}>⚠</button>
      </span>
    </li>`;
  }).join("");
}

/* 为一条已发/待发提示追加现场问题记录的内联表单（暂停时同样可用）。
 * 已发提示记录其发出快照，待发提示记录当前编号与内容。 */
function issueEditHtml(c, cueStatus) {
  const statusTag = cueStatus === "issued"
    ? `<span class="tag issue-issued">已发</span>`
    : `<span class="tag issue-pending">待发</span>`;
  return `<li class="issue-edit" data-id="${c.id}">
    <span class="badge">${escapeHtml(c.number)}</span>
    ${statusTag}
    <span class="ops">
      <button class="primary" data-action="save-issue" data-id="${c.id}" ${canWrite() ? "" : "disabled"}>保存记录</button>
      <button data-action="cancel-issue">取消</button>
    </span>
    <textarea class="issue-input" rows="2" maxlength="200"
      placeholder="现场问题（去除首尾空白后 1～200 字符）" aria-label="现场问题"></textarea>
    <p class="hint-inline issue-meta">将随版本 v${state.version} 保存为服务端时间的快照；不改提示进度${state.paused ? "，暂停时也可记录" : ""}。</p>
  </li>`;
}

async function saveIssue(id) {
  const li = $(`.issue-edit[data-id="${id}"]`);
  const input = li ? $(".issue-input", li) : null;
  const text = input ? input.value.trim() : "";
  if (!text) { toast("问题内容不能为空"); if (input) input.focus(); return; }
  if (text.length > 200) { toast("问题内容不能超过 200 字符"); if (input) input.focus(); return; }
  // 400（正文不合规）时 api 不刷新状态，表单与已输入文字保留；
  // 409 时 api 已应用最新状态并提示；成功后退出表单，记录出现在侧栏。
  const data = await api("/api/issues", { version: state.version, cue_id: id, text });
  if (data) {
    editingIssueId = null;
    render();
  }
}

/* ---------------------------------------------------------------- 改写提议
 * 无控制权的页面（只读页也可以）点待发提示后的 💡 即可提交改写建议：
 * POST /api/suggestions 提交 cue_id、建议内容与页面所见版本号，服务端只保存
 * 原文快照、不改提示也不改版本号；控制席在右侧列表凭最新版本号接受或拒绝：
 * 接受时服务端要求目标仍待发且内容等于原文快照（其他提示编辑、暂停、重排
 * 不使提议失效），"写入建议 + 标记已接受 + 版本 +1"同一事务完成；拒绝仅
 * 标记状态、不改版本。版本过期 / 已处理 / 目标失效均返回 409 附最新状态。 */

/* 为一条待发提示提交改写提议的内联表单：只读页（无控制凭据）也可提交。 */
function suggestionEditHtml(c) {
  return `<li class="suggestion-edit" data-id="${c.id}">
    <span class="badge">${escapeHtml(c.number)}</span>
    <span class="tag issue-pending">改写提议</span>
    <span class="ops">
      <button class="primary" data-action="save-suggestion" data-id="${c.id}">提交提议</button>
      <button data-action="cancel-suggestion">取消</button>
    </span>
    <div class="suggestion-original" title="提交时的原文快照">原文：${escapeHtml(c.content)}</div>
    <textarea class="suggestion-input" rows="2"
      placeholder="建议改写为的内容（去除首尾空白后不能为空）" aria-label="建议内容"></textarea>
    <p class="hint-inline issue-meta">只保存建议与原文快照，<b>不直接改动提示、不改版本</b>；需控制席审核接受后才生效。</p>
  </li>`;
}

/* 接受 / 拒绝 / 提交按钮的可用性：
 * - 提交与取消（待发列表内的表单）：任何页面可用，仅在请求在途时禁用防重复；
 * - 接受 / 拒绝（侧栏列表）：另需本页具备控制资格；目标已失效的待审提议
 *   不允许接受（服务端同样以 409 拒绝），仍可拒绝；已处理的不再显示按钮。 */
function updateSuggestionButtons() {
  if (!state || state.status === "setup") return;
  const writable = canWrite();
  $$('button[data-action="save-suggestion"]').forEach((btn) => {
    btn.disabled = busy;
  });
  $$('#suggestions-list button[data-action="accept-suggestion"]').forEach((btn) => {
    const valid = btn.dataset.valid === "1";
    btn.disabled = busy || !writable || !valid;
  });
  $$('#suggestions-list button[data-action="reject-suggestion"]').forEach((btn) => {
    btn.disabled = busy || !writable;
  });
}

const SUGGESTION_STATUS = {
  pending: { label: "待审核", cls: "sug-pending" },
  accepted: { label: "已接受", cls: "sug-accepted" },
  rejected: { label: "已拒绝", cls: "sug-rejected" },
};

function renderSuggestions() {
  const ul = $("#suggestions-list");
  const count = $("#suggestions-count");
  const items = state.suggestions || [];
  const pendingCount = items.filter((x) => x.status === "pending").length;
  if (count) {
    count.textContent = items.length
      ? `（${pendingCount} 条待审 · 共 ${items.length} 条，按提交顺序）`
      : "";
  }
  if (!items.length) {
    ul.innerHTML = `<li class="empty">尚无改写提议；点待发提示后的 💡，无控制权的页面也可提交建议，由控制席审核。</li>`;
    return;
  }
  const writable = canWrite();
  ul.innerHTML = items.map((s) => {
    const meta = SUGGESTION_STATUS[s.status] || SUGGESTION_STATUS.pending;
    let stateLine;
    if (s.status === "pending") {
      stateLine = s.target_valid
        ? `<span class="tag ${meta.cls}">${meta.label}</span>
           <span class="hint-inline">目标仍待发且与原文一致，可接受</span>`
        : `<span class="tag ${meta.cls}">${meta.label}</span>
           <span class="hint-inline sug-invalid">目标已改动或已发出：接受将被拒绝，可拒绝此提议</span>`;
    } else {
      stateLine = `<span class="tag ${meta.cls}">${meta.label}</span>
        <span class="time">${fmtTime(s.decided_at)}</span>`;
    }
    const actions = s.status === "pending"
      ? `<span class="ops">
          <button class="small primary" data-action="accept-suggestion" data-id="${s.id}"
            data-valid="${s.target_valid ? "1" : "0"}"
            title="接受：把该提示改写为建议内容（同一事务标记已接受、版本 +1）"
            ${writable && s.target_valid ? "" : "disabled"}>接受 ✓</button>
          <button class="small" data-action="reject-suggestion" data-id="${s.id}"
            title="拒绝：仅标记已拒绝，不改提示与版本"
            ${writable ? "" : "disabled"}>拒绝 ✕</button>
        </span>`
      : "";
    return `<li class="suggestion-item sug-${s.status}">
      <div class="issue-top">
        <span class="badge">${escapeHtml(s.cue_number)}</span>
        ${stateLine}
        <span class="time">提交于 ${fmtTime(s.created_at)}</span>
      </div>
      <div class="suggestion-original" title="提交时的原文快照">原文：${escapeHtml(s.original_content)}</div>
      <div class="suggestion-text" title="建议内容">建议：${escapeHtml(s.content)}</div>
      ${actions}
    </li>`;
  }).join("");
}

/* 提交改写提议：建议内容非空即可（沿用编辑接口的内容校验，服务端去空白）。
 * 提交不改版本，成功后同版本状态中会出现新提议；409（过期 / 目标已发出）
 * 由 api() 统一刷新并提示；400 / 网络失败时表单保留已输入内容。 */
async function saveSuggestion(id) {
  const li = $(`.suggestion-edit[data-id="${id}"]`);
  const input = li ? $(".suggestion-input", li) : null;
  const text = input ? input.value.trim() : "";
  if (!text) { toast("建议内容不能为空"); if (input) input.focus(); return; }
  const data = await api("/api/suggestions", { version: state.version, cue_id: id, content: text });
  if (data) {
    suggestingId = null;
    render();
    toast("已提交改写提议，等待控制席审核");
  }
}

/* 控制席接受 / 拒绝待审提议：携带页面最新版本号（apiHeaders 自动带凭据）。
 * 接受在服务端同事务完成"改提示 + 标记已接受 + 版本 +1"；拒绝只改提议
 * 状态、不改版本。409（版本过期 / 提议已处理 / 目标失效）由 api() 统一
 * 刷新为最新状态并提示，提议本身保持不变。 */
function decideSuggestion(id, accept) {
  const path = `/api/suggestions/${encodeURIComponent(id)}/${accept ? "accept" : "reject"}`;
  api(path, { version: state.version }).then((data) => {
    if (data) toast(accept ? "已接受改写提议：提示已更新" : "已拒绝该改写提议");
  });
}

/* ---------------------------------------------------------------- 观众字幕
 * 执行台可为**演出中的待发提示**另行设置观众字幕（POST /api/subtitles，
 * 携带版本号、受控制席保护；文本去空白后 1～200 字符，null 清除）。字幕
 * 绑定提示身份：手工编辑 / 接受提议改写该提示（即使同文）即失效，重排不
 * 影响。设置只改字幕与版本号，不推进提示、不换核验轮次、不重新计时，也
 * 不改变观众当前画面——画面只随实际发出（有效字幕上屏 / 失效清屏）、撤回
 * （清屏留设置）、归档 / 重置 / 开新场（清屏清设置）变化。观众另开
 * /screen 独立页轮询 GET /api/screen，只看到字幕与跨场递增的画面序号。 */

const SUBTITLE_MAX_LEN = 200;

/* 服务端状态中的观众画面视图；旧服务端 / 异常响应缺该字段时按清屏处理。 */
function screenState() {
  const sc = state && state.screen;
  return sc && typeof sc === "object" ? sc : { subtitle: null, seq: 0 };
}

/* 执行台顶部的观众画面状态条：当前字幕（或清屏中）与跨场画面序号。 */
function renderScreenBar() {
  const bar = $("#screen-bar");
  if (!bar) return;
  if (!state || state.status === "setup") {
    bar.classList.add("hidden");
    return;
  }
  bar.classList.remove("hidden");
  const sc = screenState();
  const textEl = $("#screen-bar-text");
  if (sc.subtitle) {
    bar.classList.remove("screen-blank");
    textEl.textContent = sc.subtitle;
    textEl.title = sc.subtitle;
  } else {
    bar.classList.add("screen-blank");
    textEl.textContent = "清屏中（无字幕）";
    textEl.title = "";
  }
  $("#screen-bar-seq").textContent = "画面 #" + sc.seq;
}

/* 待发列表中一条提示的字幕设置 / 有效性标记。 */
function subtitleTagHtml(c) {
  if (!c.subtitle) return "";
  return c.subtitle_valid
    ? `<span class="tag sub-on" title="已绑定有效字幕：该提示发出时观众屏将显示此字幕（而非提示正文）">字幕 ✓</span>`
    : `<span class="tag sub-off" title="该提示被手工改写或接受提议后字幕已失效：发出时观众屏清屏；可重新设置">字幕已失效</span>`;
}

/* 字幕设置内联表单（控制席操作）：空提交表示清除设置。 */
function subtitleEditHtml(c) {
  return `<li class="subtitle-edit" data-id="${c.id}">
    <span class="badge">${escapeHtml(c.number)}</span>
    <span class="tag issue-pending">观众字幕</span>
    <span class="ops">
      <button class="primary" data-action="save-subtitle" data-id="${c.id}">保存字幕</button>
      <button data-action="cancel-subtitle">取消</button>
    </span>
    <textarea class="subtitle-input" rows="2" maxlength="200"
      placeholder="观众字幕（去除首尾空白后 1～200 字符；保存空内容为清除字幕设置）"
      aria-label="观众字幕">${escapeHtml(c.subtitle || "")}</textarea>
    <p class="hint-inline issue-meta">字幕<b>独立于提示正文</b>：该提示发出时观众屏显示字幕；手工改写或接受提议改写本提示会使字幕失效，重排不影响。设置不推进提示、不换核验轮次。</p>
  </li>`;
}

/* 保存 / 取消按钮可用性：请求在途禁用防重复，另需本页具备控制资格。 */
function updateSubtitleButtons() {
  if (!state || state.status === "setup") return;
  const writable = canWrite();
  $$('button[data-action="save-subtitle"]').forEach((btn) => {
    btn.disabled = busy || !writable;
  });
  // 待发行内的字幕入口同样随在途 / 资格禁用
  $$('#pending-list button[data-action="add-subtitle"]').forEach((btn) => {
    btn.disabled = busy || !writable;
  });
}

/* 保存字幕：去除首尾空白后 1～200 字符；空内容作为清除（subtitle=null）。
 * 400（超长）不刷新状态、保留输入；409 由 api() 统一刷新；成功退出表单。 */
async function saveSubtitle(id) {
  const li = $(`.subtitle-edit[data-id="${id}"]`);
  const input = li ? li.querySelector(".subtitle-input") : null;
  const raw = input ? input.value : "";
  const text = raw.trim();
  if (text.length > SUBTITLE_MAX_LEN) {
    toast(`字幕去除首尾空白后不能超过 ${SUBTITLE_MAX_LEN} 字符`);
    if (input) input.focus();
    return;
  }
  const data = await api("/api/subtitles", {
    version: state.version,
    cue_id: id,
    subtitle: text ? text : null, // 空内容：清除该提示的字幕设置
  });
  if (data) {
    subtitleId = null;
    render();
    toast(text ? "已设置观众字幕" : "已清除该提示的字幕设置");
  }
}

function renderIssues() {
  const ul = $("#issues-list");
  const count = $("#issues-count");
  const issues = state.issues || [];
  if (count) count.textContent = issues.length ? `（${issues.length} 条，按保存顺序）` : "";
  if (!issues.length) {
    ul.innerHTML = `<li class="empty">尚无现场问题记录；点提示后的 ⚠ 可随时记录（含暂停时）</li>`;
    return;
  }
  ul.innerHTML = issues.map((it) => {
    const tag = it.cue_status === "issued"
      ? `<span class="tag issue-issued">已发</span>`
      : `<span class="tag issue-pending">待发</span>`;
    const t = it.created_at ? new Date(it.created_at).toLocaleString() : "";
    return `<li class="issue-item">
      <div class="issue-top">
        <span class="badge">${escapeHtml(it.cue_number)}</span>
        ${tag}
        <span class="time">${t}</span>
      </div>
      <div class="issue-text">${escapeHtml(it.text)}</div>
      <div class="issue-cue-content" title="记录时该提示的内容快照">↳ ${escapeHtml(it.cue_content)}</div>
    </li>`;
  }).join("");
}

/* ---------------------------------------------------------------- 设置页 */

function resetSetupRows() {
  $("#cue-rows").innerHTML = "";
  addRow();
  addRow();
}

function addRow() {
  const div = document.createElement("div");
  div.className = "cue-row";
  div.innerHTML = `
    <span class="row-index"></span>
    <input class="num" placeholder="编号（唯一）" maxlength="30">
    <input class="cnt" placeholder="提示内容（必填）">
    <button class="remove-row" title="删除此行">✕</button>`;
  div.querySelector(".remove-row").addEventListener("click", () => {
    if ($$(".cue-row").length <= state.min_cues) {
      toast(`至少需要 ${state.min_cues} 条提示`);
      return;
    }
    div.remove();
    renumberRows();
  });
  $("#cue-rows").appendChild(div);
  renumberRows();
}

function renumberRows() {
  $$(".cue-row").forEach((r, i) => { $(".row-index", r).textContent = i + 1; });
  $("#add-row").disabled = $$(".cue-row").length >= state.max_cues;
}

/* ---------------------------------------------------------------- 事件 */

document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-action]");
  if (!btn || btn.disabled) return;
  const action = btn.dataset.action;

  if (action === "confirm") {
    if (!state.current) return;
    api("/api/confirm", { version: state.version });
  } else if (action === "edit") {
    editingId = Number(btn.dataset.id);
    editingIssueId = null;
    suggestingId = null;
    subtitleId = null;
    render();
  } else if (action === "cancel-edit") {
    editingId = null;
    render();
  } else if (action === "save-edit") {
    const li = btn.closest("li");
    const number = $(".num-input", li).value.trim();
    const content = $(".content-input", li).value.trim();
    if (!number || !content) { toast("编号和内容都不能为空"); return; }
    const id = Number(btn.dataset.id);
    editingId = null;
    api("/api/edit", { version: state.version, cue_id: id, number, content });
  } else if (action === "up" || action === "down") {
    const ids = state.pending.map((c) => c.id);
    const i = Number(btn.dataset.idx);
    const j = action === "up" ? i - 1 : i + 1;
    if (j < 0 || j >= ids.length) return;
    [ids[i], ids[j]] = [ids[j], ids[i]];
    api("/api/reorder", { version: state.version, order: ids });
  } else if (action === "add-issue") {
    const id = Number(btn.dataset.id);
    // 同一时刻只开一个问题记录表单
    editingIssueId = editingIssueId === id ? null : id;
    editingId = null;
    suggestingId = null;
    subtitleId = null;
    render();
  } else if (action === "cancel-issue") {
    editingIssueId = null;
    render();
  } else if (action === "save-issue") {
    saveIssue(Number(btn.dataset.id));
  } else if (action === "add-suggestion") {
    // 无控制权的页面也可提交改写提议：同刻只开一个提议表单（无需控制凭据）
    const id = Number(btn.dataset.id);
    suggestingId = suggestingId === id ? null : id;
    editingId = null;
    editingIssueId = null;
    subtitleId = null;
    render();
  } else if (action === "cancel-suggestion") {
    suggestingId = null;
    render();
  } else if (action === "save-suggestion") {
    if (busy) return; // 请求在途防重复提交
    saveSuggestion(Number(btn.dataset.id));
  } else if (action === "add-subtitle") {
    // 观众字幕设置（控制席操作）：同刻只开一个字幕表单
    const id = Number(btn.dataset.id);
    subtitleId = subtitleId === id ? null : id;
    editingId = null;
    editingIssueId = null;
    suggestingId = null;
    render();
  } else if (action === "cancel-subtitle") {
    subtitleId = null;
    render();
  } else if (action === "save-subtitle") {
    if (busy) return; // 请求在途防重复提交
    saveSubtitle(Number(btn.dataset.id));
  } else if (action === "accept-suggestion" || action === "reject-suggestion") {
    if (!canWrite()) {
      toast("本页面不是当前控制席：可凭最新版本号接管后再审核");
      return;
    }
    decideSuggestion(Number(btn.dataset.id), action === "accept-suggestion");
  } else if (action === "verify-report") {
    // 工种就绪回报：无需控制凭据（缺失/失效凭据也不 403），只读页面也可提交；
    // 本页缓存的凭据由 apiHeaders 自动随请求提交，供服务端回报本页资格
    reportVerify(btn.dataset.dept);
  } else if (action === "auto-toggle") {
    // 开关自动发出：开启时提交输入框中的间隔；关闭保留已设间隔
    if (autoState().enabled) {
      api("/api/auto", { version: state.version, enabled: false });
    } else {
      const interval = readAutoInterval();
      if (interval === null) return;
      api("/api/auto", { version: state.version, enabled: true, interval });
    }
  } else if (action === "auto-apply") {
    // 设置间隔：开启中改间隔会从完整间隔重新计时（服务端规则）
    const interval = readAutoInterval();
    if (interval === null) return;
    api("/api/auto", { version: state.version, enabled: autoState().enabled, interval });
  }
});

$("#add-row").addEventListener("click", () => {
  if ($$(".cue-row").length >= state.max_cues) {
    toast(`最多 ${state.max_cues} 条提示`);
    return;
  }
  addRow();
});

$("#start-show").addEventListener("click", () => {
  const cues = $$(".cue-row").map((r) => ({
    number: $(".num", r).value.trim(),
    content: $(".cnt", r).value.trim(),
  }));
  if (cues.length < state.min_cues || cues.length > state.max_cues) {
    toast(`提示数量需在 ${state.min_cues}～${state.max_cues} 条之间`);
    return;
  }
  if (cues.some((c) => !c.number)) { toast("编号不能为空"); return; }
  if (new Set(cues.map((c) => c.number)).size !== cues.length) {
    toast("编号必须唯一");
    return;
  }
  if (cues.some((c) => !c.content)) { toast("内容不能为空"); return; }
  api("/api/setup", { cues });
});

$("#reset-btn").addEventListener("click", () => {
  if (!confirm("确定要清空当前进度，重新设置本场提示吗？")) return;
  api("/api/reset", {});
});

/* 启用本场核验（仅开场前）：凭当前版本号，受控制席保护；成功后本场演出中
 * 灯光、音响须分别对当前提示报就绪，全部就绪才能确认发出。 */
$("#verify-enable-btn").addEventListener("click", () => {
  if (!state || busy || !canWrite()) return;
  api("/api/verify/enable", { version: state.version });
});

$("#pause-btn").addEventListener("click", () => {
  if (!state || state.status !== "live") return;
  // 提交目标暂停状态与页面所见版本号；同版本的其他操作先生效时会拿到 409
  api("/api/pause", { version: state.version, paused: !state.paused });
});

/* 撤回末条已发提示（误点确认后补救）：二次确认后提交页面所见的末条已发
 * id 与版本号。服务端把它按发出快照插到待发队首，其他已发/待发与问题
 * 记录都不变；暂停时也可撤回，暂停标记不变。同版本竞争失败由 api() 统一
 * 弹 409 提示并刷新。 */
$("#withdraw-btn").addEventListener("click", () => {
  if (!state || !state.issued.length) return;
  const last = state.issued[state.issued.length - 1];
  const ok = confirm(
    `撤回最后一条已发提示「${last.number}」？\n\n` +
    `它将按发出时的内容退回为当前待发（排在待发队首），保留原编号；\n` +
    "其他已发记录、待发顺序与现场问题记录都不受影响。\n\n" +
    "该提示之后再次确认时会记录新的发出时间。确定撤回吗？"
  );
  if (!ok) return;
  // 成功响应需要定制提示；403/409/网络失败仍走 api() 内的统一处理
  api("/api/withdraw", { version: state.version, cue_id: last.id })
    .then((data) => {
      if (data) toast(`已撤回末条提示「${last.number}」：已退回待发队首`);
    });
});

$("#control-btn").addEventListener("click", () => {
  if (!state || busy) return;
  if (!state.control_enabled) {
    claimControl(true); // 未启用：本页启用并成为控制席
    return;
  }
  if (controlGranted) return; // 本页已是控制席（按钮此时隐藏，双保险）
  if (!confirm("接管后原控制席的凭据立即失效，其后续写操作将被拒绝（对方可凭最新版本号再次接管）。确定接管控制席吗？")) return;
  claimControl(false);
});

/* ---------------------------------------------------------------- 档案事件 */

$("#archives-btn").addEventListener("click", openArchivesModal);
$("#archive-btn").addEventListener("click", openArchiveForm);
$("#archives-close").addEventListener("click", closeArchivesModal);
$("#archives-modal").addEventListener("click", (e) => {
  if (e.target === $("#archives-modal")) closeArchivesModal();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("#archives-modal").classList.contains("hidden")) {
    closeArchivesModal();
  }
});

$("#archives-body").addEventListener("click", (e) => {
  const btn = e.target.closest("[data-action]");
  if (!btn || btn.disabled) return;
  const action = btn.dataset.action;
  if (action === "archive-form") {
    openArchiveForm();
  } else if (action === "archive-list") {
    loadArchiveList();
  } else if (action === "archive-cancel") {
    closeArchivesModal();
  } else if (action === "archive-submit") {
    submitArchive();
  } else if (action === "archive-detail") {
    loadArchiveDetail(Number(btn.dataset.id));
  } else if (action === "start-from-archive") {
    submitStartFromArchive();
  } else if (action === "compare-view") {
    openCompareView();
  }
});

$("#archives-body").addEventListener("change", (e) => {
  if (e.target.id === "compare-base" || e.target.id === "compare-compare") {
    compareSelectionChanged();
  }
});

$("#archives-body").addEventListener("keydown", (e) => {
  if (e.key !== "Enter") return;
  if (e.target.id === "archive-name") {
    e.preventDefault();
    submitArchive();
    return;
  }
  const item = e.target.closest("[data-action='archive-detail']");
  if (item) loadArchiveDetail(Number(item.dataset.id));
});

/* ---------------------------------------------------------------- 启动 */

(async function init() {
  try {
    /* 启动即提交本标签页缓存的凭据供服务端核验：被接管后的旧凭据会得到
     * control_granted: false，页面直接只读并显示接管入口，无需先触发 403；
     * 凭据仍有效则恢复本页控制；未启用控制席时该头不影响旧行为。 */
    const headers = {};
    if (controlToken) headers["X-Control-Token"] = controlToken;
    const res = await fetch("/api/state", { headers });
    applyState(await res.json());
  } catch (err) {
    toast("无法连接服务端：" + err.message);
  }
})();

/* 自动发出在服务端后台计时：页面打开期间每秒轮询同步进度与倒计时（纯查询），
 * 页面关闭不影响发出。测试环境无 setInterval 时不启动轮询。 */
if (typeof setInterval === "function") {
  setInterval(pollState, 1000);
}
