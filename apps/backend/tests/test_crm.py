from __future__ import annotations

import json
import time
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from aitsev.config import Settings
from aitsev.crm.dynamics import (
    FieldSpec,
    analyze_records,
    attribute_payload,
    merge_plans,
    publish_payload,
)
from aitsev.crm.platforms import PLATFORMS
from aitsev.db.models import CrmChange
from aitsev.main import create_app
from aitsev.pipeline.sandbox import LocalRunner
from tests.conftest import ALICE_KEY, BOB_KEY
from tests.fake_dataverse import ORG_URL, TOKEN_URL, FakeDataverse, attr, contacts
from tests.fakes import Script, make_gateway

ALICE = {"Authorization": f"Bearer {ALICE_KEY}"}
BOB = {"Authorization": f"Bearer {BOB_KEY}"}


def spec(**kw) -> FieldSpec:
    base = {"schema_name": "new_Tier", "display_name": "Tier", "type": "text"}
    base.update(kw)
    return FieldSpec.model_validate(base)


# ------------------------------------------------------------------ payloads


def test_attribute_payloads_per_type():
    text = attribute_payload(spec(max_length=200, required="required", description="d"))
    assert text["@odata.type"] == "Microsoft.Dynamics.CRM.StringAttributeMetadata"
    assert text["AttributeType"] == "String" and text["MaxLength"] == 200
    assert text["RequiredLevel"]["Value"] == "ApplicationRequired"
    assert text["DisplayName"]["LocalizedLabels"][0] == {
        "@odata.type": "Microsoft.Dynamics.CRM.LocalizedLabel",
        "Label": "Tier",
        "LanguageCode": 1033,
    }
    assert text["Description"]["LocalizedLabels"][0]["Label"] == "d"
    expectations = {
        "memo": ("MemoAttributeMetadata", "Memo"),
        "whole_number": ("IntegerAttributeMetadata", "Integer"),
        "decimal": ("DecimalAttributeMetadata", "Decimal"),
        "boolean": ("BooleanAttributeMetadata", "Boolean"),
        "date": ("DateTimeAttributeMetadata", "DateTime"),
        "datetime": ("DateTimeAttributeMetadata", "DateTime"),
    }
    for kind, (odata, attribute_type) in expectations.items():
        body = attribute_payload(spec(type=kind))
        assert body["@odata.type"] == f"Microsoft.Dynamics.CRM.{odata}"
        assert body["AttributeType"] == attribute_type
    assert attribute_payload(spec(type="date"))["Format"] == "DateOnly"
    assert attribute_payload(spec(type="datetime"))["Format"] == "DateAndTime"
    number = attribute_payload(spec(type="whole_number", min_value=0, max_value=100))
    assert (number["MinValue"], number["MaxValue"]) == (0, 100)
    assert attribute_payload(spec(type="decimal", precision=4))["Precision"] == 4
    boolean = attribute_payload(spec(type="boolean", true_label="Active", false_label="Inactive"))
    assert boolean["OptionSet"]["TrueOption"]["Label"]["LocalizedLabels"][0]["Label"] == "Active"


def test_choice_values_follow_the_publisher_prefix():
    choice = spec(
        type="choice",
        options=[{"label": "Bronze"}, {"label": "Silver", "value": 100000000}, {"label": "Gold"}],
    )
    body = attribute_payload(choice, option_value_prefix=10000)
    values = [
        (o["Label"]["LocalizedLabels"][0]["Label"], o["Value"])
        for o in body["OptionSet"]["Options"]
    ]
    assert values == [("Bronze", 100000001), ("Silver", 100000000), ("Gold", 100000002)]
    assert body["OptionSet"]["IsGlobal"] is False
    assert publish_payload("contact")["ParameterXml"].count("<entity>contact</entity>") == 1


