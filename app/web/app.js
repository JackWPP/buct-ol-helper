/* 雨课堂网课助手 · 看板 */
"use strict";

const $ = (id) => document.getElementById(id);

/* 支持用 ?token=xxx 直接打开看板：记住口令（后续接口带 X-Access-Token 头），并清理地址栏 */
(function initToken() {
  try {
    const url = new URL(location.href);
    const t = url.searchParams.get("token");
    if (t) {
      localStorage.setItem("ykt_token", t);
      url.searchParams.delete("token");
      history.replaceState(null, "", url.pathname + (url.search || "") + (url.hash || ""));
    }
  } catch (e) { /* ignore */ }
})();
let overview = null;
let lastLogId = 0;
let detailCourseId = null;
let qrKey = null;
let qrTimer = null;

/* ---------------- 工具 ---------------- */

function toast(msg, type = "info", ms = 4200) {
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  el.textContent = msg;
  $("toasts").appendChild(el);
  setTimeout(() => el.remove(), ms);
}

async function api(path, method = "GET", body = null) {
  const opts = { method, headers: {} };
  try {
    const tk = localStorage.getItem("ykt_token");
    if (tk) opts.headers["X-Access-Token"] = tk;
  } catch (e) { /* ignore */ }
  if (body !== null) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const resp = await fetch(path, opts);
  if (resp.status === 401 && !path.startsWith("/api/auth")) {
    const data = await resp.json().catch(() => ({}));
    if ((data.detail || "").includes("口令")) {
      location.href = "/login";
      throw new Error("需要访问口令");
    }
  }
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.detail || `HTTP ${resp.status}`);
  return data;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function fmtDur(seconds) {
  const s = Math.max(0, Math.round(seconds || 0));
  if (s < 60) return `${s} 秒`;
  if (s < 3600) return `${Math.round(s / 60)} 分钟`;
  const h = s / 3600;
  return h >= 10 ? `${h.toFixed(0)} 小时` : `${h.toFixed(1)} 小时`;
}

function fmtClock(seconds) {
  const s = Math.max(0, Math.round(seconds || 0));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  const p = (n) => String(n).padStart(2, "0");
  return h > 0 ? `${h}:${p(m)}:${p(sec)}` : `${m}:${p(sec)}`;
}

const STATUS_TEXT = { watching: "刷课中", done: "已完成", paused: "已暂停", idle: "待命" };
const LEAF_STATUS_TEXT = { pending: "待学", watching: "学习中", done: "已完成", failed: "失败", skipped: "已跳过" };

