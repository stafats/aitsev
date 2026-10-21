"""Plan-gated model choice, weighted usage, browser sign-in for apps, and download links."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from aitsev import catalog
from aitsev.accounts import plans
from aitsev.accounts.email import LogEmailSender
from aitsev.config import Settings
from aitsev.db.models import Account, DeviceAuth, ModelCall
from aitsev.db.session import Database
from aitsev.main import create_app
from aitsev.pipeline.sandbox import LocalRunner
from tests.conftest import ALICE_KEY
from tests.fakes import make_gateway
from tests.test_accounts import WEB, bearer, run, signup, task_body
from tests.test_api import script

OPERATOR = {"Authorization": f"Bearer {ALICE_KEY}"}
CATALOG = json.dumps(
    [
        {"id": "fake/m1", "label": "Model One"},
        {"id": "fake/m3", "label": "Model Three"},
        {"id": "fake/m2", "label": "Premium Two", "tier": "premium", "weight": 5},
        {"id": "missing/x", "label": "Not configured"},
    ]
)


@pytest.fixture
def catalog_settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"model_catalog": CATALOG})


def make_client(settings: Settings, *, worker: bool = False) -> TestClient:
    app = create_app(
        settings,
        gateway=make_gateway(script()),
        runner=LocalRunner(),
        start_worker=worker,
        email_sender=LogEmailSender(),
    )
    return TestClient(app)


@pytest.fixture
def client(catalog_settings: Settings) -> Iterator[TestClient]:
    with make_client(catalog_settings) as test_client:
        yield test_client


def set_plan(client: TestClient, admin_token: str, account_id: str, plan: str) -> None:
    response = client.put(
        f"/v1/admin/accounts/{account_id}", json={"plan": plan}, headers=bearer(admin_token)
    )
    assert response.status_code == 200, response.text


# ------------------------------------------------------------------ catalog and choice


def test_default_catalog_has_standard_and_premium_models(settings):
    models = catalog.load(settings)
    tiers = {m.tier for m in models}
    assert tiers == {"standard", "premium"}
    assert all(m.id.startswith("ollama-cloud/") for m in models if m.tier == "standard")
    assert catalog.weights(settings)["anthropic/claude-opus-5-5"] == 10.0
    assert catalog.tier_allowed(None, "premium")
    assert not catalog.tier_allowed("free", "premium")
    assert catalog.tier_allowed("pro", "premium")


def test_catalog_endpoint_reflects_plan_and_availability(client):
    admin = signup(client)
    client.cookies.clear()
    listed = client.get("/v1/models/catalog", headers=bearer(admin["token"])).json()
    assert listed["plan"] == "free"
    assert listed["premium_allowed"] is False
    assert listed["max_selected"] == 3
    assert listed["upgrade_url"].endswith("/app/#/account")
    by_id = {m["id"]: m for m in listed["models"]}
    assert by_id["fake/m1"]["allowed"] and by_id["fake/m1"]["available"]
    assert by_id["fake/m2"]["allowed"] is False
    assert by_id["missing/x"]["available"] is False

    set_plan(client, admin["token"], admin["account"]["id"], "pro")
    upgraded = client.get("/v1/models/catalog", headers=bearer(admin["token"])).json()
    assert upgraded["premium_allowed"] is True
    operator = client.get("/v1/models/catalog", headers=OPERATOR).json()
    assert operator["plan"] is None and operator["premium_allowed"] is True


@pytest.mark.parametrize(
    ("models", "code", "text"),
    [
        (["fake/m2"], 402, "Pro plan"),
        (["nope/y"], 422, "Unknown model"),
        (["missing/x"], 422, "not available"),
        (["fake/m1", "fake/m3", "fake/m1", "fake/m3"], 422, "at most 3"),
    ],
)
def test_model_choice_is_validated(client, models, code, text):
    token = signup(client)["token"]
    client.cookies.clear()
    response = client.post(
        "/v1/tasks", json={**task_body(), "models": models}, headers=bearer(token)
    )
    assert response.status_code == code
    assert text.lower() in response.text.lower()


def test_free_plan_can_choose_standard_models_and_pro_premium(client):
    admin = signup(client)
    client.cookies.clear()
    token = admin["token"]
    ok = client.post(
        "/v1/tasks", json={**task_body(), "models": ["fake/m1", "fake/m1"]}, headers=bearer(token)
    )
    assert ok.status_code == 201
    assert ok.json()["models"] == ["fake/m1"]
    auto = client.post("/v1/tasks", json=task_body(), headers=bearer(token))
    assert auto.json()["models"] == []
    set_plan(client, token, admin["account"]["id"], "pro")
    premium = client.post(
        "/v1/tasks", json={**task_body(), "models": ["fake/m2"]}, headers=bearer(token)
    )
    assert premium.status_code == 201
    assert (
        client.post(
            "/v1/tasks", json={**task_body(), "models": ["fake/m2"]}, headers=OPERATOR
        ).status_code
        == 201
    )


def test_chosen_models_write_the_code(catalog_settings):
    with make_client(catalog_settings, worker=True) as client:
        task = client.post(
            "/v1/tasks",
            json={**task_body(), "models": ["fake/m3"], "size": "large"},
            headers=OPERATOR,
        ).json()
        deadline = time.monotonic() + 60
        status = ""
        while time.monotonic() < deadline:
            status = client.get(f"/v1/tasks/{task['id']}", headers=OPERATOR).json()["status"]
            if status in ("awaiting_approval", "failed"):
                break
            time.sleep(0.05)
        assert status == "awaiting_approval"
        events = client.get(f"/v1/tasks/{task['id']}/events", headers=OPERATOR).json()
        started = next(e for e in events if e["type"] == "generation_started")
        assert started["data"]["models"] == ["fake/m3"]  # one chosen model, despite "large"


def test_premium_tokens_count_more(settings):
    async def go(db: Database) -> None:
        await db.create_schema()
        async with db.session() as session:
            for model in ("fake/m1", "fake/m2"):
                session.add(
                    ModelCall(
                        owner="acct",
                        stage="generate",
                        model=model,
                        ok=True,
                        prompt_tokens=60,
                        completion_tokens=40,
                        latency_ms=1,
                    )
                )
            await session.commit()
            plain = await plans.usage_for(session, "acct")
            weighted = await plans.usage_for(session, "acct", weights={"fake/m2": 5.0})
        assert plain.tokens == 200
        assert weighted.tokens == 600

    run(settings.database_url, go)


# ------------------------------------------------------------------ browser sign-in


def start_device(client: TestClient, name: str = "Aitsev Desktop") -> dict:
    response = client.post("/v1/auth/device/start", json={"client_name": name})
    assert response.status_code == 200, response.text
    return response.json()


def test_device_sign_in_flow(client):
    user = signup(client)
    started = start_device(client, "  Aitsev   for VS Code ")
    assert started["device_code"].startswith("dev_")
    assert len(started["user_code"]) == 9 and started["user_code"][4] == "-"
    assert started["verification_url"].endswith(f"/app/#/connect?code={started['user_code']}")

    poll = {"device_code": started["device_code"]}
    assert client.post("/v1/auth/device/poll", json=poll).json() == {
        "status": "pending",
        "api_key": None,
        "email": None,
    }
    info = client.get(f"/v1/auth/device/{started['user_code'].lower()}").json()
    assert info["client_name"] == "Aitsev for VS Code"
    assert info["status"] == "pending"

    decided = client.post(
        "/v1/auth/device/decision",
        json={"user_code": started["user_code"], "approve": True},
        headers=WEB,
    )
    assert decided.json()["status"] == "approved"
    again = client.post(
        "/v1/auth/device/decision",
        json={"user_code": started["user_code"], "approve": True},
        headers=WEB,
    )
    assert again.status_code == 409

    client.cookies.clear()
    granted = client.post("/v1/auth/device/poll", json=poll).json()
    assert granted["status"] == "approved"
    assert granted["email"] == user["account"]["email"]
    key = granted["api_key"]
    assert key.startswith("aitsev_")
    assert client.post("/v1/auth/device/poll", json=poll).status_code == 400  # only once
    assert (
        client.get("/v1/account", headers={"X-API-Key": key}).json()["email"] == "ada@example.com"
    )
    keys = client.get("/v1/account/keys", headers={"X-API-Key": key}).json()
    assert [k["name"] for k in keys] == ["Aitsev for VS Code"]


def test_denied_and_expired_device_codes(client, catalog_settings):
    signup(client)
    denied = start_device(client)
    client.post(
        "/v1/auth/device/decision",
        json={"user_code": denied["user_code"], "approve": False},
        headers=WEB,
    )
    refused = client.post("/v1/auth/device/poll", json={"device_code": denied["device_code"]})
    assert refused.status_code == 400 and "declined" in refused.text

    expired = start_device(client)

    async def expire(db: Database) -> None:
        async with db.session() as session:
            await session.execute(
                update(DeviceAuth)
                .where(DeviceAuth.user_code == expired["user_code"])
                .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
            await session.commit()

    run(catalog_settings.database_url, expire)
    assert (
        client.post(
            "/v1/auth/device/poll", json={"device_code": expired["device_code"]}
        ).status_code
        == 400
    )
    assert client.get(f"/v1/auth/device/{expired['user_code']}").status_code == 404
    assert (
        client.post(
            "/v1/auth/device/poll", json={"device_code": "dev_unknown_code_123"}
        ).status_code
        == 400
    )


def test_only_browser_sessions_can_approve(client):
    signup(client)
    key = client.post("/v1/account/keys", json={"name": "k"}, headers=WEB).json()["key"]
    started = start_device(client)
    client.cookies.clear()
    with_key = {"X-API-Key": key}
    assert (
        client.get(f"/v1/auth/device/{started['user_code']}", headers=with_key).status_code == 403
    )
    assert (
        client.get(f"/v1/auth/device/{started['user_code']}", headers=OPERATOR).status_code == 400
    )


def test_device_start_is_rate_limited(client):
    for _ in range(20):
        start_device(client)
    assert client.post("/v1/auth/device/start", json={"client_name": "x"}).status_code == 429


def test_approval_for_disabled_account_gives_no_key(client, catalog_settings):
    signup(client)
    started = start_device(client)
    client.post(
        "/v1/auth/device/decision",
        json={"user_code": started["user_code"], "approve": True},
        headers=WEB,
    )

    async def disable(db: Database) -> None:
        async with db.session() as session:
            await session.execute(update(Account).values(disabled=True))
            await session.commit()

    run(catalog_settings.database_url, disable)
    client.cookies.clear()
    assert (
        client.post(
            "/v1/auth/device/poll", json={"device_code": started["device_code"]}
        ).status_code
        == 403
    )


# ------------------------------------------------------------------ downloads


def test_download_links(settings):
    with make_client(settings) as client:
        assert client.get("/download/windows", follow_redirects=False).status_code == 404
    published = settings.model_copy(
        update={"download_base_url": "https://github.com/acme/aitsev/releases/latest/download/"}
    )
    with make_client(published) as client:
        win = client.get("/download/windows", follow_redirects=False)
        assert win.status_code == 302
        assert win.headers["location"] == (
            "https://github.com/acme/aitsev/releases/latest/download/Aitsev-IDE-Setup.exe"
        )
        assert (
            client.get("/download/vscode", follow_redirects=False)
            .headers["location"]
            .endswith("/aitsev.vsix")
        )
        assert client.get("/download/beos", follow_redirects=False).status_code == 404
