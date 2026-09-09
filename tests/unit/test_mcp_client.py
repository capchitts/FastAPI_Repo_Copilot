from typing import Any

import httpx
import pytest

from repo_chat.exceptions.base import AgentError, AgentTimeoutError, AgentUnavailableError
from repo_chat.mcp.client import MCPToolClient
from repo_chat.observability.context import correlation_id_var


@pytest.mark.asyncio
async def test_retries_safe_tool_with_exponential_backoff() -> None:
    delays: list[float] = []
    attempts = 0

    async def sleep(delay: float) -> None:
        delays.append(delay)

    async def call_once(_tool: str, _arguments: dict[str, Any]) -> dict[str, bool]:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ConnectionError("temporary refusal")
        return {"ok": True}

    client = MCPToolClient(
        "http://agent/mcp",
        maximum_retries=2,
        retry_base_seconds=0.1,
        sleep=sleep,
        jitter=lambda: 0.5,
    )
    client._call_once = call_once  # type: ignore[method-assign]

    result = await client.call("find_entity", {"name": "APIRouter"})

    assert result == {"ok": True}
    assert attempts == 3
    assert delays == [0.1, 0.2]


@pytest.mark.asyncio
async def test_retry_exhaustion_raises_typed_transient_error() -> None:
    attempts = 0

    async def call_once(_tool: str, _arguments: dict[str, Any]) -> Any:
        nonlocal attempts
        attempts += 1
        raise TimeoutError("slow")

    client = MCPToolClient(
        "http://agent/mcp",
        maximum_retries=1,
        retry_base_seconds=0,
    )
    client._call_once = call_once  # type: ignore[method-assign]

    with pytest.raises(AgentTimeoutError, match="timed out"):
        await client.call("get_dependencies", {"entity_id": "entity"})

    assert attempts == 2


@pytest.mark.asyncio
async def test_does_not_retry_mutating_or_non_transient_failures() -> None:
    attempts = 0

    async def unavailable(_tool: str, _arguments: dict[str, Any]) -> Any:
        nonlocal attempts
        attempts += 1
        raise ConnectionError("offline")

    client = MCPToolClient("http://agent/mcp", maximum_retries=3, retry_base_seconds=0)
    client._call_once = unavailable  # type: ignore[method-assign]

    with pytest.raises(AgentUnavailableError):
        await client.call("index_repository", {"repository_id": "fastapi"})
    assert attempts == 1

    attempts = 0

    async def invalid(_tool: str, _arguments: dict[str, Any]) -> Any:
        nonlocal attempts
        attempts += 1
        raise AgentError("invalid request")

    client._call_once = invalid  # type: ignore[method-assign]
    with pytest.raises(AgentError, match="invalid request"):
        await client.call("find_entity", {"name": ""})
    assert attempts == 1


@pytest.mark.asyncio
async def test_nested_transport_error_is_classified_as_unavailable() -> None:
    async def call_once(_tool: str, _arguments: dict[str, Any]) -> Any:
        raise ExceptionGroup("transport", [httpx.ConnectError("connection reset")])

    client = MCPToolClient("http://agent/mcp", maximum_retries=0)
    client._call_once = call_once  # type: ignore[method-assign]

    with pytest.raises(AgentUnavailableError, match="unavailable"):
        await client.call("find_entity", {"name": "APIRouter"})


@pytest.mark.asyncio
async def test_mcp_httpx_fork_transport_error_is_classified_as_unavailable() -> None:
    connect_error = type("ConnectError", (Exception,), {"__module__": "httpx2"})

    async def call_once(_tool: str, _arguments: dict[str, Any]) -> Any:
        raise ExceptionGroup("transport", [connect_error("connection refused")])

    client = MCPToolClient("http://agent/mcp", maximum_retries=0)
    client._call_once = call_once  # type: ignore[method-assign]

    with pytest.raises(AgentUnavailableError, match="unavailable"):
        await client.call("find_entity", {"name": "APIRouter"})


def test_rejects_invalid_retry_configuration() -> None:
    with pytest.raises(ValueError, match="maximum_retries"):
        MCPToolClient("http://agent/mcp", maximum_retries=-1)
    with pytest.raises(ValueError, match="retry_base_seconds"):
        MCPToolClient("http://agent/mcp", retry_base_seconds=-0.1)


def test_downstream_headers_use_active_correlation_id() -> None:
    token = correlation_id_var.set("trace-456")
    try:
        headers = MCPToolClient._request_headers()
    finally:
        correlation_id_var.reset(token)

    assert headers == {"x-correlation-id": "trace-456"}
