"""Signalling for ChatGPT Web Voice. Media stays on the WebRTC peer connection."""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any

from chatgpt_api.core.errors import ProviderError
from chatgpt_api.providers.chatgpt.auth import ChatGPTAuthConfig
from chatgpt_api.providers.chatgpt.timezone import local_timezone_payload

VOICE_URL = "https://chatgpt.com/realtime/wm?dcid=0"
MAX_OFFER_BYTES = 65_536
SESSION_TTL_SECONDS = 3_600
VOICES = frozenset({"breeze", "cove", "ember", "fathom", "glimmer", "juniper", "maple", "orbit", "vale"})
_SESSION_ACCOUNTS: dict[str, tuple[str, float]] = {}
_SESSION_LOCK = threading.Lock()


class VoiceSignallingError(ProviderError):
    def __init__(self, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True, slots=True)
class VoiceAnswer:
    answer_sdp: str
    bridge_session_id: str
    account: str


def _validated_offer(value: Any) -> str:
    if not isinstance(value, str) or not value.startswith("v=0"):
        raise VoiceSignallingError("offer_sdp must be a WebRTC SDP offer", 400)
    if len(value.encode("utf-8")) > MAX_OFFER_BYTES or "m=audio" not in value:
        raise VoiceSignallingError("offer_sdp must contain audio and be at most 64 KiB", 400)
    return value.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n").rstrip("\r\n") + "\r\n"


def _validated_session_id(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not re.fullmatch(r"vs_[a-f0-9]{32}", value):
        raise VoiceSignallingError("invalid bridge_session_id", 400)
    return value


def bound_account(session_id: str | None) -> str | None:
    now = time.monotonic()
    with _SESSION_LOCK:
        expired = [key for key, (_, deadline) in _SESSION_ACCOUNTS.items() if deadline <= now]
        for key in expired:
            _SESSION_ACCOUNTS.pop(key, None)
        record = _SESSION_ACCOUNTS.get(session_id) if session_id else None
        return record[0] if record else None


def release_session(session_id: Any) -> bool:
    validated = _validated_session_id(session_id)
    if not validated:
        raise VoiceSignallingError("bridge_session_id is required", 400)
    with _SESSION_LOCK:
        return _SESSION_ACCOUNTS.pop(validated, None) is not None


def negotiate_voice(
    *,
    offer_sdp: Any,
    voice: Any,
    account_order: tuple[str, ...],
    load_auth: Any,
    bridge_session_id: Any = None,
) -> VoiceAnswer:
    """Exchange one browser offer for one upstream answer, with bounded account failover."""
    offer = _validated_offer(offer_sdp)
    if not isinstance(voice, str) or voice not in VOICES:
        raise VoiceSignallingError("unsupported voice", 400)
    session_id = _validated_session_id(bridge_session_id)
    preferred = bound_account(session_id)
    accounts = tuple(dict.fromkeys(([preferred] if preferred in account_order else []) + list(account_order)))
    if not accounts:
        raise VoiceSignallingError("no ChatGPT account configured", 503)
    last_error: VoiceSignallingError | None = None
    for account in accounts[:3]:
        try:
            auth, impersonate = load_auth(account)
            answer = _post_offer(auth, impersonate, offer, voice)
        except VoiceSignallingError as exc:
            last_error = exc
            if exc.status == 401:
                continue
            raise
        session_id = session_id or "vs_" + uuid.uuid4().hex
        with _SESSION_LOCK:
            _SESSION_ACCOUNTS[session_id] = (account, time.monotonic() + SESSION_TTL_SECONDS)
        return VoiceAnswer(answer_sdp=answer, bridge_session_id=session_id, account=account)
    raise last_error or VoiceSignallingError("no account could establish voice", 503)


def _post_offer(auth: ChatGPTAuthConfig, impersonate: str, offer: str, voice: str) -> str:
    try:
        from curl_cffi import requests
    except ImportError as exc:
        raise VoiceSignallingError("curl_cffi is required for voice signalling", 503) from exc
    if not auth.access_token:
        raise VoiceSignallingError("ChatGPT account has no access token", 401)
    timezone = local_timezone_payload()
    voice_id = str(uuid.uuid4()).upper()
    session = {
        "voice": voice,
        "voice_mode": "wingman",
        "voice_session_id": voice_id,
        "voice_status_request_id": voice_id,
        "backend_reasoning_effort": "instant",
        "language_code": "auto",
        "requested_default_model": "",
        "model_slug": "",
        "model_slug_advanced": "",
        "client_tools": [],
        "conversation_mode": {"kind": "primary_assistant"},
        "enable_message_streaming": True,
        **timezone,
    }
    headers = {
        "authorization": f"Bearer {auth.access_token}",
        "origin": "https://chatgpt.com",
        "referer": "https://chatgpt.com/",
        "accept": "*/*",
    }
    for name in ("user-agent", "oai-device-id", "oai-client-version", "oai-client-build-number", "oai-language"):
        value = auth.headers.get(name)
        if value:
            headers[name] = value
    if auth.cookies:
        headers["cookie"] = "; ".join(f"{name}={value}" for name, value in auth.cookies.items())
    boundary = "----ChatGPTVoice" + uuid.uuid4().hex
    fields = {"sdp": offer, "session": json.dumps(session, separators=(",", ":"))}
    body = b"".join(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode("utf-8")
        for name, value in fields.items()
    ) + f"--{boundary}--\r\n".encode("ascii")
    headers["content-type"] = f"multipart/form-data; boundary={boundary}"
    try:
        response = requests.post(
            VOICE_URL,
            headers=headers,
            data=body,
            impersonate=impersonate,
            timeout=35,
        )
    except Exception as exc:
        raise VoiceSignallingError(f"voice signalling connection failed: {type(exc).__name__}") from exc
    if response.status_code == 401:
        raise VoiceSignallingError("ChatGPT account authentication expired", 401)
    if response.status_code == 429:
        raise VoiceSignallingError("ChatGPT voice limit reached", 429)
    if response.status_code not in {200, 201}:
        raise VoiceSignallingError(f"ChatGPT voice signalling returned HTTP {response.status_code}")
    answer = response.text.strip()
    if not answer.startswith("v=0") or "m=audio" not in answer:
        raise VoiceSignallingError("ChatGPT voice signalling did not return audio SDP")
    return answer
