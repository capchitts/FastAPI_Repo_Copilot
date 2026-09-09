import pytest
from pydantic import ValidationError

from repo_chat.contracts.agents import AgentHealth, HealthStatus
from repo_chat.contracts.common import ToolResult
from repo_chat.contracts.evidence import Evidence, EvidenceKind, SourceLocation


def make_location() -> SourceLocation:
    return SourceLocation(
        repository_id="fastapi",
        revision="abc123",
        file_path="fastapi/routing.py",
        start_line=10,
        end_line=20,
        content_hash="sha256:test",
    )


def test_source_location_accepts_valid_lines() -> None:
    location = make_location()

    assert location.repository_id == "fastapi"
    assert location.start_line == 10
    assert location.end_line == 20


@pytest.mark.parametrize(("field", "value"), [("start_line", 0), ("end_line", 0)])
def test_source_location_rejects_non_positive_lines(field: str, value: int) -> None:
    values = {
        "repository_id": "fastapi",
        "revision": "abc123",
        "file_path": "fastapi/routing.py",
        "start_line": 1,
        "end_line": 2,
    }
    values[field] = value

    with pytest.raises(ValidationError):
        SourceLocation.model_validate(values)


def test_evidence_contains_source_provenance() -> None:
    evidence = Evidence(
        kind=EvidenceKind.SOURCE,
        entity_id="class:fastapi.routing.APIRouter",
        location=make_location(),
        excerpt="class APIRouter:",
    )

    assert evidence.kind is EvidenceKind.SOURCE
    assert evidence.location is not None
    assert evidence.location.file_path == "fastapi/routing.py"
    assert evidence.graph_path == []


def test_tool_result_supports_typed_data_and_evidence() -> None:
    evidence = Evidence(kind=EvidenceKind.SOURCE, location=make_location())

    result = ToolResult[dict[str, str]](
        data={"name": "APIRouter"},
        evidence=[evidence],
        confidence=0.9,
        index_revision="abc123",
        trace_id="trace-1",
    )

    assert result.data == {"name": "APIRouter"}
    assert result.evidence == [evidence]
    assert result.confidence == 0.9


@pytest.mark.parametrize("confidence", [-0.01, 1.01])
def test_tool_result_rejects_confidence_outside_unit_interval(confidence: float) -> None:
    with pytest.raises(ValidationError):
        ToolResult[str](data="answer", confidence=confidence, trace_id="trace-1")


def test_tool_result_collection_defaults_are_not_shared() -> None:
    first = ToolResult[str](trace_id="trace-1")
    second = ToolResult[str](trace_id="trace-2")

    first.warnings.append("partial result")

    assert second.warnings == []
    assert second.evidence == []


def test_agent_health_accepts_valid_latency() -> None:
    health = AgentHealth(
        agent="graph-agent",
        status=HealthStatus.HEALTHY,
        latency_ms=12.5,
        details={"neo4j": "connected"},
    )

    assert health.status is HealthStatus.HEALTHY
    assert health.latency_ms == 12.5


def test_agent_health_rejects_negative_latency() -> None:
    with pytest.raises(ValidationError):
        AgentHealth(
            agent="graph-agent",
            status=HealthStatus.UNHEALTHY,
            latency_ms=-1,
        )
