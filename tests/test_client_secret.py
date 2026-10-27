"""Uploading replacement OAuth credentials through the UI's endpoint."""
from __future__ import annotations

import json
import os
import stat
import threading
import urllib.error
import urllib.request
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from conftest import ACCOUNTS


VALID = {
    "installed": {
        "client_id": "123456789012-abcdefghijklmnop.apps.googleusercontent.com",
        "client_secret": "GOCSPX-not-a-real-secret",
        "project_id": "chat-archive-demo",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }
}


@pytest.fixture()
def server(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    (home / "accounts").mkdir(parents=True)
    (home / "accounts.json").write_text(json.dumps(ACCOUNTS))
    monkeypatch.setenv("GCHAT_ARCHIVE_HOME", str(home))

    from gchat_archive import serve  # imported after the env var is set

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    httpd.me = "demo@example.com"
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d" % httpd.server_port, home
    finally:
        httpd.shutdown()


def call(url: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            body = res.read().decode()
            return res.status, (json.loads(body) if body.startswith("{") else {"html": body})
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def test_upload_replaces_and_removes(server) -> None:
    base, home = server
    status, body = call("%s/api/oauth/client-secret" % base)
    assert status == 200 and body["present"] is False

    status, body = call("%s/api/oauth/client-secret" % base, {"json": json.dumps(VALID)})
    assert status == 200, body
    assert body["present"] is True
    assert body["project_id"] == "chat-archive-demo"
    # the secret itself is never echoed back
    assert "GOCSPX" not in json.dumps(body)
    assert "…" in body["client_id"]

    stored = home / "client_secret.json"
    assert stored.is_file()
    if os.name != "nt":  # Windows has no POSIX permission bits to assert
        assert stat.S_IMODE(stored.stat().st_mode) == 0o600
    assert json.loads(stored.read_text())["installed"]["project_id"] == "chat-archive-demo"

    status, body = call("%s/api/oauth/client-secret" % base, {"remove": True})
    assert status == 200 and body["present"] is False
    assert not stored.exists()


def test_rejects_a_web_client_with_an_actionable_message(server) -> None:
    base, _ = server
    web = {"web": {"client_id": "x.apps.googleusercontent.com", "client_secret": "y"}}
    status, body = call("%s/api/oauth/client-secret" % base, {"json": json.dumps(web)})
    assert status == 400
    assert "Desktop app" in body["error"]


def test_rejects_broken_json(server) -> None:
    base, _ = server
    status, body = call("%s/api/oauth/client-secret" % base, {"json": "{not json"})
    assert status == 400
    assert "valid JSON" in body["error"]


def test_missing_fields_are_named(server) -> None:
    base, _ = server
    status, body = call("%s/api/oauth/client-secret" % base, {"json": json.dumps({"installed": {}})})
    assert status == 400
    assert "client_id" in body["error"]


def test_oauth_start_shows_setup_page_without_a_client(server) -> None:
    base, _ = server
    req = urllib.request.Request("%s/oauth/start" % base)
    with urllib.request.urlopen(req, timeout=10) as res:
        page = res.read().decode()
    assert res.status == 200
    assert "client_secret.json" in page
    assert "Desktop app" in page
    assert "/api/oauth/client-secret" in page
