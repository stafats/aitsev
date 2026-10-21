// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Bridge } from "../../src/renderer/api";
import { mountApp } from "../../src/renderer/app";
import type { AppContext } from "../../src/renderer/context";
import { buildConnectionBody, PRESETS } from "../../src/renderer/views/connections";
import { specDetails } from "../../src/renderer/views/crm";
import { sourceLabel, stepsSummary } from "../../src/renderer/views/mappings";

type Handler = (method: string, path: string, body?: unknown) => { ok: boolean; status?: number; data?: unknown; error?: string };

interface FakeBridge extends Bridge {
  calls: Array<{ method: string; path: string; body?: unknown }>;
  readyCalls: number;
}

function fakeBridge(handler: Handler, hasApiKey = true): FakeBridge {
  const bridge: FakeBridge = {
    calls: [],
    readyCalls: 0,
    getSettings: async () => ({ serverUrl: "http://localhost:8000", hasApiKey }),
    saveSettings: async (input) => ({ ok: true, settings: { serverUrl: input.serverUrl, hasApiKey: Boolean(input.apiKey) || hasApiKey } }),
    request: async (method, path, body) => {
      bridge.calls.push({ method, path, body });
      const r = handler(method, path, body);
      return { ok: r.ok, status: r.status ?? (r.ok ? 200 : 400), data: r.data ?? null, error: r.error };
    },
    saveFile: async () => ({ ok: true, path: "/tmp/x.py" }),
    openTextFile: async () => ({ ok: true, name: "spec.json", content: '{"openapi":"3.0.0"}' }),
    ready: () => {
      bridge.readyCalls += 1;
    },
  };
  return bridge;
}

async function settle(): Promise<void> {
  for (let i = 0; i < 8; i++) await new Promise((r) => setTimeout(r, 0));
}

function byText(root: ParentNode, selector: string, text: string): HTMLElement {
  const found = Array.from(root.querySelectorAll<HTMLElement>(selector)).find((el) => el.textContent?.trim().startsWith(text));
  if (!found) throw new Error(`No ${selector} with text ${text}. Page: ${document.body.textContent?.slice(0, 400)}`);
  return found;
}

function setValue(el: Element, value: string, event: "input" | "change" = "input"): void {
  (el as HTMLInputElement).value = value;
  el.dispatchEvent(new Event(event, { bubbles: true }));
}

const CONNECTIONS = [
  { id: "c1", name: "Legacy CRM", base_url: "https://legacy.example/api", auth: { type: "bearer" }, has_credentials: true,
    spec_format: "openapi", environment: "unknown", entities: ["Customer"], discovered_at: "2026-10-21T00:00:00Z", last_error: null },
  { id: "c2", name: "D365 Sandbox", base_url: "https://org.crm.example/api/data/v9.2", auth: { type: "oauth2_client_credentials" },
    has_credentials: true, spec_format: "odata", environment: "sandbox", entities: ["contact"], discovered_at: null, last_error: null },
];

const MAPPING = {
  id: "m1", name: "Customer → contact", status: "draft", source_connection_id: "c1", source_entity: "Customer",
  target_connection_id: "c2", target_entity: "contact", version: 2, dry_run_passed: false, error: null,
  models_used: ["fake/m1", "fake/m2"], unmapped_targets: ["telephone1"],
  blocking_issues: ["1 mapping(s) still need review: firstname"],
  fields: [
    { target: "lastname", source: "full_name", sources: [], separator: " ", constant: null, steps: [{ op: "split", sep: " ", index: -1 }],
      confidence: 0.95, method: "agreed", status: "approved", reason: "2 models agree" },
    { target: "firstname", source: "full_name", sources: [], separator: " ", constant: null, steps: [{ op: "split", sep: " ", index: 0 }],
      confidence: 0.7, method: "llm", status: "proposed", reason: "First word" },
  ],
};

