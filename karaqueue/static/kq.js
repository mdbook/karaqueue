// Shared bits for every page: the room code, saved keys, API calls, live updates.

const code = (location.pathname.split("/")[2] || "").toUpperCase();

const keys = {
  get: (room) => {
    try { return JSON.parse(localStorage.getItem("kq:" + room) || "null"); }
    catch (_) { return null; }
  },
  set: (room, value) => localStorage.setItem("kq:" + room, JSON.stringify(value)),
  drop: (room) => localStorage.removeItem("kq:" + room),
};

async function api(method, path, body) {
  const saved = keys.get(code);
  const headers = { "Content-Type": "application/json" };
  if (saved) headers.Authorization = "Bearer " + saved.token;
  let res;
  try {
    res = await fetch("/api" + path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (_) {
    const err = new Error("Can't reach the party. Check you're on the Wi-Fi, then try again.");
    err.status = 0;
    throw err;
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(typeof data.detail === "string" ? data.detail : "Something went wrong");
    err.status = res.status;
    throw err;
  }
  return data;
}

// Calls `refresh` now and whenever the server says the room changed. Reconnects if the socket drops.
// Options: `types` lists which message types trigger a refresh (default just "update"),
// `onopen(ws)` runs on every (re)connect, `onmessage(msg, ws)` sees every message.
function live(refresh, opts = {}) {
  const types = opts.types || ["update"];
  let wait = 500, opened = 0, sock = null, retry = null, gone = false, turnedAway = false;
  const status = (connected) => document.querySelectorAll("[data-connection]").forEach(el => {
    el.textContent = connected ? "Live updates" : "Reconnecting…";
    el.dataset.status = connected ? "online" : "offline";
  });
  const connect = () => {
    clearTimeout(retry);
    const ws = sock = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws/" + code);
    ws.onopen = () => { status(true); opened = Date.now(); if (opts.onopen) opts.onopen(ws); refresh(); };
    ws.onmessage = (e) => {
      let msg = {};
      try { msg = JSON.parse(e.data); } catch (_) { return; }
      if (types.includes(msg.type)) refresh(msg);
      if (opts.onmessage) opts.onmessage(msg, ws);
    };
    ws.onclose = (e) => {
      status(false);
      if (e.code === 4404) { gone = true; return refresh(); }
      // The server accepts and then shuts sockets it won't keep, so only a socket that stayed up
      // resets the wait. Too many from this address (4429) or in the room (1013) waits longer.
      if (opened && Date.now() - opened > 10000) wait = 500;
      opened = 0;
      turnedAway = e.code === 4429 || e.code === 1013;
      retry = setTimeout(connect, e.code === 4429 || e.code === 1013 ? Math.max(wait, 30000) : wait);
      wait = Math.min(wait * 2, 10000);
    };
  };
  // Back on the Wi-Fi, or the phone woke up: reconnect now instead of waiting out the backoff.
  const wake = () => {
    if (gone || turnedAway || document.hidden || (sock && sock.readyState <= WebSocket.OPEN)) return;
    wait = 500;
    connect();
  };
  addEventListener("online", wake);
  document.addEventListener("visibilitychange", wake);
  refresh();
  connect();
}

// Tiny DOM builder so user text always goes in as text, never as HTML.
function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (v !== false && v != null) el.setAttribute(k, v === true ? "" : v);
  }
  for (const child of children.flat()) {
    if (child != null && child !== false) el.append(child.nodeType ? child : String(child));
  }
  return el;
}

// Text that can play the karaoke wipe (see .wipe in style.css). Add "sung" to the class to show it filled.
function wipeText(tag, cls, text) {
  return h(tag, { class: cls + " wipe", "data-text": text }, text);
}

function show(id, ...children) {
  document.getElementById(id).replaceChildren(...children.flat().filter((c) => c != null && c !== false));
}

// Shows a toast. `kind` is "error" (default) or "ok".
function flash(message, kind = "error") {
  const el = document.getElementById("flash");
  el.textContent = message;
  el.className = kind;
  el.hidden = !message;
  if (message) setTimeout(() => { if (el.textContent === message) el.hidden = true; }, 4000);
}

// Wraps an action so errors land in the flash bar instead of the console.
const act = (fn) => async (...args) => {
  try { await fn(...args); } catch (e) { flash(e.message); }
};

