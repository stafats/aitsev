/** Approve a sign-in request from the desktop app or an editor extension. */
import { api } from "../renderer/api";
import { errorBanner, infoBanner, messageOf } from "../renderer/context";
import { h, mount } from "../renderer/dom";
import type { Account } from "./auth";

interface DeviceInfo {
  user_code: string;
  client_name: string;
  status: string;
  expires_at: string;
}

export async function connectView(root: HTMLElement, account: Account, code: string | null, onDone: () => void): Promise<void> {
  let info: DeviceInfo | null = null;
  let error: string | null = code ? null : "This sign-in link is incomplete. Start the sign-in again from the app.";
  let result: "approved" | "denied" | null = null;
  let busy = false;

  if (code) {
    try {
      info = await api<DeviceInfo>("GET", `/v1/auth/device/${encodeURIComponent(code)}`);
      if (info.status !== "pending") error = "This sign-in was already handled. Start again from the app if needed.";
    } catch (err) {
      error = messageOf(err);
    }
  }

  async function decide(approve: boolean): Promise<void> {
    if (!info || busy) return;
    busy = true;
    render();
    try {
      await api("POST", "/v1/auth/device/decision", { user_code: info.user_code, approve });
      result = approve ? "approved" : "denied";
    } catch (err) {
      error = messageOf(err);
    }
    busy = false;
    render();
  }

  function render(): void {
    const pending = info && info.status === "pending" && !result && !error;
    mount(
      root,
      h("div", { className: "auth" },
        h("div", { className: "auth-card card" },
          h("div", { className: "brand" }, "Aitsev"),
          h("h1", {}, result === "approved" ? "You're signed in" : result === "denied" ? "Sign-in declined" : "Sign in to an app"),
          errorBanner(error),
          result === "approved"
            ? infoBanner(`${info?.client_name ?? "The app"} is now connected to ${account.email}. You can close this tab and return to it.`)
            : result === "denied"
              ? infoBanner("Nothing was connected. You can close this tab.")
              : null,
          pending && info
            ? [
                h("p", {}, h("strong", {}, info.client_name), " wants to use your Aitsev account ", h("strong", {}, account.email), "."),
                h("p", { className: "muted" }, "Check that the app shows this code:"),
                h("p", { className: "code-display", "aria-label": "Sign-in code" }, info.user_code),
                h("div", { className: "row" },
                  h("button", { className: "primary", disabled: busy, onClick: () => void decide(true) }, "Allow"),
                  h("button", { className: "secondary", disabled: busy, onClick: () => void decide(false) }, "Deny")),
              ]
            : null,
          h("div", { className: "auth-links" }, h("button", { type: "button", className: "link", onClick: onDone }, "Open the Aitsev web app")),
        )),
    );
  }
  render();
}
