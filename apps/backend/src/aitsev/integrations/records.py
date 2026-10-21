"""Turning one source record into one target record, and checking it against the target schema."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from aitsev.integrations.mapping import FieldMapping, mappable_targets
from aitsev.integrations.schema import EntityDef
from aitsev.integrations.transforms import TransformError, read_path, run_steps

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")


def _input(record: dict[str, Any], mapping: FieldMapping) -> Any:
    if mapping.constant is not None:
        return mapping.constant
    if mapping.sources:
        parts = [read_path(record, s) for s in mapping.sources]
        text = [str(p).strip() for p in parts if p is not None and str(p).strip()]
        return mapping.separator.join(text) if text else None
    if mapping.source:
        return read_path(record, mapping.source)
    return None


def transform_record(
    record: dict[str, Any], fields: Sequence[FieldMapping]
) -> tuple[dict[str, Any], list[str]]:
    """Apply every approved mapping. Returns the target record and per-field errors."""
    out: dict[str, Any] = {}
    errors: list[str] = []
    for mapping in fields:
        if mapping.status != "approved" or not mapping.has_input:
            continue
        try:
            value = run_steps(mapping.steps, _input(record, mapping))
        except TransformError as exc:
            errors.append(f"{mapping.target}: {exc}")
            continue
        if value is not None:
            out[mapping.target] = value
    return out, errors


def validate_record(record: dict[str, Any], target: EntityDef) -> list[str]:
    errors: list[str] = []
    for field in mappable_targets(target):
        value = record.get(field.name)
        if value is None or (isinstance(value, str) and not value.strip()):
            if field.required:
                errors.append(f"{field.name}: required value is missing")
            continue
        kind = field.type
        ok = True
        if kind == "integer":
            ok = isinstance(value, int) and not isinstance(value, bool)
        elif kind == "number":
            ok = isinstance(value, int | float) and not isinstance(value, bool)
        elif kind == "boolean":
            ok = isinstance(value, bool)
        elif kind == "date":
            ok = isinstance(value, str) and bool(_DATE.match(value))
        elif kind == "datetime":
            ok = isinstance(value, str) and bool(_DATETIME.match(value))
        elif kind == "string":
            ok = isinstance(value, str)
        if not ok:
            errors.append(
                f"{field.name}: expected {kind}, got {type(value).__name__} {value!r}"[:300]
            )
            continue
        if field.enum and str(value) not in field.enum:
            errors.append(f"{field.name}: {value!r} is not one of the allowed values")
        if field.max_length and isinstance(value, str) and len(value) > field.max_length:
            errors.append(f"{field.name}: longer than {field.max_length} characters")
    return errors
