"""Safe, read-only access to revision-pinned repository source."""

import hashlib
from pathlib import Path

from repo_chat.contracts.code_analysis import CodeSnippet
from repo_chat.contracts.evidence import Evidence, EvidenceKind, SourceLocation
from repo_chat.exceptions.base import EntityNotFoundError, IndexingError


class SourceStore:
    """Read bounded UTF-8 source files below a configured repository root."""

    def __init__(self, root: Path, *, maximum_file_bytes: int = 1_000_000) -> None:
        self._root = root.resolve()
        if maximum_file_bytes < 1:
            raise ValueError("maximum_file_bytes must be positive")
        self._maximum_file_bytes = maximum_file_bytes

    def read_snippet(
        self,
        *,
        repository_id: str,
        revision: str,
        file_path: str,
        start_line: int,
        end_line: int,
        context_lines: int = 0,
    ) -> CodeSnippet:
        """Return an exact source range with optional bounded context."""
        if start_line < 1 or end_line < start_line:
            raise IndexingError("Invalid source line range")
        if not 0 <= context_lines <= 50:
            raise IndexingError("Context lines must be between 0 and 50")
        source_path = self.resolve_file(repository_id, revision, file_path)
        size = source_path.stat().st_size
        if size > self._maximum_file_bytes:
            raise IndexingError("Source file exceeds the configured size limit")
        raw = source_path.read_bytes()
        try:
            source = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise IndexingError("Source file is not valid UTF-8") from error
        lines = source.splitlines()
        if start_line > max(1, len(lines)):
            raise IndexingError("Source start line exceeds file length")
        actual_start = max(1, start_line - context_lines)
        actual_end = min(max(1, len(lines)), end_line + context_lines)
        code = "\n".join(lines[actual_start - 1 : actual_end])
        content_hash = hashlib.sha256(raw).hexdigest()
        location = SourceLocation(
            repository_id=repository_id,
            revision=revision,
            file_path=file_path,
            start_line=actual_start,
            end_line=actual_end,
            content_hash=content_hash,
        )
        return CodeSnippet(
            repository_id=repository_id,
            revision=revision,
            file_path=file_path,
            start_line=actual_start,
            end_line=actual_end,
            code=code,
            content_hash=content_hash,
            evidence=Evidence(kind=EvidenceKind.SOURCE, location=location, excerpt=code),
        )

    def read_file(self, repository_id: str, revision: str, file_path: str) -> CodeSnippet:
        """Read a complete source file within the configured size limit."""
        path = self.resolve_file(repository_id, revision, file_path)
        if path.stat().st_size > self._maximum_file_bytes:
            raise IndexingError("Source file exceeds the configured size limit")
        try:
            line_count = max(1, len(path.read_text(encoding="utf-8").splitlines()))
        except UnicodeDecodeError as error:
            raise IndexingError("Source file is not valid UTF-8") from error
        return self.read_snippet(
            repository_id=repository_id,
            revision=revision,
            file_path=file_path,
            start_line=1,
            end_line=line_count,
        )

    def resolve_file(self, repository_id: str, revision: str, file_path: str) -> Path:
        """Resolve snapshot or active-checkout layouts without allowing traversal."""
        for value, label in (
            (repository_id, "repository_id"),
            (revision, "revision"),
            (file_path, "file_path"),
        ):
            path_value = Path(value)
            if path_value.is_absolute() or ".." in path_value.parts:
                raise IndexingError(f"{label} must be a safe relative path")
        candidates = (
            self._root / repository_id / "revisions" / revision / file_path,
            self._root / repository_id / file_path,
        )
        for candidate in candidates:
            resolved = candidate.resolve()
            try:
                resolved.relative_to(self._root)
            except ValueError as error:
                raise IndexingError("Resolved source path escaped the source root") from error
            if resolved.is_file():
                return resolved
        raise EntityNotFoundError("Source file was not found", details={"file_path": file_path})
