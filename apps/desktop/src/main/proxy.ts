/**
 * The only way the renderer reaches the server: a narrow, validated request proxy that adds the
 * API key in the main process.
 */

export type HttpMethod = "GET" | "POST" | "PUT" | "DELETE";

export interface ApiRequest {
  method: HttpMethod;
  path: string;
  body?: unknown;
}

export interface ApiResponse {
  ok: boolean;
  status: number;
  data: unknown;
  error?: string;
}

const METHODS = new Set<HttpMethod>(["GET", "POST", "PUT", "DELETE"]);
const PATH = /^\/(health|v1\/[A-Za-z0-9._~\-/]*)(\?[A-Za-z0-9._~\-=&%]*)?$/;
const MAX_BODY = 10 * 1024 * 1024;

export function validateRequest(raw: unknown): ApiRequest {
  if (!raw || typeof raw !== "object") throw new Error("Invalid request");
  const req = raw as Record<string, unknown>;
  const method = req.method;
  const path = req.path;
  if (typeof method !== "string" || !METHODS.has(method as HttpMethod)) throw new Error("Method not allowed");
  if (typeof path !== "string" || !PATH.test(path) || path.includes("..") || path.includes("//")) {
    throw new Error("Path not allowed");
  }
  if (req.body !== undefined) {
    const size = JSON.stringify(req.body).length;
    if (size > MAX_BODY) throw new Error("Request body is too large");
  }
  return { method: method as HttpMethod, path, body: req.body };
}

export type FetchLike = (url: string, init: RequestInit) => Promise<Response>;

function detail(data: unknown): string {
  if (data && typeof data === "object" && "detail" in data) {
    const d = (data as { detail: unknown }).detail;
    if (typeof d === "string") return d;
    if (Array.isArray(d)) {
      return d
        .map((item) =>
          item && typeof item === "object" && "msg" in item
            ? `${((item as { loc?: unknown[] }).loc ?? []).slice(1).join(".")}: ${String((item as { msg: unknown }).msg)}`
            : JSON.stringify(item),
        )
        .join("; ");
    }
  }
  return typeof data === "string" ? data.slice(0, 300) : "";
}

export async function performRequest(
  request: ApiRequest,
  serverUrl: string,
  apiKey: string | null,
  fetchImpl: FetchLike = (url, init) => fetch(url, init),
  timeoutMs = 60_000,
): Promise<ApiResponse> {
  if (!apiKey && request.path !== "/health") {
    return { ok: false, status: 0, data: null, error: "Set your API key in Settings first" };
  }
  const headers: Record<string, string> = { Accept: "application/json" };
  if (apiKey) headers.Authorization = `Bearer ${apiKey}`;
  const init: RequestInit = { method: request.method, headers, signal: AbortSignal.timeout(timeoutMs) };
  if (request.body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(request.body);
  }
  let response: Response;
  try {
    response = await fetchImpl(`${serverUrl}${request.path}`, init);
  } catch (err) {
    const reason = err instanceof Error ? err.message : String(err);
    return { ok: false, status: 0, data: null, error: `Cannot reach the server at ${serverUrl}: ${reason}` };
  }
  const text = await response.text();
  let data: unknown = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
  }
  if (response.ok) return { ok: true, status: response.status, data };
  const prefix =
    response.status === 401 ? "Invalid API key" : response.status === 404 ? "Not found" : `Error ${response.status}`;
  const message = detail(data);
  return { ok: false, status: response.status, data, error: message ? `${prefix}: ${message}` : prefix };
}
