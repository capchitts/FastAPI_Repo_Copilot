import pytest

from repo_chat.repository.mcp_server import create_server


class FakeRepositoryService:
    pass


@pytest.mark.asyncio
async def test_repository_server_exposes_source_boundary_tools() -> None:
    server = create_server(FakeRepositoryService())  # type: ignore[arg-type]

    tools = await server.list_tools()

    assert {tool.name for tool in tools} == {
        "acquire_repository",
        "get_repository_metadata",
        "read_file",
        "get_code_snippet",
        "verify_source",
        "search_source",
    }
    assert all(tool.description for tool in tools)
    assert all(tool.input_schema for tool in tools)
    assert all(tool.output_schema for tool in tools)
