"""Public API contracts exposed by the FastAPI gateway."""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from repo_chat.contracts.agents import AgentHealth
from repo_chat.contracts.evidence import Evidence
from repo_chat.contracts.indexing import IndexJobStatus, IndexMode
from repo_chat.contracts.orchestration import AgentName


class ChatRequest(BaseModel):
    """One public chat request."""

    message: str = Field(min_length=1, max_length=10_000)
    session_id: str | None = Field(default=None, min_length=1, max_length=128)
    user_id: str | None = Field(default=None, min_length=1, max_length=128)
    repository_id: str | None = Field(default=None, min_length=1, max_length=256)
    revision: str | None = Field(default=None, min_length=1, max_length=128)
    stream: bool = False


class ChatResponse(BaseModel):
    """Non-streaming public chat response."""

    answer: str
    session_id: str
    trace_id: str
    agents_used: list[AgentName] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    partial: bool = False


class StreamEventType(StrEnum):
    """Event names shared by HTTP streaming and WebSocket chat."""

    STATUS = "status"
    EVIDENCE = "evidence"
    TOKEN = "token"
    WARNING = "warning"
    DONE = "done"
    ERROR = "error"


class StreamEvent(BaseModel):
    """A typed chat streaming event."""

    type: StreamEventType
    data: Any


class ReadinessResponse(BaseModel):
    """Gateway readiness state."""

    status: str
    dependencies: dict[str, bool] = Field(default_factory=dict)


class IndexRequest(BaseModel):
    """Request to index an allowed repository directory."""

    repository_path: str = Field(min_length=1, max_length=1_024)
    repository_id: str = Field(min_length=1, max_length=256)
    revision: str = Field(min_length=1, max_length=128)
    mode: IndexMode = IndexMode.FULL


class IndexAccepted(BaseModel):
    """Reference returned when an indexing job has been accepted."""

    job_id: str
    status: IndexJobStatus
    status_url: str


class AgentsHealthResponse(BaseModel):
    """Aggregate health of internal MCP services."""

    status: str
    agents: list[AgentHealth] = Field(default_factory=list)
