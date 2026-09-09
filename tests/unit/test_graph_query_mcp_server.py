import pytest

from repo_chat.graph.mcp_server import create_server


class FakeGraphQueryService:
    pass


@pytest.mark.asyncio
async def test_server_exposes_all_required_graph_tools() -> None:
    server = create_server(FakeGraphQueryService())  # type: ignore[arg-type]

    tools = await server.list_tools()

    assert {tool.name for tool in tools} == {
        "find_entity",
        "get_dependencies",
        "get_dependents",
        "trace_imports",
        "find_related",
        "hybrid_search",
        "execute_query",
    }
    assert all(tool.description for tool in tools)
    assert all(tool.input_schema for tool in tools)
    assert all(tool.output_schema for tool in tools)