function nowPlaying(state) {
  const np = state.now_playing;
  if (!np) return h("p", { class: "muted" }, state.up_next ? "Waiting for the host to start the next song." : "Nobody's singing yet.");
  return h("div", { class: "now" },
    h("div", { class: "label" }, "Now singing"),
    h("div", { class: "singer" }, np.singer),
    h("div", { class: "song" }, np.title, h("span", { class: "by" }, np.artist ? " by " + np.artist : "")));
}

// Clipboard API requires HTTPS; local parties often use a plain HTTP LAN address.
async function copyText(value) {
  if (navigator.clipboard && window.isSecureContext) {
    try { await navigator.clipboard.writeText(value); return; } catch (_) { /* Try the LAN fallback. */ }
  }
  const previous = document.activeElement;
  const field = h("textarea", { "aria-label": "Link to copy", style: "position:fixed;left:-9999px;top:0" }, value);
  document.body.append(field);
  field.select();
  const copied = document.execCommand("copy");
  field.remove();
  if (previous) previous.focus({ preventScroll: true });
  if (!copied) throw new Error("Couldn't copy the link. Try copying it from your browser's address bar.");
}

// Karaoke uploads are usually titled "Artist - Song (Karaoke Version)", so take a guess.
function guessSong(youtubeTitle) {
  const text = new DOMParser().parseFromString(youtubeTitle, "text/html").body.textContent;
  const cleaned = text.replace(/[([][^)\]]*(karaoke|instrumental|lyrics?)[^)\]]*[)\]]/gi, "").replace(/\bkaraoke( version)?\b/gi, "").trim();
  const [first, ...rest] = cleaned.split(/\s+[-|\u2013]\s+/);
  return rest.length ? { artist: first.trim(), title: rest.join(" - ").trim() } : { artist: "", title: first.trim() };
}

// Search YouTube (or paste a link) and save songs into the library for good. The admin Library
// tab and the host page both have the markup: #yt-search with #yt-q, then #yt-results and #saves.
function songSaver({ search, save, list }) {
  const STATE = { waiting: "Waiting its turn", saving: "Saving now", saved: "In the library" };
  let timer = null;
  const refresh = act(async () => {
    const jobs = await list();
    show("saves", jobs.map((j) => h("li", {},
      h("div", { class: "grow" }, h("div", {}, j.title, j.artist ? h("span", { class: "muted" }, " · " + j.artist) : null),
        h("span", { class: "small " + (j.state === "failed" ? "off" : "muted") }, j.state === "failed" ? j.error : STATE[j.state])))));
    clearTimeout(timer);
    if (jobs.some((j) => j.state === "waiting" || j.state === "saving")) timer = setTimeout(refresh, 2000);
  });
  // Check the name before saving: it becomes the file name the library reads.
  function name(li, video, guess) {
    const title = h("input", { value: guess.title, "aria-label": "Song title", placeholder: "Song title", required: true, maxlength: 200 });
    const artist = h("input", { value: guess.artist, "aria-label": "Artist", placeholder: "Artist", maxlength: 200 });
    li.replaceChildren(h("form", { class: "song-edit", onsubmit: act(async (e) => {
      e.preventDefault();
      const job = await save({ video, title: title.value, artist: artist.value });
      flash(job.state === "saved" ? job.title + " is already in the library" : "Saving " + job.title, "ok");
      li.remove();
      refresh();
    }) }, title, artist, h("div", { class: "actions" }, h("button", { class: "small", type: "submit" }, "Save"))));
    title.focus();
  }
  document.getElementById("yt-search").onsubmit = act(async (e) => {
    e.preventDefault();
    const q = document.getElementById("yt-q").value.trim();
    if (!q) return;
    if (/youtu\.?be/i.test(q)) {
      const li = h("li", {});
      show("yt-results", li);
      name(li, q, { title: "", artist: "" });
      return;
    }
    const button = e.target.querySelector("button");
    button.disabled = true;
    let hits;
    try { hits = await search(q); } finally { button.disabled = false; }
    show("yt-results", hits.length ? hits.map((r) => {
      const li = h("li", {},
        h("div", { class: "grow" }, h("div", {}, guessSong(r.title).title || r.title), h("span", { class: "muted small" }, r.channel)),
        h("button", { class: "ghost small", type: "button", onclick: () => name(li, r.video_id, guessSong(r.title)) }, "Save"));
      return li;
    }) : h("li", { class: "empty muted" }, "Nothing found. Try fewer words."));
  });
  refresh();
  return refresh;
}
