import asyncio

import pytest

from repo_chat.contracts.evidence import Evidence, EvidenceKind, SourceLocation
from repo_chat.contracts.orchestration import (
    AgentName,
    AgentOutput,
    AgentTask,
    ConversationContext,
    ConversationRole,
    QueryAnalysis,
    QueryIntent,
    SessionPreferences,
    SynthesizedResponse,
)
from repo_chat.orchestration.memory import InMemoryConversationMemory
from repo_chat.orchestration.service import OrchestratorService


class StubAnswerSynthesizer:
    def __init__(self, answer: str | None = None, error: Exception | None = None) -> None:
        self.answer = answer
        self.error = error
        self.calls = 0

    async def synthesize(self, _query: str, _draft: object) -> str:
        self.calls += 1
        if self.error is not None:
            raise self.error
        assert self.answer is not None
        return self.answer


@pytest.mark.parametrize(
    ("query", "intent"),
    [
        ("What is `APIRouter`?", QueryIntent.ENTITY_LOOKUP),
        ("What classes inherit from APIRouter?", QueryIntent.DEPENDENCY_TRAVERSAL),
        ("Show the implementation of Depends", QueryIntent.IMPLEMENTATION),
        ("Compare Path and Query", QueryIntent.COMPARISON),
        ("What design patterns are used?", QueryIntent.PATTERN_ANALYSIS),
        ("Explain the complete request lifecycle", QueryIntent.ARCHITECTURE),
        ("Please re-index the repository", QueryIntent.INDEXING),
    ],
)
def test_analyzes_expected_query_intents(query: str, intent: QueryIntent) -> None:
    service = OrchestratorService(InMemoryConversationMemory())

    result = service.analyze_query(query)

    assert result.intent is intent


def test_extracts_explicit_entities_and_uses_context_for_references() -> None:
    service = OrchestratorService(InMemoryConversationMemory())
    explicit = service.analyze_query("Compare `Path` and Query")
    context = ConversationContext(session_id="session", active_entities=["APIRouter"])
    referenced = service.analyze_query("What inherits from it?", context)

    assert [entity.name for entity in explicit.entities] == ["Path", "Query"]
    assert [entity.name for entity in referenced.entities] == ["APIRouter"]
    assert referenced.entities[0].source == "conversation"


def test_routes_graph_source_and_index_queries_minimally() -> None:
    service = OrchestratorService(InMemoryConversationMemory())

    graph_plan = service.route_to_agents(service.analyze_query("What calls APIRouter?"))
    implementation_plan = service.route_to_agents(
        service.analyze_query("Show the implementation of APIRouter")
    )
    index_plan = service.route_to_agents(service.analyze_query("Reindex the repository"))

    assert [task.agent for task in graph_plan.tasks] == [AgentName.GRAPH, AgentName.GRAPH]
    assert [task.sequence for task in graph_plan.tasks] == [0, 1]
    assert [task.sequence for task in implementation_plan.tasks] == [0, 1, 2]
    assert implementation_plan.tasks[1].agent is AgentName.REPOSITORY
    assert implementation_plan.tasks[2].depends_on == [AgentName.REPOSITORY]
    assert [task.agent for task in index_plan.tasks] == [AgentName.INDEXER]


def test_source_routes_resolve_a_location_before_analysis() -> None:
    service = OrchestratorService(InMemoryConversationMemory())

    pattern_plan = service.route_to_agents(service.analyze_query("Find patterns in `routing.py`"))
    architecture_plan = service.route_to_agents(
        service.analyze_query("Explain APIRouter architecture")
    )

    assert [task.sequence for task in pattern_plan.tasks] == [0, 1, 2]
    assert pattern_plan.tasks[2].depends_on == [AgentName.REPOSITORY]
    assert [task.sequence for task in architecture_plan.tasks] == [0, 1, 2]
    assert architecture_plan.tasks[2].depends_on == [AgentName.REPOSITORY]


def test_broad_query_uses_hybrid_discovery_instead_of_inventing_a_location() -> None:
    service = OrchestratorService(InMemoryConversationMemory())
    analysis = service.analyze_query("Explain the plugin architecture")

    plan = service.route_to_agents(analysis)

    assert analysis.ambiguity == 0.6
    assert [task.operation for task in plan.tasks] == [
        "hybrid_search",
        "verify_source",
        "explain_implementation",
    ]


