from __future__ import annotations

import json
import runpy
from typing import Any

import httpx
import pytest
import yaml
from pydantic import SecretStr, ValidationError

from aitsev.config import Settings
from aitsev.integrations.codegen import export_script, script_name
from aitsev.integrations.crypto import SecretBox, SecretBoxError
from aitsev.integrations.http import (
    ApiClient,
    AuthConfig,
    BlockedAddressError,
    Credentials,
    IntegrationError,
    Pagination,
    check_url_allowed,
)
from aitsev.integrations.mapping import (
    FieldMapping,
    blocking_issues,
    combine,
    default_steps,
    name_score,
    normalize_name,
    rule_based,
)
from aitsev.integrations.records import transform_record, validate_record
from aitsev.integrations.schema import (
    EntityDef,
    FieldDef,
    SchemaParseError,
    infer_from_samples,
    parse_odata,
    parse_openapi,
)
from aitsev.integrations.service import parse_spec
from aitsev.integrations.transforms import STEPS_ADAPTER, TransformError, read_path, run_steps
from tests.fake_systems import CRM_METADATA, LEGACY_OPENAPI


def steps(*items: dict[str, Any]):
    return STEPS_ADAPTER.validate_python(list(items))


# ------------------------------------------------------------------ schema


def test_parse_openapi_entities_paths_and_types():
    schema = parse_openapi(LEGACY_OPENAPI)
    customer = schema.entity("Customer")
    assert customer is not None
    assert (customer.list_path, customer.create_path, customer.records_key) == (
        "/customers",
        "/customers",
        "data",
    )
    types = {f.name: (f.type, f.required, f.read_only) for f in customer.fields}
    assert types["id"] == ("integer", True, True)
    assert types["signup_date"] == ("date", False, False)
    assert customer.field("status").enum == ["A", "I"]


def test_parse_openapi_yaml_allof_nullable_and_swagger_definitions():
    doc = {
        "openapi": "3.1.0",
        "paths": {
            "/things": {
                "get": {
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "array",
                                        "items": {"$ref": "#/components/schemas/Thing"},
                                    }
                                }
                            }
                        }
                    }
                }
            }
        },
        "components": {
            "schemas": {
                "Base": {
                    "type": "object",
                    "required": ["id"],
                    "properties": {"id": {"type": "string"}},
                },
                "Thing": {
                    "allOf": [
                        {"$ref": "#/components/schemas/Base"},
                        {
                            "properties": {
                                "when": {"type": ["string", "null"], "format": "date-time"}
                            }
                        },
                    ]
                },
            }
        },
    }
    schema = parse_spec("openapi", yaml.safe_dump(doc))
    thing = schema.entity("Thing")
    assert thing.list_path == "/things" and thing.records_key is None
    assert {f.name: f.type for f in thing.fields} == {"id": "string", "when": "datetime"}
    swagger = parse_openapi(
        {"swagger": "2.0", "definitions": {"Pet": {"properties": {"name": {"type": "string"}}}}}
    )
    assert swagger.entities[0].name == "Pet"


@pytest.mark.parametrize(
    "doc",
    [
        {"no": "version"},
        {"openapi": "3.0.0", "components": {"schemas": {}}},
        {"openapi": "3.0.0", "components": {"schemas": {"A": {"$ref": "other.yaml#/A"}}}},
        {"openapi": "3.0.0", "components": {"schemas": {"A": {"type": "string"}}}},
    ],
)
def test_parse_openapi_errors(doc):
    with pytest.raises(SchemaParseError):
        parse_openapi(doc)


