import http.client
import asyncio
import json
import threading
from http.server import ThreadingHTTPServer

import pytest

from chatgpt_api.api import openai_compat as compat
from chatgpt_api.api.config import OpenAICompatConfig
from chatgpt_api.api.conversation_stream import ConversationChannel, ConversationStreams
from chatgpt_api.providers.chatgpt import voice
from chatgpt_api.providers.chatgpt.auth import ChatGPTAuthConfig
from chatgpt_api.providers.chatgpt.voice_events import VoiceMessageStream, voice_event
from chatgpt_api.providers.chatgpt import voice_events


CONVERSATION_ID = "b7f60d76-41d0-4de3-b661-f6f4f8798224"
PARENT_ID = "97d96f51-e9c2-4fe8-8448-f5d7350e08ac"


def _event(delta):
    return {"type": "chat_message_delta", "payload": {"delta": delta}}


def _root(counter=0, role="user", text="Hola", message_id="u1"):
    return _event({"c": counter, "p": "", "o": "add", "v": {"message": {
        "id": message_id, "author": {"role": role}, "recipient": "all",
        "create_time": 100 + counter, "status": "in_progress",
        "content": {"content_type": "multimodal_text", "parts": [
            {"content_type": "audio_transcription", "text": text, "direction": "in" if role == "user" else "out"},
        ]},
    }}})


def _startup():
    return {"type": "startup_telemetry", "payload": {"conversation_id": CONVERSATION_ID}}


def _session(monkeypatch):
    monkeypatch.setattr(voice, "_post_offer", lambda *_args, **_kwargs: "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n")
    return voice.negotiate_voice(
        offer_sdp="v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n", voice="fathom",
        account_order=("primary",),
        load_auth=lambda _account: (ChatGPTAuthConfig(access_token="test-token"), "chrome"),
    ).bridge_session_id


def test_voice_decoder_reconstructs_observed_compressed_patches_and_both_roles():
    decoder = VoiceMessageStream()
    first = decoder.apply(_root(text=" Hola"))
    assert first["text"] == "Hola"
    second = decoder.apply(_event({"o": "patch", "v": [
        {"p": "/message/content/parts/0/text", "o": "append", "v": " mundo"},
        {"p": "/message/metadata/missing", "o": "replace", "v": 1},
    ]}))
    assert second["text"] == "Hola mundo"
    third = decoder.apply(_event({"v": [
        {"p": "/message/content/parts/0/text", "o": "append", "v": "."},
    ]}))
    assert third["text"] == "Hola mundo."
    finished = decoder.apply(_event({"p": "/message/status", "o": "replace", "v": "finished_successfully"}))
    assert finished["status"] == "finished_successfully"
    assert first["text"] == "Hola"  # Already published records must remain immutable.
    assistant = decoder.apply(_root(counter=1, role="assistant", text="Recibido", message_id="a1"))
    assert assistant["role"] == "assistant"
    assert assistant["created_at"] == "1970-01-01T00:01:41Z"
    # A reconnect starts another counter sequence on the same bridge binding.
    assert decoder.apply(_root(counter=0, text="Otro turno", message_id="u2"))["id"] == "u2"


def test_missing_roots_hidden_messages_and_telemetry_are_not_presented_as_text():
    decoder = VoiceMessageStream()
    assert decoder.apply(_event({"p": "/message/content/parts/0/text", "o": "append", "v": "suffix"})) is None
    assert decoder.apply(_root(role="tool")) is None
    hidden = _root()
    hidden["payload"]["delta"]["v"]["message"]["metadata"] = {"is_visually_hidden_from_conversation": True}
    assert decoder.apply(hidden) is None
    assert voice_event('{"type":"data_message","data":"not-json"}') is None
    assert voice_event({"type": "session_bootstrap", "payload": {}}) is None
    assert voice_event({"type": []}) is None
    assert voice_event(json.dumps({"type": "data_message", "data": json.dumps(_root())}))["type"] == "chat_message_delta"


def test_channel_snapshot_preserves_live_text_when_history_lags_and_recovers_replay_gap():
    channel = ConversationChannel("primary")
    message = {"id": "u1", "role": "user", "text": "Texto completo", "created_at": "2026-01-01", "status": "in_progress"}
    channel.publish(message)
    channel.seed({"messages": [{**message, "text": "Texto"}]})
    assert channel.snapshot(CONVERSATION_ID)["messages"][0]["text"] == "Texto completo"
    channel.publish(message)
    assert channel.revision == 1
    for index in range(260):
        channel.publish({**message, "text": str(index)})
    assert len(channel.events) == 256
    assert channel.wait(0, timeout=0) is None
    assert channel.wait(channel.revision, timeout=0) == []


def test_explicit_reload_updates_history_without_rolling_back_a_live_turn():
    channel = ConversationChannel("primary")
    finished = {"id": "a1", "role": "assistant", "text": "Anterior", "created_at": "2026-01-01", "status": "finished_successfully"}
    live = {**finished, "id": "u2", "text": "En directo", "created_at": "2026-01-02", "status": "in_progress"}
    channel.publish(finished)
    channel.publish(live)
    channel.seed({"messages": [{**finished, "text": "Corregido"}, {**live, "text": "En"}]}, refresh=True, conversation_id=CONVERSATION_ID)
    assert [message["text"] for message in channel.snapshot(CONVERSATION_ID)["messages"]] == ["Corregido", "En directo"]
    assert channel.wait(2, timeout=0)[0]["type"] == "snapshot"
    assert channel.wait(2, timeout=0)[0]["conversation_id"] == CONVERSATION_ID


