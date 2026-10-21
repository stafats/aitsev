import { promises as fs } from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { describe, expect, it } from "vitest";
import { candidatePaths, detectEditors, EDITORS, installExtension, runCli, type Runner } from "../../src/main/editors";
import type { FetchLike } from "../../src/main/proxy";
import { pollSignIn, safeVerificationUrl, SignInError, startSignIn } from "../../src/main/signin";
import { SettingsStore, type Encryptor } from "../../src/main/store";

const SERVER = "https://app.aitsev.com";

function fakeFetch(replies: Array<{ status: number; body?: unknown } | Error>) {
  const calls: Array<{ url: string; body: unknown }> = [];
  const impl: FetchLike = async (url, init) => {
    calls.push({ url, body: init.body ? JSON.parse(String(init.body)) : undefined });
    const next = replies.shift();
    if (!next) throw new Error("no more replies");
    if (next instanceof Error) throw next;
    return new Response(next.body === undefined ? "" : JSON.stringify(next.body), { status: next.status });
  };
  return { impl, calls };
}

const START = { device_code: "dev_abc", user_code: "BCDF-GHJK", verification_url: `${SERVER}/app/#/connect?code=BCDF-GHJK`, expires_in: 600, interval: 3 };

describe("browser sign-in", () => {
  it("starts with the app name and checks the approval page origin", async () => {
    const { impl, calls } = fakeFetch([{ status: 200, body: START }]);
    const started = await startSignIn(SERVER, "Aitsev Desktop (pc)", impl);
    expect(started.user_code).toBe("BCDF-GHJK");
    expect(calls[0]).toEqual({ url: `${SERVER}/v1/auth/device/start`, body: { client_name: "Aitsev Desktop (pc)" } });
    expect(() => safeVerificationUrl(SERVER, "https://evil.example/app")).toThrow(SignInError);
    expect(() => safeVerificationUrl(SERVER, undefined)).toThrow(/no sign-in page/);
    const bad = fakeFetch([{ status: 429, body: { detail: "Too many sign-in attempts" } }]);
    await expect(startSignIn(SERVER, "x", bad.impl)).rejects.toThrow("Too many sign-in attempts");
    const down = fakeFetch([new TypeError("ECONNREFUSED")]);
    await expect(startSignIn(SERVER, "x", down.impl)).rejects.toThrow(/Cannot reach/);
  });

  it("polls until approved, riding out pending replies and network blips", async () => {
    const { impl, calls } = fakeFetch([
      { status: 200, body: { status: "pending" } },
      new TypeError("network"),
      { status: 429, body: { detail: "slow down" } },
      { status: 200, body: { status: "approved", api_key: "aitsev_key", email: "ada@example.com" } },
    ]);
    const sleeps: number[] = [];
    const result = await pollSignIn(SERVER, START, impl, { sleep: async (ms) => { sleeps.push(ms); } });
    expect(result).toEqual({ apiKey: "aitsev_key", email: "ada@example.com" });
    expect(sleeps).toEqual([3000, 3000, 3000, 3000]);
    expect(calls.every((c) => c.url === `${SERVER}/v1/auth/device/poll`)).toBe(true);
  });

  it("stops on denial, expiry and cancel", async () => {
    const denied = fakeFetch([{ status: 400, body: { detail: "Sign-in was declined" } }]);
    await expect(pollSignIn(SERVER, START, denied.impl, { sleep: async () => {} })).rejects.toThrow("declined");
    let t = 0;
    const pending = fakeFetch(Array.from({ length: 5 }, () => ({ status: 200, body: { status: "pending" } })));
    await expect(pollSignIn(SERVER, { ...START, expires_in: 6 }, pending.impl, { sleep: async () => {}, now: () => (t += 2000) }))
      .rejects.toThrow(/expired/);
    const abort = new AbortController();
    abort.abort();
    await expect(pollSignIn(SERVER, START, pending.impl, { sleep: async () => {}, signal: abort.signal })).rejects.toThrow("cancelled");
  });
});

describe("editors", () => {
  it("knows where VS Code-family editors live on each platform", () => {
    const vscode = EDITORS.find((e) => e.id === "vscode")!;
    const win = candidatePaths(vscode, "win32", { LOCALAPPDATA: "C:\\Users\\a\\AppData\\Local", PATH: "C:\\bin" });
    expect(win[0]).toBe("C:\\Users\\a\\AppData\\Local\\Programs\\Microsoft VS Code\\bin\\code.cmd");
    expect(win).toContain("C:\\bin\\code.cmd");
    const mac = candidatePaths(EDITORS.find((e) => e.id === "cursor")!, "darwin", { HOME: "/Users/a", PATH: "/usr/local/bin" });
    expect(mac[0]).toBe("/Applications/Cursor.app/Contents/Resources/app/bin/cursor");
    expect(mac).toContain("/usr/local/bin/cursor");
  });

  it("detects installed editors and installs the bundled extension", async () => {
    const present = new Set(["/usr/bin/code", "/opt/kiro/bin/kiro"]);
    const found = await detectEditors("linux", { PATH: "/usr/bin:/opt/kiro/bin" }, async (f) => present.has(f));
    expect(found.filter((e) => e.cliPath).map((e) => e.id)).toEqual(["vscode", "kiro"]);
    const runs: Array<[string, string[], boolean]> = [];
    const run: Runner = async (cmd, args, windows) => {
      runs.push([cmd, args, windows]);
      return cmd.endsWith("kiro") ? { code: 1, output: "line1\nFailed: bad vsix" } : { code: 0, output: "ok" };
    };
    const results = await installExtension(found, ["vscode", "kiro", "cursor"], "/app/aitsev.vsix", "linux", run);
    expect(results).toEqual([
      { id: "vscode", name: "Visual Studio Code", ok: true },
      { id: "cursor", name: "Cursor", ok: false, error: "Not installed on this computer" },
      { id: "kiro", name: "Kiro", ok: false, error: "Failed: bad vsix" },
    ]);
    expect(runs[0]).toEqual(["/usr/bin/code", ["--install-extension", "/app/aitsev.vsix", "--force"], false]);
  });

  it("runs a real command line tool", async () => {
    const result = await runCli(process.execPath, ["-e", "console.log('hi')"], false);
    expect(result).toEqual({ code: 0, output: "hi\n" });
    expect((await runCli("/definitely/not/here", [], false)).code).toBe(-1);
  });
});

describe("signed-in email", () => {
  const enc: Encryptor = { isEncryptionAvailable: () => true, encryptString: (t) => Buffer.from(t).reverse(), decryptString: (b) => Buffer.from(b).reverse().toString() };

  it("keeps the email with its key and drops it on sign-out or server change", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "aitsev-email-"));
    const store = new SettingsStore(dir, enc);
    await store.save({ serverUrl: SERVER, apiKey: "aitsev_0123456789abcdef", email: "ada@example.com" });
    expect(await store.publicSettings()).toEqual({ serverUrl: SERVER, hasApiKey: true, email: "ada@example.com" });
    await store.save({ serverUrl: SERVER });
    expect((await store.publicSettings()).email).toBe("ada@example.com");
    await store.save({ serverUrl: "https://other.example.com" });
    expect((await store.publicSettings()).email).toBeUndefined();
    await store.save({ serverUrl: SERVER, apiKey: "aitsev_0123456789abcdef", email: "ada@example.com" });
    await store.save({ serverUrl: SERVER, apiKey: null });
    expect(await store.publicSettings()).toEqual({ serverUrl: SERVER, hasApiKey: false });
    await fs.rm(dir, { recursive: true, force: true });
  });
});
