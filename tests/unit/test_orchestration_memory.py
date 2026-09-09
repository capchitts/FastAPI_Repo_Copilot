import pytest

from repo_chat.contracts.orchestration import ConversationRole
from repo_chat.orchestration.memory import InMemoryConversationMemory


@pytest.mark.asyncio
async def test_memory_bounds_turns_and_tracks_recent_entities() -> None:
    memory = InMemoryConversationMemory(maximum_turns=2)

    await memory.append_turn("session", ConversationRole.USER, "one", entities=["APIRouter"])
    await memory.append_turn("session", ConversationRole.ASSISTANT, "two")
    context = await memory.append_turn(
        "session", ConversationRole.USER, "three", entities=["Depends"]
    )

    assert [turn.content for turn in context.turns] == ["two", "three"]
    assert context.active_entities == ["APIRouter", "Depends"]


@pytest.mark.asyncio
async def test_memory_returns_defensive_snapshots() -> None:
    memory = InMemoryConversationMemory()
    first = await memory.append_turn(
        "session", ConversationRole.USER, "question", entities=["FastAPI"]
    )
    first.turns.clear()
    first.active_entities.clear()

    second = await memory.get_context("session")

    assert len(second.turns) == 1
    assert second.active_entities == ["FastAPI"]


@pytest.mark.asyncio
async def test_memory_persists_repository_scope() -> None:
    memory = InMemoryConversationMemory()

    await memory.append_turn(
        "session",
        ConversationRole.USER,
        "Explain APIRouter",
        repository_id="fastapi",
        revision="abc123",
    )
    context = await memory.append_turn(
        "session", ConversationRole.USER, "What calls it?"
    )

    assert context.repository_id == "fastapi"
    assert context.revision == "abc123"


def test_memory_rejects_invalid_bound() -> None:
    with pytest.raises(ValueError, match="positive"):
        InMemoryConversationMemory(maximum_turns=0)