let root: HTMLElement;
beforeEach(() => {
  document.body.innerHTML = '<div id="root"></div>';
  root = document.getElementById("root") as HTMLElement;
});
afterEach(() => {
  vi.useRealTimers();
});

describe("helpers", () => {
  it("builds connection bodies per auth type", () => {
    const base = { name: " D365 ", base_url: "https://org/api", environment: "sandbox", pagination: "odata", page_size: "50", spec_format: "odata", spec_text: "" };
    const oauth = buildConnectionBody({ ...base, auth_type: "oauth2_client_credentials", token_url: "https://login/t", scope: "s", client_id: "id", client_secret: "sec" });
    expect(oauth).toMatchObject({ name: "D365", auth: { type: "oauth2_client_credentials", token_url: "https://login/t", scope: "s" },
      credentials: { client_id: "id", client_secret: "sec" }, pagination: { style: "odata", page_size: 50 } });
    expect(buildConnectionBody({ ...base, auth_type: "api_key", api_key: "k" })).toMatchObject({ auth: { type: "api_key", header_name: "X-API-Key" }, credentials: { api_key: "k" } });
    expect(buildConnectionBody({ ...base, auth_type: "basic", username: "u", password: "p" }).credentials).toEqual({ username: "u", password: "p" });
    expect(buildConnectionBody({ ...base, auth_type: "none" }).credentials).toEqual({});
  });

  it("summarises steps, sources and column specs", () => {
    expect(stepsSummary([])).toBe("copy");
    expect(stepsSummary([{ op: "split", sep: " ", index: 0 }, { op: "map_values", values: { A: 0, B: 1 } }, { op: "to_float" }]))
      .toBe('split " " [0] → map 2 values → to float');
    expect(sourceLabel({ ...MAPPING.fields[0]!, sources: ["first", "last"] } as never)).toBe("first + last");
    expect(sourceLabel({ ...MAPPING.fields[0]!, constant: "X" } as never)).toBe('constant "X"');
    const spec = { schema_name: "new_T", display_name: "T", description: "", type: "choice", required: "none", max_length: null,
      min_value: null, max_value: null, precision: null, options: [{ label: "A", value: null }, { label: "B", value: null }], true_label: "Yes", false_label: "No" };
    expect(specDetails(spec)).toBe("A / B");
    expect(specDetails({ ...spec, type: "decimal", options: [], min_value: 0 })).toBe("0 to …, 2 decimals");
  });
});

describe("app shell", () => {
  it("opens the welcome screen when not signed in and reports ready", async () => {
    const bridge = fakeBridge(() => ({ ok: true, data: [] }), false);
    window.aitsev = bridge;
    mountApp(root);
    await settle();
    expect(root.querySelector("h1")?.textContent).toBe("Welcome to Aitsev");
    expect(root.querySelector(".shell")?.className).toBe("shell bare");
    expect(bridge.readyCalls).toBe(1);
  });

  it("saves settings and tests the connection", async () => {
    const bridge = fakeBridge((_m, path) => {
      if (path === "/health") return { ok: true, data: { version: "0.4.0" } };
      return { ok: true, data: { roles: [{ available: true }, { available: false }] } };
    }, false);
    window.aitsev = bridge;
    mountApp(root);
    await settle();
    byText(root, "button", "Use a different server").click();
    await settle();
    expect(byText(root, "nav button", "Settings").getAttribute("aria-current")).toBe("page");
    setValue(root.querySelector('input[type="password"]') as Element, "key-0123456789abcdef");
    byText(root, "button", "Save").click();
    await settle();
    expect(root.textContent).toContain("Connected to Aitsev 0.4.0: 1 of 2 model roles available.");
  });

  it("shows server errors in place", async () => {
    window.aitsev = fakeBridge(() => ({ ok: false, status: 401, error: "Invalid API key" }));
    mountApp(root);
    await settle();
    expect(root.querySelector('[role="alert"]')?.textContent).toBe("Invalid API key");
  });
});

