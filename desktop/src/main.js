/**
 * 雨课堂网课助手 · 桌面客户端（Electron 主进程）
 *
 * 两种模式：
 *   remote —— 连接自建服务（云服务器 / 本机已启动的服务），只需地址 + 访问口令
 *   local  —— 由本桌面端拉起本机服务（需要本机安装 uv 且有项目代码）
 */
const { app, BrowserWindow, Tray, Menu, ipcMain, shell, dialog, nativeImage, Notification } = require("electron");
const path = require("node:path");
const fs = require("node:fs");
const { spawn } = require("node:child_process");

const DEFAULT_CONFIG = {
  mode: "remote",
  url: "",
  token: "",
  projectDir: "",
  localPort: 8765,
};

const state = {
  config: { ...DEFAULT_CONFIG },
  win: null,
  tray: null,
  localProc: null,
  localLogs: [],
  status: null,
  timer: null,
};

const configPath = () => path.join(app.getPath("userData"), "config.json");
const iconPath = (name) => path.join(__dirname, "..", "build", name);

function loadConfig() {
  try {
    const raw = JSON.parse(fs.readFileSync(configPath(), "utf8"));
    state.config = { ...DEFAULT_CONFIG, ...raw };
  } catch {
    /* 首次运行 */
  }
}

function saveConfig() {
  try {
    fs.mkdirSync(path.dirname(configPath()), { recursive: true });
    fs.writeFileSync(configPath(), JSON.stringify(state.config, null, 2), "utf8");
  } catch (e) {
    console.error("保存配置失败", e);
  }
}

function baseUrl() {
  return (state.config.url || "").replace(/\/+$/, "");
}

function dashboardUrl() {
  const token = state.config.token ? `?token=${encodeURIComponent(state.config.token)}` : "";
  return `${baseUrl()}/${token}`;
}

async function api(pathname, options = {}) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (state.config.token) headers["X-Access-Token"] = state.config.token;
  const res = await fetch(`${baseUrl()}${pathname}`, { ...options, headers });
  if (res.status === 401) throw new Error("访问口令不正确");
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

// ---------------------------------------------------------------------
// 窗口
// ---------------------------------------------------------------------

function createWindow() {
  state.win = new BrowserWindow({
    width: 1280,
    height: 880,
    minWidth: 900,
    minHeight: 600,
    title: "雨课堂网课助手",
    backgroundColor: "#0f1117",
    icon: iconPath("icon.png"),
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  state.win.on("closed", () => {
    state.win = null;
  });

  // 外部链接用系统浏览器打开
  state.win.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: "deny" };
  });

  if (state.config.url) {
    state.win.loadURL(dashboardUrl());
  } else {
    state.win.loadFile(path.join(__dirname, "renderer", "setup.html"));
  }
  return state.win;
}

function showSetup() {
  if (!state.win) createWindow();
  state.win.show();
  state.win.loadFile(path.join(__dirname, "renderer", "setup.html"));
}

function showDashboard() {
  if (!state.win) createWindow();
  state.win.show();
  state.win.loadURL(dashboardUrl());
}

// ---------------------------------------------------------------------
// 本机服务托管（local 模式）
// ---------------------------------------------------------------------

function startLocalService() {
  if (state.localProc) return;
  const dir = state.config.projectDir;
  if (!dir || !fs.existsSync(path.join(dir, "run.py"))) {
    pushLog("[错误] 项目目录无效：%s（应包含 run.py）", dir);
    return;
  }
  const port = state.config.localPort || 8765;
  pushLog("[启动] uv run python run.py（端口 %d，目录 %s）", port, dir);
  state.localProc = spawn("uv", ["run", "--directory", dir, "python", "run.py"], {
    cwd: dir,
    env: { ...process.env, YKT_PORT: String(port) },
    stdio: ["ignore", "pipe", "pipe"],
  });
  const onData = (buf) => {
    const text = buf.toString();
    text.split("\n").filter(Boolean).forEach((line) => pushLog("%s", line));
  };
  state.localProc.stdout.on("data", onData);
  state.localProc.stderr.on("data", onData);
  state.localProc.on("exit", (code) => {
    pushLog("[退出] 本机服务已停止（code=%s）", code);
    state.localProc = null;
  });
  state.config.url = `http://127.0.0.1:${port}`;
  saveConfig();
}

function stopLocalService() {
  if (state.localProc) {
    state.localProc.kill();
    state.localProc = null;
    pushLog("[停止] 已结束本机服务进程");
  }
}

function pushLog(template, ...args) {
  const line = args.length ? template.replace(/%s|%d/g, () => String(args.shift())) : template;
  state.localLogs.push(`${new Date().toLocaleTimeString()} ${line}`);
  if (state.localLogs.length > 400) state.localLogs.shift();
  if (state.win && !state.win.isDestroyed()) {
    state.win.webContents.send("local:log", line);
  }
}

// ---------------------------------------------------------------------
// 托盘
// ---------------------------------------------------------------------

function trayImage() {
  const img = nativeImage.createFromPath(iconPath("tray.png"));
  if (!img.isEmpty()) {
    const resized = img.resize({ width: 18, height: 18 });
    resized.setTemplateImage(true);
    return resized;
  }
  return nativeImage.createEmpty();
}

