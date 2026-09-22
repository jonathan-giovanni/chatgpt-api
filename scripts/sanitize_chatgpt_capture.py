"""Create a structurally useful, irreversibly redacted ChatGPT capture summary."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from chatgpt_api.providers.chatgpt.request_capture import CapturedRequest


SAFE_STRING_KEYS = {
    "action",
    "client_prepare_state",
    "content_type",
    "force_parallel_switch",
    "kind",
    "model",
    "paragen_cot_summary_display_override",
    "thinking_effort",
    "timezone",
    "variant_purpose",
}
ID_KEY = re.compile(r"(?:^id$|_id$|uuid$)", re.IGNORECASE)
UUID_SEGMENT = re.compile(
    r"(?i)(?:g-p-)?[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}|g-p-[0-9a-f]{24,}"
)


def sanitized_url(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlsplit(value)
    path = UUID_SEGMENT.sub("<id>", parsed.path)
    query_keys = sorted(part.split("=", 1)[0] for part in parsed.query.split("&") if part)
    return f"{parsed.scheme}://{parsed.netloc}{path}" + (
        f"?<{','.join(query_keys)}>" if query_keys else ""
    )


def sanitize(value: Any, *, key: str = "") -> Any:
    if isinstance(value, dict):
        return {str(child_key): sanitize(child, key=str(child_key)) for child_key, child in value.items()}
    if isinstance(value, list):
        return [sanitize(child, key=key) for child in value]
    if isinstance(value, str):
        if ID_KEY.search(key):
            return "<redacted-id>"
        if key in SAFE_STRING_KEYS and len(value) <= 100:
            return value
        if "url" in key.lower():
            return sanitized_url(value)
        return "<redacted-string>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return f"<redacted-{type(value).__name__}>"


def capture_summary(capture: CapturedRequest) -> dict[str, Any]:
    return {
        "url": sanitized_url(capture.url),
        "status": capture.status,
        "header_names": sorted(capture.headers),
        "cookie_names": sorted(capture.cookies),
        "request_json": sanitize(capture.request_json or {}),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    summary = capture_summary(CapturedRequest.from_file(args.capture.expanduser().resolve()))
    serialized = json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        output = args.output.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(serialized, encoding="utf-8")
        print(output)
    else:
        print(serialized, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
