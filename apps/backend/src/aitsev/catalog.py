"""Models users can choose from, and which plans include them.

The server owns every provider key and does the routing: planning, review and judging always use
the server's configured roles. Users may pick which models write their code (up to
``MAX_SELECTED``); premium models (Claude, GPT, ...) need a paid plan and count more towards the
monthly token allowance, because they cost more to run.

Override the list with ``AITSEV_MODEL_CATALOG`` (JSON list of objects with id, label, tier and
optionally description and weight).
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, Field, TypeAdapter

from aitsev.accounts import plans
from aitsev.config import Settings

Tier = Literal["standard", "premium"]
MAX_SELECTED = 3
PREMIUM_WEIGHT = 5.0


class CatalogModel(BaseModel):
    id: str = Field(description="provider/model reference")
    label: str
    tier: Tier = "standard"
    description: str = ""
    # How many allowance tokens one real token counts as.
    weight: float = Field(default=1.0, gt=0, le=100)


DEFAULT_CATALOG: list[CatalogModel] = [
    CatalogModel(
        id="ollama-cloud/gpt-oss:120b",
        label="GPT-OSS 120B",
        description="Strong all-round coding and reasoning",
    ),
    CatalogModel(
        id="ollama-cloud/kimi-k2.6",
        label="Kimi K2.6",
        description="Long-context agentic coding",
    ),
    CatalogModel(
        id="ollama-cloud/deepseek-v4-flash",
        label="DeepSeek V4 Flash",
        description="Fast and precise code generation",
    ),
    CatalogModel(id="ollama-cloud/glm-5", label="GLM-5", description="Careful refactoring"),
    CatalogModel(
        id="anthropic/claude-sonnet-5-5",
        label="Claude Sonnet 5.5",
        tier="premium",
        description="Anthropic's balanced coding model",
        weight=PREMIUM_WEIGHT,
    ),
    CatalogModel(
        id="anthropic/claude-opus-5-5",
        label="Claude Opus 5.5",
        tier="premium",
        description="Anthropic's most capable model for hard problems",
        weight=PREMIUM_WEIGHT * 2,
    ),
    CatalogModel(
        id="openai/gpt-5",
        label="GPT-5",
        tier="premium",
        description="OpenAI's flagship model",
        weight=PREMIUM_WEIGHT,
    ),
]

_LIST = TypeAdapter(list[CatalogModel])


@lru_cache(maxsize=8)
def _parse(raw: str) -> tuple[CatalogModel, ...]:
    return tuple(_LIST.validate_python(json.loads(raw)))


def load(settings: Settings) -> list[CatalogModel]:
    if settings.model_catalog:
        return list(_parse(settings.model_catalog))
    return list(DEFAULT_CATALOG)


def weights(settings: Settings) -> dict[str, float]:
    return {m.id: m.weight for m in load(settings) if m.weight != 1.0}


def tier_allowed(plan_id: str | None, tier: Tier) -> bool:
    """``plan_id`` None means an operator (all models)."""
    if tier == "standard" or plan_id is None:
        return True
    return plans.plan_for(plan_id).premium_models


class SelectionError(Exception):
    def __init__(self, message: str, *, upgrade: bool = False) -> None:
        super().__init__(message)
        self.upgrade = upgrade


def check_selection(
    settings: Settings,
    selected: list[str],
    plan_id: str | None,
    is_available: object,
) -> list[str]:
    """Validate a user's model choice. ``is_available`` is ``gateway.is_available``."""
    if not selected:
        return []
    if len(selected) > MAX_SELECTED:
        raise SelectionError(f"Choose at most {MAX_SELECTED} models")
    by_id = {m.id: m for m in load(settings)}
    result: list[str] = []
    for ref in selected:
        model = by_id.get(ref)
        if model is None:
            raise SelectionError(f"Unknown model {ref!r}. See GET /v1/models/catalog")
        if not tier_allowed(plan_id, model.tier):
            raise SelectionError(
                f"{model.label} is included with the Pro plan and above. "
                "Upgrade your plan to use it.",
                upgrade=True,
            )
        if not callable(is_available) or not is_available(ref):
            raise SelectionError(f"{model.label} is not available on this server right now")
        if ref not in result:
            result.append(ref)
    return result
