"""Interactive coding assistant: chat about the code, edit it, and analyse it in the background.

* ``ask``   — answers questions about the project; never edits.
* ``agent`` — answers and returns file edits (full new contents). When test commands are given,
  the edits are verified in the sandbox first; one automatic repair round runs if they fail.
* ``analyze`` — reviews files and returns line-level suggestions for the editor.

The server owns the models. ``model="auto"`` routes to the configured planner, then the
generators, then the fallbacks; a chosen catalog model is used on its own.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from aitsev.config import Settings
from aitsev.gateway import ModelGateway
from aitsev.gateway.types import ChatMessage, ChatRequest
from aitsev.pipeline.parsing import OutputFormatError, extract_json_object, parse_file_blocks
from aitsev.pipeline.prompts import GENERATION_FORMAT, render_files
from aitsev.pipeline.sandbox import Runner, VerificationResult
from aitsev.pipeline.workspace import UnsafePathError, normalize_path

Mode = Literal["ask", "agent"]
Severity = Literal["info", "warning", "error"]
MAX_SUGGESTIONS = 30
_BLOCKS = re.compile(r"<<<FILE .*?>>>END FILE\s*|<<<DELETE .*?>>>\s*", re.DOTALL)

CHAT_SYSTEM = (
    "You are Aitsev, a pair-programming assistant inside the user's code editor. You can see the "
    "project files the user shared. Be precise and practical: explain briefly, point to exact "
    "files and lines, and prefer small, safe, production-quality changes that match the "
    "project's style."
)
ASK_RULES = (
    "Answer the user's question about this project. Do not output file blocks; show short code "
    "snippets in markdown only when they help."
)
AGENT_RULES = (
    "When the user asks for a change, make it. Start with a short explanation in plain text, then "
    "output the edited files.\n"
    + GENERATION_FORMAT.replace("SUMMARY: <", "SUMMARY (optional): <")
    + "\nOnly edit files whose content you were shown, or create new files. If the user only asks "
    "a question, answer without file blocks."
)
ANALYZE_SYSTEM = (
    "You are a meticulous code health analyst. Review the project files for real bugs, security "
    "problems, performance issues, missing error handling and clear maintainability problems. "
    "Ignore style nitpicks. Report only issues you are confident about, each tied to a file and "
    "line."
)
ANALYZE_FORMAT = (
    "Reply with one JSON object only:\n"
    '{"suggestions": [{"path": "relative/file.ext", "line": 12, "severity": "warning", '
    '"title": "short title", "detail": "why it matters and how to fix it"}]}\n'
    'severity is one of "error", "warning", "info". Use [] when there is nothing important.'
)


class ChatError(Exception):
    """A request the assistant cannot serve (bad input or no model)."""


@dataclass(slots=True)
class Edit:
    path: str
    action: Literal["create", "modify", "delete"]
    content: str | None
    diff: str


@dataclass(slots=True)
class ChatOutcome:
    reply: str
    model: str
    edits: list[Edit] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    verification: dict[str, Any] | None = None
    repaired: bool = False
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass(slots=True)
class Suggestion:
    path: str
    line: int
    severity: Severity
    title: str
    detail: str


def route(settings: Settings, model: str) -> list[str]:
    if model != "auto":
        return [model]
    ordered = [settings.planner_model, *settings.generator_models, *settings.fallback_models]
    return list(dict.fromkeys(ordered))


def _conversation(
    system: str,
    rules: str,
    messages: Sequence[tuple[str, str]],
    files: Mapping[str, str],
    other_paths: Sequence[str],
    active_path: str | None,
    selection: str | None,
) -> list[ChatMessage]:
    context = render_files(files, other_paths)
    if active_path:
        context += f"\n<active_file>{active_path}</active_file>"
    if selection:
        context += f"\n<selected_code>\n{selection}\n</selected_code>"
    out = [ChatMessage("system", f"{system}\n\n{rules}"), ChatMessage("user", context)]
    out.append(ChatMessage("assistant", "I have read the project files. What would you like?"))
    out.extend(
        ChatMessage("user" if role == "user" else "assistant", text) for role, text in messages
    )
    return out


def _unified(path: str, old: str, new: str) -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )


def extract_edits(
    text: str, files: Mapping[str, str], other_paths: Sequence[str]
) -> tuple[str, list[Edit], list[str]]:
    """Split a model reply into prose and validated edits. Returns (reply, edits, skipped)."""
    try:
        blocks = parse_file_blocks(text)
    except OutputFormatError as exc:
        if "no <<<FILE blocks" in str(exc):
            return text.strip(), [], []
        raise ChatError(f"The model's edits were malformed: {exc}") from exc
    prose = _BLOCKS.sub("", text)
    prose = re.sub(r"(?im)^\s*SUMMARY[^:]*:\s*", "", prose).strip() or blocks.summary
    hidden = set(other_paths)
    edits: list[Edit] = []
    skipped: list[str] = []
    for raw, content in blocks.files.items():
        try:
            path = normalize_path(raw)
        except UnsafePathError:
            skipped.append(f"{raw} (unsafe path)")
            continue
        if path in files:
            if files[path] != content:
                edits.append(Edit(path, "modify", content, _unified(path, files[path], content)))
        elif path in hidden:
            skipped.append(f"{path} (its content was not shared, so it was not overwritten)")
        else:
            edits.append(Edit(path, "create", content, _unified(path, "", content)))
    for raw in blocks.deleted:
        try:
            path = normalize_path(raw)
        except UnsafePathError:
            skipped.append(f"{raw} (unsafe path)")
            continue
        if path in files:
            edits.append(Edit(path, "delete", None, _unified(path, files[path], "")))
        else:
            skipped.append(f"{path} (cannot delete a file that was not shared)")
    return prose, edits, skipped


def _apply(files: Mapping[str, str], edits: Sequence[Edit]) -> dict[str, str]:
    result = dict(files)
    for edit in edits:
        if edit.action == "delete":
            result.pop(edit.path, None)
        elif edit.content is not None:
            result[edit.path] = edit.content
    return result


def _failure_report(result: VerificationResult) -> str:
    lines = []
    for command in result.commands:
        if not command.ok:
            lines.append(
                f"$ {command.command}\n(exit {command.exit_code})\n{command.output[-4000:]}"
            )
    return "\n\n".join(lines) or "The checks failed."


class Assistant:
    def __init__(self, gateway: ModelGateway, settings: Settings, runner: Runner) -> None:
        self.gateway = gateway
        self.settings = settings
        self.runner = runner

    async def _ask_models(
        self,
        refs: Sequence[str],
        conversation: list[ChatMessage],
        *,
        stage: str,
        json_mode: bool = False,
    ) -> tuple[str, str, int, int]:
        request = ChatRequest(
            messages=conversation,
            temperature=0.2,
            max_tokens=self.settings.generation_max_tokens,
            json_mode=json_mode,
        )
        available = self.gateway.available(refs)
        if not available:
            raise ChatError(
                "No model is available right now. Try again later or pick another model."
            )
        if len(available) == 1:
            result = await self.gateway.chat(available[0], request, stage=stage)
        else:
            result = await self.gateway.chat_with_fallback(available, request, stage=stage)
        return result.content, result.model, result.prompt_tokens, result.completion_tokens

    async def chat(
        self,
        *,
        mode: Mode,
        model: str,
        messages: Sequence[tuple[str, str]],
        files: Mapping[str, str],
        context_paths: Sequence[str],
        active_path: str | None = None,
        selection: str | None = None,
        setup_commands: Sequence[str] = (),
        verify_commands: Sequence[str] = (),
        sandbox_image: str | None = None,
    ) -> ChatOutcome:
        context = (
            {p: files[p] for p in context_paths if p in files} if context_paths else dict(files)
        )
        others = sorted(p for p in files if p not in context)
        rules = AGENT_RULES if mode == "agent" else ASK_RULES
        conversation = _conversation(
            CHAT_SYSTEM, rules, messages, context, others, active_path, selection
        )
        refs = route(self.settings, model)
        text, used, p_tokens, c_tokens = await self._ask_models(refs, conversation, stage="chat")
        if mode == "ask":
            return ChatOutcome(
                _BLOCKS.sub("", text).strip(),
                used,
                prompt_tokens=p_tokens,
                completion_tokens=c_tokens,
            )
        reply, edits, skipped = extract_edits(text, context, others)
        outcome = ChatOutcome(
            reply, used, edits, skipped, prompt_tokens=p_tokens, completion_tokens=c_tokens
        )
        if not edits or not verify_commands:
            return outcome

        verification = await self._verify(
            files, edits, setup_commands, verify_commands, sandbox_image
        )
        if verification.passed is False:
            # One repair round with the failing output, then verify again.
            repair = [
                *conversation,
                ChatMessage("assistant", text),
                ChatMessage(
                    "user",
                    "Your changes failed verification. Fix them so every command passes. Output "
                    "the corrected files in the same format.\n<failures>\n"
                    f"{_failure_report(verification)}\n</failures>",
                ),
            ]
            fixed_text, used, p2, c2 = await self._ask_models([used], repair, stage="chat_repair")
            outcome.prompt_tokens += p2
            outcome.completion_tokens += c2
            fixed_reply, fixed_edits, fixed_skipped = extract_edits(fixed_text, context, others)
            if fixed_edits:
                second = await self._verify(
                    files, fixed_edits, setup_commands, verify_commands, sandbox_image
                )
                outcome.edits, outcome.skipped, outcome.repaired = fixed_edits, fixed_skipped, True
                outcome.reply = f"{reply}\n\n{fixed_reply}".strip()
                verification = second
        outcome.verification = verification.to_dict()
        return outcome

    async def _verify(
        self,
        files: Mapping[str, str],
        edits: Sequence[Edit],
        setup: Sequence[str],
        verify: Sequence[str],
        image: str | None,
    ) -> VerificationResult:
        return await self.runner.verify(
            _apply(files, edits),
            setup_commands=list(setup),
            verify_commands=list(verify),
            timeout=self.settings.sandbox_timeout_seconds,
            image=image or self.settings.sandbox_image,
        )

    async def analyze(
        self, *, model: str, files: Mapping[str, str], focus: str | None, limit: int
    ) -> tuple[list[Suggestion], str]:
        if not files:
            return [], ""
        user = render_files(files)
        if focus:
            user += f"\n<focus>{focus}</focus>"
        user += f"\n\n{ANALYZE_FORMAT}\nReport at most {limit} suggestions."
        conversation = [ChatMessage("system", ANALYZE_SYSTEM), ChatMessage("user", user)]
        text, used, _p, _c = await self._ask_models(
            route(self.settings, model), conversation, stage="analyze", json_mode=True
        )
        try:
            data = extract_json_object(text)
        except OutputFormatError:
            return [], used
        raw_items = data.get("suggestions")
        out: list[Suggestion] = []
        for item in raw_items if isinstance(raw_items, list) else []:
            suggestion = _suggestion(item, files)
            if suggestion is not None:
                out.append(suggestion)
            if len(out) >= limit:
                break
        return out, used


def _suggestion(item: object, files: Mapping[str, str]) -> Suggestion | None:
    if not isinstance(item, dict):
        return None
    path = item.get("path")
    title = item.get("title")
    if not isinstance(path, str) or not isinstance(title, str) or not title.strip():
        return None
    try:
        path = normalize_path(path)
    except UnsafePathError:
        return None
    if path not in files:
        return None
    line_count = max(1, files[path].count("\n") + (0 if files[path].endswith("\n") else 1))
    try:
        line = int(item.get("line") or 1)
    except (TypeError, ValueError):
        line = 1
    severity = item.get("severity")
    return Suggestion(
        path=path,
        line=min(max(1, line), line_count),
        severity=severity if severity in ("info", "warning", "error") else "warning",
        title=title.strip()[:200],
        detail=str(item.get("detail") or "").strip()[:2000],
    )
