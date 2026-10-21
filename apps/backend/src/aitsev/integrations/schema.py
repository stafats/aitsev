"""A normalized schema model, and parsers from OpenAPI, OData $metadata and sample records."""

from __future__ import annotations

import re
from typing import Any, Literal

from defusedxml import DefusedXmlException, ElementTree
from pydantic import BaseModel, Field

FieldType = Literal["string", "integer", "number", "boolean", "date", "datetime", "object", "array"]


class FieldDef(BaseModel):
    name: str
    type: FieldType = "string"
    required: bool = False
    read_only: bool = False
    description: str = ""
    enum: list[str] = Field(default_factory=list)
    max_length: int | None = None
    format: str | None = None


class EntityDef(BaseModel):
    name: str
    fields: list[FieldDef]
    key: str | None = None
    list_path: str | None = None  # GET returns records
    create_path: str | None = None  # POST creates one record
    records_key: str | None = None  # where the list lives in the response body, e.g. "value"

    def field(self, name: str) -> FieldDef | None:
        return next((f for f in self.fields if f.name == name), None)


class SchemaModel(BaseModel):
    source: Literal["openapi", "odata", "samples"]
    entities: list[EntityDef]

    def entity(self, name: str) -> EntityDef | None:
        return next((e for e in self.entities if e.name == name), None)


class SchemaParseError(ValueError):
    """The document could not be read as the declared schema format."""


# ------------------------------------------------------------------ OpenAPI

_OPENAPI_TYPES: dict[str, FieldType] = {
    "string": "string",
    "integer": "integer",
    "number": "number",
    "boolean": "boolean",
    "object": "object",
    "array": "array",
}


def _resolve(doc: dict[str, Any], node: Any, depth: int = 0) -> dict[str, Any]:
    if not isinstance(node, dict):
        return {}
    ref = node.get("$ref")
    if isinstance(ref, str) and depth < 20:
        if not ref.startswith("#/"):
            raise SchemaParseError(f"Only local $ref values are supported, got {ref!r}")
        target: Any = doc
        for part in ref[2:].split("/"):
            target = (
                target.get(part.replace("~1", "/").replace("~0", "~"))
                if isinstance(target, dict)
                else None
            )
        return _resolve(doc, target, depth + 1)
    if "allOf" in node and depth < 20:
        merged: dict[str, Any] = {"type": "object", "properties": {}, "required": []}
        for part in node["allOf"]:
            sub = _resolve(doc, part, depth + 1)
            merged["properties"].update(sub.get("properties", {}))
            merged["required"] += list(sub.get("required", []))
        return merged
    return node


def _field_from_openapi(name: str, spec: dict[str, Any], required: bool) -> FieldDef:
    raw_type = spec.get("type", "string")
    if isinstance(raw_type, list):  # OpenAPI 3.1: ["string", "null"]
        raw_type = next((t for t in raw_type if t != "null"), "string")
    fmt = spec.get("format")
    ftype: FieldType = _OPENAPI_TYPES.get(str(raw_type), "string")
    if ftype == "string" and fmt == "date":
        ftype = "date"
    elif ftype == "string" and fmt == "date-time":
        ftype = "datetime"
    return FieldDef(
        name=name,
        type=ftype,
        required=required,
        read_only=bool(spec.get("readOnly")),
        description=str(spec.get("description") or "")[:500],
        enum=[str(v) for v in spec.get("enum", [])][:200],
        max_length=spec.get("maxLength") if isinstance(spec.get("maxLength"), int) else None,
        format=str(fmt) if fmt else None,
    )


_RECORD_KEYS = ("data", "items", "value", "results", "records")


def _ref_name(node: Any) -> str | None:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith(("#/components/schemas/", "#/definitions/")):
            return ref.rsplit("/", 1)[-1]
    return None


def _list_target(doc: dict[str, Any], schema: Any) -> tuple[str, str | None] | None:
    """For a list response, return (entity schema name, key holding the records or None)."""
    resolved = _resolve(doc, schema)
    if resolved.get("type") == "array":
        name = _ref_name(resolved.get("items"))
        return (name, None) if name else None
    props = resolved.get("properties") or {}
    for key in _RECORD_KEYS:
        if key in props:
            inner = _resolve(doc, props[key])
            if inner.get("type") == "array":
                name = _ref_name(inner.get("items"))
                if name:
                    return name, key
    return None


