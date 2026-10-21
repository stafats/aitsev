"""The model gateway: provider registry, retries, fallback and usage recording."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

import httpx

from aitsev.config import Settings
from aitsev.gateway.errors import ModelError, ModelNotConfiguredError
from aitsev.gateway.providers import (
    AnthropicProvider,
    OllamaProvider,
    OpenAICompatibleProvider,
    Provider,
)
from aitsev.gateway.types import ChatRequest, ChatResult, ModelRef

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CallRecord:
    task_id: str | None
    stage: str
    model: str
    ok: bool
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int
    error: str | None


class UsageRecorder(Protocol):
    async def __call__(self, record: CallRecord) -> None: ...


Sleep = Callable[[float], Awaitable[None]]


class ModelGateway:
    """Routes ``provider/model`` references to providers, with retries and fallback."""

    def __init__(
        self,
        providers: dict[str, Provider],
        *,
        max_retries: int = 3,
        retry_base_delay: float = 1.0,
        recorder: UsageRecorder | None = None,
        sleep: Sleep = asyncio.sleep,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.providers = providers
        self.max_retries = max(0, max_retries)
        self.retry_base_delay = retry_base_delay
        self.recorder = recorder
        self._sleep = sleep
        self._client = client

    @classmethod
    def from_settings(
        cls, settings: Settings, recorder: UsageRecorder | None = None
    ) -> ModelGateway:
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.model_timeout_seconds, connect=15.0)
        )
        providers: dict[str, Provider] = {}
        if settings.ollama_api_key:
            providers["ollama-cloud"] = OllamaProvider(
                "ollama-cloud",
                client,
                settings.ollama_cloud_base_url,
                settings.ollama_api_key.get_secret_value(),
            )
        if settings.ollama_local_base_url:
            providers["ollama-local"] = OllamaProvider(
                "ollama-local", client, settings.ollama_local_base_url
            )
        if settings.openai_api_key:
            providers["openai"] = OpenAICompatibleProvider(
                "openai",
                client,
                settings.openai_base_url,
                settings.openai_api_key.get_secret_value(),
            )
        if settings.anthropic_api_key:
            providers["anthropic"] = AnthropicProvider(
                "anthropic",
                client,
                settings.anthropic_base_url,
                settings.anthropic_api_key.get_secret_value(),
            )
        return cls(
            providers,
            max_retries=settings.model_max_retries,
            retry_base_delay=settings.model_retry_base_delay,
            recorder=recorder,
            client=client,
        )

    def is_available(self, ref: str) -> bool:
        try:
            return ModelRef.parse(ref).provider in self.providers
        except ValueError:
            return False

    def available(self, refs: Sequence[str]) -> list[str]:
        """Keep only references whose provider is configured, preserving order, no duplicates."""
        seen: set[str] = set()
        result: list[str] = []
        for ref in refs:
            if ref not in seen and self.is_available(ref):
                seen.add(ref)
                result.append(ref)
        return result

    async def chat(
        self, ref: str, request: ChatRequest, *, stage: str, task_id: str | None = None
    ) -> ChatResult:
        """Call one model, retrying transient failures with exponential backoff."""
        model_ref = ModelRef.parse(ref)
        provider = self.providers.get(model_ref.provider)
        if provider is None:
            raise ModelNotConfiguredError(f"Provider {model_ref.provider!r} is not configured")
        attempt = 0
        while True:
            try:
                result = await provider.chat(model_ref.model, request)
            except ModelError as exc:
                await self._record(task_id, stage, ref, None, error=str(exc))
                if not exc.retryable or attempt >= self.max_retries:
                    raise
                delay = self.retry_base_delay * (2**attempt) * (1 + random.random() * 0.25)  # noqa: S311
                log.warning("Retrying %s after error (%s), attempt %d", ref, exc, attempt + 1)
                attempt += 1
                await self._sleep(delay)
                continue
            result.model = ref
            await self._record(task_id, stage, ref, result)
            return result

    async def chat_with_fallback(
        self,
        refs: Sequence[str],
        request: ChatRequest,
        *,
        stage: str,
        task_id: str | None = None,
    ) -> ChatResult:
        """Try each model in order until one answers."""
        candidates = self.available(refs)
        if not candidates:
            raise ModelNotConfiguredError(f"None of these models are configured: {list(refs)}")
        last_error: ModelError | None = None
        for ref in candidates:
            try:
                return await self.chat(ref, request, stage=stage, task_id=task_id)
            except ModelError as exc:
                log.warning("Model %s failed for %s, trying next: %s", ref, stage, exc)
                last_error = exc
        assert last_error is not None  # noqa: S101 - loop ran at least once
        raise last_error

    async def list_models(self) -> dict[str, list[str] | str]:
        """Models each configured provider reports; an error string when a provider fails."""
        result: dict[str, list[str] | str] = {}
        for name, provider in self.providers.items():
            try:
                result[name] = await provider.list_models()
            except ModelError as exc:
                result[name] = f"error: {exc}"
        return result

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def _record(
        self,
        task_id: str | None,
        stage: str,
        ref: str,
        result: ChatResult | None,
        *,
        error: str | None = None,
    ) -> None:
        if self.recorder is None:
            return
        record = CallRecord(
            task_id=task_id,
            stage=stage,
            model=ref,
            ok=result is not None,
            prompt_tokens=result.prompt_tokens if result else 0,
            completion_tokens=result.completion_tokens if result else 0,
            latency_ms=result.latency_ms if result else 0,
            error=error,
        )
        try:
            await self.recorder(record)
        except Exception:  # recording must never break a model call
            log.exception("Failed to record model usage")
