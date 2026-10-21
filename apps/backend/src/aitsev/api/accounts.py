"""Sign-up, login, email verification, password reset, API keys, usage and admin."""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import AfterValidator, BaseModel, ConfigDict, Field
from sqlalchemy import func, select, update

from aitsev import catalog
from aitsev.accounts import plans
from aitsev.accounts.email import EmailSender, OutgoingEmail
from aitsev.accounts.security import (
    API_KEY_PREFIX,
    DUMMY_HASH,
    SESSION_PREFIX,
    RateLimiter,
    hash_password,
    new_token,
    password_problem,
    token_hash,
    valid_email,
    verify_password,
)
from aitsev.api.deps import (
    SESSION_COOKIE,
    AdminDep,
    PrincipalDep,
    SessionDep,
    SettingsDep,
)
from aitsev.db.models import Account, ApiKey, AuthSession, DeviceAuth, OneTimeToken

router = APIRouter(prefix="/v1", tags=["accounts"])

HTTP_422 = 422
HTTP_429 = 429
VERIFY_HOURS = 48
RESET_HOURS = 2
MAX_KEYS = 20

EmailStr = Annotated[str, Field(max_length=320), AfterValidator(valid_email)]


@dataclass
class Limiters:
    """Per-process abuse limits (one set per app)."""

    login: RateLimiter = field(default_factory=lambda: RateLimiter(10, 15 * 60))
    signup: RateLimiter = field(default_factory=lambda: RateLimiter(5, 60 * 60))
    email: RateLimiter = field(default_factory=lambda: RateLimiter(5, 60 * 60))
    device_start: RateLimiter = field(default_factory=lambda: RateLimiter(20, 60 * 60))
    device_poll: RateLimiter = field(default_factory=lambda: RateLimiter(400, 15 * 60))


def _limits(request: Request) -> Limiters:
    limiters: Limiters = request.app.state.limiters
    return limiters


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _email_sender(request: Request) -> EmailSender:
    sender: EmailSender = request.app.state.email_sender
    return sender


# ------------------------------------------------------------------ schemas


class SignupIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)
    name: str = Field(default="", max_length=200)


class LoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


class TokenIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=10, max_length=200)


class EmailIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr


class ResetIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=10, max_length=200)
    password: str = Field(min_length=1, max_length=256)


class PasswordChangeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


class AccountOut(BaseModel):
    id: str
    email: str
    name: str
    email_verified: bool
    plan: str
    plan_name: str
    is_admin: bool
    disabled: bool
    created_at: datetime


class LoginOut(BaseModel):
    account: AccountOut
    token: str = Field(description="Session token; browsers also get it as an HttpOnly cookie")
    expires_at: datetime


class KeyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)


class KeyOut(BaseModel):
    id: str
    name: str
    prefix: str
    created_at: datetime
    last_used_at: datetime | None


class NewKeyOut(KeyOut):
    key: str = Field(description="Shown once. Store it now.")


class AdminAccountOut(AccountOut):
    last_login_at: datetime | None
    usage: plans.UsageReport


class AdminUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan: Literal["free", "pro", "team", "enterprise"] | None = None
    disabled: bool | None = None
    is_admin: bool | None = None
    email_verified: bool | None = None


class PlanOut(BaseModel):
    id: str
    name: str
    monthly_tokens: int | None
    monthly_tasks: int | None
    monthly_records: int | None
    price_note: str
    premium_models: bool


def _account_out(account: Account) -> AccountOut:
    return AccountOut(
        id=account.id,
        email=account.email,
        name=account.name,
        email_verified=account.email_verified,
        plan=account.plan,
        plan_name=plans.plan_for(account.plan).name,
        is_admin=account.is_admin,
        disabled=account.disabled,
        created_at=account.created_at,
    )


def _require_account(principal: PrincipalDep) -> Account:
    if principal.account is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "This needs a personal account (operator keys have no account)",
        )
    return principal.account


async def _issue_token(session: SessionDep, account: Account, purpose: str, hours: int) -> str:
    await session.execute(
        update(OneTimeToken)
        .where(
            OneTimeToken.account_id == account.id,
            OneTimeToken.purpose == purpose,
            OneTimeToken.used_at.is_(None),
        )
        .values(used_at=datetime.now(UTC))
    )
    token = new_token("")
    session.add(
        OneTimeToken(
            account_id=account.id,
            purpose=purpose,
            token_hash=token_hash(token),
            expires_at=datetime.now(UTC) + timedelta(hours=hours),
        )
    )
    return token


