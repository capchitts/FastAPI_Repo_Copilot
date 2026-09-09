from datetime import UTC, datetime
from typing import Any

import pytest

from repo_chat.contracts.orchestration import (
    AgentName,
    EpisodicMemory,
    QueryIntent,
    SessionPreferences,
    SynthesizedResponse,
)
from repo_chat.orchestration.operational_state import (
    RedisOrchestrationOperationalState,
    routing_audit_record,
)


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: str, **_options: Any) -> None:
        self.values[key] = value

    async def expire(self, _key: str, _seconds: int) -> None:
        return None

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        return self.lists.get(key, [])[start : end + 1]

    def pipeline(self, *, transaction: bool) -> "FakePipeline":
        assert transaction
        return FakePipeline(self)


class FakePipeline:
    def __init__(self, redis: FakeRedis) -> None:
        self.redis = redis
        self.operations: list[tuple[str, tuple[Any, ...]]] = []

    async def __aenter__(self) -> "FakePipeline":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def lpush(self, *args: Any) -> None:
        self.operations.append(("lpush", args))

    def ltrim(self, *args: Any) -> None:
        self.operations.append(("ltrim", args))

    def expire(self, *args: Any) -> None:
        self.operations.append(("expire", args))

    async def execute(self) -> None:
        for operation, arguments in self.operations:
            if operation == "lpush":
                key, value = arguments
                self.redis.lists.setdefault(key, []).insert(0, value)
            elif operation == "ltrim":
                key, start, end = arguments
                self.redis.lists[key] = self.redis.lists.get(key, [])[start : end + 1]


@pytest.mark.asyncio
async def test_cache_is_revision_and_preference_scoped() -> None:
    redis = FakeRedis()
    state = RedisOrchestrationOperationalState(redis)  # type: ignore[arg-type]
    response = SynthesizedResponse(answer="grounded")
    balanced = SessionPreferences()
    detailed = SessionPreferences(response_detail="detailed")

    await state.put_cached_response("fastapi", "one", "Explain X", balanced, response)

    assert await state.get_cached_response("fastapi", "one", " explain   x ", balanced) == response
    assert await state.get_cached_response("fastapi", "two", "Explain X", balanced) is None
    assert await state.get_cached_response("fastapi", "one", "Explain X", detailed) is None


@pytest.mark.asyncio
async def test_preferences_and_bounded_audit_are_namespaced() -> None:
    redis = FakeRedis()
    state = RedisOrchestrationOperationalState(
        redis, audit_maximum_records=1
    )  # type: ignore[arg-type]
    preferences = SessionPreferences(code_examples="always")
    await state.set_preferences("session", preferences)

    for query in ("first", "second"):
        await state.append_audit(
            "session",
            routing_audit_record(
                query,
                repository_id="fastapi",
                revision="one",
                intent=QueryIntent.GENERAL,
                planned_agents=[AgentName.GRAPH],
                successful_agents=[AgentName.GRAPH],
                partial=False,
                cache_hit=False,
            ),
        )

    assert await state.get_preferences("session") == preferences
    records = redis.lists["repo-chat:routing-audit:session"]
    assert len(records) == 1
    assert "second" not in records[0]


@pytest.mark.asyncio
async def test_episodic_recall_is_user_repository_and_relevance_scoped() -> None:
    redis = FakeRedis()
    state = RedisOrchestrationOperationalState(redis)  # type: ignore[arg-type]
    await state.remember_episode(
        "user-one",
        EpisodicMemory(
            query="Explain APIRouter implementation",
            answer="APIRouter groups routes.",
            repository_id="fastapi",
            revision="one",
            entities=["APIRouter"],
            created_at=datetime.now(UTC),
        ),
    )

    relevant = await state.recall_episodes(
        "user-one", "How did APIRouter work before?", "fastapi"
    )
    wrong_user = await state.recall_episodes(
        "user-two", "How did APIRouter work before?", "fastapi"
    )
    wrong_repository = await state.recall_episodes(
        "user-one", "How did APIRouter work before?", "other"
    )

    assert [episode.entities for episode in relevant] == [["APIRouter"]]
    assert wrong_user == []
    assert wrong_repository == []
