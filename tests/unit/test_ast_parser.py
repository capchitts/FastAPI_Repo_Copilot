from pathlib import Path

import pytest

from repo_chat.contracts.indexing import EntityKind, ParsedFile, RelationshipKind
from repo_chat.indexing.ast_parser import (
    module_name_from_path,
    parse_python_file,
    parse_python_source,
)

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "sample_repo"
FIXTURE_FILE = FIXTURE_ROOT / "sample" / "models.py"


def parse_fixture() -> ParsedFile:
    return parse_python_file(
        FIXTURE_FILE,
        repository_root=FIXTURE_ROOT,
        repository_id="fixture",
        revision="abc123",
    )


def test_module_name_is_derived_from_python_path() -> None:
    assert module_name_from_path("fastapi/routing.py") == "fastapi.routing"
    assert module_name_from_path("fastapi/__init__.py") == "fastapi"


def test_parser_extracts_core_entity_kinds() -> None:
    result = parse_fixture()
    kinds = {entity.kind for entity in result.entities}

    assert {
        EntityKind.FILE,
        EntityKind.MODULE,
        EntityKind.CLASS,
        EntityKind.FUNCTION,
        EntityKind.METHOD,
        EntityKind.PARAMETER,
        EntityKind.DECORATOR,
        EntityKind.IMPORT,
        EntityKind.DOCSTRING,
    } <= kinds
    assert result.issues == []


def test_parser_builds_qualified_names_and_method_metadata() -> None:
    result = parse_fixture()
    by_name = {entity.qualified_name: entity for entity in result.entities}

    assert "sample.models.ItemService" in by_name
    assert "sample.models.ItemService.get" in by_name
    execute = by_name["sample.models.ItemService.execute"]
    assert execute.kind is EntityKind.METHOD
    assert execute.metadata["is_async"] is True
    assert execute.metadata["returns"] == "object"


def test_parser_extracts_parameters_annotations_and_defaults() -> None:
    result = parse_fixture()
    by_name = {entity.qualified_name: entity for entity in result.entities}

    limit = by_name["sample.models.ItemService.get.limit"]
    assert limit.kind is EntityKind.PARAMETER
    assert limit.metadata["annotation"] == "int"
    assert limit.metadata["default"] == "10"

    args = by_name["sample.models.ItemService.execute.args"]
    kwargs = by_name["sample.models.ItemService.execute.kwargs"]
    assert args.metadata["parameter_kind"] == "var_positional"
    assert kwargs.metadata["parameter_kind"] == "var_keyword"


def test_parser_extracts_imports_and_aliases() -> None:
    result = parse_fixture()
    imports = [entity for entity in result.entities if entity.kind is EntityKind.IMPORT]

    assert any(entity.metadata["imported_name"] == "collections.abc.Callable" for entity in imports)
    dataclass_import = next(
        entity for entity in imports if entity.metadata["imported_name"] == "dataclasses.dataclass"
    )
    assert dataclass_import.metadata["alias"] == "record"


def test_parser_extracts_structural_relationships() -> None:
    result = parse_fixture()
    kinds = {relationship.kind for relationship in result.relationships}

    assert {
        RelationshipKind.CONTAINS,
        RelationshipKind.IMPORTS,
        RelationshipKind.INHERITS_FROM,
        RelationshipKind.CALLS,
        RelationshipKind.DECORATED_BY,
        RelationshipKind.HAS_PARAMETER,
        RelationshipKind.DOCUMENTED_BY,
    } <= kinds

    inheritance = next(
        relationship
        for relationship in result.relationships
        if relationship.kind is RelationshipKind.INHERITS_FROM
    )
    assert inheritance.target_qualified_name == "BaseService"
    assert inheritance.resolved is False


def test_parser_extracts_calls_conservatively() -> None:
    result = parse_fixture()
    calls = [
        relationship
        for relationship in result.relationships
        if relationship.kind is RelationshipKind.CALLS
    ]

    assert any(call.target_qualified_name == "build_payload" for call in calls)
    assert any(call.target_qualified_name == "callback" for call in calls)
    assert all(call.resolved is False for call in calls)


def test_parser_extracts_docstring_text_and_source_lines() -> None:
    result = parse_fixture()
    docstrings = [entity for entity in result.entities if entity.kind is EntityKind.DOCSTRING]

    service_docstring = next(
        entity
        for entity in docstrings
        if entity.qualified_name == "sample.models.ItemService.__doc__"
    )
    assert service_docstring.metadata["summary"] == (
        "A service containing sync and asynchronous methods."
    )
    assert service_docstring.start_line > 0
    assert service_docstring.end_line >= service_docstring.start_line


def test_entity_ids_are_stable_across_repeated_parses() -> None:
    first = parse_fixture()
    second = parse_fixture()

    assert [entity.id for entity in first.entities] == [entity.id for entity in second.entities]
    assert first.content_hash == second.content_hash


def test_content_change_updates_hash_without_changing_symbol_identity() -> None:
    original = parse_python_source(
        "def example() -> int:\n    return 1\n",
        repository_id="fixture",
        revision="one",
        file_path="sample/example.py",
    )
    changed = parse_python_source(
        "def example() -> int:\n    return 2\n",
        repository_id="fixture",
        revision="two",
        file_path="sample/example.py",
    )
    original_function = next(
        entity
        for entity in original.entities
        if entity.name == "example" and entity.kind is EntityKind.FUNCTION
    )
    changed_function = next(
        entity
        for entity in changed.entities
        if entity.name == "example" and entity.kind is EntityKind.FUNCTION
    )

    assert original.content_hash != changed.content_hash
    assert original_function.id == changed_function.id


def test_syntax_error_returns_fatal_issue_instead_of_raising() -> None:
    result = parse_python_source(
        "def broken(:\n",
        repository_id="fixture",
        revision="abc123",
        file_path="sample/broken.py",
    )

    assert result.entities == []
    assert len(result.issues) == 1
    assert result.issues[0].fatal is True
    assert result.issues[0].line == 1


def test_empty_file_still_produces_file_and_module_entities() -> None:
    result = parse_python_source(
        "",
        repository_id="fixture",
        revision="abc123",
        file_path="sample/empty.py",
    )

    assert [entity.kind for entity in result.entities] == [
        EntityKind.FILE,
        EntityKind.MODULE,
    ]
    assert all(entity.start_line == 1 for entity in result.entities)


def test_file_parser_rejects_paths_outside_repository_root(tmp_path: Path) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("value = 1\n", encoding="utf-8")

    with pytest.raises(ValueError, match="contained by repository_root"):
        parse_python_file(
            outside,
            repository_root=repository_root,
            repository_id="fixture",
            revision="abc123",
        )
