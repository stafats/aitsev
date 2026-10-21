"""Integration services: discovery, auto-mapping and job execution, run in the background."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Coroutine
from datetime import UTC, datetime
from typing import Any

import httpx
import yaml
from sqlalchemy import select, update

from aitsev.accounts.plans import current_owner
from aitsev.config import Settings
from aitsev.db.models import Connection, Job, JobState, Mapping, MappingState
from aitsev.db.session import Database
from aitsev.gateway import ModelGateway
from aitsev.integrations.crypto import SecretBox
from aitsev.integrations.http import (
    ApiClient,
    AuthConfig,
    Credentials,
    IntegrationError,
    Pagination,
)
from aitsev.integrations.mapping import (
    FieldMapping,
    ask_model,
    combine,
    mappable_targets,
    mapping_messages,
    rule_based,
)
from aitsev.integrations.records import transform_record, validate_record
from aitsev.integrations.schema import (
    EntityDef,
    SchemaModel,
    SchemaParseError,
    infer_from_samples,
    parse_odata,
    parse_openapi,
)

log = logging.getLogger(__name__)

MAX_STORED_ERRORS = 200
MIN_RECORDS_FOR_ERROR_RATE = 50


def parse_spec(spec_format: str, text: str) -> SchemaModel:
    """Parse a stored spec. ``samples`` is JSON: {"Entity": {"list_path"?, "create_path"?,
    "records": [...]}}."""
    if spec_format == "openapi":
        try:
            doc = json.loads(text) if text.lstrip().startswith("{") else yaml.safe_load(text)
        except (ValueError, yaml.YAMLError) as exc:
            raise SchemaParseError(f"OpenAPI document is not valid JSON or YAML: {exc}") from exc
        return parse_openapi(doc)
    if spec_format == "odata":
        return parse_odata(text)
    if spec_format == "samples":
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise SchemaParseError(f"Samples must be JSON: {exc}") from exc
        if not isinstance(data, dict) or not data:
            raise SchemaParseError('Samples must look like {"Entity": {"records": [...]}}')
        entities = []
        for name, spec in data.items():
            if not isinstance(spec, dict):
                raise SchemaParseError(f"Samples for {name!r} must be an object")
            entities.append(
                infer_from_samples(
                    name,
                    spec.get("records") or [],
                    list_path=spec.get("list_path"),
                    create_path=spec.get("create_path"),
                )
            )
        return SchemaModel(source="samples", entities=entities)
    raise SchemaParseError(f"Unknown spec format {spec_format!r}")


class IntegrationService:
    def __init__(
        self,
        db: Database,
        gateway: ModelGateway,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.db = db
        self.gateway = gateway
        self.settings = settings
        self.transport = transport
        self._box: SecretBox | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def box(self) -> SecretBox:
        if self._box is None:
            self._box = SecretBox.from_settings(self.settings)
        return self._box

    # ------------------------------------------------------------------ lifecycle

    def spawn(self, coro: Coroutine[Any, Any, None], name: str) -> None:
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def recover(self) -> None:
        """Background work does not survive a restart; mark it so the user can re-run it."""
        async with self.db.session() as session:
            for mapping in (
                await session.scalars(
                    select(Mapping).where(Mapping.status == MappingState.MAPPING.value)
                )
            ).all():
                mapping.status = MappingState.FAILED.value
                mapping.error = "Interrupted by a server restart; run auto-map again"
            for job in (
                await session.scalars(
                    select(Job).where(
                        Job.status.in_([JobState.QUEUED.value, JobState.RUNNING.value])
                    )
                )
            ).all():
                job.status = JobState.FAILED.value
                job.error = (
                    "Interrupted by a server restart. Records written before the restart stay "
                    "in the target; check them before starting a new run."
                )
                job.finished_at = datetime.now(UTC)
            await session.commit()

    async def shutdown(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def wait_idle(self) -> None:
        """Wait for background work (used by tests)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    # ------------------------------------------------------------------ connections

    def client_for(self, conn: Connection) -> ApiClient:
        creds = Credentials.model_validate(
            self.box.decrypt(conn.credentials_encrypted) if conn.credentials_encrypted else {}
        )
        return ApiClient(
            conn.base_url,
            AuthConfig.model_validate(conn.auth or {}),
            creds,
            headers=conn.headers or {},
            timeout=self.settings.integration_timeout_seconds,
            max_retries=self.settings.integration_max_retries,
            allow_private=self.settings.private_networks_allowed(),
            transport=self.transport,
        )

    async def discover(self, conn: Connection, spec_path: str | None) -> SchemaModel:
        text = conn.spec_text
        if spec_path:
            async with self.client_for(conn) as client:
                text = await client.get_text(spec_path)
        if not text.strip():
            raise SchemaParseError("No spec: upload spec_text or give spec_path to fetch it")
        schema = parse_spec(conn.spec_format, text)
        conn.spec_text = text
        conn.schema_doc = schema.model_dump()
        conn.discovered_at = datetime.now(UTC)
        conn.last_error = None
        return schema

    async def sample_records(self, conn: Connection, entity: EntityDef, limit: int) -> list[Any]:
        if not entity.list_path:
            return []
        pagination = Pagination.model_validate(conn.pagination or {})
        records: list[Any] = []
        async with self.client_for(conn) as client:
            async for record in client.paginate(
                entity.list_path, pagination, records_key=entity.records_key, limit=limit
            ):
                records.append(record)
        return records

    # ------------------------------------------------------------------ auto-mapping

    async def run_mapping(self, mapping_id: str, include_samples: bool) -> None:
        try:
            await self._run_mapping(mapping_id, include_samples)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("Auto-mapping %s failed", mapping_id)
            async with self.db.session() as session:
                mapping = await session.get(Mapping, mapping_id)
                if mapping is not None:
                    mapping.status = MappingState.FAILED.value
                    mapping.error = f"{type(exc).__name__}: {exc}"[:2000]
                    await session.commit()

    async def _run_mapping(self, mapping_id: str, include_samples: bool) -> None:
        async with self.db.session() as session:
            mapping = await session.get(Mapping, mapping_id)
            if mapping is None:
                return
            source_conn = await session.get(Connection, mapping.source_connection_id)
            target_conn = await session.get(Connection, mapping.target_connection_id)
        current_owner.set(mapping.owner)
        if source_conn is None or target_conn is None:
            raise IntegrationError("A connection of this mapping was deleted")
        source = entity_of(source_conn, mapping.source_entity)
        target = entity_of(target_conn, mapping.target_entity)

        rules = rule_based(source, target)
        uncertain = [
            f
            for f in mappable_targets(target)
            if f.name not in rules or rules[f.name].confidence < 0.9 or f.required
        ]
        models = self.gateway.available(
            [*self.settings.generator_models, *self.settings.reviewer_models]
        )[:2]
        opinions: list[tuple[str, dict[str, FieldMapping]]] = []
        if uncertain and models:
            source_samples: list[Any] = []
            target_samples: list[Any] = []
            if include_samples:
                with contextlib.suppress(IntegrationError):
                    source_samples = await self.sample_records(source_conn, source, 5)
                with contextlib.suppress(IntegrationError):
                    target_samples = await self.sample_records(target_conn, target, 5)
            messages = mapping_messages(
                source, target, uncertain, rules, source_samples, target_samples
            )
            answers = await asyncio.gather(
                *(
                    ask_model(
                        self.gateway,
                        m,
                        messages,
                        source,
                        target,
                        self.settings.structured_max_tokens,
                    )
                    for m in models
                )
            )
            opinions = [(m, a) for m, a in zip(models, answers, strict=True) if a]
        fields = combine(target, rules, opinions)

        async with self.db.session() as session:
            row = await session.get(Mapping, mapping_id)
            if row is None:
                return
            row.fields = [f.model_dump(mode="json") for f in fields]
            row.models_used = [m for m, _ in opinions]
            row.status = MappingState.DRAFT.value
            row.error = None
            row.version += 1
            await session.commit()

    # ------------------------------------------------------------------ jobs

    async def run_job(self, job_id: str) -> None:
        try:
            await self._run_job(job_id)
        except asyncio.CancelledError:
            await self._finish_job(job_id, JobState.FAILED, "Stopped by a server shutdown")
            raise
        except Exception as exc:
            log.exception("Job %s failed", job_id)
            await self._finish_job(job_id, JobState.FAILED, f"{type(exc).__name__}: {exc}")

    async def _run_job(self, job_id: str) -> None:
        async with self.db.session() as session:
            claimed = await session.execute(
                update(Job)
                .where(Job.id == job_id, Job.status == JobState.QUEUED.value)
                .values(status=JobState.RUNNING.value, started_at=datetime.now(UTC))
                .execution_options(synchronize_session=False)
            )
            await session.commit()
            if claimed.rowcount != 1:  # type: ignore[attr-defined]
                return  # cancelled before it started
            job = await session.get(Job, job_id)
            if job is None:
                return
            mapping = await session.get(Mapping, job.mapping_id)
            if mapping is None:
                raise IntegrationError("Mapping was deleted")
            source_conn = await session.get(Connection, mapping.source_connection_id)
            target_conn = await session.get(Connection, mapping.target_connection_id)
            if source_conn is None or target_conn is None:
                raise IntegrationError("A connection of this mapping was deleted")
            dry_run, limit, chunk = job.dry_run, job.record_limit, max(1, job.chunk_size)
            fields = [FieldMapping.model_validate(f) for f in mapping.fields]
            mapping_version = mapping.version

        source = entity_of(source_conn, mapping.source_entity)
        target = entity_of(target_conn, mapping.target_entity)
        if not source.list_path:
            raise IntegrationError(f"Source entity {source.name} has no list endpoint")
        if not dry_run and not target.create_path:
            raise IntegrationError(f"Target entity {target.name} has no create endpoint")

        stats = {"read": 0, "valid": 0, "invalid": 0, "written": 0, "write_failed": 0}
        errors: list[dict[str, Any]] = []
        sample: list[dict[str, Any]] = []
        max_records = min(limit or self.settings.job_max_records, self.settings.job_max_records)
        pagination = Pagination.model_validate(source_conn.pagination or {})

        def record_error(index: int, key: Any, stage: str, messages: list[str]) -> None:
            if len(errors) < MAX_STORED_ERRORS:
                errors.append(
                    {"index": index, "source_key": key, "stage": stage, "errors": messages[:10]}
                )

        async with self.client_for(source_conn) as reader, self.client_for(target_conn) as writer:
            batch_count = 0
            async for record in reader.paginate(
                source.list_path,
                pagination,
                records_key=source.records_key,
                limit=max_records,
            ):
                index = stats["read"]
                stats["read"] += 1
                key = record.get(source.key) if source.key else None
                out, transform_errors = transform_record(record, fields)
                problems = transform_errors + validate_record(out, target)
                if problems:
                    stats["invalid"] += 1
                    record_error(index, key, "validate", problems)
                else:
                    stats["valid"] += 1
                    if len(sample) < self.settings.job_sample_size:
                        sample.append(out)
                    if not dry_run:
                        try:
                            await writer.post_json(str(target.create_path), out)
                            stats["written"] += 1
                        except IntegrationError as exc:
                            stats["write_failed"] += 1
                            record_error(index, key, "write", [str(exc)[:500]])
                batch_count += 1
                if batch_count >= chunk:
                    batch_count = 0
                    if not await self._checkpoint(job_id, stats, errors, sample):
                        return  # cancelled
                    if not dry_run and self._too_many_errors(stats):
                        await self._finish_job(
                            job_id,
                            JobState.STOPPED,
                            f"Stopped: error rate above {self.settings.job_max_error_rate:.0%}",
                            stats,
                            errors,
                            sample,
                        )
                        return

        if not await self._checkpoint(job_id, stats, errors, sample):
            return
        if not dry_run and self._too_many_errors(stats):
            await self._finish_job(
                job_id,
                JobState.STOPPED,
                f"Finished with error rate above {self.settings.job_max_error_rate:.0%}",
                stats,
                errors,
                sample,
            )
            return
        passed_dry_run = dry_run and stats["read"] > 0 and stats["invalid"] == 0
        await self._finish_job(
            job_id,
            JobState.SUCCEEDED,
            None,
            stats,
            errors,
            sample,
            dry_run_passed=(mapping.id, mapping_version) if passed_dry_run else None,
        )

    def _too_many_errors(self, stats: dict[str, int]) -> bool:
        processed = stats["read"]
        failed = stats["invalid"] + stats["write_failed"]
        return (
            processed >= MIN_RECORDS_FOR_ERROR_RATE
            and failed / processed > self.settings.job_max_error_rate
        )

    async def _checkpoint(
        self,
        job_id: str,
        stats: dict[str, int],
        errors: list[dict[str, Any]],
        sample: list[dict[str, Any]],
    ) -> bool:
        """Save progress; returns False when the job was cancelled."""
        async with self.db.session() as session:
            await session.execute(
                update(Job)
                .where(Job.id == job_id)
                .values(stats=dict(stats), errors=list(errors), sample=list(sample))
                .execution_options(synchronize_session=False)
            )
            await session.commit()
            current = await session.scalar(select(Job.status).where(Job.id == job_id))
            return current not in (None, JobState.CANCELLED.value)

    async def _finish_job(
        self,
        job_id: str,
        state: JobState,
        error: str | None,
        stats: dict[str, int] | None = None,
        errors: list[dict[str, Any]] | None = None,
        sample: list[dict[str, Any]] | None = None,
        dry_run_passed: tuple[str, int] | None = None,
    ) -> None:
        """Set the final state atomically, never overwriting a cancellation.

        When a dry run passed, the mapping is marked in the same transaction, so a client that
        sees the job succeed can start the real run straight away.
        """
        values: dict[str, Any] = {
            "status": state.value,
            "error": error[:2000] if error else None,
            "finished_at": datetime.now(UTC),
        }
        if stats is not None:
            values["stats"] = dict(stats)
        if errors is not None:
            values["errors"] = list(errors)
        if sample is not None:
            values["sample"] = list(sample)
        async with self.db.session() as session:
            result = await session.execute(
                update(Job)
                .where(Job.id == job_id, Job.status != JobState.CANCELLED.value)
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            if dry_run_passed is not None and result.rowcount == 1:  # type: ignore[attr-defined]
                mapping_id, version = dry_run_passed
                await session.execute(
                    update(Mapping)
                    .where(Mapping.id == mapping_id, Mapping.version == version)
                    .values(dry_run_ok_version=version)
                    .execution_options(synchronize_session=False)
                )
            await session.commit()


def entity_of(conn: Connection, name: str) -> EntityDef:
    if not conn.schema_doc:
        raise IntegrationError(f"Connection {conn.name!r} has not been discovered yet")
    schema = SchemaModel.model_validate(conn.schema_doc)
    entity = schema.entity(name)
    if entity is None:
        raise IntegrationError(f"Entity {name!r} not found in connection {conn.name!r}")
    return entity
