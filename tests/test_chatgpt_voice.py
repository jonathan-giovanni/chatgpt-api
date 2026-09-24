import asyncio

import pytest

from chatgpt_api.api import openai_compat as compat
from chatgpt_api.api.config import OpenAICompatConfig
from chatgpt_api.providers.chatgpt.auth import ChatGPTAuthConfig
from chatgpt_api.providers.chatgpt import voice


OFFER = "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"
ANSWER = "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"


def test_voice_signalling_keeps_account_binding_on_reconnect(monkeypatch):
    seen = []

    def post(auth, _impersonate, offer, selected_voice, **options):
        seen.append((auth.access_token, offer, selected_voice, options))
        return ANSWER

    monkeypatch.setattr(voice, "_post_offer", post)
    load = lambda account: (ChatGPTAuthConfig(access_token=account), "chrome")
    first = voice.negotiate_voice(offer_sdp=OFFER, voice="cove", account_order=("primary", "backup"), load_auth=load)
    second = voice.negotiate_voice(
        offer_sdp=OFFER,
        voice="cove",
        account_order=("backup", "primary"),
        load_auth=load,
        bridge_session_id=first.bridge_session_id,
    )

    assert first.answer_sdp == ANSWER
    assert second.bridge_session_id == first.bridge_session_id
    assert [item[0] for item in seen] == ["primary", "primary"]
    assert seen[0][3]["upstream_session_id"] == seen[1][3]["upstream_session_id"]
    assert voice.release_session(first.bridge_session_id)


def test_voice_signalling_fails_over_only_on_expired_auth(monkeypatch):
    attempts = []

    def post(auth, _impersonate, _offer, _voice, **_options):
        attempts.append(auth.access_token)
        if auth.access_token == "expired":
            raise voice.VoiceSignallingError("expired", 401)
        return ANSWER

    monkeypatch.setattr(voice, "_post_offer", post)
    load = lambda account: (ChatGPTAuthConfig(access_token=account), "chrome")
    result = voice.negotiate_voice(offer_sdp=OFFER, voice="cove", account_order=("expired", "valid"), load_auth=load)
    assert result.account == "valid"
    assert attempts == ["expired", "valid"]
    voice.release_session(result.bridge_session_id)


def test_voice_offer_uses_multipart_without_exposing_web_token(monkeypatch):
    from curl_cffi import requests

    captured = {}

    class Response:
        status_code = 201
        text = ANSWER

    def post(url, **kwargs):
        captured.update(url=url, **kwargs)
        return Response()

    monkeypatch.setattr(requests, "post", post)
    answer = voice._post_offer(ChatGPTAuthConfig(access_token="test-token"), "chrome", OFFER, "cove")

    assert answer == ANSWER.strip()
    assert captured["url"].endswith("/realtime/wm?dcid=0")
    assert captured["headers"]["authorization"] == "Bearer test-token"
    assert captured["headers"]["content-type"].startswith("multipart/form-data; boundary=")
    assert b'name="sdp"' in captured["data"]
    assert b'name="session"' in captured["data"]
    assert b"test-token" not in captured["data"]


def test_voice_offer_preserves_thread_model_and_project(monkeypatch):
    from curl_cffi import requests

    captured = {}

    class Response:
        status_code = 201
        text = ANSWER

    def post(_url, **kwargs):
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr(requests, "post", post)
    conversation_id = "b7f60d76-41d0-4de3-b661-f6f4f8798224"
    parent_id = "97d96f51-e9c2-4fe8-8448-f5d7350e08ac"
    project_id = "g-p-1234567890123456"
    voice._post_offer(
        ChatGPTAuthConfig(access_token="token"), "chrome", OFFER, "cove",
        conversation_id=conversation_id, parent_message_id=parent_id,
        project_id=project_id, model="gpt-5", upstream_session_id="VOICE-1",
    )
    body = captured["data"].decode("utf-8")
    assert f'"conversation_id":"{conversation_id}"' in body
    assert f'"parent_message_id":"{parent_id}"' in body
    assert f'"gizmo_id":"{project_id}"' in body
    assert '"requested_default_model":""' in body
    assert '"model_slug":""' in body
    assert '"voice_session_id":"VOICE-1"' in body


def test_voice_reconnect_cannot_switch_conversation_or_account(monkeypatch):
    monkeypatch.setattr(voice, "_post_offer", lambda *_args, **_kwargs: ANSWER)
    load = lambda account: (ChatGPTAuthConfig(access_token=account), "chrome")
    first = voice.negotiate_voice(
        offer_sdp=OFFER, voice="cove", account_order=("primary",), load_auth=load,
        conversation_id="b7f60d76-41d0-4de3-b661-f6f4f8798224",
    )
    with pytest.raises(voice.VoiceSignallingError, match="conversation_id cannot change"):
        voice.negotiate_voice(
            offer_sdp=OFFER, voice="cove", account_order=("backup",), load_auth=load,
            bridge_session_id=first.bridge_session_id,
            conversation_id="97d96f51-e9c2-4fe8-8448-f5d7350e08ac",
        )
    assert voice.bound_account(first.bridge_session_id) == "primary"
    voice.release_session(first.bridge_session_id)


def test_voice_api_prepares_initial_text_and_files_in_same_project_thread(monkeypatch):
    conversation_id = "b7f60d76-41d0-4de3-b661-f6f4f8798224"
    parent_id = "97d96f51-e9c2-4fe8-8448-f5d7350e08ac"
    captured = {}

    async def completion(_config, body, _router):
        captured["chat"] = body
        return {"conversation_id": conversation_id, "chatgpt_account": "primary",
                "choices": [{"message": {"content": "Listo"}}]}

    class Store:
        def get_conversation_session(self, _conversation_id):
            return {"account": "primary", "parent_message_id": parent_id,
                    "project_id": "g-p-1234567890123456"}

    def negotiate(**kwargs):
        captured["voice"] = kwargs
        return voice.VoiceAnswer(ANSWER, "vs_" + "a" * 32, "primary", conversation_id)

    monkeypatch.setattr(compat, "_chat_completion", completion)
    monkeypatch.setattr(compat, "_admin_store", lambda _config: Store())
    monkeypatch.setattr(compat, "negotiate_voice", negotiate)
    response = asyncio.run(compat._start_voice_session(
        OpenAICompatConfig(account="primary"), compat.AccountRouter(("primary",), "failover"),
        {"offer_sdp": OFFER, "text": "Resume", "project": "Investigacion", "model": "gpt-5",
         "files": [{"filename": "notes.txt", "file_data": "aG9sYQ=="}]},
    ))
    assert captured["chat"]["chatgpt_project"] == "Investigacion"
    assert captured["chat"]["temporary_chat"] is False
    assert captured["chat"]["messages"][0]["content"][1]["file"]["filename"] == "notes.txt"
    assert captured["voice"]["conversation_id"] == conversation_id
    assert captured["voice"]["parent_message_id"] == parent_id
    assert captured["voice"]["project_id"] == "g-p-1234567890123456"
    assert captured["voice"]["account_order"] == ("primary",)
    assert response["initial_response"] == "Listo"
    assert response["voice_model"] == "auto"


@pytest.mark.parametrize("offer", ["", "v=0\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\n", "v=0" + "x" * 66_000], ids=["empty", "video", "oversize"])
def test_voice_signalling_rejects_invalid_or_non_audio_offer(offer):
    with pytest.raises(voice.VoiceSignallingError) as exc:
        voice.negotiate_voice(offer_sdp=offer, voice="cove", account_order=("primary",), load_auth=lambda _: None)
    assert exc.value.status == 400
