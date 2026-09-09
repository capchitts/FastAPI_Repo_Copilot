"""MCP request middleware for correlation and structured timing logs."""

from time import perf_counter
from typing import Any

import structlog
from mcp.server.context import CallNext, HandlerResult, ServerRequestContext

from repo_chat.observability.context import correlation_id_var, create_correlation_id


class MCPObservabilityMiddleware:
    """Bind inbound trace headers to context and log each MCP request boundary."""

    def __init__(self, service_name: str) -> None:
        self._service_name = service_name
        self._logger = structlog.get_logger(service_name)

    async def __call__(
        self,
        ctx: ServerRequestContext[Any, Any],
        call_next: CallNext,
    ) -> HandlerResult:
        headers = getattr(ctx.request, "headers", None)
        trace_id = headers.get("x-correlation-id") if headers is not None else None
        token = correlation_id_var.set(trace_id or create_correlation_id())
        started = perf_counter()
        try:
            result = await call_next(ctx)
            self._logger.info(
                "mcp_request_completed",
                service=self._service_name,
                method=ctx.method,
                duration_ms=round((perf_counter() - started) * 1_000, 3),
                outcome="success",
            )
            return result
        except Exception as error:
            self._logger.error(
                "mcp_request_failed",
                service=self._service_name,
                method=ctx.method,
                duration_ms=round((perf_counter() - started) * 1_000, 3),
                outcome="error",
                error_type=type(error).__name__,
            )
            raise
        finally:
            correlation_id_var.reset(token)
