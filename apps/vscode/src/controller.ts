import * as vscode from "vscode";
import {
  AitsevApiError,
  AitsevClient,
  RUNNING_STATUSES,
  applyChanges,
  commitPaths,
  createBranch,
  findConflicts,
  isGitRepo,
  pollDeviceSignIn,
  prepareTask,
  startDeviceSignIn,
  type CatalogModel,
  type TaskDetail,
  type TaskEvent,
  type TaskResult,
  type TaskSize,
  type TaskSummary,
} from "@aitsev/agent-core";
import { branchName, commitMessage, describeConflicts, toDetail, toRow, type ViewState } from "./model";
import type { PanelMessage, TaskPanel } from "./panel";
import type { ResultDocuments } from "./resultDocuments";
import type { MementoLike, TaskStore } from "./store";

const SECRET_KEY = "aitsev.apiKey";
const EMAIL_KEY = "aitsev.email";
const MODELS_KEY = "aitsev.models.v1";
const WELCOMED_KEY = "aitsev.welcomed";
export const DEFAULT_SERVER = "https://app.aitsev.com";

interface ChosenModel {
  id: string;
  label: string;
}

interface Config {
  serverUrl: string;
  defaultSize: TaskSize;
  setupCommands: string[];
  verifyCommands: string[];
  sandboxImage: string;
  exclude: string[];
  maxContextKb: number;
  createBranch: boolean;
  autoCommit: boolean;
  pollIntervalSeconds: number;
}

function readConfig(): Config {
  const c = vscode.workspace.getConfiguration("aitsev");
  return {
    serverUrl: c.get<string>("serverUrl", DEFAULT_SERVER).trim() || DEFAULT_SERVER,
    defaultSize: c.get<TaskSize>("defaultSize", "auto"),
    setupCommands: c.get<string[]>("setupCommands", []),
    verifyCommands: c.get<string[]>("verifyCommands", []),
    sandboxImage: c.get<string>("sandboxImage", "").trim(),
    exclude: c.get<string[]>("exclude", []),
    maxContextKb: c.get<number>("maxContextKb", 400),
    createBranch: c.get<boolean>("createBranch", true),
    autoCommit: c.get<boolean>("autoCommit", false),
    pollIntervalSeconds: Math.max(1, c.get<number>("pollIntervalSeconds", 3)),
  };
}

function errorText(err: unknown): string {
  if (err instanceof AitsevApiError) return err.message;
  return err instanceof Error ? err.message : String(err);
}

export class Controller implements vscode.Disposable {
  private apiKey: string | undefined;
  private tasks: TaskSummary[] = [];
  private selectedId: string | null = null;
  private detail: TaskDetail | null = null;
  private events: TaskEvent[] = [];
  private result: TaskResult | null = null;
  private busy: string | null = null;
  private error: string | null = null;
  private notice: string | null = null;
  private timer: NodeJS.Timeout | undefined;
  private readonly lastStatus = new Map<string, string>();
  private readonly disposables: vscode.Disposable[] = [];

  constructor(
    private readonly secrets: vscode.SecretStorage,
    private readonly store: TaskStore,
    private readonly panel: TaskPanel,
    private readonly results: ResultDocuments,
    private readonly output: vscode.OutputChannel,
    private readonly globalState: MementoLike,
  ) {
    this.disposables.push(
      vscode.workspace.onDidChangeConfiguration((e) => {
        if (e.affectsConfiguration("aitsev")) {
          this.schedulePoll();
          void this.refresh();
        }
      }),
      secrets.onDidChange((e) => {
        if (e.key === SECRET_KEY) void this.loadKey().then(() => this.refresh());
      }),
    );
  }

  async start(): Promise<void> {
    await this.loadKey();
    this.schedulePoll();
    await this.refresh();
    if (!this.apiKey && !this.globalState.get<boolean>(WELCOMED_KEY, false)) {
      await this.globalState.update(WELCOMED_KEY, true);
      void vscode.window
        .showInformationMessage("Welcome to Aitsev. Sign in to start building with several AI models.", "Sign in")
        .then((choice) => (choice === "Sign in" ? this.signIn() : undefined));
    }
  }

  dispose(): void {
    if (this.timer) clearInterval(this.timer);
    for (const d of this.disposables) d.dispose();
  }

  // ------------------------------------------------------------------ messages

