import asyncio
import json
import threading

import pytest
from curl_cffi import requests

from chatgpt_api.core.errors import ProviderError
from chatgpt_api.core.request_metrics import request_metrics
from chatgpt_api.core.types import ChatDelta
from chatgpt_api.providers.chatgpt import preparation
from chatgpt_api.providers.chatgpt.auth import ChatGPTAuthConfig
from chatgpt_api.providers.chatgpt.transport import ChatGPTStreamError, ChatGPTWebTransport
from chatgpt_api.api import openai_compat as compat
from chatgpt_api.api.config import OpenAICompatConfig


class Response:
    def __init__(self, data=None, lines=(), status=200):
        self.data = data or {}
        self.headers = {}
        self.status_code = status
        self.text = json.dumps(self.data)
        self.lines = lines
        self.closed = False

    def json(self):
        return self.data

    def iter_lines(self):
        yield from self.lines

    def close(self):
        self.closed = True


@pytest.fixture
def setup():
    return ChatGPTWebTransport(ChatGPTAuthConfig(access_token="secret"))


def test_each_turn_gets_fresh_requirements(setup, monkeypatch):
    calls = []

    def post(url, **kwargs):
        calls.append(url)
        return Response({"conduit_token": "new-conduit"} if url.endswith("prepare") else {"token": "fresh"})

    monkeypatch.setattr(requests, "post", post)
    for _ in range(2):
        headers = setup._refresh_web_tokens({"authorization": "secret"}, {"model": "test", "conversation_id": "same-thread"})
        assert headers["openai-sentinel-chat-requirements-token"] == "fresh"
    assert sum(url.endswith("prepare") for url in calls) == 2
    assert sum(url.endswith("chat-requirements") for url in calls) == 2


def test_done_stops_reading_but_message_finished_does_not(setup, monkeypatch):
    def lines():
        yield b'data: {"p":"/message/status","v":"finished_successfully"}'
        yield b'data: {"v":"more text"}'
        yield b'data: [DONE]'
        raise AssertionError("Must not wait for HTTP EOF after terminal DONE")

    response = Response(lines=lines())
    monkeypatch.setattr(requests, "post", lambda *a, **kw: response)
    with request_metrics(True) as trace:
        events = list(setup._iter_post_conversation("url", {}, {}))
    assert len(events) == 2 and response.closed
    assert "upstream_message_finished" in trace.snapshot(200)["marks_ms"]
    assert "upstream_eof" not in trace.snapshot(200)["marks_ms"]


def test_error_closes_response_without_replay(setup, monkeypatch):
    error = Response(status=403, data={"error": {"code": "invalid_chat_requirements_token"}})
    calls = []
    monkeypatch.setattr(requests, "post", lambda *a, **kw: calls.append(a) or error)
    with pytest.raises(ProviderError):
        list(setup._iter_post_conversation("url", {}, {}))
    assert error.closed and len(calls) == 1


def test_parallel_default_and_request_override_reset(monkeypatch):
    monkeypatch.delenv("CHATGPT_PARALLEL_PREPARATION", raising=False)
    assert preparation.parallel_preparation_enabled()
    with preparation.chat_policy({"chatgpt_parallel_preparation": False}):
        assert not preparation.parallel_preparation_enabled()
    assert preparation.parallel_preparation_enabled()
    with pytest.raises(ValueError):
        with preparation.chat_policy({"chatgpt_parallel_preparation": "false"}):
            pass
    assert preparation.parallel_preparation_enabled()


