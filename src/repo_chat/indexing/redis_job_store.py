"""Redis-backed durable indexing-job storage."""

from datetime import datetime

from redis.asyncio import Redis
from redis.exceptions import WatchError

from repo_chat.contracts.indexing import IndexJob, IndexJobStatus
from repo_chat.indexing.job_store import IndexJobStore


class RedisIndexJobStore(IndexJobStore):
    """Persist jobs as JSON with optimistic atomic progress updates."""

    def __init__(
        self,
        redis: Redis,
        *,
        ttl_seconds: int = 604_800,
        key_prefix: str = "repo-chat:index-job",
    ) -> None:
        if ttl_seconds < 60:
            raise ValueError("ttl_seconds must be at least 60")
        self._redis = redis
        self._ttl_seconds = ttl_seconds
        self._key_prefix = key_prefix
        self._active_key = f"{key_prefix}:active"

    async def create(self, job: IndexJob) -> IndexJob:
        async with self._redis.pipeline(transaction=True) as pipeline:
            pipeline.set(self._key(job.id), job.model_dump_json(), ex=self._ttl_seconds)
            pipeline.sadd(self._active_key, job.id)
            await pipeline.execute()
        return job.model_copy(deep=True)

    async def get(self, job_id: str) -> IndexJob | None:
        value = await self._redis.get(self._key(job_id))
        if value is None:
            return None
        await self._redis.expire(self._key(job_id), self._ttl_seconds)
        return IndexJob.model_validate_json(value)

    async def update(self, job_id: str, **changes: object) -> IndexJob:
        return await self._mutate(job_id, changes=changes, increments={})

    async def increment(self, job_id: str, **increments: int) -> IndexJob:
        return await self._mutate(job_id, changes={}, increments=increments)

    async def fail_interrupted(self, completed_at: datetime) -> int:
        job_ids = await self._redis.smembers(self._active_key)
        interrupted = 0
        for raw_job_id in job_ids:
            job_id = raw_job_id.decode() if isinstance(raw_job_id, bytes) else str(raw_job_id)
            job = await self.get(job_id)
            if job is None:
                await self._redis.srem(self._active_key, job_id)
            elif job.status in {IndexJobStatus.PENDING, IndexJobStatus.RUNNING}:
                await self.update(
                    job_id,
                    status=IndexJobStatus.FAILED,
                    completed_at=completed_at,
                    error="Indexer restarted before the job completed",
                )
                interrupted += 1
        return interrupted

    async def _mutate(
        self,
        job_id: str,
        *,
        changes: dict[str, object],
        increments: dict[str, int],
    ) -> IndexJob:
        key = self._key(job_id)
        while True:
            async with self._redis.pipeline(transaction=True) as pipeline:
                try:
                    await pipeline.watch(key)
                    value = await pipeline.get(key)
                    if value is None:
                        raise KeyError(job_id)
                    current = IndexJob.model_validate_json(value)
                    updated = current.model_copy(
                        update={
                            **changes,
                            **{
                                field: getattr(current, field) + increment
                                for field, increment in increments.items()
                            },
                        }
                    )
                    pipeline.multi()  # type: ignore[no-untyped-call]
                    pipeline.set(key, updated.model_dump_json(), ex=self._ttl_seconds)
                    if updated.status in {IndexJobStatus.COMPLETED, IndexJobStatus.FAILED}:
                        pipeline.srem(self._active_key, job_id)
                    await pipeline.execute()
                    return updated
                except WatchError:
                    continue

    def _key(self, job_id: str) -> str:
        return f"{self._key_prefix}:{job_id}"
