"""The "Sync now" button: start a sync, watch /api/sync/status, refuse a second.

`GCHAT_SYNC_COMMAND` exists so this runs the button's real code path without
touching the network: it replaces the argv of the child process.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

ACCOUNTS = {"active": "", "accounts": []}


@pytest.fixture()
def server(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    (home / "accounts").mkdir(parents=True)
    (home / "accounts.json").write_text(json.dumps(ACCOUNTS))
    monkeypatch.setenv("GCHAT_ARCHIVE_HOME", str(home))
    # a sync that finishes quickly, prints something, and needs no Google access
    monkeypatch.setenv("GCHAT_SYNC_COMMAND", "sh -c 'echo fetching; sleep 1; echo done'")

    from gchat_archive import serve

    serve._SYNC.update({"proc": None, "started_at": 0.0, "finished_at": 0.0, "returncode": None})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    httpd.me = ""
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield "http://127.0.0.1:%d" % httpd.server_port, home
    finally:
        httpd.shutdown()


def call(url: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            return res.status, json.loads(res.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def test_status_is_idle_before_any_sync(server) -> None:
    base, _ = server
    status, body = call(base + "/api/sync/status")
    assert status == 200
    assert body["running"] is False
    assert body["returncode"] is None


def test_sync_now_runs_to_completion(server) -> None:
    base, home = server
    status, body = call(base + "/api/sync/start", {})
    assert status == 200, body
    assert body["status"]["running"] is True

    deadline = time.time() + 20
    state = {"running": True, "returncode": None, "tail": []}
    while time.time() < deadline:
        _, state = call(base + "/api/sync/status")
        if not state["running"]:
            break
        time.sleep(0.3)

    assert state["running"] is False
    assert state["returncode"] == 0
    # the child's output is captured for the UI
    assert any("fetching" in line for line in state["tail"]) or "fetching" in str(state["tail"])


def test_second_start_is_refused_while_running(server) -> None:
    base, _ = server
    first, _ = call(base + "/api/sync/start", {})
    assert first == 200
    second, body = call(base + "/api/sync/start", {})
    assert second == 409
    assert "already running" in body["error"]