describe("connections", () => {
  it("lists, creates and discovers connections", async () => {
    const bridge = fakeBridge((method, path) => {
      if (method === "GET" && path === "/v1/connections") return { ok: true, data: CONNECTIONS };
      if (method === "POST" && path === "/v1/connections") return { ok: true, status: 201, data: { ...CONNECTIONS[1], name: "New" } };
      if (path.endsWith("/discover")) return { ok: true, data: { entities: [{}, {}] } };
      return { ok: false, error: `unexpected ${method} ${path}` };
    });
    window.aitsev = bridge;
    mountApp(root);
    await settle();
    expect(root.textContent).toContain("Legacy CRM");
    expect(root.textContent).toContain("sandbox");

    byText(root, "button", "Add connection").click();
    const inputs = root.querySelectorAll("input");
    setValue(inputs[0]!, "New");
    setValue(inputs[1]!, "https://org.crm.example/api/data/v9.2");
    const selects = root.querySelectorAll("select");
    setValue(selects[1]!, "oauth2_client_credentials", "change");
    expect(root.textContent).toContain("Client secret");
    byText(root, "button", "Load spec from file").click();
    await settle();
    byText(root, "button", "Save connection").click();
    await settle();
    const post = bridge.calls.find((c) => c.method === "POST" && c.path === "/v1/connections");
    expect(post?.body).toMatchObject({ name: "New", base_url: "https://org.crm.example/api/data/v9.2",
      auth: { type: "oauth2_client_credentials" }, spec_text: '{"openapi":"3.0.0"}' });
    expect(root.textContent).toContain("Saved New");

    const discoverButtons = Array.from(root.querySelectorAll("button")).filter((b) => b.textContent === "Discover");
    discoverButtons[1]!.click(); // the OData connection, never discovered
    await settle();
    const discover = bridge.calls.find((c) => c.path === "/v1/connections/c2/discover");
    expect(discover?.body).toEqual({ spec_path: "$metadata" });
    expect(root.textContent).toContain("found 2 entities");
  });
});

describe("mapping review", () => {
  it("edits decisions, saves, approves and starts a dry run", async () => {
    let mapping = structuredClone(MAPPING);
    const bridge = fakeBridge((method, path, body) => {
      if (path === "/v1/mappings" && method === "GET") return { ok: true, data: [mapping] };
      if (path === "/v1/connections") return { ok: true, data: CONNECTIONS };
      if (path === "/v1/mappings/m1" && method === "GET") return { ok: true, data: mapping };
      if (path === "/v1/mappings/m1" && method === "PUT") {
        mapping = { ...mapping, fields: (body as { fields: typeof mapping.fields }).fields, blocking_issues: [], version: 3 };
        return { ok: true, data: mapping };
      }
      if (path === "/v1/mappings/m1/approve") {
        mapping = { ...mapping, status: "approved" };
        return { ok: true, data: mapping };
      }
      if (path.startsWith("/v1/jobs?")) return { ok: true, data: [] };
      if (path === "/v1/connections/c1/schema") return { ok: true, data: { entities: [{ name: "Customer", fields: [{ name: "full_name" }, { name: "email" }] }] } };
      if (path === "/v1/jobs" && method === "POST") return { ok: true, data: { id: "j1", mapping_id: "m1", dry_run: true, status: "queued", stats: {}, errors: [], sample: [], error: null, created_at: "", finished_at: null } };
      if (path === "/v1/jobs/j1") return { ok: true, data: { id: "j1", mapping_id: "m1", dry_run: true, status: "succeeded", stats: { read: 7, valid: 7 }, errors: [], sample: [{ lastname: "Rao" }], error: null, created_at: "", finished_at: "" } };
      return { ok: false, error: `unexpected ${method} ${path}` };
    });
    window.aitsev = bridge;
    const ctx: AppContext = mountApp(root);
    await settle();
    ctx.navigate({ name: "mapping", id: "m1" });
    await settle();
    expect(root.textContent).toContain("Before you can approve");
    expect(byText(root, "button", "Approve mapping").hasAttribute("disabled")).toBe(true);

    setValue(root.querySelector('select[aria-label="Decision for firstname"]')!, "approved", "change");
    byText(root, "button", "Save changes").click();
    await settle();
    const put = bridge.calls.find((c) => c.method === "PUT");
    expect((put?.body as { fields: Array<{ status: string }> }).fields.map((f) => f.status)).toEqual(["approved", "approved"]);

    byText(root, "button", "Approve mapping").click();
    await settle();
    expect(root.textContent).toContain("Run a dry run first");
    expect(byText(root, "button", "Run migration").hasAttribute("disabled")).toBe(true);
    byText(root, "button", "Dry run").click();
    await settle();
    expect(bridge.calls.find((c) => c.path === "/v1/jobs" && c.method === "POST")?.body).toEqual({ mapping_id: "m1", dry_run: true });
    expect(root.querySelector("h1")?.textContent).toBe("Dry run");
    expect(root.textContent).toContain("Sample of transformed records");
  });
});

