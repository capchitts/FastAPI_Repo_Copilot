from enum import StrEnum

from pydantic import BaseModel, Field


class HealthStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


class AgentHealth(BaseModel):
    agent: str
    status: HealthStatus
    latency_ms: float | None = Field(default=None, ge=0)
    details: dict[str, str] = Field(default_factory=dict)
