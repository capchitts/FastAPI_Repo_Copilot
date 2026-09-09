"""Contracts returned by the Graph Query Agent."""

from typing import Any

from pydantic import BaseModel, Field

from repo_chat.contracts.evidence import SnapshotProvenance
from repo_chat.contracts.indexing import EntityKind, RelationshipKind


class GraphEntity(BaseModel):
    """Compact representation of an indexed source entity."""

    id: str
    kind: EntityKind
    name: str
    qualified_name: str
    file_path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    repository_id: str | None = None
    revision: str | None = None
    snapshot: SnapshotProvenance | None = None


class GraphRelation(BaseModel):
    """One relationship returned by a bounded traversal."""

    source: GraphEntity
    relationship: RelationshipKind
    target: GraphEntity
    depth: int = Field(ge=1)
    resolved: bool = False


class EntitySearchResult(BaseModel):
    """Results of a graph entity lookup."""

    query: str
    entities: list[GraphEntity] = Field(default_factory=list)


class HybridSearchHit(BaseModel):
    """One fused exact/semantic retrieval result."""

    entity: GraphEntity
    score: float = Field(ge=0)
    sources: list[str] = Field(default_factory=list)


class HybridSearchResult(BaseModel):
    """Revision-scoped results fused with reciprocal-rank fusion."""

    query: str
    repository_id: str
    revision: str
    hits: list[HybridSearchHit] = Field(default_factory=list)


class TraversalResult(BaseModel):
    """Relationships discovered from an entity or module."""

    root: str
    relations: list[GraphRelation] = Field(default_factory=list)


class CypherResult(BaseModel):
    """Size-limited JSON-compatible custom query output."""

    columns: list[str] = Field(default_factory=list)
    rows: list[dict[str, Any]] = Field(default_factory=list)
    truncated: bool = False
