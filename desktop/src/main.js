/**
 * 雨课堂网课助手 · 桌面客户端（Electron 主进程）
 *
 * 三种运行模式：
 *   embedded（默认）—— App 内置服务：自带 Python 运行时、依赖与 SQLite，双击即用
 *   remote          —— 连接自建服务（云服务器 / 内网），只需地址 + 访问口令
 *   localdev        —— 开发模式：用 uv 拉起本机项目源码（需安装 uv）
 */
const { app, BrowserWindow, Tray, Menu, ipcMain, shell, dialog, nativeImage, Notification } = require("electron");
const path = require("node:path");
const fs = require("node:fs");
const { spawn } = require("node:child_process");

const DEFAULT_CONFIG = {
  mode: "embedded",
  url: "",
  token: "",
  projectDir: "",
  localPort: 8765,
  embeddedPort: 0,
};

const state = {
  config: { ...DEFAULT_CONFIG },
  win: null,
  tray: null,
  proc: null,
  procExit: null,
  announcedPort: 0,
  starting: null,
  logs: [],
  status: null,
  timer: null,
};

const configPath = () => path.join(app.getPath("userData"), "config.json");
const iconPath = (name) => path.join(__dirname, "..", "build", name);
const dataDir = () => path.join(app.getPath("userData"), "data");

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
// 内置服务（embedded 模式）
// ---------------------------------------------------------------------

/** 内置服务可执行文件：打包后在 Resources/server，开发时在 desktop/resources。 */
function bundledServerBinary() {
  const candidates = [];
  if (process.resourcesPath) candidates.push(path.join(process.resourcesPath, "server", "ykt-server"));
  candidates.push(path.join(__dirname, "..", "resources", "ykt-server", "ykt-server"));
  for (const c of candidates) {
    try {
      if (c && fs.existsSync(c)) return c;
    } catch {
      /* ignore */
    }
  }
  return null;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function probe(url, token, timeout = 2500) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeout);
  try {
    return await fetch(`${url}/api/system/overview`, {
      headers: token ? { "X-Access-Token": token } : {},
      signal: ctrl.signal,
    });
  } finally {
    clearTimeout(timer);
  }
}

/** 等待服务就绪；进程中途退出则立即失败。 */
async function waitReady(url, token, timeoutMs = 90000) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    if (state.procExit !== null) throw new Error(`内置服务已退出（code=${state.procExit}）`);
    try {
      const res = await probe(url, token);
      if (res.ok) {
        // 校验响应特征，避免误连到恰好占用该端口的其它服务
        const data = await res.json().catch(() => null);
        if (data && data.engine && data.account) return true;
      }
    } catch {
      /* 还没起来 */
    }
    await new Promise((r) => setTimeout(r, 600));
  }
  return false;
}

