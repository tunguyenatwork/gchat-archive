"""The version the binary prints must be the version that was installed.

`__version__` was a hand-maintained constant, so a wheel built as 0.2.4 still
answered `gchat-archive --version` with 0.2.3 — the metadata and the program
disagreed, and bug reports would carry the wrong number.
"""
from __future__ import annotations

from importlib.metadata import version

import gchat_archive


def test_version_matches_installed_metadata() -> None:
    assert gchat_archive.__version__ == version("gchat-archive")


def test_version_is_not_the_placeholder() -> None:
    assert gchat_archive.__version__ not in ("", "0.0.0", "unknown")
