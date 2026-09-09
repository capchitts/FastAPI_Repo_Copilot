"""Contracts exposed by the revision-pinned Repository Agent."""

from datetime import datetime

from pydantic import BaseModel, Field

from repo_chat.contracts.code_analysis import CodeSnippet
from repo_chat.contracts.evidence import SourceLocation


class RepositoryMetadata(BaseModel):
    """Bounded metadata for one controlled repository snapshot."""

    repository_id: str
    revision: str
    source_path: str
    file_count: int = Field(ge=0)
    python_file_count: int = Field(ge=0)
    total_bytes: int = Field(ge=0)
    remote_url: str | None = None
    requested_ref: str | None = None
    commit_timestamp: datetime | None = None
    captured_at: datetime | None = None


class RepositoryAcquireRequest(BaseModel):
    """Request to acquire an immutable snapshot from an approved Git remote."""

    repository_id: str = Field(min_length=1, max_length=256)
    remote_url: str = Field(min_length=1, max_length=2_048)
    ref: str = Field(default="HEAD", min_length=1, max_length=256)


class RepositoryAcquisition(BaseModel):
    """Metadata for one safely materialized Git snapshot."""

    repository_id: str
    remote_url: str
    requested_ref: str
    revision: str
    commit_timestamp: datetime
    captured_at: datetime
    repository_path: str
    reused: bool = False


class SourceVerification(BaseModel):
    """Proof that a source file exists under the requested snapshot."""

    repository_id: str
    revision: str
    file_path: str
    exists: bool
    content_hash: str | None = None
    content_hash_matches: bool | None = None
    location: SourceLocation | None = None


class SourceSearchMatch(BaseModel):
    """One bounded lexical source match."""

    location: SourceLocation
    excerpt: str


class SourceSearchResult(BaseModel):
    """Repository-scoped lexical search results."""

    query: str
    matches: list[SourceSearchMatch] = Field(default_factory=list)
    searched_files: int = Field(ge=0)
    truncated: bool = False


class RepositoryFile(BaseModel):
    """Complete bounded source file returned by the Repository Agent."""

    snippet: CodeSnippet
