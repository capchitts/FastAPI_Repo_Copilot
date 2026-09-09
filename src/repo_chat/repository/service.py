"""Safe revision-pinned repository access and lexical discovery."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from repo_chat.analysis.source_store import SourceStore
from repo_chat.contracts.code_analysis import CodeSnippet
from repo_chat.contracts.evidence import SourceLocation
from repo_chat.contracts.repository import (
    RepositoryAcquisition,
    RepositoryMetadata,
    SourceSearchMatch,
    SourceSearchResult,
    SourceVerification,
)
from repo_chat.exceptions.base import EntityNotFoundError, IndexingError
from repo_chat.repository.git_acquisition import GitAcquisitionService


class RepositoryService:
    """Own all bounded reads from controlled repository snapshots."""

    def __init__(
        self,
        root: Path,
        *,
        maximum_file_bytes: int = 1_000_000,
        maximum_search_files: int = 5_000,
        git_acquisition: GitAcquisitionService | None = None,
    ) -> None:
        self._root = root.resolve()
        self._source_store = SourceStore(root, maximum_file_bytes=maximum_file_bytes)
        self._maximum_file_bytes = maximum_file_bytes
        self._maximum_search_files = maximum_search_files
        self._git_acquisition = git_acquisition

    async def acquire_repository(
        self,
        *,
        repository_id: str,
        remote_url: str,
        ref: str = "HEAD",
    ) -> RepositoryAcquisition:
        """Acquire a revision snapshot through the configured Git boundary."""
        if self._git_acquisition is None:
            raise IndexingError("Git repository acquisition is not configured")
        return await self._git_acquisition.acquire(
            repository_id=repository_id,
            remote_url=remote_url,
            ref=ref,
        )

    def get_metadata(self, repository_id: str, revision: str) -> RepositoryMetadata:
        """Return bounded filesystem metadata without exposing file contents."""
        root = self._resolve_snapshot(repository_id, revision)
        files = [path for path in root.rglob("*") if path.is_file() and not self._ignored(path)]
        acquisition = self._acquisition_metadata(root, revision)
        return RepositoryMetadata.model_validate(
            {
                "repository_id": repository_id,
                "revision": revision,
                "source_path": str(root),
                "file_count": len(files),
                "python_file_count": sum(path.suffix == ".py" for path in files),
                "total_bytes": sum(path.stat().st_size for path in files),
                **acquisition,
            }
        )

    def read_file(self, repository_id: str, revision: str, file_path: str) -> CodeSnippet:
        """Read one complete bounded UTF-8 source file."""
        return self._source_store.read_file(repository_id, revision, file_path)

    def get_code_snippet(
        self,
        *,
        repository_id: str,
        revision: str,
        file_path: str,
        start_line: int,
        end_line: int,
        context_lines: int = 3,
    ) -> CodeSnippet:
        """Read a bounded line range with source evidence."""
        return self._source_store.read_snippet(
            repository_id=repository_id,
            revision=revision,
            file_path=file_path,
            start_line=start_line,
            end_line=end_line,
            context_lines=context_lines,
        )

    def verify_source(
        self,
        *,
        repository_id: str,
        revision: str,
        file_path: str,
        expected_content_hash: str | None = None,
    ) -> SourceVerification:
        """Verify file containment, existence, and optionally its content hash."""
        path = self._source_store.resolve_file(repository_id, revision, file_path)
        raw = path.read_bytes()
        if len(raw) > self._maximum_file_bytes:
            raise IndexingError("Source file exceeds the configured size limit")
        content_hash = hashlib.sha256(raw).hexdigest()
        line_count = max(1, len(raw.decode("utf-8").splitlines()))
        return SourceVerification(
            repository_id=repository_id,
            revision=revision,
            file_path=file_path,
            exists=True,
            content_hash=content_hash,
            content_hash_matches=(content_hash == expected_content_hash)
            if expected_content_hash
            else None,
            location=SourceLocation(
                repository_id=repository_id,
                revision=revision,
                file_path=file_path,
                start_line=1,
                end_line=line_count,
                content_hash=content_hash,
            ),
        )

    def search_source(
        self,
        query: str,
        *,
        repository_id: str,
        revision: str,
        limit: int = 20,
    ) -> SourceSearchResult:
        """Perform bounded case-insensitive literal search over Python source."""
        if not query.strip():
            raise IndexingError("Search query cannot be empty")
        if not 1 <= limit <= 100:
            raise IndexingError("Search limit must be between 1 and 100")
        root = self._resolve_snapshot(repository_id, revision)
        files = [
            path
            for path in sorted(root.rglob("*.py"))
            if not self._ignored(path.relative_to(root))
        ]
        truncated = len(files) > self._maximum_search_files
        files = files[: self._maximum_search_files]
        matches: list[SourceSearchMatch] = []
        needle = query.casefold()
        for path in files:
            if path.stat().st_size > self._maximum_file_bytes:
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except UnicodeDecodeError:
                continue
            for line_number, line in enumerate(lines, start=1):
                if needle not in line.casefold():
                    continue
                relative = path.relative_to(root).as_posix()
                content_hash = hashlib.sha256(path.read_bytes()).hexdigest()
                location = SourceLocation(
                    repository_id=repository_id,
                    revision=revision,
                    file_path=relative,
                    start_line=line_number,
                    end_line=line_number,
                    content_hash=content_hash,
                )
                matches.append(SourceSearchMatch(location=location, excerpt=line.strip()))
                if len(matches) >= limit:
                    return SourceSearchResult(
                        query=query,
                        matches=matches,
                        searched_files=len(files),
                        truncated=True,
                    )
        return SourceSearchResult(
            query=query,
            matches=matches,
            searched_files=len(files),
            truncated=truncated,
        )

    def _resolve_snapshot(self, repository_id: str, revision: str) -> Path:
        for value, label in ((repository_id, "repository_id"), (revision, "revision")):
            candidate = Path(value)
            if candidate.is_absolute() or ".." in candidate.parts:
                raise IndexingError(f"{label} must be a safe relative path")
        candidates = (
            self._root / repository_id / "revisions" / revision,
            self._root / repository_id,
        )
        for candidate in candidates:
            resolved = candidate.resolve()
            try:
                resolved.relative_to(self._root)
            except ValueError as error:
                raise IndexingError("Repository path escaped the source root") from error
            if resolved.is_dir():
                return resolved
        raise EntityNotFoundError(
            "Repository revision was not found",
            details={"repository_id": repository_id, "revision": revision},
        )

    @staticmethod
    def _ignored(path: Path) -> bool:
        ignored = {".git", ".venv", "venv", "__pycache__", "node_modules"}
        return any(part in ignored or part.startswith(".") for part in path.parts)

    @staticmethod
    def _acquisition_metadata(root: Path, revision: str) -> dict[str, object]:
        marker = root / ".repo-chat-snapshot.json"
        if not marker.is_file():
            return {}
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict) or payload.get("revision") != revision:
            return {}
        return {
            key: payload[key]
            for key in ("remote_url", "requested_ref", "commit_timestamp", "captured_at")
            if key in payload
        }
