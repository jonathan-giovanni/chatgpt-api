"""Opt-in, process-local reuse of requirements within a single chat thread."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Iterator

TOKEN_HEADERS = ("openai-sentinel-chat-requirements-token", "openai-sentinel-proof-token")


@dataclass(frozen=True)
class Entry:
    created: float
    headers: dict[str, str] = field(repr=False)
    proof_required: bool = False


class RequirementsCache:
    def __init__(self, capacity: int = 128) -> None:
        self.capacity = capacity
        self._entries: OrderedDict[tuple[str, str], Entry] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: tuple[str, str], ttl: float) -> Entry | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry and time.monotonic() - entry.created >= ttl:
                self._entries.pop(key, None)
                return None
            if entry:
                self._entries.move_to_end(key)
            return entry

    def put(self, key: tuple[str, str], entry: Entry) -> None:
        with self._lock:
            self._entries[key] = entry
            self._entries.move_to_end(key)
            while len(self._entries) > self.capacity:
                self._entries.popitem(last=False)

    def invalidate(self, key: tuple[str, str]) -> None:
        with self._lock:
            self._entries.pop(key, None)


CACHE = RequirementsCache()


@dataclass
class Exchange:
    ttl: float = 0
    close_on_done: bool = True
    parallel_preparation: bool = False
    key: tuple[str, str] | None = field(default=None, repr=False)
    scope: str | None = field(default=None, repr=False)
    candidate: Entry | None = field(default=None, repr=False)
    hit: bool = False
    force_fresh: bool = False

    def bind(self, conversation_id: str | None) -> None:
        if self.ttl and self.scope and self.candidate and conversation_id:
            CACHE.put((self.scope, conversation_id), self.candidate)


_OPTIONS: ContextVar[dict[str, bool]] = ContextVar("web_chat_options", default={})
_EXCHANGE: ContextVar[Exchange | None] = ContextVar("web_chat_exchange", default=None)


@contextmanager
def chat_policy(body: dict[str, Any]) -> Iterator[None]:
    options = {}
    for name in ("chatgpt_reuse_requirements", "chatgpt_stream_close_on_done", "chatgpt_parallel_preparation"):
        if name in body:
            if not isinstance(body[name], bool):
                raise ValueError(f"{name} must be a boolean")
            options[name] = body[name]
    token = _OPTIONS.set(options)
    try:
        yield
    finally:
        _OPTIONS.reset(token)


@contextmanager
def chat_exchange() -> Iterator[Exchange]:
    options = _OPTIONS.get()
    try:
        ttl = min(900, max(0, int(os.environ.get("CHATGPT_REQUIREMENTS_CACHE_TTL_SECONDS", "0"))))
    except ValueError:
        ttl = 0
    if options.get("chatgpt_reuse_requirements") is False:
        ttl = 0
    parallel_default = os.environ.get("CHATGPT_PARALLEL_PREPARATION", "false").lower() in {"true", "1", "yes", "on"}
    exchange = Exchange(ttl=ttl, close_on_done=options.get("chatgpt_stream_close_on_done", True),
                        parallel_preparation=options.get("chatgpt_parallel_preparation", parallel_default))
    token = _EXCHANGE.set(exchange)
    try:
        yield exchange
    finally:
        _EXCHANGE.reset(token)


def current_exchange() -> Exchange | None:
    return _EXCHANGE.get()


def cache_scope(headers: dict[str, str], payload: dict[str, Any], endpoint: str, impersonate: str) -> str:
    identity = {k.lower(): v for k, v in headers.items() if k.lower().startswith("openai-sentinel-") or k.lower() in {
        "authorization", "cookie", "oai-device-id", "oai-session-id", "chatgpt-account-id", "user-agent",
    }}
    context = [identity, endpoint, impersonate, payload.get("model"), payload.get("conversation_mode"),
               payload.get("thinking_effort"), payload.get("history_and_training_disabled")]
    return hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()


def cacheable_requirements(data: dict[str, Any]) -> bool:
    if not isinstance(data.get("token"), str) or not data["token"]:
        return False
    return not any(isinstance(data.get(name), dict) and data[name].get("required")
                   for name in ("arkose", "turnstile", "captcha"))
