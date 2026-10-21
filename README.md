# Aitsev

Verified multi-model AI for code, system integration and data migration.

Several AI models plan, build and check each piece of work independently. Their output is run in a sandbox, reviewed by the other models, and judged on the evidence. Nothing is applied until a person approves it.

## Status

| Phase | Scope | State |
| --- | --- | --- |
| 1 | Core engine: model gateway, multi-model pipeline, sandbox verification, approvals, audit log | **Built** |
| 2 | VS Code extension + local agent (workspace scan, context selection, safe apply, git) | **Built** |
| 3 | Integration and data transfer pack (connections, schema discovery, multi-model auto-mapping, dry-run and migration jobs, script export) | **Built** (API) |
| 4 | Desktop app (integrations, mappings, jobs, Dynamics 365) + Dynamics 365 pack (analysis, AI column design, sandbox-first apply) | **Built** |
| 5 | Public release: accounts, plans and usage limits, web app, signed installers with auto-update, release pipeline | **Built** (payment collection pluggable) |

## Repository layout

```
apps/
  vscode/         VS Code extension (works in Cursor, Kiro, Windsurf too)
  desktop/        Electron app for integrations, migrations and Dynamics 365
  backend/        FastAPI core engine (Python 3.12+)
    src/aitsev/
      api/        HTTP endpoints
      db/         SQLAlchemy models and sessions
      gateway/    Model gateway: Ollama Cloud, local Ollama, OpenAI, Anthropic
      pipeline/   Planner, generators, sandbox, reviewers, judge
      integrations/  Connections, schema discovery, auto-mapping, jobs, script export
      crm/        Dynamics 365 pack: org analysis, column design and apply
      worker.py   Durable database-backed task queue
    migrations/   Alembic migrations
    tests/        Unit and end-to-end tests
packages/
  agent-core/     Local agent shared by the extension and the future desktop app:
                  workspace scan, context selection, verify-command detection,
                  API client, conflict-safe apply, git helpers
docker-compose.yml  PostgreSQL + API for local runs
```

Phase 4 adds `apps/desktop`, reusing `packages/agent-core`.

## How a task runs

1. **Plan** — the planner model writes a spec, steps and acceptance criteria, and rates the task small, normal or large.
2. **Generate** — 1, 2 or 3 models (by size) each write a complete solution in parallel.
3. **Verify** — each solution goes through built-in syntax checks, then your setup and verify commands in a sandbox. Failures go back to the same model with the error log, up to 2 repair rounds.
4. **Cross-review** — every passing solution is reviewed by a different model.
5. **Judge** — a judge model picks the winner from labelled candidates using test results first, then reviews. If the judge fails, a deterministic rule picks.
6. **Approve** — the task waits for you. You approve, reject, or retry with feedback.

Every step is in the audit log, and every model call is recorded with its token usage.

## Quick start (Windows, macOS or Linux)

