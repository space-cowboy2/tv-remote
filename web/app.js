// TV Remote web app. Talks to the hub API on the same origin.

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

const state = { ws: null, wsReady: false, msgId: 0, pending: new Map(), retry: 0, appsLoaded: false };

// ------------------------------------------------------------------ helpers
let toastTimer;
function toast(msg, ms = 2500) {
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), ms);
}

function buzz() {
  try { navigator.vibrate?.(12); } catch {}
}

async function api(path, { method = "GET", body } = {}) {
  const res = await fetch(`/api/${path}`, {
    method,
    credentials: "same-origin",
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (res.status === 401 && path !== "login") {
    showLogin();
    throw new Error("Please sign in");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `Error ${res.status}`);
  return data;
}

// ------------------------------------------------------------------- screens
function showLogin() {
  closeSocket();
  $("#app").hidden = true;
  $("#login").hidden = false;
  $("#key").focus();
}

function showApp() {
  $("#login").hidden = true;
  $("#app").hidden = false;
  openSocket();
  refreshStatus();
}

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = $("#login-error");
  err.textContent = "";
  try {
    await api("login", { method: "POST", body: { key: $("#key").value.trim() } });
    $("#key").value = "";
    showApp();
  } catch (ex) {
    err.textContent = ex.message;
  }
});

$("#logout").addEventListener("click", async () => {
  await api("logout", { method: "POST" }).catch(() => {});
  showLogin();
});

// -------------------------------------------------------------------- status
async function refreshStatus() {
  const el = $("#status");
  try {
    const s = await api("status");
    el.className = `status ${s.power}`;
    el.lastElementChild.textContent =
      s.power === "on" ? "TV on" : s.power === "standby" ? "TV in standby" : "TV off";
  } catch (ex) {
    el.className = "status error";
    el.lastElementChild.textContent = navigator.onLine ? "Hub unreachable" : "Offline";
  }
}
setInterval(() => { if (!document.hidden && !$("#app").hidden) refreshStatus(); }, 15000);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && !$("#app").hidden) { refreshStatus(); openSocket(); }
});

