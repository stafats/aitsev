"""Provider-neutral request and response types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Role = Literal["system", "user", "assistant"]


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: Role
    content: str


@dataclass(frozen=True, slots=True)
class ModelRef:
    """A model reference written as ``provider/model``, e.g. ``ollama-cloud/kimi-k2.6``."""

    provider: str
    model: str

    @classmethod
    def parse(cls, ref: str) -> ModelRef:
        provider, sep, model = ref.strip().partition("/")
        if not sep or not provider or not model:
            raise ValueError(f"Model reference must be 'provider/model', got {ref!r}")
        return cls(provider=provider, model=model)

    def __str__(self) -> str:
        return f"{self.provider}/{self.model}"


@dataclass(frozen=True, slots=True)
class ChatRequest:
    messages: list[ChatMessage]
    temperature: float = 0.2
    max_tokens: int = 8192
    json_mode: bool = False


@dataclass(slots=True)
class ChatResult:
    model: str
    content: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    raw: dict[str, object] = field(default_factory=dict, repr=False)
