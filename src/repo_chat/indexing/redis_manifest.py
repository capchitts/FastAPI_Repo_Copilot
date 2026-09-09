"""Redis-backed incremental-index manifest."""

from redis.asyncio import Redis

from repo_chat.indexing.manifest import IndexedFileManifest, IndexManifestStore


class RedisIndexManifestStore(IndexManifestStore):
    """Persist per-file hashes and source revisions in repository hashes."""

    def __init__(self, redis: Redis, *, key_prefix: str = "repo-chat:index-manifest") -> None:
        self._redis = redis
        self._key_prefix = key_prefix

    async def get(self, repository_id: str, file_path: str) -> IndexedFileManifest | None:
        value = await self._redis.hget(self._key(repository_id), file_path)
        return IndexedFileManifest.model_validate_json(value) if value is not None else None

    async def put(self, repository_id: str, file_path: str, manifest: IndexedFileManifest) -> None:
        await self._redis.hset(
            self._key(repository_id),
            mapping={file_path: manifest.model_dump_json()},
        )

    async def list(self, repository_id: str) -> dict[str, IndexedFileManifest]:
        values = await self._redis.hgetall(self._key(repository_id))
        return {
            self._decode(file_path): IndexedFileManifest.model_validate_json(value)
            for file_path, value in values.items()
        }

    async def delete(self, repository_id: str, file_path: str) -> None:
        await self._redis.hdel(self._key(repository_id), file_path)

    def _key(self, repository_id: str) -> str:
        return f"{self._key_prefix}:{repository_id}"

    @staticmethod
    def _decode(value: str | bytes) -> str:
        return value.decode("utf-8") if isinstance(value, bytes) else value