@pytest.mark.parametrize(
    "query",
    [
        "Where does FastAPI collect validation errors while processing parameters?",
        "How does FastAPI decide whether to await a route function or use a thread pool?",
        "Where is a returned Python object converted into an HTTP response?",
    ],
)
def test_conceptual_questions_route_to_hybrid_discovery(query: str) -> None:
    service = OrchestratorService(InMemoryConversationMemory())

    plan = service.route_to_agents(service.analyze_query(query))

    assert [task.operation for task in plan.tasks] == [
        "hybrid_search",
        "verify_source",
        "explain_implementation",
    ]


def test_request_lifecycle_expands_to_bounded_concept_entities() -> None:
    service = OrchestratorService(InMemoryConversationMemory())

    analysis = service.analyze_query("Explain the complete lifecycle of a FastAPI request")
    plan = service.route_to_agents(analysis)

    assert [entity.name for entity in analysis.entities] == [
        "FastAPI.__call__",
        "APIRoute.get_route_handler",
        "get_request_handler",
        "solve_dependencies",
        "run_endpoint_function",
        "serialize_response",
    ]
    assert all(entity.source == "concept_map" for entity in analysis.entities)
    assert [task.sequence for task in plan.tasks] == [0, 1, 2]


def test_dependency_injection_concept_overrides_framework_capitalization() -> None:
    service = OrchestratorService(InMemoryConversationMemory())

    analysis = service.analyze_query(
        "Can you explain how dependency injection works in FastApi and give a code example?"
    )

    assert [entity.name for entity in analysis.entities] == [
        "Depends",
        "get_dependant",
        "get_parameterless_sub_dependant",
        "solve_dependencies",
    ]
    assert all(entity.source == "concept_map" for entity in analysis.entities)


def test_fastapi_framework_name_is_normalized_to_class_entity() -> None:
    service = OrchestratorService(InMemoryConversationMemory())

    analysis = service.analyze_query("What is the FastApi class?")

    assert [entity.name for entity in analysis.entities] == ["FastAPI"]
    assert analysis.intent is QueryIntent.ENTITY_LOOKUP


@pytest.mark.parametrize(
    ("query", "intent", "entities"),
    [
        (
            "How does FastAPI handle request validation?",
            QueryIntent.GENERAL,
            [
                "get_request_handler",
                "solve_dependencies",
                "request_body_to_args",
                "_validate_value_with_model_field",
            ],
        ),
        (
            "How does dependency injection work and show examples from the codebase?",
            QueryIntent.GENERAL,
            [
                "Depends",
                "get_dependant",
                "get_parameterless_sub_dependant",
                "solve_dependencies",
            ],
        ),
        (
            "Find all decorators used in the routing module",
            QueryIntent.PATTERN_ANALYSIS,
            ["APIRouter"],
        ),
        (
            "What design patterns are used in FastAPI's core and why?",
            QueryIntent.PATTERN_ANALYSIS,
            ["FastAPI"],
        ),
    ],
)
def test_assignment_broad_queries_expand_to_bounded_entities(
    query: str, intent: QueryIntent, entities: list[str]
) -> None:
    service = OrchestratorService(InMemoryConversationMemory())

    analysis = service.analyze_query(query)
    plan = service.route_to_agents(analysis)

    assert analysis.intent is intent
    assert [entity.name for entity in analysis.entities] == entities
    assert all(entity.source == "concept_map" for entity in analysis.entities)
    assert [task.sequence for task in plan.tasks] == [0, 1, 2]


def test_synthesis_preserves_evidence_and_partial_failures() -> None:
    service = OrchestratorService(InMemoryConversationMemory())
    evidence = Evidence(kind=EvidenceKind.GRAPH, entity_id="class-1")
    outputs = [
        AgentOutput(
            agent=AgentName.GRAPH,
            operation="find_entity",
            success=True,
            data={"name": "APIRouter"},
            evidence=[evidence, evidence],
        ),
        AgentOutput(
            agent=AgentName.CODE_ANALYST,
            operation="explain_implementation",
            success=False,
            error="source unavailable",
        ),
    ]

    result = service.synthesize_response("Explain APIRouter", outputs)

    assert result.agents_used == [AgentName.GRAPH]
    assert len(result.evidence) == 1
    assert result.partial is True
    assert "source unavailable" in result.warnings[0]


