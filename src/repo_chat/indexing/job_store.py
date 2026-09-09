"""Index-job persistence interfaces and an in-process implementation."""

import asyncio
from datetime import datetime
from typing import Protocol

from repo_chat.contracts.indexing import IndexJob, IndexJobStatus


class IndexJobStore(Protocol):
    """Storage boundary for durable indexing progress."""

    async def create(self, job: IndexJob) -> IndexJob: ...

    async def get(self, job_id: str) -> IndexJob | None: ...

    async def update(self, job_id: str, **changes: object) -> IndexJob: ...

    async def increment(self, job_id: str, **increments: int) -> IndexJob: ...

    async def fail_interrupted(self, completed_at: datetime) -> int: ...


class InMemoryIndexJobStore:
    """Concurrency-safe job storage for tests and lightweight local use."""

    def __init__(self) -> None:
        self._jobs: dict[str, IndexJob] = {}
        self._lock = asyncio.Lock()

    async def create(self, job: IndexJob) -> IndexJob:
        async with self._lock:
            self._jobs[job.id] = job.model_copy(deep=True)
            return job.model_copy(deep=True)

    async def get(self, job_id: str) -> IndexJob | None:
        async with self._lock:
            job = self._jobs.get(job_id)
            return job.model_copy(deep=True) if job else None

    async def update(self, job_id: str, **changes: object) -> IndexJob:
        async with self._lock:
            job = self._jobs[job_id].model_copy(update=changes)
            self._jobs[job_id] = job
            return job.model_copy(deep=True)

    async def increment(self, job_id: str, **increments: int) -> IndexJob:
        async with self._lock:
            current = self._jobs[job_id]
            changes = {
                field: getattr(current, field) + increment
                for field, increment in increments.items()
            }
            job = current.model_copy(update=changes)
            self._jobs[job_id] = job
            return job.model_copy(deep=True)

    async def fail_interrupted(self, completed_at: datetime) -> int:
        async with self._lock:
            interrupted = 0
            for job_id, job in list(self._jobs.items()):
                if job.status in {IndexJobStatus.PENDING, IndexJobStatus.RUNNING}:
                    self._jobs[job_id] = job.model_copy(
                        update={
                            "status": IndexJobStatus.FAILED,
                            "completed_at": completed_at,
                            "error": "Indexer restarted before the job completed",
                        }
                    )
                    interrupted += 1
            return interrupted