def parse_openapi(doc: dict[str, Any]) -> SchemaModel:
    if not isinstance(doc, dict) or not ("openapi" in doc or "swagger" in doc):
        raise SchemaParseError("Not an OpenAPI document (missing 'openapi' or 'swagger')")
    schemas = (doc.get("components") or {}).get("schemas") or doc.get("definitions") or {}
    if not isinstance(schemas, dict) or not schemas:
        raise SchemaParseError("The OpenAPI document defines no schemas")

    list_paths: dict[str, tuple[str, str | None]] = {}
    create_paths: dict[str, str] = {}
    for path, item in (doc.get("paths") or {}).items():
        if not isinstance(item, dict) or "{" in path:
            continue
        get = item.get("get")
        if isinstance(get, dict):
            responses = get.get("responses") or {}
            ok = responses.get("200") or responses.get(200) or {}
            content = (ok.get("content") or {}).get("application/json") or {}
            target = _list_target(doc, content.get("schema") or ok.get("schema"))
            if target and target[0] not in list_paths:
                list_paths[target[0]] = (path, target[1])
        post = item.get("post")
        if isinstance(post, dict):
            body = ((post.get("requestBody") or {}).get("content") or {}).get("application/json")
            name = _ref_name((body or {}).get("schema"))
            if name and name not in create_paths:
                create_paths[name] = path

    entities: list[EntityDef] = []
    for name, raw in schemas.items():
        spec = _resolve(doc, raw)
        props = spec.get("properties")
        if not isinstance(props, dict) or not props:
            continue
        required = set(spec.get("required", []))
        fields = [
            _field_from_openapi(fname, _resolve(doc, fspec), fname in required)
            for fname, fspec in props.items()
        ]
        key = next((f.name for f in fields if f.name.lower() in {"id", f"{name.lower()}id"}), None)
        list_path, records_key = list_paths.get(name, (None, None))
        entities.append(
            EntityDef(
                name=name,
                fields=fields,
                key=key,
                list_path=list_path,
                create_path=create_paths.get(name),
                records_key=records_key,
            )
        )
    if not entities:
        raise SchemaParseError("No object schemas with properties were found")
    return SchemaModel(source="openapi", entities=entities)


# ------------------------------------------------------------------ OData CSDL ($metadata)

_EDM_TYPES: dict[str, FieldType] = {
    "Edm.String": "string",
    "Edm.Guid": "string",
    "Edm.Int16": "integer",
    "Edm.Int32": "integer",
    "Edm.Int64": "integer",
    "Edm.Byte": "integer",
    "Edm.Decimal": "number",
    "Edm.Double": "number",
    "Edm.Single": "number",
    "Edm.Boolean": "boolean",
    "Edm.Date": "date",
    "Edm.DateTimeOffset": "datetime",
    "Edm.DateTime": "datetime",
}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_odata(xml_text: str) -> SchemaModel:
    """Parse an OData v4 $metadata document (Dynamics 365 / Dataverse, SAP, Business Central)."""
    try:
        root = ElementTree.fromstring(xml_text.encode("utf-8"))
    except ElementTree.ParseError as exc:
        raise SchemaParseError(f"Invalid XML: {exc}") from exc
    except DefusedXmlException as exc:
        raise SchemaParseError(f"Unsafe XML refused ({type(exc).__name__})") from exc
    entity_sets: dict[str, str] = {}
    for el in root.iter():
        if _local(el.tag) == "EntitySet":
            etype = (el.get("EntityType") or "").rsplit(".", 1)[-1]
            if etype and el.get("Name"):
                entity_sets[etype] = str(el.get("Name"))
    entities: list[EntityDef] = []
    for el in root.iter():
        if _local(el.tag) != "EntityType" or not el.get("Name"):
            continue
        name = str(el.get("Name"))
        key = None
        fields: list[FieldDef] = []
        for child in el:
            tag = _local(child.tag)
            if tag == "Key":
                ref = next((r for r in child if _local(r.tag) == "PropertyRef"), None)
                key = ref.get("Name") if ref is not None else None
            elif tag == "Property" and child.get("Name"):
                max_len = child.get("MaxLength")
                fields.append(
                    FieldDef(
                        name=str(child.get("Name")),
                        type=_EDM_TYPES.get(child.get("Type") or "", "string"),
                        required=child.get("Nullable") == "false",
                        max_length=int(max_len) if max_len and max_len.isdigit() else None,
                    )
                )
        if not fields:
            continue
        set_name = entity_sets.get(name)
        path = f"/{set_name}" if set_name else None
        entities.append(
            EntityDef(
                name=name,
                fields=fields,
                key=key,
                list_path=path,
                create_path=path,
                records_key="value",
            )
        )
    if not entities:
        raise SchemaParseError("No EntityType elements with properties were found")
    return SchemaModel(source="odata", entities=entities)


# ------------------------------------------------------------------ samples

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")


def _infer_type(values: list[Any]) -> FieldType:
    seen = [v for v in values if v is not None]
    if not seen:
        return "string"
    if all(isinstance(v, bool) for v in seen):
        return "boolean"
    if all(isinstance(v, int) and not isinstance(v, bool) for v in seen):
        return "integer"
    if all(isinstance(v, int | float) and not isinstance(v, bool) for v in seen):
        return "number"
    if all(isinstance(v, dict) for v in seen):
        return "object"
    if all(isinstance(v, list) for v in seen):
        return "array"
    if all(isinstance(v, str) and _DATE.match(v) for v in seen):
        return "date"
    if all(isinstance(v, str) and _DATETIME.match(v) for v in seen):
        return "datetime"
    return "string"


def infer_from_samples(
    entity: str,
    records: list[dict[str, Any]],
    list_path: str | None = None,
    create_path: str | None = None,
) -> EntityDef:
    if not records or not all(isinstance(r, dict) for r in records):
        raise SchemaParseError("Samples must be a non-empty list of JSON objects")
    names: list[str] = []
    for record in records:
        for key in record:
            if key not in names:
                names.append(key)
    fields = [
        FieldDef(
            name=n,
            type=_infer_type([r.get(n) for r in records]),
            required=all(r.get(n) not in (None, "") for r in records),
        )
        for n in names
    ]
    key_name = next((n for n in names if n.lower() in {"id", f"{entity.lower()}id"}), None)
    return EntityDef(
        name=entity, fields=fields, key=key_name, list_path=list_path, create_path=create_path
    )
