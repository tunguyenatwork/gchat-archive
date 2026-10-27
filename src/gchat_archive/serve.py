#!/usr/bin/env python3
"""Local web UI for the offline Google Chat archive.

    python -m gchat_archive serve               # http://127.0.0.1:8765
    python -m gchat_archive serve --port 9000 --open

Read-only, binds to localhost, no network access to Google: everything comes
out of chats.sqlite. Stdlib only, so it keeps working without the venv's deps.
"""
from __future__ import annotations

import argparse
import json
import os
import mimetypes
import re
import shutil
import socket
import subprocess
import sqlite3
import sys
import threading
import time
import traceback
import urllib.parse
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import accounts
from . import auth
from . import manage
from . import backup
from . import paths
from . import schedule
from . import service

def ARCHIVE() -> Path:  # noqa: N802 - kept call-compatible with the old constant
    """Root of the active account's archive (resolved per call)."""
    return accounts.active_root()


def DB() -> Path:  # noqa: N802
    return ARCHIVE() / "chats.sqlite"


def ATT_FILES() -> Path:  # noqa: N802
    return ARCHIVE() / "attachments"


def ATT_MANIFEST() -> Path:  # noqa: N802
    return ARCHIVE() / "attachments.jsonl"


def ATT_TEXT() -> Path:  # noqa: N802
    return ARCHIVE() / "attachments_text.jsonl"
WEB = Path(__file__).resolve().parent / "web"

