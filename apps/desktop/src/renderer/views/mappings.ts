import { api, bridge, type Connection, type FieldMapping, type Job, type Mapping } from "../api";
import { type AppContext, errorBanner, infoBanner, messageOf } from "../context";
import { badge, field, h, meter, select, toneFor, when, mount } from "../dom";

interface SchemaEntity {
  name: string;
  fields: Array<{ name: string; type: string; required: boolean }>;
}

export function stepsSummary(steps: Array<Record<string, unknown>>): string {
  if (!steps.length) return "copy";
  return steps
    .map((s) => {
      const op = String(s.op);
      if (op === "split") return `split "${String(s.sep ?? " ")}" [${String(s.index ?? 0)}]`;
      if (op === "map_values") return `map ${Object.keys((s.values as object) ?? {}).length} values`;
      if (op === "truncate") return `truncate ${String(s.length)}`;
      if (op === "default") return `default ${JSON.stringify(s.value)}`;
      if (op === "to_date" && s.input_format) return `date from ${String(s.input_format)}`;
      return op.replace("_", " ");
    })
    .join(" → ");
}

export function sourceLabel(m: FieldMapping): string {
  if (m.constant !== null && m.constant !== undefined) return `constant ${JSON.stringify(m.constant)}`;
  if (m.sources.length) return m.sources.join(` + `);
  return m.source ?? "–";
}

export async function mappingsView(root: HTMLElement, ctx: AppContext): Promise<void> {
  let error: string | null = null;
  let mappings: Mapping[] = [];
  let connections: Connection[] = [];
  const form = { source: "", sourceEntity: "", target: "", targetEntity: "", samples: false };

  async function load(): Promise<void> {
    try {
      [mappings, connections] = await Promise.all([
        api<Mapping[]>("GET", "/v1/mappings"),
        api<Connection[]>("GET", "/v1/connections"),
      ]);
      error = null;
    } catch (err) {
      error = messageOf(err);
    }
    render();
  }

  async function create(): Promise<void> {
    try {
      const created = await api<Mapping>("POST", "/v1/mappings", {
        source_connection_id: form.source,
        source_entity: form.sourceEntity,
        target_connection_id: form.target,
        target_entity: form.targetEntity,
        include_samples: form.samples,
      });
      ctx.navigate({ name: "mapping", id: created.id });
    } catch (err) {
      error = messageOf(err);
      render();
    }
  }

  function entityOptions(connId: string): Array<[string, string]> {
    const conn = connections.find((c) => c.id === connId);
    return [["", "Choose entity…"], ...(conn?.entities ?? []).map((e): [string, string] => [e, e])];
  }

  function render(): void {
    const connOptions: Array<[string, string]> = [["", "Choose connection…"], ...connections.map((c): [string, string] => [c.id, c.name])];
    const ready = form.source && form.sourceEntity && form.target && form.targetEntity;
    mount(root, 
      h("h1", {}, "Mappings"),
      h("p", { className: "lead" }, "Map one entity to another. Rules and several AI models propose; you review and approve."),
      errorBanner(error),
      h(
        "div",
        { className: "card" },
        h("h2", {}, "New mapping"),
        h(
          "div",
          { className: "grid2" },
          field("From connection", select(connOptions, form.source, { onChange: (e: Event) => { form.source = (e.target as HTMLSelectElement).value; form.sourceEntity = ""; render(); } })),
          field("From entity", select(entityOptions(form.source), form.sourceEntity, { onChange: (e: Event) => { form.sourceEntity = (e.target as HTMLSelectElement).value; render(); } })),
          field("To connection", select(connOptions, form.target, { onChange: (e: Event) => { form.target = (e.target as HTMLSelectElement).value; form.targetEntity = ""; render(); } })),
          field("To entity", select(entityOptions(form.target), form.targetEntity, { onChange: (e: Event) => { form.targetEntity = (e.target as HTMLSelectElement).value; render(); } })),
        ),
        h("label", {}, h("input", { type: "checkbox", checked: form.samples, style: "width:auto;margin-right:6px", onChange: (e: Event) => { form.samples = (e.target as HTMLInputElement).checked; } }),
          "Send up to 5 sample records to the models (better mappings; real data leaves your systems)"),
        h("div", { className: "row" }, h("button", { className: "primary", disabled: !ready, onClick: () => void create() }, "Auto-map")),
      ),
      h("h2", {}, `Mappings (${mappings.length})`),
      mappings.length
        ? h(
            "table",
            {},
            h("tr", {}, ...["Name", "Status", "Fields", "Dry run", "Version"].map((t) => h("th", {}, t))),
            ...mappings.map((m) =>
              h(
                "tr",
                { className: "clickable", tabindex: "0", onClick: () => ctx.navigate({ name: "mapping", id: m.id }),
                  onKeydown: (e: KeyboardEvent) => { if (e.key === "Enter") ctx.navigate({ name: "mapping", id: m.id }); } },
                h("td", {}, m.name),
                h("td", {}, badge(m.status, toneFor(m.status))),
                h("td", {}, String(m.fields.length)),
                h("td", {}, m.dry_run_passed ? badge("passed", "good") : badge("not yet")),
                h("td", {}, String(m.version)),
              ),
            ),
          )
        : h("p", { className: "muted" }, "No mappings yet."),
    );
  }

  render();
  await load();
}