function buildTrayMenu() {
  const s = state.status;
  const stats = s?.stats || {};
  const engine = s?.engine || {};
  const items = [
    { label: stats.videos ? `进度：${stats.done_videos}/${stats.videos} 个视频` : "尚未同步课程", enabled: false },
    {
      label: s ? `引擎：${engine.running ? "运行中" : "已暂停"}（${engine.active_videos || 0} 路并行）` : "未连接服务",
      enabled: false,
    },
    { type: "separator" },
    { label: "打开看板", click: showDashboard },
    { label: "开始刷课", enabled: Boolean(s), click: () => callApi("/api/engine/start") },
    { label: "暂停刷课", enabled: Boolean(s), click: () => callApi("/api/engine/stop") },
    { label: "立即汇报进度", enabled: Boolean(s), click: () => callApi("/api/system/report") },
    { type: "separator" },
    { label: "重新设置…", click: showSetup },
    { label: "退出", click: () => app.quit() },
  ];
  return Menu.buildFromTemplate(items);
}

async function callApi(pathname, options) {
  try {
    await api(pathname, { method: "POST", ...(options || {}) });
    notify("操作已提交", pathname);
    refreshStatus();
  } catch (e) {
    notify("操作失败", String(e.message || e));
  }
}

function notify(title, body) {
  if (Notification.isSupported()) new Notification({ title, body }).show();
}

async function refreshStatus() {
  if (!state.config.url) {
    state.status = null;
  } else {
    try {
      state.status = await api("/api/system/overview");
    } catch {
      state.status = null;
    }
  }
  if (state.tray) {
    const s = state.status;
    state.tray.setToolTip(
      s
        ? `雨课堂网课助手 · ${s.stats.done_videos}/${s.stats.videos} 已完成 · ${s.engine.running ? "运行中" : "已暂停"}`
        : "雨课堂网课助手 · 未连接"
    );
    state.tray.setContextMenu(buildTrayMenu());
  }
}

function createTray() {
  state.tray = new Tray(trayImage());
  state.tray.setTitle("");
  state.tray.setContextMenu(buildTrayMenu());
  state.tray.on("click", () => {
    if (state.win) {
      state.win.isVisible() ? state.win.hide() : (state.win.show(), state.win.focus());
    } else {
      createWindow();
    }
  });
  refreshStatus();
  state.timer = setInterval(refreshStatus, 30000);
}

// ---------------------------------------------------------------------
// IPC
// ---------------------------------------------------------------------

function registerIpc() {
  ipcMain.handle("config:get", () => state.config);

  ipcMain.handle("config:save", (_e, patch) => {
    Object.assign(state.config, patch);
    if (state.config.mode === "local" && state.config.projectDir) {
      startLocalService();
      state.config.url = `http://127.0.0.1:${state.config.localPort || 8765}`;
    }
    saveConfig();
    showDashboard();
    return state.config;
  });

  ipcMain.handle("conn:test", async (_e, { url, token }) => {
    try {
      const res = await fetch(`${String(url).replace(/\/+$/, "")}/api/system/overview`, {
        headers: token ? { "X-Access-Token": token } : {},
      });
      if (res.status === 401) return { ok: false, error: "访问口令不正确（未设置口令时请留空）" };
      if (!res.ok) return { ok: false, error: `服务返回 HTTP ${res.status}` };
      const data = await res.json();
      const user = data.account?.user || {};
      return {
        ok: true,
        summary:
          `已连接：${data.account?.logged_in ? "已登录" : "未登录"}` +
          (user.display_name ? `（${user.display_name}）` : "") +
          ` · 课程 ${data.stats?.courses ?? 0} 门 · 视频 ${data.stats?.done_videos ?? 0}/${data.stats?.videos ?? 0}` +
          ` · 引擎${data.engine?.running ? "运行中" : "已暂停"}`,
      };
    } catch (e) {
      return { ok: false, error: `无法连接：${e.message}` };
    }
  });

  ipcMain.handle("dir:pick", async () => {
    const res = await dialog.showOpenDialog({
      title: "选择 ykt-helper 项目目录（应包含 run.py）",
      properties: ["openDirectory"],
    });
    return res.canceled ? null : res.filePaths[0];
  });

  ipcMain.handle("open:external", (_e, url) => shell.openExternal(url));

  ipcMain.handle("local:state", () => ({
    running: Boolean(state.localProc),
    logs: state.localLogs.slice(-120),
  }));

  ipcMain.handle("local:start", () => {
    startLocalService();
    return { running: Boolean(state.localProc) };
  });

  ipcMain.handle("local:stop", () => {
    stopLocalService();
    return { running: false };
  });

  ipcMain.handle("engine:call", async (_e, { path: pathname, method }) => {
    try {
      const data = await api(pathname, { method: method || "POST" });
      refreshStatus();
      return { ok: true, data };
    } catch (e) {
      return { ok: false, error: String(e.message || e) };
    }
  });

  ipcMain.handle("app:version", () => ({
    app: app.getVersion(),
    electron: process.versions.electron,
    configPath: configPath(),
  }));
}

// ---------------------------------------------------------------------
// 生命周期
// ---------------------------------------------------------------------

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (state.win) {
      state.win.show();
      state.win.focus();
    }
  });

  app.whenReady().then(() => {
    loadConfig();
    registerIpc();
    if (state.config.mode === "local" && state.config.projectDir) startLocalService();
    createWindow();
    createTray();

    app.on("activate", () => {
      if (BrowserWindow.getAllWindows().length === 0) createWindow();
    });
  });

  app.on("window-all-closed", () => {
    // 保留在托盘里运行（macOS 习惯）
    if (process.platform !== "darwin") app.quit();
  });

  app.on("before-quit", () => {
    stopLocalService();
    if (state.timer) clearInterval(state.timer);
  });
}
