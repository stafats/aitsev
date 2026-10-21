"""Request and response models for the public API."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from aitsev.db.models import TaskSize

IMAGE_PATTERN = r"^[a-z0-9][a-z0-9._/:@-]{0,199}$"
MAX_COMMAND_LENGTH = 2000


class TaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    instructions: str = Field(min_length=1, max_length=50_000)
    files: dict[str, str] = Field(
        default_factory=dict, description="Project files as path -> full text content"
    )
    context_paths: list[str] = Field(
        default_factory=list,
        description="Files the models should read (all files go to the sandbox). "
        "Empty means every file is context.",
    )
    setup_commands: list[str] = Field(
        default_factory=list,
        description="Run first, with network access (e.g. install dependencies into .venv)",
    )
    verify_commands: list[str] = Field(
        default_factory=list,
        description="Build, lint and test commands; all must exit 0. Run without network.",
    )
    size: TaskSize = TaskSize.AUTO
    sandbox_image: str | None = Field(default=None, pattern=IMAGE_PATTERN)
    models: list[str] = Field(
        default_factory=list,
        max_length=3,
        description="Models that write the code (see GET /v1/models/catalog). Empty means the "
        "server picks. Planning, review and judging are always routed by the server.",
    )

    @field_validator("setup_commands", "verify_commands")
    @classmethod
    def _commands(cls, value: list[str]) -> list[str]:
        cleaned = [c.strip() for c in value]
        for command in cleaned:
            if not command:
                raise ValueError("Commands cannot be empty")
            if len(command) > MAX_COMMAND_LENGTH:
                raise ValueError(f"Commands must be at most {MAX_COMMAND_LENGTH} characters")
            if "\x00" in command:
                raise ValueError("Commands cannot contain NUL bytes")
        return cleaned


class UsageOut(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class TaskSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    status: str
    requested_size: str
    effective_size: str | None
    run_number: int
    error: str | None
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None


class CandidateOut(BaseModel):
    id: str
    run_number: int
    label: str
    model: str
    status: str
    attempts: int
    summary: str
    files_changed: list[str]
    files_deleted: list[str]
    verification: dict[str, Any] | None
    reviews: list[dict[str, Any]]
    score: float | None
    error: str | None


class TaskDetail(TaskSummary):
    instructions: str
    feedback: str | None
    file_count: int
    context_paths: list[str]
    models: list[str]
    setup_commands: list[str]
    verify_commands: list[str]
    sandbox_image: str | None
    plan: dict[str, Any] | None
    decision: dict[str, Any] | None
    winner_candidate_id: str | None
    usage: UsageOut
    candidates: list[CandidateOut]


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    actor: str
    type: str
    message: str
    data: dict[str, Any]


class FileChangeOut(BaseModel):
    path: str
    action: str
    content: str | None
    diff: str


class ResultOut(BaseModel):
    task_id: str
    status: str
    candidate_id: str
    label: str
    model: str
    summary: str
    decision: dict[str, Any] | None
    verification: dict[str, Any] | None
    files: list[FileChangeOut]


class ApproveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    comment: str | None = Field(default=None, max_length=4000)


class RejectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=4000)


class RetryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    feedback: str = Field(min_length=1, max_length=10_000)


class ModelRoleOut(BaseModel):
    role: str
    model: str
    available: bool


class ModelsOut(BaseModel):
    roles: list[ModelRoleOut]
    providers: dict[str, list[str] | str]


class CatalogEntryOut(BaseModel):
    id: str
    label: str
    tier: str
    description: str
    weight: float
    available: bool = Field(description="The server has this provider configured")
    allowed: bool = Field(description="Your plan includes this model")


class CatalogOut(BaseModel):
    plan: str | None
    premium_allowed: bool
    max_selected: int
    upgrade_url: str
    models: list[CatalogEntryOut]
