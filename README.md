# Interview Preparation Pack

Use these guides to follow concrete questions and source examples through the system. Start with the [architecture overview](01_ARCHITECTURE_OVERVIEW.md), then read the component you want to understand. The [architect deep dive](000_ARCHITECT_LEVEL_DEEP_DIVE.md) explains trade-offs after those basics.

Explanations use a simple order: example → what the code does → why it does it → current limitation. Small `create_item`/`validate_item` examples are illustrative; recorded measurements are labeled historical. **Future** means proposed work, not a current feature.

Use [00_REAL_ARTIFACTS.md](00_REAL_ARTIFACTS.md) to see representative payloads and how to read their fields. Finish with [17_ADVANCED_INTERVIEW_QA.md](17_ADVANCED_INTERVIEW_QA.md) to practice explaining decisions through examples. For async/concurrency interviews, read [18_ASYNC_CONCURRENCY.md](18_ASYNC_CONCURRENCY.md). It explains where the project uses fan-out, where it stays sequential, and why those choices are intentional. [Assignment.md](../Assignment.md) is the original requirements document; these guides explain what is implemented and where it differs.

## Study map

| File | Component |
|---|---|
| `00_REAL_ARTIFACTS.md` | Actual contracts, payloads, storage shapes, and evidence |
| `01_ARCHITECTURE_OVERVIEW.md` | Complete topology and responsibilities |
| `02_GATEWAY_UI.md` | FastAPI Gateway, UI, REST/SSE/WebSocket |
| `03_MCP_COMMUNICATION.md` | MCP SDK, clients, contracts, transport |
| `04_ORCHESTRATOR.md` | Routing, fan-out, synthesis, memory |
| `05_INDEXER.md` | AST indexing, revisions, reconciliation |
| `06_GRAPH_AGENT.md` | Neo4j queries and hybrid retrieval |
| `07_REPOSITORY_AGENT.md` | Safe Git and source provenance |
| `08_CODE_ANALYST.md` | Static source reasoning |
| `09_NEO4J.md` | Knowledge graph schema |
| `10_SEMANTIC_SEARCH.md` | Qdrant, FastEmbed, hybrid ranking |
| `11_REDIS_MEMORY.md` | Sessions, cache, jobs, locks, episodes |
| `12_LLM_SYNTHESIS.md` | Groq and evidence-constrained generation |
| `13_OBSERVABILITY.md` | Correlation logging and tracing |
| `14_DEPLOYMENT_SECURITY.md` | Compose, configuration, security, scaling |
| `15_TESTING_EVALUATION.md` | Tests, golden set, load smoke |
| `16_END_TO_END_FLOW.md` | Complete worked request walkthrough |
| `17_ADVANCED_INTERVIEW_QA.md` | Advanced current-state interview questions |
| `18_ASYNC_CONCURRENCY.md` | Async, concurrency, parallel, and sequential workflow design |
| `19_AGENTIC_AI_ENGINEER_INTERVIEW_QUESTIONS.md` | Mid-senior Agentic AI Engineer interview questions |
| `000_ARCHITECT_LEVEL_DEEP_DIVE.md` | Full AI-Architect-level component deep dive |

For **detailed responsibilities and lifecycle interview questions**, read guides `02`–`15`. The five agent guides (`04`–`08`) explain startup, inputs/tools, operation steps, handoffs, state ownership, and failure/restart behavior. The [architecture ownership map](01_ARCHITECTURE_OVERVIEW.md) links every component, and the [end-to-end flow](16_END_TO_END_FLOW.md) connects preparation, chat, and presentation lifecycles.

For cross-cutting answers, also use the master [INTERVIEW_PREPARATION.md](INTERVIEW_PREPARATION.md), the design [DESIGN.md](DESIGN.md), and [OPERATIONS.md](OPERATIONS.md).
