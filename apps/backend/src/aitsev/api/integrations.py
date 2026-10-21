"""Integration endpoints: connections, discovery, auto-mapping, review, jobs and script export."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field, HttpUrl
from sqlalchemy import select, update

from aitsev.accounts import plans
from aitsev.api.deps import OwnerDep, PrincipalDep, SessionDep, SettingsDep, check_quota
from aitsev.crm.platforms import platform_for
from aitsev.db.models import Connection, Job, JobState, Mapping, MappingState
from aitsev.integrations.codegen import export_script
from aitsev.integrations.crypto import SecretBoxError
from aitsev.integrations.http import (
    AuthConfig,
    Credentials,
    IntegrationError,
    Pagination,
    check_url_allowed,
)
from aitsev.integrations.mapping import FieldMapping, blocking_issues
from aitsev.integrations.schema import EntityDef, SchemaModel, SchemaParseError
from aitsev.integrations.service import IntegrationService, entity_of

router = APIRouter(prefix="/v1", tags=["integrations"])

HTTP_413 = 413
HTTP_422 = 422
MAX_SPEC_BYTES = 5_000_000
_FORBIDDEN_HEADERS = {"authorization", "cookie", "host", "content-length", "x-api-key"}


def _service(request: Request) -> IntegrationService:
    service: IntegrationService = request.app.state.integrations
    return service


# ------------------------------------------------------------------ schemas


class ConnectionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    base_url: HttpUrl
    auth: AuthConfig = Field(default_factory=AuthConfig)
    credentials: Credentials = Field(default_factory=Credentials)
    headers: dict[str, str] = Field(default_factory=dict, description="Extra non-secret headers")
    pagination: Pagination = Field(default_factory=Pagination)
    spec_format: Literal["openapi", "odata", "samples"]
    spec_text: str = Field(default="", description="The spec document (or samples JSON)")
    environment: Literal["sandbox", "production", "unknown"] = Field(
        default="unknown",
        description="CRM changes need extra confirmation unless this is 'sandbox'",
    )
    platform: Literal["auto", "dynamics", "salesforce", "hubspot", "zoho"] = Field(
        default="auto", description="CRM platform for the CRM pack; auto = detect from the URL"
    )


class ConnectionOut(BaseModel):
    id: str
    name: str
    base_url: str
    auth: AuthConfig
    has_credentials: bool
    headers: dict[str, str]
    pagination: Pagination
    spec_format: str
    environment: str
    platform: str = Field(description="Effective CRM platform (auto-detected when not set)")
    entities: list[str]
    discovered_at: datetime | None
    last_error: str | None
    created_at: datetime


class DiscoverIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    spec_path: str | None = Field(
        default=None, max_length=500, description="Fetch the spec from this path, e.g. /$metadata"
    )


class MappingCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=200)
    source_connection_id: str
    source_entity: str
    target_connection_id: str
    target_entity: str
    include_samples: bool = Field(
        default=False,
        description="Send up to 5 sample records from each side to the models (better mappings, "
        "but real data leaves your systems)",
    )


class MappingOut(BaseModel):
    id: str
    name: str
    status: str
    source_connection_id: str
    source_entity: str
    target_connection_id: str
    target_entity: str
    fields: list[FieldMapping]
    unmapped_targets: list[str]
    blocking_issues: list[str]
    models_used: list[str]
    version: int
    dry_run_passed: bool
    error: str | None
    approved_at: datetime | None
    updated_at: datetime


class MappingUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fields: list[FieldMapping]


class JobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mapping_id: str
    dry_run: bool = True
    limit: int | None = Field(default=None, ge=1)
    chunk_size: int = Field(default=100, ge=1, le=10_000)


class JobOut(BaseModel):
    id: str
    mapping_id: str
    mapping_version: int
    dry_run: bool
    status: str
    record_limit: int | None
    stats: dict[str, int]
    errors: list[dict[str, Any]]
    sample: list[dict[str, Any]]
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


# ------------------------------------------------------------------ helpers


def _conn_out(conn: Connection) -> ConnectionOut:
    entities = [e["name"] for e in (conn.schema_doc or {}).get("entities", [])]
    return ConnectionOut(
        id=conn.id,
        name=conn.name,
        base_url=conn.base_url,
        auth=AuthConfig.model_validate(conn.auth or {}),
        has_credentials=bool(conn.credentials_encrypted),
        headers=conn.headers or {},
        pagination=Pagination.model_validate(conn.pagination or {}),
        spec_format=conn.spec_format,
        environment=conn.environment,
        platform=platform_for(conn.platform, conn.base_url).id,
        entities=entities,
        discovered_at=conn.discovered_at,
        last_error=conn.last_error,
        created_at=conn.created_at,
    )


def _fields(mapping: Mapping) -> list[FieldMapping]:
    return [FieldMapping.model_validate(f) for f in mapping.fields or []]


async def _target_entity(session: SessionDep, mapping: Mapping) -> EntityDef | None:
    conn = await session.get(Connection, mapping.target_connection_id)
    if conn is None or not conn.schema_doc:
        return None
    return SchemaModel.model_validate(conn.schema_doc).entity(mapping.target_entity)


async def _mapping_out(session: SessionDep, mapping: Mapping) -> MappingOut:
    fields = _fields(mapping)
    target = await _target_entity(session, mapping)
    issues = blocking_issues(target, fields) if target else ["Target entity is unavailable"]
    mapped = {f.target for f in fields if f.status != "rejected"}
    unmapped = (
        [
            f.name
            for f in target.fields
            if not f.read_only and f.name != target.key and f.name not in mapped
        ]
        if target
        else []
    )
    return MappingOut(
        id=mapping.id,
        name=mapping.name,
        status=mapping.status,
        source_connection_id=mapping.source_connection_id,
        source_entity=mapping.source_entity,
        target_connection_id=mapping.target_connection_id,
        target_entity=mapping.target_entity,
        fields=fields,
        unmapped_targets=unmapped,
        blocking_issues=issues if mapping.status != MappingState.MAPPING else [],
        models_used=list(mapping.models_used or []),
        version=mapping.version,
        dry_run_passed=mapping.dry_run_ok_version == mapping.version,
        error=mapping.error,
        approved_at=mapping.approved_at,
        updated_at=mapping.updated_at,
    )


def _job_out(job: Job) -> JobOut:
    return JobOut(
        id=job.id,
        mapping_id=job.mapping_id,
        mapping_version=job.mapping_version,
        dry_run=job.dry_run,
        status=job.status,
        record_limit=job.record_limit,
        stats=job.stats or {},
        errors=job.errors or [],
        sample=job.sample or [],
        error=job.error,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
    )


async def _own(session: SessionDep, model: Any, item_id: str, owner: str, label: str) -> Any:
    row = (
        await session.scalars(select(model).where(model.id == item_id, model.owner == owner))
    ).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{label} not found")
    return row


# ------------------------------------------------------------------ connections


@router.post("/connections", status_code=201, response_model=ConnectionOut)
async def create_connection(
    body: ConnectionCreate,
    request: Request,
    session: SessionDep,
    owner: OwnerDep,
    settings: SettingsDep,
) -> ConnectionOut:
    if len(body.spec_text.encode("utf-8")) > MAX_SPEC_BYTES:
        raise HTTPException(HTTP_413, f"spec_text is larger than {MAX_SPEC_BYTES} bytes")
    bad = [h for h in body.headers if h.lower() in _FORBIDDEN_HEADERS]
    if bad:
        raise HTTPException(HTTP_422, f"Put secrets in credentials, not headers: {bad}")
    base_url = str(body.base_url)
    try:
        await check_url_allowed(base_url, allow_private=settings.private_networks_allowed())
        if body.auth.token_url:
            await check_url_allowed(
                body.auth.token_url, allow_private=settings.private_networks_allowed()
            )
    except IntegrationError as exc:
        raise HTTPException(HTTP_422, str(exc)) from exc
    secrets = body.credentials.model_dump(exclude_none=True)
    try:
        encrypted = _service(request).box.encrypt(secrets) if secrets else ""
    except SecretBoxError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    conn = Connection(
        owner=owner,
        name=body.name.strip(),
        base_url=base_url,
        auth=body.auth.model_dump(),
        credentials_encrypted=encrypted,
        headers=body.headers,
        pagination=body.pagination.model_dump(),
        spec_format=body.spec_format,
        spec_text=body.spec_text,
        environment=body.environment,
        platform=body.platform,
    )
    session.add(conn)
    await session.commit()
    return _conn_out(conn)


@router.get("/connections", response_model=list[ConnectionOut])
async def list_connections(session: SessionDep, owner: OwnerDep) -> list[ConnectionOut]:
    rows = (
        await session.scalars(
            select(Connection).where(Connection.owner == owner).order_by(Connection.created_at)
        )
    ).all()
    return [_conn_out(c) for c in rows]


@router.get("/connections/{connection_id}", response_model=ConnectionOut)
async def get_connection(connection_id: str, session: SessionDep, owner: OwnerDep) -> ConnectionOut:
    return _conn_out(await _own(session, Connection, connection_id, owner, "Connection"))


@router.delete("/connections/{connection_id}", status_code=204)
async def delete_connection(connection_id: str, session: SessionDep, owner: OwnerDep) -> None:
    conn = await _own(session, Connection, connection_id, owner, "Connection")
    used = (
        await session.scalars(
            select(Mapping.id).where(
                (Mapping.source_connection_id == conn.id)
                | (Mapping.target_connection_id == conn.id)
            )
        )
    ).first()
    if used:
        raise HTTPException(status.HTTP_409_CONFLICT, "Delete the mappings that use it first")
    await session.delete(conn)
    await session.commit()


@router.post("/connections/{connection_id}/discover", response_model=SchemaModel)
async def discover(
    connection_id: str, body: DiscoverIn, request: Request, session: SessionDep, owner: OwnerDep
) -> SchemaModel:
    conn = await _own(session, Connection, connection_id, owner, "Connection")
    try:
        schema = await _service(request).discover(conn, body.spec_path)
    except (IntegrationError, SchemaParseError, SecretBoxError) as exc:
        conn.last_error = str(exc)[:2000]
        await session.commit()
        raise HTTPException(HTTP_422, str(exc)) from exc
    await session.commit()
    return schema


@router.get("/connections/{connection_id}/schema", response_model=SchemaModel)
async def get_schema(connection_id: str, session: SessionDep, owner: OwnerDep) -> SchemaModel:
    conn = await _own(session, Connection, connection_id, owner, "Connection")
    if not conn.schema_doc:
        raise HTTPException(status.HTTP_409_CONFLICT, "Run discovery first")
    return SchemaModel.model_validate(conn.schema_doc)


# ------------------------------------------------------------------ mappings


@router.post("/mappings", status_code=202, response_model=MappingOut)
async def create_mapping(
    body: MappingCreate,
    request: Request,
    session: SessionDep,
    owner: OwnerDep,
    principal: PrincipalDep,
    settings: SettingsDep,
) -> MappingOut:
    await check_quota(principal, settings, session, "tokens")
    source = await _own(session, Connection, body.source_connection_id, owner, "Source connection")
    target = await _own(session, Connection, body.target_connection_id, owner, "Target connection")
    try:
        entity_of(source, body.source_entity)
        entity_of(target, body.target_entity)
    except IntegrationError as exc:
        raise HTTPException(HTTP_422, str(exc)) from exc
    mapping = Mapping(
        owner=owner,
        name=body.name or f"{body.source_entity} → {body.target_entity}",
        source_connection_id=source.id,
        source_entity=body.source_entity,
        target_connection_id=target.id,
        target_entity=body.target_entity,
        status=MappingState.MAPPING.value,
    )
    session.add(mapping)
    await session.commit()
    _service(request).spawn(
        _service(request).run_mapping(mapping.id, body.include_samples), f"mapping-{mapping.id}"
    )
    return await _mapping_out(session, mapping)


@router.get("/mappings", response_model=list[MappingOut])
async def list_mappings(session: SessionDep, owner: OwnerDep) -> list[MappingOut]:
    rows = (
        await session.scalars(
            select(Mapping).where(Mapping.owner == owner).order_by(Mapping.created_at)
        )
    ).all()
    return [await _mapping_out(session, m) for m in rows]


@router.get("/mappings/{mapping_id}", response_model=MappingOut)
async def get_mapping(mapping_id: str, session: SessionDep, owner: OwnerDep) -> MappingOut:
    mapping = await _own(session, Mapping, mapping_id, owner, "Mapping")
    await session.refresh(mapping)
    return await _mapping_out(session, mapping)


@router.put("/mappings/{mapping_id}", response_model=MappingOut)
async def update_mapping(
    mapping_id: str, body: MappingUpdate, session: SessionDep, owner: OwnerDep
) -> MappingOut:
    """Replace the field mappings after review. Any change resets approval and the dry run."""
    mapping = await _own(session, Mapping, mapping_id, owner, "Mapping")
    if mapping.status == MappingState.MAPPING:
        raise HTTPException(status.HTTP_409_CONFLICT, "Auto-mapping is still running")
    target = await _target_entity(session, mapping)
    source_conn = await session.get(Connection, mapping.source_connection_id)
    if target is None or source_conn is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "A connection of this mapping is missing")
    source = entity_of(source_conn, mapping.source_entity)
    source_names = {f.name for f in source.fields}
    seen: set[str] = set()
    for field in body.fields:
        tfield = target.field(field.target)
        if tfield is None or tfield.read_only:
            raise HTTPException(HTTP_422, f"{field.target!r} is not a writable target field")
        if field.target in seen:
            raise HTTPException(HTTP_422, f"{field.target!r} is mapped twice")
        seen.add(field.target)
        for name in [field.source, *field.sources]:
            if name and name.split(".", 1)[0] not in source_names:
                raise HTTPException(HTTP_422, f"Source field {name!r} does not exist")
    old = {f.target: f for f in _fields(mapping)}
    updated = []
    for field in body.fields:
        before = old.get(field.target)
        changed = before is None or field.model_dump(
            exclude={"status", "reason"}
        ) != before.model_dump(exclude={"status", "reason"})
        updated.append(field.model_copy(update={"method": "manual"}) if changed else field)
    mapping.fields = [f.model_dump(mode="json") for f in updated]
    mapping.version += 1
    mapping.status = MappingState.DRAFT.value
    mapping.approved_at = None
    await session.commit()
    return await _mapping_out(session, mapping)


@router.post("/mappings/{mapping_id}/approve", response_model=MappingOut)
async def approve_mapping(mapping_id: str, session: SessionDep, owner: OwnerDep) -> MappingOut:
    mapping = await _own(session, Mapping, mapping_id, owner, "Mapping")
    if mapping.status != MappingState.DRAFT:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"Cannot approve a mapping in status {mapping.status!r}"
        )
    target = await _target_entity(session, mapping)
    issues = (
        blocking_issues(target, _fields(mapping)) if target else ["Target entity is unavailable"]
    )
    if issues:
        raise HTTPException(status.HTTP_409_CONFLICT, "; ".join(issues))
    mapping.status = MappingState.APPROVED.value
    mapping.approved_at = datetime.now(UTC)
    await session.commit()
    return await _mapping_out(session, mapping)


@router.get("/mappings/{mapping_id}/export", response_class=PlainTextResponse)
async def export_mapping(
    mapping_id: str, session: SessionDep, owner: OwnerDep
) -> PlainTextResponse:
    mapping = await _own(session, Mapping, mapping_id, owner, "Mapping")
    if mapping.status != MappingState.APPROVED:
        raise HTTPException(status.HTTP_409_CONFLICT, "Approve the mapping before exporting it")
    source_conn = await session.get(Connection, mapping.source_connection_id)
    target_conn = await session.get(Connection, mapping.target_connection_id)
    if source_conn is None or target_conn is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "A connection of this mapping is missing")
    filename, text = export_script(
        mapping_id=mapping.id,
        version=mapping.version,
        source_url=source_conn.base_url,
        source=entity_of(source_conn, mapping.source_entity),
        target_url=target_conn.base_url,
        target=entity_of(target_conn, mapping.target_entity),
        pagination=source_conn.pagination or {},
        fields=_fields(mapping),
    )
    return PlainTextResponse(
        text, headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


# ------------------------------------------------------------------ jobs


@router.post("/jobs", status_code=202, response_model=JobOut)
async def create_job(
    body: JobCreate,
    request: Request,
    session: SessionDep,
    owner: OwnerDep,
    principal: PrincipalDep,
    settings: SettingsDep,
) -> JobOut:
    mapping = await _own(session, Mapping, body.mapping_id, owner, "Mapping")
    if mapping.status != MappingState.APPROVED:
        raise HTTPException(status.HTTP_409_CONFLICT, "Approve the mapping before running jobs")
    if not body.dry_run and mapping.dry_run_ok_version != mapping.version:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Run a dry run with no invalid records on this mapping version before a real run",
        )
    running = (
        await session.scalars(
            select(Job.id).where(
                Job.mapping_id == mapping.id,
                Job.status.in_([JobState.QUEUED.value, JobState.RUNNING.value]),
            )
        )
    ).first()
    if running:
        raise HTTPException(status.HTTP_409_CONFLICT, "A job for this mapping is already running")
    record_limit = body.limit
    if not body.dry_run:
        usage = await check_quota(principal, settings, session, "records")
        account = principal.account
        if usage is not None and account is not None:
            remaining = plans.report(account.plan, usage).records.remaining
            if remaining is not None:  # never write more than the plan has left this month
                record_limit = remaining if record_limit is None else min(record_limit, remaining)
    job = Job(
        owner=owner,
        mapping_id=mapping.id,
        mapping_version=mapping.version,
        dry_run=body.dry_run,
        record_limit=record_limit,
        chunk_size=body.chunk_size,
        status=JobState.QUEUED.value,
    )
    session.add(job)
    await session.commit()
    _service(request).spawn(_service(request).run_job(job.id), f"job-{job.id}")
    return _job_out(job)


@router.get("/jobs", response_model=list[JobOut])
async def list_jobs(
    session: SessionDep, owner: OwnerDep, mapping_id: str | None = None
) -> list[JobOut]:
    query = select(Job).where(Job.owner == owner)
    if mapping_id:
        query = query.where(Job.mapping_id == mapping_id)
    rows = (await session.scalars(query.order_by(Job.created_at.desc()).limit(100))).all()
    return [_job_out(j) for j in rows]


@router.get("/jobs/{job_id}", response_model=JobOut)
async def get_job(job_id: str, session: SessionDep, owner: OwnerDep) -> JobOut:
    job = await _own(session, Job, job_id, owner, "Job")
    await session.refresh(job)
    return _job_out(job)


@router.post("/jobs/{job_id}/cancel", response_model=JobOut)
async def cancel_job(job_id: str, session: SessionDep, owner: OwnerDep) -> JobOut:
    job = await _own(session, Job, job_id, owner, "Job")
    result = await session.execute(
        update(Job)
        .where(Job.id == job.id, Job.status.in_([JobState.QUEUED.value, JobState.RUNNING.value]))
        .values(status=JobState.CANCELLED.value, finished_at=datetime.now(UTC))
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    await session.refresh(job)
    if result.rowcount != 1:  # type: ignore[attr-defined]
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"Cannot cancel a job in status {job.status!r}"
        )
    return _job_out(job)
