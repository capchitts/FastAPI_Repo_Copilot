from typing import Generic, TypeVar

from pydantic import BaseModel, Field

from repo_chat.contracts.evidence import Evidence

DataT = TypeVar("DataT")


class ToolResult(BaseModel, Generic[DataT]):
    data: DataT | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    warnings: list[str] = Field(default_factory=list)
    index_revision: str | None = None
    trace_id: str
