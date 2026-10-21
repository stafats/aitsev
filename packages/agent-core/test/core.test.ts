import { execFileSync } from "node:child_process";
import { promises as fs } from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  AitsevApiError,
  AitsevClient,
  applyChanges,
  commitPaths,
  createBranch,
  currentBranch,
  detectVerifyPlan,
  findConflicts,
  isGitRepo,
  isTestFile,
  normalizePath,
  prepareTask,
  resolveInside,
  scanWorkspace,
  selectContext,
  snapshotOf,
  titleFrom,
  tokenize,
  UnsafePathError,
  waitForTask,
  type FileChange,
  type ScannedFile,
} from "../src/index";

let root: string;

async function write(rel: string, content: string | Buffer): Promise<void> {
  const full = path.join(root, rel);
  await fs.mkdir(path.dirname(full), { recursive: true });
  await fs.writeFile(full, content);
}

beforeEach(async () => {
  root = await fs.mkdtemp(path.join(os.tmpdir(), "aitsev-core-"));
});

afterEach(async () => {
  await fs.rm(root, { recursive: true, force: true });
});

describe("paths", () => {
  it.each([
    ["src/app.py", "src/app.py"],
    ["./src//app.py", "src/app.py"],
    ["src\\win\\a.cs", "src/win/a.cs"],
    [".github/workflows/ci.yml", ".github/workflows/ci.yml"],
  ])("normalizes %s", (raw, expected) => {
    expect(normalizePath(raw)).toBe(expected);
  });

  it.each(["", " ", ".", "/etc/passwd", "C:/x", "c:\\x", "../a", "a/../../b", ".git/config", "a/.git/x", "a\0b", "a/".repeat(250)])(
    "rejects %j",
    (raw) => {
      expect(() => normalizePath(raw)).toThrow(UnsafePathError);
    },
  );

  it("resolveInside keeps paths in the root", () => {
    expect(resolveInside(root, "a/b.txt")).toBe(path.join(root, "a", "b.txt"));
    expect(() => resolveInside(root, "../escape")).toThrow(UnsafePathError);
  });
});

describe("scanWorkspace", () => {
  it("honours .gitignore, built-in excludes, secrets, binaries and size limits", async () => {
    await write(".gitignore", "ignored-dir/\n*.tmp\n");
    await write("src/app.ts", "export const a = 1;\n");
    await write("README.md", "# hi\n");
    await write("ignored-dir/x.ts", "x");
    await write("notes.tmp", "x");
    await write("node_modules/pkg/index.js", "x");
    await write(".venv/lib/x.py", "x");
    await write(".env", "SECRET=1");
    await write(".env.local", "SECRET=1");
    await write(".env.example", "SECRET=");
    await write("certs/server.pem", "-----BEGIN");
    await write("image.png", Buffer.from([0x89, 0x50, 0x00, 0x01]));
    await write("big.txt", "x".repeat(2000));
    const result = await scanWorkspace(root, { maxFileBytes: 1000 });
    const paths = result.files.map((f) => f.path);
    expect(paths).toEqual([".env.example", ".gitignore", "README.md", "src/app.ts"]);
    const reasons = Object.fromEntries(result.skipped.map((s) => [s.path, s.reason]));
    expect(reasons["image.png"]).toBe("binary");
    expect(reasons["big.txt"]).toBe("too-large");
    expect(reasons[".env"]).toBe("ignored");
    expect(paths).not.toContain(".env.local");
    expect(result.truncated).toBe(false);
    expect(result.totalBytes).toBe(result.files.reduce((n, f) => n + f.bytes, 0));
  });

  it("stops at the total limit and reports truncation", async () => {
    for (let i = 0; i < 5; i++) await write(`f${i}.txt`, "x".repeat(100));
    const result = await scanWorkspace(root, { maxTotalBytes: 250 });
    expect(result.files).toHaveLength(2);
    expect(result.truncated).toBe(true);
    const fileLimit = await scanWorkspace(root, { maxFiles: 3 });
    expect(fileLimit.files).toHaveLength(3);
  });

  it("does not follow symlinks", async () => {
    const outside = await fs.mkdtemp(path.join(os.tmpdir(), "aitsev-outside-"));
    await fs.writeFile(path.join(outside, "secret.txt"), "top secret");
    await write("a.txt", "a");
    try {
      await fs.symlink(outside, path.join(root, "link"), "dir");
      await fs.symlink(path.join(outside, "secret.txt"), path.join(root, "file-link"));
    } catch {
      return; // symlinks unavailable (Windows without privilege)
    }
    const result = await scanWorkspace(root);
    expect(result.files.map((f) => f.path)).toEqual(["a.txt"]);
    await fs.rm(outside, { recursive: true, force: true });
  });
});