  handle(message: PanelMessage): void {
    const run = async (): Promise<void> => {
      switch (message.type) {
        case "ready":
          this.render();
          await this.refresh();
          return;
        case "refresh":
          return this.refresh();
        case "setApiKey":
          return this.setApiKey();
        case "signIn":
          return this.signIn();
        case "chooseModels":
          return this.chooseModels();
        case "submit":
          return this.submit(message.instructions, message.size as TaskSize, message.useOpenFiles ? this.openFiles() : []);
        case "select":
          return this.select(message.id);
        case "approve":
          return this.approve(message.id);
        case "reject":
          return this.reject(message.id);
        case "retry":
          return this.retry(message.id);
        case "cancel":
          return this.cancel(message.id);
        case "openDiff":
          return this.openDiff(message.id, message.path);
      }
    };
    run().catch((err: unknown) => this.fail(err));
  }

  // ------------------------------------------------------------------ commands

  async setApiKey(): Promise<void> {
    const key = await vscode.window.showInputBox({
      title: "Aitsev API key",
      prompt: `API key for ${readConfig().serverUrl} (stored in your OS keychain)`,
      password: true,
      ignoreFocusOut: true,
      validateInput: (v) => (v.trim().length >= 16 ? null : "API keys are at least 16 characters"),
    });
    if (!key) return;
    await this.secrets.store(SECRET_KEY, key.trim());
    this.apiKey = key.trim();
    this.error = null;
    await this.checkConnection(true);
  }

  /** Sign in through the browser: no key to copy. */
  async signIn(): Promise<void> {
    const { serverUrl } = readConfig();
    let start;
    try {
      start = await startDeviceSignIn(serverUrl, `Aitsev for ${vscode.env.appName || "VS Code"}`);
    } catch (err) {
      this.fail(err);
      return;
    }
    await vscode.env.openExternal(vscode.Uri.parse(start.verification_url));
    const cancelled = { value: false };
    let grant;
    try {
      grant = await vscode.window.withProgress(
        {
          location: vscode.ProgressLocation.Notification,
          title: `Aitsev: approve the sign-in in your browser (code ${start.user_code})`,
          cancellable: true,
        },
        (_progress, token) => {
          const abort = new AbortController();
          token.onCancellationRequested(() => {
            cancelled.value = true;
            abort.abort();
          });
          return pollDeviceSignIn(serverUrl, start, { signal: abort.signal });
        },
      );
    } catch (err) {
      if (!cancelled.value) this.fail(err);
      return;
    }
    await this.secrets.store(SECRET_KEY, grant.apiKey);
    this.apiKey = grant.apiKey;
    this.clearSelection();
    await this.globalState.update(EMAIL_KEY, grant.email);
    this.error = null;
    this.notice = null;
    void vscode.window.showInformationMessage(`Aitsev: signed in as ${grant.email}.`);
    await this.refresh();
  }

  /** Pick which models write the code; planning, review and judging stay with the server. */
  async chooseModels(): Promise<void> {
    const client = await this.client(true);
    if (!client) return;
    let catalog;
    try {
      catalog = await client.catalog();
    } catch (err) {
      this.fail(err);
      return;
    }
    const current = new Set(this.chosenModels().map((m) => m.id));
    type Item = vscode.QuickPickItem & { model: CatalogModel };
    const items: Item[] = catalog.models
      .filter((m) => m.available)
      .map((m) => ({
        label: m.label,
        description: m.allowed ? (m.weight > 1 ? `premium · counts ×${m.weight}` : "") : "$(lock) Pro plan",
        detail: m.description || undefined,
        picked: m.allowed && current.has(m.id),
        model: m,
      }));
    const picked = await vscode.window.showQuickPick(items, {
      canPickMany: true,
      title: "Aitsev: models that write your code",
      placeHolder: `Pick up to ${catalog.max_selected}, or none to let Aitsev choose (recommended)`,
      ignoreFocusOut: true,
    });
    if (!picked) return;
    const locked = picked.filter((p) => !p.model.allowed);
    if (locked.length) {
      void vscode.window
        .showWarningMessage(`${locked.map((p) => p.model.label).join(", ")} ${locked.length === 1 ? "needs" : "need"} the Pro plan.`, "Upgrade")
        .then((choice) => (choice === "Upgrade" ? vscode.env.openExternal(vscode.Uri.parse(catalog.upgrade_url)) : undefined));
    }
    const chosen = picked
      .filter((p) => p.model.allowed)
      .slice(0, catalog.max_selected)
      .map((p) => ({ id: p.model.id, label: p.model.label }));
    await this.globalState.update(MODELS_KEY, chosen);
    this.notice = chosen.length ? `Models: ${chosen.map((m) => m.label).join(", ")}` : "Models: automatic";
    this.render();
  }

