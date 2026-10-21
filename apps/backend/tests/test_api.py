from __future__ import annotations

import time
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from aitsev.config import Settings
from aitsev.main import build_runner, create_app
from aitsev.pipeline.sandbox import LocalRunner
from tests.conftest import ALICE_KEY, BOB_KEY
from tests.fakes import (
    BUGGY_PROJECT,
    CORRECT,
    PLAN,
    VERIFY,
    WRONG,
    Script,
    judge,
    make_gateway,
    review,
)

ALICE = {"Authorization": f"Bearer {ALICE_KEY}"}
BOB = {"X-API-Key": BOB_KEY}


def script() -> Script:
    return (
        Script()
        .default("plan", PLAN)
        .default("generate", CORRECT)
        .default("repair", CORRECT)
        .default("review", review(8))
        .default("judge", judge("A"))
    )


@pytest.fixture
def scripted() -> Script:
    return script()


@pytest.fixture
def client(settings: Settings, scripted: Script) -> Iterator[TestClient]:
    app = create_app(settings, gateway=make_gateway(scripted), runner=LocalRunner())
    with TestClient(app) as test_client:
        yield test_client


def payload(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "title": "Fix add",
        "instructions": "Make add() return the sum.",
        "files": dict(BUGGY_PROJECT),
        "verify_commands": list(VERIFY),
        "size": "normal",
    }
    body.update(overrides)
    return body