def test_parallel_preparation_overlaps_fresh_calls_and_preserves_metrics(setup, monkeypatch):
    arrived = threading.Barrier(2)
    calls = []

    def post(url, **kwargs):
        calls.append(url)
        arrived.wait(timeout=3)  # Would time out if preparation were sequential.
        return Response({"conduit_token": "new"} if url.endswith("prepare") else {"token": "fresh"})

    monkeypatch.setattr(requests, "post", post)
    with preparation.chat_policy({"chatgpt_parallel_preparation": True}), request_metrics(True) as trace:
        headers = setup._refresh_web_tokens({"authorization": "secret"}, {"model": "test"})
    assert len(calls) == 2
    assert headers["x-conduit-token"] == "new"
    assert headers["openai-sentinel-chat-requirements-token"] == "fresh"
    spans = trace.snapshot(200)["spans"]
    assert {s['name'] for s in spans} >= {"upstream.prepare", "upstream.requirements"}
    assert trace.snapshot(200)["facts"]["parallel_preparation"]


def test_request_store_reuse_keeps_reads_live_and_resets_between_requests(tmp_path):
    config = OpenAICompatConfig(account="test", admin_db_path=tmp_path / "test.sqlite")
    other = OpenAICompatConfig(account="test", admin_db_path=tmp_path / "other.sqlite")
    with compat._admin_store_scope():
        store = compat._admin_store(config)
        assert compat._admin_store(config) is store
        assert compat._admin_store(other) is not store
        store.set_setting("example", "new value")
        assert compat._admin_store(config).get_setting("example") == "new value"
    with compat._admin_store_scope():
        assert compat._admin_store(config) is not store
        assert compat._admin_store(config).get_setting("example") == "new value"


def test_done_preserves_preceding_websocket_handoff(setup, monkeypatch):
    response = Response(lines=[b'data: {"type":"stream_handoff","options":[{"type":"subscribe_ws_topic","topic_id":"topic"}]}',
                               b'data: [DONE]'])
    monkeypatch.setattr(requests, "post", lambda *a, **kw: response)

    def follow(events, headers):
        assert len(events) == 1 and events[0]["type"] == "stream_handoff"
        return [{"v": "websocket text"}]

    monkeypatch.setattr(setup, "_follow_stream_handoffs", follow)

    async def collect():
        return [event async for event in setup._stream_conversation_events("url", {}, {})]

    events = asyncio.run(collect())
    assert events[-1] == {"v": "websocket text"}
    assert response.closed


def test_http_200_usage_limit_event_is_an_explicit_error(setup, monkeypatch):
    response = Response(lines=[b'data: {"message":null,"error":"private provider detail","error_code":"usage_limit"}',
                               b'data: [DONE]'])
    monkeypatch.setattr(requests, "post", lambda *a, **kw: response)
    with request_metrics(True) as trace, pytest.raises(ChatGPTStreamError) as caught:
        list(setup._iter_post_conversation("url", {}, {}))
    assert response.closed
    assert "private provider detail" not in str(caught.value)
    assert trace.snapshot(200)["facts"]["upstream_usage_limit"]
    status, payload = compat._provider_error_status_and_payload(caught.value)
    assert status == 429 and payload["error"]["code"] == "chatgpt_rate_limited"
    assert payload["error"]["provider_status"] is None  # The upstream HTTP status was 200.


def test_model_limit_is_not_borrowed_from_a_different_model():
    assert compat._matching_model_limit({"model_limits":[{"model_slug":"other-model","resets_after":"private"}]}, "test", "test") is None


def test_one_account_does_not_read_models_just_to_sort_one_item(monkeypatch):
    monkeypatch.setattr(compat, "_account_supports_model", lambda *_a: pytest.fail("No model ranking is needed"))
    config = OpenAICompatConfig(account="test")
    assert compat._account_order_for_model(config, compat.AccountRouter(("test",)), "gpt-6-mini", None) == ("test",)


def test_limit_is_computed_once_per_request_and_refreshes_next_request(tmp_path):
    config = OpenAICompatConfig(account="test", admin_db_path=tmp_path / "admin.sqlite")
    with compat._admin_store_scope():
        store = compat._admin_store(config)
        store.set_setting(compat.BRIDGE_SETTINGS_KEY, {"concurrency": {"chat": {"accounts": {"test": 2}}}})
        assert compat._feature_account_concurrency_limit(config, "chat", "test") == 2
        store.set_setting(compat.BRIDGE_SETTINGS_KEY, {"concurrency": {"chat": {"accounts": {"test": 3}}}})
        assert compat._feature_account_concurrency_limit(config, "chat", "test") == 2
    with compat._admin_store_scope():
        assert compat._feature_account_concurrency_limit(config, "chat", "test") == 3


