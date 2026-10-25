#!/usr/bin/env python3
"""Durable user decisions about the archive: exclusions and deletions.

build.py regenerates chats.sqlite from raw/ on every sync, so a delete that only
touched the DB would silently come back the next morning. Everything the user
decides here is stored in archive_manage.json and re-applied on every rebuild.

    excluded_spaces     don't fetch new messages again (history kept, still listed)
    deleted_spaces      purged from the DB, raw JSONL and attachment files
    deleted_messages    per-message purge, survives re-sync
    deleted_attachments per-file purge, keyed by the same key as attachments.jsonl
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import paths

ARCHIVE = paths.data_home()
# No module-level path: the rules file belongs to whichever account is active.

KEYS = ("excluded_spaces", "deleted_spaces", "deleted_messages", "deleted_attachments")


def default_path() -> Path:
    """Rules file for the account currently in use (late import avoids a cycle)."""
    from . import accounts

    return accounts.active_root() / "archive_manage.json"


def load_from(path: Path) -> dict:
    """Read a specific rules file (build.py --rules), falling back to defaults."""
    data = {key: [] for key in KEYS}
    if path.exists():
        try:
            stored = json.loads(path.read_text())
        except json.JSONDecodeError:
            stored = {}
        for key in KEYS:
            value = stored.get(key)
            if isinstance(value, list):
                data[key] = [str(v) for v in value]
    return data


def load(path: Path | None = None) -> dict:
    data = {key: [] for key in KEYS}
    target = path or default_path()
    if target.exists():
        try:
            stored = json.loads(target.read_text())
        except json.JSONDecodeError:
            stored = {}
        for key in KEYS:
            value = stored.get(key)
            if isinstance(value, list):
                data[key] = [str(v) for v in value]
    return data


def save(data: dict, path: Path | None = None) -> None:
    target = path or default_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    clean = {key: sorted(set(data.get(key) or [])) for key in KEYS}
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(clean, indent=2))
    os.replace(tmp, target)


def add(section: str, values) -> dict:
    if section not in KEYS:
        raise KeyError(section)
    data = load()
    data[section] = sorted(set(data[section]) | set(values))
    save(data)
    return data


def remove(section: str, values) -> dict:
    if section not in KEYS:
        raise KeyError(section)
    data = load()
    data[section] = sorted(set(data[section]) - set(values))
    save(data)
    return data


def as_sets(data: dict | None = None) -> dict:
    data = data or load()
    return {key: set(data.get(key) or []) for key in KEYS}


def attachment_key(att: dict) -> str:
    """Stable id for an attachment, matching attachments.jsonl keys.

    Drive-backed files are identified by their Drive file id (the same file can
    be shared many times); uploads by their Chat attachment id.
    """
    drive = (att.get("driveDataRef") or {}).get("driveFileId")
    if drive:
        return "drive_" + drive
    return "att_" + (att.get("name") or "").rsplit("/", 1)[-1]
