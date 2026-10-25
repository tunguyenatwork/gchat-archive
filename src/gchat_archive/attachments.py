#!/usr/bin/env python3
"""Download and text-extract the documents shared in Google Chat, so their
contents are searchable alongside message text.

    venv/bin/python3 attachments.py download     # fetch documents (Drive + uploads)
    venv/bin/python3 attachments.py extract      # text out of each file
    venv/bin/python3 attachments.py stats

Writes:
  ~/.hermes/chat-archive/attachments/<key>.<ext>   downloaded bytes
  ~/.hermes/chat-archive/attachments.jsonl          one manifest line per file
  ~/.hermes/chat-archive/attachments_text.jsonl     extracted text, keyed by message

Only text-bearing types are fetched (PDF, Office, CSV/TXT, Google Docs/Sheets/
Slides). Images and video are deliberately skipped: they carry no searchable
text without OCR, and OCR over 5k+ screenshots is not worth the cost here —
their filenames are still indexed from the message payload.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import api as gchat
from . import accounts
from . import manage

ARCHIVE = accounts.active_root()  # per-account archive directory
RAW = ARCHIVE / "raw"
FILES = ARCHIVE / "attachments"
MANIFEST = ARCHIVE / "attachments.jsonl"
TEXT = ARCHIVE / "attachments_text.jsonl"
# Extraction runs in the current interpreter: the [text] extra supplies the parsers.

MAX_BYTES = 25 * 1024 * 1024

GOOGLE_APPS = {
    "application/vnd.google-apps.document": ("txt", "text/plain"),
    "application/vnd.google-apps.spreadsheet": ("csv", "text/csv"),
    "application/vnd.google-apps.presentation": ("txt", "text/plain"),
}

EXTRACTABLE = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/msword": "doc",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.ms-excel": "xls",
    "text/csv": "csv",
    "text/plain": "txt",
    "text/markdown": "md",
    "application/json": "json",
    "text/html": "html",
    "application/rtf": "rtf",
}


def load_manifest() -> dict[str, dict]:
    done: dict[str, dict] = {}
    if MANIFEST.exists():
        for line in MANIFEST.read_text().splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("key"):
                done[rec["key"]] = rec
    return done


def iter_attachments():
    """Yield (message, space_id, attachment) for every attachment in the archive."""
    for path in sorted(RAW.glob("*.jsonl")):
        space_id = path.stem.replace("_", "/", 1)
        with path.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                for att in msg.get("attachment") or []:
                    yield msg, space_id, att


def attachment_key(att: dict) -> str:
    return manage.attachment_key(att)


IMAGE_EXT = {
    "image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp",
    "image/heic": "heic", "image/bmp": "bmp", "image/svg+xml": "svg", "image/tiff": "tiff",
}


def plan(att: dict) -> tuple[str, str] | None:
    """(extension, export_mime or '' for raw bytes), or None when not indexable."""
    ctype = att.get("contentType") or ""
    if ctype in GOOGLE_APPS:
        ext, export = GOOGLE_APPS[ctype]
        return ext, export
    if ctype in EXTRACTABLE:
        return EXTRACTABLE[ctype], ""
    # Images are only fetched when the account opts in: they carry no text for
    # the index, and there are ~10x more of them than documents.
    if ctype in IMAGE_EXT and accounts.images_enabled():
        return IMAGE_EXT[ctype], ""
    return None


class Downloader:
    """One HTTP fetch per call. Concurrency is handled by the caller's pool."""

    def __init__(self):
        self.client = gchat.Client(min_interval=0.0)

    def token(self) -> str:
        return self.client.creds().token

    def drive_get(self, file_id: str, export: str | None, token: str) -> bytes:
        if export:
            url = "https://www.googleapis.com/drive/v3/files/%s/export?%s" % (
                urllib.parse.quote(file_id),
                urllib.parse.urlencode({"mimeType": export}),
            )
        else:
            url = "https://www.googleapis.com/drive/v3/files/%s?%s" % (
                urllib.parse.quote(file_id),
                urllib.parse.urlencode({"alt": "media"}),
            )
        return self._fetch(url, token)

    def chat_media(self, att: dict, token: str) -> bytes:
        resource = (att.get("attachmentDataRef") or {}).get("resourceName")
        if resource:
            url = "https://chat.googleapis.com/v1/media/%s?%s" % (
                urllib.parse.quote(resource, safe=""),
                urllib.parse.urlencode({"alt": "media"}),
            )
            return self._fetch(url, token)
        # Fallback: the attachment resource itself supports alt=media.
        url = "https://chat.googleapis.com/v1/%s?%s" % (
            att["name"],
            urllib.parse.urlencode({"alt": "media"}),
        )
        return self._fetch(url, token)

    def _fetch(self, url: str, token: str) -> bytes:
        for attempt in range(4):
            req = urllib.request.Request(url, method="GET")
            req.add_header("Authorization", "Bearer " + token)
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    return resp.read(MAX_BYTES + 1)
            except urllib.error.HTTPError as exc:
                if exc.code in (429, 500, 502, 503, 504):
                    time.sleep(min(20, 1.5 ** attempt))
                    continue
                raise gchat.ChatError(exc.code, exc.read().decode(errors="replace")[:200])
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                time.sleep(min(20, 1.5 ** attempt))
        raise gchat.ChatError(429, "retries exhausted")


