"""Cross-platform smoke test: does this build actually run on this OS?

Unit tests exercise logic; this proves the shipped package starts, serves its UI
and reads its own configuration on the machine it was installed on — the part
that differs most between macOS, Linux and Windows.

    python scripts/smoke.py

Exits non-zero with a readable reason if anything fails. Creates a temporary data
directory, so it never touches a real archive.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

CHECKS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append(("%-46s %s %s" % (name, "ok" if ok else "FAIL", detail)).rstrip())
    if not ok:
        raise SystemExit("smoke test failed: %s %s" % (name, detail))


def run(args: list[str], home: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ, GCHAT_ARCHIVE_HOME=str(home), PYTHONIOENCODING="utf-8")
    return subprocess.run([sys.executable, "-m", "gchat_archive", *args],
                          capture_output=True, text=True, env=env, timeout=120)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main() -> int:
    home = Path(tempfile.mkdtemp(prefix="gchat-smoke-"))
    print("python     :", sys.version.split()[0], "on", sys.platform)
    print("data dir   :", home)

    version = run(["--version"], home)
    check("--version", version.returncode == 0 and "gchat-archive" in version.stdout,
          version.stdout.strip() or version.stderr.strip())

    doctor = run(["doctor"], home)
    check("doctor runs", doctor.returncode == 0, doctor.stderr.strip()[-120:])
    check("doctor sees the data dir", str(home) in doctor.stdout)

    # A fresh install has no account: serving must still work and show /setup.
    port = free_port()
    env = dict(os.environ, GCHAT_ARCHIVE_HOME=str(home), PYTHONIOENCODING="utf-8")
    server = subprocess.Popen(
        [sys.executable, "-m", "gchat_archive", "serve", "--port", str(port)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        base = "http://127.0.0.1:%d" % port
        for _ in range(60):
            time.sleep(0.25)
            try:
                with urllib.request.urlopen(base + "/api/auth/status", timeout=2) as res:
                    status = json.load(res)
                break
            except (urllib.error.URLError, ConnectionError, OSError):
                continue
        else:
            out = server.stdout.read() if server.stdout else ""
            check("server starts", False, out[-300:])
            return 1

        check("server responds", isinstance(status, dict) and "configured" in status)

        for path, must_contain in (("/login", "password"),
                                   ("/styles.css", "--bg"),
                                   ("/setup", "client_secret"),
                                   ("/app.js", "sync")):
            with urllib.request.urlopen(base + path, timeout=5) as res:
                body = res.read().decode("utf-8", "replace")
            check("serves %s" % path, res.status == 200 and must_contain in body,
                  "%s bytes" % len(body))
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()

    # Read-only scheduler/service probes: they must not explode on this platform.
    for args, label in ((["schedule", "status"], "schedule status"),
                        (["service", "status"], "service status"),
                        (["accounts", "list"], "accounts list")):
        proc = run(args, home)
        check(label, proc.returncode == 0, proc.stderr.strip()[-120:])

    print()
    for line in CHECKS:
        print(" ", line)
    print("\nall checks passed on this platform (%d)" % len(CHECKS))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
