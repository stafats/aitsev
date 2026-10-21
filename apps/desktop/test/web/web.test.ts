// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Bridge } from "../../src/renderer/api";
import { formatCount, usageRow } from "../../src/web/account";
import { parseHash } from "../../src/web/auth";
import { createWebBridge, errorText, PATH } from "../../src/web/bridge";
import { startWeb } from "../../src/web/main";

type Reply = { status: number; body?: unknown };
type Route = (method: string, path: string, body: unknown) => Reply;

interface Call {
  method: string;
  path: string;
  body: unknown;
  headers: Record<string, string>;
  credentials?: string;
}

function fakeFetch(route: Route) {
  const calls: Call[] = [];
  const impl = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ method: init?.method ?? "GET", path, body, headers: (init?.headers ?? {}) as Record<string, string>, credentials: init?.credentials });
    const reply = route(init?.method ?? "GET", path, body);
    return new Response(reply.body === undefined ? "" : JSON.stringify(reply.body), { status: reply.status });
  }) as typeof fetch;
  return { impl, calls };
}

async function settle(): Promise<void> {
  for (let i = 0; i < 12; i++) await new Promise((r) => setTimeout(r, 0));
}

function byText(selector: string, text: string): HTMLElement {
  const found = Array.from(document.querySelectorAll<HTMLElement>(selector)).find((el) => el.textContent?.trim().startsWith(text));
  if (!found) throw new Error(`No ${selector} "${text}". Page: ${document.body.textContent?.slice(0, 500)}`);
  return found;
}

function type(label: string, value: string): void {
  const lab = Array.from(document.querySelectorAll("label")).find((l) => l.textContent?.startsWith(label));
  if (!lab) throw new Error(`No field ${label}`);
  const input = document.getElementById(lab.getAttribute("for") ?? "") as HTMLInputElement;
  input.value = value;
  input.dispatchEvent(new Event("input", { bubbles: true }));
}

const ACCOUNT = {
  id: "a1", email: "ada@example.com", name: "Ada", email_verified: true, plan: "free", plan_name: "Free",
  is_admin: false, disabled: false, created_at: "2026-10-01T00:00:00Z",
};
const LIMIT = (used: number, limit: number | null) => ({ used, limit, remaining: limit === null ? null : Math.max(0, limit - used) });
const USAGE = { plan: "free", plan_name: "Free", period_start: "2026-10-01T00:00:00Z", tokens: LIMIT(250_000, 300_000), tasks: LIMIT(3, 20), records: LIMIT(0, 1000) };

const CATALOG = {
  plan: "free", premium_allowed: false, max_selected: 3, upgrade_url: "http://x/app/#/account",
  models: [
    { id: "ollama-cloud/glm-5", label: "GLM-5", tier: "standard", description: "Careful refactoring", weight: 1, available: true, allowed: true },
    { id: "anthropic/claude-opus-5-5", label: "Claude Opus 5.5", tier: "premium", description: "", weight: 10, available: true, allowed: false },
    { id: "openai/gpt-5", label: "GPT-5", tier: "premium", description: "", weight: 5, available: false, allowed: true },
  ],
};

let root: HTMLElement;
beforeEach(() => {
  document.body.innerHTML = '<div id="app"></div>';
  root = document.getElementById("app") as HTMLElement;
  window.history.replaceState(null, "", "/app/");
});

