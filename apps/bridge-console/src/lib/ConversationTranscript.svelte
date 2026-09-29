<script lang="ts">
  import {
    readConversationEvents,
    type TimelineMessage,
  } from "./conversationStream";

  let {
    apiKey,
    baseUrl,
    conversationId,
  }: {
    apiKey: string;
    baseUrl: string;
    conversationId: string;
  } = $props();

  const UUID =
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  let lookupId = $state("");
  let lastVoiceId = $state("");
  let messages = $state<TimelineMessage[]>([]);
  let untranscribedAudio = $state(0);
  let account = $state("");
  let updatedAt = $state("");
  let error = $state("");
  let loading = $state(false);
  let streamState = $state("");
  let refreshNow = $state(0);
  let displayedId = "";
  let requestedRefresh = 0;

  $effect(() => {
    const id = conversationId.trim();
    if (id && id !== lastVoiceId) {
      lastVoiceId = id;
      lookupId = id;
    }
  });

  $effect(() => {
    const id = lookupId.trim();
    const url = baseUrl.replace(/\/+$/, "");
    const key = apiKey;
    const refreshRequested = refreshNow !== requestedRefresh;
    requestedRefresh = refreshNow;
    if (id !== displayedId) {
      displayedId = id;
      messages = [];
      untranscribedAudio = 0;
      account = "";
      updatedAt = "";
      error = "";
      streamState = "";
    }
    if (!UUID.test(id)) {
      messages = [];
      untranscribedAudio = 0;
      updatedAt = "";
      error = id ? "Introduce un UUID de conversación válido." : "";
      return;
    }
    let cancelled = false;
    const controller = new AbortController();
    let retryTimer: ReturnType<typeof setTimeout> | undefined;
    async function connect() {
      loading = true;
      streamState = "Conectando el flujo de texto…";
      let retry = 0;
      let refresh = refreshRequested;
      while (!cancelled) {
        try {
          const response = await fetch(
            `${url}/chatgpt/conversations/${encodeURIComponent(id)}/events${refresh ? "?refresh=1" : ""}`,
            {
              headers: key ? { Authorization: `Bearer ${key}` } : {},
              cache: "no-store",
              signal: controller.signal,
            },
          );
          if (!response.ok) {
            const data = await response.json();
            if (response.status === 429) {
              error =
                "ChatGPT limitó la carga del historial. Pulsa Actualizar dentro de un minuto.";
              streamState = "Historial pendiente.";
              loading = false;
              return;
            }
            if ([400, 401, 404].includes(response.status)) {
              error = data.error?.message || `HTTP ${response.status}`;
              streamState = "Flujo de texto no disponible.";
              loading = false;
              return;
            }
            throw new Error(data.error?.message || `HTTP ${response.status}`);
          }
          if (cancelled) return;
          refresh = false;
          retry = 0;
          loading = false;
          error = "";
          streamState = "Texto por eventos · conectado.";
          await readConversationEvents(response, (event) => {
            if (cancelled || event.conversation_id !== id) return;
            if (event.type === "snapshot") {
              messages = event.messages || [];
              account = event.account || "";
              untranscribedAudio = event.untranscribed_audio_messages || 0;
              error = event.history_warning || "";
            } else if (event.type === "message" && event.message) {
              const message = event.message;
              const index = messages.findIndex(
                (item) => item.id === message.id,
              );
              messages =
                index < 0
                  ? [...messages, message]
                  : messages.map((item) =>
                      item.id === message.id ? message : item,
                    );
              messages = [...messages].sort((a, b) =>
                (a.created_at || "").localeCompare(b.created_at || ""),
              );
            }
            updatedAt = new Date().toLocaleTimeString("es-ES");
          });
        } catch (cause) {
          if (!cancelled) {
            error = cause instanceof Error ? cause.message : String(cause);
          }
        }
        if (cancelled) return;
        loading = false;
        streamState = "Reconectando el flujo de texto…";
        await new Promise<void>((resolve) => {
          const finish = () => {
            if (retryTimer) clearTimeout(retryTimer);
            controller.signal.removeEventListener("abort", finish);
            resolve();
          };
          retryTimer = setTimeout(finish, Math.min(1000 * 2 ** retry++, 15000));
          controller.signal.addEventListener("abort", finish, { once: true });
        });
      }
    }
    void connect();
    return () => {
      cancelled = true;
      controller.abort();
      if (retryTimer) clearTimeout(retryTimer);
    };
  });

  function formatDate(value: string | null): string {
    if (!value) return "Hora no disponible";
    const date = new Date(value);
    return Number.isNaN(date.getTime())
      ? "Hora no disponible"
      : new Intl.DateTimeFormat("es-ES", {
          dateStyle: "medium",
          timeStyle: "medium",
        }).format(date);
  }
