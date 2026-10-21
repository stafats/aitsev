import { bridge } from "./api";
import type { AppContext, Route } from "./context";
import { h, mount } from "./dom";
import { connectionsView } from "./views/connections";
import { crmView } from "./views/crm";
import { jobView, mappingsView, mappingView } from "./views/mappings";
import { settingsView, tasksView } from "./views/simple";
import { welcomeView } from "./views/welcome";

const NAV: Array<[Route["name"], string]> = [
  ["connections", "Connections"],
  ["mappings", "Mappings"],
  ["crm", "CRM"],
  ["tasks", "Code tasks"],
];

/** The web app swaps the desktop Settings screen for an Account screen supplied by its entry point. */
export interface AppOptions {
  mode?: "desktop" | "web";
  accountView?: (root: HTMLElement, ctx: AppContext) => Promise<void>;
  signOut?: () => void;
}

const SECTION: Record<Route["name"], Route["name"]> = {
  connections: "connections",
  mappings: "mappings",
  mapping: "mappings",
  job: "mappings",
  crm: "crm",
  tasks: "tasks",
  settings: "settings",
  account: "account",
  welcome: "settings",
};

export function mountApp(container: HTMLElement, options: AppOptions = {}): AppContext {
  const web = options.mode === "web";
  const navItems: Array<[Route["name"], string]> = [...NAV, web ? ["account", "Account"] : ["settings", "Settings"]];
  let route: Route = { name: "connections" };
  let timers: number[] = [];
  const main = h("main", { id: "main" });
  const nav = h("nav", { "aria-label": "Sections" });
  const dialog = h("dialog", {}) as HTMLDialogElement;
  const shell = h("div", { className: "shell" }, nav, main);
  container.replaceChildren(shell, dialog);

  const ctx: AppContext = {
    navigate(next) {
      route = next;
      show();
    },
    refresh() {
      show();
    },
    every(ms, fn) {
      timers.push(window.setInterval(fn, ms));
    },
    confirm(title, message, confirmLabel = "Continue") {
      return new Promise((resolve) => {
        let settled = false;
        const finish = (value: boolean) => {
          if (settled) return;
          settled = true;
          resolve(value);
          if (dialog.open) dialog.close();
        };
        dialog.replaceChildren(
          h("h2", {}, title),
          h("p", {}, message),
          h("div", { className: "row" },
            h("button", { className: "secondary", onClick: () => finish(false) }, "Cancel"),
            h("button", { className: "danger", "data-confirm": "yes", onClick: () => finish(true) }, confirmLabel)),
        );
        dialog.onclose = () => finish(false);
        if (typeof dialog.showModal === "function") dialog.showModal();
        else dialog.setAttribute("open", "");
      });
    },
  };

  function renderNav(): void {
    mount(
      nav,
      h("div", { className: "brand" }, "Aitsev"),
      ...navItems.map(([name, label]) =>
        h("button", { "aria-current": SECTION[route.name] === name ? "page" : false, onClick: () => ctx.navigate({ name } as Route) }, label),
      ),
      web && options.signOut ? h("button", { className: "signout", onClick: options.signOut }, "Sign out") : null,
    );
  }

  function show(): void {
    for (const t of timers) window.clearInterval(t);
    timers = [];
    renderNav();
    shell.className = route.name === "welcome" ? "shell bare" : "shell";
    const view = h("div", {});
    main.replaceChildren(view);
    const r = route;
    const run =
      r.name === "connections" ? connectionsView(view, ctx)
      : r.name === "mappings" ? mappingsView(view, ctx)
      : r.name === "mapping" ? mappingView(view, ctx, r.id)
      : r.name === "job" ? jobView(view, ctx, r.id)
      : r.name === "crm" ? crmView(view, ctx)
      : r.name === "tasks" ? tasksView(view, ctx)
      : r.name === "account" && options.accountView ? options.accountView(view, ctx)
      : r.name === "welcome" ? welcomeView(view, ctx)
      : settingsView(view, ctx);
    run.catch((err: unknown) => {
      mount(view, h("div", { className: "banner error" }, err instanceof Error ? err.message : String(err)));
    });
  }

  if (web) {
    show();
    return ctx;
  }
  void bridge()
    .getSettings()
    .then((s) => {
      route = s.hasApiKey ? { name: "connections" } : { name: "welcome" };
      show();
      bridge().ready();
    });
  return ctx;
}

if (
  typeof document !== "undefined" &&
  document.getElementById("app") &&
  !document.body.dataset["web"] &&
  typeof window.aitsev !== "undefined"
) {
  mountApp(document.getElementById("app") as HTMLElement);
}
