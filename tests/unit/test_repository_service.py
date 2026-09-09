from pathlib import Path

import pytest

from repo_chat.exceptions.base import IndexingError
from repo_chat.repository.service import RepositoryService


def create_repository(tmp_path: Path) -> RepositoryService:
    root = tmp_path / "repositories"
    repository = root / "fixture"
    repository.mkdir(parents=True)
    (repository / "service.py").write_text(
        "def resolve_dependency(value: str) -> str:\n    return value\n",
        encoding="utf-8",
    )
    return RepositoryService(root)


def test_repository_metadata_read_verify_and_search(tmp_path: Path) -> None:
    service = create_repository(tmp_path)

    metadata = service.get_metadata("fixture", "abc123")
    snippet = service.get_code_snippet(
        repository_id="fixture",
        revision="abc123",
        file_path="service.py",
        start_line=1,
        end_line=1,
    )
    verification = service.verify_source(
        repository_id="fixture",
        revision="abc123",
        file_path="service.py",
        expected_content_hash=snippet.content_hash,
    )
    search = service.search_source(
        "resolve_dependency",
        repository_id="fixture",
        revision="abc123",
    )

    assert metadata.python_file_count == 1
    assert snippet.code.startswith("def resolve_dependency")
    assert verification.content_hash_matches is True
    assert search.matches[0].location.file_path == "service.py"


def test_repository_agent_rejects_traversal_and_unbounded_search(tmp_path: Path) -> None:
    service = create_repository(tmp_path)

    with pytest.raises(IndexingError, match="safe relative"):
        service.read_file("fixture", "abc123", "../secret")
    with pytest.raises(IndexingError, match="limit"):
        service.search_source("value", repository_id="fixture", revision="abc123", limit=101)