describe("Dynamics 365", () => {
  const change = {
    id: "ch1", connection_id: "c2", environment: "production", entity: "contact", request: "Track loyalty",
    publisher_prefix: "new", solution: null, status: "approved", version: 3, error: null, results: [],
    items: [{ spec: { schema_name: "new_Tier", display_name: "Tier", description: "", type: "choice", required: "none", max_length: null,
      min_value: null, max_value: null, precision: null, options: [{ label: "Bronze", value: null }, { label: "Gold", value: null }],
      true_label: "Yes", false_label: "No" }, include: true, agreed: true, proposed_by: ["a", "b"], note: "", payload: {} }],
  };

  it("needs explicit confirmation before applying to production", async () => {
    const bridge = fakeBridge((method, path, body) => {
      if (path === "/v1/connections") return { ok: true, data: [{ ...CONNECTIONS[1], environment: "production" }] };
      if (path.startsWith("/v1/crm/analyses")) return { ok: true, data: [] };
      if (path.startsWith("/v1/crm/changes?")) return { ok: true, data: [change] };
      if (path === "/v1/crm/changes/ch1/apply") return { ok: true, data: { ...change, status: "applying", ...(body as object) } };
      return { ok: false, error: `unexpected ${method} ${path}` };
    });
    window.aitsev = bridge;
    const ctx = mountApp(root);
    await settle();
    ctx.navigate({ name: "crm" });
    await settle();
    expect(root.textContent).toContain("Not marked as a sandbox");
    byText(root, "tr", "contact").click();
    expect(root.textContent).toContain("Bronze / Gold");

    byText(root, "button", "Apply").click();
    await settle();
    const dialog = document.querySelector("dialog") as HTMLDialogElement;
    expect(dialog.textContent).toContain('marked "production"');
    byText(dialog, "button", "Cancel").click();
    await settle();
    expect(bridge.calls.some((c) => c.path.endsWith("/apply"))).toBe(false);

    byText(root, "button", "Apply").click();
    await settle();
    (document.querySelector('dialog button[data-confirm="yes"]') as HTMLButtonElement).click();
    await settle();
    const apply = bridge.calls.find((c) => c.path === "/v1/crm/changes/ch1/apply");
    expect(apply?.body).toEqual({ confirm_production: true });
  });

  it("starts an analysis and shows the report", async () => {
    let analyses: unknown[] = [];
    const bridge = fakeBridge((method, path) => {
      if (path === "/v1/connections") return { ok: true, data: [CONNECTIONS[1]] };
      if (path.startsWith("/v1/crm/analyses?")) return { ok: true, data: analyses };
      if (path.startsWith("/v1/crm/changes?")) return { ok: true, data: [] };
      if (path === "/v1/crm/c2/analyses" && method === "POST") {
        analyses = [{ id: "a1", connection_id: "c2", entities: ["contact"], status: "done", error: null, created_at: "",
          report: [{ logical_name: "contact", display_name: "Contact", total_records: 20, sampled: 20,
            fields: [{ name: "telephone1", display_name: "Phone", type: "String", custom: false, required: false, fill_rate: 0.05 }],
            unused_custom_fields: [], low_fill_fields: ["telephone1"], required_but_empty: [], duplicate_groups: [],
            suggestions: ["Column(s) telephone1 are filled in under 10% of records"] }] }];
        return { ok: true, status: 202, data: analyses[0] };
      }
      return { ok: false, error: `unexpected ${method} ${path}` };
    });
    window.aitsev = bridge;
    const ctx = mountApp(root);
    await settle();
    ctx.navigate({ name: "crm" });
    await settle();
    setValue(root.querySelector("input")!, "Contact");
    byText(root, "button", "Analyse").click();
    await settle();
    expect(bridge.calls.find((c) => c.path === "/v1/crm/c2/analyses")?.body).toEqual({ entities: ["contact"], sample_size: 200 });
    expect(root.textContent).toContain("Contact (contact)");
    expect(root.textContent).toContain("filled in under 10%");
  });
});

