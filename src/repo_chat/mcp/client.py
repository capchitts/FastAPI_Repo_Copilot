"""Short-lived MCP 2.x Streamable HTTP tool client with bounded retries."""

import asyncio
import random
from collections.abc import Awaitable, Callable
from time import perf_counter
from typing import Any

import httpx
import httpx2
import structlog
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from repo_chat.exceptions.base import AgentError, AgentTimeoutError, AgentUnavailableError
from repo_chat.observability.context import get_correlation_id

Sleep = Callable[[float], Awaitable[None]]
Jitter = Callable[[], float]
logger = structlog.get_logger(__name__)


class MCPToolClient:
    """Invoke structured MCP tools through an initialized client session."""

    _SAFE_RETRY_TOOLS = {
        "analyze_query",
        "route_to_agents",
        "get_conversation_context",
        "get_session_preferences",
        "synthesize_response",
        "get_index_status",
        "parse_python_ast",
        "extract_entities",
        "find_entity",
        "get_dependencies",
        "get_dependents",
        "trace_imports",
        "find_related",
        "execute_query",
        "analyze_function",
        "analyze_class",
        "find_patterns",
        "get_code_snippet",
        "get_repository_metadata",
        "read_file",
        "verify_source",
        "search_source",
        "explain_implementation",
        "compare_implementations",
    }

    def __init__(
        self,
        url: str,
        *,
        timeout_seconds: float = 10.0,
        maximum_retries: int = 2,
        retry_base_seconds: float = 0.1,
        sleep: Sleep = asyncio.sleep,
        jitter: Jitter = random.random,
    ) -> None:
        if maximum_retries < 0:
            raise ValueError("maximum_retries cannot be negative")
        if retry_base_seconds < 0:
            raise ValueError("retry_base_seconds cannot be negative")
        self._url = url
        self._timeout_seconds = timeout_seconds
        self._maximum_retries = maximum_retries
        self._retry_base_seconds = retry_base_seconds
        self._sleep = sleep
        self._jitter = jitter

    async def call(self, tool: str, arguments: dict[str, Any]) -> Any:
        """Retry safe tools only when transport failures are transient."""
        for attempt in range(self._maximum_retries + 1):
            started = perf_counter()
            try:
                result = await self._call_once(tool, arguments)
                logger.info(
                    "mcp_tool_call_completed",
                    tool=tool,
                    url=self._url,
                    attempt=attempt + 1,
                    duration_ms=round((perf_counter() - started) * 1_000, 3),
                    outcome="success",
                )
                return result
            except Exception as error:
                normalized = self._normalize_error(tool, error)
                if (
                    tool not in self._SAFE_RETRY_TOOLS
                    or not normalized.retryable
                    or attempt == self._maximum_retries
                ):
                    logger.error(
                        "mcp_tool_call_failed",
                        tool=tool,
                        url=self._url,
                        attempt=attempt + 1,
                        duration_ms=round((perf_counter() - started) * 1_000, 3),
                        outcome="error",
                        error_code=normalized.code,
                    )
                    raise normalized from error
                delay = self._retry_base_seconds * (2**attempt) * (0.5 + self._jitter())
                logger.warning(
                    "mcp_tool_call_retrying",
                    tool=tool,
                    url=self._url,
                    attempt=attempt + 1,
                    delay_seconds=round(delay, 3),
                    error_code=normalized.code,
                )
                await self._sleep(delay)
        raise AssertionError("retry loop must return or raise")

    async def _call_once(self, tool: str, arguments: dict[str, Any]) -> Any:
        """Perform one MCP session and structured tool invocation."""
        async with httpx2.AsyncClient(
            headers=self._request_headers(),
            timeout=self._timeout_seconds,
        ) as http_client:
            async with streamable_http_client(self._url, http_client=http_client) as (
                read_stream,
                write_stream,
            ):
                async with ClientSession(
                    read_stream,
                    write_stream,
                    read_timeout_seconds=self._timeout_seconds,
                ) as session:
                    await session.initialize()
                    result = await session.call_tool(
                        tool,
                        arguments,
                        read_timeout_seconds=self._timeout_seconds,
                    )
        if getattr(result, "is_error", False):
            reason = self._tool_error_reason(result)
            raise AgentError(
                f"MCP tool '{tool}' returned an error: {reason}"
                if reason
                else f"MCP tool '{tool}' returned an error",
                details={"url": self._url},
            )
        structured = getattr(result, "structured_content", None)
        if structured is None:
            raise AgentError(
                f"MCP tool '{tool}' did not return structured content",
                details={"url": self._url},
            )
        return structured

    @staticmethod
    def _tool_error_reason(result: Any) -> str | None:
        """Extract a bounded MCP error message without exposing request arguments."""
        for item in getattr(result, "content", []):
            value = getattr(item, "text", None)
            if isinstance(value, str) and value.strip():
                return value.strip()[:500]
        return None

    @staticmethod
    def _request_headers() -> dict[str, str]:
        return {"x-correlation-id": get_correlation_id()}

    def _normalize_error(self, tool: str, error: Exception) -> AgentError:
        if isinstance(error, AgentError):
            return error
        if self._contains_error(error, (TimeoutError, httpx.TimeoutException)) or (
            self._contains_named_error(error, {"ConnectTimeout", "ReadTimeout", "WriteTimeout"})
        ):
            return AgentTimeoutError(
                f"MCP tool '{tool}' timed out",
                details={"url": self._url},
            )
        if self._contains_error(error, (ConnectionError, OSError, httpx.TransportError)) or (
            self._contains_named_error(
                error,
                {
                    "ConnectError",
                    "NetworkError",
                    "ProtocolError",
                    "ReadError",
                    "WriteError",
                },
            )
        ):
            return AgentUnavailableError(
                f"MCP server unavailable while calling '{tool}'",
                details={"url": self._url},
            )
        return AgentError(
            f"MCP tool '{tool}' failed: {error}",
            details={"url": self._url},
        )

    @classmethod
    def _contains_error(
        cls, error: BaseException, kinds: type[BaseException] | tuple[type[BaseException], ...]
    ) -> bool:
        if isinstance(error, kinds):
            return True
        if isinstance(error, BaseExceptionGroup):
            return any(cls._contains_error(child, kinds) for child in error.exceptions)
        return False

    @classmethod
    def _contains_named_error(cls, error: BaseException, names: set[str]) -> bool:
        """Recognize transport forks such as the MCP SDK's ``httpx2`` package."""
        if type(error).__name__ in names and type(error).__module__.startswith(
            ("httpx", "httpcore")
        ):
            return True
        if isinstance(error, BaseExceptionGroup):
            return any(cls._contains_named_error(child, names) for child in error.exceptions)
        return False

    async def healthcheck(self) -> bool:
        """Verify that the remote MCP server can initialize and list tools."""
        try:
            async with streamable_http_client(self._url) as (read_stream, write_stream):
                async with ClientSession(
                    read_stream,
                    write_stream,
                    read_timeout_seconds=self._timeout_seconds,
                ) as session:
                    await session.initialize()
                    await session.list_tools()
        except Exception:
            return False
        return True
