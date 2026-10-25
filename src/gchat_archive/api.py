#!/usr/bin/env python3
"""Google Chat REST access for the offline archive.

Deliberately not a thin wrapper around a generic helper: per-space 403/404 must
not abort a sync, and 429/5xx must back off with jitter rather than exit.

Known API quirk (verified 2026-10-23): `filter` and `startTime` on
spaces.messages.list are silently ignored, and pageSize is only honoured when
`orderBy` is present. Hence every read pages newest-first with
orderBy=createTime desc and does its own client-side windowing.
"""
from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import accounts

CHAT_BASE = "https://chat.googleapis.com/v1"
GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1"

SCOPES_HINT = "chat.messages, chat.spaces.readonly"


def token_path() -> Path:
    """Token for whichever account this process is working on."""
    return accounts.active_token()


class ChatError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__("HTTP %s: %s" % (status, message))
        self.status = status
        self.message = message


class Client:
    """Minimal Chat client with token refresh, retries and a soft rate limit."""

    def __init__(self, min_interval: float = 0.12, max_retries: int = 6):
        self.min_interval = min_interval
        self.max_retries = max_retries
        self._creds = None
        self._last_call = 0.0
        self.requests = 0

    # --- auth -------------------------------------------------------------
    def creds(self):
        if self._creds is not None and self._creds.valid:
            return self._creds
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        creds = Credentials.from_authorized_user_file(str(token_path()))
        if not creds.valid:
            creds.refresh(Request())
            token = json.loads(token_path().read_text())
            token["token"] = creds.token
            token["refresh_token"] = creds.refresh_token or token.get("refresh_token")
            token["scopes"] = sorted(creds.scopes or token.get("scopes", []))
            token_path().write_text(json.dumps(token, indent=2))
        self._creds = creds
        return creds

    # --- transport --------------------------------------------------------
    def _throttle(self):
        wait = self.min_interval - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def get(self, url: str, params: dict | None = None) -> dict:
        if params:
            url = url + "?" + urllib.parse.urlencode(params)
        for attempt in range(self.max_retries):
            self._throttle()
            req = urllib.request.Request(url, method="GET")
            req.add_header("Authorization", "Bearer " + self.creds().token)
            self.requests += 1
            try:
                with urllib.request.urlopen(req, timeout=90) as resp:
                    raw = resp.read().decode()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode(errors="replace")
                if exc.code in (429, 500, 502, 503, 504):
                    time.sleep(min(60, (2 ** attempt) + random.random()))
                    self._creds = None  # force a token re-read on retry
                    continue
                raise ChatError(exc.code, detail[:400]) from exc
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                time.sleep(min(30, (2 ** attempt) + random.random()))
                continue
        raise ChatError(429, "retries exhausted")

    # --- API --------------------------------------------------------------
    def my_identity(self) -> dict:
        """Who am I? Taken from Gmail's profile (same OAuth token)."""
        try:
            data = self.get(GMAIL_BASE + "/users/me/profile")
            return {"email": data.get("emailAddress"), "name": None}
        except ChatError:
            return {"email": None, "name": None}

    def iter_spaces(self):
        page_token = None
        while True:
            params = {"pageSize": "1000"}
            if page_token:
                params["pageToken"] = page_token
            data = self.get(CHAT_BASE + "/spaces", params)
            yield from data.get("spaces", [])
            page_token = data.get("nextPageToken")
            if not page_token:
                return

    def iter_messages(self, space: str, stop_when_known: set[str] | None = None):
        """Yield messages newest-first, paging until exhausted.

        When `stop_when_known` is given (incremental sync), stop as soon as a
        whole page contains nothing new: everything older is already archived.
        """
        page_token = None
        while True:
            params = {"orderBy": "createTime desc", "pageSize": "1000"}
            if page_token:
                params["pageToken"] = page_token
            data = self.get("%s/%s/messages" % (CHAT_BASE, space), params)
            batch = data.get("messages", [])
            yield from batch
            page_token = data.get("nextPageToken")
            if not page_token:
                return
            if stop_when_known is not None and batch:
                if all(m.get("name") in stop_when_known for m in batch):
                    return
            if not batch:
                return
