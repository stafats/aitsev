# Aitsev desktop

The Aitsev app for integrations, data migration and Dynamics 365, with no coding environment needed.

* **Connections** — add the systems you move data between (OpenAPI, OData `$metadata` or samples; API key, bearer, basic or OAuth2), then discover their entities.
* **Mappings** — auto-map one entity to another, review every field with its confidence, the models' reasons and a decision per field, then approve. Dry run, run the migration, watch progress and failed records, or export it as a Python script.
* **Dynamics 365** — analyse data quality (fill rates, unused custom columns, required-but-empty columns, duplicates) and design new columns with AI. Review the plan, approve, and apply to a sandbox; non-sandbox environments ask for explicit confirmation.
* **Code tasks** — a read-only overview; coding happens in the VS Code extension.

## Run from source

```bash
pnpm install
cd apps/desktop
pnpm start          # builds and opens the app
```

Open **Settings**, enter the server URL and your API key, and press **Test connection**.

## Build an installer

```bash
pnpm run dist       # Windows: .exe (NSIS), macOS: .dmg, Linux: AppImage — built for the OS you run it on
```

The GitHub Actions workflow `desktop-ci` builds the Windows installer on every push. Installers are unsigned until a code-signing certificate is added, so Windows SmartScreen will warn on first launch.

## Security design

* The API key is encrypted with the operating system's key store (Electron `safeStorage`: DPAPI on Windows, Keychain on macOS, libsecret on Linux) and only the background process reads it.
* The window runs with context isolation, the Chromium sandbox, no Node access and a strict Content Security Policy. It can only call a small bridge; every server request goes through an allow-list (`/health` and `/v1/...` with GET, POST, PUT or DELETE).
* Server data is always rendered as text, never as HTML. Navigation and new windows are blocked.

## Tests

```bash
pnpm run typecheck
pnpm run test       # main-process logic and every screen (happy-dom)
pnpm run smoke      # launches the real app and checks it starts (needs a display; use xvfb-run on Linux)
```
