import { api, bridge, type Connection } from "../api";
import { type AppContext, errorBanner, infoBanner, messageOf } from "../context";
import { badge, field, h, select, when, mount } from "../dom";

const AUTH_TYPES: Array<[string, string]> = [
  ["none", "No auth"],
  ["api_key", "API key header"],
  ["bearer", "Bearer token"],
  ["basic", "Username and password"],
  ["oauth2_client_credentials", "OAuth2 client credentials (Dynamics 365, Salesforce)"],
  ["oauth2_refresh_token", "OAuth2 refresh token (Zoho CRM)"],
];

/** Ready-made settings for the CRMs Aitsev supports; the user fills in their own address and keys. */
export const PRESETS: Record<string, { label: string; values: Record<string, string>; help: string }> = {
  custom: { label: "Custom API", values: {}, help: "" },
  dynamics: {
    label: "Dynamics 365",
    values: { platform: "dynamics", base_url: "https://<org>.crm.dynamics.com/api/data/v9.2", auth_type: "oauth2_client_credentials",
      token_url: "https://login.microsoftonline.com/<tenant-id>/oauth2/v2.0/token", scope: "https://<org>.crm.dynamics.com/.default",
      pagination: "odata", spec_format: "odata" },
    help: "Register an app in Microsoft Entra ID, add it as an application user in the environment, then use its client ID and secret.",
  },
  salesforce: {
    label: "Salesforce",
    values: { platform: "salesforce", base_url: "https://<my-domain>.my.salesforce.com/services/data/v62.0", auth_type: "oauth2_client_credentials",
      token_url: "https://<my-domain>.my.salesforce.com/services/oauth2/token", scope: "", pagination: "none", spec_format: "samples", spec_text: "{}" },
    help: "Create a connected app with the client credentials flow enabled and a run-as user, then use its consumer key and secret.",
  },
  hubspot: {
    label: "HubSpot",
    values: { platform: "hubspot", base_url: "https://api.hubapi.com", auth_type: "bearer", pagination: "cursor", spec_format: "samples", spec_text: "{}" },
    help: "Create a private app in HubSpot (Settings → Integrations → Private apps) with CRM schema and object scopes, and paste its access token.",
  },
  zoho: {
    label: "Zoho CRM",
    values: { platform: "zoho", base_url: "https://www.zohoapis.in/crm/v6", auth_type: "oauth2_refresh_token",
      token_url: "https://accounts.zoho.in/oauth/v2/token", token_prefix: "Zoho-oauthtoken", pagination: "page", spec_format: "samples", spec_text: "{}" },
    help: "Create a self client in the Zoho API console (use .com / .eu instead of .in for other regions), generate a refresh token with ZohoCRM.settings.ALL and ZohoCRM.modules.ALL, then paste the client ID, secret and refresh token.",
  },
};

const SPEC_FORMATS: Array<[string, string]> = [
  ["openapi", "OpenAPI (JSON or YAML)"],
  ["odata", "OData $metadata (Dynamics 365, SAP)"],
  ["samples", "Sample records (JSON)"],
];

const PAGINATION: Array<[string, string]> = [
  ["none", "No paging"],
  ["page", "page + per_page"],
  ["offset", "offset + limit"],
  ["cursor", "cursor"],
  ["odata", "OData nextLink"],
];

export function buildConnectionBody(form: Record<string, string>): Record<string, unknown> {
  const auth: Record<string, unknown> = { type: form.auth_type };
  const credentials: Record<string, string> = {};
  if (form.auth_type === "api_key") {
    auth.header_name = form.header_name || "X-API-Key";
    credentials.api_key = form.api_key ?? "";
  } else if (form.auth_type === "bearer") {
    credentials.token = form.token ?? "";
  } else if (form.auth_type === "basic") {
    credentials.username = form.username ?? "";
    credentials.password = form.password ?? "";
  } else if (form.auth_type === "oauth2_client_credentials") {
    auth.token_url = form.token_url || null;
    auth.scope = form.scope || null;
    credentials.client_id = form.client_id ?? "";
    credentials.client_secret = form.client_secret ?? "";
  } else if (form.auth_type === "oauth2_refresh_token") {
    auth.token_url = form.token_url || null;
    auth.token_prefix = form.token_prefix || "Bearer";
    credentials.client_id = form.client_id ?? "";
    credentials.client_secret = form.client_secret ?? "";
    credentials.refresh_token = form.refresh_token ?? "";
  }
  const body: Record<string, unknown> = {
    name: form.name?.trim(),
    base_url: form.base_url?.trim(),
    environment: form.environment,
    auth,
    credentials,
    pagination: { style: form.pagination, page_size: Number(form.page_size || 100) },
    spec_format: form.spec_format,
    spec_text: form.spec_text ?? "",
  };
  if (form.platform && form.platform !== "auto") body.platform = form.platform;
  return body;
}

