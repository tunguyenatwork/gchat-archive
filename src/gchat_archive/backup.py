"""Backup and restore: one tar.gz that rebuilds this archive on another machine.

Three sizes, because the parts differ wildly in cost:

    config    credentials, accounts, rules, sync watermarks   (~10 KB)
    archive   config + chats.sqlite                           (~500 MB)
    full      archive + raw JSONL + downloaded attachments    (~3 GB)

A backup contains secrets (OAuth client, tokens, password hash). They are what
makes a restore self-sufficient, so they are included — and the UI says so.
"""
from __future__ import annotations

import json
import os
import shutil
import tarfile
import tempfile
import time
from pathlib import Path

from . import __version__, accounts, paths

KINDS = ("config", "archive", "full")

# Never worth archiving: logs and anything a previous restore left behind.
SKIP_NAMES = {
    "serve.log", "sync.log", "final_sync.log", "images.log", "attachments.log",
    "browser_report.json", "last_sync.json", "backup-upload.tar.gz",
}
SKIP_DIRS = {"chrome-profile", "__pycache__", ".restore-staging"}

CONFIG_FILES = ("accounts.json", "client_secret.json", "auth.json")
ACCOUNT_CONFIG = ("archive_manage.json", "state.json", "archive_manage.json.bak")


def _rel(path: Path, root: Path) -> str:
    return str(path.relative_to(root))


def _included(root: Path, kind: str) -> list[tuple[Path, str]]:
    """(source path, arcname) pairs for this backup kind."""
    if kind not in KINDS:
        raise ValueError("unknown backup kind %r" % kind)
    out: list[tuple[Path, str]] = []

    for name in CONFIG_FILES:
        candidate = root / name
        if candidate.is_file():
            out.append((candidate, name))

    tokens = root / "tokens"
    if tokens.is_dir():
        for entry in sorted(tokens.iterdir()):
            if entry.is_file() and entry.name not in SKIP_NAMES:
                out.append((entry, "tokens/%s" % entry.name))

    for account_dir in sorted((root / "accounts").glob("*")) if (root / "accounts").is_dir() else []:
        if not account_dir.is_dir():
            continue
        slug = account_dir.name
        for name in ACCOUNT_CONFIG:
            candidate = account_dir / name
            if candidate.is_file():
                out.append((candidate, "accounts/%s/%s" % (slug, name)))

        db = account_dir / "chats.sqlite"
        if kind in ("archive", "full") and db.is_file():
            out.append((db, "accounts/%s/chats.sqlite" % slug))

        for meta in sorted(account_dir.glob("*.meta.json")):
            out.append((meta, "accounts/%s/%s" % (slug, meta.name)))

        if kind == "full":
            raw = account_dir / "raw"
            if raw.is_dir():
                for entry in sorted(raw.glob("*.jsonl")):
                    out.append((entry, "accounts/%s/raw/%s" % (slug, entry.name)))
            for name in ("attachments.jsonl", "attachments_text.jsonl"):
                candidate = account_dir / name
                if candidate.is_file():
                    out.append((candidate, "accounts/%s/%s" % (slug, name)))
            files = account_dir / "attachments"
            if files.is_dir():
                for entry in sorted(files.iterdir()):
                    if entry.is_file():
                        out.append((entry, "accounts/%s/attachments/%s" % (slug, entry.name)))
    return out


def counts(root: Path | None = None) -> dict:
    """Cheap summary used by the manifest and the UI."""
    root = root or paths.data_home()
    summary = {"accounts": [], "messages": 0, "spaces": 0}
    registry = accounts.load() if root == paths.data_home() else {}
    for entry in registry.get("accounts", []):
        summary["accounts"].append({"slug": entry["slug"], "email": entry.get("email", "")})
    for account_dir in sorted((root / "accounts").glob("*/chats.sqlite")):
        try:
            import sqlite3

            con = sqlite3.connect(paths.ro_uri(account_dir), uri=True, timeout=5)
            summary["messages"] += con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            summary["spaces"] += con.execute("SELECT COUNT(*) FROM spaces").fetchone()[0]
            con.close()
        except Exception:  # a corrupt or half-written DB must not break a backup
            continue
    return summary


