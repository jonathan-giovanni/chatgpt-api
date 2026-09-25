# ChatGPT Web Voice: WebRTC and SIP/RTP

This is an experimental adapter to ChatGPT Web's private Voice signalling, not
the public OpenAI Realtime API. The primary UI is the WebRTC panel inside the
Bridge Console Test Lab at `http://127.0.0.1:8080/#test-lab`. It shares the chat
test's selected Project, model, conversation UUID, initial text, and text
attachments. It accepts a local audio file or microphone. The ChatGPT credential
stays on the bridge. Media flows through WebRTC directly between the peer and
ChatGPT; the bridge handles only SDP and optional chat preparation.

The wrapper watches local and returned audio without injecting health-check
messages into the chat. It closes the flow when the remote audio track ends,
when ChatGPT has no audio response for 30 seconds after a user turn, or after
30 seconds without voice activity following a response. Failed transport
reconnects are attempted twice; if they fail, the session is released.

## Browser test

1. Start both local services: `docker compose up -d --build chatgpt-api bridge-console`.
2. Open `http://127.0.0.1:8080/#test-lab` in the Bridge Console. Use **Refresh** if its status is still loading.
3. In **Single message test**, select a Project (optional), model, existing conversation UUID if continuing, initial message, and text attachments.
4. In **Voz en esta conversación** below it, choose a local audio file or microphone and start voice. The panel uses the same Project, model, UUID, message, and text files. An empty Project creates the conversation outside Projects.
5. When the UUID is known, use the voice panel's follow-up composer for text or text-file turns in the same thread. The outer chat panel also receives that UUID for later chat tests. Finalizar releases the voice binding.

The local audio file is decoded into a browser media track. It is not uploaded
as a chat attachment. The panel caps it at 20 MiB and five minutes. Microphone
capture is opt-in. The ChatGPT account determines the effective live voice
model; `model` selects the model for the optional initial text turn. Passing a
regular chat model slug to the private live voice handshake was observed to
produce a connected but silent call, so the voice model remains automatic.

## Minimal WebRTC API

Create a browser `RTCPeerConnection` with an audio track and optional negotiated
`oai-events` DataChannel (id `0`), then send its gathered SDP offer:

```http
POST /v1/chatgpt/voice/sessions
Authorization: Bearer <bridge-key>
Content-Type: application/json

{
  "offer_sdp": "v=0\r\n...",
  "voice": "cove",
  "model": "auto",
  "project": "My Project",
  "text": "Start by summarizing these notes.",
  "files": [{"filename": "notes.txt", "file_data": "<base64 UTF-8>"}]
}
```

`project`, `text`, `files`, `model`, and `conversation_id` are optional. A new
conversation is created when `conversation_id` is omitted. `files` requires
`text`; attachment limits match Chat Completions (10 files, 20 MiB each,
25 MiB total). For an existing UUID, the initial text/attachments extend that
conversation before voice starts. Project names and aliases resolve using the
bridge's local Project mappings. The response is:

```json
{
  "answer_sdp": "v=0\r\n...",
  "bridge_session_id": "vs_<opaque-id>",
  "conversation_id": "<UUID or null>",
  "voice_model": "auto",
  "account": "<local-account-alias>",
  "initial_response": "<present only when text was sent>"
}
```

Apply `answer_sdp` as the remote description. For a new call with no initial
text, ChatGPT creates the conversation when speech or an in-call message arrives;
the UUID may arrive later through a DataChannel event. The UUID can then be used
with `POST /v1/chat/completions` for subsequent text turns and attachments.

On network loss, create a new offer and call the same route with only
`offer_sdp`, `voice`, and `bridge_session_id`. The bridge reuses the same account,
upstream voice session ID, conversation UUID, Project and initial chat model. It does
not replay the initial text or attachments. Release with:

```http
POST /v1/chatgpt/voice/sessions/release
Authorization: Bearer <bridge-key>
Content-Type: application/json

{"bridge_session_id":"vs_<opaque-id>"}
```