# Scopes requested at consent. chat.messages + spaces for the archive, drive for
# exporting Google Docs/Sheets, gmail.readonly only to learn the account address.
OAUTH_SCOPES = [
    "https://www.googleapis.com/auth/chat.messages",
    "https://www.googleapis.com/auth/chat.spaces.readonly",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/gmail.readonly",
]

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
}


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(paths.ro_uri(DB()), uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def connect_for(slug: str) -> sqlite3.Connection:
    """Read-only connection to one account's archive."""
    path = accounts.account_root(slug) / "chats.sqlite"
    con = sqlite3.connect(paths.ro_uri(path), uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def connect_rw_for(slug: str) -> sqlite3.Connection:
    """Writable connection to one account's archive."""
    path = accounts.account_root(slug) / "chats.sqlite"
    con = sqlite3.connect(str(path), timeout=20)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=20000")
    return con


def record_deleted(ids: list[str], slug: str) -> None:
    """Persist deletions in that account's rules file, not the active one."""
    path = accounts.account_root(slug) / "archive_manage.json"
    data = manage.load_from(path)
    data["deleted_messages"] = sorted(set(data.get("deleted_messages") or []) | set(ids))
    manage.save(data, path)


def searchable_accounts() -> list[dict]:
    """Accounts that actually have a database to query."""
    data = accounts.ensure()
    out = []
    for entry in data["accounts"]:
        if (accounts.account_root(entry["slug"]) / "chats.sqlite").exists():
            out.append({"slug": entry["slug"], "email": entry.get("email") or entry["slug"]})
    return out


def send_mail(token_file: Path, to_email: str, subject: str, text: str) -> str:
    """Send a plain-text mail through the Gmail API; returns the sender used."""
    import base64

    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    creds = Credentials.from_authorized_user_file(str(token_file))
    if not creds.valid:
        creds.refresh(Request())
    raw = "To: %s\r\nSubject: %s\r\nMIME-Version: 1.0\r\nContent-Type: text/plain; charset=UTF-8\r\n\r\n%s" % (
        to_email,
        subject,
        text,
    )
    body = json.dumps({"raw": base64.urlsafe_b64encode(raw.encode()).decode()}).encode()
    req = urllib.request.Request(
        "https://gmail.googleapis.com/gmail/v1/users/me/messages/send", data=body, method="POST"
    )
    req.add_header("Authorization", "Bearer " + creds.token)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=45) as resp:
        json.loads(resp.read())
    return str(getattr(creds, "account", "") or token_file.name)


def connect_rw() -> sqlite3.Connection:
    """Writable connection for UI edits (delete/exclude).

    WAL so the read-only handlers keep serving while a delete rebuilds the index.
    """
    con = sqlite3.connect(str(DB()), timeout=20)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=20000")
    return con


def search_where(q: str, att: str, date_from: str, date_to: str, space: str = "") -> tuple[str, list]:
    """The one definition of "what this search matches".

    Shared by /api/search and delete-by-query, so a delete removes exactly what
    the user is looking at.
    """
    clause = attachment_clause(att)
    date_sql, params = date_bounds(date_from, date_to)
    clause += date_sql
    args: list = list(params)
    if space:
        clause += " AND m.space_id = ?"
        args.append(space)
    return clause, args


def rebuild_fts(con: sqlite3.Connection) -> None:
    """Full FTS rebuild. Seconds on a 240k-row archive — avoid in request paths."""
    con.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")


def drop_spaces_from_fts(con: sqlite3.Connection, spaces: list[str]) -> None:
    """Remove every indexed row for these conversations."""
    for start in range(0, len(spaces), 200):
        batch = spaces[start:start + 200]
        marks = ",".join("?" * len(batch))
        con.execute(
            "INSERT INTO messages_fts(messages_fts, rowid, text, sender_name, att_text) "
            "SELECT 'delete', rowid, text, sender_name, att_text FROM messages WHERE space_id IN (%s)" % marks,
            batch,
        )


def drop_from_fts(con: sqlite3.Connection, ids: list[str]) -> None:
    """Remove just these rows from the external-content index.

    FTS5 has no triggers here, so a delete needs an explicit index operation.
    Rebuilding costs seconds over the whole archive; issuing a 'delete' with the
    old column values touches only the affected rows.
    """
    for start in range(0, len(ids), 500):
        batch = ids[start:start + 500]
        marks = ",".join("?" * len(batch))
        con.execute(
            "INSERT INTO messages_fts(messages_fts, rowid, text, sender_name, att_text) "
            "SELECT 'delete', rowid, text, sender_name, att_text FROM messages WHERE id IN (%s)" % marks,
            batch,
        )


_MANIFEST_CACHE = {"mtime": 0.0, "data": {}}
_MANIFEST_LOCK = threading.Lock()


def manifest_index() -> dict:
    """key -> manifest record, reloaded only when the file changes."""
    try:
        mtime = ATT_MANIFEST().stat().st_mtime
    except FileNotFoundError:
        return {}
    with _MANIFEST_LOCK:
        if mtime != _MANIFEST_CACHE["mtime"]:
            data = {}
            for line in ATT_MANIFEST().read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("key"):
                    stored = Path(rec.get("path") or "")
                    if stored and not stored.is_file():
                        candidate = ATT_FILES() / stored.name
                        if candidate.is_file():
                            rec["path"] = str(candidate)
                    data[rec["key"]] = rec
            _MANIFEST_CACHE.update({"mtime": mtime, "data": data})
        return _MANIFEST_CACHE["data"]


def extracted_text_for(message_ids: set[str], deleted_keys: set[str]) -> dict[str, str]:
    """Re-read extracted attachment text for specific messages only."""
    out: dict[str, str] = {}
    if not ATT_TEXT().exists() or not message_ids:
        return out
    with ATT_TEXT().open() as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            mid = rec.get("message_id")
            if mid not in message_ids or rec.get("key") in deleted_keys:
                continue
            text = rec.get("text") or ""
            if text:
                out[mid] = (out.get(mid, "") + "\n" + text).strip()
    return out


def fts_query(raw: str) -> str:
    """Turn free text into a safe FTS5 query: every token ANDed, last one prefix-matched.

    Quoted phrases are preserved; everything else is stripped of FTS operators so
    a stray '-' or ':' from a user (or from pasted chat text) cannot raise a
    syntax error.
    """
    tokens = re.findall(r'"[^"]*"|\S+', raw.strip())
    parts = []
    for i, tok in enumerate(tokens):
        if tok.startswith('"') and tok.endswith('"') and len(tok) > 1:
            parts.append(tok)
            continue
        clean = re.sub(r'[^\w@.\-/]', "", tok, flags=re.UNICODE)
        if not clean:
            continue
        clean = clean.replace('"', "")
        if i == len(tokens) - 1:
            parts.append('"%s"*' % clean)
        else:
            parts.append('"%s"' % clean)
    return " AND ".join(parts)


def like_query(raw: str) -> str:
    esc = raw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return "%" + esc + "%"


def row_to_message(row: sqlite3.Row, me: str) -> dict:
    return {
        "id": row["id"],
        "space_id": row["space_id"],
        "space_label": row["label"] if "label" in row.keys() else None,
        "thread_id": row["thread_id"],
        "sender": row["sender_name"],
        "email": row["sender_email"],
        "mine": bool(me and row["sender_email"] and row["sender_email"].lower() == me.lower()),
        "time": row["create_time"],
        "updated": row["update_time"],
        "deleted": row["deleted"],
        "text": row["text"] or "",
        "attachments": json.loads(row["attachments"]) if row["attachments"] else [],
        "reactions": json.loads(row["reactions"]) if row["reactions"] else [],
        "quoted": json.loads(row["quoted"]) if row["quoted"] else None,
    }


def attachment_clause(mode: str) -> str:
    """SQL predicate for the with/without attachments filter.

    attachments is NULL (not '[]') when a message has none, so the test is exact
    and index-free.
    """
    has = "(m.attachments IS NOT NULL AND m.attachments <> '[]')"
    if mode == "with":
        return " AND " + has
    if mode == "without":
        return " AND NOT " + has
    return ""


def date_bounds(date_from: str | None, date_to: str | None) -> tuple[str, list[str]]:
    """SQL + params for a local-time date range.

    Messages are stored as UTC RFC3339, but a user picking "2026-10-01" means
    midnight in *their* timezone. The dates are therefore anchored to the local
    zone and converted to UTC bounds; `to` is inclusive of the whole day.
    """
    clause, params = "", []
    here = datetime.now().astimezone().tzinfo

    def utc_stamp(value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    if date_from:
        try:
            start = datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=here)
            clause += " AND m.create_time >= ?"
            params.append(utc_stamp(start))
        except ValueError:
            pass
    if date_to:
        try:
            # Exclusive upper bound at the start of the following day.
            end = datetime.strptime(date_to, "%Y-%m-%d").replace(tzinfo=here) + timedelta(days=1)
            clause += " AND m.create_time < ?"
            params.append(utc_stamp(end))
        except ValueError:
            pass
    return clause, params



# ---------- settings: sync schedule + accounts ----------
def oauth_client() -> dict:
    path = paths.client_secret()
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data.get("installed") or data.get("web") or {}


def parse_client_secret(data) -> dict:
    """Return the installed-app client, or raise ValueError with a fixable reason."""
    if not isinstance(data, dict):
        raise ValueError("that file is not a JSON object")
    if "installed" not in data:
        if "web" in data:
            raise ValueError("this is a \"web\" client — recreate it as application type \"Desktop app\"")
        raise ValueError("no \"installed\" section — download the JSON for an OAuth client of type \"Desktop app\"")
    client = data["installed"]
    if not client.get("client_id") or not client.get("client_secret"):
        raise ValueError("client_id / client_secret are missing from the file")
    if not str(client.get("client_id", "")).endswith(".apps.googleusercontent.com"):
        raise ValueError("that client_id does not look like a Google OAuth client")
    return client


_SYNC = {
    "proc": None,
    "started_at": 0.0,
    "finished_at": 0.0,
    "returncode": None,
    "lock": threading.Lock(),
}


def sync_command(mode: str = "normal") -> list[str]:
    """The argv a manual sync runs (overridable so tests need no network)."""
    override = os.environ.get("GCHAT_SYNC_COMMAND")
    if override:
        import shlex

        # posix=True strips backslashes, which would mangle a Windows path such
        # as C:\Python\python.exe; Windows quoting is not POSIX quoting.
        return shlex.split(override, posix=(os.name != "nt"))
    argv = [sys.executable, "-m", "gchat_archive"]
    default_home = Path.home() / (".%s" % "gchat-archive")
    if paths.data_home() != default_home:
        argv += ["--data-dir", str(paths.data_home())]
    argv.append("sync")
    if mode == "check-all":
        argv.append("--check-all")
    return argv


def sync_status() -> dict:
    """State of the current/last manual sync, for the UI button."""
    state = _SYNC
    running = state["proc"] is not None and state["proc"].poll() is None
    log = paths.data_home() / "sync.log"
    tail: list[str] = []
    if log.exists():
        try:
            tail = log.read_text(errors="replace").splitlines()[-6:]
        except OSError:
            tail = []
    last: dict = {}
    try:
        last = json.loads((paths.data_home() / "last_sync.json").read_text())
    except (OSError, json.JSONDecodeError):
        last = {}
    def stamp(value: float) -> str | None:
        """ISO local time: the UI's relTime() parses strings, not epoch floats."""
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(value)) if value else None

    return {
        "running": running,
        "started_at": stamp(state["started_at"]),
        "finished_at": stamp(state["finished_at"]),
        "returncode": state["returncode"],
        # elapsed: live while running, final once finished (was hard-coded 0 after)
        "seconds": round(
            (time.time() if running else state["finished_at"]) - state["started_at"], 1
        ) if state["started_at"] and (running or state["finished_at"]) else 0,
        "tail": tail,
        "last_run_at": last.get("at"),
        "last_ok": last.get("ok"),
        "added_messages": last.get("added_messages"),
        "added_spaces": last.get("added_spaces"),
        "total_messages": last.get("total_messages"),
        "total_spaces": last.get("total_spaces"),
        "last_detail": last.get("detail") or "",
    }


_BACKUP_SIZES = {"at": 0.0, "data": {}}


def backup_sizes() -> dict:
    """Byte estimate per backup kind, cached: stat()ing 12k attachments per
    settings open would be wasteful."""
    now = time.time()
    if now - _BACKUP_SIZES["at"] > 60:
        try:
            _BACKUP_SIZES["data"] = backup.estimate()
        except Exception:
            _BACKUP_SIZES["data"] = {}
        _BACKUP_SIZES["at"] = now
    return _BACKUP_SIZES["data"]


