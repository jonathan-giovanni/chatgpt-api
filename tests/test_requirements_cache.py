import asyncio
import json
import threading

import pytest
from curl_cffi import requests

from chatgpt_api.core.errors import ProviderError
from chatgpt_api.core.request_metrics import request_metrics
from chatgpt_api.core.types import ChatRequest, ContentPart, Message
from chatgpt_api.providers.chatgpt import requirements_cache as cache
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
def setup(monkeypatch):
    monkeypatch.setenv("CHATGPT_REQUIREMENTS_CACHE_TTL_SECONDS", "900")
    monkeypatch.setattr(cache, "CACHE", cache.RequirementsCache())
    # Transport imports the shared object directly.
    monkeypatch.setattr("chatgpt_api.providers.chatgpt.transport.CACHE", cache.CACHE)
    return ChatGPTWebTransport(ChatGPTAuthConfig(access_token="secret"))


def test_absolute_expiry_capacity_and_identity_isolation(monkeypatch):
    now = [10.0]
    monkeypatch.setattr(cache.time, "monotonic", lambda: now[0])
    entries = cache.RequirementsCache(capacity=2)
    key = ("identity", "thread")
    entry = cache.Entry(10, {"token": "secret"})
    entries.put(key, entry)
    now[0] = 909
    assert entries.get(key, 900) is entry
    now[0] = 910
    assert entries.get(key, 900) is None  # Hits do not extend the TTL.
    for i in range(3):
        entries.put(("identity", str(i)), entry)
    assert entries.get(("identity", "0"), 9999) is None
    scope = cache.cache_scope({"authorization": "one"}, {"model": "a"}, "url", "browser")
    assert scope != cache.cache_scope({"authorization": "two"}, {"model": "a"}, "url", "browser")
    assert scope != cache.cache_scope({"authorization": "one"}, {"model": "b"}, "url", "browser")
    assert scope != cache.cache_scope({"authorization": "one"}, {"model": "a", "conversation_mode": {"kind": "project"}}, "url", "browser")
    assert "secret" not in repr(entry)


def test_reuses_only_requirements_after_successful_uuid_binding(setup, monkeypatch):
    calls = []

    def post(url, **kwargs):
        calls.append(url)
        return Response({"conduit_token": "new-conduit"} if url.endswith("prepare") else {"token": "requirements"})

    monkeypatch.setattr(requests, "post", post)
    payload = {"model": "test", "conversation_mode": {"kind": "primary_assistant"}}
    headers = {"authorization": "Bearer secret"}
    with cache.chat_exchange() as exchange:
        setup._refresh_web_tokens(headers, payload)
        exchange.bind("thread")
    with cache.chat_exchange() as exchange:
        result = setup._refresh_web_tokens(headers, {**payload, "conversation_id": "thread"})
        assert exchange.hit
        assert result["x-conduit-token"] == "new-conduit"
        assert result["openai-sentinel-chat-requirements-token"] == "requirements"
    assert len(calls) == 3  # Prepare still runs on both turns.
    with cache.chat_policy({"chatgpt_reuse_requirements": False}), cache.chat_exchange() as exchange:
        setup._refresh_web_tokens(headers, {**payload, "conversation_id": "thread"})
        assert not exchange.hit
    assert len(calls) == 5


@pytest.mark.parametrize("challenge", ["captcha", "arkose", "turnstile"])
def test_interactive_challenge_is_not_cached(challenge):
    assert not cache.cacheable_requirements({"token": "secret", challenge: {"required": True}})


def test_done_stops_reading_but_message_finished_does_not(setup, monkeypatch):
    def lines():
        yield b'data: {"p":"/message/status","v":"finished_successfully"}'
        yield b'data: {"v":"more text"}'
        yield b'data: [DONE]'
        raise AssertionError("Must not wait for HTTP EOF after terminal DONE")

    response = Response(lines=lines())
    monkeypatch.setattr(requests, "post", lambda *a, **kw: response)
    with cache.chat_exchange(), request_metrics(True) as trace:
        events = list(setup._iter_post_conversation("url", {}, {}))
    assert len(events) == 2 and response.closed
    assert "upstream_message_finished" in trace.snapshot(200)["marks_ms"]
    assert "upstream_eof" not in trace.snapshot(200)["marks_ms"]


