from __future__ import annotations

import json

import httpx
import pytest
from pydantic import SecretStr

from aitsev.config import Settings
from aitsev.gateway import (
    CallRecord,
    ChatMessage,
    ChatRequest,
    ModelGateway,
    ModelNotConfiguredError,
    ModelRef,
    ModelRequestError,
    ModelResponseError,
)
from aitsev.gateway.providers import AnthropicProvider, OllamaProvider, OpenAICompatibleProvider

REQUEST = ChatRequest(
    [ChatMessage("system", "be precise"), ChatMessage("user", "hi")],
    temperature=0.3,
    max_tokens=123,
)


def client_with(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def no_sleep_gateway(providers, recorder=None, max_retries=2) -> tuple[ModelGateway, list[float]]:
    delays: list[float] = []

    async def sleep(seconds: float) -> None:
        delays.append(seconds)

    return (
        ModelGateway(
            providers, max_retries=max_retries, retry_base_delay=1, sleep=sleep, recorder=recorder
        ),
        delays,
    )


def test_model_ref_parsing():
    assert ModelRef.parse("ollama-cloud/kimi-k2.6") == ModelRef("ollama-cloud", "kimi-k2.6")
    assert str(ModelRef.parse("a/b/c")) == "a/b/c"
    for bad in ("", "noslash", "/model", "provider/"):
        with pytest.raises(ValueError):
            ModelRef.parse(bad)


async def test_ollama_cloud_request_and_response_shape():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "message": {"role": "assistant", "content": "hello"},
                "prompt_eval_count": 12,
                "eval_count": 7,
            },
        )

    provider = OllamaProvider("ollama-cloud", client_with(handler), "https://ollama.com/", "k123")
    result = await provider.chat(
        "gpt-oss:120b",
        ChatRequest(REQUEST.messages, temperature=0.3, max_tokens=123, json_mode=True),
    )
    assert seen["url"] == "https://ollama.com/api/chat"
    assert seen["auth"] == "Bearer k123"
    body = seen["body"]
    assert body["model"] == "gpt-oss:120b"
    assert body["stream"] is False
    assert body["format"] == "json"
    assert body["options"] == {"temperature": 0.3, "num_predict": 123}
    assert body["messages"][0] == {"role": "system", "content": "be precise"}
    assert (result.content, result.prompt_tokens, result.completion_tokens) == ("hello", 12, 7)


async def test_ollama_local_sends_no_auth_and_lists_models():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in request.headers
        assert request.url.path == "/api/tags"
        return httpx.Response(200, json={"models": [{"name": "qwen3"}, {"model": "llama3"}]})

    provider = OllamaProvider("ollama-local", client_with(handler), "http://localhost:11434")
    assert await provider.list_models() == ["llama3", "qwen3"]


async def test_ollama_missing_content_is_response_error():
    provider = OllamaProvider(
        "o", client_with(lambda r: httpx.Response(200, json={"done": True})), "http://x"
    )
    with pytest.raises(ModelResponseError):
        await provider.chat("m", REQUEST)


async def test_non_json_body_is_response_error():
    provider = OllamaProvider(
        "o", client_with(lambda r: httpx.Response(200, text="<html>")), "http://x"
    )
    with pytest.raises(ModelResponseError):
        await provider.chat("m", REQUEST)


async def test_openai_compatible_shape():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "{}"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 4},
            },
        )

    provider = OpenAICompatibleProvider("openai", client_with(handler), "https://api.x/v1", "sk")
    result = await provider.chat(
        "gpt-x", ChatRequest(REQUEST.messages, max_tokens=50, json_mode=True)
    )
    assert seen["url"] == "https://api.x/v1/chat/completions"
    assert seen["auth"] == "Bearer sk"
    assert seen["body"]["response_format"] == {"type": "json_object"}
    assert seen["body"]["max_completion_tokens"] == 50
    assert (result.prompt_tokens, result.completion_tokens) == (3, 4)


