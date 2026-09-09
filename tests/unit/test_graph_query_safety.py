import pytest

from repo_chat.exceptions.base import GraphQueryError
from repo_chat.graph.query_safety import validate_read_only_cypher


@pytest.mark.parametrize(
    "query",
    [
        "CREATE (node:Unsafe)",
        "MATCH (node) DELETE node",
        "MATCH (node) DETACH DELETE node",
        "MATCH (node) SET node.value = 1 RETURN node",
        "CALL db.labels()",
        "LOAD CSV FROM 'https://example.test/data.csv' AS row RETURN row",
        "DROP INDEX entity_name",
    ],
)
def test_rejects_mutating_or_procedural_queries(query: str) -> None:
    with pytest.raises(GraphQueryError):
        validate_read_only_cypher(query, limit=10)


def test_rejects_empty_and_multiple_statements() -> None:
    with pytest.raises(GraphQueryError, match="empty"):
        validate_read_only_cypher("  ", limit=10)
    with pytest.raises(GraphQueryError, match="Multiple"):
        validate_read_only_cypher("MATCH (n) RETURN n; MATCH (m) RETURN m", limit=10)


@pytest.mark.parametrize("limit", [0, 201])
def test_rejects_excessive_result_limits(limit: int) -> None:
    with pytest.raises(GraphQueryError, match="between"):
        validate_read_only_cypher("MATCH (n) RETURN n", limit=limit)


def test_allows_keywords_inside_strings_and_comments() -> None:
    safe = validate_read_only_cypher(
        "// DELETE is documentation\nRETURN 'CREATE is text' AS message;",
        limit=25,
    )

    assert safe.limit == 25
    assert safe.query.startswith("CALL {")
    assert safe.query.endswith("RETURN * LIMIT $result_limit")


def test_rejects_non_read_clause_start() -> None:
    with pytest.raises(GraphQueryError, match="read-only clause"):
        validate_read_only_cypher("EXPLAIN MATCH (n) RETURN n", limit=10)
