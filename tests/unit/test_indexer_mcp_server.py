from pathlib import Path

import pytest

from repo_chat.contracts.indexing import ParsedFile
from repo_chat.indexing.mcp_server import create_server
from repo_chat.indexing.service import IndexerService


class FakeGraphRepository:
    async def write_parsed_file(self, _parsed_file: ParsedFile) -> None:
        return None


@pytest.mark.asyncio
async def test_server_exposes_all_required_indexer_tools(tmp_path: Path) -> None:
    service = IndexerService(  # type: ignore[arg-type]
        FakeGraphRepository(),
        repository_root=tmp_path,
    )
    server = create_server(service)

    tools = await server.list_tools()

    assert {tool.name for tool in tools} == {
        "index_repository",
        "index_file",
        "parse_python_ast",
        "extract_entities",
        "get_index_status",
    }
    assert all(tool.description for tool in tools)
    assert all(tool.input_schema for tool in tools)