async function startEmbeddedServer() {
  if (state.starting) return state.starting;
  state.starting = (async () => {
    if (state.proc) return { ok: true, port: state.config.embeddedPort };
    const bin = bundledServerBinary();
    if (!bin) return { ok: false, error: "未找到内置服务程序（安装包不完整，请重新下载）" };
    const preferred = Number(state.config.localPort) || 8765;
    fs.mkdirSync(dataDir(), { recursive: true });
    state.procExit = null;
    state.announcedPort = 0;
    pushLog("[启动] 内置服务（首选端口 %d，数据目录 %s）", preferred, dataDir());
    state.proc = spawn(bin, ["--serve"], {
      cwd: app.getPath("userData"),
      env: {
        ...process.env,
        YKT_HOST: "127.0.0.1",
        YKT_PORT: String(preferred),
        YKT_DATA_DIR: dataDir(),
        YKT_ACCESS_TOKEN: "",
        YKT_PARENT_PID: String(process.pid),
      },
      // stdin 用管道：本进程退出时管道关闭，子进程读到 EOF 会自行退出（防残留）
      stdio: ["pipe", "pipe", "pipe"],
    });
    const onData = (buf) => {
      buf
        .toString()
        .split("\n")
        .filter(Boolean)
        .forEach((line) => {
          // 服务端绑定成功后回报真实端口（首选端口被占用时它会自动改用系统分配的空闲端口）
          const m = line.match(/\[ykt\] LISTEN (\d+)/);
          if (m) state.announcedPort = Number(m[1]);
          pushLog("%s", line);
        });
    };
    state.proc.stdout.on("data", onData);
    state.proc.stderr.on("data", onData);
    state.proc.on("exit", (code) => {
      pushLog("[退出] 内置服务已停止（code=%s）", code);
      state.proc = null;
      state.procExit = code;
      refreshStatus();
    });

    // 等待端口回报（最多 10 秒），期间子进程若退出则立即失败
    const deadline = Date.now() + 10000;
    while (!state.announcedPort && state.procExit === null && Date.now() < deadline) {
      await sleep(200);
    }
    if (state.procExit !== null) {
      return { ok: false, error: `内置服务启动失败（退出码 ${state.procExit}），请查看启动日志` };
    }
    const port = state.announcedPort || preferred;

    state.config.url = `http://127.0.0.1:${port}`;
    state.config.token = "";
    state.config.embeddedPort = port;
    saveConfig();
    if (port !== preferred) {
      pushLog("[端口] %d 已被占用，已自动改用 %d", preferred, port);
    }

    const ready = await waitReady(state.config.url, "", 60000);
    if (!ready) return { ok: false, error: "内置服务启动超时，请查看启动日志" };
    pushLog("[就绪] 看板地址：%s", state.config.url);
    return { ok: true, port };
  })();
  try {
    return await state.starting;
  } finally {
    state.starting = null;
  }
}

/** 开发模式：用 uv 拉起本机项目源码 */
function startDevService() {
  if (state.proc) return { ok: true, port: state.config.embeddedPort };
  const dir = state.config.projectDir;
  if (!dir || !fs.existsSync(path.join(dir, "run.py"))) {
    pushLog("[错误] 项目目录无效：%s（应包含 run.py）", dir);
    return { ok: false, error: "项目目录无效，应包含 run.py" };
  }
  const port = Number(state.config.localPort) || 8765;
  pushLog("[启动] uv run python run.py（端口 %d）", port);
  state.proc = spawn("uv", ["run", "--directory", dir, "python", "run.py"], {
    cwd: dir,
    env: {
      ...process.env,
      YKT_HOST: "127.0.0.1",
      YKT_PORT: String(port),
      YKT_DATA_DIR: dataDir(),
      YKT_PARENT_PID: String(process.pid),
    },
    stdio: ["ignore", "pipe", "pipe"],
  });
  const onData = (buf) => {
    buf
      .toString()
      .split("\n")
      .filter(Boolean)
      .forEach((line) => pushLog("%s", line));
  };
  state.proc.stdout.on("data", onData);
  state.proc.stderr.on("data", onData);
  state.proc.on("exit", (code) => {
    pushLog("[退出] 本机服务已停止（code=%s）", code);
    state.proc = null;
  });
  state.config.url = `http://127.0.0.1:${port}`;
  state.config.embeddedPort = port;
  saveConfig();
  return { ok: true, port };
}

function stopService() {
  if (state.proc) {
    pushLog("[停止] 正在结束服务进程…");
    try {
      state.proc.kill("SIGTERM");
      const proc = state.proc;
      setTimeout(() => {
        try {
          if (proc && !proc.killed) proc.kill("SIGKILL");
        } catch {
          /* ignore */
        }
      }, 4000);
    } catch (e) {
      pushLog("[警告] 结束进程失败：%s", e.message);
    }
    state.proc = null;
  }
}

