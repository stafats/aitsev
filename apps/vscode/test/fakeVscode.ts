/**
 * A minimal in-memory stand-in for the `vscode` module, enough to load and drive the bundled
 * extension in Node. It records what the extension registers and shows.
 */
import Module from "node:module";
import * as path from "node:path";

type Listener<T> = (value: T) => unknown;

class EventEmitter<T> {
  private listeners: Listener<T>[] = [];
  event = (listener: Listener<T>) => {
    this.listeners.push(listener);
    return { dispose: () => (this.listeners = this.listeners.filter((l) => l !== listener)) };
  };
  fire(value: T): void {
    for (const l of this.listeners) l(value);
  }
  dispose(): void {
    this.listeners = [];
  }
}

class Uri {
  constructor(
    readonly scheme: string,
    readonly path: string,
    readonly query = "",
  ) {}
  get fsPath(): string {
    return this.path;
  }
  static file(p: string): Uri {
    return new Uri("file", path.resolve(p));
  }
  static from(parts: { scheme: string; path: string; query?: string }): Uri {
    return new Uri(parts.scheme, parts.path, parts.query ?? "");
  }
  static joinPath(base: Uri, ...segments: string[]): Uri {
    return new Uri(base.scheme, path.join(base.path, ...segments));
  }
  toString(): string {
    return `${this.scheme}://${this.path}${this.query ? `?${this.query}` : ""}`;
  }
}

class Position {
  constructor(
    readonly line: number,
    readonly character: number,
  ) {}
}

class Range {
  constructor(
    readonly start: Position,
    readonly end: Position,
  ) {}
}

class Diagnostic {
  source?: string;
  code?: string | number;
  constructor(
    readonly range: Range,
    readonly message: string,
    readonly severity: number,
  ) {}
}

class CodeAction {
  command?: { command: string; title: string; arguments?: unknown[] };
  diagnostics?: Diagnostic[];
  constructor(
    readonly title: string,
    readonly kind: string,
  ) {}
}

export interface FakeHarness {
  module: Record<string, unknown>;
  commands: Map<string, (...args: unknown[]) => unknown>;
  executed: Array<{ command: string; args: unknown[] }>;
  messages: Array<{ level: string; text: string; items: string[] }>;
  inputAnswers: Array<string | undefined>;
  warningAnswers: Array<string | undefined>;
  config: Record<string, unknown>;
  secrets: Map<string, string>;
  posted: unknown[];
  chatPosted: unknown[];
  chatHtml: () => string;
  sendFromChat: (message: unknown) => void;
  diagnostics: Map<string, Diagnostic[]>;
  codeActions: () => { provideCodeActions: (doc: unknown, range: unknown, ctx: { diagnostics: Diagnostic[] }) => CodeAction[] } | undefined;
  statusBar: { text: string };
  saveListeners: Array<() => void>;
  shownDocuments: string[];
  opened: string[];
  progressTitles: string[];
  cancelProgress: () => void;
  quickPickAnswers: Array<(items: Array<{ label: string }>) => unknown>;
  globalState: Map<string, unknown>;
  webviewHtml: () => string;
  sendFromWebview: (message: unknown) => void;
  context: Record<string, unknown>;
  output: string[];
  workspaceRoot: string;
  install: () => () => void;
}

