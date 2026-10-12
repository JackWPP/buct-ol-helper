const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("ykt", {
  getConfig: () => ipcRenderer.invoke("config:get"),
  saveConfig: (patch) => ipcRenderer.invoke("config:save", patch),
  testConnection: (payload) => ipcRenderer.invoke("conn:test", payload),
  pickDir: () => ipcRenderer.invoke("dir:pick"),
  openExternal: (url) => ipcRenderer.invoke("open:external", url),
  localState: () => ipcRenderer.invoke("local:state"),
  localStart: () => ipcRenderer.invoke("local:start"),
  localStop: () => ipcRenderer.invoke("local:stop"),
  engineCall: (pathname, method) => ipcRenderer.invoke("engine:call", { path: pathname, method }),
  version: () => ipcRenderer.invoke("app:version"),
  onLocalLog: (cb) => ipcRenderer.on("local:log", (_e, line) => cb(line)),
});
