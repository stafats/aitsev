/**
 * Loads the bundled extension (dist/extension.js) with a fake `vscode` module.
 *
 * - Smoke tests always run.
 * - The end-to-end test runs when AITSEV_E2E=1: it starts a fake Ollama server and the real
 *   Aitsev backend (apps/backend/.venv), then submits, reviews and applies a task through the
 *   extension exactly as a user would.
 */
import { execFileSync, spawn, type ChildProcess } from "node:child_process";
import { promises as fs, existsSync, readFileSync } from "node:fs";
import * as http from "node:http";
import type { AddressInfo } from "node:net";
import * as os from "node:os";
import * as path from "node:path";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { createFakeVscode, packageDefaults, type FakeHarness } from "./fakeVscode";

const EXT_DIR = path.resolve(__dirname, "..");
const pkg = JSON.parse(readFileSync(path.join(EXT_DIR, "package.json"), "utf8"));

interface Loaded {
  harness: FakeHarness;
  ext: { activate: (ctx: unknown) => void; deactivate: () => void };
  restore: () => void;
}

function loadExtension(workspaceRoot: string): Loaded {
  execFileSync(process.execPath, ["esbuild.mjs"], { cwd: EXT_DIR, stdio: "ignore" });
  const harness = createFakeVscode(workspaceRoot, packageDefaults(pkg));
  const restore = harness.install();
  const bundle = path.join(EXT_DIR, "dist", "extension.js");
  delete require.cache[bundle];
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  const ext = require(bundle) as Loaded["ext"];
  ext.activate(harness.context);
  return { harness, ext, restore };
}

type State = {
  configured: boolean;
  busy: string | null;
  error: string | null;
  tasks: Array<{ id: string; status: string }>;
  selected: null | {
    id: string;
    status: string;
    applied: boolean;
    files: Array<{ path: string; action: string }>;
    candidates: Array<{ label: string; winner: boolean }>;
    canApprove: boolean;
  };
};

function lastState(h: FakeHarness): State | undefined {
  for (let i = h.posted.length - 1; i >= 0; i--) {
    const m = h.posted[i] as { type: string; state: State };
    if (m.type === "state") return m.state;
  }
  return undefined;
}

async function until<T>(fn: () => T | undefined | false, label: string, timeoutMs = 90_000): Promise<T> {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const v = fn();
    if (v) return v;
    if (Date.now() > deadline) throw new Error(`Timed out waiting for ${label}`);
    await new Promise((r) => setTimeout(r, 100));
  }
}

describe("extension smoke", () => {
  let loaded: Loaded;
  let root: string;

  beforeAll(async () => {
    root = await fs.mkdtemp(path.join(os.tmpdir(), "aitsev-ext-"));
    loaded = loadExtension(root);
  });

  afterAll(async () => {
    for (const d of loaded.harness.context.subscriptions as Array<{ dispose: () => unknown }>) d.dispose();
    loaded.restore();
    await fs.rm(root, { recursive: true, force: true });
  });

  it("registers every contributed command", () => {
    const contributed = pkg.contributes.commands.map((c: { command: string }) => c.command).sort();
    expect([...loaded.harness.commands.keys()].sort()).toEqual(contributed);
  });

  it("renders a locked-down webview", () => {
    const html = loaded.harness.webviewHtml();
    const nonce = /nonce-([A-Za-z0-9]{32})/.exec(html)?.[1];
    expect(nonce).toBeTruthy();
    expect(html).toContain("default-src 'none'");
    expect(html).toContain(`<script nonce="${nonce}"`);
    expect(html).not.toMatch(/<script(?![^>]*nonce=)/);
  });

  it("shows the setup screen until an API key is set, and ignores bad messages", async () => {
    loaded.harness.sendFromWebview({ type: "ready" });
    loaded.harness.sendFromWebview({ type: "eval", code: "alert(1)" });
    const state = await until(() => lastState(loaded.harness), "first state");
    expect(state.configured).toBe(false);
  });

  it("rejects short API keys through validation and stores good ones", async () => {
    loaded.harness.config["aitsev.serverUrl"] = "http://127.0.0.1:9"; // nothing listens here
    loaded.harness.inputAnswers.push("x".repeat(20));
    await loaded.harness.commands.get("aitsev.setApiKey")?.();
    expect(loaded.harness.secrets.get("aitsev.apiKey")).toBe("x".repeat(20));
    const state = await until(() => (lastState(loaded.harness)?.error ? lastState(loaded.harness) : undefined), "error state");
    expect(state.error).toMatch(/Cannot reach the Aitsev server/);
    await loaded.harness.commands.get("aitsev.clearApiKey")?.();
    expect(loaded.harness.secrets.has("aitsev.apiKey")).toBe(false);
  });
});

