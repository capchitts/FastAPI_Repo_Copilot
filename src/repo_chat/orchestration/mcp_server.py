"""MCP 2.x server exposing central orchestration tools."""

from mcp.server.mcpserver import MCPServer
from redis.asyncio import Redis

from repo_chat.config.settings import Settings
from repo_chat.contracts.orchestration import (
    AgentOutput,
    ConversationContext,
    QueryAnalysis,
    RoutingPlan,
    SessionPreferences,
    SynthesizedResponse,
)
from repo_chat.mcp.client import MCPToolClient
from repo_chat.observability.logging import configure_logging
from repo_chat.observability.mcp import MCPObservabilityMiddleware
from repo_chat.orchestration.executor import MCPAgentExecutor
from repo_chat.orchestration.operational_state import RedisOrchestrationOperationalState
from repo_chat.orchestration.redis_memory import RedisConversationMemory
from repo_chat.orchestration.service import OrchestratorService
from repo_chat.orchestration.synthesizer import CompatibleResponsesAnswerSynthesizer


def create_server(service: OrchestratorService) -> MCPServer[None]:
    """Create the Orchestrator MCP server around its typed service."""
    server: MCPServer[None] = MCPServer(
        name="fastapi-repository-orchestrator",
        title="FastAPI Repository Orchestrator",
        description="Analyzes, routes, and synthesizes repository questions.",
        version="0.1.0",
        middleware=[MCPObservabilityMiddleware("orchestrator")],
    )

    @server.tool(name="analyze_query", structured_output=True)
    async def analyze_query_tool(query: str, session_id: str | None = None) -> QueryAnalysis:
        """Classify query intent and extract repository entities."""
        context = await service.get_conversation_context(session_id) if session_id else None
        return service.analyze_query(query, context)

    @server.tool(name="route_to_agents", structured_output=True)
    def route_to_agents_tool(analysis: QueryAnalysis) -> RoutingPlan:
        """Produce an ordered parallel/sequential specialized-agent plan."""
        return service.route_to_agents(analysis)

    @server.tool(name="get_conversation_context", structured_output=True)
    async def get_conversation_context_tool(session_id: str) -> ConversationContext:
        """Retrieve bounded recent turns and active entities for a session."""
        return await service.get_conversation_context(session_id)

    @server.tool(name="get_session_preferences", structured_output=True)
    async def get_session_preferences_tool(session_id: str) -> SessionPreferences:
        """Return bounded presentation preferences for one session."""
        return await service.get_preferences(session_id)

    @server.tool(name="set_session_preferences", structured_output=True)
    async def set_session_preferences_tool(
        session_id: str, preferences: SessionPreferences
    ) -> SessionPreferences:
        """Persist validated presentation preferences for one session."""
        return await service.set_preferences(session_id, preferences)

    @server.tool(name="synthesize_response", structured_output=True)
    def synthesize_response_tool(
        query: str,
        outputs: list[AgentOutput],
    ) -> SynthesizedResponse:
        """Combine successful agent evidence and explicit failure warnings."""
        return service.synthesize_response(query, outputs)

    @server.tool(name="chat", structured_output=True)
    async def chat_tool(
        session_id: str,
        message: str,
        user_id: str | None = None,
        repository_id: str | None = None,
        revision: str | None = None,
    ) -> SynthesizedResponse:
        """Execute one complete context-aware repository chat turn."""
        return await service.orchestrate(
            session_id,
            message,
            user_id=user_id,
            repository_id=repository_id,
            revision=revision,
        )

    return server


def main() -> None:
    """Run the Orchestrator over MCP Streamable HTTP."""
    settings = Settings(service_name="orchestrator")
    configure_logging(settings.log_level)
    redis = Redis.from_url(settings.redis_url)
    memory = RedisConversationMemory(
        redis,
        maximum_turns=settings.conversation_maximum_turns,
        ttl_seconds=settings.conversation_ttl_seconds,
    )
    operational_state = RedisOrchestrationOperationalState(
        redis,
        cache_ttl_seconds=settings.response_cache_ttl_seconds,
        audit_ttl_seconds=settings.routing_audit_ttl_seconds,
        audit_maximum_records=settings.routing_audit_maximum_records,
        preference_ttl_seconds=settings.conversation_ttl_seconds,
    )
    executor = MCPAgentExecutor(
        indexer=MCPToolClient(
            settings.indexer_mcp_url,
            timeout_seconds=settings.agent_timeout_seconds,
            maximum_retries=settings.agent_max_retries,
            retry_base_seconds=settings.agent_retry_base_seconds,
        ),
        graph=MCPToolClient(
            settings.graph_agent_mcp_url,
            timeout_seconds=settings.semantic_agent_timeout_seconds,
            maximum_retries=settings.agent_max_retries,
            retry_base_seconds=settings.agent_retry_base_seconds,
        ),
        code_analyst=MCPToolClient(
            settings.code_analyst_mcp_url,
            timeout_seconds=settings.agent_timeout_seconds,
            maximum_retries=settings.agent_max_retries,
            retry_base_seconds=settings.agent_retry_base_seconds,
        ),
        repository=MCPToolClient(
            settings.repository_agent_mcp_url,
            timeout_seconds=settings.agent_timeout_seconds,
            maximum_retries=settings.agent_max_retries,
            retry_base_seconds=settings.agent_retry_base_seconds,
        ),
    )
    answer_synthesizer = None
    if settings.llm_enabled and settings.llm_api_key is not None:
        answer_synthesizer = CompatibleResponsesAnswerSynthesizer(
            api_key=settings.llm_api_key.get_secret_value(),
            model=settings.llm_model,
            base_url=settings.llm_base_url,
            timeout_seconds=settings.llm_timeout_seconds,
            maximum_input_characters=settings.llm_maximum_input_characters,
            maximum_output_tokens=settings.llm_maximum_output_tokens,
        )
    server = create_server(
        OrchestratorService(
            memory,
            executor,
            agent_timeout_seconds=(
                settings.semantic_agent_timeout_seconds
                * (settings.agent_max_retries + 1)
                + settings.agent_retry_base_seconds * (2**settings.agent_max_retries)
            ),
            answer_synthesizer=answer_synthesizer,
            operational_state=operational_state,
        )
    )
    server.run(
        transport="streamable-http",
        host=settings.mcp_host,
        port=settings.orchestrator_port,
    )


if __name__ == "__main__":
    main()
