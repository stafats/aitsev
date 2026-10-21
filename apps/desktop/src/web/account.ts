/** Account screen for the web app: plan and usage, API keys, password, and administration. */
import { api } from "../renderer/api";
import { type AppContext, errorBanner, infoBanner, messageOf } from "../renderer/context";
import { badge, field, h, mount, select, when } from "../renderer/dom";
import type { Account } from "./auth";

interface Limit {
  used: number;
  limit: number | null;
  remaining: number | null;
}

export interface Usage {
  plan: string;
  plan_name: string;
  period_start: string;
  tokens: Limit;
  tasks: Limit;
  records: Limit;
}

interface ApiKey {
  id: string;
  name: string;
  prefix: string;
  created_at: string;
  last_used_at: string | null;
}

export interface CatalogModel {
  id: string;
  label: string;
  tier: string;
  description: string;
  weight: number;
  available: boolean;
  allowed: boolean;
}

export interface Catalog {
  plan: string | null;
  premium_allowed: boolean;
  max_selected: number;
  upgrade_url: string;
  models: CatalogModel[];
}

export function modelStatus(m: CatalogModel): HTMLElement {
  if (!m.allowed) return badge("Pro and above", "warn");
  if (!m.available) return badge("Coming soon");
  return badge(m.weight > 1 ? `Included · counts ×${m.weight}` : "Included", "good");
}

interface AdminAccount extends Account {
  last_login_at: string | null;
  usage: Usage;
}

const PLANS: Array<[string, string]> = [
  ["free", "Free"],
  ["pro", "Pro"],
  ["team", "Team"],
  ["enterprise", "Enterprise"],
];

