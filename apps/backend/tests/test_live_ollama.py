"""Live smoke tests against Ollama Cloud. Skipped unless OLLAMA_API_KEY is set.

Run with:  OLLAMA_API_KEY=... uv run pytest -m live -s
Set AITSEV_LIVE_MODEL to choose the model (default: the configured planner model).
"""

from __future__ import annotations

import os

import pytest

from aitsev.config import Settings
from aitsev.gateway import ChatMessage, ChatRequest, ModelGateway

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.environ.get("OLLAMA_API_KEY"), reason="OLLAMA_API_KEY not set"),
]


async def test_ollama_cloud_lists_models_and_answers():
    settings = Settings()
    gateway = ModelGateway.from_settings(settings)
    try:
        models = await gateway.list_models()
        listed = models["ollama-cloud"]
        assert isinstance(listed, list) and listed, f"No models listed: {listed}"
        print("\nOllama Cloud models:", ", ".join(listed))
        print(
            "Configured roles missing from the list:",
            [
                m
                for m in {settings.planner_model, settings.judge_model, *settings.generator_models}
                if m.startswith("ollama-cloud/") and m.split("/", 1)[1] not in listed
            ],
        )
        model = os.environ.get("AITSEV_LIVE_MODEL", settings.planner_model)
        result = await gateway.chat(
            model,
            ChatRequest(
                [ChatMessage("user", 'Reply with the JSON object {"ok": true} and nothing else.')],
                max_tokens=200,
                json_mode=True,
            ),
            stage="live-test",
        )
        assert '"ok"' in result.content.replace(" ", "")
    finally:
        await gateway.close()
