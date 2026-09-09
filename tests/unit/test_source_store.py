from pathlib import Path

import pytest

from repo_chat.analysis.source_store import SourceStore
from repo_chat.contracts.evidence import EvidenceKind
from repo_chat.exceptions.base import EntityNotFoundError, IndexingError


def create_source_root(tmp_path: Path) -> Path:
    root = tmp_path / "repositories"
    checkout = root / "fastapi"
    checkout.mkdir(parents=True)
    (checkout / "module.py").write_text("line_one = 1\nline_two = 2\nline_three = 3\n")
    return root


def test_reads_bounded_snippet_with_context_and_evidence(tmp_path: Path) -> None:
    store = SourceStore(create_source_root(tmp_path))

    snippet = store.read_snippet(
        repository_id="fastapi",
        revision="abc123",
        file_path="module.py",
        start_line=2,
        end_line=2,
        context_lines=1,
    )

    assert snippet.start_line == 1
    assert snippet.end_line == 3
    assert snippet.code == "line_one = 1\nline_two = 2\nline_three = 3"
    assert snippet.evidence.kind is EvidenceKind.SOURCE
    assert snippet.evidence.location is not None
    assert snippet.evidence.location.content_hash == snippet.content_hash


def test_revision_snapshot_takes_precedence_over_active_checkout(tmp_path: Path) -> None:
    root = create_source_root(tmp_path)
    snapshot = root / "fastapi" / "revisions" / "abc123"
    snapshot.mkdir(parents=True)
    (snapshot / "module.py").write_text("snapshot = True\n")
    store = SourceStore(root)

    snippet = store.read_file("fastapi", "abc123", "module.py")

    assert snippet.code == "snapshot = True"


@pytest.mark.parametrize(
    ("repository_id", "revision", "file_path"),
    [
        ("../outside", "abc123", "module.py"),
        ("fastapi", "../outside", "module.py"),
        ("fastapi", "abc123", "../outside.py"),
        ("fastapi", "abc123", "/etc/passwd"),
    ],
)
def test_rejects_path_traversal(
    tmp_path: Path,
    repository_id: str,
    revision: str,
    file_path: str,
) -> None:
    store = SourceStore(create_source_root(tmp_path))

    with pytest.raises(IndexingError, match="safe relative path"):
        store.read_file(repository_id, revision, file_path)


def test_rejects_missing_file_and_invalid_line_ranges(tmp_path: Path) -> None:
    store = SourceStore(create_source_root(tmp_path))

    with pytest.raises(EntityNotFoundError):
        store.read_file("fastapi", "abc123", "missing.py")
    with pytest.raises(IndexingError, match="line range"):
        store.read_snippet(
            repository_id="fastapi",
            revision="abc123",
            file_path="module.py",
            start_line=3,
            end_line=2,
        )
    with pytest.raises(IndexingError, match="exceeds"):
        store.read_snippet(
            repository_id="fastapi",
            revision="abc123",
            file_path="module.py",
            start_line=50,
            end_line=50,
        )


def test_enforces_file_size_limit(tmp_path: Path) -> None:
    store = SourceStore(create_source_root(tmp_path), maximum_file_bytes=4)

    with pytest.raises(IndexingError, match="size limit"):
        store.read_file("fastapi", "abc123", "module.py")


def test_rejects_invalid_store_and_context_limits(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="positive"):
        SourceStore(tmp_path, maximum_file_bytes=0)
    store = SourceStore(create_source_root(tmp_path))
    with pytest.raises(IndexingError, match="Context"):
        store.read_snippet(
            repository_id="fastapi",
            revision="abc123",
            file_path="module.py",
            start_line=1,
            end_line=1,
            context_lines=51,
        )