function file(p: string, content = "", bytes?: number): ScannedFile {
  return { path: p, content, bytes: bytes ?? Buffer.byteLength(content) };
}

describe("selectContext", () => {
  it("uses the whole project when it fits", () => {
    const sel = selectContext([file("b.py", "x"), file("a.py", "y")], "anything", { maxContextBytes: 100 });
    expect(sel).toEqual({ contextPaths: ["a.py", "b.py"], contextBytes: 2, wholeProject: true });
  });

  it("ranks pinned, mentioned and related files and pairs tests", () => {
    const files = [
      file("src/billing/invoice.py", "def total(lines): return sum(l.amount for l in lines)", 300),
      file("tests/test_invoice.py", "from billing.invoice import total", 300),
      file("src/users/profile.py", "class Profile: pass", 300),
      file("src/util/strings.py", "def slug(s): return s", 300),
      file("docs/huge.md", "invoice ".repeat(10), 300),
      file("src/picked.py", "x = 1", 300),
    ];
    const sel = selectContext(files, "Fix rounding in the invoice total calculation", {
      maxContextBytes: 1000,
      pinned: ["src/picked.py"],
    });
    expect(sel.wholeProject).toBe(false);
    expect(sel.contextPaths).toContain("src/picked.py");
    expect(sel.contextPaths).toContain("src/billing/invoice.py");
    expect(sel.contextPaths).toContain("tests/test_invoice.py");
    expect(sel.contextPaths).not.toContain("src/users/profile.py");
    expect(sel.contextBytes).toBeLessThanOrEqual(1000);
  });

  it("prefers files named in the instructions", () => {
    const files = [file("a/config.ts", "x", 500), file("b/other.ts", "x", 500)];
    const sel = selectContext(files, "Update config.ts to add a timeout", { maxContextBytes: 600 });
    expect(sel.contextPaths).toEqual(["a/config.ts"]);
  });

  it("tokenizes camelCase and snake_case", () => {
    expect(tokenize("getUserInvoice_total")).toEqual(["get", "user", "invoice", "total"]);
    expect(isTestFile("pkg/test_a.py")).toBe(true);
    expect(isTestFile("src/a.spec.ts")).toBe(true);
    expect(isTestFile("src/a.ts")).toBe(false);
  });
});

describe("detectVerifyPlan", () => {
  it("node with npm lockfile and scripts", () => {
    const plan = detectVerifyPlan({
      "package.json": JSON.stringify({ scripts: { test: "vitest run", build: "tsc", lint: "eslint ." } }),
      "package-lock.json": "{}",
    });
    expect(plan.kind).toBe("node");
    expect(plan.image).toBe("node:22-slim");
    expect(plan.setupCommands).toEqual(["npm ci --no-audit --no-fund"]);
    expect(plan.verifyCommands).toEqual(["npm run lint --silent", "npm run build --silent", "npm run test --silent"]);
  });

  it("node with pnpm and npm's placeholder test script", () => {
    const plan = detectVerifyPlan({
      "package.json": JSON.stringify({ scripts: { test: 'echo "Error: no test specified" && exit 1' } }),
      "pnpm-lock.yaml": "",
    });
    expect(plan.setupCommands[0]).toContain("pnpm install --frozen-lockfile");
    expect(plan.verifyCommands).toEqual([]);
    expect(plan.notes.join(" ")).toContain("No test");
  });

  it("python with requirements and tests", () => {
    const plan = detectVerifyPlan({ "requirements.txt": "fastapi\n", "app.py": "", "tests/test_app.py": "" });
    expect(plan.kind).toBe("python");
    expect(plan.setupCommands[0]).toContain("pip install --quiet -r requirements.txt");
    expect(plan.setupCommands[0]).toContain("pip install --quiet pytest");
    expect(plan.verifyCommands).toEqual([".venv/bin/python -m pytest -q"]);
  });

  it("python with uv.lock", () => {
    const plan = detectVerifyPlan({ "pyproject.toml": "[tool.pytest.ini_options]\n", "uv.lock": "" });
    expect(plan.setupCommands[0]).toContain("uv sync --frozen");
    expect(plan.verifyCommands[0]).toContain("uv run --frozen --offline pytest");
  });

  it("python without tests falls back to compile check", () => {
    const plan = detectVerifyPlan(new Map([["main.py", "print(1)"]]));
    expect(plan.verifyCommands[0]).toContain("compileall");
    expect(plan.notes[0]).toContain("No tests");
  });

  it("unknown projects get no commands", () => {
    const plan = detectVerifyPlan({ "README.md": "" });
    expect(plan).toMatchObject({ kind: "unknown", image: null, verifyCommands: [] });
  });
});

