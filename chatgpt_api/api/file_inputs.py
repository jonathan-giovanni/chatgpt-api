"""Bounded inline file inputs for Chat Completions (no URL/path fetching)."""

from __future__ import annotations

import base64
import binascii
from pathlib import PurePosixPath
from typing import Any

from chatgpt_api.core.types import ContentPart

# Conservative bridge limits, not claims about upstream account limits.
MAX_INPUT_FILES = 10
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_TOTAL_FILE_BYTES = 25 * 1024 * 1024
TEXT_MIME_TYPES = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".json": "application/json",
}
AUDIO_MIME_TYPES = {"wav": "audio/wav", "mp3": "audio/mpeg"}


def _decode_data(value: Any, expected_mime: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise ValueError("file data must be non-empty base64")
    if value.startswith("data:"):
        header, sep, value = value.partition(",")
        aliases = {"audio/x-wav": "audio/wav", "audio/mp3": "audio/mpeg"}
        mime = header[5:].removesuffix(";base64")
        if (
            not sep
            or not header.endswith(";base64")
            or aliases.get(mime, mime) != expected_mime
        ):
            raise ValueError("file data URL MIME type must match the filename/format")
    if len(value) > 4 * ((MAX_FILE_BYTES + 2) // 3):
        raise ValueError("file exceeds the bridge limit of 20 MiB")
    try:
        data = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("file data must be valid base64") from exc
    if not data or len(data) > MAX_FILE_BYTES:
        raise ValueError("file must contain 1 byte to 20 MiB")
    return data


def file_content_part(item: dict[str, Any]) -> ContentPart:
    audio = item.get("type") == "input_audio"
    value = item.get("input_audio" if audio else "file")
    if not isinstance(value, dict):
        raise ValueError("input_audio/file must be an object")
    if audio:
        fmt = value.get("format")
        if fmt not in AUDIO_MIME_TYPES:
            raise ValueError("input_audio.format must be wav or mp3")
        mime = AUDIO_MIME_TYPES[fmt]
        name = f"audio.{fmt}"
        data = _decode_data(value.get("data"), mime)
    else:
        if "file_id" in value:
            raise ValueError("file_id is unsupported; use inline file_data and filename")
        name = value.get("filename")
        if (
            not isinstance(name, str)
            or not name
            or len(name) > 200
            or any(c in name for c in "/\\")
            or any(ord(c) < 32 for c in name)
        ):
            raise ValueError("filename must be a basename of at most 200 characters")
        suffix = PurePosixPath(name).suffix.lower()
        mime = TEXT_MIME_TYPES.get(suffix)
        if mime is None:
            raise ValueError(
                "supported text filenames: .txt, .md, .csv, .json; use input_audio for audio"
            )
        data = _decode_data(value.get("file_data"), mime)
        try:
            data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("text files must use UTF-8 encoding") from exc
        if b"\0" in data:
            raise ValueError("text files must not contain NUL bytes")
    if audio and name.endswith(".wav") and not (
        data[:4] == b"RIFF" and data[8:12] == b"WAVE"
    ):
        raise ValueError("input_audio data is not a WAV container")
    if audio and name.endswith(".mp3") and not (
        data.startswith(b"ID3")
        or (len(data) >= 2 and data[0] == 255 and data[1] & 0xE0 == 0xE0)
    ):
        raise ValueError("input_audio data is not an MP3 container")
    return ContentPart.file_bytes(data, mime, name)


def validate_file_parts(messages: list[Any]) -> None:
    parts = [
        part
        for message in messages
        for part in message.content
        if part.kind == "file_bytes"
    ]
    if not parts:
        return
    if any(
        message.role != "user" and any(p.kind == "file_bytes" for p in message.content)
        for message in messages
    ):
        raise ValueError("file and input_audio parts require role=user")
    media_count = sum(
        p.kind in {"file_bytes", "image_bytes", "image_url"}
        for message in messages
        for p in message.content
    )
    if media_count > MAX_INPUT_FILES:
        raise ValueError("at most 10 attachments per request, including images")
    if (
        sum(len(p.data or b"") for message in messages for p in message.content)
        > MAX_TOTAL_FILE_BYTES
    ):
        raise ValueError("attachments exceed the bridge total limit of 25 MiB")
