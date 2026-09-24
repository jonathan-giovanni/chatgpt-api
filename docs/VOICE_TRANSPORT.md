# Experimental ChatGPT Web Voice transport

The local page at `http://127.0.0.1:8000/voice` can feed a local audio file or
microphone into a two-way ChatGPT Web Voice call. The browser owns the WebRTC
peer connection; the bridge exchanges its SDP offer for an answer. This is an
experimental adapter to a private ChatGPT Web protocol, not the public OpenAI
Realtime API and not an OpenAI-compatible STT/TTS endpoint.

## Run locally

1. Start the existing bridge with `docker compose up -d --build chatgpt-api`.
2. Open `/voice` on the local bridge. Enter the bridge's `CHATGPT_API_KEY`.
3. Choose **Archivo local**, select an audio file supported by your browser,
   and click **Conectar**. The file is decoded in the browser and played into
   the outgoing WebRTC track. It is not uploaded to the bridge as a file.
4. The remote audio player receives ChatGPT's speech. Click **Finalizar** to
   close the peer connection and release the bridge's account binding.

Microphone capture is opt-in through the source selector and browser permission
prompt. A file is capped at 20 MiB and five minutes in this test page. The
backend `/v1/chatgpt/voice/sessions` endpoint accepts a WebRTC audio SDP offer
and an optional `voice` and `bridge_session_id`:

```json
{"offer_sdp":"v=0\r\n...","voice":"cove"}
```

It returns `answer_sdp`, an opaque `bridge_session_id`, and the selected local
account alias. The same ID may be sent on a later offer to prefer the same
account. This does **not** guarantee recovery of the same upstream call or
conversation. `POST /v1/chatgpt/voice/sessions/release` with
`{"bridge_session_id":"vs_..."}` forgets the binding. Both POST routes require
the bridge bearer key when configured.

## Transport and processing

1. The browser makes an `RTCPeerConnection` with an outgoing audio track and
   an `oai-events` DataChannel, then creates an SDP offer and gathers ICE
   candidates.
2. The bridge uses its existing ChatGPT account capture to authenticate a
   multipart `POST https://chatgpt.com/realtime/wm?dcid=0` containing the offer
   and a small voice session configuration. The ChatGPT credential remains
   server-side. The bridge never relays or stores call audio.
3. The browser applies the SDP answer. ICE, DTLS, SRTP and codec negotiation
   are handled by WebRTC. Incoming audio plays through the remote media track.
   This is continuous, bidirectional media rather than upload/transcribe/TTS
   polling.
4. When available, the DataChannel carries conversation updates, captions,
   and in-call text events. Some older embedded Chromium builds cannot parse
   the optional `a=sctp-init` SDP attribute. The page removes that attribute
   to let audio connect, then reports if the DataChannel remains unavailable.

The server retries only across configured accounts after a definite HTTP 401.
It does not replay an ambiguous signalling timeout, which could create a second
call. The page waits through short WebRTC disconnections and makes at most two
new offers after a sustained failure. A new offer can start a new upstream
call; the user should assume that conversational context may be lost. There
is no promise of seamless recovery across a model or session limit.

This private endpoint and its event schema can change without notice. The
publicly documented [Realtime WebRTC API](https://developers.openai.com/api/docs/guides/voice-webrtc)
has a separate authentication, endpoint and lifecycle. OpenAI's
[WebRTC architecture overview](https://openai.com/index/delivering-low-latency-voice-ai-at-scale/)
explains the ICE/DTLS/SRTP transport layers, but does not specify ChatGPT Web's
private signalling contract. The current adapter was cross-checked against an
independent Web Voice implementation and a live local call.

## Other audio sources

The source boundary is a browser `MediaStreamTrack`. A VoIP integration should
terminate its SIP/RTP leg in a separate trusted gateway, decode/jitter-buffer
and resample there as needed, then expose a track or PCM stream to the browser
source adapter. Raw RTP packets cannot be forwarded to this WebRTC peer
connection as if they were microphone samples; WebRTC negotiates codec,
encryption and transport separately. An unattended server-to-server VoIP bridge
would need a real WebRTC peer implementation and its own lifecycle handling.
The public [OpenAI SIP guide](https://developers.openai.com/api/docs/guides/voice-sip)
describes a different, supported integration path that requires an API key.

No RTP/SIP adapter is shipped here. The first verified non-microphone source is
the local audio file. The generic `input_audio` chat attachment path remains
separate and has not produced reliable transcription in a live probe.
