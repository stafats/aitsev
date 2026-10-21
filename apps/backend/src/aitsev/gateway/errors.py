"""Gateway errors."""

from __future__ import annotations


class ModelError(Exception):
    """Base class for all model call failures."""

    retryable: bool = False


class ModelNotConfiguredError(ModelError):
    """The provider or model is not configured (for example, a missing API key)."""


class ModelRequestError(ModelError):
    """The provider rejected or failed the request."""

    def __init__(self, message: str, *, status_code: int | None = None, retryable: bool) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


class ModelResponseError(ModelError):
    """The provider answered, but the response could not be used."""
