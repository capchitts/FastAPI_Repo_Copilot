"""Application service behind the Indexer MCP tools."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import structlog

from repo_chat.contracts.indexing import (
    CodeEntity,
    IndexFileResult,
    IndexJob,
    IndexJobStatus,
    IndexMode,
    ParsedFile,
)
from repo_chat.exceptions.base import EntityNotFoundError, IndexingError
from repo_chat.graph.repository import GraphRepository
from repo_chat.graph.resolution import resolve_repository_relationships
from repo_chat.indexing.ast_parser import parse_python_file, parse_python_source
from repo_chat.indexing.coordination import IndexCoordination, InMemoryIndexCoordination
from repo_chat.indexing.job_store import IndexJobStore, InMemoryIndexJobStore
from repo_chat.indexing.manifest import (
    IndexedFileManifest,
    IndexManifestStore,
    InMemoryIndexManifestStore,
)
from repo_chat.semantic.store import SemanticStore

SchemaInitializer = Callable[[], Awaitable[None]]
logger = structlog.get_logger(__name__)
ANALYSIS_VERSION = 2


class IndexerService:
    """Parse and persist Python repositories while tracking asynchronous jobs."""

    def __init__(
        self,
        graph_repository: GraphRepository,
        *,
        repository_root: Path,
        initialize_schema: SchemaInitializer | None = None,
        job_store: IndexJobStore | None = None,
        manifest_store: IndexManifestStore | None = None,
        semantic_store: SemanticStore | None = None,
        coordination: IndexCoordination | None = None,
    ) -> None:
        self._graph_repository = graph_repository
        self._repository_root = repository_root.resolve()
        self._initialize_schema = initialize_schema
        self._schema_ready = initialize_schema is None
        self._schema_lock = asyncio.Lock()
        self._job_store = job_store or InMemoryIndexJobStore()
        self._manifest_store = manifest_store or InMemoryIndexManifestStore()
        self._semantic_store = semantic_store
        self._coordination = coordination or InMemoryIndexCoordination()
        self._job_store_ready = False
        self._job_store_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()

    async def ensure_schema(self) -> None:
        """Initialize graph constraints once for this service process."""
        if self._schema_ready:
            return
        async with self._schema_lock:
            if not self._schema_ready:
                assert self._initialize_schema is not None
                await self._initialize_schema()
                self._schema_ready = True

    def parse_python_ast(
        self,
        code: str,
        *,
        repository_id: str,
        revision: str,
        file_path: str,
    ) -> ParsedFile:
        """Parse supplied Python text without executing it."""
        return parse_python_source(
            code,
            repository_id=repository_id,
            revision=revision,
            file_path=self._validate_relative_path(file_path),
        )

    def extract_entities(
        self,
        code: str,
        *,
        repository_id: str,
        revision: str,
        file_path: str,
    ) -> list[CodeEntity]:
        """Extract normalized source entities from supplied Python text."""
        return self.parse_python_ast(
            code,
            repository_id=repository_id,
            revision=revision,
            file_path=file_path,
        ).entities

    async def index_file(
        self,
        file_path: str,
        *,
        repository_path: str,
        repository_id: str,
        revision: str,
    ) -> IndexFileResult:
        """Parse and persist one Python file under an allowed repository path."""
        await self.ensure_schema()
        root = self._resolve_repository(repository_path)
        relative_file = self._validate_relative_path(file_path)
        path = root / relative_file
        if path.suffix != ".py":
            raise IndexingError("Only Python files can be indexed")
        if not path.is_file():
            raise EntityNotFoundError(
                "Source file was not found",
                details={"file_path": relative_file},
            )
        parsed = parse_python_file(
            path,
            repository_root=root,
            repository_id=repository_id,
            revision=revision,
        )
        if any(issue.fatal for issue in parsed.issues):
            raise IndexingError(
                "Source file contains invalid Python syntax",
                details={"file_path": relative_file},
            )
        await self._graph_repository.write_parsed_file(parsed)
        semantic_chunks = 0
        if self._semantic_store is not None:
            semantic_chunks = await self._semantic_store.index_file(
                parsed, path.read_text(encoding="utf-8")
            )
        await self._manifest_store.put(
            repository_id,
            parsed.file_path,
            IndexedFileManifest(
                content_hash=parsed.content_hash,
                revision=revision,
                analysis_version=ANALYSIS_VERSION,
            ),
        )
        result = self._file_result(parsed)
        return result.model_copy(update={"semantic_chunk_count": semantic_chunks})

    async def index_repository(
        self,
        repository_path: str,
        *,
        repository_id: str,
        revision: str,
        mode: IndexMode = IndexMode.FULL,
    ) -> IndexJob:
        """Start repository indexing and return its job immediately."""
        await self._ensure_job_store_ready()
        root = self._resolve_repository(repository_path)
        identity = self._index_identity(repository_id, revision, mode, root)
        proposed_job_id = str(uuid4())
        claimed_job_id = await self._coordination.claim_idempotency(
            identity, proposed_job_id
        )
        if claimed_job_id != proposed_job_id:
            for _ in range(20):
                existing = await self._job_store.get(claimed_job_id)
                if existing is not None:
                    return existing
                await asyncio.sleep(0.01)
            raise IndexingError("Idempotent indexing job is still being registered")
        job = IndexJob(
            id=proposed_job_id,
            repository_id=repository_id,
            repository_path=str(root),
            revision=revision,
            mode=mode,
            created_at=datetime.now(UTC),
        )
        await self._job_store.create(job)
        task = asyncio.create_task(self._run_repository_job(job.id, root))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job.model_copy(deep=True)

    async def get_index_status(self, job_id: str) -> IndexJob:
        """Return a snapshot of an indexing job."""
        await self._ensure_job_store_ready()
        job = await self._job_store.get(job_id)
        if job is None:
            raise EntityNotFoundError(
                "Indexing job was not found",
                details={"job_id": job_id},
            )
        return job

    async def wait_for_jobs(self) -> None:
        """Wait for active jobs; intended for graceful service shutdown and tests."""
        tasks = tuple(self._tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_repository_job(self, job_id: str, root: Path) -> None:
        initial_job = await self.get_index_status(job_id)
        lock_token = str(uuid4())
        acquired = await self._coordination.acquire_repository_lock(
            initial_job.repository_id, lock_token
        )
        if not acquired:
            await self._update_job(
                job_id,
                status=IndexJobStatus.FAILED,
                error="Another indexing job holds the repository lock",
                completed_at=datetime.now(UTC),
            )
            return
        try:
            await self._update_job(
                job_id,
                status=IndexJobStatus.RUNNING,
                started_at=datetime.now(UTC),
            )
            await self.ensure_schema()
            files = self._discover_python_files(root)
            await self._update_job(job_id, discovered_files=len(files))
            job = await self.get_index_status(job_id)
            parsed_files = resolve_repository_relationships(
                [
                    parse_python_file(
                        path,
                        repository_root=root,
                        repository_id=job.repository_id,
                        revision=job.revision,
                    )
                    for path in files
                ]
            )
            parsed_by_path = {parsed.file_path: parsed for parsed in parsed_files}
            previous_manifests = await self._manifest_store.list(job.repository_id)
            current_paths = {path.relative_to(root).as_posix() for path in files}
            removed_paths = set(previous_manifests) - current_paths
            removed_by_hash: dict[str, list[str]] = {}
            for removed_path in removed_paths:
                manifest = previous_manifests[removed_path]
                removed_by_hash.setdefault(manifest.content_hash, []).append(removed_path)
            renamed_paths: set[str] = set()
            for path in files:
                job = await self.get_index_status(job_id)
                relative_path = path.relative_to(root).as_posix()
                parsed = parsed_by_path[relative_path]
                previous = await self._manifest_store.get(job.repository_id, relative_path)
                rename_candidates = removed_by_hash.get(parsed.content_hash, [])
                if previous is None and rename_candidates:
                    renamed_paths.add(rename_candidates.pop())
                if (
                    job.mode is IndexMode.INCREMENTAL
                    and previous is not None
                    and previous.content_hash == parsed.content_hash
                    and previous.analysis_version == ANALYSIS_VERSION
                ):
                    await self._graph_repository.carry_forward_file(
                        repository_id=job.repository_id,
                        source_revision=previous.revision,
                        target_revision=job.revision,
                        file_path=relative_path,
                        content_hash=parsed.content_hash,
                    )
                    semantic_chunks = 0
                    if self._semantic_store is not None:
                        semantic_chunks = await self._semantic_store.carry_forward_file(
                            repository_id=job.repository_id,
                            source_revision=previous.revision,
                            target_revision=job.revision,
                            file_path=relative_path,
                        )
                        if semantic_chunks == 0:
                            semantic_chunks = await self._semantic_store.index_file(
                                parsed, path.read_text(encoding="utf-8")
                            )
                        # Existing manifests can predate semantic indexing. Backfill rather
                        # than silently carrying an empty vector set into the new revision.
                        if semantic_chunks == 0:
                            semantic_chunks = await self._semantic_store.index_file(
                                parsed, path.read_text(encoding="utf-8")
                            )
                    await self._manifest_store.put(
                        job.repository_id,
                        relative_path,
                        IndexedFileManifest(
                            content_hash=parsed.content_hash,
                            revision=job.revision,
                            analysis_version=ANALYSIS_VERSION,
                        ),
                    )
                    await self._increment_job(
                        job_id,
                        skipped_files=1,
                        processed_files=1,
                        semantic_chunk_count=semantic_chunks,
                    )
                    continue
                if any(issue.fatal for issue in parsed.issues):
                    await self._increment_job(job_id, failed_files=1, processed_files=1)
                    continue
                await self._graph_repository.write_parsed_file(parsed)
                semantic_chunks = 0
                if self._semantic_store is not None:
                    if previous is not None and previous.content_hash == parsed.content_hash:
                        semantic_chunks = await self._semantic_store.carry_forward_file(
                            repository_id=job.repository_id,
                            source_revision=previous.revision,
                            target_revision=job.revision,
                            file_path=relative_path,
                        )
                    if semantic_chunks == 0:
                        semantic_chunks = await self._semantic_store.index_file(
                            parsed, path.read_text(encoding="utf-8")
                        )
                await self._manifest_store.put(
                    job.repository_id,
                    relative_path,
                    IndexedFileManifest(
                        content_hash=parsed.content_hash,
                        revision=job.revision,
                        analysis_version=ANALYSIS_VERSION,
                    ),
                )
                await self._increment_job(
                    job_id,
                    processed_files=1,
                    entity_count=len(parsed.entities),
                    relationship_count=len(parsed.relationships),
                    semantic_chunk_count=semantic_chunks,
                )
                await asyncio.sleep(0)
            await self._graph_repository.write_repository_relationships(parsed_files)
            final_job = await self.get_index_status(job_id)
            status = (
                IndexJobStatus.FAILED
                if final_job.failed_files
                else IndexJobStatus.COMPLETED
            )
            completed_at = datetime.now(UTC)
            if status is IndexJobStatus.COMPLETED:
                for removed_path in sorted(removed_paths):
                    await self._graph_repository.remove_file_from_revision(
                        repository_id=final_job.repository_id,
                        revision=final_job.revision,
                        file_path=removed_path,
                    )
                    if self._semantic_store is not None:
                        await self._semantic_store.delete_file(
                            repository_id=final_job.repository_id,
                            revision=final_job.revision,
                            file_path=removed_path,
                        )
                    await self._manifest_store.delete(
                        final_job.repository_id, removed_path
                    )
                await self._update_job(
                    job_id,
                    deleted_file_count=len(removed_paths - renamed_paths),
                    renamed_file_count=len(renamed_paths),
                )
                acquisition = self._snapshot_acquisition_metadata(root, final_job.revision)
                remote_url_value = acquisition.get("remote_url")
                commit_timestamp_value = acquisition.get("commit_timestamp")
                requested_ref_value = acquisition.get("requested_ref")
                await self._graph_repository.record_revision_snapshot(
                    repository_id=final_job.repository_id,
                    revision=final_job.revision,
                    repository_source=final_job.repository_path,
                    captured_at=final_job.created_at,
                    indexed_at=completed_at,
                    index_job_id=final_job.id,
                    remote_url=remote_url_value
                    if isinstance(remote_url_value, str)
                    else None,
                    commit_timestamp=commit_timestamp_value
                    if isinstance(commit_timestamp_value, datetime)
                    else None,
                    branch_or_tag=requested_ref_value
                    if isinstance(requested_ref_value, str)
                    else None,
                )
            await self._update_job(
                job_id,
                status=status,
                completed_at=completed_at,
                error="One or more discovered Python files failed to parse"
                if status is IndexJobStatus.FAILED
                else None,
            )
        except Exception as error:
            logger.exception(
                "repository_index_failed",
                job_id=job_id,
                error_type=type(error).__name__,
                reason=str(error)[:500],
            )
            await self._update_job(
                job_id,
                status=IndexJobStatus.FAILED,
                error=str(error),
                completed_at=datetime.now(UTC),
            )
        finally:
            await self._coordination.release_repository_lock(
                initial_job.repository_id, lock_token
            )

    async def _update_job(self, job_id: str, **changes: object) -> None:
        await self._job_store.update(job_id, **changes)

    async def _increment_job(self, job_id: str, **increments: int) -> None:
        await self._job_store.increment(job_id, **increments)

    async def _ensure_job_store_ready(self) -> None:
        """Mark tasks left by a prior process as interrupted exactly once."""
        if self._job_store_ready:
            return
        async with self._job_store_lock:
            if not self._job_store_ready:
                await self._job_store.fail_interrupted(datetime.now(UTC))
                self._job_store_ready = True

    def _resolve_repository(self, repository_path: str) -> Path:
        candidate = Path(repository_path)
        resolved = (
            candidate.resolve()
            if candidate.is_absolute()
            else (self._repository_root / candidate).resolve()
        )
        try:
            resolved.relative_to(self._repository_root)
        except ValueError as error:
            raise IndexingError("Repository path is outside the configured root") from error
        if not resolved.is_dir():
            raise EntityNotFoundError(
                "Repository directory was not found",
                details={"repository_path": repository_path},
            )
        return resolved

    @staticmethod
    def _validate_relative_path(file_path: str) -> str:
        path = Path(file_path)
        if path.is_absolute() or ".." in path.parts:
            raise IndexingError("File path must be repository-relative")
        return path.as_posix()

    @staticmethod
    def _is_ignored(path: Path) -> bool:
        ignored_parts = {".git", ".venv", "venv", "__pycache__", "node_modules"}
        return any(part in ignored_parts or part.startswith(".") for part in path.parts)

    @classmethod
    def _discover_python_files(cls, root: Path) -> list[Path]:
        return sorted(
            path for path in root.rglob("*.py") if not cls._is_ignored(path.relative_to(root))
        )

    @staticmethod
    def _file_result(parsed: ParsedFile) -> IndexFileResult:
        return IndexFileResult(
            file_path=parsed.file_path,
            repository_id=parsed.repository_id,
            revision=parsed.revision,
            entity_count=len(parsed.entities),
            relationship_count=len(parsed.relationships),
            issue_count=len(parsed.issues),
            content_hash=parsed.content_hash,
        )

    @staticmethod
    def _index_identity(
        repository_id: str, revision: str, mode: IndexMode, root: Path
    ) -> str:
        import hashlib

        value = f"{repository_id}\0{revision}\0{mode.value}\0{root}"
        return hashlib.sha256(value.encode()).hexdigest()

    @staticmethod
    def _snapshot_acquisition_metadata(root: Path, revision: str) -> dict[str, object]:
        """Read trusted acquisition metadata only when it matches this snapshot."""
        marker = root / ".repo-chat-snapshot.json"
        if not marker.is_file():
            return {}
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict) or payload.get("revision") != revision:
            return {}
        commit_timestamp = payload.get("commit_timestamp")
        if isinstance(commit_timestamp, str):
            try:
                commit_timestamp = datetime.fromisoformat(commit_timestamp)
            except ValueError:
                commit_timestamp = None
        return {
            "remote_url": payload.get("remote_url")
            if isinstance(payload.get("remote_url"), str)
            else None,
            "requested_ref": payload.get("requested_ref")
            if isinstance(payload.get("requested_ref"), str)
            else None,
            "commit_timestamp": commit_timestamp
            if isinstance(commit_timestamp, datetime)
            else None,
        }
