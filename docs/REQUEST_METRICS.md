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
| `local.admin_store_initialize` | SQLite schema-version check, once per database path within each chat request; DDL runs only when the schema needs initialization |
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

New chats and continuations both refresh prepare/requirements; the two fresh
HTTP calls run in parallel by default. A Project's
instructions/history are resolved upstream using its ID. The wrapper does not
download and prepend Project context to ordinary text requests. A missing
parent message may cause a final conversation snapshot; files, errors,
research, and tools have other paths and can add HTTP calls. Inspect the trace
for the actual request rather than assuming every route follows the text path.

The transport uses fresh curl sessions. Both preparation requests still run
in parallel; authentication and proof validation remain fresh on every turn.
There is no token, response, or persistent authentication cache. The marks
locate time spent waiting for upstream text versus local preparation, but do
not distinguish ChatGPT's internal computation from network/server queue time.
`supports_buffering` is inherited from the capture and defaults to `true`;
first-text timing is when the wrapper receives text, not an internal model
token timestamp.

The conversation POST always uses the existing fresh-session `stream=True`
path and relays assistant text as it arrives. At upstream `[DONE]`, it stops
parsing SSE events and explicitly closes the response. The bridge then saves
the conversation parent and sends the downstream completion marker. An earlier
message status change alone cannot safely terminate a
conversation stream: tool messages or a WebSocket handoff may follow. Handoffs
observed before `[DONE]` are still followed. Response close can wait for the
library's background network task; stopping parsing does not guarantee an
equivalent reduction in elapsed time. Experimental HTTP pooling and a
conversation-callback path were removed after controlled comparisons did not
establish faster replies. The working conversation stream and its completion
semantics are preserved.

Measure **last text to message finished**, **message finished to upstream done**,
**done to stream closed**, and **closed to client completion** separately. A
missing `upstream_eof` mark is expected when parsing stops on `[DONE]`. A client
using `stream: false` waits for the full reply and finalization before receiving
its JSON response.

Bridge Console Test Lab uses `stream: true` for ordinary text without
attachments. It renders incoming text deltas and reports first-text and total
elapsed time, while preserving the selected Project and continuation UUID.
Its SSE reader consumes the response through EOF and handles `[DONE]` without
truncating the text. Requests with attachments retain their existing JSON
response path. Streaming lets users see available text earlier; it does not
make ChatGPT generate the answer faster.

Local settings are read while routing and setting concurrency limits. Chat
requests reuse the `BridgeAdminStore` instance within that request, avoiding
repeated schema checks. `PRAGMA user_version` skips DDL once the schema is
initialized, and SQLite connections are explicitly closed after each operation.
Single-account routing skips metadata reads needed only for ordering an account
pool. Concurrency configuration is resolved once per request. Queries still read
SQLite, and the next request reads current settings; settings and Project rows
are not persistently cached.
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

## Preparation and model defaults

These provider-specific switches work for JSON and streamed ordinary chat:

| Setting | Default | Behavior |
| --- | --- | --- |
| `CHATGPT_PARALLEL_PREPARATION` | `true` | Overlap fresh prepare/requirements HTTP calls. Both use the original captured session headers; requirements does not consume the prepare response. No additional retry or request is introduced. Set `false` for the sequential baseline. |
| JSON `chatgpt_parallel_preparation` | Environment default | Boolean override for comparing sequential and parallel preparation. |
| `CHATGPT_DEFAULT_MODEL` / server `--default-model` | `gpt-6-mini` | Lightweight ordinary-chat default when the client omits `model`; an explicit model, including `auto`, is preserved. `/v1/models` lists observed models and the configured default; `source: configured` alone does not confirm account support. |

Requirements/proof caching, HTTP pooling, callback streaming and their
experimental controls were removed. Cache trials had no eligible hits, and
the transport comparisons did not establish faster replies. Session validation
remains fresh on every turn. The conversation transport stops parsing at
`[DONE]` and closes its response. No extra speculative request or retry is
added for performance.

`gpt-6-mini` was validated in the local comparison, not established as the
fastest model for every account or workload. Change the configured default if
your account lacks that model; use explicit `auto` to delegate model selection
to ChatGPT. The live voice handshake still lets ChatGPT select its voice model.

## Paired comparison with and without a Project

Use the same synthetic prompt, model, account and target output length for
both scopes. Enable request timings locally, then run:

