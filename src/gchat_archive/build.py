#!/usr/bin/env python3
"""Build the offline archive DB from raw Google Chat JSONL.

    ~/.hermes/hermes-agent/venv/bin/python3 build.py

Produces ~/.hermes/chat-archive/chats.sqlite: one row per message, plus an FTS5
full-text index over message text and sender names, plus resolved space labels
(DMs come back from the API as "spaces/XXXX", so the label is derived from the
other participant seen in that conversation).

Rebuilds from raw JSONL every run, so it is idempotent and safe after any
partial sync. Dedupes by message resource name, keeping the newest revision.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

from . import accounts
from . import manage

ARCHIVE = accounts.active_root()  # per-account archive directory
RAW = ARCHIVE / "raw"
DB = ARCHIVE / "chats.sqlite"
STATE = ARCHIVE / "state.json"
ATT_TEXT = ARCHIVE / "attachments_text.jsonl"

# message_id -> extracted attachment text (filled by load_attachment_text)
ATT_MAP: dict[str, str] = {}

SCHEMA = """
CREATE TABLE spaces (
    id            TEXT PRIMARY KEY,
    display_name  TEXT,
    label         TEXT,
    type          TEXT,
    threading     TEXT,
    member_count  INTEGER,
    last_activity TEXT,
    first_activity TEXT,
    msg_count     INTEGER DEFAULT 0,
    error         TEXT,
    bot_only      INTEGER DEFAULT 0,
    excluded      INTEGER DEFAULT 0
);

CREATE TABLE messages (
    id           TEXT PRIMARY KEY,
    space_id     TEXT NOT NULL,
    thread_id    TEXT,
    sender_name  TEXT,
    sender_email TEXT,
    sender_type  TEXT,
    create_time  TEXT,
    update_time  TEXT,
    deleted      INTEGER DEFAULT 0,
    text         TEXT,
    attachments  TEXT,
    reactions    TEXT,
    quoted TEXT,
    raw TEXT,
    att_text TEXT
);

CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);

