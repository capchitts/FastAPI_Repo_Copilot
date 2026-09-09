import logging
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

from repo_chat.observability.context import get_correlation_id


def add_correlation_id(
    _logger: object,
    _method_name: str,
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    event_dict["correlation_id"] = get_correlation_id()
    return event_dict


def configure_logging(log_level: str) -> None:
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level.upper(),
        force=True,
    )

    structlog.configure(
        processors=[
            add_correlation_id,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.add_log_level,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