describe("browser sign-in and model choice", () => {
  let loaded: Loaded;
  let root: string;
  let server: http.Server;
  let url: string;
  let polls = 0;
  let approve = true;
  const created: unknown[] = [];

  beforeAll(async () => {
    server = http.createServer((req, res) => {
      let body = "";
      req.on("data", (c: Buffer) => (body += c.toString()));
      req.on("end", () => {
        const send = (status: number, data: unknown) => {
          res.writeHead(status, { "Content-Type": "application/json" });
          res.end(JSON.stringify(data));
        };
        if (req.url === "/v1/auth/device/start") {
          polls = 0;
          return send(200, { device_code: "dev_abc", user_code: "BCDF-GHJK", verification_url: `${url}/app/#/connect?code=BCDF-GHJK`, expires_in: 60, interval: 1 });
        }
        if (req.url === "/v1/auth/device/poll") {
          polls += 1;
          if (!approve) return send(400, { detail: "Sign-in was declined" });
          return send(200, polls < 2 ? { status: "pending" } : { status: "approved", api_key: "aitsev_from_browser_0123", email: "ada@example.com" });
        }
        if (req.url === "/v1/models/catalog") {
          return send(200, { plan: "free", premium_allowed: false, max_selected: 3, upgrade_url: `${url}/app/#/account`, models: [
            { id: "ollama-cloud/glm-5", label: "GLM-5", tier: "standard", description: "", weight: 1, available: true, allowed: true },
            { id: "ollama-cloud/kimi-k2.6", label: "Kimi K2.6", tier: "standard", description: "", weight: 1, available: true, allowed: true },
            { id: "anthropic/claude-opus-5-5", label: "Claude Opus 5.5", tier: "premium", description: "", weight: 10, available: true, allowed: false },
            { id: "openai/gpt-5", label: "GPT-5", tier: "premium", description: "", weight: 5, available: false, allowed: false },
          ] });
        }
        if (req.url?.startsWith("/v1/tasks") && req.method === "POST") {
          created.push(JSON.parse(body));
          return send(201, { id: "t1", title: "x", status: "queued", requested_size: "auto", effective_size: null, run_number: 1, error: null,
            created_at: "2026-10-21T00:00:00Z", updated_at: "2026-10-21T00:00:00Z", finished_at: null });
        }
        if (req.url?.startsWith("/v1/tasks")) return send(200, req.url.includes("?") ? [] : {});
        return send(404, { detail: "Not found" });
      });
    });
    await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
    url = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
    root = await fs.mkdtemp(path.join(os.tmpdir(), "aitsev-signin-"));
    await fs.writeFile(path.join(root, "a.py"), "x = 1\n");
    loaded = loadExtension(root);
    loaded.harness.config["aitsev.serverUrl"] = url;
  });

  afterAll(async () => {
    for (const d of loaded.harness.context.subscriptions as Array<{ dispose: () => unknown }>) d.dispose();
    loaded.restore();
    server.close();
    await fs.rm(root, { recursive: true, force: true });
  });

  it("welcomes first-time users once", async () => {
    await until(() => loaded.harness.messages.find((m) => m.text.startsWith("Welcome to Aitsev")), "welcome");
    expect(loaded.harness.messages.find((m) => m.text.startsWith("Welcome to Aitsev"))?.items).toEqual(["Sign in"]);
    expect(loaded.harness.globalState.get("aitsev.welcomed")).toBe(true);
  });

  it("signs in through the browser without copying a key", async () => {
    const h = loaded.harness;
    h.sendFromWebview({ type: "signIn" });
    await until(() => h.secrets.get("aitsev.apiKey"), "key stored");
    expect(h.secrets.get("aitsev.apiKey")).toBe("aitsev_from_browser_0123");
    expect(h.opened).toEqual([`${url}/app/#/connect?code=BCDF-GHJK`]);
    expect(h.progressTitles[0]).toContain("BCDF-GHJK");
    await until(() => lastState(h)?.configured && (lastState(h) as unknown as { account: string }).account === "ada@example.com", "signed-in state");
    expect(h.messages.some((m) => m.text === "Aitsev: signed in as ada@example.com.")).toBe(true);
  });

  it("chooses models, refuses locked ones and sends the choice with tasks", async () => {
    const h = loaded.harness;
    let offered: string[] = [];
    h.quickPickAnswers.push((items) => {
      offered = items.map((i) => i.label);
      return items.filter((i) => i.label !== "Kimi K2.6");
    });
    await h.commands.get("aitsev.chooseModels")?.();
    expect(offered).toEqual(["GLM-5", "Kimi K2.6", "Claude Opus 5.5"]); // unavailable GPT-5 is hidden
    expect(h.messages.some((m) => m.text === "Claude Opus 5.5 needs the Pro plan." && m.items[0] === "Upgrade")).toBe(true);
    expect(h.globalState.get("aitsev.models.v1")).toEqual([{ id: "ollama-cloud/glm-5", label: "GLM-5" }]);
    expect((lastState(h) as unknown as { models: string }).models).toBe("GLM-5");

    h.sendFromWebview({ type: "submit", instructions: "Change x", size: "auto", useOpenFiles: false });
    await until(() => created.length > 0, "task created");
    expect((created[0] as { models: string[] }).models).toEqual(["ollama-cloud/glm-5"]);

    h.quickPickAnswers.push(() => []);
    await h.commands.get("aitsev.chooseModels")?.();
    expect((lastState(h) as unknown as { models: string }).models).toBe("Automatic");
  });

  it("reports declined sign-ins and signs out", async () => {
    const h = loaded.harness;
    await h.commands.get("aitsev.clearApiKey")?.();
    expect(h.globalState.has("aitsev.email")).toBe(false);
    approve = false;
    await h.commands.get("aitsev.signIn")?.();
    expect(lastState(h)?.error).toBe("Sign-in was declined");
    expect(h.secrets.has("aitsev.apiKey")).toBe(false);
  });
});

