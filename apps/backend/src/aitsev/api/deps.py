"""Shared FastAPI dependencies: services, authentication and quotas.

Three ways to authenticate:

* operator keys from ``AITSEV_API_KEYS`` (name:key pairs) — for admins and automation
* personal API keys (``aitsev_...``) created by an account — for the extension and desktop app
* browser sessions (``aitsev_session`` cookie, or ``Bearer sess_...``) — for the web app.
  Cookie-authenticated requests that change data must send ``X-Aitsev-Client: web`` (CSRF guard).
"""

from __future__ import annotations

import hmac
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from aitsev import catalog
from aitsev.accounts import plans
from aitsev.accounts.security import API_KEY_PREFIX, SESSION_PREFIX, token_hash
from aitsev.config import Settings
from aitsev.db.models import Account, ApiKey, AuthSession
from aitsev.db.session import Database
from aitsev.gateway import ModelGateway
from aitsev.worker import Worker

DEV_OWNER = "local-dev"
SESSION_COOKIE = "aitsev_session"
CSRF_HEADER = "x-aitsev-client"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


@dataclass(frozen=True, slots=True)
class Principal:
    owner: str
    via: str  # operator | api_key | session | dev
    account: Account | None = None

    @property
    def is_admin(self) -> bool:
        return self.via in ("operator", "dev") or bool(self.account and self.account.is_admin)


def get_settings_dep(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_db(request: Request) -> Database:
    db: Database = request.app.state.db
    return db


def get_gateway(request: Request) -> ModelGateway:
    gateway: ModelGateway = request.app.state.gateway
    return gateway


def get_worker(request: Request) -> Worker | None:
    worker: Worker | None = request.app.state.worker
    return worker


async def get_session(db: Annotated[Database, Depends(get_db)]) -> AsyncIterator[AsyncSession]:
    async with db.session() as session:
        yield session


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED, detail, headers={"WWW-Authenticate": "Bearer"}
    )


async def _account_for_api_key(db: Database, key: str) -> Account | None:
    async with db.session() as session:
        row = (
            await session.scalars(
                select(ApiKey).where(
                    ApiKey.key_hash == token_hash(key), ApiKey.revoked_at.is_(None)
                )
            )
        ).first()
        if row is None:
            return None
        await session.execute(
            update(ApiKey).where(ApiKey.id == row.id).values(last_used_at=datetime.now(UTC))
        )
        account = await session.get(Account, row.account_id)
        await session.commit()
        return account


async def _account_for_session(db: Database, token: str) -> Account | None:
    async with db.session() as session:
        row = (
            await session.scalars(
                select(AuthSession).where(
                    AuthSession.token_hash == token_hash(token), AuthSession.revoked_at.is_(None)
                )
            )
        ).first()
        if row is None:
            return None
        expires = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=UTC)
        if expires <= datetime.now(UTC):
            return None
        return await session.get(Account, row.account_id)


async def get_principal(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header()] = None,
) -> Principal:
    keys: dict[str, str] = request.app.state.api_keys
    settings: Settings = request.app.state.settings
    db: Database = request.app.state.db

    supplied = x_api_key
    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer" and token:
            supplied = token.strip()
    via_cookie = False
    if not supplied:
        cookie = request.cookies.get(SESSION_COOKIE)
        if cookie:
            supplied, via_cookie = cookie, True

    if supplied:
        account: Account | None = None
        via = ""
        if supplied.startswith(API_KEY_PREFIX):
            account, via = await _account_for_api_key(db, supplied), "api_key"
        elif supplied.startswith(SESSION_PREFIX):
            if (
                via_cookie
                and request.method not in SAFE_METHODS
                and request.headers.get(CSRF_HEADER) != "web"
            ):
                raise HTTPException(status.HTTP_403_FORBIDDEN, "Missing X-Aitsev-Client header")
            account, via = await _account_for_session(db, supplied), "session"
        else:
            for key, name in keys.items():
                if hmac.compare_digest(key.encode(), supplied.encode()):
                    return Principal(owner=name, via="operator")
            raise _unauthorized("Invalid API key")
        if account is None:
            raise _unauthorized("Invalid or expired credentials")
        if account.disabled:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "This account is disabled")
        return Principal(owner=account.id, via=via, account=account)

    if not keys and settings.environment == "development":
        return Principal(owner=DEV_OWNER, via="dev")
    raise _unauthorized("Missing API key")


PrincipalDep = Annotated[Principal, Depends(get_principal)]


def get_owner(principal: PrincipalDep) -> str:
    return principal.owner


def require_admin(principal: PrincipalDep) -> Principal:
    if not principal.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Administrators only")
    return principal


async def check_quota(
    principal: Principal, settings: Settings, session: AsyncSession, *needs: plans.Resource
) -> plans.Usage | None:
    """Enforce plan limits for accounts (operator keys are not metered against a plan)."""
    account = principal.account
    if account is None:
        return None
    if settings.email_verification_required() and not account.email_verified:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Verify your email address first")
    usage = await plans.usage_for(session, principal.owner, weights=catalog.weights(settings))
    try:
        plans.check(account.plan, usage, needs)
    except plans.QuotaExceededError as exc:
        raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, str(exc)) from exc
    return usage


SessionDep = Annotated[AsyncSession, Depends(get_session)]
OwnerDep = Annotated[str, Depends(get_owner)]
AdminDep = Annotated[Principal, Depends(require_admin)]
SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
GatewayDep = Annotated[ModelGateway, Depends(get_gateway)]
WorkerDep = Annotated[Worker | None, Depends(get_worker)]