CREATE VIRTUAL TABLE messages_fts USING fts5(
    text, sender_name, att_text,
    content='messages', content_rowid='rowid',
    tokenize="porter unicode61 remove_diacritics 2"
);
"""


def card_text(node, out: list[str]) -> None:
    """Flatten interactive-card payloads into plain text so they are searchable."""
    if isinstance(node, dict):
        if isinstance(node.get("text"), str):
            out.append(node["text"])
        for key in ("title", "subtitle", "textParagraph", "label"):
            val = node.get(key)
            if isinstance(val, str):
                out.append(val)
            elif isinstance(val, dict) and isinstance(val.get("text"), str):
                out.append(val["text"])
        for value in node.values():
            if isinstance(value, (dict, list)):
                card_text(value, out)
    elif isinstance(node, list):
        for item in node:
            card_text(item, out)


def message_text(msg: dict) -> str:
    parts = []
    text = msg.get("text") or msg.get("argumentText")
    if text:
        parts.append(text.strip())
    if msg.get("cardsV2") or msg.get("cards"):
        extra: list[str] = []
        card_text(msg.get("cardsV2") or msg.get("cards") or [], extra)
        seen = {p for p in parts}
        for chunk in extra:
            chunk = chunk.strip()
            if chunk and chunk not in seen:
                parts.append(chunk)
                seen.add(chunk)
    return "\n".join(parts)


def msg_row(msg: dict, space_id: str) -> tuple:
    sender = msg.get("sender") or {}
    quoted = None
    qm = (msg.get("quotedMessageMetadata") or {}).get("quotedMessageSnapshot")
    if qm:
        quoted = json.dumps({"sender": qm.get("sender"), "text": qm.get("text")}, ensure_ascii=False)
    attachments = [
        {
            "key": manage.attachment_key(a),
            "name": a.get("contentName"),
            "type": a.get("contentType"),
            "uri": a.get("downloadUri"),
            "source": a.get("source"),
        }
        for a in (msg.get("attachment") or [])
    ]
    reactions = [
        {
            "emoji": (r.get("emoji") or {}).get("unicode")
            or (r.get("emoji") or {}).get("customEmoji", {}).get("uid"),
            "count": r.get("reactionCount"),
        }
        for r in (msg.get("emojiReactions") or [])
    ]
    deleted = 1 if (msg.get("deleteTime") or msg.get("deletionMetadata")) else 0
    # Attachment filenames are always indexed; document *contents* only when the
    # downloader/extractor has produced text for this message.
    name_blob = " ".join(a["name"] or "" for a in attachments)
    extracted = ATT_MAP.get(msg.get("name"), "")
    att_text = (name_blob + "\n" + extracted).strip() or None
    return (
        msg.get("name"),
        space_id,
        (msg.get("thread") or {}).get("name"),
        sender.get("displayName") or sender.get("name") or "Unknown",
        sender.get("email"),
        sender.get("type"),
        msg.get("createTime"),
        msg.get("lastUpdateTime") or msg.get("createTime"),
        deleted,
        message_text(msg),
        json.dumps(attachments, ensure_ascii=False) if attachments else None,
        json.dumps(reactions, ensure_ascii=False) if reactions else None,
        quoted,
        json.dumps(msg, ensure_ascii=False),
        att_text,
    )


def load_attachment_text(deleted_keys: set[str] | None = None) -> dict[str, str]:
    """message_id -> extracted text of every document attached to it."""
    deleted_keys = deleted_keys or set()
    texts: dict[str, str] = {}
    if not ATT_TEXT.exists():
        return texts
    with ATT_TEXT.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("key") in deleted_keys:
                continue
            mid, text = rec.get("message_id"), rec.get("text") or ""
            if mid and text:
                texts[mid] = (texts.get(mid, "") + "\n" + text).strip()
    return texts


def load_raw() -> tuple[dict[str, dict], dict[str, list[tuple]]]:
    """Return deduped messages per space (newest revision wins) and DM senders."""
    per_space: dict[str, dict[str, tuple]] = {}
    dm_senders: dict[str, Counter] = {}
    for path in sorted(RAW.glob("*.jsonl")):
        space_id = path.stem.replace("_", "/", 1)
        store = per_space.setdefault(space_id, {})
        counter = dm_senders.setdefault(space_id, Counter())
        with path.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                name = msg.get("name")
                if not name:
                    continue
                row = msg_row(msg, space_id)
                prev = store.get(name)
                if prev is None or (row[7] or "") >= (prev[7] or ""):
                    store[name] = row
                email = row[4]
                if email or row[5] == "BOT":  # bots often expose no email
                    counter[(row[3], email or "", row[5] or "HUMAN")] += 1
    return per_space, dm_senders


def load_metas() -> dict[str, dict]:
    """Per-space metadata written by fetch.py, plus anything only in state.json."""
    metas: dict[str, dict] = {}
    for path in RAW.glob("*.meta.json"):
        try:
            meta = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        if meta.get("id"):
            metas[meta["id"]] = meta
    if STATE.exists():
        try:
            state = json.loads(STATE.read_text())
        except json.JSONDecodeError:
            state = {}
        for space_id, meta in (state.get("spaces") or {}).items():
            if space_id in metas:
                continue
            merged = dict(meta)
            merged.setdefault("id", space_id)
            metas[space_id] = merged
    return metas


def resolve_label(space_id: str, meta: dict, me: str | None, senders: Counter) -> str:
    """Human label for a conversation.

    Google returns no display name for DMs and many group chats — the API only
    exposes an opaque `spaces/XXXX` name, and memberships need a scope we do not
    hold. So DM and group labels are derived from the senders actually seen in
    the archived messages, which is enough to recognise a conversation.
    """
    name = meta.get("display_name") or ""
    if name and not name.startswith("spaces/"):
        return name

    def others_of(kind: str):
        return sorted(
            ((name, count) for (name, email, sender_type), count in senders.items()
             if sender_type == kind
             and not (me and email and email.lower() == me.lower())
             and (kind != "HUMAN" or email)),
            key=lambda kv: kv[1],
            reverse=True,
        )

    humans = others_of("HUMAN")
    bots = others_of("BOT")
    space_type = meta.get("type")
    if space_type == "GROUP_CHAT" and len(humans) > 1:
        named = [n for n, _ in humans[:4]]
        extra = len(humans) - len(named)
        return "Group: " + ", ".join(named) + (" +%d" % extra if extra > 0 else "")
    if humans:
        return "%s (DM)" % humans[0][0]
    if bots:
        return "%s (bot)" % bots[0][0]
    if space_type == "DIRECT_MESSAGE":
        return "Direct message"
    return "Space %s" % space_id.split("/")[-1][:10]


def previous_message_count() -> int:
    """How many messages the current DB holds, or 0 if there isn't one yet."""
    if not DB.exists():
        return 0
    try:
        con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=5)
        try:
            return con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        finally:
            con.close()
    except sqlite3.Error:
        return 0


def rule_path_from_argv() -> Path:
    """--rules <path> overrides which filter file the build honours."""
    argv = sys.argv
    for index, arg in enumerate(argv):
        if arg == "--rules" and index + 1 < len(argv):
            return Path(argv[index + 1]).expanduser().resolve()
        if arg.startswith("--rules="):
            return Path(arg.split("=", 1)[1]).expanduser().resolve()
    return manage.default_path()


