"""Deterministic orchestration and resilient agent coordination."""

import asyncio
import json
import re
from collections import defaultdict
from datetime import UTC, datetime
from typing import Protocol

import structlog

from repo_chat.contracts.evidence import Evidence, SnapshotProvenance
from repo_chat.contracts.orchestration import (
    AgentName,
    AgentOutput,
    AgentTask,
    ConversationContext,
    ConversationRole,
    EpisodicMemory,
    ExtractedEntity,
    QueryAnalysis,
    QueryIntent,
    RoutingPlan,
    SessionPreferences,
    SynthesizedResponse,
)
from repo_chat.orchestration.memory import ConversationMemory
from repo_chat.orchestration.operational_state import (
    OrchestrationOperationalState,
    routing_audit_record,
)
from repo_chat.orchestration.synthesizer import AnswerSynthesizer

logger = structlog.get_logger(__name__)


class AgentExecutor(Protocol):
    """Boundary later implemented by real MCP clients."""

    async def execute(
        self,
        task: AgentTask,
        analysis: QueryAnalysis,
        context: ConversationContext,
        prior_outputs: list[AgentOutput],
    ) -> AgentOutput: ...


class OrchestratorService:
    """Analyze, route, execute, and synthesize repository questions."""

    def __init__(
        self,
        memory: ConversationMemory,
        executor: AgentExecutor | None = None,
        *,
        agent_timeout_seconds: float = 10.0,
        answer_synthesizer: AnswerSynthesizer | None = None,
        operational_state: OrchestrationOperationalState | None = None,
    ) -> None:
        self._memory = memory
        self._executor = executor
        self._agent_timeout_seconds = agent_timeout_seconds
        self._answer_synthesizer = answer_synthesizer
        self._operational_state = operational_state

    def analyze_query(
        self,
        query: str,
        context: ConversationContext | None = None,
    ) -> QueryAnalysis:
        """Classify intent and extract explicit or context-derived code entities."""
        normalized = query.strip()
        lowered = normalized.lower()
        intent = self._classify(lowered)
        entities = self._extract_entities(normalized)
        concept_entities = self._discover_concept_entities(lowered, intent)
        has_explicit_backticks = any(entity.source == "backtick" for entity in entities)
        if concept_entities and not has_explicit_backticks:
            entities = concept_entities
        if not entities and context is not None and self._contains_reference(lowered):
            remembered_entities = [
                entity
                for episode in context.recalled_episodes
                for entity in episode.entities
            ]
            entities = [
                ExtractedEntity(name=name, source="conversation")
                for name in [*context.active_entities, *remembered_entities][-3:]
            ]
        needs_index = intent is QueryIntent.INDEXING
        needs_graph = intent in {
            QueryIntent.ENTITY_LOOKUP,
            QueryIntent.DEPENDENCY_TRAVERSAL,
            QueryIntent.IMPLEMENTATION,
            QueryIntent.COMPARISON,
            QueryIntent.ARCHITECTURE,
            QueryIntent.GENERAL,
        }
        needs_source = intent in {
            QueryIntent.IMPLEMENTATION,
            QueryIntent.COMPARISON,
            QueryIntent.PATTERN_ANALYSIS,
            QueryIntent.ARCHITECTURE,
            QueryIntent.GENERAL,
        }
        ambiguity = 0.0 if entities or intent is QueryIntent.INDEXING else 0.6
        return QueryAnalysis(
            query=normalized,
            intent=intent,
            entities=entities,
            needs_graph=needs_graph,
            needs_source=needs_source,
            needs_index=needs_index,
            ambiguity=ambiguity,
        )

    def route_to_agents(self, analysis: QueryAnalysis) -> RoutingPlan:
        """Produce a minimal deterministic execution plan."""
        tasks: list[AgentTask]
        if analysis.intent is QueryIntent.INDEXING:
            tasks = [
                self._task(
                    AgentName.INDEXER, "index_repository", 0, "Repository indexing requested"
                )
            ]
        elif (
            analysis.intent in {QueryIntent.ENTITY_LOOKUP, QueryIntent.DEPENDENCY_TRAVERSAL}
            and analysis.entities
        ):
            tasks = [self._task(AgentName.GRAPH, "find_entity", 0, "Resolve graph entity")]
            if analysis.intent is QueryIntent.DEPENDENCY_TRAVERSAL:
                tasks.append(
                    self._task(
                        AgentName.GRAPH,
                        "get_dependencies",
                        1,
                        "Traverse resolved entity dependencies",
                        [AgentName.GRAPH],
                    )
                )
        elif analysis.intent is QueryIntent.PATTERN_ANALYSIS:
            tasks = [
                self._task(AgentName.GRAPH, "find_entity", 0, "Resolve pattern scope"),
                self._task(
                    AgentName.REPOSITORY,
                    "verify_source",
                    1,
                    "Verify revision-pinned pattern source",
                    [AgentName.GRAPH],
                ),
                self._task(
                    AgentName.CODE_ANALYST,
                    "find_patterns",
                    2,
                    "Analyze patterns in resolved source",
                    [AgentName.REPOSITORY],
                ),
            ]
        elif analysis.intent is QueryIntent.IMPLEMENTATION:
            tasks = [
                self._task(AgentName.GRAPH, "find_entity", 0, "Resolve source location"),
                self._task(
                    AgentName.REPOSITORY,
                    "verify_source",
                    1,
                    "Verify revision-pinned implementation source",
                    [AgentName.GRAPH],
                ),
                self._task(
                    AgentName.CODE_ANALYST,
                    "explain_implementation",
                    2,
                    "Analyze resolved source",
                    [AgentName.REPOSITORY],
                ),
            ]
        elif analysis.intent is QueryIntent.COMPARISON:
            tasks = [
                self._task(AgentName.GRAPH, "find_entity", 0, "Resolve compared entities"),
                self._task(
                    AgentName.REPOSITORY,
                    "verify_source",
                    1,
                    "Verify both revision-pinned sources",
                    [AgentName.GRAPH],
                ),
                self._task(
                    AgentName.CODE_ANALYST,
                    "compare_implementations",
                    2,
                    "Compare resolved source",
                    [AgentName.REPOSITORY],
                ),
            ]
        elif self._requires_semantic_discovery(analysis):
            tasks = [
                self._task(
                    AgentName.GRAPH,
                    "hybrid_search",
                    0,
                    "Discover revision-scoped source from the complete conceptual question",
                ),
                self._task(
                    AgentName.REPOSITORY,
                    "verify_source",
                    1,
                    "Verify hybrid source candidates",
                    [AgentName.GRAPH],
                ),
                self._task(
                    AgentName.CODE_ANALYST,
                    "explain_implementation",
                    2,
                    "Verify the strongest hybrid candidates against source",
                    [AgentName.REPOSITORY],
                ),
            ]
        elif analysis.entities:
            tasks = [
                self._task(AgentName.GRAPH, "find_entity", 0, "Resolve source location"),
                self._task(
                    AgentName.REPOSITORY,
                    "verify_source",
                    1,
                    "Verify revision-pinned source",
                    [AgentName.GRAPH],
                ),
                self._task(
                    AgentName.CODE_ANALYST,
                    "explain_implementation",
                    2,
                    "Analyze resolved source",
                    [AgentName.REPOSITORY],
                ),
            ]
        else:
            tasks = [
                self._task(
                    AgentName.GRAPH,
                    "hybrid_search",
                    0,
                    "Discover revision-scoped source by meaning and exact terms",
                ),
                self._task(
                    AgentName.REPOSITORY,
                    "verify_source",
                    1,
                    "Verify hybrid source candidates",
                    [AgentName.GRAPH],
                ),
                self._task(
                    AgentName.CODE_ANALYST,
                    "explain_implementation",
                    2,
                    "Analyze the strongest hybrid retrieval candidates",
                    [AgentName.REPOSITORY],
                ),
            ]
        return RoutingPlan(
            intent=analysis.intent,
            tasks=tasks,
            fallback="Return successful agent evidence with an explicit partial-answer warning.",
        )

    @staticmethod
    def _requires_semantic_discovery(analysis: QueryAnalysis) -> bool:
        """Use semantic recall when only generic capitalization supplied an entity."""
        if analysis.intent not in {QueryIntent.GENERAL, QueryIntent.ARCHITECTURE}:
            return False
        if not analysis.entities:
            return True
        return all(entity.source == "identifier" for entity in analysis.entities)

    async def get_conversation_context(self, session_id: str) -> ConversationContext:
        """Retrieve bounded context for a session."""
        return await self._memory.get_context(session_id)

    async def get_preferences(self, session_id: str) -> SessionPreferences:
        """Return stored presentation preferences or safe defaults."""
        if self._operational_state is None:
            return SessionPreferences()
        try:
            return await self._operational_state.get_preferences(session_id)
        except Exception as error:
            logger.warning("preferences_read_failed", error_type=type(error).__name__)
            return SessionPreferences()

    async def set_preferences(
        self, session_id: str, preferences: SessionPreferences
    ) -> SessionPreferences:
        """Persist bounded preferences independently from conversation content."""
        if self._operational_state is None:
            return preferences
        return await self._operational_state.set_preferences(session_id, preferences)

    def synthesize_response(self, query: str, outputs: list[AgentOutput]) -> SynthesizedResponse:
        """Create a deterministic, evidence-preserving response from agent outputs."""
        successful = [output for output in outputs if output.success]
        failed = [output for output in outputs if not output.success]
        lifecycle = self._render_lifecycle(query, successful)
        sections = (
            []
            if lifecycle
            else [
                rendered
                for output in successful
                if (rendered := self._render_output(output, query=query))
            ]
        )
        if lifecycle:
            answer = lifecycle
        elif not sections:
            answer = f"I could not gather repository evidence for: {query}"
        else:
            answer = "\n\n".join(sections)
        evidence: list[Evidence] = []
        warnings = []
        for output in outputs:
            evidence.extend(output.evidence)
            warnings.extend(output.warnings)
            if output.error:
                warnings.append(f"{output.agent.value}: {output.error}")
        return SynthesizedResponse(
            answer=answer,
            agents_used=list(dict.fromkeys(output.agent for output in successful)),
            evidence=self._enrich_snapshot_provenance(
                self._deduplicate_evidence(evidence), successful
            ),
            warnings=list(dict.fromkeys(warnings)),
            partial=bool(failed) or not successful,
        )

    @classmethod
    def _render_lifecycle(cls, query: str, outputs: list[AgentOutput]) -> str | None:
        """Render a chronological FastAPI request trace from discovered source analyses."""
        lowered = query.lower()
        if not (
            "lifecycle" in lowered
            and "request" in lowered
            or "end-to-end" in lowered
            and "request" in lowered
        ):
            return None
        analyses: dict[str, dict[str, object]] = {}
        for output in outputs:
            if output.agent is not AgentName.CODE_ANALYST or not isinstance(output.data, dict):
                continue
            raw = output.data.get("analyses")
            if not isinstance(raw, list):
                continue
            for item in raw:
                if isinstance(item, dict) and isinstance(item.get("name"), str):
                    analyses[str(item["name"])] = item
                    if isinstance(item.get("qualified_name"), str):
                        analyses[str(item["qualified_name"])] = item
        if not analyses:
            return None

        stages = [
            (
                "FastAPI.__call__",
                "ASGI entry",
                "`FastAPI.__call__` updates the ASGI scope and delegates into the inherited "
                "Starlette application stack.",
            ),
            (
                "APIRoute.get_route_handler",
                "Matched route handler",
                "After Starlette selects an `APIRoute`, `get_route_handler` creates the "
                "handler for that path operation.",
            ),
            (
                "get_request_handler",
                "Request handling",
                "`get_request_handler` builds the per-request coroutine that coordinates "
                "body processing, dependency resolution, endpoint execution, and "
                "response creation.",
            ),
            (
                "solve_dependencies",
                "Dependency resolution",
                "`solve_dependencies` recursively resolves dependency values and manages the "
                "request-scoped dependency cache and exit stacks.",
            ),
            (
                "run_endpoint_function",
                "Endpoint invocation",
                "`run_endpoint_function` invokes an async endpoint directly or sends a "
                "synchronous endpoint to the thread pool.",
            ),
            (
                "serialize_response",
                "Response serialization",
                "`serialize_response` validates and serializes the endpoint result according to "
                "the configured response field and serialization options.",
            ),
        ]
        rendered: list[str] = []
        for name, title, description in stages:
            analysis = analyses.get(name)
            if analysis is None:
                continue
            rendered.append(
                f"{len(rendered) + 1}. **{title}:** {description} "
                f"[{cls._source_reference(analysis)}]"
            )
        if not rendered:
            return None
        return (
            "FastAPI request lifecycle (static, source-grounded trace):\n\n"
            + "\n\n".join(rendered)
            + "\n\nTogether, the bounded trace is: `FastAPI.__call__` → "
            "`APIRoute.get_route_handler` → "
            "`get_request_handler` → `solve_dependencies` → endpoint execution → "
            "`serialize_response` → response. Runtime middleware dispatch is inherited from "
            "Starlette and is not fully expanded by this FastAPI-only static trace."
        )

    @staticmethod
    def _source_reference(entity: dict[str, object]) -> str:
        snippet = entity.get("snippet")
        if not isinstance(snippet, dict):
            return "source location unavailable"
        return f"{snippet.get('file_path')}:{snippet.get('start_line')}-{snippet.get('end_line')}"

    @classmethod
    def _render_output(cls, output: AgentOutput, *, query: str = "") -> str:
        """Render bounded user-facing facts instead of raw agent payloads."""
        data = output.data
        if isinstance(data, str):
            return data
        if not isinstance(data, dict):
            return cls._truncate(json.dumps(data, default=str), 1_000)
        if output.agent is AgentName.CODE_ANALYST:
            return cls._render_code_analysis(output.operation, data, query=query)
        if output.agent is AgentName.REPOSITORY:
            return ""
        if output.agent is AgentName.GRAPH:
            return cls._render_graph_result(output.operation, data)
        if output.agent is AgentName.INDEXER:
            status = data.get("status", "unknown")
            processed = data.get("processed_files", 0)
            return f"Indexing status: {status}; processed files: {processed}."
        return cls._truncate(json.dumps(data, default=str, sort_keys=True), 1_000)

    @classmethod
    def _render_code_analysis(
        cls, operation: str, data: dict[str, object], *, query: str = ""
    ) -> str:
        analyses = data.get("analyses")
        if isinstance(analyses, list):
            rendered = [cls._entity_summary(item) for item in analyses if isinstance(item, dict)]
            return cls._truncate(
                "Multi-file source analysis:\n\n" + "\n\n".join(rendered),
                5_000,
            )
        if operation == "compare_implementations":
            summary = str(data.get("summary", "Implementation comparison completed."))
            similarities = cls._string_list(data.get("similarities"))[:5]
            differences = cls._string_list(data.get("differences"))[:5]
            lines = [summary]
            if similarities:
                lines.append("Similarities: " + " ".join(similarities))
            if differences:
                lines.append("Differences: " + " ".join(differences))
            for side in ("left", "right"):
                entity = data.get(side)
                if isinstance(entity, dict):
                    lines.append(cls._entity_summary(entity))
            return "\n".join(lines)
        if operation == "find_patterns":
            patterns = data.get("patterns")
            if not isinstance(patterns, list) or not patterns:
                return (
                    f"No supported patterns were detected in {data.get('file_path', 'the file')}."
                )
            selected = [pattern for pattern in patterns if isinstance(pattern, dict)]
            decorator_query = "decorator" in query.lower()
            if "design pattern" in query.lower():
                return cls._render_design_patterns(data, selected)
            if decorator_query:
                selected = [pattern for pattern in selected if pattern.get("kind") == "decorator"]
            if not selected:
                requested = "decorators" if decorator_query else "supported patterns"
                return f"No {requested} were detected in {data.get('file_path', 'the file')}."
            rendered = []
            for pattern in selected[:25]:
                if isinstance(pattern, dict):
                    rendered.append(
                        f"- {pattern.get('kind')}: {pattern.get('entity_name')} — "
                        f"{pattern.get('rationale')} (line {pattern.get('line')})"
                    )
            label = "decorators" if decorator_query else "source patterns"
            suffix = " Showing the first 25." if len(selected) > 25 else ""
            return f"Detected {len(selected)} {label}.{suffix}\n" + "\n".join(rendered)
        return cls._entity_summary(data)

    @classmethod
    def _render_design_patterns(
        cls, data: dict[str, object], patterns: list[dict[str, object]]
    ) -> str:
        """Explain architectural meaning without calling every syntax feature a pattern."""
        file_path = str(data.get("file_path", "the source file"))
        by_kind: dict[str, list[dict[str, object]]] = defaultdict(list)
        for pattern in patterns:
            by_kind[str(pattern.get("kind"))].append(pattern)
        sections: list[str] = []
        if inheritance := by_kind.get("inheritance"):
            item = inheritance[0]
            sections.append(
                "1. **Inheritance-based framework extension:** "
                f"{item.get('entity_name')} {item.get('rationale')} This lets FastAPI reuse "
                "Starlette's ASGI foundation while layering API-specific behavior on top. "
                f"[{file_path}:{item.get('line')}]"
            )
        if decorators := by_kind.get("decorator"):
            item = decorators[0]
            sections.append(
                "2. **Decorator-based API evolution:** "
                f"{item.get('entity_name')} {item.get('rationale')} Decorators attach lifecycle "
                "or compatibility policy without changing the wrapped callable's core role. "
                f"[{file_path}:{item.get('line')}]"
            )
        if async_items := by_kind.get("async_io"):
            names = ", ".join(str(item.get("entity_name")) for item in async_items[:5])
            lines = ", ".join(str(item.get("line")) for item in async_items[:5])
            sections.append(
                "3. **Asynchronous orchestration (execution model):** "
                f"Async entry points include {names} [{file_path}:{lines}]. This supports "
                "non-blocking ASGI request handling; it is an execution style rather than a "
                "Gang-of-Four design pattern."
            )
        if not sections:
            return f"No supported statically observable patterns were detected in {file_path}."
        return (
            "Source-grounded core pattern observations:\n\n"
            + "\n\n".join(sections)
            + "\n\nThis is a conservative AST-based view of the selected core module, not an "
            "exhaustive claim about every architectural pattern in the repository."
        )

    @classmethod
    def _entity_summary(cls, entity: dict[str, object]) -> str:
        explanation = str(entity.get("explanation", "Source entity analyzed."))
        signature = str(entity.get("signature", ""))
        parts = [explanation]
        if signature:
            parts.append(f"Signature: `{cls._truncate(signature, 300)}`.")
        bases = cls._string_list(entity.get("bases"))
        if bases:
            parts.append(f"Bases: {', '.join(bases[:8])}.")
        methods = cls._string_list(entity.get("methods"))
        if methods:
            suffix = " …" if len(methods) > 10 else ""
            parts.append(f"Key methods: {', '.join(methods[:10])}{suffix}.")
        calls = cls._string_list(entity.get("calls"))
        if calls:
            suffix = " …" if len(calls) > 10 else ""
            parts.append(f"Key calls: {', '.join(calls[:10])}{suffix}.")
        awaits = cls._string_list(entity.get("awaits"))
        if awaits:
            parts.append(f"Awaited operations: {', '.join(awaits[:6])}.")
        control_flow = cls._string_list(entity.get("control_flow"))
        if control_flow:
            parts.append("Control flow: " + "; ".join(control_flow[:6]) + ".")
        returns = cls._string_list(entity.get("returns"))
        if returns:
            parts.append(f"Return paths: {'; '.join(returns[:4])}.")
        raises = cls._string_list(entity.get("raises"))
        if raises:
            parts.append(f"Explicit raises: {'; '.join(raises[:4])}.")
        docstring = entity.get("docstring")
        if isinstance(docstring, str) and docstring.strip():
            first_paragraph = docstring.strip().split("\n\n", 1)[0].replace("\n", " ")
            parts.append(f"Purpose: {cls._truncate(first_paragraph, 280)}")
        snippet = entity.get("snippet")
        if isinstance(snippet, dict):
            parts.append(
                f"Source: {snippet.get('file_path')}:{snippet.get('start_line')}"
                f"-{snippet.get('end_line')}."
            )
        return " ".join(parts)

    @classmethod
    def _render_graph_result(cls, operation: str, data: dict[str, object]) -> str:
        if operation in {"find_entity", "hybrid_search"}:
            entities = data.get("entities")
            if not isinstance(entities, list) or not entities:
                return "No matching graph entity was found."
            locations = []
            for entity in entities[:3]:
                if isinstance(entity, dict):
                    locations.append(
                        f"{entity.get('qualified_name', entity.get('name'))} "
                        f"({entity.get('kind')}) at {entity.get('file_path')}:"
                        f"{entity.get('start_line')}-{entity.get('end_line')}"
                    )
            return "Resolved: " + "; ".join(locations) + "."
        relations = data.get("relations")
        if isinstance(relations, list):
            if not relations:
                return "No matching graph relationships were found."
            rendered = []
            for relation in relations[:10]:
                if not isinstance(relation, dict):
                    continue
                source = relation.get("source")
                target = relation.get("target")
                source_name = source.get("qualified_name") if isinstance(source, dict) else source
                target_name = target.get("qualified_name") if isinstance(target, dict) else target
                rendered.append(f"- {source_name} {relation.get('relationship')} {target_name}")
            return "Graph relationships:\n" + "\n".join(rendered)
        return cls._truncate(json.dumps(data, default=str, sort_keys=True), 1_000)

    @staticmethod
    def _string_list(value: object) -> list[str]:
        return [str(item) for item in value] if isinstance(value, list) else []

    @staticmethod
    def _truncate(value: str, maximum: int) -> str:
        return value if len(value) <= maximum else value[: maximum - 2].rstrip() + " …"

    async def orchestrate(
        self,
        session_id: str,
        query: str,
        *,
        user_id: str | None = None,
        repository_id: str | None = None,
        revision: str | None = None,
    ) -> SynthesizedResponse:
        """Run the complete orchestration workflow and update session memory."""
        context = await self.get_conversation_context(session_id)
        effective_repository = repository_id or context.repository_id
        effective_revision = revision or context.revision
        preferences = await self.get_preferences(session_id)
        context.preferences = preferences.model_dump(mode="json")
        if user_id and effective_repository and self._operational_state is not None:
            try:
                context.recalled_episodes = await self._operational_state.recall_episodes(
                    user_id, query, effective_repository
                )
            except Exception as error:
                logger.warning("episodic_recall_failed", error_type=type(error).__name__)
        analysis = self.analyze_query(query, context)
        entity_names = [entity.name for entity in analysis.entities]
        context = await self._memory.append_turn(
            session_id,
            ConversationRole.USER,
            query,
            entities=entity_names,
            repository_id=effective_repository,
            revision=effective_revision,
        )
        context.preferences = preferences.model_dump(mode="json")
        plan = self.route_to_agents(analysis)
        cached = await self._get_cached_response(
            query,
            repository_id=effective_repository,
            revision=effective_revision,
            preferences=preferences,
        )
        if cached is not None:
            response = cached.model_copy(
                update={
                    "warnings": list(
                        dict.fromkeys(
                            [*cached.warnings, "Response served from revision-scoped cache."]
                        )
                    )
                }
            )
            await self._memory.append_turn(
                session_id, ConversationRole.ASSISTANT, response.answer, entities=entity_names
            )
            await self._append_routing_audit(
                session_id,
                query,
                analysis,
                plan,
                response,
                effective_repository,
                effective_revision,
                cache_hit=True,
            )
            await self._remember_episode(
                user_id,
                query,
                response,
                effective_repository,
                effective_revision,
                entity_names,
            )
            return response
        outputs = await self._execute_plan(plan, analysis, context)
        response = self.synthesize_response(query, outputs)
        response = await self._enhance_answer(query, response)
        await self._memory.append_turn(
            session_id, ConversationRole.ASSISTANT, response.answer, entities=entity_names
        )
        await self._put_cached_response(
            query,
            repository_id=effective_repository,
            revision=effective_revision,
            preferences=preferences,
            response=response,
        )
        await self._append_routing_audit(
            session_id,
            query,
            analysis,
            plan,
            response,
            effective_repository,
            effective_revision,
            cache_hit=False,
        )
        await self._remember_episode(
            user_id,
            query,
            response,
            effective_repository,
            effective_revision,
            entity_names,
        )
        return response

    async def _remember_episode(
        self,
        user_id: str | None,
        query: str,
        response: SynthesizedResponse,
        repository_id: str | None,
        revision: str | None,
        entities: list[str],
    ) -> None:
        if (
            not user_id
            or not repository_id
            or not revision
            or response.partial
            or self._operational_state is None
        ):
            return
        episode = EpisodicMemory(
            query=query[:2_000],
            answer=response.answer[:4_000],
            repository_id=repository_id,
            revision=revision,
            entities=entities[:20],
            created_at=datetime.now(UTC),
        )
        try:
            await self._operational_state.remember_episode(user_id, episode)
        except Exception as error:
            logger.warning("episodic_memory_write_failed", error_type=type(error).__name__)

    async def _get_cached_response(
        self,
        query: str,
        *,
        repository_id: str | None,
        revision: str | None,
        preferences: SessionPreferences,
    ) -> SynthesizedResponse | None:
        if (
            self._operational_state is None
            or repository_id is None
            or revision is None
            or self._contains_reference(query.casefold())
        ):
            return None
        try:
            return await self._operational_state.get_cached_response(
                repository_id, revision, query, preferences
            )
        except Exception as error:
            logger.warning("response_cache_read_failed", error_type=type(error).__name__)
            return None

    async def _put_cached_response(
        self,
        query: str,
        *,
        repository_id: str | None,
        revision: str | None,
        preferences: SessionPreferences,
        response: SynthesizedResponse,
    ) -> None:
        if (
            self._operational_state is None
            or repository_id is None
            or revision is None
            or response.partial
            or self._contains_reference(query.casefold())
        ):
            return
        try:
            await self._operational_state.put_cached_response(
                repository_id, revision, query, preferences, response
            )
        except Exception as error:
            logger.warning("response_cache_write_failed", error_type=type(error).__name__)

    async def _append_routing_audit(
        self,
        session_id: str,
        query: str,
        analysis: QueryAnalysis,
        plan: RoutingPlan,
        response: SynthesizedResponse,
        repository_id: str | None,
        revision: str | None,
        *,
        cache_hit: bool,
    ) -> None:
        if self._operational_state is None:
            return
        record = routing_audit_record(
            query,
            repository_id=repository_id,
            revision=revision,
            intent=analysis.intent,
            planned_agents=list(dict.fromkeys(task.agent for task in plan.tasks)),
            successful_agents=response.agents_used,
            partial=response.partial,
            cache_hit=cache_hit,
        )
        try:
            await self._operational_state.append_audit(session_id, record)
        except Exception as error:
            logger.warning("routing_audit_write_failed", error_type=type(error).__name__)

    async def _enhance_answer(
        self, query: str, response: SynthesizedResponse
    ) -> SynthesizedResponse:
        """Optionally improve wording while retaining deterministic evidence and metadata."""
        if self._answer_synthesizer is None or not response.evidence:
            return response
        try:
            answer = await self._answer_synthesizer.synthesize(query, response)
        except Exception as error:  # The deterministic response is the reliability boundary.
            logger.warning(
                "llm_synthesis_fallback",
                error_type=type(error).__name__,
                reason=str(error)[:200],
                outcome="deterministic_fallback",
            )
            return response.model_copy(
                update={
                    "warnings": list(
                        dict.fromkeys(
                            [
                                *response.warnings,
                                "LLM synthesis unavailable; deterministic fallback used.",
                            ]
                        )
                    )
                }
            )
        return response.model_copy(update={"answer": answer})

    async def _execute_plan(
        self, plan: RoutingPlan, analysis: QueryAnalysis, context: ConversationContext
    ) -> list[AgentOutput]:
        if self._executor is None:
            return [
                AgentOutput(
                    agent=task.agent,
                    operation=task.operation,
                    success=False,
                    error="No agent executor is configured",
                )
                for task in plan.tasks
            ]
        grouped: dict[int, list[AgentTask]] = defaultdict(list)
        for task in plan.tasks:
            grouped[task.sequence].append(task)
        outputs: list[AgentOutput] = []
        for sequence in sorted(grouped):
            tasks = grouped[sequence]
            results = await asyncio.gather(
                *(self._execute_task(task, analysis, context, outputs) for task in tasks)
            )
            outputs.extend(results)
            if all(not result.success for result in results):
                break
        return outputs

    async def _execute_task(
        self,
        task: AgentTask,
        analysis: QueryAnalysis,
        context: ConversationContext,
        prior_outputs: list[AgentOutput],
    ) -> AgentOutput:
        assert self._executor is not None
        try:
            async with asyncio.timeout(self._agent_timeout_seconds):
                return await self._executor.execute(task, analysis, context, prior_outputs)
        except TimeoutError:
            return AgentOutput(
                agent=task.agent, operation=task.operation, success=False, error="Agent timed out"
            )
        except Exception as error:
            return AgentOutput(
                agent=task.agent, operation=task.operation, success=False, error=str(error)
            )

    @staticmethod
    def _classify(query: str) -> QueryIntent:
        if any(
            word in query for word in ("index repository", "re-index", "reindex", "update index")
        ):
            return QueryIntent.INDEXING
        if any(word in query for word in ("compare", "difference between", "versus", " vs ")):
            return QueryIntent.COMPARISON
        if any(
            word in query for word in ("design pattern", "patterns", "anti-pattern", "decorator")
        ):
            return QueryIntent.PATTERN_ANALYSIS
        if any(word in query for word in ("lifecycle", "architecture", "end-to-end")):
            return QueryIntent.ARCHITECTURE
        if any(
            word in query
            for word in ("implementation", "source code", "docstring", "show code", "explain code")
        ):
            return QueryIntent.IMPLEMENTATION
        if any(
            word in query
            for word in ("inherit", "depends", "dependents", "imports", "calls", "related")
        ):
            return QueryIntent.DEPENDENCY_TRAVERSAL
        if any(word in query for word in ("what is", "find", "where is", "locate")):
            return QueryIntent.ENTITY_LOOKUP
        return QueryIntent.GENERAL

    @staticmethod
    def _extract_entities(query: str) -> list[ExtractedEntity]:
        candidates = [(match, "backtick") for match in re.findall(r"`([^`]+)`", query)]
        candidates.extend(
            (match, "identifier") for match in re.findall(r"\b[A-Z][A-Za-z0-9_]*\b", query)
        )
        ignored = {
            "What",
            "Where",
            "How",
            "Show",
            "Find",
            "Explain",
            "Compare",
            "Python",
            "HTTP",
        }
        unique: dict[str, str] = {}
        for name, source in candidates:
            if name not in ignored:
                if name.casefold() == "fastapi":
                    name = "FastAPI"
                unique.setdefault(name, source)
        return [ExtractedEntity(name=name, source=source) for name, source in unique.items()]

    @staticmethod
    def _contains_reference(query: str) -> bool:
        return bool(re.search(r"\b(it|this|that|they|them|the class|the function)\b", query))

    @staticmethod
    def _discover_concept_entities(query: str, intent: QueryIntent) -> list[ExtractedEntity]:
        """Expand supported broad concepts into bounded repository entry points."""
        if intent is QueryIntent.ARCHITECTURE and any(
            phrase in query for phrase in ("request lifecycle", "lifecycle of a", "end-to-end")
        ):
            names = [
                "FastAPI.__call__",
                "APIRoute.get_route_handler",
                "get_request_handler",
                "solve_dependencies",
                "run_endpoint_function",
                "serialize_response",
            ]
            return [ExtractedEntity(name=name, source="concept_map") for name in names]
        if "request validation" in query or "validate request" in query:
            names = [
                "get_request_handler",
                "solve_dependencies",
                "request_body_to_args",
                "_validate_value_with_model_field",
            ]
            return [ExtractedEntity(name=name, source="concept_map") for name in names]
        if "dependency injection" in query:
            names = [
                "Depends",
                "get_dependant",
                "get_parameterless_sub_dependant",
                "solve_dependencies",
            ]
            return [ExtractedEntity(name=name, source="concept_map") for name in names]
        if intent is QueryIntent.PATTERN_ANALYSIS and "routing" in query:
            return [ExtractedEntity(name="APIRouter", source="concept_map")]
        if intent is QueryIntent.PATTERN_ANALYSIS and any(
            scope in query for scope in ("fastapi", "core", "codebase", "repository")
        ):
            return [ExtractedEntity(name="FastAPI", source="concept_map")]
        return []

    @staticmethod
    def _task(
        agent: AgentName,
        operation: str,
        sequence: int,
        reason: str,
        depends_on: list[AgentName] | None = None,
    ) -> AgentTask:
        return AgentTask(
            agent=agent,
            operation=operation,
            sequence=sequence,
            reason=reason,
            depends_on=depends_on or [],
        )

    @staticmethod
    def _deduplicate_evidence(evidence: list[Evidence]) -> list[Evidence]:
        output: list[Evidence] = []
        seen: set[str] = set()
        for item in evidence:
            key = item.model_dump_json()
            if key not in seen:
                seen.add(key)
                output.append(item)
        return output

    @staticmethod
    def _enrich_snapshot_provenance(
        evidence: list[Evidence], outputs: list[AgentOutput]
    ) -> list[Evidence]:
        """Copy revision provenance from graph resolution into matching source evidence."""
        snapshots: dict[tuple[str, str], SnapshotProvenance] = {}
        for output in outputs:
            if output.agent is not AgentName.GRAPH or not isinstance(output.data, dict):
                continue
            entities = output.data.get("entities")
            if not isinstance(entities, list):
                continue
            for entity in entities:
                if not isinstance(entity, dict) or not isinstance(entity.get("snapshot"), dict):
                    continue
                repository_id = entity.get("repository_id")
                revision = entity.get("revision")
                if isinstance(repository_id, str) and isinstance(revision, str):
                    snapshots[(repository_id, revision)] = SnapshotProvenance.model_validate(
                        entity["snapshot"]
                    )
        enriched = []
        for item in evidence:
            location = item.location
            if location is None or location.snapshot is not None:
                enriched.append(item)
                continue
            snapshot = snapshots.get((location.repository_id, location.revision))
            enriched.append(
                item
                if snapshot is None
                else item.model_copy(
                    update={"location": location.model_copy(update={"snapshot": snapshot})}
                )
            )
        return enriched