def test_synthesis_enriches_source_evidence_with_graph_snapshot() -> None:
    service = OrchestratorService(InMemoryConversationMemory())
    graph = AgentOutput(
        agent=AgentName.GRAPH,
        operation="find_entity",
        success=True,
        data={
            "entities": [
                {
                    "repository_id": "fastapi",
                    "revision": "abc123",
                    "snapshot": {
                        "repository_source": "/data/repositories/fastapi",
                        "captured_at": "2026-09-03T08:35:52Z",
                        "indexed_at": "2026-09-03T08:36:13Z",
                        "index_job_id": "job-1",
                    },
                }
            ]
        },
    )
    source = AgentOutput(
        agent=AgentName.CODE_ANALYST,
        operation="explain_implementation",
        success=True,
        data={"summary": "source"},
        evidence=[
            Evidence(
                kind=EvidenceKind.SOURCE,
                location=SourceLocation(
                    repository_id="fastapi",
                    revision="abc123",
                    file_path="fastapi/routing.py",
                    start_line=10,
                    end_line=20,
                ),
            )
        ],
    )

    result = service.synthesize_response("Explain APIRouter", [graph, source])

    location = next(item.location for item in result.evidence if item.location is not None)
    assert location.snapshot is not None
    assert location.snapshot.index_job_id == "job-1"
    assert location.snapshot.indexed_at is not None


def test_synthesis_renders_bounded_source_summary_instead_of_snippet_dump() -> None:
    service = OrchestratorService(InMemoryConversationMemory())
    huge_code = "class APIRouter:\n" + "    pass\n" * 5_000
    output = AgentOutput(
        agent=AgentName.CODE_ANALYST,
        operation="explain_implementation",
        success=True,
        data={
            "explanation": "APIRouter is a class with 32 directly declared methods.",
            "signature": "class APIRouter(routing.Router)",
            "bases": ["routing.Router"],
            "methods": [f"method_{index}" for index in range(32)],
            "docstring": "Groups path operations.\n\nA much longer explanation follows.",
            "snippet": {
                "file_path": "fastapi/routing.py",
                "start_line": 2255,
                "end_line": 2374,
                "code": huge_code,
            },
        },
    )

    result = service.synthesize_response("Explain APIRouter", [output])

    assert "APIRouter is a class" in result.answer
    assert "fastapi/routing.py:2255-2374" in result.answer
    assert huge_code not in result.answer
    assert len(result.answer) < 1_000


def test_decorator_query_filters_unrelated_patterns() -> None:
    service = OrchestratorService(InMemoryConversationMemory())
    output = AgentOutput(
        agent=AgentName.CODE_ANALYST,
        operation="find_patterns",
        success=True,
        data={
            "file_path": "fastapi/routing.py",
            "patterns": [
                {
                    "kind": "inheritance",
                    "entity_name": "APIRoute",
                    "rationale": "Inherits routing.Route.",
                    "line": 10,
                },
                {
                    "kind": "decorator",
                    "entity_name": "RouteContext",
                    "rationale": "Uses decorator dataclass.",
                    "line": 20,
                },
            ],
        },
    )

    result = service.synthesize_response("Find all decorators used in the routing module", [output])

    assert "Detected 1 decorators" in result.answer
    assert "RouteContext" in result.answer
    assert "inheritance" not in result.answer


def test_design_pattern_query_explains_meaning_and_static_boundary() -> None:
    service = OrchestratorService(InMemoryConversationMemory())
    output = AgentOutput(
        agent=AgentName.CODE_ANALYST,
        operation="find_patterns",
        success=True,
        data={
            "file_path": "fastapi/applications.py",
            "patterns": [
                {
                    "kind": "inheritance",
                    "entity_name": "FastAPI",
                    "rationale": "Inherits from Starlette.",
                    "line": 42,
                },
                {
                    "kind": "decorator",
                    "entity_name": "on_event",
                    "rationale": "Uses decorator deprecated.",
                    "line": 4654,
                },
                {
                    "kind": "async_io",
                    "entity_name": "__call__",
                    "rationale": "Declared with async def.",
                    "line": 1160,
                },
            ],
        },
    )

    result = service.synthesize_response(
        "What design patterns are used in FastAPI core and why?", [output]
    )

    assert "Inheritance-based framework extension" in result.answer
    assert "Decorator-based API evolution" in result.answer
    assert "execution style rather than" in result.answer
    assert "not an exhaustive claim" in result.answer
    assert "fastapi/applications.py:42" in result.answer


