import { bridgeUrl, discover, activate, securePost, syncOnce } from "./bridge.js";

const $ = (selector) => document.querySelector(selector);
const ui = {
  ip: $("#ip"), connect: $("#connect"), refresh: $("#refresh"),
  orb: $("#orb"), toggle: $("#auto-toggle"), change: $("#change-account"),
  form: $("#connect-form"), message: $("#message"), account: $("#account"),
  server: $("#server"), expiry: $("#expiry"), lastSync: $("#last-sync"),
  stateLabel: $("#state-label"), headline: $("#headline"), subline: $("#subline"),
  autoDone: $("#auto-done"),
};

const COPY = {
  ok: ["Conectado", "Todo en sincronía", "Tu cuenta está lista para conversar."],
  expiring: ["Por renovar", "Se acerca el vencimiento", "La renovación automática actuará pronto."],
  expired: ["Token vencido", "Hay que renovar", "Abre ChatGPT en Chrome y renueva la sesión."],
  revoked: ["Sesión interrumpida", "Vuelve a iniciar sesión", "Tu acceso a ChatGPT necesita atención."],
  missing_capture: ["Sin captura", "Falta la cuenta", "Registra primero la cuenta en Bridge Console."],
  unknown: ["Comprobando", "Analizando sesión", "La fecha de vencimiento no está disponible."],
  error: ["Atención", "Revisa la conexión", "No se pudo completar la comprobación."],
  connecting: ["Conectando", "Buscando tu bridge", "Tu sesión segura, siempre a punto."],
  paused: ["En pausa", "Renovación pausada", "La activas de nuevo tocando el pulso."],
};

function setVisual(state) {
  const [label, headline, subline] = COPY[state] || COPY.error;
  document.body.dataset.state = state;
  ui.stateLabel.textContent = label;
  ui.headline.textContent = headline;
  ui.subline.textContent = subline;
}

function message(text = "", error = false) {
  ui.message.textContent = text;
  ui.message.classList.toggle("error", error);
}

function niceDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString();
}

async function render() {
  const state = await chrome.storage.local.get([
    "bridgeBase", "bridgeToken", "bridgeAccount", "autoRenew", "lastSyncAt",
    "lastAutomaticRenewal", "lastError", "health",
  ]);
  const enabled = state.autoRenew !== false;
  ui.toggle.setAttribute("aria-checked", String(enabled));
  ui.account.textContent = state.bridgeAccount || "Sin vincular";
  ui.server.textContent = state.bridgeBase ? new URL(state.bridgeBase).host : "Sin detectar";
  ui.lastSync.textContent = state.lastSyncAt ? niceDate(state.lastSyncAt) : "—";
  ui.autoDone.hidden = !state.lastAutomaticRenewal || !enabled;
  ui.form.hidden = Boolean(state.bridgeToken);
  ui.connect.hidden = Boolean(state.bridgeToken);
  ui.refresh.hidden = !state.bridgeToken;
  ui.change.hidden = !state.bridgeToken;
  if (!state.bridgeToken) {
    setVisual(enabled ? "connecting" : "paused");
    ui.expiry.textContent = "—";
    return;
  }
  try {
    const health = await securePost("health");
    ui.expiry.textContent = niceDate(health.token_expires_at);
    setVisual(enabled ? health.state : "paused");
    message(state.lastError || "", Boolean(state.lastError));
  } catch (error) {
    setVisual(enabled ? "error" : "paused");
    message(error.message, true);
  }
}

async function connect() {
  ui.connect.disabled = true;
  setVisual("connecting");
  message("Buscando primero en localhost…");
  try {
    const address = ui.ip.value.trim();
    if (address && !["localhost", "127.0.0.1"].includes(address)) {
      const origin = bridgeUrl(address).replace(":8000", "") + "/*";
      if (!await chrome.permissions.request({ origins: [origin] })) {
        throw new Error("Chrome necesita acceso a esa IP para conectar.");
      }
    }
    const found = await discover(address);
    message(`Bridge encontrado en ${found.base}. Verificando tu sesión de Chrome…`);
    const prior = await chrome.storage.local.get(["bridgeToken", "bridgeFingerprint"]);
    if (prior.bridgeFingerprint && prior.bridgeFingerprint !== found.fingerprint) {
      throw new Error("La identidad del bridge cambió. Revisa la IP antes de reconectar.");
    }
    if (!prior.bridgeToken) await activate(found);
    await chrome.runtime.sendMessage({ type: "check-now" });
    await render();
    message("Conexión y cuenta verificadas.");
  } catch (error) {
    setVisual("error");
    message(error.message, true);
  } finally {
    ui.connect.disabled = false;
  }
}

async function renew() {
  ui.refresh.disabled = true;
  setVisual("connecting");
  message("Renovando desde tu sesión de ChatGPT…");
  try {
    await syncOnce();
    await securePost("report", { state: "ok" });
    await chrome.runtime.sendMessage({ type: "check-now" });
    await render();
    message("Sesión renovada correctamente.");
  } catch (error) {
    setVisual("error");
    message(error.message, true);
  } finally {
    ui.refresh.disabled = false;
  }
}

async function toggleAuto() {
  const state = await chrome.storage.local.get("autoRenew");
  const enabled = state.autoRenew === false;
  await chrome.storage.local.set({ autoRenew: enabled });
  await render();
  message(enabled ? "Renovación automática activada." : "Renovación automática en pausa.");
}

ui.connect.addEventListener("click", connect);
ui.refresh.addEventListener("click", renew);
ui.orb.addEventListener("click", toggleAuto);
ui.toggle.addEventListener("click", toggleAuto);
ui.change.addEventListener("click", async () => {
  await chrome.storage.local.remove(["bridgeToken", "bridgeAccount", "lastSyncAt", "lastAutomaticRenewal"]);
  message("Abre la cuenta deseada en ChatGPT y vuelve a conectar.");
  await render();
});

chrome.storage.local.get(["bridgeBase"]).then(async (state) => {
  if (state.bridgeBase) {
    const host = new URL(state.bridgeBase).hostname;
    ui.ip.value = host === "127.0.0.1" || host === "localhost" ? "" : host;
  }
  await render();
});
