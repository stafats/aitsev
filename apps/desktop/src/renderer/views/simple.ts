import { api, bridge, type TaskSummary } from "../api";
import { type Catalog, type CatalogModel, modelStatus, type Usage, usageRow } from "../../web/account";
import { editorsCard } from "./welcome";
import { type AppContext, errorBanner, infoBanner, messageOf } from "../context";
import { badge, field, h, toneFor, when, mount } from "../dom";

export async function tasksView(root: HTMLElement, ctx: AppContext): Promise<void> {
  let tasks: TaskSummary[] = [];
  let error: string | null = null;
  async function load(): Promise<void> {
    try {
      tasks = await api<TaskSummary[]>("GET", "/v1/tasks?limit=50");
      error = null;
    } catch (err) {
      error = messageOf(err);
    }
    render();
  }
  function render(): void {
    mount(root, 
      h("h1", {}, "Code tasks"),
      h("p", { className: "lead" }, "Coding tasks are submitted, reviewed and applied from the VS Code extension. This is a read-only overview."),
      errorBanner(error),
      tasks.length
        ? h("table", {}, h("tr", {}, h("th", {}, "Task"), h("th", {}, "Status"), h("th", {}, "Updated")),
            ...tasks.map((t) => h("tr", {}, h("td", {}, t.title, t.error ? h("div", { className: "muted" }, t.error.slice(0, 160)) : null),
              h("td", {}, badge(t.status.replace("_", " "), toneFor(t.status))), h("td", {}, when(t.updated_at)))))
        : h("p", { className: "muted" }, "No tasks yet."),
    );
  }
  render();
  await load();
  ctx.every(5000, () => void load());
}

export async function settingsView(root: HTMLElement, ctx: AppContext): Promise<void> {
  const settings = await bridge().getSettings();
  let serverUrl = settings.serverUrl;
  let apiKey = "";
  let hasKey = settings.hasApiKey;
  const email = settings.email ?? null;
  let error: string | null = null;
  let notice: string | null = null;
  let usage: Usage | null = null;
  let models: CatalogModel[] = [];
  const editors = h("div", {});

  async function loadAccount(): Promise<void> {
    if (!hasKey) return;
    const [u, c] = await Promise.allSettled([
      api<Usage>("GET", "/v1/account/usage"),
      api<Catalog>("GET", "/v1/models/catalog"),
    ]);
    usage = u.status === "fulfilled" ? u.value : null;
    models = c.status === "fulfilled" ? c.value.models : [];
  }

  async function save(): Promise<void> {
    const result = await bridge().saveSettings({ serverUrl, apiKey: apiKey || undefined });
    if (result.ok && result.settings) {
      hasKey = result.settings.hasApiKey;
      serverUrl = result.settings.serverUrl;
      apiKey = "";
      error = null;
      notice = "Saved.";
      await test();
      return;
    }
    error = result.error ?? "Could not save";
    render();
  }

  async function test(): Promise<void> {
    try {
      const health = await api<{ version: string }>("GET", "/health");
      const roles = await api<{ roles: Array<{ available: boolean }> }>("GET", "/v1/models");
      const available = roles.roles.filter((r) => r.available).length;
      notice = `Connected to Aitsev ${health.version}: ${available} of ${roles.roles.length} model roles available.`;
      error = available === 0 ? "The server has no models available. Check its provider keys (OLLAMA_API_KEY)." : null;
    } catch (err) {
      error = messageOf(err);
    }
    render();
  }

  async function signOut(): Promise<void> {
    await bridge().saveSettings({ serverUrl, apiKey: null });
    ctx.navigate({ name: "welcome" });
  }

  function render(): void {
    mount(root,
      h("h1", {}, "Settings"),
      errorBanner(error),
      infoBanner(notice),
      h("section", { className: "card" },
        h("h2", {}, "Account"),
        hasKey
          ? [
              h("p", {}, email ? `Signed in as ${email}` : "Connected with an API key", usage ? [" · ", badge(`${usage.plan_name} plan`, "good")] : null),
              usage ? [usageRow("Model tokens", usage.tokens), usageRow("Code tasks", usage.tasks), usageRow("Migrated records", usage.records)] : null,
              h("button", { className: "secondary", onClick: () => void signOut() }, "Sign out"),
            ]
          : [
              h("p", { className: "muted" }, "Not signed in."),
              bridge().signIn ? h("button", { className: "primary", onClick: () => ctx.navigate({ name: "welcome" }) }, "Sign in with your browser") : null,
            ]),
      models.length
        ? h("section", { className: "card" },
            h("h2", {}, "AI models"),
            h("p", { className: "muted" }, "Aitsev routes every task across several models. Choose which ones write your code in the editor extension (Aitsev: Choose models)."),
            h("ul", { className: "models-list" },
              ...models.map((m) => h("li", {}, h("span", {}, h("strong", {}, m.label), m.description ? h("span", { className: "muted" }, ` — ${m.description}`) : null), modelStatus(m)))))
        : null,
      editors,
      h("details", { className: "card", open: !hasKey && !bridge().signIn },
        h("summary", {}, "Advanced: server and API key"),
        h("p", { className: "muted" }, "Your API key is stored encrypted by your operating system and only used by the app's background process."),
        field("Server URL", h("input", { type: "url", value: serverUrl, onInput: (e: Event) => { serverUrl = (e.target as HTMLInputElement).value; } })),
        field(hasKey ? "API key (saved — enter a new one to replace it)" : "API key",
          h("input", { type: "password", value: apiKey, autocomplete: "off", onInput: (e: Event) => { apiKey = (e.target as HTMLInputElement).value; } })),
        h("div", { className: "row" },
          h("button", { className: "primary", onClick: () => void save() }, "Save"),
          h("button", { className: "secondary", onClick: () => void test() }, "Test connection"))),
    );
    void editorsCard(editors);
  }
  render();
  await loadAccount();
  render();
}
