# ChatGPT Web Voice: WebRTC and SIP/RTP

This is an experimental adapter to ChatGPT Web's private Voice signalling, not
the public OpenAI Realtime API. The primary UI is the WebRTC panel inside the
Bridge Console Test Lab at `http://127.0.0.1:8080/#test-lab`. It shares the chat
test's selected Project, model, conversation UUID, initial text, and text
attachments. It accepts a local audio file or microphone. The ChatGPT credential
stays on the bridge. Media flows through WebRTC directly between the peer and
ChatGPT; the bridge handles only SDP and optional chat preparation.

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

### Local SIP/RTP smoke test (Windows PowerShell)

Run these commands from the repository root in three terminals (the browser is
optional for this transport check). The local bridge must already have at least
one valid ChatGPT account configured.

1. Start or rebuild the API bridge:

   ```powershell
   docker compose up -d --build chatgpt-api
   ```

2. In terminal A, start the one-call SIP gateway. Use the same local bridge key
   configured in Compose; the local default is shown here:

   ```powershell
   $env:CHATGPT_API_KEY = "local-dev-key"
   uv run --extra sip chatgpt-sip --listen-ip 127.0.0.1 --sip-port 5066 --rtp-port 40006 --text "Responde brevemente en español a la prueba de voz."
   ```

3. In terminal B, run the SIP client, which sends an INVITE, PCMU/8000 RTP and
   BYE, then reports received return audio:

   ```powershell
   uv run --extra sip python scripts/sip_rtp_smoke.py
   ```

   The default 440 Hz tone verifies RTP transport and codec handling. To test a
   spoken turn, pass a short uncompressed PCM WAV (8, 16, 24, or 32 bit):

   ```powershell
   uv run --extra sip python scripts/sip_rtp_smoke.py --wav "C:\audio\prueba.wav"
   ```

4. A successful transport check reports `SIP INVITE: 200 OK`, received RTP
   packets, and `SIP BYE: 200`. The tone can be treated as noise and receive
   only silence; that still validates the transport. For a voice check, use a
   spoken WAV and confirm its PCM peak is greater than zero. `Ctrl+C` stops the
   gateway. All ports bind to loopback by default.

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
