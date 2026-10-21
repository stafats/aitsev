from __future__ import annotations

import pytest
from pydantic import SecretStr

from aitsev.config import SandboxMode, Settings


def test_csv_and_json_model_lists_from_env(monkeypatch):
    monkeypatch.setenv("AITSEV_GENERATOR_MODELS", "ollama-cloud/a, ollama-cloud/b ,")
    monkeypatch.setenv("AITSEV_REVIEWER_MODELS", '["x/1", "x/2"]')
    monkeypatch.setenv("AITSEV_SANDBOX_MODE", "local")
    monkeypatch.setenv("OLLAMA_API_KEY", "abc")
    settings = Settings(_env_file=None)
    assert settings.generator_models == ["ollama-cloud/a", "ollama-cloud/b"]
    assert settings.reviewer_models == ["x/1", "x/2"]
    assert settings.sandbox_mode == SandboxMode.LOCAL
    assert settings.ollama_api_key is not None
    assert settings.ollama_api_key.get_secret_value() == "abc"


def test_defaults_are_production_safe(monkeypatch):
    for name in ("OLLAMA_API_KEY", "AITSEV_SANDBOX_MODE", "AITSEV_ENVIRONMENT"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(_env_file=None)
    assert settings.sandbox_mode == SandboxMode.DOCKER
    assert settings.ollama_cloud_base_url == "https://ollama.com"
    assert settings.auto_create_schema is False
    assert all(m.startswith("ollama-cloud/") for m in settings.generator_models)


def test_parsed_api_keys():
    settings = Settings(
        _env_file=None,
        api_keys=SecretStr(" alice:aaaaaaaaaaaaaaaa , bob:bbbbbbbbbbbbbbbbbb,"),
    )
    assert settings.parsed_api_keys() == {"aaaaaaaaaaaaaaaa": "alice", "bbbbbbbbbbbbbbbbbb": "bob"}
    assert Settings(_env_file=None).parsed_api_keys() == {}


@pytest.mark.parametrize("raw", ["nocolon", "alice:short", ":aaaaaaaaaaaaaaaaaa"])
def test_parsed_api_keys_rejects_bad_entries(raw):
    with pytest.raises(ValueError):
        Settings(_env_file=None, api_keys=SecretStr(raw)).parsed_api_keys()


def test_secrets_do_not_leak_in_repr():
    settings = Settings(_env_file=None, api_keys=SecretStr("alice:aaaaaaaaaaaaaaaaaaaa"))
    assert "aaaaaaaaaaaa" not in repr(settings)


def test_empty_secrets_count_as_unset(monkeypatch):
    monkeypatch.setenv("OLLAMA_API_KEY", "")
    monkeypatch.setenv("AITSEV_SECRET_KEY", "   ")
    settings = Settings(_env_file=None, OPENAI_API_KEY=SecretStr(""))
    assert settings.ollama_api_key is None
    assert settings.secret_key is None
    assert settings.openai_api_key is None