def test_parse_odata_and_xml_attacks():
    schema = parse_odata(CRM_METADATA)
    contact = schema.entity("contact")
    assert (
        contact.key == "contactid"
        and contact.list_path == "/contacts"
        and contact.records_key == "value"
    )
    assert contact.field("new_signupdate").type == "date"
    assert contact.field("revenue").type == "number"
    assert contact.field("lastname").required is True
    with pytest.raises(SchemaParseError):
        parse_odata("<not-xml")
    with pytest.raises(SchemaParseError):
        parse_odata("<Edmx></Edmx>")
    billion_laughs = (
        '<?xml version="1.0"?><!DOCTYPE l [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;">]><r>&b;</r>'
    )
    with pytest.raises(SchemaParseError, match="Unsafe XML"):
        parse_odata(billion_laughs)


def test_infer_from_samples_and_samples_spec():
    entity = infer_from_samples(
        "Order",
        [
            {
                "id": 1,
                "total": 9.5,
                "paid": True,
                "on": "2024-01-02",
                "at": "2024-01-02T10:00:00Z",
                "note": None,
            },
            {
                "id": 2,
                "total": 3,
                "paid": False,
                "on": "2024-02-03",
                "at": "2024-02-03 11:00",
                "note": "x",
            },
        ],
    )
    assert {f.name: f.type for f in entity.fields} == {
        "id": "integer",
        "total": "number",
        "paid": "boolean",
        "on": "date",
        "at": "datetime",
        "note": "string",
    }
    assert entity.key == "id" and entity.field("note").required is False
    spec = json.dumps({"Order": {"list_path": "/orders", "records": [{"id": 1}]}})
    assert parse_spec("samples", spec).entity("Order").list_path == "/orders"
    for bad in ("[]", "{}", '{"A": 1}', "nope"):
        with pytest.raises(SchemaParseError):
            parse_spec("samples", bad)
    with pytest.raises(SchemaParseError):
        infer_from_samples("X", [])
    with pytest.raises(SchemaParseError):
        parse_spec("graphql", "{}")


# ------------------------------------------------------------------ transforms


@pytest.mark.parametrize(
    ("step_list", "value", "expected"),
    [
        ([{"op": "trim"}, {"op": "lower"}], "  MiXeD ", "mixed"),
        ([{"op": "upper"}], "in", "IN"),
        ([{"op": "to_int"}], "1,234", 1234),
        ([{"op": "to_int"}], True, 1),
        ([{"op": "to_float"}], "1,200.50", 1200.5),
        ([{"op": "to_bool"}], "Yes", True),
        ([{"op": "to_bool"}], "off", False),
        ([{"op": "to_string"}], 12, "12"),
        ([{"op": "to_date", "input_format": "%d/%m/%Y"}], "21/10/2026", "2026-10-21"),
        ([{"op": "to_date"}], "2026-10-21T10:00:00Z", "2026-10-21"),
        ([{"op": "to_datetime"}], "2026-10-21T10:00:00Z", "2026-10-21T10:00:00+00:00"),
        ([{"op": "split", "sep": " ", "index": -1}], "Asha Devi Rao", "Rao"),
        ([{"op": "split", "sep": ",", "index": 5}], "a,b", None),
        ([{"op": "truncate", "length": 3}], "abcdef", "abc"),
        ([{"op": "map_values", "values": {"A": 0}, "default": 9}], "B", 9),
        ([{"op": "default", "value": "n/a"}], "  ", "n/a"),
        ([{"op": "default", "value": "n/a"}, {"op": "upper"}], None, "N/A"),
        ([{"op": "prefix", "text": "+91 "}], "98765", "+91 98765"),
        ([{"op": "upper"}], None, None),
    ],
)
def test_transform_steps(step_list, value, expected):
    assert run_steps(steps(*step_list), value) == expected


@pytest.mark.parametrize(
    ("step_list", "value"),
    [
        ([{"op": "to_int"}], "1.5"),
        ([{"op": "to_int"}], "abc"),
        ([{"op": "to_float"}], "x"),
        ([{"op": "to_bool"}], "maybe"),
        ([{"op": "to_date", "input_format": "%Y"}], "21/10"),
        ([{"op": "map_values", "values": {}, "strict": True}], "Z"),
    ],
)
def test_transform_errors(step_list, value):
    with pytest.raises(TransformError):
        run_steps(steps(*step_list), value)