function pushLog(template, ...args) {
  const line = args.length ? template.replace(/%s|%d/g, () => String(args.shift())) : template;
  state.logs.push(`${new Date().toLocaleTimeString()} ${line}`);
  if (state.logs.length > 400) state.logs.shift();
  if (state.win && !state.win.isDestroyed()) {
    state.win.webContents.send("local:log", line);
  }
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

  state.win.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: "deny" };
  });
  return state.win;
}

function loadPage(file, payload) {
  if (!state.win) createWindow();
  state.win.show();
  state.win.loadFile(path.join(__dirname, "renderer", file));
  if (payload) {
    state.win.webContents.once("did-finish-load", () => {
      state.win.webContents.send("boot:state", payload);
    });
  }
}

function showLoading(text = "正在启动内置服务…") {
  loadPage("loading.html", { phase: "loading", text });
}

function showDashboard() {
  if (!state.win) createWindow();
  state.win.show();
  state.win.loadURL(dashboardUrl());
}

function showSetup(error = "") {
  loadPage("setup.html", { phase: error ? "error" : "setup", text: error });
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

function modeLabel() {
  if (state.config.mode === "embedded") return `内置服务（端口 ${state.config.embeddedPort || state.config.localPort}）`;
  if (state.config.mode === "localdev") return "开发模式（本机源码）";
  return state.config.url ? `自建服务 ${state.config.url}` : "未配置";
}

function buildTrayMenu() {
  const s = state.status;
  const stats = s?.stats || {};
  const engine = s?.engine || {};
  return Menu.buildFromTemplate([
    { label: modeLabel(), enabled: false },
    { label: stats.videos ? `进度：${stats.done_videos}/${stats.videos} 个视频` : "尚未同步课程", enabled: false },
    {
      label: s ? `引擎：${engine.running ? "运行中" : "已暂停"}（${engine.active_videos || 0} 路并行）` : "服务未就绪",
      enabled: false,
    },
    { type: "separator" },
    { label: "打开看板", click: showDashboard },
    { label: "开始刷课", enabled: Boolean(s), click: () => callApi("/api/engine/start") },
    { label: "暂停刷课", enabled: Boolean(s), click: () => callApi("/api/engine/stop") },
    { label: "立即汇报进度", enabled: Boolean(s), click: () => callApi("/api/system/report") },
    { type: "separator" },
    { label: "安装命令行工具（ykt）…", click: () => installCli(true) },
    { label: "重新设置…", click: () => showSetup() },
    { label: "查看数据目录", click: () => shell.openPath(dataDir()) },
    { label: "退出", click: () => app.quit() },
  ]);
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
        : "雨课堂网课助手 · 服务未就绪"
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
      showDashboard();
    }
  });
  refreshStatus();
  state.timer = setInterval(refreshStatus, 30000);
}

// ---------------------------------------------------------------------
// 命令行工具安装（把内置服务程序暴露为 ykt 命令）
// ---------------------------------------------------------------------

const CLI_DIR = path.join(app.getPath("home"), ".local", "bin");
const CLI_CFG_DIR = path.join(app.getPath("home"), ".ykt-helper");

function installCli(interactive = false) {
  const bin = bundledServerBinary();
  if (!bin) return { ok: false, error: "未找到内置服务程序" };
  const target = path.join(CLI_DIR, "ykt");
  try {
    fs.mkdirSync(CLI_DIR, { recursive: true });
    fs.mkdirSync(CLI_CFG_DIR, { recursive: true });
    const script = `#!/bin/sh\n# 由「雨课堂网课助手」桌面端生成；卸载直接删除本文件即可\nexec "${bin}" "$@"\n`;
    fs.writeFileSync(target, script, { mode: 0o755 });
    fs.chmodSync(target, 0o755);
    fs.writeFileSync(
      path.join(CLI_CFG_DIR, "config.json"),
      JSON.stringify({ url: baseUrl(), token: state.config.token || "" }, null, 2),
      "utf8"
    );
    const inPath = (process.env.PATH || "").split(":").includes(CLI_DIR);
    const hint = inPath ? `已安装：${target}` : `已安装：${target}\n请把 ${CLI_DIR} 加入 PATH 后即可直接使用 ykt 命令。`;
    if (interactive) notify("命令行工具已安装", hint);
    return { ok: true, path: target, hint, bin };
  } catch (e) {
    if (interactive) notify("安装失败", String(e.message || e));
    return { ok: false, error: String(e.message || e) };
  }
}

