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

from . import paths

CHAT_URL = "https://chat.google.com/"
DEFAULT_PORT = 9222

# The harvester runs inside the page: only there is the DOM (and the session).
HARVEST_JS = r"""
(() => {
  // Real Chat markup (verified against chat.google.com, Oct 2026):
  //   <c-wiz data-topic-id=… data-local-sort-time-msec=… data-multiplat-item-id="MSG_LIST/At/<id>"
  //          data-p='%.@.null,["space/<SPACE_ID>",…]'>
  //     <… aria-label="Message <Sender>"> … body … </…>
  // The attributes are stable, unlike the obfuscated class names, so the
  // extractor keys on those and falls back to row heuristics only if they vanish.
  const pane = (() => {
    let best = null, bestScore = 0;
    for (const el of document.querySelectorAll('div,main,section')) {
      const st = getComputedStyle(el);
      if (!/(auto|scroll)/.test(st.overflowY)) continue;
      const score = el.scrollHeight - el.clientHeight;
      if (score > bestScore && el.clientHeight > 200) { best = el; bestScore = score; }
    }
    return best || document.scrollingElement;
  })();
  if (!pane) return { pane: null, messages: [], count: 0, atTop: true };

  // "Mon 5:44 PM", "Yesterday 11:55 AM", "Today 9:14 AM" — and the same inline
  // inside a row header.
  const TIME = /^(?:(?:Today|Yesterday|[A-Z][a-z]{2,8}(?:,?\s+\d{1,2})?)\s+)?\d{1,2}:\d{2}\s?[AP]M$/;
  const INLINE_STAMP = /^(?:(?:Today|Yesterday|[A-Z][a-z]{2,8}(?:,?\s+\d{1,2})?)\s+)?\d{1,2}:\d{2}\s?[AP]M\s*/;
  const containers = [...pane.querySelectorAll('c-wiz[data-topic-id]')];

  const cleanBody = (el, sender) => {
    const blocks = (el.innerText || '').split(/\n{2,}/);
    const lines = [];
    for (const block of blocks) {
      const parts = block.split('\n').map((x) => x.trim()).filter(Boolean);
      let text = '';
      const keep = [];
      for (const part of parts) {
        if (part === ',' || part === '@' || part === 'Add reaction') continue;
        if (TIME.test(part)) continue;
        if (part === sender) continue;
        if (!text) text = part;
        else if (/[.!?]$/.test(text)) { keep.push(text); text = part; }
        else text += (part.startsWith('@') || part.startsWith(',') ? '' : ' ') + part;
      }
      if (text) keep.push(text);
      if (keep.length) lines.push(keep.join('\n'));
    }
    let out = lines.join('\n\n').trim();
    // Headers and receipt chrome that ride along in the row's text.
    out = out.replace(INLINE_STAMP, '');
    out = out.replace(/^Edited\s+/, '');
    out = out.replace(/\s*Message viewed by [^.]*\./g, '');
    out = out.replace(/\s*Press tab[^.]*\./g, '');
    out = out.replace(/\s*Add reaction/g, '');
    if (sender && out.startsWith(sender)) out = out.slice(sender.length).replace(/^[\s,]+/, '');
    return out.trim();
  };

  const messages = [];
  for (const el of containers) {
    const label = el.querySelector('[aria-label^="Message "]');
    const sender = (label ? label.getAttribute('aria-label') : '').replace(/^Message\s+/, '').trim();
    const msec = Number(el.getAttribute('data-local-sort-time-msec') || 0);
    const item = (el.getAttribute('data-multiplat-item-id') || '').split('/').pop();
    const spaceMatch = (el.getAttribute('data-p') || '').match(/"space\/([^"]+)"/);
    const text = cleanBody(el, sender);
    if (!text) continue;
    messages.push({
      id: item || el.getAttribute('data-topic-id'),
      topic: el.getAttribute('data-topic-id') || '',
      space: spaceMatch ? spaceMatch[1] : '',
      sender,
      time: msec ? new Date(msec).toISOString() : '',
      text,
    });
  }

  window.__gca_pane = pane;
  return {
    pane: { cls: (pane.className || '').toString().slice(0, 40),
            scrollHeight: pane.scrollHeight, clientHeight: pane.clientHeight,
            scrollTop: pane.scrollTop },
    count: messages.length,
    atTop: pane.scrollTop <= 2,
    messages,
    conversations: [...document.querySelectorAll('[role="listitem"]')]
      .map((e) => (e.innerText || '').split('\n').filter(Boolean)[0] || '')
      .filter((t) => t && t.length < 80).slice(0, 300),
  };
})()
"""

# Backwards-compatible alias: calibration and harvesting read the same report.
COLLECT_JS = HARVEST_JS