def wait_for(client: TestClient, task_id: str, statuses: set[str], timeout: float = 60) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data = client.get(f"/v1/tasks/{task_id}", headers=ALICE).json()
        if data["status"] in statuses:
            return data
        time.sleep(0.05)
    raise AssertionError(f"Task stuck in {data['status']}")


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong-key-000000000000"},
        {"Authorization": "Basic abc"},
        {"X-API-Key": "nope"},
    ],
)
def test_auth_is_required(client, headers):
    response = client.get("/v1/tasks", headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_production_refuses_to_start_without_secret_key(settings):
    prod = settings.model_copy(
        update={"environment": "production", "api_keys": SecretStr(""), "secret_key": None}
    )
    with pytest.raises(RuntimeError, match="AITSEV_SECRET_KEY"):
        create_app(prod, gateway=make_gateway(script()), runner=LocalRunner())


def test_local_sandbox_is_refused_in_production(settings):
    prod = settings.model_copy(update={"environment": "production"})
    with pytest.raises(RuntimeError, match="development"):
        build_runner(prod)


def test_dev_mode_without_keys_uses_local_owner(settings):
    dev = settings.model_copy(update={"api_keys": SecretStr("")})
    app = create_app(dev, gateway=make_gateway(script()), runner=LocalRunner(), start_worker=False)
    with TestClient(app) as c:
        created = c.post("/v1/tasks", json=payload())
        assert created.status_code == 201
        assert c.get("/v1/tasks").json()[0]["id"] == created.json()["id"]


def test_full_flow_create_run_result_approve(client, scripted):
    scripted.on("fake/m2", "generate", WRONG)  # B needs a repair round
    created = client.post("/v1/tasks", json=payload(), headers=ALICE)
    assert created.status_code == 201, created.text
    task = created.json()
    assert task["status"] in {"queued", "planning"}
    assert task["file_count"] == 2

    done = wait_for(client, task["id"], {"awaiting_approval", "failed"})
    assert done["status"] == "awaiting_approval", done.get("error")
    assert done["plan"]["summary"]
    assert len(done["candidates"]) == 2
    assert done["decision"]["method"] == "judge_model"
    assert done["usage"]["total_tokens"] > 0
    assert done["winner_candidate_id"]

    result = client.get(f"/v1/tasks/{task['id']}/result", headers=ALICE)
    assert result.status_code == 200
    body = result.json()
    assert body["label"] == "A"
    assert body["verification"]["passed"] is True
    [change] = body["files"]
    assert change["path"] == "calc.py" and change["action"] == "modified"
    assert "+    return a + b" in change["diff"] and "-    return a - b" in change["diff"]
    assert change["content"] == "def add(a, b):\n    return a + b\n"

    approved = client.post(
        f"/v1/tasks/{task['id']}/approve", json={"comment": "Looks good"}, headers=ALICE
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"
    assert approved.json()["finished_at"]

    again = client.post(f"/v1/tasks/{task['id']}/approve", json={}, headers=ALICE)
    assert again.status_code == 409

    events = client.get(f"/v1/tasks/{task['id']}/events", headers=ALICE).json()
    types = [e["type"] for e in events]
    assert types[0] == "created" and types[-1] == "approved"
    assert events[-1]["actor"] == "user:alice" and events[-1]["message"] == "Looks good"
    later = client.get(
        f"/v1/tasks/{task['id']}/events", params={"after": events[-2]["id"]}, headers=ALICE
    ).json()
    assert [e["type"] for e in later] == ["approved"]


def test_reject_then_retry_with_feedback(client, scripted):
    task_id = client.post("/v1/tasks", json=payload(size="small"), headers=ALICE).json()["id"]
    wait_for(client, task_id, {"awaiting_approval"})

    rejected = client.post(
        f"/v1/tasks/{task_id}/reject", json={"reason": "Add a docstring"}, headers=ALICE
    )
    assert rejected.json()["status"] == "rejected"
    assert (
        client.post(f"/v1/tasks/{task_id}/reject", json={"reason": "x"}, headers=ALICE).status_code
        == 409
    )

    retried = client.post(
        f"/v1/tasks/{task_id}/retry", json={"feedback": "Add a docstring"}, headers=ALICE
    )
    assert retried.status_code == 200
    assert retried.json()["run_number"] == 2 and retried.json()["feedback"] == "Add a docstring"
    done = wait_for(client, task_id, {"awaiting_approval"})
    assert {c["run_number"] for c in done["candidates"]} == {1, 2}
    winner = next(c for c in done["candidates"] if c["id"] == done["winner_candidate_id"])
    assert winner["run_number"] == 2
    last_generate = [r for _, s, r in scripted.calls if s == "generate"][-1]
    assert "Add a docstring" in last_generate.messages[1].content


def test_cancel_queued_task(settings):
    app = create_app(
        settings, gateway=make_gateway(script()), runner=LocalRunner(), start_worker=False
    )
    with TestClient(app) as c:
        task_id = c.post("/v1/tasks", json=payload(), headers=ALICE).json()["id"]
        cancelled = c.post(f"/v1/tasks/{task_id}/cancel", headers=ALICE)
        assert cancelled.json()["status"] == "cancelled"
        assert c.post(f"/v1/tasks/{task_id}/cancel", headers=ALICE).status_code == 409
        assert c.get(f"/v1/tasks/{task_id}/result", headers=ALICE).status_code == 409
        retried = c.post(f"/v1/tasks/{task_id}/retry", json={"feedback": "go"}, headers=ALICE)
        assert retried.json()["status"] == "queued"


def test_failed_task_reports_error(client, scripted):
    scripted.default("generate", WRONG).default("repair", WRONG)
    task_id = client.post("/v1/tasks", json=payload(size="small"), headers=ALICE).json()["id"]
    done = wait_for(client, task_id, {"failed"})
    assert "No candidate passed verification" in done["error"]
    assert done["candidates"][0]["status"] == "failed"
    assert done["candidates"][0]["verification"]["passed"] is False


def test_tasks_are_private_to_their_owner(client):
    task_id = client.post("/v1/tasks", json=payload(), headers=ALICE).json()["id"]
    assert client.get(f"/v1/tasks/{task_id}", headers=BOB).status_code == 404
    assert client.get(f"/v1/tasks/{task_id}/events", headers=BOB).status_code == 404
    assert client.post(f"/v1/tasks/{task_id}/cancel", headers=BOB).status_code == 404
    assert client.get("/v1/tasks", headers=BOB).json() == []
    assert len(client.get("/v1/tasks", headers=ALICE).json()) == 1


def test_list_filters_and_paging(settings):
    app = create_app(
        settings, gateway=make_gateway(script()), runner=LocalRunner(), start_worker=False
    )
    with TestClient(app) as c:
        ids = [
            c.post("/v1/tasks", json=payload(title=f"t{i}"), headers=ALICE).json()["id"]
            for i in range(3)
        ]
        c.post(f"/v1/tasks/{ids[0]}/cancel", headers=ALICE)
        queued = c.get("/v1/tasks", params={"status": "queued"}, headers=ALICE).json()
        assert {t["id"] for t in queued} == set(ids[1:])
        page = c.get("/v1/tasks", params={"limit": 1, "offset": 1}, headers=ALICE).json()
        assert len(page) == 1
        bad = c.get("/v1/tasks", params={"status": "nonsense"}, headers=ALICE)
        assert bad.status_code == 422


@pytest.mark.parametrize(
    ("override", "code"),
    [
        ({"files": {"../escape.py": "x"}}, 422),
        ({"files": {"/abs.py": "x"}}, 422),
        ({"files": {".git/config": "x"}}, 422),
        ({"title": ""}, 422),
        ({"instructions": ""}, 422),
        ({"unknown_field": 1}, 422),
        ({"verify_commands": [""]}, 422),
        ({"verify_commands": ["x" * 2001]}, 422),
        ({"verify_commands": ["echo"] * 11}, 422),
        ({"sandbox_image": "Bad Image!"}, 422),
        ({"sandbox_image": "--privileged"}, 422),
        ({"size": "huge"}, 422),
        ({"files": {f"f{i}.py": "" for i in range(201)}}, 413),
        ({"files": {"big.py": "x" * 200_001}}, 413),
        ({"files": {f"f{i}.py": "x" * 150_000 for i in range(5)}}, 413),
    ],
)
def test_create_validation(settings, override, code):
    app = create_app(
        settings, gateway=make_gateway(script()), runner=LocalRunner(), start_worker=False
    )
    with TestClient(app) as c:
        response = c.post("/v1/tasks", json=payload(**override), headers=ALICE)
        assert response.status_code == code, response.text


def test_paths_are_normalized_on_create(settings):
    app = create_app(
        settings, gateway=make_gateway(script()), runner=LocalRunner(), start_worker=False
    )
    with TestClient(app) as c:
        response = c.post(
            "/v1/tasks", json=payload(files={"./src\\a.py": "x = 1\n"}), headers=ALICE
        )
        assert response.status_code == 201
        assert response.json()["file_count"] == 1


def test_unknown_task_is_404(client):
    assert client.get("/v1/tasks/nope", headers=ALICE).status_code == 404
    assert client.get("/v1/tasks/nope/result", headers=ALICE).status_code == 404


def test_models_endpoint_reports_roles_and_providers(client):
    data = client.get("/v1/models", headers=ALICE).json()
    roles = {(r["role"], r["model"]): r["available"] for r in data["roles"]}
    assert roles[("planner", "fake/m1")] is True
    assert ("generator", "fake/m3") in roles
    assert data["providers"] == {"fake": ["m1", "m2", "m3"]}


def test_openapi_docs_are_served(client):
    schema = client.get("/openapi.json").json()
    assert schema["info"]["title"] == "Aitsev API"
    assert "/v1/tasks/{task_id}/approve" in schema["paths"]


def test_health_reports_database_failure(client):
    async def broken(*args, **kwargs):
        raise RuntimeError("db down")

    original = client.app.state.db.sessionmaker
    try:

        class BrokenSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            execute = broken

            async def rollback(self):
                return None

        client.app.state.db.sessionmaker = BrokenSession
        response = client.get("/health")
        assert response.status_code == 503
        assert response.json()["database"] == "unreachable"
    finally:
        client.app.state.db.sessionmaker = original


def test_context_paths_validation_and_limits(settings):
    small = settings.model_copy(update={"max_context_bytes": 100})
    app = create_app(
        small, gateway=make_gateway(script()), runner=LocalRunner(), start_worker=False
    )
    big = {"big.py": "x = 1\n" * 50, "calc.py": "def add(a, b):\n    return a - b\n"}
    with TestClient(app) as c:
        too_big = c.post("/v1/tasks", json=payload(files=big), headers=ALICE)
        assert too_big.status_code == 413 and "context_paths" in too_big.json()["detail"]
        ok = c.post(
            "/v1/tasks", json=payload(files=big, context_paths=["./calc.py"]), headers=ALICE
        )
        assert ok.status_code == 201, ok.text
        assert ok.json()["context_paths"] == ["calc.py"]
        missing = c.post(
            "/v1/tasks", json=payload(files=big, context_paths=["nope.py"]), headers=ALICE
        )
        assert missing.status_code == 422
        unsafe = c.post(
            "/v1/tasks", json=payload(files=big, context_paths=["../x.py"]), headers=ALICE
        )
        assert unsafe.status_code == 422
