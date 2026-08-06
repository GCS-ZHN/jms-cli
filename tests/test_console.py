"""Tests for jms.transport.console — platform mapping and pure helpers.

The real console I/O (termios on POSIX, console API on Windows) needs a
TTY and cannot run in CI, so only the platform mapping, the pure key
mapping helper and idempotent state handling are covered here. The relay
loops themselves are tested with a FakeConsole in the backend tests.
"""

import codecs
import os
import threading
from unittest.mock import MagicMock

import pytest

from jms.transport.console import (
    LocalConsole,
    PosixConsole,
    WindowsConsole,
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
    monkeypatch.setattr("sys.stdin", MagicMock(isatty=lambda: False))
    assert PosixConsole().isatty() is False


def test_windows_console_isatty_requires_stdin_console() -> None:
    """Only stdin must be a console (stdout may be redirected)."""
    console = WindowsConsole.__new__(WindowsConsole)
    console._in_mode = None
    console._out_mode = 0x0001
    assert console.isatty() is False
    console._in_mode = 0x0001
    assert console.isatty() is True


def test_windows_console_stdin_buffer_and_condition() -> None:
    """wait_stdin/read_stdin expose reader-thread appends (pure logic)."""
    console = WindowsConsole.__new__(WindowsConsole)
    console._buffer = bytearray()
    console._cond = threading.Condition()

    with console._cond:
        console._buffer.extend(b"abc")
        console._cond.notify()
    assert console.wait_stdin(0.1) is True
    assert console.read_stdin() == b"abc"
    assert console.read_stdin() == b""

    # Empty buffer times out without data
    assert console.wait_stdin(0.05) is False


def test_windows_console_write_stdout_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redirected-stdout fallback writes are chunked via os.write."""
    console = WindowsConsole.__new__(WindowsConsole)
    console._h_out = None
    sizes: list[int] = []

    def fake_write(fd: int, data: bytes) -> int:
        sizes.append(len(data))
        return len(data)

    monkeypatch.setattr("jms.transport.console.os.write", fake_write)
    monkeypatch.setattr(
        "jms.transport.console.sys.stdout", MagicMock(fileno=lambda: 7),
    )
    console.write_stdout(b"x" * 70000)
    assert sizes == [32768, 32768, 4464]


def test_windows_console_write_stdout_console_path_uses_write_console_w(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Console output is UTF-8 decoded and written via WriteConsoleW."""
    console = WindowsConsole.__new__(WindowsConsole)
    console._h_out = object()
    console._decoder = codecs.getincrementaldecoder("utf-8")()
    fake_kernel32 = MagicMock()
    fake_kernel32.WriteConsoleW.side_effect = lambda *_: True
    monkeypatch.setattr(
        "jms.transport.console._get_kernel32", lambda: fake_kernel32,
    )

    console.write_stdout(b"hi")
    console.write_stdout(b"\xe4\xb8")  # first half of 中
    console.write_stdout(b"\xad")      # second half

    texts = [
        "".join(call.args[1]) for call in fake_kernel32.WriteConsoleW.call_args_list
    ]
    assert texts == ["hi", "中"]


def test_windows_console_write_console_chunks_at_8192_chars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Large console writes are split into 8192-char chunks."""
    console = WindowsConsole.__new__(WindowsConsole)
    console._h_out = object()
    console._decoder = codecs.getincrementaldecoder("utf-8")()
    fake_kernel32 = MagicMock()
    fake_kernel32.WriteConsoleW.side_effect = lambda *_: True
    monkeypatch.setattr(
        "jms.transport.console._get_kernel32", lambda: fake_kernel32,
    )

    console.write_stdout(b"x" * 20000)
    sizes = [call.args[2] for call in fake_kernel32.WriteConsoleW.call_args_list]
    assert sizes == [8192, 8192, 3616]
