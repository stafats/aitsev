"""Two fake external systems served through httpx.MockTransport.

* legacy.example — an old CRM with an OpenAPI spec, bearer auth and page/per_page paging
* crm.example    — a Dynamics-style OData service with OAuth2 client credentials
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs

import httpx

LEGACY_URL = "https://legacy.example/api"
CRM_URL = "https://crm.example/api/data/v9.2"
TOKEN_URL = "https://login.example/oauth2/token"
LEGACY_TOKEN = "legacy-bearer-token"

LEGACY_OPENAPI = {
    "openapi": "3.0.3",
    "info": {"title": "Legacy CRM", "version": "1"},
    "paths": {
        "/customers": {
            "get": {
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/CustomerPage"}
                            }
                        }
                    }
                }
            },
            "post": {
                "requestBody": {
                    "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/Customer"}}
                    }
                }
            },
        },
        "/customers/{id}": {"get": {"responses": {"200": {}}}},
    },
    "components": {
        "schemas": {
            "CustomerPage": {
                "type": "object",
                "properties": {
                    "data": {"type": "array", "items": {"$ref": "#/components/schemas/Customer"}},
                    "page": {"type": "integer"},
                },
            },
            "Customer": {
                "type": "object",
                "required": ["id", "full_name"],
                "properties": {
                    "id": {"type": "integer", "readOnly": True},
                    "full_name": {"type": "string"},
                    "email_address": {"type": "string", "format": "email"},
                    "phone": {"type": "string"},
                    "company_name": {"type": "string"},
                    "signup_date": {"type": "string", "format": "date"},
                    "status": {"type": "string", "enum": ["A", "I"]},
                    "annual_revenue": {"type": "string", "description": "e.g. 1,200.50"},
                },
            },
        }
    },
}

CRM_METADATA = """<?xml version="1.0" encoding="utf-8"?>
<edmx:Edmx Version="4.0" xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx">
  <edmx:DataServices>
    <Schema Namespace="Microsoft.Dynamics.CRM" xmlns="http://docs.oasis-open.org/odata/ns/edm">
      <EntityType Name="contact">
        <Key><PropertyRef Name="contactid"/></Key>
        <Property Name="contactid" Type="Edm.Guid"/>
        <Property Name="firstname" Type="Edm.String" MaxLength="50"/>
        <Property Name="lastname" Type="Edm.String" Nullable="false" MaxLength="50"/>
        <Property Name="emailaddress1" Type="Edm.String" MaxLength="100"/>
        <Property Name="telephone1" Type="Edm.String" MaxLength="50"/>
        <Property Name="companyname" Type="Edm.String" MaxLength="100"/>
        <Property Name="new_signupdate" Type="Edm.Date"/>
        <Property Name="statecode" Type="Edm.Int32"/>
        <Property Name="revenue" Type="Edm.Decimal"/>
      </EntityType>
      <EntityContainer Name="System">
        <EntitySet Name="contacts" EntityType="Microsoft.Dynamics.CRM.contact"/>
      </EntityContainer>
    </Schema>
  </edmx:DataServices>
</edmx:Edmx>
"""


def legacy_customers(count: int) -> list[dict[str, Any]]:
    out = []
    for i in range(1, count + 1):
        out.append(
            {
                "id": i,
                "full_name": f"Asha Rao{i}",
                "email_address": f"asha{i}@example.com",
                "phone": f"+91 98765 {i:05d}",
                "company_name": f"Company {i}",
                "signup_date": "2024-05-17",
                "status": "A" if i % 2 else "I",
                "annual_revenue": "1,200.50",
            }
        )
    return out


@dataclass
class FakeSystems:
    customers: list[dict[str, Any]] = field(default_factory=lambda: legacy_customers(7))
    created: list[dict[str, Any]] = field(default_factory=list)
    requests: list[httpx.Request] = field(default_factory=list)
    token_requests: int = 0
    expire_next_token: bool = False
    fail_email_marker: str = "bad"
    legacy_503_once: bool = False

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url.startswith(TOKEN_URL):
            return self._token(request)
        if url.startswith(LEGACY_URL):
            return self._legacy(request)
        if url.startswith(CRM_URL):
            return self._crm(request)
        return httpx.Response(404, json={"error": "unknown host"})

    def _token(self, request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode())
        if form.get("client_id") != ["crm-client"] or form.get("client_secret") != ["crm-secret"]:
            return httpx.Response(401, json={"error": "invalid_client"})
        self.token_requests += 1
        return httpx.Response(
            200, json={"access_token": f"crm-token-{self.token_requests}", "expires_in": 3600}
        )

    def _legacy(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") != f"Bearer {LEGACY_TOKEN}":
            return httpx.Response(401, json={"error": "unauthorized"})
        path = request.url.path.removeprefix("/api")
        if path == "/openapi.json":
            return httpx.Response(200, json=LEGACY_OPENAPI)
        if path == "/customers" and request.method == "GET":
            if self.legacy_503_once:
                self.legacy_503_once = False
                return httpx.Response(503, headers={"Retry-After": "0"})
            page = int(request.url.params.get("page", "1"))
            size = int(request.url.params.get("per_page", "100"))
            chunk = self.customers[(page - 1) * size : page * size]
            return httpx.Response(200, json={"data": chunk, "page": page})
        return httpx.Response(404)

    def _crm(self, request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer crm-token-"):
            return httpx.Response(401, json={"error": "no token"})
        if self.expire_next_token:
            self.expire_next_token = False
            return httpx.Response(401, json={"error": "token expired"})
        path = request.url.path.removeprefix("/api/data/v9.2")
        if path == "/$metadata":
            return httpx.Response(
                200, text=CRM_METADATA, headers={"content-type": "application/xml"}
            )
        if path == "/contacts" and request.method == "POST":
            body = json.loads(request.content)
            if self.fail_email_marker and self.fail_email_marker in str(body.get("emailaddress1")):
                return httpx.Response(400, json={"error": {"message": "Invalid email"}})
            body["contactid"] = f"guid-{len(self.created) + 1}"
            self.created.append(body)
            return httpx.Response(204)
        if path == "/contacts" and request.method == "GET":
            return httpx.Response(200, json={"value": self.created[:5]})
        return httpx.Response(404)
