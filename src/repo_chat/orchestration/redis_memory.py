"""Redis-backed bounded conversation memory."""

from datetime import UTC, datetime

from redis.asyncio import Redis
from redis.exceptions import WatchError

from repo_chat.contracts.orchestration import (
    ConversationContext,
    ConversationRole,
    ConversationTurn,
)


class RedisConversationMemory:
    """Persist bounded session context with sliding expiration."""

    def __init__(
        self,
        redis: Redis,
        *,
        maximum_turns: int = 10,
        ttl_seconds: int = 86400,
        key_prefix: str = "repo-chat:session",
    ) -> None:
        self._redis = redis
        if maximum_turns < 1:
            raise ValueError("maximum_turns must be positive")
        if ttl_seconds < 1:
            raise ValueError("ttl_seconds must be positive")
        self._maximum_turns = maximum_turns
        self._ttl_seconds = ttl_seconds
        self._key_prefix = key_prefix

    async def get_context(self, session_id: str) -> ConversationContext:
        """Load and refresh one session, or return an empty context."""
        key = self._key(session_id)
        value = await self._redis.get(key)
        if value is None:
            return ConversationContext(session_id=session_id)
        await self._redis.expire(key, self._ttl_seconds)
        return ConversationContext.model_validate_json(value)

    async def append_turn(
        self,
        session_id: str,
        role: ConversationRole,
        content: str,
        *,
        entities: list[str] | None = None,
        repository_id: str | None = None,
        revision: str | None = None,
    ) -> ConversationContext:
        """Append using Redis optimistic locking so multiple workers cannot lose turns."""
        key = self._key(session_id)
        turn = ConversationTurn(
            role=role,
            content=content,
            created_at=datetime.now(UTC),
            entities=entities or [],
        )
        while True:
            async with self._redis.pipeline(transaction=True) as pipeline:
                try:
                    await pipeline.watch(key)
                    value = await pipeline.get(key)
                    context = (
                        ConversationContext.model_validate_json(value)
                        if value is not None
                        else ConversationContext(session_id=session_id)
                    )
                    context.turns.append(turn)
                    context.turns = context.turns[-self._maximum_turns :]
                    for entity in entities or []:
                        if entity in context.active_entities:
                            context.active_entities.remove(entity)
                        context.active_entities.append(entity)
                    context.active_entities = context.active_entities[-20:]
                    context.repository_id = repository_id or context.repository_id
                    context.revision = revision or context.revision
                    pipeline.multi()  # type: ignore[no-untyped-call]
                    pipeline.set(key, context.model_dump_json(), ex=self._ttl_seconds)
                    await pipeline.execute()
                    return context
                except WatchError:
                    continue

    def _key(self, session_id: str) -> str:
        return f"{self._key_prefix}:{session_id}"
