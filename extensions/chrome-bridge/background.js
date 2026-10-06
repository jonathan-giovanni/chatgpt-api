import { securePost, syncOnce } from "./bridge.js";

const COLORS = {
  ok: "#35e8aa", expiring: "#f4bc58", expired: "#ff637e",
  revoked: "#ff637e", error: "#ff637e", connecting: "#59b9ff",
  paused: "#64748b", unknown: "#59b9ff",
};

async function paintIcon(state) {
  const color = COLORS[state] || COLORS.connecting;
  const imageData = {};
  for (const size of [16, 32, 48]) {
    const canvas = new OffscreenCanvas(size, size);
    const ctx = canvas.getContext("2d");
    const mid = size / 2;
    const gradient = ctx.createRadialGradient(mid, mid, size * .12, mid, mid, size * .52);
    gradient.addColorStop(0, "#172941");
    gradient.addColorStop(1, "#07111f");
    ctx.fillStyle = gradient;
    ctx.beginPath();
    ctx.arc(mid, mid, mid - 1, 0, Math.PI * 2);
    ctx.fill();
    ctx.strokeStyle = color;
    ctx.lineWidth = Math.max(1.5, size * .075);
    ctx.shadowColor = color;
    ctx.shadowBlur = size * .18;
    ctx.beginPath();
    ctx.arc(mid, mid, size * .34, -.75 * Math.PI, .76 * Math.PI);
    ctx.stroke();
    ctx.shadowBlur = 0;
    ctx.strokeStyle = "#e9fdff";
    ctx.lineWidth = Math.max(1.2, size * .068);
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    ctx.beginPath();
    ctx.moveTo(size * .27, mid);
    ctx.lineTo(size * .41, mid);
    ctx.lineTo(size * .47, size * .38);
    ctx.lineTo(size * .54, size * .65);
    ctx.lineTo(size * .60, mid);
    ctx.lineTo(size * .73, mid);
    ctx.stroke();
    imageData[size] = ctx.getImageData(0, 0, size, size);
  }
  await chrome.action.setIcon({ imageData });
}

export async function updateVisualState(state, autoRenew = true) {
  const visual = autoRenew ? state : "paused";
  const { lastAutomaticRenewal } = await chrome.storage.local.get("lastAutomaticRenewal");
  await paintIcon(visual);
  await chrome.action.setBadgeBackgroundColor({ color: COLORS[visual] || COLORS.connecting });
  await chrome.action.setBadgeText({
    text: visual === "ok" ? (lastAutomaticRenewal ? "✓" : "") : visual === "paused" ? "OFF" :
      visual === "connecting" ? "··" : visual === "unknown" ? "?" : "!",
  });
  await chrome.action.setTitle({ title: `ChatGPT Bridge · ${visual} · renovación ${autoRenew ? "activa" : "pausada"}` });
}

async function checkAndRenew() {
  const state = await chrome.storage.local.get(["bridgeToken", "autoRenew", "lastError"]);
  if (!state.bridgeToken) {
    await updateVisualState("connecting", state.autoRenew !== false);
    return;
  }
  const enabled = state.autoRenew !== false;
  try {
    const health = await securePost("health");
    await chrome.storage.local.set({ health: health.state, accountHealth: health });
    if (health.state === "ok" && state.lastError) {
      await securePost("report", { state: "ok" });
      await chrome.storage.local.set({ lastError: "" });
    }
    await updateVisualState(health.state, enabled);
    if (!enabled || !["expired", "expiring", "revoked"].includes(health.state)) return;
    let error;
    for (let attempt = 0; attempt < 3; attempt++) {
      try {
        await syncOnce({ automatic: true });
        await securePost("report", { state: "ok" });
        await updateVisualState("ok", true);
        return;
      } catch (exc) {
        error = exc;
        if (attempt < 2) await new Promise((resolve) => setTimeout(resolve, 2000 * (attempt + 1)));
      }
    }
    throw error;
  } catch (error) {
    const message = String(error?.message || error);
    const needsLogin = /sesión activa|inicia sesión|CAPTCHA|access token/i.test(message);
    await chrome.storage.local.set({ health: "error", lastError: message });
    try { await securePost("report", { state: needsLogin ? "needs_login" : "network_error" }); } catch { /* bridge may be offline */ }
    await updateVisualState(needsLogin ? "revoked" : "error", enabled);
  }
}

async function start() {
  await chrome.alarms.create("bridge-health", { periodInMinutes: 720 });
  await checkAndRenew();
}

chrome.runtime.onInstalled.addListener(() => { void start(); });
chrome.runtime.onStartup.addListener(() => { void start(); });
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "bridge-health") void checkAndRenew();
});
chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "local" || !changes.autoRenew) return;
  void chrome.storage.local.get(["health", "autoRenew"]).then((state) =>
    updateVisualState(state.health || "connecting", state.autoRenew !== false));
});
chrome.runtime.onMessage.addListener((message, _sender, respond) => {
  if (message?.type !== "check-now") return;
  checkAndRenew().then(() => respond({ ok: true })).catch((error) => respond({ error: String(error) }));
  return true;
});