@pytest.mark.parametrize(
    "kw",
    [
        {"schema_name": "1_bad"},
        {"schema_name": "has-dash"},
        {"type": "choice", "options": [{"label": "Only one"}]},
        {"type": "choice", "options": [{"label": "A"}, {"label": "a"}]},
        {"type": "text", "options": [{"label": "A"}, {"label": "B"}]},
        {"type": "text", "max_length": 5000},
        {"type": "whole_number", "min_value": 5, "max_value": 1},
    ],
)
def test_field_spec_validation(kw):
    with pytest.raises(ValidationError):
        spec(**kw)


@pytest.mark.parametrize(
    "name", ["LoyaltyTier", "new_", "New_Tier", "toolongprefix_Tier", "other_Tier"]
)
def test_dynamics_names_need_the_publisher_prefix(name):
    assert PLATFORMS["dynamics"].name_problem(spec(schema_name=name), "new") is not None
    assert PLATFORMS["dynamics"].name_problem(spec(schema_name="new_Tier"), "new") is None


# ------------------------------------------------------------------ analysis and merging


def test_analyze_records_finds_problems():
    dv = FakeDataverse()
    report = analyze_records(
        "contact", {"EntitySetName": "contacts", "DisplayName": {}}, dv.attributes, dv.records, 20
    )
    fill = {f.name: f.fill_rate for f in report.fields}
    assert fill["fullname"] == 1.0
    assert fill["telephone1"] == 0.05
    assert fill["parentcustomerid"] == 1.0  # lookup read from _parentcustomerid_value
    assert "contactid" not in fill  # identifiers are not analysed
    assert report.unused_custom_fields == ["new_legacycode"]
    assert report.low_fill_fields == ["telephone1"]
    assert report.required_but_empty == ["lastname"]
    assert report.duplicate_groups[0] == {
        "field": "emailaddress1",
        "value": "dup@example.com",
        "count": 3,
    }
    assert len(report.suggestions) == 4
    empty = analyze_records("contact", {}, dv.attributes, [], 0)
    assert empty.suggestions == ["The entity has no records to analyse."]
    clean = analyze_records("contact", {}, [attr("fullname", "String")], [{"fullname": "A"}], 1)
    assert clean.suggestions == ["No data quality problems were found in the sample."]


def test_merge_plans_marks_agreement_and_skips_existing():
    a = [
        spec(schema_name="new_Tier", type="choice", options=[{"label": "A"}, {"label": "B"}]),
        spec(schema_name="new_Points", type="whole_number"),
        spec(schema_name="new_Legacycode"),
    ]
    b = [
        spec(schema_name="new_tier", type="choice", options=[{"label": "A"}, {"label": "B"}]),
        spec(schema_name="new_Points", type="decimal"),
    ]
    merged = merge_plans([("p/m1", a), ("p/m2", b)], {"new_legacycode"})
    by_name = {m["spec"]["schema_name"].lower(): m for m in merged}
    assert set(by_name) == {"new_tier", "new_points"}
    assert by_name["new_tier"]["agreed"] is True and merged[0]["spec"]["schema_name"] == "new_Tier"
    assert by_name["new_points"]["agreed"] is False and "disagree" in by_name["new_points"]["note"]


# ------------------------------------------------------------------ API flow

PLAN_A = {
    "fields": [
        {
            "schema_name": "new_LoyaltyTier",
            "display_name": "Loyalty Tier",
            "type": "choice",
            "options": [{"label": "Bronze"}, {"label": "Silver"}, {"label": "Gold"}],
        },
        {
            "schema_name": "new_LoyaltyPoints",
            "display_name": "Loyalty Points",
            "type": "whole_number",
            "min_value": 0,
        },
        {"schema_name": "other_Wrong", "display_name": "Wrong prefix", "type": "text"},
        {
            "schema_name": "new_Bad",
            "display_name": "Bad",
            "type": "choice",
            "options": [{"label": "x"}],
        },
        {"schema_name": "new_LegacyCode", "display_name": "Exists already", "type": "text"},
    ]
}
PLAN_B = {
    "fields": [
        {
            "schema_name": "new_LoyaltyTier",
            "display_name": "Tier",
            "type": "choice",
            "options": [{"label": "Bronze"}, {"label": "Silver"}, {"label": "Gold"}],
        },
        {"schema_name": "new_LoyaltyJoined", "display_name": "Loyalty Joined", "type": "date"},
    ]
}


