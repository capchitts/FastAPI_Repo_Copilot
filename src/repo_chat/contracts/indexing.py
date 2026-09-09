"""Typed contracts produced by repository source parsers."""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator


class EntityKind(StrEnum):
    """Kinds of source entities represented in the knowledge graph."""

    FILE = "file"
    MODULE = "module"
    CLASS = "class"
    FUNCTION = "function"
    METHOD = "method"
    PARAMETER = "parameter"
    DECORATOR = "decorator"
    IMPORT = "import"
    DOCSTRING = "docstring"


class RelationshipKind(StrEnum):
    """Supported relationships between source entities."""

    CONTAINS = "CONTAINS"
    IMPORTS = "IMPORTS"
    INHERITS_FROM = "INHERITS_FROM"
    CALLS = "CALLS"
    DECORATED_BY = "DECORATED_BY"
    HAS_PARAMETER = "HAS_PARAMETER"
    DOCUMENTED_BY = "DOCUMENTED_BY"
    DEPENDS_ON = "DEPENDS_ON"


class IndexMode(StrEnum):
    """Supported repository indexing modes."""

    FULL = "full"
    INCREMENTAL = "incremental"


class IndexJobStatus(StrEnum):
    """Lifecycle states of a repository indexing job."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class CodeEntity(BaseModel):
    """A normalized entity extracted from a source file."""

    id: str
    kind: EntityKind
    name: str
    qualified_name: str
    file_path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    content_hash: str
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_line_range(self) -> "CodeEntity":
        """Reject inverted source ranges."""
        if self.end_line < self.start_line:
            raise ValueError("end_line must be greater than or equal to start_line")
        return self


class CodeRelationship(BaseModel):
    """A directed relationship, which may point to an unresolved symbol."""

    kind: RelationshipKind
    source_id: str
    target_id: str
    target_qualified_name: str
    resolved: bool = False
    file_path: str
    line: int = Field(ge=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ParseIssue(BaseModel):
    """A non-fatal or fatal issue found while parsing a source file."""

    file_path: str
    message: str
    line: int | None = Field(default=None, ge=1)
    fatal: bool = False


class ParsedFile(BaseModel):
    """Complete normalized output for one parsed Python file."""

    repository_id: str
    revision: str
    file_path: str
    module_name: str
    content_hash: str
    entities: list[CodeEntity] = Field(default_factory=list)
    relationships: list[CodeRelationship] = Field(default_factory=list)
    issues: list[ParseIssue] = Field(default_factory=list)


class GraphStatistics(BaseModel):
    """High-level counts for one indexed repository revision."""

    repository_id: str
    revision: str
    entity_count: int = Field(ge=0)
    relationship_count: int = Field(ge=0)
    entities_by_kind: dict[str, int] = Field(default_factory=dict)


class IndexFileResult(BaseModel):
    """Summary returned after one file is persisted."""

    file_path: str
    repository_id: str
    revision: str
    entity_count: int = Field(ge=0)
    relationship_count: int = Field(ge=0)
    issue_count: int = Field(ge=0)
    content_hash: str
    semantic_chunk_count: int = Field(default=0, ge=0)
    deleted_file_count: int = Field(default=0, ge=0)
    renamed_file_count: int = Field(default=0, ge=0)


class IndexJob(BaseModel):
    """Progress and outcome of a repository indexing job."""

    id: str
    repository_id: str
    repository_path: str
    revision: str
    mode: IndexMode
    status: IndexJobStatus = IndexJobStatus.PENDING
    discovered_files: int = Field(default=0, ge=0)
    processed_files: int = Field(default=0, ge=0)
    skipped_files: int = Field(default=0, ge=0)
    failed_files: int = Field(default=0, ge=0)
    entity_count: int = Field(default=0, ge=0)
    relationship_count: int = Field(default=0, ge=0)
    semantic_chunk_count: int = Field(default=0, ge=0)
    error: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
