"""Extract the visible text timeline from a ChatGPT Web conversation snapshot."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def conversation_timeline(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Follow the active branch and return only user/assistant text messages.

    ChatGPT can keep alternate branches and non-visible tool nodes in ``mapping``.
    Following ``current_node`` avoids presenting those as spoken turns.
    """
    mapping = snapshot.get("mapping")
    if not isinstance(mapping, dict):
        mapping = {}
    current_node = snapshot.get("current_node")
    nodes: list[dict[str, Any]] = []
    if isinstance(current_node, str) and current_node in mapping:
        seen: set[str] = set()
        while current_node in mapping and current_node not in seen:
            seen.add(current_node)
            node = mapping[current_node]
            if not isinstance(node, dict):
                break
            nodes.append(node)
            current_node = node.get("parent")
        nodes.reverse()
    else:
        nodes = [node for node in mapping.values() if isinstance(node, dict)]
        nodes.sort(key=lambda node: _sort_time(node.get("message")))

    messages: list[dict[str, Any]] = []
    untranscribed_audio = 0
    for node in nodes:
        message = node.get("message")
        if not isinstance(message, dict):
            continue
        author = message.get("author")
        role = author.get("role") if isinstance(author, dict) else None
        if role not in {"user", "assistant"} or message.get("recipient") not in (None, "all"):
            continue
        metadata = message.get("metadata")
        if isinstance(metadata, dict) and metadata.get("is_visually_hidden_from_conversation"):
            continue
        content = message.get("content")
        text = _content_text(content)
        if not text:
            if isinstance(content, dict) and "audio" in str(content.get("content_type", "")):
                untranscribed_audio += 1
            continue
        message_id = message.get("id") or node.get("id")
        if not isinstance(message_id, str) or not message_id:
            continue
        messages.append({
            "id": message_id,
            "role": role,
            "text": text,
            "created_at": _iso_time(message.get("create_time") or node.get("create_time")),
            "status": message.get("status") if isinstance(message.get("status"), str) else None,
        })
    return {"messages": messages, "untranscribed_audio_messages": untranscribed_audio}


def _content_text(content: Any) -> str:
    if not isinstance(content, dict):
        return ""
    parts = content.get("parts")
    if isinstance(parts, list):
        text = "\n".join(filter(None, (_text_part(part) for part in parts)))
        if text.strip():
            return text.strip()
    for field in ("text", "transcription"):
        value = content.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _text_part(part: Any) -> str:
    if isinstance(part, str):
        return part.strip()
    if not isinstance(part, dict):
        return ""
    for field in ("text", "transcription"):
        value = part.get(field)
        if isinstance(value, str):
            return value.strip()
    return ""


def _sort_time(message: Any) -> float:
    if isinstance(message, dict):
        value = message.get("create_time")
        if isinstance(value, (int, float)):
            return float(value)
    return 0.0


def _iso_time(value: Any) -> str | None:
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str) and value.strip():
        return value
    return None