@pytest.fixture
def dataverse() -> FakeDataverse:
    return FakeDataverse()


@pytest.fixture
def client(settings: Settings, dataverse: FakeDataverse) -> Iterator[TestClient]:
    script = (
        Script()
        .on("fake/m1", "crm_plan", json.dumps(PLAN_A))
        .on("fake/m2", "crm_plan", json.dumps(PLAN_B))
    )
    app = create_app(
        settings,
        gateway=make_gateway(script),
        runner=LocalRunner(),
        start_worker=False,
        integration_transport=dataverse.transport(),
    )
    with TestClient(app) as c:
        yield c


def connect(client: TestClient, environment: str = "sandbox") -> str:
    response = client.post(
        "/v1/connections",
        json={
            "name": f"Contoso {environment}",
            "base_url": ORG_URL,
            "auth": {"type": "oauth2_client_credentials", "token_url": TOKEN_URL},
            "credentials": {"client_id": "dv", "client_secret": "dv-secret"},
            "spec_format": "odata",
            "environment": environment,
        },
        headers=ALICE,
    )
    assert response.status_code == 201, response.text
    assert response.json()["environment"] == environment
    return response.json()["id"]


def wait(client: TestClient, path: str, done: set[str], timeout: float = 20) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(path, headers=ALICE).json()
        if body["status"] in done:
            return body
        time.sleep(0.05)
    raise AssertionError(f"{path} stuck in {body['status']}")


def test_analysis_flow(client: TestClient):
    conn = connect(client)
    started = client.post(
        f"/v1/crm/{conn}/analyses", json={"entities": ["contact"], "sample_size": 50}, headers=ALICE
    )
    assert started.status_code == 202
    done = wait(client, f"/v1/crm/analyses/{started.json()['id']}", {"done", "failed"})
    assert done["status"] == "done", done["error"]
    [report] = done["report"]
    assert (
        report["display_name"] == "Contact"
        and report["total_records"] == 20
        and report["sampled"] == 20
    )
    assert report["unused_custom_fields"] == ["new_legacycode"]
    assert (
        client.get("/v1/crm/analyses", params={"connection_id": conn}, headers=ALICE).json()[0][
            "id"
        ]
        == done["id"]
    )

    missing = client.post(
        f"/v1/crm/{conn}/analyses", json={"entities": ["account"]}, headers=ALICE
    ).json()
    failed = wait(client, f"/v1/crm/analyses/{missing['id']}", {"done", "failed"})
    assert failed["status"] == "failed" and "404" in failed["error"]
    bad = client.post(
        f"/v1/crm/{conn}/analyses", json={"entities": ["Contact; drop"]}, headers=ALICE
    )
    assert bad.status_code == 422


def start_change(client: TestClient, conn: str, **kw) -> dict:
    body = {
        "entity": "contact",
        "request": "Track loyalty: a tier (Bronze/Silver/Gold), points, join date",
    }
    body.update(kw)
    started = client.post(f"/v1/crm/{conn}/changes", json=body, headers=ALICE)
    assert started.status_code == 202, started.text
    return wait(client, f"/v1/crm/changes/{started.json()['id']}", {"proposed", "failed"})


