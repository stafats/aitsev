"""Accounts: sign-up, sessions, CSRF, API keys, verification, reset, admin and quotas."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from aitsev.accounts import plans
from aitsev.accounts.email import LogEmailSender, OutgoingEmail, SmtpEmailSender
from aitsev.accounts.security import (
    RateLimiter,
    hash_password,
    password_problem,
    valid_email,
    verify_password,
)
from aitsev.audit import DbUsageRecorder
from aitsev.config import Settings
from aitsev.db.models import Account, AuthSession, ModelCall, Task
from aitsev.db.session import Database
from aitsev.gateway.gateway import CallRecord
from aitsev.main import create_app
from aitsev.pipeline.sandbox import LocalRunner
from tests.conftest import ALICE_KEY
from tests.fakes import BUGGY_PROJECT, VERIFY, make_gateway
from tests.test_api import script

PASSWORD = "correct horse battery"
WEB = {"X-Aitsev-Client": "web"}
OPERATOR = {"Authorization": f"Bearer {ALICE_KEY}"}


@pytest.fixture
def outbox() -> LogEmailSender:
    return LogEmailSender()


def _client(settings: Settings, outbox: LogEmailSender) -> TestClient:
    app = create_app(
        settings,
        gateway=make_gateway(script()),
        runner=LocalRunner(),
        start_worker=False,
        email_sender=outbox,
    )
    return TestClient(app)


@pytest.fixture
def client(settings: Settings, outbox: LogEmailSender) -> Iterator[TestClient]:
    with _client(settings, outbox) as test_client:
        yield test_client


@pytest.fixture
def strict_client(settings: Settings, outbox: LogEmailSender) -> Iterator[TestClient]:
    strict = settings.model_copy(update={"require_email_verification": True})
    with _client(strict, outbox) as test_client:
        yield test_client


def signup(client: TestClient, email: str = "ada@example.com", **extra: str) -> dict:
    response = client.post("/v1/auth/signup", json={"email": email, "password": PASSWORD, **extra})
    assert response.status_code == 201, response.text
    return response.json()


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def link_token(email: OutgoingEmail) -> str:
    match = re.search(r"token=([A-Za-z0-9_\-]+)", email.body)
    assert match, email.body
    return match.group(1)


def task_body() -> dict:
    return {
        "title": "Fix add",
        "instructions": "Make add() return the sum.",
        "files": dict(BUGGY_PROJECT),
        "verify_commands": list(VERIFY),
    }


def run(db_url: str, coro_factory) -> None:  # type: ignore[no-untyped-def]
    async def go() -> None:
        db = Database(db_url)
        try:
            await coro_factory(db)
        finally:
            await db.dispose()

    asyncio.run(go())


# ------------------------------------------------------------------ security helpers


def test_password_hashing_round_trip():
    stored = hash_password(PASSWORD)
    assert stored.startswith("scrypt$")
    assert verify_password(PASSWORD, stored)
    assert not verify_password("wrong password!", stored)
    assert not verify_password(PASSWORD, "garbage")
    assert not verify_password(PASSWORD, "scrypt$1$2$3$!!$!!")


def test_password_rules():
    assert password_problem("short")
    assert password_problem("a" * 300)
    assert password_problem("ada@example.com", "ada@example.com")
    assert password_problem("abababababab")
    assert password_problem(PASSWORD) is None


def test_email_validation():
    assert valid_email("  Ada@Example.COM ") == "ada@example.com"
    for bad in ("no-at-sign", "a@b", "two@@example.com", "sp ace@example.com"):
        with pytest.raises(ValueError, match="valid email"):
            valid_email(bad)


def test_rate_limiter_window():
    limiter = RateLimiter(2, 10)
    assert limiter.hit("k", now=0) and limiter.hit("k", now=1)
    assert not limiter.hit("k", now=2)
    assert limiter.hit("k", now=11)
    limiter.reset("k")
    assert limiter.hit("k", now=11.5)


def test_smtp_sender_uses_starttls_and_ssl(monkeypatch):
    sent: list[tuple[str, str]] = []

    class FakeSmtp:
        def __init__(self, host, port, **_kw):  # type: ignore[no-untyped-def]
            sent.append(("connect", f"{host}:{port}"))

        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, *_a):  # type: ignore[no-untyped-def]
            return False

        def starttls(self, **_kw):  # type: ignore[no-untyped-def]
            sent.append(("starttls", ""))

        def login(self, user, _password):  # type: ignore[no-untyped-def]
            sent.append(("login", user))

        def send_message(self, message):  # type: ignore[no-untyped-def]
            sent.append(("send", message["To"]))

    monkeypatch.setattr("smtplib.SMTP", FakeSmtp)
    monkeypatch.setattr("smtplib.SMTP_SSL", FakeSmtp)
    email = OutgoingEmail("ada@example.com", "Hi", "Body")
    asyncio.run(SmtpEmailSender("mail", 587, "user", "pw", "Aitsev <a@b.co>").send(email))
    asyncio.run(SmtpEmailSender("mail", 465, None, None, "Aitsev <a@b.co>").send(email))
    assert sent == [
        ("connect", "mail:587"),
        ("starttls", ""),
        ("login", "user"),
        ("send", "ada@example.com"),
        ("connect", "mail:465"),
        ("send", "ada@example.com"),
    ]


# ------------------------------------------------------------------ sign-up and login


def test_signup_creates_admin_first_then_regular_accounts(client):
    first = signup(client, name="Ada")
    assert first["account"]["is_admin"] is True
    assert first["account"]["email_verified"] is True  # development default
    assert first["token"].startswith("sess_")
    assert "aitsev_session" in client.cookies
    client.cookies.clear()
    second = signup(client, "bob@example.com")
    assert second["account"]["is_admin"] is False
    assert second["account"]["plan"] == "free"


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"email": "not-an-email", "password": PASSWORD}, 422),
        ({"email": "x@example.com", "password": "short"}, 422),
        ({"email": "x@example.com", "password": PASSWORD, "admin": True}, 422),
    ],
)
def test_signup_validation(client, body, code):
    assert client.post("/v1/auth/signup", json=body).status_code == code


def test_duplicate_signup_is_rejected_case_insensitively(client):
    signup(client)
    response = client.post(
        "/v1/auth/signup", json={"email": "ADA@example.com", "password": PASSWORD}
    )
    assert response.status_code == 409


def test_signup_can_be_closed(settings, outbox):
    closed = settings.model_copy(update={"allow_signup": False})
    with _client(closed, outbox) as client:
        response = client.post(
            "/v1/auth/signup", json={"email": "a@example.com", "password": PASSWORD}
        )
        assert response.status_code == 403


def test_signup_is_rate_limited_per_address(client):
    for i in range(5):
        signup(client, f"user{i}@example.com")
    response = client.post(
        "/v1/auth/signup", json={"email": "user9@example.com", "password": PASSWORD}
    )
    assert response.status_code == 429


def test_login_and_wrong_password(client):
    signup(client)
    client.cookies.clear()
    bad = client.post("/v1/auth/login", json={"email": "ada@example.com", "password": "nope"})
    assert bad.status_code == 401
    unknown = client.post("/v1/auth/login", json={"email": "who@example.com", "password": PASSWORD})
    assert unknown.status_code == 401
    assert unknown.json()["detail"] == bad.json()["detail"]  # no account enumeration
    good = client.post("/v1/auth/login", json={"email": " Ada@Example.com", "password": PASSWORD})
    assert good.status_code == 200
    assert client.get("/v1/account").json()["email"] == "ada@example.com"


def test_login_is_rate_limited(client):
    signup(client)
    for _ in range(10):
        client.post("/v1/auth/login", json={"email": "ada@example.com", "password": "nope"})
    blocked = client.post("/v1/auth/login", json={"email": "ada@example.com", "password": PASSWORD})
    assert blocked.status_code == 429


# ------------------------------------------------------------------ sessions and CSRF


def test_cookie_session_needs_csrf_header_for_changes(client):
    signup(client)
    assert client.get("/v1/tasks").status_code == 200
    blocked = client.post("/v1/tasks", json=task_body())
    assert blocked.status_code == 403
    assert "X-Aitsev-Client" in blocked.json()["detail"]
    assert client.post("/v1/tasks", json=task_body(), headers=WEB).status_code == 201


def test_bearer_session_token_needs_no_csrf_header(client):
    token = signup(client)["token"]
    client.cookies.clear()
    assert client.post("/v1/tasks", json=task_body(), headers=bearer(token)).status_code == 201


def test_logout_revokes_the_session(client):
    token = signup(client)["token"]
    assert client.post("/v1/auth/logout", headers=WEB).status_code == 204
    assert client.get("/v1/account", headers=bearer(token)).status_code == 401


def test_logout_with_bearer_and_without_credentials(client):
    token = signup(client)["token"]
    client.cookies.clear()
    assert client.post("/v1/auth/logout", headers=bearer(token)).status_code == 204
    assert client.get("/v1/account", headers=bearer(token)).status_code == 401
    assert client.post("/v1/auth/logout").status_code == 204


def test_expired_session_is_rejected(client, settings):
    token = signup(client)["token"]

    async def expire(db: Database) -> None:
        async with db.session() as session:
            await session.execute(
                update(AuthSession).values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
            )
            await session.commit()

    run(settings.database_url, expire)
    assert client.get("/v1/account", headers=bearer(token)).status_code == 401


def test_accounts_own_their_tasks(client):
    ada = signup(client)["token"]
    client.cookies.clear()
    bob = signup(client, "bob@example.com")["token"]
    client.cookies.clear()
    task = client.post("/v1/tasks", json=task_body(), headers=bearer(ada)).json()
    assert client.get(f"/v1/tasks/{task['id']}", headers=bearer(bob)).status_code == 404
    assert client.get(f"/v1/tasks/{task['id']}", headers=bearer(ada)).status_code == 200
    assert client.get(f"/v1/tasks/{task['id']}", headers=OPERATOR).status_code == 404


# ------------------------------------------------------------------ API keys


def test_api_key_lifecycle(client):
    signup(client)
    created = client.post("/v1/account/keys", json={"name": "laptop"}, headers=WEB)
    assert created.status_code == 201
    key = created.json()["key"]
    assert key.startswith("aitsev_")
    listed = client.get("/v1/account/keys").json()
    assert [k["name"] for k in listed] == ["laptop"]
    assert "key" not in listed[0]
    assert listed[0]["prefix"] == key[:12]

    with_key = {"X-API-Key": key}
    client.cookies.clear()
    assert client.post("/v1/tasks", json=task_body(), headers=with_key).status_code == 201
    assert client.get("/v1/account/keys", headers=with_key).json()[0]["last_used_at"]
    # A key cannot mint more keys.
    assert client.post("/v1/account/keys", json={"name": "x"}, headers=with_key).status_code == 403

    revoked = client.delete(f"/v1/account/keys/{listed[0]['id']}", headers=with_key)
    assert revoked.status_code == 204
    assert client.get("/v1/tasks", headers=with_key).status_code == 401
    assert client.get("/v1/tasks", headers={"X-API-Key": "aitsev_unknown"}).status_code == 401


def test_revoking_unknown_key_is_404(client):
    signup(client)
    assert client.delete("/v1/account/keys/nope", headers=WEB).status_code == 404


def test_operator_keys_have_no_account(client):
    assert client.get("/v1/account", headers=OPERATOR).status_code == 400


# ------------------------------------------------------------------ verification and reset


def test_email_verification_flow(strict_client, outbox):
    client = strict_client
    data = signup(client)
    assert data["account"]["email_verified"] is False
    assert len(outbox.outbox) == 1
    assert "/app/#/verify?token=" in outbox.outbox[0].body
    blocked = client.post("/v1/tasks", json=task_body(), headers=WEB)
    assert blocked.status_code == 403
    assert "Verify" in blocked.json()["detail"]

    resent = client.post("/v1/auth/resend-verification", headers=WEB)
    assert resent.json() == {"status": "sent"}
    old, new = link_token(outbox.outbox[0]), link_token(outbox.outbox[1])
    assert client.post("/v1/auth/verify", json={"token": old}).status_code == 400  # superseded
    verified = client.post("/v1/auth/verify", json={"token": new})
    assert verified.json()["email_verified"] is True
    assert client.post("/v1/auth/verify", json={"token": new}).status_code == 400  # single use
    assert client.post("/v1/tasks", json=task_body(), headers=WEB).status_code == 201
    again = client.post("/v1/auth/resend-verification", headers=WEB)
    assert again.json() == {"status": "already verified"}


def test_password_reset_flow(client, outbox):
    old_session = signup(client)["token"]
    client.cookies.clear()
    unknown = client.post("/v1/auth/forgot-password", json={"email": "who@example.com"})
    known = client.post("/v1/auth/forgot-password", json={"email": "ada@example.com"})
    assert unknown.status_code == known.status_code == 202
    assert unknown.json() == known.json()
    assert len(outbox.outbox) == 1
    token = link_token(outbox.outbox[0])

    weak = client.post("/v1/auth/reset-password", json={"token": token, "password": "short"})
    assert weak.status_code == 422
    new_password = "a brand new passphrase"
    done = client.post("/v1/auth/reset-password", json={"token": token, "password": new_password})
    assert done.status_code == 200
    assert client.get("/v1/account", headers=bearer(old_session)).status_code == 401
    assert (
        client.post(
            "/v1/auth/login", json={"email": "ada@example.com", "password": PASSWORD}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/v1/auth/login", json={"email": "ada@example.com", "password": new_password}
        ).status_code
        == 200
    )
    reused = client.post("/v1/auth/reset-password", json={"token": token, "password": PASSWORD})
    assert reused.status_code == 400


def test_change_password(client):
    signup(client)
    wrong = client.post(
        "/v1/account/password",
        json={"current_password": "nope", "new_password": "another good one"},
        headers=WEB,
    )
    assert wrong.status_code == 400
    weak = client.post(
        "/v1/account/password",
        json={"current_password": PASSWORD, "new_password": "short"},
        headers=WEB,
    )
    assert weak.status_code == 422
    ok = client.post(
        "/v1/account/password",
        json={"current_password": PASSWORD, "new_password": "another good one"},
        headers=WEB,
    )
    assert ok.status_code == 204


# ------------------------------------------------------------------ admin


def test_admin_manages_accounts(client):
    admin = signup(client)["token"]
    client.cookies.clear()
    bob = signup(client, "bob@example.com")
    client.cookies.clear()
    bob_id, bob_token = bob["account"]["id"], bob["token"]

    assert client.get("/v1/admin/accounts", headers=bearer(bob_token)).status_code == 403
    listed = client.get("/v1/admin/accounts", headers=bearer(admin)).json()
    assert {a["email"] for a in listed} == {"ada@example.com", "bob@example.com"}
    assert listed[0]["usage"]["tasks"]["limit"] == 20

    upgraded = client.put(
        f"/v1/admin/accounts/{bob_id}", json={"plan": "pro"}, headers=bearer(admin)
    )
    assert upgraded.json()["plan"] == "pro"
    usage = client.get("/v1/account/usage", headers=bearer(bob_token)).json()
    assert usage["tasks"]["limit"] == 500

    disabled = client.put(
        f"/v1/admin/accounts/{bob_id}", json={"disabled": True}, headers=bearer(admin)
    )
    assert disabled.json()["disabled"] is True
    assert client.get("/v1/account", headers=bearer(bob_token)).status_code == 401
    login = client.post("/v1/auth/login", json={"email": "bob@example.com", "password": PASSWORD})
    assert login.status_code == 403

    admin_id = listed[0]["id"] if listed[0]["email"] == "ada@example.com" else listed[1]["id"]
    self_demote = client.put(
        f"/v1/admin/accounts/{admin_id}", json={"is_admin": False}, headers=bearer(admin)
    )
    assert self_demote.status_code == 409
    missing = client.put("/v1/admin/accounts/nope", json={"plan": "pro"}, headers=bearer(admin))
    assert missing.status_code == 404
    bad_plan = client.put(
        f"/v1/admin/accounts/{bob_id}", json={"plan": "gold"}, headers=bearer(admin)
    )
    assert bad_plan.status_code == 422


def test_operator_key_is_admin_and_disabled_api_key_is_refused(client, settings):
    signup(client)
    key = client.post("/v1/account/keys", json={"name": "ci"}, headers=WEB).json()["key"]
    client.cookies.clear()
    assert client.get("/v1/admin/accounts", headers=OPERATOR).status_code == 200

    async def disable(db: Database) -> None:
        async with db.session() as session:
            await session.execute(update(Account).values(disabled=True))
            await session.commit()

    run(settings.database_url, disable)
    assert client.get("/v1/tasks", headers={"X-API-Key": key}).status_code == 403


# ------------------------------------------------------------------ plans and quotas


def test_plans_are_listed(client):
    listed = client.get("/v1/plans").json()
    assert [p["id"] for p in listed] == ["free", "pro", "team", "enterprise"]
    assert listed[-1]["monthly_tokens"] is None


def test_token_quota_blocks_new_work(client, settings):
    data = signup(client)
    owner = data["account"]["id"]

    async def burn(db: Database) -> None:
        async with db.session() as session:
            session.add(
                ModelCall(
                    owner=owner,
                    stage="generate",
                    model="fake/m1",
                    ok=True,
                    prompt_tokens=200_000,
                    completion_tokens=100_000,
                    latency_ms=1,
                )
            )
            await session.commit()

    run(settings.database_url, burn)
    usage = client.get("/v1/account/usage").json()
    assert usage["tokens"] == {"used": 300_000, "limit": 300_000, "remaining": 0}
    blocked = client.post("/v1/tasks", json=task_body(), headers=WEB)
    assert blocked.status_code == 402
    assert "Free plan" in blocked.json()["detail"]
    # Operator keys are not metered.
    assert client.post("/v1/tasks", json=task_body(), headers=OPERATOR).status_code == 201


def test_task_quota(client, settings):
    owner = signup(client)["account"]["id"]

    async def fill(db: Database) -> None:
        async with db.session() as session:
            for _ in range(20):
                session.add(
                    Task(
                        owner=owner,
                        title="t",
                        instructions="i",
                        files={},
                        verify_commands=[],
                        setup_commands=[],
                    )
                )
            await session.commit()

    run(settings.database_url, fill)
    assert client.post("/v1/tasks", json=task_body(), headers=WEB).status_code == 402


def test_usage_counts_only_this_month_and_real_jobs():
    usage = plans.Usage(period_start=plans.month_start(), tokens=10, tasks=1, records=999)
    report = plans.report("free", usage)
    assert report.records.remaining == 1
    plans.check("free", usage, ("records",))
    with pytest.raises(plans.QuotaExceededError, match="migrated records"):
        plans.check("free", usage.model_copy(update={"records": 1000}), ("records",))
    plans.check("enterprise", usage.model_copy(update={"records": 10**9}), ("records",))
    assert plans.plan_for("unknown").id == "free"
    assert plans.month_start(datetime(2026, 3, 17, 5, tzinfo=UTC)) == datetime(
        2026, 3, 1, tzinfo=UTC
    )


def test_usage_recorder_tags_the_owner(settings):
    async def go(db: Database) -> None:
        await db.create_schema()
        token = plans.current_owner.set("acct-1")
        try:
            await DbUsageRecorder(db)(
                CallRecord(
                    task_id=None,
                    stage="mapping",
                    model="fake/m1",
                    ok=True,
                    prompt_tokens=5,
                    completion_tokens=7,
                    latency_ms=1,
                    error=None,
                )
            )
        finally:
            plans.current_owner.reset(token)
        async with db.session() as session:
            usage = await plans.usage_for(session, "acct-1")
        assert usage.tokens == 12

    run(settings.database_url, go)


# ------------------------------------------------------------------ web app hosting


def test_web_app_and_landing_are_served_with_security_headers(settings, outbox, tmp_path):
    web = tmp_path / "web"
    (web / "app").mkdir(parents=True)
    (web / "app" / "index.html").write_text("<html>app</html>")
    (web / "app" / "app.js").write_text("console.log(1)")
    (web / "landing.html").write_text("<html>landing</html>")
    (web / "landing.css").write_text("body{}")
    hosted = settings.model_copy(update={"web_dist_dir": str(web)})
    with _client(hosted, outbox) as client:
        landing = client.get("/")
        assert landing.text == "<html>landing</html>"
        assert "frame-ancestors 'none'" in landing.headers["content-security-policy"]
        assert landing.headers["x-content-type-options"] == "nosniff"
        assert client.get("/landing.css").headers["content-type"].startswith("text/css")
        assert client.get("/app/").text == "<html>app</html>"
        assert client.get("/app/app.js").status_code == 200
        assert client.get("/app/../landing.html").status_code in (200, 404)  # never escapes root
        assert "content-security-policy" not in client.get("/docs").headers


def test_missing_web_build_is_reported(settings, outbox, tmp_path):
    hosted = settings.model_copy(update={"web_dist_dir": str(tmp_path / "nope")})
    with pytest.raises(RuntimeError, match=r"app/index\.html"):
        create_app(hosted, gateway=make_gateway(script()), runner=LocalRunner())


def test_hsts_when_cookies_are_secure(settings, outbox):
    secure = settings.model_copy(update={"cookie_secure": True})
    with _client(secure, outbox) as client:
        assert "max-age" in client.get("/health").headers["strict-transport-security"]
