import { promises as fs } from "node:fs";
import * as vscode from "vscode";
import {
  AitsevApiError,
  applyChanges,
  findConflicts,
  normalizePath,
  prepareTask,
  resolveInside,
  type AitsevClient,
  type ChatBody,
  type ChatEdit,
  type Snapshot,
  type Suggestion,
} from "@aitsev/agent-core";
import {
  editViews,
  fixPrompt,
  historyFor,
  inverseChanges,
  isChatMessage,
  modelOptions,
  newId,
  toChanges,
  verificationOf,
  type ChatEntry,
  type ChatMessage,
  type ChatMode,
  type ChatState,
  type ModelOption,
  type SuggestionView,
} from "./chatModel";
import { makeNonce } from "./model";
import type { ResultDocuments } from "./resultDocuments";
import type { MementoLike } from "./store";

const ENTRIES_KEY = "aitsev.chat.v1";
const MODEL_KEY = "aitsev.chat.model";
const MODE_KEY = "aitsev.chat.mode";
const AUTOPILOT_KEY = "aitsev.chat.autopilot";
const MAX_ENTRIES = 100;

/** What the chat needs from the rest of the extension. */
export interface ChatSession {
  apiClient(prompt?: boolean): Promise<AitsevClient | undefined>;
  signIn(): Promise<void>;
  isSignedIn(): boolean;
}

interface Pending {
  root: string;
  edits: ChatEdit[];
  snapshot: Snapshot;
  before: Record<string, string | null>;
}

function errorText(err: unknown): string {
  if (err instanceof AitsevApiError && err.status === 402) return `${err.message}`;
  return err instanceof Error ? err.message : String(err);
}

async function readIfExists(file: string): Promise<string | null> {
  try {
    return await fs.readFile(file, "utf8");
  } catch {
    return null;
  }
}

/** The Kiro-style chat: ask about the code or let the agent change it. */
export class ChatController implements vscode.WebviewViewProvider, vscode.Disposable {
  static readonly viewId = "aitsev.chat";
  private view: vscode.WebviewView | undefined;
  private entries: ChatEntry[];
  private busy: string | null = null;
  private models: ModelOption[] = [{ id: "auto", label: "Auto (best for each request)", locked: false }];
  private suggestions: SuggestionView[] = [];
  private analyzing = false;
  private readonly pending = new Map<string, Pending>();

  constructor(
    private readonly extensionUri: vscode.Uri,
    private readonly session: ChatSession,
    private readonly results: ResultDocuments,
    private readonly workspaceState: MementoLike,
    private readonly globalState: MementoLike,
    private readonly output: vscode.OutputChannel,
  ) {
    // Proposals cannot be applied after a reload (the snapshot is gone), so mark them expired.
    this.entries = this.workspaceState.get<ChatEntry[]>(ENTRIES_KEY, []).map((e) => ({
      ...e,
      edits: e.edits?.map((x) => (x.status === "proposed" ? { ...x, status: "rejected" as const, note: "Expired after reload" } : x)),
    }));
  }

  // ------------------------------------------------------------------ webview

  resolveWebviewView(view: vscode.WebviewView): void {
    this.view = view;
    const media = vscode.Uri.joinPath(this.extensionUri, "media");
    view.webview.options = { enableScripts: true, localResourceRoots: [media] };
    view.webview.html = this.html(view.webview, media);
    view.webview.onDidReceiveMessage((raw: unknown) => {
      if (isChatMessage(raw)) void this.handle(raw).catch((err: unknown) => this.error(err));
    });
    view.onDidDispose(() => {
      this.view = undefined;
    });
  }

  async reveal(): Promise<void> {
    await vscode.commands.executeCommand("aitsev.chat.focus");
  }

  dispose(): void {
    this.pending.clear();
  }

  get model(): string {
    return this.globalState.get<string>(MODEL_KEY, "auto");
  }

  private get mode(): ChatMode {
    return this.globalState.get<ChatMode>(MODE_KEY, "agent");
  }

  private get autopilot(): boolean {
    return this.globalState.get<boolean>(AUTOPILOT_KEY, false);
  }

