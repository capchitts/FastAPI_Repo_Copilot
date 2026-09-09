#!/usr/bin/env python3
"""Run the versioned golden query set against a live Gateway."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import uuid4

import httpx

from repo_chat.evaluation.golden import (
    GoldenCase,
    GoldenCaseResult,
    evaluate_response,
    summarize_latencies,
)

PROJECT_ROOT = Path(__file__).parents[1]
DEFAULT_SUITE = PROJECT_ROOT / "evaluations" / "golden_queries.json"
DEFAULT_REPORT = PROJECT_ROOT / "evaluation-results" / "latest.json"


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--repository-id", default="fastapi")
    parser.add_argument("--revision", required=True)
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--case", action="append", dest="case_ids")
    parser.add_argument("--timeout", type=float, default=200.0)
    return parser.parse_args()


async def run_case(
    client: httpx.AsyncClient,
    case: GoldenCase,
    *,
    repository_id: str,
    revision: str,
    run_id: str,
) -> GoldenCaseResult:
    started = perf_counter()
    try:
        response = await client.post(
            "/api/chat",
            headers={"x-correlation-id": f"golden-{run_id}-{case.id}"},
            json={
                "message": case.query,
                "session_id": f"golden-{run_id}-{case.id}",
                "repository_id": repository_id,
                "revision": revision,
            },
        )
        try:
            payload: dict[str, Any] = response.json()
        except ValueError:
            payload = {"answer": response.text}
        return evaluate_response(
            case,
            payload,
            response_status=response.status_code,
            latency_ms=(perf_counter() - started) * 1_000,
        )
    except httpx.HTTPError as error:
        return evaluate_response(
            case,
            {"answer": str(error)},
            response_status=0,
            latency_ms=(perf_counter() - started) * 1_000,
        )


async def run_stream_case(
    client: httpx.AsyncClient,
    raw_case: dict[str, Any],
    *,
    repository_id: str,
    revision: str,
    run_id: str,
) -> dict[str, Any]:
    """Verify the live SSE event contract and incremental answer chunks."""
    started = perf_counter()
    events: list[str] = []
    statuses: list[str] = []
    first_event_ms: float | None = None
    current_event = ""
    response_status = 0
    async with client.stream(
        "POST",
        "/api/chat",
        headers={"x-correlation-id": f"golden-{run_id}-{raw_case['id']}"},
        json={
            "message": raw_case["query"],
            "session_id": f"golden-{run_id}-{raw_case['id']}",
            "repository_id": repository_id,
            "revision": revision,
            "stream": True,
        },
    ) as response:
        response_status = response.status_code
        async for line in response.aiter_lines():
            if line.startswith("event: "):
                current_event = line.removeprefix("event: ")
                events.append(current_event)
                if first_event_ms is None:
                    first_event_ms = (perf_counter() - started) * 1_000
            elif line.startswith("data: ") and current_event == "status":
                data = json.loads(line.removeprefix("data: "))
                if isinstance(data, dict) and data.get("state"):
                    statuses.append(str(data["state"]))
    required = set(raw_case["required_events"])
    checks = {
        "http_200": response_status == 200,
        "required_events": required.issubset(events),
        "started_before_completed": bool(statuses)
        and statuses[0] == "orchestration_started"
        and "completed" in statuses,
        "chunked_tokens": events.count("token") >= int(raw_case["minimum_token_events"]),
    }
    failures = [name for name, passed in checks.items() if not passed]
    return {
        "id": raw_case["id"],
        "passed": not failures,
        "latency_ms": (perf_counter() - started) * 1_000,
        "first_event_ms": first_event_ms,
        "checks": checks,
        "failures": failures,
        "events": events,
        "statuses": statuses,
        "response_status": response_status,
    }


async def main() -> int:
    arguments = parse_arguments()
    raw_suite = json.loads(arguments.suite.read_text(encoding="utf-8"))
    cases = [GoldenCase.model_validate(item) for item in raw_suite["cases"]]
    stream_cases = list(raw_suite.get("stream_cases", []))
    if arguments.case_ids:
        selected = set(arguments.case_ids)
        cases = [case for case in cases if case.id in selected]
        stream_cases = [case for case in stream_cases if case["id"] in selected]
        missing = selected - {case.id for case in cases} - {
            str(case["id"]) for case in stream_cases
        }
        if missing:
            raise SystemExit(f"Unknown golden case IDs: {sorted(missing)}")
    run_id = uuid4().hex[:8]
    async with httpx.AsyncClient(base_url=arguments.base_url, timeout=arguments.timeout) as client:
        results: list[dict[str, Any]] = []
        for case in cases:
            result = await run_case(
                client,
                case,
                repository_id=arguments.repository_id,
                revision=arguments.revision,
                run_id=run_id,
            )
            results.append(result.model_dump(mode="json"))
            state = "PASS" if result.passed else "FAIL"
            print(f"{state:4}  {case.id:34} {result.latency_ms:9.1f} ms")
            for failure in result.failures:
                print(f"      - {failure}")
        for stream_case in stream_cases:
            stream_result = await run_stream_case(
                client,
                stream_case,
                repository_id=arguments.repository_id,
                revision=arguments.revision,
                run_id=run_id,
            )
            results.append(stream_result)
            state = "PASS" if stream_result["passed"] else "FAIL"
            print(
                f"{state:4}  {stream_case['id']:34} "
                f"{stream_result['latency_ms']:9.1f} ms"
            )
            for failure in stream_result["failures"]:
                print(f"      - {failure}")

    passed = sum(bool(result["passed"]) for result in results)
    pass_rate = passed / len(results) if results else 0.0
    report = {
        "suite_version": raw_suite.get("version"),
        "run_id": run_id,
        "created_at": datetime.now(UTC).isoformat(),
        "base_url": arguments.base_url,
        "repository_id": arguments.repository_id,
        "revision": arguments.revision,
        "passed": passed,
        "total": len(results),
        "pass_rate": pass_rate,
        "minimum_pass_rate": raw_suite["minimum_pass_rate"],
        "latency": summarize_latencies(results),
        "results": results,
    }
    arguments.report.parent.mkdir(parents=True, exist_ok=True)
    arguments.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"\nGolden evaluation: {passed}/{len(results)} passed ({pass_rate:.1%})")
    latency = report["latency"]
    print(
        "Latency: "
        f"p50={latency['p50_ms']:.1f} ms, p95={latency['p95_ms']:.1f} ms, "
        f"max={latency['maximum_ms']:.1f} ms"
    )
    print(f"Report: {arguments.report}")
    return 0 if pass_rate >= float(raw_suite["minimum_pass_rate"]) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
