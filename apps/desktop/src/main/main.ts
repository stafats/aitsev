import { promises as fs } from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { app, BrowserWindow, dialog, ipcMain, safeStorage, session, shell } from "electron";
import { detectEditors, EDITORS, fileExists, installExtension } from "./editors";
import { performRequest, validateRequest } from "./proxy";
import { pollSignIn, startSignIn } from "./signin";
import { SettingsStore } from "./store";
import { startAutoUpdates } from "./updater";

const SMOKE = process.argv.includes("--smoke");
const MAX_OPEN_BYTES = 5 * 1024 * 1024;

let store: SettingsStore;
let window: BrowserWindow | null = null;
let signInAbort: AbortController | null = null;
let signInUrl: string | null = null;

const errorText = (err: unknown) => (err instanceof Error ? err.message : String(err));

/** The editor extension shipped inside the installer. */
function bundledVsix(): string {
  return app.isPackaged
    ? path.join(process.resourcesPath, "extension", "aitsev.vsix")
    : path.join(__dirname, "..", "..", "vscode", "aitsev.vsix");
}

function createWindow(): BrowserWindow {
  const win = new BrowserWindow({
    width: 1280,
    height: 860,
    minWidth: 900,
    minHeight: 600,
    title: "Aitsev Studio",
    show: !SMOKE,
    backgroundColor: "#1e1e1e",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
      webSecurity: true,
      spellcheck: true,
    },
  });
  // Never navigate away from the app or open windows; external links go to the browser.
  win.webContents.on("will-navigate", (event) => event.preventDefault());
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith("https://")) void shell.openExternal(url);
    return { action: "deny" };
  });
  void win.loadFile(path.join(__dirname, "renderer", "index.html"));
  return win;
}

function registerIpc(): void {
  ipcMain.handle("settings:get", () => store.publicSettings());
  ipcMain.handle("settings:save", async (_event, input: unknown) => {
    const data = (input ?? {}) as { serverUrl?: unknown; apiKey?: unknown };
    try {
      return {
        ok: true,
        settings: await store.save({
          serverUrl: String(data.serverUrl ?? ""),
          apiKey: data.apiKey === null ? null : typeof data.apiKey === "string" ? data.apiKey : undefined,
        }),
      };
    } catch (err) {
      return { ok: false, error: err instanceof Error ? err.message : String(err) };
    }
  });
  ipcMain.handle("api:request", async (_event, raw: unknown) => {
    let request;
    try {
      request = validateRequest(raw);
    } catch (err) {
      return { ok: false, status: 0, data: null, error: err instanceof Error ? err.message : String(err) };
    }
    return performRequest(request, await store.serverUrl(), await store.apiKey());
  });
  ipcMain.handle("file:save", async (_event, input: unknown) => {
    const data = (input ?? {}) as { defaultName?: unknown; content?: unknown };
    if (typeof data.content !== "string") return { ok: false, error: "Nothing to save" };
    const name = typeof data.defaultName === "string" ? path.basename(data.defaultName) : "export.txt";
    const result = await dialog.showSaveDialog(window ?? undefined!, { defaultPath: name });
    if (result.canceled || !result.filePath) return { ok: false, error: "cancelled" };
    await fs.writeFile(result.filePath, data.content, "utf8");
    return { ok: true, path: result.filePath };
  });
  ipcMain.handle("file:open-text", async () => {
    const result = await dialog.showOpenDialog(window ?? undefined!, {
      properties: ["openFile"],
      filters: [
        { name: "Specs", extensions: ["json", "yaml", "yml", "xml", "txt"] },
        { name: "All files", extensions: ["*"] },
      ],
    });
    const file = result.filePaths[0];
    if (result.canceled || !file) return { ok: false, error: "cancelled" };
    const stat = await fs.stat(file);
    if (stat.size > MAX_OPEN_BYTES) return { ok: false, error: "The file is larger than 5 MB" };
    return { ok: true, name: path.basename(file), content: await fs.readFile(file, "utf8") };
  });
  ipcMain.handle("auth:start", async (event) => {
    signInAbort?.abort();
    const abort = new AbortController();
    signInAbort = abort;
    const serverUrl = await store.serverUrl();
    let start;
    try {
      start = await startSignIn(serverUrl, `Aitsev Studio (${os.hostname().slice(0, 40)})`, (url, init) => fetch(url, init));
    } catch (err) {
      return { ok: false, error: errorText(err) };
    }
    signInUrl = start.verification_url;
    void shell.openExternal(start.verification_url);
    void pollSignIn(serverUrl, start, (url, init) => fetch(url, init), { signal: abort.signal })
      .then(async (result) => {
        await store.save({ serverUrl, apiKey: result.apiKey, email: result.email });
        if (!event.sender.isDestroyed()) event.sender.send("auth:result", { ok: true, email: result.email });
      })
      .catch((err: unknown) => {
        if (!abort.signal.aborted && !event.sender.isDestroyed()) event.sender.send("auth:result", { ok: false, error: errorText(err) });
      })
      .finally(() => {
        if (signInAbort === abort) signInAbort = null;
      });
    return { ok: true, userCode: start.user_code, verificationUrl: start.verification_url };
  });
  ipcMain.handle("auth:cancel", () => {
    signInAbort?.abort();
    signInAbort = null;
  });
  ipcMain.handle("auth:reopen", () => {
    if (signInAbort && signInUrl) void shell.openExternal(signInUrl);
  });
  ipcMain.handle("editors:list", async () => {
    const found = await detectEditors(process.platform, process.env);
    return {
      bundled: await fileExists(bundledVsix()),
      editors: found.map((e) => ({ id: e.id, name: e.name, found: e.cliPath !== null })),
    };
  });
  ipcMain.handle("editors:install", async (_event, raw: unknown) => {
    const known = new Set(EDITORS.map((e) => e.id));
    const ids = Array.isArray(raw) ? raw.filter((x): x is string => typeof x === "string" && known.has(x)) : [];
    const vsix = bundledVsix();
    if (!(await fileExists(vsix))) return ids.map((id) => ({ id, name: id, ok: false, error: "The extension is not bundled with this build" }));
    return installExtension(await detectEditors(process.platform, process.env), ids, vsix, process.platform);
  });
  ipcMain.on("renderer:ready", () => {
    if (SMOKE) {
      process.stdout.write("AITSEV_SMOKE_OK\n");
      app.quit();
    }
  });
}

app.whenReady().then(() => {
  store = new SettingsStore(app.getPath("userData"), safeStorage);
  // A strict CSP for everything the window loads.
  session.defaultSession.webRequest.onHeadersReceived((details, callback) => {
    callback({
      responseHeaders: {
        ...details.responseHeaders,
        "Content-Security-Policy": [
          "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'none'",
        ],
      },
    });
  });
  registerIpc();
  window = createWindow();
  startAutoUpdates(app, SMOKE);
  if (SMOKE) setTimeout(() => {
    process.stdout.write("AITSEV_SMOKE_TIMEOUT\n");
    app.exit(1);
  }, 30_000);
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) window = createWindow();
  });
}).catch((err: unknown) => {
  console.error(err);
  app.exit(1);
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});
