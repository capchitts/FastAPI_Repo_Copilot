"""Public FastAPI gateway for repository chat."""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import suppress
from pathlib import Path
from time import perf_counter
from uuid import uuid4

import structlog
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import ValidationError
from starlette.middleware.base import RequestResponseEndpoint

from repo_chat.config.settings import Settings
from repo_chat.contracts.gateway import (
    AgentsHealthResponse,
    ChatRequest,
    ChatResponse,
    IndexAccepted,
    IndexRequest,
    ReadinessResponse,
    StreamEvent,
    StreamEventType,
)
from repo_chat.contracts.indexing import GraphStatistics, IndexJob
from repo_chat.contracts.orchestration import SessionPreferences
from repo_chat.contracts.repository import RepositoryAcquireRequest, RepositoryAcquisition
from repo_chat.exceptions.base import AgentError
from repo_chat.gateway.service import GatewayService
from repo_chat.mcp.client import MCPToolClient
from repo_chat.observability.context import correlation_id_var, create_correlation_id
from repo_chat.observability.logging import configure_logging

logger = structlog.get_logger(__name__)
STATIC_DIRECTORY = Path(__file__).with_name("static")
CHAT_UI_HTML = (STATIC_DIRECTORY / "index.html").read_text(encoding="utf-8")
CHAT_UI_CSS = (STATIC_DIRECTORY / "styles.css").read_text(encoding="utf-8")
CHAT_UI_JAVASCRIPT = (STATIC_DIRECTORY / "app.js").read_text(encoding="utf-8")


def _event_sequence(response: ChatResponse) -> list[StreamEvent]:
    events: list[StreamEvent] = []
    events.extend(
        StreamEvent(type=StreamEventType.EVIDENCE, data=item.model_dump(mode="json"))
        for item in response.evidence
    )
    events.extend(
        StreamEvent(type=StreamEventType.TOKEN, data=chunk)
        for chunk in _answer_chunks(response.answer)
    )
    events.extend(
        StreamEvent(type=StreamEventType.WARNING, data=warning) for warning in response.warnings
    )
    events.append(StreamEvent(type=StreamEventType.DONE, data=response.model_dump(mode="json")))
    return events


def _answer_chunks(answer: str, maximum_characters: int = 160) -> list[str]:
    """Split a completed answer into readable transport chunks."""
    chunks: list[str] = []
    remaining = answer
    while remaining:
        boundary = min(maximum_characters, len(remaining))
        if boundary < len(remaining):
            whitespace = remaining.rfind(" ", 0, boundary + 1)
            if whitespace > maximum_characters // 2:
                boundary = whitespace + 1
        chunks.append(remaining[:boundary])
        remaining = remaining[boundary:]
    return chunks or [""]


def _sse(event: StreamEvent) -> str:
    payload = json.dumps(event.data, separators=(",", ":"))
    return f"event: {event.type.value}\ndata: {payload}\n\n"


async def _sse_chat_events(
    service: GatewayService,
    chat_request: ChatRequest,
    request: Request,
    session_id: str,
    trace_id: str,
) -> AsyncIterator[str]:
    """Stream immediate progress, heartbeats, evidence, and answer chunks."""
    task = asyncio.create_task(service.chat(chat_request, session_id, trace_id))
    yield _sse(
        StreamEvent(
            type=StreamEventType.STATUS,
            data={"state": "orchestration_started", "trace_id": trace_id},
        )
    )
    try:
        while not task.done():
            if await request.is_disconnected():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
                return
            done, _ = await asyncio.wait({task}, timeout=1.0)
            if not done:
                yield _sse(
                    StreamEvent(type=StreamEventType.STATUS, data={"state": "working"})
                )
        response = await task
        yield _sse(StreamEvent(type=StreamEventType.STATUS, data={"state": "completed"}))
        for event in _event_sequence(response):
            yield _sse(event)
    except asyncio.CancelledError:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        raise
    except Exception as error:
        yield _sse(
            StreamEvent(type=StreamEventType.ERROR, data={"message": str(error)})
        )


async def _admitted_sse_chat_events(
    service: GatewayService,
    chat_request: ChatRequest,
    request: Request,
    session_id: str,
    trace_id: str,
    admission: asyncio.BoundedSemaphore,
) -> AsyncIterator[str]:
    """Hold one admission slot for the complete streaming-response lifetime."""
    try:
        async for event in _sse_chat_events(
            service, chat_request, request, session_id, trace_id
        ):
            yield event
    finally:
        admission.release()


