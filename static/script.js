/* =========================================================================
   Mnemo — frontend
   Talks to the Flask API: conversation CRUD, SSE-streamed chat, full-text
   search, long-term memory, context metering, voice in and voice out.
   ========================================================================= */
"use strict";

const $ = (id) => document.getElementById(id);

const el = {
  app: $("app"),
  sidebar: $("sidebar"),
  sidebarOpen: $("sidebar-open"),
  sidebarClose: $("sidebar-close"),
  scrim: $("scrim"),
  newChat: $("new-chat-btn"),
  incognitoBtn: $("incognito-btn"),
  incognitoBar: $("incognito-bar"),
  incognitoExit: $("incognito-exit"),
  filter: $("filter-input"),
  deepSearch: $("deep-search-btn"),
  convList: $("conversation-list"),
  memoryBtn: $("memory-btn"),
  memoryCount: $("memory-count"),
  statLine: $("stat-line"),
  themeToggle: $("theme-toggle"),
  voiceBtn: $("voice-btn"),

  title: $("conversation-title"),
  arrayLength: $("array-length"),
  modelBadge: $("model-badge"),
  contextMeter: $("context-meter"),
  meterFill: $("meter-fill"),
  meterLabel: $("meter-label"),
  autospeak: $("autospeak-btn"),
  menuBtn: $("menu-btn"),
  menu: $("menu"),
  pinLabel: $("pin-label"),

  notSavedChip: $("not-saved-chip"),

  thread: $("thread"),
  emptyState: $("empty-state"),
  incognitoEmpty: $("incognito-empty"),
  suggestions: $("suggestions"),
  scrollBottom: $("scroll-bottom"),

  form: $("chat-form"),
  input: $("message-input"),
  micBtn: $("mic-btn"),
  sendBtn: $("send-btn"),
  composer: document.querySelector(".composer"),
  composerHint: $("composer-hint"),

  searchModal: $("search-modal"),
  searchInput: $("search-input"),
  searchResults: $("search-results"),

  voiceModal: $("voice-modal"),
  voiceClose: $("voice-close"),
  voiceList: $("voice-list"),
  voiceSourceNote: $("voice-source-note"),
  rateRange: $("rate-range"),
  rateValue: $("rate-value"),
  pitchRange: $("pitch-range"),
  pitchValue: $("pitch-value"),
  voicePreview: $("voice-preview"),

  settingsModal: $("settings-modal"),
  settingsClose: $("settings-close"),
  settingsSave: $("settings-save"),
  modelList: $("model-list"),
  personaInput: $("persona-input"),
  personaReset: $("persona-reset"),

  memoryModal: $("memory-modal"),
  memoryClose: $("memory-close"),
  memoryAdd: $("memory-add"),
  memoryInput: $("memory-input"),
  memoryList: $("memory-list"),
  memoryClear: $("memory-clear"),
  memoryDedupe: $("memory-dedupe"),
  rememberToggle: $("remember-toggle"),

  dialogModal: $("dialog-modal"),
  dialogPanel: document.querySelector(".dialog-panel"),
  dialogTitle: $("dialog-title"),
  dialogBody: $("dialog-body"),
  dialogInput: $("dialog-input"),
  dialogCancel: $("dialog-cancel"),
  dialogConfirm: $("dialog-confirm"),

  toast: $("toast"),
  audio: $("tts-audio"),
};

const state = {
  config: { models: [], default_model: "", default_system_prompt: "" },
  conversations: [],
  activeId: null,
  conversation: null,
  incognitoId: null,
  messages: [],
  memories: [],
  filter: "",
  streaming: false,
  abort: null,
  autoScroll: true,
  voices: [],
  speakingId: null,
  searchCursor: -1,
  searchRows: [],
  pendingModel: null,
  listening: false,
};

const prefs = {
  get(key, fallback) {
    try {
      const raw = localStorage.getItem("sm:" + key);
      return raw === null ? fallback : JSON.parse(raw);
    } catch { return fallback; }
  },
  set(key, value) {
    try { localStorage.setItem("sm:" + key, JSON.stringify(value)); } catch { /* private mode */ }
  },
};

/* ============================== helpers ============================== */

function toast(message, kind = "") {
  el.toast.textContent = message;
  el.toast.className = "toast" + (kind ? " " + kind : "");
  el.toast.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.toast.hidden = true; }, kind === "error" ? 5200 : 2600);
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: options.body ? { "Content-Type": "application/json" } : {},
    ...options,
  });
  const data = res.status === 204 ? {} : await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}

function relativeDay(ts) {
  const date = new Date(ts * 1000);
  const today = new Date();
  const startOf = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const days = Math.round((startOf(today) - startOf(date)) / 86400000);
  if (days <= 0) return "Today";
  if (days === 1) return "Yesterday";
  if (days < 7) return "Previous 7 days";
  if (days < 30) return "Previous 30 days";
  return date.toLocaleDateString(undefined, { month: "long", year: "numeric" });
}

function clockTime(ts) {
  return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

const icons = {
  bot: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="8" width="16" height="12" rx="4"/><path d="M12 8V4M9 14h.01M15 14h.01"/></svg>`,
  copy: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>`,
  speaker: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M11 5 6 9H3v6h3l5 4V5Z"/><path d="M15.5 8.5a5 5 0 0 1 0 7"/></svg>`,
  stop: `<svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2.5"/></svg>`,
  spinner: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 3a9 9 0 1 0 9 9" /></svg>`,
  trash: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6"/></svg>`,
  redo: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-3-6.7L21 8"/><path d="M21 3v5h-5"/></svg>`,
  pencil: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>`,
  pin: `<svg class="conv-pin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 17v5M9 3h6l-1 6 3 3v2H7v-2l3-3-1-6Z"/></svg>`,
  mask: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M4 12h16M7.5 12l1.2-4.2A2 2 0 0 1 10.6 6.3h2.8a2 2 0 0 1 1.9 1.5L16.5 12"/><circle cx="7" cy="15.5" r="2.5"/><circle cx="17" cy="15.5" r="2.5"/><path d="M9.5 15.5h5"/></svg>`,
};

/** The assistant wears a mask while off the record. */
const botAvatar = () => (state.incognitoId ? icons.mask : icons.bot);

/* =========================== dialog helper =========================== */
// Replaces the browser's prompt()/confirm(), which are ugly, unstyleable, and
// on some setups get suppressed entirely. Returns a promise: the entered
// string for a prompt, true/false for a confirm, or null when cancelled.

let dialogResolve = null;

function closeDialog(value) {
  el.dialogModal.hidden = true;
  el.dialogPanel.classList.remove("danger");
  const resolve = dialogResolve;
  dialogResolve = null;
  if (resolve) resolve(value);
}

function openDialog({ title, body = "", value = null, confirmLabel = "OK", danger = false }) {
  // Never leave a previous caller hanging if two dialogs race.
  if (dialogResolve) closeDialog(null);

  el.dialogTitle.textContent = title;
  el.dialogBody.textContent = body;
  el.dialogBody.hidden = !body;
  el.dialogConfirm.textContent = confirmLabel;
  el.dialogPanel.classList.toggle("danger", danger);

  const isPrompt = value !== null;
  el.dialogInput.hidden = !isPrompt;
  el.dialogInput.value = isPrompt ? value : "";

  el.dialogModal.hidden = false;
  setTimeout(() => {
    if (isPrompt) { el.dialogInput.focus(); el.dialogInput.select(); }
    else el.dialogConfirm.focus();
  }, 30);

  return new Promise((resolve) => { dialogResolve = resolve; });
}

const askText = (title, value, body) =>
  openDialog({ title, body, value, confirmLabel: "Save" });

const askConfirm = (title, body, confirmLabel = "Confirm", danger = true) =>
  openDialog({ title, body, confirmLabel, danger }).then((v) => v === true);

el.dialogConfirm.addEventListener("click", () => {
  closeDialog(el.dialogInput.hidden ? true : el.dialogInput.value.trim());
});
el.dialogCancel.addEventListener("click", () => closeDialog(null));
el.dialogModal.addEventListener("click", (e) => {
  if (e.target === el.dialogModal) closeDialog(null);
});
el.dialogInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); el.dialogConfirm.click(); }
});