export function formatCount(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(n % 1_000_000 ? 1 : 0)}M`;
  if (n >= 10_000) return `${Math.round(n / 1000)}k`;
  return n.toLocaleString("en-US");
}

export function usageRow(label: string, limit: Limit): HTMLElement {
  const pct = limit.limit ? Math.min(100, Math.round((limit.used / limit.limit) * 100)) : 0;
  const tone = pct >= 100 ? "bad" : pct >= 80 ? "warn" : "good";
  return h("div", { className: "usage" },
    h("div", { className: "usage-label" }, h("span", {}, label),
      h("span", { className: "muted" }, limit.limit === null ? `${formatCount(limit.used)} used · unlimited` : `${formatCount(limit.used)} of ${formatCount(limit.limit)}`)),
    limit.limit === null ? null : h("span", { className: `meter wide ${tone}`, role: "img", "aria-label": `${pct}% used` }, h("span", { style: `width:${pct}%` })));
}

export function accountView(state: { account: Account }) {
  return async function view(root: HTMLElement, ctx: AppContext): Promise<void> {
    let usage: Usage | null = null;
    let keys: ApiKey[] = [];
    let accounts: AdminAccount[] = [];
    let models: CatalogModel[] = [];
    let error: string | null = null;
    let notice: string | null = null;
    let newKey: string | null = null;
    let keyName = "";
    const pw = { current: "", next: "" };

    async function load(): Promise<void> {
      try {
        const [me, u, k, c] = await Promise.all([
          api<Account>("GET", "/v1/account"),
          api<Usage>("GET", "/v1/account/usage"),
          api<ApiKey[]>("GET", "/v1/account/keys"),
          api<Catalog>("GET", "/v1/models/catalog"),
        ]);
        models = c.models;
        state.account = me;
        usage = u;
        keys = k;
        accounts = me.is_admin ? await api<AdminAccount[]>("GET", "/v1/admin/accounts") : [];
      } catch (err) {
        error = messageOf(err);
      }
      render();
    }

    async function act(fn: () => Promise<void>): Promise<void> {
      error = null;
      notice = null;
      try {
        await fn();
      } catch (err) {
        error = messageOf(err);
      }
      await load();
    }

    const createKey = () =>
      act(async () => {
        const created = await api<{ key: string }>("POST", "/v1/account/keys", { name: keyName.trim() || "My key" });
        newKey = created.key;
        keyName = "";
      });

    const revokeKey = async (key: ApiKey) => {
      if (!(await ctx.confirm("Revoke API key?", `Apps using "${key.name}" (${key.prefix}…) will stop working.`, "Revoke"))) return;
      await act(async () => {
        await api("DELETE", `/v1/account/keys/${encodeURIComponent(key.id)}`);
        newKey = null;
      });
    };

    const changePassword = () =>
      act(async () => {
        await api("POST", "/v1/account/password", { current_password: pw.current, new_password: pw.next });
        pw.current = "";
        pw.next = "";
        notice = "Password changed.";
      });

    const resend = () =>
      act(async () => {
        await api("POST", "/v1/auth/resend-verification");
        notice = `We sent a new confirmation link to ${state.account.email}.`;
      });

    const adminUpdate = (target: AdminAccount, change: Record<string, unknown>) =>
      act(async () => {
        await api("PUT", `/v1/admin/accounts/${encodeURIComponent(target.id)}`, change);
        notice = `Updated ${target.email}.`;
      });

    function render(): void {
      const account = state.account;
      mount(root,
        h("h1", {}, "Account"),
        h("p", { className: "lead" }, account.email, " · ", badge(`${account.plan_name} plan`, "good"), account.is_admin ? [" ", badge("admin")] : null),
        errorBanner(error),
        infoBanner(notice),
        account.email_verified
          ? null
          : h("div", { className: "banner", role: "status" }, "Confirm your email address to start tasks and migrations. ",
              h("button", { className: "link", onClick: () => void resend() }, "Send the link again")),
        h("section", { className: "card" },
          h("h2", {}, "Usage this month"),
          usage
            ? [usageRow("Model tokens", usage.tokens), usageRow("Code tasks", usage.tasks), usageRow("Migrated records", usage.records),
               h("p", { className: "muted" }, `Resets on the 1st of each month (UTC). Since ${when(usage.period_start)}.`)]
            : h("p", { className: "muted" }, "Loading…")),
        models.length
          ? h("section", { className: "card" },
              h("h2", {}, "AI models"),
              h("p", { className: "muted" }, "Aitsev picks the best models for each task automatically. In the desktop app and editor extension you can also choose which models write your code."),
              h("ul", { className: "models-list" },
                ...models.map((m) => h("li", {}, h("span", {}, h("strong", {}, m.label), m.description ? h("span", { className: "muted" }, ` — ${m.description}`) : null), modelStatus(m)))))
          : null,
        h("section", { className: "card" },
          h("h2", {}, "API keys"),
          h("p", { className: "muted" }, "The desktop app and editor extensions sign in through your browser and create their own keys. Create one here only for scripts or manual setup."),
          newKey
            ? h("div", { className: "banner", role: "status" }, "Copy your new key now. It will not be shown again: ",
                h("code", { className: "secret", tabindex: "0" }, newKey))
            : null,
          keys.length
            ? h("table", {}, h("tr", {}, h("th", {}, "Name"), h("th", {}, "Key"), h("th", {}, "Last used"), h("th", {}, "")),
                ...keys.map((k) => h("tr", {}, h("td", {}, k.name), h("td", {}, h("code", {}, `${k.prefix}…`)), h("td", {}, when(k.last_used_at)),
                  h("td", {}, h("button", { className: "secondary", onClick: () => void revokeKey(k) }, "Revoke")))))
            : h("p", { className: "muted" }, "No keys yet."),
          h("div", { className: "row" },
            h("input", { type: "text", placeholder: "Key name, e.g. Work laptop", value: keyName, maxlength: "100",
              "aria-label": "New key name", onInput: (e: Event) => { keyName = (e.target as HTMLInputElement).value; } }),
            h("button", { className: "primary", onClick: () => void createKey() }, "Create key"))),
        h("section", { className: "card" },
          h("h2", {}, "Password"),
          field("Current password", h("input", { type: "password", autocomplete: "current-password", value: pw.current,
            onInput: (e: Event) => { pw.current = (e.target as HTMLInputElement).value; } })),
          field("New password (10+ characters)", h("input", { type: "password", autocomplete: "new-password", value: pw.next,
            onInput: (e: Event) => { pw.next = (e.target as HTMLInputElement).value; } })),
          h("button", { className: "secondary", onClick: () => void changePassword() }, "Change password")),
        account.is_admin
          ? h("section", { className: "card" },
              h("h2", {}, "Accounts"),
              h("p", { className: "muted" }, "Set plans after payment is confirmed, or disable an account to end its sessions."),
              h("table", {},
                h("tr", {}, h("th", {}, "Email"), h("th", {}, "Plan"), h("th", {}, "Tokens used"), h("th", {}, "Last sign-in"), h("th", {}, "")),
                ...accounts.map((a) => h("tr", {},
                  h("td", {}, a.email, a.disabled ? [" ", badge("disabled", "bad")] : null, a.is_admin ? [" ", badge("admin")] : null),
                  h("td", {}, select(PLANS, a.plan, { "aria-label": `Plan for ${a.email}`,
                    onChange: (e: Event) => void adminUpdate(a, { plan: (e.target as HTMLSelectElement).value }) })),
                  h("td", {}, formatCount(a.usage.tokens.used)),
                  h("td", {}, when(a.last_login_at)),
                  h("td", {}, a.id === account.id ? null
                    : h("button", { className: "secondary", onClick: () => void adminUpdate(a, { disabled: !a.disabled }) }, a.disabled ? "Enable" : "Disable"))))))
          : null,
      );
    }

    render();
    await load();
  };
}