  async clearApiKey(): Promise<void> {
    await this.secrets.delete(SECRET_KEY);
    await this.globalState.update(EMAIL_KEY, undefined);
    this.apiKey = undefined;
    this.clearSelection();
    this.tasks = [];
    this.render();
  }

  async checkConnection(quiet = false): Promise<boolean> {
    const client = await this.client();
    if (!client) return false;
    try {
      const health = await client.health();
      const models = await client.models();
      const available = models.roles.filter((r) => r.available).length;
      const message = `Connected to Aitsev ${health.version}: ${available}/${models.roles.length} model roles available.`;
      this.notice = available === 0 ? "Server reachable, but no models are available. Check its provider keys." : null;
      if (!quiet) void vscode.window.showInformationMessage(message);
      this.output.appendLine(message);
      this.render();
      return true;
    } catch (err) {
      this.fail(err);
      return false;
    }
  }

  async newTask(pinnedUris: vscode.Uri[] = []): Promise<void> {
    const instructions = await vscode.window.showInputBox({
      title: "New Aitsev task",
      prompt: pinnedUris.length
        ? `Describe the change (focus: ${pinnedUris.map((u) => vscode.workspace.asRelativePath(u)).join(", ")})`
        : "Describe the change you want",
      ignoreFocusOut: true,
    });
    if (!instructions?.trim()) return;
    const pinned = pinnedUris.length ? pinnedUris : this.openFiles();
    await this.submit(instructions, readConfig().defaultSize, pinned);
  }

  // ------------------------------------------------------------------ actions

  private async submit(instructions: string, size: TaskSize, pinned: vscode.Uri[]): Promise<void> {
    const client = await this.client(true);
    if (!client) return;
    const folder = await this.pickFolder(pinned);
    if (!folder) return;
    const config = readConfig();
    const root = folder.uri.fsPath;
    const pinnedPaths = pinned
      .filter((u) => vscode.workspace.getWorkspaceFolder(u)?.uri.toString() === folder.uri.toString())
      .map((u) => vscode.workspace.asRelativePath(u, false));

    await this.withBusy("Scanning workspace…", async () => {
      const prepared = await prepareTask(root, instructions, {
        size,
        pinned: pinnedPaths,
        maxContextBytes: config.maxContextKb * 1024,
        scan: { exclude: config.exclude },
        setupCommands: config.setupCommands,
        verifyCommands: config.verifyCommands,
        sandboxImage: config.sandboxImage || null,
      });
      for (const warning of prepared.warnings) this.output.appendLine(`[warning] ${warning}`);
      this.output.appendLine(
        `Submitting "${prepared.body.title}": ${prepared.scan.files.length} files, ` +
          `${prepared.selection.wholeProject ? "all" : prepared.selection.contextPaths.length} as model context, ` +
          `verify: ${prepared.body.verify_commands?.join(" && ") || "(none)"}`,
      );
      this.busy = "Submitting task…";
      this.render();
      const models = this.chosenModels().map((m) => m.id);
      const task = await client.createTask(models.length ? { ...prepared.body, models } : prepared.body);
      await this.store.add({
        id: task.id,
        root,
        title: task.title,
        createdAt: task.created_at,
        snapshot: prepared.snapshot,
      });
      this.lastStatus.set(task.id, task.status);
      this.selectedId = task.id;
      const firstWarning = prepared.warnings[0];
      this.notice = firstWarning ?? null;
    });
    await this.refresh();
  }

  private async select(id: string): Promise<void> {
    this.selectedId = id;
    this.detail = null;
    this.events = [];
    this.result = null;
    await this.refresh();
  }