/* =============================== theme =============================== */

function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  prefs.set("theme", theme);
}

(function initTheme() {
  const saved = prefs.get("theme", null);
  if (saved) applyTheme(saved);
  else applyTheme(window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
})();

el.themeToggle.addEventListener("click", () => {
  const current = document.documentElement.getAttribute("data-theme");
  applyTheme(current === "dark" ? "light" : "dark");
});

/* ============================== sidebar ============================== */

function openSidebar() { el.app.classList.add("sidebar-open"); el.scrim.hidden = false; }
function closeSidebar() { el.app.classList.remove("sidebar-open"); el.scrim.hidden = true; }

el.sidebarOpen.addEventListener("click", openSidebar);
el.sidebarClose.addEventListener("click", closeSidebar);
el.scrim.addEventListener("click", closeSidebar);

function renderSidebar() {
  const q = state.filter.toLowerCase();
  const items = state.conversations.filter(
    (c) => !q ||
      c.title.toLowerCase().includes(q) ||
      (c.preview || "").toLowerCase().includes(q)
  );

  el.convList.innerHTML = "";

  if (!items.length) {
    const empty = document.createElement("p");
    empty.className = "conv-empty";
    empty.textContent = state.filter
      ? "No chats match that filter."
      : "No saved chats yet. Start one above.";
    el.convList.appendChild(empty);
    return;
  }

  let lastGroup = null;
  for (const conv of items) {
    const group = conv.pinned ? "Pinned" : relativeDay(conv.updated_at);
    if (group !== lastGroup) {
      lastGroup = group;
      const header = document.createElement("div");
      header.className = "conv-group";
      header.textContent = group;
      el.convList.appendChild(header);
    }

    const item = document.createElement("div");
    item.className = "conv-item" +
      (conv.id === state.activeId && !state.incognitoId ? " active" : "");

    const main = document.createElement("button");
    main.type = "button";
    main.className = "conv-main";
    main.style.cssText = "border:none;background:none;padding:0;text-align:left;";
    main.innerHTML =
      `<span class="conv-title"></span>` +
      `<span class="conv-sub"></span>`;
    main.querySelector(".conv-title").textContent = conv.title;
    main.querySelector(".conv-sub").textContent =
      `${conv.message_count} msg${conv.message_count === 1 ? "" : "s"} · ${clockTime(conv.updated_at)}`;
    main.addEventListener("click", () => {
      openConversation(conv.id);
      closeSidebar();
    });

    const del = document.createElement("button");
    del.type = "button";
    del.className = "conv-del";
    del.title = "Delete conversation";
    del.innerHTML = icons.trash;
    del.addEventListener("click", async (e) => {
      e.stopPropagation();
      const ok = await askConfirm(
        "Delete this conversation?",
        `“${conv.title}” and all ${conv.message_count} of its messages will be removed from the database. This can't be undone.`,
        "Delete"
      );
      if (!ok) return;
      try {
        await api(`/api/conversations/${conv.id}`, { method: "DELETE" });
        if (state.activeId === conv.id) { state.activeId = null; state.messages = []; }
        await refreshConversations();
        if (!state.activeId) {
          const next = state.conversations[0];
          if (next) openConversation(next.id);
          else startBlankConversation();
        }
        toast("Conversation deleted.");
      } catch (err) { toast(err.message, "error"); }
    });

    if (conv.pinned) item.insertAdjacentHTML("afterbegin", icons.pin);
    item.append(main, del);
    el.convList.appendChild(item);
  }
}

async function refreshConversations() {
  const data = await api("/api/conversations");
  state.conversations = data.conversations;
  renderSidebar();
  updateStats();
}

function updateStats() {
  const convs = state.conversations.length;
  const msgs = state.conversations.reduce((n, c) => n + c.message_count, 0);
  el.statLine.textContent = `${convs} chat${convs === 1 ? "" : "s"} · ${msgs} messages on disk`;
}

el.filter.addEventListener("input", () => {
  state.filter = el.filter.value.trim();
  renderSidebar();
});

el.newChat.addEventListener("click", () => {
  startBlankConversation();
  closeSidebar();
});

/* ============================= incognito ============================= */

/** Everything that has to flip when entering or leaving incognito. The
 *  attribute goes on <html> rather than .app so the page background and every
 *  colour token change with it. */
function applyIncognitoChrome(on) {
  if (on) document.documentElement.setAttribute("data-incognito", "true");
  else document.documentElement.removeAttribute("data-incognito");

  el.app.classList.toggle("incognito", on);
  el.incognitoBar.hidden = !on;
  el.notSavedChip.hidden = !on;
  // A token count is meaningless for a thread that isn't kept.
  el.contextMeter.hidden = on;
  el.input.placeholder = on
    ? "Say something off the record…"
    : "Send a message…";
  el.composerHint.textContent = on
    ? "Enter to send · Shift+Enter for a new line · nothing here is being saved"
    : "Enter to send · Shift+Enter for a new line";
}

async function enterIncognito() {
  if (state.incognitoId) return;
  try {
    const data = await api("/api/incognito", { method: "POST" });
    state.incognitoId = data.incognito_id;
    state.activeId = null;
    state.conversation = null;
    state.messages = [];
    applyIncognitoChrome(true);
    el.title.textContent = "Incognito chat";
    el.arrayLength.textContent = "0";
    setContext(null);
    renderThread();
    renderSidebar();
    el.input.focus();
  } catch (err) { toast(err.message, "error"); }
}

async function leaveIncognito({ silent = false } = {}) {
  if (!state.incognitoId) return;
  const id = state.incognitoId;
  const had = state.messages.length;
  state.incognitoId = null;
  applyIncognitoChrome(false);

  // keepalive so the purge still lands if the tab is closing
  fetch(`/api/incognito/${id}`, { method: "DELETE", keepalive: true }).catch(() => {});

  if (!silent) {
    const next = state.conversations[0];
    if (next) await openConversation(next.id);
    else startBlankConversation();
    if (had) toast(`Incognito chat discarded — ${had} message${had === 1 ? "" : "s"} gone.`);
  }
}

el.incognitoBtn.addEventListener("click", () => {
  if (state.incognitoId) leaveIncognito();
  else { enterIncognito(); closeSidebar(); }
});
el.incognitoExit.addEventListener("click", () => leaveIncognito());

/* ============================ conversation ============================ */

function startBlankConversation() {
  // No server round-trip: the row is created lazily on the first message, so
  // clicking "New chat" repeatedly doesn't litter the sidebar with empties.
  if (state.incognitoId) leaveIncognito({ silent: true });
  state.activeId = null;
  state.conversation = null;
  state.messages = [];
  el.title.textContent = "New chat";
  el.arrayLength.textContent = "0";
  setModelBadge(null);
  setContext(null);
  renderThread();
  renderSidebar();
  el.input.focus();
}

async function openConversation(id) {
  if (state.incognitoId) await leaveIncognito({ silent: true });
  try {
    const data = await api(`/api/conversations/${id}`);
    state.activeId = id;
    state.conversation = data.conversation;
    state.messages = data.messages;
    el.title.textContent = data.conversation.title;
    el.arrayLength.textContent = data.messages.length;
    setModelBadge(data.conversation.model);
    renderThread();
    renderSidebar();
    refreshContext();
  } catch (err) {
    toast(err.message, "error");
    startBlankConversation();
  }
}

function setModelBadge(model) {
  const id = model || state.config.default_model || "gemini-flash-latest";
  const known = state.config.models.find((m) => m.id === id);
  el.modelBadge.textContent = known ? known.name : id.replace("-latest", "");
  el.modelBadge.title = `${id} — click to change model or persona`;
}

/* =========================== context meter =========================== */

function setContext(context) {
  if (!context) {
    el.meterFill.style.width = "0%";
    el.meterLabel.textContent = "0 tokens";
    el.contextMeter.classList.remove("warm");
    el.contextMeter.title = "How much of the model's context window this thread uses";
    return;
  }
  const { tokens, limit, percent, trim_at: trimAt } = context;
  // A raw percentage of a 1M window is always ~0%, which tells you nothing.
  // Scale the bar against the trim threshold instead — that's the number the
  // app actually acts on.
  const shown = Math.min(100, (tokens / (trimAt || limit)) * 100);
  el.meterFill.style.width = shown.toFixed(1) + "%";
  el.meterLabel.textContent = tokens >= 1000
    ? `${(tokens / 1000).toFixed(1)}k tokens`
    : `${tokens} tokens`;
  el.contextMeter.classList.toggle("warm", shown > 70);
  el.contextMeter.title =
    `${tokens.toLocaleString()} of ${limit.toLocaleString()} tokens (${percent}% of the window).\n` +
    `Past ${trimAt.toLocaleString()} the oldest turns get summarised instead of resent.`;
}

async function refreshContext() {
  if (!state.activeId || state.incognitoId) { setContext(null); return; }
  try {
    const data = await api(`/api/context?conversation_id=${state.activeId}`);
    setContext(data.context);
  } catch { /* meter just stays where it was */ }
}

el.contextMeter.addEventListener("click", () => {
  toast(el.contextMeter.title.split("\n")[0]);
});

/* ============================== thread ============================== */

function renderThread() {
  el.thread.innerHTML = "";
  el.scrollBottom.hidden = true;

  if (!state.messages.length) {
    // Incognito gets its own opening screen — the normal one talks about
    // writing to disk, which is exactly what isn't happening here.
    const empty = state.incognitoId ? el.incognitoEmpty : el.emptyState;
    const other = state.incognitoId ? el.emptyState : el.incognitoEmpty;
    other.hidden = true;
    el.thread.appendChild(empty);
    empty.hidden = false;
    return;
  }

  el.emptyState.hidden = true;
  el.incognitoEmpty.hidden = true;
  state.messages.forEach((msg, i) => el.thread.appendChild(buildMessage(msg, i)));
  requestAnimationFrame(() => scrollToBottom(true));
}

function buildMessage(msg, index) {
  const isUser = msg.role === "user";

  const wrap = document.createElement("div");
  wrap.className = `msg ${isUser ? "user" : "assistant"}`;
  if (msg.id) wrap.dataset.messageId = msg.id;

  const avatar = document.createElement("div");
  avatar.className = "avatar";
  avatar.innerHTML = isUser ? "YOU" : botAvatar();

  const col = document.createElement("div");
  col.className = "msg-col";

  const head = document.createElement("div");
  head.className = "msg-head";
  head.innerHTML =
    `<span class="msg-role">${isUser ? "you" : "assistant"}</span>` +
    `<span class="msg-index">history[${index}]</span>` +
    (msg.created_at ? `<span>${clockTime(msg.created_at)}</span>` : "");

  const bubble = document.createElement("div");
  bubble.className = "bubble";
  if (isUser) bubble.textContent = msg.text;
  else bubble.innerHTML = MD.render(msg.text);

  col.append(head, bubble);
  if (msg.text) col.appendChild(buildActions(msg, bubble, index));

  wrap.append(avatar, col);
  return wrap;
}

function buildActions(msg, bubble, index) {
  const actions = document.createElement("div");
  actions.className = "msg-actions";

  const copy = document.createElement("button");
  copy.type = "button";
  copy.className = "msg-action";
  copy.innerHTML = `${icons.copy}<span>Copy</span>`;
  copy.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(msg.text);
      copy.querySelector("span").textContent = "Copied";
      setTimeout(() => { copy.querySelector("span").textContent = "Copy"; }, 1400);
    } catch { toast("Clipboard blocked by the browser.", "error"); }
  });

  const speak = document.createElement("button");
  speak.type = "button";
  speak.className = "msg-action speak";
  speak.innerHTML = `${icons.speaker}<span>Listen</span>`;
  speak.addEventListener("click", () => toggleSpeech(msg, speak, bubble));

  actions.append(copy, speak);

  // Rewind actions need a server-side id to rewind *to*.
  if (msg.id && !state.incognitoId) {
    if (msg.role === "model") {
      const redo = document.createElement("button");
      redo.type = "button";
      redo.className = "msg-action";
      redo.innerHTML = `${icons.redo}<span>Regenerate</span>`;
      redo.addEventListener("click", () => {
        if (state.streaming) { toast("Wait for the current reply to finish.", "error"); return; }
        runTurn({ regenerateFrom: msg.id });
      });
      actions.appendChild(redo);
    } else {
      const edit = document.createElement("button");
      edit.type = "button";
      edit.className = "msg-action";
      edit.innerHTML = `${icons.pencil}<span>Edit</span>`;
      edit.addEventListener("click", () => beginEdit(msg, index));
      actions.appendChild(edit);
    }
  }

  return actions;
}

