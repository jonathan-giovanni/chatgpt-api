export type ChatStreamProgress = {
  content: string;
  conversationId: string;
  firstTextMs: number | null;
};

export type ChatStreamResult = ChatStreamProgress & { totalMs: number };

function errorMessage(payload: unknown, fallback: string): string {
  if (typeof payload === "string" && payload.trim()) return payload;
  if (payload && typeof payload === "object") {
    const value = payload as Record<string, unknown>;
    if (typeof value.message === "string") return value.message;
    if (value.error) return errorMessage(value.error, fallback);
  }
  return fallback;
}

/** Read the existing chat SSE API and finish only after a complete HTTP body. */
export async function readChatCompletion(
  response: Response,
  receive: (progress: ChatStreamProgress) => void,
  started = performance.now(),
): Promise<ChatStreamResult> {
  const contentType = response.headers.get("content-type") ?? "";
  if (!response.ok || !contentType.includes("text/event-stream")) {
    const text = await response.text();
    let payload: unknown = text;
    try {
      payload = JSON.parse(text);
    } catch {
      // A proxy may return plain text instead of the API's JSON error.
    }
    throw new Error(errorMessage(payload, response.ok
      ? "La respuesta no es un stream de chat."
      : `HTTP ${response.status}`));
  }
  if (!response.body) throw new Error("El navegador no permite leer el stream.");

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let data: string[] = [];
  let eventType = "";
  let done = false;
  let content = "";
  let conversationId = "";
  let firstTextMs: number | null = null;

  function dispatch() {
    const raw = data.join("\n");
    data = [];
    const type = eventType;
    eventType = "";
    if (!raw || done) return;
    if (raw.trim() === "[DONE]") {
      done = true;
      return;
    }
    let payload;
    try {
      payload = JSON.parse(raw);
    } catch {
      throw new Error("El stream de chat contiene un mensaje inválido.");
    }
    if (type === "error" || payload?.error) {
      throw new Error(errorMessage(payload, "La petición de chat falló."));
    }
    const id = payload?.conversation_id ?? payload?.chatgpt_conversation_id;
    if (typeof id === "string" && id) conversationId = id;
    const delta = payload?.choices?.[0]?.delta?.content;
    if (typeof delta === "string" && delta) {
      content += delta;
      // This API currently encodes provider failures as an assistant delta.
      if (/^ChatGPT provider error \(\d{3}\):/.test(content)) {
        throw new Error(content);
      }
      firstTextMs ??= Math.round(performance.now() - started);
    }
    receive({ content, conversationId, firstTextMs });
  }

  function line(value: string) {
    if (!value) {
      dispatch();
    } else if (value.startsWith("data:")) {
      data.push(value.slice(5).replace(/^ /, ""));
    } else if (value.startsWith("event:")) {
      eventType = value.slice(6).trim();
    }
  }

  function consume() {
    let boundary: number;
    while ((boundary = buffer.indexOf("\n")) >= 0) {
      line(buffer.slice(0, boundary).replace(/\r$/, ""));
      buffer = buffer.slice(boundary + 1);
    }
  }

  try {
    while (true) {
      const chunk = await reader.read();
      if (chunk.done) break;
      buffer += decoder.decode(chunk.value, { stream: true });
      consume();
    }
    buffer += decoder.decode();
    consume();
    if (buffer) line(buffer.replace(/\r$/, ""));
    dispatch();
    if (!done) throw new Error("El stream terminó sin confirmar [DONE]. La respuesta puede estar incompleta.");
    return { content, conversationId, firstTextMs, totalMs: Math.round(performance.now() - started) };
  } catch (error) {
    await reader.cancel().catch(() => {});
    throw error;
  } finally {
    reader.releaseLock();
  }
}
