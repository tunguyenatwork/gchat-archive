#!/usr/bin/env python3
"""Password protection for the reader UI.

One password guards the whole local app (it is a single-user tool). Stored as a
PBKDF2 hash, never the password itself. Sessions are opaque random tokens held in
memory, so restarting the server logs everyone out — acceptable for a localhost
tool and it means nothing durable to steal.

Also owns the one-time reset password: hashed, single-use, 30-minute expiry.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path

from . import paths

ROOT = paths.data_home()
AUTH_FILE = ROOT / "auth.json"
ITERATIONS = 240_000
SESSION_TTL = 7 * 24 * 3600
RESET_TTL = 30 * 60

_lock = threading.Lock()
_sessions: dict[str, float] = {}


def _hash(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS)


def _load() -> dict:
    if AUTH_FILE.exists():
        try:
            return json.loads(AUTH_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def _save(data: dict) -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    tmp = AUTH_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.chmod(tmp, 0o600)
    os.replace(tmp, AUTH_FILE)
    try:
        os.chmod(AUTH_FILE, 0o600)
    except OSError:
        pass


def configured() -> bool:
    return bool(_load().get("hash"))


def set_password(password: str) -> None:
    if len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    salt = secrets.token_bytes(16)
    data = _load()
    data.update(
        {
            "salt": salt.hex(),
            "hash": _hash(password, salt).hex(),
            "iterations": ITERATIONS,
            "updated_at": int(time.time()),
        }
    )
    data.pop("must_change", None)
    data.pop("reset", None)
    _save(data)


def verify(password: str) -> bool:
    data = _load()
    if not data.get("hash") or not data.get("salt"):
        return False
    salt = bytes.fromhex(data["salt"])
    expected = bytes.fromhex(data["hash"])
    iterations = int(data.get("iterations") or ITERATIONS)
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(candidate, expected)


def change_password(current: str, new: str) -> None:
    if not verify(current):
        raise ValueError("current password is incorrect")
    set_password(new)


# --- sessions -------------------------------------------------------------
def new_session() -> str:
    token = secrets.token_urlsafe(32)
    with _lock:
        _sessions[token] = time.time() + SESSION_TTL
    return token


def valid_session(token: str | None) -> bool:
    if not token:
        return False
    with _lock:
        expiry = _sessions.get(token)
        if not expiry:
            return False
        if expiry < time.time():
            _sessions.pop(token, None)
            return False
        return True


def drop_session(token: str | None) -> None:
    if token:
        with _lock:
            _sessions.pop(token, None)


# --- reset ----------------------------------------------------------------
def make_reset_code() -> str:
    """Human-typeable temporary password, e.g. cedar-4821."""
    words = ("cedar", "amber", "cobalt", "maple", "quartz", "willow", "onyx", "harbor", "ember", "slate")
    return "%s-%04d" % (secrets.choice(words), secrets.randbelow(10000))


def store_reset(code: str, email: str) -> None:
    data = _load()
    salt = secrets.token_bytes(16)
    data["reset"] = {
        "salt": salt.hex(),
        "hash": _hash(code, salt).hex(),
        "email": email,
        "expires": int(time.time()) + RESET_TTL,
    }
    _save(data)


def consume_reset(code: str) -> bool:
    """True if the code matches and is still valid. Single use."""
    data = _load()
    reset = data.get("reset") or {}
    if not reset or reset.get("expires", 0) < time.time():
        return False
    salt = bytes.fromhex(reset["salt"])
    if not hmac.compare_digest(_hash(code, salt).hex(), reset["hash"]):
        return False
    data.pop("reset", None)
    _save(data)
    return True


def reset_pending_for() -> str | None:
    reset = _load().get("reset") or {}
    if reset.get("expires", 0) >= time.time():
        return reset.get("email")
    return None


def login_with_reset(code: str) -> bool:
    """Accept a mailed one-time password, then adopt it as the password.

    Adopting it means the same code keeps working until the user changes it —
    otherwise a consumed code would lock them out immediately after signing in.
    `must_change` nudges them to replace it.
    """
    if not consume_reset(code):
        return False
    set_password(code)
    data = _load()
    data["must_change"] = True
    _save(data)
    return True
