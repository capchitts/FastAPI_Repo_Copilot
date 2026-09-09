from typing import Any

import pytest

from repo_chat.contracts.orchestration import ConversationRole
from repo_chat.orchestration.redis_memory import RedisConversationMemory


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.expirations: list[tuple[str, int]] = []

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def expire(self, key: str, seconds: int) -> None:
        self.expirations.append((key, seconds))

    async def set(self, key: str, value: str, **options: Any) -> None:
        self.values[key] = value
        self.expirations.append((key, options["ex"]))

    def pipeline(self, *, transaction: bool) -> "FakePipeline":
        assert transaction is True
        return FakePipeline(self)


class FakePipeline:
    def __init__(self, redis: FakeRedis) -> None:
        self.redis = redis
        self.pending: tuple[str, str, int] | None = None

    async def __aenter__(self) -> "FakePipeline":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def watch(self, _key: str) -> None:
        return None

    async def get(self, key: str) -> str | None:
        return await self.redis.get(key)

    def multi(self) -> None:
        return None

    def set(self, key: str, value: str, **options: Any) -> None:
        self.pending = (key, value, int(options["ex"]))

    async def execute(self) -> None:
        assert self.pending is not None
        key, value, expiration = self.pending
        await self.redis.set(key, value, ex=expiration)


@pytest.mark.asyncio
async def test_redis_memory_round_trip_and_sliding_ttl() -> None:
    redis = FakeRedis()
    memory = RedisConversationMemory(redis, maximum_turns=2, ttl_seconds=60)  # type: ignore[arg-type]

    await memory.append_turn(
        "session",
        ConversationRole.USER,
        "one",
        entities=["APIRouter"],
        repository_id="fastapi",
        revision="abc123",
    )
    await memory.append_turn("session", ConversationRole.ASSISTANT, "two")
    await memory.append_turn("session", ConversationRole.USER, "three", entities=["Depends"])
    context = await memory.get_context("session")

    assert [turn.content for turn in context.turns] == ["two", "three"]
    assert context.active_entities == ["APIRouter", "Depends"]
    assert context.repository_id == "fastapi"
    assert context.revision == "abc123"
    assert redis.expirations[-1] == ("repo-chat:session:session", 60)


@pytest.mark.asyncio
async def test_missing_redis_session_returns_empty_context() -> None:
    memory = RedisConversationMemory(FakeRedis())  # type: ignore[arg-type]

    context = await memory.get_context("missing")

    assert context.session_id == "missing"
    assert context.turns == []


def test_redis_memory_rejects_invalid_limits() -> None:
    with pytest.raises(ValueError, match="maximum_turns"):
        RedisConversationMemory(FakeRedis(), maximum_turns=0)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="ttl_seconds"):
        RedisConversationMemory(FakeRedis(), ttl_seconds=0)  # type: ignore[arg-type]
