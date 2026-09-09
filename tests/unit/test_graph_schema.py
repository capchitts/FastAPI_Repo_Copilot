from repo_chat.contracts.indexing import EntityKind
from repo_chat.graph.schema import ENTITY_LABELS, schema_statements


def test_every_entity_kind_has_a_safe_static_label() -> None:
    assert set(ENTITY_LABELS) == set(EntityKind)
    assert all(label.isalpha() for label in ENTITY_LABELS.values())


def test_schema_statements_are_idempotent() -> None:
    statements = schema_statements()

    assert statements
    assert all("IF NOT EXISTS" in statement for statement in statements)
    assert any("entity_id" in statement for statement in statements)
    assert any("external_symbol_id" in statement for statement in statements)
    assert any("qualified_name" in statement for statement in statements)


def test_schema_contains_no_user_supplied_interpolation() -> None:
    assert all("$" not in statement for statement in schema_statements())
