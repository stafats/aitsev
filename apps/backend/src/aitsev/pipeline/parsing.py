"""Parsing model output: JSON objects and file blocks."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

_FILE_START = re.compile(r"^<<<FILE\s+(.+?)\s*$")
_FILE_END = re.compile(r"^>>>END FILE\s*$")
_DELETE = re.compile(r"^<<<DELETE\s+(.+?)\s*>>>\s*$")
_SUMMARY = re.compile(r"^\s*SUMMARY:\s*(.*)$", re.IGNORECASE)


class OutputFormatError(ValueError):
    """Model output did not follow the required format."""


def extract_json_object(text: str) -> dict[str, Any]:
    """Return the first complete JSON object in ``text`` (tolerates fences and prose)."""
    stripped = text.strip()
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        value = None
    if isinstance(value, dict):
        return value
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            value, _ = decoder.raw_decode(text, match.start())
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise OutputFormatError("No JSON object found in model output")


@dataclass(slots=True)
class FileBlocks:
    summary: str
    files: dict[str, str] = field(default_factory=dict)
    deleted: list[str] = field(default_factory=list)


def parse_file_blocks(text: str) -> FileBlocks:
    """Parse the generation format.

    ```
    SUMMARY: what changed and why
    <<<FILE path/to/file.py
    ...full file content...
    >>>END FILE
    <<<DELETE path/to/old.py>>>
    ```
    """
    summary_lines: list[str] = []
    files: dict[str, str] = {}
    deleted: list[str] = []
    current_path: str | None = None
    current_lines: list[str] = []
    in_summary = False

    for line in text.splitlines():
        if current_path is not None:
            if _FILE_END.match(line):
                if current_path in files:
                    raise OutputFormatError(f"File {current_path!r} appears twice")
                files[current_path] = "\n".join(current_lines) + ("\n" if current_lines else "")
                current_path, current_lines = None, []
            else:
                current_lines.append(line)
            continue
        start = _FILE_START.match(line)
        if start:
            current_path = start.group(1).strip().strip("`\"'")
            in_summary = False
            continue
        delete = _DELETE.match(line)
        if delete:
            deleted.append(delete.group(1).strip().strip("`\"'"))
            in_summary = False
            continue
        summary = _SUMMARY.match(line)
        if summary:
            in_summary = True
            summary_lines.append(summary.group(1))
            continue
        if in_summary and line.strip() and not line.strip().startswith("```"):
            summary_lines.append(line.strip())

    if current_path is not None:
        raise OutputFormatError(f"File block for {current_path!r} was not closed with >>>END FILE")
    if not files and not deleted:
        raise OutputFormatError("Output contained no <<<FILE blocks and no <<<DELETE lines")
    overlap = set(files) & set(deleted)
    if overlap:
        raise OutputFormatError(f"Files both written and deleted: {sorted(overlap)}")
    return FileBlocks(summary=" ".join(summary_lines).strip(), files=files, deleted=deleted)
