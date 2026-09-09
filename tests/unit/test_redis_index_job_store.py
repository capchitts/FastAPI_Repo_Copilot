from datetime import UTC, datetime
from typing import Any

import pytest

from repo_chat.contracts.indexing import IndexJob, IndexJobStatus, IndexMode
from repo_chat.indexing.redis_job_store import RedisIndexJobStore


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.sets: dict[str, set[str]] = {}
        self.expirations: dict[str, int] = {}

    def pipeline(self, *, transaction: bool) -> "FakePipeline":
        assert transaction is True
        return FakePipeline(self)

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def expire(self, key: str, seconds: int) -> None:
        self.expirations[key] = seconds

    async def smembers(self, key: str) -> set[str]:
        return set(self.sets.get(key, set()))

    async def srem(self, key: str, member: str) -> None:
        self.sets.setdefault(key, set()).discard(member)


class FakePipeline:
    def __init__(self, redis: FakeRedis) -> None:
        self.redis = redis
        self.commands: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

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
        self.commands.append(("set", (key, value), options))

    def sadd(self, key: str, member: str) -> None:
        self.commands.append(("sadd", (key, member), {}))

    def srem(self, key: str, member: str) -> None:
        self.commands.append(("srem", (key, member), {}))

    async def execute(self) -> None:
        for command, arguments, options in self.commands:
            key, value = arguments
            if command == "set":
                self.redis.values[str(key)] = str(value)
                self.redis.expirations[str(key)] = int(options["ex"])
            elif command == "sadd":
                self.redis.sets.setdefault(str(key), set()).add(str(value))
            else:
                self.redis.sets.setdefault(str(key), set()).discard(str(value))


def make_job(job_id: str = "job-1") -> IndexJob:
    return IndexJob(
        id=job_id,
        repository_id="fastapi",
        repository_path="/repositories/fastapi",
        revision="abc123",
        mode=IndexMode.FULL,
        created_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_redis_job_store_persists_and_atomically_updates_progress() -> None:
    redis = FakeRedis()
    store = RedisIndexJobStore(redis, ttl_seconds=60)  # type: ignore[arg-type]

    await store.create(make_job())
    await store.update("job-1", status=IndexJobStatus.RUNNING)
    await store.increment("job-1", processed_files=2, entity_count=7)
    job = await store.get("job-1")

    assert job is not None
    assert job.status is IndexJobStatus.RUNNING
    assert job.processed_files == 2
    assert job.entity_count == 7
    assert "job-1" in redis.sets["repo-chat:index-job:active"]


@pytest.mark.asyncio
async def test_redis_job_store_marks_restart_interruption_and_retains_status() -> None:
    redis = FakeRedis()
    first = RedisIndexJobStore(redis, ttl_seconds=60)  # type: ignore[arg-type]
    await first.create(make_job())
    await first.update("job-1", status=IndexJobStatus.RUNNING, processed_files=3)

    restarted = RedisIndexJobStore(redis, ttl_seconds=60)  # type: ignore[arg-type]
    recovered = await restarted.fail_interrupted(datetime.now(UTC))
    job = await restarted.get("job-1")

    assert recovered == 1
    assert job is not None
    assert job.status is IndexJobStatus.FAILED
    assert job.processed_files == 3
    assert job.error == "Indexer restarted before the job completed"
    assert "job-1" not in redis.sets["repo-chat:index-job:active"]


def test_redis_job_store_rejects_short_ttl() -> None:
    with pytest.raises(ValueError, match="at least 60"):
        RedisIndexJobStore(FakeRedis(), ttl_seconds=59)  # type: ignore[arg-type]
