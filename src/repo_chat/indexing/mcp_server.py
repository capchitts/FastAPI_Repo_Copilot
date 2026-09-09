"""MCP 2.x server exposing the required Indexer tools."""

from mcp.server.mcpserver import MCPServer
from redis.asyncio import Redis

from repo_chat.config.settings import Settings
from repo_chat.contracts.indexing import (
    CodeEntity,
    IndexFileResult,
    IndexJob,
    IndexMode,
    ParsedFile,
)
from repo_chat.graph.client import Neo4jClient
from repo_chat.graph.repository import GraphRepository
from repo_chat.graph.schema import initialize_schema
from repo_chat.indexing.coordination import RedisIndexCoordination
from repo_chat.indexing.redis_job_store import RedisIndexJobStore
from repo_chat.indexing.redis_manifest import RedisIndexManifestStore
from repo_chat.indexing.service import IndexerService
from repo_chat.observability.logging import configure_logging
from repo_chat.observability.mcp import MCPObservabilityMiddleware
from repo_chat.semantic.store import QdrantSemanticStore


def create_server(service: IndexerService) -> MCPServer[None]:
    """Create an Indexer MCP server around a testable application service."""
    server: MCPServer[None] = MCPServer(
        name="fastapi-repository-indexer",
        title="FastAPI Repository Indexer",
        description="Parses Python repositories and maintains the shared knowledge graph.",
        version="0.1.0",
        middleware=[MCPObservabilityMiddleware("indexer")],
    )

    @server.tool(name="parse_python_ast", structured_output=True)
    def parse_python_ast_tool(
        code: str,
        repository_id: str,
        revision: str,
        file_path: str,
    ) -> ParsedFile:
        """Parse Python code and return its normalized AST entities and relationships."""
        return service.parse_python_ast(
            code,
            repository_id=repository_id,
            revision=revision,
            file_path=file_path,
        )

    @server.tool(name="extract_entities", structured_output=True)
    def extract_entities_tool(
        code: str,
        repository_id: str,
        revision: str,
        file_path: str,
    ) -> list[CodeEntity]:
        """Extract classes, functions, methods, imports, parameters, and related entities."""
        return service.extract_entities(
            code,
            repository_id=repository_id,
            revision=revision,
            file_path=file_path,
        )

    @server.tool(name="index_file", structured_output=True)
    async def index_file_tool(
        file_path: str,
        repository_path: str,
        repository_id: str,
        revision: str,
    ) -> IndexFileResult:
        """Parse and persist one Python file from an allowed local repository."""
        return await service.index_file(
            file_path,
            repository_path=repository_path,
            repository_id=repository_id,
            revision=revision,
        )

    @server.tool(name="index_repository", structured_output=True)
    async def index_repository_tool(
        repository_path: str,
        repository_id: str,
        revision: str,
        mode: IndexMode = IndexMode.FULL,
    ) -> IndexJob:
        """Start full or incremental indexing and return a trackable job."""
        return await service.index_repository(
            repository_path,
            repository_id=repository_id,
            revision=revision,
            mode=mode,
        )

    @server.tool(name="get_index_status", structured_output=True)
    async def get_index_status_tool(job_id: str) -> IndexJob:
        """Return current progress and statistics for an indexing job."""
        return await service.get_index_status(job_id)

    return server


def main() -> None:
    """Run the Indexer over MCP Streamable HTTP."""
    settings = Settings(service_name="indexer")
    configure_logging(settings.log_level)
    client = Neo4jClient(settings)
    redis = Redis.from_url(settings.redis_url)
    repository = GraphRepository(client.driver, settings.neo4j_database)
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

    async def prepare_schema() -> None:
        await client.verify_connectivity()
        await initialize_schema(client.driver, settings.neo4j_database)

    service = IndexerService(
        repository,
        repository_root=settings.repository_root,
        initialize_schema=prepare_schema,
        job_store=RedisIndexJobStore(redis, ttl_seconds=settings.index_job_ttl_seconds),
        manifest_store=RedisIndexManifestStore(redis),
        semantic_store=semantic_store,
        coordination=RedisIndexCoordination(
            redis,
            lock_ttl_seconds=settings.index_lock_ttl_seconds,
            idempotency_ttl_seconds=settings.index_job_ttl_seconds,
        ),
    )
    server = create_server(service)
    server.run(
        transport="streamable-http",
        host=settings.mcp_host,
        port=settings.mcp_port,
    )


if __name__ == "__main__":
    main()
