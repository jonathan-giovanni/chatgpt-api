# Chat request timings

Set `CHATGPT_REQUEST_METRICS=true` in your private `.env`, then recreate the API
with `docker compose up -d --build chatgpt-api`. Each
`POST /v1/chat/completions` response gets a generated `X-Request-Id`. When the
request ends, the API writes one JSON line with `event: chat_request_metrics`
to stderr; Docker collects it in `docker compose logs chatgpt-api`.

Metrics are disabled by default. Logs contain durations, status codes, counts,
and booleans. They exclude prompt/response text, account names, conversation or
Project IDs, URLs, headers, cookies, tokens, and exception messages.

## What is measured

All durations use a monotonic clock. Marks are milliseconds since the HTTP
handler entered `do_POST`. This excludes time before the server receives the
request; the benchmark also measures the client's end-to-end elapsed time.

| Timing | Meaning |
| --- | --- |
| `local.request_parse` | Reading and decoding the inbound JSON body |
| `local.conversation_lookup`, `local.project_lookup` | Local SQLite lookup; no ChatGPT history/context fetch |
| `local.model_account_selection`, `local.capture_load_provider` | Local model/account settings and decrypting/loading the capture |
| `local.account_routing`, `local.bridge_settings` | Account routing and local concurrency configuration reads |
| `queue.chat`, `queue.account` | Time waiting to acquire the local concurrency semaphores |
| `local.payload_and_uploads` | Building the upstream payload; includes uploads if files exist |
| `upstream.prepare`, `upstream.requirements` | Separate HTTP requests preceding the conversation |
| `local.proof_of_work` | CPU time generating the requested session proof |
| `session.prepare_requirements_proof` | Parent span covering those preparation steps |
| `upstream.conversation_headers` | Conversation POST until response headers are received |
| `upstream_conversation_send`, `upstream_first_event` | Start of conversation POST and first parsed SSE event |
| `provider_first_text`, `provider_last_text` | First and last non-empty assistant text delta received by the async transport |
| `client_first_text`, `client_last_text` | First and last text delta flushed to the downstream SSE client |
| `upstream_done`, `upstream_eof` | Upstream `[DONE]` and end of its response body |
| `local.finalize_conversation` | Saving the parent message and conversation binding locally |
| `upstream.conversation_snapshot`, `upstream.conversation_init` | Additional HTTP reads if the particular flow needs them |
| `upstream.websocket_handoff` | Optional WebSocket URL request, connection, and topic reads after SSE |
| `client_complete`, `total_ms` | Final response flushed and handler completion |

Spans can be nested; **do not add parent and child durations**. A streamed HTTP
200 can still contain a provider failure. Check `facts.provider_failed` and
span error/status fields, rather than counting the status alone as success.
`facts.provider_attempts` counts actual calls to the chat transport.

For ordinary text chat with a known Project, the usual successful path is:

1. Resolve Project/account and any continuation UUID locally.
2. Acquire local concurrency slots and build the new message payload.
3. POST `conversation/prepare`, POST `sentinel/chat-requirements`, and generate
   the proof locally when required.
4. POST the conversation and relay its streamed text.
5. Save the latest parent message and conversation binding locally.

New chats and continuations both refresh prepare/requirements. A Project's
instructions/history are resolved upstream using its ID. The wrapper does not
download and prepend Project context to ordinary text requests. A missing
parent message may cause a final conversation snapshot; files, errors,
research, and tools have other paths and can add HTTP calls. Inspect the trace
for the actual request rather than assuming every route follows the text path.

The current transport uses a fresh curl session for each `requests.post/get`
call. Session timings therefore include network connection setup. The marks
locate time spent waiting for upstream text versus local preparation, but do
not distinguish ChatGPT's internal computation from network/server queue time.
`supports_buffering` is inherited from the capture and defaults to `true`;
first-text timing is when the wrapper receives text, not an internal model
token timestamp.

The transport currently consumes the upstream body until EOF, including after
`[DONE]`, and only then saves the conversation parent and sends the downstream
completion marker. Measure **last text to upstream done**, **done to EOF**, and
**EOF to client completion** separately: waiting for completion is not the same
as generating more visible text. A client using `stream: false` waits for all
of these steps before receiving its JSON response.

Local settings are read repeatedly while routing and setting concurrency
limits. `_admin_store` constructs `BridgeAdminStore` on each call; its
constructor checks the SQLite schema before the requested query. Docker bind
mount I/O is included in these local timings. A slow local span does not by
itself establish whether disk I/O, lock contention, or scheduling caused it.

## Repeatable live benchmark

Put a synthetic prompt into an ignored local text file. Then run:

```sh
python scripts/benchmark_chat_latency.py \
  --project "<configured Project name>" \
  --model "<model from /v1/models>" \
  --prompt-file outputs/test-prompt.txt \
  --conversations 20 --follow-ups 3
```

The script sends 80 requests **sequentially**: an initial message plus three
continuations in each of 20 new conversations. It repeats the same input to
control its size, sends only the current message with the UUID on later turns,
and does not set thinking effort. Project instructions and conversation
history can still affect response length and latency. No benchmark requests
are retried; a failed response or changed UUID stops the run.

The script uses `CHATGPT_API_KEY` from the environment or private `.env` and
writes to ignored `outputs/request-metrics/client.jsonl`. That local file
includes conversation UUIDs and response text so continuity can be audited;
keep it private. Pair its `request_id` with the server log to compare time to
first text, completion, and internal preparation. Separate error samples and
report response length along with averages/medians. These observations do not
establish universal performance for other Projects or models.
