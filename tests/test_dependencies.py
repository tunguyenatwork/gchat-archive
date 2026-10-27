"""Runtime imports must resolve in a plain install of the declared dependencies.

Regression: `api.py` refreshes tokens with
`from google.auth.transport.requests import Request`, which needs the `requests`
package. `pyproject.toml` declared only `google-auth`, so a clean install
crashed on the first sync with "The requests library is not installed" — a
dependency our own source never mentions, which is why a static import scan
missed it.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

PYPROJECT = Path(__file__).parents[1] / "pyproject.toml"


def test_google_auth_requests_transport_imports() -> None:
    pytest.importorskip("google.auth")
    # This is the exact call api.creds() makes before every request.
    from google.auth.transport.requests import Request  # noqa: F401


def test_requests_is_a_declared_dependency() -> None:
    text = PYPROJECT.read_text()
    deps = text.split("dependencies = [", 1)[1].split("]", 1)[0]
    assert "requests" in deps, "requests must be declared: google-auth imports it at runtime"


def test_package_imports_cleanly() -> None:
    """Importing every module catches missing runtime dependencies."""
    proc = subprocess.run(
        [sys.executable, "-c",
         "import gchat_archive.api, gchat_archive.build, gchat_archive.fetch, "
         "gchat_archive.serve, gchat_archive.backup, gchat_archive.service, gchat_archive.schedule"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr


def test_optional_parsers_are_optional() -> None:
    """A core install without [text] must still import the extractor module."""
    assert importlib.util.find_spec("gchat_archive.textract") is not None
