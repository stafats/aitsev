from __future__ import annotations

import asyncio

from aitsev.db.models import TaskStatus
from aitsev.pipeline.orchestrator import Orchestrator
from aitsev.worker import Worker
from tests.conftest import add_task, get_task
from tests.fakes import CORRECT, PLAN, Script, judge, make_gateway, review


class RecordingOrchestrator:
    def __init__(self, delay: float = 0.0) -> None:
        self.ran: list[str] = []
        self.delay = delay
        self.max_parallel = 0
        self._active = 0

    async def run(self, task_id: str) -> None:
        self._active += 1
        self.max_parallel = max(self.max_parallel, self._active)
        try:
            await asyncio.sleep(self.delay)
            self.ran.append(task_id)
        finally:
            self._active -= 1


async def test_claim_is_atomic_and_oldest_first(db):
    first = await add_task(db)
    second = await add_task(db)
    worker = Worker(db, RecordingOrchestrator())  # type: ignore[arg-type]
    assert await worker.claim_next() == first
    assert await worker.claim_next() == second
    assert await worker.claim_next() is None
    assert (await get_task(db, first)).status == TaskStatus.PLANNING


async def test_two_workers_never_claim_the_same_task(db):
    ids = {await add_task(db) for _ in range(6)}
    a = Worker(db, RecordingOrchestrator())  # type: ignore[arg-type]
    b = Worker(db, RecordingOrchestrator())  # type: ignore[arg-type]
    claimed: list[str] = []
    for _ in range(6):
        results = await asyncio.gather(a.claim_next(), b.claim_next())
        claimed += [r for r in results if r]
    assert sorted(claimed) == sorted(ids)
    assert len(claimed) == len(set(claimed))


async def test_recover_requeues_interrupted_tasks(db):
    running = await add_task(db, status=TaskStatus.GENERATING)
    waiting = await add_task(db, status=TaskStatus.AWAITING_APPROVAL)
    done = await add_task(db, status=TaskStatus.APPROVED)
    worker = Worker(db, RecordingOrchestrator())  # type: ignore[arg-type]
    assert await worker.recover() == 1
    assert (await get_task(db, running)).status == TaskStatus.QUEUED
    assert (await get_task(db, waiting)).status == TaskStatus.AWAITING_APPROVAL
    assert (await get_task(db, done)).status == TaskStatus.APPROVED


async def test_concurrency_limit_is_respected(db):
    for _ in range(5):
        await add_task(db)
    orch = RecordingOrchestrator(delay=0.05)
    worker = Worker(db, orch, concurrency=2, poll_seconds=0.01)  # type: ignore[arg-type]
    await worker.start()
    try:
        for _ in range(200):
            if len(orch.ran) == 5:
                break
            await asyncio.sleep(0.02)
    finally:
        await worker.stop()
    assert len(orch.ran) == 5
    assert orch.max_parallel == 2


async def test_worker_runs_real_pipeline_end_to_end(db, settings, runner):
    script = (
        Script()
        .default("plan", PLAN)
        .default("generate", CORRECT)
        .default("review", review(9))
        .default("judge", judge("A"))
    )
    orch = Orchestrator(db, make_gateway(script), runner, settings)
    worker = Worker(db, orch, concurrency=1, poll_seconds=0.01)
    task_id = await add_task(db)
    await worker.start()
    try:
        for _ in range(500):
            if (await get_task(db, task_id)).status == TaskStatus.AWAITING_APPROVAL:
                break
            await asyncio.sleep(0.02)
    finally:
        await worker.stop()
    assert (await get_task(db, task_id)).status == TaskStatus.AWAITING_APPROVAL


async def test_stop_cancels_in_flight_runs(db):
    await add_task(db)
    orch = RecordingOrchestrator(delay=30)
    worker = Worker(db, orch, concurrency=1, poll_seconds=0.01)  # type: ignore[arg-type]
    await worker.start()
    for _ in range(100):
        if worker.active:
            break
        await asyncio.sleep(0.01)
    assert worker.active == 1
    await asyncio.wait_for(worker.stop(), timeout=5)
    assert worker.active == 0
