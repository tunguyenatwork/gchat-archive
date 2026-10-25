#!/usr/bin/env python3
"""Sync every Google Chat space the account belongs to into local JSONL.

    ~/.hermes/hermes-agent/venv/bin/python3 fetch.py              # incremental (safe re-run)
    ~/.hermes/hermes-agent/venv/bin/python3 fetch.py --full       # re-pull everything from scratch
    ~/.hermes/hermes-agent/venv/bin/python3 fetch.py --space spaces/AAA --space spaces/BBB
    ~/.hermes/hermes-agent/venv/bin/python3 fetch.py --retry-failed

Raw messages land in ~/.hermes/chat-archive/raw/<space>.jsonl (append-only,
one raw API resource per line). build.py dedupes by message name, so re-running
after an incremental sync is always consistent.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import api as gchat
from . import accounts
from . import manage

ARCHIVE = accounts.active_root()  # per-account archive directory
RAW = ARCHIVE / "raw"
STATE = ARCHIVE / "state.json"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def subtract_seconds(iso: str, seconds: int) -> str:
    """RFC3339 string minus N seconds, for skew-tolerant comparisons."""
    try:
        when = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso
    return (when - timedelta(seconds=seconds)).isoformat(timespec="seconds").replace("+00:00", "Z")


def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except json.JSONDecodeError:
            pass
    return {"version": 1, "me": None, "spaces": {}, "updated": None}


def save_state(state: dict) -> None:
    state["updated"] = now_iso()
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE)


def raw_path(space_id: str) -> Path:
    return RAW / (space_id.replace("/", "_") + ".jsonl")


def known_ids(space_id: str) -> set[str]:
    path = raw_path(space_id)
    if not path.exists():
        return set()
    ids: set[str] = set()
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                name = json.loads(line).get("name")
            except json.JSONDecodeError:
                continue
            if name:
                ids.add(name)
    return ids


def space_meta(sp: dict) -> dict:
    return {
        "id": sp.get("name"),
        "display_name": sp.get("displayName") or sp.get("name"),
        "type": sp.get("spaceType"),
        "threading": sp.get("spaceThreadingState"),
        "member_count": sp.get("memberCount"),
        "last_activity": sp.get("lastActiveTime"),
        "create_time": sp.get("createTime"),
    }


def meta_path(space_id: str) -> Path:
    return raw_path(space_id).with_suffix(".meta.json")


def write_meta(meta: dict) -> None:
    """Persist space metadata next to its JSONL immediately.

    build.py reads these instead of state.json, which only checkpoints every 25
    spaces and is therefore stale during a long first sync.
    """
    RAW.mkdir(parents=True, exist_ok=True)
    meta_path(meta["id"]).write_text(json.dumps(meta, indent=2))


def sync_space(client: gchat.Client, meta: dict, full: bool, state: dict) -> dict:
    """Append anything new in one space. Never rewrites or truncates history.

    The archive is deliberately append-only: if a message or a whole space is
    deleted upstream, the copy we already hold stays on disk and stays in the
    DB. `--full` therefore means "page through this space completely again",
    not "start the file over" — truncating here would lose exactly the messages
    Google no longer returns.
    """
    space_id = meta["id"]
    existing = known_ids(space_id)
    added = 0
    oldest = newest = None

    RAW.mkdir(parents=True, exist_ok=True)
    try:
        with raw_path(space_id).open("a") as fh:
            for msg in client.iter_messages(space_id, stop_when_known=existing or None):
                name = msg.get("name")
                if not name:
                    continue
                if name in existing:
                    continue
                existing.add(name)
                fh.write(json.dumps(msg) + "\n")
                added += 1
                create = msg.get("createTime")
                if create:
                    oldest = create if oldest is None or create < oldest else oldest
                    newest = create if newest is None or create > newest else newest
    except gchat.ChatError as exc:
        prev = state["spaces"].get(space_id, {})
        state["spaces"][space_id] = {
            **prev,
            **meta,
            "error": str(exc),
            "error_status": exc.status,
            "last_attempt": now_iso(),
        }
        return {"space": space_id, "added": added, "error": exc.status}

    prev = state["spaces"].get(space_id, {})
    state["spaces"][space_id] = {
        **prev,
        **meta,
        "error": None,
        "error_status": None,
        "total": len(existing),
        "newest": newest or prev.get("newest"),
        "oldest": oldest or prev.get("oldest"),
        "complete": bool(prev.get("complete") or full or added == 0),
        "last_sync": now_iso(),
        # Marks that we looked at this space at this moment, which is what the
        # unchanged-space skip compares lastActiveTime against.
        "last_checked": now_iso(),
    }
    return {"space": space_id, "added": added}


def main() -> None:
    ap = argparse.ArgumentParser(description="Archive Google Chat to local JSONL")
    ap.add_argument(
        "--full",
        action="store_true",
        help="page through every space end-to-end (append-only: nothing is ever truncated)",
    )
    ap.add_argument("--space", action="append", default=None, help="limit to one or more space ids")
    ap.add_argument("--retry-failed", action="store_true", help="only retry spaces that errored last run")
    ap.add_argument(
        "--check-all",
        action="store_true",
        help="re-check every space even if its last activity is unchanged (catches old-message edits)",
    )
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    client = gchat.Client()
    state = load_state()
    me = state.get("me") or client.my_identity().get("email")
    state["me"] = me

    log = (lambda *a: None) if args.quiet else (lambda *a: print(*a, flush=True))

    log("Listing spaces...")
    spaces = [space_meta(s) for s in client.iter_spaces()]
    log("  %d spaces visible to %s" % (len(spaces), me or "unknown account"))
    for meta in spaces:
        write_meta(meta)

    # User decisions from the web UI: excluded spaces keep their history but are
    # never fetched again; deleted spaces are gone from the archive entirely.
    book = manage.as_sets()
    before_filter = len(spaces)
    spaces = [
        s for s in spaces
        if s["id"] not in book["deleted_spaces"] and s["id"] not in book["excluded_spaces"]
    ]
    if len(spaces) != before_filter:
        log("  %d space(s) skipped by archive rules (excluded/deleted)" % (before_filter - len(spaces)))

    if args.space:
        wanted = set(args.space)
        spaces = [s for s in spaces if s["id"] in wanted]
    if args.retry_failed:
        spaces = [s for s in spaces if state["spaces"].get(s["id"], {}).get("error")]

    # Skip spaces with no activity since the last time we read them. Comparing
    # against our own last_checked (not the newest stored message) avoids
    # re-checking forever when Google's lastActiveTime includes things that are
    # not readable messages (reactions, membership churn). The 120 s margin
    # absorbs clock skew between us and Google.
    # Edits/deletions that never bump lastActiveTime only surface on a
    # --check-all run, which the README suggests doing weekly.
    if not args.full and not args.check_all and not args.retry_failed:
        changed = []
        for meta in spaces:
            prev = state["spaces"].get(meta["id"], {})
            last_active, last_checked = meta.get("last_activity"), prev.get("last_checked")
            if last_active and last_checked and last_active <= subtract_seconds(last_checked, 120):
                continue
            changed.append(meta)
        if len(changed) != len(spaces):
            log("  %d unchanged space(s) skipped (use --check-all to re-check)" % (len(spaces) - len(changed)))
        spaces = changed

    total_new = 0
    failures = []
    started = time.time()
    for idx, meta in enumerate(spaces, 1):
        result = sync_space(client, meta, args.full, state)
        if result.get("error"):
            failures.append((meta["id"], meta["display_name"], result["error"]))
            log("[%d/%d] %-38s ERROR %s" % (idx, len(spaces), (meta["display_name"] or "")[:38], result["error"]))
        else:
            total_new += result["added"]
            if result["added"]:
                log("[%d/%d] %-38s +%d" % (idx, len(spaces), (meta["display_name"] or "")[:38], result["added"]))
        if idx % 25 == 0:
            save_state(state)

    save_state(state)
    elapsed = time.time() - started
    log(
        "\nDone: %d new messages across %d spaces in %.0fs (%d API calls)"
        % (total_new, len(spaces), elapsed, client.requests)
    )
    if failures:
        log("\n%d space(s) unavailable:" % len(failures))
        for sid, name, err in failures[:40]:
            log("  %-24s %-34s %s" % (sid, (name or "")[:34], err[:60]))
        log("  (re-run with --retry-failed to try again)")
    sys.exit(0)


if __name__ == "__main__":
    main()