async function beginEdit(msg) {
  if (state.streaming) { toast("Wait for the current reply to finish.", "error"); return; }
  const next = await askText(
    "Edit this message",
    msg.text,
    "The reply below it will be discarded and regenerated from your new wording."
  );
  if (next === null || !next || next === msg.text) return;
  runTurn({ message: next, editMessageId: msg.id });
}

function nearBottom() {
  return el.thread.scrollHeight - el.thread.scrollTop - el.thread.clientHeight < 90;
}

function scrollToBottom(force = false) {
  if (force || state.autoScroll) el.thread.scrollTop = el.thread.scrollHeight;
}

el.thread.addEventListener("scroll", () => {
  state.autoScroll = nearBottom();
  el.scrollBottom.hidden = state.autoScroll || !state.messages.length;
});

el.scrollBottom.addEventListener("click", () => {
  state.autoScroll = true;
  scrollToBottom(true);
  el.scrollBottom.hidden = true;
});

/* =========================== sending a turn =========================== */

function autoGrow() {
  el.input.style.height = "auto";
  el.input.style.height = Math.min(el.input.scrollHeight, 190) + "px";
}

el.input.addEventListener("input", autoGrow);
el.input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    el.form.requestSubmit();
  }
});

el.suggestions.querySelectorAll("button").forEach((btn) => {
  btn.addEventListener("click", () => {
    el.input.value = btn.textContent;
    autoGrow();
    el.form.requestSubmit();
  });
});

