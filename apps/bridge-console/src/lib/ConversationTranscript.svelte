<script lang="ts">
  type TimelineMessage = {
    id: string;
    role: "user" | "assistant";
    text: string;
    created_at: string | null;
    status: string | null;
  };

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
  let refreshNow = $state(0);
  let displayedId = "";

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
    void refreshNow;
    if (id !== displayedId) {
      displayedId = id;
      messages = [];
      untranscribedAudio = 0;
      account = "";
      updatedAt = "";
      error = "";
    }
    if (!UUID.test(id)) {
      messages = [];
      untranscribedAudio = 0;
      updatedAt = "";
      error = id ? "Introduce un UUID de conversación válido." : "";
      return;
    }
    let cancelled = false;
    let pending = false;
    let nextRequestAt = 0;
    const controller = new AbortController();
    async function refresh() {
      if (pending || cancelled || Date.now() < nextRequestAt) return;
      pending = true;
      loading = true;
      try {
        const response = await fetch(
          `${url}/chatgpt/conversations/${encodeURIComponent(id)}/messages`,
          {
            headers: key ? { Authorization: `Bearer ${key}` } : {},
            cache: "no-store",
            signal: controller.signal,
          },
        );
        const data = await response.json();
        if (!response.ok) {
          if (response.status === 429) {
            nextRequestAt = Date.now() + 60_000;
            throw new Error(
              "ChatGPT ha limitado las consultas. Se reintentará en un minuto.",
            );
          }
          throw new Error(data.error?.message || `HTTP ${response.status}`);
        }
        if (cancelled) return;
        nextRequestAt = 0;
        messages = Array.isArray(data.messages) ? data.messages : [];
        untranscribedAudio = Number(data.untranscribed_audio_messages) || 0;
        account = String(data.account || "");
        updatedAt = new Date().toLocaleTimeString("es-ES");
        error = "";
      } catch (cause) {
        if (!cancelled) {
          nextRequestAt = Math.max(nextRequestAt, Date.now() + 15_000);
          error = cause instanceof Error ? cause.message : String(cause);
        }
      } finally {
        pending = false;
        if (!cancelled) loading = false;
      }
    }
    void refresh();
    const timer = setInterval(() => {
      if (!document.hidden) void refresh();
    }, 4000);
    return () => {
      cancelled = true;
      controller.abort();
      clearInterval(timer);
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
        Consulta el hilo de ChatGPT por UUID. La lista se actualiza cada cuatro
        segundos mientras esta página está abierta.
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
