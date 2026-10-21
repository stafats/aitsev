"""Password hashing, token generation and a small in-memory rate limiter."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import time
from collections import deque
from threading import Lock

# scrypt parameters (OWASP: N=2^17, r=8, p=1 ~ 128 MiB). 2^15 keeps login fast on small
# servers while staying far above brute-force reach for strong passwords.
_N, _R, _P, _DKLEN = 2**15, 8, 1, 64
_MAXMEM = 128 * 1024 * 1024

API_KEY_PREFIX = "aitsev_"
SESSION_PREFIX = "sess_"
MIN_PASSWORD = 10


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN, maxmem=_MAXMEM
    )
    salt_b64 = base64.b64encode(salt).decode()
    digest_b64 = base64.b64encode(digest).decode()
    return f"scrypt${_N}${_R}${_P}${salt_b64}${digest_b64}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
            maxmem=_MAXMEM,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# A dummy hash lets login spend the same time whether or not the email exists.
DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def password_problem(password: str, email: str = "") -> str | None:
    if len(password) < MIN_PASSWORD:
        return f"Use at least {MIN_PASSWORD} characters"
    if len(password) > 256:
        return "Use at most 256 characters"
    if email and password.lower() == email.lower():
        return "Do not use your email address as the password"
    if len(set(password)) < 4:
        return "Use a less repetitive password"
    return None


def new_token(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    """Tokens have 256 bits of randomness, so a fast hash is enough (no salt needed)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")


def normalize_email(email: str) -> str:
    return email.strip().lower()


def valid_email(email: str) -> str:
    """Pydantic validator: a pragmatic address check (the verification email is the real test)."""
    value = normalize_email(email)
    if len(value) > 320 or not _EMAIL.match(value):
        raise ValueError("Enter a valid email address")
    return value


class RateLimiter:
    """Sliding-window limiter keyed by string (per process; put a shared store in front of
    several API replicas)."""

    def __init__(self, limit: int, window_seconds: float) -> None:
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = {}
        self._lock = Lock()

    def hit(self, key: str, now: float | None = None) -> bool:
        """Record an attempt. Returns False when the key is over the limit."""
        current = time.monotonic() if now is None else now
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= current - self.window:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(current)
            if len(self._hits) > 100_000:  # bound memory under attack
                self._hits = {k: v for k, v in self._hits.items() if v}
            return True

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)
