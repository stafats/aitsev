"""CRM pack on Salesforce, HubSpot and Zoho CRM: analysis, AI field design and creation."""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from aitsev.crm.dynamics import FieldSpec
from aitsev.crm.platforms import PLATFORMS, detect, platform_for, zoho_api_name
from aitsev.main import create_app
from aitsev.pipeline.sandbox import LocalRunner
from tests.fake_crms import HS_URL, SF_TOKEN, SF_URL, ZOHO_TOKEN, ZOHO_URL, FakeCrms
from tests.fakes import Script, make_gateway
from tests.test_crm import ALICE, wait

SF_PLAN = {
    "fields": [
        {
            "schema_name": "Loyalty_Tier__c",
            "display_name": "Loyalty Tier",
            "type": "choice",
            "options": [{"label": "Bronze"}, {"label": "Gold"}],
        },
        {"schema_name": "Points__c", "display_name": "Points", "type": "whole_number"},
        {"schema_name": "Tier__c", "display_name": "Tier", "type": "text"},  # exists
        {"schema_name": "Bad_Name", "display_name": "Bad", "type": "text"},  # no __c
    ]
}
HS_PLAN = {
    "fields": [
        {"schema_name": "renewal_date", "display_name": "Renewal Date", "type": "date"},
        {
            "schema_name": "contract_type",
            "display_name": "Contract Type",
            "type": "choice",
            "options": [{"label": "Annual plan"}, {"label": "Monthly"}],
        },
        {"schema_name": "Not_Snake", "display_name": "Bad", "type": "text"},
        {"schema_name": "is_partner", "display_name": "Partner", "type": "boolean"},
    ]
}
ZOHO_PLAN = {
    "fields": [
        {
            "schema_name": "Lead_Score",
            "display_name": "Lead Score",
            "type": "decimal",
            "precision": 1,
        },
        {"schema_name": "Wrong", "display_name": "Source Detail", "type": "memo"},  # name ≠ label
        {"schema_name": "Email", "display_name": "Email", "type": "text"},  # exists
    ]
}


@pytest.fixture
def crms() -> FakeCrms:
    return FakeCrms()


@pytest.fixture
def client(settings, crms) -> Iterator[TestClient]:
    script = Script().default(
        "crm_plan", lambda req: json.dumps(_plan_for(req.messages[0].content))
    )
    app = create_app(
        settings,
        gateway=make_gateway(script),
        runner=LocalRunner(),
        start_worker=False,
        integration_transport=crms.transport(),
    )
    with TestClient(app) as c:
        yield c


def _plan_for(system: str) -> dict:
    if "Salesforce" in system:
        return SF_PLAN
    if "HubSpot" in system:
        return HS_PLAN
    return ZOHO_PLAN


def connect(client: TestClient, base_url: str, auth: dict, credentials: dict, **extra) -> dict:
    response = client.post(
        "/v1/connections",
        json={
            "name": base_url,
            "base_url": base_url,
            "auth": auth,
            "credentials": credentials,
            "spec_format": "samples",
            "spec_text": "{}",
            "environment": "sandbox",
            **extra,
        },
        headers=ALICE,
    )
    assert response.status_code == 201, response.text
    return response.json()


def analyze(client: TestClient, conn: str, entity: str, sample: int = 50) -> dict:
    started = client.post(
        f"/v1/crm/{conn}/analyses",
        json={"entities": [entity], "sample_size": sample},
        headers=ALICE,
    )
    assert started.status_code == 202, started.text
    done = wait(client, f"/v1/crm/analyses/{started.json()['id']}", {"done", "failed"})
    assert done["status"] == "done", done["error"]
    return done["report"][0]


def plan_and_apply(client: TestClient, conn: str, entity: str) -> tuple[dict, dict]:
    started = client.post(
        f"/v1/crm/{conn}/changes",
        json={"entity": entity, "request": "Add the fields we need for renewals"},
        headers=ALICE,
    )
    assert started.status_code == 202, started.text
    change = wait(client, f"/v1/crm/changes/{started.json()['id']}", {"proposed", "failed"})
    assert change["status"] == "proposed", change["error"]
    assert client.post(f"/v1/crm/changes/{change['id']}/approve", headers=ALICE).status_code == 200
    client.post(f"/v1/crm/changes/{change['id']}/apply", json={}, headers=ALICE)
    final = wait(
        client, f"/v1/crm/changes/{change['id']}", {"applied", "partially_applied", "failed"}
    )
    return change, final


# ------------------------------------------------------------------ unit


