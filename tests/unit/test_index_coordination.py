import pytest

from repo_chat.indexing.coordination import InMemoryIndexCoordination


@pytest.mark.asyncio
async def test_index_coordination_claims_idempotency_and_checks_lock_token() -> None:
    coordination = InMemoryIndexCoordination()

    assert await coordination.claim_idempotency("identity", "job-one") == "job-one"
    assert await coordination.claim_idempotency("identity", "job-two") == "job-one"
    assert await coordination.acquire_repository_lock("fastapi", "owner-one") is True
    assert await coordination.acquire_repository_lock("fastapi", "owner-two") is False

    await coordination.release_repository_lock("fastapi", "owner-two")
    assert await coordination.acquire_repository_lock("fastapi", "owner-two") is False
    await coordination.release_repository_lock("fastapi", "owner-one")
    assert await coordination.acquire_repository_lock("fastapi", "owner-two") is True
