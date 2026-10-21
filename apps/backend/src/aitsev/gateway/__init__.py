"""Model gateway: one interface to every model provider."""

from aitsev.gateway.errors import (
    ModelError,
    ModelNotConfiguredError,
    ModelRequestError,
    ModelResponseError,
)
from aitsev.gateway.gateway import CallRecord, ModelGateway, UsageRecorder
from aitsev.gateway.types import ChatMessage, ChatRequest, ChatResult, ModelRef

__all__ = [
    "CallRecord",
    "ChatMessage",
    "ChatRequest",
    "ChatResult",
    "ModelError",
    "ModelGateway",
    "ModelNotConfiguredError",
    "ModelRef",
    "ModelRequestError",
    "ModelResponseError",
    "UsageRecorder",
]
