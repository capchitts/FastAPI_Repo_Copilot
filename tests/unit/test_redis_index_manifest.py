import pytest

from repo_chat.indexing.manifest import IndexedFileManifest
from repo_chat.indexing.redis_manifest import RedisIndexManifestStore


class FakeRedis:
    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}

    async def hget(self, key: str, field: str) -> str | None:
        return self.hashes.get(key, {}).get(field)

    async def hset(self, key: str, *, mapping: dict[str, str]) -> None:
        self.hashes.setdefault(key, {}).update(mapping)

    async def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    async def hdel(self, key: str, field: str) -> None:
        self.hashes.get(key, {}).pop(field, None)


@pytest.mark.asyncio
async def test_redis_manifest_survives_store_recreation() -> None:
    redis = FakeRedis()
    first = RedisIndexManifestStore(redis)  # type: ignore[arg-type]
    await first.put(
        "fastapi",
        "fastapi/routing.py",
        IndexedFileManifest(content_hash="hash-1", revision="revision-1"),
    )

    restarted = RedisIndexManifestStore(redis)  # type: ignore[arg-type]
    manifest = await restarted.get("fastapi", "fastapi/routing.py")

    assert manifest == IndexedFileManifest(content_hash="hash-1", revision="revision-1")
    assert await restarted.get("fastapi", "missing.py") is None

    listed = await restarted.list("fastapi")
    assert set(listed) == {"fastapi/routing.py"}

    await restarted.delete("fastapi", "fastapi/routing.py")
    assert await restarted.list("fastapi") == {}