function fakeFetch(handler: (url: string, init: RequestInit) => { status?: number; body?: unknown }) {
  const calls: Array<{ url: string; init: RequestInit }> = [];
  const impl = async (url: string, init: RequestInit = {}): Promise<Response> => {
    calls.push({ url, init });
    const { status = 200, body } = handler(url, init);
    return new Response(body === undefined ? "" : typeof body === "string" ? body : JSON.stringify(body), { status });
  };
  return { impl, calls };
}

describe("AitsevClient", () => {
  it("sends auth, JSON bodies and builds URLs", async () => {
    const { impl, calls } = fakeFetch(() => ({ body: { id: "t1", status: "queued" } }));
    const client = new AitsevClient({ baseUrl: "http://host:8000/", apiKey: "k", fetch: impl });
    await client.createTask({ title: "t", instructions: "i", files: {} });
    await client.listTasks({ status: "queued", limit: 5 });
    await client.events("a b", 7);
    await client.approve("t1");
    await client.reject("t1", "no");
    await client.retry("t1", "again");
    await client.cancel("t1");
    expect(calls[0]?.url).toBe("http://host:8000/v1/tasks");
    expect((calls[0]?.init.headers as Record<string, string>).Authorization).toBe("Bearer k");
    expect(JSON.parse(String(calls[0]?.init.body))).toEqual({ title: "t", instructions: "i", files: {} });
    expect(calls[1]?.url).toBe("http://host:8000/v1/tasks?status=queued&limit=5");
    expect(calls[2]?.url).toBe("http://host:8000/v1/tasks/a%20b/events?after=7");
    expect(JSON.parse(String(calls[4]?.init.body))).toEqual({ reason: "no" });
    expect(calls[6]?.init.method).toBe("POST");
  });

  it("turns HTTP errors into readable AitsevApiError", async () => {
    const statuses: Array<[number, unknown, string]> = [
      [401, { detail: "Invalid API key" }, "Invalid or missing API key: Invalid API key"],
      [404, { detail: "Task not found" }, "Not found: Task not found"],
      [422, { detail: [{ msg: "field required" }, { msg: "too long" }] }, "Server error 422: field required; too long"],
      [500, "boom", "Server error 500: boom"],
    ];
    for (const [status, body, message] of statuses) {
      const client = new AitsevClient({ baseUrl: "http://x", apiKey: "k", fetch: fakeFetch(() => ({ status, body })).impl });
      const err = await client.getTask("t").catch((e: unknown) => e);
      expect(err).toBeInstanceOf(AitsevApiError);
      expect((err as AitsevApiError).status).toBe(status);
      expect((err as AitsevApiError).message).toBe(message);
    }
  });

  it("reports unreachable servers", async () => {
    const client = new AitsevClient({
      baseUrl: "http://nowhere",
      apiKey: "k",
      fetch: async () => {
        throw new TypeError("fetch failed");
      },
    });
    await expect(client.health()).rejects.toThrow(/Cannot reach the Aitsev server/);
  });

  it("waitForTask polls until a wanted status", async () => {
    const sequence = ["queued", "planning", "awaiting_approval"];
    let i = 0;
    const client = new AitsevClient({
      baseUrl: "http://x",
      apiKey: "k",
      fetch: fakeFetch(() => ({ body: { id: "t", status: sequence[Math.min(i++, 2)] } })).impl,
    });
    const seen: string[] = [];
    const task = await waitForTask(client, "t", { intervalMs: 1, onUpdate: (t) => seen.push(t.status) });
    expect(task.status).toBe("awaiting_approval");
    expect(seen).toEqual(sequence);
  });

  it("waitForTask honours abort and timeout", async () => {
    const client = new AitsevClient({
      baseUrl: "http://x",
      apiKey: "k",
      fetch: fakeFetch(() => ({ body: { id: "t", status: "generating" } })).impl,
    });
    const controller = new AbortController();
    controller.abort();
    await expect(waitForTask(client, "t", { signal: controller.signal })).rejects.toThrow(/Stopped/);
    await expect(waitForTask(client, "t", { intervalMs: 1, timeoutMs: 5 })).rejects.toThrow(/Timed out/);
  });
});

