"""Translate routing tasks into calls to specialized MCP servers."""

import asyncio
from typing import Any

from repo_chat.contracts.evidence import Evidence, EvidenceKind
from repo_chat.contracts.orchestration import (
    AgentName,
    AgentOutput,
    AgentTask,
    ConversationContext,
    QueryAnalysis,
)
from repo_chat.exceptions.base import AgentError
from repo_chat.mcp.client import MCPToolClient


class MCPAgentExecutor:
    """Execute normalized Orchestrator tasks through MCP tool clients."""

    _MAXIMUM_EVIDENCE_EXCERPT_CHARS = 4_000

    def __init__(
        self,
        *,
        indexer: MCPToolClient,
        graph: MCPToolClient,
        code_analyst: MCPToolClient,
        repository: MCPToolClient | None = None,
    ) -> None:
        self._clients = {
            AgentName.INDEXER: indexer,
            AgentName.GRAPH: graph,
            AgentName.CODE_ANALYST: code_analyst,
        }
        if repository is not None:
            self._clients[AgentName.REPOSITORY] = repository

    async def execute(
        self,
        task: AgentTask,
        analysis: QueryAnalysis,
        context: ConversationContext,
        prior_outputs: list[AgentOutput],
    ) -> AgentOutput:
        """Build validated tool arguments and normalize an MCP result."""
        client = self._clients[task.agent]
        if task.agent is AgentName.REPOSITORY and task.operation == "verify_source":
            if not context.repository_id or not context.revision:
                raise AgentError("Repository and revision context are required for source access")
            entities = self._graph_entities(prior_outputs)[:6]
            if not entities:
                raise AgentError("A prior graph entity result is required for source verification")
            results = await asyncio.gather(
                *(
                    client.call(
                        "verify_source",
                        {
                            "repository_id": context.repository_id,
                            "revision": context.revision,
                            "file_path": entity["file_path"],
                        },
                    )
                    for entity in entities
                )
            )
            evidence = []
            for result in results:
                if not isinstance(result, dict) or not isinstance(result.get("location"), dict):
                    continue
                evidence.append(
                    Evidence(
                        kind=EvidenceKind.SOURCE,
                        location=result["location"],
                    )
                )
            return AgentOutput(
                agent=task.agent,
                operation=task.operation,
                success=True,
                data={"verifications": results},
                evidence=evidence,
            )
        if task.agent is AgentName.GRAPH and task.operation == "hybrid_search":
            if not context.repository_id or not context.revision:
                raise AgentError("Repository and revision context are required for hybrid search")
            result = await client.call(
                "hybrid_search",
                {
                    "query": analysis.query,
                    "repository_id": context.repository_id,
                    "revision": context.revision,
                    "limit": 10,
                },
            )
            hits = result.get("hits", []) if isinstance(result, dict) else []
            entities = [
                {
                    **hit["entity"],
                    "retrieval_score": hit.get("score"),
                    "retrieval_sources": hit.get("sources", []),
                }
                for hit in hits
                if isinstance(hit, dict) and isinstance(hit.get("entity"), dict)
            ]
            return AgentOutput(
                agent=task.agent,
                operation=task.operation,
                success=True,
                data={"query": analysis.query, "entities": entities},
                evidence=[
                    Evidence(kind=EvidenceKind.GRAPH, entity_id=str(entity["id"]))
                    for entity in entities
                    if entity.get("id")
                ],
            )
        if task.agent is AgentName.GRAPH and task.operation == "find_entity":
            names = [entity.name for entity in analysis.entities]
            if not names:
                raise AgentError("No entity was identified for graph lookup")
            results = [
                await client.call("find_entity", {"name": name, "limit": 10}) for name in names
            ]
            entities = []
            for name, result in zip(names, results, strict=True):
                candidates = result.get("entities", []) if isinstance(result, dict) else []
                selected = self._select_entity(
                    name, [item for item in candidates if isinstance(item, dict)]
                )
                if selected is not None:
                    entities.append({**selected, "requested_name": name})
            data: Any = {"query": analysis.query, "entities": entities}
            return AgentOutput(
                agent=task.agent,
                operation=task.operation,
                success=True,
                data=data,
                evidence=[
                    Evidence(kind=EvidenceKind.GRAPH, entity_id=str(entity["id"]))
                    for entity in entities
                    if entity.get("id")
                ],
            )
        if task.agent is AgentName.CODE_ANALYST and task.operation == "explain_implementation":
            entities = self._graph_entities(prior_outputs)
            if len(entities) > 1:
                return await self._explain_multiple(task, context, entities[:6])
        arguments = self._arguments(task, analysis, context, prior_outputs)
        data = await client.call(task.operation, arguments)
        evidence = self._extract_evidence(data)
        return AgentOutput(
            agent=task.agent,
            operation=task.operation,
            success=True,
            data=data,
            evidence=evidence,
        )

    async def _explain_multiple(
        self,
        task: AgentTask,
        context: ConversationContext,
        entities: list[dict[str, Any]],
    ) -> AgentOutput:
        """Analyze bounded graph-resolved source entities concurrently."""
        if not context.repository_id or not context.revision:
            raise AgentError("Repository and revision context are required for source analysis")
        client = self._clients[AgentName.CODE_ANALYST]
        calls = [
            client.call(
                "explain_implementation",
                {
                    "repository_id": context.repository_id,
                    "revision": context.revision,
                    "file_path": entity["file_path"],
                    "entity_name": entity.get("requested_name", entity["name"]),
                },
            )
            for entity in entities
        ]
        results = await asyncio.gather(*calls, return_exceptions=True)
        analyses = [result for result in results if isinstance(result, dict)]
        warnings = [str(result) for result in results if isinstance(result, BaseException)]
        if not analyses:
            raise AgentError("Code Analyst could not analyze any discovered lifecycle entity")
        data: Any = {"analyses": analyses}
        return AgentOutput(
            agent=task.agent,
            operation=task.operation,
            success=True,
            data=data,
            evidence=self._extract_evidence(data),
            warnings=warnings,
        )

    def _arguments(
        self,
        task: AgentTask,
        analysis: QueryAnalysis,
        context: ConversationContext,
        prior_outputs: list[AgentOutput],
    ) -> dict[str, Any]:
        names = [entity.name for entity in analysis.entities]
        if task.agent is AgentName.GRAPH and task.operation == "find_entity":
            if not names:
                raise AgentError("No entity was identified for graph lookup")
            return {"name": names[0]}
        if task.agent is AgentName.GRAPH and task.operation == "get_dependencies":
            entity = self._first_graph_entity(prior_outputs)
            return {"entity_id": entity["id"], "max_depth": 3}
        if task.agent is AgentName.CODE_ANALYST:
            if not context.repository_id or not context.revision:
                raise AgentError("Repository and revision context are required for source analysis")
            entity = self._first_graph_entity(prior_outputs)
            base = {
                "repository_id": context.repository_id,
                "revision": context.revision,
                "file_path": entity["file_path"],
            }
            if task.operation == "explain_implementation":
                return {
                    **base,
                    "entity_name": entity.get("requested_name", entity["name"]),
                }
            if task.operation == "compare_implementations":
                entities = self._graph_entities(prior_outputs)
                if len(entities) < 2:
                    raise AgentError("Two resolved graph entities are required for comparison")
                return {
                    "repository_id": context.repository_id,
                    "revision": context.revision,
                    "left_file_path": entities[0]["file_path"],
                    "left_entity_name": entities[0]["name"],
                    "right_file_path": entities[1]["file_path"],
                    "right_entity_name": entities[1]["name"],
                }
            if task.operation == "find_patterns":
                return base
        if task.agent is AgentName.INDEXER:
            raise AgentError(
                "Indexer routing requires an explicit repository path from the gateway"
            )
        raise AgentError(f"No MCP argument mapping exists for {task.agent.value}.{task.operation}")

    @staticmethod
    def _first_graph_entity(outputs: list[AgentOutput]) -> dict[str, Any]:
        entities = MCPAgentExecutor._graph_entities(outputs)
        if entities:
            return entities[0]
        raise AgentError("A prior graph entity result is required")

    @staticmethod
    def _graph_entities(outputs: list[AgentOutput]) -> list[dict[str, Any]]:
        for output in reversed(outputs):
            if output.agent is AgentName.GRAPH and output.success and isinstance(output.data, dict):
                entities = output.data.get("entities")
                if isinstance(entities, list):
                    return [entity for entity in entities if isinstance(entity, dict)]
        return []

    @staticmethod
    def _select_entity(name: str, entities: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Prefer an exact executable definition over imports and documentation nodes."""
        if not entities:
            return None
        core_kinds = {"class", "function", "method", "module"}
        requested = name.casefold()

        def matches(entity: dict[str, Any]) -> bool:
            simple_name = str(entity.get("name", "")).casefold()
            qualified_name = str(entity.get("qualified_name", "")).casefold()
            return (
                simple_name == requested
                or qualified_name == requested
                or qualified_name.endswith(f".{requested}")
            )

        credible = [entity for entity in entities if matches(entity)]
        if not credible:
            return None

        def rank(entity: dict[str, Any]) -> tuple[bool, bool, bool, bool]:
            simple_name = str(entity.get("name", ""))
            qualified_name = str(entity.get("qualified_name", ""))
            return (
                simple_name == name,
                str(entity.get("kind", "")) in core_kinds,
                qualified_name == name or qualified_name.endswith(f".{name}"),
                str(entity.get("file_path", "")).startswith("fastapi/"),
            )

        return max(credible, key=rank)

    @staticmethod
    def _extract_evidence(data: Any) -> list[Evidence]:
        evidence: list[Evidence] = []

        def visit(value: Any, *, evidence_value: bool = False) -> None:
            if evidence_value:
                candidates = value if isinstance(value, list) else [value]
                for candidate in candidates:
                    if not isinstance(candidate, dict):
                        continue
                    try:
                        item = Evidence.model_validate(candidate)
                        if (
                            item.excerpt is not None
                            and len(item.excerpt) > MCPAgentExecutor._MAXIMUM_EVIDENCE_EXCERPT_CHARS
                        ):
                            item = item.model_copy(
                                update={
                                    "excerpt": item.excerpt[
                                        : MCPAgentExecutor._MAXIMUM_EVIDENCE_EXCERPT_CHARS - 2
                                    ].rstrip()
                                    + " …"
                                }
                            )
                        evidence.append(item)
                    except ValueError:
                        continue
                return
            if isinstance(value, dict):
                for key, child in value.items():
                    visit(child, evidence_value=key == "evidence")
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(data)
        unique = {item.model_dump_json(): item for item in evidence}
        return list(unique.values())