def test_column_plan_review_approve_apply(client: TestClient, dataverse: FakeDataverse):
    conn = connect(client)
    change = start_change(client, conn, solution="ContosoCore")
    assert change["status"] == "proposed", change["error"]
    items = {i["spec"]["schema_name"]: i for i in change["items"]}
    # wrong prefix, invalid and already-existing columns were dropped
    assert set(items) == {"new_LoyaltyTier", "new_LoyaltyPoints", "new_LoyaltyJoined"}
    assert items["new_LoyaltyTier"]["agreed"] is True
    assert items["new_LoyaltyPoints"]["agreed"] is False
    assert items["new_LoyaltyTier"]["payload"]["AttributeType"] == "Picklist"
    assert change["models_used"] == ["fake/m1", "fake/m2"]

    # review: drop the join date, rename points
    edited = [
        {"spec": items["new_LoyaltyTier"]["spec"], "include": True},
        {"spec": {**items["new_LoyaltyPoints"]["spec"], "display_name": "Points"}, "include": True},
        {"spec": items["new_LoyaltyJoined"]["spec"], "include": False},
    ]
    updated = client.put(f"/v1/crm/changes/{change['id']}", json={"items": edited}, headers=ALICE)
    assert updated.status_code == 200
    assert updated.json()["version"] == change["version"] + 1
    dup = client.put(
        f"/v1/crm/changes/{change['id']}", json={"items": [edited[0], edited[0]]}, headers=ALICE
    )
    assert dup.status_code == 422
    wrong = {
        "spec": {**items["new_LoyaltyTier"]["spec"], "schema_name": "abc_Tier"},
        "include": True,
    }
    assert (
        client.put(
            f"/v1/crm/changes/{change['id']}", json={"items": [wrong]}, headers=ALICE
        ).status_code
        == 422
    )

    assert (
        client.post(f"/v1/crm/changes/{change['id']}/apply", json={}, headers=ALICE).status_code
        == 409
    )
    assert client.post(f"/v1/crm/changes/{change['id']}/approve", headers=ALICE).status_code == 200
    applied = client.post(f"/v1/crm/changes/{change['id']}/apply", json={}, headers=ALICE)
    assert applied.status_code == 202 and applied.json()["status"] == "applying"
    final = wait(
        client, f"/v1/crm/changes/{change['id']}", {"applied", "partially_applied", "failed"}
    )
    assert final["status"] == "applied", final["error"]
    assert [r["status"] for r in final["results"]] == ["created", "created"]
    assert final["applied_by"] == "user:alice"
    assert [c["SchemaName"] for c in dataverse.created] == ["new_LoyaltyTier", "new_LoyaltyPoints"]
    assert dataverse.created[1]["DisplayName"]["LocalizedLabels"][0]["Label"] == "Points"
    assert dataverse.solution_headers == ["ContosoCore", "ContosoCore"]
    assert dataverse.published == [
        "<importexportxml><entities><entity>contact</entity></entities></importexportxml>"
    ]
    again = client.post(f"/v1/crm/changes/{change['id']}/apply", json={}, headers=ALICE)
    assert again.status_code == 409


def test_production_needs_explicit_confirmation(client: TestClient, dataverse: FakeDataverse):
    conn = connect(client, "production")
    change = start_change(client, conn)
    client.post(f"/v1/crm/changes/{change['id']}/approve", headers=ALICE)
    refused = client.post(f"/v1/crm/changes/{change['id']}/apply", json={}, headers=ALICE)
    assert refused.status_code == 409 and "sandbox first" in refused.json()["detail"]
    assert dataverse.created == []
    ok = client.post(
        f"/v1/crm/changes/{change['id']}/apply", json={"confirm_production": True}, headers=ALICE
    )
    assert ok.status_code == 202
    assert (
        wait(client, f"/v1/crm/changes/{change['id']}", {"applied", "partially_applied", "failed"})[
            "status"
        ]
        == "applied"
    )


