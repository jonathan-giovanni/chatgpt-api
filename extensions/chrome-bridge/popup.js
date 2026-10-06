import { bridgeUrl, discover, beginPair, finishPair, securePost, syncOnce } from "./bridge.js";

const ip = document.querySelector("#ip");
const status = document.querySelector("#status");
let polling;

function show(message, kind = "") {
  status.textContent = message;
  status.className = kind;
}

async function refresh() {
  const state = await chrome.storage.local.get([
    "bridgeBase", "bridgeAccount", "bridgeToken", "bridgeFingerprint", "pairCode", "lastError", "lastSyncAt",
  ]);
  if (!state.bridgeBase) return show("Bridge aún no detectado.");
  if (state.pairCode && !state.bridgeToken) {
    show(`Pendiente de aprobación. Código: ${state.pairCode}\nHuella: ${state.bridgeFingerprint.slice(0, 16)}…`);
    return;
  }
  if (!state.bridgeToken) return show("Bridge detectado. Pulsa conectar para emparejar.");
  try {
    const result = await securePost("health");
    const expiry = result.token_expires_at ? new Date(result.token_expires_at).toLocaleString() : "desconocido";
    show(`Conectado: ${state.bridgeAccount}\nEstado: ${result.state}\nToken hasta: ${expiry}\nÚltima renovación: ${state.lastSyncAt ? new Date(state.lastSyncAt).toLocaleString() : "ninguna"}${state.lastError ? `\nAviso: ${state.lastError}` : ""}`,
      result.state === "ok" ? "ok" : "error");
  } catch (error) {
    show(`Bridge no disponible: ${error.message}`, "error");
  }
}

async function pollPair() {
  try {
    const result = await finishPair();
    if (result.state === "approved") {
      clearInterval(polling);
      show(`Emparejado con ${result.account}. Renovando sesión…`);
      await syncOnce();
      await refresh();
    }
  } catch (error) {
    clearInterval(polling);
    show(error.message, "error");
  }
}

document.querySelector("#connect").addEventListener("click", async () => {
  try {
    show("Buscando bridge…");
    const address = ip.value.trim();
    if (address && !["localhost", "127.0.0.1"].includes(address)) {
      const origin = bridgeUrl(address).replace(":8000", "") + "/*";
      if (!await chrome.permissions.request({ origins: [origin] })) throw new Error("Chrome necesita permiso para esa IP.");
    }
    const found = await discover(address);
    const prior = await chrome.storage.local.get(["bridgeFingerprint", "bridgeBase", "bridgeToken"]);
    if (prior.bridgeToken && (prior.bridgeFingerprint !== found.fingerprint || prior.bridgeBase !== found.base)) {
      throw new Error("La IP apunta a otro bridge. Revoca el emparejamiento anterior en Accounts antes de cambiar.");
    }
    if (prior.bridgeToken) return refresh();
    const pair = await beginPair(found);
    show(`Bridge: ${found.base}\nCódigo: ${pair.code}\nHuella: ${found.fingerprint.slice(0, 16)}…\nApruébalo en Accounts.`);
    clearInterval(polling);
    polling = setInterval(pollPair, 3000);
  } catch (error) {
    show(error.message, "error");
  }
});

document.querySelector("#refresh").addEventListener("click", async () => {
  try {
    show("Comprobando sesión y renovando…");
    await syncOnce();
    await refresh();
  } catch (error) {
    show(error.message, "error");
  }
});

chrome.storage.local.get(["bridgeBase", "pairRequestId"]).then((state) => {
  if (state.bridgeBase) ip.value = new URL(state.bridgeBase).hostname === "127.0.0.1" ? "" : new URL(state.bridgeBase).hostname;
  void refresh();
  if (state.pairRequestId) polling = setInterval(pollPair, 3000);
});
