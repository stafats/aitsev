/** First run: sign in through the browser, then add Aitsev to the editors on this computer. */
import { bridge, type EditorInfo, type InstallResult } from "../api";
import { type AppContext, errorBanner, infoBanner, messageOf } from "../context";
import { h, mount } from "../dom";

/** Lists VS Code-family editors and installs the bundled extension into the ones picked. */
export async function editorsCard(holder: HTMLElement): Promise<void> {
  const b = bridge();
  if (!b.listEditors || !b.installEditors) return;
  let editors: EditorInfo[] = [];
  let bundled = false;
  let picked = new Set<string>();
  let results: InstallResult[] = [];
  let busy = false;
  let error: string | null = null;
  try {
    const listed = await b.listEditors();
    editors = listed.editors;
    bundled = listed.bundled;
    picked = new Set(editors.filter((e) => e.found).map((e) => e.id));
  } catch (err) {
    error = messageOf(err);
  }

  async function install(): Promise<void> {
    busy = true;
    render();
    try {
      results = await b.installEditors!([...picked]);
    } catch (err) {
      error = messageOf(err);
    }
    busy = false;
    render();
  }

  function render(): void {
    const found = editors.filter((e) => e.found);
    mount(holder,
      h("section", { className: "card" },
        h("h2", {}, "Add Aitsev to your editors"),
        errorBanner(error),
        !bundled
          ? h("p", { className: "muted" }, "The editor extension ships with the installed app. In a development build, run pnpm package:vscode first.")
          : found.length === 0
            ? h("p", { className: "muted" }, "No VS Code, Cursor, Kiro, Windsurf or VSCodium installation was found. Install one, then come back here.")
            : [
                h("p", { className: "muted" }, "Aitsev runs inside these editors as an extension."),
                h("div", { className: "editor-list" },
                  ...editors.map((e) => h("label", { className: e.found ? "editor" : "editor missing" },
                    h("input", { type: "checkbox", checked: picked.has(e.id), disabled: !e.found || busy, "data-editor": e.id,
                      onChange: (ev: Event) => {
                        if ((ev.target as HTMLInputElement).checked) picked.add(e.id);
                        else picked.delete(e.id);
                      } }),
                    ` ${e.name}`, e.found ? null : h("span", { className: "muted" }, " — not found")))),
                h("button", { className: "primary", disabled: busy, onClick: () => void install() }, busy ? "Adding…" : "Add to selected editors"),
              ],
        results.length
          ? h("ul", { className: "results" },
              ...results.map((r) => h("li", { className: r.ok ? "ok" : "bad" }, r.ok ? `✓ ${r.name}` : `✗ ${r.name}: ${r.error ?? "failed"}`)))
          : null,
        results.some((r) => r.ok)
          ? infoBanner("Done. Restart the editor, open the Aitsev view in its sidebar and choose Sign in with browser.")
          : null));
  }
  render();
}

export async function welcomeView(root: HTMLElement, ctx: AppContext): Promise<void> {
  const b = bridge();
  const settings = await b.getSettings();
  let step: "start" | "waiting" | "editors" = settings.hasApiKey ? "editors" : "start";
  let code: string | null = null;
  let email = settings.email ?? null;
  let error: string | null = null;
  let unsubscribe: (() => void) | null = null;

  async function signIn(): Promise<void> {
    if (!b.signIn) return;
    error = null;
    unsubscribe?.();
    unsubscribe = b.onSignInResult?.((result) => {
      unsubscribe?.();
      unsubscribe = null;
      if (!root.isConnected) return;
      if (result.ok) {
        email = result.email ?? null;
        step = "editors";
      } else {
        error = result.error ?? "Sign-in failed";
        step = "start";
      }
      render();
    }) ?? null;
    const started = await b.signIn();
    if (!started.ok) {
      unsubscribe?.();
      error = started.error ?? "Sign-in could not start";
      return render();
    }
    code = started.userCode ?? null;
    step = "waiting";
    render();
  }

  async function cancel(): Promise<void> {
    unsubscribe?.();
    unsubscribe = null;
    await b.cancelSignIn?.();
    step = "start";
    render();
  }

  function render(): void {
    const editors = h("div", {});
    mount(root,
      h("div", { className: "welcome" },
        h("div", { className: "welcome-brand" }, "Aitsev"),
        step === "start"
          ? [
              h("h1", {}, "Welcome to Aitsev"),
              h("p", { className: "lead" }, "Several AI models write, test and cross-check your code, integrations and migrations. Sign in to connect this app to your account."),
              errorBanner(error),
              h("button", { className: "primary big", onClick: () => void signIn() }, "Sign in with your browser"),
              h("p", { className: "muted" }, "New to Aitsev? You can create a free account on the page that opens."),
              h("p", { className: "muted small" }, `Server: ${settings.serverUrl} · `,
                h("button", { className: "link", onClick: () => ctx.navigate({ name: "settings" }) }, "Use a different server or an API key")),
            ]
          : step === "waiting"
            ? [
                h("h1", {}, "Finish signing in in your browser"),
                h("p", { className: "lead" }, "Check that the browser shows this code, then click Allow."),
                h("p", { className: "code-display", "aria-label": "Sign-in code" }, code ?? ""),
                h("p", { className: "muted" }, "Waiting for approval…"),
                h("div", { className: "row" },
                  h("button", { className: "secondary", onClick: () => void b.reopenSignIn?.() }, "Open the browser again"),
                  h("button", { className: "secondary", onClick: () => void cancel() }, "Cancel")),
              ]
            : [
                h("h1", {}, "You're all set"),
                h("p", { className: "lead" }, email ? `Signed in as ${email}.` : "Signed in."),
                editors,
                h("button", { className: "primary big", onClick: () => ctx.navigate({ name: "connections" }) }, "Start using Aitsev"),
              ]));
    if (step === "editors") void editorsCard(editors);
  }
  render();
}
