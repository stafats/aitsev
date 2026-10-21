/** Pure helpers shared by the extension and its tests (no `vscode` import here). */
import type { Snapshot, TaskDetail, TaskEvent, TaskResult, TaskStatus, TaskSummary } from "@aitsev/agent-core";

/** What the extension remembers about a task it submitted from this workspace. */
export interface TrackedTask {
  id: string;
  root: string;
  title: string;
  createdAt: string;
  snapshot: Snapshot;
  /** Set once the approved result has been written to disk. */
  appliedAt?: string;
  branch?: string;
  commit?: string;
}

export interface TaskRow {
  id: string;
  title: string;
  status: TaskStatus;
  statusLabel: string;
  tone: StatusTone;
  updatedAt: string;
  local: boolean;
  applied: boolean;
}

export type StatusTone = "running" | "attention" | "success" | "danger" | "muted";

export interface DetailView {
  id: string;
  title: string;
  status: TaskStatus;
  statusLabel: string;
  tone: StatusTone;
  instructions: string;
  error: string | null;
  plan: { summary: string; steps: string[] } | null;
  decision: { label: string; method: string; rationale: string } | null;
  candidates: Array<{
    label: string;
    model: string;
    status: string;
    attempts: number;
    score: number | null;
    winner: boolean;
    error: string | null;
  }>;
  files: Array<{ path: string; action: string }>;
  events: Array<{ time: string; message: string; type: string }>;
  tokens: number;
  canApprove: boolean;
  canReject: boolean;
  canRetry: boolean;
  canCancel: boolean;
  local: boolean;
  applied: boolean;
  appliedNote: string | null;
}

export interface ViewState {
  configured: boolean;
  serverUrl: string;
  busy: string | null;
  error: string | null;
  notice: string | null;
  tasks: TaskRow[];
  selected: DetailView | null;
  defaultSize: string;
  account: string | null;
  models: string;
}

const LABELS: Record<TaskStatus, [string, StatusTone]> = {
  queued: ["Queued", "running"],
  planning: ["Planning", "running"],
  generating: ["Building", "running"],
  reviewing: ["Reviewing", "running"],
  judging: ["Judging", "running"],
  awaiting_approval: ["Needs your approval", "attention"],
  approved: ["Approved", "success"],
  rejected: ["Rejected", "muted"],
  failed: ["Failed", "danger"],
  cancelled: ["Cancelled", "muted"],
};

export function statusInfo(status: TaskStatus): { label: string; tone: StatusTone } {
  const [label, tone] = LABELS[status] ?? [status, "muted"];
  return { label, tone };
}

export function toRow(summary: TaskSummary, tracked: TrackedTask | undefined): TaskRow {
  const info = statusInfo(summary.status);
  return {
    id: summary.id,
    title: summary.title,
    status: summary.status,
    statusLabel: info.label,
    tone: info.tone,
    updatedAt: summary.updated_at,
    local: tracked !== undefined,
    applied: Boolean(tracked?.appliedAt),
  };
}

export function toDetail(
  task: TaskDetail,
  events: TaskEvent[],
  result: TaskResult | null,
  tracked: TrackedTask | undefined,
): DetailView {
  const info = statusInfo(task.status);
  const current = task.candidates.filter((c) => c.run_number === task.run_number);
  const waiting = task.status === "awaiting_approval";
  return {
    id: task.id,
    title: task.title,
    status: task.status,
    statusLabel: info.label,
    tone: info.tone,
    instructions: task.instructions,
    error: task.error,
    plan: task.plan ? { summary: task.plan.summary, steps: task.plan.steps ?? [] } : null,
    decision: task.decision
      ? { label: task.decision.winner_label, method: task.decision.method, rationale: task.decision.rationale }
      : null,
    candidates: current.map((c) => ({
      label: c.label,
      model: c.model,
      status: c.status,
      attempts: c.attempts,
      score: c.score,
      winner: c.id === task.winner_candidate_id,
      error: c.error,
    })),
    files: (result?.files ?? []).map((f) => ({ path: f.path, action: f.action })),
    events: events.slice(-40).map((e) => ({ time: e.created_at, message: e.message || e.type, type: e.type })),
    tokens: task.usage.total_tokens,
    canApprove: waiting && tracked !== undefined,
    canReject: waiting,
    canRetry: ["awaiting_approval", "rejected", "failed", "cancelled"].includes(task.status),
    canCancel: ["queued", "planning", "generating", "reviewing", "judging", "awaiting_approval"].includes(task.status),
    local: tracked !== undefined,
    applied: Boolean(tracked?.appliedAt),
    appliedNote: tracked?.appliedAt
      ? `Applied ${tracked.branch ? `on branch ${tracked.branch}` : "to the workspace"}${
          tracked.commit ? ` (commit ${tracked.commit.slice(0, 8)})` : ""
        }`
      : null,
  };
}

export function slugify(text: string, max = 40): string {
  const slug = text
    .toLowerCase()
    .normalize("NFKD")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, max)
    .replace(/-+$/g, "");
  return slug || "task";
}

export function branchName(task: { id: string; title: string }): string {
  return `aitsev/${slugify(task.title)}-${task.id.slice(0, 8)}`;
}

export function commitMessage(task: { id: string; title: string }, summary: string, model: string): string {
  const body = summary.trim() ? `\n\n${summary.trim()}` : "";
  return `${task.title}${body}\n\nGenerated and verified by Aitsev (task ${task.id}, winning model ${model}).`;
}

export function escapeHtml(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

export function makeNonce(random: () => number = Math.random): string {
  const chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
  let out = "";
  for (let i = 0; i < 32; i++) out += chars.charAt(Math.floor(random() * chars.length));
  return out;
}

export function describeConflicts(conflicts: Array<{ path: string; reason: string }>): string {
  const reasons: Record<string, string> = {
    "changed-since-submit": "edited",
    "created-since-submit": "created",
    "deleted-since-submit": "deleted",
  };
  const items = conflicts.slice(0, 6).map((c) => `${c.path} (${reasons[c.reason] ?? c.reason})`);
  const more = conflicts.length > 6 ? ` and ${conflicts.length - 6} more` : "";
  return `${items.join(", ")}${more}`;
}

export type PanelMessage =
  | { type: "ready" }
  | { type: "refresh" }
  | { type: "setApiKey" }
  | { type: "signIn" }
  | { type: "chooseModels" }
  | { type: "submit"; instructions: string; size: string; useOpenFiles: boolean }
  | { type: "select"; id: string }
  | { type: "approve"; id: string }
  | { type: "reject"; id: string }
  | { type: "retry"; id: string }
  | { type: "cancel"; id: string }
  | { type: "openDiff"; id: string; path: string };

const ALLOWED = new Set(["ready", "refresh", "setApiKey", "signIn", "chooseModels", "submit", "select", "approve", "reject", "retry", "cancel", "openDiff"]);

export function isPanelMessage(value: unknown): value is PanelMessage {
  if (!value || typeof value !== "object") return false;
  const msg = value as Record<string, unknown>;
  if (typeof msg.type !== "string" || !ALLOWED.has(msg.type)) return false;
  if (["select", "approve", "reject", "retry", "cancel", "openDiff"].includes(msg.type) && typeof msg.id !== "string") {
    return false;
  }
  if (msg.type === "openDiff" && typeof msg.path !== "string") return false;
  if (msg.type === "submit") {
    return typeof msg.instructions === "string" && typeof msg.size === "string" && typeof msg.useOpenFiles === "boolean";
  }
  return true;
}