describe("chat, agent edits and background analysis", () => {
  let loaded: Loaded;
  let root: string;
  let server: http.Server;
  let url: string;
  const chatBodies: Array<Record<string, unknown>> = [];
  let nextReply: (body: Record<string, unknown>) => unknown = () => ({});
  let beforeReply: () => Promise<void> = async () => {};

  const FIXED = "def add(a, b):\n    return a + b\n";
  const EDITS = [
    { path: "calc.py", action: "modify", content: FIXED, diff: "--- a/calc.py\n+++ b/calc.py\n-    return a - b\n+    return a + b\n" },
    { path: "notes.md", action: "create", content: "# Notes\n", diff: "+# Notes\n" },
  ];
  const chatState = (h: FakeHarness) => {
    for (let i = h.chatPosted.length - 1; i >= 0; i--) {
      const m = h.chatPosted[i] as { type: string; state: Record<string, unknown> };
      if (m.type === "state") return m.state as {
        busy: string | null; mode: string; autopilot: boolean; model: string; signedIn: boolean;
        models: Array<{ id: string; locked: boolean }>; suggestions: Array<{ id: string; title: string }>;
        entries: Array<{ id: string; role: string; text: string; verification?: string; edits?: Array<{ path: string; status: string }> }>;
      };
    }
    return undefined;
  };
  const lastEntry = (h: FakeHarness) => chatState(h)?.entries.at(-1);

  beforeAll(async () => {
    server = http.createServer((req, res) => {
      let raw = "";
      req.on("data", (c: Buffer) => (raw += c.toString()));
      req.on("end", () => {
        void (async () => {
          const send = (code: number, data: unknown) => {
            res.writeHead(code, { "Content-Type": "application/json" });
            res.end(JSON.stringify(data));
          };
          if (req.url === "/v1/models/catalog") {
            return send(200, { plan: "free", premium_allowed: false, max_selected: 3, upgrade_url: `${url}/app/#/account`, models: [
              { id: "ollama-cloud/glm-5", label: "GLM-5", tier: "standard", description: "", weight: 1, available: true, allowed: true },
              { id: "anthropic/claude-opus-5-5", label: "Claude Opus 5.5", tier: "premium", description: "", weight: 10, available: true, allowed: false },
            ] });
          }
          if (req.url === "/v1/chat") {
            const body = JSON.parse(raw) as Record<string, unknown>;
            chatBodies.push(body);
            await beforeReply();
            return send(200, nextReply(body));
          }
          if (req.url === "/v1/analyze") {
            return send(200, { model: "m", suggestions: [
              { path: "calc.py", line: 2, severity: "error", title: "add subtracts", detail: "Use + instead of -" },
            ] });
          }
          if (req.url?.startsWith("/v1/tasks")) return send(200, []);
          return send(404, { detail: "Not found" });
        })();
      });
    });
    await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
    url = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
    root = await fs.mkdtemp(path.join(os.tmpdir(), "aitsev-chat-"));
    await fs.writeFile(path.join(root, "calc.py"), "def add(a, b):\n    return a - b\n");
    await fs.writeFile(path.join(root, "test_calc.py"), "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n");
    loaded = loadExtension(root);
    const h = loaded.harness;
    h.config["aitsev.serverUrl"] = url;
    h.config["aitsev.backgroundAnalysis"] = false;
    h.secrets.set("aitsev.apiKey", "aitsev_test_key_0123456789");
    await (h.context.secrets as { store: (k: string, v: string) => Promise<void> }).store("aitsev.apiKey", "aitsev_test_key_0123456789");
    h.sendFromChat({ type: "ready" });
    await until(() => chatState(h)?.models.length === 3 && chatState(h), "models loaded");
  });

  afterAll(async () => {
    for (const d of loaded.harness.context.subscriptions as Array<{ dispose: () => unknown }>) d.dispose();
    loaded.restore();
    server.close();
    await fs.rm(root, { recursive: true, force: true });
  });

  it("renders a locked-down chat view with a model dropdown", () => {
    const h = loaded.harness;
    expect(h.chatHtml()).toContain("default-src 'none'");
    const state = chatState(h)!;
    expect(state.signedIn).toBe(true);
    expect(state.models.map((m) => [m.id, m.locked])).toEqual([["auto", false], ["ollama-cloud/glm-5", false], ["anthropic/claude-opus-5-5", true]]);
    expect(state.mode).toBe("agent");
    expect(state.autopilot).toBe(false);
  });

  it("asks questions with the project as context", async () => {
    const h = loaded.harness;
    nextReply = () => ({ reply: "`add` subtracts.", model: "ollama-cloud/glm-5", edits: [], skipped: [], verification: null, repaired: false, prompt_tokens: 1, completion_tokens: 1 });
    h.sendFromChat({ type: "setMode", mode: "ask" });
    h.sendFromChat({ type: "setModel", model: "ollama-cloud/glm-5" });
    h.sendFromChat({ type: "send", text: "What does add do?" });
    await until(() => lastEntry(h)?.role === "assistant" && !chatState(h)?.busy, "answer");
    expect(lastEntry(h)?.text).toBe("`add` subtracts.");
    const body = chatBodies.at(-1)!;
    expect(body.mode).toBe("ask");
    expect(body.model).toBe("ollama-cloud/glm-5");
    expect(Object.keys(body.files as object).sort()).toEqual(["calc.py", "test_calc.py"]);
    expect(body.verify_commands).toBeUndefined();
    expect(body.messages).toEqual([{ role: "user", content: "What does add do?" }]);
  });

  it("refuses locked models and offers the upgrade", async () => {
    const h = loaded.harness;
    h.sendFromChat({ type: "setModel", model: "anthropic/claude-opus-5-5" });
    await until(() => h.messages.find((m) => m.text.includes("Pro plan")), "upgrade prompt");
    expect(chatState(h)?.model).toBe("auto");
  });

  it("proposes edits in Supervised mode and applies only what you accept", async () => {
    const h = loaded.harness;
    nextReply = () => ({ reply: "Fixed the operator.", model: "m", edits: EDITS, skipped: ["README.md (not shared)"], verification: { passed: true, commands: [] }, repaired: false, prompt_tokens: 1, completion_tokens: 1 });
    h.sendFromChat({ type: "setMode", mode: "agent" });
    h.sendFromChat({ type: "send", text: "Fix add" });
    await until(() => lastEntry(h)?.edits?.length === 2 && !chatState(h)?.busy, "proposal");
    const entry = lastEntry(h)!;
    expect(entry.verification).toBe("passed");
    expect(entry.edits?.map((e) => e.status)).toEqual(["proposed", "proposed"]);
    const body = chatBodies.at(-1)!;
    expect((body.verify_commands as string[]).length).toBeGreaterThan(0); // tests detected → verified in the sandbox
    expect(body.messages).toHaveLength(3); // includes the earlier question and answer
    expect(await fs.readFile(path.join(root, "calc.py"), "utf8")).toContain("a - b"); // nothing written yet

    h.sendFromChat({ type: "diff", entryId: entry.id, path: "calc.py" });
    await until(() => h.executed.find((e) => e.command === "vscode.diff"), "diff");

    h.sendFromChat({ type: "accept", entryId: entry.id, path: "calc.py" });
    h.sendFromChat({ type: "reject", entryId: entry.id, path: "notes.md" });
    await until(() => lastEntry(h)?.edits?.map((e) => e.status).join() === "applied,rejected", "accepted and rejected");
    expect(await fs.readFile(path.join(root, "calc.py"), "utf8")).toBe(FIXED);
    expect(existsSync(path.join(root, "notes.md"))).toBe(false);

    h.sendFromChat({ type: "undo", entryId: entry.id });
    await until(() => lastEntry(h)?.edits?.[0]?.status === "undone", "undone");
    expect(await fs.readFile(path.join(root, "calc.py"), "utf8")).toBe("def add(a, b):\n    return a - b\n");
  });

  it("writes edits straight away in Autopilot and protects your concurrent edits", async () => {
    const h = loaded.harness;
    h.sendFromChat({ type: "setAutopilot", on: true });
    await until(() => chatState(h)?.autopilot, "autopilot on");
    nextReply = () => ({ reply: "Done.", model: "m", edits: EDITS, skipped: [], verification: null, repaired: false, prompt_tokens: 1, completion_tokens: 1 });
    h.sendFromChat({ type: "send", text: "Fix add and add notes" });
    await until(() => lastEntry(h)?.edits?.every((e) => e.status === "applied") && lastEntry(h), "auto-applied");
    expect(await fs.readFile(path.join(root, "calc.py"), "utf8")).toBe(FIXED);
    expect(await fs.readFile(path.join(root, "notes.md"), "utf8")).toBe("# Notes\n");
    h.sendFromChat({ type: "undo", entryId: lastEntry(h)!.id });
    await until(() => lastEntry(h)?.edits?.every((e) => e.status === "undone"), "undo autopilot");
    expect(existsSync(path.join(root, "notes.md"))).toBe(false);

    // The user edits calc.py while the model is working: Autopilot must not overwrite it.
    beforeReply = async () => {
      await fs.writeFile(path.join(root, "calc.py"), "# my edit\ndef add(a, b):\n    return a - b\n");
    };
    h.sendFromChat({ type: "send", text: "Fix add again" });
    await until(() => lastEntry(h)?.edits?.some((e) => e.status === "conflict") && !chatState(h)?.busy && lastEntry(h), "conflict");
    beforeReply = async () => {};
    const entry = lastEntry(h)!;
    expect(entry.edits?.map((e) => e.status)).toEqual(["conflict", "applied"]);
    expect(await fs.readFile(path.join(root, "calc.py"), "utf8")).toContain("# my edit");
    h.warningAnswers.push("Overwrite");
    h.sendFromChat({ type: "accept", entryId: entry.id, path: "calc.py" });
    await until(() => lastEntry(h)?.edits?.[0]?.status === "applied", "forced");
    expect(await fs.readFile(path.join(root, "calc.py"), "utf8")).toBe(FIXED);
    h.sendFromChat({ type: "setAutopilot", on: false });
  });

  it("shows server errors in the conversation", async () => {
    const h = loaded.harness;
    nextReply = () => { throw new Error("unused"); };
    const original = nextReply;
    server.removeAllListeners("request");
    server.on("request", (_req, res) => {
      res.writeHead(402, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ detail: "Your Free plan includes 300,000 model tokens per month" }));
    });
    h.sendFromChat({ type: "send", text: "Anything" });
    await until(() => lastEntry(h)?.role === "error" && lastEntry(h), "error entry");
    expect(lastEntry(h)?.text).toContain("Free plan includes 300,000");
    nextReply = original;
  });

  it("starts a new chat", async () => {
    const h = loaded.harness;
    await h.commands.get("aitsev.newChat")?.();
    await until(() => chatState(h)?.entries.length === 0, "cleared");
    expect(h.globalState.get("aitsev.chat.mode")).toBe("agent");
  });
});

