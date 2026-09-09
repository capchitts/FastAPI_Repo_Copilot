from pathlib import Path
from typing import Any

import pytest

from repo_chat.contracts.indexing import EntityKind, RelationshipKind
from repo_chat.graph.repository import GraphRepository
from repo_chat.graph.resolution import resolve_local_relationships
from repo_chat.indexing.ast_parser import parse_python_file

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "sample_repo"


class FakeResult:
    def __init__(self) -> None:
        self.consumed = False

    async def consume(self) -> None:
        self.consumed = True


class FakeTransaction:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], FakeResult]] = []

    async def run(self, query: str, **parameters: Any) -> FakeResult:
        result = FakeResult()
        self.calls.append((query, parameters, result))
        return result


class FakeSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], FakeResult]] = []

    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def run(self, query: str, **parameters: Any) -> FakeResult:
        result = FakeResult()
        self.calls.append((query, parameters, result))
        return result


class FakeDriver:
    def __init__(self) -> None:
        self.created_session = FakeSession()

    def session(self, **kwargs: object) -> FakeSession:
        return self.created_session


def parse_fixture():  # type: ignore[no-untyped-def]
    parsed = parse_python_file(
        FIXTURE_ROOT / "sample" / "models.py",
        repository_root=FIXTURE_ROOT,
        repository_id="fixture",
        revision="abc123",
    )
    return resolve_local_relationships(parsed)


@pytest.mark.asyncio
async def test_write_transaction_batches_entities_by_kind() -> None:
    transaction = FakeTransaction()
    parsed = parse_fixture()

    await GraphRepository._write_file_transaction(transaction, parsed)  # type: ignore[arg-type]

    entity_calls = [call for call in transaction.calls if "MERGE (entity:Entity:" in call[0]]
    present_kinds = {entity.kind for entity in parsed.entities}
    assert len(entity_calls) == len(present_kinds)
    assert all(call[1]["rows"] for call in entity_calls)
    assert all(call[2].consumed for call in transaction.calls)


@pytest.mark.asyncio
async def test_write_transaction_uses_parameters_for_entity_values() -> None:
    transaction = FakeTransaction()
    parsed = parse_fixture()

    await GraphRepository._write_file_transaction(transaction, parsed)  # type: ignore[arg-type]

    queries = "\n".join(call[0] for call in transaction.calls)
    assert "$rows" in queries
    assert "sample.models.ItemService" not in queries
    class_call = next(call for call in transaction.calls if "Entity:Class" in call[0])
    assert any(
        row["qualified_name"] == "sample.models.ItemService" for row in class_call[1]["rows"]
    )


@pytest.mark.asyncio
async def test_write_transaction_creates_external_symbols_for_unresolved_targets() -> None:
    transaction = FakeTransaction()
    parsed = parse_fixture()

    await GraphRepository._write_file_transaction(transaction, parsed)  # type: ignore[arg-type]

    external_call = next(
        call for call in transaction.calls if "MERGE (symbol:ExternalSymbol" in call[0]
    )
    external_names = {row["qualified_name"] for row in external_call[1]["rows"]}
    assert "callback" in external_names


@pytest.mark.asyncio
async def test_write_transaction_emits_required_relationship_types() -> None:
    transaction = FakeTransaction()
    parsed = parse_fixture()

    await GraphRepository._write_file_transaction(transaction, parsed)  # type: ignore[arg-type]

    queries = "\n".join(call[0] for call in transaction.calls)
    present_relationships = {relationship.kind for relationship in parsed.relationships}
    for relationship_kind in present_relationships:
        assert f"relationship:{relationship_kind.value}" in queries

    assert RelationshipKind.INHERITS_FROM in present_relationships
    assert EntityKind.CLASS in {entity.kind for entity in parsed.entities}


@pytest.mark.asyncio
async def test_remove_file_only_detaches_target_revision_membership() -> None:
    driver = FakeDriver()
    repository = GraphRepository(driver, "neo4j")  # type: ignore[arg-type]

    await repository.remove_file_from_revision(
        repository_id="fixture",
        revision="revision-two",
        file_path="sample/removed.py",
    )

    query, parameters, result = driver.created_session.calls[0]
    assert "DELETE membership" in query
    assert "DETACH DELETE entity" in query
    assert parameters == {
        "repository_id": "fixture",
        "revision_id": "fixture:revision-two",
        "file_path": "sample/removed.py",
    }
    assert result.consumed is True