def fetch_one(dl: Downloader, key: str, item: dict, token: str) -> dict:
    """Download a single attachment and return its manifest record."""
    att, spec = item["att"], item["spec"]
    ext, export = spec
    dest = FILES / ("%s.%s" % (key, ext))
    record = {
        "key": key,
        "message_id": item["message"],
        "space_id": item["space_id"],
        "name": att.get("contentName") or key,
        "content_type": att.get("contentType"),
        "source": att.get("source"),
        "path": str(dest),
    }
    try:
        if key.startswith("drive_"):
            data = dl.drive_get(key[len("drive_"):], export, token)
        else:
            data = dl.chat_media(att, token)
        if len(data) > MAX_BYTES:
            record["error"] = "too_large"
        else:
            dest.write_bytes(data)
            record["bytes"] = len(data)
    except gchat.ChatError as exc:
        record["error"] = "HTTP %s" % exc.status
    except Exception as exc:  # noqa: BLE001 - one bad file must not stop the run
        record["error"] = "%s: %s" % (type(exc).__name__, exc)
    return record


def cmd_download(limit: int | None, workers: int = 8) -> None:
    done = load_manifest()
    FILES.mkdir(parents=True, exist_ok=True)
    dl = Downloader()
    book = manage.as_sets()
    queued: dict[str, dict] = {}
    for msg, space_id, att in iter_attachments():
        spec = plan(att)
        if not spec:
            continue
        key = attachment_key(att)
        # Never re-fetch a space or file the user deleted, even if it is still
        # reachable upstream.
        if space_id in book["deleted_spaces"] or key in book["deleted_attachments"]:
            continue
        rec = done.get(key)
        if rec and ("path" in rec or rec.get("error")):
            continue
        queued.setdefault(key, {"att": att, "message": msg.get("name"), "space_id": space_id, "spec": spec})

    targets = list(queued.items())
    if limit:
        targets = targets[:limit]
    print("attachments to fetch: %d (already have %d)" % (len(targets), len(done)), flush=True)
    if not targets:
        return

    token = dl.token()
    fetched = failures = finished = 0
    with MANIFEST.open("a") as manifest, ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch_one, dl, key, item, token): key for key, item in targets}
        for future in as_completed(futures):
            try:
                record = future.result()
            except Exception as exc:  # noqa: BLE001
                record = {"key": futures[future], "error": "%s: %s" % (type(exc).__name__, exc)}
            finished += 1
            if record.get("error"):
                failures += 1
            else:
                fetched += 1
            manifest.write(json.dumps(record) + "\n")
            manifest.flush()
            if finished % 50 == 0:
                print("  %d/%d done (%d ok, %d failed)" % (finished, len(targets), fetched, failures), flush=True)

    print("downloaded %d files, %d failures" % (fetched, failures))


def cmd_extract() -> None:
    if not MANIFEST.exists():
        print("Run `attachments.py download` first.", file=sys.stderr)
        sys.exit(1)
    try:
        from . import textract
    except ImportError as exc:  # pragma: no cover - broken install
        print("Extractor unavailable: %s" % exc, file=sys.stderr)
        sys.exit(1)
    textract.run(MANIFEST, TEXT)


def cmd_stats() -> None:
    manifest = load_manifest()
    by_type: dict[str, int] = {}
    ok = err = 0
    for rec in manifest.values():
        if rec.get("error"):
            err += 1
        else:
            ok += 1
        by_type[rec.get("content_type") or "?"] = by_type.get(rec.get("content_type") or "?", 0) + 1
    chars = 0
    docs = 0
    if TEXT.exists():
        for line in TEXT.read_text().splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec.get("chars"):
                docs += 1
                chars += rec["chars"]
    print("downloaded: %d ok, %d failed" % (ok, err))
    for ctype, count in sorted(by_type.items(), key=lambda kv: kv[1], reverse=True)[:10]:
        print("  %-70s %d" % (ctype, count))
    print("extracted: %d documents, %s characters" % (docs, f"{chars:,}"))


def main() -> None:
    action = sys.argv[1] if len(sys.argv) > 1 else "stats"
    if action == "download":
        limit = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else None
        cmd_download(limit)
    elif action == "extract":
        cmd_extract()
    elif action == "stats":
        cmd_stats()
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
