"""The database is a projection of raw JSONL minus the rules file."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from conftest import run

from gchat_archive.paths import ro_uri as _ro


def db(home: Path) -> Path:
    return home / "accounts" / "demo" / "chats.sqlite"


def query(home: Path, sql: str, args: tuple = ()):
    con = sqlite3.connect(_ro(db(home)), uri=True)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def test_build_indexes_messages_and_search(archive: Path) -> None:
    run("build", home=archive)

    assert query(archive, "SELECT COUNT(*) FROM messages")[0][0] == 4
    assert query(archive, "SELECT COUNT(*) FROM spaces")[0][0] == 2

    hits = query(archive, "SELECT m.text FROM messages_fts f JOIN messages m ON m.rowid = f.rowid "
                          "WHERE messages_fts MATCH ?", ('"onboarding"',))
    assert len(hits) == 2, hits

    # Conversations get readable labels rather than opaque ids.
    labels = dict(query(archive, "SELECT id, label FROM spaces"))
    assert labels["spaces/AAA"] == "Project Room"
    assert "Dana Reed" in labels["spaces/BBB"]


def test_deleted_message_rule_survives_rebuild(archive: Path) -> None:
    run("build", home=archive)
    rules = archive / "accounts" / "demo" / "archive_manage.json"
    rules.write_text(json.dumps({"excluded_spaces": [], "deleted_spaces": [],
                                 "deleted_messages": ["spaces/AAA/messages/3.3"],
                                 "deleted_attachments": []}))

    run("build", home=archive)

    assert query(archive, "SELECT COUNT(*) FROM messages")[0][0] == 3
    assert query(archive, "SELECT COUNT(*) FROM messages WHERE text LIKE '%Nothing to see%'")[0][0] == 0
    # and the index agrees: no dangling row, nothing left to match
    assert query(archive, "SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH ?", ('"nothing"',))[0][0] == 0


def test_excluded_space_is_flagged_not_deleted(archive: Path) -> None:
    rules = archive / "accounts" / "demo" / "archive_manage.json"
    rules.write_text(json.dumps({"excluded_spaces": ["spaces/BBB"], "deleted_spaces": [],
                                 "deleted_messages": [], "deleted_attachments": []}))

    run("build", home=archive)

    rows = dict(query(archive, "SELECT id, excluded FROM spaces"))
    assert rows["spaces/BBB"] == 1
    assert rows["spaces/AAA"] == 0
    assert query(archive, "SELECT COUNT(*) FROM messages")[0][0] == 4


def test_shrink_guard_refuses_a_lossy_rebuild(archive: Path) -> None:
    run("build", home=archive)

    # Simulate a half-written raw archive: fewer messages than the DB holds.
    raw = archive / "accounts" / "demo" / "raw" / "spaces_AAA.jsonl"
    lines = raw.read_text().splitlines()
    raw.write_text("\n".join(lines[:1]) + "\n")

    proc = run("build", home=archive, check=False)
    assert proc.returncode == 2
    assert "Refusing to rebuild" in proc.stderr
    assert query(archive, "SELECT COUNT(*) FROM messages")[0][0] == 4  # untouched

    # ...and the escape hatch works when the loss is intended.
    run("build", "--allow-shrink", home=archive)
    assert query(archive, "SELECT COUNT(*) FROM messages")[0][0] == 2