async def _consume_token(session: SessionDep, token: str, purpose: str) -> Account:
    row = (
        await session.scalars(
            select(OneTimeToken).where(
                OneTimeToken.token_hash == token_hash(token),
                OneTimeToken.purpose == purpose,
                OneTimeToken.used_at.is_(None),
            )
        )
    ).first()
    expires = None
    if row is not None:
        expires = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=UTC)
    if row is None or expires is None or expires <= datetime.now(UTC):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This link is invalid or has expired")
    account = await session.get(Account, row.account_id)
    if account is None or account.disabled:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This link is invalid or has expired")
    row.used_at = datetime.now(UTC)
    return account


async def _send_verification(
    request: Request, settings: SettingsDep, account: Account, token: str
) -> None:
    link = f"{settings.public_url.rstrip('/')}/app/#/verify?token={token}"
    await _email_sender(request).send(
        OutgoingEmail(
            to=account.email,
            subject="Confirm your Aitsev email address",
            body=f"Hello{(' ' + account.name) if account.name else ''},\n\n"
            f"Confirm your email address to start using Aitsev:\n{link}\n\n"
            f"This link expires in {VERIFY_HOURS} hours. "
            "If you did not sign up, ignore this email.",
        )
    )


async def _start_session(
    request: Request,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
    account: Account,
) -> LoginOut:
    token = new_token(SESSION_PREFIX)
    expires = datetime.now(UTC) + timedelta(days=settings.session_days)
    session.add(
        AuthSession(
            account_id=account.id,
            token_hash=token_hash(token),
            user_agent=(request.headers.get("user-agent") or "")[:300],
            expires_at=expires,
        )
    )
    account.last_login_at = datetime.now(UTC)
    await session.commit()
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.session_days * 86400,
        httponly=True,
        secure=settings.secure_cookies(),
        samesite="strict",
        path="/",
    )
    return LoginOut(account=_account_out(account), token=token, expires_at=expires)


# ------------------------------------------------------------------ auth


@router.post("/auth/signup", status_code=201, response_model=LoginOut)
async def signup(
    body: SignupIn, request: Request, response: Response, session: SessionDep, settings: SettingsDep
) -> LoginOut:
    if not settings.allow_signup:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Sign-up is closed on this server")
    if not _limits(request).signup.hit(_client_ip(request)):
        raise HTTPException(HTTP_429, "Too many sign-ups from this address; try again later")
    email = body.email
    problem = password_problem(body.password, email)
    if problem:
        raise HTTPException(HTTP_422, problem)
    exists = (await session.scalars(select(Account.id).where(Account.email == email))).first()
    if exists:
        raise HTTPException(status.HTTP_409_CONFLICT, "An account with this email already exists")
    first = not (await session.scalar(select(func.count(Account.id))))
    account = Account(
        email=email,
        name=body.name.strip(),
        password_hash=hash_password(body.password),
        plan=settings.default_plan if settings.default_plan in plans.PLANS else "free",
        is_admin=first,  # the first account on a new server administers it
        email_verified=not settings.email_verification_required(),
    )
    session.add(account)
    await session.flush()
    if not account.email_verified:
        token = await _issue_token(session, account, "verify", VERIFY_HOURS)
        await session.commit()
        await _send_verification(request, settings, account, token)
    return await _start_session(request, response, session, settings, account)


@router.post("/auth/login", response_model=LoginOut)
async def login(
    body: LoginIn, request: Request, response: Response, session: SessionDep, settings: SettingsDep
) -> LoginOut:
    email = body.email
    if not _limits(request).login.hit(f"{email}|{_client_ip(request)}"):
        raise HTTPException(HTTP_429, "Too many attempts; wait 15 minutes and try again")
    account = (await session.scalars(select(Account).where(Account.email == email))).first()
    ok = verify_password(body.password, account.password_hash if account else DUMMY_HASH)
    if account is None or not ok:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong email or password")
    if account.disabled:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This account is disabled")
    _limits(request).login.reset(f"{email}|{_client_ip(request)}")
    return await _start_session(request, response, session, settings, account)


@router.post("/auth/logout", status_code=204)
async def logout(request: Request, response: Response, session: SessionDep) -> None:
    token = request.cookies.get(SESSION_COOKIE)
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer sess_"):
        token = auth.split(" ", 1)[1].strip()
    if token:
        await session.execute(
            update(AuthSession)
            .where(AuthSession.token_hash == token_hash(token))
            .values(revoked_at=datetime.now(UTC))
        )
        await session.commit()
    response.delete_cookie(SESSION_COOKIE, path="/")


