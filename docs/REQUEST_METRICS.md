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
| `local.admin_store_initialize` | SQLite schema initialization, once per database path within each chat request |
| `queue.chat`, `queue.account` | Time waiting to acquire the local concurrency semaphores |
| `local.payload_and_uploads` | Building the upstream payload; includes uploads if files exist |
| `upstream.prepare`, `upstream.requirements` | Separate HTTP requests preceding the conversation |
| `local.proof_of_work` | CPU time generating the requested session proof |
| `session.prepare_requirements_proof` | Parent span covering those preparation steps |
| `upstream.conversation_headers` | Conversation POST until response headers are received |
| `upstream_conversation_send`, `upstream_first_event` | Start of conversation POST and first parsed SSE event |
| `provider_first_text`, `provider_last_text` | First and last non-empty assistant text delta received by the async transport |
| `upstream_first_text_parsed`, `upstream_last_text_parsed` | Text parsed in the HTTP worker, before the async queue; compare completion events on this same clock |
| `client_first_text`, `client_last_text` | First and last text delta flushed to the downstream SSE client |
| `upstream_message_finished`, `upstream_end_turn`, `upstream_message_complete` | Observed message status/end-turn/completion events; diagnostic, never used alone to truncate the reply |
| `upstream_done`, `upstream_eof`, `upstream_stream_closed` | Upstream `[DONE]`, natural body EOF when consumed, and completion of resource cleanup |
| `local.upstream_response_close` | Time spent in the HTTP library's synchronous response cleanup |
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

By default, new chats and continuations both refresh prepare/requirements. A Project's
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

The transport stops parsing at upstream `[DONE]`, explicitly closes the HTTP
response, saves the conversation parent, and sends the downstream completion
marker. An earlier message status change alone cannot safely terminate a
conversation stream: tool messages or a WebSocket handoff may follow. Handoffs
observed before `[DONE]` are still followed. The HTTP library's `close()` can
itself wait for its background network task, so stopping parsing does **not**
guarantee an equivalent reduction in elapsed time.

Measure **last text to message finished**, **message finished to upstream done**,
**done to stream closed**, and **closed to client completion** separately. With
the EOF control enabled, also measure **done to EOF**. A missing `upstream_eof`
mark is expected when parsing stops on `[DONE]`. A client using `stream: false`
waits for the full reply and finalization before receiving its JSON response.

Local settings are read while routing and setting concurrency limits. Chat
requests now reuse the `BridgeAdminStore` instance within that request, avoiding
repeated schema initialization. Its queries still read SQLite, and the next
request creates a fresh instance; settings and Project rows are not cached.
The latest parent is persisted **before** completing the downstream response.
Docker bind
mount I/O is included in these local timings. A slow local span does not by
itself establish whether disk I/O, lock contention, or scheduling caused it.

Some upstream failures arrive as SSE error events inside HTTP 200, including
`error_code: usage_limit`. The transport propagates that error instead of
discarding it and diagnosing empty output. JSON requests receive the mapped
error status; an SSE response that already started still has HTTP 200 and
contains a provider error. Metrics mark `upstream_stream_error`,
`upstream_usage_limit` and `provider_failed`. For an explicit ordinary-chat
stream error, the wrapper does not add a conversation-init request merely to
guess the cause. Model reset metadata is only attributed to an actual matching
model, never borrowed from a different model's entry.

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

## Preparation and stream controls

These provider-specific switches work for JSON and streamed ordinary chat:

| Setting | Default | Behavior |
| --- | --- | --- |
| `CHATGPT_PARALLEL_PREPARATION` | `false` | Opt in to overlapping fresh prepare/requirements HTTP calls. Both use the original captured session headers; requirements does not consume the prepare response. No additional retry or request is introduced. |
| `CHATGPT_REQUIREMENTS_CACHE_TTL_SECONDS` | `0` | Experimental, process-local requirements/proof reuse; `0` disables it, values are capped at 900 seconds. Prepare is always fresh. |
| JSON `chatgpt_parallel_preparation` | Environment default | Boolean override for comparing sequential and parallel preparation. |
| JSON `chatgpt_reuse_requirements` | Environment TTL | `false` forces fresh requirements; `true` permits reuse only if a nonzero TTL is configured and the entry is eligible. |
| JSON `chatgpt_stream_close_on_done` | `true` | `false` consumes EOF as a diagnostic baseline; `true` stops parsing at `[DONE]` and closes the response. |

Cache entries bind to the successfully returned conversation UUID. They are
isolated by session/credential fingerprint, browser identity, model, Project,
thinking effort, and temporary-chat mode. Hits do not extend their original
expiry. The cache holds at most 128 entries in RAM, does not write tokens to
disk, and is cleared on restart. Credential changes create a different scope.

**An interactive CAPTCHA/Arkose/Turnstile requirement makes the result
ineligible for caching.** No challenge is bypassed. An explicit invalid-token
JSON error before stream acceptance invalidates the entry and allows one fresh
attempt; other HTTP rejections invalidate it without that replay. Network
timeouts and ambiguous failures do not trigger a token-cache retry. The
900-second TTL is an experimental client limit, not a documented upstream
validity guarantee. A run with zero cache hits cannot establish token reuse
or its latency benefit.

## Paired comparison with and without a Project

Use the same synthetic prompt, model, account and target output length for
both scopes. Configure timings and the experimental TTL locally, then run:

```sh
python scripts/benchmark_chat_reuse.py \
  --project "<configured Project name>" --model "<model from /v1/models>" \
  --prompt-file outputs/test-prompt.txt --pairs 10
```

This creates 10 Project threads and 10 ordinary threads in the Project's account.
Each thread gets a seed turn plus four continuations: fresh/cached requirements
crossed with EOF/DONE termination. Scope order alternates and continuation
order rotates. Confirm actual `facts.requirements_cache_hit`, response length,
all upstream status codes and UUID continuity before comparing timings.

Add `--age-probes` to check original cached tokens at 5, 10, approximately
14.75 minutes and after the 15-minute client expiry. These probes do not
demonstrate reuse if challenges make the entry ineligible. If caching is
ineligible, use `--parallel-controls` instead to compare sequential/parallel
fresh preparation crossed with EOF/DONE termination. Do not combine those
modes to claim a token age guarantee. `--resume-from` can reuse initial UUIDs
from a local interrupted run, with a new warmup; it never automatically replays
an unfinished request. Report those warmups separately from first-turn data.
`--continue-results` appends after recorded cases, including failures, without
replaying them; it rejects changing models within the output. Use a separate
output directory if the chosen model hits a usage limit and another model is
needed. Keep failures separate from latency averages for successful replies.

Results stay under ignored `outputs/requirements-cache/`. Server logs have no
content or identities; local client results include private UUIDs and text.