export async function mappingView(root: HTMLElement, ctx: AppContext, id: string): Promise<void> {
  let error: string | null = null;
  let notice: string | null = null;
  let mapping: Mapping | null = null;
  let jobs: Job[] = [];
  let sourceFields: string[] = [];
  let draft: FieldMapping[] = [];
  let dirty = false;
  let limit = "";

  async function load(): Promise<void> {
    try {
      mapping = await api<Mapping>("GET", `/v1/mappings/${id}`);
      jobs = await api<Job[]>("GET", `/v1/jobs?mapping_id=${encodeURIComponent(id)}`);
      if (!sourceFields.length) {
        const schema = await api<{ entities: SchemaEntity[] }>("GET", `/v1/connections/${mapping.source_connection_id}/schema`);
        sourceFields = schema.entities.find((e) => e.name === mapping?.source_entity)?.fields.map((f) => f.name) ?? [];
      }
      if (!dirty) draft = mapping.fields.map((f) => ({ ...f }));
      error = null;
    } catch (err) {
      error = messageOf(err);
    }
    render();
  }

  async function save(): Promise<void> {
    try {
      mapping = await api<Mapping>("PUT", `/v1/mappings/${id}`, { fields: draft });
      dirty = false;
      draft = mapping.fields.map((f) => ({ ...f }));
      notice = "Saved. Approve the mapping when every field is reviewed.";
      error = null;
    } catch (err) {
      error = messageOf(err);
    }
    render();
  }

  async function approve(): Promise<void> {
    try {
      mapping = await api<Mapping>("POST", `/v1/mappings/${id}/approve`);
      notice = "Approved. Start with a dry run.";
      error = null;
    } catch (err) {
      error = messageOf(err);
    }
    render();
  }

  async function runJob(dryRun: boolean): Promise<void> {
    if (!dryRun) {
      const ok = await ctx.confirm(
        "Run the migration",
        `This writes records into ${mapping?.target_entity}. Records written cannot be undone by Aitsev. Continue?`,
        "Run migration",
      );
      if (!ok) return;
    }
    try {
      const body: Record<string, unknown> = { mapping_id: id, dry_run: dryRun };
      if (limit.trim()) body.limit = Number(limit);
      const job = await api<Job>("POST", "/v1/jobs", body);
      ctx.navigate({ name: "job", id: job.id });
    } catch (err) {
      error = messageOf(err);
      render();
    }
  }

  async function exportScript(): Promise<void> {
    try {
      const text = await api<string>("GET", `/v1/mappings/${id}/export`);
      const saved = await bridge().saveFile(`migrate_${mapping?.source_entity}_to_${mapping?.target_entity}.py`.toLowerCase(), text);
      if (saved.ok) notice = `Saved ${saved.path}`;
    } catch (err) {
      error = messageOf(err);
    }
    render();
  }

  function update(index: number, patch: Partial<FieldMapping>): void {
    const current = draft[index];
    if (!current) return;
    draft[index] = { ...current, ...patch };
    dirty = true;
    render();
  }

  function render(): void {
    if (!mapping) {
      mount(root, h("h1", {}, "Mapping"), errorBanner(error), h("p", { className: "muted" }, "Loading…"));
      return;
    }
    const m = mapping;
    const working = m.status === "mapping";
    const sourceOptions: Array<[string, string]> = [["", "(none)"], ...sourceFields.map((f): [string, string] => [f, f])];
    const table = h(
      "table",
      {},
      h("tr", {}, ...["Target field", "Source", "Conversion", "Confidence", "How", "Decision"].map((t) => h("th", {}, t))),
      ...draft.map((f, i) =>
        h(
          "tr",
          {},
          h("td", {}, h("strong", {}, f.target), f.reason ? h("div", { className: "muted" }, f.reason) : null),
          h("td", {}, f.sources.length || f.constant !== null
            ? sourceLabel(f)
            : select(sourceOptions, f.source ?? "", { "aria-label": `Source for ${f.target}`, onChange: (e: Event) => update(i, { source: (e.target as HTMLSelectElement).value || null }) })),
          h("td", {}, stepsSummary(f.steps)),
          h("td", {}, meter(f.confidence)),
          h("td", {}, badge(f.method, f.method === "agreed" || f.method === "exact" ? "good" : "")),
          h("td", {}, select([["approved", "Approve"], ["proposed", "Needs review"], ["rejected", "Reject"]], f.status, {
            "aria-label": `Decision for ${f.target}`,
            onChange: (e: Event) => update(i, { status: (e.target as HTMLSelectElement).value as FieldMapping["status"] }),
          })),
        ),
      ),
    );
    mount(root, 
      h("div", { className: "row" }, h("button", { className: "secondary", onClick: () => ctx.navigate({ name: "mappings" }) }, "← Mappings")),
      h("h1", {}, m.name),
      h("p", { className: "lead" }, `${m.source_entity} → ${m.target_entity} · version ${m.version} · `, badge(m.status, toneFor(m.status)),
        m.models_used.length ? ` · models: ${m.models_used.map((x) => x.split("/").pop()).join(", ")}` : ""),
      errorBanner(error ?? m.error),
      infoBanner(working ? "Rules and models are mapping the fields…" : notice),
      m.blocking_issues.length && !working
        ? h("div", { className: "card" }, h("strong", {}, "Before you can approve"), h("ul", { className: "plain" }, ...m.blocking_issues.map((x) => h("li", {}, x))))
        : null,
      working ? null : table,
      m.unmapped_targets.length && !working ? h("p", { className: "muted" }, `Not mapped: ${m.unmapped_targets.join(", ")}`) : null,
      working
        ? null
        : h(
            "div",
            { className: "row" },
            h("button", { className: "primary", disabled: !dirty, onClick: () => void save() }, "Save changes"),
            h("button", { className: "secondary", disabled: dirty || m.status !== "draft", onClick: () => {
              draft = draft.map((f) => (f.status === "proposed" ? { ...f, status: "approved" } : f));
              dirty = true;
              render();
            } }, "Approve all proposed"),
            h("button", { className: "primary", disabled: dirty || m.status !== "draft" || m.blocking_issues.length > 0, onClick: () => void approve() }, "Approve mapping"),
          ),
      m.status === "approved"
        ? h(
            "div",
            { className: "card" },
            h("h2", {}, "Run"),
            h("p", { className: "muted" }, m.dry_run_passed
              ? "The dry run passed for this version. You can run the migration."
              : "Run a dry run first: it transforms and checks every record without writing anything."),
            field("Limit records (optional)", h("input", { type: "number", min: "1", value: limit, onInput: (e: Event) => { limit = (e.target as HTMLInputElement).value; } })),
            h("div", { className: "row" },
              h("button", { className: "primary", onClick: () => void runJob(true) }, "Dry run"),
              h("button", { className: "danger", disabled: !m.dry_run_passed, onClick: () => void runJob(false) }, "Run migration"),
              h("button", { className: "secondary", onClick: () => void exportScript() }, "Export as Python script")),
          )
        : null,
      jobs.length
        ? h("div", {}, h("h2", {}, "Jobs"),
            h("table", {}, h("tr", {}, ...["Started", "Type", "Status", "Read", "Valid", "Written", "Failed"].map((t) => h("th", {}, t))),
              ...jobs.map((j) => h("tr", { className: "clickable", onClick: () => ctx.navigate({ name: "job", id: j.id }) },
                h("td", {}, when(j.created_at)),
                h("td", {}, j.dry_run ? "dry run" : "migration"),
                h("td", {}, badge(j.status, toneFor(j.status))),
                h("td", {}, String(j.stats.read ?? 0)),
                h("td", {}, String(j.stats.valid ?? 0)),
                h("td", {}, String(j.stats.written ?? 0)),
                h("td", {}, String((j.stats.invalid ?? 0) + (j.stats.write_failed ?? 0)))))))
        : null,
    );
  }

  render();
  await load();
  ctx.every(2000, () => {
    if (mapping?.status === "mapping" || jobs.some((j) => j.status === "running" || j.status === "queued")) void load();
  });
}

