"""The pipeline that turns one task into one approved-ready result.

plan -> generate (N models in parallel, each with a verify/repair loop) -> cross-review
-> judge -> awaiting_approval

Every stage writes audit events. Cancellation is checked between steps.
"""

from __future__ import annotations

import asyncio
import logging
import statistics
from collections.abc import Coroutine, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from aitsev.accounts.plans import current_owner
from aitsev.audit import add_event, record_event
from aitsev.config import Settings
from aitsev.db.models import Candidate, CandidateStatus, Task, TaskSize, TaskStatus
from aitsev.db.session import Database
from aitsev.gateway import ChatMessage, ChatRequest, ModelError, ModelGateway
from aitsev.pipeline import prompts
from aitsev.pipeline.outputs import JudgeDecision, Plan, Review
from aitsev.pipeline.parsing import OutputFormatError, extract_json_object, parse_file_blocks
from aitsev.pipeline.sandbox import Runner, VerificationResult
from aitsev.pipeline.workspace import (
    ChangeSet,
    UnsafePathError,
    apply_changes,
    builtin_checks,
    changed_line_count,
    diff_changes,
    normalize_files,
    normalize_path,
)

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

TEAM_SIZE = {"small": 1, "normal": 2, "large": 3}
LABELS = "ABCDEFGH"
MAX_CANDIDATE_RENDER = 40_000


class TaskCancelledError(Exception):
    """The task was cancelled while the pipeline was running."""


class PipelineError(Exception):
    """A stage could not produce a usable result; the task fails with this message."""


@dataclass(frozen=True, slots=True)
class TaskSnapshot:
    id: str
    owner: str
    instructions: str
    feedback: str | None
    files: dict[str, str]
    context_paths: list[str]
    models: list[str]
    setup_commands: list[str]
    verify_commands: list[str]
    sandbox_image: str | None
    requested_size: str
    run_number: int

    @property
    def context_files(self) -> dict[str, str]:
        """Files the models read: the context subset, or every file when none is set."""
        if not self.context_paths:
            return self.files
        return {p: self.files[p] for p in self.context_paths if p in self.files}

    @property
    def other_paths(self) -> list[str]:
        """Paths that exist in the project but are not shown to the models."""
        shown = self.context_files
        return sorted(p for p in self.files if p not in shown)


@dataclass(slots=True)
class Attempt:
    id: str
    label: str
    model: str
    status: CandidateStatus = CandidateStatus.GENERATING
    attempts: int = 0
    summary: str = ""
    changes: ChangeSet = field(default_factory=ChangeSet)
    verification: VerificationResult | None = None
    reviews: list[dict[str, Any]] = field(default_factory=list)
    score: float | None = None
    error: str | None = None


