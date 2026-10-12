const $ = (id) => document.getElementById(id);

function setMsg(el, text, type = "") {
  el.textContent = text;
  el.className = `msg ${type}`;
}

function switchTab(tab) {
  for (const t of ["embedded", "remote", "dev"]) {
    const btn = $(`tab-${t}`);
    if (btn) btn.classList.toggle("active", t === tab);
    const pane = $(`pane-${t}`);
    if (pane) pane.hidden = t !== tab;
  }
}

$("tab-embedded").addEventListener("click", () => switchTab("embedded"));
$("tab-remote").addEventListener("click", () => switchTab("remote"));

async function init() {
  const cfg = await window.ykt.getConfig();
  const v = await window.ykt.version();

  $("url").value = cfg.url && cfg.mode === "remote" ? cfg.url : "";
  $("token").value = cfg.mode === "remote" ? cfg.token || "" : "";
  $("project-dir").value = cfg.projectDir || "";
  $("port").value = cfg.localPort || 8765;
  $("data-dir").value = v.dataDir || "";
  $("ver").textContent = `v${v.app} · Electron ${v.electron}`;

  if (!v.hasBundledServer) {
    const badge = $("badge-bundle");
    badge.textContent = "未内置（开发版）";
    badge.style.background = "#3a2f16";
    badge.style.color = "#e2c268";
    badge.style.borderColor = "#6b551f";
  }

  if (cfg.mode === "remote") switchTab("remote");
  else if (cfg.mode === "localdev") switchTab("dev");

  const st = await window.ykt.localState();
  if (st.logs && st.logs.length) {
    $("logs").textContent = st.logs.join("\n");
  }
}

window.ykt.onBootState((s) => {
  if (s && s.phase === "error" && s.text) {
    $("banner").textContent = s.text;
    $("banner").classList.add("show");
  }
});

window.ykt.onLocalLog((line) => {
  const logs = $("logs");
  if (!logs) return;
  logs.textContent += (logs.textContent ? "\n" : "") + line;
  logs.scrollTop = logs.scrollHeight;
});

$("btn-save-embedded").addEventListener("click", async () => {
  const port = Number($("port").value) || 8765;
  setMsg($("msg-embedded"), "正在启动内置服务…");
  await window.ykt.saveConfig({ mode: "embedded", localPort: port });
});

$("btn-open-data").addEventListener("click", async () => {
  const v = await window.ykt.version();
  window.ykt.openExternal(`file://${v.dataDir}`);
});

$("btn-test").addEventListener("click", async () => {
  setMsg($("msg-remote"), "正在测试…");
  const res = await window.ykt.testConnection({ url: $("url").value.trim(), token: $("token").value });
  setMsg($("msg-remote"), res.ok ? `✅ ${res.summary}` : `❌ ${res.error}`, res.ok ? "ok" : "bad");
});

$("btn-save-remote").addEventListener("click", async () => {
  const url = $("url").value.trim();
  if (!url) {
    setMsg($("msg-remote"), "请填写服务地址", "bad");
    return;
  }
  await window.ykt.saveConfig({ mode: "remote", url, token: $("token").value });
});

$("btn-pick").addEventListener("click", async () => {
  const dir = await window.ykt.pickDir();
  if (dir) $("project-dir").value = dir;
});

$("btn-save-dev").addEventListener("click", async () => {
  const dir = $("project-dir").value.trim();
  if (!dir) {
    setMsg($("msg-dev"), "请选择项目目录", "bad");
    return;
  }
  setMsg($("msg-dev"), "正在用 uv 启动…");
  await window.ykt.saveConfig({ mode: "localdev", projectDir: dir, localPort: Number($("port").value) || 8765 });
});

$("docs-link").addEventListener("click", (e) => {
  e.preventDefault();
  window.ykt.openExternal("https://github.com/JackWPP/buct-ol-helper#readme");
});

$("dev-toggle").addEventListener("click", () => switchTab("dev"));

$("cli-install").addEventListener("click", async () => {
  const res = await window.ykt.cliInstall();
  window.alert(res.ok ? `✅ 已安装，可在终端直接使用 ykt 命令\n\n${res.path}\n\n${res.hint}` : `❌ 安装失败：${res.error}`);
});

init();
