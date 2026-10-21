/** Pure chat helpers shared by the extension and its tests (no `vscode` import here). */
import type { CatalogModel, ChatEdit, ChatReply, FileChange, Suggestion } from "@aitsev/agent-core";

export type EditStatus = "proposed" | "applied" | "rejected" | "undone" | "conflict" | "failed";
export type ChatMode = "agent" | "ask";

export interface ChatEditView {
  path: string;
  action: ChatEdit["action"];
  status: EditStatus;
  note?: string;
  additions: number;
  deletions: number;
}

export interface ChatEntry {
  id: string;
  role: "user" | "assistant" | "error";
  text: string;
  at: string;
  model?: string;
  edits?: ChatEditView[];
  skipped?: string[];
  verification?: "passed" | "failed" | "repaired" | null;
  autopilot?: boolean;
}

export interface ModelOption {
  id: string;
  label: string;
  locked: boolean;
}

export interface SuggestionView extends Suggestion {
  id: string;
}

export interface ChatState {
  signedIn: boolean;
  busy: string | null;
  entries: ChatEntry[];
  models: ModelOption[];
  model: string;
  mode: ChatMode;
  autopilot: boolean;
  suggestions: SuggestionView[];
  analyzing: boolean;
  hasFolder: boolean;
}

export type ChatMessage =
  | { type: "ready" }
  | { type: "send"; text: string }
  | { type: "setModel"; model: string }
  | { type: "setMode"; mode: ChatMode }
  | { type: "setAutopilot"; on: boolean }
  | { type: "newChat" }
  | { type: "signIn" }
  | { type: "openFolder" }
  | { type: "analyze" }
  | { type: "diff"; entryId: string; path: string }
  | { type: "accept"; entryId: string; path?: string }
  | { type: "reject"; entryId: string; path?: string }
  | { type: "undo"; entryId: string }
  | { type: "fix"; id: string }
  | { type: "openFile"; path: string; line?: number };

const WITH_ENTRY = new Set(["diff", "accept", "reject", "undo"]);
const ALLOWED = new Set([
  "ready", "send", "setModel", "setMode", "setAutopilot", "newChat", "signIn", "openFolder", "analyze",
  "diff", "accept", "reject", "undo", "fix", "openFile",
]);

export function isChatMessage(value: unknown): value is ChatMessage {
  if (!value || typeof value !== "object") return false;
  const m = value as Record<string, unknown>;
  if (typeof m.type !== "string" || !ALLOWED.has(m.type)) return false;
  if (WITH_ENTRY.has(m.type) && typeof m.entryId !== "string") return false;
  if (m.type === "diff" && typeof m.path !== "string") return false;
  if ((m.type === "accept" || m.type === "reject") && m.path !== undefined && typeof m.path !== "string") return false;
  if (m.type === "send") return typeof m.text === "string" && m.text.trim().length > 0 && m.text.length <= 50_000;
  if (m.type === "setModel") return typeof m.model === "string" && m.model.length <= 200;
  if (m.type === "setMode") return m.mode === "agent" || m.mode === "ask";
  if (m.type === "setAutopilot") return typeof m.on === "boolean";
  if (m.type === "fix") return typeof m.id === "string";
  if (m.type === "openFile") return typeof m.path === "string" && (m.line === undefined || typeof m.line === "number");
  return true;
}

/** Map assistant edits to the agent-core change format used for safe writes. */
export function toChanges(edits: ChatEdit[]): FileChange[] {
  return edits.map((e) => ({
    path: e.path,
    action: e.action === "create" ? "added" : e.action === "delete" ? "deleted" : "modified",
    content: e.content,
    diff: e.diff,
  }));
}

/** Changes that put files back the way they were before ``applied`` was written. */
export function inverseChanges(applied: ChatEdit[], before: Record<string, string | null>): FileChange[] {
  return applied.map((e) => {
    const previous = before[e.path] ?? null;
    if (previous === null) return { path: e.path, action: "deleted", content: null, diff: "" };
    return { path: e.path, action: e.action === "delete" ? "added" : "modified", content: previous, diff: "" };
  });
}

export function diffStats(diff: string): { additions: number; deletions: number } {
  let additions = 0;
  let deletions = 0;
  for (const line of diff.split("\n")) {
    if (line.startsWith("+++") || line.startsWith("---")) continue;
    if (line.startsWith("+")) additions++;
    else if (line.startsWith("-")) deletions++;
  }
  return { additions, deletions };
}

export function editViews(reply: ChatReply): ChatEditView[] {
  return reply.edits.map((e) => ({ path: e.path, action: e.action, status: "proposed", ...diffStats(e.diff) }));
}

export function verificationOf(reply: ChatReply): ChatEntry["verification"] {
  if (!reply.verification || reply.verification.passed === null) return null;
  if (reply.verification.passed) return reply.repaired ? "repaired" : "passed";
  return "failed";
}

export function modelOptions(models: CatalogModel[]): ModelOption[] {
  return [
    { id: "auto", label: "Auto (best for each request)", locked: false },
    ...models.filter((m) => m.available).map((m) => ({ id: m.id, label: m.allowed ? m.label : `${m.label} (Pro)`, locked: !m.allowed })),
  ];
}

/** The prompt sent when the user clicks Fix on a background suggestion. */
export function fixPrompt(s: Suggestion): string {
  return `Fix this issue in ${s.path} at line ${s.line}: ${s.title}.${s.detail ? ` ${s.detail}` : ""}`;
}

export function newId(random: () => number = Math.random): string {
  return `${Date.now().toString(36)}${Math.floor(random() * 1e9).toString(36)}`;
}

/** Keep the conversation sent to the model within a budget, newest turns first. */
export function historyFor(entries: ChatEntry[], maxChars = 60_000): Array<{ role: "user" | "assistant"; content: string }> {
  const turns = entries.filter((e) => e.role !== "error" && e.text.trim());
  const out: Array<{ role: "user" | "assistant"; content: string }> = [];
  let used = 0;
  for (let i = turns.length - 1; i >= 0; i--) {
    const e = turns[i]!;
    const text = e.role === "assistant" && e.edits?.length
      ? `${e.text}\n(Edited: ${e.edits.map((x) => `${x.path} [${x.status}]`).join(", ")})`
      : e.text;
    if (used + text.length > maxChars && out.length) break;
    used += text.length;
    out.unshift({ role: e.role as "user" | "assistant", content: text });
  }
  while (out.length && out[0]!.role !== "user") out.shift();
  return out;
}
