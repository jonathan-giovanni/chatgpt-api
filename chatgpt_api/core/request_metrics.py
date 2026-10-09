"""Opt-in request timings. Never records prompts, identities, URLs, or secrets."""

from __future__ import annotations

import contextvars
import functools
import inspect
import json
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

_current: contextvars.ContextVar[RequestMetrics | None] = contextvars.ContextVar("request_metrics", default=None)


class RequestMetrics:
    def __init__(self) -> None:
        self.request_id = uuid.uuid4().hex
        self.started = time.perf_counter()
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.spans: list[dict[str, Any]] = []
        self.marks: dict[str, float] = {}
        self.facts: dict[str, Any] = {}
        self.lock = threading.Lock()

    def elapsed_ms(self) -> float:
        return round((time.perf_counter() - self.started) * 1000, 3)

    def snapshot(self, status: int | None) -> dict[str, Any]:
        with self.lock:
            return {"event": "chat_request_metrics", "request_id": self.request_id,
                    "started_at": self.started_at, "status": status,
                    "total_ms": self.elapsed_ms(), "marks_ms": dict(self.marks),
                    "facts": dict(self.facts), "spans": sorted(self.spans, key=lambda item: item["start_ms"])}


def current_metrics() -> RequestMetrics | None:
    return _current.get()


@contextmanager
def request_metrics(enabled: bool):
    trace = RequestMetrics() if enabled else None
    token = _current.set(trace)
    try:
        yield trace
    finally:
        _current.reset(token)


@contextmanager
def metric_span(name: str):
    trace = _current.get()
    if trace is None:
        yield None
        return
    started = time.perf_counter()
    item = {"name": name, "start_ms": trace.elapsed_ms()}
    try:
        yield item
    except BaseException as exc:
        item["error_type"] = type(exc).__name__
        raise
    finally:
        item["duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
        with trace.lock:
            if len(trace.spans) < 200:
                trace.spans.append(item)


def metric_mark(name: str, *, first: bool = True) -> None:
    trace = _current.get()
    if trace is not None:
        with trace.lock:
            if not first or name not in trace.marks:
                trace.marks[name] = trace.elapsed_ms()


def metric_facts(**values: bool | int | float | None) -> None:
    trace = _current.get()
    if trace is not None:
        with trace.lock:
            trace.facts.update(values)


def metric_increment(name: str, amount: int = 1) -> None:
    trace = _current.get()
    if trace is not None:
        with trace.lock:
            trace.facts[name] = trace.facts.get(name, 0) + amount


def timed(name: str):
    def decorate(function):
        if inspect.iscoroutinefunction(function):
            @functools.wraps(function)
            async def async_wrapper(*args, **kwargs):
                with metric_span(name):
                    return await function(*args, **kwargs)
            return async_wrapper
        @functools.wraps(function)
        def sync_wrapper(*args, **kwargs):
            with metric_span(name):
                return function(*args, **kwargs)
        return sync_wrapper
    return decorate


def emit_metrics(trace: RequestMetrics, status: int | None) -> None:
    # A single JSON line per request; logging failures must not affect delivery.
    try:
        print(json.dumps(trace.snapshot(status), separators=(",", ":")), file=sys.stderr, flush=True)
    except (OSError, ValueError):
        pass