def client_secret_status() -> dict:
    """What the UI shows about the stored OAuth client (never the secret itself)."""
    path = paths.client_secret()
    status = {"present": False, "path": str(path), "client_id": "", "project_id": "", "type": ""}
    if not path.exists():
        return status
    try:
        client = parse_client_secret(json.loads(path.read_text()))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        status.update({"present": True, "error": str(exc)})
        return status
    cid = str(client.get("client_id", ""))
    status.update({
        "present": True,
        "client_id": cid[:12] + "…" + cid[-18:] if len(cid) > 32 else cid,
        "project_id": client.get("project_id", "") or "",
        "type": "installed",
    })
    return status


def redirect_uri(port: int) -> str:
    # Installed-app clients accept a loopback redirect; the port must match the
    # one this server is listening on.
    return "http://localhost:%d/oauth/callback" % port


def exchange_code(code: str, port: int) -> dict:
    client = oauth_client()
    if not client:
        raise RuntimeError("no OAuth client secret found at %s" % paths.client_secret())
    body = urllib.parse.urlencode(
        {
            "code": code,
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
            "redirect_uri": redirect_uri(port),
            "grant_type": "authorization_code",
        }
    ).encode()
    req = urllib.request.Request(client.get("token_uri", "https://oauth2.googleapis.com/token"), data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def profile_email(access_token: str) -> str:
    req = urllib.request.Request("https://gmail.googleapis.com/gmail/v1/users/me/profile")
    req.add_header("Authorization", "Bearer " + access_token)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read()).get("emailAddress", "")


