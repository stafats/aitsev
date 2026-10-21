# Aitsev server image: the API plus the web app (landing page at /, app at /app).
# Build from the repository root:  docker build -f apps/backend/Dockerfile .

# ---- web app ----
FROM node:22-slim AS web
WORKDIR /src
RUN corepack enable
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml tsconfig.base.json ./
COPY apps/desktop/package.json apps/desktop/
COPY packages/agent-core/package.json packages/agent-core/
COPY apps/vscode/package.json apps/vscode/
RUN pnpm install --frozen-lockfile --filter aitsev-desktop... --ignore-scripts
COPY apps/desktop apps/desktop
RUN cd apps/desktop && node build.mjs --web

# ---- API ----
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH" \
    AITSEV_WEB_DIST_DIR=/app/web

# Docker CLI only (no daemon): the sandbox starts sibling containers through the host socket.
COPY --from=docker:27-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv

WORKDIR /app

# Dependencies first (cached layer), exactly as pinned in uv.lock.
COPY apps/backend/pyproject.toml apps/backend/uv.lock apps/backend/README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY apps/backend/alembic.ini ./
COPY apps/backend/src ./src
COPY apps/backend/migrations ./migrations
RUN uv sync --frozen --no-dev

COPY --from=web /src/apps/desktop/dist-web ./web

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

# Apply migrations, then serve.
CMD ["sh", "-c", "alembic upgrade head && aitsev-api"]
