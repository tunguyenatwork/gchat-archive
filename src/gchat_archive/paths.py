"""Where the archive lives on this machine.

One place decides the data directory so every module agrees, and so a user can
relocate the archive with an environment variable or ``--data-dir`` without
touching code.

Resolution order:

1. ``GCHAT_ARCHIVE_HOME`` (set by ``--data-dir``)
2. the platform default: ``~/.gchat-archive``

Google-native documents are exported through the Drive API, so the OAuth client
secret is read from ``GCHAT_CLIENT_SECRET`` or ``<data>/client_secret.json``.
"""
from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "gchat-archive"
HOME_ENV = "GCHAT_ARCHIVE_HOME"
SECRET_ENV = "GCHAT_CLIENT_SECRET"


def data_home() -> Path:
    """Root of this user's archive (created on demand elsewhere)."""
    raw = os.environ.get(HOME_ENV)
    if raw:
        return Path(raw).expanduser()
    return Path.home() / (".%s" % APP_NAME)


def set_home(path: str | Path) -> Path:
    """Pin the data directory for this process, before modules import."""
    resolved = Path(path).expanduser()
    os.environ[HOME_ENV] = str(resolved)
    return resolved


def client_secret() -> Path:
    """OAuth client JSON for an installed app (Google Cloud console)."""
    raw = os.environ.get(SECRET_ENV)
    if raw:
        return Path(raw).expanduser()
    return data_home() / "client_secret.json"


def web_root() -> Path:
    """Static reader UI, shipped inside the package."""
    return Path(__file__).resolve().parent / "web"
