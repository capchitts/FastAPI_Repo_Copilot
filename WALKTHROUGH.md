# Walkthrough: Run and Verify the FastAPI Repository Chat Agent

This walkthrough is intended for an evaluator or end user running the project on their own machine. It shows how to configure the stack, start all services, index a repository, run example questions, inspect multi-agent behavior, and verify observability/test coverage.

## 1. Prerequisites

Install or confirm:

- Docker with Compose support
- `git`
- `curl`
- `jq`
- Python 3.12 and `uv`, only if running local tests/scripts outside Docker

The examples assume commands are run from the repository root.

## 2. Prepare local configuration

Create a local `.env` file:

```bash
cp .env.example .env
```

Open `.env` and set a Neo4j password:

```dotenv
REPO_CHAT_NEO4J_PASSWORD=replace-with-a-local-password
```

Optional LLM synthesis is disabled by default. To run without any external LLM provider, keep:

```dotenv
REPO_CHAT_LLM_ENABLED=false
```

Create the local repository mount directory if it does not already exist:

```bash
mkdir -p repositories
```

## 3. Start the full service stack

Build and start all services:

```bash
docker compose up -d --build
```

If your machine uses the older Compose binary:

```bash
docker-compose up -d --build
```

Check containers:

```bash
docker compose ps
```

Check Gateway readiness:

```bash
curl -sS http://localhost:8000/health/ready | jq
```

Check all MCP agents:

```bash
curl -sS http://localhost:8000/api/agents/health | jq
```

Expected result: Gateway should report ready, and configured agents should report healthy. The first startup may take longer because images are built and the embedding model may be downloaded on first semantic use.

## 4. Acquire or provide repository source

There are two supported ways to provide source code.

### Option A: use an existing local checkout

Place a FastAPI checkout under the repository mount:

```bash
git clone https://github.com/fastapi/fastapi.git repositories/fastapi
```

Capture the current commit:

```bash
export FASTAPI_REVISION="$(git -C repositories/fastapi rev-parse HEAD)"
echo "$FASTAPI_REVISION"
```

### Option B: acquire a controlled snapshot through the Repository Agent

The Repository Agent can fetch an approved HTTPS Git remote and publish a commit snapshot under `repositories/<id>/revisions/<sha>`.

```bash
curl -sS -X POST http://localhost:8000/api/repositories/acquire \
  -H 'content-type: application/json' \
  -d '{
    "repository_id": "fastapi-upstream",
    "remote_url": "https://github.com/fastapi/fastapi.git",
    "ref": "master"
  }' | jq
```

Copy the returned `revision` value:

```bash
export FASTAPI_REVISION="PASTE_RETURNED_REVISION"
```

For the index request in the next step, use:

```text
repository_path = fastapi-upstream/revisions/<revision>
repository_id   = fastapi-upstream
revision        = <revision>
```

If using Option A, use:

```text
repository_path = fastapi
repository_id   = fastapi
revision        = $FASTAPI_REVISION
```

## 5. Submit an indexing job

For a local checkout at `repositories/fastapi`:

```bash
curl -sS -X POST http://localhost:8000/api/index \
  -H 'content-type: application/json' \
  -d "{
    \"repository_path\": \"fastapi\",
    \"repository_id\": \"fastapi\",
    \"revision\": \"${FASTAPI_REVISION}\",
    \"mode\": \"full\"
  }" | tee /tmp/repo-chat-index-job.json | jq
```

Store the job ID:

```bash
export INDEX_JOB_ID="$(jq -r '.job_id' /tmp/repo-chat-index-job.json)"
echo "$INDEX_JOB_ID"
```

Poll until the job completes or fails:

```bash
curl -sS "http://localhost:8000/api/index/status/${INDEX_JOB_ID}" | jq
```

Expected behavior:

- `status` starts as `pending` or `running`.
- `processed_files` increases during the job.
- On success, `status` becomes `completed`.
- If a parse or store failure occurs, `status` becomes `failed` and `error` explains the failure.

Indexing populates:

- Neo4j with repositories, revisions, files, modules, classes, functions, methods, imports, calls, inheritance, decorators, parameters, and revision memberships.
- Qdrant with semantic vectors for class/function/method chunks when semantic search is enabled.
- Redis with job status, file manifests, idempotency claims, and repository locks.

## 6. Verify graph population

