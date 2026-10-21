from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from pydantic import SecretStr

from aitsev.config import SandboxMode, Settings
from aitsev.db.models import Task, TaskStatus
from aitsev.db.session import Database
from aitsev.pipeline.sandbox import LocalRunner
from tests.fakes import BUGGY_PROJECT, VERIFY

ALICE_KEY = "alice-key-0123456789abcdef"
BOB_KEY = "bob-key-0123456789abcdef00"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        environment="development",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
        auto_create_schema=True,
        api_keys=SecretStr(f"alice:{ALICE_KEY},bob:{BOB_KEY}"),
        planner_model="fake/m1",
        generator_models=["fake/m1", "fake/m2", "fake/m3"],
        reviewer_models=["fake/m3", "fake/m1"],
        judge_model="fake/m1",
        fallback_models=["fake/m2"],
        sandbox_mode=SandboxMode.LOCAL,
        sandbox_timeout_seconds=120,
        max_repair_attempts=2,
        worker_poll_seconds=0.05,
        worker_concurrency=2,
        max_task_files=200,
        max_task_bytes=600_000,
        max_file_bytes=200_000,
        max_context_bytes=400_000,
    )


@pytest.fixture
async def db(settings: Settings) -> AsyncIterator[Database]:
    database = Database(settings.database_url)
    await database.create_schema()
    yield database
    await database.dispose()


@pytest.fixture
def runner() -> LocalRunner:
    return LocalRunner(output_limit=5000)


async def add_task(
    db: Database,
    *,
    owner: str = "alice",
    files: dict[str, str] | None = None,
    verify: list[str] | None = None,
    size: str = "auto",
    feedback: str | None = None,
    status: TaskStatus = TaskStatus.QUEUED,
    context_paths: list[str] | None = None,
) -> str:
    async with db.session() as session:
        task = Task(
            owner=owner,
            title="Fix add",
            instructions="Make add() return the sum of its arguments.",
            files=dict(BUGGY_PROJECT if files is None else files),
            context_paths=list(context_paths or []),
            verify_commands=list(VERIFY if verify is None else verify),
            setup_commands=[],
            requested_size=size,
            feedback=feedback,
            status=status.value,
        )
        session.add(task)
        await session.commit()
        return task.id


async def get_task(db: Database, task_id: str) -> Task:
    async with db.session() as session:
        task = await session.get(Task, task_id)
        assert task is not None
        await session.refresh(task, attribute_names=["candidates"])
        return task