export function createFakeVscode(workspaceRoot: string, packageDefaults: Record<string, unknown>): FakeHarness {
  const commands = new Map<string, (...args: unknown[]) => unknown>();
  const executed: Array<{ command: string; args: unknown[] }> = [];
  const messages: Array<{ level: string; text: string; items: string[] }> = [];
  const inputAnswers: Array<string | undefined> = [];
  const warningAnswers: Array<string | undefined> = [];
  const config: Record<string, unknown> = {};
  const secrets = new Map<string, string>();
  const posted: unknown[] = [];
  const output: string[] = [];
  const secretChanges = new EventEmitter<{ key: string }>();
  let html = "";
  let receive: (m: unknown) => void = () => undefined;
  let chatHtmlValue = "";
  let chatReceive: (m: unknown) => void = () => undefined;
  const chatPosted: unknown[] = [];
  const diagnostics = new Map<string, Diagnostic[]>();
  let codeActionProvider: ReturnType<FakeHarness["codeActions"]>;
  const statusBar = { text: "", command: "", tooltip: "", show: () => undefined, dispose: () => undefined };
  const saveListeners: Array<() => void> = [];
  const shownDocuments: string[] = [];
  const state = new Map<string, unknown>();
  const globalState = new Map<string, unknown>();
  const opened: string[] = [];
  const progressTitles: string[] = [];
  const quickPickAnswers: Array<(items: Array<{ label: string }>) => unknown> = [];
  const cancelEmitter = new EventEmitter<void>();

  const folder = { uri: Uri.file(workspaceRoot), name: path.basename(workspaceRoot), index: 0 };

  const show =
    (level: string) =>
    (text: string, ...rest: unknown[]): Promise<string | undefined> => {
      const items = rest.filter((r): r is string => typeof r === "string");
      messages.push({ level, text, items });
      if (level === "warning" && items.length) return Promise.resolve(warningAnswers.shift());
      return Promise.resolve(undefined);
    };

  const vscodeModule: Record<string, unknown> = {
    Uri: Object.assign(Uri, { parse: (value: string) => ({ toString: () => value, scheme: value.split(":")[0] }) }),
    EventEmitter,
    ProgressLocation: { Notification: 15 },
    env: {
      appName: "Fake Code",
      openExternal: async (uri: { toString: () => string }) => {
        opened.push(uri.toString());
        return true;
      },
    },
    commands: {
      registerCommand: (id: string, fn: (...args: unknown[]) => unknown) => {
        commands.set(id, fn);
        return { dispose: () => commands.delete(id) };
      },
      executeCommand: async (command: string, ...args: unknown[]) => {
        executed.push({ command, args });
        const fn = commands.get(command);
        return fn ? fn(...args) : undefined;
      },
    },
    window: {
      createOutputChannel: () => ({ appendLine: (l: string) => output.push(l), dispose: () => undefined }),
      showInputBox: async () => inputAnswers.shift(),
      showInformationMessage: show("info"),
      showWarningMessage: show("warning"),
      showErrorMessage: show("error"),
      showWorkspaceFolderPick: async () => folder,
      showQuickPick: async (items: Array<{ label: string }>) => {
        const answer = quickPickAnswers.shift();
        return answer ? answer(items) : undefined;
      },
      withProgress: async (options: { title: string }, task: (p: unknown, token: unknown) => Promise<unknown>) => {
        progressTitles.push(options.title);
        return task({ report: () => undefined }, { onCancellationRequested: cancelEmitter.event, isCancellationRequested: false });
      },
      registerWebviewViewProvider: (id: string, provider: { resolveWebviewView: (v: unknown) => void }) => {
        const isChat = id === "aitsev.chat";
        const view = {
          webview: {
            options: {},
            cspSource: "vscode-resource:",
            set html(value: string) {
              if (isChat) chatHtmlValue = value;
              else html = value;
            },
            get html() {
              return isChat ? chatHtmlValue : html;
            },
            asWebviewUri: (u: Uri) => u,
            postMessage: async (m: unknown) => {
              (isChat ? chatPosted : posted).push(m);
              return true;
            },
            onDidReceiveMessage: (fn: (m: unknown) => void) => {
              if (isChat) chatReceive = fn;
              else receive = fn;
              return { dispose: () => undefined };
            },
          },
          onDidDispose: () => ({ dispose: () => undefined }),
        };
        provider.resolveWebviewView(view);
        return { dispose: () => undefined };
      },
      visibleTextEditors: [],
      activeTextEditor: undefined,
      createStatusBarItem: () => statusBar,
      showTextDocument: async (doc: { uri: Uri }) => {
        shownDocuments.push(doc.uri.path);
        return {};
      },
    },
    languages: {
      createDiagnosticCollection: () => ({
        set: (uri: Uri, list: Diagnostic[]) => diagnostics.set(uri.path, list),
        clear: () => diagnostics.clear(),
        dispose: () => diagnostics.clear(),
      }),
      registerCodeActionsProvider: (_selector: unknown, provider: ReturnType<FakeHarness["codeActions"]>) => {
        codeActionProvider = provider;
        return { dispose: () => undefined };
      },
    },
    Position,
    Range,
    Diagnostic,
    DiagnosticSeverity: { Error: 0, Warning: 1, Information: 2, Hint: 3 },
    CodeAction,
    CodeActionKind: { QuickFix: "quickfix" },
    StatusBarAlignment: { Left: 1, Right: 2 },
    workspace: {
      workspaceFolders: [folder],
      getWorkspaceFolder: (uri: Uri) => (uri.path.startsWith(workspaceRoot) ? folder : undefined),
      asRelativePath: (uri: Uri) => path.relative(workspaceRoot, uri.path).split(path.sep).join("/"),
      getConfiguration: (section: string) => ({
        get: <T>(key: string, fallback: T): T => {
          const full = `${section}.${key}`;
          if (full in config) return config[full] as T;
          if (full in packageDefaults) return packageDefaults[full] as T;
          return fallback;
        },
      }),
      onDidChangeConfiguration: () => ({ dispose: () => undefined }),
      onDidSaveTextDocument: (fn: () => void) => {
        saveListeners.push(fn);
        return { dispose: () => undefined };
      },
      openTextDocument: async (uri: Uri) => ({ uri }),
      registerTextDocumentContentProvider: () => ({ dispose: () => undefined }),
    },
  };

  const context = {
    extensionUri: Uri.file(path.resolve(__dirname, "..")),
    subscriptions: [] as Array<{ dispose: () => unknown }>,
    workspaceState: {
      get: <T>(key: string, fallback: T): T => (state.has(key) ? (state.get(key) as T) : fallback),
      update: async (key: string, value: unknown) => {
        state.set(key, JSON.parse(JSON.stringify(value)));
      },
    },
    globalState: {
      get: <T>(key: string, fallback: T): T => (globalState.has(key) ? (globalState.get(key) as T) : fallback),
      update: async (key: string, value: unknown) => {
        if (value === undefined) globalState.delete(key);
        else globalState.set(key, JSON.parse(JSON.stringify(value)));
      },
    },
    secrets: {
      get: async (key: string) => secrets.get(key),
      store: async (key: string, value: string) => {
        secrets.set(key, value);
        secretChanges.fire({ key });
      },
      delete: async (key: string) => {
        secrets.delete(key);
        secretChanges.fire({ key });
      },
      onDidChange: secretChanges.event,
    },
  };

  function install(): () => void {
    const loader = Module as unknown as { _load: (request: string, ...rest: unknown[]) => unknown };
    const original = loader._load;
    loader._load = function (request: string, ...rest: unknown[]) {
      if (request === "vscode") return vscodeModule;
      return original.call(this, request, ...rest);
    };
    return () => {
      loader._load = original;
    };
  }

  return {
    module: vscodeModule,
    commands,
    executed,
    messages,
    inputAnswers,
    warningAnswers,
    config,
    secrets,
    posted,
    chatPosted,
    chatHtml: () => chatHtmlValue,
    sendFromChat: (m) => chatReceive(m),
    diagnostics,
    codeActions: () => codeActionProvider,
    statusBar,
    saveListeners,
    shownDocuments,
    opened,
    progressTitles,
    cancelProgress: () => cancelEmitter.fire(),
    quickPickAnswers,
    globalState,
    webviewHtml: () => html,
    sendFromWebview: (m) => receive(m),
    context,
    output,
    workspaceRoot,
    install,
  };
}

/** Default values of every contributed setting, read from package.json. */
export function packageDefaults(pkg: {
  contributes: { configuration: { properties: Record<string, { default?: unknown }> } };
}): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [key, spec] of Object.entries(pkg.contributes.configuration.properties)) out[key] = spec.default;
  return out;
}