  async handle(message: ChatMessage): Promise<void> {
    switch (message.type) {
      case "ready":
        this.render();
        await this.loadModels();
        return;
      case "send":
        return this.send(message.text);
      case "setModel":
        await this.globalState.update(MODEL_KEY, this.models.some((m) => m.id === message.model && !m.locked) ? message.model : "auto");
        if (this.models.find((m) => m.id === message.model)?.locked) {
          void vscode.window.showWarningMessage("That model is included with the Pro plan and above.", "Upgrade").then((c) => {
            if (c === "Upgrade") void this.openUpgrade();
          });
        }
        return this.render();
      case "setMode":
        await this.globalState.update(MODE_KEY, message.mode);
        return this.render();
      case "setAutopilot":
        await this.globalState.update(AUTOPILOT_KEY, message.on);
        return this.render();
      case "newChat":
        this.entries = [];
        this.pending.clear();
        await this.save();
        return this.render();
      case "signIn":
        await this.session.signIn();
        await this.loadModels();
        return this.render();
      case "openFolder":
        await vscode.commands.executeCommand("vscode.openFolder");
        return;
      case "analyze":
        await vscode.commands.executeCommand("aitsev.analyzeNow");
        return;
      case "diff":
        return this.diff(message.entryId, message.path);
      case "accept":
        return this.accept(message.entryId, message.path, false);
      case "reject":
        return this.reject(message.entryId, message.path);
      case "undo":
        return this.undo(message.entryId);
      case "fix": {
        const s = this.suggestions.find((x) => x.id === message.id);
        if (s) await this.fix(s);
        return;
      }
      case "openFile":
        return this.openFile(message.path, message.line);
    }
  }

  // ------------------------------------------------------------------ public actions

  /** Ask about the current selection (editor context menu / command). */
  async askAboutSelection(): Promise<void> {
    const editor = vscode.window.activeTextEditor;
    const text = editor?.document.getText(editor.selection).trim();
    await this.reveal();
    if (!editor || !text) return;
    const rel = vscode.workspace.asRelativePath(editor.document.uri, false);
    const question = await vscode.window.showInputBox({ title: "Ask Aitsev", prompt: `About the selected code in ${rel}`, ignoreFocusOut: true });
    if (question?.trim()) await this.send(question.trim(), text);
  }

  async fix(s: Suggestion): Promise<void> {
    await this.reveal();
    await this.send(fixPrompt(s), undefined, "agent");
  }

  setSuggestions(suggestions: SuggestionView[], analyzing: boolean): void {
    this.suggestions = suggestions;
    this.analyzing = analyzing;
    this.render();
  }

  async loadModels(): Promise<void> {
    const client = await this.session.apiClient(false);
    if (!client) return this.render();
    try {
      const catalog = await client.catalog();
      this.models = modelOptions(catalog.models);
      if (!this.models.some((m) => m.id === this.model && !m.locked)) await this.globalState.update(MODEL_KEY, "auto");
    } catch (err) {
      this.output.appendLine(`[chat] could not load models: ${errorText(err)}`);
    }
    this.render();
  }

  // ------------------------------------------------------------------ sending

  private folder(): vscode.WorkspaceFolder | undefined {
    const active = vscode.window.activeTextEditor?.document.uri;
    const fromActive = active ? vscode.workspace.getWorkspaceFolder(active) : undefined;
    return fromActive ?? vscode.workspace.workspaceFolders?.[0];
  }

