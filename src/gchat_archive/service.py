"""Optional: run the reader as a background service that starts at login.

    macOS    launchd LaunchAgent        ~/Library/LaunchAgents/com.gchat-archive.serve.plist
    Linux    systemd --user unit        ~/.config/systemd/user/gchat-archive.service
    Windows  Task Scheduler (ONLOGON)   task "gchat-archive serve"

The service runs the same command you would type — ``python -m gchat_archive
serve`` — against this install and data directory. Installing is reversible with
``service uninstall``; nothing outside these files is touched.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from . import paths

LABEL = "com.gchat-archive.serve"
LINUX_UNIT = "gchat-archive.service"
WINDOWS_TASK = "gchat-archive serve"
DEFAULT_PORT = 8765


def _python() -> str:
    return sys.executable


def _program(port: int) -> list[str]:
    return [_python(), "-m", "gchat_archive.serve", "--port", str(port)]


def _env_home() -> str | None:
    return os.environ.get(paths.HOME_ENV)


def unit_path() -> Path:
    if os.name == "nt":
        return Path()  # Tasks live in the scheduler, not a file
    if sys.platform == "darwin":
        return Path.home() / "Library" / "LaunchAgents" / ("%s.plist" % LABEL)
    return Path.home() / ".config" / "systemd" / "user" / LINUX_UNIT


def _log(name: str) -> Path:
    return paths.data_home() / name


# --- macOS -----------------------------------------------------------------

def _plist(port: int) -> str:
    args = "".join("<string>%s</string>" % part for part in _program(port))
    env = ""
    home = _env_home()
    if home:
        env = ("<key>EnvironmentVariables</key><dict><key>%s</key><string>%s</string></dict>"
               % (paths.HOME_ENV, home))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key><array>{args}</array>
  {env}
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{_log('service.log')}</string>
  <key>StandardErrorPath</key><string>{_log('service.err.log')}</string>
  <key>ProcessType</key><string>Background</string>
</dict>
</plist>
"""


def _launchctl(*args: str) -> tuple[int, str]:
    out = subprocess.run(["launchctl", *args], capture_output=True, text=True, timeout=30)
    return out.returncode, (out.stdout or out.stderr).strip()


def _mac_install(port: int) -> tuple[bool, str]:
    path = unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_plist(port))
    _launchctl("unload", str(path))          # ignore failure: not loaded yet
    code, detail = _launchctl("load", "-w", str(path))
    return code == 0, detail or "loaded %s" % path


def _mac_uninstall() -> tuple[bool, str]:
    path = unit_path()
    if path.exists():
        _launchctl("unload", "-w", str(path))
        path.unlink()
    return True, "service removed"


def _mac_status() -> dict:
    path = unit_path()
    code, detail = _launchctl("list", LABEL)
    return {"installed": path.exists(), "running": code == 0, "detail": detail.splitlines()[:2]}


# --- Linux -----------------------------------------------------------------

def _linux_install(port: int) -> tuple[bool, str]:
    path = unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    env = ""
    if _env_home():
        env = "Environment=%s=%s\n" % (paths.HOME_ENV, _env_home())
    path.write_text(f"""[Unit]
Description=gchat-archive reader
After=network-online.target

[Service]
Type=simple
{env}ExecStart={' '.join(_program(port))}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
""")
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, text=True, timeout=30)
    out = subprocess.run(["systemctl", "--user", "enable", "--now", LINUX_UNIT],
                         capture_output=True, text=True, timeout=60)
    ok = out.returncode == 0
    detail = (out.stdout or out.stderr).strip() or "unit installed"
    if ok:
        detail += "\nTip: `loginctl enable-linger $USER` keeps it running when you are logged out."
    return ok, detail


def _linux_uninstall() -> tuple[bool, str]:
    subprocess.run(["systemctl", "--user", "disable", "--now", LINUX_UNIT],
                   capture_output=True, text=True, timeout=30)
    path = unit_path()
    if path.exists():
        path.unlink()
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, text=True, timeout=30)
    return True, "service removed"


def _linux_status() -> dict:
    path = unit_path()
    out = subprocess.run(["systemctl", "--user", "is-active", LINUX_UNIT], capture_output=True, text=True, timeout=15)
    return {"installed": path.exists(), "running": out.stdout.strip() == "active", "detail": out.stdout.strip()}


# --- Windows ---------------------------------------------------------------

def _win_install(port: int) -> tuple[bool, str]:
    command = '"%s" -m gchat_archive.serve --port %d' % (_python(), port)
    out = subprocess.run(["schtasks", "/Create", "/F", "/SC", "ONLOGON", "/TN", WINDOWS_TASK,
                          "/TR", command], capture_output=True, text=True, timeout=45)
    return out.returncode == 0, (out.stdout or out.stderr).strip()[:300]


def _win_uninstall() -> tuple[bool, str]:
    out = subprocess.run(["schtasks", "/Delete", "/F", "/TN", WINDOWS_TASK],
                         capture_output=True, text=True, timeout=45)
    return out.returncode == 0, (out.stdout or out.stderr).strip()[:300]


def _win_status() -> dict:
    out = subprocess.run(["schtasks", "/Query", "/TN", WINDOWS_TASK], capture_output=True, text=True, timeout=30)
    return {"installed": out.returncode == 0, "running": out.returncode == 0,
            "detail": (out.stdout or "").strip().splitlines()[-1] if out.returncode == 0 else ""}


# --- public ----------------------------------------------------------------

def install(port: int = DEFAULT_PORT) -> tuple[bool, str]:
    if os.name == "nt":
        return _win_install(port)
    if sys.platform == "darwin":
        return _mac_install(port)
    return _linux_install(port)


def uninstall() -> tuple[bool, str]:
    if os.name == "nt":
        return _win_uninstall()
    if sys.platform == "darwin":
        return _mac_uninstall()
    return _linux_uninstall()


def status() -> dict:
    base = {"platform": "windows" if os.name == "nt" else ("launchd" if sys.platform == "darwin" else "systemd"),
            "path": str(unit_path()) if os.name != "nt" else WINDOWS_TASK,
            "command": " ".join(_program(DEFAULT_PORT))}
    if os.name == "nt":
        base.update(_win_status())
    elif sys.platform == "darwin":
        base.update(_mac_status())
    else:
        base.update(_linux_status())
    return base