@router.post("/auth/verify", response_model=AccountOut)
async def verify_email(body: TokenIn, session: SessionDep) -> AccountOut:
    account = await _consume_token(session, body.token, "verify")
    account.email_verified = True
    await session.commit()
    return _account_out(account)


@router.post("/auth/resend-verification", status_code=202)
async def resend_verification(
    principal: PrincipalDep, request: Request, session: SessionDep, settings: SettingsDep
) -> dict[str, str]:
    account = _require_account(principal)
    if account.email_verified:
        return {"status": "already verified"}
    if not _limits(request).email.hit(f"verify|{account.id}"):
        raise HTTPException(HTTP_429, "Too many emails; try again later")
    row = await session.get(Account, account.id)
    assert row is not None  # noqa: S101
    token = await _issue_token(session, row, "verify", VERIFY_HOURS)
    await session.commit()
    await _send_verification(request, settings, row, token)
    return {"status": "sent"}


@router.post("/auth/forgot-password", status_code=202)
async def forgot_password(
    body: EmailIn, request: Request, session: SessionDep, settings: SettingsDep
) -> dict[str, str]:
    """Always answers the same way, so it cannot be used to discover accounts."""
    email = body.email
    if _limits(request).email.hit(f"reset|{email}"):
        account = (await session.scalars(select(Account).where(Account.email == email))).first()
        if account is not None and not account.disabled:
            token = await _issue_token(session, account, "reset", RESET_HOURS)
            await session.commit()
            link = f"{settings.public_url.rstrip('/')}/app/#/reset?token={token}"
            await _email_sender(request).send(
                OutgoingEmail(
                    to=account.email,
                    subject="Reset your Aitsev password",
                    body=f"Reset your password here:\n{link}\n\nThis link expires in {RESET_HOURS} "
                    "hours. If you did not ask for it, ignore this email; "
                    "your password is unchanged.",
                )
            )
    return {"status": "If the address has an account, a reset link is on its way."}


