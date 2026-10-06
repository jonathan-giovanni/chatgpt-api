const enc = new TextEncoder();
const dec = new TextDecoder();

export function bytes64(bytes) {
  let text = "";
  for (let i = 0; i < bytes.length; i += 0x8000) {
    text += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  }
  return btoa(text);
}

export function from64(text) {
  return Uint8Array.from(atob(text), (char) => char.charCodeAt(0));
}

export function bridgeUrl(ip = "") {
  const host = ip.trim() || "127.0.0.1";
  if (host !== "localhost" && host !== "127.0.0.1" &&
      !/^(?:(?:10|127)\.\d{1,3}|192\.168\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3})\.\d{1,3}$/.test(host)) {
    throw new Error("Escribe una IP privada de la LAN, sin puerto.");
  }
  if (host.split(".").some((part) => Number(part) > 255)) {
    throw new Error("IP no válida.");
  }
  return `http://${host}:8000`;
}

export async function json(base, path, options = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 2500);
  try {
    const response = await fetch(`${base}/v1/chatgpt/extension/${path}`, {
      cache: "no-store", ...options, signal: controller.signal,
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    });
    const value = await response.json();
    if (!response.ok) throw new Error(value.error?.message || value.state || `HTTP ${response.status}`);
    return value;
  } finally {
    clearTimeout(timer);
  }
}

export async function discover(ip = "") {
  const candidates = [bridgeUrl(), bridgeUrl("localhost")];
  if (ip.trim() && !["localhost", "127.0.0.1"].includes(ip.trim())) candidates.push(bridgeUrl(ip));
  for (const base of candidates) {
    try {
      const found = await json(base, "discover");
      if (found.service === "chatgpt-bridge-extension" && found.version === 1) {
        return { ...found, base };
      }
    } catch { /* try the next local address */ }
  }
  throw new Error(ip ? "Bridge no encontrado en localhost ni en esa IP (puerto 8000)." : "Bridge no encontrado en localhost.");
}

