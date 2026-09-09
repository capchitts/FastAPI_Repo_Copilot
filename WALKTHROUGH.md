# FastAPI Repository Chat Agent — Submission Walkthrough

This document is both the required written walkthrough and a script for a 10–15 minute demo video. It demonstrates the implemented system against the official FastAPI repository at revision `50113da16fec53b66b80d75e80a89296de4fa5a5`.

## Demo outcome

The system indexes Python structure into Neo4j and symbol chunks into Qdrant, answers repository questions through collaborating MCP services, cites revision-pinned source, retains bounded session context in Redis, persists index-job status, and returns useful partial results when an agent is unavailable.

The verified FastAPI index contains:

- 1,138 Python files;
- 20,494 extracted entity events;
- 31,392 extracted relationship events;
- zero failed files.

## Before recording

1. Place the FastAPI checkout at `repositories/fastapi`.
2. Copy `.env.example` to `.env` and configure Neo4j credentials.
3. Never display `.env`, passwords, tokens, or Aura credentials in the recording.
4. Build the stack and index the repository before recording if time is limited.
5. Open four terminals: health, API calls, optional logs, and tests.

Start the stack:

```bash
sudo docker compose up -d --build
sudo docker compose ps
```

Set the revision used by the examples:

```bash
export FASTAPI_REVISION=50113da16fec53b66b80d75e80a89296de4fa5a5
```

## 0:00–1:00 — Problem and solution

Say:

> Suppose I ask, “Explain code for `APIRouter`.” The system finds the definition, checks the file, reads the code, and answers with source references. The Orchestrator chooses those steps; Graph, Repository, and Analyst carry them out through MCP. Before questions, the Indexer prepares graph and vector data. Redis remembers conversations and indexing progress. An optional model can improve wording, but the first answer is built from source facts.

Clarify the assignment ambiguity:

> The assignment says five MCP servers but names four roles. I implemented those roles plus a fifth Repository MCP server with a concrete least-privilege boundary: metadata, bounded source retrieval and search, and provenance verification. The gateway remains a separate non-agent transport service.

## 1:00–2:30 — Architecture

Show [DESIGN.md](DESIGN.md) and describe this request path:

```text
Client
  │ REST / SSE / WebSocket
  ▼
FastAPI Gateway
  │ MCP Streamable HTTP
  ▼
Orchestrator MCP
  ├── Graph Query MCP ─────── Neo4j + Qdrant
  ├── Repository MCP ──────── revision-pinned source verification
  ├── Code Analyst MCP ───── revision-pinned source checkout

Gateway operational request → Indexer MCP → Neo4j + Qdrant

Redis: conversation sessions + durable index-job records
```

Explain one job per component:

- Gateway receives the question and gives it a session/trace ID.
- Orchestrator decides “find → verify → analyze”; its executor supplies actual tool arguments.
- Graph returns candidate symbols and locations from Neo4j or hybrid search.
- Repository acquires Git snapshots and checks selected source files.
- Analyst reads the source and extracts facts without executing it.
- Indexer prepares the graph and vectors before chat. A call in `routes.py` can be connected to its definition in `helpers.py` after both files are parsed.
- Redis stores turns, job progress, manifests, cache entries, and coordination keys. The source volume holds files; Neo4j holds structure; Qdrant holds vectors and bounded text.

## 2:30–3:15 — Health and deployment

Run:

```bash
curl -sS http://localhost:8000/health/ready | jq
curl -sS http://localhost:8000/api/agents/health | jq
```

Point out that Compose uses separate non-root application images, health-ordered dependencies, a private network, persistent Neo4j/Redis volumes, and a read-only source mount for query services.

## 3:15–4:30 — Indexing and graph population

If the repository is already indexed, show graph statistics without repeating the full indexing run:

```bash
curl -sS "http://localhost:8000/api/graph/statistics?repository_id=fastapi&revision=${FASTAPI_REVISION}" | jq
```

To demonstrate job submission, run this only if re-indexing time is acceptable:

```bash
curl -sS -X POST http://localhost:8000/api/index \
  -H 'content-type: application/json' \
  -d "{
    \"repository_path\": \"fastapi\",
    \"repository_id\": \"fastapi\",
    \"revision\": \"${FASTAPI_REVISION}\",
    \"mode\": \"incremental\"
  }" | jq
```

