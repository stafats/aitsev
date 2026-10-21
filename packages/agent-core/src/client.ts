/** Typed client for the Aitsev backend API (v1). */

export type TaskStatus =
  | "queued"
  | "planning"
  | "generating"
  | "reviewing"
  | "judging"
  | "awaiting_approval"
  | "approved"
  | "rejected"
  | "failed"
  | "cancelled";

export const TERMINAL_STATUSES: ReadonlySet<TaskStatus> = new Set([
  "approved",
  "rejected",
  "failed",
  "cancelled",
]);

export const RUNNING_STATUSES: ReadonlySet<TaskStatus> = new Set([
  "queued",
  "planning",
  "generating",
  "reviewing",
  "judging",
]);

export type TaskSize = "auto" | "small" | "normal" | "large";

export interface TaskCreate {
  title: string;
  instructions: string;
  files: Record<string, string>;
  context_paths?: string[];
  setup_commands?: string[];
  verify_commands?: string[];
  size?: TaskSize;
  sandbox_image?: string | null;
  /** Models that write the code (from catalog()); empty or missing = the server chooses. */
  models?: string[];
}

export interface Candidate {
  id: string;
  run_number: number;
  label: string;
  model: string;
  status: string;
  attempts: number;
  summary: string;
  files_changed: string[];
  files_deleted: string[];
  verification: { passed: boolean | null; builtin_issues: string[]; commands: unknown[] } | null;
  reviews: Array<Record<string, unknown>>;
  score: number | null;
  error: string | null;
}

export interface TaskSummary {
  id: string;
  title: string;
  status: TaskStatus;
  requested_size: TaskSize;
  effective_size: string | null;
  run_number: number;
  error: string | null;
  created_at: string;
  updated_at: string;
  finished_at: string | null;
}

export interface TaskDetail extends TaskSummary {
  instructions: string;
  feedback: string | null;
  file_count: number;
  context_paths: string[];
  models?: string[];
  setup_commands: string[];
  verify_commands: string[];
  sandbox_image: string | null;
  plan: { summary: string; steps: string[]; complexity: string } | null;
  decision: { method: string; winner_label: string; rationale: string } | null;
  winner_candidate_id: string | null;
  usage: { prompt_tokens: number; completion_tokens: number; total_tokens: number };
  candidates: Candidate[];
}

export interface TaskEvent {
  id: number;
  created_at: string;
  actor: string;
  type: string;
  message: string;
  data: Record<string, unknown>;
}

export interface FileChange {
  path: string;
  action: "added" | "modified" | "deleted";
  content: string | null;
  diff: string;
}

export interface TaskResult {
  task_id: string;
  status: TaskStatus;
  candidate_id: string;
  label: string;
  model: string;
  summary: string;
  decision: TaskDetail["decision"];
  verification: Candidate["verification"];
  files: FileChange[];
}

export interface ModelsInfo {
  roles: Array<{ role: string; model: string; available: boolean }>;
  providers: Record<string, string[] | string>;
}

export interface CatalogModel {
  id: string;
  label: string;
  tier: "standard" | "premium" | string;
  description: string;
  weight: number;
  available: boolean;
  allowed: boolean;
}

export interface ModelCatalog {
  plan: string | null;
  premium_allowed: boolean;
  max_selected: number;
  upgrade_url: string;
  models: CatalogModel[];
}

export interface ChatTurn {
  role: "user" | "assistant";
  content: string;
}

export interface ChatBody {
  messages: ChatTurn[];
  mode: "ask" | "agent";
  model?: string;
  files: Record<string, string>;
  context_paths?: string[];
  active_path?: string | null;
  selection?: string | null;
  setup_commands?: string[];
  verify_commands?: string[];
  sandbox_image?: string | null;
}

export interface ChatEdit {
  path: string;
  action: "create" | "modify" | "delete";
  content: string | null;
  diff: string;
}

export interface ChatReply {
  reply: string;
  model: string;
  edits: ChatEdit[];
  skipped: string[];
  verification: { passed: boolean | null; commands: Array<{ command: string; exit_code: number | null; output: string }> } | null;
  repaired: boolean;
  prompt_tokens: number;
  completion_tokens: number;
}

export interface Suggestion {
  path: string;
  line: number;
  severity: "info" | "warning" | "error";
  title: string;
  detail: string;
}

export class AitsevApiError extends Error {
  override name = "AitsevApiError";
  constructor(
    message: string,
    readonly status: number,
    readonly detail: unknown,
  ) {
    super(message);
  }
}

export type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

export interface ClientOptions {
  baseUrl: string;
  apiKey: string;
  fetch?: FetchLike;
  timeoutMs?: number;
}

export class AitsevClient {
  private readonly baseUrl: string;
  private readonly apiKey: string;
  private readonly fetchImpl: FetchLike;
  private readonly timeoutMs: number;

  constructor(options: ClientOptions) {
    this.baseUrl = options.baseUrl.replace(/\/+$/, "");
    this.apiKey = options.apiKey;
    this.fetchImpl = options.fetch ?? ((input, init) => fetch(input, init));
    this.timeoutMs = options.timeoutMs ?? 60_000;
  }