describe("safety", () => {
  it("renders server text as text, never HTML", async () => {
    window.aitsev = fakeBridge((_m, path) =>
      path === "/v1/connections"
        ? { ok: true, data: [{ ...CONNECTIONS[0], name: '<img src=x onerror="alert(1)">' }] }
        : { ok: true, data: [] });
    mountApp(root);
    await settle();
    expect(root.querySelector("img")).toBeNull();
    expect(root.textContent).toContain('<img src=x onerror="alert(1)">');
    expect(root.textContent).not.toContain("null");
  });
});

describe("first run", () => {
  function desktopBridge(handler: Handler, opts: { hasApiKey?: boolean; email?: string; bundled?: boolean } = {}) {
    const base = fakeBridge(handler, opts.hasApiKey ?? false);
    let listener: ((r: { ok: boolean; email?: string; error?: string }) => void) | null = null;
    const installs: string[][] = [];
    const saved: unknown[] = [];
    let cancelled = 0;
    let hasKey = opts.hasApiKey ?? false;
    const bridge = Object.assign(base, {
      getSettings: async () => ({ serverUrl: "https://app.aitsev.com", hasApiKey: hasKey, email: hasKey ? opts.email : undefined }),
      saveSettings: async (input: { serverUrl: string; apiKey?: string | null }) => {
        saved.push(input);
        if (input.apiKey === null) hasKey = false;
        return { ok: true, settings: { serverUrl: input.serverUrl, hasApiKey: false } };
      },
      signIn: async () => ({ ok: true, userCode: "BCDF-GHJK", verificationUrl: "https://app.aitsev.com/app/#/connect?code=BCDF-GHJK" }),
      cancelSignIn: async () => { cancelled += 1; },
      reopenSignIn: async () => {},
      onSignInResult: (cb: (r: { ok: boolean; email?: string; error?: string }) => void) => {
        listener = cb;
        return () => { listener = null; };
      },
      listEditors: async () => ({
        bundled: opts.bundled ?? true,
        editors: [
          { id: "vscode", name: "Visual Studio Code", found: true },
          { id: "cursor", name: "Cursor", found: true },
          { id: "kiro", name: "Kiro", found: false },
        ],
      }),
      installEditors: async (ids: string[]) => {
        installs.push(ids);
        return ids.map((id) => (id === "cursor"
          ? { id, name: "Cursor", ok: false, error: "Extension host busy" }
          : { id, name: "Visual Studio Code", ok: true }));
      },
    });
    return { bridge, installs, saved, finish: (r: { ok: boolean; email?: string; error?: string }) => listener?.(r), cancelled: () => cancelled };
  }

  it("signs in through the browser and adds the extension to editors", async () => {
    const fake = desktopBridge(() => ({ ok: true, data: [] }));
    window.aitsev = fake.bridge;
    mountApp(root);
    await settle();
    byText(root, "button", "Sign in with your browser").click();
    await settle();
    expect(root.querySelector(".code-display")?.textContent).toBe("BCDF-GHJK");
    fake.finish({ ok: true, email: "ada@example.com" });
    await settle();
    expect(root.textContent).toContain("Signed in as ada@example.com.");
    const kiro = root.querySelector('input[data-editor="kiro"]') as HTMLInputElement;
    expect(kiro.disabled).toBe(true);
    const cursor = root.querySelector('input[data-editor="cursor"]') as HTMLInputElement;
    expect(cursor.checked).toBe(true);
    byText(root, "button", "Add to selected editors").click();
    await settle();
    expect(fake.installs).toEqual([["vscode", "cursor"]]);
    expect(root.textContent).toContain("✓ Visual Studio Code");
    expect(root.textContent).toContain("✗ Cursor: Extension host busy");
    expect(root.textContent).toContain("Restart the editor");
    byText(root, "button", "Start using Aitsev").click();
    await settle();
    expect(root.querySelector("h1")?.textContent).toBe("Connections");
    expect(root.querySelector(".shell")?.className).toBe("shell");
  });

  it("shows sign-in failures and can cancel", async () => {
    const fake = desktopBridge(() => ({ ok: true, data: [] }));
    window.aitsev = fake.bridge;
    mountApp(root);
    await settle();
    byText(root, "button", "Sign in with your browser").click();
    await settle();
    fake.finish({ ok: false, error: "The sign-in code expired. Try again." });
    await settle();
    expect(root.querySelector('[role="alert"]')?.textContent).toBe("The sign-in code expired. Try again.");
    byText(root, "button", "Sign in with your browser").click();
    await settle();
    byText(root, "button", "Cancel").click();
    await settle();
    expect(fake.cancelled()).toBe(1);
    expect(root.querySelector("h1")?.textContent).toBe("Welcome to Aitsev");
  });

  it("shows account, models and sign-out in settings", async () => {
    const fake = desktopBridge((_m, path) => {
      if (path === "/v1/account/usage") {
        return { ok: true, data: { plan: "pro", plan_name: "Pro", period_start: "2026-10-01T00:00:00Z",
          tokens: { used: 10, limit: 100, remaining: 90 }, tasks: { used: 1, limit: 5, remaining: 4 }, records: { used: 0, limit: null, remaining: null } } };
      }
      if (path === "/v1/models/catalog") {
        return { ok: true, data: { plan: "pro", premium_allowed: true, max_selected: 3, upgrade_url: "x",
          models: [{ id: "anthropic/claude-sonnet-5-5", label: "Claude Sonnet 5.5", tier: "premium", description: "", weight: 5, available: true, allowed: true }] } };
      }
      return { ok: true, data: [] };
    }, { hasApiKey: true, email: "ada@example.com", bundled: false });
    window.aitsev = fake.bridge;
    mountApp(root);
    await settle();
    byText(root, "nav button", "Settings").click();
    await settle();
    expect(root.textContent).toContain("Signed in as ada@example.com");
    expect(root.textContent).toContain("Pro plan");
    expect(root.textContent).toContain("Included · counts ×5");
    expect(root.textContent).toContain("ships with the installed app");
    byText(root, "button", "Sign out").click();
    await settle();
    expect(fake.saved).toEqual([{ serverUrl: "https://app.aitsev.com", apiKey: null }]);
    expect(root.querySelector("h1")?.textContent).toBe("Welcome to Aitsev");
  });
});

