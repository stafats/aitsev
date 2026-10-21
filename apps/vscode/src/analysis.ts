import * as vscode from "vscode";
import { scanWorkspace, selectContext, type Suggestion } from "@aitsev/agent-core";
import { newId, type SuggestionView } from "./chatModel";
import type { ChatSession } from "./chat";

const SEVERITY: Record<Suggestion["severity"], vscode.DiagnosticSeverity> = {
  error: vscode.DiagnosticSeverity.Error,
  warning: vscode.DiagnosticSeverity.Warning,
  info: vscode.DiagnosticSeverity.Information,
};
const FOCUS = "bugs errors security exceptions validation performance";

export interface AnalyzerHost {
  model(): string;
  show(suggestions: SuggestionView[], analyzing: boolean): void;
  fix(s: Suggestion): Promise<void>;
}

/**
 * Background review: when a folder is open (and after saves) the server's models look for real
 * problems; findings appear as squiggles, in the Problems view and in the chat panel.
 */
export class Analyzer implements vscode.CodeActionProvider, vscode.Disposable {
  static readonly providedCodeActionKinds = [vscode.CodeActionKind.QuickFix];
  private readonly collection = vscode.languages.createDiagnosticCollection("aitsev");
  private readonly status = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
  private readonly disposables: vscode.Disposable[] = [];
  private suggestions: SuggestionView[] = [];
  private timer: NodeJS.Timeout | undefined;
  private running = false;
  private lastRun = 0;

  constructor(
    private readonly session: ChatSession,
    private readonly host: AnalyzerHost,
    private readonly output: vscode.OutputChannel,
  ) {
    this.status.command = "aitsev.openChat";
    this.status.text = "$(sparkle) Aitsev";
    this.status.tooltip = "Open Aitsev chat";
    this.status.show();
    this.disposables.push(
      this.collection,
      this.status,
      vscode.languages.registerCodeActionsProvider({ scheme: "file" }, this, {
        providedCodeActionKinds: Analyzer.providedCodeActionKinds,
      }),
      vscode.workspace.onDidSaveTextDocument(() => this.schedule(20_000)),
    );
  }

  private enabled(): boolean {
    return vscode.workspace.getConfiguration("aitsev").get<boolean>("backgroundAnalysis", true);
  }

  /** Run soon (debounced), at most once every two minutes in the background. */
  schedule(delayMs: number): void {
    if (!this.enabled()) return;
    if (this.timer) clearTimeout(this.timer);
    const wait = Math.max(delayMs, this.lastRun + 120_000 - Date.now());
    this.timer = setTimeout(() => void this.run(false), wait);
  }

  dispose(): void {
    if (this.timer) clearTimeout(this.timer);
    for (const d of this.disposables) d.dispose();
  }

  get current(): SuggestionView[] {
    return this.suggestions;
  }

  /** Analyse now. ``manual`` runs prompt for sign-in and report errors to the user. */
  async run(manual: boolean): Promise<void> {
    if (this.running) return;
    const folder = vscode.workspace.workspaceFolders?.[0];
    if (!folder) {
      if (manual) void vscode.window.showWarningMessage("Open a folder first: Aitsev analyses the files in your workspace.");
      return;
    }
    const client = await this.session.apiClient(manual);
    if (!client) return;
    this.running = true;
    this.lastRun = Date.now();
    this.status.text = "$(sync~spin) Aitsev analyzing";
    this.host.show(this.suggestions, true);
    try {
      const config = vscode.workspace.getConfiguration("aitsev");
      const scan = await scanWorkspace(folder.uri.fsPath, { exclude: config.get<string[]>("exclude", []) });
      const open = vscode.window.visibleTextEditors
        .filter((e) => e.document.uri.scheme === "file")
        .map((e) => vscode.workspace.asRelativePath(e.document.uri, false));
      const selection = selectContext(scan.files, FOCUS, {
        maxContextBytes: config.get<number>("analysisMaxKb", 120) * 1024,
        pinned: open.filter((p) => scan.files.some((f) => f.path === p)),
      });
      const wanted = new Set(selection.contextPaths);
      const files = Object.fromEntries(scan.files.filter((f) => wanted.has(f.path)).map((f) => [f.path, f.content]));
      if (!Object.keys(files).length) return;
      const result = await client.analyze({ files, model: this.host.model(), limit: 20 });
      this.suggestions = result.suggestions.map((s) => ({ ...s, id: newId() }));
      this.publish(folder);
      if (manual) void vscode.window.showInformationMessage(`Aitsev found ${this.suggestions.length} suggestion(s).`);
    } catch (err) {
      const text = err instanceof Error ? err.message : String(err);
      this.output.appendLine(`[analysis] ${text}`);
      if (manual) void vscode.window.showErrorMessage(`Aitsev analysis failed: ${text}`);
    } finally {
      this.running = false;
      this.status.text = this.suggestions.length ? `$(lightbulb) Aitsev: ${this.suggestions.length}` : "$(sparkle) Aitsev";
      this.host.show(this.suggestions, false);
    }
  }

  private publish(folder: vscode.WorkspaceFolder): void {
    this.collection.clear();
    const byFile = new Map<string, vscode.Diagnostic[]>();
    for (const s of this.suggestions) {
      const line = Math.max(0, s.line - 1);
      const diagnostic = new vscode.Diagnostic(
        new vscode.Range(new vscode.Position(line, 0), new vscode.Position(line, Number.MAX_SAFE_INTEGER)),
        s.detail ? `${s.title}: ${s.detail}` : s.title,
        SEVERITY[s.severity],
      );
      diagnostic.source = "Aitsev";
      diagnostic.code = s.id;
      const list = byFile.get(s.path) ?? [];
      list.push(diagnostic);
      byFile.set(s.path, list);
    }
    for (const [path, list] of byFile) {
      this.collection.set(vscode.Uri.joinPath(folder.uri, ...path.split("/")), list);
    }
  }

  provideCodeActions(_document: vscode.TextDocument, _range: vscode.Range, context: vscode.CodeActionContext): vscode.CodeAction[] {
    const actions: vscode.CodeAction[] = [];
    for (const diagnostic of context.diagnostics) {
      if (diagnostic.source !== "Aitsev") continue;
      const suggestion = this.suggestions.find((s) => s.id === diagnostic.code);
      if (!suggestion) continue;
      const action = new vscode.CodeAction(`Ask Aitsev to fix: ${suggestion.title}`, vscode.CodeActionKind.QuickFix);
      action.command = { command: "aitsev.fixSuggestion", title: "Fix with Aitsev", arguments: [suggestion.id] };
      action.diagnostics = [diagnostic];
      actions.push(action);
    }
    return actions;
  }

  async fix(id: string): Promise<void> {
    const suggestion = this.suggestions.find((s) => s.id === id);
    if (suggestion) await this.host.fix(suggestion);
  }
}
