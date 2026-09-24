# ChatGPT Web Voice: WebRTC and SIP/RTP

This is an experimental adapter to ChatGPT Web's private Voice signalling, not
the public OpenAI Realtime API. The browser page at `http://127.0.0.1:8000/voice`
supports an audio file or microphone, optional initial text, its chat model,
Project selection by name, an existing conversation UUID, and text attachments.
The ChatGPT credential stays on the bridge. Media flows through WebRTC directly
between the peer and ChatGPT; the bridge handles only SDP and optional chat
preparation.

## Browser test

1. Start the bridge: `docker compose up -d --build chatgpt-api`.
2. Open `/voice`, enter the local `CHATGPT_API_KEY`, and load Projects/models.
3. Optionally choose a Project and model, enter an existing conversation UUID,
   type an initial message, and attach UTF-8 `.txt`, `.md`, `.csv`, or `.json` files.
4. Choose a local audio file or microphone and connect. The initial message is
   completed before the WebRTC offer is exchanged; its answer and UUID appear
   on the page. An omitted Project creates a conversation outside Projects.
5. While connected, send additional text and text attachments through the
   page. When a UUID is known, these use Chat Completions with that UUID, so
   the response stays in the same chat. End the call to release the binding.

The local audio file is decoded into a browser media track. It is not uploaded
as a chat attachment. The page caps it at 20 MiB and five minutes. Microphone
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

Install the optional SIP transport and run it next to the local bridge:

```sh
uv run --extra sip chatgpt-sip --sip-port 5060 --rtp-port 40000
```

Set `CHATGPT_API_KEY` in the gateway environment to the bridge key first. The
gateway accepts one SIP/UDP call at a time, answers a PCMU/8000 RTP stream, and
acts as the WebRTC peer to ChatGPT Web. It supports `OPTIONS`, `INVITE`, `ACK`,
`CANCEL`, and `BYE`; rejected codecs receive SIP 488 and a second call receives
486. It sends silence during RTP gaps, bounds its jitter queue, and makes at
most two WebRTC reconnection attempts using the same bridge session ID. A
failed call closes rather than silently creating a new chat on another account.

The same initial context can be supplied with `--model`, `--project`,
`--conversation-id`, `--text`, and repeatable `--attachment path` options.
Attachments use the same text-file limits as the browser. This gateway is a
direct SIP user agent, not a registrar or PBX: point a trusted PBX/SIP client
at its IP and port. It defaults to loopback and accepts only `127.0.0.1`.
Remote operation requires an explicit `--listen-ip`, `--advertise-ip`, and
`--allow-ip` CIDR for trusted peers, plus network/firewall protection. The
gateway has no SIP Digest, TLS, SRTP, NAT traversal, transcoding, or multi-call
support; use a PBX/SBC for those functions. The bridge bearer key is never sent
in SIP or RTP.

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
