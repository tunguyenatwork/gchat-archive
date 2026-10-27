/* Offline Google Chat archive — vanilla JS, no build step, no network. */
(() => {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  const el = {
    search: $("#search"),
    spaces: $("#spaces"),
    messages: $("#messages"),
    results: $("#results"),
    pane: $("#pane"),
    empty: $("#empty"),
    title: $("#conv-title"),
    badge: $("#conv-badge"),
    meta: $("#conv-meta"),
    stats: $("#stats"),
    acct: $("#acct"),
    toast: $("#toast"),
    category: $("#category"),
    attach: $("#attach"),
    theme: $("#theme"),
    dateFrom: $("#date-from"),
    dateTo: $("#date-to"),
    dateClear: $("#date-clear"),
    suggest: $("#suggest"),
    menu: $("#conv-menu"),
    actions: $("#conv-actions"),
    actExclude: $("#act-exclude"),
    actDelete: $("#act-delete"),
    actSelect: $("#act-select"),
    actRange: $("#act-range"),
    rangePanel: $("#range-panel"),
    rangeFrom: $("#range-from"),
    rangeTo: $("#range-to"),
    rangeAll: $("#range-all"),
    rangeStatus: $("#range-status"),
    rangeCount: $("#range-count"),
    rangeDelete: $("#range-delete"),
    selbar: $("#selbar"),
    selcount: $("#selcount"),
    selDelete: $("#sel-delete"),
    selAll: $("#sel-all"),
    selCancel: $("#sel-cancel"),
    spaceSelect: $("#space-select"),
    spacebar: $("#spacebar"),
    spacecount: $("#spacecount"),
    spaceExclude: $("#space-exclude"),
    spaceDelete: $("#space-delete"),
    spaceDeleteAll: $("#space-delete-all"),
    spaceDone: $("#space-done"),
    modal: $("#modal"),
    modalTitle: $("#modal-title"),
    modalBody: $("#modal-body"),
    modalClose: $("#modal-close"),
    navToggle: $("#nav-toggle"),
    scrim: $("#scrim"),
    settingsBtn: $("#settings-btn"),
    syncNow: $("#sync-now"),
    syncState: $("#sync-state"),
    accountFilter: $("#account-filter"),
  };

  const state = {
    meta: {},
    spaces: [],
    category: "all",
    attFilter: "any",
    dateFrom: "",
    dateTo: "",
    accountScope: "active",
    collapsed: new Set(),
    space: null,
    spaceMeta: null,
    messages: [],
    hasMore: false,
    loading: false,
    mode: "idle", // idle | chat | search
    searchOffset: 0,
    query: "",
    sort: "rank", // rank | time
    suggestIndex: -1,
    modalMessage: null,
    selecting: false,
    selected: new Set(),
    lastPicked: null,
    picked: new Map(),   // search results: "<account>\u0000<id>" -> {account, id}
    pickAnchor: null,
    selectingSpaces: false,
    pickedSpaces: new Set(),
    spaceAnchor: null,
  };

  // ---------- helpers ----------
  const esc = (s) =>
    String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");

  const URL_RE = /\b(https?:\/\/[^\s<>"']+)/g;
  const MENTION_RE = /(^|\s)(@[A-Z][\w.'-]+(?: [A-Z][\w.'-]+)?)/g;

  function linkify(text) {
    return text
      .split(URL_RE)
      .map((chunk, i) =>
        i % 2
          ? `<a href="${esc(chunk)}" target="_blank" rel="noopener noreferrer">${esc(chunk)}</a>`
          : chunk
      )
      .join("");
  }

  function renderText(raw) {
    let safe = esc(raw);
    safe = safe.replace(MENTION_RE, (m, pre, mention) => `${pre}<span class="mention">${mention}</span>`);
    return linkify(safe);
  }

  const timeFmt = new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" });
  const dateFmt = new Intl.DateTimeFormat(undefined, { weekday: "long", year: "numeric", month: "short", day: "numeric" });
  const shortFmt = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", year: "numeric" });

  const parse = (iso) => (iso ? new Date(iso) : null);
  const dayKey = (d) => (d ? `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}` : "");

  function relTime(iso) {
    const d = parse(iso);
    if (!d) return "";
    const diff = (Date.now() - d.getTime()) / 1000;
    if (diff < 60) return "just now";
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
    if (diff < 86400 * 7) return `${Math.floor(diff / 86400)}d ago`;
    return shortFmt.format(d);
  }

  function initials(name) {
    if (!name) return "?";
    if (name.startsWith("users/")) return "?";
    const parts = name.replace(/\(.*?\)/g, "").trim().split(/\s+/).filter(Boolean);
    if (!parts.length) return "?";
    return (parts[0][0] + (parts.length > 1 ? parts[parts.length - 1][0] : "")).toUpperCase();
  }

  const plural = (n, word) => `${n.toLocaleString()} ${word}${n === 1 ? "" : "s"}`;

  function toast(msg) {
    el.toast.textContent = msg;
    el.toast.classList.remove("hidden");
    clearTimeout(toast.t);
    toast.t = setTimeout(() => el.toast.classList.add("hidden"), 2600);
  }

  async function api(path) {
    const res = await fetch(path);
    if (res.status === 401) {
      location.href = "/login";
      throw new Error("authentication required");
    }
    if (!res.ok) {
      const body = await res.text().catch(() => "");
      throw new Error(`${res.status} ${body.slice(0, 160)}`);
    }
    return res.json();
  }

  async function post(path, payload) {
    const res = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload || {}),
    });
    if (res.status === 401) {
      location.href = "/login";
      throw new Error("authentication required");
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `${res.status}`);
    return data;
  }

  function fmtBytes(n) {
    if (!n) return "";
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
    return `${(n / 1024 / 1024).toFixed(1)} MB`;
  }

  function accountQuery() {
    return state.accountScope && state.accountScope !== "active"
      ? `&account=${encodeURIComponent(state.accountScope)}`
      : "";
  }

  function dateQuery() {
    const parts = [];
    if (state.dateFrom) parts.push(`from=${encodeURIComponent(state.dateFrom)}`);
    if (state.dateTo) parts.push(`to=${encodeURIComponent(state.dateTo)}`);
    return parts.length ? "&" + parts.join("&") : "";
  }

  const typeLabel = { SPACE: "Space", GROUP_CHAT: "Group", DIRECT_MESSAGE: "DM" };
  const typeIcon = { SPACE: "#", GROUP_CHAT: "G", DIRECT_MESSAGE: "@" };

  // ---------- sidebar ----------
  const CATS = [
    { key: "SPACE", title: "Spaces" },
    { key: "GROUP_CHAT", title: "Group chats" },
    { key: "DIRECT_MESSAGE", title: "Direct messages" },
    { key: "BOT", title: "Bots" },
    { key: "OTHER", title: "Other" },
  ];

  function categoryOf(s) {
    if (s.bot_only) return "BOT";
    if (s.type === "SPACE" || s.type === "GROUP_CHAT" || s.type === "DIRECT_MESSAGE") return s.type;
    return "OTHER";
  }

  function spaceHTML(s) {
    const picked = state.pickedSpaces.has(s.id);
    const check = state.selectingSpaces
      ? `<span class="s-check"><input type="checkbox" data-sel-space="${esc(s.id)}" ${picked ? "checked" : ""}></span>`
      : "";
    return `<div class="space${state.space === s.id ? " active" : ""}${s.excluded ? " excluded" : ""}${state.selectingSpaces ? " selecting" : ""}${picked ? " picked" : ""}" data-space="${esc(s.id)}">
      ${check}
      <span class="icon">${typeIcon[categoryOf(s)] || "•"}</span>
      <span class="name" title="${esc(s.label || s.id)}${s.excluded ? " (excluded from sync)" : ""}">${esc(s.label || s.id)}${s.excluded ? ' <span class="pin">⏸</span>' : ""}</span>
      <span class="n">${s.msg_count ? s.msg_count.toLocaleString() : ""}</span>
    </div>`;
  }

  function renderSpaces() {
    const q = el.search.value.trim().toLowerCase();
    const matches = (s) => {
      if (q && state.mode !== "search" && !(s.label || "").toLowerCase().includes(q)) return false;
      return true;
    };

    if (state.category !== "all") {
      const rows = state.spaces.filter((s) => categoryOf(s) === state.category && matches(s));
      el.spaces.innerHTML =
        rows.map(spaceHTML).join("") ||
        `<div class="subtle" style="padding:14px 10px;font-size:12px">Nothing in this category.</div>`;
      return;
    }

    // "All": one collapsible section per category, newest first inside each.
    const parts = [];
    for (const cat of CATS) {
      const rows = state.spaces.filter((s) => categoryOf(s) === cat.key && matches(s));
      if (!rows.length) continue;
      const collapsed = state.collapsed.has(cat.key);
      const withMsgs = rows.filter((s) => s.msg_count > 0).length;
      parts.push(
        `<div class="sect${collapsed ? " collapsed" : ""}" data-cat="${cat.key}">
           <span class="caret">▾</span><span class="sect-title">${cat.title}</span>
           <span class="cn" title="${withMsgs} with messages">${rows.length}</span>
         </div>`
      );
      if (!collapsed) parts.push(rows.map(spaceHTML).join(""));
    }
    el.spaces.innerHTML =
      parts.join("") ||
      `<div class="subtle" style="padding:14px 10px;font-size:12px">No conversations match.</div>`;
  }

  function renderStats() {
    const m = state.meta;
    el.stats.innerHTML =
      `<div>${(m.messages || 0).toLocaleString()} messages · ${(m.spaces || 0).toLocaleString()} conversations</div>` +
      `<div>indexed ${esc(m.built_at || "?")} · ${((m.db_bytes || 0) / 1e6).toFixed(1)} MB</div>`;
    el.acct.textContent = m.me ? `${m.me} · offline` : "offline";
  }


  // ---------- one-off sync (sidebar button) ----------
  let syncPoll = null;

  function syncTotals() {
    const m = state.meta || {};
    if (!m.messages) return "";
    const bits = [`${m.messages.toLocaleString()} messages`];
    if (m.spaces) bits.push(`${m.spaces.toLocaleString()} conversations`);
    return bits.join(" · ");
  }

  function renderSync(status) {
    const s = status || {};
    const totals = syncTotals();

    el.syncNow.disabled = !!s.running;
    el.syncNow.textContent = s.running ? "Syncing…" : "Sync now";

    let text = "";
    let kind = "idle";
    const rc = s.returncode;
    if (s.running) {
      kind = "busy";
      text = `⟳ in progress · ${Math.max(1, Math.round(s.seconds || 0))}s`;
    } else if (rc !== null && rc !== undefined && rc !== 0) {
      kind = "fail";
      text = `✗ failed (exit ${rc})`;
    } else if (rc === 0) {
      kind = "ok";
      const when = s.finished_at ? relTime(s.finished_at) : "just now";
      text = `✓ synced ${when} · ${Math.round(s.seconds || 0)}s`;
    } else if (s.last_run_at) {
      kind = s.last_ok === false ? "fail" : "ok";
      text = `${s.last_ok === false ? "✗" : "✓"} last sync ${relTime(s.last_run_at)}`;
    } else {
      text = "never synced here";
    }

    el.syncState.textContent = totals ? `${text} · ${totals}` : text;
    el.syncState.className = "subtle sync-" + kind;
    el.syncState.title = s.last_detail || s.command || "";
  }

  async function pollSync() {
    try {
      const status = await api("/api/sync/status");
      renderSync(status);
      if (status.running) {
        if (!syncPoll) syncPoll = setInterval(pollSync, 2000);
      } else {
        clearInterval(syncPoll);
        syncPoll = null;
        await refreshStats();
        renderSync(status);
      }
    } catch {
      clearInterval(syncPoll);
      syncPoll = null;
    }
  }

  async function startOneOffSync(mode) {
    if (el.syncNow.disabled) return;
    el.syncNow.disabled = true;
    el.syncState.textContent = "starting…";
    try {
      const res = await post("/api/sync/start" + (mode === "check-all" ? "?mode=check-all" : ""), {});
      renderSync(res.status);
      toast(mode === "check-all" ? "Re-checking every conversation" : "Sync started");
      pollSync();
    } catch (err) {
      const busy = /already running/i.test(err.message || "");
      toast(busy ? "A sync is already running" : `Could not start sync: ${err.message}`);
      pollSync();
    }
  }

  if (el.syncNow) {
    el.syncNow.addEventListener("click", () => startOneOffSync());
    // Paint the bar on load, and keep polling if a sync is already running.
    api("/api/sync/status")
      .then((status) => {
        renderSync(status);
        if (status.running) pollSync();
      })
      .catch(() => {});
  }

  async function refreshStats() {
    try {
      state.meta = await api("/api/meta");
      renderStats();
    } catch {
      /* stats are cosmetic; ignore */
    }
  }

  // ---------- conversation ----------
  function messageHTML(m, prev) {
    const d = parse(m.time);
    const pd = prev ? parse(prev.time) : null;
    const sameSenderAsPrev =
      prev && prev.sender === m.sender && pd && d && d - pd < 5 * 60 * 1000 && dayKey(d) === dayKey(pd);
    const edited = m.updated && m.time && m.updated.slice(0, 19) !== m.time.slice(0, 19);
    const quoted = m.quoted
      ? `<div class="quoted"><b>${esc(m.quoted.sender || "")}</b><span>${esc(m.quoted.text || "")}</span></div>`
      : "";
    const attach = m.attachments && m.attachments.length
      ? `<div class="attach">${m.attachments
          .map(
            (a) =>
              `<button class="chip" data-att-msg="${esc(m.id)}" title="${esc(a.type || "")}">📎 ${esc(
                a.name || a.type || "attachment"
              )}</button>`
          )
          .join("")}</div>`
      : "";
    const reacts = m.reactions && m.reactions.length
      ? `<div class="reacts">${m.reactions
          .map((r) => `<span class="r">${esc(r.emoji || "·")}${r.count ? " " + r.count : ""}</span>`)
          .join("")}</div>`
      : "";

    const picked = state.selecting && state.selected.has(m.id);
    const check = state.selecting
      ? `<label class="msg-check"><input type="checkbox" data-sel-msg="${esc(m.id)}" ${picked ? "checked" : ""}></label>`
      : "";
    return `<div class="msg${m.mine ? " mine" : ""}${sameSenderAsPrev ? " cont" : ""}${m.deleted ? " deleted" : ""}${state.selecting ? " selecting" : ""}${picked ? " picked" : ""}" data-id="${esc(m.id)}">
      ${check}
      <button class="msg-del" data-del-msg="${esc(m.id)}" title="Delete this message from the archive">✕</button>
      <div class="avatar">${sameSenderAsPrev ? "" : esc(initials(m.sender))}</div>
      <div class="body">
        ${
          sameSenderAsPrev
            ? ""
            : `<div class="meta"><span class="who">${esc(m.sender)}</span><span class="time">${d ? timeFmt.format(d) : ""}</span>${edited ? `<span class="edited">edited</span>` : ""}</div>`
        }
        ${quoted}
        <div class="text">${m.deleted ? "<em>message deleted</em>" : renderText(m.text || "")}</div>
        ${attach}${reacts}
      </div>
    </div>`;
  }

  function renderMessages({ stickToBottom = false, keepScroll = false } = {}) {
    const pane = el.pane;
    const prevHeight = pane.scrollHeight;
    const prevTop = pane.scrollTop;

    const parts = [];
    let currentDay = "";
    let prev = null;
    for (const m of state.messages) {
      const d = parse(m.time);
      const key = dayKey(d);
      if (key !== currentDay) {
        currentDay = key;
        parts.push(`<div class="daysep">${d ? dateFmt.format(d) : ""}</div>`);
        prev = null;
      }
      parts.push(messageHTML(m, prev));
      prev = m;
    }
    el.messages.innerHTML = parts.join("");

    if (stickToBottom) pane.scrollTop = pane.scrollHeight;
    else if (keepScroll) pane.scrollTop = prevTop + (pane.scrollHeight - prevHeight);
  }

  function updateHeader() {
    if (state.mode === "search") {
      el.title.textContent = state.query ? `Search: ${state.query}` : "Search";
      el.badge.textContent = "";
      el.meta.textContent = "matching messages across all conversations";
      el.menu.classList.add("hidden");
      el.actSelect.classList.add("hidden");
      if (state.selecting) setSelecting(false);
      return;
    }
    const s = state.spaceMeta;
    if (!s) {
      el.title.textContent = "Select a conversation";
      el.badge.textContent = "";
      el.meta.textContent = "";
      el.menu.classList.add("hidden");
      el.actSelect.classList.add("hidden");
      if (state.selecting) setSelecting(false);
      return;
    }
    el.title.textContent = s.label || s.id;
    const kind = typeLabel[s.type] || s.type || "";
    el.badge.textContent = s.excluded ? `${kind} · excluded` : kind;
    el.badge.classList.toggle("warn", !!s.excluded);
    el.menu.classList.remove("hidden");
    el.actSelect.classList.remove("hidden");
    el.actExclude.textContent = s.excluded ? "Include in sync again" : "Exclude from sync";
    const bits = [];
    if (s.msg_count) bits.push(`${s.msg_count.toLocaleString()} messages`);
    if (s.first_activity) bits.push(`from ${shortFmt.format(new Date(s.first_activity))}`);
    if (s.last_activity) bits.push(`last ${relTime(s.last_activity)}`);
    if (s.excluded) bits.push("new messages are not synced");
    el.meta.textContent = bits.join(" · ");
  }

  async function loadMessages() {
    if (!state.space || state.loading) return;
    state.loading = true;
    try {
      const data = await api(`/api/messages?space=${encodeURIComponent(state.space)}&limit=100&att=${state.attFilter}${dateQuery()}`);
      state.messages = data.messages;
      state.hasMore = data.has_more;
      state.mode = "chat";
      el.results.classList.add("hidden");
      el.messages.classList.remove("hidden");
      el.empty.classList.add("hidden");
      renderMessages({ stickToBottom: true });
      if (state.hasMore) el.pane.scrollTop = el.pane.scrollHeight;
      updateHeader();
    } catch (err) {
      toast(`Load failed: ${err.message}`);
    } finally {
      state.loading = false;
    }
  }

  async function loadOlder() {
    if (!state.space || state.loading || !state.hasMore || state.mode !== "chat") return;
    state.loading = true;
    try {
      const oldest = state.messages[0];
      const data = await api(
        `/api/messages?space=${encodeURIComponent(state.space)}&limit=100&att=${state.attFilter}${dateQuery()}&before=${encodeURIComponent(oldest.time)}`
      );
      if (!data.messages.length) {
        state.hasMore = false;
      } else {
        state.messages = data.messages.concat(state.messages);
        state.hasMore = data.has_more;
        renderMessages({ keepScroll: true });
      }
    } catch (err) {
      toast(`Load failed: ${err.message}`);
    } finally {
      state.loading = false;
    }
  }

  // ---------- bulk selection ----------
  function setSelecting(on) {
    state.selecting = on;
    if (!on) {
      state.selected.clear();
      state.lastPicked = null;
    }
    el.selbar.classList.toggle("hidden", !on);
    updateSelBar();
    renderMessages({ keepScroll: true });
  }

  function updateSelBar() {
    const n = state.selected.size;
    el.selcount.textContent = n === 1 ? "1 message selected" : `${n} messages selected`;
    el.selDelete.disabled = n === 0;
    el.selDelete.textContent = n ? `Delete ${n}…` : "Delete selected";
  }

  // Repaint one row instead of re-rendering the list: a full render detaches
  // the node the click came from, which silently drops the second click of a
  // rapid two-click selection.
  function paintRow(id) {
    const row = [...el.messages.querySelectorAll(".msg")].find((r) => r.dataset.id === id);
    if (!row) return;
    const on = state.selected.has(id);
    row.classList.toggle("picked", on);
    const box = row.querySelector("[data-sel-msg]");
    if (box) box.checked = on;
  }

  function togglePick(id, shiftKey) {
    const idx = state.messages.findIndex((m) => m.id === id);
    if (idx === -1) return;

    if (shiftKey && state.lastPicked) {
      const from = state.messages.findIndex((m) => m.id === state.lastPicked);
      if (from !== -1) {
        const [a, b] = from < idx ? [from, idx] : [idx, from];
        const makeSelected = !state.selected.has(id);
        for (let i = a; i <= b; i++) {
          const mid = state.messages[i].id;
          if (makeSelected) state.selected.add(mid);
          else state.selected.delete(mid);
          paintRow(mid);
        }
        state.lastPicked = id;
        updateSelBar();
        return;
      }
    }

    if (state.selected.has(id)) state.selected.delete(id);
    else state.selected.add(id);
    state.lastPicked = id;
    paintRow(id);
    updateSelBar();
  }

  async function deleteSelected() {
    const ids = [...state.selected];
    if (!ids.length) return;
    if (!confirm(`Remove ${ids.length} message${ids.length === 1 ? "" : "s"} from the archive?\n\nGoogle Chat is not touched — this only hides them here, and you can undo it by editing archive_manage.json and rebuilding.`)) return;
    try {
      const res = await post("/api/messages/delete", { ids });
      state.messages = state.messages.filter((m) => !state.selected.has(m.id));
      setSelecting(false);
      toast(`Removed ${res.removed} message${res.removed === 1 ? "" : "s"} from the archive`);
      refreshStats();
    } catch (err) {
      toast(`Delete failed: ${err.message}`);
    }
  }

  async function deleteByRange() {
    const from = el.rangeFrom.value, to = el.rangeTo.value;
    const all = el.rangeAll.checked;
    const space = all ? "" : (state.space || "");
    if (!from && !to) {
      el.rangeStatus.textContent = "Pick a date first.";
      return;
    }
    if (!all && !space) {
      el.rangeStatus.textContent = "Open a conversation, or tick \u201call conversations\u201d.";
      return;
    }
    try {
      const dry = await post("/api/messages/delete", { from, to, space, dry_run: true });
      if (!dry.matched) {
        el.rangeStatus.textContent = "No messages in that range.";
        el.rangeDelete.disabled = true;
        return;
      }
      el.rangeStatus.textContent = `${dry.matched.toLocaleString()} message${dry.matched === 1 ? "" : "s"} would be removed${all ? " across all conversations" : ""}.`;
      el.rangeDelete.disabled = false;
      el.rangeDelete.dataset.count = dry.matched;
    } catch (err) {
      el.rangeStatus.textContent = `Count failed: ${err.message}`;
    }
  }

  async function confirmRangeDelete() {
    const from = el.rangeFrom.value, to = el.rangeTo.value;
    const all = el.rangeAll.checked;
    const space = all ? "" : (state.space || "");
    const n = Number(el.rangeDelete.dataset.count || 0);
    if (!n) return;
    if (!confirm(`Remove ${n.toLocaleString()} messages from the archive?\n\nGoogle Chat keeps everything; this only prunes your local copy.`)) return;
    try {
      const res = await post("/api/messages/delete", { from, to, space });
      el.rangeStatus.textContent = `Removed ${plural(res.removed, "message")}.`;
      el.rangeDelete.disabled = true;
      el.rangeDelete.dataset.count = "";
      if (space && state.space === space) await openSpace(space, { keepScroll: false });
      refreshStats();
      toast(`Removed ${plural(res.removed, "message")}`);
    } catch (err) {
      el.rangeStatus.textContent = `Delete failed: ${err.message}`;
    }
  }

  async function openSpace(id, opts = {}) {
    state.space = id;
    state.spaceMeta = state.spaces.find((s) => s.id === id) || null;
    renderSpaces();
    updateHeader(); // show the name immediately; messages arrive a beat later
    el.pane.scrollTop = 0;
    await loadMessages();
    if (opts.anchorId) await jumpTo(id, opts.anchorId);
  }

  async function jumpTo(spaceId, messageId) {
    try {
      const data = await api(
        `/api/context?space=${encodeURIComponent(spaceId)}&id=${encodeURIComponent(messageId)}`
      );
      state.messages = data.messages;
      state.hasMore = data.has_more;
      state.mode = "chat";
      renderMessages();
      updateHeader();
      const node = el.messages.querySelector(`.msg[data-id="${CSS.escape(messageId)}"]`);
      if (node) {
        node.scrollIntoView({ block: "center" });
        node.classList.add("flash");
        setTimeout(() => node.classList.remove("flash"), 1600);
      }
    } catch (err) {
      toast(`Could not open message: ${err.message}`);
    }
  }

  // ---------- search ----------
  let searchTimer = null;
  let suggestTimer = null;
  let suggestSeq = 0;

  function scheduleSearch() {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(runSearch, 200);
    clearTimeout(suggestTimer);
    suggestTimer = setTimeout(loadSuggest, 90);
  }

  async function loadAccountFilter() {
    try {
      const data = await api("/api/settings");
      if (!data.accounts || data.accounts.length < 2) {
        el.accountFilter.classList.add("hidden");
        return;
      }
      el.accountFilter.innerHTML =
        `<option value="all">All accounts (${data.accounts.length})</option>` +
        data.accounts
          .map(
            (a) =>
              `<option value="${esc(a.slug)}"${a.slug === data.active ? " selected" : ""}>${esc(a.email)}${
                a.sync ? "" : " · sync paused"
              }</option>`
          )
          .join("");
      el.accountFilter.classList.remove("hidden");
      state.accountScope = data.active;
      el.accountFilter.addEventListener("change", async () => {
        state.accountScope = el.accountFilter.value;
        if (state.accountScope !== "all") {
          try {
            await post("/api/settings/account/activate", { slug: state.accountScope });
            state.space = null;
            state.spaceMeta = null;
            state.messages = [];
            el.messages.innerHTML = "";
          } catch (err) {
            toast(`Could not switch account: ${err.message}`);
          }
        }
        if (state.mode === "search" && el.search.value.trim().length >= 2) runSearch();
      });
    } catch {
      /* settings unavailable: leave the filter hidden */
    }
  }

  async function loadSuggest() {
    const q = el.search.value.trim();
    if (q.length < 2) {
      hideSuggest();
      return;
    }
    const seq = ++suggestSeq;
    try {
      const data = await api(`/api/suggest?q=${encodeURIComponent(q)}`);
      if (seq !== suggestSeq) return; // a newer keystroke already won
      renderSuggest(data);
    } catch {
      hideSuggest();
    }
  }

  function hideSuggest() {
    el.suggest.classList.add("hidden");
    el.suggest.innerHTML = "";
    state.suggestIndex = -1;
  }

  function renderSuggest(data) {
    const rows = [];
    for (const s of data.spaces || []) {
      rows.push(`<div class="sug" data-kind="space" data-space="${esc(s.id)}">
        <span class="sug-icon">${typeIcon[categoryOf(s)] || "•"}</span>
        <span class="sug-name">${esc(s.label || s.id)}</span>
        <span class="sug-n">${(s.msg_count || 0).toLocaleString()}</span>
      </div>`);
    }
    if (data.total > 0 || rows.length) {
      rows.push(`<div class="sug" data-kind="all">
        <span class="sug-icon">⌕</span>
        <span class="sug-name">Search all messages for “${esc(data.query)}”</span>
        <span class="sug-n">${(data.total || 0).toLocaleString()}</span>
      </div>`);
    }
    el.suggest.innerHTML = rows.join("");
    el.suggest.classList.toggle("hidden", rows.length === 0);
    state.suggestIndex = -1;
  }

  function activateSuggestion(node) {
    if (!node) return;
    if (node.dataset.kind === "space") {
      el.search.value = "";
      hideSuggest();
      state.mode = "chat";
      el.results.classList.add("hidden");
      openSpace(node.dataset.space);
    } else {
      hideSuggest();
      state.searchOffset = 0;
      runSearch();
    }
  }

  function moveSuggestion(delta) {
    const items = [...el.suggest.querySelectorAll(".sug")];
    if (!items.length) return;
    state.suggestIndex = (state.suggestIndex + delta + items.length) % items.length;
    items.forEach((n, i) => n.classList.toggle("on", i === state.suggestIndex));
    items[state.suggestIndex].scrollIntoView({ block: "nearest" });
  }

  async function runSearch(append = false) {
    const q = el.search.value.trim();
    if (q.length < 2) {
      if (state.mode === "search") {
        state.mode = state.space ? "chat" : "idle";
        el.results.classList.add("hidden");
        if (state.space) {
          el.messages.classList.remove("hidden");
          updateHeader();
        } else {
          el.empty.classList.remove("hidden");
        }
      }
      renderSpaces();
      return;
    }
    if (!append) {
      state.searchOffset = 0;
      state.query = q;
      state.picked.clear();
      state.pickAnchor = null;
      state.selecting = false;
    }
    const offset = state.searchOffset;
    el.results.classList.remove("hidden");
    el.messages.classList.add("hidden");
    el.empty.classList.add("hidden");
    if (!append) el.results.innerHTML = `<p class="hint">Searching…</p>`;
    state.mode = "search";
    updateHeader();

    try {
      const data = await api(
        `/api/search?q=${encodeURIComponent(q)}&limit=50&offset=${offset}&sort=${state.sort}&att=${state.attFilter}${dateQuery()}${accountQuery()}`
      );
      if (!append) {
        el.results.innerHTML = `<div class="resbar">
            <span class="hint">${state.sort === "rank" ? "Best matches" : "Newest first"}</span>
            <button data-sort="rank" class="${state.sort === "rank" ? "on" : ""}">Relevance</button>
            <button data-sort="time" class="${state.sort === "time" ? "on" : ""}">Newest</button>
            <span class="rsel-gap"></span>
            <span class="hint" data-rsel-count></span>
            <button data-rsel-toggle>${state.selecting ? "Cancel" : "Select"}</button>
            <button data-rsel-delete class="danger" disabled></button>
            <button data-rsel-all class="danger">Delete all matches…</button>
          </div>`;
      }
      const card = document.createDocumentFragment();
      if (!data.results.length && !append) {
        card.appendChild(
          Object.assign(document.createElement("p"), {
            className: "hint",
            textContent: `No messages match “${q}”.`,
          })
        );
      }
      for (const r of data.results) {
        const btn = document.createElement("button");
        btn.className = "result" + (state.selecting ? " selecting" : "");
        btn.dataset.space = r.space_id;
        btn.dataset.id = r.id;
        btn.dataset.account = r.account || "";
        const pickKey = `${r.account || ""}\u0000${r.id}`;
        const picked = state.picked.has(pickKey);
        btn.innerHTML = `${state.selecting
          ? `<span class="r-check"><input type="checkbox" data-pick="${esc(pickKey)}" ${picked ? "checked" : ""}></span>`
          : ""}${picked ? "" : ""}<div class="r-top">
            <span class="r-space">${esc(r.space_label || r.space_id)}</span>
            <span class="r-sep">›</span>
            <span class="r-who">${esc(r.sender || "")}${r.mine ? " (you)" : ""}</span>
            <span class="r-time">${r.time ? shortFmt.format(new Date(r.time)) + " " + timeFmt.format(new Date(r.time)) : ""}</span>
          </div>
          ${r.matched === "attachment" ? `<div class="r-att">📎 inside attached document</div>` : ""}
          <div class="r-snip">${renderSnippet(r.snippet)}</div>`;
        card.appendChild(btn);
      }
      el.results.appendChild(card);
      updateResultBar();
      const old = el.results.querySelector(".more");
      if (old) old.remove();
      if (data.has_more) {
        const more = document.createElement("button");
        more.className = "more";
        more.textContent = "Load more results";
        more.onclick = () => {
          state.searchOffset += 50;
          runSearch(true);
        };
        el.results.appendChild(more);
      }
    } catch (err) {
      toast(`Search failed: ${err.message}`);
    }
  }

  function renderSnippet(snip) {
    // Server marks hits with \u0001/\u0002 so escaping here cannot break tags.
    const safe = esc(snip || "");
    return safe.replace(/\u0001/g, "<mark>").replace(/\u0002/g, "</mark>");
  }

  // ---------- conversation actions ----------
  function closeMenu() {
    el.actions.classList.add("hidden");
  }

  async function toggleExclude() {
    const s = state.spaceMeta;
    if (!s) return;
    const next = !s.excluded;
    try {
      await post("/api/space/exclude", { space: s.id, excluded: next });
      s.excluded = next ? 1 : 0;
      const ix = state.spaces.findIndex((x) => x.id === s.id);
      if (ix >= 0) state.spaces[ix].excluded = s.excluded;
      renderSpaces();
      updateHeader();
      closeMenu();
      toast(next ? "Excluded — new messages will not be synced" : "Back in the sync list");
    } catch (err) {
      toast(`Could not update: ${err.message}`);
    }
  }

  async function deleteConversation() {
    const s = state.spaceMeta;
    if (!s) return;
    const ok = confirm(
      `Delete "${s.label}" (${s.msg_count.toLocaleString()} messages) from the local archive?\n\n` +
        `Google Chat is not touched. It will not be re-synced, so this is how you keep it out for good.`
    );
    if (!ok) return;
    try {
      const data = await post("/api/space/delete", { space: s.id });
      state.spaces = state.spaces.filter((x) => x.id !== s.id);
      state.space = null;
      state.spaceMeta = null;
      state.messages = [];
      el.messages.innerHTML = "";
      el.results.classList.add("hidden");
      el.empty.classList.remove("hidden");
      state.mode = "idle";
      renderSpaces();
      updateHeader();
      refreshStats();
      toast(`Deleted ${data.messages_removed.toLocaleString()} messages`);
    } catch (err) {
      toast(`Delete failed: ${err.message}`);
    } finally {
      closeMenu();
    }
  }

  async function deleteMessage(id) {
    if (!confirm("Delete this message from the local archive?")) return;
    try {
      await post("/api/message/delete", { id });
      state.messages = state.messages.filter((m) => m.id !== id);
      if (state.spaceMeta) state.spaceMeta.msg_count = Math.max(0, (state.spaceMeta.msg_count || 1) - 1);
      renderMessages();
      updateHeader();
      refreshStats();
      toast("Message deleted");
    } catch (err) {
      toast(`Delete failed: ${err.message}`);
    }
  }

  // ---------- attachments ----------
  function closeModal() {
    el.modal.classList.add("hidden");
    el.modalBody.innerHTML = "";
  }

  async function openAttachments(messageId) {
    el.modal.classList.remove("hidden");
    el.modalTitle.textContent = "Attachments";
    el.modalBody.innerHTML = `<p class="subtle">Loading…</p>`;
    try {
      const data = await api(`/api/attachments?message=${encodeURIComponent(messageId)}`);
      if (!data.attachments.length) {
        el.modalBody.innerHTML = `<p class="subtle">This message has no attachments.</p>`;
        return;
      }
      el.modalBody.innerHTML = data.attachments
        .map((a) => {
          const bits = [a.type || "unknown type"];
          if (a.bytes) bits.push(fmtBytes(a.bytes));
          if (a.deleted) bits.push("deleted from archive");
          else if (!a.archived) bits.push(a.error ? `not archived (${a.error})` : "not archived locally");
          const href = `/file?key=${encodeURIComponent(a.key)}`;
          const actions = a.archived
            ? `<a class="btn" href="${href}" target="_blank" rel="noopener">View</a>
               <a class="btn" href="${href}&download=1">Download</a>
               <button class="btn danger" data-att-del="${esc(a.key)}">Delete</button>`
            : `<span class="subtle">no local copy</span>`;
          return `<div class="att-row">
            <div class="att-main">
              <div class="att-name">${esc(a.name)}</div>
              <div class="att-sub subtle">${esc(bits.join(" · "))}</div>
            </div>
            <div class="att-actions">${actions}</div>
          </div>`;
        })
        .join("");
    } catch (err) {
      el.modalBody.innerHTML = `<p class="subtle">Could not load attachments: ${esc(err.message)}</p>`;
    }
  }

  async function deleteAttachment(key, messageId) {
    if (!confirm("Delete this file from the local archive? Its text stops being searchable.")) return;
    try {
      await post("/api/attachment/delete", { key });
      toast("Attachment deleted");
      await openAttachments(messageId);
    } catch (err) {
      toast(`Delete failed: ${err.message}`);
    }
  }

  // ---------- events ----------
  el.rangePanel.addEventListener("click", (e) => e.stopPropagation());
  el.menu.addEventListener("click", (e) => {
    e.stopPropagation();
    el.actions.classList.toggle("hidden");
  });
  el.actExclude.addEventListener("click", toggleExclude);
  el.actDelete.addEventListener("click", deleteConversation);
  document.addEventListener("click", (e) => {
    if (!el.actions.classList.contains("hidden") && !e.target.closest("#conv-actions, #conv-menu")) closeMenu();
    if (!el.rangePanel.classList.contains("hidden") && !e.target.closest("#range-panel, #act-range")) {
      el.rangePanel.classList.add("hidden");
    }
  });

  el.modalClose.addEventListener("click", closeModal);
  el.modal.addEventListener("click", (e) => {
    if (e.target === el.modal) closeModal();
    const del = e.target.closest("[data-att-del]");
    if (del) deleteAttachment(del.dataset.attDel, state.modalMessage);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (state.selecting) {
      setSelecting(false);
    } else if (!el.modal.classList.contains("hidden")) {
      closeModal();
    } else if (document.body.classList.contains("nav-open")) {
      setNav(false);
    }
  });

  el.actSelect.addEventListener("click", () => setSelecting(!state.selecting));
  el.selCancel.addEventListener("click", () => setSelecting(false));
  el.selDelete.addEventListener("click", deleteSelected);
  el.selAll.addEventListener("click", () => {
    for (const m of state.messages) state.selected.add(m.id);
    for (const m of state.messages) paintRow(m.id);
    updateSelBar();
  });

  el.actRange.addEventListener("click", (e) => {
    e.stopPropagation();
    el.rangePanel.classList.toggle("hidden");
    if (!el.rangePanel.classList.contains("hidden")) {
      el.rangeFrom.value = el.rangeFrom.value || state.dateFrom || "";
      el.rangeTo.value = el.rangeTo.value || state.dateTo || "";
      el.rangeStatus.textContent = "";
      el.rangeDelete.disabled = true;
      el.rangeDelete.dataset.count = "";
    }
  });
  el.rangeCount.addEventListener("click", deleteByRange);
  el.rangeDelete.addEventListener("click", confirmRangeDelete);
  el.rangeFrom.addEventListener("change", () => { el.rangeDelete.dataset.count = ""; el.rangeStatus.textContent = ""; });
  el.rangeTo.addEventListener("change", () => { el.rangeDelete.dataset.count = ""; el.rangeStatus.textContent = ""; });

  el.messages.addEventListener("click", (e) => {
    if (state.selecting) {
      const box = e.target.closest("[data-sel-msg]");
      const row = e.target.closest(".msg");
      if (box || row) {
        e.preventDefault();
        const id = (box ? box.dataset.selMsg : row.dataset.id);
        if (id) togglePick(id, e.shiftKey);
        return;
      }
    }
    const del = e.target.closest("[data-del-msg]");
    if (del) {
      deleteMessage(del.dataset.delMsg);
      return;
    }
    const chip = e.target.closest("[data-att-msg]");
    if (chip) {
      state.modalMessage = chip.dataset.attMsg;
      openAttachments(chip.dataset.attMsg);
    }
  });

  // ---------- conversation selection ----------
  function paintSpace(id) {
    const row = [...el.spaces.querySelectorAll(".space")].find((r) => r.dataset.space === id);
    if (!row) return;
    const on = state.pickedSpaces.has(id);
    row.classList.toggle("picked", on);
    const box = row.querySelector("[data-sel-space]");
    if (box) box.checked = on;
  }

  function updateSpaceBar() {
    const n = state.pickedSpaces.size;
    el.spacecount.textContent = n ? `${n} conversation${n === 1 ? "" : "s"} selected` : "Tick conversations to exclude or delete";
    el.spaceDelete.disabled = n === 0;
    el.spaceDelete.textContent = n ? `Delete ${n}…` : "Delete…";
    el.spaceExclude.disabled = n === 0;
    el.spaceExclude.textContent = n ? `Exclude ${n}` : "Exclude";
  }

  function setSpaceSelecting(on) {
    state.selectingSpaces = on;
    if (!on) {
      state.pickedSpaces.clear();
      state.spaceAnchor = null;
    }
    el.spacebar.classList.toggle("hidden", !on);
    renderSpaces();
    updateSpaceBar();
  }

  function toggleSpacePick(id, shiftKey) {
    const rows = [...el.spaces.querySelectorAll(".space")];
    const idx = rows.findIndex((r) => r.dataset.space === id);
    if (idx === -1) return;
    if (shiftKey && state.spaceAnchor) {
      const from = rows.findIndex((r) => r.dataset.space === state.spaceAnchor);
      if (from !== -1) {
        const [a, b] = from < idx ? [from, idx] : [idx, from];
        const select = !state.pickedSpaces.has(id);
        for (let i = a; i <= b; i++) {
          const sid = rows[i].dataset.space;
          if (select) state.pickedSpaces.add(sid);
          else state.pickedSpaces.delete(sid);
          paintSpace(sid);
        }
        state.spaceAnchor = id;
        updateSpaceBar();
        return;
      }
    }
    if (state.pickedSpaces.has(id)) state.pickedSpaces.delete(id);
    else state.pickedSpaces.add(id);
    state.spaceAnchor = id;
    paintSpace(id);
    updateSpaceBar();
  }

  async function applySpaces(spaces, mode) {
    if (!spaces.length) return;
    try {
      const dry = await post("/api/spaces/delete", { spaces, mode, dry_run: true });
      if (!dry.spaces) {
        toast("Those conversations are not in this archive");
        return;
      }
      const verb = mode === "exclude" ? "Stop syncing" : "Delete";
      const keep = mode === "exclude" ? "\n\nThey stay in the archive; the nightly sync will skip them." : "\n\nGoogle Chat is untouched — this only prunes the local copy.";
      if (!confirm(`${verb} ${dry.spaces} conversation${dry.spaces === 1 ? "" : "s"} (${dry.messages.toLocaleString()} messages)?${keep}`)) return;

      const res = await post("/api/spaces/delete", { spaces, mode });
      if (mode === "delete") {
        state.spaces = state.spaces.filter((s) => !spaces.includes(s.id));
        if (spaces.includes(state.space)) {
          state.space = null;
          state.spaceMeta = null;
          state.messages = [];
          el.messages.innerHTML = "";
          el.empty.classList.remove("hidden");
          updateHeader();
        }
      }
      setSpaceSelecting(false);
      toast(
        mode === "exclude"
          ? `Excluded ${res.spaces} conversation${res.spaces === 1 ? "" : "s"} from sync`
          : `Deleted ${res.spaces} conversation${res.spaces === 1 ? "" : "s"} (${res.messages.toLocaleString()} messages)`
      );
      refreshStats();
    } catch (err) {
      toast(`Failed: ${err.message}`);
    }
  }

  el.spaceSelect.addEventListener("click", () => setSpaceSelecting(!state.selectingSpaces));
  el.spaceDone.addEventListener("click", () => setSpaceSelecting(false));
  el.spaceDelete.addEventListener("click", () => applySpaces([...state.pickedSpaces], "delete"));
  el.spaceExclude.addEventListener("click", () => applySpaces([...state.pickedSpaces], "exclude"));
  el.spaceDeleteAll.addEventListener("click", () => {
    const listed = [...el.spaces.querySelectorAll(".space")].map((r) => r.dataset.space);
    applySpaces(listed, "delete");
  });

  el.spaces.addEventListener("click", (e) => {
    const sect = e.target.closest(".sect");
    if (sect) {
      const key = sect.dataset.cat;
      if (state.collapsed.has(key)) state.collapsed.delete(key);
      else state.collapsed.add(key);
      renderSpaces();
      return;
    }
    const node = e.target.closest(".space");
    if (!node) return;
    if (state.selectingSpaces) {
      e.preventDefault();
      toggleSpacePick(node.dataset.space, e.shiftKey);
      return;
    }
    el.search.value = "";
    state.mode = "chat";
    el.results.classList.add("hidden");
    setNav(false); // a chosen conversation should reveal itself on a phone
    openSpace(node.dataset.space);
  });

  // ---------- search-result selection ----------
  function paintResult(key) {
    const row = [...el.results.querySelectorAll(".result")].find(
      (r) => `${r.dataset.account || ""}\u0000${r.dataset.id}` === key
    );
    if (!row) return;
    const on = state.picked.has(key);
    row.classList.toggle("picked", on);
    const box = row.querySelector("[data-pick]");
    if (box) box.checked = on;
  }

  function updateResultBar() {
    const n = state.picked.size;
    const count = el.results.querySelector("[data-rsel-count]");
    if (count) count.textContent = n ? `${n} selected` : "";
    const del = el.results.querySelector("[data-rsel-delete]");
    if (del) {
      del.disabled = n === 0;
      del.textContent = n ? `Delete ${n}…` : "Delete selected";
    }
    const toggle = el.results.querySelector("[data-rsel-toggle]");
    if (toggle) toggle.textContent = state.selecting ? "Cancel" : "Select";
  }

  function toggleResultPick(key, shiftKey) {
    const rows = [...el.results.querySelectorAll(".result")];
    const keyOf = (r) => `${r.dataset.account || ""}\u0000${r.dataset.id}`;
    const idx = rows.findIndex((r) => keyOf(r) === key);
    if (idx === -1) return;

    if (shiftKey && state.pickAnchor) {
      const from = rows.findIndex((r) => keyOf(r) === state.pickAnchor);
      if (from !== -1) {
        const [a, b] = from < idx ? [from, idx] : [idx, from];
        const select = !state.picked.has(key);
        for (let i = a; i <= b; i++) {
          const k = keyOf(rows[i]);
          if (select) state.picked.set(k, { account: rows[i].dataset.account, id: rows[i].dataset.id });
          else state.picked.delete(k);
          paintResult(k);
        }
        state.pickAnchor = key;
        updateResultBar();
        return;
      }
    }

    if (state.picked.has(key)) state.picked.delete(key);
    else {
      const row = rows[idx];
      state.picked.set(key, { account: row.dataset.account, id: row.dataset.id });
    }
    state.pickAnchor = key;
    paintResult(key);
    updateResultBar();
  }

  function setResultSelecting(on) {
    state.selecting = on;
    if (!on) {
      state.picked.clear();
      state.pickAnchor = null;
      for (const row of el.results.querySelectorAll(".result")) {
        row.classList.remove("selecting", "picked");
        const box = row.querySelector(".r-check");
        if (box) box.remove();
      }
    } else {
      for (const row of el.results.querySelectorAll(".result")) {
        row.classList.add("selecting");
        if (!row.querySelector(".r-check")) {
          const wrap = document.createElement("span");
          wrap.className = "r-check";
          const key = `${row.dataset.account || ""}\u0000${row.dataset.id}`;
          wrap.innerHTML = `<input type="checkbox" data-pick="${esc(key)}">`;
          row.prepend(wrap);
        }
      }
    }
    updateResultBar();
  }

  async function deletePicked() {
    const items = [...state.picked.values()];
    if (!items.length) return;
    if (!confirm(`Remove ${items.length} message${items.length === 1 ? "" : "s"} from the archive?\n\nGoogle Chat is untouched; this only prunes the local copy.`)) return;
    const byAccount = new Map();
    for (const it of items) {
      if (!byAccount.has(it.account)) byAccount.set(it.account, []);
      byAccount.get(it.account).push(it.id);
    }
    let removed = 0;
    try {
      for (const [account, ids] of byAccount) {
        const res = await post("/api/messages/delete", { ids, account });
        removed += res.removed;
      }
      for (const it of items) {
        const row = [...el.results.querySelectorAll(".result")].find(
          (r) => r.dataset.id === it.id && (r.dataset.account || "") === (it.account || "")
        );
        if (row) row.remove();
      }
      setResultSelecting(false);
      toast(`Removed ${plural(removed, "message")} from the archive`);
      refreshStats();
    } catch (err) {
      toast(`Delete failed: ${err.message}`);
    }
  }

  async function deleteAllMatches() {
    const q = state.query;
    if (!q) return;
    const payload = {
      query: q,
      att: state.attFilter,
      from: state.dateFrom,
      to: state.dateTo,
      account: state.accountScope || "active",
    };
    try {
      const dry = await post("/api/messages/delete", { ...payload, dry_run: true });
      if (!dry.matched) {
        toast("Nothing matches the current search");
        return;
      }
      const where = state.accountScope === "all" ? " across every account" : "";
      if (!confirm(`Remove ${dry.matched.toLocaleString()} message${dry.matched === 1 ? "" : "s"} matching “${q}”${where}?\n\nGoogle Chat is untouched; this only prunes the local copy.`)) return;
      const res = await post("/api/messages/delete", payload);
      toast(`Removed ${plural(res.removed, "message")}`);
      refreshStats();
      runSearch();
    } catch (err) {
      toast(`Delete failed: ${err.message}`);
    }
  }

  el.results.addEventListener("click", (e) => {
    const toggle = e.target.closest("[data-rsel-toggle]");
    if (toggle) {
      setResultSelecting(!state.selecting);
      return;
    }
    const del = e.target.closest("[data-rsel-delete]");
    if (del) {
      if (!del.disabled) deletePicked();
      return;
    }
    const delAll = e.target.closest("[data-rsel-all]");
    if (delAll) {
      deleteAllMatches();
      return;
    }
    if (state.selecting) {
      const box = e.target.closest("[data-pick]");
      const node = e.target.closest(".result");
      if (box || node) {
        e.preventDefault();
        const row = box ? box.closest(".result") : node;
        const key = `${row.dataset.account || ""}\u0000${row.dataset.id}`;
        toggleResultPick(key, e.shiftKey);
        return;
      }
    }

    const sortBtn = e.target.closest("[data-sort]");
    if (sortBtn) {
      state.sort = sortBtn.dataset.sort;
      runSearch();
      return;
    }
    const node = e.target.closest(".result");
    if (!node) return;
    setNav(false);
    openSpace(node.dataset.space, { anchorId: node.dataset.id });
  });

  el.pane.addEventListener("scroll", () => {
    if (state.mode === "chat" && el.pane.scrollTop < 120) loadOlder();
  });

  el.search.addEventListener("input", () => {
    const q = el.search.value.trim();
    if (q.length >= 2) {
      scheduleSearch();
    } else {
      hideSuggest(); // otherwise the overlay keeps covering the filter dropdowns
      if (state.mode === "search") runSearch();
      else renderSpaces();
    }
  });

  el.search.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      if (el.suggest.classList.contains("hidden")) loadSuggest();
      else moveSuggestion(1);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      moveSuggestion(-1);
    } else if (e.key === "Enter") {
      const items = [...el.suggest.querySelectorAll(".sug")];
      if (state.suggestIndex >= 0 && items[state.suggestIndex]) {
        e.preventDefault();
        activateSuggestion(items[state.suggestIndex]);
      } else {
        hideSuggest();
        state.searchOffset = 0;
        runSearch();
      }
    } else if (e.key === "Escape") {
      if (!el.suggest.classList.contains("hidden")) {
        hideSuggest();
      } else {
        el.search.value = "";
        el.search.blur();
        runSearch();
      }
    }
  });

  // mousedown, not click: the input's blur would clear the list first.
  el.suggest.addEventListener("mousedown", (e) => {
    const node = e.target.closest(".sug");
    if (!node) return;
    e.preventDefault();
    activateSuggestion(node);
  });

  el.search.addEventListener("blur", () => setTimeout(hideSuggest, 120));

  el.pane.addEventListener("scroll", () => {
    if (state.mode === "search" && el.pane.scrollTop + el.pane.clientHeight > el.pane.scrollHeight - 260) {
      const more = el.results.querySelector(".more");
      if (more && !state.searchingMore) {
        state.searchingMore = true;
        state.searchOffset += 50;
        runSearch(true).finally(() => (state.searchingMore = false));
      }
    }
  });

  el.category.addEventListener("change", () => {
    state.category = el.category.value;
    if (state.category !== "all") state.collapsed.delete(state.category);
    renderSpaces();
  });

  el.attach.addEventListener("change", () => {
    state.attFilter = el.attach.value;
    if (state.mode === "search" && el.search.value.trim().length >= 2) {
      state.searchOffset = 0;
      runSearch();
    } else if (state.space) {
      el.pane.scrollTop = 0;
      loadMessages();
    }
  });

  // ---------- date range ----------
  function applyDateRange() {
    const from = el.dateFrom.value;
    const to = el.dateTo.value;
    if (from && to && from > to) {
      toast("“From” is after “To” — pick a valid range");
      return;
    }
    state.dateFrom = from;
    state.dateTo = to;
    el.dateClear.classList.toggle("hidden", !from && !to);
    if (state.mode === "search" && el.search.value.trim().length >= 2) {
      state.searchOffset = 0;
      runSearch();
    } else if (state.space) {
      el.pane.scrollTop = 0;
      loadMessages();
    }
  }

  el.dateFrom.addEventListener("change", applyDateRange);
  el.dateTo.addEventListener("change", applyDateRange);
  el.dateClear.addEventListener("click", () => {
    el.dateFrom.value = "";
    el.dateTo.value = "";
    applyDateRange();
  });

  // ---------- mobile drawer ----------
  const NARROW = window.matchMedia("(max-width: 720px)");
  const setNav = (open) => {
    document.body.classList.toggle("nav-open", open && NARROW.matches);
  };
  el.navToggle.addEventListener("click", (e) => {
    e.stopPropagation();
    setNav(!document.body.classList.contains("nav-open"));
  });
  el.scrim.addEventListener("click", () => setNav(false));
  // Rotating or resizing past the breakpoint must not leave the page scrolled-locked.
  NARROW.addEventListener("change", () => setNav(false));

  // ---------- settings ----------
  function exprFor(freq, time) {
    const [h, m] = (time || "05:00").split(":").map((n) => parseInt(n, 10) || 0);
    if (freq === "daily") return `${m} ${h} * * *`;
    const hours = parseInt(freq, 10) || 6;
    return `${m} */${hours} * * *`;
  }

  function freqFromExpr(expr) {
    if (!expr) return { freq: "daily", time: "05:00" };
    const parts = expr.trim().split(/\s+/);
    if (parts.length !== 5) return { freq: "daily", time: "05:00" };
    const [m, h] = parts;
    const pad = (n) => String(n).padStart(2, "0");
    if (h.startsWith("*/")) return { freq: h.slice(2), time: `${pad(parseInt(m, 10))}:00` };
    return { freq: "daily", time: `${pad(parseInt(h, 10) || 0)}:${pad(parseInt(m, 10) || 0)}` };
  }


  function oauthHTML(data) {
    const cs = data.client_secret || {};
    if (cs.present && !cs.error) {
      return `<div class="cs-box">
        <div class="cs-row">
          <span class="cs-dot ok"></span>
          <span>OAuth client: <code>${esc(cs.client_id || "stored")}</code>${cs.project_id ? ` · <span class="subtle">project ${esc(cs.project_id)}</span>` : ""}</span>
        </div>
        <div class="cs-actions">
          <label class="linkbtn">Replace client_secret.json<input type="file" id="cs-file" accept=".json,application/json" hidden></label>
          <a class="linkbtn" href="/setup#howto" target="_blank" rel="noreferrer">How to get this file</a>
        </div>
      </div>`;
    }
    if (cs.present && cs.error) {
      return `<div class="cs-box warn">
        <div class="cs-row"><span class="cs-dot bad"></span><span>Stored OAuth client is unusable: ${esc(cs.error)}</span></div>
        <div class="cs-actions">
          <label class="linkbtn">Upload a new client_secret.json<input type="file" id="cs-file" accept=".json,application/json" hidden></label>
          <a class="linkbtn" href="/setup#howto" target="_blank" rel="noreferrer">How to get this file</a>
        </div>
      </div>`;
    }
    return `<div class="cs-box warn">
      <div class="cs-row"><span class="cs-dot bad"></span><span>No OAuth client yet — Google sign-in is unavailable.</span></div>
      <div class="cs-actions">
        <label class="linkbtn">Upload client_secret.json<input type="file" id="cs-file" accept=".json,application/json" hidden></label>
        <a class="linkbtn" href="/setup" target="_blank" rel="noreferrer">Step-by-step instructions</a>
      </div>
    </div>`;
  }

  async function uploadClientSecret(file) {
    if (!file) return;
    try {
      const text = await file.text();
      const res = await post("/api/oauth/client-secret", { json: text });
      toast(res.ok ? "OAuth client saved — you can add a Google account now" : "Saved");
      openSettings();
    } catch (err) {
      toast(`Upload rejected: ${err.message}`);
    }
  }


  function fmtBytes(n) {
    if (!n) return "0 KB";
    if (n >= 1e9) return (n / 1e9).toFixed(1) + " GB";
    if (n >= 1e6) return Math.round(n / 1e6) + " MB";
    return Math.max(1, Math.round(n / 1e3)) + " KB";
  }

  function backupHTML(sizes) {
    const s = sizes || {};
    return `<section>
      <h4>Backup &amp; restore</h4>
      <p class="sched-note">
        Backups include credentials — the OAuth client, account tokens and the password hash —
        so the file can rebuild this archive on another machine. Keep it somewhere private.
      </p>
      <div class="sched-row">
        <a class="linkbtn" href="/api/backup?kind=config">Settings only · ${fmtBytes(s.config)}</a>
        <a class="linkbtn" href="/api/backup?kind=archive">Archive + database · ${fmtBytes(s.archive)}</a>
        <a class="linkbtn" href="/api/backup?kind=full">Everything (raw + files) · ${fmtBytes(s.full)}</a>
      </div>
      <div class="cs-box">
        <div class="cs-row"><span class="cs-dot ok"></span><span>Restore from a backup file (<code>.tar.gz</code>)</span></div>
        <div class="cs-actions">
          <label class="linkbtn">Choose file<input type="file" id="restore-file" accept=".gz,.tgz,application/gzip" hidden></label>
        </div>
        <p class="sched-note" id="restore-status"></p>
        <div class="sched-row hidden" id="restore-confirm">
          <button class="primary" id="restore-yes">Replace this archive</button>
          <button class="linkbtn" id="restore-no">Cancel</button>
        </div>
      </div>
    </section>`;
  }

  function uploadBackup(file) {
    const status = el.modalBody.querySelector("#restore-status");
    const confirmBox = el.modalBody.querySelector("#restore-confirm");
    if (!file) return;
    status.textContent = `Uploading ${fmtBytes(file.size)}…`;
    confirmBox.classList.add("hidden");

    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/backup/restore");
    xhr.setRequestHeader("Content-Type", "application/gzip");
    xhr.upload.addEventListener("progress", (e) => {
      if (e.lengthComputable) status.textContent = `Uploading ${fmtBytes(e.loaded)} of ${fmtBytes(e.total)}…`;
    });
    xhr.addEventListener("load", () => {
      let data = {};
      try { data = JSON.parse(xhr.responseText || "{}"); } catch { /* keep empty */ }
      if (xhr.status === 409 && data.needs_confirm) {
        const m = data.manifest || {};
        status.innerHTML = `Backup from ${esc(String(m.created_at || "?"))} · ${esc(String(m.kind || ""))} · `
          + `${(m.counts && m.counts.messages ? m.counts.messages.toLocaleString() : "?")} messages.<br>`
          + `This replaces the archive on this machine. The current settings are copied aside first.`;
        confirmBox.classList.remove("hidden");
        return;
      }
      if (xhr.status !== 200) {
        status.textContent = data.error || `Restore failed (${xhr.status})`;
        return;
      }
      status.textContent = `Restored ${data.installed} files — reloading…`;
      setTimeout(() => location.reload(), 800);
    });
    xhr.addEventListener("error", () => { status.textContent = "Upload failed (network error)"; });
    xhr.send(file);
  }

  async function confirmRestore() {
    const status = el.modalBody.querySelector("#restore-status");
    status.textContent = "Installing…";
    try {
      const res = await post("/api/backup/restore-confirm", {});
      status.textContent = `Restored ${res.installed} files — reloading…`;
      setTimeout(() => location.reload(), 800);
    } catch (err) {
      status.textContent = `Restore failed: ${err.message}`;
    }
  }


  function serviceHTML(svc) {
    const s = svc || {};
    const running = !!s.running;
    return `<section>
      <h4>Run at login</h4>
      <p class="sched-note">
        ${running
          ? `Installed as a ${esc(s.platform || "service")} entry — it starts with your session and restarts if it stops.`
          : `Optional: start this reader automatically every time you log in (${esc(s.platform || "system service")}).`}
        ${s.path ? `<br><span class="subtle">${esc(String(s.path))}</span>` : ""}
      </p>
      <div class="sched-row">
        ${running
          ? `<button class="linkbtn" id="svc-remove">Remove background service</button>`
          : `<button class="primary" id="svc-install">Install background service</button>`}
      </div>
    </section>`;
  }

  async function serviceAction(action) {
    try {
      const res = await post(`/api/service/${action}`, {});
      toast(res.note || res.detail || (action === "install" ? "Service installed" : "Service removed"));
      openSettings();
    } catch (err) {
      toast(`Could not ${action} the service: ${err.message}`);
    }
  }

  async function openSettings() {
    el.modal.classList.remove("hidden");
    el.modalTitle.textContent = "Settings";
    el.modalBody.innerHTML = `<p class="subtle">Loading…</p>`;
    let data;
    try {
      data = await api("/api/settings");
    } catch (err) {
      el.modalBody.innerHTML = `<p class="subtle">Could not load settings: ${esc(err.message)}</p>`;
      return;
    }

    const { freq, time } = freqFromExpr(data.schedule.expr);
    const accountsHTML = data.accounts
      .map(
        (a) => `<div class="acct${a.active ? " on" : ""}">
          <input type="radio" name="acct" value="${esc(a.slug)}" ${a.active ? "checked" : ""}>
          <div class="acct-mail">${esc(a.email)}<div class="acct-meta">${
            a.synced ? `${a.messages.toLocaleString()} messages archived` : "no archive yet — run a sync"
          }</div></div>
          <label class="acct-sync" title="Include this account in scheduled syncs">
            <input type="checkbox" data-acct-sync="${esc(a.slug)}" ${a.sync ? "checked" : ""}> sync
          </label>
          <label class="acct-sync" title="Also download image attachments (no text to index)">
            <input type="checkbox" data-acct-images="${esc(a.slug)}" ${a.images ? "checked" : ""}> images
          </label>
          <button data-acct-remove="${esc(a.slug)}" ${data.accounts.length < 2 ? "disabled title=\"The only account cannot be removed\"" : ""}>Remove</button>
        </div>`
      )
      .join("");

    const lastRun = data.schedule.last_run_at
      ? `${relTime(data.schedule.last_run_at)} · ${data.schedule.last_status || "unknown"}`
      : "never";

    el.modalBody.innerHTML = `<div class="settings">
      <section>
        <h4>Google accounts</h4>
        ${accountsHTML}
        ${oauthHTML(data)}
        ${
          data.oauth_available
            ? `<a class="add-acct" href="/oauth/start">+ Add Google account…</a>
               <p class="sched-note">Opens Google sign-in. The browser you sign in with decides which account is added.</p>`
            : ""
        }
      </section>
      <section>
        <h4>Sync schedule</h4>
        <div class="sched-row">
          <label><input type="checkbox" id="sched-paused" ${data.schedule.paused ? "checked" : ""}> Paused</label>
          <label>run at <input type="time" id="sched-time" value="${time}"></label>
          <label>
            <select id="sched-freq">
              <option value="daily" ${freq === "daily" ? "selected" : ""}>daily</option>
              <option value="6" ${freq === "6" ? "selected" : ""}>every 6 hours</option>
              <option value="12" ${freq === "12" ? "selected" : ""}>every 12 hours</option>
            </select>
          </label>
          <button class="primary" id="sched-save">Save schedule</button>
        </div>
        <p class="sched-note">
          Current: ${esc(data.schedule.display)}${data.schedule.paused ? " (paused)" : ""}<br>
          Last run: ${esc(lastRun)}${data.schedule.last_error ? ` — ${esc(String(data.schedule.last_error).slice(0, 120))}` : ""}<br>
          Applies to every account; each account is synced in turn.
        </p>
      </section>
      ${backupHTML(data.backup_sizes)}
      ${serviceHTML(data.service)}
      <section>
        <h4>Security</h4>
        ${
          data.security.configured
            ? `<div class="sched-row">
                 <input type="password" id="pw-current" placeholder="Current password" autocomplete="current-password">
                 <input type="password" id="pw-next" placeholder="New password (8+)" autocomplete="new-password">
                 <button class="primary" id="pw-save">Change password</button>
               </div>
               <p class="sched-note">
                 ${data.security.must_change ? "You are using a temporary password — change it now.<br>" : ""}
                 <button class="linkbtn" id="logout">Sign out</button>
               </p>`
            : `<p class="sched-note">No password set. The archive is open to anyone who can reach this port.</p>
               <div class="sched-row">
                 <input type="password" id="pw-next" placeholder="Choose a password (8+)" autocomplete="new-password">
                 <button class="primary" id="pw-save">Set password</button>
               </div>`
        }
      </section>
    </div>`;

    el.modalBody.querySelectorAll("input[name=acct]").forEach((radio) => {
      radio.addEventListener("change", async () => {
        try {
          await post("/api/settings/account/activate", { slug: radio.value });
          toast("Switched account — reloading");
          setTimeout(() => location.reload(), 500);
        } catch (err) {
          toast(`Could not switch: ${err.message}`);
        }
      });
    });

    el.modalBody.querySelectorAll("[data-acct-remove]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const slug = btn.dataset.acctRemove;
        if (!confirm(`Remove ${slug} from the archive?\n\nIts synced messages and downloaded files are deleted from this computer. The Google account itself is untouched.`)) return;
        try {
          await post("/api/settings/account/remove", { slug, delete_archive: true });
          toast("Account removed");
          openSettings();
        } catch (err) {
          toast(`Could not remove: ${err.message}`);
        }
      });
    });

    el.modalBody.querySelectorAll("[data-acct-sync]").forEach((box) => {
      box.addEventListener("change", async () => {
        try {
          await post("/api/settings/account/sync", { slug: box.dataset.acctSync, enabled: box.checked });
          toast(box.checked ? "Sync resumed for this account" : "Sync paused for this account");
        } catch (err) {
          box.checked = !box.checked;
          toast(`Could not update: ${err.message}`);
        }
      });
    });

    el.modalBody.querySelectorAll("[data-acct-images]").forEach((box) => {
      box.addEventListener("change", async () => {
        try {
          const res = await post("/api/settings/account/images", { slug: box.dataset.acctImages, enabled: box.checked });
          toast(box.checked ? "Images will download on the next sync" : "Images will no longer be fetched");
        } catch (err) {
          box.checked = !box.checked;
          toast(`Could not update: ${err.message}`);
        }
      });
    });

    const svcInstall = el.modalBody.querySelector("#svc-install");
    if (svcInstall) svcInstall.addEventListener("click", () => serviceAction("install"));
    const svcRemove = el.modalBody.querySelector("#svc-remove");
    if (svcRemove) svcRemove.addEventListener("click", () => serviceAction("uninstall"));

    const restoreFile = el.modalBody.querySelector("#restore-file");
    if (restoreFile) restoreFile.addEventListener("change", (e) => uploadBackup(e.target.files[0]));
    const restoreYes = el.modalBody.querySelector("#restore-yes");
    if (restoreYes) restoreYes.addEventListener("click", confirmRestore);
    const restoreNo = el.modalBody.querySelector("#restore-no");
    if (restoreNo) restoreNo.addEventListener("click", () => {
      el.modalBody.querySelector("#restore-confirm").classList.add("hidden");
      el.modalBody.querySelector("#restore-status").textContent = "";
    });

    const csFile = el.modalBody.querySelector("#cs-file");
    if (csFile) csFile.addEventListener("change", (e) => uploadClientSecret(e.target.files[0]));

    const pwSave = el.modalBody.querySelector("#pw-save");
    if (pwSave) {
      pwSave.addEventListener("click", async () => {
        const next = el.modalBody.querySelector("#pw-next").value;
        const currentBox = el.modalBody.querySelector("#pw-current");
        try {
          if (data.security.configured) {
            await post("/api/auth/change", { current: currentBox.value || "", next });
            toast("Password changed");
          } else {
            await post("/api/auth/setup", { password: next });
            toast("Password set — it is required from now on");
          }
          openSettings();
        } catch (err) {
          toast(`Could not save password: ${err.message}`);
        }
      });
    }
    const logout = el.modalBody.querySelector("#logout");
    if (logout) {
      logout.addEventListener("click", async () => {
        try {
          await post("/api/logout", {});
        } finally {
          location.href = "/login";
        }
      });
    }

    const save = el.modalBody.querySelector("#sched-save");
    if (save) {
      save.addEventListener("click", async () => {
        const paused = el.modalBody.querySelector("#sched-paused").checked;
        const expr = exprFor(el.modalBody.querySelector("#sched-freq").value, el.modalBody.querySelector("#sched-time").value);
        save.disabled = true;
        try {
          const res = await post("/api/settings/schedule", { expr, paused });
          toast(res.actions.length ? "Schedule updated" : "No change");
          openSettings();
        } catch (err) {
          toast(`Could not save: ${err.message}`);
          save.disabled = false;
        }
      });
    }
  }

  el.settingsBtn.addEventListener("click", openSettings);

  // Surface the result of an OAuth round-trip.
  (() => {
    const params = new URLSearchParams(location.search);
    if (params.has("oauth_ok")) {
      toast(`Added ${params.get("oauth_ok")} — run a sync to archive it`);
      history.replaceState({}, "", "/");
      setTimeout(() => location.reload(), 1200);
    } else if (params.has("oauth_error")) {
      toast(`Sign-in failed: ${params.get("oauth_error")}`);
      history.replaceState({}, "", "/");
    }
  })();

  // ---------- theme ----------
  const THEME_KEY = "chatArchiveTheme";
  const knownThemes = [...el.theme.options].map((o) => o.value);
  if (!knownThemes.includes(document.documentElement.dataset.theme)) {
    document.documentElement.dataset.theme = "dark";
  }
  el.theme.value = document.documentElement.dataset.theme;
  el.theme.addEventListener("change", () => {
    const value = el.theme.value;
    document.documentElement.dataset.theme = value;
    try {
      localStorage.setItem(THEME_KEY, value);
    } catch {
      /* private mode: theme lasts for this session only */
    }
  });

  document.addEventListener("keydown", (e) => {
    if ((e.key === "/" || (e.key === "k" && (e.metaKey || e.ctrlKey))) && document.activeElement !== el.search) {
      e.preventDefault();
      el.search.focus();
      el.search.select();
    }
  });

  // ---------- boot ----------
  (async function init() {
    try {
      state.meta = await api("/api/meta");
      renderStats();
      const data = await api("/api/spaces");
      state.spaces = data.spaces;
      renderSpaces();
      loadAccountFilter();
      // Most recent conversation first, so the archive opens on something useful.
      const first = state.spaces.find((s) => s.msg_count > 0);
      if (first) await openSpace(first.id);
    } catch (err) {
      console.error("archive init failed", err);
      el.empty.innerHTML = `<p>Could not read the archive: ${esc(err.message)}</p>
        <p class="subtle">Run <kbd>fetch.py</kbd> then <kbd>build.py</kbd>, then reload.</p>`;
    }
  })();
})();
