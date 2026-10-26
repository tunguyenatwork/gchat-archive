"""Guard the bug class that broke sign-in: a name removed but still referenced.

`OAUTH_SCOPES` was deleted with the scheduler block but stayed referenced in
serve.py, so /oauth/start raised NameError at runtime — no import error, no test
failure, just a dead page. pyflakes catches exactly that statically.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).parents[1] / "src" / "gchat_archive"


def test_no_undefined_names() -> None:
    pytest.importorskip("pyflakes")
    proc = subprocess.run(
        [sys.executable, "-m", "pyflakes", *sorted(str(p) for p in SRC.glob("*.py"))],
        capture_output=True, text=True,
    )
    undefined = [line for line in (proc.stdout or "").splitlines()
                 if "undefined name" in line or "undefined" in line]
    assert not undefined, "undefined names in the package:\n" + "\n".join(undefined)
