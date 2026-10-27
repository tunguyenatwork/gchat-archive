"""Run a one-off sync in the background, for the UI's "Sync now" button.

The job is the same command cron runs (``python -m gchat_archive sync``), so a
manual sync behaves exactly like the scheduled one: per-account pause flags are
honoured, output goes to ``sync-now.log``, and ``last_sync.json`` is updated.

Only one manual sync runs at a time. State lives in this process; the persistent
record of the last run is the file the scheduler also writes.
"""
from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

from . import paths, schedule

LOG_NAME = "sync-now.log"
TAIL_CHARS = 2400


class SyncJob:
    """Single-flight background sync."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self.state = "idle"          # idle | running | done | failed
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.returncode: int | None = None
        self.command: list[str] = []

    # --- helpers -----------------------------------------------------------
    def log_path(self) -> Path:
        return paths.data_home() / LOG_NAME

    def tail(self) -> str:
        try:
            text = self.log_path().read_text(errors="replace")
        except OSError:
            return ""
        return text[-TAIL_CHARS:]

    def status(self) -> dict:
        running = self.state == "running" and self._proc is not None and self._proc.poll() is None
        elapsed = None
        if self.started_at:
            end = self.finished_at if self.finished_at else time.time()
            elapsed = round(end - self.started_at, 1)
        last = schedule._last_run()
        return {
            # fields the reader UI binds to
            "seconds": elapsed or 0,
            "last_run_at": last.get("at"),
            "state": "running" if running else self.state,
            "running": running,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.started_at)) if self.started_at else None,
            "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.finished_at)) if self.finished_at else None,
            "elapsed_seconds": elapsed,
            "returncode": self.returncode,
            "command": " ".join(self.command),
            "log": str(self.log_path()),
            "tail": self.tail(),
        }

    # --- control -----------------------------------------------------------
    def start(self, command: list[str] | None = None, mode: str | None = None) -> tuple[bool, str]:
        """Begin a sync. Refuses if one is already running."""
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return False, "a sync is already running"

            cmd = command or [sys.executable, "-m", "gchat_archive", "sync"]
            if mode == "check-all" and command is None:
                cmd.append("--check-all")
            log = self.log_path()
            log.parent.mkdir(parents=True, exist_ok=True)

            self.state = "running"
            self.started_at = time.time()
            self.finished_at = None
            self.returncode = None
            self.command = cmd

            with log.open("w") as fh:
                fh.write("$ %s\n" % " ".join(cmd))
                fh.flush()
                self._proc = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT)

            threading.Thread(target=self._wait, args=(self._proc,), daemon=True).start()
            return True, "started"

    def _wait(self, proc: subprocess.Popen) -> None:
        code = proc.wait()
        with self._lock:
            self.state = "done" if code == 0 else "failed"
            self.finished_at = time.time()
            self.returncode = code
        # The CLI records its own run, but a manual sync should show up in the
        # same place the scheduler reads, even if it was killed early.
        if code != 0:
            schedule.record_run(False, "manual sync exited %d" % code)

    def stop(self) -> bool:
        proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.terminate()
            return True
        return False


JOB = SyncJob()