def test_platform_detection():
    assert detect(SF_URL) == "salesforce"
    assert detect(HS_URL) == "hubspot"
    assert detect(ZOHO_URL) == "zoho"
    assert detect("https://org.crm8.dynamics.com/api/data/v9.2") == "dynamics"
    assert platform_for("zoho", SF_URL).id == "zoho"  # an explicit choice wins
    assert platform_for("auto", HS_URL).id == "hubspot"


def test_name_rules_per_platform():
    def spec(name: str, label: str = "Loyalty Tier", **kw) -> FieldSpec:
        return FieldSpec(schema_name=name, display_name=label, type=kw.pop("type", "text"), **kw)

    sf, hs, zoho = PLATFORMS["salesforce"], PLATFORMS["hubspot"], PLATFORMS["zoho"]
    assert sf.name_problem(spec("Loyalty_Tier__c"), "") is None
    for bad in ("Loyalty", "Loyalty__Tier__c", "Tier_c"):
        assert sf.name_problem(spec(bad), "") is not None
    assert sf.name_problem(spec("Notes__c", max_length=1000), "") is not None
    assert hs.name_problem(spec("loyalty_tier"), "") is None
    assert hs.name_problem(spec("LoyaltyTier"), "") is not None
    assert zoho_api_name("Loyalty tier (2024)") == "Loyalty_Tier_2024"
    assert zoho.name_problem(spec("Loyalty_Tier"), "") is None
    assert zoho.name_problem(spec("Tier"), "") is not None


def test_payloads_per_platform():
    choice = FieldSpec(
        schema_name="Tier__c",
        display_name="Tier",
        type="choice",
        options=[{"label": "Gold plan"}, {"label": "Silver"}],
    )
    sf = PLATFORMS["salesforce"].payload(choice, 10000)
    assert sf["Metadata"]["type"] == "Picklist"
    assert [v["fullName"] for v in sf["Metadata"]["valueSet"]["valueSetDefinition"]["value"]] == [
        "Gold plan",
        "Silver",
    ]
    hs = PLATFORMS["hubspot"].payload(choice, 10000)
    assert hs["fieldType"] == "select" and hs["options"][0]["value"] == "gold_plan"
    zoho = PLATFORMS["zoho"].payload(choice, 10000)
    assert zoho["fields"][0]["data_type"] == "picklist"
    for kind in ("text", "memo", "whole_number", "decimal", "boolean", "date", "datetime"):
        spec = FieldSpec(schema_name="X__c", display_name="X", type=kind)
        assert PLATFORMS["salesforce"].payload(spec, 1)["Metadata"]["type"]
        assert PLATFORMS["hubspot"].payload(spec, 1)["type"]
        assert PLATFORMS["zoho"].payload(spec, 1)["fields"][0]["data_type"]


# ------------------------------------------------------------------ Salesforce


def test_salesforce_analysis_and_fields(client, crms):
    conn = connect(
        client,
        SF_URL,
        {"type": "oauth2_client_credentials", "token_url": SF_TOKEN},
        {"client_id": "sf", "client_secret": "secret"},
    )
    assert conn["platform"] == "salesforce"
    report = analyze(client, conn["id"], "Contact", 40)
    assert report["total_records"] == 1234 and report["sampled"] == 40
    assert "Legacy__c" in report["unused_custom_fields"]
    assert report["duplicate_groups"][0]["field"] == "Email"
    assert crms.soql[0].startswith(
        "SELECT Id, Name, LastName, Email, Legacy__c, Tier__c FROM Contact LIMIT 40"
    )

    change, final = plan_and_apply(client, conn["id"], "Contact")
    assert change["platform"] == "salesforce"
    names = [i["spec"]["schema_name"] for i in change["items"]]
    assert names == ["Loyalty_Tier__c", "Points__c"]  # existing and badly named dropped
    assert change["items"][0]["payload"]["Metadata"]["type"] == "Picklist"
    assert final["status"] == "applied", final["error"]
    assert [c["FullName"] for c in crms.sf_created] == [
        "Contact.Loyalty_Tier__c",
        "Contact.Points__c",
    ]
    assert "field-level security" in final["results"][0]["message"]

    bad = client.post(
        f"/v1/crm/{conn['id']}/analyses", json={"entities": ["Contact; DROP"]}, headers=ALICE
    )
    assert bad.status_code == 422


