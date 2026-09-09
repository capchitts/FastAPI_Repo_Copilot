"""Async Neo4j driver lifecycle management."""

from neo4j import AsyncDriver, AsyncGraphDatabase
from neo4j.exceptions import Neo4jError, ServiceUnavailable

from repo_chat.config.settings import Settings
from repo_chat.exceptions.base import GraphQueryError


class Neo4jClient:
    """Own one reusable async driver for a service process."""

    def __init__(self, settings: Settings) -> None:
        self._driver: AsyncDriver = AsyncGraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_username, settings.neo4j_password.get_secret_value()),
        )

    @property
    def driver(self) -> AsyncDriver:
        """Return the managed driver for graph repositories."""
        return self._driver

    async def verify_connectivity(self) -> None:
        """Verify that the configured Neo4j server is reachable."""
        try:
            await self._driver.verify_connectivity()
        except (Neo4jError, ServiceUnavailable) as error:
            raise GraphQueryError(
                "Neo4j connectivity verification failed",
                details={"error_type": type(error).__name__},
            ) from error

    async def close(self) -> None:
        """Release the driver's connection pool."""
        await self._driver.close()

    async def __aenter__(self) -> "Neo4jClient":
        await self.verify_connectivity()
        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_value: BaseException | None,
        _traceback: object,
    ) -> None:
        await self.close()