def create_app(service: GatewayService, *, maximum_concurrent_chats: int = 32) -> FastAPI:
    """Create the public gateway with injected dependencies."""
    if maximum_concurrent_chats < 1:
        raise ValueError("maximum_concurrent_chats must be positive")
    app = FastAPI(title="FastAPI Repository Chat Agent", version="0.1.0")
    admission = asyncio.BoundedSemaphore(maximum_concurrent_chats)

    @app.middleware("http")
    async def correlation_middleware(
        request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        trace_id = request.headers.get("x-correlation-id") or create_correlation_id()
        token = correlation_id_var.set(trace_id)
        started = perf_counter()
        try:
            response = await call_next(request)
            response.headers["x-correlation-id"] = trace_id
            logger.info(
                "gateway_request_completed",
                method=request.method,
                path=request.url.path,
                status_code=response.status_code,
                duration_ms=round((perf_counter() - started) * 1_000, 3),
                outcome="success" if response.status_code < 500 else "error",
            )
            return response
        except Exception as error:
            logger.error(
                "gateway_request_failed",
                method=request.method,
                path=request.url.path,
                duration_ms=round((perf_counter() - started) * 1_000, 3),
                outcome="error",
                error_type=type(error).__name__,
            )
            raise
        finally:
            correlation_id_var.reset(token)

    @app.exception_handler(AgentError)
    async def agent_error_handler(request: Request, error: AgentError) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={
                "detail": "An internal MCP agent request failed",
                "trace_id": request.headers.get("x-correlation-id") or correlation_id_var.get(),
                "error": str(error),
                "error_code": error.code,
            },
        )

    @app.get("/health/live")
    async def liveness() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", include_in_schema=False)
    async def chat_ui() -> HTMLResponse:
        """Serve the lightweight repository chat interface."""
        return HTMLResponse(CHAT_UI_HTML)

    @app.get("/static/styles.css", include_in_schema=False)
    async def chat_ui_styles() -> Response:
        return Response(CHAT_UI_CSS, media_type="text/css")

    @app.get("/static/app.js", include_in_schema=False)
    async def chat_ui_script() -> Response:
        return Response(CHAT_UI_JAVASCRIPT, media_type="text/javascript")

    @app.get("/health/ready", response_model=ReadinessResponse)
    async def readiness() -> Response | ReadinessResponse:
        ready = await service.ready()
        payload = ReadinessResponse(
            status="ready" if ready else "not_ready",
            dependencies={"orchestrator": ready},
        )
        if ready:
            return payload
        return JSONResponse(status_code=503, content=payload.model_dump(mode="json"))

    @app.post(
        "/api/chat",
        response_model=ChatResponse,
        responses={
            200: {"content": {"text/event-stream": {}}},
            429: {"description": "Chat capacity temporarily exhausted"},
            503: {"description": "Agent unavailable"},
        },
    )
    async def chat(chat_request: ChatRequest, request: Request) -> Response:
        session_id = chat_request.session_id or str(uuid4())
        trace_id = correlation_id_var.get()
        if admission.locked():
            return JSONResponse(
                status_code=429,
                headers={"retry-after": "1"},
                content={
                    "detail": "Chat capacity is temporarily exhausted",
                    "trace_id": trace_id,
                    "error_code": "chat_capacity_exhausted",
                },
            )
        await admission.acquire()
        if chat_request.stream:
            return StreamingResponse(
                _admitted_sse_chat_events(
                    service, chat_request, request, session_id, trace_id, admission
                ),
                media_type="text/event-stream",
                headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
            )
        try:
            response = await service.chat(chat_request, session_id, trace_id)
            return JSONResponse(content=response.model_dump(mode="json"))
        finally:
            admission.release()

    @app.post("/api/index", response_model=IndexAccepted, status_code=202)
    async def start_index(request: IndexRequest) -> IndexAccepted:
        return await service.start_index(request)

    @app.post("/api/repositories/acquire", response_model=RepositoryAcquisition)
    async def acquire_repository(
        request: RepositoryAcquireRequest,
    ) -> RepositoryAcquisition:
        return await service.acquire_repository(request)

    @app.get("/api/index/status/{job_id}", response_model=IndexJob)
    async def index_status(job_id: str) -> IndexJob:
        return await service.index_status(job_id)

    @app.get("/api/agents/health", response_model=AgentsHealthResponse)
    async def agents_health() -> AgentsHealthResponse:
        return await service.agents_health()

    @app.get(
        "/api/sessions/{session_id}/preferences", response_model=SessionPreferences
    )
    async def get_session_preferences(session_id: str) -> SessionPreferences:
        return await service.get_preferences(session_id)

    @app.put(
        "/api/sessions/{session_id}/preferences", response_model=SessionPreferences
    )
    async def set_session_preferences(
        session_id: str, preferences: SessionPreferences
    ) -> SessionPreferences:
        return await service.set_preferences(session_id, preferences)

    @app.get("/api/graph/statistics", response_model=GraphStatistics)
    async def graph_statistics(repository_id: str, revision: str) -> GraphStatistics:
        return await service.graph_statistics(repository_id, revision)

    @app.websocket("/ws/chat")
    async def websocket_chat(websocket: WebSocket) -> None:
        await websocket.accept()
        try:
            while True:
                try:
                    request = ChatRequest.model_validate(await websocket.receive_json())
                    session_id = request.session_id or str(uuid4())
                    trace_id = websocket.headers.get("x-correlation-id") or str(uuid4())
                    await websocket.send_json(
                        StreamEvent(
                            type=StreamEventType.STATUS,
                            data={"state": "orchestration_started", "trace_id": trace_id},
                        ).model_dump(mode="json")
                    )
                    chat_task = asyncio.create_task(service.chat(request, session_id, trace_id))
                    disconnect_task = asyncio.create_task(websocket.receive())
                    try:
                        while not chat_task.done():
                            done, _ = await asyncio.wait(
                                {chat_task, disconnect_task},
                                timeout=1.0,
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                            if disconnect_task in done:
                                message = disconnect_task.result()
                                chat_task.cancel()
                                with suppress(asyncio.CancelledError):
                                    await chat_task
                                if message.get("type") == "websocket.disconnect":
                                    raise WebSocketDisconnect
                                await websocket.send_json(
                                    StreamEvent(
                                        type=StreamEventType.ERROR,
                                        data={"message": "Only one active chat is supported"},
                                    ).model_dump(mode="json")
                                )
                                break
                            if not done:
                                await websocket.send_json(
                                    StreamEvent(
                                        type=StreamEventType.STATUS,
                                        data={"state": "working"},
                                    ).model_dump(mode="json")
                                )
                        if chat_task.cancelled():
                            continue
                        response = await chat_task
                    finally:
                        disconnect_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await disconnect_task
                    await websocket.send_json(
                        StreamEvent(
                            type=StreamEventType.STATUS, data={"state": "completed"}
                        ).model_dump(mode="json")
                    )
                    for event in _event_sequence(response):
                        await websocket.send_json(event.model_dump(mode="json"))
                except (ValidationError, AgentError) as error:
                    await websocket.send_json(
                        StreamEvent(
                            type=StreamEventType.ERROR,
                            data={"message": str(error)},
                        ).model_dump(mode="json")
                    )
        except WebSocketDisconnect:
            return

    return app


def build_app() -> FastAPI:
    """Build the production gateway from environment settings."""
    settings = Settings(service_name="gateway")
    configure_logging(settings.log_level)
    orchestrator = MCPToolClient(
        settings.orchestrator_mcp_url,
        timeout_seconds=settings.gateway_orchestrator_timeout_seconds,
        maximum_retries=settings.agent_max_retries,
        retry_base_seconds=settings.agent_retry_base_seconds,
    )
    indexer = MCPToolClient(
        settings.indexer_mcp_url,
        timeout_seconds=settings.agent_timeout_seconds,
        maximum_retries=settings.agent_max_retries,
        retry_base_seconds=settings.agent_retry_base_seconds,
    )
    graph = MCPToolClient(
        settings.graph_agent_mcp_url,
        timeout_seconds=settings.agent_timeout_seconds,
        maximum_retries=settings.agent_max_retries,
        retry_base_seconds=settings.agent_retry_base_seconds,
    )
    code_analyst = MCPToolClient(
        settings.code_analyst_mcp_url,
        timeout_seconds=settings.agent_timeout_seconds,
        maximum_retries=settings.agent_max_retries,
        retry_base_seconds=settings.agent_retry_base_seconds,
    )
    repository = MCPToolClient(
        settings.repository_agent_mcp_url,
        timeout_seconds=max(
            settings.agent_timeout_seconds,
            settings.git_command_timeout_seconds + 5,
        ),
        maximum_retries=settings.agent_max_retries,
        retry_base_seconds=settings.agent_retry_base_seconds,
    )
    return create_app(
        GatewayService(
            orchestrator,
            indexer=indexer,
            graph=graph,
            code_analyst=code_analyst,
            repository=repository,
        ),
        maximum_concurrent_chats=settings.gateway_maximum_concurrent_chats,
    )


app = build_app()