</script>

<article
  class="rounded-[2rem] border border-white/10 bg-slate-900/80 p-5 xl:col-span-2"
>
  <div class="flex flex-wrap items-start justify-between gap-3">
    <div>
      <p class="text-xs font-black uppercase tracking-[0.2em] text-sky-300">
        Conversación
      </p>
      <h2 class="mt-1 text-xl font-black text-white">
        Mensajes de voz y texto
      </h2>
      <p class="mt-2 text-sm text-slate-400">
        Consulta el hilo por UUID. Durante llamadas del wrapper, el texto llega
        por eventos de WebRTC o SIP, sin consultas periódicas a ChatGPT.
      </p>
    </div>
    <button
      class="rounded-xl border border-white/15 px-4 py-2 text-sm font-bold text-slate-100"
      onclick={() => refreshNow++}
      disabled={!UUID.test(lookupId.trim()) || loading}>Actualizar</button
    >
  </div>

  <label
    class="mt-5 block text-sm font-bold text-slate-300"
    for="transcript-conversation-id"
  >
    UUID de conversación WebRTC o SIP/RTP
  </label>
  <input
    id="transcript-conversation-id"
    class="mt-2 w-full rounded-xl border border-white/10 bg-slate-950 px-3 py-3 font-mono text-sm text-white outline-none focus:border-sky-300/60"
    bind:value={lookupId}
    placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
    spellcheck="false"
  />
  {#if streamState}
    <p class="mt-2 text-xs text-sky-200" role="status">{streamState}</p>
  {/if}
  {#if updatedAt}
    <p class="mt-2 text-xs text-slate-400">
      Cuenta: {account} · Actualizado: {updatedAt}
    </p>
  {/if}
  {#if error}
    <p
      class="mt-3 rounded-xl border border-amber-400/20 bg-amber-400/10 p-3 text-sm text-amber-100"
      role="status"
    >
      {error}
    </p>
  {/if}
  {#if untranscribedAudio}
    <p
      class="mt-3 rounded-xl border border-amber-400/20 bg-amber-400/10 p-3 text-sm text-amber-100"
    >
      {untranscribedAudio} mensaje(s) de audio todavía no tienen texto en el historial
      de ChatGPT.
    </p>
  {/if}
  {#if messages.length}
    <ol
      class="mt-5 grid max-h-[36rem] gap-3 overflow-y-auto pr-1"
      aria-label="Mensajes de la conversación"
    >
      {#each messages as message (message.id)}
        <li
          class={message.role === "user"
            ? "rounded-2xl border border-sky-300/20 bg-sky-300/5 p-4"
            : "rounded-2xl border border-white/10 bg-slate-950/70 p-4"}
        >
          <div class="flex flex-wrap items-center justify-between gap-2">
            <strong
              class={message.role === "user"
                ? "text-sky-200"
                : "text-emerald-200"}
            >
              {message.role === "user" ? "Tú" : "ChatGPT"}
            </strong>
            <time
              datetime={message.created_at || undefined}
              class="text-xs text-slate-400"
              >{formatDate(message.created_at)}</time
            >
          </div>
          <p
            class="mt-2 whitespace-pre-wrap break-words text-sm leading-relaxed text-slate-100"
          >
            {message.text}
          </p>
          {#if message.status && message.status !== "finished_successfully"}
            <p class="mt-2 text-xs text-slate-400">{message.status}</p>
          {/if}
        </li>
      {/each}
    </ol>
  {:else if UUID.test(lookupId.trim()) && !loading && !error}
    <p class="mt-4 text-sm text-slate-400">
      Aún no hay mensajes con texto en este hilo.
    </p>
  {:else if !lookupId.trim()}
    <p class="mt-4 text-sm text-slate-400">
      Inicia una conversación de voz o pega aquí su UUID para ver los mensajes.
    </p>
  {/if}
</article>
