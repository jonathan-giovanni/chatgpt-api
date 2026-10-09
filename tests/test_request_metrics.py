import asyncio
import http.client
import json
import threading
from http.server import HTTPServer

import pytest

from chatgpt_api.api import openai_compat as compat
from chatgpt_api.api.config import OpenAICompatConfig
from chatgpt_api.core.request_metrics import current_metrics, metric_mark, metric_span, request_metrics
from chatgpt_api.providers.chatgpt.auth import ChatGPTAuthConfig
from chatgpt_api.providers.chatgpt.transport import ChatGPTWebTransport


@pytest.mark.parametrize("enabled", [False, True])
def test_http_metrics_are_opt_in_and_exclude_request_and_response_content(monkeypatch, capsys, enabled):
    monkeypatch.setenv("CHATGPT_REQUEST_METRICS", str(enabled))

    async def fake_completion(config, body, router):
        return {"choices": [{"message": {"content": "private response"}}]}

    monkeypatch.setattr(compat, "_chat_completion", fake_completion)
    server = HTTPServer(("127.0.0.1", 0), compat._handler_class(OpenAICompatConfig(account="test", api_key="secret-key")))
    thread = threading.Thread(target=server.handle_request)
    thread.start()
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    try:
        connection.request("POST", "/v1/chat/completions", json.dumps({"messages": [{"content": "private prompt"}]}),
                           {"Authorization": "Bearer secret-key", "Content-Type": "application/json"})
        response = connection.getresponse()
        assert response.status == 200
        assert "private response" in response.read().decode()
        request_id = response.getheader("X-Request-Id")
    finally:
        connection.close()
        thread.join(5)
        server.server_close()
    log = capsys.readouterr().err
    assert "secret-key" not in log and "private prompt" not in log and "private response" not in log
    if enabled:
        trace = json.loads(log)
        assert trace["request_id"] == request_id
        assert trace["status"] == 200
        assert trace["marks_ms"]["client_complete"] <= trace["total_ms"]
    else:
        assert not log and request_id is None


def test_trace_crosses_transport_worker_and_is_reset_after_request(monkeypatch):
    transport = ChatGPTWebTransport(ChatGPTAuthConfig(access_token="private-token"))

    def fake_events(*args):
        assert current_metrics() is not None
        with metric_span("upstream.test"):
            metric_mark("upstream_first_event")
            yield {"private_content": "not logged"}

    monkeypatch.setattr(transport, "_iter_post_conversation", fake_events)
    monkeypatch.setattr(transport, "_follow_stream_handoffs", lambda *args: [])

    async def collect():
        return [event async for event in transport._stream_conversation_events("private-url", {}, {})]

    with request_metrics(True) as trace:
        assert asyncio.run(collect()) == [{"private_content": "not logged"}]
        snapshot = trace.snapshot(200)
    assert current_metrics() is None
    assert "upstream_first_event" in snapshot["marks_ms"]
    assert snapshot["spans"][0]["name"] == "upstream.test"
    assert "private" not in json.dumps(snapshot)


def test_error_span_records_type_without_exception_message():
    with request_metrics(True) as trace:
        with pytest.raises(ValueError):
            with metric_span("local.test"):
                raise ValueError("private exception detail")
    assert trace.snapshot(400)["spans"][0]["error_type"] == "ValueError"
    assert "private exception detail" not in json.dumps(trace.snapshot(400))
