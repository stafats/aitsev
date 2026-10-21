"""FastAPI application factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from aitsev import __version__
from aitsev.accounts.email import EmailSender, sender_from_settings
from aitsev.api import accounts, chat, crm, integrations, system, tasks
from aitsev.audit import DbUsageRecorder
from aitsev.config import SandboxMode, Settings, get_settings
from aitsev.crm.service import CrmService
from aitsev.db.session import Database
from aitsev.gateway import ModelGateway
from aitsev.integrations.service import IntegrationService
from aitsev.pipeline.orchestrator import Orchestrator
from aitsev.pipeline.sandbox import DockerRunner, LocalRunner, Runner
from aitsev.worker import Worker

log = logging.getLogger("aitsev")

PAGE_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)
# Interactive API docs load their own scripts; everything else gets the strict policy.
DOCS_PATHS = ("/docs", "/redoc", "/openapi.json")


# File names the release workflow publishes (see apps/desktop/package.json "build").
DOWNLOADS = {
    # Aitsev IDE (the VS Code-based editor with Aitsev built in)
    "windows": "Aitsev-IDE-Setup.exe",
    "mac": "Aitsev-IDE-mac-arm64.dmg",
    "linux": "Aitsev-IDE-linux-x64.deb",
    "linux-tar": "Aitsev-IDE-linux-x64.tar.gz",
    # Aitsev Studio (integrations, migration and CRM desktop app)
    "studio-windows": "Aitsev-Studio-Setup.exe",
    "studio-mac": "Aitsev-Studio-mac-arm64.dmg",
    "studio-mac-intel": "Aitsev-Studio-mac-x64.dmg",
    "studio-linux": "Aitsev-Studio-linux.AppImage",
    # Extension for VS Code, Cursor, Kiro and Windsurf
    "vscode": "aitsev.vsix",
}


def add_downloads(app: FastAPI, settings: Settings) -> None:
    @app.get("/download/{platform}", include_in_schema=False)
    async def download(platform: str) -> RedirectResponse:
        name = DOWNLOADS.get(platform)
        if name is None:
            raise HTTPException(404, f"Unknown platform. Choose one of: {', '.join(DOWNLOADS)}")
        if not settings.download_base_url:
            raise HTTPException(404, "Downloads are not published yet")
        return RedirectResponse(f"{settings.download_base_url.rstrip('/')}/{name}", 302)


def mount_web(app: FastAPI, web_dir: str) -> None:
    """Serve the built web app: landing page at /, the app at /app."""
    root = Path(web_dir).resolve()
    if not (root / "app" / "index.html").is_file():
        raise RuntimeError(
            f"AITSEV_WEB_DIST_DIR={web_dir} does not contain app/index.html; "
            "build it with: pnpm --filter aitsev-desktop build:web"
        )
    landing = root / "landing.html"

    @app.get("/", include_in_schema=False)
    async def landing_page() -> FileResponse:
        return FileResponse(landing if landing.is_file() else root / "app" / "index.html")

    @app.get("/landing.css", include_in_schema=False)
    async def landing_css() -> FileResponse:
        return FileResponse(root / "landing.css", media_type="text/css")

    app.mount("/app", StaticFiles(directory=root / "app", html=True), name="web")


def build_runner(settings: Settings) -> Runner:
    if settings.sandbox_mode == SandboxMode.LOCAL:
        if settings.environment != "development":
            raise RuntimeError("AITSEV_SANDBOX_MODE=local is only allowed in development")
        log.warning("Using the LOCAL sandbox: verify commands run unisolated on this machine")
        return LocalRunner(output_limit=settings.sandbox_output_limit)
    return DockerRunner(
        settings.sandbox_image,
        memory=settings.sandbox_memory,
        cpus=settings.sandbox_cpus,
        output_limit=settings.sandbox_output_limit,
        work_root=settings.sandbox_work_root,
    )


def create_app(
    settings: Settings | None = None,
    *,
    gateway: ModelGateway | None = None,
    runner: Runner | None = None,
    start_worker: bool = True,
    integration_transport: httpx.AsyncBaseTransport | None = None,
    email_sender: EmailSender | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    api_keys = settings.parsed_api_keys()
    # Outside development every request needs credentials: an operator key or an account.
    if settings.secret_key is None and settings.environment != "development":
        raise RuntimeError("AITSEV_SECRET_KEY must be set outside development")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db = Database(settings.database_url)
        if settings.auto_create_schema:
            await db.create_schema()
        model_gateway = gateway or ModelGateway.from_settings(settings, DbUsageRecorder(db))
        if gateway is not None and model_gateway.recorder is None:
            model_gateway.recorder = DbUsageRecorder(db)
        orchestrator = Orchestrator(db, model_gateway, runner or build_runner(settings), settings)
        worker = (
            Worker(
                db,
                orchestrator,
                concurrency=settings.worker_concurrency,
                poll_seconds=settings.worker_poll_seconds,
            )
            if start_worker
            else None
        )
        app.state.settings = settings
        app.state.api_keys = api_keys
        app.state.email_sender = email_sender or sender_from_settings(settings)
        app.state.limiters = accounts.Limiters()
        app.state.db = db
        app.state.gateway = model_gateway
        app.state.orchestrator = orchestrator
        app.state.worker = worker
        service = IntegrationService(db, model_gateway, settings, transport=integration_transport)
        app.state.integrations = service
        await service.recover()
        crm_service = CrmService(db, model_gateway, settings, service)
        app.state.crm = crm_service
        await crm_service.recover()
        if worker is not None:
            await worker.start()
        log.info("Aitsev %s started (%s)", __version__, settings.environment)
        try:
            yield
        finally:
            if worker is not None:
                await worker.stop()
            await service.shutdown()
            if gateway is None:
                await model_gateway.close()
            await db.dispose()

    app = FastAPI(
        title="Aitsev API",
        version=__version__,
        description="Verified multi-model AI for code, integration and data migration.",
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if not request.url.path.startswith(DOCS_PATHS):
            response.headers.setdefault("Content-Security-Policy", PAGE_CSP)
            response.headers.setdefault("X-Frame-Options", "DENY")
        if settings.secure_cookies():
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["GET", "POST", "PUT", "DELETE"],
            allow_headers=["Authorization", "Content-Type", "X-API-Key", "X-Aitsev-Client"],
        )
    app.include_router(system.router)
    app.include_router(accounts.router)
    app.include_router(tasks.router)
    app.include_router(chat.router)
    app.include_router(integrations.router)
    app.include_router(crm.router)
    add_downloads(app, settings)
    if settings.web_dist_dir:
        mount_web(app, settings.web_dist_dir)
    return app
