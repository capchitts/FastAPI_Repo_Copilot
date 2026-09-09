"""Validation for the Graph Agent's custom read-only Cypher tool."""

import re
from dataclasses import dataclass

from repo_chat.exceptions.base import GraphQueryError

_COMMENT_PATTERN = re.compile(r"//[^\n]*|/\*.*?\*/", re.DOTALL)
_STRING_PATTERN = re.compile(r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"")
_WRITE_KEYWORDS = re.compile(
    r"\b(CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|LOAD\s+CSV|FOREACH|CALL|GRANT|DENY|REVOKE)\b",
    re.IGNORECASE,
)
_ALLOWED_START = re.compile(r"^\s*(MATCH|OPTIONAL\s+MATCH|WITH|UNWIND|RETURN)\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class SafeCypher:
    """Validated query text and its enforced result limit."""

    query: str
    limit: int


def validate_read_only_cypher(query: str, *, limit: int, maximum_limit: int = 200) -> SafeCypher:
    """Reject mutations, procedures, multiple statements, and excessive limits."""
    normalized = query.strip()
    if not normalized:
        raise GraphQueryError("Cypher query cannot be empty")
    if not 1 <= limit <= maximum_limit:
        raise GraphQueryError(
            f"Result limit must be between 1 and {maximum_limit}",
            details={"limit": limit},
        )

    inspection_text = _STRING_PATTERN.sub("''", _COMMENT_PATTERN.sub(" ", normalized))
    stripped = inspection_text.rstrip()
    if ";" in stripped.rstrip(";"):
        raise GraphQueryError("Multiple Cypher statements are not allowed")
    normalized = normalized.rstrip().rstrip(";").rstrip()
    inspection_text = inspection_text.rstrip().rstrip(";").rstrip()
    if not _ALLOWED_START.match(inspection_text):
        raise GraphQueryError("Custom Cypher must start with a read-only clause")
    match = _WRITE_KEYWORDS.search(inspection_text)
    if match:
        raise GraphQueryError(
            "Mutating Cypher and procedure calls are not allowed",
            details={"keyword": match.group(1).upper()},
        )

    wrapped = f"CALL {{\n{normalized}\n}}\nRETURN * LIMIT $result_limit"
    return SafeCypher(query=wrapped, limit=limit)
