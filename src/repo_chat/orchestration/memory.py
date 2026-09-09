"""Conversation-memory interfaces and an in-process implementation."""

import asyncio
from collections import defaultdict
from datetime import UTC, datetime
from typing import Protocol

from repo_chat.contracts.orchestration import (
    ConversationContext,
    ConversationRole,
    ConversationTurn,
)


class ConversationMemory(Protocol):
    """Storage contract implemented by in-memory and Redis repositories."""

    async def get_context(self, session_id: str) -> ConversationContext: ...

    async def append_turn(
        self,
        session_id: str,
        role: ConversationRole,
        content: str,
        *,
        entities: list[str] | None = None,
        repository_id: str | None = None,
        revision: str | None = None,
    ) -> ConversationContext: ...


class InMemoryConversationMemory:
    """Bounded concurrency-safe memory for tests and local development."""

    def __init__(self, *, maximum_turns: int = 10) -> None:
        if maximum_turns < 1:
            raise ValueError("maximum_turns must be positive")
        self._maximum_turns = maximum_turns
        self._turns: dict[str, list[ConversationTurn]] = defaultdict(list)
        self._entities: dict[str, list[str]] = defaultdict(list)
        self._scopes: dict[str, tuple[str | None, str | None]] = {}
        self._lock = asyncio.Lock()

    async def get_context(self, session_id: str) -> ConversationContext:
        """Return a defensive snapshot of the selected session."""
        async with self._lock:
            return ConversationContext(
                session_id=session_id,
                turns=list(self._turns[session_id]),
                active_entities=list(self._entities[session_id]),
                repository_id=self._scopes.get(session_id, (None, None))[0],
                revision=self._scopes.get(session_id, (None, None))[1],
            )

    async def append_turn(
        self,
        session_id: str,
        role: ConversationRole,
        content: str,
        *,
        entities: list[str] | None = None,
        repository_id: str | None = None,
        revision: str | None = None,
    ) -> ConversationContext:
        """Append a turn, trim history, and retain unique recent entities."""
        turn = ConversationTurn(
            role=role,
            content=content,
            created_at=datetime.now(UTC),
            entities=entities or [],
        )
        async with self._lock:
            turns = self._turns[session_id]
            turns.append(turn)
            del turns[: max(0, len(turns) - self._maximum_turns)]
            active = self._entities[session_id]
            for entity in entities or []:
                if entity in active:
                    active.remove(entity)
                active.append(entity)
            del active[: max(0, len(active) - 20)]
            current_repository, current_revision = self._scopes.get(session_id, (None, None))
            self._scopes[session_id] = (
                repository_id or current_repository,
                revision or current_revision,
            )
            return ConversationContext(
                session_id=session_id,
                turns=list(turns),
                active_entities=list(active),
                repository_id=self._scopes[session_id][0],
                revision=self._scopes[session_id][1],
            )
