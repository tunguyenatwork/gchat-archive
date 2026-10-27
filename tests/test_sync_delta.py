"""The sync bar reports what the run added, not the archive size."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from conftest import run


def test_archive_totals_counts_built_archive(archive: Path) -> None:
    run("build", home=archive)

    import os
    os.environ["GCHAT_ARCHIVE_HOME"] = str(archive)
    from gchat_archive import cli

    assert cli._archive_totals() == (4, 2)

    # one more message in raw, rebuilt, must show up in the count
    raw = archive / "accounts" / "demo" / "raw" / "spaces_AAA.jsonl"
    extra = {"name": "spaces/AAA/messages/4.4", "text": "Added later",
             "createTime": "2026-01-09T10:00:00Z",
             "sender": {"displayName": "Dana Reed", "email": "dana@example.com", "type": "HUMAN"}}
    raw.write_text(raw.read_text() + json.dumps(extra) + "\n")
    run("build", home=archive)

    assert cli._archive_totals() == (5, 2)


def test_cmd_sync_records_what_was_added(monkeypatch, archive: Path) -> None:
    import os
    os.environ["GCHAT_ARCHIVE_HOME"] = str(archive)
    from gchat_archive import cli, schedule

    # before: 4 messages / 2 conversations, after: 6 / 3
    totals = iter([(4, 2), (6, 3)])
    monkeypatch.setattr(cli, "_archive_totals", lambda: next(totals))
    monkeypatch.setattr(cli, "_run", lambda *a, **kw: 0)

    args = SimpleNamespace(full=False, check_all=False, retry_failed=False, space=None,
                           no_attachments=True, no_build=True, allow_shrink=False)
    assert cli.cmd_sync(args) == 0

    state = json.loads(schedule._state_file().read_text())
    assert state["added_messages"] == 2
    assert state["added_spaces"] == 1
    assert state["total_messages"] == 6
    assert state["total_spaces"] == 3


def test_status_exposes_the_deltas(archive: Path, monkeypatch) -> None:
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer

    monkeypatch.setenv("GCHAT_ARCHIVE_HOME", str(archive))
    (archive / "last_sync.json").write_text(json.dumps({
        "at": "2026-10-27T05:00:00", "ok": True, "detail": "",
        "added_messages": 37, "added_spaces": 2,
        "total_messages": 245864, "total_spaces": 585,
    }))

    from gchat_archive import serve

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    httpd.me = "demo@example.com"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/api/sync/status" % httpd.server_port, timeout=10) as res:
            body = json.load(res)
    finally:
        httpd.shutdown()

    assert body["added_messages"] == 37
    assert body["added_spaces"] == 2
    assert body["total_messages"] == 245864
    assert body["last_ok"] is True
