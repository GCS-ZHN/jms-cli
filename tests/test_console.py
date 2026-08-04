"""Tests for jms.transport.console — platform mapping and pure helpers.

The real console I/O (termios on POSIX, console API on Windows) needs a
TTY and cannot run in CI, so only the platform mapping, the pure key
mapping helper and idempotent state handling are covered here. The relay
loops themselves are tested with a FakeConsole in the backend tests.
"""

import os

import pytest

from jms.transport.console import (
    LocalConsole,
    PosixConsole,
    _key_to_bytes,
    get_local_console,
)


def test_get_local_console_matches_platform() -> None:
    console = get_local_console()
    assert isinstance(console, LocalConsole)
    if os.name == "nt":
        assert type(console).__name__ == "WindowsConsole"
    else:
        assert isinstance(console, PosixConsole)


@pytest.mark.parametrize("char,state,expected", [
    ("]", 0x0008, b"\x1d"),      # left Ctrl+]
    ("]", 0x0004, b"\x1d"),      # right Ctrl+]
    ("C", 0x0008, b"\x03"),      # Ctrl+C → SIGINT byte (raw semantics)
    ("[", 0x0008, b"\x1b"),      # Ctrl+[ → ESC
    ("a", 0, b"a"),              # plain key passes through
    ("]", 0, b"]"),              # plain ] is not the disconnect key
    ("中", 0, "中".encode("utf-8")),
    ("\x1d", 0, b"\x1d"),        # already a control char passes through
    ("", 0, b""),                # null char / no text
])
def test_key_to_bytes(char: str, state: int, expected: bytes) -> None:
    assert _key_to_bytes(char, state) == expected


def test_posix_console_exit_raw_without_enter_is_noop() -> None:
    """exit_raw must be idempotent and safe before enter_raw."""
    console = PosixConsole()
    console.exit_raw()
    console.exit_raw()


def test_posix_console_isatty_reflects_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import MagicMock
    monkeypatch.setattr("sys.stdin", MagicMock(isatty=lambda: False))
    assert PosixConsole().isatty() is False
