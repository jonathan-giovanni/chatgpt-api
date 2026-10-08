# ChatGPT Web Voice: WebRTC and SIP/RTP

This is an experimental adapter to ChatGPT Web's private Voice signalling, not
the public OpenAI Realtime API. The primary UI is the WebRTC panel inside the
Bridge Console Test Lab at `http://127.0.0.1:8080/#test-lab`. It shares the chat
test's selected Project, model, conversation UUID, initial text, and
attachments. It accepts a local audio file or microphone. The ChatGPT credential
stays on the bridge. Media flows through WebRTC directly between the peer and
ChatGPT; the bridge handles SDP, optional chat preparation, and text event fan-out.

The wrapper watches local and returned audio without injecting health-check
messages into the chat. It closes the flow when the remote audio track ends,
when ChatGPT has no audio response for 30 seconds after a user turn, or after
30 seconds without voice activity following a response. Failed transport
reconnects are attempted twice; if they fail, the session is released.

## Browser test

1. Start both local services: `docker compose up -d --build chatgpt-api bridge-console`.
2. Open `http://127.0.0.1:8080/#test-lab` in the Bridge Console. Use **Refresh** if its status is still loading.
3. In **Single message test**, select a Project (optional), model, existing conversation UUID if continuing, initial message, and supported attachments.
4. In **Voz en esta conversación** below it, choose a local audio file or microphone and start voice. The panel uses the same Project, model, UUID, message, and attachments. An empty Project creates the conversation outside Projects.
5. When the UUID is known, use the voice panel's follow-up composer for text or text-file turns in the same thread. The outer chat panel also receives that UUID for later chat tests. Finalizar releases the voice binding.
6. The **Mensajes de voz y texto** panel below the voice controls reads that UUID automatically. You can also paste a UUID from a SIP call. It loads history once and then receives text events. **Actualizar** explicitly reloads history; the panel does not poll ChatGPT.

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
  "voice": "fathom",
  "model": "auto",
  "project": "My Project",
  "text": "Start by summarizing these notes.",
  "files": [{"filename": "notes.pdf", "file_data": "<base64>"}]
}
```

`voice` is optional and defaults to `fathom` (the ChatGPT voice named Arbor).
The UI displays ChatGPT's names and sends their internal IDs: Arbor=`fathom`,
Breeze=`breeze`, Ember=`ember`, Sol=`glimmer`, Cove=`cove`, Spruce=`orbit`,
Vale=`vale`, Maple=`maple`, and Juniper=`juniper`. The former `arbor`, `sol`,
and `spruce` IDs remain accepted as aliases. `project`, `text`, `files`, `model`, and
`conversation_id` are optional. A new conversation is created when
`conversation_id` is omitted. `files` requires
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

Apply `answer_sdp` directly as the remote description, without trimming it.
The bridge normalizes SDP records to CRLF, retains the final line terminator,
and removes the optional `a=sctp-init` extension for browser/aiortc compatibility.
This applies to initial calls and reconnects for both WebRTC and SIP/RTP;
ICE credentials, DTLS fingerprints and media attributes are preserved.
For a new call with no initial
text, ChatGPT creates the conversation when speech or an in-call message arrives;
the UUID may arrive later through a DataChannel event. The UUID can then be used
with `POST /v1/chat/completions` for subsequent text turns and attachments.

## Read the conversation text

Any client can read the visible text timeline without sending a message into the
conversation:

```http
GET /v1/chatgpt/conversations/<UUID>/messages
Authorization: Bearer <bridge-key>
```

The response contains the selected ChatGPT account and ordered messages with
`id`, `role` (`user` or `assistant`), `text`, `created_at` (UTC ISO 8601), and
`status`. For example:

```json
{
  "conversation_id": "b7f60d76-41d0-4de3-b661-f6f4f8798224",
  "account": "main-free",
  "messages": [
    {"id": "<message-id>", "role": "user", "text": "Hola", "created_at": "2026-09-29T10:00:00Z", "status": "finished_successfully"}
  ],
  "untranscribed_audio_messages": 0
}
```

The bridge uses the saved account for known UUIDs. For an existing ChatGPT
conversation that the bridge has not seen, it checks the configured accounts
and saves the successful account association. `?account=<alias>` selects one
configured account explicitly. This endpoint performs one read when requested;
it does not send a prompt or change the conversation. Repeatedly downloading
the full history was observed to trigger HTTP 429, so the dashboard now uses
the event stream below instead of periodic history requests. If the initial
history load is rate-limited, it stops and asks for a manual retry in a minute.

This endpoint is a history snapshot, not token-by-token transcription. Voice
turns appear as text only if ChatGPT includes their transcription in its
conversation history. `untranscribed_audio_messages` counts visible audio
nodes with no text; the bridge does not invent missing words. A new voice call
without initial text can return `conversation_id: null` until ChatGPT creates
the thread and its UUID becomes available. For a SIP call with initial text or
an existing UUID, or when the data channel announces a new conversation, the
gateway prints `SIP conversation UUID: ...` in its logs.

### Live text by conversation UUID

Open one authenticated stream from any HTTP client:

```shell
curl -N -H "Authorization: Bearer <bridge-key>" \
  http://127.0.0.1:8000/v1/chatgpt/conversations/<UUID>/events