export async function jobView(root: HTMLElement, ctx: AppContext, id: string): Promise<void> {
  let job: Job | null = null;
  let error: string | null = null;

  async function load(): Promise<void> {
    try {
      job = await api<Job>("GET", `/v1/jobs/${id}`);
      error = null;
    } catch (err) {
      error = messageOf(err);
    }
    render();
  }

  async function cancel(): Promise<void> {
    try {
      job = await api<Job>("POST", `/v1/jobs/${id}/cancel`);
    } catch (err) {
      error = messageOf(err);
    }
    render();
  }

  function render(): void {
    if (!job) {
      mount(root, h("h1", {}, "Job"), errorBanner(error), h("p", { className: "muted" }, "Loading…"));
      return;
    }
    const j = job;
    const running = j.status === "running" || j.status === "queued";
    const stat = (label: string, value: number | undefined) => h("div", {}, h("strong", {}, String(value ?? 0)), h("span", { className: "muted" }, label));
    mount(root, 
      h("div", { className: "row" }, h("button", { className: "secondary", onClick: () => ctx.navigate({ name: "mapping", id: j.mapping_id }) }, "← Mapping")),
      h("h1", {}, j.dry_run ? "Dry run" : "Migration run"),
      h("p", { className: "lead" }, badge(j.status, toneFor(j.status)), ` · started ${when(j.created_at)}`, j.finished_at ? ` · finished ${when(j.finished_at)}` : ""),
      errorBanner(error ?? j.error),
      h("div", { className: "card stats" },
        stat("read", j.stats.read), stat("valid", j.stats.valid), stat("invalid", j.stats.invalid),
        stat("written", j.stats.written), stat("write failed", j.stats.write_failed)),
      running ? h("div", { className: "row" }, h("button", { className: "danger", onClick: () => void cancel() }, "Stop job")) : null,
      j.errors.length
        ? h("div", {}, h("h2", {}, `Failed records (${j.errors.length}${j.errors.length >= 200 ? "+" : ""})`),
            h("table", {}, h("tr", {}, h("th", {}, "#"), h("th", {}, "Source key"), h("th", {}, "Stage"), h("th", {}, "Problems")),
              ...j.errors.map((e) => h("tr", {}, h("td", {}, String(e.index + 1)), h("td", {}, String(e.source_key ?? "–")), h("td", {}, e.stage),
                h("td", {}, h("ul", { className: "plain" }, ...e.errors.map((x) => h("li", {}, x))))))))
        : null,
      j.sample.length ? h("div", {}, h("h2", {}, "Sample of transformed records"), h("pre", {}, JSON.stringify(j.sample.slice(0, 5), null, 2))) : null,
    );
  }

  render();
  await load();
  ctx.every(1500, () => {
    if (job && (job.status === "running" || job.status === "queued")) void load();
  });
}
