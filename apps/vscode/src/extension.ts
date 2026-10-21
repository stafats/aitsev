import * as vscode from "vscode";
import { Analyzer } from "./analysis";
import { ChatController } from "./chat";
import { Controller } from "./controller";
import { TaskPanel } from "./panel";
import { RESULT_SCHEME, ResultDocuments } from "./resultDocuments";
import { TaskStore } from "./store";

export function activate(context: vscode.ExtensionContext): void {
  const output = vscode.window.createOutputChannel("Aitsev");
  const results = new ResultDocuments();
  const store = new TaskStore(context.workspaceState);
  // the panel forwards messages to the controller, which is created right after it
  let controller: Controller | undefined;
  const panel = new TaskPanel(context.extensionUri, (message) => controller?.handle(message));
  controller = new Controller(context.secrets, store, panel, results, output, context.globalState);
  const tasks = controller;
  const session = {
    apiClient: (prompt?: boolean) => tasks.apiClient(prompt),
    signIn: () => tasks.signIn(),
    isSignedIn: () => tasks.isSignedIn(),
  };
  const chat = new ChatController(context.extensionUri, session, results, context.workspaceState, context.globalState, output);
  const analyzer = new Analyzer(session, {
    model: () => chat.model,
    show: (suggestions, analyzing) => chat.setSuggestions(suggestions, analyzing),
    fix: (s) => chat.fix(s),
  }, output);

  context.subscriptions.push(
    output,
    results,
    controller,
    chat,
    analyzer,
    vscode.workspace.registerTextDocumentContentProvider(RESULT_SCHEME, results),
    vscode.window.registerWebviewViewProvider(ChatController.viewId, chat, {
      webviewOptions: { retainContextWhenHidden: true },
    }),
    vscode.window.registerWebviewViewProvider(TaskPanel.viewId, panel, {
      webviewOptions: { retainContextWhenHidden: true },
    }),
    context.secrets.onDidChange((e) => {
      if (e.key !== "aitsev.apiKey") return;
      void chat.loadModels();
      if (tasks.isSignedIn()) analyzer.schedule(3_000);
    }),
    vscode.commands.registerCommand("aitsev.openChat", () => chat.reveal()),
    vscode.commands.registerCommand("aitsev.newChat", () => chat.handle({ type: "newChat" })),
    vscode.commands.registerCommand("aitsev.askAboutSelection", () => chat.askAboutSelection()),
    vscode.commands.registerCommand("aitsev.analyzeNow", () => analyzer.run(true)),
    vscode.commands.registerCommand("aitsev.fixSuggestion", (id: string) => analyzer.fix(id)),
    vscode.commands.registerCommand("aitsev.newTask", () => controller?.newTask()),
    vscode.commands.registerCommand("aitsev.newTaskForFiles", (uri?: vscode.Uri, uris?: vscode.Uri[]) => {
      const picked = uris?.length ? uris : uri ? [uri] : [];
      return controller?.newTask(picked);
    }),
    vscode.commands.registerCommand("aitsev.signIn", () => controller?.signIn()),
    vscode.commands.registerCommand("aitsev.chooseModels", () => controller?.chooseModels()),
    vscode.commands.registerCommand("aitsev.setApiKey", () => controller?.setApiKey()),
    vscode.commands.registerCommand("aitsev.clearApiKey", () => controller?.clearApiKey()),
    vscode.commands.registerCommand("aitsev.checkConnection", () => controller?.checkConnection()),
    vscode.commands.registerCommand("aitsev.refresh", () => controller?.refresh()),
  );

  void controller.start().then(() => {
    if (tasks.isSignedIn()) analyzer.schedule(5_000);
  });
  // In the Aitsev IDE the chat is the home screen: open it on first launch.
  if (vscode.env.appName === "Aitsev" && !context.globalState.get<boolean>("aitsev.ide.chatShown", false)) {
    void context.globalState.update("aitsev.ide.chatShown", true);
    void chat.reveal();
  }
}

export function deactivate(): void {
  // disposables registered on the context are cleaned up by VS Code
}
