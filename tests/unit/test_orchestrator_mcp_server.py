import pytest

from repo_chat.orchestration.mcp_server import create_server
from repo_chat.orchestration.memory import InMemoryConversationMemory
from repo_chat.orchestration.service import OrchestratorService


@pytest.mark.asyncio
async def test_server_exposes_all_required_orchestrator_tools() -> None:
    server = create_server(OrchestratorService(InMemoryConversationMemory()))

    tools = await server.list_tools()

    assert {tool.name for tool in tools} == {
        "analyze_query",
        "route_to_agents",
        "get_conversation_context",
        "get_session_preferences",
        "set_session_preferences",
        "synthesize_response",
        "chat",
    }
    assert all(tool.description for tool in tools)
    assert all(tool.input_schema for tool in tools)
    assert all(tool.output_schema for tool in tools)