el.form.addEventListener("submit", (e) => {
  e.preventDefault();
  if (state.streaming) { state.abort?.abort(); return; }
  const message = el.input.value.trim();
  if (message) runTurn({ message });
});

function setBusy(busy) {
  state.streaming = busy;
  el.composer.classList.toggle("busy", busy);
  el.sendBtn.disabled = false;
  el.sendBtn.setAttribute("aria-label", busy ? "Stop generating" : "Send message");
}

/**
 * One turn of conversation. Handles three shapes:
 *   { message }                          — a normal new message
 *   { regenerateFrom: <assistant id> }   — rewind to it and re-answer
 *   { message, editMessageId: <user id> }— rewind to it and replace the text
 */
async function runTurn({ message = "", regenerateFrom = null, editMessageId = null }) {
  stopListening();
  el.emptyState.hidden = true;
  el.incognitoEmpty.hidden = true;

  // Rewind the local view first so the thread matches what the server will do.
  const rewindId = regenerateFrom ?? editMessageId;
  if (rewindId != null) {
    const index = state.messages.findIndex((m) => m.id === rewindId);
    if (index === -1) { toast("That message is no longer in this conversation.", "error"); return; }
    state.messages.splice(index);
    renderThread();
  }

  let userNode = null;
  let userMsg = null;
  if (!regenerateFrom) {
    userMsg = { role: "user", text: message, created_at: Date.now() / 1000 };
    state.messages.push(userMsg);
    userNode = buildMessage(userMsg, state.messages.length - 1);
    el.thread.appendChild(userNode);
    el.input.value = "";
    autoGrow();
  }

  state.autoScroll = true;
  scrollToBottom(true);

  const replyNode = document.createElement("div");
  replyNode.className = "msg assistant streaming";
  replyNode.innerHTML =
    `<div class="avatar">${botAvatar()}</div>` +
    `<div class="msg-col">` +
      `<div class="msg-head"><span class="msg-role">assistant</span>` +
      `<span class="msg-index">history[${state.messages.length}]</span></div>` +
      `<div class="bubble"><span class="typing"><span></span><span></span><span></span></span></div>` +
    `</div>`;
  el.thread.appendChild(replyNode);
  scrollToBottom(true);

  const bubble = replyNode.querySelector(".bubble");
  setBusy(true);
  state.abort = new AbortController();

  let accumulated = "";
  let stopped = false;
  let frame = null;
  const paint = () => {
    frame = null;
    bubble.innerHTML = MD.render(accumulated);
    scrollToBottom();
  };

  try {
    const body = state.incognitoId
      ? { message, incognito_id: state.incognitoId }
      : {
          message,
          conversation_id: state.activeId,
          regenerate_from: regenerateFrom,
          edit_message_id: editMessageId,
          remember: prefs.get("remember", true),
        };

    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: state.abort.signal,
    });

    if (!res.ok || !res.body) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.error || `Server returned ${res.status}`);
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let failed = null;
    let finished = null;

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      let split;
      while ((split = buffer.indexOf("\n\n")) !== -1) {
        const raw = buffer.slice(0, split);
        buffer = buffer.slice(split + 2);
        if (!raw.trim()) continue;

        const eventMatch = raw.match(/^event:\s*(.+)$/m);
        const dataMatch = raw.match(/^data:\s*([\s\S]*)$/m);
        if (!eventMatch || !dataMatch) continue;

        const type = eventMatch[1].trim();
        let payload;
        try { payload = JSON.parse(dataMatch[1]); } catch { continue; }

        if (type === "start") {
          if (!payload.incognito) {
            state.activeId = payload.conversation_id;
            if (payload.title) el.title.textContent = payload.title;
          }
          // Stamp the row id the server assigned so search results and the
          // rewind actions can address this message.
          if (userMsg && payload.user_message_id) {
            userMsg.id = payload.user_message_id;
            userMsg.created_at = payload.created_at;
            userNode.dataset.messageId = payload.user_message_id;
          }
        } else if (type === "delta") {
          accumulated += payload.text;
          if (!frame) frame = requestAnimationFrame(paint);
        } else if (type === "error") {
          failed = payload.error;
        } else if (type === "done") {
          finished = payload;
        }
      }
    }

    if (frame) cancelAnimationFrame(frame);

    if (failed) {
      // Server rolled the user turn back, so mirror that locally.
      if (userMsg) { state.messages.pop(); userNode.remove(); el.input.value = message; autoGrow(); }
      replyNode.remove();
      if (!state.messages.length) renderThread();
      throw new Error(failed);
    }

    const reply = {
      role: "model",
      text: accumulated,
      id: finished?.message_id,
      created_at: finished?.created_at || Date.now() / 1000,
    };
    state.messages.push(reply);
    el.arrayLength.textContent = finished?.history_length ?? state.messages.length;
    if (finished?.title) el.title.textContent = finished.title;

    // The user row now has an id, so rebuild it with its rewind action.
    if (userMsg && userMsg.id) {
      userNode.replaceWith(buildMessage(userMsg, state.messages.length - 2));
    }
    replyNode.replaceWith(buildMessage(reply, state.messages.length - 1));
    scrollToBottom();

    if (!state.incognitoId) {
      refreshContext();          // token count, off the critical path
      await refreshConversations();
      // Fact extraction runs in a background thread server-side, so give it a
      // moment before asking what it learned.
      setTimeout(refreshMemories, 2500);
    }

    if (prefs.get("autospeak", false) && accumulated) {
      const node = el.thread.querySelector(".msg.assistant:last-child");
      toggleSpeech(reply, node?.querySelector(".speak"), node?.querySelector(".bubble"));
    }
  } catch (err) {
    if (err.name === "AbortError") {
      stopped = true;
      replyNode.classList.remove("streaming");
      if (accumulated) {
        const reply = { role: "model", text: accumulated, created_at: Date.now() / 1000 };
        state.messages.push(reply);
        replyNode.replaceWith(buildMessage(reply, state.messages.length - 1));
      } else {
        if (userMsg) { state.messages.pop(); userNode.remove(); }
        replyNode.remove();
      }
      toast("Stopped.");
      if (!state.incognitoId) refreshConversations().catch(() => {});
    } else {
      toast(err.message, "error");
    }
  } finally {
    if (!stopped) replyNode.classList.remove("streaming");
    setBusy(false);
    state.abort = null;
    el.input.focus();
  }
}

