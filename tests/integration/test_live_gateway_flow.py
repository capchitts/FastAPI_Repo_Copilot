"""Live process-level test for the complete gateway and MCP request path."""

import asyncio
import os
import shutil
import socket
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from neo4j import AsyncGraphDatabase
from redis.asyncio import Redis

from repo_chat.config.settings import Settings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_E2E_INTEGRATION") != "1",
        reason="set RUN_E2E_INTEGRATION=1 to run live gateway/MCP tests",
    ),
]

PROJECT_ROOT = Path(__file__).parents[2]
FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "sample_repo"


def _free_port() -> int:
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        return int(server.getsockname()[1])


async def _wait_for_gateway(client: httpx.AsyncClient, deadline_seconds: float = 30.0) -> None:
    deadline = asyncio.get_running_loop().time() + deadline_seconds
    while asyncio.get_running_loop().time() < deadline:
        try:
            response = await client.get("/api/agents/health")
            if response.status_code == 200 and response.json()["status"] == "healthy":
                return
        except httpx.HTTPError:
            pass
        await asyncio.sleep(0.2)
    raise AssertionError("Gateway and MCP services did not become healthy in time")


async def _wait_for_index(
    client: httpx.AsyncClient, status_url: str, deadline_seconds: float = 30.0
) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + deadline_seconds
    while asyncio.get_running_loop().time() < deadline:
        response = await client.get(status_url)
        response.raise_for_status()
        job: dict[str, Any] = response.json()
        if job["status"] in {"completed", "failed"}:
            return job
        await asyncio.sleep(0.2)
    raise AssertionError("Indexing job did not finish in time")


@pytest.mark.asyncio
async def test_live_gateway_index_chat_and_context(tmp_path: Path) -> None:
    """Prove HTTP, MCP, Neo4j, source access, and Redis memory in one flow."""
    settings = Settings()
    redis_url = os.getenv("E2E_REDIS_URL", "redis://127.0.0.1:6379/0")
    repository_id = f"e2e-{uuid4().hex}"
    revision = "fixture-revision"
    repository = tmp_path / repository_id
    shutil.copytree(FIXTURE_ROOT, repository)

    indexer_port, graph_port, analyst_port, orchestrator_port, repository_port, gateway_port = (
        _free_port() for _ in range(6)
    )
    environment = os.environ.copy()
    environment.update(
        {
            "REPO_CHAT_MCP_HOST": "127.0.0.1",
            "REPO_CHAT_MCP_PORT": str(indexer_port),
            "REPO_CHAT_GRAPH_AGENT_PORT": str(graph_port),
            "REPO_CHAT_CODE_ANALYST_PORT": str(analyst_port),
            "REPO_CHAT_ORCHESTRATOR_PORT": str(orchestrator_port),
            "REPO_CHAT_REPOSITORY_AGENT_PORT": str(repository_port),
            "REPO_CHAT_GATEWAY_HOST": "127.0.0.1",
            "REPO_CHAT_GATEWAY_PORT": str(gateway_port),
            "REPO_CHAT_REPOSITORY_ROOT": str(tmp_path),
            "REPO_CHAT_REDIS_URL": redis_url,
            "REPO_CHAT_INDEXER_MCP_URL": f"http://127.0.0.1:{indexer_port}/mcp",
            "REPO_CHAT_GRAPH_AGENT_MCP_URL": f"http://127.0.0.1:{graph_port}/mcp",
            "REPO_CHAT_CODE_ANALYST_MCP_URL": f"http://127.0.0.1:{analyst_port}/mcp",
            "REPO_CHAT_ORCHESTRATOR_MCP_URL": f"http://127.0.0.1:{orchestrator_port}/mcp",
            "REPO_CHAT_REPOSITORY_AGENT_MCP_URL": (
                f"http://127.0.0.1:{repository_port}/mcp"
            ),
        }
    )
    commands = [
        "apps.indexer.server",
        "apps.graph_agent.server",
        "apps.code_analyst.server",
        "apps.repository_agent.server",
        "apps.orchestrator.server",
        "apps.gateway.server",
    ]
    processes = [
        await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            module,
            cwd=PROJECT_ROOT,
            env=environment,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        for module in commands
    ]
    session_id = f"session-{uuid4().hex}"
    index_job_id: str | None = None
    driver = AsyncGraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_username, settings.neo4j_password.get_secret_value()),
    )
    redis = Redis.from_url(redis_url)
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{gateway_port}", timeout=30.0
        ) as client:
            await _wait_for_gateway(client)
            accepted = await client.post(
                "/api/index",
                json={
                    "repository_path": repository_id,
                    "repository_id": repository_id,
                    "revision": revision,
                    "mode": "full",
                },
            )
            accepted.raise_for_status()
            assert accepted.status_code == 202
            index_job_id = accepted.json()["job_id"]
            job = await _wait_for_index(client, accepted.json()["status_url"])
            assert job["status"] == "completed", job.get("error")

            first = await client.post(
                "/api/chat",
                json={
                    "message": "Show the implementation of ItemService",
                    "session_id": session_id,
                    "repository_id": repository_id,
                    "revision": revision,
                },
            )
            assert first.status_code == 200, first.text
            assert first.json()["partial"] is False
            assert first.json()["agents_used"] == ["graph", "code_analyst"]
            assert "ItemService" in first.json()["answer"]
            assert len(first.json()["answer"]) < 2_000
            assert {item["kind"] for item in first.json()["evidence"]} == {
                "graph",
                "source",
            }

            follow_up = await client.post(
                "/api/chat",
                json={
                    "message": "What depends on it?",
                    "session_id": session_id,
                },
            )
            assert follow_up.status_code == 200, follow_up.text
            assert follow_up.json()["partial"] is False
            assert follow_up.json()["agents_used"] == ["graph"]
            assert await redis.exists(f"repo-chat:session:{session_id}") == 1
            stored_context = await redis.get(f"repo-chat:session:{session_id}")
            assert stored_context is not None
            assert repository_id in stored_context.decode()
            assert revision in stored_context.decode()
    finally:
        for process in reversed(processes):
            process.terminate()
        for process in reversed(processes):
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                process.kill()
                await process.wait()
        await redis.delete(f"repo-chat:session:{session_id}")
        if index_job_id is not None:
            await redis.delete(f"repo-chat:index-job:{index_job_id}")
            await redis.srem("repo-chat:index-job:active", index_job_id)
        async with driver.session(database=settings.neo4j_database) as session:
            result = await session.run(
                """
                MATCH (repository:Repository {id: $repository_id})
                OPTIONAL MATCH (repository)-[:HAS_REVISION]->(revision:Revision)
                OPTIONAL MATCH (entity:Entity)-[:AT_REVISION]->(revision)
                DETACH DELETE entity, revision, repository
                """,
                repository_id=repository_id,
            )
            await result.consume()
        await redis.aclose()
        await driver.close()
