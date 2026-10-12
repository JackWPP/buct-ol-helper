const $ = (id) => document.getElementById(id);

function setMsg(el, text, type = "") {
  el.textContent = text;
  el.className = `msg ${type}`;
}

function switchTab(tab) {
  $("tab-remote").classList.toggle("active", tab === "remote");
  $("tab-local").classList.toggle("active", tab === "local");
  $("pane-remote").hidden = tab !== "remote";
  $("pane-local").hidden = tab !== "local";
}

$("tab-remote").addEventListener("click", () => switchTab("remote"));
$("tab-local").addEventListener("click", () => switchTab("local"));

async function init() {
  const cfg = await window.ykt.getConfig();
  $("url").value = cfg.url || "";
  $("token").value = cfg.token || "";
  $("project-dir").value = cfg.projectDir || "";
  $("port").value = cfg.localPort || 8765;
  if (cfg.mode === "local") switchTab("local");

  const v = await window.ykt.version();
  $("ver").textContent = `v${v.app} · Electron ${v.electron}`;
  $("docs-link").addEventListener("click", (e) => {
    e.preventDefault();
    window.ykt.openExternal("https://github.com/JackWPP/buct-ol-helper#readme");
  });
  const st = await window.ykt.localState();
  $("logs").textContent = st.logs.join("\n");
}

window.ykt.onLocalLog((line) => {
  const logs = $("logs");
  logs.textContent += (logs.textContent ? "\n" : "") + line;
  logs.scrollTop = logs.scrollHeight;
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

$("btn-local-start").addEventListener("click", async () => {
  setMsg($("msg-local"), "正在启动本机服务…");
  await window.ykt.saveConfig({ mode: "local", projectDir: $("project-dir").value.trim(), localPort: Number($("port").value) || 8765 });
  const st = await window.ykt.localStart();
  setMsg($("msg-local"), st.running ? "服务启动中，等日志出现 Uvicorn running 后再进入看板" : "启动失败，请检查项目目录与 uv", st.running ? "ok" : "bad");
});

$("btn-local-stop").addEventListener("click", async () => {
  await window.ykt.localStop();
  setMsg($("msg-local"), "已停止本机服务");
});

$("btn-save-local").addEventListener("click", async () => {
  const dir = $("project-dir").value.trim();
  if (!dir) {
    setMsg($("msg-local"), "请选择项目目录", "bad");
    return;
  }
  await window.ykt.saveConfig({ mode: "local", projectDir: dir, localPort: Number($("port").value) || 8765 });
});

init();
