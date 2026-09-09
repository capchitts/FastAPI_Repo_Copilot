"""MCP 2.x server exposing source-grounded code-analysis tools."""

from mcp.server.mcpserver import MCPServer

from repo_chat.analysis.service import CodeAnalysisService
from repo_chat.analysis.source_store import SourceStore
from repo_chat.config.settings import Settings
from repo_chat.contracts.code_analysis import (
    CodeSnippet,
    EntityAnalysis,
    ImplementationComparison,
    PatternAnalysis,
)
from repo_chat.observability.logging import configure_logging
from repo_chat.observability.mcp import MCPObservabilityMiddleware


def create_server(service: CodeAnalysisService) -> MCPServer[None]:
    """Create the Code Analyst MCP server around its typed service."""
    server: MCPServer[None] = MCPServer(
        name="fastapi-repository-code-analyst",
        title="FastAPI Repository Code Analyst",
        description="Retrieves and analyzes revision-pinned Python source.",
        version="0.1.0",
        middleware=[MCPObservabilityMiddleware("code-analyst")],
    )

    @server.tool(name="analyze_function", structured_output=True)
    def analyze_function_tool(
        repository_id: str, revision: str, file_path: str, function_name: str
    ) -> EntityAnalysis:
        """Analyze a function or method implementation and return source evidence."""
        return service.analyze_function(
            repository_id=repository_id,
            revision=revision,
            file_path=file_path,
            function_name=function_name,
        )

    @server.tool(name="analyze_class", structured_output=True)
    def analyze_class_tool(
        repository_id: str, revision: str, file_path: str, class_name: str
    ) -> EntityAnalysis:
        """Analyze a class, its bases, methods, decorators, and calls."""
        return service.analyze_class(
            repository_id=repository_id,
            revision=revision,
            file_path=file_path,
            class_name=class_name,
        )

    @server.tool(name="find_patterns", structured_output=True)
    def find_patterns_tool(repository_id: str, revision: str, file_path: str) -> PatternAnalysis:
        """Detect conservative source-grounded implementation patterns."""
        return service.find_patterns(
            repository_id=repository_id, revision=revision, file_path=file_path
        )

    @server.tool(name="get_code_snippet", structured_output=True)
    def get_code_snippet_tool(
        repository_id: str,
        revision: str,
        file_path: str,
        start_line: int,
        end_line: int,
        context_lines: int = 3,
    ) -> CodeSnippet:
        """Retrieve a bounded code range with surrounding context and provenance."""
        return service.get_code_snippet(
            repository_id=repository_id,
            revision=revision,
            file_path=file_path,
            start_line=start_line,
            end_line=end_line,
            context_lines=context_lines,
        )

    @server.tool(name="explain_implementation", structured_output=True)
    def explain_implementation_tool(
        repository_id: str, revision: str, file_path: str, entity_name: str
    ) -> EntityAnalysis:
        """Explain how a named source entity is structurally implemented."""
        return service.explain_implementation(
            repository_id=repository_id,
            revision=revision,
            file_path=file_path,
            entity_name=entity_name,
        )

    @server.tool(name="compare_implementations", structured_output=True)
    def compare_implementations_tool(
        repository_id: str,
        revision: str,
        left_file_path: str,
        left_entity_name: str,
        right_file_path: str,
        right_entity_name: str,
    ) -> ImplementationComparison:
        """Compare two source entities and return similarities, differences, and evidence."""
        return service.compare_implementations(
            repository_id=repository_id,
            revision=revision,
            left_file_path=left_file_path,
            left_entity_name=left_entity_name,
            right_file_path=right_file_path,
            right_entity_name=right_entity_name,
        )

    return server


def main() -> None:
    """Run the Code Analyst over MCP Streamable HTTP."""
    settings = Settings(service_name="code-analyst")
    configure_logging(settings.log_level)
    store = SourceStore(
        settings.repository_root, maximum_file_bytes=settings.source_maximum_file_bytes
    )
    create_server(
        CodeAnalysisService(
            store,
            maximum_entity_snippet_lines=settings.source_maximum_snippet_lines,
        )
    ).run(transport="streamable-http", host=settings.mcp_host, port=settings.code_analyst_port)


if __name__ == "__main__":
    main()