def test_unknown_steps_are_rejected_and_paths_read():
    with pytest.raises(ValidationError):
        steps({"op": "eval", "code": "__import__('os')"})
    assert read_path({"a": {"b": {"c": 1}}}, "a.b.c") == 1
    assert read_path({"a": 1}, "a.b") is None


# ------------------------------------------------------------------ mapping


def test_name_matching():
    assert normalize_name("new_SignupDate") == "signupdate"
    assert normalize_name("Annual_Revenue__c") == "annualrevenue"
    assert name_score("email_address", "emailaddress1") == (0.92, "name")
    assert name_score("Company Name", "companyname") == (1.0, "exact")
    assert name_score("zip", "postalcode")[0] == 0.92
    assert name_score("color", "telephone1")[0] < 0.6


def entity(name: str, *fields: FieldDef, key: str | None = None) -> EntityDef:
    return EntityDef(name=name, fields=list(fields), key=key)


def test_rule_based_is_one_to_one_with_type_steps():
    source = entity(
        "S",
        FieldDef(name="email"),
        FieldDef(name="mail"),
        FieldDef(name="age", type="string"),
        FieldDef(name="note"),
    )
    target = entity(
        "T",
        FieldDef(name="id"),
        FieldDef(name="emailaddress1"),
        FieldDef(name="age", type="integer"),
        FieldDef(name="notes", max_length=10),
        key="id",
    )
    rules = rule_based(source, target)
    assert set(rules) == {"emailaddress1", "age", "notes"}
    assert rules["emailaddress1"].source in {"email", "mail"}
    assert [s.op for s in rules["age"].steps] == ["to_int"]
    assert [s.op for s in rules["notes"].steps] == ["truncate"]
    assert "id" not in rules
    assert [
        s.op
        for s in default_steps(FieldDef(name="a", type="datetime"), FieldDef(name="b", type="date"))
    ] == ["to_date"]


def fm(target: str, source: str, conf: float = 0.8, method: str = "llm") -> FieldMapping:
    return FieldMapping(target=target, source=source, confidence=conf, method=method)


def test_combine_agreement_disagreement_and_rules():
    target = entity(
        "T", *(FieldDef(name=n, required=n == "a") for n in ["a", "b", "c", "d", "e", "f"])
    )
    rules = {
        "d": fm("d", "dd", 0.95, "name"),
        "e": fm("e", "ee", 0.92, "name"),
        "f": fm("f", "ff", 0.7, "name"),
    }
    opinions = [
        (
            "p/m1",
            {"a": fm("a", "x"), "b": fm("b", "y"), "c": fm("c", "z"), "e": fm("e", "other", 0.9)},
        ),
        ("p/m2", {"a": fm("a", "x", 0.85), "b": fm("b", "q", 0.9), "f": fm("f", "ff", 0.8)}),
    ]
    merged = {m.target: m for m in combine(target, rules, opinions)}
    assert (
        merged["a"].method == "agreed"
        and merged["a"].confidence == 0.95
        and merged["a"].status == "approved"
    )
    assert (
        merged["b"].confidence == 0.5
        and "disagree" in merged["b"].reason
        and merged["b"].source == "q"
    )
    assert merged["c"].confidence == 0.7 and merged["c"].status == "proposed"
    assert merged["d"].source == "dd" and merged["d"].status == "approved"
    assert merged["e"].source == "ee" and merged["e"].confidence == 0.6  # rule kept, but flagged
    assert merged["f"].source == "ff"
    issues = blocking_issues(target, list(merged.values()))
    assert any("need review" in i for i in issues)
    approved_all = [m.model_copy(update={"status": "approved"}) for m in merged.values()]
    assert blocking_issues(target, approved_all) == []
    assert blocking_issues(target, [m for m in approved_all if m.target != "a"]) == [
        "Required field 'a' has no approved mapping"
    ]