describe("applyChanges", () => {
  const changes: FileChange[] = [
    { path: "a.txt", action: "modified", content: "new a\n", diff: "" },
    { path: "dir/new.txt", action: "added", content: "fresh\n", diff: "" },
    { path: "old.txt", action: "deleted", content: null, diff: "" },
  ];

  it("writes, creates and deletes when nothing changed locally", async () => {
    await write("a.txt", "old a\n");
    await write("old.txt", "bye\n");
    const snap = snapshotOf({ "a.txt": "old a\n", "old.txt": "bye\n" });
    const report = await applyChanges(root, changes, snap);
    expect(report).toEqual({ written: ["a.txt", "dir/new.txt"], deleted: ["old.txt"], conflicts: [], applied: true });
    expect(await fs.readFile(path.join(root, "a.txt"), "utf8")).toBe("new a\n");
    expect(await fs.readFile(path.join(root, "dir/new.txt"), "utf8")).toBe("fresh\n");
    await expect(fs.stat(path.join(root, "old.txt"))).rejects.toThrow();
    const leftovers = (await fs.readdir(root)).filter((n) => n.includes(".aitsev-"));
    expect(leftovers).toEqual([]);
  });

  it("refuses to overwrite local edits unless forced", async () => {
    await write("a.txt", "edited by user\n");
    await write("dir/new.txt", "user made this\n");
    const snap = snapshotOf({ "a.txt": "old a\n", "old.txt": "bye\n" });
    const conflicts = await findConflicts(root, changes, snap);
    expect(conflicts).toEqual([
      { path: "a.txt", reason: "changed-since-submit" },
      { path: "dir/new.txt", reason: "created-since-submit" },
      { path: "old.txt", reason: "deleted-since-submit" },
    ]);
    const blocked = await applyChanges(root, changes, snap);
    expect(blocked.applied).toBe(false);
    expect(await fs.readFile(path.join(root, "a.txt"), "utf8")).toBe("edited by user\n");
    const forced = await applyChanges(root, changes, snap, { force: true });
    expect(forced.applied).toBe(true);
    expect(await fs.readFile(path.join(root, "a.txt"), "utf8")).toBe("new a\n");
  });

  it("rejects unsafe paths before writing anything", async () => {
    const bad: FileChange[] = [
      { path: "fine.txt", action: "added", content: "x", diff: "" },
      { path: "../escape.txt", action: "added", content: "x", diff: "" },
    ];
    await expect(applyChanges(root, bad, {})).rejects.toThrow(UnsafePathError);
    await expect(fs.stat(path.join(root, "fine.txt"))).rejects.toThrow();
    await expect(
      applyChanges(root, [{ path: "x.txt", action: "added", content: null, diff: "" }], {}),
    ).rejects.toThrow(/no content/);
  });
});

function gitAvailable(): boolean {
  try {
    execFileSync("git", ["--version"]);
    return true;
  } catch {
    return false;
  }
}

