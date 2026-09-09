"""Contracts produced by the Code Analyst Agent."""

from enum import StrEnum

from pydantic import BaseModel, Field

from repo_chat.contracts.evidence import Evidence
from repo_chat.contracts.indexing import EntityKind


class PatternKind(StrEnum):
    """Statically detectable implementation patterns."""

    INHERITANCE = "inheritance"
    DECORATOR = "decorator"
    ASYNC_IO = "async_io"
    DEPENDENCY_INJECTION = "dependency_injection"
    FACTORY = "factory"


class CodeSnippet(BaseModel):
    """A bounded source-code excerpt and its provenance."""

    repository_id: str
    revision: str
    file_path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    code: str
    content_hash: str
    evidence: Evidence


class EntityAnalysis(BaseModel):
    """Deterministic structural analysis of a function, method, or class."""

    name: str
    qualified_name: str
    kind: EntityKind
    signature: str
    docstring: str | None = None
    decorators: list[str] = Field(default_factory=list)
    parameters: list[str] = Field(default_factory=list)
    bases: list[str] = Field(default_factory=list)
    methods: list[str] = Field(default_factory=list)
    calls: list[str] = Field(default_factory=list)
    awaits: list[str] = Field(default_factory=list)
    control_flow: list[str] = Field(default_factory=list)
    returns: list[str] = Field(default_factory=list)
    raises: list[str] = Field(default_factory=list)
    explanation: str
    snippet: CodeSnippet


class PatternMatch(BaseModel):
    """One source-grounded pattern observation."""

    kind: PatternKind
    entity_name: str
    rationale: str
    line: int = Field(ge=1)


class PatternAnalysis(BaseModel):
    """Pattern matches found within one source file."""

    file_path: str
    patterns: list[PatternMatch] = Field(default_factory=list)
    evidence: Evidence


class ImplementationComparison(BaseModel):
    """Structural comparison of two analyzed entities."""

    left: EntityAnalysis
    right: EntityAnalysis
    similarities: list[str] = Field(default_factory=list)
    differences: list[str] = Field(default_factory=list)
    summary: str
