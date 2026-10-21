# Aitsev backend (core engine)

FastAPI service that runs Aitsev's multi-model pipeline:

**plan → generate with several models in parallel → verify in a sandbox (with repair loop) → cross-review → judge → wait for approval**

Nothing is applied until a person approves the result. Every step is written to an audit log, and every model call is recorded with its token usage.

## Run locally

```bash
uv sync                           # installs exactly what uv.lock pins
cp ../../.env.example .env        # then fill in OLLAMA_API_KEY and AITSEV_API_KEYS
uv run alembic upgrade head
uv run aitsev-api                 # http://localhost:8000/docs
```

## Checks

```bash
uv run ruff check src tests migrations
uv run ruff format --check src tests migrations
uv run mypy                       # strict; add --platform win32 to check Windows too
uv run pytest --cov               # fails under 90% coverage
```

Docker sandbox tests run only when a Docker daemon is available (`-m docker`). Live model tests run only when `OLLAMA_API_KEY` is set (`-m live`).

## API (v1)

All endpoints except `/health` need `Authorization: Bearer <api key>` (or `X-API-Key`).

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/health` | Liveness and database check |
| GET | `/v1/models` | Configured model roles and the models each provider offers |
| POST | `/v1/tasks` | Submit a task (instructions, files, setup and verify commands, size) |
| GET | `/v1/tasks` | List your tasks (`?status=`, `limit`, `offset`) |
| GET | `/v1/tasks/{id}` | Status, plan, candidates, decision and token usage |
| GET | `/v1/tasks/{id}/events` | Audit log (`?after=<event id>` for polling) |
| GET | `/v1/tasks/{id}/result` | Winning changes: full file contents and unified diffs |
| POST | `/v1/tasks/{id}/approve` | Approve the result |
| POST | `/v1/tasks/{id}/reject` | Reject with a reason |
| POST | `/v1/tasks/{id}/retry` | Re-run with feedback (new run number) |
| POST | `/v1/tasks/{id}/cancel` | Cancel a queued, running or waiting task |
| POST/GET/DELETE | `/v1/connections` | External systems (credentials encrypted, never returned) |
| POST | `/v1/connections/{id}/discover` | Read entities and fields from OpenAPI, OData `$metadata` or samples |
| GET | `/v1/connections/{id}/schema` | The discovered schema |
| POST/GET | `/v1/mappings` | Start auto-mapping between two entities; list mappings |
| GET/PUT | `/v1/mappings/{id}` | Review and edit field mappings |
| POST | `/v1/mappings/{id}/approve` | Approve (only when no blocking issues remain) |
| GET | `/v1/mappings/{id}/export` | Download the migration as a standalone Python script |
| POST/GET | `/v1/jobs` | Dry run or real migration run; list jobs |
| GET | `/v1/jobs/{id}` | Progress, counts, failed records and a sample |
| POST | `/v1/jobs/{id}/cancel` | Stop a running job |
| POST/GET | `/v1/crm/{connection}/analyses`, `/v1/crm/analyses/{id}` | Dynamics 365 data-quality analysis |
| POST/GET | `/v1/crm/{connection}/changes`, `/v1/crm/changes/{id}` | AI-designed column plans |
| PUT | `/v1/crm/changes/{id}` | Edit the plan |
| POST | `/v1/crm/changes/{id}/approve` | Approve the plan |
| POST | `/v1/crm/changes/{id}/apply` | Create the columns (non-sandbox needs `confirm_production`) |

## Task statuses

`queued → planning → generating → reviewing → judging → awaiting_approval → approved | rejected`

A task can also end as `failed` (error message in `error`) or `cancelled`. `retry` sends a waiting, rejected, failed or cancelled task back to `queued` with your feedback.

## Configuration

Settings come from environment variables (see `.env.example` at the repo root). Model roles use `provider/model` references:

* `ollama-cloud/<model>` — needs `OLLAMA_API_KEY`
* `ollama-local/<model>` — needs `AITSEV_OLLAMA_LOCAL_BASE_URL`
* `openai/<model>` — needs `OPENAI_API_KEY`
* `anthropic/<model>` — needs `ANTHROPIC_API_KEY`

Models whose provider is not configured are skipped automatically.
