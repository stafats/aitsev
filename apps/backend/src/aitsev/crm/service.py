"""Background work for the CRM pack (Dynamics 365, Salesforce, HubSpot, Zoho CRM):
analysis, field plans and applying them."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy import update

from aitsev.accounts.plans import current_owner
from aitsev.config import Settings
from aitsev.crm.dynamics import (
    ANALYZED_TYPES,
    FieldSpec,
    analyze_records,
    entity_info,
    merge_plans,
)
from aitsev.crm.platforms import Platform, platform_for
from aitsev.db.models import Connection, CrmAnalysis, CrmChange, CrmChangeState
from aitsev.db.session import Database
from aitsev.gateway import ChatMessage, ChatRequest, ModelError, ModelGateway
from aitsev.integrations.http import IntegrationError
from aitsev.integrations.service import IntegrationService
from aitsev.pipeline.parsing import OutputFormatError, extract_json_object

log = logging.getLogger(__name__)


class _PlanAnswer(BaseModel):
    fields: list[dict[str, Any]]


class CrmService:
    def __init__(
        self,
        db: Database,
        gateway: ModelGateway,
        settings: Settings,
        integrations: IntegrationService,
    ) -> None:
        self.db = db
        self.gateway = gateway
        self.settings = settings
        self.integrations = integrations

    def spawn(self, coro: Any, name: str) -> None:
        self.integrations.spawn(coro, name)

    async def recover(self) -> None:
        async with self.db.session() as session:
            await session.execute(
                update(CrmAnalysis)
                .where(CrmAnalysis.status == "running")
                .values(status="failed", error="Interrupted by a server restart")
            )
            await session.execute(
                update(CrmChange)
                .where(CrmChange.status == CrmChangeState.PLANNING.value)
                .values(status=CrmChangeState.FAILED.value, error="Interrupted by a restart")
            )
            await session.execute(
                update(CrmChange)
                .where(CrmChange.status == CrmChangeState.APPLYING.value)
                .values(
                    status=CrmChangeState.FAILED.value,
                    error="Interrupted while applying. Check which columns exist in the "
                    "environment before applying again (existing columns are skipped).",
                )
            )
            await session.commit()

    # ------------------------------------------------------------------ analysis

    async def run_analysis(self, analysis_id: str) -> None:
        try:
            async with self.db.session() as session:
                analysis = await session.get(CrmAnalysis, analysis_id)
                if analysis is None:
                    return
                conn = await session.get(Connection, analysis.connection_id)
                entities, sample = list(analysis.entities), analysis.sample_size
            if conn is None:
                raise IntegrationError("The connection was deleted")
            reports = []
            platform = platform_for(conn.platform, conn.base_url)
            async with self.integrations.client_for(conn) as client:
                for entity in entities:
                    attributes = await platform.fields(client, entity)
                    names = [
                        str(a["LogicalName"])
                        for a in attributes
                        if a.get("AttributeType") in ANALYZED_TYPES
                    ]
                    records, total = await platform.records(client, entity, sample, names)
                    info = {"LogicalName": entity, "EntitySetName": entity}
                    if platform.id == "dynamics":
                        info = await entity_info(client, entity)
                    report = analyze_records(entity, info, attributes, records, total)
                    reports.append(report.model_dump(mode="json"))
            await self._finish_analysis(analysis_id, "done", reports, None)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("CRM analysis %s failed: %s", analysis_id, exc)
            await self._finish_analysis(analysis_id, "failed", [], f"{exc}"[:2000])

    async def _finish_analysis(
        self, analysis_id: str, status: str, report: list[dict[str, Any]], error: str | None
    ) -> None:
        async with self.db.session() as session:
            row = await session.get(CrmAnalysis, analysis_id)
            if row is None:
                return
            row.status = status
            row.report = report
            row.error = error
            row.finished_at = datetime.now(UTC)
            await session.commit()

    # ------------------------------------------------------------------ planning

    async def run_plan(self, change_id: str) -> None:
        try:
            await self._run_plan(change_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("CRM plan %s failed: %s", change_id, exc)
            async with self.db.session() as session:
                row = await session.get(CrmChange, change_id)
                if row is not None:
                    row.status = CrmChangeState.FAILED.value
                    row.error = f"{exc}"[:2000]
                    await session.commit()

    async def _run_plan(self, change_id: str) -> None:
        async with self.db.session() as session:
            change = await session.get(CrmChange, change_id)
            if change is None:
                return
            conn = await session.get(Connection, change.connection_id)
            entity, request, prefix = change.entity, change.request, change.publisher_prefix
            current_owner.set(change.owner)
        if conn is None:
            raise IntegrationError("The connection was deleted")
        platform = platform_for(conn.platform, conn.base_url)
        async with self.integrations.client_for(conn) as client:
            existing = await platform.fields(client, entity)
        existing_names = {platform.existing_key(str(a.get("LogicalName", ""))) for a in existing}
        models = self.gateway.available(
            [*self.settings.generator_models, *self.settings.reviewer_models]
        )[:2]
        if not models:
            raise IntegrationError("No models are available to plan the columns")
        existing_list = sorted(str(a.get("LogicalName")) for a in existing)[:400]
        messages = [
            ChatMessage("system", platform.plan_system(entity, prefix)),
            ChatMessage(
                "user", json.dumps({"request": request, "existing_columns": existing_list})
            ),
        ]
        answers = await asyncio.gather(*(self._ask(m, messages, prefix, platform) for m in models))
        opinions = [(m, a) for m, a in zip(models, answers, strict=True) if a]
        if not opinions:
            raise IntegrationError("No model returned a usable column plan")
        items = merge_plans(
            [
                (m, [s for s in a if platform.existing_key(s.schema_name) not in existing_names])
                for m, a in opinions
            ],
            existing_names,
        )
        if not items:
            raise IntegrationError("Every proposed column already exists on this table")
        async with self.db.session() as session:
            row = await session.get(CrmChange, change_id)
            if row is None:
                return
            row.plan = [{**item, "include": True} for item in items]
            row.models_used = [m for m, _ in opinions]
            row.status = CrmChangeState.PROPOSED.value
            row.error = None
            row.version += 1
            await session.commit()

    async def _ask(
        self, model: str, messages: list[ChatMessage], prefix: str, platform: Platform
    ) -> list[FieldSpec]:
        convo = list(messages)
        for _ in range(2):
            try:
                result = await self.gateway.chat(
                    model,
                    ChatRequest(
                        convo,
                        temperature=0.1,
                        max_tokens=self.settings.structured_max_tokens,
                        json_mode=True,
                    ),
                    stage="crm-plan",
                )
            except ModelError as exc:
                log.warning("CRM plan model %s failed: %s", model, exc)
                return []
            try:
                answer = _PlanAnswer.model_validate(extract_json_object(result.content))
            except (OutputFormatError, ValidationError) as exc:
                convo = [
                    *convo,
                    ChatMessage("assistant", result.content),
                    ChatMessage("user", f"Invalid answer: {str(exc)[:400]}. Return only JSON."),
                ]
                continue
            specs = []
            for item in answer.fields:
                try:
                    spec = FieldSpec.model_validate(item)
                except ValidationError:
                    continue
                if platform.name_problem(spec, prefix) is None:
                    specs.append(spec)
            return specs
        return []

    # ------------------------------------------------------------------ apply

    async def apply(self, change_id: str, actor: str) -> None:
        try:
            await self._apply(change_id, actor)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Applying CRM change %s failed: %s", change_id, exc)
            async with self.db.session() as session:
                row = await session.get(CrmChange, change_id)
                if row is not None and row.status == CrmChangeState.APPLYING.value:
                    row.status = CrmChangeState.FAILED.value
                    row.error = f"{exc}"[:2000]
                    await session.commit()

    async def _apply(self, change_id: str, actor: str) -> None:
        async with self.db.session() as session:
            change = await session.get(CrmChange, change_id)
            if change is None or change.status != CrmChangeState.APPLYING.value:
                return
            conn = await session.get(Connection, change.connection_id)
            entity, solution = change.entity, change.solution
            option_prefix = change.option_value_prefix
            specs = [
                FieldSpec.model_validate(item["spec"])
                for item in change.plan
                if item.get("include")
            ]
        if conn is None:
            raise IntegrationError("The connection was deleted")
        platform = platform_for(conn.platform, conn.base_url)
        results: list[dict[str, Any]] = []
        created = 0
        async with self.integrations.client_for(conn) as client:
            existing = {
                platform.existing_key(str(a.get("LogicalName", "")))
                for a in await platform.fields(client, entity)
            }
            for spec in specs:
                if platform.existing_key(spec.schema_name) in existing:
                    results.append(
                        {
                            "schema_name": spec.schema_name,
                            "status": "skipped",
                            "message": "Column already exists",
                        }
                    )
                    continue
                try:
                    await platform.create(client, entity, spec, option_prefix, solution)
                except IntegrationError as exc:
                    results.append(
                        {
                            "schema_name": spec.schema_name,
                            "status": "failed",
                            "message": str(exc)[:500],
                        }
                    )
                    continue
                created += 1
                results.append(
                    {
                        "schema_name": spec.schema_name,
                        "status": "created",
                        "message": platform.created_note,
                    }
                )
            publish_error = await platform.finish(client, entity, created)
        failed = sum(1 for r in results if r["status"] == "failed")
        if failed == 0 and publish_error is None:
            state = CrmChangeState.APPLIED
        elif created:
            state = CrmChangeState.PARTIAL
        else:
            state = CrmChangeState.FAILED
        async with self.db.session() as session:
            row = await session.get(CrmChange, change_id)
            if row is None:
                return
            row.results = results
            row.status = state.value
            row.error = publish_error or (f"{failed} column(s) failed" if failed else None)
            row.applied_at = datetime.now(UTC)
            row.applied_by = actor
            await session.commit()