  health(): Promise<{ status: string; database: string; version: string }> {
    return this.request("GET", "/health");
  }

  models(): Promise<ModelsInfo> {
    return this.request("GET", "/v1/models");
  }

  /** Models the signed-in user can choose, and which their plan includes. */
  catalog(): Promise<ModelCatalog> {
    return this.request("GET", "/v1/models/catalog");
  }

  /** One assistant turn; agent mode may return edits (nothing is written by the server). */
  chat(body: ChatBody): Promise<ChatReply> {
    return this.request("POST", "/v1/chat", body, 15 * 60_000);
  }

  /** Background review: line-level suggestions. */
  analyze(body: { files: Record<string, string>; focus?: string; model?: string; limit?: number }): Promise<{ model: string; suggestions: Suggestion[] }> {
    return this.request("POST", "/v1/analyze", body, 5 * 60_000);
  }

  createTask(body: TaskCreate): Promise<TaskDetail> {
    return this.request("POST", "/v1/tasks", body);
  }

  listTasks(params: { status?: TaskStatus; limit?: number; offset?: number } = {}): Promise<TaskSummary[]> {
    const query = new URLSearchParams();
    if (params.status) query.set("status", params.status);
    if (params.limit !== undefined) query.set("limit", String(params.limit));
    if (params.offset !== undefined) query.set("offset", String(params.offset));
    const qs = query.toString();
    return this.request("GET", `/v1/tasks${qs ? `?${qs}` : ""}`);
  }

  getTask(id: string): Promise<TaskDetail> {
    return this.request("GET", `/v1/tasks/${encodeURIComponent(id)}`);
  }

  events(id: string, after = 0): Promise<TaskEvent[]> {
    return this.request("GET", `/v1/tasks/${encodeURIComponent(id)}/events?after=${after}`);
  }

  result(id: string): Promise<TaskResult> {
    return this.request("GET", `/v1/tasks/${encodeURIComponent(id)}/result`);
  }

  approve(id: string, comment?: string): Promise<TaskDetail> {
    return this.request("POST", `/v1/tasks/${encodeURIComponent(id)}/approve`, comment ? { comment } : {});
  }

  reject(id: string, reason: string): Promise<TaskDetail> {
    return this.request("POST", `/v1/tasks/${encodeURIComponent(id)}/reject`, { reason });
  }

  retry(id: string, feedback: string): Promise<TaskDetail> {
    return this.request("POST", `/v1/tasks/${encodeURIComponent(id)}/retry`, { feedback });
  }

  cancel(id: string): Promise<TaskDetail> {
    return this.request("POST", `/v1/tasks/${encodeURIComponent(id)}/cancel`);
  }

  private async request<T>(method: string, path: string, body?: unknown, timeoutMs?: number): Promise<T> {
    const headers: Record<string, string> = { Authorization: `Bearer ${this.apiKey}`, Accept: "application/json" };
    const init: RequestInit = { method, headers, signal: AbortSignal.timeout(timeoutMs ?? this.timeoutMs) };
    if (body !== undefined) {
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
    let response: Response;
    try {
      response = await this.fetchImpl(`${this.baseUrl}${path}`, init);
    } catch (err) {
      const reason = err instanceof Error ? err.message : String(err);
      throw new AitsevApiError(`Cannot reach the Aitsev server at ${this.baseUrl}: ${reason}`, 0, null);
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
    if (!response.ok) {
      throw new AitsevApiError(describeError(response.status, data), response.status, data);
    }
    return data as T;
  }
}

function describeError(status: number, data: unknown): string {
  const detail = data && typeof data === "object" && "detail" in data ? (data as { detail: unknown }).detail : data;
  let text: string;
  if (typeof detail === "string") text = detail;
  else if (Array.isArray(detail)) {
    text = detail
      .map((d) => (d && typeof d === "object" && "msg" in d ? String((d as { msg: unknown }).msg) : JSON.stringify(d)))
      .join("; ");
  } else text = detail ? JSON.stringify(detail) : "";
  const prefix =
    status === 401 ? "Invalid or missing API key" : status === 404 ? "Not found" : `Server error ${status}`;
  return text ? `${prefix}: ${text}` : prefix;
}

/** Poll a task until it reaches one of the wanted statuses or a terminal one. */
export async function waitForTask(
  client: AitsevClient,
  id: string,
  options: {
    until?: ReadonlySet<TaskStatus>;
    intervalMs?: number;
    timeoutMs?: number;
    onUpdate?: (task: TaskDetail) => void;
    signal?: AbortSignal;
  } = {},
): Promise<TaskDetail> {
  const until = options.until ?? new Set<TaskStatus>(["awaiting_approval", ...TERMINAL_STATUSES]);
  const interval = options.intervalMs ?? 2000;
  const deadline = Date.now() + (options.timeoutMs ?? 60 * 60 * 1000);
  for (;;) {
    if (options.signal?.aborted) throw new Error("Stopped waiting for the task");
    const task = await client.getTask(id);
    options.onUpdate?.(task);
    if (until.has(task.status)) return task;
    if (Date.now() > deadline) throw new Error(`Timed out waiting for task ${id} (status ${task.status})`);
    await new Promise((resolve) => setTimeout(resolve, interval));
  }
}
