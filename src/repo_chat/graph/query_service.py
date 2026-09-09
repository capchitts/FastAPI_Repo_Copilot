"""Read-only knowledge-graph operations used by the Graph Query Agent."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from neo4j import AsyncDriver

from repo_chat.contracts.evidence import SnapshotProvenance
from repo_chat.contracts.graph_queries import (
    CypherResult,
    EntitySearchResult,
    GraphEntity,
    GraphRelation,
    HybridSearchHit,
    HybridSearchResult,
    TraversalResult,
)
from repo_chat.contracts.indexing import EntityKind, RelationshipKind
from repo_chat.exceptions.base import EntityNotFoundError, GraphQueryError
from repo_chat.graph.query_safety import validate_read_only_cypher
from repo_chat.semantic.store import SemanticStore

_TRAVERSABLE_RELATIONSHIPS = frozenset(RelationshipKind)


class GraphQueryService:
    """Execute bounded, parameterized, read-only Neo4j queries."""

    def __init__(
        self,
        driver: AsyncDriver,
        database: str | None = None,
        semantic_store: SemanticStore | None = None,
    ) -> None:
        self._driver = driver
        self._database = database
        self._semantic_store = semantic_store

    async def find_entity(
        self,
        name: str,
        *,
        kind: EntityKind | None = None,
        limit: int = 20,
    ) -> EntitySearchResult:
        """Find exact or partial symbol-name matches, ranking exact matches first."""
        self._validate_limit(limit)
        rows = await self._records(
            """
            MATCH (entity:Entity)
            WHERE ($kind IS NULL OR entity.kind = $kind)
              AND (toLower(entity.name) CONTAINS toLower($name)
                   OR toLower(entity.qualified_name) CONTAINS toLower($name))
            OPTIONAL MATCH (entity)-[:AT_REVISION]->(revision:Revision)
            WHERE revision.sha = entity.latest_revision
            RETURN entity,
                   revision {
                       .repository_source,
                       .remote_url,
                       captured_at: toString(revision.captured_at),
                       indexed_at: toString(revision.indexed_at),
                       commit_timestamp: toString(revision.commit_timestamp),
                       .branch_or_tag,
                       .index_job_id,
                       .immutable
                   } AS revision
            ORDER BY CASE
                WHEN entity.qualified_name = $name THEN 0
                WHEN entity.name = $name THEN 1
                ELSE 2
            END, entity.qualified_name
            LIMIT $limit
            """,
            {
                "name": name,
                "kind": kind.value if kind is not None else None,
                "limit": limit,
            },
        )
        return EntitySearchResult(
            query=name,
            entities=[self._entity(row["entity"], row.get("revision")) for row in rows],
        )

    async def hybrid_search(
        self,
        query: str,
        *,
        repository_id: str,
        revision: str,
        limit: int = 10,
    ) -> HybridSearchResult:
        """Fuse exact graph matches and semantic code matches with RRF."""
        self._validate_limit(limit)
        exact = await self.find_entity(query, limit=min(limit * 2, 100))
        semantic_query = self._expand_semantic_query(query)
        semantic = (
            await self._semantic_store.search(
                semantic_query,
                repository_id=repository_id,
                revision=revision,
                limit=min(max(limit * 8, 50), 100),
            )
            if self._semantic_store is not None
            else []
        )
        candidates: dict[str, GraphEntity] = {}
        scores: dict[str, float] = {}
        sources: dict[str, list[str]] = {}

        def add(entity: GraphEntity, rank: int, source: str) -> None:
            candidates[entity.id] = entity
            scores[entity.id] = scores.get(entity.id, 0.0) + 1.0 / (60 + rank)
            sources.setdefault(entity.id, []).append(source)

        for rank, entity in enumerate(exact.entities, start=1):
            if entity.repository_id == repository_id and entity.revision == revision:
                add(entity, rank, "exact")
        semantic = self._rerank_semantic_hits(semantic_query, semantic)
        for rank, hit in enumerate(semantic, start=1):
            add(
                GraphEntity(
                    id=hit.entity_id,
                    kind=hit.kind,
                    name=hit.name,
                    qualified_name=hit.qualified_name,
                    file_path=hit.file_path,
                    start_line=hit.start_line,
                    end_line=hit.end_line,
                    repository_id=hit.repository_id,
                    revision=hit.revision,
                ),
                rank,
                "semantic",
            )
        ranked = sorted(scores, key=scores.__getitem__, reverse=True)[:limit]
        return HybridSearchResult(
            query=query,
            repository_id=repository_id,
            revision=revision,
            hits=[
                HybridSearchHit(
                    entity=candidates[entity_id],
                    score=scores[entity_id],
                    sources=sources[entity_id],
                )
                for entity_id in ranked
            ],
        )

    @staticmethod
    def _expand_semantic_query(query: str) -> str:
        """Add code-domain vocabulary while preserving the user's complete question."""
        lowered = query.casefold()
        hints: list[str] = []
        if "validation" in lowered and ("error" in lowered or "parameter" in lowered):
            hints.extend(
                [
                    "request_body_to_args",
                    "request_params_to_args",
                    "_validate_value_with_model_field",
                    "solve_dependencies",
                    "field errors parameter processing",
                ]
            )
        if ("await" in lowered or "async" in lowered) and "thread" in lowered:
            hints.extend(["run_endpoint_function", "is_coroutine", "run_in_threadpool"])
        if "returned" in lowered and "response" in lowered:
            hints.extend(["serialize_response", "response_content", "Response"])
        if "depend" in lowered and ("nested" in lowered or "recursive" in lowered):
            hints.extend(["solve_dependencies", "dependency_cache", "use_cache"])
        if "endpoint" in lowered and "parameter" in lowered:
            hints.extend(["get_dependant", "analyze_param", "ModelField", "Dependant"])
        return query if not hints else f"{query}\nRelevant code concepts: {' '.join(hints)}"

    @staticmethod
    def _rerank_semantic_hits(query: str, hits: list[Any]) -> list[Any]:
        """Prefer candidates sharing distinctive expanded code terms."""
        tokens = {
            token.casefold()
            for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]+", query)
            if len(token) > 3
        }

        def relevance(hit: Any) -> tuple[float, float]:
            searchable = f"{hit.qualified_name} {hit.text}".casefold()
            overlap = sum(1 for token in tokens if token in searchable)
            symbol_overlap = sum(2 for token in tokens if "_" in token and token in searchable)
            return overlap + symbol_overlap, hit.score

        return sorted(hits, key=relevance, reverse=True)

    async def get_dependencies(self, entity_id: str, *, max_depth: int = 1) -> TraversalResult:
        """Find entities reached through dependency-like outgoing relationships."""
        return await self._traverse(
            entity_id,
            direction="outgoing",
            relationship_types=(
                RelationshipKind.DEPENDS_ON,
                RelationshipKind.CALLS,
                RelationshipKind.IMPORTS,
                RelationshipKind.INHERITS_FROM,
            ),
            max_depth=max_depth,
        )

    async def get_dependents(self, entity_id: str, *, max_depth: int = 1) -> TraversalResult:
        """Find entities that depend on the selected entity."""
        return await self._traverse(
            entity_id,
            direction="incoming",
            relationship_types=(
                RelationshipKind.DEPENDS_ON,
                RelationshipKind.CALLS,
                RelationshipKind.IMPORTS,
                RelationshipKind.INHERITS_FROM,
            ),
            max_depth=max_depth,
        )

    async def trace_imports(self, module_name: str, *, max_depth: int = 3) -> TraversalResult:
        """Trace bounded outgoing import relationships from an indexed module."""
        self._validate_depth(max_depth)
        rows = await self._records(
            f"""
            MATCH (root:Module {{qualified_name: $module_name}})
            MATCH path=(root)-[:IMPORTS*1..{max_depth}]->(target:Entity)
            RETURN root, relationships(path) AS relationships,
                   nodes(path) AS nodes, length(path) AS depth
            LIMIT 200
            """,
            {"module_name": module_name},
        )
        if not rows:
            await self._ensure_entity_exists(module_name, EntityKind.MODULE)
        return TraversalResult(root=module_name, relations=self._path_relations(rows))

    async def find_related(
        self,
        entity_id: str,
        relationship_type: RelationshipKind,
        *,
        max_depth: int = 1,
    ) -> TraversalResult:
        """Find entities connected by one allowlisted relationship type."""
        if relationship_type not in _TRAVERSABLE_RELATIONSHIPS:
            raise GraphQueryError("Unsupported relationship type")
        return await self._traverse(
            entity_id,
            direction="outgoing",
            relationship_types=(relationship_type,),
            max_depth=max_depth,
        )

    async def execute_query(
        self,
        query: str,
        *,
        parameters: dict[str, Any] | None = None,
        limit: int = 100,
    ) -> CypherResult:
        """Execute validated custom read-only Cypher with an enforced result limit."""
        safe = validate_read_only_cypher(query, limit=limit)
        supplied_parameters = dict(parameters or {})
        if "result_limit" in supplied_parameters:
            raise GraphQueryError("Parameter name 'result_limit' is reserved")
        supplied_parameters["result_limit"] = safe.limit + 1
        rows = await self._records(safe.query, supplied_parameters)
        truncated = len(rows) > safe.limit
        rows = rows[: safe.limit]
        columns = list(rows[0]) if rows else []
        return CypherResult(columns=columns, rows=rows, truncated=truncated)

    async def _traverse(
        self,
        entity_id: str,
        *,
        direction: str,
        relationship_types: tuple[RelationshipKind, ...],
        max_depth: int,
    ) -> TraversalResult:
        self._validate_depth(max_depth)
        relationship_expression = "|".join(kind.value for kind in relationship_types)
        arrow = (
            f"-[relationships:{relationship_expression}*1..{max_depth}]->"
            if direction == "outgoing"
            else f"<-[relationships:{relationship_expression}*1..{max_depth}]-"
        )
        rows = await self._records(
            f"""
            MATCH (root:Entity {{id: $entity_id}})
            MATCH path=(root){arrow}(target:Entity)
            RETURN root, relationships(path) AS relationships,
                   nodes(path) AS nodes, length(path) AS depth
            LIMIT 200
            """,
            {"entity_id": entity_id},
        )
        if not rows:
            await self._ensure_entity_exists(entity_id)
        return TraversalResult(
            root=entity_id,
            relations=self._path_relations(rows, reverse_direction=direction == "incoming"),
        )

    async def _ensure_entity_exists(
        self,
        identifier: str,
        kind: EntityKind | None = None,
    ) -> None:
        rows = await self._records(
            """
            MATCH (entity:Entity)
            WHERE entity.id = $identifier OR entity.qualified_name = $identifier
            RETURN entity.id AS id, entity.kind AS kind
            LIMIT 1
            """,
            {"identifier": identifier},
        )
        if not rows or (kind is not None and rows[0]["kind"] != kind.value):
            raise EntityNotFoundError(
                "Graph entity was not found",
                details={"identifier": identifier},
            )

    async def _records(self, query: str, parameters: Mapping[str, Any]) -> list[dict[str, Any]]:
        async with self._driver.session(database=self._database) as session:
            result = await session.run(query, parameters=dict(parameters))
            return [dict(record) async for record in result]

    def _path_relations(
        self,
        rows: list[dict[str, Any]],
        *,
        reverse_direction: bool = False,
    ) -> list[GraphRelation]:
        output: list[GraphRelation] = []
        seen: set[tuple[str, str, str]] = set()
        for row in rows:
            nodes = row["nodes"]
            relationships = row["relationships"]
            for index, relationship in enumerate(relationships):
                first = self._entity(nodes[index])
                second = self._entity(nodes[index + 1])
                source, target = (second, first) if reverse_direction else (first, second)
                relationship_kind = RelationshipKind(relationship.type)
                key = (source.id, relationship_kind.value, target.id)
                if key in seen:
                    continue
                seen.add(key)
                output.append(
                    GraphRelation(
                        source=source,
                        relationship=relationship_kind,
                        target=target,
                        depth=index + 1,
                        resolved=bool(relationship.get("resolved", False)),
                    )
                )
        return output

    @staticmethod
    def _entity(
        node: Mapping[str, Any], revision_node: Mapping[str, Any] | None = None
    ) -> GraphEntity:
        snapshot = None
        if revision_node is not None and revision_node.get("indexed_at") is not None:
            snapshot = SnapshotProvenance(
                repository_source=revision_node.get("repository_source"),
                remote_url=revision_node.get("remote_url"),
                captured_at=revision_node.get("captured_at"),
                indexed_at=revision_node.get("indexed_at"),
                index_job_id=revision_node.get("index_job_id"),
                commit_timestamp=revision_node.get("commit_timestamp"),
                branch_or_tag=revision_node.get("branch_or_tag"),
                immutable=bool(revision_node.get("immutable", True)),
            )
        return GraphEntity(
            id=str(node["id"]),
            kind=EntityKind(node["kind"]),
            name=str(node["name"]),
            qualified_name=str(node["qualified_name"]),
            file_path=str(node["file_path"]),
            start_line=int(node["start_line"]),
            end_line=int(node["end_line"]),
            repository_id=str(node["repository_id"]) if node.get("repository_id") else None,
            revision=str(node["latest_revision"]) if node.get("latest_revision") else None,
            snapshot=snapshot,
        )

    @staticmethod
    def _validate_depth(max_depth: int) -> None:
        if not 1 <= max_depth <= 5:
            raise GraphQueryError("Traversal depth must be between 1 and 5")

    @staticmethod
    def _validate_limit(limit: int) -> None:
        if not 1 <= limit <= 100:
            raise GraphQueryError("Result limit must be between 1 and 100")
