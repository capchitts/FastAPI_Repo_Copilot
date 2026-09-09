"""Batch persistence of parsed source structures in Neo4j."""

import json
from collections import defaultdict
from datetime import datetime
from typing import Any

from neo4j import AsyncDriver, AsyncManagedTransaction

from repo_chat.contracts.indexing import (
    CodeEntity,
    CodeRelationship,
    EntityKind,
    GraphStatistics,
    ParsedFile,
    RelationshipKind,
)
from repo_chat.graph.resolution import resolve_local_relationships
from repo_chat.graph.schema import ENTITY_LABELS


class GraphRepository:
    """Persist repository revisions and parsed files using parameterized Cypher."""

    def __init__(self, driver: AsyncDriver, database: str | None = None) -> None:
        self._driver = driver
        self._database = database

    async def write_parsed_file(self, parsed_file: ParsedFile) -> None:
        """Atomically upsert a parsed file and all of its relationships."""
        resolved_file = resolve_local_relationships(parsed_file)
        async with self._driver.session(database=self._database) as session:
            await session.execute_write(self._write_file_transaction, resolved_file)

    async def write_repository_relationships(
        self, parsed_files: list[ParsedFile]
    ) -> None:
        """Retry revision relationships after every repository entity exists."""
        relationships = [
            relationship
            for parsed in parsed_files
            if not any(issue.fatal for issue in parsed.issues)
            for relationship in parsed.relationships
        ]
        async with self._driver.session(database=self._database) as session:
            await session.execute_write(
                self._write_repository_relationships_transaction, relationships
            )

    @staticmethod
    async def _write_repository_relationships_transaction(
        transaction: AsyncManagedTransaction,
        relationships: list[CodeRelationship],
    ) -> None:
        unresolved = [relationship for relationship in relationships if not relationship.resolved]
        if unresolved:
            await GraphRepository._write_external_symbols(transaction, unresolved)
        grouped: dict[RelationshipKind, list[CodeRelationship]] = defaultdict(list)
        for relationship in relationships:
            grouped[relationship.kind].append(relationship)
        for kind, items in grouped.items():
            await GraphRepository._write_relationship_batch(transaction, kind, items)

    async def carry_forward_file(
        self,
        *,
        repository_id: str,
        source_revision: str,
        target_revision: str,
        file_path: str,
        content_hash: str,
    ) -> None:
        """Associate unchanged verified entities with a new repository revision."""
        source_revision_id = f"{repository_id}:{source_revision}"
        target_revision_id = f"{repository_id}:{target_revision}"
        async with self._driver.session(database=self._database) as session:
            result = await session.run(
                """
                MERGE (repository:Repository {id: $repository_id})
                MERGE (target:Revision {id: $target_revision_id})
                SET target.sha = $target_revision,
                    target.repository_id = $repository_id
                MERGE (repository)-[:HAS_REVISION]->(target)
                WITH target
                MATCH (entity:Entity)-[:AT_REVISION]->(:Revision {id: $source_revision_id})
                WHERE entity.repository_id = $repository_id
                  AND entity.file_path = $file_path
                  AND entity.content_hash = $content_hash
                SET entity.latest_revision = $target_revision
                MERGE (entity)-[:AT_REVISION]->(target)
                """,
                repository_id=repository_id,
                source_revision_id=source_revision_id,
                target_revision_id=target_revision_id,
                target_revision=target_revision,
                file_path=file_path,
                content_hash=content_hash,
            )
            await result.consume()

    async def remove_file_from_revision(
        self, *, repository_id: str, revision: str, file_path: str
    ) -> None:
        """Remove a deleted path only from the target revision, preserving history."""
        revision_id = f"{repository_id}:{revision}"
        async with self._driver.session(database=self._database) as session:
            result = await session.run(
                """
                MATCH (entity:Entity)-[membership:AT_REVISION]->(:Revision {id: $revision_id})
                WHERE entity.repository_id = $repository_id
                  AND entity.file_path = $file_path
                DELETE membership
                WITH DISTINCT entity
                OPTIONAL MATCH (entity)-[:AT_REVISION]->(remaining:Revision)
                WITH entity,
                     [sha IN collect(remaining.sha) WHERE sha IS NOT NULL] AS revisions
                SET entity.latest_revision = CASE
                    WHEN size(revisions) = 0 THEN null
                    ELSE revisions[0]
                END
                WITH entity, revisions
                WHERE size(revisions) = 0
                DETACH DELETE entity
                """,
                repository_id=repository_id,
                revision_id=revision_id,
                file_path=file_path,
            )
            await result.consume()

    async def record_revision_snapshot(
        self,
        *,
        repository_id: str,
        revision: str,
        repository_source: str,
        captured_at: datetime,
        indexed_at: datetime,
        index_job_id: str,
        remote_url: str | None = None,
        commit_timestamp: datetime | None = None,
        branch_or_tag: str | None = None,
    ) -> None:
        """Attach completed index-job provenance to one repository revision."""
        revision_id = f"{repository_id}:{revision}"
        async with self._driver.session(database=self._database) as session:
            result = await session.run(
                """
                MATCH (snapshot:Revision {id: $revision_id})
                SET snapshot.repository_source = $repository_source,
                    snapshot.captured_at = $captured_at,
                    snapshot.indexed_at = $indexed_at,
                    snapshot.index_job_id = $index_job_id,
                    snapshot.remote_url = $remote_url,
                    snapshot.commit_timestamp = $commit_timestamp,
                    snapshot.branch_or_tag = $branch_or_tag,
                    snapshot.immutable = true
                """,
                revision_id=revision_id,
                repository_source=repository_source,
                captured_at=captured_at,
                indexed_at=indexed_at,
                index_job_id=index_job_id,
                remote_url=remote_url,
                commit_timestamp=commit_timestamp,
                branch_or_tag=branch_or_tag,
            )
            await result.consume()

    @staticmethod
    async def _write_file_transaction(
        transaction: AsyncManagedTransaction,
        parsed_file: ParsedFile,
    ) -> None:
        revision_id = f"{parsed_file.repository_id}:{parsed_file.revision}"
        result = await transaction.run(
            """
            MERGE (repository:Repository {id: $repository_id})
            MERGE (revision:Revision {id: $revision_id})
            SET revision.sha = $revision, revision.repository_id = $repository_id
            MERGE (repository)-[:HAS_REVISION]->(revision)
            """,
            repository_id=parsed_file.repository_id,
            revision_id=revision_id,
            revision=parsed_file.revision,
        )
        await result.consume()

        grouped_entities: dict[EntityKind, list[CodeEntity]] = defaultdict(list)
        for entity in parsed_file.entities:
            grouped_entities[entity.kind].append(entity)
        for entity_kind, entities in grouped_entities.items():
            await GraphRepository._write_entity_batch(
                transaction,
                entity_kind,
                entities,
                revision_id,
                parsed_file.repository_id,
                parsed_file.revision,
            )

        unresolved = [
            relationship for relationship in parsed_file.relationships if not relationship.resolved
        ]
        if unresolved:
            await GraphRepository._write_external_symbols(transaction, unresolved)

        grouped_relationships: dict[RelationshipKind, list[CodeRelationship]] = defaultdict(list)
        for relationship in parsed_file.relationships:
            grouped_relationships[relationship.kind].append(relationship)
        for relationship_kind, relationships in grouped_relationships.items():
            await GraphRepository._write_relationship_batch(
                transaction, relationship_kind, relationships
            )

    @staticmethod
    async def _write_entity_batch(
        transaction: AsyncManagedTransaction,
        kind: EntityKind,
        entities: list[CodeEntity],
        revision_id: str,
        repository_id: str,
        revision: str,
    ) -> None:
        label = ENTITY_LABELS[kind]
        rows = [GraphRepository._entity_row(entity) for entity in entities]
        query = f"""
            UNWIND $rows AS row
            MERGE (entity:Entity:{label} {{id: row.id}})
            SET entity.kind = row.kind,
                entity.name = row.name,
                entity.qualified_name = row.qualified_name,
                entity.file_path = row.file_path,
                entity.start_line = row.start_line,
                entity.end_line = row.end_line,
                entity.content_hash = row.content_hash,
                entity.metadata_json = row.metadata_json,
                entity.repository_id = $repository_id,
                entity.latest_revision = $revision
            WITH entity
            MATCH (revision:Revision {{id: $revision_id}})
            MERGE (entity)-[:AT_REVISION]->(revision)
        """
        result = await transaction.run(
            query,
            rows=rows,
            revision_id=revision_id,
            repository_id=repository_id,
            revision=revision,
        )
        await result.consume()

    @staticmethod
    def _entity_row(entity: CodeEntity) -> dict[str, Any]:
        return {
            "id": entity.id,
            "kind": entity.kind.value,
            "name": entity.name,
            "qualified_name": entity.qualified_name,
            "file_path": entity.file_path,
            "start_line": entity.start_line,
            "end_line": entity.end_line,
            "content_hash": entity.content_hash,
            "metadata_json": json.dumps(entity.metadata, sort_keys=True),
        }

    @staticmethod
    async def _write_external_symbols(
        transaction: AsyncManagedTransaction,
        relationships: list[CodeRelationship],
    ) -> None:
        rows = [
            {
                "id": relationship.target_id,
                "qualified_name": relationship.target_qualified_name,
            }
            for relationship in relationships
        ]
        result = await transaction.run(
            """
            UNWIND $rows AS row
            MERGE (symbol:ExternalSymbol {id: row.id})
            SET symbol.qualified_name = row.qualified_name
            """,
            rows=rows,
        )
        await result.consume()

    @staticmethod
    async def _write_relationship_batch(
        transaction: AsyncManagedTransaction,
        kind: RelationshipKind,
        relationships: list[CodeRelationship],
    ) -> None:
        rows = [
            {
                "source_id": relationship.source_id,
                "target_id": relationship.target_id,
                "resolved": relationship.resolved,
                "file_path": relationship.file_path,
                "line": relationship.line,
                "metadata_json": json.dumps(relationship.metadata, sort_keys=True),
            }
            for relationship in relationships
        ]
        resolved_rows = [row for row in rows if row["resolved"]]
        if resolved_rows:
            cleanup = await transaction.run(
                f"""
                UNWIND $rows AS row
                MATCH (source:Entity {{id: row.source_id}})
                      -[stale:{kind.value}]->(symbol:ExternalSymbol)
                WHERE stale.file_path = row.file_path AND stale.line = row.line
                DELETE stale
                """,
                rows=resolved_rows,
            )
            await cleanup.consume()
        query = f"""
            UNWIND $rows AS row
            MATCH (source:Entity {{id: row.source_id}})
            MATCH (target {{id: row.target_id}})
            WHERE target:Entity OR target:ExternalSymbol
            MERGE (source)-[relationship:{kind.value}]->(target)
            SET relationship.resolved = row.resolved,
                relationship.file_path = row.file_path,
                relationship.line = row.line,
                relationship.metadata_json = row.metadata_json
        """
        result = await transaction.run(query, rows=rows)
        await result.consume()

    async def get_statistics(self, repository_id: str, revision: str) -> GraphStatistics:
        """Return entity and relationship counts for one repository revision."""
        revision_id = f"{repository_id}:{revision}"
        async with self._driver.session(database=self._database) as session:
            result = await session.run(
                """
                MATCH (entity:Entity)-[:AT_REVISION]->(:Revision {id: $revision_id})
                OPTIONAL MATCH (entity)-[relationship]->(target)
                WHERE NOT type(relationship) = 'AT_REVISION'
                RETURN entity.kind AS kind,
                       count(DISTINCT entity) AS entity_count,
                       count(DISTINCT relationship) AS relationship_count
                """,
                revision_id=revision_id,
            )
            records = [record async for record in result]

        entities_by_kind = {
            str(record["kind"]): int(record["entity_count"])
            for record in records
            if record["kind"] is not None
        }
        return GraphStatistics(
            repository_id=repository_id,
            revision=revision,
            entity_count=sum(entities_by_kind.values()),
            relationship_count=sum(int(record["relationship_count"]) for record in records),
            entities_by_kind=entities_by_kind,
        )
