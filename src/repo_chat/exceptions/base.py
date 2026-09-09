from typing import Any


class RepositoryChatError(Exception):
    """Base exception for expected application failures."""

    code = "repository_chat_error"
    retryable = False

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class ConfigurationError(RepositoryChatError):
    code = "configuration_error"


class AgentError(RepositoryChatError):
    code = "agent_error"


class AgentUnavailableError(AgentError):
    code = "agent_unavailable"
    retryable = True


class AgentTimeoutError(AgentError):
    code = "agent_timeout"
    retryable = True


class IndexingError(RepositoryChatError):
    code = "indexing_error"


class GraphQueryError(RepositoryChatError):
    code = "graph_query_error"


class EntityNotFoundError(RepositoryChatError):
    code = "entity_not_found"
