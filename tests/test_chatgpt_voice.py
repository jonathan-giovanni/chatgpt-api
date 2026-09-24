import pytest

from chatgpt_api.providers.chatgpt.auth import ChatGPTAuthConfig
from chatgpt_api.providers.chatgpt import voice


OFFER = "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"
ANSWER = "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"


def test_voice_signalling_keeps_account_binding_on_reconnect(monkeypatch):
    seen = []

    def post(auth, _impersonate, offer, selected_voice):
        seen.append((auth.access_token, offer, selected_voice))
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
    assert voice.release_session(first.bridge_session_id)


def test_voice_signalling_fails_over_only_on_expired_auth(monkeypatch):
    attempts = []

    def post(auth, _impersonate, _offer, _voice):
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


@pytest.mark.parametrize("offer", ["", "v=0\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\n", "v=0" + "x" * 66_000])
def test_voice_signalling_rejects_invalid_or_non_audio_offer(offer):
    with pytest.raises(voice.VoiceSignallingError) as exc:
        voice.negotiate_voice(offer_sdp=offer, voice="cove", account_order=("primary",), load_auth=lambda _: None)
    assert exc.value.status == 400