def test_eof_control_and_error_close_response(setup, monkeypatch):
    response = Response(lines=[b'data: [DONE]'])
    monkeypatch.setattr(requests, "post", lambda *a, **kw: response)
    with cache.chat_policy({"chatgpt_stream_close_on_done": False}), cache.chat_exchange(), request_metrics(True) as trace:
        assert list(setup._iter_post_conversation("url", {}, {})) == []
    assert response.closed and "upstream_eof" in trace.snapshot(200)["marks_ms"]
    error = Response(status=403, data={"error": {"code": "challenge_required"}})
    monkeypatch.setattr(requests, "post", lambda *a, **kw: error)
    with pytest.raises(ProviderError):
        list(setup._iter_post_conversation("url", {}, {}))
    assert error.closed


@pytest.mark.parametrize("known", [False, True])
def test_cached_token_rejection_only_retries_explicit_preaccept_error(setup, monkeypatch, known):
    calls = []
    response = Response(lines=[b'data: {"v":"ok","conversation_id":"thread"}', b'data: [DONE]'])

    def post(url, **kwargs):
        calls.append(url)
        if url.endswith("prepare"):
            return Response({"conduit_token": "conduit"})
        if url.endswith("chat-requirements"):
            return Response({"token": "fresh"})
        if sum(x.endswith("conversation") for x in calls) == 1:
            return Response(status=403, data={"error": {"code": "invalid_chat_requirements_token" if known else "challenge_required"}})
        return response

    monkeypatch.setattr(requests, "post", post)
    request = ChatRequest(messages=[Message(role="user", content=[ContentPart(kind="text", text="hello")])], model="test", conversation_id="thread")
    payload = setup.build_chat_payload(request)
    scope = cache.cache_scope(setup.auth.request_headers(), payload, setup.endpoints.requirements_url, setup.impersonate)
    cache.CACHE.put((scope, "thread"), cache.Entry(cache.time.monotonic(), {cache.TOKEN_HEADERS[0]: "old"}))

    async def collect():
        return [delta async for delta in setup.stream_chat(request)]

    if known:
        assert any(delta.text == "ok" for delta in asyncio.run(collect()))
        assert sum(x.endswith("conversation") for x in calls) == 2
    else:
        with pytest.raises(ProviderError):
            asyncio.run(collect())
        assert sum(x.endswith("conversation") for x in calls) == 1
        assert cache.CACHE.get((scope, "thread"), 900) is None


def test_policy_rejects_non_boolean_and_resets():
    with pytest.raises(ValueError):
        with cache.chat_policy({"chatgpt_reuse_requirements": "false"}):
            pass
    assert cache.current_exchange() is None


def test_parallel_preparation_overlaps_fresh_calls_and_preserves_metrics(setup, monkeypatch):
    arrived = threading.Barrier(2)
    calls = []

    def post(url, **kwargs):
        calls.append(url)
        arrived.wait(timeout=3)  # Would time out if preparation were sequential.
        return Response({"conduit_token": "new"} if url.endswith("prepare") else {"token": "fresh"})

    monkeypatch.setattr(requests, "post", post)
    with cache.chat_policy({"chatgpt_parallel_preparation": True}), cache.chat_exchange(), request_metrics(True) as trace:
        headers = setup._refresh_web_tokens({"authorization": "secret"}, {"model": "test"})
    assert len(calls) == 2
    assert headers["x-conduit-token"] == "new"
    assert headers[cache.TOKEN_HEADERS[0]] == "fresh"
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
        with cache.chat_exchange():
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
