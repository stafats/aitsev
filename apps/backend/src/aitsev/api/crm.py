"""CRM pack endpoints (Dynamics 365, Salesforce, HubSpot, Zoho CRM): analyse objects, plan new
fields, review, approve and apply."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select, update

from aitsev.api.deps import OwnerDep, PrincipalDep, SessionDep, SettingsDep, check_quota
from aitsev.audit import user_actor
from aitsev.crm.dynamics import FieldSpec
from aitsev.crm.platforms import platform_for
from aitsev.crm.service import CrmService
from aitsev.db.models import Connection, CrmAnalysis, CrmChange, CrmChangeState

router = APIRouter(prefix="/v1/crm", tags=["crm"])
HTTP_422 = 422


def _service(request: Request) -> CrmService:
    service: CrmService = request.app.state.crm
    return service


async def _own(session: SessionDep, model: Any, item_id: str, owner: str, label: str) -> Any:
    row = (
        await session.scalars(select(model).where(model.id == item_id, model.owner == owner))
    ).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{label} not found")
    return row


# ------------------------------------------------------------------ schemas


class AnalysisCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entities: list[str] = Field(min_length=1, max_length=10, examples=[["contact", "account"]])
    sample_size: int = Field(default=200, ge=1, le=5000)


class AnalysisOut(BaseModel):
    id: str
    connection_id: str
    entities: list[str]
    sample_size: int
    status: str
    report: list[dict[str, Any]]
    error: str | None
    created_at: datetime
    finished_at: datetime | None


class ChangeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity: str = Field(min_length=1, max_length=100, examples=["contact", "Account", "deals"])
    request: str = Field(min_length=5, max_length=5000)
    publisher_prefix: str = Field(default="new", pattern=r"^[a-z][a-z0-9]{1,7}$")
    option_value_prefix: int = Field(default=10_000, ge=10_000, le=99_999)
    solution: str | None = Field(default=None, pattern=r"^[A-Za-z][A-Za-z0-9_]{0,99}$")


class PlanItemIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    spec: FieldSpec
    include: bool = True


class ChangeUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[PlanItemIn] = Field(min_length=1, max_length=200)


class ApplyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm_production: bool = False


class PlanItemOut(BaseModel):
    spec: FieldSpec
    include: bool
    agreed: bool
    proposed_by: list[str]
    note: str
    payload: dict[str, Any]


class ChangeOut(BaseModel):
    id: str
    connection_id: str
    platform: str
    environment: str
    entity: str
    request: str
    publisher_prefix: str
    solution: str | None
    status: str
    items: list[PlanItemOut]
    models_used: list[str]
    results: list[dict[str, Any]]
    version: int
    error: str | None
    applied_by: str | None
    created_at: datetime
    applied_at: datetime | None


def _analysis_out(row: CrmAnalysis) -> AnalysisOut:
    return AnalysisOut(
        id=row.id,
        connection_id=row.connection_id,
        entities=list(row.entities),
        sample_size=row.sample_size,
        status=row.status,
        report=list(row.report or []),
        error=row.error,
        created_at=row.created_at,
        finished_at=row.finished_at,
    )


async def _change_out(session: SessionDep, row: CrmChange) -> ChangeOut:
    conn = await session.get(Connection, row.connection_id)
    platform = platform_for(conn.platform if conn else None, conn.base_url if conn else "")
    items = []
    for item in row.plan or []:
        spec = FieldSpec.model_validate(item["spec"])
        items.append(
            PlanItemOut(
                spec=spec,
                include=bool(item.get("include", True)),
                agreed=bool(item.get("agreed")),
                proposed_by=list(item.get("proposed_by", [])),
                note=str(item.get("note", "")),
                payload=platform.payload(spec, row.option_value_prefix),
            )
        )
    return ChangeOut(
        id=row.id,
        connection_id=row.connection_id,
        platform=platform.id,
        environment=conn.environment if conn else "unknown",
        entity=row.entity,
        request=row.request,
        publisher_prefix=row.publisher_prefix,
        solution=row.solution,
        status=row.status,
        items=items,
        models_used=list(row.models_used or []),
        results=list(row.results or []),
        version=row.version,
        error=row.error,
        applied_by=row.applied_by,
        created_at=row.created_at,
        applied_at=row.applied_at,
    )


# ------------------------------------------------------------------ analysis


@router.post("/{connection_id}/analyses", status_code=202, response_model=AnalysisOut)
async def start_analysis(
    connection_id: str,
    body: AnalysisCreate,
    request: Request,
    session: SessionDep,
    owner: OwnerDep,
    principal: PrincipalDep,
    settings: SettingsDep,
) -> AnalysisOut:
    await check_quota(principal, settings, session, "tokens")
    conn = await _own(session, Connection, connection_id, owner, "Connection")
    platform = platform_for(conn.platform, conn.base_url)
    bad = [e for e in body.entities if not platform.valid_entity(e)]
    if bad:
        raise HTTPException(HTTP_422, f"Invalid {platform.name} object names: {bad}")
    row = CrmAnalysis(
        owner=owner, connection_id=conn.id, entities=body.entities, sample_size=body.sample_size
    )
    session.add(row)
    await session.commit()
    _service(request).spawn(_service(request).run_analysis(row.id), f"crm-analysis-{row.id}")
    return _analysis_out(row)


@router.get("/analyses", response_model=list[AnalysisOut])
async def list_analyses(
    session: SessionDep, owner: OwnerDep, connection_id: str | None = None
) -> list[AnalysisOut]:
    query = select(CrmAnalysis).where(CrmAnalysis.owner == owner)
    if connection_id:
        query = query.where(CrmAnalysis.connection_id == connection_id)
    rows = (await session.scalars(query.order_by(CrmAnalysis.created_at.desc()).limit(50))).all()
    return [_analysis_out(r) for r in rows]


@router.get("/analyses/{analysis_id}", response_model=AnalysisOut)
async def get_analysis(analysis_id: str, session: SessionDep, owner: OwnerDep) -> AnalysisOut:
    row = await _own(session, CrmAnalysis, analysis_id, owner, "Analysis")
    await session.refresh(row)
    return _analysis_out(row)


# ------------------------------------------------------------------ column changes


@router.post("/{connection_id}/changes", status_code=202, response_model=ChangeOut)
async def start_change(
    connection_id: str,
    body: ChangeCreate,
    request: Request,
    session: SessionDep,
    owner: OwnerDep,
    principal: PrincipalDep,
    settings: SettingsDep,
) -> ChangeOut:
    await check_quota(principal, settings, session, "tokens")
    conn = await _own(session, Connection, connection_id, owner, "Connection")
    if not platform_for(conn.platform, conn.base_url).valid_entity(body.entity):
        raise HTTPException(HTTP_422, f"Invalid object name {body.entity!r}")
    row = CrmChange(
        owner=owner,
        connection_id=conn.id,
        entity=body.entity,
        request=body.request,
        publisher_prefix=body.publisher_prefix,
        option_value_prefix=body.option_value_prefix,
        solution=body.solution,
    )
    session.add(row)
    await session.commit()
    _service(request).spawn(_service(request).run_plan(row.id), f"crm-plan-{row.id}")
    return await _change_out(session, row)


@router.get("/changes", response_model=list[ChangeOut])
async def list_changes(
    session: SessionDep, owner: OwnerDep, connection_id: str | None = None
) -> list[ChangeOut]:
    query = select(CrmChange).where(CrmChange.owner == owner)
    if connection_id:
        query = query.where(CrmChange.connection_id == connection_id)
    rows = (await session.scalars(query.order_by(CrmChange.created_at.desc()).limit(50))).all()
    return [await _change_out(session, r) for r in rows]


@router.get("/changes/{change_id}", response_model=ChangeOut)
async def get_change(change_id: str, session: SessionDep, owner: OwnerDep) -> ChangeOut:
    row = await _own(session, CrmChange, change_id, owner, "Change")
    await session.refresh(row)
    return await _change_out(session, row)


@router.put("/changes/{change_id}", response_model=ChangeOut)
async def update_change(
    change_id: str, body: ChangeUpdate, session: SessionDep, owner: OwnerDep
) -> ChangeOut:
    row = await _own(session, CrmChange, change_id, owner, "Change")
    if row.status not in (CrmChangeState.PROPOSED, CrmChangeState.APPROVED):
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"Cannot edit a change in status {row.status!r}"
        )
    names = [i.spec.logical_name for i in body.items]
    if len(names) != len(set(names)):
        raise HTTPException(HTTP_422, "Each column may appear only once")
    conn = await session.get(Connection, row.connection_id)
    platform = platform_for(conn.platform if conn else None, conn.base_url if conn else "")
    problems = [
        f"{i.spec.schema_name}: {problem}"
        for i in body.items
        if (problem := platform.name_problem(i.spec, row.publisher_prefix))
    ]
    if problems:
        raise HTTPException(HTTP_422, "; ".join(problems))
    old = {i["spec"]["schema_name"].lower(): i for i in row.plan or []}
    row.plan = [
        {
            "spec": i.spec.model_dump(mode="json"),
            "include": i.include,
            "agreed": bool(old.get(i.spec.logical_name, {}).get("agreed"))
            and old[i.spec.logical_name]["spec"] == i.spec.model_dump(mode="json"),
            "proposed_by": old.get(i.spec.logical_name, {}).get("proposed_by", ["you"]),
            "note": "",
        }
        for i in body.items
    ]
    row.status = CrmChangeState.PROPOSED.value
    row.version += 1
    await session.commit()
    return await _change_out(session, row)


@router.post("/changes/{change_id}/approve", response_model=ChangeOut)
async def approve_change(change_id: str, session: SessionDep, owner: OwnerDep) -> ChangeOut:
    row = await _own(session, CrmChange, change_id, owner, "Change")
    if row.status != CrmChangeState.PROPOSED:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"Cannot approve a change in status {row.status!r}"
        )
    if not any(i.get("include") for i in row.plan or []):
        raise HTTPException(status.HTTP_409_CONFLICT, "Include at least one column")
    try:
        for item in row.plan:
            FieldSpec.model_validate(item["spec"])
    except ValidationError as exc:
        raise HTTPException(HTTP_422, str(exc)) from exc
    row.status = CrmChangeState.APPROVED.value
    await session.commit()
    return await _change_out(session, row)


@router.post("/changes/{change_id}/apply", status_code=202, response_model=ChangeOut)
async def apply_change(
    change_id: str, body: ApplyIn, request: Request, session: SessionDep, owner: OwnerDep
) -> ChangeOut:
    row = await _own(session, CrmChange, change_id, owner, "Change")
    conn = await session.get(Connection, row.connection_id)
    if conn is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "The connection was deleted")
    if conn.environment != "sandbox" and not body.confirm_production:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"This connection is marked '{conn.environment}', not 'sandbox'. Apply the change to "
            "a sandbox first, then repeat with confirm_production=true.",
        )
    claimed = await session.execute(
        update(CrmChange)
        .where(CrmChange.id == row.id, CrmChange.status == CrmChangeState.APPROVED.value)
        .values(status=CrmChangeState.APPLYING.value, error=None)
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    if claimed.rowcount != 1:  # type: ignore[attr-defined]
        await session.refresh(row)
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"Cannot apply a change in status {row.status!r}"
        )
    await session.refresh(row)
    _service(request).spawn(
        _service(request).apply(row.id, user_actor(owner)), f"crm-apply-{row.id}"
    )
    return await _change_out(session, row)