  async send(text: string, selection?: string, forceMode?: ChatMode): Promise<void> {
    if (this.busy) return;
    const folder = this.folder();
    if (!folder) {
      this.push({ id: newId(), role: "error", text: "Open a project folder first (File → Open Folder), then ask again.", at: new Date().toISOString() });
      return this.render();
    }
    const client = await this.session.apiClient(true);
    if (!client) return this.render();
    const mode = forceMode ?? this.mode;
    this.push({ id: newId(), role: "user", text, at: new Date().toISOString() });
    this.busy = "Reading your project…";
    this.render();
    try {
      const config = vscode.workspace.getConfiguration("aitsev");
      const root = folder.uri.fsPath;
      const editor = vscode.window.activeTextEditor;
      const active = editor && vscode.workspace.getWorkspaceFolder(editor.document.uri)?.uri.toString() === folder.uri.toString()
        ? vscode.workspace.asRelativePath(editor.document.uri, false)
        : null;
      const open = vscode.window.visibleTextEditors
        .filter((e) => e.document.uri.scheme === "file" && vscode.workspace.getWorkspaceFolder(e.document.uri)?.uri.toString() === folder.uri.toString())
        .map((e) => vscode.workspace.asRelativePath(e.document.uri, false));
      const prepared = await prepareTask(root, text, {
        pinned: [...new Set([...(active ? [active] : []), ...open])],
        maxContextBytes: config.get<number>("maxContextKb", 400) * 1024,
        scan: { exclude: config.get<string[]>("exclude", []) },
        setupCommands: config.get<string[]>("setupCommands", []),
        verifyCommands: config.get<string[]>("verifyCommands", []),
      });
      const verify = mode === "agent" && config.get<boolean>("chat.verifyEdits", true) && (prepared.body.verify_commands?.length ?? 0) > 0;
      const contextPaths = prepared.body.context_paths ?? [];
      const contextFiles = contextPaths.length
        ? Object.fromEntries(contextPaths.map((p) => [p, prepared.body.files[p] as string]))
        : prepared.body.files;
      const body: ChatBody = {
        messages: historyFor(this.entries),
        mode,
        model: this.model,
        files: verify ? prepared.body.files : contextFiles,
        context_paths: verify ? contextPaths : [],
        active_path: active,
        selection: selection ?? (editor && !editor.selection.isEmpty ? editor.document.getText(editor.selection).slice(0, 20_000) : null),
      };
      if (verify) {
        body.setup_commands = prepared.body.setup_commands;
        body.verify_commands = prepared.body.verify_commands;
        body.sandbox_image = prepared.body.sandbox_image ?? null;
      }
      this.busy = verify ? "Writing and testing changes…" : mode === "agent" ? "Working on it…" : "Thinking…";
      this.render();
      const reply = await client.chat(body);
      const entry: ChatEntry = {
        id: newId(),
        role: "assistant",
        text: reply.reply || (reply.edits.length ? "Here are the changes." : "(no answer)"),
        at: new Date().toISOString(),
        model: reply.model,
        edits: editViews(reply),
        skipped: reply.skipped,
        verification: verificationOf(reply),
        autopilot: this.autopilot,
      };
      this.push(entry);
      if (reply.edits.length) {
        this.pending.set(entry.id, { root, edits: reply.edits, snapshot: prepared.snapshot, before: {} });
        if (this.autopilot) {
          this.busy = null;
          await this.accept(entry.id, undefined, true);
        }
      }
    } catch (err) {
      this.push({ id: newId(), role: "error", text: errorText(err), at: new Date().toISOString() });
      this.output.appendLine(`[chat] ${errorText(err)}`);
    } finally {
      this.busy = null;
      await this.save();
      this.render();
    }
  }

  // ------------------------------------------------------------------ edits

  private entry(entryId: string): ChatEntry | undefined {
    return this.entries.find((e) => e.id === entryId);
  }

  private setStatus(entry: ChatEntry, path: string, status: NonNullable<ChatEntry["edits"]>[number]["status"], note?: string): void {
    const edit = entry.edits?.find((x) => x.path === path);
    if (edit) {
      edit.status = status;
      edit.note = note;
    }
  }

  async accept(entryId: string, path: string | undefined, automatic: boolean): Promise<void> {
    const entry = this.entry(entryId);
    const pending = this.pending.get(entryId);
    if (!entry || !pending) return;
    const open = new Set((entry.edits ?? []).filter((x) => x.status === "proposed" || x.status === "conflict").map((x) => x.path));
    const targets = pending.edits.filter((e) => open.has(e.path) && (!path || e.path === path));
    if (!targets.length) return;
    const conflicts = await findConflicts(pending.root, toChanges(targets), pending.snapshot);
    let force = false;
    if (conflicts.length) {
      if (automatic) {
        for (const c of conflicts) this.setStatus(entry, c.path, "conflict", "You changed this file after asking; review it before accepting");
      } else {
        const choice = await vscode.window.showWarningMessage(
          `${conflicts.map((c) => c.path).join(", ")} changed after you asked. Overwrite with Aitsev's version?`,
          { modal: true },
          "Overwrite",
        );
        force = choice === "Overwrite";
        if (!force) return this.render();
      }
    }
    const conflicted = new Set(force ? [] : conflicts.map((c) => c.path));
    const writable = targets.filter((e) => !conflicted.has(e.path));
    for (const e of writable) {
      const file = resolveInside(pending.root, normalizePath(e.path));
      if (!(e.path in pending.before)) pending.before[e.path] = await readIfExists(file);
    }
    try {
      await applyChanges(pending.root, toChanges(writable), pending.snapshot, { force: true });
      for (const e of writable) this.setStatus(entry, e.path, "applied");
    } catch (err) {
      for (const e of writable) this.setStatus(entry, e.path, "failed", errorText(err));
    }
    await this.save();
    this.render();
  }

