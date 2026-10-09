import assert from "node:assert/strict";
import test from "node:test";
import { readChatCompletion } from "../src/lib/chatStream.ts";

const encoder = new TextEncoder();
function response(chunks: Uint8Array[]) {
  return new Response(new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(chunk);
      controller.close();
    },
  }), { headers: { "Content-Type": "text/event-stream; charset=utf-8" } });
}
const event = (content: string, conversationId = "conversation-uuid") =>
  `data: ${JSON.stringify({ conversation_id: conversationId, choices: [{ delta: { content } }] })}\n\n`;

test("delivers the first text while the HTTP stream remains open", async () => {
  let controller: ReadableStreamDefaultController<Uint8Array>;
  const stream = new ReadableStream<Uint8Array>({ start(value) { controller = value; } });
  const updates: string[] = [];
  let firstReceived: () => void;
  const first = new Promise<void>((resolve) => { firstReceived = resolve; });
  let complete = false;
  const result = readChatCompletion(new Response(stream, {
    headers: { "Content-Type": "text/event-stream" },
  }), (progress) => { updates.push(progress.content); firstReceived(); }).then((value) => {
    complete = true;
    return value;
  });
  controller!.enqueue(encoder.encode(event("Primero")));
  await first;
  assert.equal(complete, false);
  assert.deepEqual(updates, ["Primero"]);
  controller!.enqueue(encoder.encode("data: [DONE]\n\n"));
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(complete, false, "[DONE] does not skip the HTTP body drain");
  controller!.close();
  const value = await result;
  assert.equal(value.content, "Primero");
  assert.equal(value.conversationId, "conversation-uuid");
  assert.equal(typeof value.firstTextMs, "number");
  assert.equal(typeof value.totalMs, "number");
});

test("handles UTF-8, CRLF and multiline SSE frames split at every byte", async () => {
  const raw = ': heartbeat\r\n\r\ndata: {"chatgpt_conversation_id":"uuid",\r\ndata: "choices":[{"delta":{"content":"á🙂"}}]}\r\n\r\n' +
    event(" listo", "uuid").replaceAll("\n", "\r\n") + "data: [DONE]\r\n\r\n";
  const result = await readChatCompletion(response(Array.from(encoder.encode(raw), (byte) => new Uint8Array([byte]))), () => {});
  assert.equal(result.content, "á🙂 listo");
  assert.equal(result.conversationId, "uuid");
});

test("does not count role-only frames as first text", async () => {
  const updates: Array<number | null> = [];
  const result = await readChatCompletion(response([encoder.encode(
    'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n' + event("Hola") + "data: [DONE]\n\n",
  )]), (value) => updates.push(value.firstTextMs));
  assert.equal(updates[0], null);
  assert.equal(typeof result.firstTextMs, "number");
});

test("accepts a final DONE frame without a trailing newline", async () => {
  const result = await readChatCompletion(response([encoder.encode(event("Hola") + "data: [DONE]")]), () => {});
  assert.equal(result.content, "Hola");
});

test("rejects EOF without DONE while retaining already delivered text", async () => {
  let partial = "";
  await assert.rejects(readChatCompletion(response([encoder.encode(event("Parcial"))]), (value) => { partial = value.content; }), /sin confirmar \[DONE\]/);
  assert.equal(partial, "Parcial");
});

test("shows the real HTTP JSON error", async () => {
  await assert.rejects(readChatCompletion(new Response(JSON.stringify({ error: { message: "Credenciales caducadas" } }), {
    status: 401, headers: { "Content-Type": "application/json" },
  }), () => {}), /Credenciales caducadas/);
});

test("rejects errors returned as HTTP 200 JSON", async () => {
  await assert.rejects(readChatCompletion(new Response(JSON.stringify({ error: { message: "Límite agotado" } }), {
    headers: { "Content-Type": "application/json" },
  }), () => {}), /Límite agotado/);
});

test("rejects a structured SSE error", async () => {
  await assert.rejects(readChatCompletion(response([encoder.encode('data: {"error":{"message":"No disponible"}}\n\n')]), () => {}), /No disponible/);
});

test("rejects an event:error message", async () => {
  await assert.rejects(readChatCompletion(response([encoder.encode('event: error\ndata: {"message":"No disponible"}\n\n')]), () => {}), /No disponible/);
});

test("rejects provider failures encoded as assistant text, even with DONE", async () => {
  await assert.rejects(readChatCompletion(response([encoder.encode(event("ChatGPT provider error (429): límite") + "data: [DONE]\n\n")]), () => {}), /provider error \(429\)/);
});

test("recognizes a provider error prefix split across delta messages", async () => {
  await assert.rejects(readChatCompletion(response([encoder.encode(event("ChatGPT provider ") + event("error (403): sesión") + "data: [DONE]\n\n")]), () => {}), /provider error \(403\)/);
});

test("rejects malformed SSE JSON", async () => {
  await assert.rejects(readChatCompletion(response([encoder.encode("data: {invalid}\n\n")]), () => {}), /mensaje inválido/);
});

test("cancels an open body when an SSE error or malformed frame fails", async () => {
  for (const raw of [
    'data: {"error":{"message":"No disponible"}}\n\n',
    "data: {invalid}\n\n",
  ]) {
    let cancelled = false;
    const stream = new ReadableStream<Uint8Array>({
      start(controller) { controller.enqueue(encoder.encode(raw)); },
      cancel() { cancelled = true; },
    });
    await assert.rejects(readChatCompletion(new Response(stream, {
      headers: { "Content-Type": "text/event-stream" },
    }), () => {}));
    assert.equal(cancelled, true);
    assert.equal(stream.locked, false);
  }
});

test("preserves the parsing error if cancelling the body also fails", async () => {
  const stream = new ReadableStream<Uint8Array>({
    start(controller) { controller.enqueue(encoder.encode("data: {invalid}\n\n")); },
    cancel() { throw new Error("Cancel failed"); },
  });
  await assert.rejects(readChatCompletion(new Response(stream, {
    headers: { "Content-Type": "text/event-stream" },
  }), () => {}), /mensaje inválido/);
  assert.equal(stream.locked, false);
});

test("ignores data after DONE but drains it without adding content", async () => {
  const result = await readChatCompletion(response([encoder.encode(event("Final") + "data: [DONE]\n\n" + event("extra"))]), () => {});
  assert.equal(result.content, "Final");
});