After indexing completes, request graph statistics:

```bash
curl -sS "http://localhost:8000/api/graph/statistics?repository_id=fastapi&revision=${FASTAPI_REVISION}" | jq
```

Expected result: non-zero `entity_count`, non-zero `relationship_count`, and an `entities_by_kind` map.

If you indexed an acquired repository using `fastapi-upstream`, replace the repository ID:

```bash
curl -sS "http://localhost:8000/api/graph/statistics?repository_id=fastapi-upstream&revision=${FASTAPI_REVISION}" | jq
```

## 7. Run a multi-agent implementation query

Ask for a concrete implementation:

```bash
curl -sS -X POST http://localhost:8000/api/chat \
  -H 'content-type: application/json' \
  -d "{
    \"message\": \"Show the implementation of APIRouter\",
    \"session_id\": \"walkthrough-session\",
    \"repository_id\": \"fastapi\",
    \"revision\": \"${FASTAPI_REVISION}\"
  }" | tee /tmp/repo-chat-answer.json | jq
```

Expected behavior:

- `agents_used` should include graph/source-analysis participation for a successful implementation answer.
- `evidence` should include source file and line information.
- `partial` should be `false` when all required stages succeed.
- `trace_id` should be present for log lookup.

If using `fastapi-upstream`, replace `repository_id` with `fastapi-upstream`.

The intended agent flow is:

```text
Gateway
  -> Orchestrator
  -> Graph Query Agent
  -> Repository Agent
  -> Code Analyst
  -> Orchestrator synthesis
```

## 8. Run a session-memory follow-up

Use the same `session_id` and omit repository/revision:

```bash
curl -sS -X POST http://localhost:8000/api/chat \
  -H 'content-type: application/json' \
  -d '{
    "message": "What depends on it?",
    "session_id": "walkthrough-session"
  }' | jq
```

Expected behavior: the Orchestrator reuses the active entity and repository/revision scope stored in Redis for that session.

## 9. Run broader repository questions

Request lifecycle question:

```bash
curl -sS -X POST http://localhost:8000/api/chat \
  -H 'content-type: application/json' \
  -d "{
    \"message\": \"Explain the complete lifecycle of a FastAPI request\",
    \"session_id\": \"walkthrough-lifecycle\",
    \"repository_id\": \"fastapi\",
    \"revision\": \"${FASTAPI_REVISION}\"
  }" | jq
```

Compare two implementation concepts:

```bash
curl -sS -X POST http://localhost:8000/api/chat \
  -H 'content-type: application/json' \
  -d "{
    \"message\": \"Compare how Path and Query parameters are implemented\",
    \"session_id\": \"walkthrough-comparison\",
    \"repository_id\": \"fastapi\",
    \"revision\": \"${FASTAPI_REVISION}\"
  }" | jq
```

Dependency injection question:

```bash
curl -sS -X POST http://localhost:8000/api/chat \
  -H 'content-type: application/json' \
  -d "{
    \"message\": \"How does dependency injection work in FastAPI?\",
    \"session_id\": \"walkthrough-dependencies\",
    \"repository_id\": \"fastapi\",
    \"revision\": \"${FASTAPI_REVISION}\"
  }" | jq
```

These examples demonstrate deterministic concept expansion, graph lookup, source verification, static source analysis, and final synthesis.

## 10. Try Server-Sent Events

```bash
curl -N -X POST http://localhost:8000/api/chat \
  -H 'content-type: application/json' \
  -d "{
    \"message\": \"Show the implementation of APIRouter\",
    \"repository_id\": \"fastapi\",
    \"revision\": \"${FASTAPI_REVISION}\",
    \"stream\": true
  }"
```

Expected event types include:

- `status`
- `evidence`
- `token`
- optional `warning`
- `done`

The current implementation streams progress and chunks of the completed answer. It does not stream provider tokens directly from an LLM.

## 11. Inspect observability logs

Get the trace ID from a chat response:

```bash
jq -r '.trace_id' /tmp/repo-chat-answer.json
```

Search logs with that ID:

```bash
docker compose logs gateway orchestrator indexer graph-agent repository-agent code-analyst \
  | rg 'PASTE_TRACE_ID_HERE'
```

Expected behavior:

- Gateway logs request completion.
- MCP client logs outbound tool calls.
- MCP server middleware logs tool-boundary outcomes.
- Logs contain correlation IDs, service/tool names, attempts, durations, outcomes, and normalized errors.

