import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from repo_chat.contracts.indexing import IndexJob, IndexJobStatus, IndexMode, ParsedFile
from repo_chat.exceptions.base import EntityNotFoundError, IndexingError
from repo_chat.indexing.job_store import InMemoryIndexJobStore
from repo_chat.indexing.manifest import InMemoryIndexManifestStore
from repo_chat.indexing.service import IndexerService


class FakeGraphRepository:
    def __init__(self) -> None:
        self.written: list[ParsedFile] = []
        self.carried: list[dict[str, str]] = []
        self.removed: list[dict[str, str]] = []
        self.repository_relationship_batches: list[list[ParsedFile]] = []
        self.snapshots: list[dict[str, object]] = []

    async def write_parsed_file(self, parsed_file: ParsedFile) -> None:
        self.written.append(parsed_file)

    async def carry_forward_file(self, **arguments: str) -> None:
        self.carried.append(arguments)

    async def remove_file_from_revision(self, **arguments: str) -> None:
        self.removed.append(arguments)

    async def write_repository_relationships(
        self, parsed_files: list[ParsedFile]
    ) -> None:
        self.repository_relationship_batches.append(parsed_files)

    async def record_revision_snapshot(self, **arguments: object) -> None:
        self.snapshots.append(arguments)


def create_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repositories" / "fixture"
    package = repository / "sample"
    package.mkdir(parents=True)
    (package / "valid.py").write_text(
        '"""Valid module."""\n\nclass Example:\n    pass\n',
        encoding="utf-8",
    )
    (package / "ignored.txt").write_text("not Python", encoding="utf-8")
    hidden = repository / ".hidden"
    hidden.mkdir()
    (hidden / "ignored.py").write_text("value = 1\n", encoding="utf-8")
    return repository


def create_service(
    tmp_path: Path,
    graph: FakeGraphRepository,
    initialize_schema=None,  # type: ignore[no-untyped-def]
    manifest_store=None,  # type: ignore[no-untyped-def]
) -> IndexerService:
    return IndexerService(  # type: ignore[arg-type]
        graph,
        repository_root=tmp_path / "repositories",
        initialize_schema=initialize_schema,
        manifest_store=manifest_store,
    )


def test_parse_and_extract_entities_are_deterministic(tmp_path: Path) -> None:
    graph = FakeGraphRepository()
    service = create_service(tmp_path, graph)
    code = "class Example:\n    pass\n"

    parsed = service.parse_python_ast(
        code,
        repository_id="fixture",
        revision="abc123",
        file_path="sample/example.py",
    )
    entities = service.extract_entities(
        code,
        repository_id="fixture",
        revision="abc123",
        file_path="sample/example.py",
    )

    assert entities == parsed.entities
    assert any(entity.name == "Example" for entity in entities)


def test_parse_rejects_unsafe_file_path(tmp_path: Path) -> None:
    service = create_service(tmp_path, FakeGraphRepository())

    with pytest.raises(IndexingError, match="repository-relative"):
        service.parse_python_ast(
            "value = 1\n",
            repository_id="fixture",
            revision="abc123",
            file_path="../outside.py",
        )


@pytest.mark.asyncio
async def test_index_file_initializes_schema_and_persists(tmp_path: Path) -> None:
    repository = create_repository(tmp_path)
    graph = FakeGraphRepository()
    schema_calls = 0

    async def initialize_schema() -> None:
        nonlocal schema_calls
        schema_calls += 1

    service = create_service(tmp_path, graph, initialize_schema)

    first = await service.index_file(
        "sample/valid.py",
        repository_path=repository.name,
        repository_id="fixture",
        revision="abc123",
    )
    await service.index_file(
        "sample/valid.py",
        repository_path=repository.name,
        repository_id="fixture",
        revision="abc123",
    )

    assert first.entity_count > 0
    assert first.issue_count == 0
    assert len(graph.written) == 2
    assert schema_calls == 1


