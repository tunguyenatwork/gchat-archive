"""Harvest Google Chat through a real browser session instead of the API.

Why this exists: the API route needs an OAuth client and scopes. This route
needs nothing but the Chrome profile you are already signed into — it drives
that page, scrolls each conversation, and reads what the web client rendered.

Trade-offs (state them honestly):

* No OAuth client, no Google Cloud project, no scopes to grant.
* Slower than the API, and it only sees messages the client is willing to load.
* Google ships obfuscated class names, so the selectors here are *structural
  guesses* refined by ``browser --calibrate``. Run calibration first; it prints
  what was found so the extractor can be corrected without guesswork.

The output is byte-compatible with ``fetch.py`` (append-only JSONL per
conversation), so ``build``, ``serve`` and the whole reader UI work unchanged.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

CHAT_URL = "https://chat.google.com/"
DEFAULT_PORT = 9222

# The harvester runs inside the page: only there is the DOM (and the session).
HARVEST_JS = r"""
(() => {
  const TIME = /^\s*(\d{1,2}:\d{2}\s?(?:AM|PM|am|pm)?)\s*$/;
  const DAY = /^(today|yesterday|monday|tuesday|wednesday|thursday|friday|saturday|sunday|\d{1,2}\/\d{1,2}\/\d{2,4}|(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2}(,\s*\d{4})?)$/i;

  function deepestScroller() {
    let best = null, bestScore = 0;
    for (const el of document.querySelectorAll('div,main,section')) {
      const st = getComputedStyle(el);
      if (!/(auto|scroll)/.test(st.overflowY)) continue;
      const score = el.scrollHeight - el.clientHeight;
      if (score > bestScore && el.clientHeight > 200) { best = el; bestScore = score; }
    }
    return best || document.scrollingElement;
  }

  // Message rows: the deepest elements that carry a timestamp of their own.
  function messageRows(pane) {
    const out = [];
    const walker = document.createTreeWalker(pane, NodeFilter.SHOW_ELEMENT);
    let node = walker.currentNode;
    while (node) {
      const direct = [...node.childNodes].some(
        (n) => n.nodeType === 3 ? TIME.test(n.textContent) : (n.children && n.children.length === 0 && TIME.test(n.textContent || ''))
      );
      if (direct && node.innerText && node.innerText.trim().length > 1) out.push(node);
      node = walker.nextNode();
    }
    return out;
  }

  function labelOf(row) {
    const cand = row.querySelector('[aria-label]');
    if (cand && cand.getAttribute('aria-label')) return cand.getAttribute('aria-label');
    const lines = (row.innerText || '').split('\n').map((s) => s.trim()).filter(Boolean);
    return lines[0] || '';
  }

  const pane = deepestScroller();
  const rows = pane ? messageRows(pane) : [];
  const sample = rows.slice(0, 8).map((r) => ({
    tag: r.tagName.toLowerCase(),
    cls: (r.className || '').toString().slice(0, 60),
    text: (r.innerText || '').slice(0, 160).replace(/\n/g, ' | '),
  }));

  return {
    url: location.href,
    title: document.title,
    pane: pane ? { tag: pane.tagName.toLowerCase(), cls: (pane.className || '').toString().slice(0, 60),
                   scrollHeight: pane.scrollHeight, clientHeight: pane.clientHeight } : null,
    rowCount: rows.length,
    sample,
    conversations: [...document.querySelectorAll('[role="listitem"], [role="option"], [data-group-id]')]
      .slice(0, 500).map((e) => (e.innerText || '').split('\n')[0].trim()).filter(Boolean),
  };
})()
"""

# Collect one rendered batch, then ask the page to scroll up.
COLLECT_JS = r"""
(() => {
  const pane = window.__gca_pane || (window.__gca_pane = (() => {
    let best = null, bestScore = 0;
    for (const el of document.querySelectorAll('div,main,section')) {
      const st = getComputedStyle(el);
      if (!/(auto|scroll)/.test(st.overflowY)) continue;
      const score = el.scrollHeight - el.clientHeight;
      if (score > bestScore && el.clientHeight > 200) { best = el; bestScore = score; }
    }
    return best || document.scrollingElement;
  })());

  const TIME = /^\s*(\d{1,2}:\d{2}\s?(?:AM|PM|am|pm)?)\s*$/;
  const DAY = /^(today|yesterday|monday|tuesday|wednesday|thursday|friday|saturday|sunday|\d{1,2}\/\d{1,2}\/\d{2,4}|(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2}(,\s*\d{4})?)$/i;

  const timeOf = (el) => {
    for (const n of el.querySelectorAll('*')) {
      if (n.children.length) continue;
      const t = (n.textContent || '').trim();
      if (TIME.test(t)) return t;
    }
    return '';
  };

  // A message row carries exactly one timestamp; anything holding two or more
  // is a container wrapping several messages. (Checked against innerText, not
  // with a `* * *` descendant selector: that matches ancestors outside the
  // element's own subtree and rejected every row.)
  const rows = [];
  for (const el of pane.querySelectorAll('*')) {
    if (!el.innerText || el.innerText.length < 2) continue;
    if (el.querySelectorAll('*').length > 80) continue;   // too big to be one message
    const time = timeOf(el);
    if (!time) continue;
    const stamps = (el.innerText.match(/\b\d{1,2}:\d{2}\s?(?:AM|PM)?\b/gi) || []).length;
    if (stamps !== 1) continue;
    rows.push(el);
  }

  const seen = new Set();
  const out = [];
  for (const el of rows) {
    const lines = (el.innerText || '').split('\n').map((s) => s.trim()).filter(Boolean);
    const time = lines.find((l) => TIME.test(l)) || '';
    const rest = lines.filter((l) => l !== time);
    if (!rest.length) continue;
    const sender = rest[0].length <= 60 ? rest[0] : '';
    const text = (sender ? rest.slice(1) : rest).join('\n');
    if (!text) continue;
    const key = sender + '|' + time + '|' + text;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({ sender, time, text });
  }
  return { count: out.length, items: out, atTop: pane.scrollTop <= 2 };
})()
"""


# --- CDP plumbing ----------------------------------------------------------

def endpoint(port: int = DEFAULT_PORT) -> str:
    with urllib.request.urlopen("http://127.0.0.1:%d/json/version" % port, timeout=5) as res:
        return json.load(res)["webSocketDebuggerUrl"]


class CDP:
    """Minimal Chrome DevTools Protocol client.

    One socket speaks for the whole browser; each tab is addressed by a session
    id, so ``Tab`` objects share the connection rather than opening another.
    """

    def __init__(self, ws_url: str):
        try:
            import websocket  # type: ignore
        except ImportError as exc:  # pragma: no cover - optional extra
            raise SystemExit(
                "browser mode needs websocket-client:\n  pip install 'gchat-archive[browser]'"
            ) from exc
        # Chrome rejects CDP WebSockets that carry an Origin header unless it was
        # started with --remote-allow-origins; the protocol does not need one.
        self.ws = websocket.create_connection(
            ws_url, timeout=30, max_size=64 * 1024 * 1024, suppress_origin=True
        )
        self._id = 0

    def call(self, method: str, session: str | None = None, **params):
        self._id += 1
        message = {"id": self._id, "method": method, "params": params}
        if session:
            message["sessionId"] = session
        self.ws.send(json.dumps(message))
        while True:
            reply = json.loads(self.ws.recv())
            if reply.get("id") != self._id:
                continue          # event for some other subscription
            if "error" in reply:
                raise RuntimeError("%s: %s" % (method, reply["error"]))
            return reply.get("result", {})

    def js(self, expression: str, session: str | None = None):
        result = self.call("Runtime.evaluate", session=session, expression=expression,
                           awaitPromise=True, returnByValue=True)
        return result.get("result", {}).get("value")

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass


class Tab:
    """A DevTools session pinned to one tab."""

    def __init__(self, browser: CDP, session: str):
        self.browser = browser
        self.session = session

    def js(self, expression: str):
        return self.browser.js(expression, session=self.session)

    def close(self) -> None:
        self.browser.close()


def attach(port: int = DEFAULT_PORT) -> Tab:
    """Attach to the running Chrome and focus (or open) the Chat tab."""
    browser = CDP(endpoint(port))
    targets = browser.call("Target.getTargets").get("targetInfos", [])
    chat = next((t for t in targets if "chat.google.com" in (t.get("url") or "")), None)
    if chat is None:
        created = browser.call("Target.createTarget", url=CHAT_URL)
        target_id = created["targetId"]
        time.sleep(5)
    else:
        target_id = chat["targetId"]
    info = browser.call("Target.attachToTarget", targetId=target_id, flatten=True)
    return Tab(browser, info["sessionId"])


def launch(profile: str | None = None, port: int = DEFAULT_PORT) -> None:
    """Start Chrome with a debug port, reusing the signed-in profile."""
    if os.name == "nt":
        exe = shutil.which("chrome") or r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    elif sys.platform == "darwin":
        exe = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    else:
        exe = shutil.which("google-chrome") or shutil.which("chromium") or "google-chrome"
    cmd = [exe, "--remote-debugging-port=%d" % port, CHAT_URL]
    if profile:
        cmd.insert(1, "--profile-directory=%s" % profile)
    print("launching:", " ".join(cmd))
    print("If Chrome is already running, quit it first (the profile can only be used once).")
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(30):
        time.sleep(1)
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                print("debug port ready on %d" % port)
                return
        except OSError:
            continue
    print("Chrome did not expose a debug port — start it manually with --remote-debugging-port=%d" % port,
          file=sys.stderr)


# --- harvesting ------------------------------------------------------------

def _stable_id(*parts: str) -> str:
    return hashlib.sha1("\x00".join(parts).encode("utf-8")).hexdigest()[:18]


def to_raw(conversation: str, items: list[dict], me: str = "") -> list[dict]:
    """Convert harvested DOM rows into the same JSON shape the API source writes."""
    out = []
    for item in items:
        text = (item.get("text") or "").strip()
        if not text:
            continue
        sender = (item.get("sender") or "").strip()
        stamp = item.get("time") or ""
        msg_id = _stable_id(conversation, sender, stamp, text[:200])
        out.append({
            "name": "spaces/%s/messages/%s" % (conversation, msg_id),
            "text": text,
            "createTime": stamp,
            "lastUpdateTime": stamp,
            "sender": {
                "displayName": sender or "unknown",
                "email": me if sender and me and sender.split()[0] in me else "",
                "type": "HUMAN",
            },
            "source": "browser",
        })
    return out


def write_conversation(root: Path, conversation: str, rows: list[dict]) -> int:
    """Append harvested rows to raw/<conversation>.jsonl, deduped by message name."""
    raw = root / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    path = raw / ("%s.jsonl" % conversation.replace("/", "_"))
    known: set[str] = set()
    if path.exists():
        with path.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    known.add(json.loads(line).get("name", ""))
                except json.JSONDecodeError:
                    continue
    added = 0
    with path.open("a") as fh:
        for row in rows:
            if row["name"] in known:
                continue
            fh.write(json.dumps(row) + "\n")
            known.add(row["name"])
            added += 1
    return added


def calibrate(port: int = DEFAULT_PORT) -> dict:
    tab = attach(port)
    try:
        return tab.js(HARVEST_JS)
    finally:
        tab.close()


def harvest(port: int = DEFAULT_PORT, conversation: str = "browser", max_rounds: int = 400) -> dict:
    """Scroll the open conversation and collect every rendered message."""
    tab = attach(port)
    collected: list[dict] = []
    seen: set[str] = set()
    for _ in range(max_rounds):
        batch = tab.js(COLLECT_JS) or {}
        fresh = 0
        for item in batch.get("items", []):
            key = (item.get("sender", ""), item.get("time", ""), item.get("text", ""))
            if key in seen:
                continue
            seen.add(key)
            collected.append(item)
            fresh += 1
        tab.js("(() => { const p = window.__gca_pane; if (p) p.scrollBy(0, -Math.max(400, p.clientHeight * 0.8)); return p.scrollTop; })()")
        time.sleep(0.9)
        if batch.get("atTop") and fresh == 0:
            break
    tab.close()
    return {"conversation": conversation, "count": len(collected), "items": collected}
