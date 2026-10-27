"""Nightly sync scheduling that works on macOS, Linux and Windows.

The archive does not own a scheduler. On POSIX a marked ``crontab`` line runs
``python -m gchat_archive sync``; on Windows a Task Scheduler entry does the
same. Both are reversible by the app's own settings panel, and the installed
command is regenerated each time so it always points at the current interpreter.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import paths

MARK = "# gchat-archive sync"
TASK_NAME = "gchat-archive sync"
STATE = "last_sync.json"


def sync_command() -> str:
    """The command a scheduler should run (quoted for the platform)."""
    exe = Path(sys.executable)
    if os.name == "nt":
        return '""%s"" -m gchat_archive sync' % exe
    return "%s -m gchat_archive sync" % exe


def _state_file() -> Path:
    return paths.data_home() / STATE


def record_run(ok: bool, detail: str = "", **extra) -> None:
    """Called by ``sync`` so the UI can show when it last ran, and what it added."""
    data = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "ok": bool(ok), "detail": detail[:400]}
    data.update({k: v for k, v in extra.items() if v is not None})
    try:
        _state_file().write_text(json.dumps(data))
    except OSError:
        pass


def _last_run() -> dict:
    try:
        return json.loads(_state_file().read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def parse_expr(expr: str) -> dict:
    """``0 5 * * *`` -> {"hour": 5, "minute": 0}. Only daily times are offered."""
    parts = (expr or "").split()
    if len(parts) < 2:
        return {}
    try:
        minute, hour = int(parts[0]), int(parts[1])
    except ValueError:
        return {}
    if not (0 <= minute < 60 and 0 <= hour < 24):
        return {}
    return {"minute": minute, "hour": hour}


def describe(expr: str) -> str:
    parsed = parse_expr(expr)
    if not parsed:
        return expr or "not scheduled"
    return "every day at %02d:%02d" % (parsed["hour"], parsed["minute"])


# --- POSIX -----------------------------------------------------------------

def _crontab_lines() -> list[str]:
    try:
        out = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    return [line for line in out.stdout.splitlines() if line.strip()]


def _write_crontab(lines: list[str]) -> bool:
    body = "\n".join(lines) + "\n"
    try:
        out = subprocess.run(["crontab", "-"], input=body, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0


def _posix_status() -> dict:
    lines = _crontab_lines()
    ours = [l for l in lines if MARK in l]
    if not ours:
        return {"installed": False, "expr": "", "paused": False}
    # The line is "<5 cron fields> <command> > /dev/null 2>&1  # marker"
    expr = " ".join(ours[0].split(MARK)[0].split()[:5])
    return {"installed": True, "expr": expr, "paused": False}


def _posix_install(expr: str) -> tuple[bool, str]:
    others = [l for l in _crontab_lines() if MARK not in l]
    others.append("%s %s %s  # %s" % (expr.strip(), sync_command(), "> /dev/null 2>&1", MARK.lstrip("# ")))
    ok = _write_crontab(others)
    return ok, "crontab updated" if ok else "could not write crontab"


def _posix_remove() -> tuple[bool, str]:
    ok = _write_crontab([l for l in _crontab_lines() if MARK not in l])
    return ok, "schedule removed" if ok else "could not write crontab"


# --- Windows ---------------------------------------------------------------

def _win_query() -> dict:
    try:
        out = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME],
                             capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return {"installed": False, "expr": "", "paused": False}
    if out.returncode != 0:
        return {"installed": False, "expr": "", "paused": False}
    return {"installed": True, "expr": "", "paused": False}


def _win_install(expr: str) -> tuple[bool, str]:
    parsed = parse_expr(expr)
    if not parsed:
        return False, "Windows scheduling needs a daily time"
    when = "%02d:%02d" % (parsed["hour"], parsed["minute"])
    cmd = ["schtasks", "/Create", "/F", "/SC", "DAILY", "/ST", when,
           "/TN", TASK_NAME, "/TR", "python -m gchat_archive sync"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    return out.returncode == 0, (out.stdout or out.stderr).strip()[:300]


def _win_remove() -> tuple[bool, str]:
    try:
        out = subprocess.run(["schtasks", "/Delete", "/F", "/TN", TASK_NAME],
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    return out.returncode == 0, (out.stdout or out.stderr).strip()[:300]


# --- public ----------------------------------------------------------------

def available() -> bool:
    return bool(shutil.which("crontab")) or os.name == "nt"


def status() -> dict:
    state = _posix_status() if os.name != "nt" else _win_query()
    last = _last_run()
    expr = state.get("expr") or ""
    return {
        "installed": state.get("installed", False),
        "expr": expr,
        "display": describe(expr) if expr else "not scheduled",
        "paused": not state.get("installed", False),
        "last_run_at": last.get("at"),
        "last_status": "ok" if last.get("ok") else ("failed" if last else None),
        "last_error": None if last.get("ok", True) else last.get("detail"),
        "platform": "windows" if os.name == "nt" else "cron",
        "available": available(),
        "command": sync_command(),
    }


def install(expr: str) -> tuple[bool, str]:
    if not parse_expr(expr):
        return False, "use a daily cron time such as '0 5 * * *'"
    return _win_install(expr) if os.name == "nt" else _posix_install(expr)


def remove() -> tuple[bool, str]:
    return _win_remove() if os.name == "nt" else _posix_remove()
