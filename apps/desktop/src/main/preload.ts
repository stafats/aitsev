import { contextBridge, ipcRenderer } from "electron";

// The renderer gets exactly these functions and nothing else from Node or Electron.
contextBridge.exposeInMainWorld("aitsev", {
  getSettings: () => ipcRenderer.invoke("settings:get"),
  saveSettings: (input: { serverUrl: string; apiKey?: string | null }) => ipcRenderer.invoke("settings:save", input),
  request: (method: string, path: string, body?: unknown) => ipcRenderer.invoke("api:request", { method, path, body }),
  saveFile: (defaultName: string, content: string) => ipcRenderer.invoke("file:save", { defaultName, content }),
  openTextFile: () => ipcRenderer.invoke("file:open-text"),
  ready: () => ipcRenderer.send("renderer:ready"),
  signIn: () => ipcRenderer.invoke("auth:start"),
  cancelSignIn: () => ipcRenderer.invoke("auth:cancel"),
  reopenSignIn: () => ipcRenderer.invoke("auth:reopen"),
  onSignInResult: (callback: (result: { ok: boolean; email?: string; error?: string }) => void) => {
    const listener = (_event: unknown, result: { ok: boolean; email?: string; error?: string }) => callback(result);
    ipcRenderer.on("auth:result", listener);
    return () => ipcRenderer.removeListener("auth:result", listener);
  },
  listEditors: () => ipcRenderer.invoke("editors:list"),
  installEditors: (ids: string[]) => ipcRenderer.invoke("editors:install", ids),
});