@router.post("/auth/reset-password", response_model=AccountOut)
async def reset_password(body: ResetIn, session: SessionDep) -> AccountOut:
    account = await _consume_token(session, body.token, "reset")
    problem = password_problem(body.password, account.email)
    if problem:
        raise HTTPException(HTTP_422, problem)
    account.password_hash = hash_password(body.password)
    account.email_verified = True  # they proved they control the mailbox
    await session.execute(
        update(AuthSession)
        .where(AuthSession.account_id == account.id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )
    await session.commit()
    return _account_out(account)


# ------------------------------------------------------------------ account


@router.get("/plans", response_model=list[PlanOut])
async def list_plans() -> list[PlanOut]:
    return [
        PlanOut(
            id=p.id,
            name=p.name,
            monthly_tokens=p.monthly_tokens,
            monthly_tasks=p.monthly_tasks,
            monthly_records=p.monthly_records,
            price_note=p.price_note,
            premium_models=p.premium_models,
        )
        for p in plans.PLANS.values()
    ]


@router.get("/account", response_model=AccountOut)
async def me(principal: PrincipalDep) -> AccountOut:
    return _account_out(_require_account(principal))


@router.get("/account/usage", response_model=plans.UsageReport)
async def my_usage(
    principal: PrincipalDep, session: SessionDep, settings: SettingsDep
) -> plans.UsageReport:
    account = _require_account(principal)
    usage = await plans.usage_for(session, account.id, weights=catalog.weights(settings))
    return plans.report(account.plan, usage)


@router.post("/account/password", status_code=204)
async def change_password(
    body: PasswordChangeIn, principal: PrincipalDep, session: SessionDep
) -> None:
    account = await session.get(Account, _require_account(principal).id)
    assert account is not None  # noqa: S101
    if not verify_password(body.current_password, account.password_hash):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The current password is wrong")
    problem = password_problem(body.new_password, account.email)
    if problem:
        raise HTTPException(HTTP_422, problem)
    account.password_hash = hash_password(body.new_password)
    await session.commit()


@router.get("/account/keys", response_model=list[KeyOut])
async def list_keys(principal: PrincipalDep, session: SessionDep) -> list[KeyOut]:
    account = _require_account(principal)
    rows = (
        await session.scalars(
            select(ApiKey)
            .where(ApiKey.account_id == account.id, ApiKey.revoked_at.is_(None))
            .order_by(ApiKey.created_at)
        )
    ).all()
    return [
        KeyOut(
            id=k.id,
            name=k.name,
            prefix=k.prefix,
            created_at=k.created_at,
            last_used_at=k.last_used_at,
        )
        for k in rows
    ]


@router.post("/account/keys", status_code=201, response_model=NewKeyOut)
async def create_key(body: KeyIn, principal: PrincipalDep, session: SessionDep) -> NewKeyOut:
    account = _require_account(principal)
    if principal.via == "api_key":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Sign in to create API keys")
    count = await session.scalar(
        select(func.count(ApiKey.id)).where(
            ApiKey.account_id == account.id, ApiKey.revoked_at.is_(None)
        )
    )
    if (count or 0) >= MAX_KEYS:
        raise HTTPException(status.HTTP_409_CONFLICT, f"At most {MAX_KEYS} active keys")
    key = new_token(API_KEY_PREFIX)
    row = ApiKey(
        account_id=account.id, name=body.name.strip(), prefix=key[:12], key_hash=token_hash(key)
    )
    session.add(row)
    await session.commit()
    return NewKeyOut(
        id=row.id,
        name=row.name,
        prefix=row.prefix,
        created_at=row.created_at,
        last_used_at=None,
        key=key,
    )


@router.delete("/account/keys/{key_id}", status_code=204)
async def revoke_key(key_id: str, principal: PrincipalDep, session: SessionDep) -> None:
    account = _require_account(principal)
    result = await session.execute(
        update(ApiKey)
        .where(ApiKey.id == key_id, ApiKey.account_id == account.id, ApiKey.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )
    await session.commit()
    if result.rowcount != 1:  # type: ignore[attr-defined]
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Key not found")


# ------------------------------------------------------------------ admin


@router.get("/admin/accounts", response_model=list[AdminAccountOut])
async def admin_accounts(
    _admin: AdminDep, session: SessionDep, settings: SettingsDep
) -> list[AdminAccountOut]:
    rows = (await session.scalars(select(Account).order_by(Account.created_at).limit(500))).all()
    factors = catalog.weights(settings)
    out = []
    for account in rows:
        usage = plans.report(
            account.plan, await plans.usage_for(session, account.id, weights=factors)
        )
        out.append(
            AdminAccountOut(
                **_account_out(account).model_dump(),
                last_login_at=account.last_login_at,
                usage=usage,
            )
        )
    return out


@router.put("/admin/accounts/{account_id}", response_model=AccountOut)
async def admin_update(
    account_id: str, body: AdminUpdateIn, admin: AdminDep, session: SessionDep
) -> AccountOut:
    account = await session.get(Account, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    if (
        admin.account is not None
        and admin.account.id == account.id
        and (body.disabled or body.is_admin is False)
    ):
        raise HTTPException(status.HTTP_409_CONFLICT, "You cannot disable or demote yourself")
    for attr in ("plan", "disabled", "is_admin", "email_verified"):
        value = getattr(body, attr)
        if value is not None:
            setattr(account, attr, value)
    if body.disabled:
        await session.execute(
            update(AuthSession)
            .where(AuthSession.account_id == account.id, AuthSession.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )
    await session.commit()
    return _account_out(account)


# ------------------------------------------------------------------ browser sign-in for apps
#
# 1. The desktop app or extension calls /auth/device/start and opens verification_url.
# 2. The user, signed in on the web, sees which app asks and approves it.
# 3. The app polls /auth/device/poll and receives a personal API key exactly once.

DEVICE_MINUTES = 10
DEVICE_INTERVAL = 3
USER_CODE_ALPHABET = "BCDFGHJKLMNPQRSTVWXZ"  # no vowels (no words), no look-alike characters


def _user_code() -> str:
    raw = "".join(secrets.choice(USER_CODE_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


class DeviceStartIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_name: str = Field(min_length=1, max_length=60)


class DeviceStartOut(BaseModel):
    device_code: str
    user_code: str
    verification_url: str
    expires_in: int
    interval: int


class DevicePollIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device_code: str = Field(min_length=10, max_length=200)


class DevicePollOut(BaseModel):
    status: Literal["pending", "approved"]
    api_key: str | None = None
    email: str | None = None


class DeviceInfoOut(BaseModel):
    user_code: str
    client_name: str
    status: str
    expires_at: datetime


class DeviceDecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_code: str = Field(min_length=9, max_length=9)
    approve: bool


@router.post("/auth/device/start", response_model=DeviceStartOut)
async def device_start(
    body: DeviceStartIn, request: Request, session: SessionDep, settings: SettingsDep
) -> DeviceStartOut:
    if not _limits(request).device_start.hit(_client_ip(request)):
        raise HTTPException(HTTP_429, "Too many sign-in attempts; try again later")
    device_code = new_token("dev_")
    user_code = _user_code()
    while (
        await session.scalars(select(DeviceAuth.id).where(DeviceAuth.user_code == user_code))
    ).first():
        user_code = _user_code()  # pragma: no cover - 20^8 codes
    session.add(
        DeviceAuth(
            device_code_hash=token_hash(device_code),
            user_code=user_code,
            client_name=" ".join(body.client_name.split()),
            expires_at=datetime.now(UTC) + timedelta(minutes=DEVICE_MINUTES),
        )
    )
    await session.commit()
    return DeviceStartOut(
        device_code=device_code,
        user_code=user_code,
        verification_url=f"{settings.public_url.rstrip('/')}/app/#/connect?code={user_code}",
        expires_in=DEVICE_MINUTES * 60,
        interval=DEVICE_INTERVAL,
    )


@router.post("/auth/device/poll", response_model=DevicePollOut)
async def device_poll(body: DevicePollIn, request: Request, session: SessionDep) -> DevicePollOut:
    if not _limits(request).device_poll.hit(_client_ip(request)):
        raise HTTPException(HTTP_429, "Polling too fast; slow down")
    row = (
        await session.scalars(
            select(DeviceAuth).where(DeviceAuth.device_code_hash == token_hash(body.device_code))
        )
    ).first()
    if row is None or row.status in ("consumed", "denied"):
        detail = "Sign-in was declined" if row and row.status == "denied" else "Unknown code"
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail)
    if _aware(row.expires_at) <= datetime.now(UTC):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This sign-in expired; start again")
    if row.status != "approved" or row.account_id is None:
        return DevicePollOut(status="pending")
    # Hand out the key exactly once: only the request that flips approved -> consumed wins.
    result = await session.execute(
        update(DeviceAuth)
        .where(DeviceAuth.id == row.id, DeviceAuth.status == "approved")
        .values(status="consumed")
    )
    if result.rowcount != 1:  # type: ignore[attr-defined]
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown code")
    account = await session.get(Account, row.account_id)
    if account is None or account.disabled:
        await session.commit()
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This account is disabled")
    key = new_token(API_KEY_PREFIX)
    session.add(
        ApiKey(
            account_id=account.id,
            name=row.client_name[:100],
            prefix=key[:12],
            key_hash=token_hash(key),
        )
    )
    await session.commit()
    return DevicePollOut(status="approved", api_key=key, email=account.email)


async def _pending_device(session: SessionDep, user_code: str) -> DeviceAuth:
    row = (
        await session.scalars(
            select(DeviceAuth).where(DeviceAuth.user_code == user_code.strip().upper())
        )
    ).first()
    if row is None or _aware(row.expires_at) <= datetime.now(UTC):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This sign-in code is invalid or expired")
    return row


def _require_browser(principal: PrincipalDep) -> Account:
    account = _require_account(principal)
    if principal.via != "session":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Approve sign-ins from the web app")
    return account


@router.get("/auth/device/{user_code}", response_model=DeviceInfoOut)
async def device_info(
    user_code: str, principal: PrincipalDep, session: SessionDep
) -> DeviceInfoOut:
    _require_browser(principal)
    row = await _pending_device(session, user_code)
    return DeviceInfoOut(
        user_code=row.user_code,
        client_name=row.client_name,
        status=row.status,
        expires_at=row.expires_at,
    )


@router.post("/auth/device/decision", response_model=DeviceInfoOut)
async def device_decision(
    body: DeviceDecisionIn, principal: PrincipalDep, session: SessionDep
) -> DeviceInfoOut:
    account = _require_browser(principal)
    row = await _pending_device(session, body.user_code)
    if row.status != "pending":
        raise HTTPException(status.HTTP_409_CONFLICT, "This sign-in was already handled")
    row.status = "approved" if body.approve else "denied"
    row.account_id = account.id if body.approve else None
    await session.commit()
    return DeviceInfoOut(
        user_code=row.user_code,
        client_name=row.client_name,
        status=row.status,
        expires_at=row.expires_at,
    )
