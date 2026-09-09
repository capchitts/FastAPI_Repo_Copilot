from repo_chat.exceptions.base import (
    AgentTimeoutError,
    AgentUnavailableError,
    ConfigurationError,
    EntityNotFoundError,
    GraphQueryError,
    IndexingError,
    RepositoryChatError,
)


def test_base_exception_preserves_message_and_details() -> None:
    error = RepositoryChatError(
        "operation failed",
        details={"repository_id": "fastapi"},
    )

    assert str(error) == "operation failed"
    assert error.message == "operation failed"
    assert error.details == {"repository_id": "fastapi"}
    assert error.code == "repository_chat_error"
    assert error.retryable is False


def test_exception_details_default_is_not_shared() -> None:
    first = RepositoryChatError("first")
    second = RepositoryChatError("second")

    first.details["changed"] = True

    assert second.details == {}


def test_transient_agent_failures_are_retryable() -> None:
    assert AgentUnavailableError("unavailable").retryable is True
    assert AgentTimeoutError("timed out").retryable is True


def test_non_transient_failures_are_not_retryable() -> None:
    errors = [
        ConfigurationError("bad settings"),
        IndexingError("parse failed"),
        GraphQueryError("invalid query"),
        EntityNotFoundError("missing"),
    ]

    assert all(error.retryable is False for error in errors)


def test_specialized_exceptions_have_stable_codes() -> None:
    assert ConfigurationError.code == "configuration_error"
    assert AgentUnavailableError.code == "agent_unavailable"
    assert AgentTimeoutError.code == "agent_timeout"
    assert IndexingError.code == "indexing_error"
    assert GraphQueryError.code == "graph_query_error"
    assert EntityNotFoundError.code == "entity_not_found"
