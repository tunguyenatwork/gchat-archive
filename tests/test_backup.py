"""Backups must rebuild an archive elsewhere, and must refuse to clobber one."""
from __future__ import annotations

import io
import json
import os
import tarfile
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from conftest import run

from gchat_archive.paths import ro_uri as _ro

from gchat_archive import backup


@pytest.fixture()
def populated(archive: Path) -> Path:
    """The synthetic archive, built, plus the files a real install would have."""
    run("build", home=archive)
    root = archive / "accounts" / "demo"
    (archive / "client_secret.json").write_text(json.dumps(
        {"installed": {"client_id": "1-x.apps.googleusercontent.com", "client_secret": "s", "project_id": "p"}}))
    (archive / "auth.json").write_text(json.dumps({"hash": "x", "salt": "y"}))
    (root / "archive_manage.json").write_text(json.dumps(
        {"excluded_spaces": ["spaces/BBB"], "deleted_spaces": [],
         "deleted_messages": [], "deleted_attachments": []}))
    (root / "raw" / "spaces_AAA.jsonl").write_text(
        (root / "raw" / "spaces_AAA.jsonl").read_text())
    return archive


def test_config_backup_holds_settings_but_not_the_database(populated: Path, tmp_path: Path) -> None:
    out = backup.create("config", dest=tmp_path / "cfg.tar.gz", root=populated)

    with tarfile.open(out) as tar:
        names = set(tar.getnames())
        manifest = json.loads(tar.extractfile("manifest.json").read())

    assert "accounts.json" in names
    assert "client_secret.json" in names
    assert "auth.json" in names
    assert "accounts/demo/archive_manage.json" in names
    assert not any(n.endswith("chats.sqlite") for n in names)
    assert manifest["kind"] == "config"
    assert manifest["contains_secrets"] is True
    assert manifest["counts"]["messages"] == 4


def test_archive_backup_restores_a_working_database(populated: Path, tmp_path: Path) -> None:
    out = backup.create("archive", dest=tmp_path / "arch.tar.gz", root=populated)
    with tarfile.open(out) as tar:
        assert "accounts/demo/chats.sqlite" in tar.getnames()

    fresh = tmp_path / "newsystem"
    result = backup.restore(out, root=fresh)

    assert result["ok"] is True
    assert result["counts"]["messages"] == 4
    db = fresh / "accounts" / "demo" / "chats.sqlite"
    assert db.is_file()

    import sqlite3
    con = sqlite3.connect(_ro(db), uri=True)
    try:
        assert con.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 4
        rules = json.loads((fresh / "accounts/demo/archive_manage.json").read_text())
        assert rules["excluded_spaces"] == ["spaces/BBB"]
    finally:
        con.close()

    # secrets come back owner-only (POSIX only: Windows has no mode bits)
    if os.name != "nt":
        assert (fresh / "client_secret.json").stat().st_mode & 0o077 == 0


def test_restore_asks_before_overwriting(populated: Path, tmp_path: Path) -> None:
    out = backup.create("archive", dest=tmp_path / "arch.tar.gz", root=populated)

    first = backup.restore(out, root=populated)
    assert first["ok"] is False and first["needs_confirm"] is True

    forced = backup.restore(out, force=True, root=populated)
    assert forced["ok"] is True
    assert forced["previous_config_saved"] is True
    assert list(populated.glob("pre-restore-*")), "the previous settings should be kept"


def test_restore_rejects_path_traversal(tmp_path: Path) -> None:
    evil = tmp_path / "evil.tar.gz"
    with tarfile.open(evil, "w:gz") as tar:
        payload = json.dumps({"kind": "config"}).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
        bad = tarfile.TarInfo("../escaped.txt")
        bad.size = 4
        tar.addfile(bad, io.BytesIO(b"oops"))

    with pytest.raises(ValueError, match="unsafe path"):
        backup.restore(evil, root=tmp_path / "dest")


def test_http_download_and_estimate(populated: Path, monkeypatch) -> None:
    # The fixture carries a stub auth.json; drop it so this test exercises the
    # download path rather than the login gate (which has its own tests).
    (populated / "auth.json").unlink()
    monkeypatch.setenv("GCHAT_ARCHIVE_HOME", str(populated))
    from gchat_archive import serve

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    httpd.me = "demo@example.com"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % httpd.server_port
    try:
        with urllib.request.urlopen(base + "/api/backup/estimate", timeout=10) as res:
            estimate = json.load(res)
        assert set(estimate["kinds"]) == {"config", "archive", "full"}
        assert estimate["bytes"]["full"] >= estimate["bytes"]["archive"]

        with urllib.request.urlopen(base + "/api/backup?kind=config", timeout=30) as res:
            body = res.read()
            assert res.headers["Content-Type"] == "application/gzip"
            assert "attachment;" in res.headers["Content-Disposition"]
        assert body[:2] == b"\x1f\x8b", "should be a gzip stream"
    finally:
        httpd.shutdown()
