import { securePost, syncOnce } from "./bridge.js";

async function updateBadge(state) {
  const colors = { ok: "#15803d", expiring: "#b45309", expired: "#b91c1c", error: "#b91c1c" };
  await chrome.action.setBadgeBackgroundColor({ color: colors[state] || "#475569" });
  await chrome.action.setBadgeText({ text: state === "ok" ? "✓" : state === "expiring" ? "!" : state === "expired" || state === "error" ? "×" : "" });
}

async function checkAndRenew() {
  const state = await chrome.storage.local.get(["bridgeToken", "lastSyncAt"]);
  if (!state.bridgeToken) return;
  try {
    const health = await securePost("health");
    await chrome.storage.local.set({ health: health.state, accountHealth: health, lastError: "" });
    await updateBadge(health.state);
    if (health.state === "ok" && Date.now() - (state.lastSyncAt || 0) < 3 * 86400000) return;
    let error;
    for (let attempt = 0; attempt < 3; attempt++) {
      try {
        await syncOnce();
        await updateBadge("ok");
        return;
      } catch (exc) {
        error = exc;
        if (attempt < 2) await new Promise((resolve) => setTimeout(resolve, 2000 * (attempt + 1)));
      }
    }
    throw error;
  } catch (error) {
    await chrome.storage.local.set({ health: "error", lastError: String(error.message || error) });
    await updateBadge("error");
  }
}

chrome.runtime.onInstalled.addListener(() => chrome.alarms.create("bridge-health", { periodInMinutes: 720 }));
chrome.runtime.onStartup.addListener(() => chrome.alarms.create("bridge-health", { periodInMinutes: 720 }));
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "bridge-health") void checkAndRenew();
});
chrome.runtime.onMessage.addListener((message, _sender, respond) => {
  if (message?.type !== "check-now") return;
  checkAndRenew().then(() => respond({ ok: true })).catch((error) => respond({ error: String(error) }));
  return true;
});
