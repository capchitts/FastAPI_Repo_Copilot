"""Stable assertions for live repository-chat regression evaluations."""

from __future__ import annotations

import math
from typing import Any

from pydantic import BaseModel, Field


class GoldenCase(BaseModel):
    """Expected observable behavior for one repository question."""

    id: str
    category: str
    query: str
    expected_agents: list[str] = Field(default_factory=list)
    expected_source_paths_any: list[str] = Field(default_factory=list)
    expected_source_paths_all: list[str] = Field(default_factory=list)
    answer_term_groups: list[list[str]] = Field(default_factory=list)
    expect_partial: bool = False


class GoldenCaseResult(BaseModel):
    """Machine-readable outcome for one live golden query."""

    id: str
    passed: bool
    latency_ms: float = Field(ge=0)
    checks: dict[str, bool]
    failures: list[str] = Field(default_factory=list)
    trace_id: str | None = None
    answer: str = ""
    agents_used: list[str] = Field(default_factory=list)
    source_paths: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    response_status: int


def summarize_latencies(results: list[dict[str, Any]]) -> dict[str, float]:
    """Return deterministic nearest-rank latency statistics for an evaluation run."""
    values = sorted(
        float(result["latency_ms"])
        for result in results
        if isinstance(result.get("latency_ms"), (int, float))
    )
    if not values:
        return {"minimum_ms": 0.0, "p50_ms": 0.0, "p95_ms": 0.0, "maximum_ms": 0.0}

    def percentile(percent: float) -> float:
        index = max(0, math.ceil(percent * len(values)) - 1)
        return round(values[index], 3)

    return {
        "minimum_ms": round(values[0], 3),
        "p50_ms": percentile(0.50),
        "p95_ms": percentile(0.95),
        "maximum_ms": round(values[-1], 3),
    }


def evaluate_response(
    case: GoldenCase,
    payload: dict[str, Any],
    *,
    response_status: int,
    latency_ms: float,
) -> GoldenCaseResult:
    """Score stable routing, grounding, and content properties without exact wording."""
    agents = [str(agent) for agent in payload.get("agents_used", [])]
    answer = str(payload.get("answer", ""))
    answer_lower = answer.casefold()
    evidence = payload.get("evidence", [])
    source_paths = sorted(
        {
            str(location["file_path"])
            for item in evidence
            if isinstance(item, dict)
            and isinstance((location := item.get("location")), dict)
            and location.get("file_path")
        }
    ) if isinstance(evidence, list) else []

    checks = {
        "http_200": response_status == 200,
        "partial": bool(payload.get("partial", True)) is case.expect_partial,
        "agents": set(case.expected_agents).issubset(agents),
        "source_any": not case.expected_source_paths_any
        or any(path in source_paths for path in case.expected_source_paths_any),
        "source_all": set(case.expected_source_paths_all).issubset(source_paths),
        "answer_terms": all(
            any(term.casefold() in answer_lower for term in alternatives)
            for alternatives in case.answer_term_groups
        ),
        "trace_id": bool(payload.get("trace_id")),
    }
    labels = {
        "http_200": f"expected HTTP 200, received {response_status}",
        "partial": f"expected partial={case.expect_partial}",
        "agents": f"missing agents: {sorted(set(case.expected_agents) - set(agents))}",
        "source_any": f"expected any source: {case.expected_source_paths_any}",
        "source_all": (
            "missing sources: "
            f"{sorted(set(case.expected_source_paths_all) - set(source_paths))}"
        ),
        "answer_terms": f"missing answer term groups: {case.answer_term_groups}",
        "trace_id": "response did not contain a trace ID",
    }
    failures = [labels[name] for name, passed in checks.items() if not passed]
    return GoldenCaseResult(
        id=case.id,
        passed=not failures,
        latency_ms=latency_ms,
        checks=checks,
        failures=failures,
        trace_id=str(payload["trace_id"]) if payload.get("trace_id") else None,
        answer=answer,
        agents_used=agents,
        source_paths=source_paths,
        warnings=[str(warning) for warning in payload.get("warnings", [])],
        response_status=response_status,
    )