```

The stream first sends the available timeline, then upserts individual
messages as their text or status changes. Replace messages by `message.id`;
do not append the entire text again. Roles and UTC timestamps are preserved.

```text
id: 0
event: snapshot
data: {"id":0,"type":"snapshot","conversation_id":"<UUID>","account":"main-free","messages":[],"untranscribed_audio_messages":0}

id: 1
event: message
data: {"id":1,"type":"message","conversation_id":"<UUID>","message":{"id":"<message-id>","role":"user","text":"Hola","created_at":"2026-09-29T10:00:00Z","status":"in_progress"}}
```

SSE heartbeats are local comments, every 15 seconds. They do not query ChatGPT
or send conversation messages. Reconnects receive a current cached snapshot,
so clients recover without repeating an upstream history request. The cache
is in memory, capped at 128 conversations, 2,000 messages per conversation,
and one hour of inactivity. A server restart requires a new snapshot; this
component is intended for a single bridge process. It does not introduce a
database of private transcripts or a background ChatGPT polling worker.

Live text comes from the **active call owned by the wrapper**. A UUID alone
does not subscribe to voice calls started separately in ChatGPT Web. Their
stored text is available through the history endpoint or manual **Actualizar**.
The bridge forwards only the text ChatGPT provides; it does not run a separate
speech recognizer or invent missing transcription.

### External WebRTC clients

The integrated Test Lab and SIP gateway already forward their received events.
Other WebRTC clients need one data channel listener and the following POST:

```http
POST /v1/chatgpt/voice/sessions/vs_<opaque-id>/events
Authorization: Bearer <bridge-key>
Content-Type: application/json

{"sequence":0,"events":[{"type":"data_message","data":"<JSON received from the data channel>"}]}
```

Keep events in arrival order. Batch up to 50 events and 512 KiB, increment
`sequence` after acknowledgement, and reuse the same sequence when retrying
that batch. Continue the sequence when reconnecting the same bridge session.
The response contains `accepted` and the discovered `conversation_id`.
Expired/released sessions are rejected; a bound conversation cannot change
UUID or account. Only message deltas and conversation announcements are kept.

The observed upstream frames are `data_message` envelopes containing
`startup_telemetry`, `chat_message_delta`, and `conversation_update`. Message
deltas carry compressed `c`, `p`, `o`, and `v` fields: unchanged counters,
operations or paths can be omitted. The shared decoder preserves that state,
applies append/replace patches, and publishes both user and assistant text.
SIP uses the same decoder and SSE API as the browser.

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

For the shortest end-to-end SIP audio plus live text workflow, including the
gateway's UUID and the SSE command, see the
[API walkthrough](OPENAI_COMPATIBILITY.md#siprtp-call-with-live-text).

The Bridge Console command includes the selected project, voice ID, model, UUID
and text. In the SIP/RTP section, select files and click **Preparar adjuntos**.
The console uploads them to a local, authenticated batch and adds its ID to the
generated Docker command. The gateway fetches that list and sends all files
with the initial text in the first voice-session request. If the initial text is
empty, it adds a short context message. Reconnects do not replay attachments.
Arbor (`fathom`) is the default voice, and the selected internal voice ID is
passed to both WebRTC and SIP/RTP. Staged batches expire after 24 hours and
stay under the ignored local `outputs/sip-attachments/` directory until cleanup.
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
options. `--attachment-batch sa_<id>` accepts a batch prepared through the API
or console. Supported files can be mixed: UTF-8 TXT/MD/CSV/JSON/HTML/XML/YAML/LOG,
PDF, DOCX/XLSX/PPTX, PNG/JPEG/WebP/GIF, and WAV/MP3. The bridge allows at most
10 attachments, 20 MiB each, and 25 MiB total. Upstream processing of audio
attachments depends on the selected ChatGPT model.
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

DataChannel support depends on the peer implementation; audio alone does not
prove the text channel is open. The UI reports when its data channel has not
opened, and history remains available on demand. When text and voice are sent simultaneously, ordering
of turns depends on ChatGPT upstream. Send one text turn at a time when order
matters.

The [public Realtime WebRTC guide](https://developers.openai.com/api/docs/guides/voice-webrtc)
and [public SIP guide](https://developers.openai.com/api/docs/guides/voice-sip)
describe separate, supported APIs requiring an OpenAI API key. ChatGPT's
[Voice help](https://help.openai.com/en/articles/20001274-chatgpt-voice)
describes the product behavior, not this private signalling contract.
