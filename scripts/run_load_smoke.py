#!/usr/bin/env python3
"""Exercise bounded concurrent chat admission against a live Gateway."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import uuid4

import httpx

PROJECT_ROOT = Path(__file__).parents[1]
DEFAULT_REPORT = PROJECT_ROOT / "evaluation-results" / "load-smoke-latest.json"


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--repository-id", default="fastapi")
    parser.add_argument("--revision", required=True)
    parser.add_argument("--requests", type=int, default=20)
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=200.0)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    return parser.parse_args()


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(percentile * len(ordered)) - 1)], 3)


async def main() -> int:
    arguments = parse_arguments()
    if arguments.requests < 1 or arguments.concurrency < 1:
        raise SystemExit("--requests and --concurrency must be positive")
    run_id = uuid4().hex[:8]
    limiter = asyncio.Semaphore(arguments.concurrency)

    async with httpx.AsyncClient(
        base_url=arguments.base_url, timeout=arguments.timeout
    ) as client:

        async def send(index: int) -> dict[str, Any]:
            async with limiter:
                started = perf_counter()
                try:
                    response = await client.post(
                        "/api/chat",
                        headers={"x-correlation-id": f"load-{run_id}-{index}"},
                        json={
                            "message": "What is the FastAPI class?",
                            "session_id": f"load-{run_id}-{index}",
                            "repository_id": arguments.repository_id,
                            "revision": arguments.revision,
                        },
                    )
                    return {
                        "index": index,
                        "status": response.status_code,
                        "latency_ms": (perf_counter() - started) * 1_000,
                    }
                except httpx.HTTPError as error:
                    return {
                        "index": index,
                        "status": 0,
                        "latency_ms": (perf_counter() - started) * 1_000,
                        "error": str(error),
                    }

        started = perf_counter()
        results = await asyncio.gather(
            *(send(index) for index in range(arguments.requests))
        )
        duration = perf_counter() - started

    completed = sum(result["status"] == 200 for result in results)
    rejected = sum(result["status"] == 429 for result in results)
    failed = len(results) - completed - rejected
    latencies = [float(result["latency_ms"]) for result in results]
    report = {
        "created_at": datetime.now(UTC).isoformat(),
        "run_id": run_id,
        "requests": arguments.requests,
        "concurrency": arguments.concurrency,
        "completed": completed,
        "rejected": rejected,
        "failed": failed,
        "duration_seconds": round(duration, 3),
        "throughput_requests_per_second": round(len(results) / duration, 3),
        "latency": {
            "p50_ms": _percentile(latencies, 0.50),
            "p95_ms": _percentile(latencies, 0.95),
            "maximum_ms": round(max(latencies), 3),
        },
        "results": results,
    }
    arguments.report.parent.mkdir(parents=True, exist_ok=True)
    arguments.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(
        f"Load smoke: completed={completed}, rejected={rejected}, failed={failed}, "
        f"throughput={report['throughput_requests_per_second']} req/s"
    )
    print(
        f"Latency: p50={report['latency']['p50_ms']} ms, "
        f"p95={report['latency']['p95_ms']} ms, max={report['latency']['maximum_ms']} ms"
    )
    print(f"Report: {arguments.report}")
    return 0 if failed == 0 and completed > 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