describe.skipIf(!gitAvailable())("git helpers", () => {
  it("commits only the given paths on a new branch", async () => {
    const g = (...args: string[]) => execFileSync("git", args, { cwd: root }).toString().trim();
    g("init", "-q", "-b", "main");
    g("config", "user.email", "t@example.com");
    g("config", "user.name", "Test");
    await write("a.txt", "1\n");
    await write("gone.txt", "bye\n");
    await write("untouched.txt", "u\n");
    g("add", ".");
    g("commit", "-qm", "init");
    expect(await isGitRepo(root)).toBe(true);
    expect(await currentBranch(root)).toBe("main");

    await createBranch(root, "aitsev/task-1");
    await write("a.txt", "2\n");
    await fs.rm(path.join(root, "gone.txt"));
    await write("new.txt", "n\n");
    await write("untouched.txt", "local edit\n");
    const sha = await commitPaths(root, ["a.txt", "gone.txt", "new.txt"], "Aitsev: change");
    expect(sha).toMatch(/^[0-9a-f]{40}$/);
    expect(g("show", "--name-status", "--format=", "HEAD").split("\n").sort()).toEqual([
      "A\tnew.txt",
      "D\tgone.txt",
      "M\ta.txt",
    ]);
    expect(g("status", "--porcelain")).toBe("M untouched.txt");
    expect(await currentBranch(root)).toBe("aitsev/task-1");
    await expect(createBranch(root, "bad..name")).rejects.toThrow(/Invalid branch/);
    await expect(commitPaths(root, [], "x")).rejects.toThrow(/Nothing to commit/);
  });

  it("reports non-repos", async () => {
    expect(await isGitRepo(root)).toBe(false);
    expect(await currentBranch(root)).toBeNull();
  });
});

describe("prepareTask", () => {
  it("builds the request body, snapshot and verify plan", async () => {
    await write("calc.py", "def add(a, b):\n    return a - b\n");
    await write("test_calc.py", "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n");
    await write(".env", "OLLAMA_API_KEY=secret");
    const prepared = await prepareTask(root, "  Fix add so it returns the sum\nmore detail  ", { size: "small" });
    expect(prepared.body.title).toBe("Fix add so it returns the sum");
    expect(Object.keys(prepared.body.files).sort()).toEqual(["calc.py", "test_calc.py"]);
    expect(prepared.body.context_paths).toEqual([]);
    expect(prepared.body.size).toBe("small");
    expect(prepared.body.verify_commands).toEqual([".venv/bin/python -m pytest -q"]);
    expect(prepared.body.sandbox_image).toBe("python:3.12-slim");
    expect(Object.keys(prepared.snapshot).sort()).toEqual(["calc.py", "test_calc.py"]);
    expect(JSON.stringify(prepared.body)).not.toContain("secret");
  });

  it("uses overrides and a context subset for big projects", async () => {
    await write("pkg/target.py", "def target():\n    pass\n");
    for (let i = 0; i < 5; i++) await write(`pkg/filler${i}.py`, `x = ${"1".repeat(400)}\n`);
    const prepared = await prepareTask(root, "Improve target", {
      maxContextBytes: 500,
      pinned: ["pkg/target.py", "not/there.py"],
      verifyCommands: ["make test"],
      setupCommands: ["make deps"],
      sandboxImage: "custom:1",
      title: "Custom",
    });
    expect(prepared.body.context_paths).toContain("pkg/target.py");
    expect(prepared.selection.wholeProject).toBe(false);
    expect(prepared.body.verify_commands).toEqual(["make test"]);
    expect(prepared.body.setup_commands).toEqual(["make deps"]);
    expect(prepared.body.sandbox_image).toBe("custom:1");
    expect(prepared.body.title).toBe("Custom");
  });

  it("warns when the project was truncated and rejects empty instructions", async () => {
    for (let i = 0; i < 3; i++) await write(`f${i}.txt`, "x".repeat(100));
    const prepared = await prepareTask(root, "do it", { scan: { maxTotalBytes: 150 } });
    expect(prepared.warnings.some((w) => w.includes("upload limit"))).toBe(true);
    await expect(prepareTask(root, "   ")).rejects.toThrow(/Describe the task/);
    expect(titleFrom("x".repeat(100))).toHaveLength(80);
  });
});
