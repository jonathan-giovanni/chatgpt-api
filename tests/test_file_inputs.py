import base64

import pytest

import chatgpt_api.api.file_inputs as file_inputs
from chatgpt_api.api.file_inputs import file_content_part, validate_file_parts
from chatgpt_api.core.types import ContentPart, Message


def _encoded(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _wav_bytes() -> bytes:
    return b"RIFF" + (36).to_bytes(4, "little") + b"WAVEfmt " + b"\0" * 28


@pytest.mark.parametrize(
    ("filename", "mime_type"),
    [
        ("notes.txt", "text/plain"),
        ("notes.md", "text/markdown"),
        ("data.csv", "text/csv"),
        ("data.json", "application/json"),
    ],
)
def test_file_content_part_accepts_supported_utf8_text(filename, mime_type):
    data = "área 42".encode("utf-8")

    part = file_content_part(
        {"type": "file", "file": {"filename": filename, "file_data": _encoded(data)}}
    )

    assert part == ContentPart.file_bytes(data, mime_type, filename)


def test_file_content_part_accepts_matching_data_url_and_utf8_bom():
    data = b"\xef\xbb\xbfhello"

    part = file_content_part(
        {
            "type": "file",
            "file": {
                "filename": "notes.txt",
                "file_data": f"data:text/plain;base64,{_encoded(data)}",
            },
        }
    )

    assert part.data == data


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        ({"filename": "notes.pdf", "file_data": "YQ=="}, "supported text filenames"),
        ({"filename": "../notes.txt", "file_data": "YQ=="}, "filename must be a basename"),
        ({"filename": "notes.txt", "file_data": "%%%"}, "valid base64"),
        ({"filename": "notes.txt", "file_data": "data:text/csv;base64,YQ=="}, "MIME type"),
        ({"filename": "notes.txt", "file_data": _encoded(b"a\0b")}, "NUL bytes"),
        ({"filename": "notes.txt", "file_data": _encoded(b"\xff")}, "UTF-8"),
        ({"file_id": "file_123", "filename": "notes.txt"}, "file_id is unsupported"),
    ],
)
def test_file_content_part_rejects_invalid_text_input(payload, error):
    with pytest.raises(ValueError, match=error):
        file_content_part({"type": "file", "file": payload})


@pytest.mark.parametrize(
    ("fmt", "data", "mime_type"),
    [
        ("wav", _wav_bytes(), "audio/wav"),
        ("mp3", b"ID3\x04\x00\x00", "audio/mpeg"),
        ("mp3", b"\xff\xfb\x90\x64", "audio/mpeg"),
    ],
)
def test_file_content_part_accepts_wav_and_mp3(fmt, data, mime_type):
    part = file_content_part(
        {"type": "input_audio", "input_audio": {"format": fmt, "data": _encoded(data)}}
    )

    assert part == ContentPart.file_bytes(data, mime_type, f"audio.{fmt}")


@pytest.mark.parametrize(
    ("fmt", "data", "error"),
    [
        ("ogg", b"OggS", "wav or mp3"),
        ("wav", b"not-wave", "not a WAV"),
        ("mp3", b"not-mp3", "not an MP3"),
    ],
)
def test_file_content_part_rejects_unsupported_or_invalid_audio(fmt, data, error):
    with pytest.raises(ValueError, match=error):
        file_content_part(
            {"type": "input_audio", "input_audio": {"format": fmt, "data": _encoded(data)}}
        )


def test_validate_file_parts_requires_user_role():
    messages = [Message("assistant", [ContentPart.file_bytes(b"a", "text/plain", "a.txt")])]

    with pytest.raises(ValueError, match="role=user"):
        validate_file_parts(messages)


def test_validate_file_parts_counts_images_and_files_together():
    parts = [ContentPart.file_bytes(b"a", "text/plain", "a.txt")]
    parts.extend(ContentPart.image_url(f"https://example.test/{index}.png") for index in range(10))

    with pytest.raises(ValueError, match="at most 10 attachments"):
        validate_file_parts([Message("user", parts)])


def test_validate_file_parts_enforces_total_byte_limit(monkeypatch):
    monkeypatch.setattr(file_inputs, "MAX_TOTAL_FILE_BYTES", 3)
    messages = [Message("user", [ContentPart.file_bytes(b"four", "text/plain", "a.txt")])]

    with pytest.raises(ValueError, match="total limit"):
        validate_file_parts(messages)