Prerequisites: [uv](https://docs.astral.sh/uv/), Docker Desktop (for the sandbox), an Ollama Cloud API key.

```bash
cp .env.example apps/backend/.env      # Windows PowerShell: Copy-Item .env.example apps/backend/.env
# edit apps/backend/.env: set OLLAMA_API_KEY and AITSEV_API_KEYS

cd apps/backend
uv sync
uv run alembic upgrade head
uv run aitsev-api
```

Open http://localhost:8000/docs, click **Authorize**, enter your API key, then:

1. `GET /v1/models` — check which model names your Ollama Cloud account has, and update the `AITSEV_*_MODEL` settings to match.
2. `POST /v1/tasks` — submit a task, for example:

```json
{
  "title": "Fix add",
  "instructions": "Make add() return the sum of its arguments.",
  "files": {
    "calc.py": "def add(a, b):\n    return a - b\n",
    "test_calc.py": "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"
  },
  "setup_commands": ["pip install --quiet --target .deps pytest"],
  "verify_commands": ["PYTHONPATH=.deps python -m pytest -q"],
  "size": "normal"
}
```

3. Poll `GET /v1/tasks/{id}` until `awaiting_approval`, read `GET /v1/tasks/{id}/result`, then `POST /v1/tasks/{id}/approve`.

### Full stack with Docker Compose

```bash
cp .env.example .env    # fill in the keys
docker compose up --build
```

This runs PostgreSQL and the API, applies migrations on start, and gives the API access to the host Docker daemon for sandboxes.

## Integration and data migration (Phase 3)

Move data between two systems in five calls (all under `/v1`, see `/docs`):

1. **Connect each system** — `POST /connections` with its base URL, auth (API key, bearer, basic, or OAuth2 client credentials), pagination style and a spec: OpenAPI (JSON or YAML), OData `$metadata` (Dynamics 365, SAP, Business Central) or sample records. Credentials are encrypted at rest and never returned.
2. **Discover** — `POST /connections/{id}/discover` reads entities, fields, types, keys, required fields and list/create endpoints. Pass `spec_path` (for example `$metadata`) to fetch the spec from the system itself.
3. **Auto-map** — `POST /mappings` picks a source and target entity. Rules match names (including common CRM synonyms like `email` ↔ `emailaddress1`) and types; then two models propose mappings and conversion steps for anything uncertain. Matches the models agree on get high confidence; disagreements are flagged. Sample records are only sent to models if you set `include_samples`.
4. **Review and approve** — `GET /mappings/{id}` shows each field with its confidence and reason, unmapped fields and blocking issues. Edit with `PUT /mappings/{id}`, then `POST /mappings/{id}/approve`. Conversions use a safe step language (split, map values, dates, numbers, truncate…), never code.
5. **Dry run, then run** — `POST /jobs` with `dry_run: true` transforms and validates every record without writing, and shows a sample. A real run (`dry_run: false`) is only allowed after a clean dry run of the same mapping version. It writes in chunks, records every failed record, and stops itself if more than 5% fail. `POST /jobs/{id}/cancel` stops it.

`GET /mappings/{id}/export` gives you the same migration as a standalone Python script (credentials come from environment variables) to keep in your repo.

Outside development, connections to localhost and private networks are refused, and only HTTPS is allowed.

## Dynamics 365 pack (Phase 4)

Uses a normal connection whose base URL is the Web API root (`https://<org>.crm.dynamics.com/api/data/v9.2`) with OAuth2 client credentials, and an **environment** label (`sandbox`, `production` or `unknown`).

* **Analyse** — `POST /v1/crm/{connection}/analyses` reads table metadata and a sample of records and reports fill rates, empty custom columns, required columns that are empty, duplicates and plain-language suggestions.
* **Design columns** — `POST /v1/crm/{connection}/changes` with a request such as "track loyalty tier, points and join date". Two models propose columns (choice, text, memo, whole number, decimal, yes/no, date); columns both propose are marked as agreed; existing columns and wrong prefixes are dropped.
* **Review, approve, apply** — edit the plan (`PUT`), approve, then apply. Every column's exact Web API payload is shown before anything runs. Columns go into your solution (`MSCRM.SolutionUniqueName`) and the table is published. Anything not marked `sandbox` needs `confirm_production: true`. Existing columns are skipped; failures are reported per column.

## The Aitsev editor, chat and agent

* **Aitsev editor (`ide/`)**: a VS Code fork, like Kiro and Cursor, with the Aitsev chat built in, the Open VSX store, and its own installers (`.github/workflows/ide-release.yml`).
* **Chat (`POST /v1/chat`)**: answers questions about the project (*Ask*) or edits it (*Agent*). There's a model dropdown, with Auto or any model on your plan.
  - When the project has tests, edits are run in the sandbox first, with one automatic repair round if they fail.
  - The editor applies edits in *Supervised* mode (diff, then Accept or Reject) or on *Autopilot* (written directly, with Undo, and never overwriting files you changed meanwhile).
* **Background analysis (`POST /v1/analyze`)**: line-level suggestions shown as squiggles, with an *Ask Aitsev to fix* quick fix.
* **CRM pack**: Dynamics 365, Salesforce, HubSpot and Zoho CRM. It analyses data quality and creates fields from plain English through each platform's metadata API (`crm/platforms.py`). Connections can use OAuth2 refresh tokens (Zoho), and Studio has a preset for each CRM.

## Accounts, plans and the web app (Phase 5)

Anyone can now sign up and use Aitsev from the browser, the desktop app or VS Code.

* **Sign-in options.** Browser sessions use an HttpOnly, SameSite=Strict cookie; changes also need the `X-Aitsev-Client: web` header (CSRF guard). Personal API keys (`aitsev_…`, created under **Account**) connect the desktop app and the extension. Operator keys in `AITSEV_API_KEYS` still work for automation and admin, and are not metered.
* **Account endpoints.** `POST /v1/auth/signup|login|logout|verify|resend-verification|forgot-password|reset-password`, `GET /v1/account`, `GET /v1/account/usage`, `POST /v1/account/password`, `GET|POST /v1/account/keys`, `DELETE /v1/account/keys/{id}`, `GET /v1/plans`.
* **Security.** Passwords are hashed with scrypt; keys, sessions and email tokens are stored only as SHA-256 hashes. Login, sign-up and email sending are rate-limited. A password reset ends every session. Responses carry CSP, `nosniff`, `X-Frame-Options` and (with secure cookies) HSTS.
* **Plans and limits.** Free 300k model tokens / 20 code tasks / 1,000 migrated records per month; Pro 5M / 500 / 100k; Team 25M / 3,000 / 1M; Enterprise unlimited. Every model call is metered to the account it ran for. When a limit is used up, new work gets HTTP 402; real migration jobs are capped at the records left this month. Accounts must confirm their email first (outside development).
* **Admin.** The first account on a new server is the admin. `GET /v1/admin/accounts` and `PUT /v1/admin/accounts/{id}` (plan, disabled, admin, verified), also on the web **Account** page. Payment collection is not wired yet: set the plan after payment, or connect your payment provider's webhook to that endpoint.
* **Model choice.** Your server holds every provider key and routes each task (planning, review and judging are always server-side). Users can pick up to three models to write their code (`GET /v1/models/catalog`, `models` on `POST /v1/tasks`). Ollama models are on every plan; premium models (Claude, GPT) need Pro or above and count ×5–×10 towards the token allowance.
* **Browser sign-in for apps.** The desktop app and the editor extension call `POST /v1/auth/device/start`, open `/app/#/connect?code=…`, and receive their own API key once the user clicks **Allow** (`/v1/auth/device/poll`). Nobody copies keys.
* **Finished-product install.** `Aitsev-Setup.exe` is a one-click installer with a progress window that launches the app. The app carries the editor extension and installs it into VS Code, Cursor, Kiro, Windsurf and VSCodium. The website's Download buttons (`/download/windows|mac|mac-intel|linux|vscode`) redirect to the latest GitHub release (`AITSEV_DOWNLOAD_BASE_URL`).
* **Web app.** `pnpm --filter aitsev-desktop build:web` builds `apps/desktop/dist-web`; set `AITSEV_WEB_DIST_DIR` to it and the API serves the landing page at `/` and the app at `/app`. The Docker image does this for you.

## Releasing

Bump the version in `apps/desktop/package.json` and `apps/vscode/package.json`, then push a tag such as `v0.5.0`. The `release` workflow builds the Windows, macOS and Linux installers and publishes them to GitHub Releases (the installed apps update themselves from there), attaches the `.vsix`, and pushes the server image to `ghcr.io/<owner>/<repo>/server`. Signing switches on when the certificate secrets listed at the top of `.github/workflows/release.yml` exist; otherwise the installers are unsigned.

## Desktop app

```bash
pnpm install
cd apps/desktop && pnpm start
```

Screens for connections, mappings review, jobs and Dynamics 365. Installers: `pnpm run dist` (or the `desktop-ci` workflow for Windows). See `apps/desktop/README.md`.

## VS Code extension

```bash
pnpm install
pnpm package:vscode                     # builds apps/vscode/aitsev.vsix
code --install-extension apps/vscode/aitsev.vsix
```

Cursor, Kiro and Windsurf install `.vsix` files the same way (Extensions view → … → Install from VSIX).

In the editor: open the **Aitsev** view in the activity bar → **Set API key** → describe a task → **Run task**. When it is ready, click a changed file to see the diff, then **Approve & apply**. The extension uploads your workspace's text files (never `.env` files, keys, build output or dependency folders), tells the models which files matter most, and refuses to overwrite files you edited after submitting unless you confirm. See `apps/vscode/README.md` for settings.

## Sandbox notes

* **Setup commands** run first, with network access, to install dependencies. Install into the project folder (for example `.venv`, `.deps` or `node_modules`) so the verify step can use them.
* **Verify commands** run without network access, with no Linux capabilities, a process limit, and memory and CPU limits.
* `AITSEV_SANDBOX_MODE=local` runs commands directly on your machine with no isolation. It is refused outside `AITSEV_ENVIRONMENT=development`.
* Hosts without Docker (for example Railway) cannot run the sandbox. Run Phase 1 on your machine or a VM with Docker. In Phase 2 verification moves to the user's machine through the local agent.

## Development

Backend:

```bash
cd apps/backend
uv sync
uv run ruff check src tests migrations
uv run ruff format --check src tests migrations
uv run mypy
uv run pytest --cov
```

`pytest -m docker` runs the real Docker sandbox tests when Docker is running. `pytest -m live` runs a live Ollama Cloud check when `OLLAMA_API_KEY` is set.

TypeScript (agent-core and the extension):

```bash
pnpm install
pnpm -r run typecheck
pnpm -r run test
# end-to-end: bundled extension + real backend (needs apps/backend/.venv from `uv sync`)
cd apps/vscode && AITSEV_E2E=1 pnpm exec vitest run      # PowerShell: $env:AITSEV_E2E="1"; pnpm exec vitest run
```
