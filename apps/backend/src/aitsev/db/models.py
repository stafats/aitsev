"""SQLAlchemy ORM models.

JSON columns use the generic ``JSON`` type so the same schema works on PostgreSQL and SQLite.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class TaskStatus(StrEnum):
    QUEUED = "queued"
    PLANNING = "planning"
    GENERATING = "generating"
    REVIEWING = "reviewing"
    JUDGING = "judging"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in TERMINAL_STATUSES

    @property
    def is_running(self) -> bool:
        return self in RUNNING_STATUSES


TERMINAL_STATUSES = frozenset(
    {TaskStatus.APPROVED, TaskStatus.REJECTED, TaskStatus.FAILED, TaskStatus.CANCELLED}
)
RUNNING_STATUSES = frozenset(
    {TaskStatus.PLANNING, TaskStatus.GENERATING, TaskStatus.REVIEWING, TaskStatus.JUDGING}
)


class TaskSize(StrEnum):
    AUTO = "auto"
    SMALL = "small"
    NORMAL = "normal"
    LARGE = "large"


class CandidateStatus(StrEnum):
    GENERATING = "generating"
    PASSED = "passed"
    UNVERIFIED = "unverified"  # passed built-in checks, no verify commands configured
    FAILED = "failed"
    ERROR = "error"  # model or parse error, no usable output


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner: Mapped[str] = mapped_column(String(100), index=True)
    title: Mapped[str] = mapped_column(String(200))
    instructions: Mapped[str] = mapped_column(Text)
    feedback: Mapped[str | None] = mapped_column(Text, default=None)
    files: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    context_paths: Mapped[list[str]] = mapped_column(JSON, default=list)
    # Models the user chose to write the code (empty = server routing).
    models: Mapped[list[str]] = mapped_column(JSON, default=list)
    setup_commands: Mapped[list[str]] = mapped_column(JSON, default=list)
    verify_commands: Mapped[list[str]] = mapped_column(JSON, default=list)
    sandbox_image: Mapped[str | None] = mapped_column(String(200), default=None)
    requested_size: Mapped[str] = mapped_column(String(20), default=TaskSize.AUTO.value)
    effective_size: Mapped[str | None] = mapped_column(String(20), default=None)
    status: Mapped[str] = mapped_column(String(30), default=TaskStatus.QUEUED.value, index=True)
    run_number: Mapped[int] = mapped_column(Integer, default=1)
    plan: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    decision: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    winner_candidate_id: Mapped[str | None] = mapped_column(String(36), default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    candidates: Mapped[list[Candidate]] = relationship(
        back_populates="task", cascade="all, delete-orphan", order_by="Candidate.created_at"
    )


class Candidate(Base):
    __tablename__ = "candidates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    run_number: Mapped[int] = mapped_column(Integer, default=1)
    label: Mapped[str] = mapped_column(String(4), default="A")
    model: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default=CandidateStatus.GENERATING.value)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[str] = mapped_column(Text, default="")
    changed_files: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    deleted_files: Mapped[list[str]] = mapped_column(JSON, default=list)
    verification: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    reviews: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    score: Mapped[float | None] = mapped_column(default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    task: Mapped[Task] = relationship(back_populates="candidates")


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    actor: Mapped[str] = mapped_column(String(120))
    type: Mapped[str] = mapped_column(String(60))
    message: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    __table_args__ = (Index("ix_audit_events_task_id_id", "task_id", "id"),)


class ModelCall(Base):
    __tablename__ = "model_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), index=True, default=None
    )
    owner: Mapped[str | None] = mapped_column(String(100), index=True, default=None)
    stage: Mapped[str] = mapped_column(String(40))
    model: Mapped[str] = mapped_column(String(200))
    ok: Mapped[bool] = mapped_column(default=True)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------------------------------------------------------------------- integrations (phase 3)


class Connection(Base):
    """An external system's API. Secrets live only in ``credentials_encrypted``."""

    __tablename__ = "connections"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner: Mapped[str] = mapped_column(String(100), index=True)
    name: Mapped[str] = mapped_column(String(200))
    base_url: Mapped[str] = mapped_column(String(500))
    auth: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    credentials_encrypted: Mapped[str] = mapped_column(Text, default="")
    headers: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    pagination: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    spec_format: Mapped[str] = mapped_column(String(20))
    # CRM platform: auto (detected from the URL) | dynamics | salesforce | hubspot | zoho
    platform: Mapped[str] = mapped_column(String(20), default="auto", server_default="auto")
    # sandbox | production | unknown (unknown is treated as production for safety)
    environment: Mapped[str] = mapped_column(String(20), default="unknown")
    spec_text: Mapped[str] = mapped_column(Text, default="")
    schema_doc: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)
    discovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class MappingState(StrEnum):
    MAPPING = "mapping"  # models are working on it
    DRAFT = "draft"
    APPROVED = "approved"
    FAILED = "failed"


