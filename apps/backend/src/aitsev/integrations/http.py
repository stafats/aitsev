"""HTTP access to external systems: auth, retries, SSRF protection and pagination."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import random
import socket
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Literal
from urllib.parse import urljoin, urlparse

import httpx
from pydantic import BaseModel, Field

from aitsev.integrations.transforms import read_path

log = logging.getLogger(__name__)

AuthType = Literal[
    "none", "api_key", "bearer", "basic", "oauth2_client_credentials", "oauth2_refresh_token"
]
RETRYABLE = frozenset({408, 425, 429, 500, 502, 503, 504})


class IntegrationError(Exception):
    """A call to an external system failed."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class BlockedAddressError(IntegrationError):
    """The URL points at an address this server is not allowed to call."""


class Credentials(BaseModel):
    """Secret parts of a connection. Stored encrypted; never returned by the API."""

    api_key: str | None = None
    token: str | None = None
    username: str | None = None
    password: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    refresh_token: str | None = None  # oauth2_refresh_token (e.g. Zoho, Salesforce web apps)


class AuthConfig(BaseModel):
    type: AuthType = "none"
    header_name: str = Field(default="X-API-Key", max_length=100)  # for api_key
    token_url: str | None = None  # for oauth2_client_credentials / oauth2_refresh_token
    scope: str | None = None
    # Word before the access token in the Authorization header ("Zoho-oauthtoken" for Zoho).
    token_prefix: str = Field(default="Bearer", pattern=r"^[A-Za-z][A-Za-z0-9._-]{0,39}$")


class Pagination(BaseModel):
    style: Literal["none", "page", "offset", "cursor", "odata"] = "none"
    page_param: str = "page"
    size_param: str = "per_page"
    page_size: int = Field(default=100, ge=1, le=5000)
    first_page: int = 1
    offset_param: str = "offset"
    limit_param: str = "limit"
    cursor_param: str = "cursor"
    cursor_path: str = "next_cursor"  # where the next cursor is in the response body


Resolver = Callable[[str], Awaitable[list[str]]]


async def _default_resolver(host: str) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