/* ============================ code copy ============================ */

el.thread.addEventListener("click", (e) => {
  const btn = e.target.closest(".code-copy");
  if (!btn) return;
  const block = btn.closest(".code-block");
  navigator.clipboard.writeText(block?.dataset.code || "").then(
    () => { btn.textContent = "copied"; setTimeout(() => { btn.textContent = "copy"; }, 1400); },
    () => toast("Clipboard blocked by the browser.", "error")
  );
});

/* ========================= conversation menu ========================= */

function closeMenu() {
  el.menu.hidden = true;
  el.menuBtn.setAttribute("aria-expanded", "false");
}

el.menuBtn.addEventListener("click", (e) => {
  e.stopPropagation();
  const open = el.menu.hidden;
  if (open) {
    el.pinLabel.textContent = state.conversation?.pinned ? "Unpin" : "Pin to top";
  }
  el.menu.hidden = !open;
  el.menuBtn.setAttribute("aria-expanded", String(open));
});

document.addEventListener("click", (e) => {
  if (!el.menu.hidden && !el.menu.contains(e.target)) closeMenu();
});

el.menu.addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-action]");
  if (!btn) return;
  closeMenu();
  const action = btn.dataset.action;

  if (action === "export-json") return exportJson();
  if (action === "export-md") return exportMarkdown();

  if (state.incognitoId) {
    toast("Incognito chats aren't saved, so there's nothing to change.", "error");
    return;
  }
  if (!state.activeId) { toast("Send a message first.", "error"); return; }

  try {
    if (action === "rename") {
      const next = await askText("Rename conversation", el.title.textContent);
      if (!next) return;
      const data = await api(`/api/conversations/${state.activeId}`, {
        method: "PATCH",
        body: JSON.stringify({ title: next }),
      });
      state.conversation = data.conversation;
      el.title.textContent = data.conversation.title;
      await refreshConversations();
      toast("Renamed.");
    } else if (action === "settings") {
      openSettings();
    } else if (action === "pin") {
      const data = await api(`/api/conversations/${state.activeId}`, {
        method: "PATCH",
        body: JSON.stringify({ pinned: !state.conversation?.pinned }),
      });
      state.conversation = data.conversation;
      await refreshConversations();
      toast(data.conversation.pinned ? "Pinned to the top." : "Unpinned.");
    } else if (action === "clear") {
      const ok = await askConfirm(
        "Clear every message?",
        `This empties “${el.title.textContent}” but keeps the conversation itself — its title, model and persona all stay, and it stays in the sidebar. To remove it entirely, use Delete conversation instead.`,
        "Clear messages"
      );
      if (!ok) return;
      await api("/api/reset", {
        method: "POST",
        body: JSON.stringify({ conversation_id: state.activeId }),
      });
      state.messages = [];
      el.arrayLength.textContent = "0";
      renderThread();
      setContext(null);
      await refreshConversations();
      toast("Messages cleared.");
    } else if (action === "delete") {
      const ok = await askConfirm(
        "Delete this conversation?",
        `“${el.title.textContent}” and all ${state.messages.length} of its messages will be removed from the database, and it will disappear from the sidebar. This can't be undone.`,
        "Delete"
      );
      if (!ok) return;
      await api(`/api/conversations/${state.activeId}`, { method: "DELETE" });
      state.activeId = null;
      await refreshConversations();
      const next = state.conversations[0];
      if (next) openConversation(next.id);
      else startBlankConversation();
      toast("Conversation deleted.");
    }
  } catch (err) { toast(err.message, "error"); }
});

/* ============================== export ============================== */

