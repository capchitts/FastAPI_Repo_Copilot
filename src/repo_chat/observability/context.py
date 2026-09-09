from contextvars import ContextVar
from uuid import uuid4

correlation_id_var: ContextVar[str] = ContextVar(
    "correlation_id",
    default="unassigned",
)


def create_correlation_id() -> str:
    return str(uuid4())


def get_correlation_id() -> str:
    return correlation_id_var.get()
