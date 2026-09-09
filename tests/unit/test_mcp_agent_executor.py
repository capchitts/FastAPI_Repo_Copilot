from typing import Any

import pytest

from repo_chat.contracts.orchestration import (
    AgentName,
    AgentOutput,
    AgentTask,
    ConversationContext,
    ExtractedEntity,
    QueryAnalysis,
    QueryIntent,
)
from repo_chat.exceptions.base import AgentError
from repo_chat.orchestration.executor import MCPAgentExecutor


class FakeClient:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, tool: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((tool, arguments))
        return self.response


def make_executor(graph_response: Any = None, code_response: Any = None):  # type: ignore[no-untyped-def]
    indexer = FakeClient({})
    graph = FakeClient(graph_response or {})
    code = FakeClient(code_response or {})
    executor = MCPAgentExecutor(indexer=indexer, graph=graph, code_analyst=code)  # type: ignore[arg-type]
    return executor, indexer, graph, code


def analysis() -> QueryAnalysis:
    return QueryAnalysis(
        query="Explain APIRouter",
        intent=QueryIntent.IMPLEMENTATION,
        entities=[ExtractedEntity(name="APIRouter", source="identifier")],
        needs_graph=True,
        needs_source=True,
    )


@pytest.mark.asyncio
async def test_executor_maps_graph_entity_lookup() -> None:
    response = {"query": "APIRouter", "entities": []}
    executor, _, graph, _ = make_executor(graph_response=response)
    task = AgentTask(agent=AgentName.GRAPH, operation="find_entity", sequence=0, reason="resolve")

    output = await executor.execute(task, analysis(), ConversationContext(session_id="s"), [])

    assert output.success is True
    assert graph.calls == [("find_entity", {"name": "APIRouter", "limit": 10})]


@pytest.mark.asyncio
async def test_executor_resolves_and_maps_both_comparison_entities() -> None:
    graph = FakeClient(
        {"entities": [{"id": "entity", "name": "placeholder", "file_path": "placeholder.py"}]}
    )
    responses = iter(
        [
            {"entities": [{"id": "left", "name": "Path", "file_path": "params.py"}]},
            {"entities": [{"id": "right", "name": "Query", "file_path": "params.py"}]},
        ]
    )

    async def call(_tool: str, arguments: dict[str, Any]) -> Any:
        graph.calls.append(("find_entity", arguments))
        return next(responses)

    graph.call = call  # type: ignore[method-assign]
    code = FakeClient({"summary": "compared"})
    executor = MCPAgentExecutor(indexer=FakeClient({}), graph=graph, code_analyst=code)  # type: ignore[arg-type]
    query = QueryAnalysis(
        query="Compare Path and Query",
        intent=QueryIntent.COMPARISON,
        entities=[
            ExtractedEntity(name="Path", source="identifier"),
            ExtractedEntity(name="Query", source="identifier"),
        ],
        needs_graph=True,
        needs_source=True,
    )
    graph_task = AgentTask(
        agent=AgentName.GRAPH, operation="find_entity", sequence=0, reason="resolve"
    )
    graph_output = await executor.execute(
        graph_task, query, ConversationContext(session_id="s"), []
    )
    code_task = AgentTask(
        agent=AgentName.CODE_ANALYST,
        operation="compare_implementations",
        sequence=1,
        reason="compare",
    )
    await executor.execute(
        code_task,
        query,
        ConversationContext(session_id="s", repository_id="fastapi", revision="abc123"),
        [graph_output],
    )

    assert graph.calls == [
        ("find_entity", {"name": "Path", "limit": 10}),
        ("find_entity", {"name": "Query", "limit": 10}),
    ]
    assert code.calls[0][1]["left_entity_name"] == "Path"
    assert code.calls[0][1]["right_entity_name"] == "Query"


@pytest.mark.asyncio
async def test_executor_passes_graph_location_to_code_analyst() -> None:
    executor, _, _, code = make_executor(code_response={"explanation": "source grounded"})
    prior = [
        AgentOutput(
            agent=AgentName.GRAPH,
            operation="find_entity",
            success=True,
            data={
                "entities": [
                    {"id": "class-1", "name": "APIRouter", "file_path": "fastapi/routing.py"}
                ]
            },
        )
    ]
    task = AgentTask(
        agent=AgentName.CODE_ANALYST,
        operation="explain_implementation",
        sequence=1,
        reason="analyze",
    )
    context = ConversationContext(session_id="s", repository_id="fastapi", revision="abc123")

    await executor.execute(task, analysis(), context, prior)

    assert code.calls == [
        (
            "explain_implementation",
            {
                "repository_id": "fastapi",
                "revision": "abc123",
                "file_path": "fastapi/routing.py",
                "entity_name": "APIRouter",
            },
        )
    ]