function download(filename, text, type) {
  const blob = new Blob([text], { type });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

function safeName() {
  return el.title.textContent.replace(/[^\w\- ]+/g, "").trim() || "conversation";
}

function exportJson() {
  if (!state.messages.length) { toast("Nothing to export yet.", "error"); return; }
  const payload = {
    title: el.title.textContent,
    model: state.conversation?.model || state.config.default_model,
    exported_at: new Date().toISOString(),
    history: state.messages.map(({ role, text, created_at }) => ({ role, text, created_at })),
  };
  download(`${safeName()}.json`, JSON.stringify(payload, null, 2), "application/json");
}

function exportMarkdown() {
  if (!state.messages.length) { toast("Nothing to export yet.", "error"); return; }
  const lines = [
    `# ${el.title.textContent}`,
    "",
    `*${state.messages.length} messages · exported ${new Date().toLocaleString()}*`,
    "",
    "---",
    "",
  ];
  for (const m of state.messages) {
    lines.push(`### ${m.role === "user" ? "You" : "Assistant"}`, "", m.text, "");
  }
  download(`${safeName()}.md`, lines.join("\n"), "text/markdown");
}

/* ========================= settings (model/persona) ========================= */

function openSettings() {
  if (!state.conversation) { toast("Send a message first.", "error"); return; }
  state.pendingModel = state.conversation.model || state.config.default_model;
  el.personaInput.value = state.conversation.system_prompt || "";
  el.personaInput.placeholder = state.config.default_system_prompt || "Default assistant persona…";
  renderModelList();
  el.settingsModal.hidden = false;
}

function renderModelList() {
  el.modelList.innerHTML = "";
  for (const model of state.config.models) {
    const option = document.createElement("button");
    option.type = "button";
    option.className = "model-option" + (model.id === state.pendingModel ? " selected" : "");
    option.innerHTML =
      `<span class="model-radio"></span>` +
      `<span class="model-meta"><span class="model-name"></span><span class="model-blurb"></span></span>` +
      `<span class="model-id"></span>`;
    option.querySelector(".model-name").textContent = model.name;
    option.querySelector(".model-blurb").textContent = model.blurb;
    option.querySelector(".model-id").textContent = model.id;
    option.addEventListener("click", () => {
      state.pendingModel = model.id;
      renderModelList();
    });
    el.modelList.appendChild(option);
  }
}

el.settingsClose.addEventListener("click", () => { el.settingsModal.hidden = true; });
el.settingsModal.addEventListener("click", (e) => {
  if (e.target === el.settingsModal) el.settingsModal.hidden = true;
});
el.personaReset.addEventListener("click", () => { el.personaInput.value = ""; });

el.settingsSave.addEventListener("click", async () => {
  try {
    const data = await api(`/api/conversations/${state.activeId}`, {
      method: "PATCH",
      body: JSON.stringify({
        model: state.pendingModel,
        system_prompt: el.personaInput.value,
      }),
    });
    state.conversation = data.conversation;
    setModelBadge(data.conversation.model);
    el.settingsModal.hidden = true;
    refreshContext();
    toast("Saved — applies from your next message.");
  } catch (err) { toast(err.message, "error"); }
});

/* ============================== memory ============================== */

async function refreshMemories() {
  try {
    const data = await api("/api/memories");
    state.memories = data.memories;
    el.memoryCount.textContent = data.memories.length;
    if (!el.memoryModal.hidden) renderMemories();
  } catch { /* leave the count as-is */ }
}

function renderMemories() {
  el.memoryList.innerHTML = "";

  if (!state.memories.length) {
    el.memoryList.innerHTML =
      `<p class="memory-empty">Nothing learned yet.<br>` +
      `Mention something durable — your name, what you're building, a preference — ` +
      `and it'll show up here after the reply.</p>`;
    return;
  }

  for (const memory of state.memories) {
    const item = document.createElement("div");
    item.className = "memory-item";

    const dot = document.createElement("span");
    dot.className = "memory-dot";

    const text = document.createElement("div");
    text.className = "memory-text";
    text.textContent = memory.text;
    const when = document.createElement("span");
    when.className = "memory-when";
    when.textContent = new Date(memory.created_at * 1000).toLocaleString();
    text.appendChild(when);

    const del = document.createElement("button");
    del.type = "button";
    del.className = "memory-del";
    del.title = "Forget this";
    del.innerHTML = icons.trash;
    del.addEventListener("click", async () => {
      try {
        await api(`/api/memories/${memory.id}`, { method: "DELETE" });
        await refreshMemories();
        renderMemories();
      } catch (err) { toast(err.message, "error"); }
    });

    item.append(dot, text, del);
    el.memoryList.appendChild(item);
  }
}

el.memoryBtn.addEventListener("click", async () => {
  el.memoryModal.hidden = false;
  el.rememberToggle.checked = prefs.get("remember", true);
  await refreshMemories();
  renderMemories();
  closeSidebar();
});

el.memoryClose.addEventListener("click", () => { el.memoryModal.hidden = true; });
el.memoryModal.addEventListener("click", (e) => {
  if (e.target === el.memoryModal) el.memoryModal.hidden = true;
});

el.memoryAdd.addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = el.memoryInput.value.trim();
  if (!text) return;
  try {
    await api("/api/memories", { method: "POST", body: JSON.stringify({ text }) });
    el.memoryInput.value = "";
    await refreshMemories();
    renderMemories();
  } catch (err) { toast(err.message, "error"); }
});

el.memoryClear.addEventListener("click", async () => {
  const ok = await askConfirm(
    "Forget everything?",
    `All ${state.memories.length} remembered facts will be deleted. Your conversations stay exactly as they are — only the cross-chat memory is wiped.`,
    "Forget everything"
  );
  if (!ok) return;
  try {
    await api("/api/memories", { method: "DELETE" });
    await refreshMemories();
    renderMemories();
    toast("Memory wiped.");
  } catch (err) { toast(err.message, "error"); }
});

el.memoryDedupe.addEventListener("click", async () => {
  try {
    const data = await api("/api/memories/dedupe", { method: "POST" });
    await refreshMemories();
    renderMemories();
    toast(data.removed
      ? `Merged ${data.removed} duplicate${data.removed === 1 ? "" : "s"}.`
      : "Nothing to merge — no duplicates found.");
  } catch (err) { toast(err.message, "error"); }
});

el.rememberToggle.addEventListener("change", () => {
  prefs.set("remember", el.rememberToggle.checked);
  toast(el.rememberToggle.checked
    ? "Will keep learning from new messages."
    : "Stopped learning — existing memories are kept.");
});

/* ============================== search ============================== */

function openSearch() {
  el.searchModal.hidden = false;
  el.searchInput.value = "";
  el.searchResults.innerHTML =
    `<p class="search-hint">Type at least 2 characters. Results come straight out of the SQLite history.</p>`;
  state.searchRows = [];
  state.searchCursor = -1;
  setTimeout(() => el.searchInput.focus(), 20);
}

function closeSearch() { el.searchModal.hidden = true; }

el.deepSearch.addEventListener("click", openSearch);
el.searchModal.addEventListener("click", (e) => {
  if (e.target === el.searchModal) closeSearch();
});

let searchTimer = null;
el.searchInput.addEventListener("input", () => {
  clearTimeout(searchTimer);
  const q = el.searchInput.value.trim();
  if (q.length < 2) {
    el.searchResults.innerHTML = `<p class="search-hint">Keep typing…</p>`;
    state.searchRows = [];
    return;
  }
  searchTimer = setTimeout(() => runSearch(q), 170);
});

