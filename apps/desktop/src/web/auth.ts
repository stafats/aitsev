/** Sign-in, sign-up, forgotten password, password reset and email verification screens. */
import { api } from "../renderer/api";
import { errorBanner, infoBanner, messageOf } from "../renderer/context";
import { field, h, mount } from "../renderer/dom";

export type AuthMode = "login" | "signup" | "forgot" | "reset" | "verify";
export type HashMode = AuthMode | "connect";

export interface Account {
  id: string;
  email: string;
  name: string;
  email_verified: boolean;
  plan: string;
  plan_name: string;
  is_admin: boolean;
  disabled: boolean;
  created_at: string;
}

/** Reads "#/reset?token=..." style links from account emails. */
export function parseHash(hash: string): { mode: HashMode | null; token: string | null } {
  const match = /^#\/(login|signup|forgot|reset|verify|connect)(?:\?(.*))?$/.exec(hash);
  if (!match) return { mode: null, token: null };
  const params = new URLSearchParams(match[2] ?? "");
  if (match[1] === "connect") {
    const code = (params.get("code") ?? "").toUpperCase();
    return { mode: "connect", token: /^[A-Z]{4}-[A-Z]{4}$/.test(code) ? code : null };
  }
  const token = params.get("token");
  return { mode: match[1] as AuthMode, token: token && /^[A-Za-z0-9_-]{10,200}$/.test(token) ? token : null };
}

export function authView(
  root: HTMLElement,
  start: { mode: AuthMode; token?: string | null; notice?: string | null },
  onSignedIn: (account: Account) => void,
): void {
  let mode: AuthMode = start.mode;
  const token = start.token ?? null;
  const values = { email: "", password: "", confirm: "", name: "" };
  let error: string | null = null;
  let notice: string | null = start.notice ?? null;
  let busy = false;

  function go(next: AuthMode): void {
    mode = next;
    error = null;
    notice = null;
    values.password = "";
    values.confirm = "";
    if (typeof history !== "undefined") history.replaceState(null, "", `#/${next}`);
    render();
  }

  async function submit(event: Event): Promise<void> {
    event.preventDefault();
    if (busy) return;
    error = null;
    if ((mode === "signup" || mode === "reset") && values.password !== values.confirm) {
      error = "The passwords do not match";
      return render();
    }
    busy = true;
    render();
    try {
      if (mode === "login" || mode === "signup") {
        const body = mode === "login"
          ? { email: values.email, password: values.password }
          : { email: values.email, password: values.password, name: values.name };
        const result = await api<{ account: Account }>("POST", `/v1/auth/${mode}`, body);
        busy = false;
        if (typeof history !== "undefined") history.replaceState(null, "", "#/");
        onSignedIn(result.account);
        return;
      }
      if (mode === "forgot") {
        const result = await api<{ status: string }>("POST", "/v1/auth/forgot-password", { email: values.email });
        notice = result.status;
      } else if (mode === "reset") {
        await api("POST", "/v1/auth/reset-password", { token, password: values.password });
        busy = false;
        go("login");
        notice = "Password changed. Sign in with your new password.";
        return render();
      }
    } catch (err) {
      error = messageOf(err);
    }
    busy = false;
    render();
  }

  function input(key: keyof typeof values, type: string, autocomplete: string): HTMLElement {
    return h("input", {
      type,
      value: values[key],
      autocomplete,
      required: key !== "name",
      onInput: (e: Event) => {
        values[key] = (e.target as HTMLInputElement).value;
      },
    });
  }

  function link(label: string, next: AuthMode): HTMLElement {
    return h("button", { type: "button", className: "link", onClick: () => go(next) }, label);
  }

  function render(): void {
    const titles: Record<AuthMode, string> = {
      login: "Sign in to Aitsev",
      signup: "Create your Aitsev account",
      forgot: "Reset your password",
      reset: "Choose a new password",
      verify: "Confirm your email",
    };
    const fields: HTMLElement[] = [];
    if (mode === "signup") fields.push(field("Name (optional)", input("name", "text", "name")));
    if (mode !== "reset" && mode !== "verify") fields.push(field("Email", input("email", "email", "email")));
    if (mode === "login") fields.push(field("Password", input("password", "password", "current-password")));
    if (mode === "signup" || mode === "reset") {
      fields.push(field("Password (10+ characters)", input("password", "password", "new-password")));
      fields.push(field("Repeat password", input("confirm", "password", "new-password")));
    }
    const label = { login: "Sign in", signup: "Create account", forgot: "Email me a reset link", reset: "Change password", verify: "" }[mode];
    const invalidReset = mode === "reset" && !token;
    mount(
      root,
      h("div", { className: "auth" },
        h("div", { className: "auth-card card" },
          h("div", { className: "brand" }, "Aitsev"),
          h("h1", {}, titles[mode]),
          errorBanner(invalidReset ? "This reset link is incomplete. Ask for a new one." : error),
          infoBanner(notice),
          mode === "verify"
            ? null
            : h("form", { onSubmit: (e: Event) => void submit(e), novalidate: true },
                ...fields,
                h("button", { className: "primary", type: "submit", disabled: busy || invalidReset }, busy ? "Please wait…" : label)),
          h("div", { className: "auth-links" },
            mode === "login" ? [link("Create an account", "signup"), link("Forgot your password?", "forgot")] : link("Back to sign in", "login")),
        )),
    );
  }
  render();
}