The browser retries at most twice. Reconnection is best effort: the upstream
may still start a fresh live call or lose conversational continuity. A known
UUID remains usable through ordinary chat. The bridge retries signalling on a
second account only after a definite 401 on a new, unbound call; it never moves
an existing conversation to another account. It does not replay ambiguous
timeouts that might duplicate a call.

## Native SIP/RTP gateway

The optional SIP service is packaged with the project. From the repository
root, start it with one command; Compose passes the bridge URL and key and
publishes the standard local ports automatically:

```powershell
docker compose run --rm --build --service-ports sip-gateway
```

The Bridge Console command includes the selected project, model, UUID and text.
The gateway opens UDP 5060 for SIP and UDP 40000 for RTP, bound to loopback.
MicroSIP can connect directly with server/domain `127.0.0.1`, user `voice`,
UDP 5060, and no password. Registration is accepted locally without
authentication; disable STUN and SRTP and enable PCMU (G.711 µ-law, 8 kHz).
For Asterisk, configure a local static UDP endpoint/trunk to `127.0.0.1:5060`,
codec `ulaw`, `direct_media=no`, and route an extension through it. The gateway
does not proxy Asterisk registrations or replace a PBX.

The gateway accepts one SIP call at a time and answers PCMU/8000 RTP. It
supports `OPTIONS`, `REGISTER`, `INVITE`, `ACK`, `CANCEL`, and `BYE`; rejected
codecs receive SIP 488 and a second call receives 486. It sends silence during
RTP gaps, bounds its jitter queue, and makes at most two WebRTC reconnection
attempts using the same bridge session ID. It sends BYE and releases WebRTC if
the client stops sending RTP or voice activity remains absent for 30 seconds.

For a direct CLI run, the same context is available through `--model`,
`--project`, `--conversation-id`, `--text`, and repeatable `--attachment path`
options. The Docker service receives the first four from the wrapper command.
The bridge bearer key is never sent in SIP or RTP. The local gateway has no SIP
Digest, TLS, SRTP, NAT traversal, transcoding, or multi-call support; use a
PBX/SBC for those functions, and do not publish its ports outside a trusted
network.

### SIP/RTP smoke test (optional)

With the gateway running, the optional client sends an INVITE, a tone and BYE,
then reports returned RTP:

```powershell
uv run --extra sip python scripts/sip_rtp_smoke.py
```

To validate a spoken turn, pass a short uncompressed PCM WAV (8, 16, 24, or 32
bit) and confirm the returned PCM peak is nonzero:

```powershell
uv run --extra sip python scripts/sip_rtp_smoke.py --wav "C:\audio\prueba.wav"
```

The smoke client uses the default ports. `Ctrl+C` releases the active session.
SIP has no digest auth, TLS or SRTP here; the host ports bind to loopback and
should not be exposed to an untrusted network.

## Observed behavior and limits

The private upstream exchange is a multipart SDP+session request to
`/realtime/wm?dcid=0`. Session fields include the conversation UUID and parent
message ID and Project `conversation_mode`. A live local test
confirmed that the upstream conversation stayed associated with the selected
Project, a later text-file turn used the same UUID, and a SIP client exchanged
PCMU RTP with nonzero return audio. These observations are not a stable public
contract.

The integrated browser established bidirectional audio but did not open its
DataChannel after an optional `a=sctp-init` answer attribute was removed for SDP
compatibility. In-call text/captions through that DataChannel are therefore not
validated in that browser; the page can still send text and attachments over
HTTP once a UUID is known. When text and voice are sent simultaneously, ordering
of turns depends on ChatGPT upstream. Send one text turn at a time when order
matters.

The [public Realtime WebRTC guide](https://developers.openai.com/api/docs/guides/voice-webrtc)
and [public SIP guide](https://developers.openai.com/api/docs/guides/voice-sip)
describe separate, supported APIs requiring an OpenAI API key. ChatGPT's
[Voice help](https://help.openai.com/en/articles/20001274-chatgpt-voice)
describes the product behavior, not this private signalling contract.