describe("web bridge", () => {
  it("sends the CSRF header and same-origin credentials and parses errors", async () => {
    const { impl, calls } = fakeFetch((_m, path) =>
      path === "/v1/tasks" ? { status: 200, body: [] } : { status: 422, body: { detail: [{ loc: ["body", "email"], msg: "Value error, Enter a valid email address" }] } });
    const bridge = createWebBridge({ fetchImpl: impl });
    expect(await bridge.request("GET", "/v1/tasks")).toEqual({ ok: true, status: 200, data: [] });
    expect(calls[0]?.headers["X-Aitsev-Client"]).toBe("web");
    expect(calls[0]?.credentials).toBe("same-origin");
    const bad = await bridge.request("POST", "/v1/auth/signup", { email: "x" });
    expect(bad.error).toBe("email: Enter a valid email address");
    expect(calls[1]?.headers["Content-Type"]).toBe("application/json");
  });

  it("blocks paths outside the API and reports network failures", async () => {
    const bridge = createWebBridge({ fetchImpl: (async () => { throw new TypeError("offline"); }) as typeof fetch });
    expect((await bridge.request("GET", "https://evil.example/")).error).toBe("Blocked request path");
    expect((await bridge.request("GET", "/v1/../admin")).error).toBe("Blocked request path");
    expect((await bridge.request("GET", "/v1/tasks")).error).toContain("Cannot reach");
    expect(PATH.test("/v1/jobs?mapping_id=abc&limit=5")).toBe(true);
    expect(PATH.test("/static/x.js")).toBe(false);
  });

  it("calls onUnauthorized for expired sessions but not for auth endpoints", async () => {
    const onUnauthorized = vi.fn();
    const { impl } = fakeFetch(() => ({ status: 401, body: { detail: "Invalid or expired credentials" } }));
    const bridge = createWebBridge({ fetchImpl: impl, onUnauthorized });
    await bridge.request("POST", "/v1/auth/login", {});
    expect(onUnauthorized).not.toHaveBeenCalled();
    const res = await bridge.request("GET", "/v1/tasks");
    expect(res.error).toBe("Invalid or expired credentials");
    expect(onUnauthorized).toHaveBeenCalledOnce();
  });

  it("downloads files and reads uploaded text", async () => {
    const bridge = createWebBridge({ fetchImpl: fakeFetch(() => ({ status: 200 })).impl });
    URL.createObjectURL = vi.fn(() => "blob:x");
    URL.revokeObjectURL = vi.fn();
    const clicks: string[] = [];
    const original = HTMLAnchorElement.prototype.click;
    HTMLAnchorElement.prototype.click = function (this: HTMLAnchorElement) { clicks.push(this.download); };
    expect(await bridge.saveFile("migrate a/b.py", "print(1)")).toEqual({ ok: true, path: "migrate_a_b.py" });
    expect(clicks).toEqual(["migrate_a_b.py"]);
    HTMLAnchorElement.prototype.click = original;

    const originalInput = HTMLInputElement.prototype.click;
    HTMLInputElement.prototype.click = function (this: HTMLInputElement) {
      const file = new File(['{"openapi":"3.0.0"}'], "spec.json", { type: "application/json" });
      Object.defineProperty(this, "files", { value: [file] });
      this.dispatchEvent(new Event("change"));
    };
    expect(await bridge.openTextFile()).toEqual({ ok: true, name: "spec.json", content: '{"openapi":"3.0.0"}' });
    HTMLInputElement.prototype.click = function (this: HTMLInputElement) { this.dispatchEvent(new Event("cancel")); };
    expect(await bridge.openTextFile()).toEqual({ ok: false, error: "cancelled" });
    HTMLInputElement.prototype.click = originalInput;

    expect(await bridge.saveSettings({ serverUrl: "x" })).toMatchObject({ ok: false });
    expect((await bridge.getSettings()).hasApiKey).toBe(true);
  });

  it("formats error details and counts", () => {
    expect(errorText(500, null)).toBe("Request failed (500)");
    expect(errorText(402, { detail: "Upgrade" })).toBe("Upgrade");
    expect(formatCount(300_000)).toBe("300k");
    expect(formatCount(5_000_000)).toBe("5M");
    expect(formatCount(1_500_000)).toBe("1.5M");
    expect(formatCount(999)).toBe("999");
    expect(usageRow("Tokens", LIMIT(300_000, 300_000)).querySelector(".meter")?.className).toContain("bad");
    expect(usageRow("Tokens", LIMIT(5, null)).textContent).toContain("unlimited");
  });

  it("parses email links", () => {
    expect(parseHash("#/reset?token=abcdefghijkl")).toEqual({ mode: "reset", token: "abcdefghijkl" });
    expect(parseHash("#/verify?token=<script>")).toEqual({ mode: "verify", token: null });
    expect(parseHash("#/nope")).toEqual({ mode: null, token: null });
  });
});

function webApp(route: Route) {
  const { impl, calls } = fakeFetch(route);
  const make = (onUnauthorized: () => void): Bridge => createWebBridge({ fetchImpl: impl, onUnauthorized });
  return { start: () => startWeb(root, make), calls };
}