def test_transform_and_validate_records():
    target = entity(
        "T",
        FieldDef(name="name", required=True, max_length=5),
        FieldDef(name="n", type="integer"),
        FieldDef(name="when", type="date"),
        FieldDef(name="kind", enum=["x", "y"]),
        FieldDef(name="flag", type="boolean"),
        FieldDef(name="amount", type="number"),
    )
    fields = [
        FieldMapping(target="name", sources=["first", "last"], separator=" ", status="approved"),
        FieldMapping(target="n", source="num", steps=steps({"op": "to_int"}), status="approved"),
        FieldMapping(target="when", source="d", status="approved"),
        FieldMapping(target="kind", constant="z", status="approved"),
        FieldMapping(target="flag", source="f", status="rejected"),
        FieldMapping(target="amount", source="amount", status="proposed"),
    ]
    out, errors = transform_record(
        {"first": "Al", "last": "Brown", "num": "x", "d": "2024/1/1"}, fields
    )
    assert out == {"name": "Al Brown", "when": "2024/1/1", "kind": "z"}
    assert errors == ["n: Not a number: 'x'"]
    problems = validate_record(out, target)
    assert "name: longer than 5 characters" in problems
    assert any(p.startswith("when: expected date") for p in problems)
    assert "kind: 'z' is not one of the allowed values" in problems
    assert validate_record({}, target) == ["name: required value is missing"]
    assert validate_record({"name": "ok", "n": True, "flag": "yes", "amount": "1"}, target) == [
        "n: expected integer, got bool True",
        "flag: expected boolean, got str 'yes'",
        "amount: expected number, got str '1'",
    ]


# ------------------------------------------------------------------ crypto


def test_secret_box():
    box = SecretBox("k" * 40)
    token = box.encrypt({"token": "abc"})
    assert "abc" not in token and box.decrypt(token) == {"token": "abc"}
    with pytest.raises(SecretBoxError, match="changed"):
        SecretBox("z" * 40).decrypt(token)
    with pytest.raises(SecretBoxError):
        SecretBox("short")
    dev = SecretBox.from_settings(Settings(_env_file=None, environment="development"))
    assert dev.decrypt(dev.encrypt({"a": 1})) == {"a": 1}
    with pytest.raises(SecretBoxError):
        SecretBox.from_settings(Settings(_env_file=None, environment="production"))
    prod = SecretBox.from_settings(
        Settings(_env_file=None, environment="production", secret_key=SecretStr("s" * 40))
    )
    assert prod.decrypt(prod.encrypt({"b": 2})) == {"b": 2}


# ------------------------------------------------------------------ SSRF guard


async def resolver_for(mapping: dict[str, list[str]]):
    async def resolve(host: str) -> list[str]:
        if host not in mapping:
            raise OSError("no such host")
        return mapping[host]

    return resolve


@pytest.mark.parametrize(
    ("url", "addresses"),
    [
        ("https://internal.example/x", ["10.0.0.5"]),
        ("https://local.example/x", ["127.0.0.1"]),
        ("https://meta.example/x", ["169.254.169.254"]),
        ("https://v6.example/x", ["::1"]),
        ("https://mixed.example/x", ["8.8.8.8", "192.168.1.10"]),
    ],
)
async def test_private_addresses_blocked(url, addresses):
    resolve = await resolver_for({url.split("/")[2]: addresses})
    with pytest.raises(BlockedAddressError):
        await check_url_allowed(url, allow_private=False, resolver=resolve)


async def test_url_guard_allows_public_https_and_blocks_other_schemes():
    resolve = await resolver_for({"api.example": ["93.184.216.34"]})
    await check_url_allowed("https://api.example/v1", allow_private=False, resolver=resolve)
    for url in (
        "http://api.example/v1",
        "ftp://api.example",
        "file:///etc/passwd",
        "https:///nohost",
    ):
        with pytest.raises(BlockedAddressError):
            await check_url_allowed(url, allow_private=False, resolver=resolve)
    with pytest.raises(IntegrationError, match="resolve"):
        await check_url_allowed("https://unknown.example", allow_private=False, resolver=resolve)
    await check_url_allowed("http://localhost:8080", allow_private=True)


