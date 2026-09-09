"""Live Neo4j tests, enabled explicitly with RUN_NEO4J_INTEGRATION=1."""

import os
from pathlib import Path
from uuid import uuid4

import pytest
from neo4j import AsyncGraphDatabase

from repo_chat.config.settings import Settings
from repo_chat.contracts.indexing import EntityKind, RelationshipKind
from repo_chat.graph.query_service import GraphQueryService
from repo_chat.graph.repository import GraphRepository
from repo_chat.graph.schema import initialize_schema
from repo_chat.indexing.ast_parser import parse_python_file

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_NEO4J_INTEGRATION") != "1",
        reason="set RUN_NEO4J_INTEGRATION=1 to run live Neo4j tests",
    ),
]

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "sample_repo"


@pytest.mark.asyncio
async def test_fixture_is_persisted_idempotently_in_neo4j() -> None:
    settings = Settings()
    repository_id = f"integration-{uuid4()}"
    revision = "fixture-revision"
    revision_id = f"{repository_id}:{revision}"
    driver = AsyncGraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_username, settings.neo4j_password.get_secret_value()),
    )
    connected = False
    try:
        await driver.verify_connectivity()
        connected = True
        await initialize_schema(driver, settings.neo4j_database)
        parsed = parse_python_file(
            FIXTURE_ROOT / "sample" / "models.py",
            repository_root=FIXTURE_ROOT,
            repository_id=repository_id,
            revision=revision,
        )
        repository = GraphRepository(driver, settings.neo4j_database)

        await repository.write_parsed_file(parsed)
        first_statistics = await repository.get_statistics(repository_id, revision)
        await repository.write_parsed_file(parsed)
        second_statistics = await repository.get_statistics(repository_id, revision)

        assert first_statistics == second_statistics
        assert first_statistics.entity_count == len(parsed.entities)
        assert first_statistics.entities_by_kind["class"] == 2

        graph_queries = GraphQueryService(driver, settings.neo4j_database)
        search = await graph_queries.find_entity(
            "ItemService",
            kind=EntityKind.CLASS,
        )
        assert len(search.entities) == 1
        assert search.entities[0].qualified_name == "sample.models.ItemService"

        related = await graph_queries.find_related(
            search.entities[0].id,
            RelationshipKind.INHERITS_FROM,
        )
        assert len(related.relations) == 1
        assert related.relations[0].target.qualified_name == "sample.models.BaseService"

        async with driver.session(database=settings.neo4j_database) as session:
            inheritance = await session.run(
                """
                MATCH (child:Class {repository_id: $repository_id})
                      -[edge:INHERITS_FROM]->(parent:Class)
                RETURN child.qualified_name AS child,
                       parent.qualified_name AS parent,
                       edge.resolved AS resolved
                """,
                repository_id=repository_id,
            )
            inheritance_record = await inheritance.single(strict=True)
            assert inheritance_record["child"] == "sample.models.ItemService"
            assert inheritance_record["parent"] == "sample.models.BaseService"
            assert inheritance_record["resolved"] is True

            external = await session.run(
                """
                MATCH (:Entity {repository_id: $repository_id})
                      -[edge:CALLS]->(symbol:ExternalSymbol)
                RETURN collect(symbol.qualified_name) AS names,
                       collect(edge.resolved) AS resolutions
                """,
                repository_id=repository_id,
            )
            external_record = await external.single(strict=True)
            assert "callback" in external_record["names"]
            assert all(value is False for value in external_record["resolutions"])
    finally:
        if connected:
            async with driver.session(database=settings.neo4j_database) as session:
                cleanup = await session.run(
                    """
                    MATCH (revision:Revision {id: $revision_id})
                    OPTIONAL MATCH (entity:Entity)-[:AT_REVISION]->(revision)
                    OPTIONAL MATCH (entity)-[]->(symbol:ExternalSymbol)
                    DETACH DELETE entity, symbol, revision
                    WITH count(*) AS ignored
                    MATCH (repository:Repository {id: $repository_id})
                    DETACH DELETE repository
                    """,
                    revision_id=revision_id,
                    repository_id=repository_id,
                )
                await cleanup.consume()
        await driver.close()