  private async reject(entryId: string, path?: string): Promise<void> {
    const entry = this.entry(entryId);
    if (!entry) return;
    for (const e of entry.edits ?? []) {
      if ((e.status === "proposed" || e.status === "conflict") && (!path || e.path === path)) e.status = "rejected";
    }
    await this.save();
    this.render();
  }

  private async undo(entryId: string): Promise<void> {
    const entry = this.entry(entryId);
    const pending = this.pending.get(entryId);
    if (!entry || !pending) return;
    const applied = pending.edits.filter((e) => entry.edits?.find((x) => x.path === e.path)?.status === "applied");
    if (!applied.length) return;
    await applyChanges(pending.root, inverseChanges(applied, pending.before), {}, { force: true });
    for (const e of applied) this.setStatus(entry, e.path, "undone");
    await this.save();
    this.render();
  }

  private async diff(entryId: string, path: string): Promise<void> {
    const pending = this.pending.get(entryId);
    const edit = pending?.edits.find((e) => e.path === path);
    if (!pending || !edit) {
      void vscode.window.showInformationMessage("This proposal is no longer available. Ask again to get a fresh one.");
      return;
    }
    const proposed = this.results.set(`chat-${entryId}`, path, edit.content ?? "");
    const left = edit.action === "create"
      ? this.results.set(`chat-${entryId}-empty`, path, "")
      : vscode.Uri.file(resolveInside(pending.root, normalizePath(path)));
    await vscode.commands.executeCommand("vscode.diff", left, proposed, `${path} ↔ Aitsev`, { preview: true });
  }

  private async openFile(path: string, line?: number): Promise<void> {
    const folder = this.folder();
    if (!folder) return;
    const uri = vscode.Uri.file(resolveInside(folder.uri.fsPath, normalizePath(path)));
    const doc = await vscode.workspace.openTextDocument(uri);
    const position = new vscode.Position(Math.max(0, (line ?? 1) - 1), 0);
    await vscode.window.showTextDocument(doc, { selection: new vscode.Range(position, position) });
  }

  private async openUpgrade(): Promise<void> {
    const client = await this.session.apiClient(false);
    if (!client) return;
    try {
      const catalog = await client.catalog();
      await vscode.env.openExternal(vscode.Uri.parse(catalog.upgrade_url));
    } catch {
      /* offline: nothing to open */
    }
  }

  // ------------------------------------------------------------------ state

  private push(entry: ChatEntry): void {
    this.entries.push(entry);
    if (this.entries.length > MAX_ENTRIES) this.entries = this.entries.slice(-MAX_ENTRIES);
  }

  private async save(): Promise<void> {
    await this.workspaceState.update(ENTRIES_KEY, this.entries);
  }

  private error(err: unknown): void {
    this.busy = null;
    this.push({ id: newId(), role: "error", text: errorText(err), at: new Date().toISOString() });
    this.render();
  }

  state(): ChatState {
    return {
      signedIn: this.session.isSignedIn(),
      busy: this.busy,
      entries: this.entries.map((e) => ({ ...e, edits: e.edits?.map((x) => ({ ...x })) })),
      models: this.models,
      model: this.model,
      mode: this.mode,
      autopilot: this.autopilot,
      suggestions: this.suggestions,
      analyzing: this.analyzing,
      hasFolder: Boolean(vscode.workspace.workspaceFolders?.length),
    };
  }

  render(): void {
    void this.view?.webview.postMessage({ type: "state", state: this.state() });
  }

  private html(webview: vscode.Webview, media: vscode.Uri): string {
    const nonce = makeNonce();
    const script = webview.asWebviewUri(vscode.Uri.joinPath(media, "chat.js"));
    const style = webview.asWebviewUri(vscode.Uri.joinPath(media, "chat.css"));
    const csp = [
      "default-src 'none'",
      `style-src ${webview.cspSource}`,
      `script-src 'nonce-${nonce}'`,
      `img-src ${webview.cspSource}`,
      `font-src ${webview.cspSource}`,
    ].join("; ");
    return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="${csp}">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link rel="stylesheet" href="${style.toString()}">
<title>Aitsev Chat</title>
</head>
<body>
<div id="app"></div>
<script nonce="${nonce}" src="${script.toString()}"></script>
</body>
</html>`;
  }
}
