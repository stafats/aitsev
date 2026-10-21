"""Plans, monthly usage and quota checks.

Usage is measured per calendar month (UTC) from the database:
* model tokens: every model call records its owner (``model_calls.owner``)
* code tasks: rows in ``tasks``
* migrated records: records written by real (non-dry-run) jobs
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aitsev.db.models import Job, ModelCall, Task

# The account a background job is working for; read by the usage recorder.
current_owner: ContextVar[str | None] = ContextVar("aitsev_current_owner", default=None)

Resource = Literal["tokens", "tasks", "records"]


@dataclass(frozen=True, slots=True)
class Plan:
    id: str
    name: str
    monthly_tokens: int | None  # None = unlimited
    monthly_tasks: int | None
    monthly_records: int | None
    price_note: str
    premium_models: bool = False  # Claude, GPT and other premium models


PLANS: dict[str, Plan] = {
    "free": Plan("free", "Free", 300_000, 20, 1_000, "Free"),
    "pro": Plan("pro", "Pro", 5_000_000, 500, 100_000, "Per user, monthly", True),
    "team": Plan("team", "Team", 25_000_000, 3_000, 1_000_000, "Per user, monthly", True),
    "enterprise": Plan("enterprise", "Enterprise", None, None, None, "Custom", True),
}


def plan_for(plan_id: str) -> Plan:
    return PLANS.get(plan_id, PLANS["free"])


def month_start(now: datetime | None = None) -> datetime:
    current = now or datetime.now(UTC)
    return current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


class Usage(BaseModel):
    period_start: datetime
    tokens: int
    tasks: int
    records: int


class Limit(BaseModel):
    used: int
    limit: int | None
    remaining: int | None


class UsageReport(BaseModel):
    plan: str
    plan_name: str
    period_start: datetime
    tokens: Limit
    tasks: Limit
    records: Limit


async def usage_for(
    session: AsyncSession,
    owner: str,
    now: datetime | None = None,
    weights: dict[str, float] | None = None,
) -> Usage:
    """Monthly usage. ``weights`` makes expensive models count more (model ref -> factor)."""
    start = month_start(now)
    per_model = (
        await session.execute(
            select(
                ModelCall.model,
                func.coalesce(func.sum(ModelCall.prompt_tokens + ModelCall.completion_tokens), 0),
            )
            .where(ModelCall.owner == owner, ModelCall.created_at >= start)
            .group_by(ModelCall.model)
        )
    ).all()
    factors = weights or {}
    tokens = sum(round(int(n) * factors.get(model, 1.0)) for model, n in per_model)
    tasks = await session.scalar(
        select(func.count(Task.id)).where(Task.owner == owner, Task.created_at >= start)
    )
    jobs = (
        await session.scalars(
            select(Job).where(Job.owner == owner, Job.dry_run.is_(False), Job.created_at >= start)
        )
    ).all()
    records = sum(int((j.stats or {}).get("written", 0)) for j in jobs)
    return Usage(
        period_start=start, tokens=int(tokens or 0), tasks=int(tasks or 0), records=records
    )


def _limit(used: int, limit: int | None) -> Limit:
    return Limit(used=used, limit=limit, remaining=None if limit is None else max(0, limit - used))


def report(plan_id: str, usage: Usage) -> UsageReport:
    plan = plan_for(plan_id)
    return UsageReport(
        plan=plan.id,
        plan_name=plan.name,
        period_start=usage.period_start,
        tokens=_limit(usage.tokens, plan.monthly_tokens),
        tasks=_limit(usage.tasks, plan.monthly_tasks),
        records=_limit(usage.records, plan.monthly_records),
    )


class QuotaExceededError(Exception):
    def __init__(self, resource: Resource, plan: Plan, used: int, limit: int) -> None:
        names = {"tokens": "model tokens", "tasks": "code tasks", "records": "migrated records"}
        super().__init__(
            f"Your {plan.name} plan includes {limit:,} {names[resource]} per month and "
            f"{used:,} are used. Upgrade your plan or wait for the next month."
        )
        self.resource = resource


def check(plan_id: str, usage: Usage, needs: tuple[Resource, ...]) -> None:
    """Raise QuotaExceededError when any needed resource is used up."""
    plan = plan_for(plan_id)
    for resource in needs:
        limit = getattr(plan, f"monthly_{resource}")
        used = getattr(usage, resource)
        if limit is not None and used >= limit:
            raise QuotaExceededError(resource, plan, used, limit)