def build_token_file(tokens: dict, email: str) -> dict:
    from datetime import datetime, timedelta, timezone

    client = oauth_client()
    expires_in = int(tokens.get("expires_in") or 3600)
    return {
        "token": tokens.get("access_token"),
        "refresh_token": tokens.get("refresh_token"),
        "token_uri": client.get("token_uri", "https://oauth2.googleapis.com/token"),
        "client_id": client.get("client_id"),
        "client_secret": client.get("client_secret"),
        "scopes": (tokens.get("scope") or " ".join(OAUTH_SCOPES)).split(),
        "expiry": (datetime.now(timezone.utc) + timedelta(seconds=expires_in))
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "account": email,
        "type": "authorized_user",
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "ChatArchive/1.0"

    def log_message(self, fmt, *args):
        """Quiet access log, but never crash on a non-string first arg.

        send_error() routes through here as log_message("code %d, ...", 404, ...)
        so the first positional is an int, not the path.
        """
        try:
            message = fmt % args if args else fmt
        except (TypeError, ValueError):
            message = " ".join(str(a) for a in (fmt,) + args)
        if "/api/" in message:
            return
        sys.stderr.write("%s - %s\n" % (self.address_string(), message))

    # --- helpers ----------------------------------------------------------
    def _json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _q(self, key, default=None):
        val = self.server.query.get(key)
        return val[0] if val else default

    # --- routes -----------------------------------------------------------
    def session_token(self) -> str | None:
        cookie = self.headers.get("Cookie") or ""
        for part in cookie.split(";"):
            name, _, value = part.strip().partition("=")
            if name == "ca_session":
                return value
        return None

    def authed(self) -> bool:
        return not auth.configured() or auth.valid_session(self.session_token())

    def set_session_cookie(self, token: str, max_age: int = auth.SESSION_TTL) -> None:
        self.send_header(
            "Set-Cookie",
            "ca_session=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=%d" % (token, max_age),
        )

    def redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        self.server.query = urllib.parse.parse_qs(parsed.query)
        path = parsed.path

        # Public surface: the login page itself, the two endpoints it needs, and
        # static assets. Assets must bypass the gate or the login page loads
        # unstyled — every CSS variable resolves to nothing and the page renders
        # as white-on-transparent.
        if path in ("/login", "/login.html"):
            return self.serve_login_page()
        if path in ("/setup", "/setup.html"):
            return self.serve_setup_page()
        if path == "/api/auth/status":
            return self.api_auth_status()
        if path == "/api/auth/accounts":
            return self.api_auth_accounts()
        if path.startswith("/assets/") or path.endswith((".css", ".js", ".svg", ".png", ".woff2", ".ico")):
            return self.serve_static(path)
        if not self.authed():
            if path.startswith("/api/"):
                return self._json({"error": "authentication required", "login": True}, 401)
            return self.redirect("/login")
        if path.startswith("/api/"):
            try:
                self.route_api(path)
            except Exception as exc:  # surfaced in the UI instead of killing the server
                self._json({"error": "%s: %s" % (type(exc).__name__, exc)}, 500)
            return
        if path in ("/oauth/start", "/oauth/callback"):
            # Not under route_api's guard: an exception here used to drop the
            # connection mid-sign-in, which looks like a crash to the user.
            try:
                if path == "/oauth/start":
                    self.serve_oauth_start()
                else:
                    self.serve_oauth_callback()
            except Exception as exc:
                traceback.print_exc()
                self.send_error(500, "sign-in failed: %s" % exc)
            return
        if path == "/file":
            try:
                self.serve_attachment(self.server.query)
            except Exception as exc:
                traceback.print_exc()
                self._json({"error": "%s: %s" % (type(exc).__name__, exc)}, 500)
            return
        self.serve_static(path)

    def serve_static(self, path):
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (WEB / rel).resolve()
        if not str(target).startswith(str(WEB.resolve())) or not target.is_file():
            self.send_error(404, "not found")
            return
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(target.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def route_api(self, path):
        # These must work on a first run, before any archive exists.
        if path == "/api/oauth/client-secret":
            return self._json(client_secret_status())
        if path == "/api/settings":
            return self.api_settings(None)
        if path == "/api/backup/estimate":
            try:
                return self._json({"kinds": backup.KINDS, "bytes": backup.estimate(), "counts": backup.counts()})
            except Exception as exc:
                return self._json({"error": str(exc)}, 500)
        if path == "/api/backup":
            return self.api_backup_download()
        if path == "/api/service/status":
            return self._json(service.status())
        if path == "/api/sync/status":
            return self._json(sync_status())

        con = connect()
        try:
            if path == "/api/meta":
                return self.api_meta(con)
            if path == "/api/spaces":
                return self.api_spaces(con)
            if path == "/api/messages":
                return self.api_messages(con)
            if path == "/api/search":
                return self.api_search(con)
            if path == "/api/suggest":
                return self.api_suggest(con)
            if path == "/api/context":
                return self.api_context(con)
            if path == "/api/attachments":
                return self.api_attachments(con)
            if path == "/api/auth/state":
                return self.api_auth_state()
            self._json({"error": "unknown endpoint"}, 404)
        finally:
            con.close()

    # --- writes -----------------------------------------------------------
    def do_POST(self):
        parsed = urllib.parse.urlsplit(self.path)
        self.server.query = urllib.parse.parse_qs(parsed.query)

        # A backup upload is a raw gzip stream, not JSON: handle it before the
        # body is parsed or it would be swallowed by json.loads.
        if parsed.path == "/api/backup/restore":
            try:
                if not self.authed():
                    return self._json({"error": "authentication required", "login": True}, 401)
                return self.api_backup_upload(int(self.headers.get("Content-Length") or 0))
            except Exception as exc:
                return self._json({"error": "%s: %s" % (type(exc).__name__, exc)}, 500)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                body = {}
        except (ValueError, json.JSONDecodeError):
            body = {}
        try:
            if parsed.path == "/api/login":
                return self.api_login(body)
            if parsed.path == "/api/auth/reset":
                return self.api_auth_reset(body)
            if not self.authed():
                return self._json({"error": "authentication required", "login": True}, 401)
            self.route_post(parsed.path, body)
        except Exception as exc:  # never let a bad edit kill the server
            self._json({"error": "%s: %s" % (type(exc).__name__, exc)}, 500)

    def route_post(self, path, body):
        if path == "/api/space/exclude":
            return self.api_space_exclude(body)
        if path == "/api/space/delete":
            return self.api_space_delete(body)
        if path == "/api/spaces/delete":
            return self.api_spaces_delete(body)
        if path == "/api/message/delete":
            return self.api_message_delete(body)
        if path == "/api/messages/delete":
            return self.api_messages_delete(body)
        if path == "/api/attachment/delete":
            return self.api_attachment_delete(body)
        if path == "/api/oauth/client-secret":
            return self.api_client_secret(body)
        if path == "/api/backup/restore-confirm":
            return self.api_backup_confirm()
        if path == "/api/sync/start":
            return self.api_sync_start(body)
        if path == "/api/service/install":
            return self.api_service("install")
        if path == "/api/service/uninstall":
            return self.api_service("uninstall")
        if path == "/api/settings/schedule":
            return self.api_save_schedule(body)
        if path == "/api/settings/account/activate":
            return self.api_activate_account(body)
        if path == "/api/settings/account/remove":
            return self.api_remove_account(body)
        if path == "/api/settings/account/sync":
            return self.api_account_sync(body)
        if path == "/api/settings/account/images":
            return self.api_account_images(body)
        if path == "/api/logout":
            return self.api_logout()
        if path == "/api/auth/setup":
            return self.api_auth_setup(body)
        if path == "/api/auth/change":
            return self.api_auth_change(body)
        self._json({"error": "unknown endpoint"}, 404)

    # --- settings ---------------------------------------------------------
    def api_settings(self, con):
        registry = accounts.ensure()
        out = []
        for entry in registry["accounts"]:
            root = accounts.account_root(entry["slug"])
            db = root / "chats.sqlite"
            count = 0
            if db.exists():
                try:
                    probe = sqlite3.connect(paths.ro_uri(db), uri=True, timeout=5)
                    count = probe.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
                    probe.close()
                except sqlite3.Error:
                    count = 0
            out.append(
                {
                    "slug": entry["slug"],
                    "email": entry.get("email") or entry["slug"],
                    "label": entry.get("label") or entry.get("email"),
                    "active": entry["slug"] == registry["active"],
                    "messages": count,
                    "synced": db.exists(),
                    "sync": accounts.account_enabled(entry),
                    "images": accounts.images_enabled(entry["slug"]),
                }
            )
        self._json(
            {
                "accounts": out,
                "active": registry["active"],
                "security": {
                    "configured": auth.configured(),
                    "must_change": bool(auth._load().get("must_change")),
                },
                "schedule": schedule.status(),
                "oauth_available": bool(oauth_client()),
                "client_secret": client_secret_status(),
                "backup_sizes": backup_sizes(),
                "service": service.status(),
                "sync": sync_status(),
                "port": self.server.server_port,
            }
        )

    def api_sync_start(self, body):
        """Start a one-off sync in the background; the UI polls /api/sync/status.

        A sync runs for minutes, so nothing happens inline — the response comes
        back as soon as the child process is alive.
        """
        state = _SYNC
        if state["proc"] is not None and state["proc"].poll() is None:
            return self._json({"ok": False, "error": "a sync is already running",
                               "status": sync_status()}, 409)

        mode = "check-all" if body.get("mode") == "check-all" else "normal"
        log = paths.data_home() / "sync.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ, GCHAT_ARCHIVE_HOME=str(paths.data_home()), PYTHONUNBUFFERED="1")
        argv = sync_command(mode)
        try:
            handle = open(log, "w")
            proc = subprocess.Popen(argv, stdout=handle, stderr=subprocess.STDOUT, env=env)
        except OSError as exc:
            return self._json({"error": "could not start the sync: %s" % exc}, 500)

        state.update({"proc": proc, "started_at": time.time(), "finished_at": 0.0,
                      "returncode": None})

        def collect(process=proc, stream=handle):
            code = process.wait()
            stream.close()
            state["finished_at"] = time.time()
            state["returncode"] = code
            # No record_run here: the sync command records the run itself, with
            # the counts it added. Writing from here overwrote those deltas with
            # a bare timestamp.

        threading.Thread(target=collect, daemon=True).start()
        return self._json({"ok": True, "mode": mode, "command": " ".join(argv),
                           "status": sync_status()})

    def api_service(self, action):
        """Install or remove the login service for this install."""
        try:
            if action == "install":
                occupied = False
                try:
                    with socket.create_connection(("127.0.0.1", self.server.server_port), timeout=1):
                        occupied = True
                except OSError:
                    pass
                ok, detail = service.install(self.server.server_port)
                note = ""
                if ok and occupied:
                    note = ("A server is already answering on this port (probably the one you started "
                            "by hand) — stop it, or the service will not be able to bind.")
                return self._json({"ok": ok, "detail": detail, "note": note, "status": service.status()})
            ok, detail = service.uninstall()
            return self._json({"ok": ok, "detail": detail, "status": service.status()})
        except Exception as exc:
            return self._json({"error": str(exc)}, 500)

    def api_backup_download(self):
        """Build the requested backup and stream it to the browser."""
        kind = (self.server.query.get("kind") or ["archive"])[0]
        if kind not in backup.KINDS:
            return self._json({"error": "kind must be one of %s" % ", ".join(backup.KINDS)}, 400)
        work = paths.data_home() / ".backup-work"
        work.mkdir(parents=True, exist_ok=True)
        target = work / "backup.tar.gz"
        try:
            backup.create(kind, dest=target)
            size = target.stat().st_size
            self.send_response(200)
            self.send_header("Content-Type", "application/gzip")
            self.send_header("Content-Disposition",
                             'attachment; filename="gchat-archive-%s-%s.tar.gz"'
                             % (kind, time.strftime("%Y%m%d-%H%M%S")))
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            with target.open("rb") as fh:
                shutil.copyfileobj(fh, self.wfile, 1024 * 1024)
        finally:
            target.unlink(missing_ok=True)
            try:
                work.rmdir()
            except OSError:
                pass

    def api_backup_upload(self, length: int):
        """Receive a backup, then try to install it (asks before overwriting)."""
        if length <= 0:
            return self._json({"error": "empty upload"}, 400)
        dest = paths.data_home() / "backup-upload.tar.gz"
        remaining = length
        with dest.open("wb") as fh:
            while remaining > 0:
                chunk = self.rfile.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                fh.write(chunk)
                remaining -= len(chunk)
        try:
            result = backup.restore(dest, force=False)
        except Exception as exc:
            dest.unlink(missing_ok=True)
            return self._json({"error": str(exc)}, 400)
        if result.get("ok"):
            dest.unlink(missing_ok=True)
            return self._json(result)
        return self._json(result, 409)

    def api_backup_confirm(self):
        """Install the backup uploaded a moment ago, replacing this archive."""
        dest = paths.data_home() / "backup-upload.tar.gz"
        if not dest.exists():
            return self._json({"error": "no uploaded backup to install — upload it again"}, 400)
        try:
            result = backup.restore(dest, force=True)
        except Exception as exc:
            return self._json({"error": str(exc)}, 400)
        finally:
            dest.unlink(missing_ok=True)
        return self._json(result)

    def api_client_secret(self, body):
        """Store (or replace, or remove) the OAuth client JSON uploaded from the UI."""
        if body.get("remove"):
            try:
                paths.client_secret().unlink()
            except FileNotFoundError:
                pass
            return self._json({"ok": True, "removed": True, **client_secret_status()})

        raw = body.get("json")
        if isinstance(raw, dict):
            data = raw
        elif isinstance(raw, str) and raw.strip():
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                return self._json({"error": "that file is not valid JSON (%s)" % exc}, 400)
        else:
            return self._json({"error": "no file contents received"}, 400)

        try:
            parse_client_secret(data)
        except ValueError as exc:
            return self._json({"error": str(exc)}, 400)

        path = paths.client_secret()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.chmod(tmp, 0o600)
        tmp.replace(path)
        return self._json({"ok": True, **client_secret_status()})

    def api_save_schedule(self, body):
        expr = (body.get("expr") or "").strip()
        want_paused = body.get("paused")

        if expr:
            ok, out = schedule.install(expr)
            if not ok:
                return self._json({"error": "could not change schedule", "detail": out}, 500)
        if want_paused is not None:
            ok, out = (schedule.remove() if bool(want_paused) else schedule.install(expr or schedule.status().get("expr") or "0 5 * * *"))
            if not ok:
                return self._json({"error": "could not pause/resume", "detail": out}, 500)

        self._json({"ok": True, "schedule": schedule.status()})

    def api_activate_account(self, body):
        slug = (body.get("slug") or "").strip()
        try:
            accounts.set_active(slug)
        except ValueError as exc:
            return self._json({"error": str(exc)}, 400)
        self.server.me = self.resolve_me()
        self._json({"ok": True, "active": slug})

    def api_remove_account(self, body):
        slug = (body.get("slug") or "").strip()
        try:
            data = accounts.remove_account(slug, delete_archive=bool(body.get("delete_archive", True)))
        except ValueError as exc:
            return self._json({"error": str(exc)}, 400)
        self.server.me = self.resolve_me()
        self._json({"ok": True, "accounts": [a["slug"] for a in data["accounts"]], "active": data["active"]})

    def serve_oauth_start(self):
        client = oauth_client()
        if not client:
            return self.serve_setup_page()
        params = {
            "client_id": client["client_id"],
            "redirect_uri": redirect_uri(self.server.server_port),
            "response_type": "code",
            "scope": " ".join(OAUTH_SCOPES),
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
        }
        url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)
        self.send_response(302)
        self.send_header("Location", url)
        self.end_headers()

    def serve_setup_page(self):
        """First run: upload client_secret.json, then continue to Google sign-in."""
        return self.serve_file(WEB / "setup.html", "text/html; charset=utf-8")

    def serve_oauth_callback(self):
        query = self.server.query
        error = (query.get("error") or [""])[0]
        code = (query.get("code") or [""])[0]
        if error or not code:
            self.redirect_home("oauth_error", error or "no_code")
            return
        try:
            tokens = exchange_code(code, self.server.server_port)
            if not tokens.get("refresh_token"):
                self.redirect_home("oauth_error", "no_refresh_token")
                return
            email = profile_email(tokens["access_token"])
            if not email:
                self.redirect_home("oauth_error", "no_email")
                return

            slug = accounts.slug_for(email)
            accounts.TOKENS_DIR.mkdir(parents=True, exist_ok=True)
            token_file = accounts.TOKENS_DIR / ("%s.json" % slug)
            token_file.write_text(json.dumps(build_token_file(tokens, email), indent=2))
            accounts.add_account(email, token_file)
            accounts.set_active(slug)
            accounts.account_root(slug).mkdir(parents=True, exist_ok=True)
            self.server.me = self.resolve_me()
            self.redirect_home("oauth_ok", email)
        except Exception as exc:  # noqa: BLE001 - surface the reason in the URL
            self.redirect_home("oauth_error", "%s: %s" % (type(exc).__name__, exc))

    def redirect_home(self, key, value):
        self.send_response(302)
        self.send_header("Location", "/?%s=%s" % (key, urllib.parse.quote(str(value)[:180])))
        self.end_headers()

    # --- auth -------------------------------------------------------------
    def serve_file(self, path: Path, content_type: str):
        try:
            body = path.read_bytes()
        except OSError:
            self.send_error(404, "not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def serve_login_page(self):
        body = (WEB / "login.html").read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def api_auth_status(self):
        data = auth._load()
        self._json(
            {
                "configured": auth.configured(),
                "must_change": bool(data.get("must_change")),
                "reset_pending": auth.reset_pending_for(),
                "authed": self.authed(),
            }
        )

    def api_auth_accounts(self):
        """Only emails — used by the locked-out reset form."""
        self._json({"accounts": [{"slug": a["slug"], "email": a.get("email") or a["slug"]} for a in accounts.ensure()["accounts"]]})

    def api_auth_state(self):
        data = auth._load()
        self._json(
            {
                "configured": auth.configured(),
                "must_change": bool(data.get("must_change")),
                "reset_pending": auth.reset_pending_for(),
                "updated_at": data.get("updated_at"),
            }
        )

    def api_login(self, body):
        password = str(body.get("password") or "")
        if not auth.configured():
            return self._json({"error": "no password is set yet"}, 400)
        ok = auth.verify(password) or auth.login_with_reset(password)
        if not ok:
            time.sleep(0.4)  # slow down guessing
            return self._json({"error": "wrong password"}, 401)
        token = auth.new_session()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        body_bytes = json.dumps({"ok": True}).encode()
        self.send_header("Content-Length", str(len(body_bytes)))
        self.set_session_cookie(token)
        self.end_headers()
        self.wfile.write(body_bytes)

    def api_logout(self):
        auth.drop_session(self.session_token())
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        payload = json.dumps({"ok": True}).encode()
        self.send_header("Content-Length", str(len(payload)))
        self.set_session_cookie("", 0)
        self.end_headers()
        self.wfile.write(payload)

    def api_auth_setup(self, body):
        if auth.configured():
            return self._json({"error": "a password is already set"}, 400)
        try:
            auth.set_password(str(body.get("password") or ""))
        except ValueError as exc:
            return self._json({"error": str(exc)}, 400)
        token = auth.new_session()
        self.send_response(200)
        payload = json.dumps({"ok": True}).encode()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.set_session_cookie(token)
        self.end_headers()
        self.wfile.write(payload)

    def api_auth_change(self, body):
        try:
            auth.change_password(str(body.get("current") or ""), str(body.get("next") or ""))
        except ValueError as exc:
            return self._json({"error": str(exc)}, 400)
        token = auth.new_session()  # new password, new session
        self.send_response(200)
        payload = json.dumps({"ok": True}).encode()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.set_session_cookie(token)
        self.end_headers()
        self.wfile.write(payload)

    def api_auth_reset(self, body):
        """Email a one-time password to whichever account the user picks."""
        email = str(body.get("email") or "").strip().lower()
        registry = accounts.ensure()
        target = next(
            (a for a in registry["accounts"] if (a.get("email") or "").lower() == email),
            None,
        )
        if not target:
            return self._json({"error": "unknown account"}, 404)

        code = auth.make_reset_code()
        body_text = (
            "Your chat archive password reset\n\n"
            "Temporary password: %s\n\n"
            "Sign in with it at http://localhost:%d/login, then change it in Settings.\n"
            "It expires in 30 minutes and can only be used once.\n" % (code, self.server.server_port)
        )
        senders = [Path(target.get("token") or ""), accounts.active_token()]
        last_error = None
        for sender in senders:
            if not sender or not sender.exists():
                continue
            try:
                send_mail(sender, target["email"], "Chat archive password reset", body_text)
                auth.store_reset(code, target["email"])
                return self._json({"ok": True, "to": target["email"], "via": sender.name})
            except Exception as exc:  # noqa: BLE001 - try the next sender
                last_error = "%s: %s" % (type(exc).__name__, exc)
        return self._json(
            {"error": "could not send mail", "detail": last_error or "no usable sender token"},
            502,
        )

    def api_account_images(self, body):
        slug = (body.get("slug") or "").strip()
        enabled = bool(body.get("enabled"))
        try:
            accounts.set_images(slug, enabled)
        except ValueError as exc:
            return self._json({"error": str(exc)}, 400)
        self._json({"ok": True, "slug": slug, "images": enabled, "note": "run a sync to fetch them"})

    def api_account_sync(self, body):
        slug = (body.get("slug") or "").strip()
        enabled = bool(body.get("enabled"))
        try:
            accounts.set_sync(slug, enabled)
        except ValueError as exc:
            return self._json({"error": str(exc)}, 400)
        self._json({"ok": True, "slug": slug, "enabled": enabled})

    def resolve_me(self) -> str:
        registry = accounts.ensure()
        for entry in registry["accounts"]:
            if entry["slug"] == registry["active"]:
                return entry.get("email") or ""
        return ""

    def api_space_exclude(self, body):
        space, excluded = body.get("space"), bool(body.get("excluded"))
        if not space:
            return self._json({"error": "space required"}, 400)
        con = connect_rw()
        try:
            con.execute("UPDATE spaces SET excluded = ? WHERE id = ?", (1 if excluded else 0, space))
            con.commit()
        finally:
            con.close()
        if excluded:
            manage.add("excluded_spaces", [space])
        else:
            manage.remove("excluded_spaces", [space])
        self._json({"ok": True, "space": space, "excluded": excluded})

    def api_space_delete(self, body):
        space = body.get("space")
        if not space:
            return self._json({"error": "space required"}, 400)
        con = connect_rw()
        try:
            removed = con.execute("DELETE FROM messages WHERE space_id = ?", (space,)).rowcount
            con.execute("DELETE FROM spaces WHERE id = ?", (space,))
            rebuild_fts(con)
            con.commit()
        finally:
            con.close()
        # Recorded so the next rebuild cannot resurrect it, and so the fetch
        # step stops pulling new messages for it.
        manage.add("deleted_spaces", [space])
        manage.remove("excluded_spaces", [space])
        self._json({"ok": True, "space": space, "messages_removed": removed})

    def api_spaces_delete(self, body):
        """Delete or exclude several conversations at once.

        {"spaces": [...], "mode": "delete"|"exclude", "dry_run": bool}
        Counts first so the UI can say how many conversations and messages are
        about to go.
        """
        spaces = [s for s in (body.get("spaces") or []) if isinstance(s, str) and s]
        if not spaces:
            return self._json({"error": "spaces required"}, 400)
        mode = body.get("mode") or "delete"
        dry_run = bool(body.get("dry_run"))

        con = connect_rw()
        try:
            marks = ",".join("?" * len(spaces))
            found = [r[0] for r in con.execute("SELECT id FROM spaces WHERE id IN (%s)" % marks, spaces)]
            total_messages = con.execute(
                "SELECT COUNT(*) FROM messages WHERE space_id IN (%s)" % marks, found
            ).fetchone()[0] if found else 0

            if dry_run or not found:
                return self._json({"ok": True, "dry_run": dry_run, "spaces": len(found), "messages": total_messages})

            if mode == "exclude":
                con.execute("UPDATE spaces SET excluded = 1 WHERE id IN (%s)" % marks, found)
                excluded = True
                removed = 0
            else:
                drop_spaces_from_fts(con, found)
                removed = con.execute("DELETE FROM messages WHERE space_id IN (%s)" % marks, found).rowcount
                con.execute("DELETE FROM spaces WHERE id IN (%s)" % marks, found)
                excluded = False
            con.commit()
        finally:
            con.close()

        if mode == "exclude":
            manage.add("excluded_spaces", found)
        else:
            manage.add("deleted_spaces", found)
            manage.remove("excluded_spaces", found)
        self._json({"ok": True, "spaces": len(found), "messages": removed, "excluded": excluded})

    def api_message_delete(self, body):
        message_id = body.get("id")
        if not message_id:
            return self._json({"error": "id required"}, 400)
        con = connect_rw()
        try:
            drop_from_fts(con, [message_id])
            removed = con.execute("DELETE FROM messages WHERE id = ?", (message_id,)).rowcount
            con.commit()
        finally:
            con.close()
        if removed:
            manage.add("deleted_messages", [message_id])
        self._json({"ok": True, "id": message_id, "removed": removed})

    def api_messages_delete(self, body):
        """Delete one, several, a date range, or every search hit.

        Accepts:
          {"ids": [...]}                          selected messages
          {"from","to"[,"space"]}                 a date range
          {"query","att","from","to","space"}     whatever the search matches
        `account` may be a slug or "all"; `dry_run` returns counts without
        deleting, so the UI can show what would go before arming the action.
        """
        ids = [i for i in (body.get("ids") or []) if isinstance(i, str) and i]
        query = (body.get("query") or "").strip()
        space = (body.get("space") or "").strip()
        dry_run = bool(body.get("dry_run"))
        # "active" is what the UI sends; it is a scope name, not a slug.
        scope = (body.get("account") or "").strip() or "active"
        if scope == "active":
            scope = accounts.active_slug()

        if scope == "all":
            slugs = [a["slug"] for a in searchable_accounts()]
        else:
            slugs = [scope]

        if not ids and not query and not (body.get("from") or body.get("to")):
            return self._json({"error": "give ids, a query, or a from/to range"}, 400)

        matched = 0
        removed = 0
        for slug in slugs:
            targets = list(ids) if ids else self.resolve_ids(slug, query, body, space)
            if not targets:
                continue
            matched += len(targets)
            if dry_run:
                continue
            try:
                con = connect_rw() if slug == accounts.active_slug() else connect_rw_for(slug)
            except sqlite3.Error:
                continue
            try:
                for start in range(0, len(targets), 500):
                    batch = targets[start:start + 500]
                    drop_from_fts(con, batch)
                    marks = ",".join("?" * len(batch))
                    removed += con.execute("DELETE FROM messages WHERE id IN (%s)" % marks, batch).rowcount
                con.commit()
            finally:
                con.close()
            record_deleted(targets, slug)

        self._json({"ok": True, "removed": removed, "matched": matched, "dry_run": dry_run})

    def resolve_ids(self, slug: str, query: str, body: dict, space: str) -> list[str]:
        """Ids matching a search (or a date range) in one account's database."""
        try:
            con = connect_for(slug)
        except sqlite3.Error:
            return []
        try:
            if not query:
                clause, params = date_bounds(body.get("from"), body.get("to"))
                if not clause:
                    return []
                sql = "SELECT m.id FROM messages m WHERE 1=1" + clause
                args = list(params)
                if space:
                    sql += " AND m.space_id = ?"
                    args.append(space)
                return [r[0] for r in con.execute(sql, args)]

            where, where_params = search_where(
                query, body.get("att") or "any", body.get("from"), body.get("to"), space
            )
            sql = "SELECT m.id FROM messages_fts f JOIN messages m ON m.rowid = f.rowid WHERE messages_fts MATCH ?" + where
            try:
                return [r[0] for r in con.execute(sql, [fts_query(query)] + where_params)]
            except sqlite3.OperationalError:
                like = like_query(query)
                alt = "SELECT m.id FROM messages m WHERE (m.text LIKE ? ESCAPE '\\' OR m.att_text LIKE ? ESCAPE '\\')" + where
                return [r[0] for r in con.execute(alt, [like, like] + where_params)]
        finally:
            con.close()

    def api_attachment_delete(self, body):
        key = body.get("key")
        if not key:
            return self._json({"error": "key required"}, 400)
        rec = manifest_index().get(key) or {}
        message_id = rec.get("message_id")

        path = rec.get("path")
        if path:
            file_path = Path(path)
            try:
                if ARCHIVE().resolve() in file_path.resolve().parents:
                    file_path.unlink(missing_ok=True)
            except OSError:
                pass

        # Drop its extracted text so it stops matching searches.
        deleted = manage.add("deleted_attachments", [key])["deleted_attachments"]
        con = connect_rw()
        try:
            if message_id:
                row = con.execute("SELECT attachments FROM messages WHERE id = ?", (message_id,)).fetchone()
                if row:
                    remaining = [
                        a for a in (json.loads(row["attachments"] or "[]")) if a.get("key") != key
                    ]
                    names = " ".join(a.get("name") or "" for a in remaining)
                    text = extracted_text_for({message_id}, set(deleted)).get(message_id, "")
                    con.execute(
                        "UPDATE messages SET att_text = ? WHERE id = ?",
                        ((names + "\n" + text).strip() or None, message_id),
                    )
            rebuild_fts(con)
            con.commit()
        finally:
            con.close()
        self._json({"ok": True, "key": key})

    def api_attachments(self, con):
        message_id = self._q("message")
        if not message_id:
            return self._json({"error": "message required"}, 400)
        row = con.execute("SELECT attachments, sender_name, create_time FROM messages WHERE id = ?", (message_id,)).fetchone()
        if not row:
            return self._json({"error": "message not found"}, 404)
        index = manifest_index()
        deleted = manage.as_sets()["deleted_attachments"]
        items = []
        for att in json.loads(row["attachments"] or "[]"):
            key = att.get("key")
            rec = index.get(key) or {}
            on_disk = bool(rec.get("path")) and Path(rec["path"]).is_file()
            items.append(
                {
                    "key": key,
                    "name": att.get("name") or rec.get("name") or key,
                    "type": att.get("type") or rec.get("content_type"),
                    "source": att.get("source"),
                    "archived": on_disk,
                    "bytes": rec.get("bytes"),
                    "error": rec.get("error"),
                    "deleted": key in deleted,
                    "searchable": bool(rec.get("bytes")) and not key in deleted,
                }
            )
        self._json({"message": message_id, "attachments": items})

    def serve_attachment(self, query):
        key = (query.get("key") or [""])[0]
        rec = manifest_index().get(key)
        if not rec or not rec.get("path"):
            self.send_error(404, "attachment not archived")
            return
        path = Path(rec["path"]).resolve()
        if ARCHIVE().resolve() not in path.parents or not path.is_file():
            self.send_error(404, "attachment missing")
            return
        body = path.read_bytes()
        ctype = rec.get("content_type") or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if ctype.startswith("application/vnd.google-apps"):
            ctype = "text/plain" if path.suffix in (".txt", ".csv") else ctype
        name = (rec.get("name") or path.name).replace('"', "")
        inline = (query.get("download") or ["0"])[0] != "1"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header(
            "Content-Disposition",
            "%s; filename=\"%s\"" % ("inline" if inline else "attachment", name),
        )
        self.end_headers()
        self.wfile.write(body)

    def api_meta(self, con):
        meta = {r["key"]: r["value"] for r in con.execute("SELECT key, value FROM meta")}
        meta.update(
            {
                "db": str(DB()),
                "db_bytes": DB().stat().st_size if DB().exists() else 0,
                # Live counts, not the values baked in at build time: deletions
                # in the UI must show up immediately.
                "messages": con.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
                "spaces": con.execute("SELECT COUNT(*) FROM spaces WHERE msg_count > 0").fetchone()[0],
                "all_spaces": con.execute("SELECT COUNT(*) FROM spaces").fetchone()[0],
                "excluded": con.execute("SELECT COUNT(*) FROM spaces WHERE excluded = 1").fetchone()[0],
            }
        )
        self._json(meta)

    def api_spaces(self, con):
        me = self.server.me
        rows = con.execute(
            """
            SELECT id, label, display_name, type, msg_count, last_activity, first_activity, error, bot_only, excluded
            FROM spaces ORDER BY (msg_count > 0) DESC, last_activity DESC
            """
        ).fetchall()
        self._json({"me": me, "spaces": [dict(r) for r in rows]})

    def api_messages(self, con):
        space = self._q("space")
        limit = min(int(self._q("limit", "80")), 500)
        before = self._q("before")
        if not space:
            return self._json({"error": "space required"}, 400)

        params: list = [space]
        where = "m.space_id = ?"
        if before:
            where += " AND m.create_time < ?"
            params.append(before)
        where += attachment_clause(self._q("att", "any"))
        date_sql, date_params = date_bounds(self._q("from"), self._q("to"))
        where += date_sql
        params += date_params
        params.append(limit + 1)
        rows = con.execute(
            """
            SELECT m.*, s.label FROM messages m JOIN spaces s ON s.id = m.space_id
            WHERE %s ORDER BY m.create_time DESC LIMIT ?""" % where,
            params,
        ).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        msgs = [row_to_message(r, self.server.me) for r in reversed(rows)]
        self._json({"messages": msgs, "has_more": has_more})

    def api_context(self, con):
        space = self._q("space")
        mid = self._q("id")
        if not space or not mid:
            return self._json({"error": "space and id required"}, 400)
        target = con.execute(
            "SELECT create_time FROM messages WHERE id = ?", (mid,)
        ).fetchone()
        if not target:
            return self._json({"error": "message not found"}, 404)
        ts = target["create_time"]
        rows = con.execute(
            """
            SELECT m.*, s.label FROM messages m JOIN spaces s ON s.id = m.space_id
            WHERE m.space_id = ? AND m.create_time >= ?
            ORDER BY m.create_time ASC LIMIT 40""",
            (space, ts),
        ).fetchall()
        before = con.execute(
            """
            SELECT m.*, s.label FROM messages m JOIN spaces s ON s.id = m.space_id
            WHERE m.space_id = ? AND m.create_time < ?
            ORDER BY m.create_time DESC LIMIT 40""",
            (space, ts),
        ).fetchall()
        msgs = [row_to_message(r, self.server.me) for r in reversed(before)]
        msgs += [row_to_message(r, self.server.me) for r in rows]
        self._json({"messages": msgs, "anchor": mid, "has_more": len(before) >= 40})

    def api_suggest(self, con):
        """Typeahead: matching conversations plus a match count for the query."""
        q = (self._q("q") or "").strip()
        if len(q) < 2:
            return self._json({"spaces": [], "total": 0, "query": q})
        like = like_query(q)
        spaces = con.execute(
            """SELECT id, label, type, msg_count FROM spaces
               WHERE label LIKE ? ESCAPE '\\'
               ORDER BY (msg_count > 0) DESC, msg_count DESC LIMIT 6""",
            [like],
        ).fetchall()
        try:
            total = con.execute(
                "SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH ?", [fts_query(q)]
            ).fetchone()[0]
        except sqlite3.OperationalError:
            total = con.execute(
                "SELECT COUNT(*) FROM messages WHERE text LIKE ? ESCAPE '\\'", [like]
            ).fetchone()[0]
        self._json({"spaces": [dict(r) for r in spaces], "total": total, "query": q})

    def api_search(self, con):
        """Full-text search, optionally across several accounts at once."""
        q = (self._q("q") or "").strip()
        if not q:
            return self._json({"results": [], "total": 0})
        limit = min(int(self._q("limit", "60")), 200)
        offset = max(int(self._q("offset", "0")), 0)
        space = self._q("space")

        scope = self._q("account", "active") or "active"
        if scope == "all":
            targets = searchable_accounts() or [{"slug": accounts.active_slug(), "email": ""}]
        elif scope == "active":
            active = accounts.active_slug()
            targets = [{"slug": active, "email": self.server.me}]
        else:
            targets = [{"slug": scope, "email": ""}]

        active_slug = accounts.active_slug()
        merged = []
        for target in targets:
            slug = target["slug"]
            own = slug == active_slug
            try:
                c = con if own else connect_for(slug)
            except sqlite3.Error:
                continue
            try:
                rows = self.search_rows(c, q, limit, offset if len(targets) == 1 else 0, space)
            finally:
                if not own:
                    c.close()
            for row in rows:
                row["account"] = slug
                row["account_email"] = target.get("email") or slug
                merged.append(row)

        if len(targets) > 1:
            # Interleave so no single account floods the list; each is already
            # ordered by its own relevance (or recency).
            buckets = {}
            for row in merged:
                buckets.setdefault(row["account"], []).append(row)
            merged = []
            for index in range(max((len(b) for b in buckets.values()), default=0)):
                for rows in buckets.values():
                    if index < len(rows):
                        merged.append(rows[index])
            merged = merged[:limit]

        self._json(
            {
                "results": merged,
                "query": q,
                "offset": offset,
                "scope": scope,
                "accounts": len(targets),
                "has_more": len(merged) >= limit,
            }
        )

    def search_rows(self, con, q, limit, offset, space):
        """Run the FTS/LIKE query against one account's database."""
        base = """
            SELECT m.id, m.space_id, s.label AS space_label, m.sender_name, m.sender_email,
                   m.create_time, m.text, m.deleted,
                   snippet(messages_fts, 0, char(1), char(2), '…', 18) AS snip,
                   snippet(messages_fts, 2, char(1), char(2), '…', 18) AS snip_att
            FROM messages_fts f
            JOIN messages m ON m.rowid = f.rowid
            JOIN spaces s ON s.id = m.space_id
            WHERE messages_fts MATCH ?
        """
        order = "m.create_time DESC" if self._q("sort") == "time" else "rank"
        params: list = [fts_query(q)]
        where, where_params = search_where(q, self._q("att", "any"), self._q("from"), self._q("to"), space)
        base += where
        params += where_params
        base += " ORDER BY %s LIMIT ? OFFSET ?" % order
        params += [limit, offset]
        try:
            rows = con.execute(base, params).fetchall()
        except sqlite3.OperationalError:
            alt = """
               SELECT m.id, m.space_id, s.label AS space_label, m.sender_name, m.sender_email,
                      m.create_time, m.text, m.deleted, m.text AS snip, m.att_text AS snip_att
               FROM messages m JOIN spaces s ON s.id = m.space_id
               WHERE (m.text LIKE ? ESCAPE '\\' OR m.att_text LIKE ? ESCAPE '\\')
               """ + where + """
               ORDER BY m.create_time DESC LIMIT ? OFFSET ?"""
            like = like_query(q)
            rows = con.execute(alt, [like, like] + where_params + [limit, offset]).fetchall()

        def pick(row):
            body = row["snip"] or ""
            if "\x01" in body:
                return body, "message"
            att = row["snip_att"] or ""
            if "\x01" in att:
                return att, "attachment"
            return body or att, "message"

        out = []
        for r in rows:
            snippet, matched = pick(r)
            out.append(
                {
                    "id": r["id"],
                    "space_id": r["space_id"],
                    "space_label": r["space_label"],
                    "sender": r["sender_name"],
                    "email": r["sender_email"],
                    "mine": bool(self.server.me and r["sender_email"]
                                 and r["sender_email"].lower() == self.server.me.lower()),
                    "time": r["create_time"],
                    "snippet": snippet,
                    "matched": matched,
                }
            )
        return out

def main(port: int = 8765, host: str = "127.0.0.1", open_browser: bool = False,
         start_path: str = "/") -> int:
    registry = accounts.ensure()
    if not registry["accounts"]:
        # Fresh install: send the user straight to the consent flow instead of a
        # reader with nothing behind it.
        print("No account yet — starting the sign-in flow.", flush=True)
        start_path = "/oauth/start"
        if not paths.client_secret().exists():
            print("Missing OAuth client at %s — see docs/SETUP.md" % paths.client_secret(), file=sys.stderr)
    me = next((a.get("email", "") for a in registry["accounts"] if a["slug"] == registry["active"]), "")
    if not registry["accounts"] or not DB().exists():
        # A freshly added account has no archive yet; that is a normal state, not
        # a reason to refuse to start.
        print("No archive yet at %s — run `gchat-archive sync` to populate it." % DB(), file=sys.stderr)

    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.me = me
    url = "http://%s:%d" % (host, port)
    print("Offline chat archive at %s  (Ctrl-C to stop)" % url, flush=True)
    if open_browser:
        target = url + start_path
        threading.Thread(target=lambda: (time.sleep(0.6), webbrowser.open(target)), daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Offline Google Chat browser")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--open", action="store_true", help="open a browser window")
    a = ap.parse_args()
    raise SystemExit(main(port=a.port, host=a.host, open_browser=a.open))
