import { api, type Analysis, type Connection, type CrmChange, type EntityReport } from "../api";
import { type AppContext, errorBanner, infoBanner, messageOf } from "../context";
import { badge, field, h, meter, select, toneFor, when, mount } from "../dom";

export function specDetails(spec: CrmChange["items"][number]["spec"]): string {
  switch (spec.type) {
    case "choice":
      return spec.options.map((o) => o.label).join(" / ");
    case "text":
    case "memo":
      return `max ${spec.max_length ?? (spec.type === "text" ? 100 : 2000)} characters`;
    case "whole_number":
    case "decimal": {
      const range = [spec.min_value, spec.max_value].map((v) => (v === null ? "…" : String(v))).join(" to ");
      return spec.type === "decimal" ? `${range}, ${spec.precision ?? 2} decimals` : range;
    }
    case "boolean":
      return `${spec.true_label} / ${spec.false_label}`;
    default:
      return spec.type === "date" ? "date only" : "date and time";
  }
}

function reportCard(r: EntityReport): HTMLElement {
  const weakest = [...r.fields].sort((a, b) => a.fill_rate - b.fill_rate).slice(0, 12);
  return h(
    "div",
    { className: "card" },
    h("h2", {}, `${r.display_name} (${r.logical_name})`),
    h("p", { className: "muted" }, `${r.total_records ?? "?"} records · ${r.sampled} sampled`),
    h("strong", {}, "Suggestions"),
    h("ul", { className: "plain" }, ...r.suggestions.map((s) => h("li", {}, s))),
    r.duplicate_groups.length
      ? h("p", {}, `Duplicates on ${r.duplicate_groups[0]?.field}: `, r.duplicate_groups.slice(0, 5).map((g) => `${g.value} ×${g.count}`).join(", "))
      : null,
    h("table", {}, h("tr", {}, h("th", {}, "Column"), h("th", {}, "Type"), h("th", {}, "Filled"), h("th", {}, "")),
      ...weakest.map((f) => h("tr", {}, h("td", {}, f.display_name, h("div", { className: "muted" }, f.name)), h("td", {}, f.type), h("td", {}, meter(f.fill_rate)),
        h("td", {}, f.custom ? badge("custom") : null, " ", f.required ? badge("required", f.fill_rate < 1 ? "bad" : "") : null)))),
  );
}

