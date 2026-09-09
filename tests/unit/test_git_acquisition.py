import os
import subprocess
from pathlib import Path

import pytest

from repo_chat.exceptions.base import IndexingError
from repo_chat.repository.git_acquisition import GitAcquisitionService


class LocalGitAcquisitionService(GitAcquisitionService):
    """Permit a test-owned local origin while production remains HTTPS-only."""

    def _safe_remote(self, remote_url: str) -> str:
        return remote_url

    async def _git(self, *arguments: str) -> str:
        """Run directly because this sandbox cannot spawn from worker threads."""
        completed = self._run_git(arguments, dict(os.environ))
        if completed.returncode != 0:
            raise AssertionError(completed.stderr.decode())
        return completed.stdout.decode()


def git(*arguments: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@pytest.mark.asyncio
async def test_acquire_materializes_and_reuses_commit_snapshot(tmp_path: Path) -> None:
    origin = tmp_path / "origin"
    origin.mkdir()
    git("init", "-b", "main", cwd=origin)
    git("config", "user.name", "Test User", cwd=origin)
    git("config", "user.email", "test@example.com", cwd=origin)
    (origin / "service.py").write_text("VALUE = 1\n", encoding="utf-8")
    git("add", "service.py", cwd=origin)
    git("commit", "-m", "fixture", cwd=origin)
    expected_revision = git("rev-parse", "HEAD", cwd=origin)
    snapshots = tmp_path / "repositories"
    service = LocalGitAcquisitionService(
        snapshots,
        allowed_hosts=set(),
        maximum_snapshot_bytes=10_000,
    )

    acquired = await service.acquire(
        repository_id="fixture",
        remote_url=str(origin),
        ref="main",
    )
    repeated = await service.acquire(
        repository_id="fixture",
        remote_url=str(origin),
        ref="main",
    )

    snapshot = Path(acquired.repository_path)
    assert acquired.revision == expected_revision
    assert (snapshot / "service.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert (snapshot / ".repo-chat-snapshot.json").is_file()
    assert not (snapshot / ".git").exists()
    assert repeated.reused is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "remote_url",
    [
        "http://github.com/example/project.git",
        "https://user:secret@github.com/example/project.git",
        "https://127.0.0.1/project.git",
        "file:///tmp/project",
    ],
)
async def test_acquire_rejects_unsafe_remotes(tmp_path: Path, remote_url: str) -> None:
    service = GitAcquisitionService(tmp_path, allowed_hosts={"github.com"})

    with pytest.raises(IndexingError):
        await service.acquire(repository_id="fixture", remote_url=remote_url)


@pytest.mark.asyncio
async def test_acquire_rejects_unsafe_identifiers_before_git(tmp_path: Path) -> None:
    service = GitAcquisitionService(tmp_path, allowed_hosts={"github.com"})

    with pytest.raises(IndexingError, match="repository_id"):
        await service.acquire(
            repository_id="../escape",
            remote_url="https://github.com/example/project.git",
        )
    with pytest.raises(IndexingError, match="ref"):
        await service.acquire(
            repository_id="fixture",
            remote_url="https://github.com/example/project.git",
            ref="--upload-pack=bad",
        )
