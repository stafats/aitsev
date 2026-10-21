from __future__ import annotations

from pathlib import Path

import pytest

from aitsev.pipeline.workspace import (
    ChangeSet,
    UnsafePathError,
    apply_changes,
    builtin_checks,
    changed_line_count,
    diff_changes,
    normalize_files,
    normalize_path,
    write_tree,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("src/app.py", "src/app.py"),
        ("./src//app.py", "src/app.py"),
        ("src\\win\\file.cs", "src/win/file.cs"),
        ("  README.md ", "README.md"),
        (".github/workflows/ci.yml", ".github/workflows/ci.yml"),
    ],
)
def test_normalize_path_accepts_relative_paths(raw, expected):
    assert normalize_path(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        ".",
        "/etc/passwd",
        "\\\\server\\share",
        "C:/Windows/x",
        "c:\\x",
        "../secret",
        "a/../../b",
        "a/./../b",
        ".git/config",
        "src/.git/hooks/pre-commit",
        "bad\x00name",
        "a/" * 250,
    ],
)
def test_normalize_path_rejects_unsafe_paths(raw):
    with pytest.raises(UnsafePathError):
        normalize_path(raw)


def test_normalize_files_rejects_duplicates_after_cleanup():
    with pytest.raises(UnsafePathError, match="Duplicate"):
        normalize_files({"a.py": "1", "./a.py": "2"})


def test_apply_changes_and_diffs():
    original = {"a.py": "x = 1\n", "b.py": "old\n", "keep.txt": "same\n"}
    changes = ChangeSet(changed={"a.py": "x = 2\n", "new.py": "y = 1\n"}, deleted=["b.py"])
    assert apply_changes(original, changes) == {
        "a.py": "x = 2\n",
        "new.py": "y = 1\n",
        "keep.txt": "same\n",
    }
    diffs = {d.path: d for d in diff_changes(original, changes)}
    assert diffs["a.py"].action == "modified" and "-x = 1" in diffs["a.py"].diff
    assert diffs["new.py"].action == "added" and diffs["new.py"].diff.startswith("--- /dev/null")
    assert diffs["b.py"].action == "deleted" and diffs["b.py"].content is None
    assert "+++ /dev/null" in diffs["b.py"].diff
    assert changed_line_count(original, changes) == 4


def test_diff_marks_missing_trailing_newline_and_skips_identical():
    original = {"a.txt": "one\n", "same.txt": "s\n"}
    changes = ChangeSet(changed={"a.txt": "one\ntwo", "same.txt": "s\n"})
    diffs = diff_changes(original, changes)
    assert [d.path for d in diffs] == ["a.txt"]
    assert "\\ No newline at end of file" in diffs[0].diff


def test_builtin_checks_catch_syntax_and_bad_deletes():
    changes = ChangeSet(
        changed={
            "ok.py": "def f():\n    return 1\n",
            "bad.py": "def f(:\n",
            "bad.json": "{nope}",
            "bad.toml": "x = = 1",
            "ok.json": '{"a": 1}',
            "notes.md": "anything ```",
        },
        deleted=["ghost.py"],
    )
    issues = builtin_checks(changes, {"ok.py": ""})
    text = "\n".join(issues)
    assert "bad.py" in text and "syntax" in text.lower()
    assert "bad.json" in text and "bad.toml" in text
    assert "ghost.py" in text
    assert "ok.py" not in text and "ok.json" not in text and "notes.md" not in text


def test_builtin_checks_never_execute_code(tmp_path: Path):
    marker = tmp_path / "pwned"
    code = f"import pathlib\npathlib.Path({str(marker)!r}).write_text('x')\n"
    assert builtin_checks(ChangeSet(changed={"x.py": code}), {}) == []
    assert not marker.exists()


def test_write_tree_writes_nested_files(tmp_path: Path):
    write_tree(tmp_path, {"a/b/c.txt": "hi", "d.txt": "yo"})
    assert (tmp_path / "a/b/c.txt").read_text() == "hi"
    assert (tmp_path / "d.txt").read_text() == "yo"


def test_write_tree_blocks_escape_even_without_normalization(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(UnsafePathError):
        write_tree(root, {"../escape.txt": "x"})
    assert not (tmp_path / "escape.txt").exists()
