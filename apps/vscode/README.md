# Aitsev for VS Code

Describe a change; several AI models build it independently, run your tests in a sandbox, review each other's work and pick the best verified result. You see the diff and decide. Nothing touches your files until you approve.

Works in VS Code, Cursor, Kiro, Windsurf and other VS Code-based editors.

## Get started

1. Run an Aitsev server (see the repository README) or use your team's server.
2. Open the **Aitsev** view in the activity bar and click **Set API key**.
3. Type what you want and press **Run task** (Ctrl+Enter).

You can also right-click files in the explorer or editor and choose **Aitsev: New Task for These Files**.

## What gets sent

* Your workspace's text files, so the sandbox can build and test the change. `.gitignore`d files, `.env` files, keys and certificates, build output and dependency folders are never sent. Add more patterns with `aitsev.exclude`.
* A smaller "context" set that the models read: the files you have open or selected, files named in your description, related tests, and other files matching the task.

## Review and apply

When a task is ready you get a notification. In the panel:

* Click a changed file to open a diff of your file against the proposal.
* **Approve & apply** writes the changes. If you edited any of those files after submitting, you are asked before anything is overwritten.
* By default a new git branch `aitsev/<task>` is created first. Turn on `aitsev.autoCommit` to also commit the applied files.
* **Reject** or **Retry with feedback** sends your comments back to the models.

## Settings

| Setting | Default | Purpose |
| --- | --- | --- |
| `aitsev.serverUrl` | `http://localhost:8000` | Aitsev server |
| `aitsev.defaultSize` | `auto` | 1, 2 or 3 models, or let the planner decide |
| `aitsev.setupCommands` | detected | Install dependencies in the sandbox (network on) |
| `aitsev.verifyCommands` | detected | Build/lint/test commands that must pass (network off) |
| `aitsev.sandboxImage` | detected | Docker image for the sandbox |
| `aitsev.exclude` | `[]` | Extra patterns never to upload |
| `aitsev.maxContextKb` | `400` | Size of the files the models read |
| `aitsev.createBranch` | `true` | Create a branch before applying |
| `aitsev.autoCommit` | `false` | Commit applied files |

Your API key is stored in the operating system keychain through VS Code's secret storage.