# ------------------------------------------------------------------ API client


def make_client(
    handler, auth: AuthConfig | None = None, creds: Credentials | None = None, **kw
) -> ApiClient:
    async def no_sleep(_: float) -> None:
        return None

    return ApiClient(
        "https://svc.example/api",
        auth or AuthConfig(),
        creds or Credentials(),
        allow_private=True,
        transport=httpx.MockTransport(handler),
        sleep=no_sleep,
        **kw,
    )


@pytest.mark.parametrize(
    ("auth", "creds", "header", "value"),
    [
        (AuthConfig(type="api_key", header_name="X-Key"), Credentials(api_key="k1"), "x-key", "k1"),
        (AuthConfig(type="bearer"), Credentials(token="t1"), "authorization", "Bearer t1"),
        (
            AuthConfig(type="basic"),
            Credentials(username="u", password="p"),
            "authorization",
            "Basic dTpw",
        ),
    ],
)
async def test_auth_headers(auth, creds, header, value):
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json={"ok": True})

    async with make_client(handler, auth, creds, headers={"X-Extra": "1"}) as client:
        assert await client.get_json("/ping") == {"ok": True}
    assert seen[header] == value and seen["x-extra"] == "1"


async def test_oauth_token_cached_and_errors():
    calls = {"token": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            calls["token"] += 1
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        assert request.headers["authorization"] == "Bearer tok"
        return httpx.Response(200, json=[])

    auth = AuthConfig(type="oauth2_client_credentials", token_url="https://svc.example/token")
    async with make_client(handler, auth, Credentials(client_id="c", client_secret="s")) as client:
        await client.get_json("/a")
        await client.get_json("/b")
    assert calls["token"] == 1

    def bad_token(request: httpx.Request) -> httpx.Response:
        return (
            httpx.Response(200, json={"nope": 1})
            if request.url.path == "/token"
            else httpx.Response(200)
        )

    async with make_client(bad_token, auth) as client:
        with pytest.raises(IntegrationError, match="access_token"):
            await client.get_json("/a")
    async with make_client(bad_token, AuthConfig(type="oauth2_client_credentials")) as client:
        with pytest.raises(IntegrationError, match="token_url"):
            await client.get_json("/a")


async def test_retries_errors_and_non_json():
    state = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] < 3:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(200, json={"done": True})

    async with make_client(flaky) as client:
        assert await client.get_json("/x") == {"done": True}
    assert state["n"] == 3

    async with make_client(lambda r: httpx.Response(400, text="bad input")) as client:
        with pytest.raises(IntegrationError) as info:
            await client.get_json("/x")
        assert info.value.status_code == 400
    async with make_client(lambda r: httpx.Response(200, text="<html>")) as client:
        with pytest.raises(IntegrationError, match="JSON"):
            await client.get_json("/x")
        assert await client.get_text("/x") == "<html>"

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    async with make_client(broken, max_retries=1) as client:
        with pytest.raises(IntegrationError, match="failed"):
            await client.get_json("/x")


def pager(records: list[dict], style: str):
    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.params
        if style == "page":
            page, size = int(p["page"]), int(p["per_page"])
            return httpx.Response(200, json={"data": records[(page - 1) * size : page * size]})
        if style == "offset":
            off, lim = int(p["offset"]), int(p["limit"])
            return httpx.Response(200, json={"items": records[off : off + lim]})
        if style == "cursor":
            start = int(p.get("cursor", "0"))
            chunk = records[start : start + 2]
            nxt = start + 2 if start + 2 < len(records) else None
            return httpx.Response(200, json={"results": chunk, "meta": {"next": nxt}})
        if style == "odata":
            skip = int(p.get("skip", "0"))
            body: dict[str, Any] = {"value": records[skip : skip + 2]}
            if skip + 2 < len(records):
                body["@odata.nextLink"] = f"https://svc.example/api/things?skip={skip + 2}"
            return httpx.Response(200, json=body)
        return httpx.Response(200, json=records)

    return handler