describe("background analysis", () => {
  let loaded: Loaded;
  let root: string;
  let server: http.Server;
  const chats: string[] = [];

  beforeAll(async () => {
    server = http.createServer((req, res) => {
      let raw = "";
      req.on("data", (c: Buffer) => (raw += c.toString()));
      req.on("end", () => {
        res.writeHead(200, { "Content-Type": "application/json" });
        if (req.url === "/v1/analyze") {
          const files = Object.keys((JSON.parse(raw) as { files: object }).files);
          res.end(JSON.stringify({ model: "m", suggestions: files.includes("calc.py")
            ? [{ path: "calc.py", line: 2, severity: "error", title: "add subtracts", detail: "Use +" }]
            : [] }));
        } else if (req.url === "/v1/chat") {
          chats.push((JSON.parse(raw) as { messages: Array<{ content: string }> }).messages.at(-1)!.content);
          res.end(JSON.stringify({ reply: "Fixed.", model: "m", edits: [], skipped: [], verification: null, repaired: false, prompt_tokens: 1, completion_tokens: 1 }));
        } else if (req.url === "/v1/models/catalog") {
          res.end(JSON.stringify({ plan: null, premium_allowed: true, max_selected: 3, upgrade_url: "x", models: [] }));
        } else res.end("[]");
      });
    });
    await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
    root = await fs.mkdtemp(path.join(os.tmpdir(), "aitsev-analysis-"));
    await fs.writeFile(path.join(root, "calc.py"), "def add(a, b):\n    return a - b\n");
    loaded = loadExtension(root);
    loaded.harness.config["aitsev.serverUrl"] = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
    await (loaded.harness.context.secrets as { store: (k: string, v: string) => Promise<void> }).store("aitsev.apiKey", "aitsev_test_key_0123456789");
  });

  afterAll(async () => {
    for (const d of loaded.harness.context.subscriptions as Array<{ dispose: () => unknown }>) d.dispose();
    loaded.restore();
    server.close();
    await fs.rm(root, { recursive: true, force: true });
  });

  it("publishes suggestions as diagnostics with a quick fix that asks the agent", async () => {
    const h = loaded.harness;
    await h.commands.get("aitsev.analyzeNow")?.();
    const diags = h.diagnostics.get(path.join(root, "calc.py"));
    expect(diags?.map((d) => [d.message, d.severity, d.source, d.range.start.line])).toEqual([["add subtracts: Use +", 0, "Aitsev", 1]]);
    expect(h.statusBar.text).toBe("$(lightbulb) Aitsev: 1");
    expect(h.messages.some((m) => m.text === "Aitsev found 1 suggestion(s).")).toBe(true);

    const actions = h.codeActions()!.provideCodeActions({}, {}, { diagnostics: diags! });
    expect(actions.map((a) => a.title)).toEqual(["Ask Aitsev to fix: add subtracts"]);
    await h.commands.get(actions[0]!.command!.command)?.(...(actions[0]!.command!.arguments ?? []));
    await until(() => chats.length > 0, "fix request");
    expect(chats[0]).toBe("Fix this issue in calc.py at line 2: add subtracts. Use +");
  });

  it("can be switched off", async () => {
    const h = loaded.harness;
    h.config["aitsev.backgroundAnalysis"] = false;
    h.saveListeners.forEach((fn) => fn());
    expect(h.statusBar.text).toBe("$(lightbulb) Aitsev: 1");
  });
});

