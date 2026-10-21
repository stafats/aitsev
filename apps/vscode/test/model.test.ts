import { describe, expect, it } from "vitest";
import type { TaskDetail, TaskSummary } from "@aitsev/agent-core";
import {
  branchName,
  commitMessage,
  describeConflicts,
  escapeHtml,
  isPanelMessage,
  makeNonce,
  slugify,
  statusInfo,
  toDetail,
  toRow,
  type TrackedTask,
} from "../src/model";
import { TaskStore } from "../src/store";

const summary: TaskSummary = {
  id: "0123456789abcdef",
  title: "Fix add",
  status: "awaiting_approval",
  requested_size: "auto",
  effective_size: "normal",
  run_number: 2,
  error: null,
  created_at: "2026-10-21T00:00:00Z",
  updated_at: "2026-10-21T00:01:00Z",
  finished_at: null,
};

const detail: TaskDetail = {
  ...summary,
  instructions: "Make add return the sum",
  feedback: null,
  file_count: 2,
  context_paths: [],
  setup_commands: [],
  verify_commands: ["pytest"],
  sandbox_image: null,
  plan: { summary: "Change operator", steps: ["edit calc.py"], complexity: "normal" },
  decision: { method: "judge_model", winner_label: "B", rationale: "Tests pass" },
  winner_candidate_id: "c2",
  usage: { prompt_tokens: 100, completion_tokens: 50, total_tokens: 150 },
  candidates: [
    { id: "old", run_number: 1, label: "A", model: "x/old", status: "failed", attempts: 3, summary: "", files_changed: [], files_deleted: [], verification: null, reviews: [], score: null, error: null },
    { id: "c1", run_number: 2, label: "A", model: "ollama-cloud/kimi", status: "passed", attempts: 1, summary: "", files_changed: ["calc.py"], files_deleted: [], verification: null, reviews: [], score: 7, error: null },
    { id: "c2", run_number: 2, label: "B", model: "ollama-cloud/glm", status: "passed", attempts: 2, summary: "", files_changed: ["calc.py"], files_deleted: [], verification: null, reviews: [], score: 9, error: null },
  ],
};

const tracked: TrackedTask = { id: summary.id, root: "/w", title: "Fix add", createdAt: "", snapshot: {} };

describe("view model", () => {
  it("maps statuses to labels and tones", () => {
    expect(statusInfo("awaiting_approval")).toEqual({ label: "Needs your approval", tone: "attention" });
    expect(statusInfo("generating").tone).toBe("running");
    expect(statusInfo("failed").tone).toBe("danger");
  });

  it("builds rows with local and applied flags", () => {
    expect(toRow(summary, undefined)).toMatchObject({ local: false, applied: false, statusLabel: "Needs your approval" });
    expect(toRow(summary, { ...tracked, appliedAt: "now" })).toMatchObject({ local: true, applied: true });
  });

  it("builds the detail view for the current run only", () => {
    const view = toDetail(
      detail,
      [{ id: 1, created_at: "t", actor: "system", type: "plan_ready", message: "", data: {} }],
      { task_id: summary.id, status: "awaiting_approval", candidate_id: "c2", label: "B", model: "m", summary: "", decision: null, verification: null, files: [{ path: "calc.py", action: "modified", content: "x", diff: "" }] },
      tracked,
    );
    expect(view.candidates.map((c) => [c.label, c.winner])).toEqual([
      ["A", false],
      ["B", true],
    ]);
    expect(view.files).toEqual([{ path: "calc.py", action: "modified" }]);
    expect(view.events[0]?.message).toBe("plan_ready");
    expect(view).toMatchObject({ canApprove: true, canReject: true, canRetry: true, canCancel: true, tokens: 150 });
    expect(toDetail(detail, [], null, undefined).canApprove).toBe(false);
    const applied = toDetail(detail, [], null, { ...tracked, appliedAt: "now", branch: "aitsev/x", commit: "abcdef1234" });
    expect(applied.appliedNote).toBe("Applied on branch aitsev/x (commit abcdef12)");
    const done = toDetail({ ...detail, status: "approved" }, [], null, tracked);
    expect(done).toMatchObject({ canApprove: false, canReject: false, canRetry: false, canCancel: false });
  });

  it("names branches and commits safely", () => {
    expect(slugify("Add Pagination to /users API!!")).toBe("add-pagination-to-users-api");
    expect(slugify("💥")).toBe("task");
    expect(branchName({ id: summary.id, title: "Fix the ADD bug" })).toBe("aitsev/fix-the-add-bug-01234567");
    expect(commitMessage({ id: "t1", title: "Fix" }, "Changed op", "m")).toBe(
      "Fix\n\nChanged op\n\nGenerated and verified by Aitsev (task t1, winning model m).",
    );
  });

  it("escapes HTML, makes nonces and describes conflicts", () => {
    expect(escapeHtml(`<a href="x">'&'</a>`)).toBe("&lt;a href=&quot;x&quot;&gt;&#39;&amp;&#39;&lt;/a&gt;");
    expect(makeNonce()).toMatch(/^[A-Za-z0-9]{32}$/);
    expect(makeNonce(() => 0)).toBe("A".repeat(32));
    const many = Array.from({ length: 8 }, (_, i) => ({ path: `f${i}`, reason: "changed-since-submit" }));
    expect(describeConflicts(many)).toBe("f0 (edited), f1 (edited), f2 (edited), f3 (edited), f4 (edited), f5 (edited) and 2 more");
  });
});

describe("panel messages", () => {
  it.each([
    [{ type: "ready" }, true],
    [{ type: "submit", instructions: "x", size: "auto", useOpenFiles: true }, true],
    [{ type: "submit", instructions: 1, size: "auto", useOpenFiles: true }, false],
    [{ type: "approve", id: "t" }, true],
    [{ type: "approve" }, false],
    [{ type: "openDiff", id: "t" }, false],
    [{ type: "openDiff", id: "t", path: "a" }, true],
    [{ type: "eval", code: "x" }, false],
    ["ready", false],
    [null, false],
  ])("validates %j", (message, ok) => {
    expect(isPanelMessage(message)).toBe(ok);
  });
});

describe("TaskStore", () => {
  it("adds, updates, caps and keeps newest first", async () => {
    const data = new Map<string, unknown>();
    const store = new TaskStore({
      get: <T>(k: string, d: T) => (data.has(k) ? (data.get(k) as T) : d),
      update: async (k, v) => {
        data.set(k, v);
      },
    });
    for (let i = 0; i < 205; i++) await store.add({ ...tracked, id: `t${i}` });
    expect(store.all()).toHaveLength(200);
    expect(store.all()[0]?.id).toBe("t204");
    await store.add({ ...tracked, id: "t204", title: "again" });
    expect(store.all().filter((t) => t.id === "t204")).toHaveLength(1);
    const updated = await store.update("t204", { appliedAt: "now", id: "hijack" } as Partial<TrackedTask>);
    expect(updated).toMatchObject({ id: "t204", appliedAt: "now" });
    expect(await store.update("missing", { appliedAt: "x" })).toBeUndefined();
  });
});
