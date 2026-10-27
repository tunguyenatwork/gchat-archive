"""accounts.json: what it is for, and the token path it stores.

The registry keeps a *path* to each account's token. That path is absolute, so a
backup restored under a different home directory used to leave every account
pointing at a file that does not exist on the new machine — sync then failed with
a missing token even though the token was right there in <data>/tokens/.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture()
def home(tmp_path: Path, monkeypatch):
    data = tmp_path / "machine-b"
    (data / "tokens").mkdir(parents=True)
    monkeypatch.setenv("GCHAT_ARCHIVE_HOME", str(data))
    return data


def registry_entry(*, slug: str = "demo", token: str) -> dict:
    return {"slug": slug, "email": "demo@example.com", "label": "Demo",
            "token": token, "sync": True}


def write_registry(home: Path, entry: dict) -> None:
    (home / "accounts.json").write_text(json.dumps({"active": entry["slug"], "accounts": [entry]}))


def test_stale_token_path_is_repaired_after_a_restore(home: Path) -> None:
    # what a backup made on machine A contains: A's absolute path
    write_registry(home, registry_entry(token="/Users/someone-else/.gchat-archive/tokens/demo.json"))
    (home / "tokens" / "demo.json").write_text('{"refresh_token": "x"}')

    from gchat_archive import accounts

    resolved = accounts.active_token()

    assert resolved == home / "tokens" / "demo.json"
    assert resolved.exists()
    # and the file on disk was corrected, not just the in-memory copy
    assert json.loads((home / "accounts.json").read_text())["accounts"][0]["token"] == str(resolved)


def test_a_valid_path_is_left_alone(home: Path) -> None:
    external = home / "elsewhere" / "keep.json"
    external.parent.mkdir()
    external.write_text("{}")
    write_registry(home, registry_entry(token=str(external)))
    (home / "tokens" / "demo.json").write_text("{}")

    from gchat_archive import accounts

    assert accounts.active_token() == external


def test_missing_everywhere_is_reported_not_invented(home: Path) -> None:
    write_registry(home, registry_entry(token="/nope/token.json"))

    from gchat_archive import accounts

    assert accounts.active_token() == Path("/nope/token.json")
    assert not accounts.active_token().exists()