@pytest.mark.asyncio
async def test_index_file_rejects_missing_or_non_python_files(tmp_path: Path) -> None:
    repository = create_repository(tmp_path)
    service = create_service(tmp_path, FakeGraphRepository())

    with pytest.raises(EntityNotFoundError, match="not found"):
        await service.index_file(
            "missing.py",
            repository_path=repository.name,
            repository_id="fixture",
            revision="abc123",
        )
    with pytest.raises(IndexingError, match="Only Python"):
        await service.index_file(
            "sample/ignored.txt",
            repository_path=repository.name,
            repository_id="fixture",
            revision="abc123",
        )


@pytest.mark.asyncio
async def test_repository_job_completes_and_ignores_hidden_directories(tmp_path: Path) -> None:
    repository = create_repository(tmp_path)
    (repository / ".repo-chat-snapshot.json").write_text(
        json.dumps(
            {
                "revision": "abc123",
                "remote_url": "https://github.com/example/fixture.git",
                "requested_ref": "main",
                "commit_timestamp": "2026-09-04T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    graph = FakeGraphRepository()
    service = create_service(tmp_path, graph)

    created = await service.index_repository(
        repository.name,
        repository_id="fixture",
        revision="abc123",
    )
    assert created.status is IndexJobStatus.PENDING

    await service.wait_for_jobs()
    completed = await service.get_index_status(created.id)

    assert completed.status is IndexJobStatus.COMPLETED
    assert completed.discovered_files == 1
    assert completed.processed_files == 1
    assert completed.failed_files == 0
    assert len(graph.written) == 1
    assert len(graph.snapshots) == 1
    assert graph.snapshots[0]["repository_id"] == "fixture"
    assert graph.snapshots[0]["revision"] == "abc123"
    assert graph.snapshots[0]["index_job_id"] == created.id
    assert graph.snapshots[0]["indexed_at"] == completed.completed_at
    assert graph.snapshots[0]["remote_url"] == "https://github.com/example/fixture.git"
    assert graph.snapshots[0]["branch_or_tag"] == "main"


@pytest.mark.asyncio
async def test_incremental_job_skips_unchanged_files(tmp_path: Path) -> None:
    repository = create_repository(tmp_path)
    graph = FakeGraphRepository()
    service = create_service(tmp_path, graph)

    full = await service.index_repository(
        repository.name,
        repository_id="fixture",
        revision="one",
    )
    await service.wait_for_jobs()
    assert (await service.get_index_status(full.id)).status is IndexJobStatus.COMPLETED

    incremental = await service.index_repository(
        repository.name,
        repository_id="fixture",
        revision="two",
        mode=IndexMode.INCREMENTAL,
    )
    await service.wait_for_jobs()
    completed = await service.get_index_status(incremental.id)

    assert completed.status is IndexJobStatus.COMPLETED
    assert completed.skipped_files == 1
    assert completed.processed_files == 1
    assert len(graph.written) == 1
    assert graph.carried == [
        {
            "repository_id": "fixture",
            "source_revision": "one",
            "target_revision": "two",
            "file_path": "sample/valid.py",
            "content_hash": graph.written[0].content_hash,
        }
    ]


@pytest.mark.asyncio
async def test_duplicate_index_request_returns_original_job(tmp_path: Path) -> None:
    repository = create_repository(tmp_path)
    graph = FakeGraphRepository()
    service = create_service(tmp_path, graph)

    first = await service.index_repository(
        repository.name, repository_id="fixture", revision="same"
    )
    duplicate = await service.index_repository(
        repository.name, repository_id="fixture", revision="same"
    )
    await service.wait_for_jobs()

    assert duplicate.id == first.id
    assert len(graph.written) == 1


@pytest.mark.asyncio
async def test_incremental_manifest_survives_service_recreation(tmp_path: Path) -> None:
    repository = create_repository(tmp_path)
    graph = FakeGraphRepository()
    manifest = InMemoryIndexManifestStore()
    first_service = create_service(tmp_path, graph, manifest_store=manifest)
    full = await first_service.index_repository(
        repository.name, repository_id="fixture", revision="one"
    )
    await first_service.wait_for_jobs()
    assert (await first_service.get_index_status(full.id)).status is IndexJobStatus.COMPLETED

    restarted_service = create_service(tmp_path, graph, manifest_store=manifest)
    incremental = await restarted_service.index_repository(
        repository.name,
        repository_id="fixture",
        revision="two",
        mode=IndexMode.INCREMENTAL,
    )
    await restarted_service.wait_for_jobs()
    completed = await restarted_service.get_index_status(incremental.id)

    assert completed.skipped_files == 1
    assert len(graph.written) == 1
    assert graph.carried[0]["source_revision"] == "one"
    assert graph.carried[0]["target_revision"] == "two"


@pytest.mark.asyncio
async def test_incremental_job_reconciles_deleted_and_renamed_files(tmp_path: Path) -> None:
    repository = create_repository(tmp_path)
    old_path = repository / "sample" / "valid.py"
    deleted_path = repository / "sample" / "deleted.py"
    deleted_path.write_text("def removed():\n    return True\n", encoding="utf-8")
    graph = FakeGraphRepository()
    manifest = InMemoryIndexManifestStore()
    service = create_service(tmp_path, graph, manifest_store=manifest)
    full = await service.index_repository(
        repository.name, repository_id="fixture", revision="one"
    )
    await service.wait_for_jobs()
    assert (await service.get_index_status(full.id)).status is IndexJobStatus.COMPLETED

    renamed_path = repository / "sample" / "renamed.py"
    old_path.rename(renamed_path)
    deleted_path.unlink()
    incremental = await service.index_repository(
        repository.name,
        repository_id="fixture",
        revision="two",
        mode=IndexMode.INCREMENTAL,
    )
    await service.wait_for_jobs()
    completed = await service.get_index_status(incremental.id)
    manifests = await manifest.list("fixture")

    assert completed.status is IndexJobStatus.COMPLETED
    assert completed.deleted_file_count == 1
    assert completed.renamed_file_count == 1
    assert {item["file_path"] for item in graph.removed} == {
        "sample/deleted.py",
        "sample/valid.py",
    }
    assert set(manifests) == {"sample/renamed.py"}


@pytest.mark.asyncio
async def test_repository_job_records_syntax_failures(tmp_path: Path) -> None:
    repository = tmp_path / "repositories" / "broken"
    repository.mkdir(parents=True)
    (repository / "broken.py").write_text("def broken(:\n", encoding="utf-8")
    service = create_service(tmp_path, FakeGraphRepository())

    created = await service.index_repository(
        repository.name,
        repository_id="broken",
        revision="abc123",
    )
    await service.wait_for_jobs()
    completed = await service.get_index_status(created.id)

    assert completed.status is IndexJobStatus.FAILED
    assert completed.failed_files == 1
    assert completed.error == "One or more discovered Python files failed to parse"


@pytest.mark.asyncio
async def test_unknown_job_and_repository_are_rejected(tmp_path: Path) -> None:
    service = create_service(tmp_path, FakeGraphRepository())

    with pytest.raises(EntityNotFoundError, match="job"):
        await service.get_index_status("missing")
    with pytest.raises(EntityNotFoundError, match="directory"):
        await service.index_repository(
            "missing",
            repository_id="missing",
            revision="abc123",
        )
    with pytest.raises(IndexingError, match="outside"):
        await service.index_repository(
            str(tmp_path),
            repository_id="outside",
            revision="abc123",
        )


@pytest.mark.asyncio
async def test_service_marks_job_from_prior_process_as_interrupted(tmp_path: Path) -> None:
    store = InMemoryIndexJobStore()
    job = IndexJob(
        id="interrupted",
        repository_id="fixture",
        repository_path=str(tmp_path),
        revision="abc123",
        mode=IndexMode.FULL,
        status=IndexJobStatus.RUNNING,
        processed_files=4,
        created_at=datetime.now(UTC),
    )
    await store.create(job)
    service = IndexerService(  # type: ignore[arg-type]
        FakeGraphRepository(),
        repository_root=tmp_path,
        job_store=store,
    )

    recovered = await service.get_index_status(job.id)

    assert recovered.status is IndexJobStatus.FAILED
    assert recovered.processed_files == 4
    assert recovered.error == "Indexer restarted before the job completed"
