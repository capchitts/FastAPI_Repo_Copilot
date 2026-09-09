import logging
from types import SimpleNamespace
from uuid import UUID

import pytest

from repo_chat.observability.context import (
    correlation_id_var,
    create_correlation_id,
    get_correlation_id,
)
from repo_chat.observability.logging import add_correlation_id, configure_logging
from repo_chat.observability.mcp import MCPObservabilityMiddleware


def test_create_correlation_id_returns_uuid() -> None:
    correlation_id = create_correlation_id()

    assert str(UUID(correlation_id)) == correlation_id


def test_correlation_id_context_can_be_set_and_reset() -> None:
    token = correlation_id_var.set("request-123")

    try:
        assert get_correlation_id() == "request-123"
    finally:
        correlation_id_var.reset(token)

    assert get_correlation_id() == "unassigned"


def test_logging_processor_adds_current_correlation_id() -> None:
    token = correlation_id_var.set("request-456")

    try:
        event = add_correlation_id(None, "info", {"event": "query_received"})
    finally:
        correlation_id_var.reset(token)

    assert event == {
        "event": "query_received",
        "correlation_id": "request-456",
    }


def test_configure_logging_uses_valid_message_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured: dict[str, object] = {}

    def basic_config(**options: object) -> None:
        configured.update(options)

    monkeypatch.setattr(logging, "basicConfig", basic_config)
    configure_logging("info")

    assert configured["format"] == "%(message)s"
    assert configured["level"] == "INFO"
    assert configured["force"] is True


@pytest.mark.asyncio
async def test_mcp_middleware_binds_inbound_trace_and_resets_context() -> None:
    observed: list[str] = []
    middleware = MCPObservabilityMiddleware("test-agent")
    context = SimpleNamespace(
        request=SimpleNamespace(headers={"x-correlation-id": "trace-123"}),
        method="tools/call",
    )

    async def call_next(_context: object) -> dict[str, bool]:
        observed.append(get_correlation_id())
        return {"ok": True}

    result = await middleware(context, call_next)  # type: ignore[arg-type]

    assert result == {"ok": True}
    assert observed == ["trace-123"]
    assert get_correlation_id() == "unassigned"
