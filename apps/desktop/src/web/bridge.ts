/**
 * Browser implementation of the desktop app's bridge. The session lives in an HttpOnly cookie,
 * so no credential is ever readable from page scripts. Every call sends X-Aitsev-Client: web,
 * which the server requires for cookie-authenticated changes (CSRF guard).
 */
import type { ApiResponse, Bridge } from "../renderer/api";

export const PATH = /^\/(health|v1\/[A-Za-z0-9._~\-/]*)(\?[A-Za-z0-9._~\-=&%+,]*)?$/;
export const MAX_UPLOAD_BYTES = 5_000_000;

export interface WebBridgeOptions {
  fetchImpl?: typeof fetch;
  /** Base URL of the API; same origin by default. */
  base?: string;
  /** Called when the server answers 401 to a signed-in request (session ended). */
  onUnauthorized?: () => void;
}

export function errorText(status: number, data: unknown): string {
  const detail = (data as { detail?: unknown } | null)?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const parts = detail
      .map((d) => {
        const item = d as { msg?: unknown; loc?: unknown };
        const where = Array.isArray(item.loc) ? item.loc.filter((x) => x !== "body").join(".") : "";
        const msg = typeof item.msg === "string" ? item.msg.replace(/^Value error, /, "") : "";
        return where && msg ? `${where}: ${msg}` : msg;
      })
      .filter(Boolean);
    if (parts.length) return parts.join("; ");
  }
  return `Request failed (${status})`;
}

export function createWebBridge(options: WebBridgeOptions = {}): Bridge {
  const doFetch = options.fetchImpl ?? ((input: RequestInfo | URL, init?: RequestInit) => fetch(input, init));
  const base = (options.base ?? "").replace(/\/+$/, "");

  async function request(method: string, path: string, body?: unknown): Promise<ApiResponse> {
    if (!PATH.test(path) || path.includes("..")) {
      return { ok: false, status: 0, data: null, error: "Blocked request path" };
    }
    const headers: Record<string, string> = { Accept: "application/json", "X-Aitsev-Client": "web" };
    const init: RequestInit = { method, headers, credentials: "same-origin", cache: "no-store" };
    if (body !== undefined) {
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
    let res: Response;
    try {
      res = await doFetch(base + path, init);
    } catch {
      return { ok: false, status: 0, data: null, error: "Cannot reach the Aitsev server. Check your connection." };
    }
    let data: unknown = null;
    const text = await res.text();
    if (text) {
      try {
        data = JSON.parse(text);
      } catch {
        data = text;
      }
    }
    if (res.status === 401 && !path.startsWith("/v1/auth/")) options.onUnauthorized?.();
    if (!res.ok) return { ok: false, status: res.status, data, error: errorText(res.status, data) };
    return { ok: true, status: res.status, data };
  }

  return {
    getSettings: async () => ({ serverUrl: base || window.location.origin, hasApiKey: true }),
    saveSettings: async () => ({ ok: false, error: "The web app uses your account; there is nothing to configure." }),
    request,
    async saveFile(defaultName, content) {
      const name = defaultName.replace(/[^A-Za-z0-9._-]/g, "_") || "download.txt";
      const url = URL.createObjectURL(new Blob([content], { type: "text/plain;charset=utf-8" }));
      const link = document.createElement("a");
      link.href = url;
      link.download = name;
      link.rel = "noopener";
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      return { ok: true, path: name };
    },
    openTextFile() {
      return new Promise((resolve) => {
        const input = document.createElement("input");
        input.type = "file";
        input.accept = ".json,.yaml,.yml,.xml,.edmx,.csv,.txt";
        input.addEventListener("cancel", () => resolve({ ok: false, error: "cancelled" }));
        input.addEventListener("change", () => {
          const file = input.files?.[0];
          if (!file) return resolve({ ok: false, error: "cancelled" });
          if (file.size > MAX_UPLOAD_BYTES) return resolve({ ok: false, error: "The file is larger than 5 MB" });
          file.text().then(
            (content) => resolve({ ok: true, name: file.name, content }),
            () => resolve({ ok: false, error: "Could not read the file" }),
          );
        });
        input.click();
      });
    },
    ready() {},
  };
}
