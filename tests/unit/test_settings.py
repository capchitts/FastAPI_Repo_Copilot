from pathlib import Path

import pytest
from pydantic import ValidationError

from repo_chat.config.settings import Environment, Settings, get_settings


def test_settings_use_expected_defaults() -> None:
    settings = Settings(_env_file=None)

    assert settings.environment is Environment.DEVELOPMENT
    assert settings.service_name == "repository-chat"
    assert settings.mcp_host == "127.0.0.1"
    assert settings.mcp_port == 8001
    assert settings.redis_url == "redis://localhost:6379/0"
    assert settings.neo4j_uri == "bolt://localhost:7687"
    assert settings.neo4j_database == "neo4j"
    assert settings.repository_root == Path("/data/repositories")
    assert settings.source_maximum_snippet_lines == 40
    assert settings.agent_timeout_seconds == 10.0
    assert settings.agent_max_retries == 2
    assert settings.agent_retry_base_seconds == 0.1
    assert settings.gateway_maximum_concurrent_chats == 32
    assert settings.llm_enabled is False
    assert settings.llm_api_key is None
    assert settings.llm_model == "openai/gpt-oss-20b"
    assert settings.llm_base_url == "https://api.groq.com/openai/v1"


def test_settings_can_be_overridden_by_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REPO_CHAT_ENVIRONMENT", "testing")
    monkeypatch.setenv("REPO_CHAT_SERVICE_NAME", "indexer")
    monkeypatch.setenv("REPO_CHAT_REDIS_URL", "redis://redis:6379/3")
    monkeypatch.setenv("REPO_CHAT_AGENT_TIMEOUT_SECONDS", "4.5")
    monkeypatch.setenv("REPO_CHAT_AGENT_MAX_RETRIES", "4")

    settings = Settings(_env_file=None)

    assert settings.environment is Environment.TESTING
    assert settings.service_name == "indexer"
    assert settings.redis_url == "redis://redis:6379/3"
    assert settings.agent_timeout_seconds == 4.5
    assert settings.agent_max_retries == 4


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("REPO_CHAT_AGENT_TIMEOUT_SECONDS", "0"),
        ("REPO_CHAT_AGENT_MAX_RETRIES", "-1"),
        ("REPO_CHAT_AGENT_MAX_RETRIES", "6"),
        ("REPO_CHAT_GATEWAY_MAXIMUM_CONCURRENT_CHATS", "0"),
    ],
)
def test_settings_reject_invalid_agent_policy(
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
    value: str,
) -> None:
    monkeypatch.setenv(variable, value)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_secret_value_is_masked_in_string_representations() -> None:
    settings = Settings(_env_file=None, neo4j_password="super-secret")

    assert str(settings.neo4j_password) == "**********"
    assert "super-secret" not in repr(settings)


def test_get_settings_returns_cached_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("REPO_CHAT_SERVICE_NAME", "gateway")

    first = get_settings()
    second = get_settings()

    assert first is second
    assert first.service_name == "gateway"
    get_settings.cache_clear()