def test_configured_default_is_published_without_claiming_observed_support(monkeypatch):
    monkeypatch.setattr(compat, "_models_for_account", lambda *_a: [{"id": "auto", "name": "Auto"}])
    model = next(m for m in compat._models_for_config(OpenAICompatConfig(account="test")) if m["id"] == "gpt-6-mini")
    assert model["chatgpt"]["source"] == "configured"


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("explicit", [None, "auto", "gpt-5-5-thinking-standard"])
def test_default_chat_model_and_explicit_selection_are_preserved(monkeypatch, tmp_path, stream, explicit):
    requests = []

    class Provider:
        account = "test"

        async def stream_chat(self, request):
            requests.append(request)
            yield ChatDelta(text="ok")

    monkeypatch.setattr(compat, "_provider_for_account", lambda *_a, **_kw: Provider())
    config = OpenAICompatConfig(account="test", admin_db_path=tmp_path / "admin.sqlite")
    body = {"messages": [{"role": "user", "content": "hello"}]}
    if explicit:
        body["model"] = explicit
    if stream:
        class Handler:
            def send_response(self, *_a): pass
            def send_header(self, *_a): pass
            def end_headers(self): pass
            from io import BytesIO
            wfile = BytesIO()
        asyncio.run(compat._chat_completion_stream(config, body, compat.AccountRouter(("test",)), Handler()))
    else:
        asyncio.run(compat._chat_completion(config, body))
    expected = explicit or "gpt-6-mini"
    assert requests and requests[0].model == ("gpt-5-5-thinking" if explicit and explicit.endswith("standard") else expected)
    assert requests[0].thinking_effort == ("standard" if explicit and explicit.endswith("standard") else None)


def test_configured_default_model_can_be_changed(monkeypatch, tmp_path):
    seen = []

    class Provider:
        account = "test"
        async def stream_chat(self, request):
            seen.append(request.model)
            yield ChatDelta(text="ok")

    monkeypatch.setattr(compat, "_provider_for_account", lambda *_a, **_kw: Provider())
    config = OpenAICompatConfig(account="test", default_model="auto", admin_db_path=tmp_path / "admin.sqlite")
    asyncio.run(compat._chat_completion(config, {"messages": [{"role": "user", "content": "hello"}]}))
    assert seen == ["auto"]


def test_every_turn_uses_fresh_preparation_and_preserves_conversation_stream(setup, monkeypatch):
    responses = []
    calls = []

    def post(url, **kwargs):
        calls.append(url)
        assert "content_callback" not in kwargs
        if url == setup.endpoints.conversation_url:
            assert kwargs["stream"] is True
            response = Response(lines=[b'data: {"v":"reply"}', b'data: [DONE]'])
        else:
            assert not kwargs.get("stream")
            response = Response({"conduit_token": "fresh"} if url.endswith("prepare") else {"token": "fresh"})
        responses.append(response)
        return response

    monkeypatch.setattr(requests, "post", post)

    async def collect(headers):
        return [event async for event in setup._stream_conversation_events(setup.endpoints.conversation_url, headers, {})]

    with preparation.chat_policy({"chatgpt_parallel_preparation": True}):
        for _ in range(2):
            headers = setup._refresh_web_tokens({"authorization": "example"}, {"model": "test", "conversation_id": "same-thread"})
            assert asyncio.run(collect(headers)) == [{"v": "reply"}]
    assert calls.count(setup.endpoints.prepare_url) == 2
    assert calls.count(setup.endpoints.requirements_url) == 2
    assert calls.count(setup.endpoints.conversation_url) == 2
    assert len(responses) == 6 and all(response.closed for response in responses)