// ---------------------------------------------------------------------------------- end to end

const BACKEND = path.resolve(EXT_DIR, "../backend");
const backendPython = [
  path.join(BACKEND, ".venv", "bin", "python"),
  path.join(BACKEND, ".venv", "Scripts", "python.exe"),
].find((p) => existsSync(p));
const runE2E = process.env.AITSEV_E2E === "1" && Boolean(backendPython);

function fakeOllama(): Promise<{ url: string; close: () => void; calls: string[] }> {
  const calls: string[] = [];
  const server = http.createServer((req, res) => {
    let body = "";
    req.on("data", (c: Buffer) => (body += c.toString()));
    req.on("end", () => {
      res.setHeader("content-type", "application/json");
      if (req.url === "/api/tags") {
        res.end(JSON.stringify({ models: [{ name: "m1" }, { name: "m2" }] }));
        return;
      }
      const request = JSON.parse(body) as { messages: Array<{ content: string }> };
      const system = request.messages[0]?.content ?? "";
      let content: string;
      if (system.includes("lead software architect")) {
        calls.push("plan");
        content = JSON.stringify({
          summary: "Return the sum",
          steps: ["Change - to + in calc.add"],
          files_to_change: ["calc.py"],
          acceptance_criteria: ["tests pass"],
          risks: [],
          complexity: "normal",
        });
      } else if (system.includes("strict senior code reviewer")) {
        calls.push("review");
        content = JSON.stringify({ score: 9, verdict: "approve", issues: [], strengths: ["correct"] });
      } else if (system.includes("pair-programming assistant")) {
        calls.push("chat");
        content = "The operator was wrong; here is the fix.\n<<<FILE calc.py\ndef add(a, b):\n    return a + b\n>>>END FILE\n";
      } else if (system.includes("code health analyst")) {
        calls.push("analyze");
        content = JSON.stringify({ suggestions: [{ path: "calc.py", line: 2, severity: "error", title: "add subtracts", detail: "Use +" }] });
      } else if (system.includes("final judge")) {
        calls.push("judge");
        content = JSON.stringify({ winner: "A", rationale: "Both pass; A is minimal" });
      } else {
        calls.push("generate");
        content = "SUMMARY: Fixed add.\n<<<FILE calc.py\ndef add(a, b):\n    return a + b\n>>>END FILE\n";
      }
      res.end(JSON.stringify({ message: { role: "assistant", content }, prompt_eval_count: 10, eval_count: 5 }));
    });
  });
  return new Promise((resolve) => {
    server.listen(0, "127.0.0.1", () => {
      const { port } = server.address() as AddressInfo;
      resolve({ url: `http://127.0.0.1:${port}`, close: () => server.close(), calls });
    });
  });
}