async function runSearch(q) {
  try {
    const data = await api(`/api/search?q=${encodeURIComponent(q)}`);
    renderSearchResults(data.results);
  } catch (err) {
    el.searchResults.innerHTML = "";
    const p = document.createElement("p");
    p.className = "search-empty";
    p.textContent = err.message;
    el.searchResults.appendChild(p);
  }
}

function renderSearchResults(results) {
  el.searchResults.innerHTML = "";
  state.searchRows = [];
  state.searchCursor = -1;

  if (!results.length) {
    el.searchResults.innerHTML = `<p class="search-empty">No messages matched.</p>`;
    return;
  }

  const count = document.createElement("div");
  count.className = "search-count";
  count.textContent = `${results.length} match${results.length === 1 ? "" : "es"}`;
  el.searchResults.appendChild(count);

  for (const r of results) {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "result";

    const top = document.createElement("div");
    top.className = "result-top";
    const role = document.createElement("span");
    role.className = "result-role" + (r.role === "model" ? " model" : "");
    role.textContent = r.role === "model" ? "assistant" : "you";
    const conv = document.createElement("span");
    conv.className = "result-conv";
    conv.textContent = r.conversation_title;
    const when = document.createElement("span");
    when.textContent = new Date(r.created_at * 1000).toLocaleDateString(undefined, {
      month: "short", day: "numeric",
    });
    top.append(role, conv, when);

    const snippet = document.createElement("div");
    snippet.className = "result-snippet";
    // The backend marks matches with «» so we can highlight without ever
    // trusting raw HTML from stored messages.
    snippet.innerHTML = MD.escapeHtml(r.snippet)
      .replace(/«/g, "<mark>")
      .replace(/»/g, "</mark>");

    row.append(top, snippet);
    row.addEventListener("click", () => jumpTo(r.conversation_id, r.id));
    el.searchResults.appendChild(row);
    state.searchRows.push(row);
  }
}

async function jumpTo(conversationId, messageId) {
  closeSearch();
  if (conversationId !== state.activeId || state.incognitoId) {
    await openConversation(conversationId);
  }
  requestAnimationFrame(() => {
    const node = el.thread.querySelector(`[data-message-id="${messageId}"]`);
    if (!node) return;
    node.scrollIntoView({ block: "center", behavior: "smooth" });
    node.classList.add("highlight");
    setTimeout(() => node.classList.remove("highlight"), 2000);
  });
}

el.searchInput.addEventListener("keydown", (e) => {
  if (!state.searchRows.length) return;
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    const dir = e.key === "ArrowDown" ? 1 : -1;
    state.searchRows[state.searchCursor]?.classList.remove("cursor");
    state.searchCursor = (state.searchCursor + dir + state.searchRows.length) % state.searchRows.length;
    const row = state.searchRows[state.searchCursor];
    row.classList.add("cursor");
    row.scrollIntoView({ block: "nearest" });
  } else if (e.key === "Enter" && state.searchCursor >= 0) {
    e.preventDefault();
    state.searchRows[state.searchCursor].click();
  }
});

/* ============================ voice input ============================ */
// Web Speech API — built into Chrome/Edge, free, no key, no quota. Firefox
// doesn't ship it, so the button hides itself rather than lying.

const SpeechRecognitionAPI = window.SpeechRecognition || window.webkitSpeechRecognition;
let recognition = null;
let dictationBase = "";

if (!SpeechRecognitionAPI) {
  el.micBtn.hidden = true;
} else {
  recognition = new SpeechRecognitionAPI();
  recognition.continuous = true;
  recognition.interimResults = true;
  recognition.lang = navigator.language || "en-US";

  recognition.addEventListener("result", (e) => {
    let settled = "";
    let pending = "";
    for (let i = e.resultIndex; i < e.results.length; i++) {
      const chunk = e.results[i][0].transcript;
      if (e.results[i].isFinal) settled += chunk;
      else pending += chunk;
    }
    if (settled) dictationBase = (dictationBase + " " + settled).trim();
    el.input.value = (dictationBase + " " + pending).trim();
    autoGrow();
  });

  recognition.addEventListener("end", () => {
    // Chrome stops on its own after a pause; restart while still armed.
    if (state.listening) { try { recognition.start(); } catch { stopListening(); } }
  });

  recognition.addEventListener("error", (e) => {
    const message = e.error === "not-allowed"
      ? "Microphone access was blocked. Allow it in the address bar to dictate."
      : `Dictation stopped: ${e.error}`;
    stopListening();
    toast(message, "error");
  });
}

function startListening() {
  if (!recognition || state.listening) return;
  dictationBase = el.input.value.trim();
  state.listening = true;
  el.micBtn.setAttribute("aria-pressed", "true");
  try {
    recognition.start();
    toast("Listening… click the mic again to stop.");
  } catch {
    stopListening();
  }
}

function stopListening() {
  if (!recognition || !state.listening) return;
  state.listening = false;
  el.micBtn.setAttribute("aria-pressed", "false");
  try { recognition.stop(); } catch { /* already stopped */ }
  el.input.focus();
}

el.micBtn.addEventListener("click", () => {
  if (state.listening) stopListening();
  else startListening();
});

/* ================================ TTS ================================ */

el.autospeak.addEventListener("click", () => {
  const next = !prefs.get("autospeak", false);
  prefs.set("autospeak", next);
  el.autospeak.setAttribute("aria-pressed", String(next));
  if (!next) stopSpeech();
  toast(next ? "Replies will be read out loud." : "Auto-speak off.");
});

function stopSpeech() {
  el.audio.pause();
  el.audio.removeAttribute("src");
  if (window.speechSynthesis) window.speechSynthesis.cancel();
  document.querySelectorAll(".msg-action.speak").forEach(resetSpeakButton);
  state.speakingId = null;
}

function resetSpeakButton(btn) {
  btn.classList.remove("playing", "loading");
  btn.closest(".msg-actions")?.classList.remove("busy");
  btn.innerHTML = `${icons.speaker}<span>Listen</span>`;
}

function setSpeakButton(btn, mode) {
  btn.closest(".msg-actions")?.classList.add("busy");
  if (mode === "loading") {
    btn.classList.add("loading");
    btn.classList.remove("playing");
    btn.innerHTML = `${icons.spinner}<span>Loading</span>`;
  } else {
    btn.classList.remove("loading");
    btn.classList.add("playing");
    btn.innerHTML = `${icons.stop}<span>Stop</span>`;
  }
}