export async function keyPair() {
  const saved = await chrome.storage.local.get("clientKeys");
  if (saved.clientKeys) {
    const publicKey = await crypto.subtle.importKey("jwk", saved.clientKeys.publicKey,
      { name: "RSA-OAEP", hash: "SHA-256" }, true, ["encrypt"]);
    const privateKey = await crypto.subtle.importKey("jwk", saved.clientKeys.privateKey,
      { name: "RSA-OAEP", hash: "SHA-256" }, true, ["decrypt"]);
    return { publicKey, privateKey };
  }
  const keys = await crypto.subtle.generateKey(
    { name: "RSA-OAEP", modulusLength: 3072, publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256" },
    true, ["encrypt", "decrypt"]);
  await chrome.storage.local.set({ clientKeys: {
    publicKey: await crypto.subtle.exportKey("jwk", keys.publicKey),
    privateKey: await crypto.subtle.exportKey("jwk", keys.privateKey),
  } });
  return keys;
}

export async function beginPair(found) {
  const keys = await keyPair();
  const saved = await chrome.storage.local.get("clientId");
  const clientId = saved.clientId || crypto.randomUUID();
  await chrome.storage.local.set({ clientId });
  const publicKey = bytes64(new Uint8Array(await crypto.subtle.exportKey("spki", keys.publicKey)));
  const pair = await json(found.base, "pair", {
    method: "POST", body: JSON.stringify({ client_id: clientId, public_key: publicKey, client_name: "Chrome" }),
  });
  await chrome.storage.local.set({
    bridgeBase: found.base, bridgeFingerprint: found.fingerprint,
    pairRequestId: pair.request_id, pairCode: pair.code, pairStartedAt: Date.now(),
  });
  return pair;
}

export async function finishPair() {
  const state = await chrome.storage.local.get(["bridgeBase", "pairRequestId", "bridgeFingerprint"]);
  if (!state.bridgeBase || !state.pairRequestId) throw new Error("No hay emparejamiento pendiente.");
  const found = await json(state.bridgeBase, "discover");
  if (found.fingerprint !== state.bridgeFingerprint) throw new Error("La identidad del bridge cambió. Revisa la IP.");
  const result = await json(state.bridgeBase, `pair/status?request_id=${encodeURIComponent(state.pairRequestId)}`);
  if (result.state !== "approved") return result;
  const keys = await keyPair();
  const tokenBytes = await crypto.subtle.decrypt({ name: "RSA-OAEP" }, keys.privateKey, from64(result.encrypted_token));
  await chrome.storage.local.set({ bridgeToken: dec.decode(tokenBytes), bridgeAccount: result.account });
  await chrome.storage.local.remove(["pairRequestId", "pairCode", "pairStartedAt"]);
  return result;
}

export async function envelope(value) {
  const state = await chrome.storage.local.get(["bridgeBase", "bridgeFingerprint", "bridgeToken", "clientId"]);
  if (!state.bridgeToken) throw new Error("Aprueba primero la extensión en Accounts.");
  const found = await json(state.bridgeBase, "discover");
  if (found.fingerprint !== state.bridgeFingerprint) throw new Error("La identidad del bridge cambió. Se bloqueó el envío.");
  const serverKey = await crypto.subtle.importKey("spki", from64(found.public_key),
    { name: "RSA-OAEP", hash: "SHA-256" }, false, ["encrypt"]);
  const aes = await crypto.subtle.generateKey({ name: "AES-GCM", length: 256 }, true, ["encrypt"]);
  const nonce = crypto.getRandomValues(new Uint8Array(12));
  const ciphertext = await crypto.subtle.encrypt(
    { name: "AES-GCM", iv: nonce, additionalData: enc.encode("chatgpt-bridge-extension-v1") },
    aes, enc.encode(JSON.stringify({ ...value, token: state.bridgeToken, client_id: state.clientId })));
  const rawKey = await crypto.subtle.exportKey("raw", aes);
  const encryptedKey = await crypto.subtle.encrypt({ name: "RSA-OAEP" }, serverKey, rawKey);
  return { base: state.bridgeBase, body: {
    key: bytes64(new Uint8Array(encryptedKey)),
    nonce: bytes64(nonce),
    ciphertext: bytes64(new Uint8Array(ciphertext)),
  } };
}

export async function securePost(path, value = {}) {
  const sealed = await envelope(value);
  return json(sealed.base, path, { method: "POST", body: JSON.stringify(sealed.body) });
}

export async function readChatgptSession() {
  const tabs = await chrome.tabs.query({ url: "https://chatgpt.com/*" });
  const tab = tabs.find((item) => item.id && !item.incognito);
  if (!tab) throw new Error("Abre chatgpt.com e inicia sesión en este perfil de Chrome.");
  const results = await chrome.scripting.executeScript({
    target: { tabId: tab.id }, world: "MAIN",
    func: async () => {
      const response = await fetch("/api/auth/session", { credentials: "include", cache: "no-store" });
      if (!response.ok) return { status: response.status };
      const session = await response.json();
      return { status: response.status, accessToken: session?.accessToken,
        email: session?.user?.email || session?.email || "" };
    },
  });
  const session = results[0]?.result;
  if (!session?.accessToken) {
    throw new Error("ChatGPT no devolvió sesión activa. Entra manualmente en chatgpt.com; si hay CAPTCHA, resuélvelo allí.");
  }
  const allCookies = await chrome.cookies.getAll({ domain: "chatgpt.com" });
  const cookies = allCookies
    .filter((cookie) => cookie.domain === "chatgpt.com" || cookie.domain === ".chatgpt.com")
    .map((cookie) => ({ name: cookie.name, value: cookie.value }));
  if (!cookies.length) throw new Error("Chrome no encontró cookies de ChatGPT.");
  return { access_token: session.accessToken, email: session.email, cookies };
}

export async function syncOnce() {
  const session = await readChatgptSession();
  const result = await securePost("sync", session);
  await chrome.storage.local.set({ lastSyncAt: Date.now(), lastError: "", health: "ok" });
  return result;
}
