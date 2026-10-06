"""Encrypted Chrome session handoff for an existing, identity-matched account."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidTag

from chatgpt_api.api.admin_store import BridgeAdminStore
from chatgpt_api.providers.chatgpt.account_info import detect_account_info
from chatgpt_api.providers.chatgpt.accounts import list_account_profiles, resolve_account_capture_path
from chatgpt_api.providers.chatgpt.crypto import encrypt_text, load_secrets_key
from chatgpt_api.providers.chatgpt.request_capture import CapturedRequest


_LOCK = threading.RLock()
_KEYS: dict[str, rsa.RSAPrivateKey] = {}
_CLIENTS_KEY = "chrome_extension_clients"


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


def _clients(store: BridgeAdminStore) -> dict[str, Any]:
    value = store.get_setting(_CLIENTS_KEY, {})
    return value if isinstance(value, dict) else {}


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
    state = "expired" if seconds is not None and seconds <= 0 else "expiring" if seconds is not None and seconds < 2 * 86400 else "ok" if seconds is not None else "unknown"
    if client.get("last_error") in {"needs_login", "invalid_session"}:
        state = "revoked"
    return {"account": account, "state": state, "token_expires_at": expires,
            "seconds_remaining": seconds, "last_sync_at": client.get("last_sync_at")}


def sync_session(accounts_dir: Path, store: BridgeAdminStore, body: dict[str, Any]) -> dict[str, Any]:
    value = _open_envelope(accounts_dir, body)
    client_id, client = _authenticated_client(store, value)
    return _sync_session_value(accounts_dir, store, client_id, client, value)


def _sync_session_value(
    accounts_dir: Path, store: BridgeAdminStore, client_id: str,
    client: dict[str, Any], value: dict[str, Any],
) -> dict[str, Any]:
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
            clients[client_id]["last_error"] = None
            store.set_setting(_CLIENTS_KEY, clients)
    return {"ok": True, "account": account, "token_expires_at": new_info.token_expires_at}


def _identity_matches(old: Any, new: Any) -> int:
    """Return a match score; conflicting claims never select an account."""
    score = 0
    for field in ("user_id", "account_id", "email"):
        before = getattr(old, field)
        after = getattr(new, field)
        if before and after:
            if str(before).lower() != str(after).lower():
                return -1
            score += 1
    return score


def _verify_browser_session(value: dict[str, Any]) -> str:
    """Confirm the browser cookies with ChatGPT before granting bridge access."""
    from curl_cffi import requests

    cookie_parts = value.get("cookies")
    if not isinstance(cookie_parts, list):
        raise ValueError("ChatGPT cookies are missing")
    cookie = "; ".join(
        f"{item.get('name')}={item.get('value')}" for item in cookie_parts
        if isinstance(item, dict) and isinstance(item.get("name"), str) and isinstance(item.get("value"), str)
    )
    try:
        response = requests.get(
            "https://chatgpt.com/api/auth/session",
            headers={"cookie": cookie, "accept": "application/json"},
            impersonate="chrome", timeout=12, allow_redirects=False,
        )
        session = response.json() if response.status_code == 200 else {}
    except Exception as exc:
        raise ValueError("ChatGPT session could not be verified") from exc
    verified_token = session.get("accessToken") if isinstance(session, dict) else None
    if not isinstance(verified_token, str) or not verified_token:
        raise ValueError("ChatGPT did not confirm the browser session")
    return verified_token


def activate_session(
    accounts_dir: Path, store: BridgeAdminStore, body: dict[str, Any],
) -> dict[str, Any]:
    """Pair inside the extension when its live session matches one saved capture."""
    value = _open_envelope(accounts_dir, body)
    client_id = str(value.get("client_id") or "")
    if not re.fullmatch(r"[a-f0-9-]{36}", client_id):
        raise ValueError("invalid client id")
    public = serialization.load_der_public_key(_decode(value.get("client_public_key"), limit=1024))
    if not isinstance(public, rsa.RSAPublicKey) or public.key_size < 2048:
        raise ValueError("invalid extension public key")
    access_token = str(value.get("access_token") or "")
    if len(access_token) > 16000 or not re.fullmatch(r"[^\s.]+\.[^\s.]+\.[^\s.]+", access_token):
        raise ValueError("ChatGPT session has no usable access token")
    new_info = detect_account_info(CapturedRequest(headers={"authorization": f"Bearer {access_token}"}))
    verified_token = _verify_browser_session(value)
    verified_info = detect_account_info(CapturedRequest(headers={"authorization": f"Bearer {verified_token}"}))
    if _identity_matches(new_info, verified_info) < 1:
        raise ValueError("ChatGPT session identity does not match the browser token")
    if not verified_info.token_expires_at or datetime.fromisoformat(verified_info.token_expires_at).timestamp() <= time.time() + 60:
        raise ValueError("ChatGPT access token is expired")
    browser_email = str(value.get("email") or "").strip().lower()
    if browser_email and verified_info.email and browser_email != verified_info.email.lower():
        raise ValueError("ChatGPT browser email does not match the access token")
    matches: list[tuple[int, str]] = []
    for profile in list_account_profiles(accounts_dir):
        if not profile.exists:
            continue
        try:
            old_info = detect_account_info(CapturedRequest.from_file(profile.capture_path))
        except (OSError, ValueError):
            continue
        score = _identity_matches(old_info, verified_info)
        if score >= 1:
            matches.append((score, profile.name))
    if not matches:
        raise ValueError("This Chrome session does not match a registered bridge account")
    matches.sort(reverse=True)
    if len(matches) > 1 and matches[0][0] == matches[1][0]:
        raise ValueError("More than one registered account matches this Chrome session")
    value["access_token"] = verified_token
    account = matches[0][1]
    token = secrets.token_urlsafe(48)
    client = {
        "token_hash": hashlib.sha256(token.encode()).hexdigest(),
        "account": account,
        "client_name": "Chrome",
        "paired_at": time.time(),
        "last_sync_at": None,
    }
    with _LOCK:
        clients = _clients(store)
        prior = clients.get(client_id)
        clients[client_id] = client
        store.set_setting(_CLIENTS_KEY, clients)
    try:
        synced = _sync_session_value(accounts_dir, store, client_id, client, value)
    except Exception:
        with _LOCK:
            clients = _clients(store)
            if prior is None:
                clients.pop(client_id, None)
            else:
                clients[client_id] = prior
            store.set_setting(_CLIENTS_KEY, clients)
        raise
    encrypted = public.encrypt(token.encode(), padding.OAEP(
        mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None
    ))
    return {"account": account, "encrypted_token": _b64(encrypted),
            "token_expires_at": synced["token_expires_at"]}


def list_clients(store: BridgeAdminStore) -> list[dict[str, Any]]:
    return [
        {"client_id": client_id, "account": value.get("account"), "client_name": value.get("client_name"),
         "paired_at": value.get("paired_at"), "last_sync_at": value.get("last_sync_at"),
         "last_error": value.get("last_error")}
        for client_id, value in _clients(store).items()
    ]


def revoke_client(store: BridgeAdminStore, client_id: str) -> bool:
    with _LOCK:
        clients = _clients(store)
        removed = clients.pop(client_id, None) is not None
        store.set_setting(_CLIENTS_KEY, clients)
        return removed


def report_client_state(accounts_dir: Path, store: BridgeAdminStore, body: dict[str, Any]) -> dict[str, Any]:
    value = _open_envelope(accounts_dir, body)
    client_id, _ = _authenticated_client(store, value)
    state = str(value.get("state") or "")
    if state not in {"needs_login", "invalid_session", "network_error", "ok"}:
        raise ValueError("invalid extension state")
    with _LOCK:
        clients = _clients(store)
        clients[client_id]["last_error"] = None if state == "ok" else state
        store.set_setting(_CLIENTS_KEY, clients)
    return {"ok": True}


def account_statuses(accounts_dir: Path, store: BridgeAdminStore) -> list[dict[str, Any]]:
    latest_client: dict[str, dict[str, Any]] = {}
    for client in _clients(store).values():
        account = str(client.get("account") or "")
        if account and float(client.get("paired_at") or 0) >= float(latest_client.get(account, {}).get("paired_at") or 0):
            latest_client[account] = client
    result = []
    for profile in list_account_profiles(accounts_dir):
        if not profile.exists:
            continue
        try:
            info = detect_account_info(CapturedRequest.from_file(profile.capture_path))
            expires = info.token_expires_at
            seconds = int(datetime.fromisoformat(expires).timestamp() - time.time()) if expires else None
            state = "expired" if seconds is not None and seconds <= 0 else "expiring" if seconds is not None and seconds < 2 * 86400 else "ready" if seconds is not None else "unknown"
        except (OSError, ValueError):
            expires, seconds, state = None, None, "invalid_capture"
        client = latest_client.get(profile.name, {})
        if client.get("last_error") in {"needs_login", "invalid_session"}:
            state = "revoked"
        elif client.get("last_error") == "network_error" and state == "ready":
            state = "warning"
        result.append({"account": profile.name, "state": state,
                       "token_expires_at": expires, "seconds_remaining": seconds,
                       "last_sync_at": client.get("last_sync_at")})
    return result
