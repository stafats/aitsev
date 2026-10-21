"""Provider adapters.

Each adapter turns a :class:`ChatRequest` into one HTTP call and maps the answer back to a
:class:`ChatResult`. Retries and fallback live in the gateway, not here.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any

import httpx

from aitsev.gateway.errors import ModelRequestError, ModelResponseError
from aitsev.gateway.types import ChatRequest, ChatResult

RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})


class Provider(ABC):
    """One model provider (an API endpoint plus credentials)."""

    name: str

    def __init__(self, name: str, client: httpx.AsyncClient) -> None:
        self.name = name
        self.client = client

    @abstractmethod
    async def chat(self, model: str, request: ChatRequest) -> ChatResult: ...

    async def list_models(self) -> list[str]:
        return []

    async def _post(self, url: str, payload: dict[str, Any], headers: dict[str, str]) -> Any:
        try:
            response = await self.client.post(url, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise ModelRequestError(f"{self.name}: request timed out", retryable=True) from exc
        except httpx.TransportError as exc:
            raise ModelRequestError(f"{self.name}: network error: {exc}", retryable=True) from exc
        return self._decode(response)

    async def _get(self, url: str, headers: dict[str, str]) -> Any:
        try:
            response = await self.client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            raise ModelRequestError(f"{self.name}: network error: {exc}", retryable=True) from exc
        return self._decode(response)

    def _decode(self, response: httpx.Response) -> Any:
        if response.status_code >= 400:
            detail = response.text[:500]
            raise ModelRequestError(
                f"{self.name}: HTTP {response.status_code}: {detail}",
                status_code=response.status_code,
                retryable=response.status_code in RETRYABLE_STATUS,
            )
        try:
            return response.json()
        except ValueError as exc:
            raise ModelResponseError(f"{self.name}: response was not JSON") from exc


class OllamaProvider(Provider):
    """Ollama's native ``/api/chat``. Works for Ollama Cloud (https://ollama.com) and local."""

    def __init__(
        self, name: str, client: httpx.AsyncClient, base_url: str, api_key: str | None = None
    ) -> None:
        super().__init__(name, client)
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    async def chat(self, model: str, request: ChatRequest) -> ChatResult:
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
            "stream": False,
            "options": {"temperature": request.temperature, "num_predict": request.max_tokens},
        }
        if request.json_mode:
            payload["format"] = "json"
        started = time.monotonic()
        data = await self._post(f"{self.base_url}/api/chat", payload, self._headers())
        message = data.get("message") if isinstance(data, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            raise ModelResponseError(f"{self.name}: response had no message content")
        return ChatResult(
            model=model,
            content=content,
            prompt_tokens=int(data.get("prompt_eval_count") or 0),
            completion_tokens=int(data.get("eval_count") or 0),
            latency_ms=int((time.monotonic() - started) * 1000),
            raw=data,
        )

    async def list_models(self) -> list[str]:
        data = await self._get(f"{self.base_url}/api/tags", self._headers())
        models = data.get("models", []) if isinstance(data, dict) else []
        return sorted(str(m.get("name") or m.get("model")) for m in models if isinstance(m, dict))


class OpenAICompatibleProvider(Provider):
    """OpenAI ``/chat/completions`` and any server that implements the same API."""

    def __init__(self, name: str, client: httpx.AsyncClient, base_url: str, api_key: str) -> None:
        super().__init__(name, client)
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    async def chat(self, model: str, request: ChatRequest) -> ChatResult:
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
            "temperature": request.temperature,
            "max_completion_tokens": request.max_tokens,
        }
        if request.json_mode:
            payload["response_format"] = {"type": "json_object"}
        started = time.monotonic()
        data = await self._post(f"{self.base_url}/chat/completions", payload, self._headers())
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelResponseError(f"{self.name}: response had no choices") from exc
        if not isinstance(content, str):
            raise ModelResponseError(f"{self.name}: response content was empty")
        usage = data.get("usage") or {}
        return ChatResult(
            model=model,
            content=content,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            latency_ms=int((time.monotonic() - started) * 1000),
            raw=data,
        )

    async def list_models(self) -> list[str]:
        data = await self._get(f"{self.base_url}/models", self._headers())
        items = data.get("data", []) if isinstance(data, dict) else []
        return sorted(str(m.get("id")) for m in items if isinstance(m, dict))


class AnthropicProvider(Provider):
    """Anthropic Messages API."""

    API_VERSION = "2023-06-01"

    def __init__(self, name: str, client: httpx.AsyncClient, base_url: str, api_key: str) -> None:
        super().__init__(name, client)
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self.api_key, "anthropic-version": self.API_VERSION}

    async def chat(self, model: str, request: ChatRequest) -> ChatResult:
        system = "\n\n".join(m.content for m in request.messages if m.role == "system")
        messages = [
            {"role": m.role, "content": m.content} for m in request.messages if m.role != "system"
        ]
        if request.json_mode:
            note = "Respond with a single JSON object only."
            system = f"{system}\n\n{note}" if system else note
        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "messages": messages,
        }
        if system:
            payload["system"] = system
        started = time.monotonic()
        data = await self._post(f"{self.base_url}/v1/messages", payload, self._headers())
        blocks = data.get("content") if isinstance(data, dict) else None
        if not isinstance(blocks, list):
            raise ModelResponseError(f"{self.name}: response had no content")
        text = "".join(
            b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"
        )
        if not text:
            raise ModelResponseError(f"{self.name}: response had no text")
        usage = data.get("usage") or {}
        return ChatResult(
            model=model,
            content=text,
            prompt_tokens=int(usage.get("input_tokens") or 0),
            completion_tokens=int(usage.get("output_tokens") or 0),
            latency_ms=int((time.monotonic() - started) * 1000),
            raw=data,
        )

    async def list_models(self) -> list[str]:
        data = await self._get(f"{self.base_url}/v1/models", self._headers())
        items = data.get("data", []) if isinstance(data, dict) else []
        return sorted(str(m.get("id")) for m in items if isinstance(m, dict))
