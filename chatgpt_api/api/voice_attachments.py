"""Short-lived local attachment batches for starting a SIP voice conversation."""

from __future__ import annotations

import base64
import json
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from chatgpt_api.api.file_inputs import (
    MAX_FILE_BYTES, MAX_INPUT_FILES, MAX_TOTAL_FILE_BYTES, file_content_part,
)

BATCH_TTL_SECONDS = 24 * 60 * 60
_BATCH_ID = re.compile(r"sa_[a-f0-9]{32}\Z")


def _batch_dir(root: Path, batch_id: str) -> Path:
    if not _BATCH_ID.fullmatch(batch_id):
        raise ValueError("invalid attachment batch ID")
    return root / batch_id


def _remove_expired(root: Path) -> None:
    if not root.is_dir():
        return
    cutoff = time.time() - BATCH_TTL_SECONDS
    for path in root.iterdir():
        if not path.is_symlink() and path.is_dir() and _BATCH_ID.fullmatch(path.name) and path.stat().st_mtime < cutoff:
            if path.resolve().parent == root.resolve():
                shutil.rmtree(path)


def stage_voice_attachments(root: Path, body: dict[str, Any]) -> dict[str, Any]:
    files = body.get("files")
    if not isinstance(files, list) or not 1 <= len(files) <= MAX_INPUT_FILES:
        raise ValueError("files must contain 1 to 10 attachments")
    prepared: list[tuple[str, bytes]] = []
    total = 0
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("each attachment needs filename and file_data")
        part = file_content_part({"type": "file", "file": item})
        data = part.data or b""
        if len(data) > MAX_FILE_BYTES:
            raise ValueError("file exceeds 20 MiB")
        total += len(data)
        if total > MAX_TOTAL_FILE_BYTES:
            raise ValueError("attachments exceed 25 MiB total")
        prepared.append((part.name or item["filename"], data))
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    _remove_expired(root)
    batch_id = "sa_" + uuid.uuid4().hex
    folder = _batch_dir(root, batch_id)
    folder.mkdir(mode=0o700)
    manifest = {"created_at": time.time(), "files": []}
    try:
        for index, (filename, data) in enumerate(prepared):
            stored = f"{index}.bin"
            file_path = folder / stored
            file_path.write_bytes(data)
            file_path.chmod(0o600)
            manifest["files"].append({"filename": filename, "stored": stored})
        manifest_path = folder / "manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        manifest_path.chmod(0o600)
    except Exception:
        shutil.rmtree(folder)
        raise
    return {"attachment_batch_id": batch_id, "files": [name for name, _ in prepared],
            "expires_in": BATCH_TTL_SECONDS}


def load_voice_attachments(root: Path, batch_id: str) -> dict[str, Any]:
    folder = _batch_dir(root, batch_id)
    try:
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("attachment batch not found") from exc
    created = manifest.get("created_at")
    if not isinstance(created, (int, float)) or time.time() - created > BATCH_TTL_SECONDS:
        raise ValueError("attachment batch expired")
    entries = manifest.get("files")
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_INPUT_FILES:
        raise ValueError("invalid attachment batch")
    files = []
    total = 0
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or entry.get("stored") != f"{index}.bin":
            raise ValueError("invalid attachment batch")
        filename = entry.get("filename")
        if not isinstance(filename, str):
            raise ValueError("invalid attachment batch")
        file_path = folder / f"{index}.bin"
        if file_path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError("attachment batch file exceeds 20 MiB")
        data = file_path.read_bytes()
        total += len(data)
        if total > MAX_TOTAL_FILE_BYTES:
            raise ValueError("attachment batch exceeds 25 MiB")
        encoded = {"filename": filename, "file_data": base64.b64encode(data).decode("ascii")}
        file_content_part({"type": "file", "file": encoded})
        files.append(encoded)
    return {"files": files}
