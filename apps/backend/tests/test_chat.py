"""Chat with the code, agent edits with sandbox verification, and background analysis."""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from aitsev.accounts.email import LogEmailSender
from aitsev.chat.agent import ChatError, extract_edits, route
from aitsev.config import Settings
from aitsev.db.models import ModelCall
from aitsev.db.session import Database
from aitsev.gateway.errors import ModelRequestError
from aitsev.main import create_app
from aitsev.pipeline.sandbox import LocalRunner
from tests.conftest import ALICE_KEY
from tests.fakes import BUGGY_PROJECT, CORRECT, PY, VERIFY, WRONG, Script, make_gateway
from tests.test_accounts import WEB, bearer, run, signup

OPERATOR = {"Authorization": f"Bearer {ALICE_KEY}"}
CATALOG = json.dumps(
    [
        {"id": "fake/m1", "label": "One"},
        {"id": "fake/m3", "label": "Three"},
        {"id": "fake/m2", "label": "Premium", "tier": "premium", "weight": 5},
    ]
)
AGENT_REPLY = (
    "I fixed the operator and added a helper.\n"
    "<<<FILE calc.py\ndef add(a, b):\n    return a + b\n>>>END FILE\n"
    "<<<FILE util.py\nX = 1\n>>>END FILE\n"
    "<<<FILE README.md\nhidden overwrite\n>>>END FILE\n"
    "<<<FILE ../evil.py\nboom\n>>>END FILE\n"
    "<<<DELETE test_calc.py>>>\n"
)


@pytest.fixture
def script() -> Script:
    return Script()


@pytest.fixture
def client(settings: Settings, script: Script) -> Iterator[TestClient]:
    configured = settings.model_copy(update={"model_catalog": CATALOG})
    app = create_app(
        configured,
        gateway=make_gateway(script),
        runner=LocalRunner(),
        start_worker=False,
        email_sender=LogEmailSender(),
    )
    with TestClient(app) as test_client:
        yield test_client


def body(**overrides: object) -> dict:
    data: dict = {
        "messages": [{"role": "user", "content": "Fix add"}],
        "mode": "agent",
        "files": dict(BUGGY_PROJECT),
    }
    data.update(overrides)
    return data


# ------------------------------------------------------------------ unit


def test_route_prefers_planner_then_generators(settings):
    assert route(settings, "auto") == ["fake/m1", "fake/m2", "fake/m3"]
    assert route(settings, "fake/m3") == ["fake/m3"]


def test_extract_edits_validates_paths_and_skips_unseen_files():
    files = dict(BUGGY_PROJECT)
    reply, edits, skipped = extract_edits(AGENT_REPLY, files, ["README.md"])
    assert reply == "I fixed the operator and added a helper."
    assert [(e.path, e.action) for e in edits] == [
        ("calc.py", "modify"),
        ("util.py", "create"),
        ("test_calc.py", "delete"),
    ]
    assert "+    return a + b" in edits[0].diff
    assert any("README.md" in s for s in skipped)
    assert any("evil" in s for s in skipped)
    # Unchanged files are not edits; plain answers have none.
    same = "<<<FILE calc.py\n" + BUGGY_PROJECT["calc.py"] + ">>>END FILE\n"
    assert extract_edits(same, files, [])[1] == []
    assert extract_edits("Just an answer.", files, []) == ("Just an answer.", [], [])
    with pytest.raises(ChatError, match="malformed"):
        extract_edits("<<<FILE calc.py\nnever closed", files, [])
    _r, _e, skipped = extract_edits("<<<DELETE nope.py>>>\n", files, [])
    assert "cannot delete" in skipped[0]


# ------------------------------------------------------------------ chat API