// ---------------------------------------------------------------------
// IPC
// ---------------------------------------------------------------------

function registerIpc() {
  ipcMain.handle("config:get", () => state.config);

  ipcMain.handle("config:save", async (_e, patch) => {
    Object.assign(state.config, patch);
    saveConfig();
    const mode = state.config.mode;
    if (mode === "embedded") {
      showLoading("正在启动内置服务…");
      const res = await startEmbeddedServer();
      if (res.ok) showDashboard();
      else showSetup(res.error);
    } else if (mode === "localdev") {
      const res = startDevService();
      if (res.ok) {
        const ready = await waitReady(state.config.url, state.config.token, 60000);
        ready ? showDashboard() : showSetup("服务启动超时，请查看启动日志");
      } else {
        showSetup(res.error);
      }
    } else {
      showDashboard();
    }
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
    running: Boolean(state.proc),
    logs: state.logs.slice(-120),
    mode: state.config.mode,
  }));

  ipcMain.handle("local:start", async () => {
    const mode = state.config.mode;
    if (mode === "localdev") return startDevService();
    if (state.proc) return { ok: true, port: state.config.embeddedPort };
    return startEmbeddedServer();
  });

  ipcMain.handle("local:stop", () => {
    stopService();
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

  ipcMain.handle("cli:install", () => installCli(false));

  ipcMain.handle("app:version", () => ({
    app: app.getVersion(),
    electron: process.versions.electron,
    configPath: configPath(),
    dataDir: dataDir(),
    hasBundledServer: Boolean(bundledServerBinary()),
    mode: state.config.mode,
  }));
}

// ---------------------------------------------------------------------
// 生命周期
// ---------------------------------------------------------------------

async function boot() {
  loadConfig();
  registerIpc();
  createTray();

  if (state.config.mode === "embedded") {
    showLoading("首次启动正在初始化（约 1-3 秒）…");
    const res = await startEmbeddedServer();
    if (res.ok) {
      showDashboard();
      if (state.config.embeddedPort) installCli(false); // 静默维护 CLI 包装脚本
    } else {
      showSetup(res.error);
    }
    return;
  }

  if (state.config.mode === "localdev") {
    showLoading("正在启动本机服务（uv）…");
    const res = startDevService();
    if (!res.ok) return showSetup(res.error);
    const ready = await waitReady(state.config.url, state.config.token, 60000);
    ready ? showDashboard() : showSetup("服务启动超时，请查看启动日志");
    return;
  }

  if (state.config.url) {
    createWindow();
    showDashboard();
  } else {
    showSetup();
  }
}

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (state.win) {
      state.win.show();
      state.win.focus();
    } else {
      showDashboard();
    }
  });

  app.whenReady().then(() => {
    boot();

    app.on("activate", () => {
      if (BrowserWindow.getAllWindows().length === 0) {
        showDashboard();
      }
    });
  });

  app.on("window-all-closed", () => {
    // 保留在托盘里运行（macOS 习惯）
    if (process.platform !== "darwin") app.quit();
  });

  app.on("before-quit", () => {
    stopService();
    if (state.timer) clearInterval(state.timer);
  });

  // 被 kill / 终端 Ctrl-C 也要带走内置服务
  for (const sig of ["SIGINT", "SIGTERM", "SIGHUP"]) {
    process.on(sig, () => {
      stopService();
      app.quit();
    });
  }
}
