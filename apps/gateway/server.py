"""Executable wrapper for the public FastAPI gateway."""

import uvicorn

from repo_chat.config.settings import Settings


def main() -> None:
    """Run the public gateway with Uvicorn."""
    settings = Settings(service_name="gateway")
    uvicorn.run(
        "repo_chat.gateway.app:app",
        host=settings.gateway_host,
        port=settings.gateway_port,
    )


if __name__ == "__main__":
    main()
