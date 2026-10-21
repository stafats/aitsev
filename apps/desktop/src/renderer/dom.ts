/** Tiny DOM helpers. All text is set with text nodes, never innerHTML. */

type Child = Node | string | number | null | undefined | false | Child[];
type Attrs = Record<string, unknown>;

export function h(tag: string, attrs: Attrs = {}, ...children: Child[]): HTMLElement {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "className") el.className = String(value);
    else if (key === "style") el.style.cssText = String(value); // CSSOM: allowed by the CSP
    else if (key.startsWith("on") && typeof value === "function") {
      el.addEventListener(key.slice(2).toLowerCase(), value as EventListener);
    } else if (key === "value" && "value" in el) (el as HTMLInputElement).value = String(value);
    else if (key === "checked" || key === "disabled" || key === "selected" || key === "required") {
      (el as unknown as Record<string, boolean>)[key] = Boolean(value);
    } else el.setAttribute(key, String(value));
  }
  append(el, children);
  return el;
}

function append(el: HTMLElement, children: Child[]): void {
  for (const child of children) {
    if (child === null || child === undefined || child === false) continue;
    if (Array.isArray(child)) append(el, child);
    else el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

export function badge(text: string, tone: "good" | "warn" | "bad" | "" = ""): HTMLElement {
  return h("span", { className: `badge ${tone}`.trim() }, text);
}

export function meter(value: number): HTMLElement {
  const pct = Math.max(0, Math.min(100, Math.round(value * 100)));
  return h("span", {}, h("span", { className: "meter", role: "img", "aria-label": `${pct}%` },
    h("span", { style: `width:${pct}%` })), `${pct}%`);
}

export function field(labelText: string, control: HTMLElement): HTMLElement {
  const id = `f-${Math.random().toString(36).slice(2, 9)}`;
  control.id = id;
  return h("div", {}, h("label", { for: id }, labelText), control);
}

export function select(options: Array<[string, string]>, value: string, attrs: Attrs = {}): HTMLSelectElement {
  return h("select", attrs, ...options.map(([v, label]) => h("option", { value: v, selected: v === value }, label))) as HTMLSelectElement;
}

export function when(iso: string | null | undefined): string {
  if (!iso) return "–";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "–" : d.toLocaleString();
}

export function toneFor(status: string): "good" | "warn" | "bad" | "" {
  if (["succeeded", "approved", "applied", "done", "awaiting_approval"].includes(status)) return "good";
  if (["failed", "stopped", "rejected"].includes(status)) return "bad";
  if (["running", "queued", "mapping", "planning", "applying", "draft", "proposed", "partially_applied", "generating", "reviewing", "judging"].includes(status)) {
    return "warn";
  }
  return "";
}

/** Replace an element's children, skipping empty values (null would otherwise render as "null"). */
export function mount(root: HTMLElement, ...children: Child[]): void {
  const holder = document.createElement("div");
  append(holder, children);
  root.replaceChildren(...Array.from(holder.childNodes));
}