  private async approve(id: string): Promise<void> {
    const client = await this.client(true);
    const tracked = this.store.get(id);
    if (!client) return;
    if (!tracked) {
      void vscode.window.showWarningMessage(
        "This task was submitted from another workspace or machine, so it cannot be applied here.",
      );
      return;
    }
    const result = await client.result(id);
    const config = readConfig();
    await this.withBusy("Applying changes…", async () => {
      const conflicts = await findConflicts(tracked.root, result.files, tracked.snapshot);
      if (conflicts.length > 0) {
        const choice = await vscode.window.showWarningMessage(
          `You changed these files after submitting the task: ${describeConflicts(conflicts)}. ` +
            "Applying will overwrite your edits.",
          { modal: true },
          "Overwrite",
        );
        if (choice !== "Overwrite") return;
      }
      let branch: string | undefined;
      const git = await isGitRepo(tracked.root);
      if (git && config.createBranch) {
        branch = branchName({ id, title: tracked.title });
        try {
          await createBranch(tracked.root, branch);
        } catch (err) {
          this.output.appendLine(`[warning] Could not create branch ${branch}: ${errorText(err)}`);
          branch = undefined;
        }
      }
      const report = await applyChanges(tracked.root, result.files, tracked.snapshot, { force: true });
      let commit: string | undefined;
      if (git && config.autoCommit) {
        try {
          commit = await commitPaths(
            tracked.root,
            [...report.written, ...report.deleted],
            commitMessage({ id, title: tracked.title }, result.summary, result.model),
          );
        } catch (err) {
          this.output.appendLine(`[warning] Commit failed: ${errorText(err)}`);
        }
      }
      await this.store.update(id, { appliedAt: new Date().toISOString(), branch, commit });
      await client.approve(id, `Applied ${report.written.length + report.deleted.length} file(s) from VS Code`);
      const where = branch ? ` on branch ${branch}` : "";
      void vscode.window.showInformationMessage(
        `Aitsev applied ${report.written.length} changed and ${report.deleted.length} deleted file(s)${where}.`,
      );
    });
    await this.refresh();
  }

  private async reject(id: string): Promise<void> {
    const client = await this.client(true);
    if (!client) return;
    const reason = await vscode.window.showInputBox({
      title: "Reject result",
      prompt: "Why are you rejecting it? (kept in the audit log)",
      ignoreFocusOut: true,
    });
    if (!reason?.trim()) return;
    await this.withBusy("Rejecting…", () => client.reject(id, reason.trim()));
    await this.refresh();
  }

  private async retry(id: string): Promise<void> {
    const client = await this.client(true);
    if (!client) return;
    const feedback = await vscode.window.showInputBox({
      title: "Retry with feedback",
      prompt: "What should the models do differently?",
      ignoreFocusOut: true,
    });
    if (!feedback?.trim()) return;
    await this.withBusy("Re-queuing…", () => client.retry(id, feedback.trim()));
    this.lastStatus.set(id, "queued");
    await this.refresh();
  }

  private async cancel(id: string): Promise<void> {
    const client = await this.client(true);
    if (!client) return;
    await this.withBusy("Cancelling…", () => client.cancel(id));
    await this.refresh();
  }

  private async openDiff(id: string, path: string): Promise<void> {
    const client = await this.client(true);
    if (!client) return;
    const result = this.result?.task_id === id ? this.result : await client.result(id);
    const change = result.files.find((f) => f.path === path);
    if (!change) return;
    const tracked = this.store.get(id);
    const proposed = this.results.set(id, path, change.content ?? "");
    const left = tracked
      ? vscode.Uri.joinPath(vscode.Uri.file(tracked.root), ...path.split("/"))
      : this.results.set(`${id}-original`, path, "");
    const leftUri = change.action === "added" ? this.results.set(`${id}-empty`, path, "") : left;
    await vscode.commands.executeCommand(
      "vscode.diff",
      leftUri,
      proposed,
      `${path} (${change.action}) ↔ Aitsev proposal`,
      { preview: true },
    );
  }

  // ------------------------------------------------------------------ refresh / polling

  async refresh(): Promise<void> {
    const client = await this.client();
    if (!client) {
      this.render();
      return;
    }
    try {
      this.tasks = await client.listTasks({ limit: 50 });
      if (this.selectedId) {
        this.detail = await client.getTask(this.selectedId);
        this.events = await client.events(this.selectedId);
        this.result = this.detail.winner_candidate_id ? await client.result(this.selectedId) : null;
      }
      this.error = null;
      this.notifyTransitions();
    } catch (err) {
      if (err instanceof AitsevApiError && err.status === 404 && this.selectedId) {
        // The selected task is gone or belongs to another account: just drop the selection.
        this.clearSelection();
        this.error = null;
      } else {
        this.error = errorText(err);
      }
    }
    this.render();
  }

  private clearSelection(): void {
    this.selectedId = null;
    this.detail = null;
    this.events = [];
    this.result = null;
  }

