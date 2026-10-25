"""Command line entry point for the Google Chat offline archive."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

from . import paths, schedule

PACKAGE = "gchat_archive"


def _pin_data_dir(value: str | None) -> None:
    """Must run before modules read their paths at import time."""
    if value:
        paths.set_home(value)


def _run(module: str, *args: str, quiet: bool = False) -> int:
    """Run one archive step in its own process (clean module state each time)."""
    cmd = [sys.executable, "-m", "%s.%s" % (PACKAGE, module), *args]
    if not quiet:
        print("$ " + " ".join(cmd[1:]), flush=True)
    return subprocess.call(cmd)


def _active_ready() -> bool:
    from . import accounts

    try:
        accounts.active_slug()
        return True
    except Exception:
        return False


# --- commands --------------------------------------------------------------

def cmd_sync(args) -> int:
    from . import accounts
    registry = accounts.ensure()
    enabled = [a for a in registry["accounts"] if accounts.account_enabled(a)]
    if not enabled:
        print("All accounts are paused (or none registered) — nothing to sync.")
        return 0

    started = time.time()
    failures = []
    for entry in enabled:
        accounts.set_active(entry["slug"])
        print("\n=== %s" % entry.get("email") or entry["slug"], flush=True)

        fetch_args = []
        if args.full:
            fetch_args.append("--full")
        if args.check_all:
            fetch_args.append("--check-all")
        if args.retry_failed:
            fetch_args.append("--retry-failed")
        for space in args.space or []:
            fetch_args += ["--space", space]
        if _run("fetch", *fetch_args) != 0:
            failures.append("%s: fetch" % entry["slug"])

        if not args.no_attachments:
            if _run("attachments", "download") != 0:
                failures.append("%s: attachments" % entry["slug"])
            if _run("attachments", "extract") != 0:
                failures.append("%s: extract" % entry["slug"])

        if not args.no_build:
            if _run("build", *(["--allow-shrink"] if args.allow_shrink else [])) != 0:
                failures.append("%s: build" % entry["slug"])

    elapsed = time.time() - started
    schedule.record_run(not failures, "; ".join(failures))
    print("\n%s in %.0fs" % ("Done" if not failures else "Finished with errors", elapsed))
    for line in failures:
        print("  ! " + line)
    return 1 if failures else 0


def cmd_serve(args) -> int:
    from . import serve

    return serve.main(
        port=args.port,
        open_browser=args.open,
        host=args.host,
    )


def cmd_login(args) -> int:
    from . import serve

    secret = paths.client_secret()
    if not secret.exists():
        print(
            "No OAuth client secret at %s\n\n"
            "Create an OAuth client (type: Desktop app) in Google Cloud Console,\n"
            "download the JSON, and save it there — or pass --client-secret PATH.\n"
            "Full steps: docs/SETUP.md" % secret,
            file=sys.stderr,
        )
        return 2
    return serve.main(port=args.port, open_browser=True, host=args.host, start_path="/oauth/start")


def cmd_build(args) -> int:
    extra = []
    if args.rules:
        extra += ["--rules", str(Path(args.rules).expanduser())]
    if args.allow_shrink:
        extra.append("--allow-shrink")
    return _run("build", *extra)


def cmd_attachments(args) -> int:
    extra = [args.action]
    if args.limit:
        extra.append(str(args.limit))
    return _run("attachments", *extra)


def cmd_accounts(args) -> int:
    from . import accounts
    registry = accounts.ensure()
    if args.action == "list":
        if not registry["accounts"]:
            print("No accounts yet — run: gchat-archive login")
            return 0
        for entry in registry["accounts"]:
            flags = [k for k in ("active", "paused") if entry.get(k)]
            state = ",".join(flags) or "-"
            print("%-28s %-34s %s" % (entry["slug"], entry.get("email", ""), state))
        return 0
    if args.action == "use":
        accounts.set_active(args.slug)
        print("active account: %s" % args.slug)
        return 0
    if args.action == "remove":
        accounts.remove_account(args.slug, delete_archive=not args.keep_files)
        print("removed %s" % args.slug)
        return 0
    return 2


def cmd_schedule(args) -> int:
    if args.action == "status":
        print(json.dumps(schedule.status(), indent=2))
        return 0
    if args.action == "pause":
        ok, out = schedule.remove()
    elif args.action == "resume":
        ok, out = schedule.install(args.expr or "0 5 * * *")
    else:
        ok, out = schedule.install(args.expr or "0 5 * * *")
    print(out)
    return 0 if ok else 1


def cmd_doctor(args) -> int:
    from . import accounts
    print("python      :", sys.version.split()[0])
    print("data dir    :", paths.data_home(), "(exists)" if paths.data_home().exists() else "(missing)")
    print("client secret:", paths.client_secret(), "present" if paths.client_secret().exists() else "MISSING")
    print("scheduler   :", "windows task" if os.name == "nt" else "crontab", "available" if schedule.available() else "unavailable")
    try:
        from . import accounts as _accounts

        registry = _accounts.load()   # read-only: doctor must not create state
        print("accounts    :", len(registry["accounts"]), "| active:", registry.get("active") or "-")
    except Exception as exc:  # noqa: BLE001 - diagnostics should not explode
        print("accounts    : error:", exc)
    for module, label in (("google.auth", "google-auth"), ("pypdf", "pypdf"),
                          ("docx", "python-docx"), ("openpyxl", "openpyxl")):
        try:
            __import__(module)
            print("%-12s: ok" % label)
        except ImportError:
            print("%-12s: missing (pip install 'gchat-archive[text]')" % label)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gchat-archive",
        description="Keep a local, searchable copy of your Google Chat history.",
    )
    parser.add_argument("--data-dir", help="where to keep the archive (default ~/.gchat-archive)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("sync", help="fetch new messages, attachments and rebuild the database")
    p.add_argument("--full", action="store_true", help="re-pull every conversation")
    p.add_argument("--check-all", action="store_true", help="also re-check conversations that look idle")
    p.add_argument("--retry-failed", action="store_true")
    p.add_argument("--space", action="append", help="only this conversation id (repeatable)")
    p.add_argument("--no-attachments", action="store_true")
    p.add_argument("--no-build", action="store_true")
    p.add_argument("--allow-shrink", action="store_true", help="permit a build that removes messages")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("serve", help="run the local reader UI")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--open", action="store_true", help="open a browser window")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("login", help="add a Google account (opens the OAuth consent page)")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--host", default="127.0.0.1")
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("build", help="rebuild the SQLite database from downloaded messages")
    p.add_argument("--rules", help="alternative rules file (excluded/deleted ids)")
    p.add_argument("--allow-shrink", action="store_true")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("attachments", help="download or extract attachment text")
    p.add_argument("action", choices=["download", "extract", "stats"])
    p.add_argument("limit", nargs="?", type=int)
    p.set_defaults(func=cmd_attachments)

    p = sub.add_parser("accounts", help="list, switch or remove accounts")
    p.add_argument("action", choices=["list", "use", "remove"])
    p.add_argument("slug", nargs="?")
    p.add_argument("--keep-files", action="store_true", help="with remove: keep the archive on disk")
    p.set_defaults(func=cmd_accounts)

    p = sub.add_parser("schedule", help="install or inspect the nightly sync")
    p.add_argument("action", choices=["status", "set", "pause", "resume"])
    p.add_argument("expr", nargs="?", help="cron time, e.g. '0 5 * * *'")
    p.set_defaults(func=cmd_schedule)

    p = sub.add_parser("doctor", help="check the local install")
    p.set_defaults(func=cmd_doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _pin_data_dir(getattr(args, "data_dir", None))
    return args.func(args) or 0


if __name__ == "__main__":
    raise SystemExit(main())