async function freePort(): Promise<number> {
  const server = http.createServer();
  await new Promise<void>((r) => server.listen(0, "127.0.0.1", () => r()));
  const { port } = server.address() as AddressInfo;
  await new Promise<void>((r) => server.close(() => r()));
  return port;
}

describe.skipIf(!runE2E)("end to end: extension + real backend", () => {
  const API_KEY = "e2e-key-0123456789abcdef";
  let backend: ChildProcess;
  let ollama: Awaited<ReturnType<typeof fakeOllama>>;
  let root: string;
  let dataDir: string;
  let loaded: Loaded;
  let serverUrl: string;
  const logs: string[] = [];

  beforeAll(async () => {
    ollama = await fakeOllama();
    dataDir = await fs.mkdtemp(path.join(os.tmpdir(), "aitsev-e2e-data-"));
    root = await fs.mkdtemp(path.join(os.tmpdir(), "aitsev-e2e-ws-"));
    await fs.writeFile(path.join(root, "calc.py"), "def add(a, b):\n    return a - b\n");
    await fs.writeFile(
      path.join(root, "test_calc.py"),
      "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
    );
    await fs.writeFile(path.join(root, ".env"), "OLLAMA_API_KEY=must-not-upload\n");
    await fs.writeFile(path.join(root, ".gitignore"), ".env\n");
    const git = (...args: string[]) => execFileSync("git", args, { cwd: root, stdio: "ignore" });
    git("init", "-q", "-b", "main");
    git("config", "user.email", "e2e@example.com");
    git("config", "user.name", "E2E");
    git("add", ".");
    git("commit", "-qm", "init");

    const port = await freePort();
    serverUrl = `http://127.0.0.1:${port}`;
    const env = {
      ...process.env,
      AITSEV_ENVIRONMENT: "development",
      AITSEV_DATABASE_URL: `sqlite+aiosqlite:///${path.join(dataDir, "e2e.db")}`,
      AITSEV_AUTO_CREATE_SCHEMA: "true",
      AITSEV_API_KEYS: `e2e:${API_KEY}`,
      AITSEV_SANDBOX_MODE: "local",
      AITSEV_OLLAMA_LOCAL_BASE_URL: ollama.url,
      AITSEV_PLANNER_MODEL: "ollama-local/m1",
      AITSEV_GENERATOR_MODELS: "ollama-local/m1,ollama-local/m2",
      AITSEV_REVIEWER_MODELS: "ollama-local/m2,ollama-local/m1",
      AITSEV_JUDGE_MODEL: "ollama-local/m1",
      AITSEV_FALLBACK_MODELS: "ollama-local/m2",
      AITSEV_WORKER_POLL_SECONDS: "0.2",
      AITSEV_PUBLIC_URL: serverUrl,
      AITSEV_MODEL_CATALOG: JSON.stringify([
        { id: "ollama-local/m1", label: "Local One" },
        { id: "ollama-local/m2", label: "Local Two", tier: "premium", weight: 3 },
      ]),
      AITSEV_PORT: String(port),
      OLLAMA_API_KEY: "",
    };
    backend = spawn(backendPython as string, ["-c", "from aitsev.cli import run_api; run_api()"], { cwd: BACKEND, env });
    backend.stdout?.on("data", (d: Buffer) => logs.push(d.toString()));
    backend.stderr?.on("data", (d: Buffer) => logs.push(d.toString()));
    await until(
      () => {
        void fetch(`${serverUrl}/health`)
          .then((r) => (r.ok ? logs.push("__ready__") : undefined))
          .catch(() => undefined);
        return logs.includes("__ready__");
      },
      "backend health",
      30_000,
    );

    loaded = loadExtension(root);
    const h = loaded.harness;
    h.config["aitsev.serverUrl"] = serverUrl;
    h.config["aitsev.pollIntervalSeconds"] = 1;
    h.config["aitsev.autoCommit"] = true;
    h.config["aitsev.setupCommands"] = ["true"];
    h.config["aitsev.verifyCommands"] = [`${backendPython} -m pytest -q -p no:cacheprovider`];
    h.inputAnswers.push(API_KEY);
    await h.commands.get("aitsev.setApiKey")?.();
  }, 120_000);

  afterAll(async () => {
    if (loaded) {
      for (const d of loaded.harness.context.subscriptions as Array<{ dispose: () => unknown }>) d.dispose();
      loaded.restore();
    }
    backend?.kill();
    ollama?.close();
    await fs.rm(root, { recursive: true, force: true });
    await fs.rm(dataDir, { recursive: true, force: true });
  });

  it("submits, verifies, reviews, diffs and applies a task", async () => {
    const h = loaded.harness;
    h.sendFromWebview({ type: "submit", instructions: "Make add() return the sum", size: "normal", useOpenFiles: false });

    const ready = await until(
      () => {
        const s = lastState(h);
        if (s?.error) throw new Error(`Extension error: ${s.error}\n${logs.join("")}`);
        return s?.selected?.status === "awaiting_approval" && s.selected.files.length > 0 && s;
      },
      "task awaiting approval",
    );
    expect(ready.selected?.files).toEqual([{ path: "calc.py", action: "modified" }]);
    expect(ready.selected?.candidates.filter((c) => c.winner).map((c) => c.label)).toEqual(["A"]);
    expect(ready.selected?.canApprove).toBe(true);
    expect(ollama.calls).toEqual(expect.arrayContaining(["plan", "generate", "review", "judge"]));
    expect(h.messages.some((m) => m.text.includes("ready for review"))).toBe(true);

    // the uploaded files never included .env
    const taskId = ready.selected?.id as string;
    const detail = (await (
      await fetch(`${serverUrl}/v1/tasks/${taskId}`, { headers: { Authorization: `Bearer ${API_KEY}` } })
    ).json()) as { file_count: number };
    expect(detail.file_count).toBe(3); // calc.py, test_calc.py, .gitignore

    h.sendFromWebview({ type: "openDiff", id: taskId, path: "calc.py" });
    await until(() => h.executed.find((e) => e.command === "vscode.diff"), "diff command");

    h.sendFromWebview({ type: "approve", id: taskId });
    await until(() => lastState(h)?.selected?.applied && lastState(h), "applied state");
    expect(await fs.readFile(path.join(root, "calc.py"), "utf8")).toBe("def add(a, b):\n    return a + b\n");
    const branch = execFileSync("git", ["rev-parse", "--abbrev-ref", "HEAD"], { cwd: root }).toString().trim();
    expect(branch).toMatch(/^aitsev\/make-add-return-the-sum-/);
    const log = execFileSync("git", ["log", "-1", "--format=%s%n%b"], { cwd: root }).toString();
    expect(log).toContain("Generated and verified by Aitsev");
    const after = (await (
      await fetch(`${serverUrl}/v1/tasks/${taskId}`, { headers: { Authorization: `Bearer ${API_KEY}` } })
    ).json()) as { status: string };
    expect(after.status).toBe("approved");
  }, 120_000);

  it("protects local edits made after submitting, then rejects and retries", async () => {
    const h = loaded.harness;
    await fs.writeFile(path.join(root, "calc.py"), "def add(a, b):\n    return a - b\n");
    h.sendFromWebview({ type: "submit", instructions: "Fix add again", size: "small", useOpenFiles: false });
    const ready = await until(() => {
      const s = lastState(h);
      return s?.selected?.status === "awaiting_approval" && s.selected.files.length > 0 && s;
    }, "second task ready");
    const taskId = ready.selected?.id as string;

    await fs.writeFile(path.join(root, "calc.py"), "# my local edit\ndef add(a, b):\n    return a - b\n");
    h.warningAnswers.push(undefined); // user closes the overwrite dialog
    h.sendFromWebview({ type: "approve", id: taskId });
    await until(() => h.messages.find((m) => m.text.includes("after submitting the task")), "conflict warning");
    await until(() => !lastState(h)?.busy, "idle after conflict");
    expect(await fs.readFile(path.join(root, "calc.py"), "utf8")).toContain("# my local edit");
    expect(lastState(h)?.selected?.applied).toBe(false);

    h.inputAnswers.push("Please keep my comment");
    h.sendFromWebview({ type: "reject", id: taskId });
    await until(() => lastState(h)?.selected?.status === "rejected", "rejected");

    h.inputAnswers.push("Keep the comment line at the top");
    h.sendFromWebview({ type: "retry", id: taskId });
    await until(() => lastState(h)?.selected?.status === "awaiting_approval" && lastState(h)?.selected?.files.length, "retry ready");

    h.warningAnswers.push("Overwrite");
    h.sendFromWebview({ type: "approve", id: taskId });
    await until(() => lastState(h)?.selected?.applied, "applied after overwrite");
    expect(await fs.readFile(path.join(root, "calc.py"), "utf8")).toBe("def add(a, b):\n    return a + b\n");
  }, 180_000);

  it("signs in through the browser and runs a task with the chosen model", async () => {
    const h = loaded.harness;
    await h.commands.get("aitsev.clearApiKey")?.();
    // A user with an account approves the sign-in on the web.
    const signup = await fetch(`${serverUrl}/v1/auth/signup`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: "e2e@example.com", password: "correct horse battery" }),
    });
    expect(signup.status).toBe(201);
    const cookie = (signup.headers.get("set-cookie") ?? "").split(";")[0] as string;
    const signingIn = h.commands.get("aitsev.signIn")?.() as Promise<void>;
    const opened = await until(() => h.opened.find((u) => u.includes("/app/#/connect?code=")), "approval page opened");
    const code = new URL(opened.replace("#/connect", "connect")).searchParams.get("code") as string;
    const decision = await fetch(`${serverUrl}/v1/auth/device/decision`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Cookie: cookie, "X-Aitsev-Client": "web" },
      body: JSON.stringify({ user_code: code, approve: true }),
    });
    expect(decision.status).toBe(200);
    await signingIn;
    const key = h.secrets.get("aitsev.apiKey") as string;
    expect(key).toMatch(/^aitsev_/);

    h.quickPickAnswers.push((items) => items); // both; the premium one is locked on Free
    await h.commands.get("aitsev.chooseModels")?.();
    expect(h.messages.some((m) => m.text === "Local Two needs the Pro plan.")).toBe(true);
    expect(h.globalState.get("aitsev.models.v1")).toEqual([{ id: "ollama-local/m1", label: "Local One" }]);

    await fs.writeFile(path.join(root, "calc.py"), "def add(a, b):\n    return a - b\n");
    h.sendFromWebview({ type: "submit", instructions: "Fix add with my model", size: "large", useOpenFiles: false });
    const ready = await until(() => {
      const s = lastState(h);
      if (s?.error) throw new Error(`Extension error: ${s.error}\n${logs.join("")}`);
      return s?.selected?.status === "awaiting_approval" && s.selected.files.length > 0 && s;
    }, "task with chosen model ready");
    const detail = (await (
      await fetch(`${serverUrl}/v1/tasks/${ready.selected?.id}`, { headers: { Authorization: `Bearer ${key}` } })
    ).json()) as { models: string[]; candidates: unknown[] };
    expect(detail.models).toEqual(["ollama-local/m1"]);
    expect(detail.candidates).toHaveLength(1); // only the chosen model wrote code, despite "large"
  }, 180_000);

  it("chats with the agent, verifies its edit in the sandbox, and applies it on accept", async () => {
    const h = loaded.harness;
    await fs.writeFile(path.join(root, "calc.py"), "def add(a, b):\n    return a - b\n");
    h.config["aitsev.backgroundAnalysis"] = false;
    h.sendFromChat({ type: "ready" });
    h.sendFromChat({ type: "setAutopilot", on: false });
    h.sendFromChat({ type: "send", text: "add() is broken, fix it" });
    const proposal = await until(() => {
      const states = h.chatPosted.filter((m) => (m as { type: string }).type === "state") as Array<{ state: { busy: string | null; entries: Array<{ id: string; role: string; text: string; verification?: string; edits?: Array<{ path: string; status: string }> }> } }>;
      const last = states.at(-1)?.state;
      const entry = last?.entries.at(-1);
      if (entry?.role === "error") throw new Error(`Chat error: ${entry.text}\n${logs.join("")}`);
      return !last?.busy && (entry?.edits?.length ?? 0) > 0 ? entry : undefined;
    }, "agent proposal");
    expect(proposal.verification).toBe("passed"); // the real backend ran pytest on the edited project
    expect(proposal.edits).toEqual([expect.objectContaining({ path: "calc.py", status: "proposed" })]);
    h.sendFromChat({ type: "accept", entryId: proposal.id, path: "calc.py" });
    await until(() => readFileSync(path.join(root, "calc.py"), "utf8").includes("a + b") || undefined, "applied");

    await h.commands.get("aitsev.analyzeNow")?.();
    expect(h.diagnostics.get(path.join(root, "calc.py"))?.[0]?.message).toBe("add subtracts: Use +");
    expect(ollama.calls).toEqual(expect.arrayContaining(["chat", "analyze"]));
  }, 180_000);
});