class Orchestrator:
    def __init__(
        self, db: Database, gateway: ModelGateway, runner: Runner, settings: Settings
    ) -> None:
        self.db = db
        self.gateway = gateway
        self.runner = runner
        self.settings = settings
        self._runs: dict[str, int] = {}  # task id -> run number this process is executing

    # ------------------------------------------------------------------ entry point

    async def run(self, task_id: str) -> None:
        try:
            task = await self._start(task_id)
        except Exception:
            log.exception("Could not start task %s", task_id)
            await self._fail(task_id, "Internal error while starting the task", None)
            return
        if task is None:
            return
        current_owner.set(task.owner)  # model usage is metered to the task's owner
        try:
            await self._execute(task)
        except TaskCancelledError:
            await record_event(
                self.db, task_id, "run_stopped", f"Run {task.run_number} stopped (cancelled)"
            )
        except PipelineError as exc:
            await self._fail(task_id, str(exc), task.run_number)
        except Exception as exc:
            log.exception("Pipeline crashed for task %s", task_id)
            await self._fail(
                task_id, f"Internal error: {type(exc).__name__}: {exc}", task.run_number
            )
        finally:
            if self._runs.get(task_id) == task.run_number:
                del self._runs[task_id]

    async def _execute(self, task: TaskSnapshot) -> None:
        plan = await self._plan(task)
        size = task.requested_size if task.requested_size != TaskSize.AUTO else plan.complexity
        if task.models:
            # The user chose who writes the code; the server still plans, reviews and judges.
            team = self.gateway.available(task.models)
            if not team:
                raise PipelineError(
                    "The models chosen for this task are not available on the server right now. "
                    "Retry later or choose other models."
                )
        else:
            team = self.gateway.available(self.settings.generator_models)[: TEAM_SIZE[size]]
        if not team:
            raise PipelineError(
                "No generator models are available. Configure a provider key (for example "
                "OLLAMA_API_KEY) and AITSEV_GENERATOR_MODELS."
            )
        await self._set_status(
            task.id,
            TaskStatus.GENERATING,
            "generation_started",
            f"Generating with {len(team)} model(s)",
            {"size": size, "models": team},
            effective_size=size,
        )
        attempts = await _gather_all(
            [self._generate(task, plan, model, LABELS[i]) for i, model in enumerate(team)]
        )
        await self._check_cancelled(task.id)

        passing = [a for a in attempts if a.status == CandidateStatus.PASSED] or [
            a for a in attempts if a.status == CandidateStatus.UNVERIFIED
        ]
        if not passing:
            details = "; ".join(f"{a.label} ({a.model}): {a.status}" for a in attempts)
            raise PipelineError(f"No candidate passed verification. {details}")

        if len(attempts) > 1:
            await self._set_status(
                task.id, TaskStatus.REVIEWING, "review_started", "Cross-reviewing candidates"
            )
            await _gather_all([self._review(task, plan, a, team) for a in passing])
            await self._check_cancelled(task.id)

        await self._set_status(task.id, TaskStatus.JUDGING, "judging_started", "Choosing a winner")
        winner, decision = await self._judge(task, plan, passing)
        await self._finish_success(task.id, winner, decision)

    # ------------------------------------------------------------------ stages

    async def _start(self, task_id: str) -> TaskSnapshot | None:
        async with self.db.session() as session:
            task = await session.get(Task, task_id)
            if task is None:
                log.warning("Task %s not found", task_id)
                return None
            status = TaskStatus(task.status)
            if status.is_terminal or status == TaskStatus.AWAITING_APPROVAL:
                return None
            task.status = TaskStatus.PLANNING.value
            task.error = None
            self._runs[task.id] = task.run_number
            add_event(
                session,
                task.id,
                "run_started",
                f"Run {task.run_number} started",
                {"run_number": task.run_number},
            )
            add_event(session, task.id, "planning_started", "Planning the task")
            snapshot = TaskSnapshot(
                id=task.id,
                owner=task.owner,
                instructions=task.instructions,
                feedback=task.feedback,
                files=dict(task.files or {}),
                context_paths=list(task.context_paths or []),
                models=list(task.models or []),
                setup_commands=list(task.setup_commands or []),
                verify_commands=list(task.verify_commands or []),
                sandbox_image=task.sandbox_image,
                requested_size=task.requested_size,
                run_number=task.run_number,
            )
            await session.commit()
            return snapshot

    async def _plan(self, task: TaskSnapshot) -> Plan:
        messages = prompts.plan_messages(
            task.instructions,
            task.feedback,
            task.context_files,
            task.other_paths,
            task.setup_commands,
            task.verify_commands,
        )
        refs = [self.settings.planner_model, *self.settings.fallback_models]
        plan, model = await self._structured(refs, messages, Plan, stage="plan", task_id=task.id)
        async with self.db.session() as session:
            row = await session.get(Task, task.id)
            assert row is not None  # noqa: S101
            if self._stopped(row):
                raise TaskCancelledError
            row.plan = plan.model_dump()
            add_event(
                session,
                task.id,
                "plan_ready",
                plan.summary[:300],
                {"model": model, "complexity": plan.complexity, "steps": len(plan.steps)},
            )
            await session.commit()
        return plan

    async def _generate(self, task: TaskSnapshot, plan: Plan, model: str, label: str) -> Attempt:
        attempt = Attempt(id="", label=label, model=model)
        async with self.db.session() as session:
            row = Candidate(task_id=task.id, run_number=task.run_number, label=label, model=model)
            session.add(row)
            await session.commit()
            attempt.id = row.id

        messages: list[ChatMessage] = prompts.generation_messages(
            task.instructions,
            task.feedback,
            plan,
            task.context_files,
            task.other_paths,
            task.setup_commands,
            task.verify_commands,
        )
        max_attempts = 1 + max(0, self.settings.max_repair_attempts)
        format_retry_left = 1
        try:
            while True:
                await self._check_cancelled(task.id)
                attempt.attempts += 1
                stage = "generate" if attempt.attempts == 1 else "repair"
                result = await self.gateway.chat(
                    model,
                    ChatRequest(
                        messages, temperature=0.2, max_tokens=self.settings.generation_max_tokens
                    ),
                    stage=stage,
                    task_id=task.id,
                )
                try:
                    attempt.summary, attempt.changes = self._parse_generation(
                        result.content, task.files
                    )
                except (OutputFormatError, UnsafePathError) as exc:
                    if format_retry_left > 0:
                        format_retry_left -= 1
                        attempt.attempts -= 1
                        messages = [
                            *messages,
                            ChatMessage("assistant", result.content),
                            prompts.format_error_message(str(exc)),
                        ]
                        continue
                    raise PipelineError(f"Unusable output: {exc}") from exc

                attempt.verification = await self._verify(task, attempt.changes)
                await record_event(
                    self.db,
                    task.id,
                    "candidate_verified",
                    f"{label} attempt {attempt.attempts}: "
                    + _verdict_text(attempt.verification.passed),
                    {
                        "candidate_id": attempt.id,
                        "label": label,
                        "model": model,
                        "attempt": attempt.attempts,
                        "passed": attempt.verification.passed,
                        "builtin_issues": attempt.verification.builtin_issues,
                    },
                )
                if attempt.verification.passed is True:
                    attempt.status = CandidateStatus.PASSED
                    break
                if attempt.verification.passed is None:
                    attempt.status = CandidateStatus.UNVERIFIED
                    break
                if attempt.attempts >= max_attempts:
                    attempt.status = CandidateStatus.FAILED
                    break
                messages = [
                    *messages,
                    ChatMessage("assistant", result.content),
                    prompts.repair_message(attempt.verification.failure_report()),
                ]
        except TaskCancelledError:
            attempt.status = CandidateStatus.ERROR
            attempt.error = "Cancelled"
            await self._save_attempt(attempt)
            raise
        except Exception as exc:
            expected = isinstance(exc, ModelError | PipelineError)
            if not expected:
                log.exception("Unexpected error generating candidate %s", label)
            attempt.status = CandidateStatus.ERROR
            attempt.error = (str(exc) if expected else f"{type(exc).__name__}: {exc}")[:2000]
            await record_event(
                self.db,
                task.id,
                "candidate_error",
                f"{label} ({model}) failed: {attempt.error[:200]}",
                {"candidate_id": attempt.id, "label": label, "model": model},
            )
        await self._save_attempt(attempt)
        return attempt

    def _parse_generation(self, text: str, original: dict[str, str]) -> tuple[str, ChangeSet]:
        blocks = parse_file_blocks(text)
        changed = normalize_files(blocks.files)
        deleted = sorted({normalize_path(p) for p in blocks.deleted})
        if len(changed) + len(deleted) > self.settings.max_output_files:
            raise OutputFormatError(
                f"Too many files changed ({len(changed) + len(deleted)}); "
                f"limit is {self.settings.max_output_files}"
            )
        for path, content in changed.items():
            if len(content.encode("utf-8")) > self.settings.max_file_bytes:
                raise OutputFormatError(f"{path} exceeds {self.settings.max_file_bytes} bytes")
        changes = ChangeSet(
            changed={p: c for p, c in changed.items() if original.get(p) != c},
            deleted=deleted,
        )
        if changes.is_empty:
            raise OutputFormatError("The answer did not change any file")
        return blocks.summary or "(no summary given)", changes

    async def _verify(self, task: TaskSnapshot, changes: ChangeSet) -> VerificationResult:
        issues = builtin_checks(changes, task.files)
        if issues:
            return VerificationResult(passed=False, builtin_issues=issues)
        return await self.runner.verify(
            apply_changes(task.files, changes),
            setup_commands=task.setup_commands,
            verify_commands=task.verify_commands,
            timeout=self.settings.sandbox_timeout_seconds,
            image=task.sandbox_image,
        )

    async def _review(
        self, task: TaskSnapshot, plan: Plan, attempt: Attempt, team: Sequence[str]
    ) -> None:
        pool = self.gateway.available([*self.settings.reviewer_models, *team])
        others = [m for m in pool if m != attempt.model]
        refs = others or pool
        rendered = self._render(attempt, task.files)
        messages = prompts.review_messages(
            task.instructions,
            plan,
            rendered,
            attempt.verification.to_dict() if attempt.verification else {},
        )
        try:
            review, reviewer = await self._structured(
                refs, messages, Review, stage="review", task_id=task.id
            )
        except PipelineError as exc:
            attempt.reviews.append({"error": str(exc)})
            await record_event(
                self.db, task.id, "review_failed", f"Review of {attempt.label} failed: {exc}"
            )
        else:
            attempt.reviews.append(
                {"reviewer": reviewer, "self_review": not others, **review.model_dump()}
            )
            scores = [float(r["score"]) for r in attempt.reviews if "score" in r]
            attempt.score = statistics.fmean(scores) if scores else None
            await record_event(
                self.db,
                task.id,
                "review_done",
                f"{attempt.label} scored {review.score:g}/10 ({review.verdict})",
                {
                    "candidate_id": attempt.id,
                    "label": attempt.label,
                    "reviewer": reviewer,
                    "score": review.score,
                    "verdict": review.verdict,
                    "issues": review.issues[:10],
                },
            )
        await self._save_attempt(attempt)

    async def _judge(
        self, task: TaskSnapshot, plan: Plan, passing: Sequence[Attempt]
    ) -> tuple[Attempt, dict[str, Any]]:
        if len(passing) == 1:
            only = passing[0]
            return only, {
                "method": "single_passing_candidate",
                "winner_label": only.label,
                "rationale": "Only one candidate passed verification.",
            }
        by_label = {a.label: a for a in passing}
        evidence = {a.label: self._evidence(a, task.files) for a in passing}
        messages = prompts.judge_messages(
            task.instructions,
            plan,
            [self._render(a, task.files) for a in passing],
            evidence,
        )
        refs = [self.settings.judge_model, *self.settings.fallback_models]
        try:
            decision, judge = await self._structured(
                refs, messages, JudgeDecision, stage="judge", task_id=task.id
            )
        except PipelineError as exc:
            log.warning("Judge failed for %s: %s", task.id, exc)
            decision, judge = None, None
        if decision is not None and decision.winner in by_label:
            winner = by_label[decision.winner]
            return winner, {
                "method": "judge_model",
                "judge": judge,
                "winner_label": winner.label,
                "rationale": decision.rationale,
                "evidence": evidence,
            }
        winner = sorted(
            passing,
            key=lambda a: (
                0 if a.status == CandidateStatus.PASSED else 1,
                -(a.score if a.score is not None else -1.0),
                changed_line_count(task.files, a.changes),
                a.label,
            ),
        )[0]
        reason = (
            f"Judge picked unknown candidate {decision.winner!r}"
            if decision is not None
            else "Judge model gave no valid answer"
        )
        return winner, {
            "method": "deterministic_fallback",
            "judge": judge,
            "winner_label": winner.label,
            "rationale": f"{reason}; chose by verification, review score, then smallest change.",
            "evidence": evidence,
        }

    # ------------------------------------------------------------------ helpers

    async def _structured(
        self,
        refs: Sequence[str],
        messages: Sequence[ChatMessage],
        schema: type[T],
        *,
        stage: str,
        task_id: str,
    ) -> tuple[T, str]:
        """Ask for JSON matching ``schema``; one self-correction per model, then the next model."""
        errors: list[str] = []
        for ref in self.gateway.available(refs):
            convo = list(messages)
            for _ in range(2):
                await self._check_cancelled(task_id)
                try:
                    result = await self.gateway.chat(
                        ref,
                        ChatRequest(
                            convo,
                            temperature=0.1,
                            max_tokens=self.settings.structured_max_tokens,
                            json_mode=True,
                        ),
                        stage=stage,
                        task_id=task_id,
                    )
                except ModelError as exc:
                    errors.append(f"{ref}: {exc}")
                    break
                try:
                    return schema.model_validate(extract_json_object(result.content)), ref
                except (OutputFormatError, ValidationError) as exc:
                    detail = str(exc)[:800]
                    errors.append(f"{ref}: {detail}")
                    convo = [
                        *convo,
                        ChatMessage("assistant", result.content),
                        prompts.json_fix_message(detail),
                    ]
        if not errors:
            raise PipelineError(
                f"No model is available for the {stage} stage. Check the provider keys "
                "(for example OLLAMA_API_KEY) and the AITSEV_*_MODEL settings."
            )
        raise PipelineError(f"No model returned a valid {stage} answer: " + " | ".join(errors[-3:]))

    def _render(self, attempt: Attempt, original: dict[str, str]) -> str:
        diffs = [(d.path, d.diff) for d in diff_changes(original, attempt.changes)]
        text = prompts.render_candidate(attempt.label, attempt.summary, diffs)
        if len(text) > MAX_CANDIDATE_RENDER:
            text = text[:MAX_CANDIDATE_RENDER] + "\n...(candidate truncated)...\n</candidate>"
        return text

    def _evidence(self, attempt: Attempt, original: dict[str, str]) -> dict[str, Any]:
        verification = attempt.verification
        return {
            "verification": attempt.status.value,
            "verify_commands_passed": sum(1 for c in verification.commands if c.ok)
            if verification
            else 0,
            "repair_attempts": attempt.attempts - 1,
            "review_score": attempt.score,
            "review_verdicts": [r.get("verdict") for r in attempt.reviews if "verdict" in r],
            "review_issues": [i for r in attempt.reviews for i in r.get("issues", [])][:10],
            "changed_lines": changed_line_count(original, attempt.changes),
            "files_changed": len(attempt.changes.changed) + len(attempt.changes.deleted),
        }

    async def _save_attempt(self, attempt: Attempt) -> None:
        async with self.db.session() as session:
            row = await session.get(Candidate, attempt.id)
            if row is None:
                return
            row.status = attempt.status.value
            row.attempts = attempt.attempts
            row.summary = attempt.summary
            row.changed_files = dict(attempt.changes.changed)
            row.deleted_files = list(attempt.changes.deleted)
            row.verification = attempt.verification.to_dict() if attempt.verification else None
            row.reviews = list(attempt.reviews)
            row.score = attempt.score
            row.error = attempt.error
            await session.commit()

    def _stopped(self, task: Task | None) -> bool:
        """True when the task was cancelled or a newer run has replaced this one."""
        if task is None or task.status == TaskStatus.CANCELLED:
            return True
        expected = self._runs.get(task.id)
        return expected is not None and task.run_number != expected

    async def _check_cancelled(self, task_id: str) -> None:
        async with self.db.session() as session:
            task = await session.get(Task, task_id)
            if self._stopped(task):
                raise TaskCancelledError

    async def _set_status(
        self,
        task_id: str,
        status: TaskStatus,
        event: str,
        message: str,
        data: dict[str, Any] | None = None,
        *,
        effective_size: str | None = None,
    ) -> None:
        async with self.db.session() as session:
            task = await session.get(Task, task_id)
            if task is None or self._stopped(task):
                raise TaskCancelledError
            task.status = status.value
            if effective_size is not None:
                task.effective_size = effective_size
            add_event(session, task_id, event, message, data)
            await session.commit()

    async def _finish_success(
        self, task_id: str, winner: Attempt, decision: dict[str, Any]
    ) -> None:
        async with self.db.session() as session:
            task = await session.get(Task, task_id)
            if task is None or self._stopped(task):
                raise TaskCancelledError
            task.status = TaskStatus.AWAITING_APPROVAL.value
            task.winner_candidate_id = winner.id
            task.decision = decision
            add_event(
                session,
                task_id,
                "awaiting_approval",
                f"Candidate {winner.label} ({winner.model}) selected; waiting for approval",
                {"candidate_id": winner.id, "method": decision["method"]},
            )
            await session.commit()

    async def _fail(self, task_id: str, message: str, run_number: int | None) -> None:
        async with self.db.session() as session:
            task = await session.get(Task, task_id)
            if task is None or TaskStatus(task.status).is_terminal:
                return
            if run_number is not None and task.run_number != run_number:
                return  # a newer run owns the task now
            task.status = TaskStatus.FAILED.value
            task.error = message[:4000]
            task.finished_at = datetime.now(UTC)
            add_event(session, task_id, "failed", message[:500])
            await session.commit()


R = TypeVar("R")


async def _gather_all(coros: Sequence[Coroutine[Any, Any, R]]) -> list[R]:
    """Run coroutines concurrently, wait for all of them, then re-raise the first error."""
    results = await asyncio.gather(*coros, return_exceptions=True)
    for item in results:
        if isinstance(item, BaseException):
            raise item
    return [r for r in results if not isinstance(r, BaseException)]


def _verdict_text(passed: bool | None) -> str:
    if passed is None:
        return "no verify commands; built-in checks passed"
    return "passed" if passed else "failed"
