"""The browser harvester must produce the same raw format as the API source."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from conftest import run

from gchat_archive import browser_source


ROWS = [
    {"sender": "Dana Reed", "time": "9:14 AM", "text": "Kickoff for the contractor onboarding"},
    {"sender": "Dana Reed", "time": "9:15 AM", "text": "Agenda is in the pinned doc"},
    {"sender": "Tu Nguyen", "time": "9:20 AM", "text": "Thanks — reviewing now"},
]


def test_to_raw_shapes_messages_like_the_api() -> None:
    raw = browser_source.to_raw("demo", ROWS)
    assert len(raw) == 3
    first = raw[0]
    assert first["name"].startswith("spaces/demo/messages/")
    assert first["text"] == "Kickoff for the contractor onboarding"
    assert first["sender"]["displayName"] == "Dana Reed"
    assert first["createTime"] == "9:14 AM"

    # Ids are content-derived, so a second harvest of the same page dedupes.
    assert [r["name"] for r in raw] == [r["name"] for r in browser_source.to_raw("demo", ROWS)]
    assert len({r["name"] for r in raw}) == 3


def test_write_conversation_appends_once(tmp_path: Path) -> None:
    root = tmp_path / "archive"
    raw = browser_source.to_raw("demo", ROWS)

    assert browser_source.write_conversation(root, "demo", raw) == 3
    assert browser_source.write_conversation(root, "demo", raw) == 0      # idempotent
    assert browser_source.write_conversation(root, "demo", raw[1:]) == 0  # subset

    lines = (root / "raw" / "demo.jsonl").read_text().strip().splitlines()
    assert len(lines) == 3
    assert json.loads(lines[0])["source"] == "browser"


def test_browser_rows_build_into_a_searchable_archive(archive: Path) -> None:
    root = archive / "accounts" / "demo"
    rows = browser_source.to_raw("spaces/CCC", [
        {"sender": "Dana Reed", "time": "11:02 AM", "text": "Browser harvest reaches the index"},
    ])
    browser_source.write_conversation(root, "spaces/CCC", rows)
    # give the conversation a label the same way fetch.py would
    (root / "raw" / "spaces_CCC.meta.json").write_text(
        json.dumps({"id": "spaces/CCC", "display_name": "Harvested Room", "type": "SPACE"})
    )

    run("build", home=archive)

    con = sqlite3.connect("file:%s?mode=ro" % (root / "chats.sqlite"), uri=True)
    try:
        hits = con.execute(
            "SELECT m.text FROM messages_fts f JOIN messages m ON m.rowid = f.rowid "
            "WHERE messages_fts MATCH ?", ('"browser"',)
        ).fetchall()
        labels = dict(con.execute("SELECT id, label FROM spaces"))
    finally:
        con.close()

    assert hits == [("Browser harvest reaches the index",)]
    assert labels["spaces/CCC"] == "Harvested Room"