def test_synthesis_orders_request_lifecycle_and_marks_static_boundary() -> None:
    service = OrchestratorService(InMemoryConversationMemory())
    names = [
        ("serialize_response", "fastapi/routing.py", 301),
        ("FastAPI.__call__", "fastapi/applications.py", 1150),
        ("solve_dependencies", "fastapi/dependencies/utils.py", 586),
        ("APIRoute.get_route_handler", "fastapi/routing.py", 1260),
        ("get_request_handler", "fastapi/routing.py", 375),
        ("run_endpoint_function", "fastapi/routing.py", 270),
    ]
    output = AgentOutput(
        agent=AgentName.CODE_ANALYST,
        operation="explain_implementation",
        success=True,
        data={
            "analyses": [
                {
                    "name": name,
                    "snippet": {
                        "file_path": file_path,
                        "start_line": line,
                        "end_line": line + 39,
                    },
                }
                for name, file_path, line in names
            ]
        },
    )

    result = service.synthesize_response(
        "Explain the complete lifecycle of a FastAPI request", [output]
    )

    assert result.answer.index("**ASGI entry:**") < result.answer.index(
        "**Matched route handler:**"
    )
    assert result.answer.index("**Matched route handler:**") < result.answer.index(
        "**Request handling:**"
    )
    assert result.answer.index("**Request handling:**") < result.answer.index(
        "**Dependency resolution:**"
    )
    assert "fastapi/dependencies/utils.py:586-625" in result.answer
    assert "**Endpoint invocation:**" in result.answer
    assert "not fully expanded" in result.answer


class RecordingExecutor:
    def __init__(self, *, delay: float = 0.0) -> None:
        self.delay = delay
        self.calls: list[AgentTask] = []
        self.contexts: list[ConversationContext] = []

    async def execute(
        self,
        task: AgentTask,
        _analysis: QueryAnalysis,
        _context: ConversationContext,
        _prior_outputs: list[AgentOutput],
    ) -> AgentOutput:
        self.calls.append(task)
        self.contexts.append(_context.model_copy(deep=True))
        if self.delay:
            await asyncio.sleep(self.delay)
        return AgentOutput(
            agent=task.agent,
            operation=task.operation,
            success=True,
            data=f"result from {task.agent.value}",
        )


class FakeOperationalState:
    def __init__(self) -> None:
        self.cached = None
        self.audits = []
        self.preferences = SessionPreferences()

    async def get_cached_response(self, *_args):  # type: ignore[no-untyped-def]
        return self.cached

    async def put_cached_response(self, *_args):  # type: ignore[no-untyped-def]
        self.cached = _args[-1].model_copy(deep=True)

    async def append_audit(self, _session_id, record):  # type: ignore[no-untyped-def]
        self.audits.append(record)

    async def get_preferences(self, _session_id):  # type: ignore[no-untyped-def]
        return self.preferences

    async def set_preferences(self, _session_id, preferences):  # type: ignore[no-untyped-def]
        self.preferences = preferences
        return preferences


@pytest.mark.asyncio
async def test_orchestrate_executes_plan_and_records_conversation() -> None:
    memory = InMemoryConversationMemory()
    executor = RecordingExecutor()
    service = OrchestratorService(memory, executor)

    response = await service.orchestrate("session", "Explain APIRouter architecture")
    context = await memory.get_context("session")

    assert response.agents_used == [
        AgentName.GRAPH,
        AgentName.REPOSITORY,
        AgentName.CODE_ANALYST,
    ]
    assert [turn.role for turn in context.turns] == [
        ConversationRole.USER,
        ConversationRole.ASSISTANT,
    ]
    assert len(executor.calls) == 3
    assert [task.sequence for task in executor.calls] == [0, 1, 2]


