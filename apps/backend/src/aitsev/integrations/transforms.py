"""A small, safe transformation language for field mappings.

Mappings never contain code. Each target field gets an ordered list of steps applied to a value
read from a source field (or several joined together), for example splitting a full name:

    {"source": "full_name", "target": "firstname",
     "steps": [{"op": "split", "sep": " ", "index": 0}]}
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, TypeAdapter


class TransformError(ValueError):
    """A value could not be transformed."""


class Trim(BaseModel):
    op: Literal["trim"] = "trim"


class Lower(BaseModel):
    op: Literal["lower"] = "lower"


class Upper(BaseModel):
    op: Literal["upper"] = "upper"


class ToInt(BaseModel):
    op: Literal["to_int"] = "to_int"


class ToFloat(BaseModel):
    op: Literal["to_float"] = "to_float"


class ToBool(BaseModel):
    op: Literal["to_bool"] = "to_bool"


class ToString(BaseModel):
    op: Literal["to_string"] = "to_string"


class ToDate(BaseModel):
    """Parse a date with ``input_format`` (strptime) and write it as ISO ``YYYY-MM-DD``."""

    op: Literal["to_date"] = "to_date"
    input_format: str | None = None


class ToDatetime(BaseModel):
    op: Literal["to_datetime"] = "to_datetime"
    input_format: str | None = None


class Split(BaseModel):
    op: Literal["split"] = "split"
    sep: str = Field(default=" ", min_length=1, max_length=10)
    index: int = 0


class Truncate(BaseModel):
    op: Literal["truncate"] = "truncate"
    length: int = Field(ge=1, le=100_000)


class MapValues(BaseModel):
    """Translate values, e.g. status codes to option-set values. Unknown values use ``default``."""

    op: Literal["map_values"] = "map_values"
    values: dict[str, Any]
    default: Any = None
    strict: bool = False  # True: unknown values are an error


class Default(BaseModel):
    """Use ``value`` when the input is missing or empty."""

    op: Literal["default"] = "default"
    value: Any


class Prefix(BaseModel):
    op: Literal["prefix"] = "prefix"
    text: str = Field(max_length=200)


Step = Annotated[
    Trim
    | Lower
    | Upper
    | ToInt
    | ToFloat
    | ToBool
    | ToString
    | ToDate
    | ToDatetime
    | Split
    | Truncate
    | MapValues
    | Default
    | Prefix,
    Field(discriminator="op"),
]

STEPS_ADAPTER: TypeAdapter[list[Step]] = TypeAdapter(list[Step])

_TRUE = {"true", "1", "yes", "y", "t", "on"}
_FALSE = {"false", "0", "no", "n", "f", "off"}


def _empty(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def apply_step(step: Step, value: Any) -> Any:
    if isinstance(step, Default):
        return step.value if _empty(value) else value
    if isinstance(step, MapValues):
        key = "" if value is None else str(value)
        if key in step.values:
            return step.values[key]
        if step.strict:
            raise TransformError(f"No mapping for value {key!r}")
        return step.default
    if value is None:
        return None
    if isinstance(step, Trim):
        return value.strip() if isinstance(value, str) else value
    if isinstance(step, Lower):
        return str(value).lower()
    if isinstance(step, Upper):
        return str(value).upper()
    if isinstance(step, ToString):
        return str(value)
    if isinstance(step, Prefix):
        return f"{step.text}{value}"
    if isinstance(step, Truncate):
        return str(value)[: step.length]
    if isinstance(step, Split):
        parts = str(value).split(step.sep)
        try:
            return parts[step.index].strip()
        except IndexError:
            return None
    if isinstance(step, ToInt):
        if isinstance(value, bool):
            return int(value)
        try:
            number = Decimal(str(value).strip().replace(",", ""))
        except InvalidOperation as exc:
            raise TransformError(f"Not a number: {value!r}") from exc
        if number != number.to_integral_value():
            raise TransformError(f"Not a whole number: {value!r}")
        return int(number)
    if isinstance(step, ToFloat):
        try:
            return float(str(value).strip().replace(",", ""))
        except ValueError as exc:
            raise TransformError(f"Not a number: {value!r}") from exc
    if isinstance(step, ToBool):
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
        raise TransformError(f"Not a yes/no value: {value!r}")
    if isinstance(step, ToDate):
        return _parse_datetime(value, step.input_format).date().isoformat()
    if isinstance(step, ToDatetime):
        return _parse_datetime(value, step.input_format).isoformat()
    raise TransformError(f"Unknown step {step!r}")  # pragma: no cover - exhaustive above


def _parse_datetime(value: Any, fmt: str | None) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    text = str(value).strip()
    try:
        if fmt:
            return datetime.strptime(text, fmt)
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TransformError(f"Not a date{f' in format {fmt}' if fmt else ''}: {value!r}") from exc


def run_steps(steps: list[Step], value: Any) -> Any:
    for step in steps:
        value = apply_step(step, value)
    return value


def read_path(record: dict[str, Any], path: str) -> Any:
    """Read ``a.b.c`` from nested dictionaries; missing keys give None."""
    current: Any = record
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current
