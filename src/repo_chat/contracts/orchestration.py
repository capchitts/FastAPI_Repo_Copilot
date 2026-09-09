"""Contracts used by the central Orchestrator Agent."""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from repo_chat.contracts.evidence import Evidence


class QueryIntent(StrEnum):
    """High-level intents used by deterministic routing."""

    ENTITY_LOOKUP = "entity_lookup"
    DEPENDENCY_TRAVERSAL = "dependency_traversal"
    IMPLEMENTATION = "implementation"
    COMPARISON = "comparison"
    PATTERN_ANALYSIS = "pattern_analysis"
    INDEXING = "indexing"
    ARCHITECTURE = "architecture"
    GENERAL = "general"


class AgentName(StrEnum):
    """Specialized agents available to the orchestrator."""

    INDEXER = "indexer"
    REPOSITORY = "repository"
    GRAPH = "graph"
    CODE_ANALYST = "code_analyst"


class ConversationRole(StrEnum):
    """Roles persisted in conversational memory."""

    USER = "user"
    ASSISTANT = "assistant"


class ExtractedEntity(BaseModel):
    """A likely code symbol mentioned in a user query."""

    name: str
    source: str


class QueryAnalysis(BaseModel):
    """Structured interpretation of one user query."""

    query: str
    intent: QueryIntent
    entities: list[ExtractedEntity] = Field(default_factory=list)
    needs_graph: bool = False
    needs_source: bool = False
    needs_index: bool = False
    ambiguity: float = Field(default=0.0, ge=0.0, le=1.0)


class AgentTask(BaseModel):
    """One planned agent invocation."""

    agent: AgentName
    operation: str
    sequence: int = Field(ge=0)
    reason: str
    depends_on: list[AgentName] = Field(default_factory=list)


class RoutingPlan(BaseModel):
    """Ordered execution plan produced from query analysis."""

    intent: QueryIntent
    tasks: list[AgentTask] = Field(default_factory=list)
    fallback: str


class ConversationTurn(BaseModel):
    """One bounded conversation event."""

    role: ConversationRole
    content: str
    created_at: datetime
    entities: list[str] = Field(default_factory=list)


class ConversationContext(BaseModel):
    """Relevant session state supplied to query analysis."""

    session_id: str
    turns: list[ConversationTurn] = Field(default_factory=list)
    active_entities: list[str] = Field(default_factory=list)
    repository_id: str | None = None
    revision: str | None = None
    preferences: dict[str, str] = Field(default_factory=dict)
    recalled_episodes: list["EpisodicMemory"] = Field(default_factory=list)


class EpisodicMemory(BaseModel):
    """One opt-in, user-scoped prior interaction available for selective recall."""

    query: str
    answer: str
    repository_id: str
    revision: str
    entities: list[str] = Field(default_factory=list)
    created_at: datetime


class SessionPreferences(BaseModel):
    """Bounded presentation preferences stored independently from chat turns."""

    response_detail: str = Field(default="balanced", pattern="^(concise|balanced|detailed)$")
    code_examples: str = Field(default="when_relevant", pattern="^(never|when_relevant|always)$")


class RoutingAuditRecord(BaseModel):
    """Privacy-conscious record of one routing outcome."""

    query_hash: str
    repository_id: str | None = None
    revision: str | None = None
    intent: QueryIntent
    planned_agents: list[AgentName] = Field(default_factory=list)
    successful_agents: list[AgentName] = Field(default_factory=list)
    partial: bool = False
    cache_hit: bool = False
    created_at: datetime


class AgentOutput(BaseModel):
    """Normalized outcome of one specialized-agent invocation."""

    agent: AgentName
    operation: str
    success: bool
    data: Any = None
    evidence: list[Evidence] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None


class SynthesizedResponse(BaseModel):
    """Evidence-preserving response returned by the Orchestrator."""

    answer: str
    agents_used: list[AgentName] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    partial: bool = False