@pytest.mark.asyncio
async def test_orchestration_caches_grounded_response_and_audits_cache_hit() -> None:
    executor = RecordingExecutor()
    operational = FakeOperationalState()
    service = OrchestratorService(
        InMemoryConversationMemory(),
        executor,
        operational_state=operational,  # type: ignore[arg-type]
    )

    first = await service.orchestrate(
        "session-one", "Explain APIRouter architecture", repository_id="fastapi", revision="one"
    )
    second = await service.orchestrate(
        "session-two", "Explain APIRouter architecture", repository_id="fastapi", revision="one"
    )

    assert len(executor.calls) == 3
    assert second.answer == first.answer
    assert "Response served from revision-scoped cache." in second.warnings
    assert [record.cache_hit for record in operational.audits] == [False, True]
    assert all(len(record.query_hash) == 64 for record in operational.audits)


@pytest.mark.asyncio
async def test_contextual_follow_up_bypasses_global_response_cache() -> None:
    executor = RecordingExecutor()
    operational = FakeOperationalState()
    operational.cached = SynthesizedResponse(answer="wrong cached answer")
    service = OrchestratorService(
        InMemoryConversationMemory(), executor, operational_state=operational  # type: ignore[arg-type]
    )

    await service.orchestrate(
        "session", "What calls it?", repository_id="fastapi", revision="one"
    )

    assert executor.calls
    assert operational.audits[0].cache_hit is False


@pytest.mark.asyncio
async def test_optional_synthesizer_changes_only_answer() -> None:
    synthesizer = StubAnswerSynthesizer("Grounded enhanced answer [source.py:1-2]")
    service = OrchestratorService(InMemoryConversationMemory(), answer_synthesizer=synthesizer)
    response = service.synthesize_response(
        "Explain Thing",
        [
            AgentOutput(
                agent=AgentName.GRAPH,
                operation="find_entity",
                success=True,
                data={"entities": []},
                evidence=[Evidence(kind=EvidenceKind.GRAPH, entity_id="class-1")],
            )
        ],
    )

    enhanced = await service._enhance_answer("Explain Thing", response)

    assert enhanced.answer == "Grounded enhanced answer [source.py:1-2]"
    assert enhanced.evidence == response.evidence
    assert enhanced.agents_used == response.agents_used
    assert enhanced.partial == response.partial
    assert synthesizer.calls == 1


@pytest.mark.asyncio
async def test_optional_synthesizer_failure_uses_deterministic_fallback() -> None:
    synthesizer = StubAnswerSynthesizer(error=TimeoutError())
    service = OrchestratorService(InMemoryConversationMemory(), answer_synthesizer=synthesizer)
    response = service.synthesize_response(
        "Explain Thing",
        [
            AgentOutput(
                agent=AgentName.GRAPH,
                operation="find_entity",
                success=True,
                data={"entities": []},
                evidence=[Evidence(kind=EvidenceKind.GRAPH, entity_id="class-1")],
            )
        ],
    )

    enhanced = await service._enhance_answer("Explain Thing", response)

    assert enhanced.answer == response.answer
    assert enhanced.partial == response.partial
    assert "LLM synthesis unavailable; deterministic fallback used." in enhanced.warnings


@pytest.mark.asyncio
async def test_agent_timeout_becomes_partial_output() -> None:
    service = OrchestratorService(
        InMemoryConversationMemory(),
        RecordingExecutor(delay=0.05),
        agent_timeout_seconds=0.001,
    )

    response = await service.orchestrate("session", "Explain APIRouter architecture")

    assert response.partial is True
    assert response.agents_used == []
    assert any("timed out" in warning for warning in response.warnings)


@pytest.mark.asyncio
async def test_follow_up_reuses_persisted_repository_scope_and_entity() -> None:
    memory = InMemoryConversationMemory()
    executor = RecordingExecutor()
    service = OrchestratorService(memory, executor)

    await service.orchestrate(
        "session",
        "Show the implementation of APIRouter",
        repository_id="fastapi",
        revision="abc123",
    )
    executor.calls.clear()
    executor.contexts.clear()
    await service.orchestrate("session", "What calls it?")

    assert executor.contexts
    assert executor.contexts[0].repository_id == "fastapi"
    assert executor.contexts[0].revision == "abc123"
    context = await memory.get_context("session")
    assert context.active_entities == ["APIRouter"]
