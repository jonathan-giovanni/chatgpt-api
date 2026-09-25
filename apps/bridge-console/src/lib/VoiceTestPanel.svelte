<script lang="ts">
  import { onDestroy } from "svelte";

  type TextAttachment = { filename: string; file_data: string };
  type VoiceResponse = {
    error?: { message?: string };
    answer_sdp?: string;
    bridge_session_id?: string;
    conversation_id?: string | null;
    account?: string;
    initial_response?: string;
  };

  let {
    apiKey,
    baseUrl,
    projectAlias,
    projectName,
    model,
    conversationId,
    initialText,
    initialFiles,
    onConversationChange,
  }: {
    apiKey: string;
    baseUrl: string;
    projectAlias: string;
    projectName: string;
    model: string;
    conversationId: string;
    initialText: string;
    initialFiles: File[];
    onConversationChange: (id: string) => void;
  } = $props();

  const AUDIO_SIZE_LIMIT = 20 * 1024 * 1024;
  const TEXT_EXTENSIONS = /\.(txt|md|csv|json)$/i;
  const voices = [
    "cove",
    "breeze",
    "ember",
    "fathom",
    "glimmer",
    "juniper",
    "maple",
    "orbit",
    "vale",
  ];

  let voice = $state("cove");
  let sourceMode = $state<"file" | "mic">("file");
  let audioFile = $state<File | null>(null);
  let status = $state("Desconectado.");
  let busy = $state(false);
  let active = $state(false);
  let followup = $state("");
  let followupFiles = $state<File[]>([]);
  let followupFileError = $state("");
  let messages = $state<string[]>([]);
  let remoteAudio: HTMLAudioElement;

  let peer: RTCPeerConnection | null = null;
  let dataChannel: RTCDataChannel | null = null;
  let inputStream: MediaStream | null = null;
  let audioContext: AudioContext | null = null;
  let decodedAudio: AudioBuffer | null = null;
  let audioDestination: MediaStreamAudioDestinationNode | null = null;
  let fileStarted = false;
  let bridgeSessionId: string | null = null;
  let generation = 0;
  let retries = 0;
  let retryTimer: ReturnType<typeof setTimeout> | null = null;
  let disconnectedTimer: ReturnType<typeof setTimeout> | null = null;
  let requestOptions: Record<string, unknown> = {};

  function append(message: string) {
    messages = [...messages.slice(-29), message];
  }

  function powerShellQuote(value: string) {
    return `'${value.replace(/'/g, "''")}'`;
  }

  const sipGatewayCommand = $derived(
    [
      "uv run --extra sip chatgpt-sip --listen-ip 127.0.0.1 --sip-port 5066 --rtp-port 40006",
      projectAlias ? `--project ${powerShellQuote(projectAlias)}` : "",
      model.trim() ? `--model ${powerShellQuote(model.trim())}` : "",
      conversationId.trim()
        ? `--conversation-id ${powerShellQuote(conversationId.trim())}`
        : "",
      initialText.trim() ? `--text ${powerShellQuote(initialText.trim())}` : "",
    ]
      .filter(Boolean)
      .join(" "),
  );

  async function readAttachments(files: File[]): Promise<TextAttachment[]> {
    if (files.some((file) => !TEXT_EXTENSIONS.test(file.name))) {
      throw new Error(
        "En Voz solo se adjuntan TXT, MD, CSV o JSON; usa el campo de audio para WAV o MP3.",
      );
    }
    if (
      files.length > 10 ||
      files.some((file) => file.size > AUDIO_SIZE_LIMIT) ||
      files.reduce((sum, file) => sum + file.size, 0) > 25 * 1024 * 1024
    ) {
      throw new Error(
        "Adjuntos: máximo 10 archivos, 20 MiB por archivo y 25 MiB en total.",
      );
    }
    return Promise.all(
      files.map(
        (file) =>
          new Promise<TextAttachment>((resolve, reject) => {
            const reader = new FileReader();
            reader.onload = () =>
              resolve({
                filename: file.name,
                file_data: String(reader.result).split(",")[1],
              });
            reader.onerror = () =>
              reject(new Error(`No se pudo leer ${file.name}`));
            reader.readAsDataURL(file);
          }),
      ),
    );
  }

  async function prepareInput() {
    if (sourceMode === "mic") {
      inputStream = await navigator.mediaDevices.getUserMedia({
        audio: true,
        video: false,
      });
      return;
    }
    if (!audioFile) throw new Error("Selecciona un archivo de audio.");
    if (!audioFile.size || audioFile.size > AUDIO_SIZE_LIMIT)
      throw new Error("El audio debe ocupar entre 1 byte y 20 MiB.");
    audioContext = new AudioContext();
    await audioContext.resume();
    decodedAudio = await audioContext.decodeAudioData(
      await audioFile.arrayBuffer(),
    );
    if (decodedAudio.duration > 300)
      throw new Error("El audio no puede superar cinco minutos.");
    audioDestination = audioContext.createMediaStreamDestination();
    inputStream = audioDestination.stream;
  }

  function startAudioFile() {
    if (
      !decodedAudio ||
      !audioContext ||
      !audioDestination ||
      fileStarted ||
      !active
    )
      return;
    const source = audioContext.createBufferSource();
    source.buffer = decodedAudio;
    source.connect(audioDestination);
    source.onended = () => {
      status = "Archivo terminado. La llamada continúa abierta.";
    };
    source.start();
    fileStarted = true;
    status = "Conectado. Reproduciendo el archivo por WebRTC.";
  }

  function updateConversation(id: unknown) {
    if (typeof id === "string" && id.trim()) onConversationChange(id);
  }

  function handleDataMessage(raw: unknown) {
    if (typeof raw !== "string") return;
    let event: Record<string, any>;
    try {
      event = JSON.parse(raw);
    } catch {
      return;
    }
    if (event.type === "data_message" && typeof event.data === "string") {
      try {
        event = JSON.parse(event.data);
      } catch {
        return;
      }
    }
    const payload =
      event.payload && typeof event.payload === "object"
        ? event.payload
        : event;
    updateConversation(payload.conversation_id);
    const delta = payload.delta || payload.text;
    if (
      (event.type === "chat_message_delta" ||
        payload.type === "chat_message_delta") &&
      typeof delta === "string" &&
      delta.trim()
    ) {
      append(`ChatGPT: ${delta}`);
    }
  }

  async function waitForIce(connection: RTCPeerConnection) {
    if (connection.iceGatheringState === "complete") return;
    await new Promise<void>((resolve, reject) => {
      const timer = setTimeout(() => {
        connection.removeEventListener("icegatheringstatechange", check);
        reject(new Error("Tiempo agotado al reunir candidatos ICE."));
      }, 12000);
      function check() {
        if (connection.iceGatheringState === "complete") {
          clearTimeout(timer);
          connection.removeEventListener("icegatheringstatechange", check);
          resolve();
        }
      }
      connection.addEventListener("icegatheringstatechange", check);
      check();
    });
  }

  function closePeer() {
    if (disconnectedTimer) clearTimeout(disconnectedTimer);
    disconnectedTimer = null;
    dataChannel?.close();
    peer?.close();
    dataChannel = null;
    peer = null;
  }

  function scheduleReconnect(reason: string) {
    if (!active || retryTimer) return;
    if (retries >= 2) {
      status = `Conexión perdida (${reason}). Finaliza y vuelve a conectar.`;
      return;
    }
    retries += 1;
    const currentGeneration = generation;
    status = `Conexión interrumpida. Reconectando ${retries}/2…`;
    retryTimer = setTimeout(async () => {
      retryTimer = null;
      if (!active || currentGeneration !== generation) return;
      closePeer();
      try {
        await connectPeer(currentGeneration);
      } catch (error) {
        scheduleReconnect(
          error instanceof Error ? error.message : String(error),
        );
      }
    }, 1000 * retries);
  }

  async function connectPeer(currentGeneration: number) {
    if (!inputStream || !active || currentGeneration !== generation) return;
    const connection = new RTCPeerConnection({ bundlePolicy: "max-bundle" });
    peer = connection;
    connection.addTrack(inputStream.getAudioTracks()[0], inputStream);
    connection.ontrack = (event) => {
      if (!remoteAudio) return;
      remoteAudio.srcObject =
        event.streams[0] || new MediaStream([event.track]);
      void remoteAudio.play().catch(() => {
        status = "Conectado. Pulsa reproducir para oír la respuesta.";
      });
    };
    connection.onconnectionstatechange = () => {
      if (!active || currentGeneration !== generation) return;
      if (connection.connectionState === "connected") {
        if (disconnectedTimer) clearTimeout(disconnectedTimer);
        disconnectedTimer = null;
        status = "Conectado por WebRTC.";
        startAudioFile();
      } else if (connection.connectionState === "failed") {
        scheduleReconnect("WebRTC");
      } else if (connection.connectionState === "disconnected") {
        disconnectedTimer = setTimeout(() => {
          if (connection.connectionState === "disconnected")
            scheduleReconnect("red");
        }, 4000);
      }
    };

    const channel = connection.createDataChannel("oai-events", {
      negotiated: true,
      id: 0,
    });
    dataChannel = channel;
    let channelOpened = false;
    channel.onmessage = (event) => handleDataMessage(event.data);
    channel.onopen = () => {
      channelOpened = true;
    };
    channel.onclose = () => {
      if (
        active &&
        connection === peer &&
        channelOpened &&
        connection.connectionState !== "closed"
      )
        scheduleReconnect("canal de datos");
    };

    await connection.setLocalDescription(await connection.createOffer());
    await waitForIce(connection);
    const body = bridgeSessionId
      ? {
          offer_sdp: connection.localDescription?.sdp,
          voice,
          bridge_session_id: bridgeSessionId,
        }
      : { ...requestOptions, offer_sdp: connection.localDescription?.sdp };
    const response = await fetch(
      `${baseUrl.replace(/\/+$/, "")}/chatgpt/voice/sessions`,
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${apiKey}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify(body),
      },
    );
    const result = (await response.json()) as VoiceResponse;
    if (!response.ok)
      throw new Error(result.error?.message || `Error HTTP ${response.status}`);
    if (!active || currentGeneration !== generation) {
      connection.close();
      if (result.bridge_session_id)
        void releaseSession(result.bridge_session_id);
      return;
    }
    bridgeSessionId = result.bridge_session_id || bridgeSessionId;
    updateConversation(result.conversation_id);
    if (result.initial_response) append(`ChatGPT: ${result.initial_response}`);
    let answer = result.answer_sdp || "";
    try {
      await connection.setRemoteDescription({ type: "answer", sdp: answer });
    } catch (error) {
      if (!/^a=sctp-init:/m.test(answer)) throw error;
      answer = answer.replace(/^a=sctp-init:[^\r\n]*(?:\r?\n|$)/gm, "");
      await connection.setRemoteDescription({ type: "answer", sdp: answer });
    }
    status = `Negociado con ${result.account || "la cuenta configurada"}; esperando WebRTC…`;
    setTimeout(() => {
      if (
        active &&
        connection === peer &&
        connection.connectionState === "connected" &&
        channel.readyState !== "open"
      ) {
        status =
          "Audio conectado. Este navegador no abrió el canal de datos; usa la continuación HTTP con UUID.";
      }
    }, 8000);
  }

  async function releaseSession(id: string) {
    try {
      await fetch(
        `${baseUrl.replace(/\/+$/, "")}/chatgpt/voice/sessions/release`,
        {
          method: "POST",
          headers: {
            Authorization: `Bearer ${apiKey}`,
            "Content-Type": "application/json",
          },
          body: JSON.stringify({ bridge_session_id: id }),
          keepalive: true,
        },
      );
    } catch {
      /* A lost bridge connection must not block local cleanup. */
    }
  }

  async function stop() {
    active = false;
    busy = false;
    generation += 1;
    if (retryTimer) clearTimeout(retryTimer);
    if (disconnectedTimer) clearTimeout(disconnectedTimer);
    retryTimer = disconnectedTimer = null;
    closePeer();
    inputStream?.getTracks().forEach((track) => track.stop());
    inputStream = null;
    if (audioContext && audioContext.state !== "closed")
      await audioContext.close();
    audioContext = null;
    decodedAudio = null;
    audioDestination = null;
    fileStarted = false;
    if (remoteAudio) remoteAudio.srcObject = null;
    if (bridgeSessionId) void releaseSession(bridgeSessionId);
    bridgeSessionId = null;
    status = "Desconectado.";
  }

  async function start() {
    if (busy || active) return;
    busy = true;
    active = true;
    retries = 0;
    generation += 1;
    const currentGeneration = generation;
    try {
      status = "Preparando audio y contexto…";
      const files = await readAttachments(initialFiles);
      requestOptions = {
        voice,
        model: model.trim() || "auto",
        project: projectAlias || undefined,
        conversation_id: conversationId.trim() || undefined,
        text: initialText.trim() || undefined,
        files: files.length ? files : undefined,
      };
      await prepareInput();
      if (active && currentGeneration === generation)
        await connectPeer(currentGeneration);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      await stop();
      status = `No se pudo iniciar: ${message}`;
    } finally {
      busy = false;
    }
  }

  function selectFollowupFiles(event: Event) {
    const input = event.currentTarget as HTMLInputElement;
    followupFiles = Array.from(input.files || []);
    followupFileError = followupFiles.some(
      (file) => !TEXT_EXTENSIONS.test(file.name),
    )
      ? "Solo TXT, MD, CSV o JSON."
      : followupFiles.length > 10 ||
          followupFiles.some((file) => file.size > AUDIO_SIZE_LIMIT) ||
          followupFiles.reduce((sum, file) => sum + file.size, 0) >
            25 * 1024 * 1024
        ? "Máximo 10 archivos, 20 MiB por archivo y 25 MiB en total."
        : "";
    if (followupFileError) followupFiles = [];
    input.value = "";
  }

  async function sendFollowup(event: SubmitEvent) {
    event.preventDefault();
    const text = followup.trim();
    if (!active || !text || !conversationId.trim()) return;
    busy = true;
    try {
      const files = await readAttachments(followupFiles);
      const content = files.length
        ? [
            { type: "text", text },
            ...files.map((file) => ({ type: "file", file })),
          ]
        : text;
      const response = await fetch(
        `${baseUrl.replace(/\/+$/, "")}/chat/completions`,
        {
          method: "POST",
          headers: {
            Authorization: `Bearer ${apiKey}`,
            "Content-Type": "application/json",
          },
          body: JSON.stringify({
            model: model.trim() || "auto",
            conversation_id: conversationId.trim(),
            temporary_chat: false,
            messages: [{ role: "user", content }],
          }),
        },
      );
      const result = await response.json();
      if (!response.ok)
        throw new Error(
          result.error?.message || `Error HTTP ${response.status}`,
        );
      append(`Tú: ${text}`);
      append(
        `ChatGPT: ${result.choices?.[0]?.message?.content || "(sin texto)"}`,
      );
      followup = "";
      followupFiles = [];
      status = "Respuesta añadida al mismo hilo.";
    } catch (error) {
      status = `No se pudo enviar: ${error instanceof Error ? error.message : String(error)}`;
    } finally {
      busy = false;
    }
  }

  onDestroy(() => {
    void stop();
  });
