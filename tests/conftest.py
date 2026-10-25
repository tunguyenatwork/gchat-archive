"""Fixtures: a synthetic archive in a temp dir, built through the real CLI."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ACCOUNTS = {
    "active": "demo",
    "accounts": [
        {
            "slug": "demo",
            "email": "demo@example.com",
            "label": "Demo",
            "token": "",
            "sync": True,
        }
    ],
}

STATE = {
    "me": "demo@example.com",
    "spaces": {
        "spaces/AAA": {
            "id": "spaces/AAA",
            "display_name": "Project Room",
            "type": "SPACE",
            "threading": "THREADED_MESSAGES",
            "member_count": 4,
        },
        "spaces/BBB": {
            "id": "spaces/BBB",
            "type": "DIRECT_MESSAGE",
            "threading": "THREADED_MESSAGES",
            "member_count": 2,
        },
    },
}


def _msg(name, text, when, sender, email):
    return {
        "name": name,
        "text": text,
        "createTime": when,
        "lastUpdateTime": when,
        "sender": {"displayName": sender, "email": email, "type": "HUMAN"},
    }


@pytest.fixture()
def archive(tmp_path: Path) -> Path:
    """A data home with two conversations and a handful of messages."""
    home = tmp_path / "home"
    root = home / "accounts" / "demo"
    (root / "raw").mkdir(parents=True)
    home.mkdir(exist_ok=True)
    (home / "accounts.json").write_text(json.dumps(ACCOUNTS))
    (root / "state.json").write_text(json.dumps(STATE))

    space_rows = [
        _msg("spaces/AAA/messages/1.1", "Kickoff for the contractor onboarding", "2026-01-05T10:00:00Z", "Dana Reed", "dana@example.com"),
        _msg("spaces/AAA/messages/2.2", "Second message about onboarding steps", "2026-01-06T10:00:00Z", "Demo Person", "demo@example.com"),
        _msg("spaces/AAA/messages/3.3", "Nothing to see here", "2026-01-07T10:00:00Z", "Dana Reed", "dana@example.com"),
    ]
    dm_rows = [
        _msg("spaces/BBB/messages/9.9", "Direct note about the deadline", "2026-01-08T10:00:00Z", "Dana Reed", "dana@example.com"),
    ]
    for space_id, rows in (("spaces_AAA", space_rows), ("spaces_BBB", dm_rows)):
        with (root / "raw" / ("%s.jsonl" % space_id)).open("w") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")
        (root / "raw" / ("%s.meta.json" % space_id)).write_text(
            json.dumps(STATE["spaces"][space_id.replace("_", "/", 1)])
        )
    return home


def run(module: str, *args: str, home: Path, check: bool = True) -> subprocess.CompletedProcess:
    env = dict(os.environ, GCHAT_ARCHIVE_HOME=str(home), PYTHONPATH=str(Path(__file__).parents[1] / "src"))
    proc = subprocess.run(
        [sys.executable, "-m", "gchat_archive.%s" % module, *args],
        capture_output=True, text=True, env=env,
    )
    if check and proc.returncode != 0:
        raise AssertionError("%s failed (%s)\nstdout:\n%s\nstderr:\n%s"
                             % (module, proc.returncode, proc.stdout, proc.stderr))
    return proc
