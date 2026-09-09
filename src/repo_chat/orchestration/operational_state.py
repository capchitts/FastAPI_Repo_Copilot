"""Redis-backed routing audit, response cache, and session preferences."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Protocol

from redis.asyncio import Redis

from repo_chat.contracts.orchestration import (
    AgentName,
    EpisodicMemory,
    QueryIntent,
    RoutingAuditRecord,
    SessionPreferences,
    SynthesizedResponse,
)

RESPONSE_CACHE_SCHEMA_VERSION = 2


class OrchestrationOperationalState(Protocol):
    async def get_cached_response(
        self, repository_id: str, revision: str, query: str, preferences: SessionPreferences
    ) -> SynthesizedResponse | None: ...

    async def put_cached_response(
        self,
        repository_id: str,
        revision: str,
        query: str,
        preferences: SessionPreferences,
        response: SynthesizedResponse,
    ) -> None: ...

    async def append_audit(self, session_id: str, record: RoutingAuditRecord) -> None: ...

    async def get_preferences(self, session_id: str) -> SessionPreferences: ...

    async def set_preferences(
        self, session_id: str, preferences: SessionPreferences
    ) -> SessionPreferences: ...

    async def recall_episodes(
        self, user_id: str, query: str, repository_id: str, limit: int = 3
    ) -> list[EpisodicMemory]: ...

    async def remember_episode(self, user_id: str, episode: EpisodicMemory) -> None: ...


class RedisOrchestrationOperationalState:
    """Keep bounded operational state in namespaced Redis keys."""

    def __init__(
        self,
        redis: Redis,
        *,
        cache_ttl_seconds: int = 3600,
        audit_ttl_seconds: int = 604800,
        audit_maximum_records: int = 100,
        preference_ttl_seconds: int = 86400,
        episode_ttl_seconds: int = 604800,
        episode_maximum_records: int = 50,
        key_prefix: str = "repo-chat",
    ) -> None:
        self._redis = redis
        self._cache_ttl = cache_ttl_seconds
        self._audit_ttl = audit_ttl_seconds
        self._audit_maximum = audit_maximum_records
        self._preference_ttl = preference_ttl_seconds
        self._episode_ttl = episode_ttl_seconds
        self._episode_maximum = episode_maximum_records
        self._prefix = key_prefix

    async def get_cached_response(
        self, repository_id: str, revision: str, query: str, preferences: SessionPreferences
    ) -> SynthesizedResponse | None:
        value = await self._redis.get(self._cache_key(repository_id, revision, query, preferences))
        return SynthesizedResponse.model_validate_json(value) if value is not None else None

    async def put_cached_response(
        self,
        repository_id: str,
        revision: str,
        query: str,
        preferences: SessionPreferences,
        response: SynthesizedResponse,
    ) -> None:
        await self._redis.set(
            self._cache_key(repository_id, revision, query, preferences),
            response.model_dump_json(),
            ex=self._cache_ttl,
        )

    async def append_audit(self, session_id: str, record: RoutingAuditRecord) -> None:
        key = f"{self._prefix}:routing-audit:{session_id}"
        async with self._redis.pipeline(transaction=True) as pipeline:
            pipeline.lpush(key, record.model_dump_json())
            pipeline.ltrim(key, 0, self._audit_maximum - 1)
            pipeline.expire(key, self._audit_ttl)
            await pipeline.execute()

    async def get_preferences(self, session_id: str) -> SessionPreferences:
        key = f"{self._prefix}:preferences:{session_id}"
        value = await self._redis.get(key)
        if value is None:
            return SessionPreferences()
        await self._redis.expire(key, self._preference_ttl)
        return SessionPreferences.model_validate_json(value)

    async def set_preferences(
        self, session_id: str, preferences: SessionPreferences
    ) -> SessionPreferences:
        await self._redis.set(
            f"{self._prefix}:preferences:{session_id}",
            preferences.model_dump_json(),
            ex=self._preference_ttl,
        )
        return preferences

    async def recall_episodes(
        self, user_id: str, query: str, repository_id: str, limit: int = 3
    ) -> list[EpisodicMemory]:
        key = f"{self._prefix}:episodes:{user_id}"
        values = await self._redis.lrange(key, 0, self._episode_maximum - 1)
        query_terms = self._terms(query)
        ranked: list[tuple[float, EpisodicMemory]] = []
        for value in values:
            episode = EpisodicMemory.model_validate_json(value)
            if episode.repository_id != repository_id:
                continue
            episode_terms = self._terms(" ".join([episode.query, *episode.entities]))
            union = query_terms | episode_terms
            score = len(query_terms & episode_terms) / len(union) if union else 0.0
            if score > 0:
                ranked.append((score, episode))
        await self._redis.expire(key, self._episode_ttl)
        ranked.sort(key=lambda item: item[0], reverse=True)
        return [episode for _, episode in ranked[:limit]]

    async def remember_episode(self, user_id: str, episode: EpisodicMemory) -> None:
        key = f"{self._prefix}:episodes:{user_id}"
        async with self._redis.pipeline(transaction=True) as pipeline:
            pipeline.lpush(key, episode.model_dump_json())
            pipeline.ltrim(key, 0, self._episode_maximum - 1)
            pipeline.expire(key, self._episode_ttl)
            await pipeline.execute()

    def _cache_key(
        self, repository_id: str, revision: str, query: str, preferences: SessionPreferences
    ) -> str:
        normalized = " ".join(query.casefold().split())
        identity = json.dumps(
            [
                repository_id,
                revision,
                normalized,
                preferences.model_dump(mode="json"),
                RESPONSE_CACHE_SCHEMA_VERSION,
            ],
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(identity.encode()).hexdigest()
        return f"{self._prefix}:response-cache:{repository_id}:{revision}:{digest}"

    @staticmethod
    def _terms(value: str) -> set[str]:
        return {
            term
            for term in "".join(
                character if character.isalnum() or character == "_" else " "
                for character in value.casefold()
            ).split()
            if len(term) > 2
        }


def routing_audit_record(
    query: str,
    *,
    repository_id: str | None,
    revision: str | None,
    intent: QueryIntent,
    planned_agents: list[AgentName],
    successful_agents: list[AgentName],
    partial: bool,
    cache_hit: bool,
) -> RoutingAuditRecord:
    """Build an audit record without retaining the raw user prompt."""
    return RoutingAuditRecord(
        query_hash=hashlib.sha256(query.encode()).hexdigest(),
        repository_id=repository_id,
        revision=revision,
        intent=intent,
        planned_agents=planned_agents,
        successful_agents=successful_agents,
        partial=partial,
        cache_hit=cache_hit,
        created_at=datetime.now(UTC),
    )
