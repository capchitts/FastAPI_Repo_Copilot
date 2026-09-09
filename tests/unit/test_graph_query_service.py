from typing import Any

import pytest

from repo_chat.contracts.indexing import EntityKind
from repo_chat.exceptions.base import GraphQueryError
from repo_chat.graph.query_service import GraphQueryService
from repo_chat.semantic.store import SemanticHit


def entity_node(
    *,
    entity_id: str = "class-1",
    name: str = "APIRouter",
    qualified_name: str = "fastapi.routing.APIRouter",
) -> dict[str, Any]:
    return {
        "id": entity_id,
        "kind": "class",
        "name": name,
        "qualified_name": qualified_name,
        "file_path": "fastapi/routing.py",
        "start_line": 10,
        "end_line": 100,
    }


class FakeResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def __aiter__(self):  # type: ignore[no-untyped-def]
        async def iterate():  # type: ignore[no-untyped-def]
            for row in self._rows:
                yield row

        return iterate()


class FakeSession:
    def __init__(self, responses: list[list[dict[str, Any]]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __aenter__(self):  # type: ignore[no-untyped-def]
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def run(self, query: str, parameters: dict[str, Any]):
        self.calls.append((query, parameters))
        return FakeResult(self.responses.pop(0))


class FakeDriver:
    def __init__(self, responses: list[list[dict[str, Any]]]) -> None:
        self.fake_session = FakeSession(responses)
        self.database: str | None = None

    def session(self, *, database: str | None = None) -> FakeSession:
        self.database = database
        return self.fake_session


class FakeSemanticStore:
    async def search(self, query: str, **filters: Any) -> list[SemanticHit]:
        assert query.startswith("dependency injection lifecycle")
        assert filters["repository_id"] == "fastapi"
        assert filters["revision"] == "abc123"
        return [
            SemanticHit(
                entity_id="function-solve",
                repository_id="fastapi",
                revision="abc123",
                kind=EntityKind.FUNCTION,
                name="solve_dependencies",
                qualified_name="fastapi.dependencies.utils.solve_dependencies",
                file_path="fastapi/dependencies/utils.py",
                start_line=586,
                end_line=625,
                content_hash="hash",
                text="resolve dependency graph and cache dependency values",
                score=0.91,
            )
        ]

    async def index_file(self, *_args: object) -> int:
        return 0

    async def carry_forward_file(self, **_arguments: str) -> int:
        return 0


@pytest.mark.asyncio
async def test_find_entity_returns_typed_results_and_parameters() -> None:
    driver = FakeDriver([[{"entity": entity_node()}]])
    service = GraphQueryService(driver, "neo4j")  # type: ignore[arg-type]

    result = await service.find_entity("APIRouter", kind=EntityKind.CLASS, limit=5)

    assert result.query == "APIRouter"
    assert result.entities[0].qualified_name == "fastapi.routing.APIRouter"
    assert result.entities[0].kind is EntityKind.CLASS
    assert driver.database == "neo4j"
    _, parameters = driver.fake_session.calls[0]
    assert parameters == {"name": "APIRouter", "kind": "class", "limit": 5}


@pytest.mark.asyncio
async def test_find_entity_includes_revision_snapshot_provenance() -> None:
    node = entity_node()
    node.update({"repository_id": "fastapi", "latest_revision": "abc123"})
    driver = FakeDriver(
        [
            [
                {
                    "entity": node,
                    "revision": {
                        "repository_source": "/data/repositories/fastapi",
                        "captured_at": "2026-09-03T08:35:52Z",
                        "indexed_at": "2026-09-03T08:36:13Z",
                        "index_job_id": "job-1",
                        "immutable": True,
                    },
                }
            ]
        ]
    )
    service = GraphQueryService(driver)  # type: ignore[arg-type]

    result = await service.find_entity("APIRouter")

    entity = result.entities[0]
    assert entity.repository_id == "fastapi"
    assert entity.revision == "abc123"
    assert entity.snapshot is not None
    assert entity.snapshot.index_job_id == "job-1"
    assert entity.snapshot.indexed_at is not None


@pytest.mark.asyncio
async def test_find_entity_rejects_unbounded_limit() -> None:
    service = GraphQueryService(FakeDriver([]))  # type: ignore[arg-type]

    with pytest.raises(GraphQueryError, match="limit"):
        await service.find_entity("anything", limit=101)


@pytest.mark.asyncio
async def test_hybrid_search_discovers_semantic_entity_with_revision_filter() -> None:
    driver = FakeDriver([[]])
    service = GraphQueryService(  # type: ignore[arg-type]
        driver, semantic_store=FakeSemanticStore()
    )

    result = await service.hybrid_search(
        "dependency injection lifecycle",
        repository_id="fastapi",
        revision="abc123",
        limit=5,
    )

    assert result.hits[0].entity.name == "solve_dependencies"
    assert result.hits[0].sources == ["semantic"]
    assert result.hits[0].entity.revision == "abc123"


def test_semantic_query_expands_code_concepts_for_parameter_analysis() -> None:
    expanded = GraphQueryService._expand_semantic_query(
        "How does FastAPI analyze endpoint function parameters?"
    )

    assert "get_dependant" in expanded
    assert "analyze_param" in expanded


def test_semantic_reranking_prefers_distinctive_symbol_hints() -> None:
    generic = SemanticHit(
        entity_id="generic",
        repository_id="fastapi",
        revision="abc",
        kind=EntityKind.CLASS,
        name="FastAPI",
        qualified_name="fastapi.applications.FastAPI",
        file_path="fastapi/applications.py",
        start_line=1,
        end_line=2,
        content_hash="one",
        text="application parameters",
        score=0.99,
    )
    relevant = generic.model_copy(
        update={
            "entity_id": "relevant",
            "name": "analyze_param",
            "qualified_name": "fastapi.dependencies.utils.analyze_param",
            "file_path": "fastapi/dependencies/utils.py",
            "text": "def analyze_param ModelField Dependant endpoint parameter",
            "score": 0.70,
        }
    )

    ranked = GraphQueryService._rerank_semantic_hits(
        "endpoint parameter analyze_param ModelField Dependant", [generic, relevant]
    )

    assert ranked[0].entity_id == "relevant"


@pytest.mark.asyncio
async def test_execute_query_enforces_limit_and_reports_truncation() -> None:
    driver = FakeDriver([[{"name": "one"}, {"name": "two"}, {"name": "three"}]])
    service = GraphQueryService(driver)  # type: ignore[arg-type]

    result = await service.execute_query(
        "MATCH (entity:Entity) RETURN entity.name AS name",
        limit=2,
    )

    assert result.columns == ["name"]
    assert len(result.rows) == 2
    assert result.truncated is True
    query, parameters = driver.fake_session.calls[0]
    assert query.startswith("CALL {")
    assert parameters["result_limit"] == 3


@pytest.mark.asyncio
async def test_execute_query_protects_reserved_parameter() -> None:
    service = GraphQueryService(FakeDriver([]))  # type: ignore[arg-type]

    with pytest.raises(GraphQueryError, match="reserved"):
        await service.execute_query(
            "RETURN 1 AS value",
            parameters={"result_limit": 999},
        )


@pytest.mark.asyncio
async def test_traversal_rejects_excessive_depth() -> None:
    service = GraphQueryService(FakeDriver([]))  # type: ignore[arg-type]

    with pytest.raises(GraphQueryError, match="depth"):
        await service.get_dependencies("class-1", max_depth=6)
