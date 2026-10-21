/** Web app entry point: the desktop screens in the browser, behind an account sign-in. */
import { ApiError, api, type Bridge } from "../renderer/api";
import { mountApp } from "../renderer/app";
import { messageOf } from "../renderer/context";
import { accountView } from "./account";
import { type Account, authView, parseHash } from "./auth";
import { connectView } from "./connect";
import { createWebBridge } from "./bridge";

export async function startWeb(container: HTMLElement, makeBridge: (onUnauthorized: () => void) => Bridge = (cb) => createWebBridge({ onUnauthorized: cb })): Promise<void> {
  let signedIn = false;
  const showAuth = (notice: string | null = null, mode: "login" | "signup" = "login") => {
    signedIn = false;
    authView(container, { mode, notice }, enter);
  };
  window.aitsev = makeBridge(() => {
    if (signedIn) showAuth("Your session ended. Sign in again.");
  });

  let connectCode: string | null | undefined;

  function enter(account: Account): void {
    signedIn = true;
    if (connectCode !== undefined) {
      const code = connectCode;
      connectCode = undefined;
      void connectView(container, account, code, () => {
        history.replaceState(null, "", "#/");
        enter(account);
      });
      return;
    }
    const state = { account };
    mountApp(container, {
      mode: "web",
      accountView: accountView(state),
      signOut: () => {
        void api("POST", "/v1/auth/logout").finally(() => showAuth("You are signed out."));
      },
    });
  }

  const { mode, token } = parseHash(window.location.hash);
  let notice: string | null = null;
  if (mode === "verify") {
    try {
      if (!token) throw new Error("This confirmation link is incomplete.");
      await api("POST", "/v1/auth/verify", { token });
      notice = "Email confirmed. Thank you!";
    } catch (err) {
      notice = messageOf(err);
    }
    history.replaceState(null, "", "#/");
  } else if (mode === "reset") {
    authView(container, { mode: "reset", token }, enter);
    return;
  } else if (mode === "connect") {
    connectCode = token;
    notice = "Sign in to connect the app to your account.";
  } else if (mode === "forgot") {
    authView(container, { mode: "forgot" }, enter);
    return;
  }

  try {
    const account = await api<Account>("GET", "/v1/account");
    const connecting = connectCode !== undefined;
    enter(account);
    if (notice && !connecting) container.querySelector("main")?.prepend(Object.assign(document.createElement("div"), { className: "banner", textContent: notice }));
  } catch (err) {
    if (err instanceof ApiError && (err.status === 401 || err.status === 400)) {
      showAuth(notice, mode === "signup" ? "signup" : "login");
    } else {
      showAuth(messageOf(err));
    }
  }
}

if (typeof document !== "undefined" && document.body?.dataset["web"] && document.getElementById("app")) {
  void startWeb(document.getElementById("app") as HTMLElement);
}