def test_ask_mode_answers_without_edits(client, script):
    script.default("chat", "add() subtracts instead of adding.\n<<<FILE calc.py\nx\n>>>END FILE\n")
    response = client.post(
        "/v1/chat",
        json=body(mode="ask", active_path="calc.py", selection="return a - b"),
        headers=OPERATOR,
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["reply"] == "add() subtracts instead of adding."
    assert data["edits"] == []
    assert data["model"] == "fake/m1"  # auto routing starts with the planner
    request = script.calls[0][2]
    context = request.messages[1].content
    assert "<active_file>calc.py</active_file>" in context
    assert "return a - b" in context
    assert "Do not output file blocks" in request.messages[0].content


def test_agent_mode_returns_edits_and_keeps_history(client, script):
    script.default("chat", AGENT_REPLY)
    history = [
        {"role": "user", "content": "What does add do?"},
        {"role": "assistant", "content": "It subtracts."},
        {"role": "user", "content": "Fix it"},
    ]
    files = {**BUGGY_PROJECT, "README.md": "# Demo\n"}
    response = client.post(
        "/v1/chat",
        json=body(messages=history, files=files, context_paths=["calc.py", "test_calc.py"]),
        headers=OPERATOR,
    )
    data = response.json()
    assert [(e["path"], e["action"]) for e in data["edits"]] == [
        ("calc.py", "modify"),
        ("util.py", "create"),
        ("test_calc.py", "delete"),
    ]
    assert data["edits"][2]["content"] is None
    assert any("README.md" in s for s in data["skipped"])  # outside the shared context
    assert data["verification"] is None
    sent = [m.content for m in script.calls[0][2].messages]
    assert sent[-3:] == ["What does add do?", "It subtracts.", "Fix it"]
    assert "README.md" in sent[1] and "# Demo" not in sent[1]


def test_agent_edits_are_verified_and_repaired(client, script):
    script.default("chat", WRONG).default("chat_repair", CORRECT)
    response = client.post("/v1/chat", json=body(verify_commands=VERIFY), headers=OPERATOR)
    data = response.json()
    assert data["repaired"] is True
    assert data["verification"]["passed"] is True
    assert data["edits"][0]["content"] == "def add(a, b):\n    return a + b\n"
    repair_prompt = script.calls[1][2].messages[-1].content
    assert "failed verification" in repair_prompt and "assert" in repair_prompt
    assert data["prompt_tokens"] == 200


def test_verified_first_time_needs_no_repair(client, script):
    script.default("chat", CORRECT)
    data = client.post("/v1/chat", json=body(verify_commands=VERIFY), headers=OPERATOR).json()
    assert data["repaired"] is False
    assert data["verification"]["passed"] is True
    assert len(script.calls) == 1


def test_repair_that_still_fails_is_reported(client, script):
    script.default("chat", WRONG).default("chat_repair", WRONG)
    data = client.post(
        "/v1/chat",
        json=body(verify_commands=[f"{PY} -c 'import sys; sys.exit(3)'"]),
        headers=OPERATOR,
    ).json()
    assert data["verification"]["passed"] is False
    assert data["repaired"] is True


@pytest.mark.parametrize(
    ("overrides", "code", "text"),
    [
        ({"model": "fake/m2"}, 402, "Pro plan"),
        ({"model": "nope/x"}, 422, "Unknown model"),
        ({"messages": [{"role": "assistant", "content": "hi"}]}, 422, "last message"),
        ({"files": {"../x.py": "x"}}, 422, ""),
        ({"context_paths": ["missing.py"]}, 422, "not among files"),
        ({"verify_commands": ["true"] * 11}, 422, "At most"),
        ({"active_path": "../../etc/passwd"}, 422, ""),
        ({"verify_commands": [""]}, 422, ""),
    ],
)
def test_chat_validation(client, script, overrides, code, text):
    script.default("chat", "ok")
    token = signup(client)["token"]
    client.cookies.clear()
    response = client.post("/v1/chat", json=body(**overrides), headers=bearer(token))
    assert response.status_code == code
    assert text.lower() in response.text.lower()


def test_large_context_is_refused(client, script, settings):
    script.default("chat", "ok")
    big = {"a.py": "x" * 150_000, "b.py": "y" * 150_000, "c.py": "z" * 150_000}
    response = client.post("/v1/chat", json=body(files=big), headers=OPERATOR)
    assert response.status_code == 413
    assert "Share fewer files" in response.text


def test_chosen_model_is_used_and_metered_to_the_account(client, script, settings):
    script.default("chat", "answer")
    user = signup(client)
    client.cookies.clear()
    data = client.post(
        "/v1/chat", json=body(model="fake/m3", mode="ask"), headers=bearer(user["token"])
    ).json()
    assert data["model"] == "fake/m3"

    async def owners(db: Database) -> None:
        async with db.session() as session:
            rows = (await session.scalars(select(ModelCall))).all()
            assert [(r.owner, r.stage) for r in rows] == [(user["account"]["id"], "chat")]

    run(settings.database_url, owners)
    usage = client.get("/v1/account/usage", headers=bearer(user["token"])).json()
    assert usage["tokens"]["used"] == 150


def test_model_failures_and_bad_edits_are_502(client, script):
    script.default("chat", ModelRequestError("boom", retryable=False))
    failed = client.post("/v1/chat", json=body(), headers=OPERATOR)
    assert failed.status_code == 502 and "boom" in failed.text
    script.default("chat", "<<<FILE calc.py\nnot closed")
    bad = client.post("/v1/chat", json=body(), headers=OPERATOR)
    assert bad.status_code == 502 and "malformed" in bad.text


def test_cookie_sessions_need_the_csrf_header(client, script):
    script.default("chat", "ok")
    signup(client)
    assert client.post("/v1/chat", json=body(mode="ask")).status_code == 403
    assert client.post("/v1/chat", json=body(mode="ask"), headers=WEB).status_code == 200


# ------------------------------------------------------------------ analysis


def test_analyze_returns_clean_suggestions(client, script):
    script.default(
        "analyze",
        json.dumps(
            {
                "suggestions": [
                    {
                        "path": "calc.py",
                        "line": 2,
                        "severity": "error",
                        "title": "Subtracts",
                        "detail": "Use +",
                    },
                    {"path": "calc.py", "line": 999, "severity": "loud", "title": "Clamped"},
                    {"path": "ghost.py", "line": 1, "title": "Unknown file"},
                    {"path": "../x", "title": "Unsafe"},
                    {"path": "calc.py", "line": "abc", "title": "Bad line"},
                    {"path": "calc.py", "title": ""},
                    "junk",
                ]
            }
        ),
    )
    response = client.post(
        "/v1/analyze", json={"files": BUGGY_PROJECT, "focus": "bugs", "limit": 3}, headers=OPERATOR
    )
    data = response.json()
    assert data["model"] == "fake/m1"
    assert data["suggestions"] == [
        {
            "path": "calc.py",
            "line": 2,
            "severity": "error",
            "title": "Subtracts",
            "detail": "Use +",
        },
        {"path": "calc.py", "line": 2, "severity": "warning", "title": "Clamped", "detail": ""},
        {"path": "calc.py", "line": 1, "severity": "warning", "title": "Bad line", "detail": ""},
    ]
    prompt = script.calls[0][2].messages[1].content
    assert "<focus>bugs</focus>" in prompt and "at most 3" in prompt


def test_analyze_tolerates_non_json_and_errors(client, script):
    script.default("analyze", "Looks fine to me!")
    data = client.post("/v1/analyze", json={"files": BUGGY_PROJECT}, headers=OPERATOR).json()
    assert data["suggestions"] == []
    script.default("analyze", ModelRequestError("down", retryable=False))
    assert (
        client.post("/v1/analyze", json={"files": BUGGY_PROJECT}, headers=OPERATOR).status_code
        == 502
    )
    assert client.post("/v1/analyze", json={"files": {}}, headers=OPERATOR).status_code == 422


def test_no_available_model_is_reported(settings, script):
    configured = settings.model_copy(
        update={"planner_model": "none/x", "generator_models": ["none/y"], "fallback_models": []}
    )
    app = create_app(
        configured, gateway=make_gateway(script), runner=LocalRunner(), start_worker=False
    )
    with TestClient(app) as test_client:
        response = test_client.post("/v1/chat", json=body(), headers=OPERATOR)
        assert response.status_code == 502
        assert "No model is available" in response.text
        analysed = test_client.post("/v1/analyze", json={"files": BUGGY_PROJECT}, headers=OPERATOR)
        assert analysed.status_code == 502