describe("CRM platforms", () => {
  it("builds refresh-token connections and keeps the chosen platform", () => {
    const zoho = buildConnectionBody({ ...PRESETS.zoho!.values, name: "Zoho", client_id: "id", client_secret: "sec", refresh_token: "r", environment: "sandbox", page_size: "100" } as Record<string, string>);
    expect(zoho).toMatchObject({
      platform: "zoho",
      auth: { type: "oauth2_refresh_token", token_url: "https://accounts.zoho.in/oauth/v2/token", token_prefix: "Zoho-oauthtoken" },
      credentials: { client_id: "id", client_secret: "sec", refresh_token: "r" },
    });
    expect(buildConnectionBody({ ...PRESETS.hubspot!.values, token: "pat" } as Record<string, string>)).toMatchObject({ platform: "hubspot", auth: { type: "bearer" }, credentials: { token: "pat" } });
    expect(buildConnectionBody({ auth_type: "none", platform: "auto" }).platform).toBeUndefined();
  });

  it("fills a connection from a preset", async () => {
    const bridge = fakeBridge((method, path) => (method === "POST" && path === "/v1/connections"
      ? { ok: true, status: 201, data: { ...CONNECTIONS[0], name: "Salesforce Sandbox" } }
      : { ok: true, data: [] }));
    window.aitsev = bridge;
    mountApp(root);
    await settle();
    byText(root, "button", "Add connection").click();
    await settle();
    (root.querySelector('[data-preset="salesforce"]') as HTMLElement).click();
    await settle();
    expect(root.textContent).toContain("connected app with the client credentials flow");
    byText(root, "button", "Save").click();
    await settle();
    const body = bridge.calls.find((c) => c.method === "POST" && c.path === "/v1/connections")?.body as Record<string, unknown>;
    expect(body).toMatchObject({
      name: "Salesforce Sandbox",
      platform: "salesforce",
      base_url: "https://<my-domain>.my.salesforce.com/services/data/v62.0",
      auth: { type: "oauth2_client_credentials", token_url: "https://<my-domain>.my.salesforce.com/services/oauth2/token" },
    });
  });

  it("uses API names as typed and hides Dynamics-only settings for Salesforce", async () => {
    const sf = { ...CONNECTIONS[1], id: "s1", name: "Salesforce", platform: "salesforce", environment: "sandbox" };
    const bridge = fakeBridge((method, path) => {
      if (path === "/v1/connections") return { ok: true, data: [sf] };
      if (method === "POST" && path.endsWith("/changes")) {
        return { ok: true, status: 202, data: { id: "ch9", connection_id: "s1", platform: "salesforce", environment: "sandbox", entity: "Contact", request: "Add a loyalty tier",
          publisher_prefix: "new", solution: null, status: "planning", items: [], results: [], version: 1, error: null } };
      }
      if (method === "POST") return { ok: true, status: 202, data: { id: "x", connection_id: "s1", entities: ["Contact"], status: "running", report: [], error: null, created_at: "" } };
      return { ok: true, data: [] };
    });
    window.aitsev = bridge;
    const ctx = mountApp(root);
    await settle();
    ctx.navigate({ name: "crm" });
    await settle();
    expect(root.querySelector("h1")?.textContent).toBe("CRM");
    expect(root.textContent).not.toContain("Publisher prefix");
    expect(root.textContent).toContain("Objects (API names");
    setValue(root.querySelector("input")!, "Contact, Account");
    byText(root, "button", "Analyse").click();
    await settle();
    expect(bridge.calls.find((c) => c.path === "/v1/crm/s1/analyses")?.body).toEqual({ entities: ["Contact", "Account"], sample_size: 200 });
    const textarea = root.querySelector("textarea") as HTMLTextAreaElement;
    setValue(root.querySelectorAll(".card input")[2] as Element, "Contact");
    setValue(textarea, "Add a loyalty tier");
    byText(root, "button", "Design fields").click();
    await settle();
    expect(bridge.calls.find((c) => c.path === "/v1/crm/s1/changes")?.body).toEqual({ entity: "Contact", request: "Add a loyalty tier" });
  });
});
