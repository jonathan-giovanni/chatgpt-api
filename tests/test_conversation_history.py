import http.client
import json
import threading
from http.server import HTTPServer
from types import SimpleNamespace

import pytest

from chatgpt_api.api import openai_compat as compat
from chatgpt_api.api.config import OpenAICompatConfig
from chatgpt_api.core.errors import ProviderError
from chatgpt_api.providers.chatgpt.auth import ChatGPTAuthConfig
from chatgpt_api.providers.chatgpt.conversation_history import conversation_timeline
from chatgpt_api.providers.chatgpt.transport import ChatGPTWebTransport


CONVERSATION_ID = "b7f60d76-41d0-4de3-b661-f6f4f8798224"


def _node(node_id, parent, role, content, created_at, **metadata):
    return {
        "id": node_id,
        "parent": parent,
        "message": {
            "id": node_id,
            "author": {"role": role},
            "content": content,
            "create_time": created_at,
            "metadata": metadata,
            "status": "finished_successfully",
        },
    }


def test_timeline_uses_active_branch_and_marks_audio_without_text():
    snapshot = {
        "current_node": "a2",
        "mapping": {
            "root": {"id": "root", "parent": None, "message": None},
            "u1": _node("u1", "root", "user", {"content_type": "text", "parts": ["Hola"]}, 100),
            "a1": _node("a1", "u1", "assistant", {"content_type": "text", "parts": ["Buenos días"]}, 101),
            "u2": _node("u2", "a1", "user", {"content_type": "audio", "parts": []}, 102),
            "a2": _node("a2", "u2", "assistant", {"content_type": "text_audio", "text": "Te escucho"}, 103),
            "alternate": _node("alternate", "u1", "assistant", {"content_type": "text", "parts": ["Otro hilo"]}, 104),
        },
    }
    result = conversation_timeline(snapshot)
    assert [(item["role"], item["text"]) for item in result["messages"]] == [
        ("user", "Hola"), ("assistant", "Buenos días"), ("assistant", "Te escucho"),
    ]
    assert result["messages"][0]["created_at"] == "1970-01-01T00:01:40Z"
    assert result["untranscribed_audio_messages"] == 1


def test_timeline_reads_transcribed_voice_and_ignores_hidden_nodes():
    snapshot = {
        "current_node": "hidden",
        "mapping": {
            "voice": _node("voice", None, "user", {"content_type": "audio_transcription", "text": "Necesito ayuda"}, 100),
            "hidden": _node("hidden", "voice", "assistant", {"content_type": "text", "parts": ["interno"]}, 101,
                            is_visually_hidden_from_conversation=True),
        },
    }
    assert conversation_timeline(snapshot) == {
        "messages": [{"id": "voice", "role": "user", "text": "Necesito ayuda",
                      "created_at": "1970-01-01T00:01:40Z", "status": "finished_successfully"}],
        "untranscribed_audio_messages": 0,
    }


def test_transport_reads_conversation_without_posting(monkeypatch):
    from curl_cffi import requests

    calls = []

    class Response:
        status_code = 200

        def json(self):
            return {"current_node": "a1", "mapping": {}}

    monkeypatch.setattr(requests, "get", lambda url, **kwargs: calls.append((url, kwargs)) or Response())
    transport = ChatGPTWebTransport(ChatGPTAuthConfig(access_token="test-token"))
    assert transport.conversation_snapshot(CONVERSATION_ID)["current_node"] == "a1"
    assert transport.conversation_parent_message_id(CONVERSATION_ID) == "a1"
    assert len(calls) == 2
    assert calls[0][0].endswith(f"/backend-api/conversation/{CONVERSATION_ID}")
    assert calls[0][1]["headers"]["authorization"] == "Bearer test-token"
    assert calls[0][1]["headers"]["accept"] == "application/json"


def test_transport_distinguishes_missing_conversation_from_upstream_error(monkeypatch):
    from curl_cffi import requests

    transport = ChatGPTWebTransport(ChatGPTAuthConfig(access_token="test-token"))
    monkeypatch.setattr(requests, "get", lambda *_args, **_kwargs: SimpleNamespace(status_code=404))
    assert transport.conversation_snapshot(CONVERSATION_ID) is None
    monkeypatch.setattr(requests, "get", lambda *_args, **_kwargs: SimpleNamespace(status_code=503, text="down"))
    with pytest.raises(ProviderError, match="503"):
        transport.conversation_snapshot(CONVERSATION_ID)


def test_conversation_route_resolves_account_and_saves_uuid(monkeypatch, tmp_path):
    config = OpenAICompatConfig(account="primary", accounts=("primary", "backup"), admin_db_path=tmp_path / "admin.sqlite")
    router = compat.AccountRouter(("primary", "backup"), "failover")
    seen = []
    snapshot = {"current_node": "a1", "mapping": {
        "a1": _node("a1", None, "assistant", {"content_type": "text", "parts": ["Listo"]}, 100)
    }}

    def provider(_config, account):
        def lookup(_conversation_id):
            seen.append(account)
            return snapshot if account == "backup" else None
        return SimpleNamespace(transport=SimpleNamespace(conversation_snapshot=lookup))

    monkeypatch.setattr(compat, "_provider_for_account", provider)
    status, data = compat._conversation_messages_response(config, router, CONVERSATION_ID)
    assert status == 200
    assert data["account"] == "backup"
    assert data["messages"][0]["text"] == "Listo"
    assert seen == ["primary", "backup"]
    assert compat._admin_store(config).get_conversation_session(CONVERSATION_ID)["account"] == "backup"
    seen.clear()
    assert compat._conversation_messages_response(config, router, CONVERSATION_ID)[0] == 200
    assert seen == ["backup"]
    assert compat._conversation_messages_response(config, router, CONVERSATION_ID, "primary")[0] == 400


def test_conversation_messages_http_route_requires_key(monkeypatch, tmp_path):
    monkeypatch.setattr(compat, "_conversation_messages_response", lambda *_args: (200, {
        "conversation_id": CONVERSATION_ID, "account": "primary", "messages": [],
        "untranscribed_audio_messages": 0,
    }))
    server = HTTPServer(("127.0.0.1", 0), compat._handler_class(
        OpenAICompatConfig(account="primary", api_key="test-key", admin_db_path=tmp_path / "admin.sqlite")))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        path = f"/v1/chatgpt/conversations/{CONVERSATION_ID}/messages"
        conn.request("GET", path)
        unauthorized = conn.getresponse()
        unauthorized.read()
        assert unauthorized.status == 401
        conn.request("GET", path, headers={"Authorization": "Bearer test-key"})
        response = conn.getresponse()
        assert response.status == 200
        assert json.loads(response.read())["conversation_id"] == CONVERSATION_ID
    finally:
        conn.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