</script>

<article class="rounded-[2rem] border border-cyan-300/20 bg-[#071018]/95 p-5">
  <div class="flex flex-wrap items-start justify-between gap-3">
    <div>
      <p class="text-xs font-black uppercase tracking-[0.18em] text-cyan-200">
        webrtc · voz
      </p>
      <h3 class="mt-1 text-xl font-black text-white">
        Voz en esta conversación
      </h3>
      <p class="mt-2 text-sm text-slate-400">
        Usa directamente el proyecto, modelo, UUID, mensaje inicial y adjuntos
        de texto del panel Chat.
      </p>
    </div>
    <span
      class="rounded-full border border-cyan-300/20 bg-cyan-300/10 px-3 py-1 text-xs text-cyan-100"
      >ChatGPT decide el modelo de voz</span
    >
  </div>

  <div class="mt-4 grid gap-2 text-sm sm:grid-cols-3">
    <div class="rounded-xl border border-white/10 bg-black/20 p-3">
      <span class="text-slate-500">Proyecto</span>
      <div class="mt-1 font-bold text-slate-200">
        {projectAlias ? projectName || projectAlias : "Fuera de proyectos"}
      </div>
    </div>
    <div class="rounded-xl border border-white/10 bg-black/20 p-3">
      <span class="text-slate-500">Modelo del mensaje inicial</span>
      <div class="mt-1 font-bold text-slate-200">{model || "auto"}</div>
    </div>
    <div class="rounded-xl border border-white/10 bg-black/20 p-3">
      <span class="text-slate-500">Conversación</span>
      <div class="mt-1 break-all font-mono text-xs text-slate-200">
        {conversationId.trim() || "Nueva; se guarda el UUID al recibirlo"}
      </div>
    </div>
  </div>

  <div class="mt-4 grid gap-4 md:grid-cols-2">
    <label class="block text-sm font-bold text-slate-300"
      >Origen de audio
      <select
        class="mt-2 w-full rounded-xl border border-white/10 bg-slate-950 px-3 py-3"
        bind:value={sourceMode}
        disabled={active}
      >
        <option value="file">Archivo de audio</option><option value="mic"
          >Micrófono</option
        >
      </select>
    </label>
    <label class="block text-sm font-bold text-slate-300"
      >Voz
      <select
        class="mt-2 w-full rounded-xl border border-white/10 bg-slate-950 px-3 py-3"
        bind:value={voice}
        disabled={active}
      >
        {#each voices as option}<option value={option}>{option}</option>{/each}
      </select>
    </label>
  </div>
  {#if sourceMode === "file"}
    <label class="mt-4 block text-sm font-bold text-slate-300"
      >Audio local (WAV, MP3 u otro formato compatible; hasta 20 MiB y 5
      minutos)
      <input
        class="mt-2 block w-full text-sm"
        type="file"
        accept="audio/*,.wav,.mp3,.ogg,.m4a"
        disabled={active}
        onchange={(event) =>
          (audioFile =
            (event.currentTarget as HTMLInputElement).files?.[0] ?? null)}
      />
    </label>
  {:else}
    <p
      class="mt-4 rounded-xl border border-amber-300/15 bg-amber-300/5 p-3 text-sm text-amber-100"
    >
      El navegador pedirá permiso para usar el micrófono. Para probar otra
      fuente RTP, usa las instrucciones SIP/RTP de abajo.
    </p>
  {/if}
  <p class="mt-3 text-xs text-slate-400">
    El mensaje y los adjuntos de texto configurados arriba se envían al mismo
    hilo antes de iniciar voz. El archivo de audio se reproduce como entrada
    WebRTC; no se sube como adjunto.
  </p>

  <div class="mt-4 flex flex-wrap gap-2">
    <button
      class="rounded-xl bg-cyan-300 px-4 py-3 font-black text-slate-950"
      onclick={start}
      disabled={busy || active || (sourceMode === "file" && !audioFile)}
      >{busy ? "Conectando…" : "Iniciar voz"}</button
    >
    <button
      class="rounded-xl border border-white/15 px-4 py-3 font-black text-slate-100"
      onclick={stop}
      disabled={!active && !busy}>Finalizar</button
    >
  </div>
  <p
    class="mt-3 rounded-xl bg-black/25 p-3 text-sm text-slate-200"
    role="status"
    aria-live="polite"
  >
    {status}
  </p>
  <audio bind:this={remoteAudio} class="mt-3 w-full" autoplay controls></audio>
  {#if messages.length}<div
      class="mt-3 max-h-36 overflow-auto rounded-xl border border-white/10 bg-black/25 p-3 text-sm text-slate-300"
      aria-live="polite"
    >
      {#each messages as message}<p>{message}</p>{/each}
    </div>{/if}

  {#if active && conversationId.trim()}
    <form class="mt-4 grid gap-2" onsubmit={sendFollowup}>
      <label class="text-sm font-bold text-slate-300" for="voice-followup"
        >Continuar en este mismo hilo</label
      >
      <textarea
        id="voice-followup"
        class="min-h-20 rounded-xl border border-white/10 bg-slate-950 p-3 text-sm"
        bind:value={followup}
        placeholder="Escribe el siguiente mensaje"
        disabled={busy}></textarea>
      <div class="flex flex-wrap items-center gap-3">
        <input
          type="file"
          multiple
          accept=".txt,.md,.csv,.json"
          onchange={selectFollowupFiles}
          disabled={busy}
          aria-label="Adjuntar archivos de texto a la continuación"
        />
        <button
          class="rounded-xl border border-cyan-300/30 bg-cyan-300/10 px-4 py-2 font-bold text-cyan-100"
          disabled={busy || !followup.trim() || Boolean(followupFileError)}
          >Enviar al mismo hilo</button
        >
      </div>
      {#if followupFileError}<p role="alert" class="text-sm text-rose-300">
          {followupFileError}
        </p>{/if}
      {#if followupFiles.length}<p class="text-xs text-slate-400">
          Adjuntos: {followupFiles.map((file) => file.name).join(", ")}
        </p>{/if}
    </form>
  {/if}

  <details class="mt-5 rounded-2xl border border-white/10 bg-black/20 p-4">
    <summary class="cursor-pointer font-bold text-slate-100"
      >Probar SIP/RTP desde el wrapper</summary
    >
    <p class="mt-3 text-sm text-slate-400">
      SIP usa UDP y no puede iniciarse desde una página web. Ejecuta el gateway
      y el cliente de prueba en dos terminales. Requiere una cuenta ChatGPT
      configurada y el puente activo.
    </p>
    <p class="mt-3 text-xs font-black uppercase tracking-wider text-slate-400">
      Terminal A · gateway local
    </p>
    <pre
      class="mt-2 overflow-auto whitespace-pre-wrap break-words rounded-xl bg-slate-950 p-3 text-xs text-cyan-100">$env:CHATGPT_API_KEY = "local-dev-key"
{sipGatewayCommand}</pre>
    <p class="mt-3 text-xs font-black uppercase tracking-wider text-slate-400">
      Terminal B · cliente de prueba (en la raíz del repositorio)
    </p>
    <pre
      class="mt-2 overflow-auto whitespace-pre-wrap break-words rounded-xl bg-slate-950 p-3 text-xs text-cyan-100">uv run --extra sip python scripts/sip_rtp_smoke.py --wav "C:\ruta\audio.wav"</pre>
    <p class="mt-3 text-xs text-slate-400">
      El gateway hereda el proyecto, modelo, UUID y texto seleccionados arriba;
      el modelo de voz sigue siendo automático. Para archivos en SIP, añade
      --attachment con su ruta local al comando. Usa WAV PCM hablado corto. Se
      espera INVITE 200, RTP de retorno con pico PCM mayor que cero y BYE 200.
      Sin --wav se envía un tono y se comprueba solo el transporte (puede
      retornar silencio). Ctrl+C detiene el gateway; ambos servicios se limitan
      a loopback.
    </p>
  </details>
</article>
