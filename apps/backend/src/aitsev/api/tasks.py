"""Task endpoints: submit, inspect, approve, reject, retry, cancel."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from aitsev import catalog
from aitsev.api.deps import (
    GatewayDep,
    OwnerDep,
    PrincipalDep,
    SessionDep,
    SettingsDep,
    WorkerDep,
    check_quota,
)
from aitsev.audit import add_event, user_actor
from aitsev.db.models import AuditEvent, Candidate, Task, TaskStatus
from aitsev.pipeline.workspace import (
    ChangeSet,
    UnsafePathError,
    diff_changes,
    normalize_files,
    normalize_path,
)
from aitsev.schemas import (
    ApproveIn,
    CandidateOut,
    EventOut,
    FileChangeOut,
    RejectIn,
    ResultOut,
    RetryIn,
    TaskCreate,
    TaskDetail,
    TaskSummary,
    UsageOut,
)

router = APIRouter(prefix="/v1/tasks", tags=["tasks"])

# Literal codes: Starlette renamed these constants and the old names now warn.
HTTP_413 = 413
HTTP_422 = 422

CANCELLABLE = frozenset(
    {
        TaskStatus.QUEUED,
        TaskStatus.PLANNING,
        TaskStatus.GENERATING,
        TaskStatus.REVIEWING,
        TaskStatus.JUDGING,
        TaskStatus.AWAITING_APPROVAL,
    }
)
RETRYABLE = frozenset(
    {TaskStatus.AWAITING_APPROVAL, TaskStatus.REJECTED, TaskStatus.FAILED, TaskStatus.CANCELLED}
)


async def _load(session: SessionDep, task_id: str, owner: str, *, with_candidates: bool) -> Task:
    query = select(Task).where(Task.id == task_id, Task.owner == owner)
    if with_candidates:
        query = query.options(selectinload(Task.candidates))
    task = (await session.scalars(query)).first()
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found")
    return task


def _candidate_out(c: Candidate) -> CandidateOut:
    return CandidateOut(
        id=c.id,
        run_number=c.run_number,
        label=c.label,
        model=c.model,
        status=c.status,
        attempts=c.attempts,
        summary=c.summary,
        files_changed=sorted(c.changed_files or {}),
        files_deleted=sorted(c.deleted_files or []),
        verification=c.verification,
        reviews=list(c.reviews or []),
        score=c.score,
        error=c.error,
    )


def _detail(task: Task) -> TaskDetail:
    summary = TaskSummary.model_validate(task)
    return TaskDetail(
        **summary.model_dump(),
        instructions=task.instructions,
        feedback=task.feedback,
        file_count=len(task.files or {}),
        context_paths=list(task.context_paths or []),
        models=list(task.models or []),
        setup_commands=list(task.setup_commands or []),
        verify_commands=list(task.verify_commands or []),
        sandbox_image=task.sandbox_image,
        plan=task.plan,
        decision=task.decision,
        winner_candidate_id=task.winner_candidate_id,
        usage=UsageOut(
            prompt_tokens=task.prompt_tokens,
            completion_tokens=task.completion_tokens,
            total_tokens=task.prompt_tokens + task.completion_tokens,
        ),
        candidates=[_candidate_out(c) for c in task.candidates],
    )


def _conflict(task: Task, action: str) -> HTTPException:
    return HTTPException(
        status.HTTP_409_CONFLICT, f"Cannot {action} a task whose status is {task.status!r}"
    )


@router.post("", status_code=status.HTTP_201_CREATED, response_model=TaskDetail)
async def create_task(
    body: TaskCreate,
    session: SessionDep,
    owner: OwnerDep,
    settings: SettingsDep,
    worker: WorkerDep,
    principal: PrincipalDep,
    gateway: GatewayDep,
) -> TaskDetail:
    await check_quota(principal, settings, session, "tasks", "tokens")
    try:
        models = catalog.check_selection(
            settings,
            body.models,
            principal.account.plan if principal.account else None,
            gateway.is_available,
        )
    except catalog.SelectionError as exc:
        code = status.HTTP_402_PAYMENT_REQUIRED if exc.upgrade else HTTP_422
        raise HTTPException(code, str(exc)) from exc
    try:
        files = normalize_files(body.files)
    except UnsafePathError as exc:
        raise HTTPException(HTTP_422, str(exc)) from exc
    if len(files) > settings.max_task_files:
        raise HTTPException(
            HTTP_413,
            f"Too many files: {len(files)} (limit {settings.max_task_files})",
        )
    total = 0
    for path, content in files.items():
        size = len(content.encode("utf-8"))
        if size > settings.max_file_bytes:
            raise HTTPException(
                HTTP_413,
                f"{path} is {size} bytes (limit {settings.max_file_bytes})",
            )
        total += size
    if total > settings.max_task_bytes:
        raise HTTPException(
            HTTP_413,
            f"Files total {total} bytes (limit {settings.max_task_bytes}). "
            "Send only the files relevant to the task.",
        )
    try:
        context_paths = sorted({normalize_path(p) for p in body.context_paths})
    except UnsafePathError as exc:
        raise HTTPException(HTTP_422, str(exc)) from exc
    missing = [p for p in context_paths if p not in files]
    if missing:
        raise HTTPException(HTTP_422, f"context_paths not among files: {missing[:5]}")
    context_bytes = sum(len(files[p].encode("utf-8")) for p in (context_paths or files))
    if context_bytes > settings.max_context_bytes:
        raise HTTPException(
            HTTP_413,
            f"Model context is {context_bytes} bytes (limit {settings.max_context_bytes}). "
            "Send context_paths with only the files relevant to the task.",
        )
    if len(body.setup_commands) + len(body.verify_commands) > settings.max_verify_commands:
        raise HTTPException(
            HTTP_422,
            f"At most {settings.max_verify_commands} setup and verify commands in total",
        )
    task = Task(
        owner=owner,
        title=body.title.strip(),
        instructions=body.instructions,
        files=files,
        context_paths=context_paths,
        models=models,
        setup_commands=body.setup_commands,
        verify_commands=body.verify_commands,
        requested_size=body.size.value,
        sandbox_image=body.sandbox_image,
        status=TaskStatus.QUEUED.value,
    )
    session.add(task)
    await session.flush()
    add_event(
        session,
        task.id,
        "created",
        f"Task created with {len(files)} file(s)",
        {
            "size": body.size.value,
            "verify_commands": len(body.verify_commands),
            "models": models or "auto",
        },
        actor=user_actor(owner),
    )
    await session.commit()
    await session.refresh(task, attribute_names=["candidates"])
    if worker is not None:
        worker.notify()
    return _detail(task)


@router.get("", response_model=list[TaskSummary])
async def list_tasks(
    session: SessionDep,
    owner: OwnerDep,
    status_filter: Annotated[TaskStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[TaskSummary]:
    query = select(Task).where(Task.owner == owner)
    if status_filter is not None:
        query = query.where(Task.status == status_filter.value)
    query = query.order_by(Task.created_at.desc()).offset(offset).limit(limit)
    rows = (await session.scalars(query)).all()
    return [TaskSummary.model_validate(t) for t in rows]


@router.get("/{task_id}", response_model=TaskDetail)
async def get_task(task_id: str, session: SessionDep, owner: OwnerDep) -> TaskDetail:
    return _detail(await _load(session, task_id, owner, with_candidates=True))


@router.get("/{task_id}/events", response_model=list[EventOut])
async def list_events(
    task_id: str,
    session: SessionDep,
    owner: OwnerDep,
    after: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> list[EventOut]:
    await _load(session, task_id, owner, with_candidates=False)
    rows = (
        await session.scalars(
            select(AuditEvent)
            .where(AuditEvent.task_id == task_id, AuditEvent.id > after)
            .order_by(AuditEvent.id)
            .limit(limit)
        )
    ).all()
    return [EventOut.model_validate(e) for e in rows]


@router.get("/{task_id}/result", response_model=ResultOut)
async def get_result(task_id: str, session: SessionDep, owner: OwnerDep) -> ResultOut:
    task = await _load(session, task_id, owner, with_candidates=True)
    winner = next((c for c in task.candidates if c.id == task.winner_candidate_id), None)
    if winner is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "This task has no selected result yet")
    changes = ChangeSet(
        changed=dict(winner.changed_files or {}), deleted=list(winner.deleted_files or [])
    )
    files = [
        FileChangeOut(path=d.path, action=d.action, content=d.content, diff=d.diff)
        for d in diff_changes(task.files or {}, changes)
    ]
    return ResultOut(
        task_id=task.id,
        status=task.status,
        candidate_id=winner.id,
        label=winner.label,
        model=winner.model,
        summary=winner.summary,
        decision=task.decision,
        verification=winner.verification,
        files=files,
    )


@router.post("/{task_id}/approve", response_model=TaskDetail)
async def approve_task(
    task_id: str, body: ApproveIn, session: SessionDep, owner: OwnerDep
) -> TaskDetail:
    task = await _load(session, task_id, owner, with_candidates=True)
    if task.status != TaskStatus.AWAITING_APPROVAL:
        raise _conflict(task, "approve")
    task.status = TaskStatus.APPROVED.value
    task.finished_at = datetime.now(UTC)
    add_event(
        session,
        task.id,
        "approved",
        body.comment or "Approved",
        {"candidate_id": task.winner_candidate_id},
        actor=user_actor(owner),
    )
    await session.commit()
    return _detail(task)


@router.post("/{task_id}/reject", response_model=TaskDetail)
async def reject_task(
    task_id: str, body: RejectIn, session: SessionDep, owner: OwnerDep
) -> TaskDetail:
    task = await _load(session, task_id, owner, with_candidates=True)
    if task.status != TaskStatus.AWAITING_APPROVAL:
        raise _conflict(task, "reject")
    task.status = TaskStatus.REJECTED.value
    task.finished_at = datetime.now(UTC)
    add_event(
        session,
        task.id,
        "rejected",
        body.reason,
        {"candidate_id": task.winner_candidate_id},
        actor=user_actor(owner),
    )
    await session.commit()
    return _detail(task)


@router.post("/{task_id}/retry", response_model=TaskDetail)
async def retry_task(
    task_id: str,
    body: RetryIn,
    session: SessionDep,
    owner: OwnerDep,
    worker: WorkerDep,
    principal: PrincipalDep,
    settings: SettingsDep,
) -> TaskDetail:
    await check_quota(principal, settings, session, "tokens")
    task = await _load(session, task_id, owner, with_candidates=True)
    if TaskStatus(task.status) not in RETRYABLE:
        raise _conflict(task, "retry")
    task.status = TaskStatus.QUEUED.value
    task.run_number += 1
    task.feedback = body.feedback
    task.plan = None
    task.decision = None
    task.winner_candidate_id = None
    task.effective_size = None
    task.error = None
    task.finished_at = None
    add_event(
        session,
        task.id,
        "retry_requested",
        body.feedback[:500],
        {"run_number": task.run_number},
        actor=user_actor(owner),
    )
    await session.commit()
    if worker is not None:
        worker.notify()
    return _detail(task)


@router.post("/{task_id}/cancel", response_model=TaskDetail)
async def cancel_task(task_id: str, session: SessionDep, owner: OwnerDep) -> TaskDetail:
    task = await _load(session, task_id, owner, with_candidates=True)
    if TaskStatus(task.status) not in CANCELLABLE:
        raise _conflict(task, "cancel")
    task.status = TaskStatus.CANCELLED.value
    task.finished_at = datetime.now(UTC)
    add_event(session, task.id, "cancelled", "Cancelled by user", actor=user_actor(owner))
    await session.commit()
    return _detail(task)
