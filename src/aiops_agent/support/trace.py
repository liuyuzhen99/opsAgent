from __future__ import annotations

from contextvars import ContextVar
from uuid import uuid4


_TRACE_ID: ContextVar[str | None] = ContextVar("trace_id", default=None)


def generate_trace_id() -> str:
    return uuid4().hex


def set_trace_id(trace_id: str) -> None:
    _TRACE_ID.set(trace_id)


def get_trace_id() -> str:
    current = _TRACE_ID.get()
    if current is None:
        current = generate_trace_id()
        _TRACE_ID.set(current)
    return current
