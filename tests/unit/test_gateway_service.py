from typing import Any

import pytest

from repo_chat.contracts.gateway import ChatRequest, IndexRequest
from repo_chat.contracts.repository import RepositoryAcquireRequest
from repo_chat.gateway.service import GatewayService


class FakeOrchestratorClient:
    def __init__(self, *, healthy: bool = True, responses: dict[str, Any] | None = None) -> None:
        self.healthy = healthy
        self.responses = responses or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, tool: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((tool, arguments))
        if tool in self.responses:
            return self.responses[tool]
        return {
            "answer": "APIRouter is implemented in routing.py",
            "agents_used": ["graph", "code_analyst"],
            "evidence": [],
            "warnings": [],
            "partial": False,
        }

    async def healthcheck(self) -> bool:
        return self.healthy


@pytest.mark.asyncio
async def test_gateway_service_calls_orchestrator_chat() -> None:
    client = FakeOrchestratorClient()
    service = GatewayService(client)
    request = ChatRequest(
        message="Explain APIRouter",
        repository_id="fastapi",
        revision="abc123",
    )

    response = await service.chat(request, "session-1", "trace-1")

    assert response.session_id == "session-1"
    assert response.trace_id == "trace-1"
    assert client.calls == [
        (
            "chat",
            {
                "session_id": "session-1",
                "user_id": None,
                "message": "Explain APIRouter",
                "repository_id": "fastapi",
                "revision": "abc123",
            },
        )
    ]


@pytest.mark.asyncio
async def test_gateway_service_reports_dependency_health() -> None:
    service = GatewayService(FakeOrchestratorClient(healthy=False))

    assert await service.ready() is False


def index_job() -> dict[str, Any]:
    return {
        "id": "job-1",
        "repository_id": "fastapi",
        "repository_path": "/repos/fastapi",
        "revision": "abc123",
        "mode": "full",
        "status": "pending",
        "created_at": "2026-09-03T00:00:00Z",
    }


@pytest.mark.asyncio
async def test_gateway_starts_and_retrieves_index_job() -> None:
    indexer = FakeOrchestratorClient(
        responses={"index_repository": index_job(), "get_index_status": index_job()}
    )
    service = GatewayService(FakeOrchestratorClient(), indexer=indexer)
    request = IndexRequest(
        repository_path="fastapi",
        repository_id="fastapi",
        revision="abc123",
    )

    accepted = await service.start_index(request)
    status = await service.index_status("job-1")

    assert accepted.job_id == "job-1"
    assert accepted.status_url == "/api/index/status/job-1"
    assert status.id == "job-1"


@pytest.mark.asyncio
async def test_gateway_acquires_repository_through_repository_agent() -> None:
    acquired = {
        "repository_id": "fastapi-next",
        "remote_url": "https://github.com/fastapi/fastapi.git",
        "requested_ref": "main",
        "revision": "a" * 40,
        "commit_timestamp": "2026-09-04T00:00:00Z",
        "captured_at": "2026-09-04T00:01:00Z",
        "repository_path": "/data/repositories/fastapi-next/revisions/" + "a" * 40,
    }
    repository = FakeOrchestratorClient(responses={"acquire_repository": acquired})
    service = GatewayService(FakeOrchestratorClient(), repository=repository)
    request = RepositoryAcquireRequest(
        repository_id="fastapi-next",
        remote_url="https://github.com/fastapi/fastapi.git",
        ref="main",
    )

    result = await service.acquire_repository(request)

    assert result.revision == "a" * 40
    assert repository.calls == [("acquire_repository", request.model_dump(mode="json"))]


@pytest.mark.asyncio
async def test_gateway_aggregates_health_and_graph_statistics() -> None:
    graph = FakeOrchestratorClient(
        responses={
            "execute_query": {
                "columns": ["kind", "entity_count", "relationship_count"],
                "rows": [
                    {"kind": "class", "entity_count": 2, "relationship_count": 3},
                    {"kind": "function", "entity_count": 4, "relationship_count": 5},
                ],
                "truncated": False,
            }
        }
    )
    service = GatewayService(
        FakeOrchestratorClient(),
        indexer=FakeOrchestratorClient(),
        graph=graph,
        code_analyst=FakeOrchestratorClient(),
    )

    health = await service.agents_health()
    statistics = await service.graph_statistics("fastapi", "abc123")

    assert health.status == "healthy"
    assert len(health.agents) == 4
    assert statistics.entity_count == 6
    assert statistics.relationship_count == 8


@pytest.mark.asyncio
async def test_gateway_health_includes_fifth_repository_agent_when_configured() -> None:
    service = GatewayService(
        FakeOrchestratorClient(),
        indexer=FakeOrchestratorClient(),
        graph=FakeOrchestratorClient(),
        code_analyst=FakeOrchestratorClient(),
        repository=FakeOrchestratorClient(),
    )

    health = await service.agents_health()

    assert health.status == "healthy"
    assert [agent.agent for agent in health.agents] == [
        "orchestrator",
        "indexer",
        "graph",
        "code_analyst",
        "repository",
    ]
