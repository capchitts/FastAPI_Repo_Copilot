from pathlib import Path

import pytest

from repo_chat.analysis.mcp_server import create_server
from repo_chat.analysis.service import CodeAnalysisService
from repo_chat.analysis.source_store import SourceStore


@pytest.mark.asyncio
async def test_server_exposes_all_required_code_analysis_tools(tmp_path: Path) -> None:
    server = create_server(CodeAnalysisService(SourceStore(tmp_path)))

    tools = await server.list_tools()

    assert {tool.name for tool in tools} == {
        "analyze_function",
        "analyze_class",
        "find_patterns",
        "get_code_snippet",
        "explain_implementation",
        "compare_implementations",
    }
    assert all(tool.description for tool in tools)
    assert all(tool.input_schema for tool in tools)
    assert all(tool.output_schema for tool in tools)
