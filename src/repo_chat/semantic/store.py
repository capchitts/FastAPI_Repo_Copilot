"""Qdrant-backed semantic storage with local open-source embeddings."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any, Protocol
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, Field

from repo_chat.contracts.indexing import CodeEntity, EntityKind, ParsedFile

_SEARCHABLE_KINDS = frozenset({EntityKind.CLASS, EntityKind.FUNCTION, EntityKind.METHOD})


class SemanticDocument(BaseModel):
    """One revision-pinned code chunk stored in the vector index."""

    entity_id: str
    repository_id: str
    revision: str
    kind: EntityKind
    name: str
    qualified_name: str
    file_path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    content_hash: str
    text: str


class SemanticHit(SemanticDocument):
    """A semantic document with its cosine similarity score."""

    score: float


class SemanticStore(Protocol):
    """Boundary shared by the Indexer and Graph Query Agent."""

    async def index_file(self, parsed: ParsedFile, source: str) -> int: ...

    async def carry_forward_file(
        self,
        *,
        repository_id: str,
        source_revision: str,
        target_revision: str,
        file_path: str,
    ) -> int: ...

    async def delete_file(
        self, *, repository_id: str, revision: str, file_path: str
    ) -> None: ...

    async def search(
        self,
        query: str,
        *,
        repository_id: str,
        revision: str,
        limit: int,
    ) -> list[SemanticHit]: ...


class QdrantSemanticStore:
    """Store code chunks in Qdrant and embed them with FastEmbed locally."""

    def __init__(
        self,
        *,
        url: str,
        collection: str,
        model_name: str,
        api_key: str | None = None,
        maximum_characters: int = 4_000,
        cache_dir: str | None = None,
    ) -> None:
        self._url = url
        self._collection = collection
        self._model_name = model_name
        self._api_key = api_key
        self._maximum_characters = maximum_characters
        self._cache_dir = cache_dir
        self._client: Any = None
        self._model: Any = None
        self._ready = False
        self._lock = asyncio.Lock()

    async def index_file(self, parsed: ParsedFile, source: str) -> int:
        documents = self._documents(parsed, source)
        await self._ensure_ready()
        return await asyncio.to_thread(self._index_documents, documents, parsed)

    async def carry_forward_file(
        self,
        *,
        repository_id: str,
        source_revision: str,
        target_revision: str,
        file_path: str,
    ) -> int:
        await self._ensure_ready()
        return await asyncio.to_thread(
            self._carry_forward,
            repository_id,
            source_revision,
            target_revision,
            file_path,
        )

    async def search(
        self,
        query: str,
        *,
        repository_id: str,
        revision: str,
        limit: int,
    ) -> list[SemanticHit]:
        if not query.strip():
            return []
        await self._ensure_ready()
        return await asyncio.to_thread(
            self._search, query, repository_id, revision, limit
        )

    async def delete_file(
        self, *, repository_id: str, revision: str, file_path: str
    ) -> None:
        await self._ensure_ready()
        await asyncio.to_thread(self._delete_file, repository_id, revision, file_path)

    async def _ensure_ready(self) -> None:
        if self._ready:
            return
        async with self._lock:
            if not self._ready:
                await asyncio.to_thread(self._initialize)
                self._ready = True

    def _initialize(self) -> None:
        from fastembed import TextEmbedding
        from qdrant_client import QdrantClient, models

        self._client = QdrantClient(url=self._url, api_key=self._api_key)
        self._model = TextEmbedding(model_name=self._model_name, cache_dir=self._cache_dir)
        dimension = len(next(iter(self._model.embed(["dimension probe"]))))
        if not self._client.collection_exists(self._collection):
            self._client.create_collection(
                collection_name=self._collection,
                vectors_config=models.VectorParams(size=dimension, distance=models.Distance.COSINE),
            )

    def _index_documents(self, documents: list[SemanticDocument], parsed: ParsedFile) -> int:
        from qdrant_client import models

        assert self._client is not None and self._model is not None
        file_filter = self._filter(parsed.repository_id, parsed.revision, parsed.file_path)
        self._client.delete(self._collection, points_selector=file_filter, wait=True)
        if not documents:
            return 0
        vectors = list(self._model.embed([document.text for document in documents]))
        points = [
            models.PointStruct(
                id=self._point_id(document),
                vector=vector.tolist(),
                payload=document.model_dump(mode="json"),
            )
            for document, vector in zip(documents, vectors, strict=True)
        ]
        self._client.upsert(self._collection, points=points, wait=True)
        return len(points)

    def _carry_forward(
        self,
        repository_id: str,
        source_revision: str,
        target_revision: str,
        file_path: str,
    ) -> int:
        from qdrant_client import models

        assert self._client is not None
        points, _ = self._client.scroll(
            self._collection,
            scroll_filter=self._filter(repository_id, source_revision, file_path),
            limit=1_000,
            with_payload=True,
            with_vectors=True,
        )
        copied = []
        for point in points:
            payload = dict(point.payload or {})
            payload["revision"] = target_revision
            document = SemanticDocument.model_validate(payload)
            copied.append(
                models.PointStruct(
                    id=self._point_id(document), vector=point.vector, payload=payload
                )
            )
        if copied:
            self._client.upsert(self._collection, points=copied, wait=True)
        return len(copied)

    def _delete_file(self, repository_id: str, revision: str, file_path: str) -> None:
        assert self._client is not None
        self._client.delete(
            self._collection,
            points_selector=self._filter(repository_id, revision, file_path),
            wait=True,
        )

    def _search(
        self, query: str, repository_id: str, revision: str, limit: int
    ) -> list[SemanticHit]:
        assert self._client is not None and self._model is not None
        vector = next(iter(self._model.embed([query]))).tolist()
        response = self._client.query_points(
            collection_name=self._collection,
            query=vector,
            query_filter=self._filter(repository_id, revision),
            limit=limit,
            with_payload=True,
        )
        hits = []
        for point in response.points:
            payload = dict(point.payload or {})
            hits.append(SemanticHit.model_validate({**payload, "score": point.score}))
        return hits

    @staticmethod
    def _filter(repository_id: str, revision: str, file_path: str | None = None) -> Any:
        from qdrant_client import models

        conditions = [
            models.FieldCondition(
                key="repository_id", match=models.MatchValue(value=repository_id)
            ),
            models.FieldCondition(key="revision", match=models.MatchValue(value=revision)),
        ]
        if file_path is not None:
            conditions.append(
                models.FieldCondition(key="file_path", match=models.MatchValue(value=file_path))
            )
        return models.Filter(must=conditions)

    @staticmethod
    def _point_id(document: SemanticDocument) -> str:
        identity = f"{document.repository_id}:{document.revision}:{document.entity_id}"
        return str(uuid5(NAMESPACE_URL, identity))

    def _documents(self, parsed: ParsedFile, source: str) -> list[SemanticDocument]:
        lines = source.splitlines()
        return [
            self._document(parsed, entity, lines)
            for entity in parsed.entities
            if entity.kind in _SEARCHABLE_KINDS
        ]

    def _document(
        self, parsed: ParsedFile, entity: CodeEntity, lines: Sequence[str]
    ) -> SemanticDocument:
        snippet = "\n".join(lines[entity.start_line - 1 : entity.end_line])
        text = (
            f"{entity.kind.value} {entity.qualified_name}\n"
            f"File: {entity.file_path}\n{snippet}"
        )[: self._maximum_characters]
        return SemanticDocument(
            entity_id=entity.id,
            repository_id=parsed.repository_id,
            revision=parsed.revision,
            kind=entity.kind,
            name=entity.name,
            qualified_name=entity.qualified_name,
            file_path=entity.file_path,
            start_line=entity.start_line,
            end_line=entity.end_line,
            content_hash=entity.content_hash,
            text=text,
        )
