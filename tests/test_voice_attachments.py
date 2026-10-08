import base64
import http.client
import json
import threading
import time
from http.server import HTTPServer

import pytest

import chatgpt_api.api.openai_compat as compat
from chatgpt_api.api.config import OpenAICompatConfig
from chatgpt_api.api.voice_attachments import load_voice_attachments, stage_voice_attachments
from chatgpt_api.providers.chatgpt.sip_gateway import Call, SipGateway, load_initial_attachments, parse_sip
from test_chatgpt_sip_gateway import _invite


def _file(name, data):
    return {"filename": name, "file_data": base64.b64encode(data).decode("ascii")}


def test_attachment_batch_round_trip_and_expiry(tmp_path, monkeypatch):
    root = tmp_path / "sip-attachments"
    staged = stage_voice_attachments(root, {"files": [
        _file("notes.txt", b"context"),
        _file("brief.pdf", b"%PDF-1.7\n"),
        _file("photo.png", b"\x89PNG\r\n\x1a\n" + b"\0" * 16),
        _file("voice.mp3", b"ID3\x04\0\0"),
    ]})
    assert staged["files"] == ["notes.txt", "brief.pdf", "photo.png", "voice.mp3"]
    assert load_voice_attachments(root, staged["attachment_batch_id"])["files"][2]["filename"] == "photo.png"
    with pytest.raises(ValueError, match="batch ID"):
        load_voice_attachments(root, "../../outside")
    now = time.time()
    monkeypatch.setattr("chatgpt_api.api.voice_attachments.time.time", lambda: now + 86401)
    with pytest.raises(ValueError, match="expired"):
        load_voice_attachments(root, staged["attachment_batch_id"])


def test_sip_voice_request_sends_attachments_only_on_initial_offer():
    files = [_file("notes.txt", b"context"), _file("brief.pdf", b"%PDF-1.7\n"),
             _file("photo.png", b"\x89PNG\r\n\x1a\n" + b"\0" * 16),
             _file("voice.mp3", b"ID3\x04\0\0")]
    gateway = SipGateway(
        bridge_url="http://127.0.0.1:8000", api_key="test", listen_ip="127.0.0.1",
        advertise_ip="127.0.0.1", sip_port=5060, rtp_port=40000,
        allowed_ips=("127.0.0.1/32",), voice="fathom", model="auto", project="INCIDENCIAS",
        conversation_id=None, text=None, files=files,
    )
    call = Call(parse_sip(_invite()), ("127.0.0.1", 5062), ("127.0.0.1", 18000))
    request = gateway._voice_request(call, "v=0\r\nm=audio")
    assert request["project"] == "INCIDENCIAS"
    assert request["files"] == files
    assert request["text"]
    call.bridge_session_id = "vs_" + "a" * 32
    assert gateway._voice_request(call, "new offer") == {
        "offer_sdp": "new offer", "voice": "fathom", "bridge_session_id": call.bridge_session_id,
    }


def test_authenticated_staging_route_supplies_sip_gateway(tmp_path):
    config = OpenAICompatConfig(account="test", api_key="test-key", image_output_dir=tmp_path / "images")
    server = HTTPServer(("127.0.0.1", 0), compat._handler_class(config))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    body = json.dumps({"files": [_file("brief.pdf", b"%PDF-1.7\n"), _file("voice.mp3", b"ID3\x04\0\0")]})
    try:
        conn.request("POST", "/v1/chatgpt/voice/attachments", body,
                     {"Content-Type": "application/json"})
        unauthorized = conn.getresponse()
        assert unauthorized.status == 401
        unauthorized.read()
        conn.request("POST", "/v1/chatgpt/voice/attachments", body,
                     {"Content-Type": "application/json", "Authorization": "Bearer test-key"})
        response = conn.getresponse()
        assert response.status == 200
        batch_id = json.loads(response.read())["attachment_batch_id"]
        files = load_initial_attachments([], batch_id, f"http://127.0.0.1:{server.server_port}", "test-key")
        assert [item["filename"] for item in files] == ["brief.pdf", "voice.mp3"]
    finally:
        conn.close()
        server.shutdown(); server.server_close(); thread.join(timeout=5)
