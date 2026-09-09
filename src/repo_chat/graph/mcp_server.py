"""MCP 2.x server exposing read-only knowledge-graph tools."""

from typing import Any

from mcp.server.mcpserver import MCPServer

from repo_chat.config.settings import Settings
from repo_chat.contracts.graph_queries import (
    CypherResult,
    EntitySearchResult,
    HybridSearchResult,
    TraversalResult,
)
from repo_chat.contracts.indexing import EntityKind, RelationshipKind
from repo_chat.graph.client import Neo4jClient
from repo_chat.graph.query_service import GraphQueryService
from repo_chat.observability.logging import configure_logging
from repo_chat.observability.mcp import MCPObservabilityMiddleware
from repo_chat.semantic.store import QdrantSemanticStore


def create_server(service: GraphQueryService) -> MCPServer[None]:
    """Create the Graph Query MCP server around its typed service."""
    server: MCPServer[None] = MCPServer(
        name="fastapi-repository-graph-query",
        title="FastAPI Repository Graph Query Agent",
        description="Performs safe entity lookup and bounded graph traversal.",
        version="0.1.0",
        middleware=[MCPObservabilityMiddleware("graph-agent")],
    )

    @server.tool(name="find_entity", structured_output=True)
    async def find_entity_tool(
        name: str,
        kind: EntityKind | None = None,
        limit: int = 20,
    ) -> EntitySearchResult:
        """Find a class, function, method, module, or other indexed entity."""
        return await service.find_entity(name, kind=kind, limit=limit)

    @server.tool(name="get_dependencies", structured_output=True)
    async def get_dependencies_tool(entity_id: str, max_depth: int = 1) -> TraversalResult:
        """Find what an indexed entity depends on, up to five graph hops."""
        return await service.get_dependencies(entity_id, max_depth=max_depth)

    @server.tool(name="hybrid_search", structured_output=True)
    async def hybrid_search_tool(
        query: str,
        repository_id: str,
        revision: str,
        limit: int = 10,
    ) -> HybridSearchResult:
        """Fuse exact graph lookup with revision-filtered semantic code search."""
        return await service.hybrid_search(
            query,
            repository_id=repository_id,
            revision=revision,
            limit=limit,
        )

    @server.tool(name="get_dependents", structured_output=True)
    async def get_dependents_tool(entity_id: str, max_depth: int = 1) -> TraversalResult:
        """Find indexed entities that depend on the selected entity."""
        return await service.get_dependents(entity_id, max_depth=max_depth)

    @server.tool(name="trace_imports", structured_output=True)
    async def trace_imports_tool(module_name: str, max_depth: int = 3) -> TraversalResult:
        """Trace a bounded import path beginning at a qualified module name."""
        return await service.trace_imports(module_name, max_depth=max_depth)

    @server.tool(name="find_related", structured_output=True)
    async def find_related_tool(
        entity_id: str,
        relationship_type: RelationshipKind,
        max_depth: int = 1,
    ) -> TraversalResult:
        """Find entities connected by an allowlisted relationship type."""
        return await service.find_related(
            entity_id,
            relationship_type,
            max_depth=max_depth,
        )

    @server.tool(name="execute_query", structured_output=True)
    async def execute_query_tool(
        query: str,
        parameters: dict[str, Any] | None = None,
        limit: int = 100,
    ) -> CypherResult:
        """Execute one validated read-only Cypher query with a bounded result set."""
        return await service.execute_query(query, parameters=parameters, limit=limit)

    return server


def main() -> None:
    """Run the Graph Query Agent over MCP Streamable HTTP."""
    settings = Settings(service_name="graph-agent")
    configure_logging(settings.log_level)
    client = Neo4jClient(settings)
    semantic_store = None
    if settings.semantic_search_enabled:
        semantic_store = QdrantSemanticStore(
            url=settings.qdrant_url,
            collection=settings.qdrant_collection,
            model_name=settings.embedding_model,
            api_key=settings.qdrant_api_key.get_secret_value()
            if settings.qdrant_api_key
            else None,
            maximum_characters=settings.semantic_chunk_maximum_characters,
            cache_dir=str(settings.embedding_cache_dir),
        )
    service = GraphQueryService(client.driver, settings.neo4j_database, semantic_store)
    server = create_server(service)
    server.run(
        transport="streamable-http",
        host=settings.mcp_host,
        port=settings.graph_agent_port,
    )


if __name__ == "__main__":
    main()