export async function crmView(root: HTMLElement, ctx: AppContext): Promise<void> {
  let error: string | null = null;
  let notice: string | null = null;
  let connections: Connection[] = [];
  let connectionId = "";
  let analyses: Analysis[] = [];
  let changes: CrmChange[] = [];
  let openChange: CrmChange | null = null;
  let dirty = false;
  const analyzeForm = { entities: "contact, account", sample: "200" };
  const changeForm = { entity: "contact", request: "", prefix: "new", solution: "" };

  async function load(): Promise<void> {
    try {
      connections = await api<Connection[]>("GET", "/v1/connections");
      if (!connectionId && connections[0]) connectionId = connections[0].id;
      if (connectionId) {
        const q = `?connection_id=${encodeURIComponent(connectionId)}`;
        [analyses, changes] = await Promise.all([
          api<Analysis[]>("GET", `/v1/crm/analyses${q}`),
          api<CrmChange[]>("GET", `/v1/crm/changes${q}`),
        ]);
        if (openChange && !dirty) openChange = changes.find((c) => c.id === openChange?.id) ?? openChange;
      }
      error = null;
    } catch (err) {
      error = messageOf(err);
    }
    render();
  }

  const platformOf = (): string => connections.find((c) => c.id === connectionId)?.platform ?? "dynamics";
  const normalise = (name: string): string => (platformOf() === "dynamics" ? name.trim().toLowerCase() : name.trim());

  async function analyze(): Promise<void> {
    const entities = analyzeForm.entities.split(",").map(normalise).filter(Boolean);
    try {
      await api("POST", `/v1/crm/${connectionId}/analyses`, { entities, sample_size: Number(analyzeForm.sample) || 200 });
      notice = "Analysing… this reads metadata and a sample of records.";
    } catch (err) {
      error = messageOf(err);
    }
    await load();
  }

  async function requestColumns(): Promise<void> {
    try {
      const change = await api<CrmChange>("POST", `/v1/crm/${connectionId}/changes`, {
        entity: normalise(changeForm.entity),
        request: changeForm.request,
        ...(platformOf() === "dynamics"
          ? { publisher_prefix: changeForm.prefix.trim(), solution: changeForm.solution.trim() || null }
          : {}),
      });
      openChange = change;
      changeForm.request = "";
      notice = "The models are designing the columns…";
    } catch (err) {
      error = messageOf(err);
    }
    await load();
  }

  async function saveChange(): Promise<void> {
    if (!openChange) return;
    try {
      openChange = await api<CrmChange>("PUT", `/v1/crm/changes/${openChange.id}`, {
        items: openChange.items.map((i) => ({ spec: i.spec, include: i.include })),
      });
      dirty = false;
      notice = "Saved.";
    } catch (err) {
      error = messageOf(err);
    }
    await load();
  }

  async function approveChange(): Promise<void> {
    if (!openChange) return;
    try {
      openChange = await api<CrmChange>("POST", `/v1/crm/changes/${openChange.id}/approve`);
      notice = "Approved. Apply it to the environment when ready.";
    } catch (err) {
      error = messageOf(err);
    }
    await load();
  }

  async function applyChange(): Promise<void> {
    if (!openChange) return;
    const change = openChange;
    const included = change.items.filter((i) => i.include).length;
    let confirmProduction = false;
    if (change.environment !== "sandbox") {
      confirmProduction = await ctx.confirm(
        "Apply to a non-sandbox environment",
        `This connection is marked "${change.environment}". Apply ${included} column(s) to ${change.entity} only after testing them in a sandbox.`,
        "Apply to production",
      );
      if (!confirmProduction) return;
    } else if (!(await ctx.confirm("Apply columns", `Create ${included} column(s) on ${change.entity} in the sandbox?`, "Apply"))) {
      return;
    }
    try {
      openChange = await api<CrmChange>("POST", `/v1/crm/changes/${change.id}/apply`, { confirm_production: confirmProduction });
    } catch (err) {
      error = messageOf(err);
    }
    await load();
  }

  function editItem(index: number, patch: Record<string, unknown>, specPatch: Record<string, unknown> = {}): void {
    if (!openChange) return;
    const items = openChange.items.map((item, i) => (i === index ? { ...item, ...patch, spec: { ...item.spec, ...specPatch } } : item));
    openChange = { ...openChange, items };
    dirty = true;
    render();
  }

  function changeDetail(c: CrmChange): HTMLElement {
    const editable = c.status === "proposed" || c.status === "approved";
    return h(
      "div",
      { className: "card" },
      h("h2", {}, `New columns on ${c.entity}`),
      h("p", { className: "muted" }, c.request),
      h("p", {}, badge(c.status, toneFor(c.status)), " ", badge(c.environment, c.environment === "sandbox" ? "good" : "bad"),
        c.solution ? ` · solution ${c.solution}` : ""),
      errorBanner(c.error),
      c.items.length
        ? h("table", {}, h("tr", {}, ...["Include", "Column", "Display name", "Type", "Details", "Required", ""].map((t) => h("th", {}, t))),
            ...c.items.map((item, i) => h("tr", {},
              h("td", {}, h("input", { type: "checkbox", checked: item.include, disabled: !editable, style: "width:auto",
                "aria-label": `Include ${item.spec.schema_name}`, onChange: (e: Event) => editItem(i, { include: (e.target as HTMLInputElement).checked }) })),
              h("td", {}, h("code", {}, item.spec.schema_name)),
              h("td", {}, editable
                ? h("input", { value: item.spec.display_name, "aria-label": `Display name for ${item.spec.schema_name}`,
                    onChange: (e: Event) => editItem(i, {}, { display_name: (e.target as HTMLInputElement).value }) })
                : item.spec.display_name),
              h("td", {}, item.spec.type),
              h("td", {}, specDetails(item.spec)),
              h("td", {}, editable
                ? select([["none", "No"], ["recommended", "Recommended"], ["required", "Required"]], item.spec.required, {
                    "aria-label": `Required for ${item.spec.schema_name}`, onChange: (e: Event) => editItem(i, {}, { required: (e.target as HTMLSelectElement).value }) })
                : item.spec.required),
              h("td", {}, item.agreed ? badge("models agree", "good") : badge("check", "warn"), item.note ? h("div", { className: "muted" }, item.note) : null))))
        : null,
      c.results.length
        ? h("div", {}, h("h2", {}, "Result"), h("table", {}, ...c.results.map((r) => h("tr", {}, h("td", {}, r.schema_name),
            h("td", {}, badge(r.status, r.status === "created" ? "good" : r.status === "failed" ? "bad" : "")), h("td", {}, r.message)))))
        : null,
      editable
        ? h("div", { className: "row" },
            h("button", { className: "primary", disabled: !dirty, onClick: () => void saveChange() }, "Save changes"),
            h("button", { className: "primary", disabled: dirty || c.status !== "proposed", onClick: () => void approveChange() }, "Approve"),
            h("button", { className: c.environment === "sandbox" ? "primary" : "danger", disabled: dirty || c.status !== "approved", onClick: () => void applyChange() },
              c.environment === "sandbox" ? "Apply to sandbox" : "Apply…"))
        : null,
    );
  }

  function render(): void {
    const conn = connections.find((c) => c.id === connectionId);
    const platform = platformOf();
    const isDynamics = platform === "dynamics";
    const objectHint = { dynamics: "contact, account", salesforce: "Contact, Account", hubspot: "contacts, deals", zoho: "Leads, Contacts" }[platform] ?? "contact";
    mount(root,
      h("h1", {}, "CRM"),
      h("p", { className: "lead" }, "Dynamics 365, Salesforce, HubSpot and Zoho CRM: analyse data quality and design new fields with AI. Apply to a sandbox first."),
      errorBanner(error),
      infoBanner(notice),
      connections.length === 0
        ? h("p", {}, "Add a CRM connection first: Connections → Add connection, then pick Dynamics 365, Salesforce, HubSpot or Zoho CRM.")
        : h(
            "div",
            {},
            field("Connection", select(connections.map((c): [string, string] => [c.id, `${c.name} (${c.environment})`]), connectionId, {
              onChange: (e: Event) => { connectionId = (e.target as HTMLSelectElement).value; openChange = null; void load(); },
            })),
            conn && conn.environment !== "sandbox" ? h("p", { className: "badge bad" }, "Not marked as a sandbox: column changes need extra confirmation.") : null,
            h("div", { className: "card" },
              h("h2", {}, "Analyse data quality"),
              h("div", { className: "grid2" },
                field(isDynamics ? "Tables (logical names, comma separated)" : "Objects (API names, comma separated)", h("input", { value: analyzeForm.entities, placeholder: objectHint, onInput: (e: Event) => { analyzeForm.entities = (e.target as HTMLInputElement).value; } })),
                field("Records to sample", h("input", { type: "number", min: "1", max: "5000", value: analyzeForm.sample, onInput: (e: Event) => { analyzeForm.sample = (e.target as HTMLInputElement).value; } }))),
              h("div", { className: "row" }, h("button", { className: "primary", onClick: () => void analyze() }, "Analyse"))),
            ...analyses.slice(0, 3).map((a) => a.status === "done"
              ? h("div", {}, h("p", { className: "muted" }, `Analysis from ${when(a.created_at)}`), ...a.report.map(reportCard))
              : h("div", { className: "card" }, badge(a.status, toneFor(a.status)), ` ${a.entities.join(", ")} `, a.error ? h("span", { className: "badge bad" }, a.error) : null)),
            h("div", { className: "card" },
              h("h2", {}, isDynamics ? "Design new columns" : "Design new fields"),
              h("div", { className: "grid2" },
                field(isDynamics ? "Table" : "Object", h("input", { value: changeForm.entity, placeholder: objectHint.split(",")[0], onInput: (e: Event) => { changeForm.entity = (e.target as HTMLInputElement).value; } })),
                isDynamics ? field("Publisher prefix", h("input", { value: changeForm.prefix, onInput: (e: Event) => { changeForm.prefix = (e.target as HTMLInputElement).value; } })) : null,
                isDynamics ? field("Solution unique name (optional)", h("input", { value: changeForm.solution, onInput: (e: Event) => { changeForm.solution = (e.target as HTMLInputElement).value; } })) : null),
              field("What do you need?", h("textarea", { value: changeForm.request, placeholder: "e.g. Track customer loyalty: a tier (Bronze, Silver, Gold), points balance and the date they joined",
                onInput: (e: Event) => { changeForm.request = (e.target as HTMLTextAreaElement).value; } })),
              h("div", { className: "row" }, h("button", { className: "primary", onClick: () => void requestColumns() }, isDynamics ? "Design columns" : "Design fields"))),
            openChange ? changeDetail(openChange) : null,
            changes.length
              ? h("div", {}, h("h2", {}, "Column changes"), h("table", {},
                  ...changes.map((c) => h("tr", { className: "clickable", onClick: () => { openChange = c; dirty = false; render(); } },
                    h("td", {}, c.entity), h("td", {}, c.request.slice(0, 80)), h("td", {}, badge(c.status, toneFor(c.status)))))))
              : null,
          ),
    );
  }

  render();
  await load();
  ctx.every(2000, () => {
    const busy = analyses.some((a) => a.status === "running") || changes.some((c) => c.status === "planning" || c.status === "applying");
    if (busy && !dirty) void load();
  });
}
