"""Decode ChatGPT Web Voice's observed message stream, independently of media."""

from __future__ import annotations

import copy
import asyncio
import json
from collections import OrderedDict
from typing import Any

import httpx

from chatgpt_api.providers.chatgpt.conversation_history import conversation_timeline


def voice_event(value: Any) -> dict[str, Any] | None:
    """Unwrap the data channel envelope; telemetry and audio are not retained."""
    try:
        if isinstance(value, str):
            value = json.loads(value)
        if isinstance(value, dict) and value.get("type") == "data_message":
            value = json.loads(value["data"]) if isinstance(value.get("data"), str) else value.get("data")
    except (ValueError, TypeError):
        return None
    if not isinstance(value, dict) or not isinstance(value.get("type"), str) or value["type"] not in {
        "startup_telemetry", "conversation_update", "chat_message_delta",
    }:
        return None
    return value


class VoiceMessageStream:
    """Maintain the compressed c/p/o delta state for one negotiated data channel.

    ChatGPT omits unchanged operations/paths in consecutive frames. A patch
    can therefore contain only ``v``; decoding each frame alone loses words.
    """

    def __init__(self) -> None:
        self.documents: OrderedDict[int, dict[str, Any]] = OrderedDict()
        self.counter = 0
        self.path = ""
        self.operation = "add"

    def apply(self, event: dict[str, Any]) -> dict[str, Any] | None:
        payload = event.get("payload", event)
        if not isinstance(payload, dict):
            return None
        delta = payload.get("delta")
        if not isinstance(delta, dict):
            return None
        if "c" in delta:
            if not isinstance(delta["c"], int) or isinstance(delta["c"], bool) or delta["c"] < 0:
                return None
            self.counter = delta["c"]
        if isinstance(delta.get("p"), str):
            self.path = delta["p"]
        if isinstance(delta.get("o"), str):
            self.operation = delta["o"]
        if self.path == "" and self.operation in {"add", "replace"} and isinstance(delta.get("v"), dict):
            self.documents[self.counter] = copy.deepcopy(delta["v"])
            while len(self.documents) > 128:
                self.documents.popitem(last=False)
        else:
            document = self.documents.get(self.counter)
            if document is None:
                return None  # A missing root cannot be reconstructed from a text suffix.
            if self.operation == "patch" and isinstance(delta.get("v"), list):
                for patch in delta["v"]:
                    if isinstance(patch, dict):
                        _patch(document, patch.get("p"), patch.get("o"), patch.get("v"))
            else:
                _patch(document, self.path, self.operation, delta.get("v"))
        document = self.documents.get(self.counter, {})
        timeline = conversation_timeline({"mapping": {"live": {"message": document.get("message")}}})
        return timeline["messages"][0] if timeline["messages"] else None


def _patch(document: dict[str, Any], path: Any, operation: Any, value: Any) -> None:
    if not isinstance(path, str) or not path.startswith("/message/"):
        return
    pieces = [piece.replace("~1", "/").replace("~0", "~") for piece in path[1:].split("/")]
    try:
        target: Any = document
        for piece in pieces[:-1]:
            target = target[int(piece)] if isinstance(target, list) else target[piece]
        key: Any = int(pieces[-1]) if isinstance(target, list) else pieces[-1]
        if operation == "append":
            current = target[key]
            if isinstance(current, str) and isinstance(value, str):
                target[key] = current + value
            elif isinstance(current, list) and isinstance(value, list):
                current.extend(copy.deepcopy(value))
        elif operation in {"add", "replace"}:
            if isinstance(target, list) and operation == "add":
                target.insert(key, copy.deepcopy(value))
            else:
                target[key] = copy.deepcopy(value)
        elif operation == "remove":
            del target[key]
    except (KeyError, IndexError, ValueError, TypeError):
        return


class VoiceEventRelay:
    """Forward an ordered data channel to the bridge without blocking audio."""

    def __init__(self, bridge_url: str, api_key: str, session_id: str, conversation: Any) -> None:
        self.url = f"{bridge_url}/v1/chatgpt/voice/sessions/{session_id}/events"
        self.api_key = api_key
        self.conversation = conversation
        self.queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=1024)
        self.sequence = 0
        self.closed = False
        self.task: asyncio.Task[Any] | None = None

    def enqueue(self, raw: Any) -> None:
        event = voice_event(raw)
        if self.closed or event is None:
            return
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            self.closed = True
            if self.task:
                self.task.cancel()
            print("Voice text stream overflow; reload the conversation history", flush=True)
            return
        if self.task is None:
            self.task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        async with httpx.AsyncClient(timeout=5) as client:
            while True:
                first = await self.queue.get()
                if first is None:
                    return
                await asyncio.sleep(0.1)
                batch = [first]
                while len(batch) < 50 and not self.queue.empty():
                    event = self.queue.get_nowait()
                    if event is None:
                        break
                    batch.append(event)
                attempt = 0
                while True:
                    try:
                        response = await client.post(
                            self.url, json={"sequence": self.sequence, "events": batch},
                            headers={"Authorization": f"Bearer {self.api_key}"},
                        )
                        if response.status_code in {400, 401, 404}:
                            print(f"Voice text stream rejected: HTTP {response.status_code}", flush=True)
                            self.closed = True
                            return
                        response.raise_for_status()
                        conversation_id = response.json().get("conversation_id")
                        if conversation_id:
                            self.conversation(conversation_id)
                        self.sequence += 1
                        break
                    except httpx.HTTPError:
                        if attempt == 0:
                            print("Voice text stream reconnecting; audio is unaffected", flush=True)
                        await asyncio.sleep(min(2 ** attempt, 10))
                        attempt += 1
                if self.closed and self.queue.empty():
                    return

    async def close(self) -> None:
        self.closed = True
        if self.task is None:
            return
        try:
            self.queue.put_nowait(None)
            await asyncio.wait_for(self.task, timeout=3)
        except (TimeoutError, asyncio.QueueFull, asyncio.CancelledError):
            self.task.cancel()
