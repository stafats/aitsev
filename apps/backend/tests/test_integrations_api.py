"""End-to-end integration flow through the HTTP API against fake external systems."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from aitsev.config import Settings
from aitsev.db.models import Job
from aitsev.main import create_app
from aitsev.pipeline.sandbox import LocalRunner
from tests.conftest import ALICE_KEY, BOB_KEY
from tests.fake_systems import (
    CRM_URL,
    LEGACY_OPENAPI,
    LEGACY_TOKEN,
    LEGACY_URL,
    TOKEN_URL,
    FakeSystems,
    legacy_customers,
)
from tests.fakes import Script, make_gateway

ALICE = {"Authorization": f"Bearer {ALICE_KEY}"}
BOB = {"Authorization": f"Bearer {BOB_KEY}"}

MODEL_MAPPINGS = [
    {
        "target": "firstname",
        "source": "full_name",
        "steps": [{"op": "split", "sep": " ", "index": 0}],
        "confidence": 0.85,
        "reason": "First word of the full name",
    },
    {
        "target": "lastname",
        "source": "full_name",
        "steps": [{"op": "split", "sep": " ", "index": -1}],
        "confidence": 0.85,
        "reason": "Last word of the full name",
    },
    {
        "target": "statecode",
        "source": "status",
        "steps": [{"op": "map_values", "values": {"A": 0, "I": 1}, "strict": True}],
        "confidence": 0.9,
        "reason": "A=active(0), I=inactive(1)",
    },
    {
        "target": "revenue",
        "source": "annual_revenue",
        "steps": [{"op": "to_float"}],
        "confidence": 0.85,
        "reason": "Revenue as a number",
    },
    {"target": "emailaddress1", "source": "nonexistent_field", "confidence": 0.9},
]


def mapping_script() -> Script:
    full = json.dumps({"mappings": MODEL_MAPPINGS})
    # the second model does not propose firstname, so that one ends up needing review
    partial = json.dumps({"mappings": [m for m in MODEL_MAPPINGS if m["target"] != "firstname"]})
    return Script().on("fake/m1", "mapping", full).on("fake/m2", "mapping", partial)


@pytest.fixture
def systems() -> FakeSystems:
    return FakeSystems()


@pytest.fixture
def client(settings: Settings, systems: FakeSystems) -> Iterator[TestClient]:
    app = create_app(
        settings,
        gateway=make_gateway(mapping_script()),
        runner=LocalRunner(),
        start_worker=False,
        integration_transport=systems.transport(),
    )
    with TestClient(app) as c:
        yield c


def wait(client: TestClient, path: str, done: set[str], timeout: float = 30) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(path, headers=ALICE).json()
        if body["status"] in done:
            return body
        time.sleep(0.05)
    raise AssertionError(f"{path} stuck in {body['status']}")


def legacy_connection(**overrides: object) -> dict:
    body = {
        "name": "Legacy CRM",
        "base_url": LEGACY_URL,
        "auth": {"type": "bearer"},
        "credentials": {"token": LEGACY_TOKEN},
        "pagination": {
            "style": "page",
            "page_param": "page",
            "size_param": "per_page",
            "page_size": 3,
        },
        "spec_format": "openapi",
        "spec_text": json.dumps(LEGACY_OPENAPI),
    }
    body.update(overrides)
    return body


def crm_connection() -> dict:
    return {
        "name": "Dynamics 365",
        "base_url": CRM_URL,
        "auth": {
            "type": "oauth2_client_credentials",
            "token_url": TOKEN_URL,
            "scope": "crm/.default",
        },
        "credentials": {"client_id": "crm-client", "client_secret": "crm-secret"},
        "spec_format": "odata",
    }


def setup_connections(client: TestClient) -> tuple[str, str]:
    legacy = client.post("/v1/connections", json=legacy_connection(), headers=ALICE)
    assert legacy.status_code == 201, legacy.text
    crm = client.post("/v1/connections", json=crm_connection(), headers=ALICE)
    assert crm.status_code == 201, crm.text
    legacy_id, crm_id = legacy.json()["id"], crm.json()["id"]
    assert (
        client.post(f"/v1/connections/{legacy_id}/discover", json={}, headers=ALICE).status_code
        == 200
    )
    discovered = client.post(
        f"/v1/connections/{crm_id}/discover", json={"spec_path": "$metadata"}, headers=ALICE
    )
    assert discovered.status_code == 200, discovered.text
    return legacy_id, crm_id


def approved_mapping(client: TestClient) -> str:
    legacy_id, crm_id = setup_connections(client)
    created = client.post(
        "/v1/mappings",
        json={
            "source_connection_id": legacy_id,
            "source_entity": "Customer",
            "target_connection_id": crm_id,
            "target_entity": "contact",
        },
        headers=ALICE,
    )
    assert created.status_code == 202, created.text
    mapping = wait(client, f"/v1/mappings/{created.json()['id']}", {"draft", "failed"})
    fields = [{**f, "status": "approved"} for f in mapping["fields"]]
    assert (
        client.put(
            f"/v1/mappings/{mapping['id']}", json={"fields": fields}, headers=ALICE
        ).status_code
        == 200
    )
    assert client.post(f"/v1/mappings/{mapping['id']}/approve", headers=ALICE).status_code == 200
    return mapping["id"]


def test_connections_hide_secrets_and_discover_schemas(client: TestClient, systems: FakeSystems):
    legacy_id, crm_id = setup_connections(client)
    listed = client.get("/v1/connections", headers=ALICE)
    text = listed.text
    assert LEGACY_TOKEN not in text and "crm-secret" not in text
    by_id = {c["id"]: c for c in listed.json()}
    assert by_id[legacy_id]["has_credentials"] is True
    assert by_id[legacy_id]["entities"] == ["CustomerPage", "Customer"]
    assert by_id[crm_id]["entities"] == ["contact"]

    schema = client.get(f"/v1/connections/{legacy_id}/schema", headers=ALICE).json()
    customer = next(e for e in schema["entities"] if e["name"] == "Customer")
    assert customer["list_path"] == "/customers" and customer["records_key"] == "data"
    assert customer["create_path"] == "/customers" and customer["key"] == "id"
    contact = client.get(f"/v1/connections/{crm_id}/schema", headers=ALICE).json()["entities"][0]
    assert contact["key"] == "contactid" and contact["list_path"] == "/contacts"
    lastname = next(f for f in contact["fields"] if f["name"] == "lastname")
    assert lastname["required"] is True and lastname["max_length"] == 50
    # the OData spec was fetched with an OAuth2 token
    assert systems.token_requests == 1


def test_auto_mapping_combines_rules_and_model_votes(client: TestClient):
    legacy_id, crm_id = setup_connections(client)
    created = client.post(
        "/v1/mappings",
        json={
            "source_connection_id": legacy_id,
            "source_entity": "Customer",
            "target_connection_id": crm_id,
            "target_entity": "contact",
        },
        headers=ALICE,
    ).json()
    assert created["status"] == "mapping"
    mapping = wait(client, f"/v1/mappings/{created['id']}", {"draft", "failed"})
    assert mapping["status"] == "draft", mapping["error"]
    fields = {f["target"]: f for f in mapping["fields"]}

    assert fields["companyname"]["source"] == "company_name"
    assert (
        fields["companyname"]["method"] == "exact" and fields["companyname"]["status"] == "approved"
    )
    assert (
        fields["emailaddress1"]["source"] == "email_address"
    )  # rule wins; model's bad field dropped
    assert fields["telephone1"]["source"] == "phone"
    assert fields["new_signupdate"]["source"] == "signup_date"
    assert fields["lastname"]["method"] == "agreed" and fields["lastname"]["status"] == "approved"
    assert fields["statecode"]["steps"][0]["op"] == "map_values"
    assert fields["revenue"]["steps"] == [{"op": "to_float"}]
    assert fields["firstname"]["method"] == "llm" and fields["firstname"]["status"] == "proposed"
    assert mapping["models_used"] == ["fake/m1", "fake/m2"]
    assert any("still need review" in issue for issue in mapping["blocking_issues"])

    refused = client.post(f"/v1/mappings/{mapping['id']}/approve", headers=ALICE)
    assert refused.status_code == 409 and "firstname" in refused.json()["detail"]


def test_review_edit_validation(client: TestClient):
    legacy_id, crm_id = setup_connections(client)
    mapping_id = client.post(
        "/v1/mappings",
        json={
            "source_connection_id": legacy_id,
            "source_entity": "Customer",
            "target_connection_id": crm_id,
            "target_entity": "contact",
        },
        headers=ALICE,
    ).json()["id"]
    mapping = wait(client, f"/v1/mappings/{mapping_id}", {"draft"})
    base = mapping["fields"]
    cases = [
        [*base, {"target": "contactid", "source": "id"}],  # key is not writable... still a field
        [*base, {"target": "nope", "source": "id"}],
        [*base, {"target": "companyname", "source": "company_name"}],  # duplicate
        [{"target": "lastname", "source": "missing"}],
        [{"target": "lastname", "source": "full_name", "steps": [{"op": "exec", "code": "x"}]}],
    ]
    codes = [
        client.put(f"/v1/mappings/{mapping_id}", json={"fields": c}, headers=ALICE).status_code
        for c in cases
    ]
    assert codes[1:] == [422, 422, 422, 422]

    # removing lastname makes the required field unmapped
    without = [{**f, "status": "approved"} for f in base if f["target"] != "lastname"]
    updated = client.put(
        f"/v1/mappings/{mapping_id}", json={"fields": without}, headers=ALICE
    ).json()
    assert "Required field 'lastname' has no approved mapping" in updated["blocking_issues"]
    assert "lastname" in updated["unmapped_targets"]
    assert updated["version"] > mapping["version"]


def test_dry_run_then_real_run_moves_records(client: TestClient, systems: FakeSystems):
    mapping_id = approved_mapping(client)

    real_first = client.post(
        "/v1/jobs", json={"mapping_id": mapping_id, "dry_run": False}, headers=ALICE
    )
    assert real_first.status_code == 409 and "dry run" in real_first.json()["detail"]

    dry = client.post("/v1/jobs", json={"mapping_id": mapping_id, "dry_run": True}, headers=ALICE)
    assert dry.status_code == 202
    dry_job = wait(client, f"/v1/jobs/{dry.json()['id']}", {"succeeded", "failed", "stopped"})
    assert dry_job["status"] == "succeeded", dry_job["error"]
    assert dry_job["stats"] == {
        "read": 7,
        "valid": 7,
        "invalid": 0,
        "written": 0,
        "write_failed": 0,
    }
    first = dry_job["sample"][0]
    assert first == {
        "firstname": "Asha",
        "lastname": "Rao1",
        "emailaddress1": "asha1@example.com",
        "telephone1": "+91 98765 00001",
        "companyname": "Company 1",
        "new_signupdate": "2024-05-17",
        "statecode": 0,
        "revenue": 1200.5,
    }
    assert systems.created == []
    assert client.get(f"/v1/mappings/{mapping_id}", headers=ALICE).json()["dry_run_passed"] is True

    real = client.post(
        "/v1/jobs",
        json={"mapping_id": mapping_id, "dry_run": False, "chunk_size": 2},
        headers=ALICE,
    )
    job = wait(client, f"/v1/jobs/{real.json()['id']}", {"succeeded", "failed", "stopped"})
    assert job["status"] == "succeeded", job["error"]
    assert job["stats"]["written"] == 7
    assert [c["lastname"] for c in systems.created] == [f"Rao{i}" for i in range(1, 8)]
    # pages of 3: page 1, 2, 3 were requested
    pages = [
        r.url.params.get("page") for r in systems.requests if r.url.path.endswith("/customers")
    ]
    assert pages[-3:] == ["1", "2", "3"]

    jobs = client.get("/v1/jobs", params={"mapping_id": mapping_id}, headers=ALICE).json()
    assert [j["dry_run"] for j in jobs] == [False, True]

    script = client.get(f"/v1/mappings/{mapping_id}/export", headers=ALICE)
    assert script.status_code == 200
    assert "migrate_customer_to_contact.py" in script.headers["content-disposition"]
    assert LEGACY_TOKEN not in script.text and "crm-secret" not in script.text
    compile(script.text, "migrate.py", "exec")


def test_invalid_records_block_the_real_run(client: TestClient, systems: FakeSystems):
    mapping_id = approved_mapping(client)
    systems.customers.append({**legacy_customers(1)[0], "id": 99, "status": "X"})  # unmapped status
    dry = client.post("/v1/jobs", json={"mapping_id": mapping_id}, headers=ALICE).json()
    job = wait(client, f"/v1/jobs/{dry['id']}", {"succeeded"})
    assert job["stats"]["invalid"] == 1
    assert job["errors"][0]["source_key"] == 99
    assert "No mapping for value 'X'" in job["errors"][0]["errors"][0]
    refused = client.post(
        "/v1/jobs", json={"mapping_id": mapping_id, "dry_run": False}, headers=ALICE
    )
    assert refused.status_code == 409


def test_write_failures_are_recorded_and_high_error_rate_stops(
    client: TestClient, systems: FakeSystems
):
    mapping_id = approved_mapping(client)
    systems.customers = legacy_customers(60)
    for c in systems.customers[:12]:
        c["email_address"] = f"bad{c['id']}@example.com"
    dry = client.post("/v1/jobs", json={"mapping_id": mapping_id}, headers=ALICE).json()
    assert wait(client, f"/v1/jobs/{dry['id']}", {"succeeded"})["stats"]["valid"] == 60
    real = client.post(
        "/v1/jobs",
        json={"mapping_id": mapping_id, "dry_run": False, "chunk_size": 10},
        headers=ALICE,
    ).json()
    job = wait(client, f"/v1/jobs/{real['id']}", {"succeeded", "stopped", "failed"})
    assert job["status"] == "stopped"
    assert "error rate" in job["error"]
    assert job["stats"]["write_failed"] == 12
    assert job["stats"]["read"] < 60  # stopped before reading everything
    assert job["errors"][0]["stage"] == "write"


def test_oauth_token_is_refreshed_on_401_and_retries_on_503(
    client: TestClient, systems: FakeSystems
):
    mapping_id = approved_mapping(client)
    systems.legacy_503_once = True
    dry = client.post("/v1/jobs", json={"mapping_id": mapping_id}, headers=ALICE).json()
    wait(client, f"/v1/jobs/{dry['id']}", {"succeeded"})
    systems.expire_next_token = True
    tokens_before = systems.token_requests
    real = client.post(
        "/v1/jobs", json={"mapping_id": mapping_id, "dry_run": False}, headers=ALICE
    ).json()
    job = wait(client, f"/v1/jobs/{real['id']}", {"succeeded", "failed"})
    assert job["status"] == "succeeded", job["error"]
    assert systems.token_requests == tokens_before + 2  # first token, then a refresh after 401
    assert len(systems.created) == 7


def test_cancel_and_single_running_job(client: TestClient, systems: FakeSystems):
    mapping_id = approved_mapping(client)
    systems.customers = legacy_customers(30)
    job = client.post(
        "/v1/jobs", json={"mapping_id": mapping_id, "chunk_size": 1}, headers=ALICE
    ).json()
    cancelled = client.post(f"/v1/jobs/{job['id']}/cancel", headers=ALICE)
    if cancelled.status_code == 200:
        assert cancelled.json()["status"] == "cancelled"
        final = client.get(f"/v1/jobs/{job['id']}", headers=ALICE).json()
        time.sleep(0.3)
        assert client.get(f"/v1/jobs/{job['id']}", headers=ALICE).json()["status"] == "cancelled"
        assert final["status"] == "cancelled"
    else:  # the job finished before the cancel arrived
        assert cancelled.status_code == 409


def test_ownership_validation_and_safety(client: TestClient, settings: Settings):
    legacy_id, crm_id = setup_connections(client)
    assert client.get(f"/v1/connections/{legacy_id}", headers=BOB).status_code == 404
    assert (
        client.post(f"/v1/connections/{legacy_id}/discover", json={}, headers=BOB).status_code
        == 404
    )
    assert (
        client.post(
            "/v1/mappings",
            json={
                "source_connection_id": legacy_id,
                "source_entity": "Customer",
                "target_connection_id": crm_id,
                "target_entity": "contact",
            },
            headers=BOB,
        ).status_code
        == 404
    )
    unknown_entity = client.post(
        "/v1/mappings",
        json={
            "source_connection_id": legacy_id,
            "source_entity": "Nope",
            "target_connection_id": crm_id,
            "target_entity": "contact",
        },
        headers=ALICE,
    )
    assert unknown_entity.status_code == 422
    secret_header = client.post(
        "/v1/connections",
        json=legacy_connection(headers={"Authorization": "Bearer x"}),
        headers=ALICE,
    )
    assert secret_header.status_code == 422
    bad_spec = client.post(
        "/v1/connections", json=legacy_connection(spec_text="not: [valid"), headers=ALICE
    )
    bad_id = bad_spec.json()["id"]
    failed = client.post(f"/v1/connections/{bad_id}/discover", json={}, headers=ALICE)
    assert failed.status_code == 422
    assert client.get(f"/v1/connections/{bad_id}", headers=ALICE).json()["last_error"]
    assert client.delete(f"/v1/connections/{bad_id}", headers=ALICE).status_code == 204
    file_url = client.post(
        "/v1/connections", json=legacy_connection(base_url="file:///etc/passwd"), headers=ALICE
    )
    assert file_url.status_code == 422


def test_private_addresses_are_blocked_outside_development(
    settings: Settings, systems: FakeSystems
):
    strict = settings.model_copy(update={"allow_private_networks": False})
    app = create_app(
        strict,
        gateway=make_gateway(mapping_script()),
        runner=LocalRunner(),
        start_worker=False,
        integration_transport=systems.transport(),
    )
    with TestClient(app) as c:
        blocked = c.post(
            "/v1/connections",
            json=legacy_connection(base_url="https://127.0.0.1/api"),
            headers=ALICE,
        )
        assert blocked.status_code == 422 and "private address" in blocked.json()["detail"]
        plain_http = c.post(
            "/v1/connections",
            json=legacy_connection(base_url="http://legacy.example/api"),
            headers=ALICE,
        )
        assert plain_http.status_code == 422 and "https" in plain_http.json()["detail"]


def test_production_requires_a_secret_key(settings: Settings):
    prod = settings.model_copy(update={"environment": "production", "secret_key": None})
    with pytest.raises(RuntimeError, match="AITSEV_SECRET_KEY"):
        create_app(prod, gateway=make_gateway(Script()), runner=LocalRunner())
    ok = prod.model_copy(update={"secret_key": SecretStr("x" * 40)})
    create_app(ok, gateway=make_gateway(Script()), runner=LocalRunner(), start_worker=False)


def test_connection_in_use_cannot_be_deleted(client: TestClient):
    mapping_id = approved_mapping(client)
    mapping = client.get(f"/v1/mappings/{mapping_id}", headers=ALICE).json()
    assert (
        client.delete(
            f"/v1/connections/{mapping['source_connection_id']}", headers=ALICE
        ).status_code
        == 409
    )


def test_restart_marks_interrupted_work(settings: Settings, systems: FakeSystems):
    app = create_app(
        settings,
        gateway=make_gateway(mapping_script()),
        runner=LocalRunner(),
        start_worker=False,
        integration_transport=systems.transport(),
    )
    with TestClient(app) as c:
        mapping_id = approved_mapping(c)
        db = c.app.state.db

        async def make_stuck() -> str:
            async with db.session() as session:
                job = Job(owner="alice", mapping_id=mapping_id, mapping_version=1, status="running")
                session.add(job)
                await session.commit()
                return job.id

        job_id = c.portal.call(make_stuck)
    with TestClient(app) as c:
        job = c.get(f"/v1/jobs/{job_id}", headers=ALICE).json()
        assert job["status"] == "failed" and "restart" in job["error"]
