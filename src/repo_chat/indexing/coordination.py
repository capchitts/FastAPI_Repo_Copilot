"""Idempotency and distributed-lock boundaries for repository indexing."""

import asyncio
from typing import Protocol

from redis.asyncio import Redis


class IndexCoordination(Protocol):
    async def claim_idempotency(self, identity: str, job_id: str) -> str: ...
    async def acquire_repository_lock(self, repository_id: str, token: str) -> bool: ...
    async def release_repository_lock(self, repository_id: str, token: str) -> None: ...


class InMemoryIndexCoordination:
    def __init__(self) -> None:
        self._claims: dict[str, str] = {}
        self._locks: dict[str, str] = {}
        self._mutex = asyncio.Lock()

    async def claim_idempotency(self, identity: str, job_id: str) -> str:
        async with self._mutex:
            return self._claims.setdefault(identity, job_id)

    async def acquire_repository_lock(self, repository_id: str, token: str) -> bool:
        async with self._mutex:
            if repository_id in self._locks:
                return False
            self._locks[repository_id] = token
            return True

    async def release_repository_lock(self, repository_id: str, token: str) -> None:
        async with self._mutex:
            if self._locks.get(repository_id) == token:
                del self._locks[repository_id]


class RedisIndexCoordination:
    """Use Redis NX keys and token-checked release for multi-replica safety."""

    _RELEASE_SCRIPT = """
        if redis.call('get', KEYS[1]) == ARGV[1] then
            return redis.call('del', KEYS[1])
        end
        return 0
    """

    def __init__(
        self,
        redis: Redis,
        *,
        lock_ttl_seconds: int = 7200,
        idempotency_ttl_seconds: int = 604800,
        key_prefix: str = "repo-chat:index",
    ) -> None:
        self._redis = redis
        self._lock_ttl = lock_ttl_seconds
        self._idempotency_ttl = idempotency_ttl_seconds
        self._prefix = key_prefix

    async def claim_idempotency(self, identity: str, job_id: str) -> str:
        key = f"{self._prefix}:idempotency:{identity}"
        claimed = await self._redis.set(key, job_id, ex=self._idempotency_ttl, nx=True)
        if claimed:
            return job_id
        existing = await self._redis.get(key)
        return existing.decode() if isinstance(existing, bytes) else str(existing)

    async def acquire_repository_lock(self, repository_id: str, token: str) -> bool:
        result = await self._redis.set(
            f"{self._prefix}:lock:{repository_id}", token, ex=self._lock_ttl, nx=True
        )
        return bool(result)

    async def release_repository_lock(self, repository_id: str, token: str) -> None:
        await self._redis.eval(
            self._RELEASE_SCRIPT,
            1,
            f"{self._prefix}:lock:{repository_id}",
            token,
        )
