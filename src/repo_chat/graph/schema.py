"""Idempotent Neo4j schema installation."""

from neo4j import AsyncDriver

from repo_chat.contracts.indexing import EntityKind

ENTITY_LABELS: dict[EntityKind, str] = {
    EntityKind.FILE: "File",
    EntityKind.MODULE: "Module",
    EntityKind.CLASS: "Class",
    EntityKind.FUNCTION: "Function",
    EntityKind.METHOD: "Method",
    EntityKind.PARAMETER: "Parameter",
    EntityKind.DECORATOR: "Decorator",
    EntityKind.IMPORT: "Import",
    EntityKind.DOCSTRING: "Docstring",
}


def schema_statements() -> tuple[str, ...]:
    """Return the fixed allowlisted DDL used by the Indexer."""
    statements = [
        "CREATE CONSTRAINT repository_id IF NOT EXISTS "
        "FOR (node:Repository) REQUIRE node.id IS UNIQUE",
        "CREATE CONSTRAINT revision_id IF NOT EXISTS FOR (node:Revision) REQUIRE node.id IS UNIQUE",
        "CREATE CONSTRAINT entity_id IF NOT EXISTS FOR (node:Entity) REQUIRE node.id IS UNIQUE",
        "CREATE CONSTRAINT external_symbol_id IF NOT EXISTS "
        "FOR (node:ExternalSymbol) REQUIRE node.id IS UNIQUE",
        "CREATE INDEX entity_name IF NOT EXISTS FOR (node:Entity) ON (node.name)",
        "CREATE INDEX entity_qualified_name IF NOT EXISTS "
        "FOR (node:Entity) ON (node.qualified_name)",
        "CREATE INDEX entity_file_path IF NOT EXISTS FOR (node:Entity) ON (node.file_path)",
    ]
    return tuple(statements)


async def initialize_schema(driver: AsyncDriver, database: str | None = None) -> None:
    """Create all constraints and indexes, safely repeatable on startup."""
    async with driver.session(database=database) as session:
        for statement in schema_statements():
            result = await session.run(statement)
            await result.consume()