def test_cache_capacity_does_not_orphan_active_subscribers():
    streams = ConversationStreams()
    for index in range(128):
        streams.channel(str(index), "primary").subscribers = 1
    first = streams.known("0")
    assert streams.channel("0", "primary") is first
    with pytest.raises(ValueError, match="capacity"):
        streams.channel("new", "primary")
    first.subscribers = 0
    streams.channel("new", "primary")
    assert streams.known("0") is None
    assert len(streams.channels) == 128


def test_sip_event_relay_retries_same_sequence_and_flushes_before_close(monkeypatch):
    calls = []
    received = []

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def post(self, url, **kwargs):
            calls.append((url, kwargs["json"]))
            if len(calls) == 1:
                raise voice_events.httpx.ReadError("response lost after upload")
            class Response:
                status_code = 200
                def raise_for_status(self):
                    pass
                def json(self):
                    return {"conversation_id": CONVERSATION_ID}
            return Response()

    async def no_delay(_seconds):
        pass

    monkeypatch.setattr(voice_events.httpx, "AsyncClient", Client)
    monkeypatch.setattr(voice_events.asyncio, "sleep", no_delay)

    async def run():
        relay = voice_events.VoiceEventRelay("http://bridge", "key", "vs_" + "a" * 32, received.append)
        relay.enqueue(json.dumps(_startup()))
        relay.enqueue(json.dumps(_root()))
        await relay.close()
        assert relay.sequence == 1

    asyncio.run(run())
    assert len(calls) == 2
    assert calls[0][1] == calls[1][1]
    assert len(calls[0][1]["events"]) == 2
    assert received == [CONVERSATION_ID]


def test_ingestion_binds_new_uuid_and_parent_without_lookup_and_is_idempotent(monkeypatch, tmp_path):
    session_id = _session(monkeypatch)
    config = OpenAICompatConfig(account="primary", admin_db_path=tmp_path / "admin.sqlite")
    streams = ConversationStreams()
    try:
        data = compat._ingest_voice_events(config, streams, session_id, {"sequence": 0, "events": [_startup(), _root()]})
        assert data["conversation_id"] == CONVERSATION_ID
        patch = {"sequence": 1, "events": [_event({"p": "/message/content/parts/0/text", "o": "append", "v": " mundo"})]}
        compat._ingest_voice_events(config, streams, session_id, patch)
        assert compat._ingest_voice_events(config, streams, session_id, patch)["duplicate"]
        channel = streams.known(CONVERSATION_ID)
        assert channel.snapshot(CONVERSATION_ID)["messages"][0]["text"] == "Hola mundo"
        compat._ingest_voice_events(config, streams, session_id, {"sequence": 2, "events": [
            {"type": "conversation_update", "payload": {"conversation_id": CONVERSATION_ID, "parent_message_id": PARENT_ID}},
        ]})
        assert voice.bound_session(session_id).conversation_id == CONVERSATION_ID
        assert voice.bound_session(session_id).parent_message_id == PARENT_ID
        assert compat._admin_store(config).get_conversation_session(CONVERSATION_ID)["account"] == "primary"
        # Reject a changed UUID atomically: preceding appends must not be applied.
        with pytest.raises(ValueError, match="cannot change"):
            compat._ingest_voice_events(config, streams, session_id, {"sequence": 3, "events": [
                _event({"v": " duplicate"}),
                {"type": "conversation_update", "payload": {"conversation_id": PARENT_ID}},
            ]})
        assert channel.snapshot(CONVERSATION_ID)["messages"][0]["text"] == "Hola mundo"
    finally:
        voice.release_session(session_id)


def test_stream_http_auth_snapshot_updates_and_reconnect_without_upstream_polling(monkeypatch, tmp_path):
    config = OpenAICompatConfig(account="primary", api_key="test-key", admin_db_path=tmp_path / "admin.sqlite")
    reads = []
    monkeypatch.setattr(compat, "_conversation_messages_response", lambda *_args: reads.append(1) or (200, {
        "account": "primary", "messages": [], "untranscribed_audio_messages": 0,
    }))
    original_wait = ConversationChannel.wait
    monkeypatch.setattr(ConversationChannel, "wait", lambda self, after, timeout=15: original_wait(self, after, timeout=0.03))
    server = ThreadingHTTPServer(("127.0.0.1", 0), compat._handler_class(config))
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    session_id = _session(monkeypatch)
    headers = {"Authorization": "Bearer test-key"}
    path = f"/v1/chatgpt/conversations/{CONVERSATION_ID}/events"

    def read_frame(response):
        frame = []
        while True:
            line = response.fp.readline().decode()
            if line == "\n":
                return "".join(frame)
            frame.append(line)

    stream = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
    post = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
    try:
        stream.request("GET", path)
        denied = stream.getresponse()
        assert denied.status == 401
        denied.read()
        assert reads == []
        stream.request("GET", path, headers=headers)
        response = stream.getresponse()
        assert response.status == 200
        assert "event: snapshot" in read_frame(response)
        for _ in range(4):
            assert ": keepalive" in read_frame(response)
        assert reads == [1]
        body = json.dumps({"sequence": 0, "events": [_startup(), _root()]})
        event_path = f"/v1/chatgpt/voice/sessions/{session_id}/events"
        post.request("POST", event_path, body=body, headers={**headers, "Content-Type": "application/json"})
        assert json.loads(post.getresponse().read())["accepted"] == 2
        frame = read_frame(response)
        assert "event: message" in frame
        assert '"text":"Hola"' in frame
        response.close()
        stream.close()
        stream = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        stream.request("GET", path, headers=headers)
        resumed = stream.getresponse()
        assert '"text":"Hola"' in read_frame(resumed)
        assert reads == [1]
        resumed.close()
    finally:
        stream.close()
        post.close()
        voice.release_session(session_id)
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
