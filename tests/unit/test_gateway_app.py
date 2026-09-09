import asyncio
from typing import Any

import httpx
import pytest

from repo_chat.contracts.gateway import ChatRequest
from repo_chat.gateway.app import _sse_chat_events, create_app
from repo_chat.gateway.service import GatewayService


class FakeOrchestratorClient:
    def __init__(self, *, healthy: bool = True, responses: dict[str, Any] | None = None) -> None:
        self.healthy = healthy
        self.responses = responses or {}

    async def call(self, tool: str, _arguments: dict[str, Any]) -> Any:
        if tool in self.responses:
            return self.responses[tool]
        return {
            "answer": "grounded answer",
            "agents_used": ["graph"],
            "evidence": [],
            "warnings": [],
            "partial": False,
        }

    async def healthcheck(self) -> bool:
        return self.healthy


class SlowOrchestratorClient(FakeOrchestratorClient):
    def __init__(self) -> None:
        super().__init__()
        self.cancelled = False

    async def call(self, tool: str, arguments: dict[str, Any]) -> Any:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return await super().call(tool, arguments)


class ControlledOrchestratorClient(FakeOrchestratorClient):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def call(self, tool: str, arguments: dict[str, Any]) -> Any:
        self.started.set()
        await self.release.wait()
        return await super().call(tool, arguments)


class DisconnectedRequest:
    async def is_disconnected(self) -> bool:
        return True


@pytest.mark.asyncio
async def test_root_serves_repository_chat_interface() -> None:
    app = create_app(GatewayService(FakeOrchestratorClient()))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/")
        stylesheet = await client.get("/static/styles.css")
        script = await client.get("/static/app.js")

    assert response.status_code == 200
    assert "FastAPI Repository Copilot" in response.text
    assert stylesheet.status_code == 200
    assert script.status_code == 200


@pytest.mark.asyncio
async def test_chat_generates_session_and_propagates_trace_id() -> None:
    app = create_app(GatewayService(FakeOrchestratorClient()))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/chat",
            headers={"x-correlation-id": "trace-1"},
            json={"message": "What is APIRouter?"},
        )

    assert response.status_code == 200
    assert response.headers["x-correlation-id"] == "trace-1"
    assert response.json()["trace_id"] == "trace-1"
    assert response.json()["session_id"]


@pytest.mark.asyncio
async def test_chat_admission_rejects_excess_work_and_recovers() -> None:
    orchestrator = ControlledOrchestratorClient()
    app = create_app(GatewayService(orchestrator), maximum_concurrent_chats=1)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        first = asyncio.create_task(client.post("/api/chat", json={"message": "first"}))
        await orchestrator.started.wait()
        rejected = await client.post("/api/chat", json={"message": "second"})
        orchestrator.release.set()
        completed = await first
        recovered = await client.post("/api/chat", json={"message": "third"})

    assert rejected.status_code == 429
    assert rejected.headers["retry-after"] == "1"
    assert rejected.json()["error_code"] == "chat_capacity_exhausted"
    assert completed.status_code == 200
    assert recovered.status_code == 200


@pytest.mark.asyncio
async def test_chat_streams_typed_sse_events() -> None:
    app = create_app(GatewayService(FakeOrchestratorClient()))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/chat",
            json={"message": "What is APIRouter?", "stream": True},
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: status" in response.text
    assert "event: token" in response.text
    assert "event: done" in response.text
    assert response.text.index("orchestration_started") < response.text.index("completed")


@pytest.mark.asyncio
async def test_sse_disconnect_cancels_orchestration() -> None:
    client = SlowOrchestratorClient()
    events = _sse_chat_events(
        GatewayService(client),
        ChatRequest(message="question", stream=True),
        DisconnectedRequest(),  # type: ignore[arg-type]
        "session",
        "trace",
    )

    first = await anext(events)
    await asyncio.sleep(0)
    remaining = [event async for event in events]

    assert "orchestration_started" in first
    assert remaining == []
    assert client.cancelled is True


@pytest.mark.asyncio
async def test_readiness_reflects_orchestrator_health() -> None:
    app = create_app(GatewayService(FakeOrchestratorClient(healthy=False)))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "dependencies": {"orchestrator": False},
    }


@pytest.mark.asyncio
async def test_operational_endpoints_delegate_to_agents() -> None:
    job = {
        "id": "job-1",
        "repository_id": "fastapi",
        "repository_path": "/repos/fastapi",
        "revision": "abc123",
        "mode": "full",
        "status": "pending",
        "created_at": "2026-09-03T00:00:00Z",
    }
    indexer = FakeOrchestratorClient(responses={"index_repository": job, "get_index_status": job})
    graph = FakeOrchestratorClient(
        responses={
            "execute_query": {
                "columns": ["kind", "entity_count", "relationship_count"],
                "rows": [{"kind": "class", "entity_count": 2, "relationship_count": 1}],
                "truncated": False,
            }
        }
    )
    service = GatewayService(
        FakeOrchestratorClient(),
        indexer=indexer,
        graph=graph,
        code_analyst=FakeOrchestratorClient(),
    )
    app = create_app(service)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        accepted = await client.post(
            "/api/index",
            json={
                "repository_path": "fastapi",
                "repository_id": "fastapi",
                "revision": "abc123",
                "mode": "full",
            },
        )
        status = await client.get("/api/index/status/job-1")
        health = await client.get("/api/agents/health")
        statistics = await client.get(
            "/api/graph/statistics",
            params={"repository_id": "fastapi", "revision": "abc123"},
        )

    assert accepted.status_code == 202
    assert accepted.json()["job_id"] == "job-1"
    assert status.json()["id"] == "job-1"
    assert health.json()["status"] == "healthy"
    assert statistics.json()["entity_count"] == 2