SCROLL_JS = "(() => { const p = window.__gca_pane || null; const el = p || document.querySelector('div[class*=Bl2pUd], div[class*=krjOGe]') || document.scrollingElement; el.scrollBy(0, -Math.max(500, el.clientHeight * 0.85)); return el.scrollTop; })()"

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
    # Only real tabs: the page also registers service-worker and widget targets
    # whose URLs contain chat.google.com, and attaching to those reads nothing.
    def is_chat(t):
        url = t.get("url") or ""
        return (t.get("type") == "page"
                and "chat.google.com" in url
                and "serviceworker" not in url
                and "widget" not in url)

    chat = next((t for t in targets if is_chat(t)), None)
    if chat is None:
        created = browser.call("Target.createTarget", url=CHAT_URL)
        target_id = created["targetId"]
        time.sleep(5)
    else:
        target_id = chat["targetId"]
    info = browser.call("Target.attachToTarget", targetId=target_id, flatten=True)
    return Tab(browser, info["sessionId"])


def chrome_user_data() -> Path:
    """Default Chrome user-data directory for this OS."""
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
    return Path.home() / ".config" / "google-chrome"


def chrome_binary() -> str:
    if os.name == "nt":
        return shutil.which("chrome") or r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    if sys.platform == "darwin":
        return "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    return shutil.which("google-chrome") or shutil.which("chromium") or "google-chrome"


_PROFILE_SKIP = shutil.ignore_patterns(
    "Cache*", "Code Cache", "GPUCache", "DawnCache", "GrShaderCache", "ShaderCache",
    "Service Worker", "Application Cache", "*.tmp", "optimization_guide*", "Safe Browsing*",
)


def copy_profile(profile: str, dest: Path) -> Path:
    """Clone one Chrome profile into an isolated user-data dir.

    Chrome 136+ refuses ``--remote-debugging-port`` when the default user-data
    directory is in use, so the only way to both keep the session cookies and
    open a debug port is to run from a copy.
    """
    source = chrome_user_data()
    src_profile = source / profile
    if not src_profile.is_dir():
        raise SystemExit("no such Chrome profile: %s" % src_profile)
    if dest.exists():
        shutil.rmtree(dest)
    (dest / profile).mkdir(parents=True)
    shutil.copytree(src_profile, dest / profile, ignore=_PROFILE_SKIP, dirs_exist_ok=True)
    local_state = source / "Local State"
    if local_state.is_file():
        shutil.copy2(local_state, dest / "Local State")
    return dest


def launch(profile: str | None = None, port: int = DEFAULT_PORT,
           profile_copy: Path | None = None) -> None:
    """Start Chrome on a debug port, reusing the signed-in profile via a copy."""
    cmd = [chrome_binary(), "--remote-debugging-port=%d" % port,
           "--no-first-run", "--no-default-browser-check", CHAT_URL]
    if profile:
        dest = profile_copy or (paths.data_home() / "chrome-profile")
        print("copying Chrome profile %r to %s (once; ~200 MB)" % (profile, dest))
        copy_profile(profile, dest)
        cmd[1:1] = ["--user-data-dir=%s" % dest, "--profile-directory=%s" % profile]
        print("copied.")
    else:
        print("No --profile given: Chrome will open its default profile, which Chrome 136+")
        print("will not expose a debug port for. Use --profile 'Profile N' instead.")
    print("launching:", " ".join(cmd))
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(40):
        time.sleep(1)
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                print("debug port ready on %d — open a conversation, then run:" % port)
                print("  gchat-archive browser calibrate --port %d" % port)
                return
        except OSError:
            continue
    print("Chrome did not expose a debug port on %d. If another Chrome window already used"
          % port, file=sys.stderr)
    print("this profile, quit it and retry.", file=sys.stderr)


# --- harvesting ------------------------------------------------------------

def _stable_id(*parts: str) -> str:
    return hashlib.sha1("\x00".join(parts).encode("utf-8")).hexdigest()[:18]


def to_raw(conversation: str, items: list[dict], me: str = "") -> list[dict]:
    """Convert harvested messages into the same JSON shape the API source writes."""
    out = []
    for item in items:
        text = (item.get("text") or "").strip()
        if not text:
            continue
        sender = (item.get("sender") or "").strip()
        space = (item.get("space") or conversation).strip()
        message_id = (item.get("id") or "").strip() or _stable_id(space, sender, text[:200])
        stamp = item.get("time") or ""
        out.append({
            "name": "spaces/%s/messages/%s" % (space, message_id),
            "text": text,
            "createTime": stamp,
            "lastUpdateTime": stamp,
            "sender": {
                "displayName": sender or "unknown",
                "email": "",
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


def harvest(port: int = DEFAULT_PORT, conversation: str = "browser",
            max_rounds: int = 600, pause: float = 0.8) -> dict:
    """Scroll the open conversation upward and collect every rendered message."""
    tab = attach(port)
    seen: dict[str, dict] = {}
    rounds = 0
    try:
        for rounds in range(1, max_rounds + 1):
            report = tab.js(HARVEST_JS) or {}
            for message in report.get("messages", []):
                key = message.get("id") or "%s|%s" % (message.get("time"), message.get("text", "")[:120])
                seen.setdefault(key, message)
            if report.get("atTop") or not report.get("pane"):
                break
            tab.js(SCROLL_JS)
            time.sleep(pause)
    finally:
        tab.close()
    items = list(seen.values())
    items.sort(key=lambda m: m.get("time") or "")
    return {"conversation": conversation, "count": len(items), "items": items, "rounds": rounds}