The system implements correlation logging, not a full distributed tracing backend.

## 12. Demonstrate partial failure behavior

Stop the Code Analyst:

```bash
docker compose stop code-analyst
```

Run a reference-dependent implementation follow-up in the same session:

```bash
curl -sS -X POST http://localhost:8000/api/chat \
  -H 'content-type: application/json' \
  -d '{
    "message": "Show the implementation of it",
    "session_id": "walkthrough-session"
  }' | jq
```

Expected behavior:

- earlier Graph/Repository evidence may remain available;
- Code Analyst failure should surface in warnings;
- `partial` should become `true` when required analysis fails.

Restart the service:

```bash
docker compose start code-analyst
curl -sS http://localhost:8000/api/agents/health | jq
```

## 13. Run tests

Install dependencies locally if needed:

```bash
uv sync
```

Run static checks and unit tests:

```bash
uv run ruff check .
uv run mypy
uv run pytest tests/unit
```

Run the default test suite:

```bash
uv run pytest
```

Run opt-in integration tests only when Redis/Neo4j are reachable from the host:

```bash
RUN_E2E_INTEGRATION=1 uv run pytest tests/integration/test_live_gateway_flow.py -vv -s
RUN_NEO4J_INTEGRATION=1 uv run pytest tests/integration/test_neo4j_persistence.py -vv -s
```

Run golden evaluation against the already indexed repository:

```bash
uv run python scripts/run_golden_evaluation.py \
  --revision "${FASTAPI_REVISION}" \
  --repository-id fastapi
```

Run a small load smoke test:

```bash
uv run python scripts/run_load_smoke.py \
  --revision "${FASTAPI_REVISION}" \
  --repository-id fastapi \
  --concurrency 3 \
  --requests 10
```

Golden evaluation checks stable behavior such as required agents, expected source paths, important concepts, partial status, and trace IDs. It avoids exact full-sentence matching because optional LLM wording can vary.

## 14. Open the browser UI

Open:

```text
http://localhost:8000/
```

Use the UI to:

- enter repository ID and revision;
- ask implementation and lifecycle questions;
- inspect source/graph evidence;
- copy trace IDs;
- verify that follow-up questions keep session context.

Open generated API docs:

```text
http://localhost:8000/docs
```

## 15. Cleanup

Stop containers without deleting persistent volumes:

```bash
docker compose down
```

Remove volumes only if you want to delete Redis, Neo4j, and Qdrant data:

```bash
docker compose down -v
```

Local source checkouts under `repositories/` are intentionally ignored by Git.

## 16. Troubleshooting

### Neo4j password error

If Compose reports that `REPO_CHAT_NEO4J_PASSWORD` is missing, confirm `.env` exists and contains:

```dotenv
REPO_CHAT_NEO4J_PASSWORD=replace-with-a-local-password
```

### Gateway not ready

Inspect service health:

```bash
docker compose ps
docker compose logs orchestrator gateway
```

The Orchestrator depends on the downstream MCP services. If an agent is unhealthy, Gateway readiness can fail.

### Indexing job fails

Inspect job status:

```bash
curl -sS "http://localhost:8000/api/index/status/${INDEX_JOB_ID}" | jq
```

Then inspect Indexer logs:

```bash
docker compose logs indexer
```

Common causes include an incorrect `repository_path`, missing source directory, invalid Neo4j credentials, or unavailable Qdrant/Redis.

### Chat returns no useful evidence

Confirm the repository was indexed with the same `repository_id` and `revision` used in the chat request:

```bash
curl -sS "http://localhost:8000/api/graph/statistics?repository_id=fastapi&revision=${FASTAPI_REVISION}" | jq
```

If counts are zero, index the repository first.

## 17. What this walkthrough demonstrates

After completing the steps above, an evaluator has seen:

- service startup through Docker Compose;
- repository source acquisition or mounting;
- indexing and knowledge graph population;
- graph statistics from Neo4j-backed data;
- semantic/vector-backed broad retrieval;
- multi-agent collaboration through MCP;
- source verification and AST-based analysis;
- deterministic synthesis with optional LLM enhancement;
- Redis-backed session memory;
- partial failure behavior;
- correlation-ID observability;
- unit/integration/golden/load-smoke testing paths.
