"""Bounded in-memory fan-out of conversation updates; no upstream polling."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any

from chatgpt_api.providers.chatgpt.voice_events import VoiceMessageStream, voice_event


@dataclass
class ConversationChannel:
    account: str
    messages: OrderedDict[str, dict[str, Any]] = field(default_factory=OrderedDict)
    events: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=256))
    revision: int = 0
    history_loaded: bool = False
    history_warning: str | None = None
    subscribers: int = 0
    untranscribed_audio_messages: int = 0
    touched: float = field(default_factory=time.monotonic)
    condition: threading.Condition = field(default_factory=threading.Condition)
    history_lock: threading.Lock = field(default_factory=threading.Lock)

    def seed(self, snapshot: dict[str, Any], *, refresh: bool = False, conversation_id: str = "") -> None:
        with self.condition:
            if refresh:
                incoming = OrderedDict((message["id"], message) for message in snapshot.get("messages", []))
                latest = max((message.get("created_at") or "" for message in incoming.values()), default="")
                # An explicit reload follows the current upstream branch, while
                # preserving a live turn that has not reached history yet.
                for key, message in self.messages.items():
                    if message.get("status") == "in_progress" or (
                        key not in incoming and latest and (message.get("created_at") or "") > latest
                    ):
                        incoming[key] = message
                self.messages = incoming
            for message in snapshot.get("messages", []):
                if message["id"] not in self.messages:
                    self.messages[message["id"]] = message
            while len(self.messages) > 2000:
                self.messages.popitem(last=False)
            self.untranscribed_audio_messages = snapshot.get("untranscribed_audio_messages", 0)
            self.history_loaded = True
            self.history_warning = None
            if refresh:
                self.revision += 1
                self.events.append(self.snapshot(conversation_id))
                self.condition.notify_all()

    def publish(self, message: dict[str, Any]) -> None:
        with self.condition:
            self.touched = time.monotonic()
            if self.messages.get(message["id"]) == message:
                return
            self.messages[message["id"]] = message
            while len(self.messages) > 2000:
                self.messages.popitem(last=False)
            self.revision += 1
            self.events.append({"id": self.revision, "type": "message", "message": message})
            self.condition.notify_all()

    def snapshot(self, conversation_id: str) -> dict[str, Any]:
        with self.condition:
            self.touched = time.monotonic()
            return {"id": self.revision, "type": "snapshot", "conversation_id": conversation_id,
                    "account": self.account,
                    "history_warning": self.history_warning,
                    "messages": sorted(self.messages.values(), key=lambda item: item.get("created_at") or ""),
                    "untranscribed_audio_messages": self.untranscribed_audio_messages}

    def wait(self, after: int, timeout: float = 15) -> list[dict[str, Any]] | None:
        with self.condition:
            self.touched = time.monotonic()
            self.condition.wait_for(lambda: self.revision > after, timeout=timeout)
            if self.events and after < self.events[0]["id"] - 1:
                return None  # Consumer fell behind the replay window: send a local snapshot.
            return [event for event in self.events if event["id"] > after]


@dataclass
class VoiceFeed:
    decoder: VoiceMessageStream = field(default_factory=VoiceMessageStream)
    sequence: int = -1
    conversation_id: str | None = None
    pending: OrderedDict[str, dict[str, Any]] = field(default_factory=OrderedDict)
    touched: float = field(default_factory=time.monotonic)
    lock: threading.Lock = field(default_factory=threading.Lock)


class ConversationStreams:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.channels: OrderedDict[str, ConversationChannel] = OrderedDict()
        self.feeds: OrderedDict[str, VoiceFeed] = OrderedDict()
        self.initial_history_lock = threading.Lock()

    def channel(self, conversation_id: str, account: str) -> ConversationChannel:
        with self._lock:
            self._expire()
            channel = self.channels.get(conversation_id)
            if channel and channel.account != account:
                raise ValueError("conversation_id belongs to another account")
            if channel is None:
                if len(self.channels) >= 128:
                    idle = next((key for key, value in self.channels.items() if value.subscribers == 0), None)
                    if idle is None:
                        raise ValueError("conversation stream capacity reached")
                    self.channels.pop(idle)
                channel = ConversationChannel(account)
                self.channels[conversation_id] = channel
            channel.touched = time.monotonic()
            self.channels.move_to_end(conversation_id)
            return channel

    def known(self, conversation_id: str) -> ConversationChannel | None:
        with self._lock:
            self._expire()
            return self.channels.get(conversation_id)

    def feed(self, session_id: str) -> VoiceFeed:
        with self._lock:
            self._expire()
            if session_id not in self.feeds and len(self.feeds) >= 128:
                raise ValueError("voice text stream capacity reached")
            feed = self.feeds.setdefault(session_id, VoiceFeed())
            feed.touched = time.monotonic()
            self.feeds.move_to_end(session_id)
            return feed

    def release(self, session_id: str) -> None:
        with self._lock:
            self.feeds.pop(session_id, None)

    def _expire(self) -> None:
        now = time.monotonic()
        for cache in (self.channels, self.feeds):
            for key in [key for key, value in cache.items()
                        if now - value.touched > 3600 and not getattr(value, "subscribers", 0)]:
                cache.pop(key, None)


def decode_voice_batch(events: Any) -> list[dict[str, Any]]:
    if not isinstance(events, list) or not 1 <= len(events) <= 50:
        raise ValueError("events must contain 1 to 50 data channel events")
    return [event for raw in events if (event := voice_event(raw)) is not None]