def main() -> None:
    allow_shrink = "--allow-shrink" in sys.argv
    rules_path = rule_path_from_argv()
    if not RAW.exists() or not any(RAW.glob("*.jsonl")):
        print("No raw JSONL found — run fetch.py first.", file=sys.stderr)
        sys.exit(1)

    global ATT_MAP
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    me = state.get("me")
    metas = load_metas()
    book = manage.as_sets(manage.load_from(rules_path))
    ATT_MAP = load_attachment_text(book["deleted_attachments"])
    deleted_messages = book["deleted_messages"]
    deleted_spaces = book["deleted_spaces"]

    per_space, dm_senders = load_raw()
    # Captured before deletions are applied: the append-only guard must compare
    # what is *on disk* against the DB, not what survived the user's filters.
    raw_total = sum(len(store) for store in per_space.values())

    # Apply the user's deletions and exclusions before anything is counted.
    for space_id in list(per_space):
        if space_id in deleted_spaces:
            del per_space[space_id]
            continue
        store = per_space[space_id]
        for message_id in [mid for mid in store if mid in deleted_messages]:
            del store[message_id]
    for space_id in list(metas):
        if space_id in deleted_spaces:
            del metas[space_id]

    # Append-only invariant: raw JSONL only ever grows, so a rebuild must never
    # produce fewer messages than the DB already has. If it would, something is
    # wrong with the raw archive (moved, wiped, partially written) and replacing
    # the DB would silently destroy history. Refuse instead of losing data.
    previous = previous_message_count()
    if previous and raw_total < previous and not allow_shrink:
        print(
            "Refusing to rebuild: %d messages on disk vs %d already archived.\n"
            "The archive is append-only, so this means raw/ is incomplete or was moved.\n"
            "Re-run fetch.py, or pass --allow-shrink if the reduction is intended."
            % (raw_total, previous),
            file=sys.stderr,
        )
        sys.exit(2)

    # Build into a sibling file and swap it in at the end: the web UI keeps
    # serving reads off the previous DB until the new one is complete.
    tmp_db = DB.with_suffix(".building.sqlite")
    tmp_db.unlink(missing_ok=True)
    con = sqlite3.connect(tmp_db)
    con.executescript(
        """
        PRAGMA journal_mode=OFF;
        PRAGMA synchronous=OFF;
        PRAGMA temp_store=MEMORY;
        """
    )
    con.executescript(SCHEMA)

    space_rows = []
    message_rows = []
    for space_id, store in per_space.items():
        meta = metas.get(space_id, {})
        rows = list(store.values())
        times = [r[6] for r in rows if r[6]]
        senders = dm_senders.get(space_id, Counter())
        label = resolve_label(space_id, meta, me, senders)
        # A conversation whose only participants are bots (e.g. the Google Drive
        # DM) is worth its own category rather than sitting among people.
        human_others = [
            1
            for (name, email, kind) in senders
            if kind == "HUMAN" and email and not (me and email.lower() == me.lower())
        ]
        bot_only = 1 if senders and not human_others else 0
        space_rows.append(
            (
                space_id,
                meta.get("display_name") or space_id,
                label,
                meta.get("type"),
                meta.get("threading"),
                meta.get("member_count"),
                max(times) if times else meta.get("last_activity"),
                min(times) if times else None,
                len(rows),
                meta.get("error"),
                bot_only,
                1 if space_id in book["excluded_spaces"] else 0,
            )
        )
        message_rows.extend(rows)

    # Spaces we have metadata for but never fetched (e.g. empty or errored).
    known = set(per_space)
    for space_id, meta in metas.items():
        if space_id in known:
            continue
        space_rows.append(
            (
                space_id,
                meta.get("display_name") or space_id,
                resolve_label(space_id, meta, me, Counter()),
                meta.get("type"),
                meta.get("threading"),
                meta.get("member_count"),
                meta.get("last_activity"),
                None,
                0,
                meta.get("error"),
                0,
                1 if space_id in book["excluded_spaces"] else 0,
            )
        )

    # Two DMs with the same person (or groups with the same first participants)
    # produce identical labels; disambiguate by recency ("Chad Tom (DM) · Oct 2026").
    def disambiguate(label: str, last_activity: str | None, space_id: str) -> str:
        if last_activity:
            try:
                from datetime import datetime

                when = datetime.fromisoformat(last_activity.replace("Z", "+00:00"))
                return "%s · %s" % (label, when.strftime("%b %Y"))
            except ValueError:
                pass
        return "%s · %s" % (label, space_id.split("/")[-1][-4:])

    label_counts = Counter(row[2] for row in space_rows)
    space_rows = [
        row[:2] + (row[2] if label_counts[row[2]] == 1 else disambiguate(row[2], row[6], row[0]),) + row[3:]
        for row in space_rows
    ]

    con.executemany("INSERT OR REPLACE INTO spaces VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", space_rows)
    con.executemany("INSERT OR REPLACE INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", message_rows)
    con.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
    con.executemany(
        "INSERT OR REPLACE INTO meta VALUES (?,?)",
        [
            ("built_at", __import__("datetime").datetime.now().astimezone().isoformat(timespec="seconds")),
            ("me", me or ""),
            ("spaces", str(len(space_rows))),
            ("messages", str(len(message_rows))),
            ("last_sync", state.get("updated") or ""),
        ],
    )
    con.commit()
    con.execute("PRAGMA optimize")
    con.close()
    tmp_db.replace(DB)

    size_mb = DB.stat().st_size / 1e6
    print("Built %s" % DB)
    print("  %d messages across %d spaces (%.1f MB)" % (len(message_rows), len(space_rows), size_mb))


if __name__ == "__main__":
    main()
