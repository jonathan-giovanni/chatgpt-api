import base64
import json
import time

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from chatgpt_api.api.admin_store import BridgeAdminStore
from chatgpt_api.api import extension_bridge as bridge
from chatgpt_api.providers.chatgpt.request_capture import CapturedRequest


def _jwt(expires, email="person@example.com"):
    claims = {
        "exp": expires,
        "https://api.openai.com/auth": {"chatgpt_user_id": "user-1", "chatgpt_account_id": "acct-1"},
        "https://api.openai.com/profile": {"email": email},
    }
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"header.{payload}.signature"


def _seal(public_key, value):
    key = AESGCM.generate_key(bit_length=256)
    nonce = b"123456789012"
    raw = json.dumps(value).encode()
    ciphertext = AESGCM(key).encrypt(nonce, raw, b"chatgpt-bridge-extension-v1")
    public = serialization.load_der_public_key(base64.b64decode(public_key))
    encrypted_key = public.encrypt(key, padding.OAEP(
        mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None
    ))
    return {"key": bridge._b64(encrypted_key), "nonce": bridge._b64(nonce), "ciphertext": bridge._b64(ciphertext)}


def test_pair_and_encrypted_sync_updates_only_assigned_account(tmp_path):
    accounts_dir = tmp_path / "accounts"
    account_dir = accounts_dir / "work"
    account_dir.mkdir(parents=True)
    capture_path = account_dir / "chatgpt-request.txt"
    old_token = _jwt(int(time.time()) - 100)
    capture_path.write_text(
        "URL: https://chatgpt.com/backend-api/f/conversation\n"
        f"authorization: Bearer {old_token}\n"
        "cookie: old=1\n"
        'Request Data: {"action":"next","model":"auto"}\n', encoding="utf-8"
    )
    store = BridgeAdminStore(tmp_path / "admin.sqlite")
    server = bridge.discovery(accounts_dir)
    client = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    client_public = bridge._b64(client.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    ))
    client_id = "12345678-1234-1234-1234-123456789abc"
    request = bridge.request_pair({"client_id": client_id, "public_key": client_public})
    assert request["code"] in [item["code"] for item in bridge.pending_pairs()]
    assert bridge.approve_pair(store, request["request_id"], "work", ["work"])["approved"]
    _, status = bridge.pair_status(request["request_id"])
    token = client.decrypt(base64.b64decode(status["encrypted_token"]), padding.OAEP(
        mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None
    )).decode()
    new_token = _jwt(int(time.time()) + 7 * 86400)
    payload = {"client_id": client_id, "token": token, "access_token": new_token,
               "email": "person@example.com", "cookies": [{"name": "session", "value": "new-secret"}]}
    sealed = _seal(server["public_key"], payload)
    result = bridge.sync_session(accounts_dir, store, sealed)
    assert result["ok"]
    saved = CapturedRequest.from_file(capture_path)
    assert saved.headers["authorization"] == f"Bearer {new_token}"
    assert saved.cookies == {"session": "new-secret"}
    assert saved.request_json == {"action": "next", "model": "auto"}
    assert new_token not in capture_path.read_text(encoding="utf-8")
    assert bridge.client_health(accounts_dir, store, _seal(server["public_key"], {
        "client_id": client_id, "token": token
    }))["state"] == "ok"

    wrong = {**payload, "access_token": _jwt(int(time.time()) + 86400, "other@example.com")}
    with pytest.raises(ValueError, match="email"):
        bridge.sync_session(accounts_dir, store, _seal(server["public_key"], wrong))
    assert CapturedRequest.from_file(capture_path).headers["authorization"] == f"Bearer {new_token}"
    assert bridge.revoke_client(store, client_id)
    with pytest.raises(PermissionError):
        bridge.client_health(accounts_dir, store, _seal(server["public_key"], {
            "client_id": client_id, "token": token
        }))


def test_pair_rejects_unknown_account_and_bad_envelope(tmp_path):
    accounts_dir = tmp_path / "accounts"
    store = BridgeAdminStore(tmp_path / "admin.sqlite")
    server = bridge.discovery(accounts_dir)
    assert len(server["fingerprint"]) == 64
    with pytest.raises(ValueError, match="existing account"):
        bridge.approve_pair(store, "missing", "other", ["work"])
    with pytest.raises(ValueError, match="invalid nonce"):
        bridge.client_health(accounts_dir, store, {"key": "AA==", "nonce": "AA==", "ciphertext": "AA=="})
