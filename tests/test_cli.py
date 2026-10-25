"""The CLI must be usable without a Google account (and package data must ship)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from conftest import run


def test_help_lists_the_commands() -> None:
    proc = run("cli", "--help", home=Path("/tmp/unused-help"))
    for command in ("sync", "serve", "login", "build", "accounts", "schedule", "doctor"):
        assert command in proc.stdout


def test_doctor_runs_on_a_bare_install(tmp_path: Path) -> None:
    proc = run("cli", "doctor", home=tmp_path / "empty")
    assert "data dir" in proc.stdout
    assert proc.returncode == 0


def test_schedule_rejects_a_non_daily_expression(tmp_path: Path) -> None:
    proc = run("cli", "schedule", "set", "*/5 * * * *", home=tmp_path / "empty", check=False)
    assert proc.returncode == 1
    assert "daily cron time" in proc.stdout


def test_web_assets_are_packaged() -> None:
    sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
    from gchat_archive import paths

    root = paths.web_root()
    for name in ("index.html", "login.html", "app.js", "styles.css"):
        assert (root / name).is_file(), name
