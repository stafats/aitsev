import * as vscode from "vscode";

export const RESULT_SCHEME = "aitsev-result";

/**
 * Read-only documents holding the AI's proposed file contents, so they can be shown in
 * VS Code's diff editor next to the file on disk.
 */
export class ResultDocuments implements vscode.TextDocumentContentProvider {
  private readonly contents = new Map<string, string>();
  private readonly emitter = new vscode.EventEmitter<vscode.Uri>();
  readonly onDidChange = this.emitter.event;

  uriFor(taskId: string, path: string): vscode.Uri {
    return vscode.Uri.from({ scheme: RESULT_SCHEME, path: `/${taskId}/${path}` });
  }

  set(taskId: string, path: string, content: string): vscode.Uri {
    const uri = this.uriFor(taskId, path);
    this.contents.set(uri.toString(), content);
    this.emitter.fire(uri);
    return uri;
  }

  provideTextDocumentContent(uri: vscode.Uri): string {
    return this.contents.get(uri.toString()) ?? "";
  }

  dispose(): void {
    this.emitter.dispose();
    this.contents.clear();
  }
}
