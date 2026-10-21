"""Test doubles: a scripted model provider that the real ModelGateway drives."""

from __future__ import annotations

import json
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import httpx

from aitsev.gateway import ChatRequest, ChatResult, ModelGateway
from aitsev.gateway.errors import ModelRequestError
from aitsev.gateway.providers import Provider

PY = sys.executable

BUGGY_PROJECT = {
    "calc.py": "def add(a, b):\n    return a - b\n",
    "test_calc.py": "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
}
VERIFY = [f"{PY} -m pytest -q -p no:cacheprovider"]

CORRECT = (
    "SUMMARY: Fixed add to return the sum.\n"
    "<<<FILE calc.py\n"
    "def add(a, b):\n"
    "    return a + b\n"
    ">>>END FILE\n"
)
WRONG = "SUMMARY: Changed add.\n<<<FILE calc.py\ndef add(a, b):\n    return a * b\n>>>END FILE\n"
SYNTAX_ERROR = "SUMMARY: Broken.\n<<<FILE calc.py\ndef add(a, b)\n    return a + b\n>>>END FILE\n"
PLAN = json.dumps(
    {
        "summary": "Fix add so it returns the sum",
        "steps": ["Change the operator in calc.add"],
        "files_to_change": ["calc.py"],
        "acceptance_criteria": ["test_add passes"],
        "risks": [],
        "complexity": "normal",
    }
)


def review(score: float, verdict: str = "approve") -> str:
    return json.dumps({"score": score, "verdict": verdict, "issues": [], "strengths": ["ok"]})


def judge(winner: str) -> str:
    return json.dumps({"winner": winner, "rationale": "Best evidence"})


def stage_of(request: ChatRequest) -> str:
    system = request.messages[0].content
    last = request.messages[-1].content
    if "lead software architect" in system:
        return "plan"
    if "strict senior code reviewer" in system:
        return "review"
    if "final judge" in system:
        return "judge"
    if "senior data integration engineer" in system:
        return "mapping"
    if "solution architect" in system:
        return "crm_plan"
    if "pair-programming assistant" in system:
        return "chat_repair" if "failed verification" in last else "chat"
    if "code health analyst" in system:
        return "analyze"
    if "failed verification" in last:
        return "repair"
    if "could not be parsed" in last:
        return "format_retry"
    return "generate"


Reply = str | Exception | Callable[[ChatRequest], "str | Exception"]
Handler = Callable[[str, ChatRequest], Awaitable[str]]


@dataclass
class Script:
    """Replies per (model, stage). A list is consumed in order; the last item repeats."""

    replies: dict[tuple[str, str], list[Reply]] = field(default_factory=dict)
    calls: list[tuple[str, str, ChatRequest]] = field(default_factory=list)
    hooks: list[Callable[[str, str, ChatRequest], Awaitable[None]]] = field(default_factory=list)

    def on(self, model: str, stage: str, *replies: Reply) -> Script:
        self.replies[(model, stage)] = list(replies)
        return self

    def default(self, stage: str, *replies: Reply) -> Script:
        self.replies[("*", stage)] = list(replies)
        return self

    async def __call__(self, model: str, request: ChatRequest) -> str:
        stage = stage_of(request)
        self.calls.append((model, stage, request))
        for hook in self.hooks:
            await hook(model, stage, request)
        queue = self.replies.get((model, stage)) or self.replies.get(("*", stage))
        if queue is None and stage == "format_retry":
            queue = self.replies.get((model, "generate")) or self.replies.get(("*", "generate"))
        if queue is None:
            raise AssertionError(f"No scripted reply for {model} at stage {stage}")
        reply = queue.pop(0) if len(queue) > 1 else queue[0]
        if callable(reply):
            reply = reply(request)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def stages(self, model: str | None = None) -> list[str]:
        return [s for m, s, _ in self.calls if model is None or m == model]


class ScriptedProvider(Provider):
    def __init__(self, name: str, script: Script, models: list[str] | None = None) -> None:
        super().__init__(name, httpx.AsyncClient())
        self.script = script
        self.models = models or []

    async def chat(self, model: str, request: ChatRequest) -> ChatResult:
        content = await self.script(f"{self.name}/{model}", request)
        return ChatResult(model=model, content=content, prompt_tokens=100, completion_tokens=50)

    async def list_models(self) -> list[str]:
        return list(self.models)


def make_gateway(script: Script, providers: tuple[str, ...] = ("fake",)) -> ModelGateway:
    async def no_sleep(_: float) -> None:
        return None

    return ModelGateway(
        {name: ScriptedProvider(name, script, ["m1", "m2", "m3"]) for name in providers},
        max_retries=2,
        retry_base_delay=0,
        sleep=no_sleep,
    )


def server_error() -> ModelRequestError:
    return ModelRequestError("HTTP 500", status_code=500, retryable=True)