Copy the returned job ID and poll it:

```bash
curl -sS http://localhost:8000/api/index/status/YOUR_JOB_ID | jq
```

Say:

> The accepted response gives us a job ID, not a finished index. Poll until completed or failed. Redis tracks how many files were processed; Neo4j stores entities and links; Qdrant stores vectors. If the Indexer process crashes, the retained counters help diagnosis, but the Python task cannot resume automatically. A later failure can leave earlier database writes committed.

## 4:30–6:00 — Multi-agent implementation query

Run:

```bash
curl -sS -X POST http://localhost:8000/api/chat \
  -H 'content-type: application/json' \
  -d "{
    \"message\": \"Show the implementation of APIRouter\",
    \"session_id\": \"walkthrough-memory\",
    \"repository_id\": \"fastapi\",
    \"revision\": \"${FASTAPI_REVISION}\"
  }" | jq
```

Point out:

- `graph` resolves the preferred executable definition rather than an import or documentation mention;
- `code_analyst` reads `fastapi/routing.py` from the selected revision;
- the response includes a bounded explanation and exact source evidence;
- `partial: false` and an empty warning list mean all required stages succeeded.

## 6:00–7:00 — Redis conversational memory

Use the same session but omit the entity, repository, and revision:

```bash
curl -sS -X POST http://localhost:8000/api/chat \
  -H 'content-type: application/json' \
  -d '{
    "message": "What depends on it?",
    "session_id": "walkthrough-memory"
  }' | jq
```

Say:

> Redis retained the bounded recent turns, active `APIRouter` entity, repository ID, and revision. The Orchestrator resolves “it” from that session and keeps the answer pinned to the same code version. Redis `WATCH` transactions prevent concurrent Orchestrator workers from silently overwriting turns. This is short-lived episodic memory, not permanent audit storage.

## 7:00–9:15 — Complex grounded queries

### Request lifecycle

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

Explain that bounded discovery expands the question into `FastAPI.__call__`, `APIRoute.get_route_handler`, `get_request_handler`, `solve_dependencies`, `run_endpoint_function`, and `serialize_response`. The final answer orders these facts chronologically and states the Starlette boundary instead of inventing unindexed runtime detail.

### Path versus Query

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

Point out the shared parameter interface and the concrete collaborator difference: `Path` constructs `params.Path`, while `Query` constructs `params.Query`.

Other verified broad categories are request validation, dependency injection, routing decorators, and conservative core-pattern analysis.

## 9:15–10:30 — Failure, retry, and fallback

Stop the Code Analyst:

```bash
sudo docker compose stop code-analyst
```

Use a reference-dependent follow-up such as “Show the implementation of it” in the session that already discussed `APIRouter`. This bypasses the shared response cache; a new session alone would not. With Analyst stopped, expect earlier successful Graph and Repository outputs to remain, an Analyst warning, and `partial: true`. Inspect the actual `agents_used` list rather than assuming only Graph ran.

Explain:

> A temporary failure reading a graph entity can be retried after a short delay. Repeating chat could append the same message twice, so the transport client does not blindly retry it. Once allowed retries run out, Orchestrator reports the failed task and keeps usable earlier results.

Restore the service:

```bash
sudo docker compose start code-analyst
curl -sS http://localhost:8000/api/agents/health | jq
```

## 10:30–11:30 — Security and correctness

Cover these points:

- Repository paths are normalized and restricted beneath the configured root.
- Source is parsed statically and never imported or executed.
- Source reads and public evidence excerpts are bounded.
- Custom Cypher rejects mutations, procedures, multiple statements, and unbounded output.
- Secrets come from configuration and must not enter logs or Git.
- Correlation IDs cross the public request boundary.
- The same correlation ID is propagated through Orchestrator and downstream MCP HTTP headers; structured JSON boundary logs show service/tool, attempt, duration, outcome, and normalized errors without recording prompts or source.
- Answers preserve graph IDs and revision-pinned file/line evidence.
- Timeouts, typed errors, partial responses, and health endpoints make failures observable.

