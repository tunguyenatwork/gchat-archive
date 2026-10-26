"""The sign-in endpoints must not 500 — a dead connection looks like a crash.

Regression: stripping the scheduler code out of serve.py removed OAUTH_SCOPES
along with it, so /oauth/start raised NameError and the browser showed
"the page isn't working".
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

CLIENT = {
    "installed": {
        "client_id": "1234-demo.apps.googleusercontent.com",
        "client_secret": "not-a-real-secret",
        "project_id": "demo",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }
}

SCOPES = (
    "https://www.googleapis.com/auth/chat.messages",
    "https://www.googleapis.com/auth/chat.spaces.readonly",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/gmail.readonly",
)


@pytest.fixture()
def server(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    (home / "accounts").mkdir(parents=True)
    (home / "accounts.json").write_text(json.dumps({"active": "", "accounts": []}))
    (home / "client_secret.json").write_text(json.dumps(CLIENT))
    monkeypatch.setenv("GCHAT_ARCHIVE_HOME", str(home))

    from gchat_archive import serve

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    httpd.me = ""
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield "http://127.0.0.1:%d" % httpd.server_port, home
    finally:
        httpd.shutdown()


def get(url: str) -> tuple[int, str]:
    """Return (status, redirect target) without following the redirect."""
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **kw):  # noqa: D401 - stdlib hook
            return None

    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(url, timeout=10) as res:
            return res.status, res.headers.get("Location", "")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Location", "")


def test_oauth_start_redirects_to_google_with_every_scope(server) -> None:
    base, _ = server
    status, location = get(base + "/oauth/start")

    assert status == 302, "sign-in must redirect, not error"
    assert location.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    for scope in SCOPES:
        assert urllib.parse.quote(scope, safe="") in location
    assert "client_id=1234-demo.apps.googleusercontent.com" in location
    assert "access_type=offline" in location


def test_oauth_callback_reports_an_error_instead_of_dying(server) -> None:
    base, _ = server
    status, location = get(base + "/oauth/callback?error=access_denied")

    assert status == 302
    assert "oauth_error=access_denied" in location


def test_oauth_start_asks_for_a_client_when_none_is_stored(server) -> None:
    base, home = server
    (home / "client_secret.json").unlink()

    status, _ = get(base + "/oauth/start")
    assert status == 200  # the setup page, not an error

