"""Gateway application service for Orchestrator MCP calls."""

import asyncio
from time import perf_counter
from typing import Any, Protocol

from repo_chat.contracts.agents import AgentHealth, HealthStatus
from repo_chat.contracts.gateway import (
    AgentsHealthResponse,
    ChatRequest,
    ChatResponse,
    IndexAccepted,
    IndexRequest,
)
from repo_chat.contracts.indexing import GraphStatistics, IndexJob
from repo_chat.contracts.orchestration import SessionPreferences, SynthesizedResponse
from repo_chat.contracts.repository import RepositoryAcquireRequest, RepositoryAcquisition
from repo_chat.exceptions.base import AgentError


class OrchestratorClient(Protocol):
    """Minimal MCP client boundary used by the gateway."""

    async def call(self, tool: str, arguments: dict[str, Any]) -> Any: ...

    async def healthcheck(self) -> bool: ...


class GatewayService:
    """Translate public API requests to typed Orchestrator calls."""

    def __init__(
        self,
        orchestrator: OrchestratorClient,
        *,
        indexer: OrchestratorClient | None = None,
        graph: OrchestratorClient | None = None,
        code_analyst: OrchestratorClient | None = None,
        repository: OrchestratorClient | None = None,
    ) -> None:
        self._orchestrator = orchestrator
        self._indexer = indexer
        self._graph = graph
        self._repository = repository
        self._agents = {
            "orchestrator": orchestrator,
            "indexer": indexer,
            "graph": graph,
            "code_analyst": code_analyst,
        }
        if repository is not None:
            self._agents["repository"] = repository

    async def chat(self, request: ChatRequest, session_id: str, trace_id: str) -> ChatResponse:
        """Execute one complete chat turn through the Orchestrator."""
        raw = await self._orchestrator.call(
            "chat",
            {
                "session_id": session_id,
                "user_id": request.user_id,
                "message": request.message,
                "repository_id": request.repository_id,
                "revision": request.revision,
            },
        )
        synthesized = SynthesizedResponse.model_validate(raw)
        return ChatResponse(
            **synthesized.model_dump(),
            session_id=session_id,
            trace_id=trace_id,
        )

    async def ready(self) -> bool:
        """Return whether the Orchestrator MCP dependency is reachable."""
        return await self._orchestrator.healthcheck()

    async def get_preferences(self, session_id: str) -> SessionPreferences:
        raw = await self._orchestrator.call(
            "get_session_preferences", {"session_id": session_id}
        )
        return SessionPreferences.model_validate(raw)

    async def set_preferences(
        self, session_id: str, preferences: SessionPreferences
    ) -> SessionPreferences:
        raw = await self._orchestrator.call(
            "set_session_preferences",
            {"session_id": session_id, "preferences": preferences.model_dump(mode="json")},
        )
        return SessionPreferences.model_validate(raw)

    async def start_index(self, request: IndexRequest) -> IndexAccepted:
        """Start an Indexer job and return its public status reference."""
        if self._indexer is None:
            raise AgentError("Indexer MCP client is not configured")
        raw = await self._indexer.call("index_repository", request.model_dump(mode="json"))
        job = IndexJob.model_validate(raw)
        return IndexAccepted(
            job_id=job.id,
            status=job.status,
            status_url=f"/api/index/status/{job.id}",
        )

    async def index_status(self, job_id: str) -> IndexJob:
        """Retrieve an Indexer job through MCP."""
        if self._indexer is None:
            raise AgentError("Indexer MCP client is not configured")
        raw = await self._indexer.call("get_index_status", {"job_id": job_id})
        return IndexJob.model_validate(raw)

    async def acquire_repository(
        self, request: RepositoryAcquireRequest
    ) -> RepositoryAcquisition:
        """Acquire an approved Git ref through the Repository MCP boundary."""
        if self._repository is None:
            raise AgentError("Repository MCP client is not configured")
        raw = await self._repository.call(
            "acquire_repository", request.model_dump(mode="json")
        )
        return RepositoryAcquisition.model_validate(raw)

    async def agents_health(self) -> AgentsHealthResponse:
        """Check all configured MCP servers concurrently."""
        health = await asyncio.gather(
            *(self._agent_health(name, client) for name, client in self._agents.items())
        )
        return AgentsHealthResponse(
            status="healthy"
            if all(item.status is HealthStatus.HEALTHY for item in health)
            else "degraded",
            agents=health,
        )

    async def graph_statistics(self, repository_id: str, revision: str) -> GraphStatistics:
        """Aggregate entity and relationship counts through safe Graph MCP Cypher."""
        if self._graph is None:
            raise AgentError("Graph MCP client is not configured")
        raw = await self._graph.call(
            "execute_query",
            {
                "query": """
                    MATCH (entity:Entity)-[:AT_REVISION]->(:Revision {id: $revision_id})
                    OPTIONAL MATCH (entity)-[relationship]->()
                    WHERE type(relationship) <> 'AT_REVISION'
                    RETURN entity.kind AS kind,
                           count(DISTINCT entity) AS entity_count,
                           count(DISTINCT relationship) AS relationship_count
                """,
                "parameters": {"revision_id": f"{repository_id}:{revision}"},
                "limit": 100,
            },
        )
        rows = raw.get("rows", []) if isinstance(raw, dict) else []
        typed_rows = [row for row in rows if isinstance(row, dict)]
        entities_by_kind = {
            str(row["kind"]): int(row["entity_count"])
            for row in typed_rows
            if row.get("kind") is not None
        }
        return GraphStatistics(
            repository_id=repository_id,
            revision=revision,
            entity_count=sum(entities_by_kind.values()),
            relationship_count=sum(int(row.get("relationship_count", 0)) for row in typed_rows),
            entities_by_kind=entities_by_kind,
        )

    @staticmethod
    async def _agent_health(name: str, client: OrchestratorClient | None) -> AgentHealth:
        if client is None:
            return AgentHealth(
                agent=name,
                status=HealthStatus.UNHEALTHY,
                details={"reason": "not configured"},
            )
        started = perf_counter()
        healthy = await client.healthcheck()
        return AgentHealth(
            agent=name,
            status=HealthStatus.HEALTHY if healthy else HealthStatus.UNHEALTHY,
            latency_ms=(perf_counter() - started) * 1_000,
        )