async def test_openai_without_choices_is_response_error():
    provider = OpenAICompatibleProvider(
        "openai", client_with(lambda r: httpx.Response(200, json={"choices": []})), "http://x", "k"
    )
    with pytest.raises(ModelResponseError):
        await provider.chat("m", REQUEST)


async def test_anthropic_shape_moves_system_prompt():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "ok"}],
                "usage": {"input_tokens": 9, "output_tokens": 2},
            },
        )

    provider = AnthropicProvider(
        "anthropic", client_with(handler), "https://api.anthropic.com", "a"
    )
    result = await provider.chat("claude-x", ChatRequest(REQUEST.messages, json_mode=True))
    body = seen["body"]
    assert seen["headers"]["x-api-key"] == "a"
    assert seen["headers"]["anthropic-version"] == "2023-06-01"
    assert body["system"].startswith("be precise") and "JSON" in body["system"]
    assert all(m["role"] != "system" for m in body["messages"])
    assert (result.content, result.prompt_tokens, result.completion_tokens) == ("ok", 9, 2)


async def test_retry_on_503_then_success_records_both_calls():
    attempts = {"n": 0}
    records: list[CallRecord] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, json={"message": {"content": "ok"}, "eval_count": 1})

    async def recorder(record: CallRecord) -> None:
        records.append(record)

    gateway, delays = no_sleep_gateway(
        {"o": OllamaProvider("o", client_with(handler), "http://x")}, recorder
    )
    result = await gateway.chat("o/m", REQUEST, stage="plan", task_id="t1")
    assert result.content == "ok" and result.model == "o/m"
    assert attempts["n"] == 2
    assert len(delays) == 1 and 1 <= delays[0] <= 1.25
    assert [r.ok for r in records] == [False, True]
    assert records[0].error and "503" in records[0].error
    assert records[1].task_id == "t1" and records[1].stage == "plan"


async def test_retries_stop_after_max_and_backoff_grows():
    gateway, delays = no_sleep_gateway(
        {"o": OllamaProvider("o", client_with(lambda r: httpx.Response(429)), "http://x")},
        max_retries=3,
    )
    with pytest.raises(ModelRequestError) as info:
        await gateway.chat("o/m", REQUEST, stage="s")
    assert info.value.status_code == 429
    assert len(delays) == 3
    assert delays[0] < delays[1] < delays[2]


async def test_client_error_is_not_retried():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, text="bad request")

    gateway, delays = no_sleep_gateway({"o": OllamaProvider("o", client_with(handler), "http://x")})
    with pytest.raises(ModelRequestError) as info:
        await gateway.chat("o/m", REQUEST, stage="s")
    assert info.value.retryable is False
    assert calls["n"] == 1 and delays == []


async def test_network_errors_are_retryable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    gateway, delays = no_sleep_gateway(
        {"o": OllamaProvider("o", client_with(handler), "http://x")}, max_retries=1
    )
    with pytest.raises(ModelRequestError) as info:
        await gateway.chat("o/m", REQUEST, stage="s")
    assert info.value.retryable and len(delays) == 1


async def test_timeouts_are_retryable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    gateway, _ = no_sleep_gateway(
        {"o": OllamaProvider("o", client_with(handler), "http://x")}, max_retries=0
    )
    with pytest.raises(ModelRequestError, match="timed out"):
        await gateway.chat("o/m", REQUEST, stage="s")


async def test_fallback_moves_to_next_model():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["model"] == "bad":
            return httpx.Response(400, text="no such model")
        return httpx.Response(200, json={"message": {"content": "from good"}})

    gateway, _ = no_sleep_gateway({"o": OllamaProvider("o", client_with(handler), "http://x")})
    result = await gateway.chat_with_fallback(["missing/x", "o/bad", "o/good"], REQUEST, stage="s")
    assert result.content == "from good" and result.model == "o/good"


