import { describe, expect, it } from "vitest";
import {
  diffStats,
  editViews,
  fixPrompt,
  historyFor,
  inverseChanges,
  isChatMessage,
  modelOptions,
  toChanges,
  verificationOf,
  type ChatEntry,
} from "../src/chatModel";

const reply = (over: object = {}) => ({
  reply: "ok", model: "m", skipped: [], verification: null, repaired: false, prompt_tokens: 1, completion_tokens: 1,
  edits: [
    { path: "a.py", action: "modify" as const, content: "new\n", diff: "--- a/a.py\n+++ b/a.py\n-old\n+new\n+more\n" },
    { path: "b.py", action: "create" as const, content: "x\n", diff: "+x\n" },
    { path: "c.py", action: "delete" as const, content: null, diff: "-gone\n" },
  ],
  ...over,
});

describe("chat model", () => {
  it("validates webview messages", () => {
    expect(isChatMessage({ type: "send", text: "hi" })).toBe(true);
    expect(isChatMessage({ type: "send", text: "  " })).toBe(false);
    expect(isChatMessage({ type: "diff", entryId: "e" })).toBe(false);
    expect(isChatMessage({ type: "accept", entryId: "e", path: 3 })).toBe(false);
    expect(isChatMessage({ type: "setMode", mode: "yolo" })).toBe(false);
    expect(isChatMessage({ type: "setAutopilot", on: "yes" })).toBe(false);
    expect(isChatMessage({ type: "openFile", path: "a", line: 3 })).toBe(true);
    expect(isChatMessage({ type: "fix" })).toBe(false);
    expect(isChatMessage({ type: "eval" })).toBe(false);
    expect(isChatMessage(null)).toBe(false);
  });

  it("maps edits to safe-apply changes and back", () => {
    const r = reply();
    expect(toChanges(r.edits).map((c) => c.action)).toEqual(["modified", "added", "deleted"]);
    const inverse = inverseChanges(r.edits, { "a.py": "old\n", "b.py": null, "c.py": "gone\n" });
    expect(inverse).toEqual([
      { path: "a.py", action: "modified", content: "old\n", diff: "" },
      { path: "b.py", action: "deleted", content: null, diff: "" },
      { path: "c.py", action: "added", content: "gone\n", diff: "" },
    ]);
  });

  it("summarises diffs, verification and models", () => {
    expect(diffStats("--- a\n+++ b\n-x\n+y\n+z\n")).toEqual({ additions: 2, deletions: 1 });
    expect(editViews(reply())[0]).toEqual({ path: "a.py", action: "modify", status: "proposed", additions: 2, deletions: 1 });
    expect(verificationOf(reply())).toBeNull();
    expect(verificationOf(reply({ verification: { passed: true, commands: [] }, repaired: true }))).toBe("repaired");
    expect(verificationOf(reply({ verification: { passed: true, commands: [] } }))).toBe("passed");
    expect(verificationOf(reply({ verification: { passed: false, commands: [] } }))).toBe("failed");
    expect(verificationOf(reply({ verification: { passed: null, commands: [] } }))).toBeNull();
    const options = modelOptions([
      { id: "a", label: "A", tier: "standard", description: "", weight: 1, available: true, allowed: true },
      { id: "p", label: "P", tier: "premium", description: "", weight: 5, available: true, allowed: false },
      { id: "x", label: "X", tier: "premium", description: "", weight: 5, available: false, allowed: true },
    ]);
    expect(options.map((o) => [o.id, o.label, o.locked])).toEqual([
      ["auto", "Auto (best for each request)", false],
      ["a", "A", false],
      ["p", "P (Pro)", true],
    ]);
    expect(fixPrompt({ path: "a.py", line: 3, severity: "error", title: "Bug", detail: "Fix it" })).toBe("Fix this issue in a.py at line 3: Bug. Fix it");
  });

  it("builds a bounded history that starts with the user", () => {
    const e = (role: ChatEntry["role"], text: string, extra: Partial<ChatEntry> = {}): ChatEntry => ({ id: text, role, text, at: "", ...extra });
    const entries = [
      e("assistant", "hello"),
      e("user", "one"),
      e("error", "boom"),
      e("assistant", "changed", { edits: [{ path: "a.py", action: "modify", status: "applied", additions: 1, deletions: 0 }] }),
      e("user", "two"),
    ];
    expect(historyFor(entries)).toEqual([
      { role: "user", content: "one" },
      { role: "assistant", content: "changed\n(Edited: a.py [applied])" },
      { role: "user", content: "two" },
    ]);
    expect(historyFor([e("user", "x".repeat(50)), e("user", "y".repeat(50))], 60)).toEqual([{ role: "user", content: "y".repeat(50) }]);
  });
});