describe("web app", () => {
  it("shows sign-in when signed out, then signs in into the app", async () => {
    let signedIn = false;
    const app = webApp((method, path, body) => {
      if (path === "/v1/account") return signedIn ? { status: 200, body: ACCOUNT } : { status: 401, body: { detail: "Missing API key" } };
      if (path === "/v1/auth/login") {
        const b = body as { password: string };
        if (b.password !== "correct horse battery") return { status: 401, body: { detail: "Wrong email or password" } };
        signedIn = true;
        return { status: 200, body: { account: ACCOUNT, token: "sess_x", expires_at: "2026-11-01T00:00:00Z" } };
      }
      if (method === "GET" && path === "/v1/connections") return { status: 200, body: [] };
      return { status: 200, body: [] };
    });
    await app.start();
    await settle();
    expect(root.querySelector("h1")?.textContent).toBe("Sign in to Aitsev");

    type("Email", "ada@example.com");
    type("Password", "wrong password!");
    byText("button", "Sign in").click();
    await settle();
    expect(root.querySelector('[role="alert"]')?.textContent).toBe("Wrong email or password");

    type("Password", "correct horse battery");
    (root.querySelector("form") as HTMLFormElement).dispatchEvent(new Event("submit", { cancelable: true }));
    await settle();
    expect(byText("nav button", "Account")).toBeTruthy();
    expect(root.querySelector("h1")?.textContent).toBe("Connections");
    expect(() => byText("nav button", "Settings")).toThrow();
  });

  it("signs up with matching passwords only", async () => {
    const app = webApp((_m, path) => {
      if (path === "/v1/account") return { status: 401, body: { detail: "Missing API key" } };
      if (path === "/v1/auth/signup") return { status: 201, body: { account: { ...ACCOUNT, email_verified: false }, token: "sess_x", expires_at: "x" } };
      return { status: 200, body: [] };
    });
    window.history.replaceState(null, "", "/app/#/signup");
    await app.start();
    await settle();
    expect(root.querySelector("h1")?.textContent).toBe("Create your Aitsev account");
    type("Email", "ada@example.com");
    type("Password (10+", "correct horse battery");
    type("Repeat", "different");
    byText("button", "Create account").click();
    await settle();
    expect(root.querySelector('[role="alert"]')?.textContent).toBe("The passwords do not match");
    type("Repeat", "correct horse battery");
    byText("button", "Create account").click();
    await settle();
    expect(app.calls.find((c) => c.path === "/v1/auth/signup")?.body).toMatchObject({ email: "ada@example.com" });
    expect(byText("nav button", "Account")).toBeTruthy();
  });

  it("verifies email links and resets passwords", async () => {
    const app = webApp((_m, path, body) => {
      if (path === "/v1/auth/verify") return (body as { token: string }).token === "goodtoken123" ? { status: 200, body: ACCOUNT } : { status: 400, body: { detail: "This link is invalid or has expired" } };
      if (path === "/v1/auth/reset-password") return { status: 200, body: ACCOUNT };
      if (path === "/v1/account") return { status: 401, body: { detail: "Missing API key" } };
      return { status: 200, body: [] };
    });
    window.history.replaceState(null, "", "/app/#/verify?token=badtoken1234");
    await app.start();
    await settle();
    expect(root.textContent).toContain("This link is invalid or has expired");

    window.history.replaceState(null, "", "/app/#/verify?token=goodtoken123");
    await app.start();
    await settle();
    expect(root.textContent).toContain("Email confirmed");

    window.history.replaceState(null, "", "/app/#/reset?token=resettoken12");
    await app.start();
    await settle();
    expect(root.querySelector("h1")?.textContent).toBe("Choose a new password");
    type("Password (10+", "a brand new passphrase");
    type("Repeat", "a brand new passphrase");
    byText("button", "Change password").click();
    await settle();
    expect(app.calls.find((c) => c.path === "/v1/auth/reset-password")?.body).toEqual({ token: "resettoken12", password: "a brand new passphrase" });
    expect(root.textContent).toContain("Password changed. Sign in with your new password.");
  });

  it("sends forgotten-password requests", async () => {
    const app = webApp((_m, path) =>
      path === "/v1/auth/forgot-password" ? { status: 202, body: { status: "If the address has an account, a reset link is on its way." } } : { status: 401, body: {} });
    await app.start();
    await settle();
    byText("button", "Forgot your password?").click();
    type("Email", "ada@example.com");
    byText("button", "Email me a reset link").click();
    await settle();
    expect(root.textContent).toContain("a reset link is on its way");
  });

  it("shows usage, manages API keys and signs out", async () => {
    let keys = [{ id: "k1", name: "Laptop", prefix: "aitsev_abcde", created_at: "2026-10-01T00:00:00Z", last_used_at: null }];
    const app = webApp((method, path, body) => {
      if (path === "/v1/account") return { status: 200, body: { ...ACCOUNT, email_verified: false } };
      if (path === "/v1/account/usage") return { status: 200, body: USAGE };
      if (path === "/v1/models/catalog") return { status: 200, body: CATALOG };
      if (path === "/v1/account/keys" && method === "GET") return { status: 200, body: keys };
      if (path === "/v1/account/keys" && method === "POST") {
        keys = [...keys, { id: "k2", name: (body as { name: string }).name, prefix: "aitsev_zzzzz", created_at: "2026-10-02T00:00:00Z", last_used_at: null }];
        return { status: 201, body: { ...keys[1], key: "aitsev_zzzzzSECRET" } };
      }
      if (path === "/v1/account/keys/k1" && method === "DELETE") {
        keys = keys.filter((k) => k.id !== "k1");
        return { status: 204 };
      }
      if (path === "/v1/auth/resend-verification") return { status: 202, body: { status: "sent" } };
      if (path === "/v1/auth/logout") return { status: 204 };
      return { status: 200, body: [] };
    });
    await app.start();
    await settle();
    byText("nav button", "Account").click();
    await settle();
    expect(root.querySelector("h1")?.textContent).toBe("Account");
    expect(root.textContent).toContain("250k of 300k");
    expect(root.querySelector(".meter.warn")).toBeTruthy();
    const models = root.querySelector(".models-list")?.textContent ?? "";
    expect(models).toContain("GLM-5");
    expect(models).toContain("Pro and above");
    expect(models).toContain("Coming soon");

    byText("button", "Send the link again").click();
    await settle();
    expect(root.textContent).toContain("We sent a new confirmation link");

    const name = root.querySelector('input[aria-label="New key name"]') as HTMLInputElement;
    name.value = "CI";
    name.dispatchEvent(new Event("input"));
    byText("button", "Create key").click();
    await settle();
    expect(root.querySelector("code.secret")?.textContent).toBe("aitsev_zzzzzSECRET");

    byText("button", "Revoke").click();
    await settle();
    (document.querySelector('[data-confirm="yes"]') as HTMLElement).click();
    await settle();
    expect(app.calls.some((c) => c.method === "DELETE" && c.path === "/v1/account/keys/k1")).toBe(true);
    expect(root.querySelector("main")?.textContent).not.toContain("Laptop");

    byText("nav button", "Sign out").click();
    await settle();
    expect(root.querySelector("h1")?.textContent).toBe("Sign in to Aitsev");
    expect(root.textContent).toContain("You are signed out.");
  });

  it("lets admins change plans and disable accounts", async () => {
    const other = { ...ACCOUNT, id: "a2", email: "bob@example.com", is_admin: false, last_login_at: null, usage: USAGE };
    const app = webApp((method, path) => {
      if (path === "/v1/account") return { status: 200, body: { ...ACCOUNT, is_admin: true } };
      if (path === "/v1/account/usage") return { status: 200, body: USAGE };
      if (path === "/v1/models/catalog") return { status: 200, body: CATALOG };
      if (path === "/v1/admin/accounts") return { status: 200, body: [{ ...ACCOUNT, is_admin: true, last_login_at: null, usage: USAGE }, other] };
      if (method === "PUT") return { status: 200, body: other };
      return { status: 200, body: [] };
    });
    await app.start();
    await settle();
    byText("nav button", "Account").click();
    await settle();
    const planSelect = root.querySelector('select[aria-label="Plan for bob@example.com"]') as HTMLSelectElement;
    planSelect.value = "pro";
    planSelect.dispatchEvent(new Event("change"));
    await settle();
    byText("button", "Disable").click();
    await settle();
    const puts = app.calls.filter((c) => c.method === "PUT");
    expect(puts.map((c) => [c.path, c.body])).toEqual([
      ["/v1/admin/accounts/a2", { plan: "pro" }],
      ["/v1/admin/accounts/a2", { disabled: true }],
    ]);
    expect(root.textContent).toContain("Updated bob@example.com.");
  });

  it("returns to sign-in when the session expires", async () => {
    let expired = false;
    const app = webApp((_m, path) => {
      if (path === "/v1/account") return { status: 200, body: ACCOUNT };
      if (expired) return { status: 401, body: { detail: "Invalid or expired credentials" } };
      return { status: 200, body: [] };
    });
    await app.start();
    await settle();
    expired = true;
    byText("nav button", "Mappings").click();
    await settle();
    expect(root.textContent).toContain("Your session ended. Sign in again.");
  });

  it("approves an app sign-in after signing in", async () => {
    let signedIn = false;
    const app = webApp((method, path) => {
      if (path === "/v1/account") return signedIn ? { status: 200, body: ACCOUNT } : { status: 401, body: { detail: "Missing API key" } };
      if (path === "/v1/auth/login") {
        signedIn = true;
        return { status: 200, body: { account: ACCOUNT, token: "sess_x", expires_at: "x" } };
      }
      if (path === "/v1/auth/device/BCDF-GHJK" && method === "GET") {
        return { status: 200, body: { user_code: "BCDF-GHJK", client_name: "Aitsev Desktop", status: "pending", expires_at: "x" } };
      }
      if (path === "/v1/auth/device/decision") return { status: 200, body: { user_code: "BCDF-GHJK", client_name: "Aitsev Desktop", status: "approved", expires_at: "x" } };
      return { status: 200, body: [] };
    });
    window.history.replaceState(null, "", "/app/#/connect?code=bcdf-ghjk");
    await app.start();
    await settle();
    expect(root.textContent).toContain("Sign in to connect the app");
    type("Email", "ada@example.com");
    type("Password", "correct horse battery");
    byText("button", "Sign in").click();
    await settle();
    expect(root.querySelector(".code-display")?.textContent).toBe("BCDF-GHJK");
    expect(root.textContent).toContain("Aitsev Desktop wants to use your Aitsev account ada@example.com");
    byText("button", "Allow").click();
    await settle();
    expect(app.calls.find((c) => c.path === "/v1/auth/device/decision")?.body).toEqual({ user_code: "BCDF-GHJK", approve: true });
    expect(root.querySelector("h1")?.textContent).toBe("You're signed in");
    byText("button", "Open the Aitsev web app").click();
    await settle();
    expect(byText("nav button", "Account")).toBeTruthy();
  });

  it("handles denied, used and broken sign-in links", async () => {
    const app = webApp((_m, path) => {
      if (path === "/v1/account") return { status: 200, body: ACCOUNT };
      if (path === "/v1/auth/device/BCDF-GHJK") return { status: 200, body: { user_code: "BCDF-GHJK", client_name: "VS Code", status: "pending", expires_at: "x" } };
      if (path === "/v1/auth/device/ZZZZ-ZZZZ") return { status: 200, body: { user_code: "ZZZZ-ZZZZ", client_name: "VS Code", status: "approved", expires_at: "x" } };
      if (path === "/v1/auth/device/decision") return { status: 200, body: {} };
      return { status: 404, body: { detail: "This sign-in code is invalid or expired" } };
    });
    window.history.replaceState(null, "", "/app/#/connect?code=BCDF-GHJK");
    await app.start();
    await settle();
    byText("button", "Deny").click();
    await settle();
    expect(root.querySelector("h1")?.textContent).toBe("Sign-in declined");

    window.history.replaceState(null, "", "/app/#/connect?code=ZZZZ-ZZZZ");
    await app.start();
    await settle();
    expect(root.textContent).toContain("already handled");

    window.history.replaceState(null, "", "/app/#/connect?code=QQQQ-QQQQ");
    await app.start();
    await settle();
    expect(root.textContent).toContain("invalid or expired");

    window.history.replaceState(null, "", "/app/#/connect?code=bad");
    await app.start();
    await settle();
    expect(root.textContent).toContain("link is incomplete");
  });
});
