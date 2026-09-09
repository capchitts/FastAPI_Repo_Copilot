"""Representative Python constructs for AST parser tests."""

from collections.abc import Callable
from dataclasses import dataclass as record


class BaseService:
    """Base service contract."""


@record
class ItemService(BaseService):
    """A service containing sync and asynchronous methods."""

    name: str = "items"

    def get(self, item_id: int, limit: int = 10) -> dict[str, int]:
        """Return one representative item."""
        payload = build_payload(item_id)
        return payload

    async def execute(
        self, callback: Callable[..., object], *args: object, **kwargs: object
    ) -> object:
        return callback(*args, **kwargs)


def traced(function: Callable[..., object]) -> Callable[..., object]:
    """Return a function unchanged for fixture purposes."""
    return function


@traced
def build_payload(item_id: int) -> dict[str, int]:
    return dict(item_id=item_id)
