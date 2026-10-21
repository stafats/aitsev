from __future__ import annotations

import pytest

from aitsev.pipeline.parsing import OutputFormatError, extract_json_object, parse_file_blocks


@pytest.mark.parametrize(
    "text",
    [
        '{"a": 1}',
        '  {"a": 1}  ',
        'Sure! Here it is:\n```json\n{"a": 1}\n```\nDone.',
        'prefix {not json} then {"a": 1} suffix',
    ],
)
def test_extract_json_object(text):
    assert extract_json_object(text) == {"a": 1}


def test_extract_json_object_nested_and_with_braces_in_strings():
    text = 'x {"a": {"b": "}{"}, "c": [1, 2]} y'
    assert extract_json_object(text) == {"a": {"b": "}{"}, "c": [1, 2]}


@pytest.mark.parametrize("text", ["", "no json here", "[1, 2, 3]", '{"unterminated": '])
def test_extract_json_object_failures(text):
    with pytest.raises(OutputFormatError):
        extract_json_object(text)


def test_parse_file_blocks_full_format():
    text = (
        "Some preamble the model added.\n"
        "SUMMARY: Fixed the bug\n"
        "and added a test.\n"
        "<<<FILE src/app.py\n"
        "def f():\n"
        "    return 1\n"
        "\n"
        ">>>END FILE\n"
        "<<<FILE README.md\n"
        "```python\n"
        "print('code fence inside a file is kept')\n"
        "```\n"
        ">>>END FILE\n"
        "<<<DELETE old/legacy.py>>>\n"
    )
    blocks = parse_file_blocks(text)
    assert blocks.summary == "Fixed the bug and added a test."
    assert blocks.files["src/app.py"] == "def f():\n    return 1\n\n"
    assert "```python" in blocks.files["README.md"]
    assert blocks.deleted == ["old/legacy.py"]


def test_parse_file_blocks_empty_file_and_quoted_path():
    blocks = parse_file_blocks("<<<FILE `pkg/__init__.py`\n>>>END FILE\n")
    assert blocks.files == {"pkg/__init__.py": ""}
    assert blocks.summary == ""


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("just prose", "no <<<FILE"),
        ("<<<FILE a.py\nx = 1\n", "not closed"),
        ("<<<FILE a.py\n1\n>>>END FILE\n<<<FILE a.py\n2\n>>>END FILE\n", "twice"),
        ("<<<FILE a.py\n1\n>>>END FILE\n<<<DELETE a.py>>>\n", "both written and deleted"),
    ],
)
def test_parse_file_blocks_errors(text, message):
    with pytest.raises(OutputFormatError, match=message):
        parse_file_blocks(text)
