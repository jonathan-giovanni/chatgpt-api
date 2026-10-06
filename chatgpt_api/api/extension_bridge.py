"""Pair a normal Chrome profile with the local bridge without exposing API keys.

Only the dashboard can approve a pairing. Session material is encrypted in the
extension before it crosses the network and is written to the existing encrypted
account capture after its identity has been checked.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidTag

from chatgpt_api.api.admin_store import BridgeAdminStore
from chatgpt_api.providers.chatgpt.account_info import detect_account_info
from chatgpt_api.providers.chatgpt.accounts import resolve_account_capture_path
from chatgpt_api.providers.chatgpt.crypto import encrypt_text, load_secrets_key
from chatgpt_api.providers.chatgpt.request_capture import CapturedRequest


_LOCK = threading.RLock()
_PENDING: dict[str, "PendingPair"] = {}
_KEYS: dict[str, rsa.RSAPrivateKey] = {}
_PAIR_TTL = 600
_CLIENTS_KEY = "chrome_extension_clients"


@dataclass
class PendingPair:
    client_id: str
    client_name: str
    public_key: rsa.RSAPublicKey
    code: str
    expires_at: float
    encrypted_token: str | None = None
    account: str | None = None


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _decode(value: Any, *, limit: int = 65536) -> bytes:
    if not isinstance(value, str) or len(value) > limit * 2:
        raise ValueError("invalid encoded value")
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise ValueError("invalid encoded value") from exc
    if len(raw) > limit:
        raise ValueError("encoded value is too large")
    return raw


def _server_key(accounts_dir: Path) -> rsa.RSAPrivateKey:
    path = accounts_dir.expanduser().resolve() / ".extension-bridge-key.pem"
    with _LOCK:
        cached = _KEYS.get(str(path))
        if cached:
            return cached
        if path.exists():
            loaded = serialization.load_pem_private_key(path.read_bytes(), password=None)
            if not isinstance(loaded, rsa.RSAPrivateKey):
                raise ValueError("invalid extension bridge key")
            key = loaded
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
            encoded = key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
            # Private key lives beside the ignored, encrypted account captures.
            with path.open("xb") as output:
                output.write(encoded)
            try:
                path.chmod(0o600)
            except OSError:
                pass
        _KEYS[str(path)] = key
        return key


def discovery(accounts_dir: Path) -> dict[str, Any]:
    public = _server_key(accounts_dir).public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return {
        "service": "chatgpt-bridge-extension",
        "version": 1,
        "public_key": _b64(public),
        "fingerprint": hashlib.sha256(public).hexdigest(),
    }


def request_pair(body: dict[str, Any]) -> dict[str, Any]:
    client_id = str(body.get("client_id") or "")
    if not re.fullmatch(r"[a-f0-9-]{36}", client_id):
        raise ValueError("invalid client id")
    public = serialization.load_der_public_key(_decode(body.get("public_key"), limit=1024))
    if not isinstance(public, rsa.RSAPublicKey) or public.key_size < 2048:
        raise ValueError("invalid extension public key")
    name = str(body.get("client_name") or "Chrome")[:80]
    request_id = secrets.token_urlsafe(24)
    code = f"{secrets.randbelow(1000000):06d}"
    with _LOCK:
        _prune_pending()
        if len(_PENDING) >= 20:
            raise ValueError("too many pending extension pairings")
        _PENDING[request_id] = PendingPair(client_id, name, public, code, time.time() + _PAIR_TTL)
    return {"request_id": request_id, "code": code, "expires_in": _PAIR_TTL}


def _prune_pending() -> None:
    now = time.time()
    for request_id, pending in list(_PENDING.items()):
        if pending.expires_at <= now:
            del _PENDING[request_id]


def pending_pairs() -> list[dict[str, Any]]:
    with _LOCK:
        _prune_pending()
        return [
            {"request_id": request_id, "code": item.code, "client_name": item.client_name,
             "expires_at": datetime.fromtimestamp(item.expires_at, timezone.utc).isoformat()}
            for request_id, item in _PENDING.items() if item.encrypted_token is None
        ]


def _clients(store: BridgeAdminStore) -> dict[str, Any]:
    value = store.get_setting(_CLIENTS_KEY, {})
    return value if isinstance(value, dict) else {}


def approve_pair(store: BridgeAdminStore, request_id: str, account: str, known_accounts: list[str]) -> dict[str, Any]:
    if account not in known_accounts:
        raise ValueError("select an existing account")
    with _LOCK:
        _prune_pending()
        item = _PENDING.get(request_id)
        if item is None or item.encrypted_token is not None:
            raise ValueError("pairing request expired or already approved")
        token = secrets.token_urlsafe(48)
        encrypted = item.public_key.encrypt(token.encode(), padding.OAEP(
            mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None
        ))
        clients = _clients(store)
        clients[item.client_id] = {
            "token_hash": hashlib.sha256(token.encode()).hexdigest(),
            "account": account,
            "client_name": item.client_name,
            "paired_at": time.time(),
            "last_sync_at": None,
        }
        store.set_setting(_CLIENTS_KEY, clients)
        item.encrypted_token = _b64(encrypted)
        item.account = account
    return {"approved": True, "account": account}


def pair_status(request_id: str) -> tuple[int, dict[str, Any]]:
    with _LOCK:
        _prune_pending()
        item = _PENDING.get(request_id)
        if item is None:
            return 404, {"state": "expired"}
        if item.encrypted_token:
            return 200, {"state": "approved", "encrypted_token": item.encrypted_token, "account": item.account}
        return 200, {"state": "pending"}


def _open_envelope(accounts_dir: Path, body: dict[str, Any]) -> dict[str, Any]:
    encrypted_key = _decode(body.get("key"), limit=1024)
    nonce = _decode(body.get("nonce"), limit=32)
    ciphertext = _decode(body.get("ciphertext"), limit=131072)
    if len(nonce) != 12:
        raise ValueError("invalid nonce")
    try:
        key = _server_key(accounts_dir).decrypt(encrypted_key, padding.OAEP(
            mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None
        ))
        value = json.loads(AESGCM(key).decrypt(nonce, ciphertext, b"chatgpt-bridge-extension-v1"))
    except (InvalidTag, ValueError, TypeError) as exc:
        raise ValueError("invalid encrypted payload") from exc
    if not isinstance(value, dict):
        raise ValueError("invalid encrypted payload")
    return value


def _authenticated_client(store: BridgeAdminStore, value: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    client_id = str(value.get("client_id") or "")
    token = str(value.get("token") or "")
    client = _clients(store).get(client_id)
    if not client or not token or not secrets.compare_digest(
        str(client.get("token_hash") or ""), hashlib.sha256(token.encode()).hexdigest()
    ):
        raise PermissionError("extension pairing is invalid or revoked")
    return client_id, client


def client_health(accounts_dir: Path, store: BridgeAdminStore, body: dict[str, Any]) -> dict[str, Any]:
    value = _open_envelope(accounts_dir, body)
    _, client = _authenticated_client(store, value)
    account = str(client["account"])
    path = resolve_account_capture_path(account, accounts_dir)
    if not path.is_file():
        return {"account": account, "state": "missing_capture", "last_sync_at": client.get("last_sync_at")}
    info = detect_account_info(CapturedRequest.from_file(path))
    expires = info.token_expires_at
    seconds = None
    if expires:
        seconds = int(datetime.fromisoformat(expires).timestamp() - time.time())
    state = "expired" if seconds is not None and seconds <= 0 else "expiring" if seconds is not None and seconds < 4 * 86400 else "ok" if seconds is not None else "unknown"
    return {"account": account, "state": state, "token_expires_at": expires,
            "seconds_remaining": seconds, "last_sync_at": client.get("last_sync_at")}


def sync_session(accounts_dir: Path, store: BridgeAdminStore, body: dict[str, Any]) -> dict[str, Any]:
    value = _open_envelope(accounts_dir, body)
    client_id, client = _authenticated_client(store, value)
    account = str(client["account"])
    access_token = str(value.get("access_token") or "")
    cookies = value.get("cookies")
    browser_email = str(value.get("email") or "").strip().lower()
    if len(access_token) > 16000 or not re.fullmatch(r"[^\s.]+\.[^\s.]+\.[^\s.]+", access_token):
        raise ValueError("ChatGPT session has no usable access token")
    if not isinstance(cookies, list) or not 0 < len(cookies) <= 200:
        raise ValueError("ChatGPT cookies are missing")
    cookie_parts = []
    for cookie in cookies:
        if not isinstance(cookie, dict):
            raise ValueError("invalid cookie")
        name, content = cookie.get("name"), cookie.get("value")
        if not isinstance(name, str) or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name):
            raise ValueError("invalid cookie name")
        if not isinstance(content, str) or len(content) > 12000 or any(c in content for c in "\r\n;"):
            raise ValueError("invalid cookie value")
        cookie_parts.append(f"{name}={content}")
    cookie_header = "; ".join(cookie_parts)
    path = resolve_account_capture_path(account, accounts_dir)
    if not path.is_file():
        raise ValueError("paired account has no capture; register it manually first")
    old = CapturedRequest.from_file(path)
    old_info = detect_account_info(old)
    new = CapturedRequest(url=old.url, status=old.status, headers={**old.headers, "authorization": f"Bearer {access_token}", "cookie": cookie_header}, request_json=old.request_json)
    new_info = detect_account_info(new)
    if not new_info.token_expires_at:
        raise ValueError("ChatGPT access token has no expiry claim")
    if datetime.fromisoformat(new_info.token_expires_at).timestamp() <= time.time() + 60:
        raise ValueError("ChatGPT access token is expired")
    if old_info.user_id and new_info.user_id and old_info.user_id != new_info.user_id:
        raise ValueError("browser session belongs to another ChatGPT user")
    if old_info.account_id and new_info.account_id and old_info.account_id != new_info.account_id:
        raise ValueError("browser session belongs to another ChatGPT account")
    if old_info.email and browser_email and old_info.email.lower() != browser_email:
        raise ValueError("browser email does not match paired account")
    if old_info.email and new_info.email and old_info.email.lower() != new_info.email.lower():
        raise ValueError("token email does not match paired account")
    if not old.url or not old.url.startswith("https://chatgpt.com/"):
        raise ValueError("existing capture URL is invalid")
    if old.request_json is None and "/backend-api/f/conversation/prepare" not in old.url:
        raise ValueError("existing capture has no request body")
    lines = [f"URL: {old.url}", f"Status: {old.status or 200}"]
    lines.extend(f"{name}: {content}" for name, content in new.headers.items())
    if old.request_json is not None:
        lines.append("Request Data: " + json.dumps(old.request_json, ensure_ascii=False, separators=(",", ":")))
    rendered = "\n".join(lines) + "\n"
    parsed = CapturedRequest.from_text(rendered)
    if parsed.headers.get("authorization") != f"Bearer {access_token}" or parsed.headers.get("cookie") != cookie_header:
        raise ValueError("refreshed capture failed validation")
    key = load_secrets_key(accounts_dir)
    temp_path = path.with_name(path.name + ".extension-tmp")
    try:
        temp_path.write_text(encrypt_text(rendered, key), encoding="utf-8")
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)
    with _LOCK:
        clients = _clients(store)
        if client_id in clients:
            clients[client_id]["last_sync_at"] = time.time()
            store.set_setting(_CLIENTS_KEY, clients)
    return {"ok": True, "account": account, "token_expires_at": new_info.token_expires_at}


def list_clients(store: BridgeAdminStore) -> list[dict[str, Any]]:
    return [
        {"client_id": client_id, "account": value.get("account"), "client_name": value.get("client_name"),
         "paired_at": value.get("paired_at"), "last_sync_at": value.get("last_sync_at")}
        for client_id, value in _clients(store).items()
    ]


def revoke_client(store: BridgeAdminStore, client_id: str) -> bool:
    with _LOCK:
        clients = _clients(store)
        removed = clients.pop(client_id, None) is not None
        store.set_setting(_CLIENTS_KEY, clients)
        return removed