def estimate(root: Path | None = None) -> dict:
    """Bytes per kind, so the UI can warn before a 3 GB download."""
    root = root or paths.data_home()
    sizes = {}
    for kind in KINDS:
        total = 0
        for source, _ in _included(root, kind):
            try:
                total += source.stat().st_size
            except OSError:
                continue
        sizes[kind] = total
    return sizes


def create(kind: str = "archive", dest: Path | str | None = None, root: Path | None = None) -> Path:
    """Write a backup and return its path."""
    root = root or paths.data_home()
    items = _included(root, kind)
    if not items:
        raise ValueError("nothing to back up — is this a fresh install?")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = Path(dest) if dest else Path(tempfile.gettempdir()) / ("gchat-archive-%s-%s.tar.gz" % (kind, stamp))
    target.parent.mkdir(parents=True, exist_ok=True)

    manifest = {
        "format": 1,
        "kind": kind,
        "app_version": __version__,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "data_home": str(root),
        "counts": counts(root),
        "files": len(items),
        "bytes": sum(p.stat().st_size for p, _ in items if p.exists()),
        "contains_secrets": True,
    }

    with tarfile.open(target, "w:gz") as tar:
        payload = json.dumps(manifest, indent=2).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(payload)
        info.mtime = int(time.time())
        tar.addfile(info, __import__("io").BytesIO(payload))
        for source, arcname in items:
            if source.exists():
                tar.add(str(source), arcname=arcname, recursive=False)
    return target


def inspect(archive_path: Path | str) -> dict:
    """Read the manifest without extracting anything."""
    with tarfile.open(archive_path, "r:gz") as tar:
        try:
            member = tar.getmember("manifest.json")
        except KeyError:
            raise ValueError("not a gchat-archive backup (no manifest.json)")
        manifest = json.loads(tar.extractfile(member).read().decode())
    manifest["archive_bytes"] = Path(archive_path).stat().st_size
    return manifest


def _safe_members(tar: tarfile.TarFile) -> list[tarfile.TarInfo]:
    out = []
    for member in tar.getmembers():
        name = member.name
        if name.startswith("/") or ".." in Path(name).parts:
            raise ValueError("refusing a backup with an unsafe path: %s" % name)
        if not (member.isfile() or member.isdir()):
            raise ValueError("refusing a backup with a special entry: %s" % name)
        out.append(member)
    return out


def restore(archive_path: Path | str, force: bool = False, root: Path | None = None) -> dict:
    """Install a backup into the data directory.

    Without ``force`` this refuses to overwrite an archive that already has
    accounts, because silently replacing somebody's history is unforgivable.
    """
    root = root or paths.data_home()
    manifest = inspect(archive_path)

    occupied = (root / "accounts.json").exists() or any((root / "accounts").glob("*")) if (root / "accounts").is_dir() else False
    if occupied and not force:
        return {"ok": False, "needs_confirm": True, "manifest": manifest}

    staging = root / ".restore-staging"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    try:
        with tarfile.open(archive_path, "r:gz") as tar:
            members = _safe_members(tar)
            for member in members:
                tar.extract(member, path=staging)

        # Keep the previous configuration so a wrong restore is recoverable.
        keep = root / ("pre-restore-%s" % time.strftime("%Y%m%d-%H%M%S"))
        moved = []
        for name in CONFIG_FILES:
            current = root / name
            if current.exists():
                keep.mkdir(exist_ok=True)
                shutil.copy2(current, keep / name)
                moved.append(name)

        installed = 0
        for source in sorted(staging.rglob("*")):
            if source.is_dir():
                continue
            rel = source.relative_to(staging)
            if rel.as_posix() == "manifest.json":
                continue
            dest = root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(dest.suffix + ".incoming")
            shutil.copy2(source, tmp)
            if dest.exists():
                dest.unlink()
            tmp.replace(dest)
            if dest.name in ("client_secret.json", "auth.json") or "tokens" in rel.parts:
                os.chmod(dest, 0o600)
            installed += 1
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    return {
        "ok": True,
        "installed": installed,
        "manifest": manifest,
        "restored_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "previous_config_saved": bool(moved),
        "counts": counts(root),
    }
