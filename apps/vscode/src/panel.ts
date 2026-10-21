import * as vscode from "vscode";
import { isPanelMessage, makeNonce, type PanelMessage, type ViewState } from "./model";

export { isPanelMessage, type PanelMessage };

/** The sidebar webview. All logic lives in the controller; this only renders state. */
export class TaskPanel implements vscode.WebviewViewProvider {
  static readonly viewId = "aitsev.panel";
  private view: vscode.WebviewView | undefined;
  private lastState: ViewState | undefined;

  constructor(
    private readonly extensionUri: vscode.Uri,
    private readonly onMessage: (message: PanelMessage) => void,
  ) {}

  resolveWebviewView(view: vscode.WebviewView): void {
    this.view = view;
    const media = vscode.Uri.joinPath(this.extensionUri, "media");
    view.webview.options = { enableScripts: true, localResourceRoots: [media] };
    view.webview.html = this.html(view.webview, media);
    view.webview.onDidReceiveMessage((raw: unknown) => {
      if (isPanelMessage(raw)) this.onMessage(raw);
    });
    view.onDidDispose(() => {
      this.view = undefined;
    });
  }

  render(state: ViewState): void {
    this.lastState = state;
    void this.view?.webview.postMessage({ type: "state", state });
  }

  replay(): void {
    if (this.lastState) this.render(this.lastState);
  }

  async reveal(): Promise<void> {
    await vscode.commands.executeCommand("aitsev.panel.focus");
  }

  private html(webview: vscode.Webview, media: vscode.Uri): string {
    const nonce = makeNonce();
    const script = webview.asWebviewUri(vscode.Uri.joinPath(media, "panel.js"));
    const style = webview.asWebviewUri(vscode.Uri.joinPath(media, "panel.css"));
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
<title>Aitsev</title>
</head>
<body>
<div id="app"></div>
<script nonce="${nonce}" src="${script.toString()}"></script>
</body>
</html>`;
  }
}
