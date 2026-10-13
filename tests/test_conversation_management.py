import http.client
import json
import threading
from http.server import HTTPServer
from types import SimpleNamespace

import pytest

from chatgpt_api.api import openai_compat as compat
from chatgpt_api.api.config import OpenAICompatConfig
from chatgpt_api.api.conversation_stream import ConversationStreams
from chatgpt_api.core.errors import ProviderError
from chatgpt_api.providers.chatgpt.auth import ChatGPTAuthConfig
from chatgpt_api.providers.chatgpt.transport import ChatGPTWebTransport

ID = "b7f60d76-41d0-4de3-b661-f6f4f8798224"


def setup_case(monkeypatch, tmp_path, snapshot=None):
    config = OpenAICompatConfig(account="primary", api_key="test-key", admin_db_path=tmp_path / "db.sqlite")
    router = compat.AccountRouter(("primary",), "failover")
    calls = []
    transport = SimpleNamespace(
        conversation_snapshot=lambda _: snapshot if snapshot is not None else {"gizmo_id": None},
        delete_conversation=lambda value: calls.append(value),
    )
    monkeypatch.setattr(compat, "_provider_for_account", lambda *_: SimpleNamespace(transport=transport))
    return config, router, transport, calls


@pytest.mark.parametrize("snapshot", [{"gizmo_id": "g-p-example"}, {"gizmo_id": None, "gizmo_type": "snorlax"}, {}, {"gizmo_id": None, "memory_scope": "project_v2"}])
def test_guard_never_deletes_projects_or_unknown_membership(monkeypatch, tmp_path, snapshot):
    config, router, _, calls = setup_case(monkeypatch, tmp_path, snapshot)
    status, data = compat._conversation_delete_response(config, router, ConversationStreams(), ID, {"outside_projects": ["true"]})
    assert status == 409
    assert data["error"]["type"] == "project_protected"
    assert calls == []


def test_delete_removes_local_continuation_and_cached_text_only_after_success(monkeypatch, tmp_path):
    config, router, _, calls = setup_case(monkeypatch, tmp_path)
    store = compat._admin_store(config)
    store.upsert_conversation_session(conversation_id=ID, parent_message_id="parent", account="primary")
    streams = ConversationStreams()
    streams.channel(ID, "primary").publish({"id": "message", "role": "user", "text": "example", "created_at": None})
    status, data = compat._conversation_delete_response(config, router, streams, ID, {"outside_projects": ["true"]})
    assert status == 200 and data["deleted"] is True
    assert calls == [ID]
    assert store.get_conversation_session(ID) is None
    assert streams.known(ID) is None


def test_failure_keeps_local_session_and_cache(monkeypatch, tmp_path):
    config, router, transport, _ = setup_case(monkeypatch, tmp_path)
    store = compat._admin_store(config)
    store.upsert_conversation_session(conversation_id=ID, parent_message_id="parent", account="primary")
    streams = ConversationStreams()
    streams.channel(ID, "primary")
    def fail(_):
        raise ProviderError("ChatGPT conversation management failed: 503")
    transport.delete_conversation = fail
    status, _ = compat._conversation_delete_response(config, router, streams, ID, {})
    assert status >= 400
    assert store.get_conversation_session(ID) is not None
    assert streams.known(ID) is not None


def test_saved_project_membership_is_also_protected(monkeypatch, tmp_path):
    config, router, _, calls = setup_case(monkeypatch, tmp_path)
    compat._admin_store(config).upsert_conversation_session(conversation_id=ID, parent_message_id="parent", account="primary", project_id="g-p-example")
    assert compat._conversation_delete_response(config, router, ConversationStreams(), ID, {"outside_projects": ["true"]})[0] == 409
    assert calls == []


def test_delete_does_not_switch_accounts_or_accept_invalid_uuid(monkeypatch, tmp_path):
    config, _, _, calls = setup_case(monkeypatch, tmp_path)
    router = compat.AccountRouter(("primary", "backup"), "failover")
    for value, query in [("not-a-uuid", {}), (ID, {}), (ID, {"account": ["missing"]}), (ID, {"account": ["primary"], "outside_projects": ["yes"]})]:
        assert compat._conversation_delete_response(config, router, ConversationStreams(), value, query)[0] == 400
    assert calls == []


def test_filtered_list_keeps_pagination_over_project_rows(monkeypatch, tmp_path):
    config, router, transport, _ = setup_case(monkeypatch, tmp_path)
    transport.list_conversations = lambda **kw: {"items": [{"id": ID, "gizmo_id": "g-p-example"}], "total": 2}
    status, data = compat._conversation_list_response(config, router, {"outside_projects": ["true"], "limit": ["1"]})
    assert status == 200 and data["items"] == [] and data["next_offset"] == 1
    transport.list_conversations = lambda **kw: {"items": [{"id": ID, "gizmo_id": None}], "total": 2}
    assert compat._conversation_list_response(config, router, {"offset": ["1"]})[1]["next_offset"] is None


def test_transport_uses_single_patch_and_closes_success_and_failure(monkeypatch):
    from curl_cffi import requests
    calls, closed = [], []
    response = SimpleNamespace(status_code=200, json=lambda: {"success": True}, close=lambda: closed.append(True))
    monkeypatch.setattr(requests, "patch", lambda url, **kwargs: calls.append((url, kwargs)) or response)
    transport = ChatGPTWebTransport(ChatGPTAuthConfig(access_token="test-token"))
    transport.delete_conversation(ID)
    assert len(calls) == 1
    assert calls[0][1]["json"] == {"is_visible": False}
    assert calls[0][0].endswith("/backend-api/conversation/" + ID)
    response.status_code = 429
    with pytest.raises(ProviderError, match="429"):
        transport.delete_conversation(ID)
    assert closed == [True, True]
    response.status_code = 200
    response.json = lambda: {"success": False}
    with pytest.raises(ProviderError, match="did not confirm"):
        transport.delete_conversation(ID)


def test_delete_route_requires_auth_and_supports_browser_cors(monkeypatch, tmp_path):
    config, _, _, calls = setup_case(monkeypatch, tmp_path)
    server = HTTPServer(("127.0.0.1", 0), compat._handler_class(config))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    try:
        path = f"/v1/chatgpt/conversations/{ID}?outside_projects=true"
        conn.request("DELETE", path)
        response = conn.getresponse()
        assert response.status == 401
        response.read()
        assert calls == []
        conn.request("DELETE", path, headers={"Authorization": "Bearer test-key"})
        response = conn.getresponse()
        assert response.status == 200
        assert "DELETE" in response.getheader("Access-Control-Allow-Methods")
        assert json.loads(response.read())["deleted"] is True
    finally:
        conn.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