async function toggleSpeech(msg, btn, bubble) {
  if (!btn) return;
  const token = msg.id || bubble;

  if (state.speakingId === token) { stopSpeech(); return; }
  stopSpeech();
  state.speakingId = token;
  setSpeakButton(btn, "loading");

  try {
    const res = await fetch("/api/tts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        text: msg.text,
        voice: prefs.get("voice", null) || undefined,
        rate: prefs.get("rate", 0),
        pitch: prefs.get("pitch", 0),
      }),
    });

    if (res.status === 503) {
      const data = await res.json().catch(() => ({}));
      browserSpeak(msg.text, btn);
      toast((data.error || "Voice service unavailable") + " — using the browser's built-in voice.", "error");
      return;
    }
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.error || "Couldn't generate audio.");
    }

    const blob = await res.blob();
    if (state.speakingId !== token) return; // cancelled while it rendered

    const url = URL.createObjectURL(blob);
    el.audio.src = url;
    el.audio.onended = () => { URL.revokeObjectURL(url); resetSpeakButton(btn); state.speakingId = null; };
    el.audio.onerror = () => { URL.revokeObjectURL(url); resetSpeakButton(btn); state.speakingId = null; };
    await el.audio.play();
    setSpeakButton(btn, "playing");
  } catch (err) {
    resetSpeakButton(btn);
    state.speakingId = null;
    toast(err.message, "error");
  }
}

// Last-resort fallback: the browser's own speech engine. Always available
// offline, just less pleasant to listen to.
function browserSpeak(text, btn) {
  if (!window.speechSynthesis) { resetSpeakButton(btn); return; }
  const utter = new SpeechSynthesisUtterance(text.replace(/```[\s\S]*?```/g, " code block "));
  utter.onend = () => { resetSpeakButton(btn); state.speakingId = null; };
  utter.onerror = () => { resetSpeakButton(btn); state.speakingId = null; };
  setSpeakButton(btn, "playing");
  window.speechSynthesis.speak(utter);
}

/* ---------------------------- voice picker ---------------------------- */

async function loadVoices() {
  try {
    const data = await api("/api/voices");
    state.voices = data.voices;
    if (!prefs.get("voice", null)) prefs.set("voice", data.default);
    if (!data.available) {
      el.voiceSourceNote.textContent =
        "edge-tts isn't installed — falling back to the browser's built-in voices. Run: pip install -r requirements.txt";
    }
    renderVoiceList();
  } catch { /* voice picker just stays empty */ }
}

function renderVoiceList() {
  el.voiceList.innerHTML = "";
  const selected = prefs.get("voice", null);

  for (const voice of state.voices) {
    const option = document.createElement("button");
    option.type = "button";
    option.className = "voice-option" + (voice.id === selected ? " selected" : "");

    const dot = document.createElement("span");
    dot.className = "voice-dot";
    dot.textContent = voice.name.slice(0, 1);

    const meta = document.createElement("span");
    meta.className = "voice-meta";
    meta.innerHTML = `<span class="voice-name"></span><span class="voice-vibe"></span>`;
    meta.querySelector(".voice-name").textContent = `${voice.name} · ${voice.accent}`;
    meta.querySelector(".voice-vibe").textContent = voice.vibe;

    option.append(dot, meta);
    option.addEventListener("click", () => {
      prefs.set("voice", voice.id);
      renderVoiceList();
    });
    el.voiceList.appendChild(option);
  }
}

el.voiceBtn.addEventListener("click", () => { el.voiceModal.hidden = false; });
el.voiceClose.addEventListener("click", () => { el.voiceModal.hidden = true; });
el.voiceModal.addEventListener("click", (e) => {
  if (e.target === el.voiceModal) el.voiceModal.hidden = true;
});

function syncRangeLabels() {
  el.rateValue.textContent = `${el.rateRange.value > 0 ? "+" : ""}${el.rateRange.value}%`;
  el.pitchValue.textContent = `${el.pitchRange.value > 0 ? "+" : ""}${el.pitchRange.value}Hz`;
}

el.rateRange.addEventListener("input", () => {
  prefs.set("rate", Number(el.rateRange.value));
  syncRangeLabels();
});
el.pitchRange.addEventListener("input", () => {
  prefs.set("pitch", Number(el.pitchRange.value));
  syncRangeLabels();
});

el.voicePreview.addEventListener("click", () => {
  const voice = state.voices.find((v) => v.id === prefs.get("voice", null));
  const sample = {
    role: "model",
    text: `Hi, I'm ${voice ? voice.name : "your assistant"}. This is how I'll read your conversation back to you.`,
  };
  const fake = document.createElement("button");
  fake.className = "msg-action speak";
  toggleSpeech(sample, fake, fake);
});

/* ============================== shortcuts ============================== */

document.addEventListener("keydown", (e) => {
  const mod = e.ctrlKey || e.metaKey;

  if (mod && e.shiftKey && e.key.toLowerCase() === "m") {
    e.preventDefault();
    if (!el.micBtn.hidden) el.micBtn.click();
  } else if (mod && e.key.toLowerCase() === "k") {
    e.preventDefault();
    openSearch();
  } else if (mod && e.key.toLowerCase() === "j") {
    e.preventDefault();
    startBlankConversation();
  } else if (e.key === "Escape") {
    if (dialogResolve) closeDialog(null);
    else if (!el.searchModal.hidden) closeSearch();
    else if (!el.voiceModal.hidden) el.voiceModal.hidden = true;
    else if (!el.settingsModal.hidden) el.settingsModal.hidden = true;
    else if (!el.memoryModal.hidden) el.memoryModal.hidden = true;
    else if (!el.menu.hidden) closeMenu();
    else if (state.listening) stopListening();
    else if (state.speakingId) stopSpeech();
  }
});

/* ================================ boot ================================ */

(async function boot() {
  el.autospeak.setAttribute("aria-pressed", String(prefs.get("autospeak", false)));
  el.rateRange.value = prefs.get("rate", 0);
  el.pitchRange.value = prefs.get("pitch", 0);
  syncRangeLabels();

  loadVoices();
  refreshMemories();

  try {
    state.config = await api("/api/config");
  } catch { /* fall back to the badge defaults */ }

  try {
    await refreshConversations();
  } catch {
    toast("Couldn't reach the server. Is app.py running?", "error");
  }

  // Always land on a fresh chat. Dropping someone back into the middle of
  // yesterday's conversation is disorienting, and everything they've saved is
  // one click away in the sidebar.
  startBlankConversation();

  autoGrow();
  el.input.focus();
})();

// An incognito thread must not outlive the tab.
window.addEventListener("beforeunload", () => {
  if (state.incognitoId) {
    fetch(`/api/incognito/${state.incognitoId}`, { method: "DELETE", keepalive: true });
  }
});