def test_partial_failure_existing_columns_and_publish_errors(
    client: TestClient, dataverse: FakeDataverse
):
    conn = connect(client)
    change = start_change(client, conn)
    dataverse.fail_schema_names = {"new_LoyaltyPoints"}
    dataverse.attributes.append(
        attr("new_loyaltyjoined", "DateTime", custom=True)
    )  # created meanwhile
    client.post(f"/v1/crm/changes/{change['id']}/approve", headers=ALICE)
    client.post(f"/v1/crm/changes/{change['id']}/apply", json={}, headers=ALICE)
    final = wait(
        client, f"/v1/crm/changes/{change['id']}", {"applied", "partially_applied", "failed"}
    )
    statuses = {r["schema_name"]: r["status"] for r in final["results"]}
    assert statuses == {
        "new_LoyaltyTier": "created",
        "new_LoyaltyPoints": "failed",
        "new_LoyaltyJoined": "skipped",
    }
    assert final["status"] == "partially_applied" and "1 column(s) failed" in final["error"]

    dataverse.fail_publish = True
    dataverse.fail_schema_names = set()
    second = start_change(client, conn, request="Add a preferred contact time text column please")
    client.post(f"/v1/crm/changes/{second['id']}/approve", headers=ALICE)
    client.post(f"/v1/crm/changes/{second['id']}/apply", json={}, headers=ALICE)
    final2 = wait(
        client, f"/v1/crm/changes/{second['id']}", {"applied", "partially_applied", "failed"}
    )
    assert final2["status"] == "partially_applied" and "publishing failed" in final2["error"]


def test_plan_failures_and_ownership(settings: Settings, dataverse: FakeDataverse):
    nothing = json.dumps(
        {"fields": [{"schema_name": "new_LegacyCode", "display_name": "x", "type": "text"}]}
    )
    script = Script().default("crm_plan", nothing)
    app = create_app(
        settings,
        gateway=make_gateway(script),
        runner=LocalRunner(),
        start_worker=False,
        integration_transport=dataverse.transport(),
    )
    with TestClient(app) as c:
        conn = connect(c)
        failed = start_change(c, conn)
        assert failed["status"] == "failed" and "already exists" in failed["error"]
        assert c.post(f"/v1/crm/changes/{failed['id']}/approve", headers=ALICE).status_code == 409
        assert c.get(f"/v1/crm/changes/{failed['id']}", headers=BOB).status_code == 404
        assert (
            c.post(
                f"/v1/crm/{conn}/analyses", json={"entities": ["contact"]}, headers=BOB
            ).status_code
            == 404
        )
        assert (
            c.post(
                f"/v1/crm/{conn}/changes",
                json={"entity": "contact", "request": "add a field", "publisher_prefix": "BAD"},
                headers=ALICE,
            ).status_code
            == 422
        )
        assert c.get("/v1/crm/changes", headers=BOB).json() == []

    junk = Script().default("crm_plan", "not json at all")
    app = create_app(
        settings,
        gateway=make_gateway(junk),
        runner=LocalRunner(),
        start_worker=False,
        integration_transport=dataverse.transport(),
    )
    with TestClient(app) as c:
        conn = connect(c)
        failed = start_change(c, conn)
        assert "No model returned a usable column plan" in failed["error"]


def test_restart_marks_applying_change_failed(settings: Settings, dataverse: FakeDataverse):
    script = Script().default("crm_plan", json.dumps(PLAN_B))
    app = create_app(
        settings,
        gateway=make_gateway(script),
        runner=LocalRunner(),
        start_worker=False,
        integration_transport=dataverse.transport(),
    )
    with TestClient(app) as c:
        conn = connect(c)
        change = start_change(c, conn)
        db = c.app.state.db

        async def mark_applying() -> None:
            async with db.session() as session:
                row = await session.get(CrmChange, change["id"])
                row.status = "applying"
                await session.commit()

        c.portal.call(mark_applying)
    with TestClient(app) as c:
        final = c.get(f"/v1/crm/changes/{change['id']}", headers=ALICE).json()
        assert final["status"] == "failed" and "Interrupted while applying" in final["error"]


def test_contacts_helper_is_stable():
    assert len(contacts(3)) == 3
