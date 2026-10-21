"""Chat with the code, let the agent edit it, and get background suggestions."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from aitsev import catalog
from aitsev.accounts.plans import current_owner
from aitsev.api.deps import GatewayDep, PrincipalDep, SessionDep, SettingsDep, check_quota
from aitsev.chat.agent import Assistant, ChatError
from aitsev.config import Settings
from aitsev.gateway.errors import ModelError
from aitsev.pipeline.workspace import UnsafePathError, normalize_files, normalize_path
from aitsev.schemas import IMAGE_PATTERN, MAX_COMMAND_LENGTH

router = APIRouter(prefix="/v1", tags=["chat"])
HTTP_413 = 413
HTTP_422 = 422


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=50_000)


class ChatIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    messages: list[Message] = Field(min_length=1, max_length=60)
    mode: Literal["ask", "agent"] = "agent"
    model: str = Field(default="auto", max_length=200)
    files: dict[str, str] = Field(default_factory=dict)
    context_paths: list[str] = Field(default_factory=list)
    active_path: str | None = Field(default=None, max_length=400)
    selection: str | None = Field(default=None, max_length=50_000)
    setup_commands: list[str] = Field(default_factory=list)
    verify_commands: list[str] = Field(default_factory=list)
    sandbox_image: str | None = Field(default=None, pattern=IMAGE_PATTERN)

    @field_validator("setup_commands", "verify_commands")
    @classmethod
    def _commands(cls, value: list[str]) -> list[str]:
        cleaned = [c.strip() for c in value]
        if any(not c or len(c) > MAX_COMMAND_LENGTH or "\x00" in c for c in cleaned):
            raise ValueError("Commands must be non-empty, without NUL bytes, and not too long")
        return cleaned


class EditOut(BaseModel):
    path: str
    action: Literal["create", "modify", "delete"]
    content: str | None
    diff: str


class ChatOut(BaseModel):
    reply: str
    model: str
    edits: list[EditOut]
    skipped: list[str]
    verification: dict[str, object] | None
    repaired: bool
    prompt_tokens: int
    completion_tokens: int


class AnalyzeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    files: dict[str, str] = Field(min_length=1)
    focus: str | None = Field(default=None, max_length=2000)
    model: str = Field(default="auto", max_length=200)
    limit: int = Field(default=10, ge=1, le=30)


class SuggestionOut(BaseModel):
    path: str
    line: int
    severity: Literal["info", "warning", "error"]
    title: str
    detail: str


class AnalyzeOut(BaseModel):
    model: str
    suggestions: list[SuggestionOut]


def _assistant(request: Request) -> Assistant:
    return Assistant(
        request.app.state.gateway, request.app.state.settings, request.app.state.orchestrator.runner
    )


def _files(
    settings: Settings, raw: dict[str, str], context_paths: list[str]
) -> tuple[dict[str, str], list[str]]:
    try:
        files = normalize_files(raw)
        context = sorted({normalize_path(p) for p in context_paths})
    except UnsafePathError as exc:
        raise HTTPException(HTTP_422, str(exc)) from exc
    if len(files) > settings.max_task_files:
        raise HTTPException(
            HTTP_413, f"Too many files: {len(files)} (limit {settings.max_task_files})"
        )
    total = 0
    for path, content in files.items():
        size = len(content.encode("utf-8"))
        if size > settings.max_file_bytes:
            raise HTTPException(
                HTTP_413, f"{path} is {size} bytes (limit {settings.max_file_bytes})"
            )
        total += size
    if total > settings.max_task_bytes:
        raise HTTPException(
            HTTP_413, f"Files total {total} bytes (limit {settings.max_task_bytes})"
        )
    missing = [p for p in context if p not in files]
    if missing:
        raise HTTPException(HTTP_422, f"context_paths not among files: {missing[:5]}")
    context_bytes = sum(len(files[p].encode("utf-8")) for p in (context or files))
    if context_bytes > settings.max_context_bytes:
        raise HTTPException(
            HTTP_413,
            f"Model context is {context_bytes} bytes (limit {settings.max_context_bytes}). "
            "Share fewer files with the assistant.",
        )
    return files, context


def _model(settings: Settings, principal: PrincipalDep, gateway: GatewayDep, model: str) -> str:
    if model == "auto":
        return model
    try:
        chosen = catalog.check_selection(
            settings,
            [model],
            principal.account.plan if principal.account else None,
            gateway.is_available,
        )
    except catalog.SelectionError as exc:
        code = status.HTTP_402_PAYMENT_REQUIRED if exc.upgrade else HTTP_422
        raise HTTPException(code, str(exc)) from exc
    return chosen[0]


@router.post("/chat", response_model=ChatOut)
async def chat(
    body: ChatIn,
    request: Request,
    principal: PrincipalDep,
    settings: SettingsDep,
    session: SessionDep,
    gateway: GatewayDep,
) -> ChatOut:
    """One assistant turn. ``agent`` mode may return file edits; nothing is written server-side."""
    await check_quota(principal, settings, session, "tokens")
    if body.messages[-1].role != "user":
        raise HTTPException(HTTP_422, "The last message must come from the user")
    model = _model(settings, principal, gateway, body.model)
    files, context = _files(settings, body.files, body.context_paths)
    if len(body.setup_commands) + len(body.verify_commands) > settings.max_verify_commands:
        raise HTTPException(HTTP_422, f"At most {settings.max_verify_commands} commands in total")
    active = None
    if body.active_path:
        try:
            active = normalize_path(body.active_path)
        except UnsafePathError as exc:
            raise HTTPException(HTTP_422, str(exc)) from exc
    current_owner.set(principal.owner)  # meter model calls to this account
    try:
        outcome = await _assistant(request).chat(
            mode=body.mode,
            model=model,
            messages=[(m.role, m.content) for m in body.messages],
            files=files,
            context_paths=context,
            active_path=active,
            selection=body.selection,
            setup_commands=body.setup_commands,
            verify_commands=body.verify_commands,
            sandbox_image=body.sandbox_image,
        )
    except ChatError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    except ModelError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"The model failed: {exc}") from exc
    return ChatOut(
        reply=outcome.reply,
        model=outcome.model,
        edits=[
            EditOut(path=e.path, action=e.action, content=e.content, diff=e.diff)
            for e in outcome.edits
        ],
        skipped=outcome.skipped,
        verification=outcome.verification,
        repaired=outcome.repaired,
        prompt_tokens=outcome.prompt_tokens,
        completion_tokens=outcome.completion_tokens,
    )


@router.post("/analyze", response_model=AnalyzeOut)
async def analyze(
    body: AnalyzeIn,
    request: Request,
    principal: PrincipalDep,
    settings: SettingsDep,
    session: SessionDep,
    gateway: GatewayDep,
) -> AnalyzeOut:
    """Background review: line-level suggestions for the editor's Problems view."""
    await check_quota(principal, settings, session, "tokens")
    model = _model(settings, principal, gateway, body.model)
    files, _ = _files(settings, body.files, [])
    current_owner.set(principal.owner)
    try:
        suggestions, used = await _assistant(request).analyze(
            model=model, files=files, focus=body.focus, limit=body.limit
        )
    except ChatError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    except ModelError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"The model failed: {exc}") from exc
    return AnalyzeOut(
        model=used,
        suggestions=[
            SuggestionOut(
                path=s.path, line=s.line, severity=s.severity, title=s.title, detail=s.detail
            )
            for s in suggestions
        ],
    )