@pytest.mark.parametrize(
    ("style", "pagination"),
    [
        ("page", Pagination(style="page", page_size=2)),
        ("offset", Pagination(style="offset", page_size=2)),
        ("cursor", Pagination(style="cursor", cursor_path="meta.next")),
        ("odata", Pagination(style="odata")),
        ("none", Pagination()),
    ],
)
async def test_pagination_styles(style, pagination):
    records = [{"id": i} for i in range(5)]
    async with make_client(pager(records, style)) as client:
        got = [r["id"] async for r in client.paginate("/things", pagination)]
        assert got == [0, 1, 2, 3, 4]
        limited = [r["id"] async for r in client.paginate("/things", pagination, limit=3)]
        assert limited == [0, 1, 2]


async def test_pagination_safety():
    def other_host(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"value": [{"id": 1}], "@odata.nextLink": "https://evil.example/steal"}
        )

    async with make_client(other_host) as client:
        with pytest.raises(BlockedAddressError):
            _ = [r async for r in client.paginate("/x", Pagination(style="odata"))]
    async with make_client(lambda r: httpx.Response(200, json={"count": 3})) as client:
        with pytest.raises(IntegrationError, match="array of records"):
            _ = [r async for r in client.paginate("/x", Pagination(), records_key="rows")]

    def endless(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": 1}], "next_cursor": "same"})

    async with make_client(endless) as client:
        with pytest.raises(IntegrationError, match="pages"):
            _ = [r async for r in client.paginate("/x", Pagination(style="cursor"), max_pages=5)]


# ------------------------------------------------------------------ export


def test_exported_script_transforms_like_the_engine(tmp_path, monkeypatch):
    source = parse_openapi(LEGACY_OPENAPI).entity("Customer")
    target = parse_odata(CRM_METADATA).entity("contact")
    fields = [
        FieldMapping(
            target="lastname",
            source="full_name",
            steps=steps({"op": "split", "sep": " ", "index": -1}),
            status="approved",
        ),
        FieldMapping(
            target="firstname",
            sources=["full_name"],
            status="approved",
            steps=steps({"op": "split", "index": 0}, {"op": "upper"}),
        ),
        FieldMapping(
            target="statecode",
            source="status",
            status="approved",
            steps=steps({"op": "map_values", "values": {"A": 0, "I": 1}}),
        ),
        FieldMapping(
            target="revenue",
            source="annual_revenue",
            steps=steps({"op": "to_float"}),
            status="approved",
        ),
        FieldMapping(
            target="new_signupdate",
            source="signup_date",
            status="approved",
            steps=steps({"op": "to_date", "input_format": "%d/%m/%Y"}),
        ),
        FieldMapping(target="telephone1", source="phone", status="rejected"),
    ]
    filename, text = export_script(
        mapping_id="m1",
        version=3,
        source_url="https://legacy.example/api",
        source=source,
        target_url="https://crm.example/api",
        target=target,
        pagination={"style": "page"},
        fields=fields,
    )
    assert filename == script_name("Customer", "contact") == "migrate_customer_to_contact.py"
    assert "telephone1" not in text
    path = tmp_path / filename
    path.write_text(text)
    module = runpy.run_path(str(path), run_name="aitsev_export_test")
    record = {
        "full_name": "Asha Devi Rao",
        "status": "I",
        "annual_revenue": "1,200.50",
        "signup_date": "17/05/2024",
    }
    engine_out, engine_errors = transform_record(record, fields)
    script_out, script_errors = module["transform"](record)
    assert script_out == engine_out
    assert engine_errors == script_errors == []
    assert module["transform"]({"full_name": "", "status": "A"})[1] == [
        "lastname: required value is missing"
    ]