async def test_fallback_raises_last_error_and_unconfigured():
    gateway, _ = no_sleep_gateway(
        {"o": OllamaProvider("o", client_with(lambda r: httpx.Response(401)), "http://x")}
    )
    with pytest.raises(ModelRequestError):
        await gateway.chat_with_fallback(["o/a", "o/b"], REQUEST, stage="s")
    with pytest.raises(ModelNotConfiguredError):
        await gateway.chat_with_fallback(["nope/a"], REQUEST, stage="s")
    with pytest.raises(ModelNotConfiguredError):
        await gateway.chat("nope/a", REQUEST, stage="s")


async def test_recorder_failure_never_breaks_a_call():
    async def broken(_: CallRecord) -> None:
        raise RuntimeError("db down")

    gateway, _ = no_sleep_gateway(
        {
            "o": OllamaProvider(
                "o",
                client_with(lambda r: httpx.Response(200, json={"message": {"content": "x"}})),
                "http://x",
            )
        },
        recorder=broken,
    )
    assert (await gateway.chat("o/m", REQUEST, stage="s")).content == "x"


def test_available_filters_and_dedupes():
    gateway = ModelGateway({"a": OllamaProvider("a", httpx.AsyncClient(), "http://x")})
    assert gateway.available(["a/1", "b/2", "a/1", "bad", "a/3"]) == ["a/1", "a/3"]
    assert gateway.is_available("a/1") and not gateway.is_available("junk")


async def test_list_models_reports_provider_errors():
    ok = OllamaProvider(
        "ok", client_with(lambda r: httpx.Response(200, json={"models": [{"name": "m"}]})), "h://x"
    )
    bad = OllamaProvider("bad", client_with(lambda r: httpx.Response(500)), "h://x")
    gateway = ModelGateway({"ok": ok, "bad": bad})
    result = await gateway.list_models()
    assert result["ok"] == ["m"]
    assert isinstance(result["bad"], str) and result["bad"].startswith("error:")


async def test_from_settings_registers_only_configured_providers(tmp_path):
    settings = Settings(
        _env_file=None,
        OLLAMA_API_KEY=SecretStr("k"),
        ollama_local_base_url="http://localhost:11434",
    )
    gateway = ModelGateway.from_settings(settings)
    try:
        assert set(gateway.providers) == {"ollama-cloud", "ollama-local"}
        cloud = gateway.providers["ollama-cloud"]
        assert isinstance(cloud, OllamaProvider) and cloud.base_url == "https://ollama.com"
    finally:
        await gateway.close()

    full = Settings(
        _env_file=None,
        OLLAMA_API_KEY=SecretStr("k"),
        OPENAI_API_KEY=SecretStr("o"),
        ANTHROPIC_API_KEY=SecretStr("a"),
    )
    gateway = ModelGateway.from_settings(full)
    try:
        assert set(gateway.providers) == {"ollama-cloud", "openai", "anthropic"}
    finally:
        await gateway.close()


async def test_openai_and_anthropic_list_models():
    openai = OpenAICompatibleProvider(
        "openai",
        client_with(lambda r: httpx.Response(200, json={"data": [{"id": "b"}, {"id": "a"}]})),
        "http://x",
        "k",
    )
    anthropic = AnthropicProvider(
        "anthropic",
        client_with(lambda r: httpx.Response(200, json={"data": [{"id": "claude-1"}]})),
        "http://x",
        "k",
    )
    assert await openai.list_models() == ["a", "b"]
    assert await anthropic.list_models() == ["claude-1"]


async def test_anthropic_without_text_is_response_error():
    provider = AnthropicProvider(
        "anthropic",
        client_with(lambda r: httpx.Response(200, json={"content": [{"type": "tool_use"}]})),
        "http://x",
        "k",
    )
    with pytest.raises(ModelResponseError):
        await provider.chat("m", REQUEST)
