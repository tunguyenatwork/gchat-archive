"""A manual-sync command line must survive both platform quoting rules.

Windows splits differently (backslashes are path separators, not escapes), and
a naive split left the quotes on each token, so the child ran `"import time..."`
with quotes as the program text — a SyntaxError and no output. That is exactly
how the Windows CI run failed.
"""
from __future__ import annotations

import pytest

from gchat_archive.serve import split_command


def test_posix_path_with_quoted_argument() -> None:
    parts = split_command('/usr/bin/python3 -c "print(1)"', windows=False)
    assert parts == ["/usr/bin/python3", "-c", "print(1)"]


def test_windows_path_keeps_its_backslashes() -> None:
    parts = split_command(r'C:\Python312\python.exe -c "print(1)"', windows=True)
    assert parts[0] == r"C:\Python312\python.exe"
    assert parts[1] == "-c"
    # the quotes must be gone, or the child treats them as code
    assert parts[2] == "print(1)"


def test_windows_single_quotes_are_not_quoting() -> None:
    # on Windows a single quote is an ordinary character, so it stays
    parts = split_command("python -c \"print('fetching')\"", windows=True)
    assert parts[-1] == "print('fetching')"


@pytest.mark.parametrize("cmd", ['python -c "import time; print(1)"',
                                 '/usr/local/bin/python3 -c "print(2)"'])
def test_round_trip_keeps_argument_count(cmd: str) -> None:
    assert len(split_command(cmd, windows=False)) == 3
