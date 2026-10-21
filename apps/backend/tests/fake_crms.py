"""Fake Salesforce, HubSpot and Zoho CRM APIs for tests, served through one httpx.MockTransport."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs

import httpx

SF_URL = "https://acme.my.salesforce.com/services/data/v62.0"
SF_TOKEN = "https://acme.my.salesforce.com/services/oauth2/token"
HS_URL = "https://api.hubapi.com"
ZOHO_URL = "https://www.zohoapis.in/crm/v6"
ZOHO_TOKEN = "https://accounts.zoho.in/oauth/v2/token"


def _sf_people(n: int) -> list[dict[str, Any]]:
    return [
        {
            "Id": f"003{i}",
            "Name": f"Person {i}",
            "Email": "dup@example.com" if i < 2 else f"p{i}@example.com",
            "LastName": f"P{i}",
            "Legacy__c": None,
        }
        for i in range(n)
    ]


@dataclass
class FakeCrms:
    sf_created: list[dict[str, Any]] = field(default_factory=list)
    hs_created: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    zoho_created: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    sf_fail: set[str] = field(default_factory=set)
    sf_fields: list[dict[str, Any]] = field(
        default_factory=lambda: [
            {
                "name": "Id",
                "label": "Contact ID",
                "type": "id",
                "custom": False,
                "nillable": False,
                "createable": False,
            },
            {
                "name": "Name",
                "label": "Full Name",
                "type": "string",
                "custom": False,
                "nillable": True,
                "createable": False,
            },
            {
                "name": "LastName",
                "label": "Last Name",
                "type": "string",
                "custom": False,
                "nillable": False,
                "createable": True,
                "defaultedOnCreate": False,
            },
            {
                "name": "Email",
                "label": "Email",
                "type": "email",
                "custom": False,
                "nillable": True,
                "createable": True,
            },
            {
                "name": "Legacy__c",
                "label": "Legacy",
                "type": "string",
                "custom": True,
                "nillable": True,
                "createable": True,
            },
            {
                "name": "Tier__c",
                "label": "Tier",
                "type": "picklist",
                "custom": True,
                "nillable": True,
                "createable": True,
            },
        ]
    )
    soql: list[str] = field(default_factory=list)
    hs_pages: int = 0

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith(SF_TOKEN) or url.startswith(ZOHO_TOKEN):
            form = parse_qs(request.content.decode())
            if url.startswith(ZOHO_TOKEN):
                if form.get("grant_type") != ["refresh_token"] or form.get("refresh_token") != [
                    "zr"
                ]:
                    return httpx.Response(400, json={"error": "invalid_code"})
                return httpx.Response(200, json={"access_token": "zoho-token", "expires_in": 3600})
            return httpx.Response(200, json={"access_token": "sf-token", "instance_url": "x"})
        if url.startswith(SF_URL):
            return self._salesforce(request)
        if url.startswith(HS_URL):
            return self._hubspot(request)
        if url.startswith(ZOHO_URL):
            return self._zoho(request)
        return httpx.Response(404)

    # ---------------------------------------------------------------- Salesforce

    def _salesforce(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") != "Bearer sf-token":
            return httpx.Response(401, json=[{"message": "Session expired"}])
        path = request.url.path.removeprefix("/services/data/v62.0/")
        if path == "sobjects/Contact/describe":
            return httpx.Response(200, json={"name": "Contact", "fields": self.sf_fields})
        if path.startswith("sobjects/") and path.endswith("/describe"):
            return httpx.Response(
                404, json=[{"errorCode": "NOT_FOUND", "message": "Unknown object"}]
            )
        if path == "query":
            q = request.url.params["q"]
            self.soql.append(q)
            if q.startswith("SELECT COUNT()"):
                return httpx.Response(200, json={"totalSize": 1234, "records": []})
            limit = int(q.rsplit("LIMIT", 1)[1])
            return httpx.Response(200, json={"totalSize": limit, "records": _sf_people(limit)})
        if path == "tooling/sobjects/CustomField" and request.method == "POST":
            body = json.loads(request.content)
            if body["FullName"].split(".", 1)[1] in self.sf_fail:
                return httpx.Response(400, json=[{"message": "Field name is invalid"}])
            self.sf_created.append(body)
            return httpx.Response(201, json={"id": "00N1", "success": True})
        return httpx.Response(404)

    # ---------------------------------------------------------------- HubSpot

    def _hubspot(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") != "Bearer hs-token":
            return httpx.Response(401, json={"message": "Authentication credentials not found"})
        path = request.url.path.lstrip("/")
        if path == "crm/v3/properties/deals" and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "name": "dealname",
                            "label": "Deal Name",
                            "type": "string",
                            "fieldType": "text",
                            "hubspotDefined": True,
                        },
                        {
                            "name": "amount",
                            "label": "Amount",
                            "type": "number",
                            "fieldType": "number",
                            "hubspotDefined": True,
                        },
                        {
                            "name": "closedate",
                            "label": "Close Date",
                            "type": "datetime",
                            "fieldType": "date",
                            "hubspotDefined": True,
                        },
                        {
                            "name": "legacy_ref",
                            "label": "Legacy Ref",
                            "type": "string",
                            "fieldType": "text",
                        },
                        {
                            "name": "is_vip",
                            "label": "VIP",
                            "type": "enumeration",
                            "fieldType": "booleancheckbox",
                        },
                    ]
                },
            )
        if path == "crm/v3/properties/deals" and request.method == "POST":
            body = json.loads(request.content)
            self.hs_created.append(("deals", body))
            return httpx.Response(201, json=body)
        if path == "crm/v3/objects/deals" and request.method == "GET":
            self.hs_pages += 1
            after = int(request.url.params.get("after", "0"))
            limit = int(request.url.params["limit"])
            assert "amount" in request.url.params["properties"]
            rows = [
                {
                    "id": str(i),
                    "properties": {
                        "dealname": "Same deal" if i < 3 else f"Deal {i}",
                        "amount": str(i * 10),
                        "legacy_ref": None,
                    },
                }
                for i in range(after, min(after + limit, 150))
            ]
            body: dict[str, Any] = {"results": rows}
            if after + limit < 150:
                body["paging"] = {"next": {"after": str(after + limit)}}
            return httpx.Response(200, json=body)
        if path == "crm/v3/objects/deals/search":
            return httpx.Response(200, json={"total": 150, "results": []})
        return httpx.Response(404, json={"message": "not found"})

    # ---------------------------------------------------------------- Zoho

    def _zoho(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") != "Zoho-oauthtoken zoho-token":
            return httpx.Response(401, json={"code": "INVALID_TOKEN"})
        path = request.url.path.removeprefix("/crm/v6/")
        if path == "settings/fields" and request.method == "GET":
            if request.url.params.get("module") != "Leads":
                return httpx.Response(400, json={"code": "INVALID_MODULE"})
            return httpx.Response(
                200,
                json={
                    "fields": [
                        {
                            "api_name": "Last_Name",
                            "field_label": "Last Name",
                            "data_type": "text",
                            "system_mandatory": True,
                        },
                        {"api_name": "Email", "field_label": "Email", "data_type": "email"},
                        {
                            "api_name": "Annual_Revenue",
                            "field_label": "Annual Revenue",
                            "data_type": "currency",
                        },
                        {
                            "api_name": "Old_Code",
                            "field_label": "Old Code",
                            "data_type": "text",
                            "custom_field": True,
                        },
                    ]
                },
            )
        if path == "settings/fields" and request.method == "POST":
            self.zoho_created.append((request.url.params["module"], json.loads(request.content)))
            return httpx.Response(201, json={"fields": [{"code": "SUCCESS"}]})
        if path == "Leads" and request.method == "GET":
            page = int(request.url.params.get("page", "1"))
            rows = [
                {
                    "id": f"{page}-{i}",
                    "Last_Name": None if i == 0 else f"L{i}",
                    "Email": f"{page}{i}@x.in",
                    "Old_Code": None,
                }
                for i in range(int(request.url.params["per_page"]))
            ]
            return httpx.Response(200, json={"data": rows, "info": {"more_records": page < 2}})
        if path == "Leads/actions/count":
            return httpx.Response(200, json={"count": 380})
        return httpx.Response(404)
