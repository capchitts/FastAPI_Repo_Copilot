"""Safe Git acquisition into immutable, revision-addressed snapshots."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from repo_chat.contracts.repository import RepositoryAcquisition
from repo_chat.exceptions.base import IndexingError


class GitAcquisitionService:
    """Fetch approved HTTPS remotes and atomically materialize commit snapshots."""

    def __init__(
        self,
        root: Path,
        *,
        allowed_hosts: set[str],
        command_timeout_seconds: float = 120.0,
        maximum_snapshot_bytes: int = 500_000_000,
    ) -> None:
        self._root = root.resolve()
        self._allowed_hosts = {host.casefold() for host in allowed_hosts}
        self._timeout = command_timeout_seconds
        self._maximum_snapshot_bytes = maximum_snapshot_bytes
        self._locks: dict[str, asyncio.Lock] = {}

    async def acquire(
        self,
        *,
        repository_id: str,
        remote_url: str,
        ref: str = "HEAD",
    ) -> RepositoryAcquisition:
        """Fetch a ref, resolve its commit, and publish an immutable snapshot."""
        repository_id = self._safe_component(repository_id, "repository_id")
        ref = self._safe_ref(ref)
        remote_url = self._safe_remote(remote_url)
        lock = self._locks.setdefault(repository_id, asyncio.Lock())
        async with lock:
            return await self._acquire_locked(repository_id, remote_url, ref)

    async def _acquire_locked(
        self, repository_id: str, remote_url: str, ref: str
    ) -> RepositoryAcquisition:
        repository_root = self._root / repository_id
        cache = repository_root / ".git-cache"
        revisions = repository_root / "revisions"
        repository_root.mkdir(parents=True, exist_ok=True)
        revisions.mkdir(exist_ok=True)

        if cache.exists():
            configured_remote = (
                await self._git("--git-dir", str(cache), "remote", "get-url", "origin")
            ).strip()
            if configured_remote != remote_url:
                raise IndexingError("Repository ID is already bound to a different remote")
            await self._git(
                "--git-dir",
                str(cache),
                "fetch",
                "--prune",
                "--depth=1",
                "--no-tags",
                "origin",
                ref,
            )
        else:
            cache.mkdir()
            await self._git("--git-dir", str(cache), "init", "--bare")
            await self._git(
                "--git-dir", str(cache), "remote", "add", "origin", remote_url
            )
            await self._git(
                "--git-dir",
                str(cache),
                "fetch",
                "--prune",
                "--depth=1",
                "--no-tags",
                "origin",
                ref,
            )

        revision = (
            await self._git("--git-dir", str(cache), "rev-parse", "--verify", "FETCH_HEAD^{commit}")
        ).strip()
        invalid_revision = len(revision) != 40 or any(
            character not in "0123456789abcdef" for character in revision
        )
        if invalid_revision:
            raise IndexingError("Git did not resolve the requested ref to a SHA-1 commit")
        commit_timestamp = datetime.fromtimestamp(
            int(
                (
                    await self._git(
                        "--git-dir", str(cache), "show", "-s", "--format=%ct", revision
                    )
                ).strip()
            ),
            tz=UTC,
        )
        destination = revisions / revision
        captured_at = datetime.now(UTC)
        if destination.is_dir():
            return self._result(
                repository_id,
                remote_url,
                ref,
                revision,
                commit_timestamp,
                captured_at,
                destination,
                reused=True,
            )

        staging = repository_root / f".snapshot-{revision[:12]}-{uuid4().hex}"
        try:
            staging.mkdir()
            await self._git(
                "--git-dir",
                str(cache),
                "--work-tree",
                str(staging),
                "checkout",
                "--force",
                revision,
                "--",
                ".",
            )
            total_bytes = self._validate_snapshot(staging)
            metadata = {
                "repository_id": repository_id,
                "remote_url": remote_url,
                "requested_ref": ref,
                "revision": revision,
                "commit_timestamp": commit_timestamp.isoformat(),
                "captured_at": captured_at.isoformat(),
                "total_bytes": total_bytes,
            }
            (staging / ".repo-chat-snapshot.json").write_text(
                json.dumps(metadata, sort_keys=True), encoding="utf-8"
            )
            os.replace(staging, destination)
        except Exception:
            if staging.exists():
                shutil.rmtree(staging)
            raise
        return self._result(
            repository_id,
            remote_url,
            ref,
            revision,
            commit_timestamp,
            captured_at,
            destination,
        )

    async def _git(self, *arguments: str) -> str:
        environment = {
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "/bin/false",
        }
        try:
            completed = await asyncio.to_thread(
                self._run_git,
                arguments,
                environment,
            )
        except subprocess.TimeoutExpired as error:
            raise IndexingError("Git command exceeded the configured timeout") from error
        except OSError as error:
            raise IndexingError("Git executable is unavailable") from error
        if completed.returncode != 0:
            reason = completed.stderr.decode("utf-8", errors="replace").strip()[-500:]
            raise IndexingError(f"Git command failed: {reason or 'unknown error'}")
        return completed.stdout.decode("utf-8", errors="strict")

    def _run_git(
        self, arguments: tuple[str, ...], environment: dict[str, str]
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            ["git", "-c", "credential.helper=", *arguments],
            capture_output=True,
            check=False,
            env=environment,
            timeout=self._timeout,
        )

    def _validate_snapshot(self, root: Path) -> int:
        total_bytes = 0
        for path in root.rglob("*"):
            if path.is_symlink():
                raise IndexingError("Repository snapshots cannot contain symbolic links")
            if not path.is_file():
                continue
            total_bytes += path.stat().st_size
            if total_bytes > self._maximum_snapshot_bytes:
                raise IndexingError("Repository snapshot exceeds the configured size limit")
        return total_bytes

    def _safe_remote(self, remote_url: str) -> str:
        parsed = urlsplit(remote_url.strip())
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.port not in (None, 443)
        ):
            raise IndexingError("Git remote must be a credential-free HTTPS URL")
        hostname = parsed.hostname.casefold()
        if hostname not in self._allowed_hosts:
            raise IndexingError("Git remote host is not allowed")
        return urlunsplit(("https", hostname, parsed.path, "", ""))

    @staticmethod
    def _safe_component(value: str, label: str) -> str:
        path = Path(value)
        if (
            not value
            or path.is_absolute()
            or len(path.parts) != 1
            or value in {".", ".."}
            or value.startswith(".")
        ):
            raise IndexingError(f"{label} must be a safe path component")
        return value

    @staticmethod
    def _safe_ref(ref: str) -> str:
        forbidden = ("..", "@{", "\\", " ", "~", "^", ":", "?", "*", "[")
        if not ref or ref.startswith(("-", ".", "/")) or ref.endswith((".", "/")):
            raise IndexingError("Git ref is invalid")
        if any(token in ref for token in forbidden) or "//" in ref:
            raise IndexingError("Git ref is invalid")
        return ref

    @staticmethod
    def _result(
        repository_id: str,
        remote_url: str,
        ref: str,
        revision: str,
        commit_timestamp: datetime,
        captured_at: datetime,
        destination: Path,
        *,
        reused: bool = False,
    ) -> RepositoryAcquisition:
        return RepositoryAcquisition(
            repository_id=repository_id,
            remote_url=remote_url,
            requested_ref=ref,
            revision=revision,
            commit_timestamp=commit_timestamp,
            captured_at=captured_at,
            repository_path=str(destination),
            reused=reused,
        )