async def check_url_allowed(
    url: str, *, allow_private: bool, resolver: Resolver = _default_resolver
) -> None:
    """Refuse non-HTTP(S) URLs and, unless allowed, any host resolving to a private address."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise BlockedAddressError(f"Only http(s) URLs are allowed: {url!r}")
    if allow_private:
        return
    if parsed.scheme != "https":
        raise BlockedAddressError("Only https URLs are allowed for external systems")
    try:
        addresses = await resolver(parsed.hostname)
    except OSError as exc:
        raise IntegrationError(f"Cannot resolve {parsed.hostname}: {exc}") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise BlockedAddressError(
                f"{parsed.hostname} resolves to a private address ({ip}); "
                "set AITSEV_ALLOW_PRIVATE_NETWORKS=true to allow it"
            )


class ApiClient:
    """Calls one external REST API with its auth, with retries and token refresh."""

    def __init__(
        self,
        base_url: str,
        auth: AuthConfig,
        credentials: Credentials,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 60.0,
        max_retries: int = 3,
        allow_private: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver = _default_resolver,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.auth = auth
        self.credentials = credentials
        self.headers = dict(headers or {})
        self.max_retries = max(0, max_retries)
        self.allow_private = allow_private
        self.resolver = resolver
        self._sleep = sleep
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=15.0),
            transport=transport,
            follow_redirects=False,
        )
        self._token: str | None = None
        self._token_expires = 0.0
        self._checked_hosts: set[str] = set()

    async def __aenter__(self) -> ApiClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    def url_for(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            # absolute next-page links must stay on the same host
            if urlparse(path).netloc != urlparse(self.base_url).netloc:
                raise BlockedAddressError(f"Refusing to follow a link to another host: {path}")
            return path
        return urljoin(self.base_url, path.lstrip("/"))

    async def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return await self._request("GET", path, params=params)

    async def get_text(self, path: str) -> str:
        text = await self._request("GET", path, as_text=True)
        return str(text or "")

    async def post_json(
        self, path: str, body: dict[str, Any], headers: dict[str, str] | None = None
    ) -> Any:
        return await self._request("POST", path, json=body, extra_headers=headers)

    async def _auth_headers(self, force_refresh: bool = False) -> dict[str, str]:
        creds, kind = self.credentials, self.auth.type
        if kind == "api_key":
            return {self.auth.header_name: creds.api_key or ""}
        if kind == "bearer":
            return {"Authorization": f"Bearer {creds.token or ''}"}
        if kind == "basic":
            token = httpx.BasicAuth(creds.username or "", creds.password or "")
            request = httpx.Request("GET", self.base_url)
            return {"Authorization": next(token.auth_flow(request)).headers["Authorization"]}
        if kind in ("oauth2_client_credentials", "oauth2_refresh_token"):
            if force_refresh or not self._token or time.monotonic() >= self._token_expires:
                await self._fetch_token()
            return {"Authorization": f"{self.auth.token_prefix} {self._token}"}
        return {}

    async def _fetch_token(self) -> None:
        if not self.auth.token_url:
            raise IntegrationError("OAuth2 connection has no token_url")
        await self._check(self.auth.token_url)
        data = {
            "grant_type": "client_credentials",
            "client_id": self.credentials.client_id or "",
            "client_secret": self.credentials.client_secret or "",
        }
        if self.auth.type == "oauth2_refresh_token":
            if not self.credentials.refresh_token:
                raise IntegrationError("This connection needs a refresh_token")
            data["grant_type"] = "refresh_token"
            data["refresh_token"] = self.credentials.refresh_token
        if self.auth.scope:
            data["scope"] = self.auth.scope
        try:
            response = await self._client.post(self.auth.token_url, data=data)
        except httpx.HTTPError as exc:
            raise IntegrationError(f"Token request failed: {exc}") from exc
        if response.status_code >= 400:
            raise IntegrationError(
                f"Token request failed with HTTP {response.status_code}", response.status_code
            )
        payload = response.json()
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if not isinstance(token, str):
            raise IntegrationError("Token response had no access_token")
        self._token = token
        expires_in = payload.get("expires_in", 3600)
        lifetime = float(expires_in) if isinstance(expires_in, int | float | str) else 3600.0
        self._token_expires = time.monotonic() + max(30.0, lifetime - 60.0)

    async def _check(self, url: str) -> None:
        host = urlparse(url).netloc
        if host in self._checked_hosts:
            return
        await check_url_allowed(url, allow_private=self.allow_private, resolver=self.resolver)
        self._checked_hosts.add(host)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        as_text: bool = False,
        extra_headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> Any:
        url = self.url_for(path)
        await self._check(url)
        attempt, refreshed = 0, False
        while True:
            accept = "*/*" if as_text else "application/json"
            headers = {
                "Accept": accept,
                **self.headers,
                **(extra_headers or {}),
                **await self._auth_headers(),
            }
            try:
                response = await self._client.request(method, url, headers=headers, **kwargs)
            except httpx.TimeoutException as exc:
                error: IntegrationError = IntegrationError(f"{method} {url} timed out")
                retry = True
                cause: Exception = exc
            except httpx.TransportError as exc:
                error, retry, cause = IntegrationError(f"{method} {url} failed: {exc}"), True, exc
            else:
                if (
                    response.status_code == 401
                    and self.auth.type == "oauth2_client_credentials"
                    and not refreshed
                ):
                    refreshed = True
                    await self._fetch_token()
                    continue
                if response.status_code < 400:
                    if as_text:
                        return response.text
                    if not response.content:
                        return None
                    try:
                        return response.json()
                    except ValueError as exc:
                        raise IntegrationError(f"{method} {url} did not return JSON") from exc
                error = IntegrationError(
                    f"{method} {url} returned HTTP {response.status_code}: {response.text[:300]}",
                    response.status_code,
                )
                retry = response.status_code in RETRYABLE
                cause = error
                if retry and attempt < self.max_retries:
                    delay = _retry_after(response) or (2**attempt) * (1 + random.random() * 0.25)  # noqa: S311
                    attempt += 1
                    await self._sleep(min(delay, 60.0))
                    continue
            if retry and attempt < self.max_retries:
                attempt += 1
                await self._sleep((2**attempt) * 0.5)
                continue
            raise error from cause

    async def paginate(
        self,
        path: str,
        pagination: Pagination,
        *,
        records_key: str | None = None,
        limit: int | None = None,
        max_pages: int = 100_000,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield records from a list endpoint, following its pagination style."""
        yielded = 0
        page = pagination.first_page
        offset = 0
        cursor: str | None = None
        next_url: str | None = path
        for _ in range(max_pages):
            params: dict[str, Any] = {}
            url = path
            if pagination.style == "page":
                params = {pagination.page_param: page, pagination.size_param: pagination.page_size}
            elif pagination.style == "offset":
                params = {
                    pagination.offset_param: offset,
                    pagination.limit_param: pagination.page_size,
                }
            elif pagination.style == "cursor" and cursor:
                params = {pagination.cursor_param: cursor}
            elif pagination.style == "odata":
                if next_url is None:
                    return
                url = next_url
            body = await self.get_json(url, params or None)
            records = _extract_records(body, records_key)
            for record in records:
                yield record
                yielded += 1
                if limit is not None and yielded >= limit:
                    return
            if pagination.style == "none" or not records:
                return
            if pagination.style == "page":
                page += 1
                if len(records) < pagination.page_size:
                    return
            elif pagination.style == "offset":
                offset += len(records)
                if len(records) < pagination.page_size:
                    return
            elif pagination.style == "cursor":
                value = read_path(body, pagination.cursor_path) if isinstance(body, dict) else None
                if not value:
                    return
                cursor = str(value)
            elif pagination.style == "odata":
                link = body.get("@odata.nextLink") if isinstance(body, dict) else None
                next_url = str(link) if link else None
        raise IntegrationError(f"Stopped after {max_pages} pages; check the pagination settings")


def _extract_records(body: Any, records_key: str | None) -> list[dict[str, Any]]:
    if records_key:
        value = read_path(body, records_key) if isinstance(body, dict) else None
    elif isinstance(body, list):
        value = body
    elif isinstance(body, dict):
        value = next(
            (body[k] for k in ("value", "data", "items", "results", "records") if k in body), None
        )
    else:
        value = None
    if not isinstance(value, list):
        raise IntegrationError(
            "The list response did not contain an array of records"
            + (f" at {records_key!r}" if records_key else "")
        )
    return [r for r in value if isinstance(r, dict)]


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if value and value.strip().isdigit():
        return float(value.strip())
    return None
