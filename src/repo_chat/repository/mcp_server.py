"""MCP server for safe revision-pinned repository access."""

from mcp.server.mcpserver import MCPServer

from repo_chat.config.settings import Settings
from repo_chat.contracts.code_analysis import CodeSnippet
from repo_chat.contracts.repository import (
    RepositoryAcquisition,
    RepositoryMetadata,
    SourceSearchResult,
    SourceVerification,
)
from repo_chat.observability.logging import configure_logging
from repo_chat.observability.mcp import MCPObservabilityMiddleware
from repo_chat.repository.git_acquisition import GitAcquisitionService
from repo_chat.repository.service import RepositoryService


def create_server(service: RepositoryService) -> MCPServer[None]:
    """Expose bounded source operations over MCP Streamable HTTP."""
    server: MCPServer[None] = MCPServer(
        name="fastapi-repository-source-agent",
        title="FastAPI Repository Source Agent",
        description="Owns safe, revision-pinned repository reads and lexical search.",
        version="0.1.0",
        middleware=[MCPObservabilityMiddleware("repository-agent")],
    )

    @server.tool(name="acquire_repository", structured_output=True)
    async def acquire_repository_tool(
        repository_id: str,
        remote_url: str,
        ref: str = "HEAD",
    ) -> RepositoryAcquisition:
        """Acquire an approved HTTPS Git ref as an immutable local snapshot."""
        return await service.acquire_repository(
            repository_id=repository_id,
            remote_url=remote_url,
            ref=ref,
        )

    @server.tool(name="get_repository_metadata", structured_output=True)
    def get_repository_metadata_tool(
        repository_id: str, revision: str
    ) -> RepositoryMetadata:
        """Return bounded metadata for a controlled repository snapshot."""
        return service.get_metadata(repository_id, revision)

    @server.tool(name="read_file", structured_output=True)
    def read_file_tool(repository_id: str, revision: str, file_path: str) -> CodeSnippet:
        """Read a complete source file subject to size and path limits."""
        return service.read_file(repository_id, revision, file_path)

    @server.tool(name="get_code_snippet", structured_output=True)
    def get_code_snippet_tool(
        repository_id: str,
        revision: str,
        file_path: str,
        start_line: int,
        end_line: int,
        context_lines: int = 3,
    ) -> CodeSnippet:
        """Read an exact source range with bounded context and evidence."""
        return service.get_code_snippet(
            repository_id=repository_id,
            revision=revision,
            file_path=file_path,
            start_line=start_line,
            end_line=end_line,
            context_lines=context_lines,
        )

    @server.tool(name="verify_source", structured_output=True)
    def verify_source_tool(
        repository_id: str,
        revision: str,
        file_path: str,
        expected_content_hash: str | None = None,
    ) -> SourceVerification:
        """Verify source existence, containment, and optional content hash."""
        return service.verify_source(
            repository_id=repository_id,
            revision=revision,
            file_path=file_path,
            expected_content_hash=expected_content_hash,
        )

    @server.tool(name="search_source", structured_output=True)
    def search_source_tool(
        query: str,
        repository_id: str,
        revision: str,
        limit: int = 20,
    ) -> SourceSearchResult:
        """Search Python source with bounded case-insensitive literal matching."""
        return service.search_source(
            query,
            repository_id=repository_id,
            revision=revision,
            limit=limit,
        )

    return server


def main() -> None:
    """Run the Repository Agent over MCP Streamable HTTP."""
    settings = Settings(service_name="repository-agent")
    configure_logging(settings.log_level)
    git_acquisition = GitAcquisitionService(
        settings.repository_root,
        allowed_hosts={
            host.strip()
            for host in settings.git_allowed_hosts.split(",")
            if host.strip()
        },
        command_timeout_seconds=settings.git_command_timeout_seconds,
        maximum_snapshot_bytes=settings.git_maximum_snapshot_bytes,
    )
    service = RepositoryService(
        settings.repository_root,
        maximum_file_bytes=settings.source_maximum_file_bytes,
        git_acquisition=git_acquisition,
    )
    create_server(service).run(
        transport="streamable-http",
        host=settings.mcp_host,
        port=settings.repository_agent_port,
    )


if __name__ == "__main__":
    main()
