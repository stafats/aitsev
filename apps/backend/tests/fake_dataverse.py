"""A fake Dataverse Web API (Dynamics 365) for tests, served through httpx.MockTransport."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, unquote

import httpx

ORG_URL = "https://contoso.crm.example/api/data/v9.2"
TOKEN_URL = "https://login.example/oauth2/token"

VALID_TYPES = {
    "Microsoft.Dynamics.CRM.StringAttributeMetadata": "String",
    "Microsoft.Dynamics.CRM.MemoAttributeMetadata": "Memo",
    "Microsoft.Dynamics.CRM.IntegerAttributeMetadata": "Integer",
    "Microsoft.Dynamics.CRM.DecimalAttributeMetadata": "Decimal",
    "Microsoft.Dynamics.CRM.BooleanAttributeMetadata": "Boolean",
    "Microsoft.Dynamics.CRM.DateTimeAttributeMetadata": "DateTime",
    "Microsoft.Dynamics.CRM.PicklistAttributeMetadata": "Picklist",
}


def attr(
    name: str, kind: str, custom: bool = False, required: str = "None", label: str = ""
) -> dict:
    return {
        "LogicalName": name,
        "SchemaName": name,
        "AttributeType": kind,
        "IsCustomAttribute": custom,
        "RequiredLevel": {"Value": required},
        "IsValidForCreate": True,
        "DisplayName": {"UserLocalizedLabel": {"Label": label or name}},
    }


def contacts(count: int) -> list[dict[str, Any]]:
    out = []
    for i in range(count):
        out.append(
            {
                "contactid": f"c{i}",
                "fullname": f"Person {i}",
                "lastname": f"Person{i}" if i % 4 else None,  # 25% empty although required
                "emailaddress1": "dup@example.com" if i < 3 else f"p{i}@example.com",
                "telephone1": "555" if i == 0 else None,  # 1 in N filled -> low fill
                "new_legacycode": None,  # unused custom column
                "_parentcustomerid_value": f"acc{i}",
            }
        )
    return out


@dataclass
class FakeDataverse:
    records: list[dict[str, Any]] = field(default_factory=lambda: contacts(20))
    attributes: list[dict[str, Any]] = field(
        default_factory=lambda: [
            attr("contactid", "Uniqueidentifier"),
            attr("fullname", "String", label="Full Name"),
            attr("lastname", "String", required="ApplicationRequired", label="Last Name"),
            attr("emailaddress1", "String", label="Email"),
            attr("telephone1", "String", label="Business Phone"),
            attr("new_legacycode", "String", custom=True, label="Legacy Code"),
            attr("parentcustomerid", "Customer", label="Company Name"),
        ]
    )
    created: list[dict[str, Any]] = field(default_factory=list)
    solution_headers: list[str | None] = field(default_factory=list)
    published: list[str] = field(default_factory=list)
    fail_schema_names: set[str] = field(default_factory=set)
    fail_publish: bool = False

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith(TOKEN_URL):
            form = parse_qs(request.content.decode())
            if form.get("client_secret") != ["dv-secret"]:
                return httpx.Response(401, json={"error": "invalid_client"})
            return httpx.Response(200, json={"access_token": "dv-token", "expires_in": 3600})
        if not url.startswith(ORG_URL):
            return httpx.Response(404)
        if request.headers.get("authorization") != "Bearer dv-token":
            return httpx.Response(401)
        path = unquote(request.url.path.removeprefix("/api/data/v9.2/"))
        entity = re.fullmatch(r"EntityDefinitions\(LogicalName='([a-z_]+)'\)(/Attributes)?", path)
        if entity:
            name, sub = entity.group(1), entity.group(2)
            if name != "contact":
                return httpx.Response(404, json={"error": {"message": "Not found"}})
            if not sub:
                return httpx.Response(
                    200,
                    json={
                        "LogicalName": "contact",
                        "EntitySetName": "contacts",
                        "PrimaryIdAttribute": "contactid",
                        "PrimaryNameAttribute": "fullname",
                        "DisplayName": {"UserLocalizedLabel": {"Label": "Contact"}},
                    },
                )
            if request.method == "GET":
                return httpx.Response(200, json={"value": self.attributes})
            return self._create_attribute(request)
        if path == "contacts" and request.method == "GET":
            top = int(request.url.params.get("$top", "5000"))
            return httpx.Response(
                200, json={"value": self.records[:top], "@odata.count": len(self.records)}
            )
        if path == "PublishXml" and request.method == "POST":
            if self.fail_publish:
                return httpx.Response(500, json={"error": {"message": "publish failed"}})
            body = json.loads(request.content)
            self.published.append(body["ParameterXml"])
            return httpx.Response(204)
        return httpx.Response(404)

    def _create_attribute(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        kind = VALID_TYPES.get(body.get("@odata.type", ""))
        schema = body.get("SchemaName", "")
        if kind is None or body.get("AttributeType") != kind:
            return httpx.Response(400, json={"error": {"message": "Bad attribute type"}})
        if not body.get("DisplayName", {}).get("LocalizedLabels"):
            return httpx.Response(400, json={"error": {"message": "DisplayName is required"}})
        if schema in self.fail_schema_names:
            return httpx.Response(400, json={"error": {"message": f"{schema} is invalid"}})
        if any(a["LogicalName"] == schema.lower() for a in self.attributes):
            return httpx.Response(400, json={"error": {"message": "Duplicate"}})
        self.created.append(body)
        self.solution_headers.append(request.headers.get("mscrm.solutionuniquename"))
        self.attributes.append(attr(schema.lower(), kind, custom=True))
        return httpx.Response(204, headers={"OData-EntityId": f"{ORG_URL}/attributes(guid)"})