## 11:30–12:15 — Tests

Run:

```bash
uv run ruff check .
uv run mypy
uv run pytest -q
```

The two live integration tests are opt-in because they require Redis/Neo4j infrastructure. Mention unit, contract, integration, process-level end-to-end, resilience, structured-output validation, invented-citation rejection, and deterministic LLM fallback coverage. Record the current test count from your final pre-submission run rather than memorizing a stale number.

Run the live golden regression set against the indexed FastAPI revision and show its per-case pass/fail table plus `evaluation-results/latest.json`. Explain that the gate checks agents, source evidence, partial status, trace IDs, and semantic concepts rather than brittle exact prose:

```bash
uv run python scripts/run_golden_evaluation.py \
  --revision 50113da16fec53b66b80d75e80a89296de4fa5a5
```

The verified September 4 baseline is 10/10 passing checks (nine answer cases plus the SSE contract) with 5,656 revision-filtered vectors in local Qdrant. The report also captures p50, p95, and maximum latency. Preserve it or capture the terminal summary for the submission demonstration.

The final measured golden run reported p50 `47.2 ms` and p95/max `2075.2 ms`; the semantic async/thread-pool query accounted for the outlier. The concurrent smoke run completed 10/10 requests with zero failures or admission rejections at concurrency 3, measured `26.781 requests/second`, p50 `108.385 ms`, and p95/max `117.098 ms`. State that these figures are local and cache-sensitive.

Use a failed semantic case to demonstrate the evaluation loop: inspect ranked evidence, distinguish retrieval from rendering failures, add bounded query expansion/reranking, and rerun only that case before executing the full suite.

## 12:15–13:00 — Honest limitations and roadmap

Say:

> The demo shows supported questions on this indexed revision. It does not prove every Python runtime path or production capacity. Jobs run inside the Indexer process, graph/source revision checks have limits, and authentication and full tracing are future work. Streaming currently chunks the finished answer. The next improvements are durable job recovery, stronger source isolation, access control, and measured scaling.

Finish with:

> The central design principle is evidence before fluency: specialized services retrieve graph structure and exact source first, and synthesis exposes what succeeded, what failed, and which revision supports every code claim.

## Optional API demonstrations

### Chat interface

Open <http://localhost:8000/>. Confirm the agent status shows ready, ask a dependency-injection question that requests a code example, and show the separate code card and copy action. Expand the source/graph evidence, point out the agent badges, and copy the trace ID. Filter the Compose logs by that ID to reconstruct the service path. Ask a follow-up in the same browser tab to demonstrate that the locally retained session ID reconnects the request to Redis conversation memory.

On a source evidence card, show the shortened revision SHA, captured/indexed timestamps, index job ID, immutable status, and controlled local snapshot path. Explain that revisions created before provenance support display a legacy label and need one reindex; the system does not invent missing historical timestamps.

### Server-Sent Events

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

Expected event types are `status`, `evidence`, `token`, optional `warning`, and `done`.

### OpenAPI

Open <http://localhost:8000/docs> and show the request/response models generated from the Pydantic contracts.

### Durable completed-job status

Retrieve a completed job, restart the Indexer, and retrieve the same ID:

```bash
curl -sS http://localhost:8000/api/index/status/YOUR_JOB_ID | jq
sudo docker compose restart indexer
curl -sS http://localhost:8000/api/index/status/YOUR_JOB_ID | jq
```

The status, counters, revision, and timestamps should remain unchanged.

## Submission checklist

- [ ] `.env` and all credentials are excluded from Git.
- [ ] `docker compose up -d --build` succeeds from a clean checkout.
- [ ] All containers become healthy.
- [ ] The official FastAPI revision is available under the configured source root.
- [ ] Index status and graph statistics are captured.
- [ ] Simple, multi-agent, lifecycle, comparison, memory, and failure examples are captured.
- [ ] Ruff, Mypy, unit tests, and opted-in integration tests are recorded.
- [ ] `README.md`, `DESIGN.md`, and `WALKTHROUGH.md` are included.
- [ ] Known limitations are stated accurately.
- [ ] The private repository grants the evaluator access.