def test_salesforce_partial_failure(client, crms):
    crms.sf_fail.add("Points__c")
    conn = connect(
        client,
        SF_URL,
        {"type": "oauth2_client_credentials", "token_url": SF_TOKEN},
        {"client_id": "sf", "client_secret": "secret"},
    )
    _change, final = plan_and_apply(client, conn["id"], "Contact")
    assert final["status"] == "partially_applied"
    assert [r["status"] for r in final["results"]] == ["created", "failed"]


# ------------------------------------------------------------------ HubSpot


def test_hubspot_analysis_and_properties(client, crms):
    conn = connect(client, HS_URL, {"type": "bearer"}, {"token": "hs-token"})
    assert conn["platform"] == "hubspot"
    report = analyze(client, conn["id"], "deals", 120)
    assert report["sampled"] == 120 and report["total_records"] == 150
    assert crms.hs_pages == 2  # 100 + 20, following paging.next.after
    assert report["unused_custom_fields"] == ["legacy_ref", "is_vip"]
    assert report["duplicate_groups"][0] == {"field": "dealname", "value": "samedeal", "count": 3}

    change, final = plan_and_apply(client, conn["id"], "deals")
    assert [i["spec"]["schema_name"] for i in change["items"]] == [
        "contract_type",
        "is_partner",
        "renewal_date",
    ]
    assert final["status"] == "applied", final["error"]
    created = {body["name"]: body for _obj, body in crms.hs_created}
    assert (
        created["renewal_date"]["type"] == "date"
        and created["renewal_date"]["groupName"] == "dealinformation"
    )
    assert created["contract_type"]["options"][0]["value"] == "annual_plan"
    assert created["is_partner"]["fieldType"] == "booleancheckbox"


def test_hubspot_review_enforces_names(client):
    conn = connect(client, HS_URL, {"type": "bearer"}, {"token": "hs-token"})
    started = client.post(
        f"/v1/crm/{conn['id']}/changes",
        json={"entity": "deals", "request": "Renewal fields"},
        headers=ALICE,
    ).json()
    change = wait(client, f"/v1/crm/changes/{started['id']}", {"proposed", "failed"})
    item = change["items"][0]
    bad = {"spec": {**item["spec"], "schema_name": "Renewal_Date"}, "include": True}
    response = client.put(f"/v1/crm/changes/{change['id']}", json={"items": [bad]}, headers=ALICE)
    assert response.status_code == 422 and "snake_case" in response.text


# ------------------------------------------------------------------ Zoho


def test_zoho_refresh_token_analysis_and_fields(client, crms):
    conn = connect(
        client,
        ZOHO_URL,
        {
            "type": "oauth2_refresh_token",
            "token_url": ZOHO_TOKEN,
            "token_prefix": "Zoho-oauthtoken",
        },
        {"client_id": "z", "client_secret": "zs", "refresh_token": "zr"},
    )
    assert conn["platform"] == "zoho"
    report = analyze(client, conn["id"], "Leads", 300)
    assert report["sampled"] == 300 and report["total_records"] == 380
    assert report["unused_custom_fields"] == ["Old_Code"]
    assert "Last_Name" in report["required_but_empty"]

    change, final = plan_and_apply(client, conn["id"], "Leads")
    assert [i["spec"]["schema_name"] for i in change["items"]] == ["Lead_Score"]
    assert final["status"] == "applied", final["error"]
    module, body = crms.zoho_created[0]
    assert module == "Leads"
    assert body["fields"][0] == {
        "field_label": "Lead Score",
        "data_type": "double",
        "decimal_place": 1,
    }


def test_zoho_bad_refresh_token_fails_cleanly(client):
    conn = connect(
        client,
        ZOHO_URL,
        {
            "type": "oauth2_refresh_token",
            "token_url": ZOHO_TOKEN,
            "token_prefix": "Zoho-oauthtoken",
        },
        {"client_id": "z", "client_secret": "zs", "refresh_token": "wrong"},
    )
    started = client.post(
        f"/v1/crm/{conn['id']}/analyses", json={"entities": ["Leads"]}, headers=ALICE
    ).json()
    failed = wait(client, f"/v1/crm/analyses/{started['id']}", {"done", "failed"})
    assert failed["status"] == "failed" and "Token request failed" in failed["error"]


def test_explicit_platform_overrides_detection(client):
    conn = connect(
        client,
        "https://crm.internal.example/api",
        {"type": "bearer"},
        {"token": "x"},
        platform="hubspot",
    )
    assert conn["platform"] == "hubspot"
    auto = connect(client, "https://crm.internal.example/api", {"type": "bearer"}, {"token": "x"})
    assert auto["platform"] == "dynamics"
