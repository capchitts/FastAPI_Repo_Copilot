from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class EvidenceKind(StrEnum):
    SOURCE = "source"
    GRAPH = "graph"
    DOCSTRING = "docstring"


class SnapshotProvenance(BaseModel):
    """Origin and indexing timestamps for an immutable repository revision."""

    repository_source: str | None = None
    remote_url: str | None = None
    captured_at: datetime | None = None
    indexed_at: datetime | None = None
    index_job_id: str | None = None
    commit_timestamp: datetime | None = None
    branch_or_tag: str | None = None
    immutable: bool = True


class SourceLocation(BaseModel):
    repository_id: str
    revision: str
    file_path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    content_hash: str | None = None
    snapshot: SnapshotProvenance | None = None


class Evidence(BaseModel):
    kind: EvidenceKind
    entity_id: str | None = None
    location: SourceLocation | None = None
    excerpt: str | None = None
    graph_path: list[str] = Field(default_factory=list)
