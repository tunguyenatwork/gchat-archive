#!/usr/bin/env python3
"""Registry of Google accounts, each with its own isolated archive.

One account = one OAuth token + one archive directory (raw JSONL, SQLite,
attachments). Nothing is shared, so a broken or removed account can never damage
another one, and re-syncing one account cannot touch the others' history.

Layout:
    <data>/accounts.json
    <data>/accounts/<slug>/   raw/ chats.sqlite attachments/ ...
    <data>/tokens/<slug>.json  tokens for accounts added here

<data> is ``~/.gchat-archive`` unless ``GCHAT_ARCHIVE_HOME`` says otherwise.
An older single-account layout (archive files directly under <data>) is
migrated into accounts/<slug>/ on first use, so existing archives keep working.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

from . import paths

ROOT = paths.data_home()
REGISTRY = ROOT / "accounts.json"
ACCOUNTS_DIR = ROOT / "accounts"
TOKENS_DIR = ROOT / "tokens"
# No implicit token: every install signs in its own account through OAuth.
DEFAULT_TOKEN = TOKENS_DIR / "default.json"

# Files that belong to an account's archive.
ARCHIVE_FILES = ("raw", "attachments", "attachments.jsonl", "attachments_text.jsonl")
ARCHIVE_GLOBS = ("chats.sqlite*", "state.json", "archive_manage.json", "sync.log", "final_sync.log")


def slug_for(email: str) -> str:
    """Directory-safe, stable id for an account email."""
    cleaned = re.sub(r"[^a-z0-9]+", "_", (email or "default").lower()).strip("_")
    return cleaned or "default"


def load() -> dict:
    if REGISTRY.exists():
        try:
            data = json.loads(REGISTRY.read_text())
            if isinstance(data, dict) and isinstance(data.get("accounts"), list):
                return data
        except json.JSONDecodeError:
            pass
    return {"active": None, "accounts": []}


def save(data: dict) -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    tmp = REGISTRY.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, REGISTRY)


def account_root(slug: str) -> Path:
    return ACCOUNTS_DIR / slug


def token_path(slug: str) -> Path:
    for entry in load()["accounts"]:
        if entry.get("slug") == slug:
            return Path(entry.get("token") or DEFAULT_TOKEN)
    return DEFAULT_TOKEN


def _legacy_files_present() -> bool:
    return (ROOT / "raw").exists() or (ROOT / "chats.sqlite").exists()


def _detect_email() -> str:
    """Email of the existing archive, from state.json, else the token file."""
    state = ROOT / "state.json"
    if state.exists():
        try:
            me = json.loads(state.read_text()).get("me")
            if me:
                return me
        except json.JSONDecodeError:
            pass
    if DEFAULT_TOKEN.exists():
        try:
            return json.loads(DEFAULT_TOKEN.read_text()).get("account") or "default"
        except json.JSONDecodeError:
            pass
    return "default"


def migrate_legacy() -> dict:
    """Move a single-account archive into accounts/<slug>/ exactly once."""
    data = load()
    if data["accounts"] or not _legacy_files_present():
        return data

    email = _detect_email()
    slug = slug_for(email)
    dest = account_root(slug)
    dest.mkdir(parents=True, exist_ok=True)

    for name in ARCHIVE_FILES:
        src = ROOT / name
        if src.exists():
            shutil.move(str(src), str(dest / name))
    for pattern in ARCHIVE_GLOBS:
        for src in ROOT.glob(pattern):
            if src.is_file():
                shutil.move(str(src), str(dest / src.name))

    data["accounts"].append(
        {
            "slug": slug,
            "email": email,
            "label": email,
            "token": str(DEFAULT_TOKEN),
            "added_at": None,
        }
    )
    _rewrite_manifest_paths(dest)
    data["active"] = slug
    save(data)
    return data


def _rewrite_manifest_paths(dest: Path) -> None:
    """Point attachment records at the account directory after a move.

    Silently skipping this makes every attachment resolve to a path that no
    longer exists, so the reader reports "not archived locally" for all of them.
    """
    manifest = dest / "attachments.jsonl"
    if not manifest.exists():
        return
    fixed = []
    for line in manifest.read_text().splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        old = Path(rec.get("path") or "")
        if old and not old.exists():
            candidate = dest / "attachments" / old.name
            if candidate.exists():
                rec["path"] = str(candidate)
        fixed.append(json.dumps(rec))
    manifest.write_text("\n".join(fixed) + "\n")


def repair_manifests() -> int:
    """Fix stale attachment paths in every registered account. Idempotent."""
    touched = 0
    for entry in ensure()["accounts"]:
        dest = account_root(entry["slug"])
        manifest = dest / "attachments.jsonl"
        if not manifest.exists():
            continue
        before = manifest.read_text()
        _rewrite_manifest_paths(dest)
        if manifest.read_text() != before:
            touched += 1
    return touched


def ensure() -> dict:
    """Load the registry, migrating the legacy layout if this is a first run."""
    data = load()
    if not data["accounts"]:
        data = migrate_legacy()
        if not data["accounts"] and not (ROOT / "accounts").exists():
            # Nothing to migrate, nothing registered, no archive on disk: this is
            # a fresh install. Do not invent an account — `gchat-archive login`
            # creates one when the user actually signs in.
            return {"active": "", "accounts": []}
        if not data["accounts"]:
            email = _detect_email()
            slug = slug_for(email)
            account_root(slug).mkdir(parents=True, exist_ok=True)
            data = {
                "active": slug,
                "accounts": [
                    {
                        "slug": slug,
                        "email": email,
                        "label": email,
                        "token": str(DEFAULT_TOKEN),
                        "added_at": None,
                    }
                ],
            }
            save(data)
    if data["accounts"] and (
        not data.get("active") or not any(a["slug"] == data["active"] for a in data["accounts"])
    ):
        data["active"] = data["accounts"][0]["slug"]
        save(data)
    return data


def active_slug() -> str:
    override = os.environ.get("HERMES_ARCHIVE_ACCOUNT")
    if override:
        return slug_for(override)
    return ensure()["active"]


def active_root() -> Path:
    root = account_root(active_slug())
    root.mkdir(parents=True, exist_ok=True)
    return root


def active_token() -> Path:
    return token_path(active_slug())


def add_account(email: str, token_file: Path, label: str | None = None) -> dict:
    """Register an account, moving its token somewhere stable."""
    data = ensure()
    slug = slug_for(email)
    TOKENS_DIR.mkdir(parents=True, exist_ok=True)
    dest = TOKENS_DIR / ("%s.json" % slug)
    if Path(token_file).resolve() != dest.resolve():
        shutil.copy2(token_file, dest)
    # A token carries a refresh token plus the client secret: owner-only.
    os.chmod(dest, 0o600)

    for entry in data["accounts"]:
        if entry["slug"] == slug:
            entry.update({"email": email, "token": str(dest), "label": label or email})
            save(data)
            return data
    data["accounts"].append(
        {
            "slug": slug,
            "email": email,
            "label": label or email,
            "token": str(dest),
            "sync": True,
            "added_at": __import__("datetime").datetime.now().astimezone().isoformat(timespec="seconds"),
        }
    )
    save(data)
    return data


def remove_account(slug: str, delete_archive: bool = True) -> dict:
    """Drop an account and (optionally) its archive. Never removes the last one."""
    data = ensure()
    keep = [a for a in data["accounts"] if a["slug"] != slug]
    if not keep:
        raise ValueError("cannot remove the only account")
    token = next((a.get("token") for a in data["accounts"] if a["slug"] == slug), None)
    data["accounts"] = keep
    if data.get("active") == slug:
        data["active"] = keep[0]["slug"]
    save(data)

    if token:
        try:
            path = Path(token)
            if path.exists() and TOKENS_DIR in path.resolve().parents:
                path.unlink()
        except OSError:
            pass
    if delete_archive:
        target = account_root(slug)
        try:
            # Compare resolved paths on both sides: on macOS the home path can
            # resolve through /System/Volumes/Data, and an unresolved check
            # silently fails, leaving a multi-hundred-MB directory orphaned.
            if target.exists() and ACCOUNTS_DIR.resolve() in target.resolve().parents:
                shutil.rmtree(target)
        except OSError:
            pass
    return data


def account_enabled(entry: dict) -> bool:
    """Per-account sync switch (absent means enabled, for older registries)."""
    return bool(entry.get("sync", True))


def images_enabled(slug: str | None = None) -> bool:
    """Whether this account also downloads image attachments (default: off)."""
    target = slug or active_slug()
    for entry in ensure()["accounts"]:
        if entry["slug"] == target:
            return bool(entry.get("images", False))
    return False


def set_images(slug: str, enabled: bool) -> dict:
    data = ensure()
    for entry in data["accounts"]:
        if entry["slug"] == slug:
            entry["images"] = bool(enabled)
            save(data)
            return data
    raise ValueError("unknown account")


def set_sync(slug: str, enabled: bool) -> dict:
    data = ensure()
    for entry in data["accounts"]:
        if entry["slug"] == slug:
            entry["sync"] = bool(enabled)
            save(data)
            return data
    raise ValueError("unknown account")


def set_active(slug: str) -> dict:
    data = ensure()
    if not any(a["slug"] == slug for a in data["accounts"]):
        raise ValueError("unknown account")
    data["active"] = slug
    save(data)
    return data