```sh
python scripts/benchmark_chat_preparation.py \
  --project "<configured Project name>" --model "<model from /v1/models>" \
  --prompt-file outputs/test-prompt.txt --pairs 10
```

This creates 10 Project threads and 10 ordinary threads in the Project's account.
Each thread gets a seed turn plus two continuations: sequential and parallel
fresh preparation, for 60 requests in total. Scope order alternates and
continuation order rotates. Confirm `facts.parallel_preparation`, response
length, all upstream status codes and UUID continuity before comparing timings.

`--resume-from` can reuse initial UUIDs
from a local interrupted run, with a new warmup; it never automatically replays
an unfinished request. Report those warmups separately from first-turn data.
`--continue-results` appends after recorded cases, including failures, without
replaying them; it rejects changing models within the output. Use a separate
output directory if the chosen model hits a usage limit and another model is
needed. Keep failures separate from latency averages for successful replies.

Results stay under ignored `outputs/chat-preparation/`. Server logs have no
content or identities; local client results include private UUIDs and text.

## Validation and discarded experiments: 2026-10-09

After the initial preparation comparison, 112 additional `gpt-6-mini` follow-ups
completed successfully on 10 existing conversation UUIDs. They comprised 50
callback/pooling trials, 12 framing checks, and 50 preparation-only pooling
trials. There were 24 warmups and 88 comparison requests, with stable UUIDs,
one provider attempt per request, and equal output lengths. Warmups are excluded
from the following means; each listed experiment compares 20 controls with 20
variant requests.

| Discarded experiment | Mean first text, control → variant | Mean full response, control → variant | Observed change in full response |
| --- | ---: | ---: | --- |
| Pooled HTTP and callback conversation stream | 3,818 → 3,812 ms | 6,197 → 6,887 ms | 11.13% higher |
| Pooling only prepare/requirements | 3,757 → 4,422 ms | 5,933 → 6,881 ms | 15.98% higher |

Neither experiment established a response-latency benefit. Bootstrap intervals
resampled conversation UUIDs and crossed zero for the combined first-text and
completion differences. These observed increases do not prove that pooling
caused the delay: upstream server/network variability remained substantial.
The preparation-only experiment's median preparation was 378.711 versus
378.504 ms, effectively unchanged, despite successful connection reuse. Both
pooling implementations and the callback experiment were removed.

The earlier 100-response run used 20 new conversations and 80 continuation
controls. Fresh parallel preparation reduced its combined mean preparation
time by 28.2%, but mean full response time was 1.4% higher; it did not establish
an overall reply-time improvement. Parallel preparation remains because it
overlaps independent mandatory calls without caching or adding requests.

The largest callback-trial response took 16.58 s: 11.34 s elapsed between the
last text and `[DONE]`, while `[DONE]` to body EOF took only 1.64 ms. This places
the main delay before the upstream termination marker rather than in local
HTTP cleanup; it cannot distinguish network waiting from server processing.
An earlier message-completion status is not used to truncate the stream.
Inputs, account/Project identifiers and complete traces remain ignored and
local; only generic numeric results are published here.

### Local database comparison

A separate synthetic benchmark ran in Docker against a Windows bind-mounted
SQLite database. It alternated 100 before/after pairs after 10 warmup pairs,
timing store initialization and one settings read. Garbage collection ran
outside the timed spans; old connections were allowed their original deferred
cleanup, while the new path includes explicit connection closure.

| Local operation | Mean before → after | Median before → after | Mean reduction |
| --- | ---: | ---: | ---: |
| Store initialization plus one settings read | 37.670 → 24.872 ms | 35.774 → 24.936 ms | 33.97% (12.798 ms) |
| Store initialization alone | 31.306 → 14.709 ms | 29.192 → 14.780 ms | 53.02% (16.597 ms) |
| Settings read alone | 6.365 → 10.162 ms | 5.660 → 9.908 ms | 59.65% higher |

The full local operation saved about 13 ms on average, despite a slower
individual settings read. This benchmark measures local database work only;
it does not establish a reduction in total ChatGPT response time. Request-local
store reuse and a single concurrency-settings lookup also avoid repeated work,
but their additional savings are not isolated by this comparison.

In the latest fresh-session control group above, mean first-text and completion
times were 3,757 and 5,933 ms. That leaves a 2.18 s average interval during which
a streaming client can display received text before a buffered JSON client
would receive its complete response. This is a delivery opportunity observed
in those traces, not a measured reduction in model generation time or a new
browser benchmark.
