"""Persistent incremental-index manifest contracts and local implementation."""

import asyncio
from typing import Protocol

from pydantic import BaseModel


class IndexedFileManifest(BaseModel):
    """Last successfully indexed content and revision for one repository file."""

    content_hash: str
    revision: str
    analysis_version: int = 1


class IndexManifestStore(Protocol):
    """Storage boundary for incremental file state."""

    async def get(self, repository_id: str, file_path: str) -> IndexedFileManifest | None: ...

    async def put(
        self, repository_id: str, file_path: str, manifest: IndexedFileManifest
    ) -> None: ...

    async def list(self, repository_id: str) -> dict[str, IndexedFileManifest]: ...

    async def delete(self, repository_id: str, file_path: str) -> None: ...


class InMemoryIndexManifestStore:
    """Concurrency-safe manifest storage for tests."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str], IndexedFileManifest] = {}
        self._lock = asyncio.Lock()

    async def get(self, repository_id: str, file_path: str) -> IndexedFileManifest | None:
        async with self._lock:
            item = self._items.get((repository_id, file_path))
            return item.model_copy(deep=True) if item else None

    async def put(self, repository_id: str, file_path: str, manifest: IndexedFileManifest) -> None:
        async with self._lock:
            self._items[(repository_id, file_path)] = manifest.model_copy(deep=True)

    async def list(self, repository_id: str) -> dict[str, IndexedFileManifest]:
        async with self._lock:
            return {
                file_path: manifest.model_copy(deep=True)
                for (stored_repository_id, file_path), manifest in self._items.items()
                if stored_repository_id == repository_id
            }

    async def delete(self, repository_id: str, file_path: str) -> None:
        async with self._lock:
            self._items.pop((repository_id, file_path), None)
