export type ConversationEvent = {
  id: number;
  type: "snapshot" | "message";
  conversation_id: string;
  account?: string;
  messages?: TimelineMessage[];
  message?: TimelineMessage;
  untranscribed_audio_messages?: number;
  history_warning?: string | null;
};

export type TimelineMessage = {
  id: string;
  role: "user" | "assistant";
  text: string;
  created_at: string | null;
  status: string | null;
};

export async function readConversationEvents(
  response: Response,
  receive: (event: ConversationEvent) => void,
): Promise<void> {
  if (!response.body)
    throw new Error("El navegador no permite leer el stream.");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
      let boundary: number;
      while ((boundary = buffer.indexOf("\n\n")) >= 0) {
        const frame = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const data = frame
          .split("\n")
          .filter((line) => line.startsWith("data:"))
          .map((line) => line.slice(5).trimStart())
          .join("\n");
        if (data) receive(JSON.parse(data) as ConversationEvent);
      }
    }
  } finally {
    reader.releaseLock();
  }
}

/** Relay only actual voice events. Retries reuse a sequence to avoid double appends. */
export class VoiceEventRelay {
  private queue: Record<string, unknown>[] = [];
  private sequence = 0;
  private running: Promise<void> | null = null;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private closed = false;
  private controller = new AbortController();

  constructor(
    private url: string,
    private apiKey: string,
    private sessionId: string,
    private conversation: (id: string) => void,
    private state: (error: string) => void,
  ) {}

  enqueue(raw: unknown): void {
    if (this.closed || typeof raw !== "string") return;
    let event;
    try {
      event = JSON.parse(raw);
      if (event.type === "data_message")
        event =
          typeof event.data === "string" ? JSON.parse(event.data) : event.data;
    } catch {
      return;
    }
    if (
      !event ||
      ![
        "startup_telemetry",
        "conversation_update",
        "chat_message_delta",
      ].includes(event.type)
    )
      return;
    if (this.queue.length >= 1024) {
      this.state(
        "El flujo de texto perdió eventos. Usa Actualizar para recuperar el historial.",
      );
      this.closed = true;
      this.controller.abort();
      return;
    }
    this.queue.push(event);
    if (!this.running && !this.timer) {
      this.timer = setTimeout(() => {
        this.timer = null;
        this.startDrain();
      }, 100);
    }
  }

  private startDrain(): void {
    this.running = this.drain().finally(() => {
      this.running = null;
      if (this.queue.length && !this.closed) this.startDrain();
    });
  }

  private async drain(): Promise<void> {
    while (this.queue.length) {
      const batch = this.queue.splice(0, 50);
      const body = JSON.stringify({ sequence: this.sequence, events: batch });
      let attempt = 0;
      while (true) {
        try {
          const response = await fetch(
            `${this.url}/chatgpt/voice/sessions/${this.sessionId}/events`,
            {
              method: "POST",
              headers: {
                Authorization: `Bearer ${this.apiKey}`,
                "Content-Type": "application/json",
              },
              body,
              signal: AbortSignal.any([
                this.controller.signal,
                AbortSignal.timeout(5000),
              ]),
            },
          );
          if (!response.ok) {
            if ([400, 401, 404].includes(response.status)) {
              this.state(
                "No se pudo publicar el texto de esta sesión. Usa Actualizar para consultar el historial.",
              );
              this.queue = [];
              this.closed = true;
              return;
            }
            throw new Error(`HTTP ${response.status}`);
          }
          const data = await response.json();
          this.sequence++;
          this.state("");
          if (typeof data.conversation_id === "string")
            this.conversation(data.conversation_id);
          break;
        } catch {
          if (this.controller.signal.aborted) return;
          this.state(
            "Reconectando el flujo de texto; el audio sigue conectado.",
          );
          await new Promise((resolve) =>
            setTimeout(resolve, Math.min(1000 * 2 ** attempt++, 10000)),
          );
          if (this.controller.signal.aborted) return;
        }
      }
    }
  }

  async close(): Promise<void> {
    this.closed = true;
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    if (!this.running && this.queue.length) this.startDrain();
    let timeout: ReturnType<typeof setTimeout> | undefined;
    await Promise.race([
      this.running,
      new Promise((resolve) => {
        timeout = setTimeout(resolve, 3000);
      }),
    ]);
    if (timeout) clearTimeout(timeout);
    this.controller.abort();
    this.queue = [];
  }
}