@pytest.mark.asyncio
async def test_executor_verifies_graph_sources_through_repository_agent() -> None:
    repository = FakeClient(
        {
            "repository_id": "fastapi",
            "revision": "abc123",
            "file_path": "fastapi/routing.py",
            "exists": True,
            "location": {
                "repository_id": "fastapi",
                "revision": "abc123",
                "file_path": "fastapi/routing.py",
                "start_line": 1,
                "end_line": 100,
            },
        }
    )
    executor = MCPAgentExecutor(  # type: ignore[arg-type]
        indexer=FakeClient({}),
        graph=FakeClient({}),
        code_analyst=FakeClient({}),
        repository=repository,
    )
    prior = [
        AgentOutput(
            agent=AgentName.GRAPH,
            operation="find_entity",
            success=True,
            data={
                "entities": [
                    {
                        "id": "class-1",
                        "name": "APIRouter",
                        "file_path": "fastapi/routing.py",
                    }
                ]
            },
        )
    ]

    output = await executor.execute(
        AgentTask(
            agent=AgentName.REPOSITORY,
            operation="verify_source",
            sequence=1,
            reason="verify",
        ),
        analysis(),
        ConversationContext(
            session_id="s", repository_id="fastapi", revision="abc123"
        ),
        prior,
    )

    assert repository.calls[0][0] == "verify_source"
    assert repository.calls[0][1]["file_path"] == "fastapi/routing.py"
    assert output.agent is AgentName.REPOSITORY
    assert output.evidence[0].location is not None


@pytest.mark.asyncio
async def test_executor_prefers_definition_and_promotes_nested_evidence() -> None:
    graph_response = {
        "entities": [
            {
                "id": "import-1",
                "kind": "import",
                "name": "fastapi.APIRouter",
                "qualified_name": "example.__import__.1",
                "file_path": "example.py",
            },
            {
                "id": "class-1",
                "kind": "class",
                "name": "APIRouter",
                "qualified_name": "fastapi.routing.APIRouter",
                "file_path": "fastapi/routing.py",
            },
        ]
    }
    nested_evidence = {
        "kind": "source",
        "location": {
            "repository_id": "fastapi",
            "revision": "abc123",
            "file_path": "fastapi/routing.py",
            "start_line": 10,
            "end_line": 20,
        },
        "excerpt": "x" * 5_000,
    }
    executor, _, _, code = make_executor(
        graph_response=graph_response,
        code_response={"explanation": "concise", "snippet": {"evidence": nested_evidence}},
    )
    graph_task = AgentTask(
        agent=AgentName.GRAPH, operation="find_entity", sequence=0, reason="resolve"
    )
    graph_output = await executor.execute(
        graph_task, analysis(), ConversationContext(session_id="s"), []
    )
    code_task = AgentTask(
        agent=AgentName.CODE_ANALYST,
        operation="explain_implementation",
        sequence=1,
        reason="explain",
    )
    code_output = await executor.execute(
        code_task,
        analysis(),
        ConversationContext(session_id="s", repository_id="fastapi", revision="abc123"),
        [graph_output],
    )

    assert graph_output.data["entities"][0]["id"] == "class-1"
    assert graph_output.data["entities"][0]["requested_name"] == "APIRouter"
    assert graph_output.evidence[0].entity_id == "class-1"
    assert code.calls[0][1]["file_path"] == "fastapi/routing.py"
    assert code_output.evidence[0].location is not None
    assert code_output.evidence[0].location.start_line == 10
    assert code_output.evidence[0].excerpt is not None
    assert len(code_output.evidence[0].excerpt) == 4_000


@pytest.mark.asyncio
async def test_executor_requires_prior_graph_location() -> None:
    executor, _, _, _ = make_executor()
    task = AgentTask(
        agent=AgentName.CODE_ANALYST,
        operation="explain_implementation",
        sequence=1,
        reason="analyze",
    )

    with pytest.raises(AgentError, match="prior graph"):
        await executor.execute(
            task,
            analysis(),
            ConversationContext(session_id="s", repository_id="fastapi", revision="abc123"),
            [],
        )


@pytest.mark.asyncio
async def test_executor_analyzes_multiple_discovered_entities() -> None:
    code = FakeClient(
        {
            "name": "entity",
            "explanation": "analyzed",
            "snippet": {
                "evidence": {
                    "kind": "source",
                    "location": {
                        "repository_id": "fastapi",
                        "revision": "abc123",
                        "file_path": "module.py",
                        "start_line": 1,
                        "end_line": 2,
                    },
                }
            },
        }
    )
    executor = MCPAgentExecutor(  # type: ignore[arg-type]
        indexer=FakeClient({}), graph=FakeClient({}), code_analyst=code
    )
    prior = [
        AgentOutput(
            agent=AgentName.GRAPH,
            operation="find_entity",
            success=True,
            data={
                "entities": [
                    {"id": "one", "name": "First", "file_path": "first.py"},
                    {"id": "two", "name": "Second", "file_path": "second.py"},
                ]
            },
        )
    ]
    task = AgentTask(
        agent=AgentName.CODE_ANALYST,
        operation="explain_implementation",
        sequence=1,
        reason="multi-file",
    )

    output = await executor.execute(
        task,
        analysis(),
        ConversationContext(session_id="s", repository_id="fastapi", revision="abc123"),
        prior,
    )

    assert len(code.calls) == 2
    assert [call[1]["entity_name"] for call in code.calls] == ["First", "Second"]
    assert len(output.data["analyses"]) == 2
    assert len(output.evidence) == 1
