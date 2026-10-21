import { h } from "./dom";

export type Route =
  | { name: "connections" }
  | { name: "mappings" }
  | { name: "mapping"; id: string }
  | { name: "job"; id: string }
  | { name: "crm" }
  | { name: "tasks" }
  | { name: "settings" }
  | { name: "welcome" }
  | { name: "account" };

export interface AppContext {
  navigate(route: Route): void;
  /** Re-render the current view. */
  refresh(): void;
  /** Run a timer that is cleared automatically when the view changes. */
  every(ms: number, fn: () => void): void;
  /** Ask for confirmation in an in-app dialog. */
  confirm(title: string, message: string, confirmLabel?: string): Promise<boolean>;
}

export function errorBanner(message: string | null): HTMLElement | null {
  return message ? h("div", { className: "banner error", role: "alert" }, message) : null;
}

export function infoBanner(message: string | null): HTMLElement | null {
  return message ? h("div", { className: "banner", role: "status" }, message) : null;
}

export function messageOf(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}
