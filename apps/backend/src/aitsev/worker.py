"""Background worker: a durable, database-backed task queue.

Queued tasks are claimed with an atomic ``UPDATE ... WHERE status = 'queued'`` so several API
replicas can run workers safely. Tasks left mid-run by a crash or restart are re-queued on
startup.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from sqlalchemy import select, update

from aitsev.audit import add_event
from aitsev.db.models import RUNNING_STATUSES, Task, TaskStatus
from aitsev.db.session import Database
from aitsev.pipeline.orchestrator import Orchestrator

log = logging.getLogger(__name__)


class Worker:
    def __init__(
        self,
        db: Database,
        orchestrator: Orchestrator,
        *,
        concurrency: int = 2,
        poll_seconds: float = 2.0,
    ) -> None:
        self.db = db
        self.orchestrator = orchestrator
        self.concurrency = max(1, concurrency)
        self.poll_seconds = poll_seconds
        self._wake = asyncio.Event()
        self._loop_task: asyncio.Task[None] | None = None
        self._running: set[asyncio.Task[None]] = set()
        self._stopping = False

    async def start(self) -> None:
        await self.recover()
        self._stopping = False
        self._loop_task = asyncio.create_task(self._loop(), name="aitsev-worker")

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        if self._loop_task is not None:
            self._loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._loop_task
        for task in list(self._running):
            task.cancel()
        if self._running:
            await asyncio.gather(*self._running, return_exceptions=True)

    def notify(self) -> None:
        """Wake the loop now instead of waiting for the next poll."""
        self._wake.set()

    @property
    def active(self) -> int:
        return len(self._running)

    async def recover(self) -> int:
        """Re-queue tasks that were mid-run when the process stopped."""
        async with self.db.session() as session:
            rows = (
                await session.scalars(
                    select(Task).where(Task.status.in_([s.value for s in RUNNING_STATUSES]))
                )
            ).all()
            for task in rows:
                task.status = TaskStatus.QUEUED.value
                add_event(session, task.id, "requeued", "Re-queued after a service restart")
            await session.commit()
            if rows:
                log.info("Re-queued %d interrupted task(s)", len(rows))
            return len(rows)

    async def claim_next(self) -> str | None:
        """Atomically move the oldest queued task to ``planning`` and return its id."""
        async with self.db.session() as session:
            candidates = (
                await session.scalars(
                    select(Task.id)
                    .where(Task.status == TaskStatus.QUEUED.value)
                    .order_by(Task.created_at)
                    .limit(5)
                )
            ).all()
            for task_id in candidates:
                result = await session.execute(
                    update(Task)
                    .where(Task.id == task_id, Task.status == TaskStatus.QUEUED.value)
                    .values(status=TaskStatus.PLANNING.value)
                    .execution_options(synchronize_session=False)
                )
                await session.commit()
                if result.rowcount == 1:  # type: ignore[attr-defined]
                    return task_id
        return None

    async def run_once(self) -> int:
        """Start as many queued tasks as free slots allow. Returns how many were started."""
        started = 0
        while len(self._running) < self.concurrency and not self._stopping:
            task_id = await self.claim_next()
            if task_id is None:
                break
            job = asyncio.create_task(self._execute(task_id), name=f"aitsev-task-{task_id}")
            self._running.add(job)
            job.add_done_callback(self._running.discard)
            job.add_done_callback(lambda _: self._wake.set())
            started += 1
        return started

    async def _execute(self, task_id: str) -> None:
        try:
            await self.orchestrator.run(task_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Worker failed to run task %s", task_id)

    async def _loop(self) -> None:
        while not self._stopping:
            self._wake.clear()
            try:
                await self.run_once()
            except Exception:
                log.exception("Worker loop error")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self.poll_seconds)