export async function connectionsView(root: HTMLElement, ctx: AppContext): Promise<void> {
  let error: string | null = null;
  let notice: string | null = null;
  let connections: Connection[] = [];
  let showForm = false;

  const values: Record<string, string> = {
    name: "",
    base_url: "",
    environment: "unknown",
    auth_type: "none",
    header_name: "X-API-Key",
    pagination: "none",
    page_size: "100",
    spec_format: "openapi",
    spec_text: "",
    platform: "auto",
  };
  let preset = "custom";

  function applyPreset(id: string): void {
    preset = id;
    const chosen = PRESETS[id];
    if (!chosen) return;
    Object.assign(values, { platform: "auto", ...chosen.values });
    if (!values.name) values.name = chosen.label === "Custom API" ? "" : `${chosen.label} Sandbox`;
    render();
  }

  async function load(): Promise<void> {
    try {
      connections = await api<Connection[]>("GET", "/v1/connections");
      error = null;
    } catch (err) {
      error = messageOf(err);
    }
    render();
  }

  function input(name: string, type = "text", placeholder = ""): HTMLInputElement {
    return h("input", {
      type,
      value: values[name] ?? "",
      placeholder,
      autocomplete: "off",
      onInput: (e: Event) => {
        values[name] = (e.target as HTMLInputElement).value;
      },
    }) as HTMLInputElement;
  }

  function choose(name: string, options: Array<[string, string]>, rerender = false): HTMLSelectElement {
    return select(options, values[name] ?? "", {
      onChange: (e: Event) => {
        values[name] = (e.target as HTMLSelectElement).value;
        if (rerender) render();
      },
    });
  }

  function credentialFields(): HTMLElement[] {
    switch (values.auth_type) {
      case "api_key":
        return [field("Header name", input("header_name")), field("API key", input("api_key", "password"))];
      case "bearer":
        return [field("Token", input("token", "password"))];
      case "basic":
        return [field("Username", input("username")), field("Password", input("password", "password"))];
      case "oauth2_refresh_token":
        return [
          field("Token URL", input("token_url", "url", "https://accounts.zoho.com/oauth/v2/token")),
          field("Token prefix", input("token_prefix", "text", "Zoho-oauthtoken")),
          field("Client ID", input("client_id")),
          field("Client secret", input("client_secret", "password")),
          field("Refresh token", input("refresh_token", "password")),
        ];
      case "oauth2_client_credentials":
        return [
          field("Token URL", input("token_url", "url", "https://login.microsoftonline.com/<tenant>/oauth2/v2.0/token")),
          field("Scope", input("scope", "text", "https://<org>.crm.dynamics.com/.default")),
          field("Client ID", input("client_id")),
          field("Client secret", input("client_secret", "password")),
        ];
      default:
        return [];
    }
  }

  async function save(): Promise<void> {
    try {
      const created = await api<Connection>("POST", "/v1/connections", buildConnectionBody(values));
      notice = `Saved ${created.name}. Run Discover to read its entities.`;
      showForm = false;
      for (const key of ["name", "base_url", "api_key", "token", "username", "password", "client_id", "client_secret", "refresh_token", "spec_text"]) {
        values[key] = "";
      }
      await load();
    } catch (err) {
      error = messageOf(err);
      render();
    }
  }

  async function loadFile(): Promise<void> {
    const file = await bridge().openTextFile();
    if (file.ok && file.content !== undefined) {
      values.spec_text = file.content;
      notice = `Loaded ${file.name}`;
      render();
    } else if (!file.ok && file.error && file.error !== "cancelled") {
      error = file.error;
      render();
    }
  }

  async function discover(conn: Connection): Promise<void> {
    // OData services publish their own $metadata; fetch it the first time.
    const specPath = conn.spec_format === "odata" && !conn.discovered_at ? "$metadata" : null;
    try {
      const schema = await api<{ entities: unknown[] }>("POST", `/v1/connections/${conn.id}/discover`, {
        spec_path: specPath,
      });
      notice = `${conn.name}: found ${schema.entities.length} entities.`;
      error = null;
    } catch (err) {
      error = messageOf(err);
    }
    await load();
  }

  async function remove(conn: Connection): Promise<void> {
    if (!(await ctx.confirm("Delete connection", `Delete ${conn.name}? Its stored credentials are removed.`, "Delete"))) {
      return;
    }
    try {
      await api("DELETE", `/v1/connections/${conn.id}`);
      notice = `Deleted ${conn.name}.`;
    } catch (err) {
      error = messageOf(err);
    }
    await load();
  }

  function render(): void {
    const envTone = (env: string) => (env === "sandbox" ? "good" : env === "production" ? "bad" : "warn");
    const form = showForm
      ? h(
          "div",
          { className: "card" },
          h("h2", {}, "New connection"),
          h("div", { className: "row presets", role: "group", "aria-label": "Start from" },
            ...Object.entries(PRESETS).map(([id, p]) =>
              h("button", { className: id === preset ? "primary" : "secondary", "data-preset": id, onClick: () => applyPreset(id) }, p.label))),
          PRESETS[preset]?.help ? h("p", { className: "muted" }, PRESETS[preset]!.help) : null,
          h(
            "div",
            { className: "grid2" },
            field("Name", input("name", "text", "Dynamics 365 Sandbox")),
            field("Base URL", input("base_url", "url", "https://org.crm.dynamics.com/api/data/v9.2")),
            field("Environment", choose("environment", [["sandbox", "Sandbox"], ["production", "Production"], ["unknown", "Unknown (treated as production)"]])),
            field("Authentication", choose("auth_type", AUTH_TYPES, true)),
            ...credentialFields(),
            field("Paging", choose("pagination", PAGINATION)),
            field("Page size", input("page_size", "number")),
            field("Spec format", choose("spec_format", SPEC_FORMATS)),
          ),
          field(
            values.spec_format === "odata" ? "Spec (optional: leave empty to fetch $metadata from the server)" : "Spec",
            h("textarea", {
              value: values.spec_text,
              onInput: (e: Event) => {
                values.spec_text = (e.target as HTMLTextAreaElement).value;
              },
            }),
          ),
          h(
            "div",
            { className: "row" },
            h("button", { className: "primary", onClick: () => void save() }, "Save connection"),
            h("button", { className: "secondary", onClick: () => void loadFile() }, "Load spec from file…"),
            h("button", { className: "secondary", onClick: () => { showForm = false; render(); } }, "Cancel"),
          ),
          h("p", { className: "muted" }, "Credentials are encrypted on the server and never shown again."),
        )
      : null;

    mount(root, 
      h("h1", {}, "Connections"),
      h("p", { className: "lead" }, "The systems you move data between. Connect, then discover their entities."),
      errorBanner(error),
      infoBanner(notice),
      form,
      showForm ? null : h("div", { className: "row" }, h("button", { className: "primary", onClick: () => { showForm = true; render(); } }, "Add connection")),
      h("h2", {}, `Saved connections (${connections.length})`),
      connections.length
        ? h(
            "table",
            {},
            h("tr", {}, ...["Name", "Environment", "Spec", "Entities", "Discovered", ""].map((t) => h("th", {}, t))),
            ...connections.map((c) =>
              h(
                "tr",
                {},
                h("td", {}, h("strong", {}, c.name), h("div", { className: "muted" }, c.base_url), c.last_error ? h("div", { className: "badge bad" }, c.last_error.slice(0, 120)) : null),
                h("td", {}, badge(c.environment, envTone(c.environment))),
                h("td", {}, c.platform && c.platform !== "dynamics" ? PRESETS[c.platform]?.label ?? c.platform : c.spec_format),
                h("td", {}, c.entities.length ? c.entities.slice(0, 6).join(", ") + (c.entities.length > 6 ? "…" : "") : h("span", { className: "muted" }, "none yet")),
                h("td", {}, when(c.discovered_at)),
                h(
                  "td",
                  {},
                  h("div", { className: "row" },
                    h("button", { className: "secondary", onClick: () => void discover(c) }, "Discover"),
                    h("button", { className: "secondary", onClick: () => void remove(c) }, "Delete")),
                ),
              ),
            ),
          )
        : h("p", { className: "muted" }, "No connections yet."),
    );
  }

  render();
  await load();
}
