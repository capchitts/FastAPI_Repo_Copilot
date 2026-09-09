from pathlib import Path

from repo_chat.contracts.indexing import EntityKind, RelationshipKind
from repo_chat.graph.resolution import (
    resolve_local_relationships,
    resolve_repository_relationships,
)
from repo_chat.indexing.ast_parser import parse_python_file, parse_python_source

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "sample_repo"


def test_resolves_unambiguous_local_inheritance_and_calls() -> None:
    parsed = parse_python_file(
        FIXTURE_ROOT / "sample" / "models.py",
        repository_root=FIXTURE_ROOT,
        repository_id="fixture",
        revision="abc123",
    )

    resolved = resolve_local_relationships(parsed)
    inheritance = next(
        relationship
        for relationship in resolved.relationships
        if relationship.kind is RelationshipKind.INHERITS_FROM
    )
    build_payload_call = next(
        relationship
        for relationship in resolved.relationships
        if relationship.kind is RelationshipKind.CALLS
        and relationship.target_qualified_name.endswith("build_payload")
    )

    assert inheritance.resolved is True
    assert inheritance.target_qualified_name == "sample.models.BaseService"
    assert build_payload_call.resolved is True
    assert build_payload_call.target_qualified_name == "sample.models.build_payload"


def test_leaves_dynamic_callable_unresolved() -> None:
    parsed = parse_python_file(
        FIXTURE_ROOT / "sample" / "models.py",
        repository_root=FIXTURE_ROOT,
        repository_id="fixture",
        revision="abc123",
    )

    resolved = resolve_local_relationships(parsed)
    callback_call = next(
        relationship
        for relationship in resolved.relationships
        if relationship.kind is RelationshipKind.CALLS
        and relationship.target_qualified_name == "callback"
    )

    assert callback_call.resolved is False
    assert callback_call.target_id.startswith("symbol:function:")


def test_does_not_resolve_ambiguous_simple_names() -> None:
    parsed = parse_python_source(
        """
def duplicate():
    return 1

class Container:
    def duplicate(self):
        return 2

def caller():
    return duplicate()
""",
        repository_id="fixture",
        revision="abc123",
        file_path="sample/ambiguous.py",
    )

    resolved = resolve_local_relationships(parsed)
    call = next(
        relationship
        for relationship in resolved.relationships
        if relationship.kind is RelationshipKind.CALLS
    )

    # The exact module-qualified function is preferred over another method with
    # the same simple name, so this reference remains deterministic.
    assert call.resolved is True
    target = next(entity for entity in resolved.entities if entity.id == call.target_id)
    assert target.kind is EntityKind.FUNCTION
    assert target.qualified_name == "sample.ambiguous.duplicate"


def test_resolution_does_not_mutate_original_parse_result() -> None:
    parsed = parse_python_source(
        "class Parent: pass\nclass Child(Parent): pass\n",
        repository_id="fixture",
        revision="abc123",
        file_path="sample/models.py",
    )

    resolved = resolve_local_relationships(parsed)
    original_edge = next(
        edge for edge in parsed.relationships if edge.kind is RelationshipKind.INHERITS_FROM
    )
    resolved_edge = next(
        edge for edge in resolved.relationships if edge.kind is RelationshipKind.INHERITS_FROM
    )

    assert original_edge.resolved is False
    assert resolved_edge.resolved is True


def test_resolves_cross_file_import_alias_inheritance_and_calls() -> None:
    base = parse_python_source(
        "class Base:\n    pass\n\ndef build():\n    return 1\n",
        repository_id="fixture",
        revision="one",
        file_path="pkg/base.py",
    )
    service = parse_python_source(
        "from .base import Base as Parent, build as make\n"
        "from . import base as core\n\n"
        "class Child(Parent):\n"
        "    def execute(self):\n"
        "        make()\n"
        "        return core.build()\n",
        repository_id="fixture",
        revision="one",
        file_path="pkg/service.py",
    )

    resolved = resolve_repository_relationships([base, service])[1]
    inheritance = next(
        edge for edge in resolved.relationships if edge.kind is RelationshipKind.INHERITS_FROM
    )
    calls = [edge for edge in resolved.relationships if edge.kind is RelationshipKind.CALLS]
    dependencies = [
        edge for edge in resolved.relationships if edge.kind is RelationshipKind.DEPENDS_ON
    ]

    assert inheritance.target_qualified_name == "pkg.base.Base"
    assert inheritance.metadata["resolution_strategy"] == "import_alias"
    assert {edge.target_qualified_name for edge in calls} == {"pkg.base.build"}
    assert all(edge.resolved for edge in calls)
    assert {edge.target_qualified_name for edge in dependencies} >= {
        "pkg.base.Base",
        "pkg.base.build",
        "pkg.base",
    }


def test_cross_file_resolution_preserves_ambiguous_target() -> None:
    first = parse_python_source(
        "def shared():\n    pass\n",
        repository_id="fixture",
        revision="one",
        file_path="one.py",
    )
    second = parse_python_source(
        "def shared():\n    pass\n",
        repository_id="fixture",
        revision="one",
        file_path="two.py",
    )
    caller = parse_python_source(
        "def caller():\n    shared()\n",
        repository_id="fixture",
        revision="one",
        file_path="caller.py",
    )

    resolved = resolve_repository_relationships([first, second, caller])[2]
    call = next(edge for edge in resolved.relationships if edge.kind is RelationshipKind.CALLS)

    assert call.resolved is False
    assert call.target_qualified_name == "shared"