// ----------------------------------------------------------------- websocket
function openSocket() {
  if (state.ws && state.ws.readyState <= 1) return;
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/api/ws`);
  state.ws = ws;
  ws.onopen = () => { state.wsReady = true; state.retry = 0; };
  ws.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    const p = state.pending.get(msg.id);
    if (p) { state.pending.delete(msg.id); msg.ok ? p.resolve() : p.reject(new Error(msg.error)); }
  };
  ws.onclose = (ev) => {
    state.wsReady = false;
    for (const p of state.pending.values()) p.reject(new Error("connection lost"));
    state.pending.clear();
    if (ev.code === 4401) return showLogin();
    if ($("#app").hidden || document.hidden) return;
    const delay = Math.min(1000 * 2 ** state.retry++, 15000);
    setTimeout(openSocket, delay);
  };
}

function closeSocket() {
  const ws = state.ws;
  state.ws = null;
  state.wsReady = false;
  if (ws) { ws.onclose = null; ws.close(); }
}

function sendWs(payload) {
  return new Promise((resolve, reject) => {
    const id = ++state.msgId;
    state.pending.set(id, { resolve, reject });
    state.ws.send(JSON.stringify({ ...payload, id }));
    setTimeout(() => {
      if (state.pending.delete(id)) reject(new Error("TV did not respond"));
    }, 5000);
  });
}

async function sendKey(key, cmd = "Click") {
  try {
    if (state.wsReady) await sendWs({ key, cmd });
    else await api("key", { method: "POST", body: { key, cmd } });
  } catch (ex) {
    toast(ex.message.includes("unreachable") || ex.message.includes("lost") ? "TV is off or unreachable" : ex.message);
    refreshStatus();
  }
}

// -------------------------------------------------------------------- keys
// Tap = one press. Buttons with data-repeat repeat while held.
function bindKeyButton(btn) {
  const key = btn.dataset.key;
  let holdTimer, repeatTimer;
  const stop = () => {
    clearTimeout(holdTimer);
    clearInterval(repeatTimer);
    btn.classList.remove("pressed");
  };
  btn.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    btn.classList.add("pressed");
    buzz();
    sendKey(key);
    if (btn.hasAttribute("data-repeat")) {
      holdTimer = setTimeout(() => { repeatTimer = setInterval(() => sendKey(key), 140); }, 450);
    }
  });
  ["pointerup", "pointerleave", "pointercancel"].forEach((t) => btn.addEventListener(t, stop));
  // Keyboard activation (Enter/Space) for accessibility
  btn.addEventListener("click", (e) => { if (e.detail === 0) sendKey(key); });
  btn.addEventListener("contextmenu", (e) => e.preventDefault());
}
$$("[data-key]").forEach(bindKeyButton);

$("#power").addEventListener("click", async () => {
  buzz();
  const btn = $("#power");
  btn.disabled = true;
  try {
    const s = await api("status");
    const target = s.power === "on" ? "off" : "on";
    toast(target === "on" ? "Turning TV on…" : "Turning TV off…");
    await api("power", { method: "POST", body: { state: target } });
  } catch (ex) {
    toast(ex.message);
  } finally {
    btn.disabled = false;
    setTimeout(refreshStatus, 1500);
  }
});

// Desktop keyboard shortcuts (ignored while typing in a field)
const SHORTCUTS = {
  ArrowUp: "KEY_UP", ArrowDown: "KEY_DOWN", ArrowLeft: "KEY_LEFT", ArrowRight: "KEY_RIGHT",
  Enter: "KEY_ENTER", Backspace: "KEY_RETURN", Escape: "KEY_HOME", h: "KEY_HOME",
  "+": "KEY_VOLUP", "=": "KEY_VOLUP", "-": "KEY_VOLDOWN", m: "KEY_MUTE", " ": "KEY_PLAY",
};
document.addEventListener("keydown", (e) => {
  if ($("#app").hidden || e.target.matches("input, textarea") || e.metaKey || e.ctrlKey || e.altKey) return;
  const k = SHORTCUTS[e.key] || (/^[0-9]$/.test(e.key) ? `KEY_${e.key}` : null);
  if (!k || e.target.matches("button") && (e.key === "Enter" || e.key === " ")) return;
  e.preventDefault();
  sendKey(k);
});

// -------------------------------------------------------------------- tabs
$$(".tabs [data-tab]").forEach((tab) =>
  tab.addEventListener("click", () => {
    $$(".tabs [data-tab]").forEach((t) => t.setAttribute("aria-selected", String(t === tab)));
    $$(".tab").forEach((p) => (p.hidden = p.id !== `tab-${tab.dataset.tab}`));
    if (tab.dataset.tab === "apps" && !state.appsLoaded) loadApps();
    if (tab.dataset.tab === "keyboard") $("#text").focus();
  })
);

// -------------------------------------------------------------------- apps
const PALETTE = ["#e50914", "#ff0033", "#1db954", "#00a8e1", "#5b3cc4", "#f5a623", "#0f9d58", "#3d6df2"];
function colorFor(name) {
  let h = 0;
  for (const c of name) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return PALETTE[h % PALETTE.length];
}

async function loadApps(refresh = false) {
  const box = $("#apps");
  box.textContent = "Loading apps…";
  try {
    const apps = await api(`apps${refresh ? "?refresh=true" : ""}`);
    box.textContent = "";
    if (!apps.length) box.textContent = "No apps found.";
    for (const app of apps) {
      const name = app.name || app.id;
      const b = document.createElement("button");
      const badge = document.createElement("span");
      badge.className = "badge";
      badge.style.background = colorFor(name);
      badge.textContent = name.slice(0, 1).toUpperCase();
      b.append(badge, document.createTextNode(name));
      b.addEventListener("click", async () => {
        buzz();
        try {
          await api("launch", { method: "POST", body: { app_id: app.id } });
          toast(`Opening ${name}`);
        } catch (ex) { toast(ex.message); }
      });
      box.append(b);
    }
    state.appsLoaded = true;
  } catch (ex) {
    box.textContent = ex.message.includes("unreachable") ? "Turn the TV on to see apps." : ex.message;
  }
}
$("#apps-refresh").addEventListener("click", () => loadApps(true));

// ---------------------------------------------------------------- keyboard
async function sendText(submit) {
  const input = $("#text");
  const text = input.value;
  if (!text) return;
  try {
    await api("text", { method: "POST", body: { text, submit } });
    input.value = "";
    toast("Sent");
  } catch (ex) { toast(ex.message); }
}
$("#text-form").addEventListener("submit", (e) => { e.preventDefault(); sendText(false); });
$("#text-submit").addEventListener("click", () => sendText(true));

// -------------------------------------------------------------------- boot
if ("serviceWorker" in navigator && (location.protocol === "https:" || location.hostname === "localhost")) {
  navigator.serviceWorker.register("sw.js").catch(() => {});
}

(async () => {
  try {
    await api("status");
    showApp();
  } catch {
    if ($("#app").hidden) showLogin();
  }
})();