  private notifyTransitions(): void {
    for (const task of this.tasks) {
      const before = this.lastStatus.get(task.id);
      this.lastStatus.set(task.id, task.status);
      if (!before || before === task.status || !this.store.get(task.id)) continue;
      if (task.status === "awaiting_approval") {
        void vscode.window
          .showInformationMessage(`Aitsev: "${task.title}" is ready for review.`, "Review")
          .then((choice) => {
            if (choice === "Review") {
              void this.panel.reveal();
              void this.select(task.id);
            }
          });
      } else if (task.status === "failed") {
        void vscode.window.showErrorMessage(`Aitsev: "${task.title}" failed. ${task.error ?? ""}`.trim());
      }
    }
  }

  private schedulePoll(): void {
    if (this.timer) clearInterval(this.timer);
    this.timer = setInterval(() => {
      const anyRunning = this.tasks.some((t) => RUNNING_STATUSES.has(t.status));
      if (anyRunning && !this.busy) void this.refresh();
    }, readConfig().pollIntervalSeconds * 1000);
  }

  // ------------------------------------------------------------------ helpers

  private render(): void {
    const tracked = new Map(this.store.all().map((t) => [t.id, t]));
    const state: ViewState = {
      configured: Boolean(this.apiKey),
      serverUrl: readConfig().serverUrl,
      busy: this.busy,
      error: this.error,
      notice: this.notice,
      tasks: this.tasks.map((t) => toRow(t, tracked.get(t.id))),
      selected:
        this.detail && this.detail.id === this.selectedId
          ? toDetail(this.detail, this.events, this.result, tracked.get(this.detail.id))
          : null,
      defaultSize: readConfig().defaultSize,
      account: this.apiKey ? this.globalState.get<string | undefined>(EMAIL_KEY, undefined) ?? null : null,
      models: this.chosenModels().map((m) => m.label).join(", ") || "Automatic",
    };
    this.panel.render(state);
  }

  private async withBusy<T>(label: string, work: () => Promise<T>): Promise<T | undefined> {
    this.busy = label;
    this.error = null;
    this.render();
    try {
      return await work();
    } catch (err) {
      this.fail(err);
      return undefined;
    } finally {
      this.busy = null;
      this.render();
    }
  }

  private fail(err: unknown): void {
    const text = errorText(err);
    this.error = text;
    this.output.appendLine(`[error] ${text}`);
    this.render();
  }

  private chosenModels(): ChosenModel[] {
    const raw = this.globalState.get<unknown>(MODELS_KEY, []);
    return Array.isArray(raw)
      ? raw.filter((m): m is ChosenModel => !!m && typeof m === "object" && typeof (m as ChosenModel).id === "string" && typeof (m as ChosenModel).label === "string")
      : [];
  }

  private async loadKey(): Promise<void> {
    this.apiKey = (await this.secrets.get(SECRET_KEY)) ?? undefined;
  }

  /** Shared with the chat and the analyzer. */
  apiClient(prompt = false): Promise<AitsevClient | undefined> {
    return this.client(prompt);
  }

  isSignedIn(): boolean {
    return Boolean(this.apiKey);
  }

  private async client(prompt = false): Promise<AitsevClient | undefined> {
    if (!this.apiKey && prompt) await this.signIn();
    if (!this.apiKey) return undefined;
    return new AitsevClient({ baseUrl: readConfig().serverUrl, apiKey: this.apiKey });
  }

  private openFiles(): vscode.Uri[] {
    const uris = new Map<string, vscode.Uri>();
    for (const editor of vscode.window.visibleTextEditors) {
      if (editor.document.uri.scheme === "file") uris.set(editor.document.uri.toString(), editor.document.uri);
    }
    const active = vscode.window.activeTextEditor?.document.uri;
    if (active?.scheme === "file") uris.set(active.toString(), active);
    return [...uris.values()];
  }

  private async pickFolder(pinned: vscode.Uri[]): Promise<vscode.WorkspaceFolder | undefined> {
    const folders = vscode.workspace.workspaceFolders ?? [];
    if (folders.length === 0) {
      void vscode.window.showWarningMessage("Open a folder first: Aitsev works on the files in your workspace.");
      return undefined;
    }
    const fromPinned = pinned.map((u) => vscode.workspace.getWorkspaceFolder(u)).find(Boolean);
    if (fromPinned) return fromPinned;
    if (folders.length === 1) return folders[0];
    return vscode.window.showWorkspaceFolderPick({ placeHolder: "Which folder should Aitsev work on?" });
  }
}