/* 带加载态的按钮包装：避免点了没反应（自动刷课按钮卡顿问题） */
async function withBusy(btn, label, fn) {
  if (!btn) return;
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = label || "处理中…";
  try {
    return await fn();
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
}

/* ---------------- 顶栏与统计 ---------------- */

function renderPills() {
  const a = overview.account, e = overview.engine, jobs = overview.jobs;
  const pills = [];
  const loginCls = a.logged_in ? "ok" : "bad";
  const uname = a.user?.display_name || a.user?.user_id || "未登录";
  pills.push(`<span class="pill ${loginCls}"><i class="dot"></i>${a.logged_in ? "已登录：" + esc(uname) : "未登录"}</span>`);

  const engineCls = e.running ? "ok" : "warn";
  pills.push(`<span class="pill ${engineCls}"><i class="dot"></i>引擎${e.running ? "运行中" : "已暂停"}${e.active_videos ? `（${e.active_videos} 路并行）` : ""}</span>`);

  pills.push(`<span class="pill"><i class="dot"></i>倍速 ${e.speed}x · 并发 ${e.max_parallel}</span>`);
  if (e.dry_run) pills.push(`<span class="pill warn"><i class="dot"></i>演练模式</span>`);
  if (jobs.sync.running) pills.push(`<span class="pill warn"><i class="dot"></i>同步中…</span>`);
  if (jobs.progress.running) pills.push(`<span class="pill warn"><i class="dot"></i>刷新进度中…</span>`);
  $("pills").innerHTML = pills.join("");
}

function renderStats() {
  const st = overview.stats;
  const cards = [
    { label: "课程", value: `${st.courses}`, sub: `启用 ${st.enabled_courses}` },
    { label: "视频总数", value: `${st.videos}` },
    { label: "已完成视频", value: `${st.done_videos}`, cls: "ok", sub: st.videos ? `${Math.round((st.done_videos / st.videos) * 100)}%` : "" },
    { label: "已看时长", value: fmtDur(st.watched_seconds) },
    { label: "剩余时长", value: fmtDur(st.remaining_seconds), cls: "accent" },
    { label: "预计耗时", value: fmtDur(st.eta_seconds), sub: "按当前并发/倍速" },
  ];
  $("stats").innerHTML = cards.map((c) => `
    <div class="stat-card">
      <div class="label">${c.label}</div>
      <div class="value ${c.cls || ""}">${c.value}</div>
      <div class="label">${c.sub || ""}</div>
    </div>`).join("");
}

/* ---------------- 课程 ---------------- */

function renderCourses() {
  const courses = overview.courses || [];
  const sub = $("courses-sub");
  if (!courses.length) {
    sub.textContent = "";
    $("course-grid").innerHTML = `<div class="empty-hint">暂无课程，登录后点击右上角「同步课程」</div>`;
    return;
  }
  sub.textContent = `共 ${courses.length} 门 · 勾选「参与刷课」后生效`;
  $("course-grid").innerHTML = courses.map((c) => {
    const pct = Math.round((c.progress || 0) * 100);
    const cls = c.status === "watching" ? "is-watching" : c.status === "done" ? "is-done" : "";
    const chip = `<span class="chip ${c.status}">${STATUS_TEXT[c.status] || c.status}</span>`;
    const netBadge = c.is_network ? `<span class="badge-net">网络课程</span>` : "";
    return `
    <div class="course-card ${cls}" data-course="${c.id}">
      <div class="course-head">
        <div>
          <div class="course-name">${esc(c.name)}${netBadge}</div>
          <div class="course-meta">${esc(c.teacher || "")} ${c.last_sync_at ? "· 同步于 " + esc(c.last_sync_at) : ""}</div>
        </div>
        ${chip}
      </div>
      <div class="progress-bar ${c.status === "done" ? "done" : ""}"><i style="width:${pct}%"></i></div>
      <div class="course-nums">
        <span>视频 ${c.done_videos}/${c.total_videos}（${pct}%）</span>
        <span>时长 ${fmtDur(c.watched_seconds)} / ${fmtDur(c.total_seconds)}</span>
      </div>
      <div class="course-nums"><span>预计还需 ${c.eta_seconds ? fmtDur(c.eta_seconds) : "—"}</span></div>
      <div class="course-actions">
        <button class="small" data-act="start" data-id="${c.id}">开始</button>
        <button class="small" data-act="pause" data-id="${c.id}">暂停</button>
        <button class="small" data-act="detail" data-id="${c.id}">详情</button>
        <button class="small" data-act="refresh" data-id="${c.id}">刷新进度</button>
        <a class="link-btn" href="${esc(c.platform_url || "#")}" target="_blank" rel="noopener" title="到雨课堂网页版打开这门课（做作业/考试）">平台 ↗</a>
        <span class="spacer"></span>
        <label class="switch">
          <input type="checkbox" data-act="enable" data-id="${c.id}" ${c.enabled ? "checked" : ""}>
          <span class="track"></span>参与刷课
        </label>
      </div>
    </div>`;
  }).join("");
}

$("course-grid").addEventListener("click", async (ev) => {
  const btn = ev.target.closest("button[data-act]");
  if (btn) {
    const id = Number(btn.dataset.id);
    const act = btn.dataset.act;
    try {
      if (act === "start") { await api(`/api/courses/${id}/start`, "POST"); toast("已开始该课程", "success"); }
      else if (act === "pause") { await api(`/api/courses/${id}/pause`, "POST"); toast("已暂停该课程", "success"); }
      else if (act === "detail") { openCourseDetail(id); return; }
      else if (act === "refresh") { await api(`/api/courses/${id}/refresh`, "POST"); toast("进度刷新已启动"); }
      pollOverview();
    } catch (e) { toast(e.message, "error"); }
    return;
  }
  const chk = ev.target.closest("input[data-act='enable']");
  if (chk) {
    try {
      await api(`/api/courses/${Number(chk.dataset.id)}/enable`, "POST", { enabled: chk.checked });
      toast(chk.checked ? "已加入自动刷课" : "已移出自动刷课", "success");
    } catch (e) { toast(e.message, "error"); chk.checked = !chk.checked; }
  }
});

/* ---------------- 实时动态 ---------------- */

async function loadLive() {
  try {
    const res = await api("/api/system/live");
    const active = res.active || [];
    const el = $("live-panel");
    if (!active.length) {
      el.innerHTML = `<div class="empty-hint">${res.engine.running ? "正在调度下一个视频…" : "当前没有正在观看的视频"}</div>`;
      return;
    }
    el.innerHTML = active.map((a) => {
      const pct = Math.round((a.progress || 0) * 100);
      const pos = fmtClock(a.position);
      const dur = a.duration ? fmtClock(a.duration) : "--:--";
      const idle = a.idle_sec > 25
        ? `<span class="warn-text">已 ${Math.round(a.idle_sec)}s 无更新</span>`
        : `<span>${Math.round(a.idle_sec)}s 前更新</span>`;
      return `<div class="live-card ${a.stale ? "warn" : ""}">
        <div class="live-head">
          <div>
            <div class="live-title"><span class="pulse"></span>${esc(a.title)}</div>
            <div class="live-course">${esc(a.course)} · ${esc(a.phase || "")}</div>
          </div>
          <div class="live-pos">${pos} / ${dur}</div>
        </div>
        <div class="progress-bar"><i style="width:${pct}%"></i></div>
        <div class="live-meta">
          <span>进度 ${pct}%</span><span>${a.speed}x</span>
          <span>已上报 ${a.flushes || 0} 批 / ${a.events_sent || 0} 条心跳</span>${idle}
        </div>
        ${a.last_response ? `<div class="live-resp">平台响应：${esc(a.last_response)}</div>` : ""}
      </div>`;
    }).join("");
  } catch (e) { /* 静默 */ }
}

/* ---------------- 日志 ---------------- */

async function loadLogs() {
  try {
    const res = await api(`/api/system/logs?after_id=${lastLogId}`);
    const logs = res.logs || [];
    if (!logs.length) return;
    const list = $("log-list");
    if (list.querySelector(".log-empty")) list.innerHTML = "";
    const frag = document.createDocumentFragment();
    logs.reverse().forEach((l) => {
      lastLogId = Math.max(lastLogId, l.id);
      const div = document.createElement("div");
      div.className = `log-line level-${l.level}`;
      div.innerHTML = `<span class="t">${esc(l.ts)}</span><span class="m">${esc(l.message)}</span>`;
      frag.appendChild(div);
    });
    list.prepend(frag);
    while (list.children.length > 200) list.removeChild(list.lastChild);
  } catch (e) { /* 静默 */ }
}

/* ---------------- 总览轮询 ---------------- */

async function pollOverview() {
  try {
    overview = await api("/api/system/overview");
    renderPills();
    renderStats();
    renderCourses();
    const btnLogin = $("btn-login");
    btnLogin.textContent = overview.account.logged_in ? "重新登录" : "扫码登录";
    btnLogin.classList.toggle("primary", !overview.account.logged_in);
  } catch (e) {
    console.error(e);
  }
}

setInterval(pollOverview, 3000);
setInterval(loadLogs, 4000);

/* ---------------- 登录 ---------------- */

$("btn-login").addEventListener("click", () => openLogin());

function openLogin() {
  $("modal-login").hidden = false;
  switchLoginTab("qr");
  startQr();
}

function switchLoginTab(tab) {
  $("tab-qr").classList.toggle("active", tab === "qr");
  $("tab-cookie").classList.toggle("active", tab === "cookie");
  $("pane-qr").hidden = tab !== "qr";
  $("pane-cookie").hidden = tab !== "cookie";
  if (tab !== "qr" && qrTimer) { clearInterval(qrTimer); qrTimer = null; }
}

$("tab-qr").addEventListener("click", () => { switchLoginTab("qr"); startQr(); });
$("tab-cookie").addEventListener("click", () => switchLoginTab("cookie"));

let qrRetry = 0;

async function startQr() {
  if (qrTimer) { clearInterval(qrTimer); qrTimer = null; }
  $("qr-status").textContent = "正在获取二维码…";
  $("qr-status").className = "qr-status";
  try {
    const res = await api("/api/auth/qr", "POST");
    qrKey = res.key;
    qrRetry = 0;
    $("qr-img").src = res.qr_image;
    $("qr-status").textContent = "等待扫码（二维码约 5 分钟有效）";
    qrTimer = setInterval(pollQr, 2200);
  } catch (e) {
    $("qr-status").textContent = `获取二维码失败：${e.message}`;
    $("qr-status").className = "qr-status bad";
    if (qrRetry < 3) {
      qrRetry += 1;
      setTimeout(() => { if (!$("modal-login").hidden) startQr(); }, 5000);
    }
  }
}

async function pollQr() {
  if (!qrKey) return;
  try {
    const res = await api(`/api/auth/qr/${qrKey}`);
    const el = $("qr-status");
    if (res.status === "confirmed") {
      clearInterval(qrTimer); qrTimer = null;
      el.textContent = "登录成功！";
      el.className = "qr-status ok";
      toast("登录成功", "success");
      setTimeout(() => { $("modal-login").hidden = true; }, 800);
      pollOverview();
    } else if (res.status === "expired" || res.status === "error") {
      el.textContent = res.message + "，正在刷新…";
      el.className = "qr-status bad";
      clearInterval(qrTimer); qrTimer = null;
      setTimeout(startQr, 3000);
    } else {
      el.textContent = res.message;
      el.className = "qr-status";
    }
  } catch (e) {
    $("qr-status").textContent = `状态查询失败：${e.message}`;
  }
}

$("btn-import-cookie").addEventListener("click", async () => {
  const raw = $("cookie-input").value.trim();
  if (!raw) { toast("请先粘贴 Cookie", "warn"); return; }
  $("btn-import-cookie").disabled = true;
  try {
    const res = await api("/api/auth/cookie", "POST", { cookie: raw });
    toast(`导入成功：${res.user?.display_name || res.user?.user_id || ""}`, "success");
    $("modal-login").hidden = true;
    pollOverview();
  } catch (e) {
    toast(`导入失败：${e.message}`, "error");
  } finally {
    $("btn-import-cookie").disabled = false;
  }
});

/* ---------------- 设置 ---------------- */

const SETTING_FIELDS = [
  ["set-speed", "engine.speed", "float"],
  ["set-parallel", "engine.max_parallel_videos", "int"],
  ["set-tick", "engine.heartbeat_tick", "float"],
  ["set-flush", "engine.flush_interval", "float"],
  ["set-check", "engine.progress_check_interval", "float"],
  ["set-retry", "engine.retry", "int"],
  ["set-autostart", "engine.auto_start", "bool"],
  ["set-dryrun", "engine.dry_run", "bool"],
  ["set-auto-select", "engine.auto_select_network_courses", "bool"],
  ["set-auto-refresh", "engine.auto_refresh_minutes", "int"],
  ["set-report-interval", "engine.report_interval_minutes", "int"],
  ["set-notify-enabled", "notify.enabled", "bool"],
  ["set-feishu-webhook", "notify.feishu.webhook", "str"],
  ["set-feishu-secret", "notify.feishu.secret", "str"],
  ["set-smtp-host", "notify.email.smtp_host", "str"],
  ["set-smtp-port", "notify.email.smtp_port", "int"],
  ["set-smtp-user", "notify.email.username", "str"],
  ["set-smtp-pass", "notify.email.password", "str"],
  ["ev-login", "notify.events.login", "bool"],
  ["ev-login-expired", "notify.events.login_expired", "bool"],
  ["ev-hourly", "notify.events.hourly", "bool"],
  ["ev-course", "notify.events.course_completed", "bool"],
  ["ev-all", "notify.events.all_completed", "bool"],
  ["ev-video", "notify.events.video_completed", "bool"],
  ["ev-anomaly", "notify.events.anomaly", "bool"],
  ["ev-errors", "notify.events.errors", "bool"],
];

$("btn-settings").addEventListener("click", async () => {
  try {
    const snap = await api("/api/system/settings");
    SETTING_FIELDS.forEach(([elId, key, type]) => {
      const el = $(elId);
      const v = snap[key];
      if (type === "bool") el.checked = Boolean(v);
      else el.value = v ?? "";
    });
    $("set-mail-to").value = (snap["notify.email.to"] || []).join(", ");
    $("modal-settings").hidden = false;
  } catch (e) { toast(e.message, "error"); }
});

$("btn-save-settings").addEventListener("click", async () => {
  const values = {};
  SETTING_FIELDS.forEach(([elId, key, type]) => {
    const el = $(elId);
    if (type === "bool") values[key] = el.checked;
    else if (type === "int") values[key] = parseInt(el.value || "0", 10);
    else if (type === "float") values[key] = parseFloat(el.value || "0");
    else values[key] = el.value.trim();
  });
  values["notify.email.to"] = $("set-mail-to").value.trim();
  try {
    await api("/api/system/settings", "POST", { values });
    toast("设置已保存并生效", "success");
    $("modal-settings").hidden = true;
    pollOverview();
  } catch (e) { toast(e.message, "error"); }
});

$("btn-notify-test").addEventListener("click", async () => {
  const CH = { feishu: "飞书", email: "邮件" };
  try {
    const res = await api("/api/system/notify/test", "POST");
    const results = res.results || {};
    const entries = Object.entries(results);
    if (!entries.length) {
      toast("没有可用的通知渠道：请先填写飞书 Webhook 或邮箱配置后保存", "warn", 7000);
      return;
    }
    const okList = entries.filter(([, v]) => v && v.ok).map(([k]) => CH[k] || k);
    const failList = entries.filter(([, v]) => v && !v.ok).map(([k, v]) => `${CH[k] || k}：${v.error}`);
    if (okList.length) toast(`测试通知已发送：${okList.join(" / ")}`, "success");
    failList.forEach((f) => toast(`发送失败 → ${f}`, "error", 9000));
  } catch (e) { toast(e.message, "error"); }
});

/* ---------------- 引擎控制 ---------------- */

$("btn-start").addEventListener("click", () =>
  withBusy($("btn-start"), "启动中…", async () => {
    try {
      const res = await api("/api/engine/start", "POST");
      const n = (res.started_courses || []).length;
      if (n) toast(`已开始 ${n} 门课程，几秒后可在「实时动态」看到心跳上报`, "success", 6000);
      else toast("没有勾选任何课程：请点「一键勾选网络课」或手动勾选「参与刷课」", "warn", 8000);
      pollOverview();
      loadLive();
    } catch (e) { toast(e.message, "error"); }
  })
);

$("btn-stop").addEventListener("click", () =>
  withBusy($("btn-stop"), "暂停中…", async () => {
    try {
      await api("/api/engine/stop", "POST");
      toast("已全部暂停（进度已保存）", "success");
      pollOverview();
      loadLive();
    } catch (e) { toast(e.message, "error"); }
  })
);

$("btn-sync").addEventListener("click", () =>
  withBusy($("btn-sync"), "同步中…", async () => {
    try {
      await api("/api/courses/sync", "POST");
      toast("课程同步已启动（完成后会自动勾选网络课程）", "success", 6000);
      pollOverview();
    } catch (e) { toast(e.message, "error"); }
  })
);

$("btn-autoselect").addEventListener("click", () =>
  withBusy($("btn-autoselect"), "处理中…", async () => {
    try {
      const res = await api("/api/courses/auto-select", "POST");
      toast(`已勾选 ${res.enabled} 门网络课程，排除 ${res.excluded} 门其他课程`, "success", 6000);
      pollOverview();
    } catch (e) { toast(e.message, "error"); }
  })
);

$("btn-refresh").addEventListener("click", () =>
  withBusy($("btn-refresh"), "刷新中…", async () => {
    try {
      await api("/api/courses/refresh-all", "POST");
      toast("已开始刷新平台进度（含补充未知时长）", "success");
      pollOverview();
    } catch (e) { toast(e.message, "error"); }
  })
);

$("btn-report").addEventListener("click", () =>
  withBusy($("btn-report"), "发送中…", async () => {
    try {
      await api("/api/system/report", "POST");
      toast("进度汇报已发送（飞书/邮件）", "success");
    } catch (e) { toast(e.message, "error"); }
  })
);

/* ---------------- 课程详情 ---------------- */

let detailTimer = null;

async function openCourseDetail(id) {
  detailCourseId = id;
  $("modal-course").hidden = false;
  $("course-leaves").innerHTML = `<div class="log-empty">加载中…</div>`;
  await loadCourseDetail();
  if (detailTimer) clearInterval(detailTimer);
  detailTimer = setInterval(loadCourseDetail, 5000);
}

async function loadCourseDetail() {
  if (!detailCourseId) return;
  try {
    const res = await api(`/api/courses/${detailCourseId}/leaves`);
    $("course-detail-title").textContent = res.course.name;
    $("course-detail-sub").textContent = `${res.course.teacher || ""} · 状态：${STATUS_TEXT[res.course.status] || res.course.status} · ${res.course.enabled ? "已参与自动刷课" : "未参与自动刷课"}`;
    const leaves = res.leaves || [];
    if (!leaves.length) {
      $("course-leaves").innerHTML = `<div class="log-empty">该课程暂无章节数据，请先同步课程</div>`;
      return;
    }
    $("course-leaves").innerHTML = leaves.map((l) => {
      const pct = Math.round((l.progress || 0) * 100);
      const kindLabel = l.kind === "video" ? "视频" : l.kind === "other" ? (l.kind_hint || "非视频") : "待识别";
      const dur = l.duration ? fmtClock(l.duration) : "时长未知";
      const manual = l.needs_manual ? ` · <span class="leaf-manual">需手动完成（点右侧「去完成」）</span>` : "";
      const err = l.error_msg && !l.needs_manual ? ` · <span style="color:#fdd2d2">${esc(l.error_msg)}</span>` : "";
      const actions = (l.status === "failed" || l.status === "skipped")
        ? `<button class="small" data-leafs="${l.id}" data-laction="retry">重试</button>`
        : l.status === "done"
          ? ""
          : `<button class="small" data-leafs="${l.id}" data-laction="skip">跳过</button>
             <button class="small" data-leafs="${l.id}" data-laction="reset">重置</button>`;
      const link = `<a class="link-btn" href="${esc(l.platform_url || "#")}" target="_blank" rel="noopener" title="到雨课堂网页版打开">${l.needs_manual ? "去完成 ↗" : "平台 ↗"}</a>`;
      return `
      <div class="leaf-row">
        <span class="leaf-status ${l.status}">${LEAF_STATUS_TEXT[l.status] || l.status}</span>
        <div class="leaf-main">
          <div class="leaf-title">${esc(l.title)}</div>
          <div class="leaf-sub">${kindLabel} · ${dur} · 已看 ${fmtClock(l.watched_seconds)}${manual}${err}</div>
        </div>
        <div class="leaf-bar">
          <div class="progress-bar ${l.status === "done" ? "done" : ""}"><i style="width:${pct}%"></i></div>
        </div>
        <div class="leaf-actions">${actions}${link}</div>
      </div>`;
    }).join("");
  } catch (e) {
    $("course-leaves").innerHTML = `<div class="log-empty">加载失败：${esc(e.message)}</div>`;
  }
}

$("course-leaves").addEventListener("click", async (ev) => {
  const btn = ev.target.closest("button[data-laction]");
  if (!btn) return;
  try {
    await api(`/api/leaves/${btn.dataset.leafs}/action`, "POST", { action: btn.dataset.laction });
    toast("操作已提交", "success");
    loadCourseDetail();
    pollOverview();
  } catch (e) { toast(e.message, "error"); }
});

/* ---------------- 弹窗通用 ---------------- */

document.querySelectorAll(".modal-mask").forEach((mask) => {
  mask.addEventListener("click", (ev) => { if (ev.target === mask) closeModal(mask.id); });
});
document.querySelectorAll("[data-close]").forEach((btn) => {
  btn.addEventListener("click", () => closeModal(btn.dataset.close));
});
document.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape") document.querySelectorAll(".modal-mask:not([hidden])").forEach((m) => closeModal(m.id));
});

function closeModal(id) {
  $(id).hidden = true;
  if (id === "modal-login" && qrTimer) { clearInterval(qrTimer); qrTimer = null; }
  if (id === "modal-course" && detailTimer) { clearInterval(detailTimer); detailTimer = null; detailCourseId = null; }
}

/* ---------------- 启动 ---------------- */

function applyHash() {
  const h = (location.hash || "").replace(/^#\/?/, "");
  if (h === "login") openLogin();
  else if (h === "settings") $("btn-settings").click();
}
window.addEventListener("hashchange", applyHash);

pollOverview();
loadLogs();
loadLive();
applyHash();
setInterval(loadLive, 1500);
