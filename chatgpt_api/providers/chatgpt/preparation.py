"""Request-local control for overlapping fresh ChatGPT preparation calls."""

from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator


_PARALLEL: ContextVar[bool | None] = ContextVar("chat_parallel_preparation", default=None)


@contextmanager
def chat_policy(body: dict[str, Any]) -> Iterator[None]:
    value = body.get("chatgpt_parallel_preparation")
    if "chatgpt_parallel_preparation" in body and not isinstance(value, bool):
        raise ValueError("chatgpt_parallel_preparation must be a boolean")
    token = _PARALLEL.set(value)
    try:
        yield
    finally:
        _PARALLEL.reset(token)


def parallel_preparation_enabled() -> bool:
    value = _PARALLEL.get()
    if value is not None:
        return value
    return os.environ.get("CHATGPT_PARALLEL_PREPARATION", "true").lower() in {"true", "1", "yes", "on"}
