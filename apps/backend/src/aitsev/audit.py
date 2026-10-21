"""Audit log and model usage recording."""

from __future__ import annotations

from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from aitsev.accounts.plans import current_owner
from aitsev.db.models import AuditEvent, ModelCall, Task
from aitsev.db.session import Database
from aitsev.gateway.gateway import CallRecord

SYSTEM_ACTOR = "system"


def user_actor(owner: str) -> str:
    return f"user:{owner}"


def add_event(
    session: AsyncSession,
    task_id: str,
    type_: str,
    message: str = "",
    data: dict[str, Any] | None = None,
    actor: str = SYSTEM_ACTOR,
) -> AuditEvent:
    """Stage an audit event in ``session``; the caller commits."""
    event = AuditEvent(task_id=task_id, type=type_, message=message, data=data or {}, actor=actor)
    session.add(event)
    return event


async def record_event(
    db: Database,
    task_id: str,
    type_: str,
    message: str = "",
    data: dict[str, Any] | None = None,
    actor: str = SYSTEM_ACTOR,
) -> None:
    async with db.session() as session:
        add_event(session, task_id, type_, message, data, actor)
        await session.commit()


class DbUsageRecorder:
    """Writes every model call to ``model_calls`` and adds tokens to the task's totals."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def __call__(self, record: CallRecord) -> None:
        async with self.db.session() as session:
            session.add(
                ModelCall(
                    task_id=record.task_id,
                    owner=current_owner.get(),
                    stage=record.stage,
                    model=record.model,
                    ok=record.ok,
                    prompt_tokens=record.prompt_tokens,
                    completion_tokens=record.completion_tokens,
                    latency_ms=record.latency_ms,
                    error=(record.error or None) and record.error[:2000],
                )
            )
            if record.task_id and (record.prompt_tokens or record.completion_tokens):
                await session.execute(
                    update(Task)
                    .where(Task.id == record.task_id)
                    .values(
                        prompt_tokens=Task.prompt_tokens + record.prompt_tokens,
                        completion_tokens=Task.completion_tokens + record.completion_tokens,
                    )
                    .execution_options(synchronize_session=False)
                )
            await session.commit()
