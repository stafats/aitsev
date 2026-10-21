"""File-set handling: path safety, applying changes, built-in checks and diffs."""

from __future__ import annotations

import difflib
import json
import re
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

MAX_PATH_LENGTH = 400
_DRIVE = re.compile(r"^[A-Za-z]:")
_FORBIDDEN_SEGMENTS = frozenset({"..", ".git"})


class UnsafePathError(ValueError):
    """A file path tries to leave the workspace or touch protected locations."""


def normalize_path(raw: str) -> str:
    """Return a clean, relative POSIX path or raise :class:`UnsafePathError`."""
    if not isinstance(raw, str):
        raise UnsafePathError("Path must be a string")
    path = raw.strip().replace("\\", "/")
    if not path:
        raise UnsafePathError("Path is empty")
    if "\x00" in path:
        raise UnsafePathError(f"Path contains a NUL byte: {raw!r}")
    if path.startswith("/") or _DRIVE.match(path):
        raise UnsafePathError(f"Path must be relative: {raw!r}")
    parts = [p for p in PurePosixPath(path).parts if p not in ("", ".")]
    if not parts:
        raise UnsafePathError(f"Path is empty: {raw!r}")
    for part in parts:
        if part in _FORBIDDEN_SEGMENTS:
            raise UnsafePathError(f"Path segment {part!r} is not allowed: {raw!r}")
    clean = "/".join(parts)
    if len(clean) > MAX_PATH_LENGTH:
        raise UnsafePathError(f"Path is too long: {raw[:60]!r}...")
    return clean


def normalize_files(files: Mapping[str, str]) -> dict[str, str]:
    """Normalize every key; duplicate paths after normalization are rejected."""
    result: dict[str, str] = {}
    for raw, content in files.items():
        path = normalize_path(raw)
        if path in result:
            raise UnsafePathError(f"Duplicate path after normalization: {raw!r}")
        result[path] = content
    return result


@dataclass(slots=True)
class ChangeSet:
    """Files a candidate writes (full contents) and files it deletes."""

    changed: dict[str, str] = field(default_factory=dict)
    deleted: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.changed and not self.deleted


def apply_changes(original: Mapping[str, str], changes: ChangeSet) -> dict[str, str]:
    result = dict(original)
    for path in changes.deleted:
        result.pop(path, None)
    result.update(changes.changed)
    return result


def write_tree(root: Path, files: Mapping[str, str]) -> None:
    """Write files under ``root``; paths must already be normalized."""
    base = root.resolve()
    for path, content in files.items():
        target = (base / path).resolve()
        if not target.is_relative_to(base):  # defence in depth after normalize_path
            raise UnsafePathError(f"Path escapes workspace: {path!r}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def builtin_checks(changes: ChangeSet, original: Mapping[str, str]) -> list[str]:
    """Cheap checks that need no sandbox: syntax of Python, JSON and TOML, and deletions."""
    issues: list[str] = []
    for path in changes.deleted:
        if path not in original:
            issues.append(f"{path}: deleted a file that does not exist")
    for path, content in changes.changed.items():
        suffix = PurePosixPath(path).suffix.lower()
        try:
            if suffix == ".py":
                compile(content, path, "exec", dont_inherit=True)
            elif suffix == ".json":
                json.loads(content)
            elif suffix == ".toml":
                tomllib.loads(content)
        except SyntaxError as exc:
            issues.append(f"{path}:{exc.lineno}: Python syntax error: {exc.msg}")
        except json.JSONDecodeError as exc:
            issues.append(f"{path}:{exc.lineno}: invalid JSON: {exc.msg}")
        except tomllib.TOMLDecodeError as exc:
            issues.append(f"{path}: invalid TOML: {exc}")
    return issues


@dataclass(frozen=True, slots=True)
class FileDiff:
    path: str
    action: str  # added | modified | deleted
    content: str | None
    diff: str


def diff_changes(original: Mapping[str, str], changes: ChangeSet) -> list[FileDiff]:
    diffs: list[FileDiff] = []
    for path in sorted(changes.changed):
        new = changes.changed[path]
        old = original.get(path)
        if old == new:
            continue
        diffs.append(
            FileDiff(
                path=path,
                action="modified" if old is not None else "added",
                content=new,
                diff=_unified(path, old or "", new, old is None, False),
            )
        )
    for path in sorted(changes.deleted):
        if path in original:
            diffs.append(
                FileDiff(
                    path=path,
                    action="deleted",
                    content=None,
                    diff=_unified(path, original[path], "", False, True),
                )
            )
    return diffs


def _unified(path: str, old: str, new: str, added: bool, deleted: bool) -> str:
    lines = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile="/dev/null" if added else f"a/{path}",
        tofile="/dev/null" if deleted else f"b/{path}",
    )
    return "".join(_ensure_newlines(lines))


def _ensure_newlines(lines: Iterable[str]) -> Iterable[str]:
    for line in lines:
        yield line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"


def changed_line_count(original: Mapping[str, str], changes: ChangeSet) -> int:
    total = 0
    for item in diff_changes(original, changes):
        total += sum(
            1
            for line in item.diff.splitlines()
            if line[:1] in "+-" and not line.startswith(("+++", "---"))
        )
    return total
