"""Encryption at rest for connection credentials (Fernet: AES-128-CBC + HMAC-SHA256)."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from aitsev.config import Settings

log = logging.getLogger(__name__)

_DEV_MATERIAL = "aitsev-development-only-key-do-not-use-in-production"


class SecretBoxError(Exception):
    """Credentials could not be encrypted or decrypted."""


class SecretBox:
    def __init__(self, material: str) -> None:
        if len(material) < 32:
            raise SecretBoxError("AITSEV_SECRET_KEY must be at least 32 characters")
        key = base64.urlsafe_b64encode(hashlib.sha256(material.encode("utf-8")).digest())
        self._fernet = Fernet(key)

    @classmethod
    def from_settings(cls, settings: Settings) -> SecretBox:
        if settings.secret_key is not None:
            return cls(settings.secret_key.get_secret_value())
        if settings.environment == "development":
            log.warning("AITSEV_SECRET_KEY is not set; using an insecure development key")
            return cls(_DEV_MATERIAL)
        raise SecretBoxError("AITSEV_SECRET_KEY must be set outside development")

    def encrypt(self, data: dict[str, Any]) -> str:
        return self._fernet.encrypt(json.dumps(data).encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> dict[str, Any]:
        try:
            value = json.loads(self._fernet.decrypt(token.encode("ascii")))
        except (InvalidToken, ValueError) as exc:
            raise SecretBoxError(
                "Stored credentials cannot be decrypted (was AITSEV_SECRET_KEY changed?)"
            ) from exc
        if not isinstance(value, dict):
            raise SecretBoxError("Stored credentials are corrupt")
        return value