class Mapping(Base):
    __tablename__ = "mappings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner: Mapped[str] = mapped_column(String(100), index=True)
    name: Mapped[str] = mapped_column(String(200))
    source_connection_id: Mapped[str] = mapped_column(ForeignKey("connections.id"))
    source_entity: Mapped[str] = mapped_column(String(200))
    target_connection_id: Mapped[str] = mapped_column(ForeignKey("connections.id"))
    target_entity: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default=MappingState.MAPPING.value)
    fields: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    models_used: Mapped[list[str]] = mapped_column(JSON, default=list)
    version: Mapped[int] = mapped_column(Integer, default=1)
    dry_run_ok_version: Mapped[int | None] = mapped_column(Integer, default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    STOPPED = "stopped"  # too many errors
    FAILED = "failed"
    CANCELLED = "cancelled"


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner: Mapped[str] = mapped_column(String(100), index=True)
    mapping_id: Mapped[str] = mapped_column(ForeignKey("mappings.id"), index=True)
    mapping_version: Mapped[int] = mapped_column(Integer)
    dry_run: Mapped[bool] = mapped_column(default=True)
    status: Mapped[str] = mapped_column(String(20), default=JobState.QUEUED.value)
    record_limit: Mapped[int | None] = mapped_column(Integer, default=None)
    chunk_size: Mapped[int] = mapped_column(Integer, default=100)
    stats: Mapped[dict[str, int]] = mapped_column(JSON, default=dict)
    errors: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    sample: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


# ---------------------------------------------------------------------- CRM pack (phase 4)


class CrmAnalysis(Base):
    __tablename__ = "crm_analyses"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner: Mapped[str] = mapped_column(String(100), index=True)
    connection_id: Mapped[str] = mapped_column(ForeignKey("connections.id"), index=True)
    entities: Mapped[list[str]] = mapped_column(JSON, default=list)
    sample_size: Mapped[int] = mapped_column(Integer, default=200)
    status: Mapped[str] = mapped_column(String(20), default="running")  # running|done|failed
    report: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class CrmChangeState(StrEnum):
    PLANNING = "planning"
    PROPOSED = "proposed"
    APPROVED = "approved"
    APPLYING = "applying"
    APPLIED = "applied"
    PARTIAL = "partially_applied"
    FAILED = "failed"


class CrmChange(Base):
    """A reviewed set of new columns for one table, applied through the Dataverse Web API."""

    __tablename__ = "crm_changes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner: Mapped[str] = mapped_column(String(100), index=True)
    connection_id: Mapped[str] = mapped_column(ForeignKey("connections.id"), index=True)
    entity: Mapped[str] = mapped_column(String(100))
    request: Mapped[str] = mapped_column(Text)
    publisher_prefix: Mapped[str] = mapped_column(String(10))
    option_value_prefix: Mapped[int] = mapped_column(Integer, default=10_000)
    solution: Mapped[str | None] = mapped_column(String(100), default=None)
    status: Mapped[str] = mapped_column(String(30), default=CrmChangeState.PLANNING.value)
    plan: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    models_used: Mapped[list[str]] = mapped_column(JSON, default=list)
    results: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    version: Mapped[int] = mapped_column(Integer, default=1)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    applied_by: Mapped[str | None] = mapped_column(String(120), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


# ---------------------------------------------------------------------- accounts (phase 5)


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    password_hash: Mapped[str] = mapped_column(String(300))
    email_verified: Mapped[bool] = mapped_column(default=False)
    plan: Mapped[str] = mapped_column(String(30), default="free")
    is_admin: Mapped[bool] = mapped_column(default=False)
    disabled: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(100))
    prefix: Mapped[str] = mapped_column(String(20))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_agent: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class OneTimeToken(Base):
    """Email verification and password reset links."""

    __tablename__ = "one_time_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    purpose: Mapped[str] = mapped_column(String(20))  # verify | reset
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class DeviceAuth(Base):
    """Browser sign-in for the desktop app and editor extensions (OAuth device-flow style)."""

    __tablename__ = "device_auths"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    device_code_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_code: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    client_name: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    account_id: Mapped[str | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
